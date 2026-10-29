#!/usr/bin/env python3
"""Аналіз вимірювань ЛР 3: X(N), дисбаланс I, USL кластера проти Compose,
профіль зі стрибком без HPA і з HPA (метрики еластичності), Д3 (NodePort / Ingress).

    py loadtest/lab3_analyze.py

Вхід: results/lab3/raw/*.json (генератор), results/lab3/prom/*.json.gz (сирі зразки
Prometheus), results/lab2/raw/step_n*.json (Compose, ЛР 2).
Вихід: results/lab3/analysis.md, analysis.json, charts/*.png.
"""
from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpjson import REPO_ROOT, RESOURCE  # noqa: E402
from lab2_analyze import (C, GRID, INK, INK2, SURF, _style, fmt_bucket,  # noqa: E402
                          fmt_err, fmt_p99s, quantile_from_buckets, usl_fit, usl_x)

LAB3 = REPO_ROOT / "results" / "lab3"
RAW, PROM, CHARTS = LAB3 / "raw", LAB3 / "prom", LAB3 / "charts"
LAB2_RAW = REPO_ROOT / "results" / "lab2" / "raw"
ITEM_PATH = RESOURCE + "/{participant_id}"
R = 4
REQ_CPU = 0.25                       # requests.cpu = 250m


# --------------------------------------------------------------------------
# Сирі зразки Prometheus (ключ ряду — мітка pod)
# --------------------------------------------------------------------------
class Prom:
    def __init__(self, path: Path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            self.d = json.load(f)

    def series(self, metric: str, **match) -> list[dict]:
        return [s for s in self.d["series"].get(metric, [])
                if all(s["labels"].get(k) == v for k, v in match.items())]

    @staticmethod
    def increase(values, a, b) -> float:
        before = [v for t, v in values if t <= a]
        inside = [v for t, v in values if a < t <= b]
        if not inside:
            return 0.0
        prev = before[-1] if before else inside[0]
        inc = 0.0
        for v in inside:
            inc += v - prev if v >= prev else v
            prev = v
        return inc

    @staticmethod
    def span(values, a, b) -> float:
        before = [t for t, _ in values if t <= a]
        inside = [t for t, _ in values if a < t <= b]
        if not inside:
            return 0.0
        return inside[-1] - (before[-1] if before else inside[0])

    def per_pod_rate(self, metric, a, b, **match) -> dict:
        inc, sp = defaultdict(float), {}
        for s in self.series(metric, **match):
            pod = s["labels"].get("pod")
            if not pod:
                continue
            inc[pod] += self.increase(s["values"], a, b)
            sp[pod] = max(sp.get(pod, 0.0), self.span(s["values"], a, b))
        return {p: inc[p] / sp[p] for p in inc if sp.get(p)}

    def per_pod_increase(self, metric, a, b, **match) -> dict:
        out = defaultdict(float)
        for s in self.series(metric, **match):
            pod = s["labels"].get("pod")
            if pod:
                out[pod] += self.increase(s["values"], a, b)
        return dict(out)

    def item_rps(self, a, b) -> dict:
        return self.per_pod_rate("http_requests_total", a, b, path=ITEM_PATH)

    def throttle(self, a, b) -> dict:
        per = self.per_pod_increase("container_cpu_cfs_periods_total", a, b)
        thr = self.per_pod_increase("container_cpu_cfs_throttled_periods_total", a, b)
        return {p: (thr.get(p, 0.0) / v if v else None) for p, v in per.items()}

    def cpu(self, a, b) -> dict:
        return self.per_pod_rate("container_cpu_usage_seconds_total", a, b)

    def p99(self, a, b, pod: str | None = None):
        acc = defaultdict(float)
        for s in self.series("http_request_duration_seconds_bucket", path=ITEM_PATH):
            if pod and s["labels"].get("pod") != pod:
                continue
            acc[float(s["labels"]["le"])] += self.increase(s["values"], a, b)
        return quantile_from_buckets(sorted(acc.items()), 0.99)

    def gauge_steps(self, metric, **match) -> list[tuple[float, float]]:
        """Ряд-сходинка (між зборами значення стале); кілька рядів — сума за часом."""
        ss = self.series(metric, **match)
        if not ss:
            return []
        if len(ss) == 1:
            return [(t, v) for t, v in ss[0]["values"]]
        times = sorted({t for s in ss for t, _ in s["values"]})
        out = []
        for t in times:
            out.append((t, sum(next((v for tt, v in reversed(s["values"]) if tt <= t), 0.0)
                               for s in ss)))
        return out


def value_at(steps, t, default=None):
    v = default
    for tt, vv in steps:
        if tt <= t:
            v = vv
        else:
            break
    return v


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def load(name):
    p = RAW / f"{name}.json"
    if not p.exists():
        return None, None
    run = json.loads(p.read_text(encoding="utf-8"))
    pp = PROM / f"{name}.json.gz"
    return run, (Prom(pp) if pp.exists() else None)


def win(run, st):
    t0 = run["t0_epoch"]
    return t0 + st["window_s"][0], t0 + st["window_s"][1]


def f1(x, nd=1, pct=False):
    if x is None:
        return "—"
    return f"{100 * x:.{nd}f}" if pct else f"{x:.{nd}f}"


# --------------------------------------------------------------------------
# Діаграми
# --------------------------------------------------------------------------
def plt_():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def chart_pods_rps(run, prom, path):
    plt = plt_()
    t0 = run["t0_epoch"]
    grid = list(range(15, int(run["t_end_epoch"] - t0) + 1, 5))
    vals = [prom.item_rps(t0 + g - 15, t0 + g) for g in grid]
    pods = sorted({p for v in vals for p in v})
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True, facecolor=SURF)
    secs = run["seconds"]
    axes[0].step([s["t_s"] for s in secs], [s["target_rps"] for s in secs], where="post",
                 color=INK2, lw=1.5, label="задана")
    axes[0].plot([s["t_s"] for s in secs], [s["achieved_rps"] for s in secs], color=C[0], lw=2,
                 label="досягнута (2xx)")
    _style(axes[0], f"{run['name']}: задана і досягнута інтенсивність", "запитів/с")
    axes[0].legend(frameon=False, fontsize=8)
    for j, p in enumerate(pods):
        axes[1].plot(grid, [v.get(p) for v in vals], color=C[j % len(C)], lw=1.8, label=p)
    _style(axes[1], "Запитів за секунду на кожен под (Prometheus, вікно 15 с)", "запитів/с")
    axes[1].legend(frameon=False, fontsize=8, ncol=2)
    axes[1].set_xlabel("час від початку прогону, с", fontsize=8, color=INK2)
    for ax in axes:
        for st in run["steps"]:
            ax.axvline(st["t_start_s"], color=GRID, lw=0.5)
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=SURF)
    plt.close(fig)


