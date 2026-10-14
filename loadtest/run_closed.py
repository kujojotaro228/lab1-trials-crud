#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate  # noqa: E402
import loadgen  # noqa: E402
from httpjson import RAW_DIR, fetch_ids, wait_healthy  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--instances", type=int, default=1)
    ap.add_argument("--level", type=int, default=500,
                    help="рівень відкритого контуру (rps), з яким порівнюємо")
    ap.add_argument("--concurrency", type=int, default=0)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--warmup", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    open_runs = sorted(RAW_DIR.glob(f"open_n{args.instances}_r{args.level}_rep*.json"))
    open_sum = [json.loads(p.read_text(encoding="utf-8")) for p in open_runs]
    conc = args.concurrency
    if not conc:
        if not open_sum:
            sys.exit(f"немає результатів open_n{args.instances}_r{args.level}_rep*.json; "
                     "спершу run_series.py або задайте --concurrency")
        l_med = statistics.median(s["little"]["L_sampled"] for s in open_sum)
        conc = max(1, round(l_med))
        print(f"L у відкритому контурі на {args.level} rps (медіана): {l_med:.2f} → C = {conc}")

    if not wait_healthy(args.base_url, timeout=60):
        sys.exit("сервіс не відповідає 200 на /healthz")
    ids = fetch_ids(args.base_url)

    for rep in range(1, args.repeats + 1):
        cfg = loadgen.make_cfg(args.base_url, mode="closed", target="item",
                               concurrency=conc, duration=args.duration,
                               warmup=args.warmup, timeout=args.timeout, seed=rep)
        summary, records = loadgen.run(cfg, ids)
        summary["instances_label"] = args.instances
        summary["level"] = args.level
        summary["repeat"] = rep
        summary["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        name = f"closed_n{args.instances}_l{args.level}_c{conc}_rep{rep}"
        loadgen.save_run(summary, records, name)
        print(loadgen.format_line(summary))

    if open_sum:
        print(f"\nвідкритий контур, {args.level} rps (медіани): "
              f"achieved={statistics.median(s['achieved_rps'] for s in open_sum):.1f} rps, "
              f"p95={statistics.median(s['p95_ms'] for s in open_sum):.1f} ms")
    aggregate.main([])


if __name__ == "__main__":
    main()
