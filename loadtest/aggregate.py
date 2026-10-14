#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpjson import RAW_DIR, RESULTS_DIR  # noqa: E402

METRICS = ["sent_rps", "achieved_rps", "p50_ms", "p95_ms", "p99_ms", "non2xx_pct",
           "mean_ms", "send_lag_p99_ms"]


def load_runs() -> list[dict]:
    runs = []
    for p in sorted(RAW_DIR.glob("*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        s["_file"] = p.name
        runs.append(s)
    return runs


def key_of(s: dict) -> tuple:
    if s["mode"] == "open":
        return ("open", s.get("instances_label", 0), float(s["rate"]), 0)
    return ("closed", s.get("instances_label", 0), float(s.get("level") or 0), s["concurrency"])


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 3) if vals else None


def aggregate(runs: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for s in runs:
        groups[key_of(s)].append(s)
    rows = []
    for (mode, inst, level, conc), items in sorted(groups.items()):
        row = {"mode": mode, "instances": inst, "rate": level,
               "concurrency": conc or None, "runs": len(items)}
        for m in METRICS:
            row[m] = _median(s.get(m) for s in items)
        row["L_sampled"] = _median(s["little"]["L_sampled"] for s in items)
        row["L_exact"] = _median(s["little"]["L_exact"] for s in items)
        row["W_s"] = _median(s["little"]["W_s"] for s in items)
        row["lambda_rps"] = _median(s["little"]["lambda_rps"] for s in items)
        inst_counts = defaultdict(int)
        for s in items:
            for k, v in s.get("instances", {}).items():
                inst_counts[k] += v
        row["instances_seen"] = dict(inst_counts)
        rows.append(row)
    return rows


def write_outputs(rows: list[dict], runs: list[dict]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    cols = ["mode", "instances", "rate", "concurrency", "runs"] + METRICS + \
           ["L_sampled", "L_exact", "W_s", "lambda_rps"]
    with open(RESULTS_DIR / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) for c in cols])

    md = ["# Результати навантаження (медіана з повторів)", "",
          "## Відкритий контур, GET /trial-participants/{id}", "",
          "| Інстансів | Задана, rps | Досягнута, rps | p50, мс | p95, мс | p99, мс | "
          "не-2xx, % | Відправлено, rps | Повторів |",
          "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["mode"] == "open":
            md.append(f"| {r['instances']} | {r['rate']:g} | {r['achieved_rps']} | {r['p50_ms']} | "
                      f"{r['p95_ms']} | {r['p99_ms']} | {r['non2xx_pct']} | {r['sent_rps']} | "
                      f"{r['runs']} |")
    closed = [r for r in rows if r["mode"] == "closed"]
    if closed:
        md += ["", "## Закритий контур", "",
               "| Інстансів | Порівнюваний рівень, rps | C (одночасних) | Досягнута, rps | "
               "p50, мс | p95, мс | p99, мс | не-2xx, % |",
               "|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for r in closed:
            md.append(f"| {r['instances']} | {r['rate']:g} | {r['concurrency']} | "
                      f"{r['achieved_rps']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | "
                      f"{r['non2xx_pct']} |")
    md += ["", "## Розподіл відповідей між інстансами (заголовок X-Instance, сума за повтори)", ""]
    for r in rows:
        if r["mode"] == "open" and r["instances_seen"]:
            total = sum(r["instances_seen"].values())
            parts = ", ".join(f"`{k}`: {v} ({100.0 * v / total:.1f}%)"
                              for k, v in sorted(r["instances_seen"].items()))
            md.append(f"- {r['instances']} інст., {r['rate']:g} rps → {parts}")
    (RESULTS_DIR / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    det = ["# Усі прогони", "",
           "| Файл | Режим | Інст. | Рівень | C | Відправлено | Досягнута | p50 | p95 | p99 | "
           "не-2xx % | L | λ·W | Статуси |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for s in runs:
        lvl = s.get("rate") or s.get("level")
        det.append(f"| {s['_file']} | {s['mode']} | {s.get('instances_label')} | {lvl} | "
                   f"{s.get('concurrency') or ''} | {s['sent_rps']} | {s['achieved_rps']} | "
                   f"{s['p50_ms']} | {s['p95_ms']} | {s['p99_ms']} | {s['non2xx_pct']} | "
                   f"{s['little']['L_sampled']} | {s['little']['lambda_x_W']} | "
                   f"{json.dumps(s['statuses'], ensure_ascii=False)} |")
    (RESULTS_DIR / "runs.md").write_text("\n".join(det) + "\n", encoding="utf-8")


def main(argv=None) -> None:
    runs = load_runs()
    if not runs:
        print("results/raw порожня")
        return
    rows = aggregate(runs)
    write_outputs(rows, runs)
    print((RESULTS_DIR / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main(sys.argv[1:])