def chart_c(compose, k8s, path):
    plt = plt_()
    fig, ax = plt.subplots(figsize=(7.5, 4.8), facecolor=SURF)
    ns = [i / 10 for i in range(10, 61)]
    ax.plot(ns, ns, color=INK2, lw=1, ls="--", label="лінійне C(N) = N")
    for k, (label, d) in enumerate((("Compose (ЛР 2)", compose), ("Kubernetes (ЛР 3)", k8s))):
        if not d:
            continue
        x1 = d["X"][1]
        xs = sorted(d["X"])
        ax.errorbar(xs, [d["X"][n] / x1 for n in xs], yerr=[d["sd"][n] / x1 for n in xs],
                    fmt="o", ms=7, color=C[k], capsize=4,
                    mfc=C[k], label=f"{label}: виміряне C(N) ± розкид")
        f = d["fit"]
        if f.get("sigma") is not None:
            ax.plot(ns, [usl_x(n, 1.0, f["sigma"], f["kappa"]) for n in ns], color=C[k], lw=2,
                    label=f"{label}: σ = {f['sigma']:.3f}, κ = {f['kappa']:.4f}")
    _style(ax, "Відносна пропускна здатність C(N) = X(N)/X(1)", "C(N)")
    ax.set_xlabel("кількість реплік N", fontsize=8, color=INK2)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=SURF)
    plt.close(fig)


