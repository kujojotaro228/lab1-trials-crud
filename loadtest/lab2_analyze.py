#!/usr/bin/env python3
"""Аналіз вимірювань ЛР 2 (кроки 7–11): таблиці, діаграми, USL.

    py loadtest/lab2_analyze.py [--suffix ""]

Вхід: results/lab2/raw/step_*.json (генератор), results/lab2/prom/*.json.gz (сирі
зразки Prometheus, експорт lab2.py), *_dstats.json (docker stats), stop_test.json,
host.json. Вихід: results/lab2/analysis.md, analysis.json, charts/*.png.

Усі величини сервера рахуються з сирих зразків лічильників (кожен збір раз на 5 с):
p99 сервера — за формулою лінійної інтерполяції в бакеті з розділу «Теоретичні
відомості» за приростами бакетів, просумованими по всіх екземплярах.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpjson import REPO_ROOT, RESOURCE  # noqa: E402

LAB2 = REPO_ROOT / "results" / "lab2"
RAW, PROM, CHARTS = LAB2 / "raw", LAB2 / "prom", LAB2 / "charts"
ITEM_PATH = RESOURCE + "/{participant_id}"
BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]

# палітра (categorical slots 1..4) і текстові кольори
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"]
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


# --------------------------------------------------------------------------
# Сирі зразки Prometheus
# --------------------------------------------------------------------------
class Prom:
    def __init__(self, path: Path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            self.d = json.load(f)
        pods = {}
        for s in self.d["series"].get("app_pod_info", []):
            pods[s["labels"]["instance"]] = s["labels"]["pod"]
        self.pods = pods

    def series(self, metric: str, **match) -> list[dict]:
        return [s for s in self.d["series"].get(metric, [])
                if all(s["labels"].get(k) == v for k, v in match.items())]

    @staticmethod
    def increase(values: list, a: float, b: float) -> float:
        """Приріст лічильника між моментами a і b за зразками (з урахуванням скидань)."""
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
    def span(values: list, a: float, b: float) -> float:
        """Фактичний проміжок між зразками, які обмежують приріст (для ділення на час)."""
        before = [t for t, _ in values if t <= a]
        inside = [t for t, _ in values if a < t <= b]
        if not inside:
            return 0.0
        return inside[-1] - (before[-1] if before else inside[0])

    def instances(self) -> list[str]:
        return sorted({s["labels"]["instance"] for s in self.series("process_cpu_seconds_total")})

    def per_instance_rate(self, metric: str, a: float, b: float, **match) -> dict:
        out = defaultdict(float)
        spans = {}
        for s in self.series(metric, **match):
            inst = s["labels"]["instance"]
            out[inst] += self.increase(s["values"], a, b)
            spans[inst] = max(spans.get(inst, 0), self.span(s["values"], a, b))
        return {k: (v / spans[k] if spans.get(k) else 0.0) for k, v in out.items()}

    def gauge_mean(self, metric: str, a: float, b: float) -> dict:
        out = {}
        for s in self.series(metric):
            vals = [v for t, v in s["values"] if a <= t <= b]
            if vals:
                out[s["labels"]["instance"]] = statistics.fmean(vals)
        return out

    def bucket_increase(self, a: float, b: float) -> list[tuple[float, float]]:
        acc = defaultdict(float)
        for s in self.series("http_request_duration_seconds_bucket", path=ITEM_PATH,
                             method="GET"):
            le = float(s["labels"]["le"])
            acc[le] += self.increase(s["values"], a, b)
        return sorted(acc.items())

    def p99_server(self, a: float, b: float, q: float = 0.99) -> dict | None:
        cum = self.bucket_increase(a, b)
        return quantile_from_buckets(cum, q)

    def throttle(self, a: float, b: float) -> dict:
        per = {s["labels"]["instance"]: self.increase(s["values"], a, b)
               for s in self.series("cgroup_cpu_nr_periods_total")}
        thr = {s["labels"]["instance"]: self.increase(s["values"], a, b)
               for s in self.series("cgroup_cpu_nr_throttled_total")}
        return {k: (thr.get(k, 0) / v if v else None) for k, v in per.items()}


def quantile_from_buckets(cum: list[tuple[float, float]], q: float) -> dict | None:
    """Q(q) = b(i−1) + (b(i) − b(i−1))·(r − c(i−1)) / (c(i) − c(i−1)), r = q·n."""
    if not cum:
        return None
    n = dict(cum).get(math.inf, cum[-1][1])
    if n <= 0:
        return None
    r = q * n
    prev_b, prev_c = 0.0, 0.0
    finite = [(le, c) for le, c in cum if le != math.inf]
    for le, c in finite:
        if prev_c < r <= c:
            val = prev_b + (le - prev_b) * (r - prev_c) / (c - prev_c)
            return {"value_s": val, "lo": prev_b, "hi": le, "err_s": le - prev_b,
                    "beyond": False, "n": n}
        prev_b, prev_c = le, c
    bk = finite[-1][0]
    return {"value_s": bk, "lo": bk, "hi": math.inf, "err_s": None, "beyond": True, "n": n}


def fmt_p99s(p: dict | None) -> str:
    if p is None:
        return "—"
    if p["beyond"]:
        return f"> {p['lo'] * 1000:g}"
    return f"{p['value_s'] * 1000:.1f}"


def fmt_bucket(p: dict | None) -> str:
    if p is None:
        return "—"
    if p["beyond"]:
        return f"({p['lo'] * 1000:g}; +Inf]"
    return f"({p['lo'] * 1000:g}; {p['hi'] * 1000:g}]"


def fmt_err(p: dict | None) -> str:
    if p is None:
        return "—"
    return "не обмежена" if p["beyond"] else f"±{p['err_s'] * 1000:g}"


# --------------------------------------------------------------------------
def load(name: str):
    run = json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))
    pp = PROM / f"{name}.json.gz"
    prom = Prom(pp) if pp.exists() else None
    dp = RAW / f"{name}_dstats.json"
    dstats = json.loads(dp.read_text(encoding="utf-8")) if dp.exists() else []
    return run, prom, dstats


def step_metrics(run: dict, prom: Prom | None, st: dict) -> dict:
    t0 = run["t0_epoch"]
    a, b = t0 + st["window_s"][0], t0 + st["window_s"][1]
    out = {"p99_server": None, "cpu": {}, "inflight": {}, "throttle": {}, "rps": {}}
    if prom is None:
        return out
    out["p99_server"] = prom.p99_server(a, b)
    out["cpu"] = prom.per_instance_rate("process_cpu_seconds_total", a, b)
    out["inflight"] = prom.gauge_mean("http_requests_in_progress", a, b)
    out["throttle"] = prom.throttle(a, b)
    out["rps"] = prom.per_instance_rate("http_requests_total", a, b, path=ITEM_PATH)
    return out


def mean_or_none(d: dict):
    vals = [v for v in d.values() if v is not None]
    return statistics.fmean(vals) if vals else None


def dstats_at(dstats: list, a: float, b: float) -> dict:
    """Середні CPU % і памʼять кожного контейнера за знімками у [a, b]."""
    acc = defaultdict(list)
    mem = {}
    for smp in dstats:
        if "rows" not in smp or not (a <= smp["t_epoch"] <= b):
            continue
        for r in smp["rows"]:
            try:
                acc[r["name"]].append(float(str(r["cpu_pct"]).rstrip("%")))
            except ValueError:
                pass
            mem[r["name"]] = r["mem"]
    return {k: {"cpu_pct": statistics.fmean(v), "n": len(v), "mem": mem.get(k)}
            for k, v in sorted(acc.items())}


# --------------------------------------------------------------------------
# USL
# --------------------------------------------------------------------------
def usl_fit(points: dict[int, float]) -> dict:
    x1 = points[1]
    rows = []
    for n, x in sorted(points.items()):
        if n == 1:
            continue
        c = x / x1
        rows.append({"N": n, "X": x, "C": c, "u": n - 1, "v": n * (n - 1), "y": n / c - 1})
    suu = sum(r["u"] ** 2 for r in rows)
    svv = sum(r["v"] ** 2 for r in rows)
    suv = sum(r["u"] * r["v"] for r in rows)
    suy = sum(r["u"] * r["y"] for r in rows)
    svy = sum(r["v"] * r["y"] for r in rows)
    det = suu * svv - suv ** 2
    res = {"rows": rows, "sums": {"Σu²": suu, "Σv²": svv, "Σuv": suv, "Σuy": suy, "Σvy": svy,
                                  "det": det}, "lambda": x1}
    if len(rows) >= 2 and det != 0:
        sigma = (svv * suy - suv * svy) / det
        kappa = (suu * svy - suv * suy) / det
        res["raw"] = {"sigma": sigma, "kappa": kappa}
        if sigma < 0:
            sigma, kappa = 0.0, svy / svv
            res["note"] = "σ < 0 → σ = 0, κ = Σvy / Σv²"
        elif kappa < 0:
            sigma, kappa = suy / suu, 0.0
            res["note"] = "κ < 0 → κ = 0, σ = Σuy / Σu²"
    elif len(rows) == 1:
        sigma, kappa = None, None
        res["note"] = "менше двох точок з N > 1"
    else:
        sigma, kappa = None, None
    res["sigma"], res["kappa"] = sigma, kappa
    if sigma is not None and kappa is not None:
        if kappa > 0 and sigma < 1:
            res["N_star"] = math.sqrt((1 - sigma) / kappa)
        elif kappa == 0 and sigma > 0:
            res["limit"] = x1 / sigma
    return res


def usl_x(n, lam, sigma, kappa):
    return lam * n / (1 + sigma * (n - 1) + kappa * n * (n - 1))


# --------------------------------------------------------------------------
# Діаграми
# --------------------------------------------------------------------------
def _style(ax, title=None, ylabel=None):
    ax.set_facecolor(SURF)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, loc="left", fontsize=10, color=INK)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8, color=INK2)


def chart_run(run: dict, prom: Prom, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t0 = run["t0_epoch"]
    secs = run["seconds"]
    t = [s["t_s"] for s in secs]
    fig, axes = plt.subplots(5, 1, figsize=(10, 14), sharex=True, facecolor=SURF)
    n_inst = len(prom.instances()) if prom else 0
    fig.suptitle(f"{run['name']}: {n_inst} екз.", x=0.01, ha="left", fontsize=12, color=INK)

    ax = axes[0]
    ax.step(t, [s["target_rps"] for s in secs], where="post", color=INK2, lw=1.5,
            label="задана")
    ax.plot(t, [s["achieved_rps"] for s in secs], color=C[0], lw=2, label="досягнута (2xx)")
    _style(ax, "Задана і досягнута інтенсивність", "запитів/с")
    ax.legend(frameon=False, fontsize=8)

    ax = axes[1]
    ax.plot(t, [s["p99_ms"] for s in secs], color=C[0], lw=1.5, label="p99 клієнта (за 1 с)")
    if prom:
        ts, vs = [], []
        tt = 15.0
        while t0 + tt <= run["t_end_epoch"]:
            p = prom.p99_server(t0 + tt - 15, t0 + tt)
            if p:
                ts.append(tt)
                vs.append(p["value_s"] * 1000)
            tt += 5
        ax.plot(ts, vs, color=C[1], lw=2, marker="o", ms=3,
                label="p99 сервера (вікно 15 с, крок 5 с)")
        ax.axhline(1000, color=GRID, lw=1, ls="--")
        ax.text(t[0], 1000, " остання скінченна межа бакета 1 с", fontsize=7, color=INK2,
                va="bottom")
    ax.set_yscale("log")
    _style(ax, "p99 клієнта і p99 сервера", "мс (лог. шкала)")
    ax.legend(frameon=False, fontsize=8)

    if prom:
        insts = prom.instances()
        grid = []
        tt = 15.0
        while t0 + tt <= run["t_end_epoch"]:
            grid.append(tt)
            tt += 5
        for k, (ttl, ylab, fn) in enumerate([
            ("Запитів за секунду на екземпляр", "запитів/с",
             lambda a, b: prom.per_instance_rate("http_requests_total", a, b, path=ITEM_PATH)),
            ("Використання CPU екземпляром (ліміт 0.5)", "ядер",
             lambda a, b: prom.per_instance_rate("process_cpu_seconds_total", a, b)),
            ("Запитів в обробці на екземпляр", "запитів",
             lambda a, b: prom.gauge_mean("http_requests_in_progress", a, b)),
        ]):
            ax = axes[2 + k]
            vals = [fn(t0 + g - 15, t0 + g) for g in grid]
            for j, inst in enumerate(insts):
                ax.plot(grid, [v.get(inst) for v in vals], color=C[j % len(C)], lw=1.8,
                        label=prom.pods.get(inst, inst))
            if k == 1:
                ax.axhline(0.5, color=INK2, lw=1, ls="--")
            _style(ax, ttl, ylab)
            ax.legend(frameon=False, fontsize=8, ncol=4)
    axes[-1].set_xlabel("час від початку прогону, с", fontsize=8, color=INK2)
    # межі сходинок
    for ax in axes:
        for st in run["steps"]:
            ax.axvline(st["t_start_s"], color=GRID, lw=0.5)
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=SURF)
    plt.close(fig)


def chart_usl(points: dict, sd: dict, excluded: dict, fit: dict, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    lam = fit["lambda"]
    fig, ax = plt.subplots(figsize=(7, 4.5), facecolor=SURF)
    nmax = max(6, int(math.ceil(fit.get("N_star", 4) or 4)) + 2)
    ns = [i / 10 for i in range(10, nmax * 10 + 1)]
    ax.plot(ns, [lam * n for n in ns], color=INK2, lw=1.2, ls="--", label="λN (лінійне)")
    if fit.get("sigma") is not None:
        ax.plot(ns, [usl_x(n, lam, fit["sigma"], fit["kappa"]) for n in ns], color=C[1], lw=2,
                label=f"USL: σ = {fit['sigma']:.3f}, κ = {fit['kappa']:.4f}")
    xs = sorted(points)
    inc = [n for n in xs if n not in excluded]
    ax.errorbar(inc, [points[n] for n in inc], yerr=[sd[n] for n in inc], fmt="o", ms=7,
                color=C[0], capsize=4, label="виміряне X(N) ± розкид")
    exc = [n for n in xs if n in excluded]
    if exc:
        ax.errorbar(exc, [points[n] for n in exc], yerr=[sd[n] for n in exc], fmt="o", ms=7,
                    mfc=SURF, color=C[0], capsize=4, label="обмежено генератором (не в підборі)")
    if fit.get("N_star"):
        ax.axvline(fit["N_star"], color=GRID, lw=1)
        ax.text(fit["N_star"], ax.get_ylim()[1] * 0.95, f" N* = {fit['N_star']:.2f}",
                fontsize=8, color=INK2, va="top")
    _style(ax, "Пропускна здатність X(N) і універсальний закон масштабованості", "запитів/с")
    ax.set_xlabel("кількість екземплярів N", fontsize=8, color=INK2)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=SURF)
    plt.close(fig)


def chart_throttle(data: dict, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 4.2), facecolor=SURF)
    k = 0
    for run_label, per_inst in data.items():
        for inst, pts in per_inst.items():
            xs = [p[0] for p in pts]
            ys = [100 * p[1] if p[1] is not None else None for p in pts]
            ax.plot(xs, ys, color=C[k % len(C)], lw=2, marker="o", ms=4,
                    ls="-" if "N=1" in run_label else "--", label=f"{run_label}: {inst}")
            k += 1
    ax.set_ylim(0, 105)
    _style(ax, "М3: частка дроселювання CPU (Δnr_throttled / Δnr_periods)", "% періодів CFS")
    ax.set_xlabel("задана інтенсивність, запитів/с", fontsize=8, color=INK2)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=SURF)
    plt.close(fig)


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suffix", default="")
    args = ap.parse_args()
    CHARTS.mkdir(parents=True, exist_ok=True)
    out = ["# Аналіз вимірювань ЛР 2", "",
           "Згенеровано `loadtest/lab2_analyze.py` з `results/lab2/raw` і `results/lab2/prom`.", ""]
    J: dict = {}

    host = json.loads((LAB2 / "host.json").read_text(encoding="utf-8")) \
        if (LAB2 / "host.json").exists() else {}
    J["host"] = host
    noop = json.loads((RAW / "step_noop.json").read_text(encoding="utf-8"))
    G = noop["X_rps"]
    J["G"] = G
    out += ["## Межа генератора G (крок 6)", "",
            "| Задана, rps | Досягнута, rps | Розкид | Надіслано, rps | p50, мс | p99, мс | не 2xx, % | помилки зʼєднання |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for st in noop["steps"]:
        out.append(f"| {st['target_rps']:g} | {st['achieved_rps']} | {st['achieved_sd']} | "
                   f"{st['sent_rps']} | {st['p50_ms']} | {st['p99_ms']} | {st['non2xx_pct']} | "
                   f"{st['connerr']} |")
    out += ["", f"**G = {G} rps** (сходинка {noop['X_target_rps']:g} rps, розкид "
                f"{noop['X_sd']}), зупинка: `{noop['stop_reason']}`.", ""]

    # ---------------------------------------------------------------- серія
    points, sds, excluded, per_n = {}, {}, {}, {}
    out += ["## Серія 1–4 екземпляри (крок 7)", "",
            "| N | X(N), rps | Розкид, rps | Сходинка X(N), rps | p99 клієнта, мс | p99 сервера, мс | "
            "Бакет p99 сервера, мс | не 2xx, % | CPU на екз., ядер | В обробці на екз. | "
            "Дроселювання, % | Зупинка | Обмежено генератором |",
            "|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---|---|"]
    for n in (1, 2, 3, 4):
        name = f"step_n{n}{args.suffix}"
        if not (RAW / f"{name}.json").exists():
            continue
        run, prom, dstats = load(name)
        best = next(s for s in run["steps"] if s["step"] == run["X_step"])
        last = run["steps"][-1]
        lim_reason = []
        if run["X_rps"] >= 0.9 * G:
            lim_reason.append("X ≥ 0.9·G")
        if last["achieved_rps"] >= 0.9 * last["target_rps"]:
            lim_reason.append("остання сходинка ≥ 0.9 заданої")
        sm = step_metrics(run, prom, best)
        points[n], sds[n] = run["X_rps"], run["X_sd"]
        if lim_reason:
            excluded[n] = lim_reason
        cpu_m, inf_m, thr_m = (mean_or_none(sm["cpu"]), mean_or_none(sm["inflight"]),
                               mean_or_none(sm["throttle"]))
        per_n[n] = {"run": run, "prom": prom, "dstats": dstats, "best": best, "sm": sm}
        out.append(
            f"| {n} | {run['X_rps']} | {run['X_sd']} | {best['target_rps']:g} | {best['p99_ms']} | "
            f"{fmt_p99s(sm['p99_server'])} | {fmt_bucket(sm['p99_server'])} | "
            f"{best['non2xx_pct']} | {cpu_m:.3f} | {inf_m:.2f} | "
            f"{100 * thr_m:.1f} | `{run['stop_reason']}` | "
            f"{'так: ' + ', '.join(lim_reason) if lim_reason else 'ні'} |"
            if cpu_m is not None else
            f"| {n} | {run['X_rps']} | {run['X_sd']} | {best['target_rps']:g} | {best['p99_ms']} | "
            f"— | — | {best['non2xx_pct']} | — | — | — | `{run['stop_reason']}` | "
            f"{'так' if lim_reason else 'ні'} |")
        J.setdefault("series", {})[n] = {
            "X": run["X_rps"], "sd": run["X_sd"], "step_target": best["target_rps"],
            "p99_client_ms": best["p99_ms"], "p99_server": sm["p99_server"],
            "non2xx_pct": best["non2xx_pct"], "cpu_per_inst": sm["cpu"],
            "inflight_per_inst": sm["inflight"], "throttle": sm["throttle"],
            "rps_per_inst": sm["rps"], "excluded": lim_reason, "pods": run["pods"],
            "stop_reason": run["stop_reason"], "steps": run["steps"]}
    out.append("")

    # усі сходинки кожного прогону
    for n, d in per_n.items():
        run, prom = d["run"], d["prom"]
        out += [f"### Сходинки прогону N = {n}", "",
                "| Задана | Досягнута | Розкид | Надіслано | Запізн. відпр. p99 | p50 кл. | p95 кл. | "
                "p99 кл. | p99 серв. | не 2xx, % | помилки зʼєдн. | CPU/екз. | в обробці/екз. | "
                "дросел., % |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for st in run["steps"]:
            sm = step_metrics(run, prom, st)
            cpu_m, inf_m, thr_m = (mean_or_none(sm["cpu"]), mean_or_none(sm["inflight"]),
                                   mean_or_none(sm["throttle"]))
            out.append(
                f"| {st['target_rps']:g} | {st['achieved_rps']} | {st['achieved_sd']} | "
                f"{st['sent_rps']} | {st.get('send_lag_p99_ms', '—')} | {st['p50_ms']} | "
                f"{st['p95_ms']} | {st['p99_ms']} | "
                f"{fmt_p99s(sm['p99_server'])} | {st['non2xx_pct']} | {st['connerr']} | "
                f"{'—' if cpu_m is None else f'{cpu_m:.3f}'} | "
                f"{'—' if inf_m is None else f'{inf_m:.2f}'} | "
                f"{'—' if thr_m is None else f'{100 * thr_m:.1f}'} |")
        out.append("")
        if prom:
            chart_run(run, prom, CHARTS / f"run_n{n}.png")

    # ---------------------------------------------------------------- крок 9
    if 1 in per_n and per_n[1]["prom"]:
        run, prom = per_n[1]["run"], per_n[1]["prom"]
        out += ["## p99 клієнта і p99 сервера на кожній сходинці, N = 1 (крок 9)", "",
                "| Задана, rps | p99 клієнта, мс | p99 сервера, мс | Бакет, мс | Макс. похибка оцінки, мс | "
                "Спостережень у вікні |",
                "|---:|---:|---:|---|---:|---:|"]
        rows9 = []
        for st in run["steps"]:
            p = step_metrics(run, prom, st)["p99_server"]
            rows9.append({"target": st["target_rps"], "p99_client_ms": st["p99_ms"], "p99_server": p})
            out.append(f"| {st['target_rps']:g} | {st['p99_ms']} | {fmt_p99s(p)} | {fmt_bucket(p)} | "
                       f"{fmt_err(p)} | {int(p['n']) if p else '—'} |")
        J["step9"] = rows9
        out.append("")

    # ---------------------------------------------------------------- USL
    fit_pts = {n: x for n, x in points.items() if n not in excluded or n == 1}
    if 1 in points:
        fit = usl_fit(fit_pts)
        J["usl"] = fit
        out += ["## Універсальний закон масштабованості (крок 10)", "",
                f"λ = X(1) = {points[1]} rps. Точки, обмежені генератором: "
                f"{', '.join(f'N={n} ({chr(44).join(r)})' for n, r in excluded.items()) or 'немає'}.", "",
                "| N | X(N) | C(N) = X(N)/X(1) | u = N−1 | v = N(N−1) | y = N/C − 1 | у підборі |",
                "|---:|---:|---:|---:|---:|---:|---|"]
        out.append(f"| 1 | {points[1]} | 1.000 | 0 | 0 | 0 | — |")
        for n in sorted(points):
            if n == 1:
                continue
            c = points[n] / points[1]
            out.append(f"| {n} | {points[n]} | {c:.4f} | {n - 1} | {n * (n - 1)} | "
                       f"{n / c - 1:.4f} | {'ні' if n in excluded else 'так'} |")
        s = fit["sums"]
        out += ["", "Суми за точками в підборі: " + ", ".join(
            f"{k} = {v:.4f}" for k, v in s.items()), ""]
        if "raw" in fit:
            out.append(f"Оцінки МНК: σ = {fit['raw']['sigma']:.5f}, κ = {fit['raw']['kappa']:.5f}.")
        if fit.get("note"):
            out.append(f"Корекція: {fit['note']}.")
        if fit.get("sigma") is not None:
            out.append(f"**σ = {fit['sigma']:.5f}, κ = {fit['kappa']:.5f}**"
                       + (f", **N\\* = √((1 − σ)/κ) = {fit['N_star']:.2f}**" if fit.get("N_star")
                          else (f", межа λ/σ = {fit['limit']:.1f} rps" if fit.get("limit") else "")))
        out.append("")
        chart_usl(points, sds, excluded, fit, CHARTS / "usl.png")

    # ---------------------------------------------------------------- крок 11
    if 4 in per_n:
        d = per_n[4]
        run, best = d["run"], d["best"]
        a, b = run["t0_epoch"] + best["t_start_s"], run["t0_epoch"] + best["t_end_s"]
        ds = dstats_at(d["dstats"], a, b)
        gen = [s["gen_cpu_cores"] for s in run["seconds"]
               if best["window_s"][0] <= s["t_s"] < best["window_s"][1]]
        J["step11"] = {"dstats": ds, "gen_cpu_cores": statistics.fmean(gen) if gen else None,
                       "step_target": best["target_rps"]}
        out += [f"## Ресурси на сходинці X(4) = {run['X_rps']} rps (задана {best['target_rps']:g}), крок 11", "",
                "| Контейнер | CPU, % (100 % = 1 ядро) | Памʼять | Знімків |", "|---|---:|---|---:|"]
        for k, v in ds.items():
            out.append(f"| {k} | {v['cpu_pct']:.1f} | {v['mem']} | {v['n']} |")
        if gen:
            out.append(f"| генератор (процес) | {100 * statistics.fmean(gen):.1f} | — | {len(gen)} с |")
        out.append("")

    # процесорний час на запит за компонентами (docker stats + генератор)
    def comp_cpu(d, st):
        run = d["run"]
        a, b = run["t0_epoch"] + st["t_start_s"], run["t0_epoch"] + st["t_end_s"]
        ds = dstats_at(d["dstats"], a, b)
        if not ds:
            return None
        web = sum(v["cpu_pct"] for k, v in ds.items() if "web" in k) / 100
        db = sum(v["cpu_pct"] for k, v in ds.items() if k.endswith("db-1") or k == "db") / 100
        lb = sum(v["cpu_pct"] for k, v in ds.items() if k.endswith("lb-1") or k == "lb") / 100
        gen = [x["gen_cpu_cores"] for x in run["seconds"]
               if st["window_s"][0] <= x["t_s"] < st["window_s"][1]]
        gen = statistics.fmean(gen) if gen else 0.0
        x = st["achieved_rps"] or 1.0
        return {"web": web, "db": db, "lb": lb, "gen": gen, "x": x,
                "total": web + db + lb + gen}

    common = min((d["best"]["target_rps"] for d in per_n.values()), default=None)
    if per_n and any(d["dstats"] for d in per_n.values()):
        out += ["## Процесорний час на запит (крок 11)", "",
                "CPU — середнє «docker stats» за сходинку (ядер), генератор — його процеси. "
                "мс CPU на запит = ядер / досягнута інтенсивність × 1000.", "",
                "| N | Сходинка | Досягнута, rps | web, ядер | db | lb | генератор | Разом, ядер | "
                "web, мс/запит | db, мс/запит | lb, мс/запит | Запитів/с на екземпляр |",
                "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        rows_cpu = []
        for n, d in sorted(per_n.items()):
            for label, st in (("X(N)", d["best"]),
                              (f"спільна {common:g}", next((x for x in d["run"]["steps"]
                                                            if x["target_rps"] == common), None))):
                if st is None or (label != "X(N)" and st is d["best"]):
                    continue
                c = comp_cpu(d, st)
                if c is None:
                    continue
                rows_cpu.append({"N": n, "label": label, **c})
                out.append(
                    f"| {n} | {label} ({st['target_rps']:g}) | {c['x']} | {c['web']:.2f} | "
                    f"{c['db']:.2f} | {c['lb']:.2f} | {c['gen']:.2f} | {c['total']:.2f} | "
                    f"{1000 * c['web'] / c['x']:.3f} | {1000 * c['db'] / c['x']:.3f} | "
                    f"{1000 * c['lb'] / c['x']:.3f} | {c['x'] / n:.0f} |")
        J["cpu_per_request"] = rows_cpu
        out.append("")

    # М3: дроселювання від заданої інтенсивності
    thr_chart = {}
    for n in (1, 4):
        if n in per_n and per_n[n]["prom"]:
            run, prom = per_n[n]["run"], per_n[n]["prom"]
            per_inst = defaultdict(list)
            for st in run["steps"]:
                th = step_metrics(run, prom, st)["throttle"]
                for inst, v in th.items():
                    per_inst[prom.pods.get(inst, inst)].append((st["target_rps"], v))
            thr_chart[f"N={n}"] = dict(per_inst)
    if thr_chart:
        chart_throttle(thr_chart, CHARTS / "m3_throttle.png")
        J["m3"] = thr_chart

    # зупинка
    sp = LAB2 / "stop_test.json"
    if sp.exists():
        stp = json.loads(sp.read_text(encoding="utf-8"))
        J["stop_test"] = {k: v for k, v in stp.items() if k != "seconds"}
        out += ["## Коректна зупинка (крок 3)", "",
                f"Навантаження {stp['rate_rps']:g} rps на 2 екземпляри, зупинка `{stp['container']}` "
                f"на {stp['t_stop_rel_s']} с (завершився на {stp['t_stopped_rel_s']} с).", "",
                f"* код завершення: **{stp['exit_code']}**;",
                f"* у вікні {stp['window_s'][0]}–{stp['window_s'][1]} с: 2xx = {stp['window_ok']}, "
                f"не 2xx = {stp['window_non2xx']}, помилок зʼєднання = {stp['window_connerr']}, "
                f"найбільший посекундний p99 = {stp['window_p99_max_ms']} мс;",
                f"* за весь прогін: не 2xx = {stp['total_non2xx']}, помилок зʼєднання = "
                f"{stp['total_connerr']}.", ""]

    (LAB2 / "analysis.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    (LAB2 / "analysis.json").write_text(json.dumps(J, ensure_ascii=False, indent=1, default=str),
                                        encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
