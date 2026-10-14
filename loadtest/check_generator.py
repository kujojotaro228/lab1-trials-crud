#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loadgen  # noqa: E402
from httpjson import RESULTS_DIR  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--rates", type=float, nargs="+", default=[10, 100, 500, 1000, 2000])
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--procs", type=int, default=0)
    args = ap.parse_args()

    rows = []
    for rate in args.rates:
        cfg = loadgen.make_cfg(args.base_url, mode="open", target="noop", rate=rate,
                               duration=args.duration, warmup=args.warmup,
                               timeout=10.0, procs=args.procs)
        s, _ = loadgen.run(cfg, [])
        print(loadgen.format_line(s) + f"  lag_p99={s['send_lag_p99_ms']} ms")
        rows.append(s)
        time.sleep(2)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "generator_check.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    g = rows[0]["generator"]
    lines = [
        "# Перевірка генератора проти /noop (nginx `return 200`)",
        "",
        f"Python {g['python']}, {g['platform']}, логічних CPU: {g['cpu_count']}. "
        f"Прогін {args.duration:g} с + прогрів {args.warmup:g} с.",
        "",
        "| Задана, rps | Процесів | Відправлено, rps | Досягнута (2xx), rps | p50, мс | p99, мс | "
        "Запізнення відправки p99, мс | не-2xx, % |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in rows:
        lines.append(f"| {s['rate']:g} | {s['generator']['procs']} | {s['sent_rps']} | "
                     f"{s['achieved_rps']} | {s['p50_ms']} | "
                     f"{s['p99_ms']} | {s['send_lag_p99_ms']} | {s['non2xx_pct']} |")
    lines += ["",
              "Висновок: генератор тримає задану інтенсивність до рівня, на якому "
              "«Досягнута» ≈ «Задана» і запізнення відправки мале. "
              "Рівні серії (10/100/500) мають бути нижче цієї межі."]
    (RESULTS_DIR / "generator_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nsaved results/generator_check.md")


if __name__ == "__main__":
    main()