def chart_profiles(prof, path):
    plt = plt_()
    fig, axes = plt.subplots(4, 2, figsize=(13, 12), sharex=True, sharey="row", facecolor=SURF)
    for c, key in enumerate(("fixed", "hpa")):
        d = prof.get(key)
        if not d:
            continue
        t = d["t"]
        ax = axes[0][c]
        ax.step(t, d["z"], where="post", color=INK2, lw=1.5, label="задана z(t)")
        ax.plot(t, d["x"], color=C[0], lw=1.8, label="досягнута (2xx)")
        _style(ax, f"{d['title']}: інтенсивність", "запитів/с")
        ax.legend(frameon=False, fontsize=8)
        ax = axes[1][c]
        ax.plot(t, d["p99"], color=C[0], lw=1.5, label="p99 клієнта (за 1 с)")
        ax.axhline(d["thr"], color=C[1], lw=1, ls="--", label="поріг 2·p99₀")
        ax.set_yscale("log")
        _style(ax, "p99 клієнта", "мс (лог.)")
        ax.legend(frameon=False, fontsize=8)
        ax = axes[2][c]
        ax.step(t, d["d"], where="post", color=INK2, lw=1.5, label="потрібно d(t)")
        ax.step(t, d["s"], where="post", color=C[2], lw=2, label="готово s(t)")
        if d.get("desired"):
            ax.step(t, d["desired"], where="post", color=C[1], lw=1.2, ls=":",
                    label="бажано (HPA)")
        ax.set_ylim(0, R + 0.5)
        _style(ax, "Кількість реплік", "реплік")
        ax.legend(frameon=False, fontsize=8)
        ax = axes[3][c]
        ax.plot(d["cpu_t"], d["cpu_u"], color=C[3], lw=2, marker="o", ms=3,
                label="середнє по подах, % від requests.cpu")
        ax.axhline(50, color=INK2, lw=1, ls="--", label="ціль HPA 50 %")
        ax.set_ylim(bottom=0)
        _style(ax, "Використання CPU відносно requests.cpu (250m)", "%")
        ax.legend(frameon=False, fontsize=8)
        ax.set_xlabel("час від початку прогону, с", fontsize=8, color=INK2)
        for row in axes:
            for xv in (30, 90):
                row[c].axvline(xv, color=GRID, lw=1)
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=SURF)
    plt.close(fig)


# --------------------------------------------------------------------------
def series_xn(prefix_dir: Path, pattern: str) -> dict:
    X, sd, runs = {}, {}, {}
    for n in range(1, 7):
        p = prefix_dir / pattern.format(n=n)
        if p.exists():
            r = json.loads(p.read_text(encoding="utf-8"))
            X[n], sd[n], runs[n] = r["X_rps"], r["X_sd"], r
    return {"X": X, "sd": sd, "runs": runs}


