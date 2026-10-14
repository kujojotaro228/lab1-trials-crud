#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate  # noqa: E402
import loadgen  # noqa: E402
from httpjson import fetch_ids, wait_healthy  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--instances", type=int, required=True,
                    help="скільки реплік web зараз піднято (мітка для результатів)")
    ap.add_argument("--rates", type=float, nargs="+", default=[10, 100, 500])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--warmup", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--pause", type=float, default=5.0)
    ap.add_argument("--procs", type=int, default=0)
    ap.add_argument("--expect-records", type=int, default=100)
    ap.add_argument("--force", action="store_true",
                    help="продовжити, навіть якщо записів не рівно 100")
    args = ap.parse_args()

    if not wait_healthy(args.base_url, timeout=60):
        sys.exit("сервіс не відповідає 200 на /healthz")
    ids = fetch_ids(args.base_url)
    print(f"у базі {len(ids)} записів")
    if len(ids) != args.expect_records and not args.force:
        sys.exit(f"очікувалось {args.expect_records} записів; запустіть loadtest/seed.py "
                 "(або --force)")

    for rate in args.rates:
        for rep in range(1, args.repeats + 1):
            cfg = loadgen.make_cfg(args.base_url, mode="open", target="item", rate=rate,
                                   duration=args.duration, warmup=args.warmup,
                                   timeout=args.timeout, procs=args.procs, seed=rep)
            summary, records = loadgen.run(cfg, ids)
            summary["instances_label"] = args.instances
            summary["repeat"] = rep
            summary["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            name = f"open_n{args.instances}_r{int(rate)}_rep{rep}"
            loadgen.save_run(summary, records, name)
            print(f"n={args.instances} rep={rep} " + loadgen.format_line(summary))
            seen = len(summary["instances"])
            if seen != args.instances:
                print(f"  УВАГА: відповіді прийшли від {seen} інстансів, "
                      f"а мітка --instances={args.instances}")
            time.sleep(args.pause)

    aggregate.main([])


if __name__ == "__main__":
    main()
