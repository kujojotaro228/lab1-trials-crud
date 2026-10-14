#!/usr/bin/env python3

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate  # noqa: E402
from httpjson import RAW_DIR, RESULTS_DIR  # noqa: E402


def fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def is_saturated(r: dict) -> bool:
    return (r["achieved_rps"] is not None and r["achieved_rps"] < 0.95 * r["rate"]) \
        or (r["non2xx_pct"] or 0) > 1.0


def main() -> None:
    runs = aggregate.load_runs()
    rows = aggregate.aggregate(runs)
    open_rows = {(r["instances"], r["rate"]): r for r in rows if r["mode"] == "open"}
    out = ["# Розрахунки (етап 7)", "",
           "Усі числа — медіани з повторів (results/summary.json); сирі прогони — results/raw/.", ""]

    # ------------------------------------------------------------ насичення
    out += ["## 0. На якому рівні сервіс перестав тримати задану інтенсивність", "",
            "Критерій: досягнута < 95 % від заданої або частка не-2xx > 1 %.", "",
            "| Інстансів | Задана, rps | Досягнута, rps | Досягнута/задана | p95, мс | p99, мс | не-2xx, % | Тримає? |",
            "|---:|---:|---:|---:|---:|---:|---:|---|"]
    for (n, rate), r in sorted(open_rows.items()):
        ratio = r["achieved_rps"] / rate if r["achieved_rps"] is not None else None
        out.append(f"| {n} | {rate:g} | {r['achieved_rps']} | {fmt(ratio, 3)} | {r['p95_ms']} | "
                   f"{r['p99_ms']} | {r['non2xx_pct']} | {'НІ' if is_saturated(r) else 'так'} |")
    out.append("")

    # ------------------------------------------------------------ 1. S і e
    out += ["## 1. Прискорення і метрика Карпа-Флатта", "",
            "S = X₂ / X₁ (досягнута пропускна здатність на 2 і на 1 інстансі на тому самому рівні),",
            "e = (1/S − 1/N) / (1 − 1/N), N = 2  ⇒  e = (1/S − 0.5) / 0.5", "",
            "| Рівень, rps | X₁, rps | X₂, rps | S | e | Примітка |",
            "|---:|---:|---:|---:|---:|---|"]
    for (n, rate), r1 in sorted(open_rows.items()):
        if n != 1 or (2, rate) not in open_rows:
            continue
        r2 = open_rows[(2, rate)]
        x1, x2 = r1["achieved_rps"], r2["achieved_rps"]
        if not x1 or not x2:
            continue
        s = x2 / x1
        e = (1 / s - 0.5) / 0.5
        if not is_saturated(r1):
            note = "1 інстанс НЕ насичений: обидва віддають задану інтенсивність, S≈1 — " \
                   "це не міра масштабованості, e тут не інтерпретується"
        elif x1 >= 0.95 * rate:
            note = (f"1 інстанс на межі: досягнута {x1:.1f} ≈ задана, але є помилки/затримка; "
                    "обидві конфігурації віддають майже задану інтенсивність, тому S≈1 і e "
                    "тут не характеризують масштабованість")
        elif not is_saturated(r2):
            note = (f"1 інстанс насичений, 2 — НІ (X₂ ≈ заданим {rate:g} rps): "
                    f"S = {x2:.1f} / {x1:.1f} = {s:.3f}; e = (1/{s:.3f} − 0.5)/0.5 = {e:.3f}. "
                    "X₂ обмежене поданим навантаженням, а не можливостями двох інстансів, "
                    "тому S — оцінка знизу для цієї пари чисел")
        elif e < 0:
            note = "S > 2 (надлінійно) — ймовірно, шум або вплив кешів/конкуренції за ядра"
        else:
            note = (f"1 інстанс насичений: S = {x2:.1f} / {x1:.1f} = {s:.3f}; "
                    f"e = (1/{s:.3f} − 0.5)/0.5 = {e:.3f} → ≈{100 * e:.0f} % роботи "
                    "не отримали виграшу від другого інстансу")
        out.append(f"| {rate:g} | {x1} | {x2} | {s:.3f} | {e:.3f} | {note} |")
    out += ["",
            "Спільні для обох інстансів складові стенда: одна база PostgreSQL, один балансувальник "
            "nginx і ті самі ядра хоста (генератор теж працює на цій машині). "
            "Яка з них обмежила — див. `docker stats` під час прогону (впишіть у звіт).", ""]

    # ------------------------------------------------------------ 2. Літтл
    out += ["## 2. Перевірка закону Літтла (1 інстанс, 100 rps)", "",
            "L = λ · W. λ — фактична інтенсивність відправки у вікні вимірювання; "
            "W — середній час від відправки до відповіді; L — середня кількість запитів у системі "
            "(відправлені − завершені). Генератор знає моменти відправки і завершення кожного "
            "запиту, тому N(t) = відправлені(t) − завершені(t) відомо в кожен момент часу; "
            "L = (1/T)·∫N(t)dt за вікно вимірювання (без прогріву). Додатково наведено оцінку L "
            "за вибіркою N(t) кожні 2 мс.", "",
            "| Прогін | λ, rps | W, с | λ·W | L (середнє N(t) за часом) | Розбіжність, % | "
            "L (вибірка кожні 2 мс) |",
            "|---|---:|---:|---:|---:|---:|---:|"]
    little_runs = sorted(RAW_DIR.glob("open_n1_r100_rep*.json"))
    picked = []
    for p in little_runs:
        s = json.loads(p.read_text(encoding="utf-8"))["little"]
        lw, le = s["lambda_x_W"], s["L_exact"]
        d = 100.0 * abs(le - lw) / le if le else None
        picked.append((d, s))
        out.append(f"| {p.stem} | {s['lambda_rps']} | {s['W_s']:.6f} | {lw:.4f} | {le:.4f} | "
                   f"{fmt(d, 3)} | {s['L_sampled']:.4f} |")
    if picked:
        valid = sorted((d for d, _ in picked if d is not None))
        med = statistics.median(valid) if valid else 0
        d, s = min(picked, key=lambda x: abs((x[0] or 0) - med))
        out += ["", "Підстановка (прогін із медіанною розбіжністю):", "",
                f"λ·W = {s['lambda_rps']} × {s['W_s']:.6f} = {s['lambda_x_W']:.4f};  "
                f"виміряне L = {s['L_exact']:.4f};  розбіжність = "
                f"|{s['L_exact']:.4f} − {s['lambda_x_W']:.4f}| / {s['L_exact']:.4f} × 100 % = "
                f"{fmt(d, 3)} %", ""]
        if s["L_sampled"] < 0.8 * s["L_exact"]:
            out += [
                "Оцінка за вибіркою кожні 2 мс вийшла систематично меншою "
                f"({s['L_sampled']:.4f}). Причина — у методі вибірки, а не в законі: запит "
                f"триває в середньому {s['W_s'] * 1000:.1f} мс, тобто стільки ж, скільки крок вибірки, а "
                "потік вибірки і цикл планування запитів прокидаються від того самого системного "
                "таймера ОС. Вибірка «синхронізується» з моментами відправки і систематично "
                "пропускає короткі запити. Під перевантаженням (1500 rps), коли запити тривають "
                "секунди, обидві оцінки близькі (див. колонки L і λ·W у results/runs.md).", ""]
    else:
        out += ["", "_Немає файлів open_n1_r100_rep*.json_", ""]

    # ------------------------------------------------------------ 3. закритий
    out += ["## 3. Закритий контур проти відкритого", "",
            "| Контур | Інстансів | Рівень/C | Досягнута, rps | p95, мс | p99, мс | не-2xx, % |",
            "|---|---:|---|---:|---:|---:|---:|"]
    closed_rows = [r for r in rows if r["mode"] == "closed"]
    for c in closed_rows:
        o = open_rows.get((c["instances"], c["rate"]))
        if o:
            out.append(f"| відкритий | {o['instances']} | {o['rate']:g} rps | {o['achieved_rps']} | "
                       f"{o['p95_ms']} | {o['p99_ms']} | {o['non2xx_pct']} |")
        out.append(f"| закритий | {c['instances']} | C = {c['concurrency']} | {c['achieved_rps']} | "
                   f"{c['p95_ms']} | {c['p99_ms']} | {c['non2xx_pct']} |")
        if o:
            out += ["", f"C = {c['concurrency']} обрано рівним середній кількості запитів у системі "
                        f"у відкритому контурі на {o['rate']:g} rps (L = {o['L_sampled']}).", ""]
    if not closed_rows:
        out += ["", "_Ще немає прогону закритого контуру (loadtest/run_closed.py)_", ""]
    

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "calculations.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    print("\nsaved results/calculations.md")


if __name__ == "__main__":
    main()