def main() -> None:
    CHARTS.mkdir(parents=True, exist_ok=True)
    out = ["# Аналіз вимірювань ЛР 3", "",
           "Згенеровано `loadtest/lab3_analyze.py` з `results/lab3/raw` і `results/lab3/prom`.", ""]
    J: dict = {}

    # ---------------------------------------------------------------- крок 7
    k8s = series_xn(RAW, "k8s_n{n}.json")
    excluded = {}
    out += ["## Крок 7. Серія без автомасштабування", "",
            "| N | X(N), rps | Розкид, rps | Сходинка X(N), rps | p99 клієнта, мс | p99 сервера, мс | "
            "Бакет, мс | не 2xx, % | Дроселювання: середнє, % | Дроселювання: макс., % | "
            "Остання сходинка: досягнута / задана | Точка |",
            "|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|---|"]
    per_n = {}
    for n, run in sorted(k8s["runs"].items()):
        _, prom = load(f"k8s_n{n}")
        best = next(s for s in run["steps"] if s["step"] == run["X_step"])
        last = run["steps"][-1]
        reached = last["achieved_rps"] < 0.9 * last["target_rps"]
        if not reached:
            excluded[n] = "остання сходинка ≥ 0.9 заданої"
        a, b = win(run, best)
        thr = prom.throttle(a, b) if prom else {}
        p99s = prom.p99(a, b) if prom else None
        tv = [v for v in thr.values() if v is not None]
        per_n[n] = {"run": run, "prom": prom, "best": best, "a": a, "b": b, "thr": thr}
        out.append(
            f"| {n} | {run['X_rps']} | {run['X_sd']} | {best['target_rps']:g} | {best['p99_ms']} | "
            f"{fmt_p99s(p99s)} | {fmt_bucket(p99s)} | {best['non2xx_pct']} | "
            f"{f1(mean(tv), 1, True)} | {f1(max(tv) if tv else None, 1, True)} | "
            f"{last['achieved_rps']} / {last['target_rps']:g} | "
            f"{'досягнута' if reached else 'НЕ досягнута (не в підборі)'} |")
        J.setdefault("series", {})[n] = {
            "X": run["X_rps"], "sd": run["X_sd"], "step": best["target_rps"],
            "p99_client_ms": best["p99_ms"], "p99_server": p99s, "throttle": thr,
            "non2xx_pct": best["non2xx_pct"], "reached": reached,
            "pods": run.get("extra", {}).get("pods")}
    out.append("")
    for n, d in per_n.items():
        run, prom = d["run"], d["prom"]
        out += [f"### Сходинки N = {n}", "",
                "| Задана | Досягнута | Розкид | Запізн. відпр. p99 | p50 кл. | p99 кл. | p99 серв. | "
                "не 2xx, % | помилки зʼєдн. | CPU на под, ядер | дросел. сер., % | дросел. макс., % |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for st in run["steps"]:
            a, b = win(run, st)
            thr = [v for v in (prom.throttle(a, b) if prom else {}).values() if v is not None]
            cpu = list((prom.cpu(a, b) if prom else {}).values())
            out.append(
                f"| {st['target_rps']:g} | {st['achieved_rps']} | {st['achieved_sd']} | "
                f"{st.get('send_lag_p99_ms')} | {st['p50_ms']} | {st['p99_ms']} | "
                f"{fmt_p99s(prom.p99(a, b) if prom else None)} | {st['non2xx_pct']} | "
                f"{st['connerr']} | {f1(mean(cpu), 3)} | {f1(mean(thr), 1, True)} | "
                f"{f1(max(thr) if thr else None, 1, True)} |")
        out.append("")

    # ---------------------------------------------------------------- крок 8
    out += ["## Крок 8. Коефіцієнт дисбалансу I = X_max / X_avg на сходинці X(N)", "",
            "| N | Под | Вузол | Запитів/с (Prometheus) | Запитів/с (X-Pod, клієнт) | p99 сервера, мс | "
            "Дроселювання, % |", "|---:|---|---|---:|---:|---:|---:|"]
    imb = {}
    for n, d in per_n.items():
        if n == 1 or d["prom"] is None:
            continue
        run, prom, best = d["run"], d["prom"], d["best"]
        rps = prom.item_rps(d["a"], d["b"])
        node_of = {p["pod"]: p["node"] for p in (run.get("extra", {}).get("pods") or [])}
        if not rps:
            continue
        x_avg = statistics.fmean(rps.values())
        x_max = max(rps.values())
        cl = best.get("pods_rps", {})
        imb[n] = {"I": x_max / x_avg, "I_client": (max(cl.values()) / statistics.fmean(cl.values())
                                                    if cl else None),
                  "rps": rps, "x_avg": x_avg, "x_max": x_max}
        hi, lo = max(rps, key=rps.get), min(rps, key=rps.get)
        for pod in sorted(rps, key=rps.get, reverse=True):
            mark = " (макс.)" if pod == hi else (" (мін.)" if pod == lo else "")
            p = prom.p99(d["a"], d["b"], pod) if pod in (hi, lo) else None
            th = d["thr"].get(pod)
            out.append(f"| {n} | `{pod}`{mark} | {node_of.get(pod, '—')} | {rps[pod]:.1f} | "
                       f"{cl.get(pod, '—')} | {fmt_p99s(p) if pod in (hi, lo) else ''} | "
                       f"{f1(th, 1, True)} |")
        imb[n]["hi"], imb[n]["lo"] = hi, lo
    out += ["", "| N | X_avg, rps | X_max, rps | **I** (Prometheus) | I (за X-Pod) |",
            "|---:|---:|---:|---:|---:|"]
    for n, v in imb.items():
        out.append(f"| {n} | {v['x_avg']:.1f} | {v['x_max']:.1f} | **{v['I']:.3f}** | "
                   f"{f1(v['I_client'], 3)} |")
    out.append("")
    J["imbalance"] = imb
    if R in per_n and per_n[R]["prom"]:
        chart_pods_rps(per_n[R]["run"], per_n[R]["prom"], CHARTS / f"pods_rps_n{R}.png")

    # ---------------------------------------------------------------- крок 9
    fit_pts = {n: x for n, x in k8s["X"].items() if n not in excluded or n == 1}
    if 1 in fit_pts and len(fit_pts) >= 3:
        k8s["fit"] = usl_fit(fit_pts)
    else:
        k8s["fit"] = {}
    compose = series_xn(LAB2_RAW, "step_n{n}.json")
    compose["fit"] = usl_fit(compose["X"]) if 1 in compose["X"] else {}
    out += ["## Крок 9. Універсальний закон масштабованості: Kubernetes і Compose", "",
            "| Стенд | X(1) | C(2) | C(3) | C(4) | σ | κ | N* |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for label, d in (("Compose (ЛР 2)", compose), ("Kubernetes (ЛР 3)", k8s)):
        if not d["X"]:
            continue
        x1 = d["X"][1]
        cs = [f"{d['X'][n] / x1:.3f}" if n in d["X"] else "—" for n in (2, 3, 4)]
        f = d["fit"]
        out.append(f"| {label} | {x1} | {' | '.join(cs)} | {f1(f.get('sigma'), 4)} | "
                   f"{f1(f.get('kappa'), 4)} | {f1(f.get('N_star'), 2)} |")
    if k8s["fit"]:
        f = k8s["fit"]
        out += ["", "Kubernetes, підбір: " + ", ".join(f"{k} = {v:.4f}" for k, v in f["sums"].items())]
        if "raw" in f:
            out.append(f"Оцінки МНК: σ = {f['raw']['sigma']:.5f}, κ = {f['raw']['kappa']:.5f}."
                       + (f" Корекція: {f['note']}." if f.get("note") else ""))
        out.append(f"Точки поза підбором: {excluded or 'немає'}.")
    out.append("")
    J["usl"] = {"k8s": {k: v for k, v in k8s.items() if k != "runs"},
                "compose": {k: v for k, v in compose.items() if k != "runs"}}
    chart_c(compose, k8s, CHARTS / "c_n.png")

    # ---------------------------------------------------------------- кроки 10–12
    Xn = dict(k8s["X"])
    fk = k8s.get("fit") or {}

    def X_of(n):
        if n in Xn:
            return Xn[n]
        if fk.get("sigma") is not None:
            return usl_x(n, Xn[1], fk["sigma"], fk["kappa"])
        return 0.0

    def d_of(z):
        for n in range(1, R + 1):
            if X_of(n) >= z:
                return n
        return R

    prof, elastic = {}, {}
    fixed_run, _ = load("prof_fixed")
    p99_0 = p99_spike = None
    if fixed_run:
        seg = fixed_run["steps"]
        p99_0, p99_spike = seg[0]["p99_ms"], seg[1]["p99_ms"]
    for key, name, title in (("fixed", "prof_fixed", f"Без HPA ({R} репліки)"),
                             ("hpa", "prof_hpa", "З HPA (1..4)")):
        run, prom = load(name)
        if not run:
            continue
        t0 = run["t0_epoch"]
        secs = run["seconds"]
        T = len(secs)
        ready = prom.gauge_steps("kube_deployment_status_replicas_ready") if prom else []
        desired = prom.gauge_steps("kube_horizontalpodautoscaler_status_desired_replicas") if prom else []
        ready_rel = [(t - t0, v) for t, v in ready]
        des_rel = [(t - t0, v) for t, v in desired]
        t = [s["t_s"] for s in secs]
        z = [s["target_rps"] for s in secs]
        s_t = [value_at(ready_rel, ti, default=float(R if key == "fixed" else 1)) for ti in t]
        d_t = [d_of(zi) for zi in z]
        thr = 2 * p99_0 if p99_0 else None
        viol = sum(1 for s in secs if thr and (s["p99_ms"] or 0) > thr)
        under = sum(1 for si, di in zip(s_t, d_t) if si < di)
        over = sum(1 for si, di in zip(s_t, d_t) if si > di)
        aU = sum(max(di - si, 0) for si, di in zip(s_t, d_t)) / T
        aO = sum(max(si - di, 0) for si, di in zip(s_t, d_t)) / T
        W = sum(s_t)
        non2xx = sum(s["non2xx"] for s in secs)
        connerr = sum(s["connerr"] for s in secs)
        cpu_t, cpu_u = [], []
        if prom:
            for g in range(15, T + 1, 5):
                c = prom.cpu(t0 + g - 15, t0 + g)
                if c:
                    cpu_t.append(g)
                    cpu_u.append(100 * statistics.fmean(c.values()) / REQ_CPU)
        elastic[key] = {"T": T, "theta_U": under / T, "theta_O": over / T, "a_U": aU, "a_O": aO,
                        "W": W, "violations": viol, "non2xx": non2xx, "connerr": connerr,
                        "threshold_ms": thr}
        prof[key] = {"title": title, "t": t, "z": z, "x": [s["achieved_rps"] for s in secs],
                     "p99": [s["p99_ms"] for s in secs], "thr": thr or 0, "d": d_t, "s": s_t,
                     "desired": [value_at(des_rel, ti) for ti in t] if des_rel else None,
                     "cpu_t": cpu_t, "cpu_u": cpu_u, "steps": run["steps"],
                     "ready_rel": ready_rel, "des_rel": des_rel}

    if prof:
        out += ["## Кроки 10–12. Профіль зі стрибком: без HPA і з HPA", ""]
        if fixed_run:
            ex = fixed_run.get("extra", {})
            out += [f"Профіль: {ex.get('profile')} (0.3·X(1) = {ex.get('low')}, стрибок = "
                    f"min(2.5·X(1), 0.9·X({R})) = {ex.get('spike')} rps). "
                    f"p99₀ (прогін без HPA, перший відрізок) = {p99_0} мс, поріг 2·p99₀ = "
                    f"{2 * p99_0 if p99_0 else '—'} мс; p99 прогону без HPA на рівні стрибка = "
                    f"{p99_spike} мс.", ""]
        out += ["| Відрізок | Прогін | Задана | Досягнута | p50 кл. | p95 кл. | p99 кл. | не 2xx, % | "
                "помилки зʼєдн. |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for key, d in prof.items():
            for i, st in enumerate(d["steps"]):
                out.append(f"| {i + 1} | {d['title']} | {st['target_rps']:g} | {st['achieved_rps']} | "
                           f"{st['p50_ms']} | {st['p95_ms']} | {st['p99_ms']} | {st['non2xx_pct']} | "
                           f"{st['connerr']} |")
        out += ["", "**Метрики еластичності** (T — усі секунди прогону):", "",
                "| Прогін | T, с | θ_U | θ_O | a_U | a_O | W, репліко-с | Секунд з p99 > 2·p99₀ | "
                "Відповідей не 2xx | Помилок зʼєднання |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for key, e in elastic.items():
            out.append(f"| {prof[key]['title']} | {e['T']} | {e['theta_U']:.3f} | {e['theta_O']:.3f} | "
                       f"{e['a_U']:.3f} | {e['a_O']:.3f} | {e['W']:.0f} | {e['violations']} | "
                       f"{e['non2xx']} | {e['connerr']} |")
        out.append("")
        out += ["d(t) = min{N ≤ 4 : X(N) ≥ z(t)} за X(N) серії кроку 7: " + ", ".join(
            f"X({n}) = {X_of(n):.0f}" for n in range(1, R + 1)) + ".", ""]
        chart_profiles(prof, CHARTS / "profiles.png")
        J["elastic"] = elastic

    # HPA: часи реакції
    if "hpa" in prof:
        d = prof["hpa"]
        ts, te, tend = 30.0, 90.0, float(d["t"][-1] + 1)
        des = d["des_rel"]
        rdy = d["ready_rel"]
        d0 = value_at(des, ts, 1)
        t_des = next((t for t, v in des if t >= ts and v != d0), None)
        rmax = max((v for t, v in rdy if 0 <= t <= tend), default=None)
        t_rmax = next((t for t, v in rdy if t >= ts and v == rmax), None)
        lim = 1.2 * p99_spike if p99_spike else None
        p99 = d["p99"]
        t_p99 = None
        if lim:
            for i in range(int(ts), int(te) - 9):
                if all(p is not None and p < lim for p in p99[i:i + 10]):
                    t_p99 = float(i)
                    break
        r0 = value_at(rdy, te, None)
        t_down = next((t for t, v in rdy if t > te and r0 is not None and v < r0), None)

        def fmt_ev(t, ref, end):
            if t is None or t > end:
                return "не досягнуто"
            return f"{t - ref:.1f} с"

        hp = {"desired_first_change_s": fmt_ev(t_des, ts, te),
              "max_ready": rmax, "max_ready_s": fmt_ev(t_rmax, ts, te),
              "p99_recovered_s": fmt_ev(t_p99, ts, te), "p99_limit_ms": lim,
              "first_scale_down_s": fmt_ev(t_down, te, tend)}
        J["hpa_times"] = hp
        out += ["**Часи реакції HPA** (стрибок — 30–90 с прогону):", "",
                "| Подія | Час |", "|---|---:|",
                f"| Від початку стрибка до першої зміни бажаної кількості реплік | {hp['desired_first_change_s']} |",
                f"| Від початку стрибка до готовності найбільшої кількості реплік ({rmax:.0f}) | {hp['max_ready_s']} |" if rmax else "| — | — |",
                f"| Від початку стрибка до 10 с поспіль p99 < 1.2·{p99_spike} = {lim:.1f} мс | {hp['p99_recovered_s']} |" if lim else "| — | — |",
                f"| Від кінця стрибка до першого зменшення готових реплік | {hp['first_scale_down_s']} |", ""]
        out += ["Бажана і готова кількість реплік (kube-state-metrics, відносно початку прогону):", "",
                "| t, с | бажано (HPA) | готово |", "|---:|---:|---:|"]
        changes, last = [], None
        for t_ in sorted({t for t, _ in des} | {t for t, _ in rdy}):
            row = (value_at(des, t_), value_at(rdy, t_))
            if row != last and -30 <= t_ <= tend + 30:
                changes.append(f"| {t_:.1f} | {row[0] if row[0] is not None else '—'} | "
                               f"{row[1] if row[1] is not None else '—'} |")
                last = row
        out += changes + [""]

    # ---------------------------------------------------------------- Д3
    rows_d3 = []
    for name, label in (("ing_nodeport", "NodePort (:8080)"), ("ing_ingress", "Ingress Traefik (:8081)")):
        run, prom = load(name)
        if not run or not prom:
            continue
        st = run["steps"][0]
        a, b = win(run, st)
        rps = prom.item_rps(a, b)
        thr = prom.throttle(a, b)
        cl = st.get("pods_rps", {})
        I = max(rps.values()) / statistics.fmean(rps.values()) if rps else None
        Ic = max(cl.values()) / statistics.fmean(cl.values()) if cl else None
        rows_d3.append({"via": label, "rate": st["target_rps"], "achieved": st["achieved_rps"],
                        "p99_client": st["p99_ms"], "p99_server": prom.p99(a, b), "I": I,
                        "I_client": Ic, "rps": rps, "thr": thr})
    if rows_d3:
        out += ["## Д3. Дисбаланс через NodePort і через Ingress (однакова задана інтенсивність)", "",
                "| Шлях | Задана | Досягнута | p99 кл., мс | p99 серв., мс | Запитів/с по подах | **I** | "
                "I (X-Pod) | Дроселювання макс., % |", "|---|---:|---:|---:|---:|---|---:|---:|---:|"]
        for r_ in rows_d3:
            tv = [v for v in r_["thr"].values() if v is not None]
            out.append(f"| {r_['via']} | {r_['rate']:g} | {r_['achieved']} | {r_['p99_client']} | "
                       f"{fmt_p99s(r_['p99_server'])} | "
                       f"{', '.join(f'{v:.0f}' for v in sorted(r_['rps'].values(), reverse=True))} | "
                       f"**{f1(r_['I'], 3)}** | {f1(r_['I_client'], 3)} | "
                       f"{f1(max(tv) if tv else None, 1, True)} |")
        out.append("")
        J["d3"] = rows_d3

    (LAB3 / "analysis.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    (LAB3 / "analysis.json").write_text(json.dumps(J, ensure_ascii=False, indent=1, default=str),
                                        encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
