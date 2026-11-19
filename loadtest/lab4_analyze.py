#!/usr/bin/env python3
"""Processing of Lab 4 measurements: results/lab4/*.json + raw/*.csv -> summary.md, summary.json, charts.

    py loadtest/lab4_analyze.py

What is computed (all from the primary sources named in the assignment):
  * steps 3–4: D — share of tasks with more than one row in task_results or more than one
    "done" log line (both sources); claim p99 — the worker histogram
    worker_task_duration_seconds{step="claim"} in Prometheus; empty claims —
    worker_claims_total{result="empty"}; locks — pg_stat_activity, sampled every second;
  * step 6: X(W) — task completions (tasks.finished_at) for each of the last 30 s before
    the queue empties; X(1) compared with 1/(t_io + t_cpu + T_poll/2); C(W), σ, κ, W* (USL);
  * step 7: λ, W_q — from tasks rows of the last 120 s; L_q — mean of the Prometheus tasks_queued series;
  * step 9: chart of tasks_queued / tasks_running (Prometheus) and completions per second
    (tasks.finished_at) with a marker at the moment of scenario F3.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpjson import REPO_ROOT  # noqa: E402
from lab2_analyze import C, _style, usl_fit, usl_x  # noqa: E402

LAB4 = REPO_ROOT / "results" / "lab4"
RAW = LAB4 / "raw"
FIG = LAB4 / "fig"
T_POLL_S = 0.010
R = 4


def load(name):
    p = LAB4 / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def tasks_csv(name):
    p = RAW / f"{name}_tasks.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def f(x):
    return None if x in (None, "") else float(x)


def pct(vals, p):
    v = sorted(x for x in vals if x is not None)
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)] if v else None


def fmt(x, nd=2, scale=1.0, dash="—"):
    return dash if x is None else f"{x * scale:.{nd}f}"


def prom_series(name, key):
    p = RAW / f"{name}_prom.json"
    if not p.exists():
        return []
    s = json.loads(p.read_text(encoding="utf-8"))["series"].get(key) or []
    if isinstance(s, dict) or not s:
        return []
    return [(float(t), float(v)) for t, v in s[0]["values"]]


def iso_epoch(x: str) -> float:
    import datetime as dt
    return dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()


def watch_exits(name: str) -> list[dict]:
    """Container exits from raw/<name>_pods_watch.tsv (kubectl get pods -w)."""
    from lab4 import parse_pod_watch
    p = RAW / f"{name}_pods_watch.tsv"
    if not p.exists():
        return []
    return parse_pod_watch(p.read_text(encoding="utf-8").splitlines())


def crash_gaps(exits: list[dict]) -> dict:
    per: dict = {}
    for e in exits:
        per.setdefault(e["pod"], []).append(iso_epoch(e["finishedAt"]))
    return {pod.rsplit("-", 1)[-1]: [round(b - a) for a, b in zip(sorted(ts), sorted(ts)[1:])]
            for pod, ts in sorted(per.items())}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    FIG.mkdir(parents=True, exist_ok=True)
    out, md = {}, ["# ЛР 4: зведення вимірювань", ""]

    # ---------------------------------------------------------------- step 2
    s2 = load("step2")
    if s2:
        out["step2"] = s2
        md += ["## Крок 2: t_io і t_cpu (20 задач, 1 воркер)", "",
               f"* fetch = {s2['fetch_ms']:.2f} мс, store = {s2['store_ms']:.2f} мс → "
               f"**t_io = {s2['t_io_ms']:.2f} мс**",
               f"* **t_cpu = {s2['t_cpu_ms']:.2f} мс** (обробка зрізу)",
               f"* захоплення: {s2['claim_ms']:.2f} мс", ""]

    # ---------------------------------------------------------------- steps 3–4
    s3, s4 = load("step3"), load("step4")
    if s3 or s4:
        md += ["## Кроки 3–4: частка дублювань D", "",
               "| Захоплення | T_poll, мс | n | Рядків результатів | n_dup (таблиця) | n_dup (журнали) | D |",
               "|---|---|---|---|---|---|---|"]
        for poll, d in (s3 or {}).items():
            md.append(f"| наївне | {poll} | {d['n']} | {d['results_rows']} | {d['n_dup_db']} | "
                      f"{d['n_dup_log']} | {d['D']:.4f} |")
        for mode, d in (s4 or {}).items():
            md.append(f"| {mode} | 1 | {d['n']} | {d['results_rows']} | {d['n_dup_db']} | "
                      f"{d['n_dup_log']} | {d['D']:.4f} |")
        md.append("")
    if s4:
        md += ["| Захоплення | p99 захоплення, мс (Prometheus / /metrics) | Порожніх захоплень (/metrics, за серію) "
               "| Порожніх (Prometheus increase) | Макс. рядків Lock |", "|---|---|---|---|---|"]
        for mode, d in s4.items():
            dr = d.get("direct") or {}
            md.append(f"| {mode} | {fmt(d['claim_p99_s'], 2, 1000)} / {fmt(dr.get('claim_p99_s'), 2, 1000)} | "
                      f"{fmt(dr.get('claims_empty'), 0)} | {fmt(d['claims_empty'], 0)} | {d['lock_wait_max']} |")
        md.append("")
        out["step4"] = {m: {k: v for k, v in d.items() if k != "per_sec"} for m, d in s4.items()}
    if s3:
        out["step3"] = s3

    # ---------------------------------------------------------------- step 5
    s5 = load("step5")
    if s5:
        md += ["## Крок 5: зупинка воркера на 2·L", "",
               f"* задача {s5['task_id']}: до — attempts {s5['before'][1]}, token {s5['before'][2]}; "
               f"після — {s5['after'][0]}, attempts {s5['after'][1]}, token {s5['after'][2]}, "
               f"завершив {s5['after'][3]}",
               f"* рядків результату: {s5['results_rows']}; рядків журналу з нулем оновлених рядків: "
               f"{len(s5['zero_rows_lines'])}", ""]
        out["step5"] = s5

    # ---------------------------------------------------------------- step 6
    s6 = load("step6")
    if s6:
        pts = {int(w): d["X"] for w, d in s6.items()}
        fit = usl_fit(pts)
        x1_model = None
        if s2:
            x1_model = 1.0 / ((s2["t_io_ms"] + s2["t_cpu_ms"]) / 1000.0 + T_POLL_S / 2)
            x1_model_claim = 1.0 / ((s2["t_io_ms"] + s2["t_cpu_ms"] + s2["claim_ms"]) / 1000.0)
        md += ["## Крок 6: X(W) і USL", "",
               "| W | X(W), задач/с | Розкид | C(W) = X(W)/X(1) | p99 захоплення, мс | Транзакцій/с | CPU бази, ядер |",
               "|---|---|---|---|---|---|---|"]
        x1 = pts[1]
        for w, d in sorted(s6.items(), key=lambda kv: int(kv[0])):
            md.append(f"| {w} | {d['X']:.2f} | {d['sd']:.2f} | {d['X'] / x1:.3f} | "
                      f"{fmt(d['claim_p99_s'], 2, 1000)} | {fmt(d['xact_per_s'], 1)} | "
                      f"{fmt(d['db_cpu_cores'], 3)} |")
        md.append("")
        if x1_model:
            md.append(f"* 1/(t_io + t_cpu + T_poll/2) = {x1_model:.2f} задач/с; виміряне X(1) = "
                      f"{x1:.2f} ({(x1 / x1_model - 1) * 100:+.1f} %); з урахуванням часу "
                      f"захоплення 1/(t_io + t_cpu + t_claim) = {x1_model_claim:.2f}")
        md.append(f"* USL: σ = {fmt(fit.get('sigma'), 4)}, κ = {fmt(fit.get('kappa'), 5)}, "
                  f"W* = {fmt(fit.get('N_star'), 1)} {fit.get('note', '')}")
        md.append("")
        out["step6"] = {"X": pts, "usl": fit, "x1_model": x1_model}
        fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=150)
        ws = sorted(pts)
        ax.errorbar(ws, [pts[w] for w in ws], yerr=[s6[str(w)]["sd"] for w in ws], fmt="o",
                    color=C[0], capsize=3, label="виміряно X(W)")
        if fit.get("sigma") is not None:
            xs = [1 + i * 0.05 for i in range(int((max(ws) * 2 - 1) / 0.05) + 1)]
            ax.plot(xs, [usl_x(x, x1, fit["sigma"], fit["kappa"]) for x in xs], color=C[1],
                    label=f"USL σ={fit['sigma']:.3f}, κ={fit['kappa']:.4f}")
        ax.plot(ws, [x1 * w for w in ws], "--", color=C[2], linewidth=1, label="лінійне W·X(1)")
        _style(ax, "Пропускна здатність пулу воркерів X(W)", "задач/с")
        ax.set_xlabel("W, воркерів", fontsize=8)
        ax.legend(fontsize=7, frameon=False)
        fig.tight_layout()
        fig.savefig(FIG / "step6_usl.png")
        plt.close(fig)

    # ---------------------------------------------------------------- step 7
    s7 = load("step7")
    if s7:
        lo, hi = s7["low"], s7["high"]
        # reference: the exact time average of the number of queued tasks over the same window,
        # from the task rows (the tasks_queued gauge is a snapshot every 5 s: 24 points)
        rows = tasks_csv("step7_low")
        w0, w1 = lo["window"]
        lq_rows = sum(max(0.0, min(f(t["started"]), w1) - max(f(t["created"]), w0))
                      for t in rows if t["started"]) / (w1 - w0)
        samples = [float(x) for x in lo.get("tasks_queued_samples", [])]
        if len(samples) > 1:
            se = statistics.stdev(samples) / math.sqrt(len(samples))
        else:
            se = None
        lo["L_q_rows"], lo["L_q_se"], lo["n_samples"] = lq_rows, se, len(samples)
        md += ["## Крок 7: закон Літтла", "",
               f"* 0.7·X(R) = {lo['rate']} задач/с, вікно останніх 120 с: λ = {lo['lambda']:.3f} задач/с, "
               f"W_q = {lo['W_q_s'] * 1000:.2f} мс, λ·W_q = {lo['lambda_W_q']:.4f}, "
               f"L_q = {fmt(lo['L_q'], 4)}, розбіжність {fmt(lo['discrepancy_pct'], 1)} %",
               f"* точне середнє кількості задач queued за рядками tasks у тому самому вікні: {lq_rows:.4f}",
               f"* 1.3·X(R) = {hi['rate']} задач/с: черга наприкінці {hi['queue_at_end']}, "
               f"W_q перших 100 = {hi['W_q_first100_s']:.3f} с, останніх 100 = {hi['W_q_last100_s']:.3f} с",
               ""]
        out["step7"] = s7
        fig, ax = plt.subplots(figsize=(6.4, 3.2), dpi=150)
        for i, lab in enumerate(("low", "high")):
            ser = prom_series(f"step7_{lab}", "tasks_queued")
            if ser:
                t0 = ser[0][0]
                ax.plot([t - t0 for t, _ in ser], [v for _, v in ser], color=C[i],
                        label=f"tasks_queued, {s7[lab]['rate']} задач/с")
        _style(ax, "Крок 7: довжина черги (Prometheus tasks_queued)", "задач")
        ax.set_xlabel("с від початку експорту", fontsize=8)
        ax.legend(fontsize=7, frameon=False)
        fig.tight_layout()
        fig.savefig(FIG / "step7_queue.png")
        plt.close(fig)

    # ---------------------------------------------------------------- step 8
    s8 = load("step8")
    if s8:
        ex8 = watch_exits("step8")
        s8["exits"] = ex8
        md += ["## Крок 8: rollout restart", "",
               f"* коди виходу контейнерів (kubectl get pods -w): "
               f"{[(e['pod'], e['exitCode'], e['reason']) for e in ex8]}",
               f"* повернуто в чергу: {s8['returned']}, failed: {s8['failed']}, "
               f"> 1 результату: {s8['multi_result']}, attempts > 1: {s8['attempts_gt1']}", ""]
        out["step8"] = s8

    # ---------------------------------------------------------------- step 9 (+ 9b)
    def timeline(prefix: str, d9: dict, fname: str, title: str, scen_label: str):
        fig, axes = plt.subplots(2, 1, figsize=(7.4, 6.0), dpi=150)
        for ax, lab in zip(axes, ("nopoison", "poison")):
            d = d9.get(lab)
            if not d:
                continue
            ts = tasks_csv(f"{prefix}_{lab}")
            t0 = min(f(t["created"]) for t in ts)
            for i, key in enumerate(("tasks_queued", "tasks_running")):
                # the gauge is refreshed every 5 s: points before the first task still show the
                # previous run (its queue was truncated by the clean start)
                ser = [(t, v) for t, v in prom_series(f"{prefix}_{lab}", key) if t >= t0 + 5]
                ax.plot([t - t0 for t, _ in ser], [v for _, v in ser], color=C[i], label=key)
            ax2 = ax.twinx()
            fin = Counter(int(f(t["finished"]) - t0) for t in ts if t["finished"] and t["status"] == "done")
            n = max(fin) + 1 if fin else 0
            ax2.bar(range(n), [fin.get(i, 0) for i in range(n)], width=1.0, color=C[2], alpha=0.5,
                    label="завершено done за секунду (права вісь)")
            ax2.set_ylabel("задач/с", fontsize=7)
            ax2.tick_params(labelsize=7)
            crashes = [e for e in watch_exits(f"{prefix}_{lab}") if e["exitCode"] != 0]
            for j, e in enumerate(crashes):
                ax.axvline(iso_epoch(e["finishedAt"]) - t0, ymin=0, ymax=0.06, color=C[3], linewidth=1,
                           label="падіння контейнера (exitCode 1)" if j == 0 else None)
            if d.get("t_scenario"):
                ax.axvline(d["t_scenario"] - t0, color=C[4], linestyle="--", linewidth=1.2,
                           label=scen_label + (": Ф3, 10 задач seed = 0" if lab == "poison" else ": без сценарію"))
            _style(ax, f"{title}, {'з отруєними задачами (Ф3)' if lab == 'poison' else 'без отруєних задач'}",
                   "задач у черзі / в обробці")
            ax.set_xlabel("с від першої задачі", fontsize=8)
            h1, l1 = ax.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
            ax.legend(h1 + h2, l1 + l2, fontsize=6, frameon=False, loc="upper left")
        fig.tight_layout()
        fig.savefig(FIG / fname)
        plt.close(fig)

    def step9_table(prefix: str, d9: dict, title: str):
        nonlocal md
        md += [f"## {title}", "",
               "| Прогін | RESTARTS | Падінь (pods -w) | attempts>1 / f | failed / f³ | >1 результату | "
               "X, задач/с | p50 / p99 очікування, с |",
               "|---|---|---|---|---|---|---|---|"]
        for lab in ("nopoison", "poison"):
            d = d9.get(lab)
            if not d:
                continue
            crashes = [e for e in watch_exits(f"{prefix}_{lab}") if e["exitCode"] != 0]
            d["crashes"] = len(crashes)
            d["crash_timeline"] = crash_gaps(crashes)
            md.append(f"| {lab} | {d['restarts_total']} | {len(crashes)} | "
                      f"{fmt(d['share_attempts_gt1'], 4)} / 0.15 | {fmt(d['share_failed'], 5)} / "
                      f"{0.15 ** 3:.5f} | {d['multi_result']} | {d['X_done_per_s']:.2f} | "
                      f"{fmt(d['wait_p50_s'], 2)} / {fmt(d['wait_p99_s'], 2)} |")
        md.append("")
        for lab in ("nopoison", "poison"):
            d = d9.get(lab)
            if d and d.get("crash_timeline"):
                md.append(f"* паузи між падіннями ({lab}), с: " + "; ".join(
                    f"{p}: {g}" for p, g in d["crash_timeline"].items()))
        md.append("")
        if d9.get("poison", {}).get("poison"):
            md += ["Отруєні задачі (seed = 0):", "", "| id | status | attempts | створення → failed, с |",
                   "|---|---|---|---|"]
            for p in d9["poison"]["poison"]:
                md.append(f"| {p['id']} | {p['status']} | {p['attempts']} | {fmt(p['created_to_final_s'], 2)} |")
            md.append("")

    s9 = load("step9")
    if s9:
        step9_table("step9", s9, "Крок 9 (основний прогін): FAIL_RATE = 0.15, 0.7·X(R), 300 с")
        out["step9"] = s9
        timeline("step9", s9, "step9_timeline.png", "Крок 9, основний прогін", "150 с")
    s9b = load("step9b")
    if s9b:
        step9_table("step9b", s9b, "Крок 9 (додатковий прогін): FAIL_RATE = 0.15, 120 задач за 50 с")
        out["step9b"] = s9b
        timeline("step9b", s9b, "step9b_timeline.png", "Крок 9, додатковий прогін", "25 с")

    # ---------------------------------------------------------------- D6
    d6 = load("d6")
    if d6:
        md += ["## Д6: скасування", "",
               f"* скасовано під час обробки: {len(d6['cancelled_running'])}, у черзі: "
               f"{len(d6['cancelled_queued'])}; коди DELETE {d6['codes']}",
               f"* від запиту до зупинки: p50 = {fmt(d6['latency_p50_s'], 1, 1000)} мс, "
               f"p99 = {fmt(d6['latency_p99_s'], 1, 1000)} мс",
               f"* статуси скасованих {d6['statuses_cancelled']}, результатів у них "
               f"{d6['results_for_cancelled']}", ""]
        out["d6"] = d6

    (LAB4 / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (LAB4 / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                       encoding="utf-8")
    print("\n".join(md))
    print(f"-> {(LAB4 / 'summary.md').relative_to(REPO_ROOT)}, fig/*.png")


if __name__ == "__main__":
    main()
