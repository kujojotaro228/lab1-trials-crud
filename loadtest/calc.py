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


def _random_time_L(csv_path: Path, w0: float, w1: float, n: int = 200_000, seed: int = 1):
    """L як середнє N(t) у випадкові (рівномірні) моменти вікна, за журналом запитів.

    Моменти вибираються незалежно від розкладу відправки, тому не «синхронізуються»
    з ним, на відміну від вибірки всередині генератора.
    """
    import bisect
    import csv
    import gzip
    import random
    sent, end = [], []
    with gzip.open(csv_path, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sent.append(float(r["sent_s"]))
            end.append(float(r["end_s"]))
    sent.sort()
    end.sort()
    rnd = random.Random(seed)
    tot = 0
    for _ in range(n):
        t = rnd.uniform(w0, w1)
        tot += bisect.bisect_right(sent, t) - bisect.bisect_right(end, t)
    return tot / n


def _pct(a, b):
    return 100.0 * abs(a - b) / b if b else None


def little_section() -> list[str]:
    out = ["## 2. Перевірка закону Літтла (1 інстанс, 100 rps)", "",
           "λ — фактична інтенсивність відправки у вікні вимірювання, W — середній час від "
           "відправки до відповіді, λ·W — права частина закону. L вимірюється окремо, "
           "спостереженням за кількістю запитів у системі N(t) = відправлені(t) − завершені(t):",
           "",
           "* **L вибірка 2 мс** — генератор під час прогону кожні 2 мс записував N(t) "
           "(незалежне вимірювання, основний результат перевірки);",
           "* **L випадкові моменти** — N(t) у 200 000 рівномірно випадкових моментах вікна, "
           "обчислене після прогону з журналу запитів (`*.csv.gz`); моменти не повʼязані з "
           "розкладом відправки.",
           "",
           "Інтеграл N(t) за моментами відправки і завершення (колонка `L_exact` у JSON) тут не "
           "наводиться як перевірка: він дорівнює сумі часів перебування, поділеній на "
           "тривалість вікна, тобто λ·W за побудовою.", "",
           "| Прогін | λ, rps | W, с | λ·W | L вибірка 2 мс | Розбіжність, % | "
           "L випадкові моменти | Розбіжність, % |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    picked = []
    for p in sorted(RAW_DIR.glob("open_n1_r100_rep*.json")):
        meta = json.loads(p.read_text(encoding="utf-8"))
        s = meta["little"]
        lw, ls = s["lambda_x_W"], s["L_sampled"]
        csv_path = p.parent / (p.stem + ".csv.gz")
        lr = (_random_time_L(csv_path, meta["warmup_s"], meta["warmup_s"] + meta["duration_s"])
              if csv_path.exists() else None)
        d1, d2 = _pct(ls, lw), (_pct(lr, lw) if lr is not None else None)
        picked.append((p.stem, s, ls, d1, lr, d2))
        out.append(f"| {p.stem} | {s['lambda_rps']} | {s['W_s']:.6f} | {lw:.4f} | {ls:.4f} | "
                   f"{fmt(d1, 1)} | {fmt(lr, 4)} | {fmt(d2, 1)} |")
    if not picked:
        return out + ["", "_Немає файлів open_n1_r100_rep*.json_", ""]

    name, s, ls, d1, lr, d2 = sorted(picked, key=lambda x: x[3])[len(picked) // 2]
    lw = s["lambda_x_W"]
    out += ["", f"Підстановка (прогін із медіанною розбіжністю, {name}):", "",
            f"λ·W = {s['lambda_rps']} × {s['W_s']:.6f} = {lw:.4f};  "
            f"виміряне L (вибірка 2 мс) = {ls:.4f};  розбіжність = "
            f"|{ls:.4f} − {lw:.4f}| / {lw:.4f} × 100 % = {fmt(d1, 1)} %", ""]
    if lr is not None:
        out += [f"Для того самого прогону L у випадкові моменти = {lr:.4f}, розбіжність з λ·W "
                f"{fmt(d2, 1)} %.", ""]

    # залежність розбіжності від W на одному інстансі
    rows = []
    for p in sorted(RAW_DIR.glob("open_n1_r*_rep*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        s2 = m["little"]
        rows.append((float(m["rate"]), p.stem, s2["W_s"], s2["lambda_x_W"], s2["L_sampled"],
                     _pct(s2["L_sampled"], s2["lambda_x_W"]), m["generator"]["procs"]))
    rows.sort()
    out += ["Розбіжність вибіркової оцінки на всіх рівнях (1 інстанс):", "",
            "| Прогін | W, мс | Інтервал між запитами одного процесу генератора, мс | "
            "λ·W | L вибірка 2 мс | Розбіжність, % |",
            "|---|---:|---:|---:|---:|---:|"]
    for rate, stem, w, lw2, ls2, d, procs in rows:
        out.append(f"| {stem} | {w * 1000:.2f} | {1000.0 * procs / rate:.1f} | {lw2:.4f} | "
                   f"{ls2:.4f} | {fmt(d, 1)} |")
    out += ["",
            "Розбіжність визначається не законом, а тим, у які моменти генератор брав вибірку. "
            "Потік вибірки і цикл планування запитів у кожному процесі генератора засинають і "
            "прокидаються за тим самим системним таймером. Вибірка тому припадає переважно на "
            "момент перед черговою відправкою, коли попередній запит цього процесу вже "
            "завершився, а наступний ще не відправлено, і N(t) систематично занижується. "
            "Наскільки сильно, залежить від того, як часто момент вибірки збігається з "
            "моментом відправки: на 10 rps розбіжність 4–18 %, на 100 rps 33–40 %, на 500 rps, "
            "де запит триває ≈ 1.2 мс при інтервалі 24 мс між запитами одного процесу, понад "
            "90 %. Коли запит триває довше за кілька кроків вибірки (1000–1500 rps на одному "
            "інстансі, W від 9 мс до 1.2 с), вибірка вже не може його пропустити, і розбіжність "
            "падає до 3–12 %. Вибірка тих самих N(t) "
            "у моменти, не повʼязані з розкладом (колонка «L випадкові моменти»), дає "
            "розбіжність з λ·W менше 1 %, тобто закон Літтла виконується, а 33–40 % на 100 rps — "
            "систематична похибка способу вибірки в генераторі.", ""]
    return out


def closed_explanation(closed_rows, open_rows) -> list[str]:
    out = []
    for c in closed_rows:
        o = open_rows.get((c["instances"], c["rate"]))
        if not o or not c["achieved_rps"]:
            continue
        w_little = c["concurrency"] / c["achieved_rps"]
        out.append(
            f"На {o['rate']:g} rps: закритий контур з C = {c['concurrency']} дав "
            f"{c['achieved_rps']} rps, p95 = {c['p95_ms']} мс, не-2xx {c['non2xx_pct']} %; "
            f"відкритий — {o['achieved_rps']} rps, p95 = {o['p95_ms']} мс, не-2xx "
            f"{o['non2xx_pct']} %. У закритому контурі черга не перевищує C, тому середня "
            f"затримка за законом Літтла обмежена значенням C / X = {c['concurrency']} / "
            f"{c['achieved_rps']} ≈ {w_little:.2f} с. У відкритому контурі запити надходять "
            "за розкладом незалежно від відповідей, надлишок накопичується, і хвіст затримки "
            "росте весь прогін.")
        out.append("")
    return out


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
            "nginx і ті самі ядра хоста (генератор теж працює на цій машині).", ""]

    # ------------------------------------------------------------ 2. Літтл
    out += little_section()

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
        out += ["", "_Немає прогонів закритого контуру (loadtest/run_closed.py)_", ""]
    else:
        out += closed_explanation(closed_rows, open_rows)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "calculations.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    print("\nsaved results/calculations.md")


if __name__ == "__main__":
    main()