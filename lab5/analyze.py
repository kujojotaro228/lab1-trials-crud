#!/usr/bin/env python3
"""Lab 5 analysis: results/lab5/raw/*.json -> results/lab5/tables.md, tables.json, fig/*.png.

    py lab5/analyze.py

Every value in the tables is the median of the three runs (computed per metric). Ratios
RW / G follow the rule of the assignment: if the global-lock value is 0, the difference
RW - G is written instead.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "results" / "lab5"
RAW = OUT / "raw"
FIG = OUT / "fig"


def load(prefix: str) -> list[dict]:
    files = sorted(RAW.glob(f"{prefix}_[0-9]*.json"))
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def mget(runs, *path):
    out = []
    for r in runs:
        v = r
        for p in path:
            v = v[p]
        out.append(v)
    return med(out)


def f(x, nd=2, dash="—"):
    if x is None:
        return dash
    if isinstance(x, float) and abs(x) >= 1000:
        return f"{x:,.0f}".replace(",", " ")
    return f"{x:.{nd}f}"


def i(x):
    return "—" if x is None else f"{int(round(x)):,}".replace(",", " ")


def ratio(rw, g):
    if rw is None or g is None:
        return "—"
    if g == 0:
        return f"Δ = {rw - g:+.3g}"
    return f"{rw / g:.3f}"


def role_summary(runs, role):
    keys = ("N", "X", "x", "p50_us", "p99_us", "h", "wait_max_ms", "w", "cv", "tau_mean_us")
    return {k: mget(runs, role, k) for k in keys}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    host = json.loads((OUT / "host.json").read_text(encoding="utf-8"))
    sel = json.loads((RAW / "selftest.json").read_text(encoding="utf-8"))
    tcs = load("tcs")
    x1 = {lk: load(f"x1_{lk}") for lk in ("global", "rw")}
    runs = {lk: load(f"run_{lk}") for lk in ("global", "rw")}
    extra = load("run_rwreader")
    n = runs["global"][0]["n"]
    n_w, n_r = runs["global"][0]["n_w"], runs["global"][0]["n_r"]

    t_w = med([r["t_cs_w_us"] for r in tcs])
    t_r = med([r["t_cs_r_us"] for r in tcs])
    t_bar = (n_w * t_w + n_r * t_r) / n
    bound = 1e6 / t_bar
    X1 = {lk: med([r["X1"] for r in x1[lk]]) for lk in x1}

    S = {}
    for lk in ("global", "rw"):
        S[lk] = {"write": role_summary(runs[lk], "write"), "read": role_summary(runs[lk], "read"),
                 "total": role_summary(runs[lk], "total"), "X": mget(runs[lk], "X")}
    if extra:
        S["rwreader"] = {"write": role_summary(extra, "write"), "read": role_summary(extra, "read"),
                         "total": role_summary(extra, "total"), "X": mget(extra, "X")}
    for lk in S:
        X = S[lk]["X"]
        S[lk]["write"]["phi"] = S[lk]["write"]["X"] / X
        S[lk]["read"]["phi"] = S[lk]["read"]["X"] / X

    inv_all = [r["invariants"]["ok"] for lk in runs for r in runs[lk]] + [r["invariants"]["ok"] for r in extra]
    inv_snap = sum(r["invariants"]["snapshots_sum_bins_ne_count"] for lk in runs for r in runs[lk])
    inv_snaps_total = sum(r["invariants"]["snapshot_calls_total"] for lk in runs for r in runs[lk])

    md = ["# ЛР 5: таблиці (медіана трьох прогонів)", ""]

    # ---------------------------------------------------------------- table 1
    md += ["## Таблиця 1. Стенд", "", "| Параметр | Значення |", "|---|---|",
           f"| n | {n} |", f"| n_w | {n_w} |", f"| n_r | {n_r} |",
           f"| Мова і версія | {host['implementation']} {host['python']} (GIL {'увімкнено' if host['gil_enabled'] else 'вимкнено'}, "
           f"switch interval {host['switch_interval_s'] * 1000:g} мс) |",
           f"| Логічних ядер | {host['logical_cpus']} |",
           f"| X_1, оп/с (глобальний замок / RWLock) | {f(X1['global'])} / {f(X1['rw'])} |",
           f"| Інваріанти після прогонів кроків 2 і 3 | {'виконано в усіх ' + str(len(inv_all)) + ' прогонах' if all(inv_all) else 'ПОРУШЕНО'}: "
           f"count = кількості викликів add, сума кошиків = count, sum і sum² = зваженим сумам кошиків; "
           f"знімків із сумою кошиків ≠ count — {inv_snap} з {i(inv_snaps_total)} |",
           ""]
    md += ["Самоперевірка стенду: " + "; ".join(f"{k} — {'так' if v else 'НІ'}" for k, v in sel["checks"].items()), ""]

    # ---------------------------------------------------------------- tables 2, 3
    def role_table(lk, title, with_tcs):
        s = S[lk]
        hdr = ["Роль", "Операцій за D", "X, оп/с", "p50, мкс", "p99, мкс", "Найбільший t_wait, мс", "w", "cv"]
        if with_tcs:
            hdr.append("t_cs, мкс")
        rows = []
        for name, key, tc in (("Запис", "write", t_w), ("Читання", "read", t_r), ("Разом", "total", t_bar)):
            r = s[key]
            row = [name, i(r["N"]), i(r["X"]), f(r["p50_us"]), f(r["p99_us"]), f(r["wait_max_ms"]),
                   f(r["w"], 3), f(r["cv"], 3)]
            if with_tcs:
                row.append(f(tc, 3) + (" (t̄_cs)" if key == "total" else ""))
            rows.append(row)
        out = [f"## {title}", "", "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
        out += ["| " + " | ".join(r) + " |" for r in rows]
        return out + [""]

    md += role_table("global", "Таблиця 2. Глобальний замок", True)
    md += [f"t̄_cs = ({n_w} · {t_w:.3f} + {n_r} · {t_r:.3f}) / {n} = {t_bar:.3f} мкс; "
           f"межа 1 / t̄_cs = {f(bound)} оп/с", ""]
    md += role_table("rw", "Таблиця 3. RWLock (політика phase)", False)
    if "rwreader" in S:
        md += role_table("rwreader", "Додатково. RWLock з перевагою читачів (політика reader)", False)

    # ---------------------------------------------------------------- table 4
    md += ["## Таблиця 4. Операції потоків за D секунд", "",
           "| Потік | Роль | Глобальний замок | RWLock |" + (" RWLock (reader) |" if extra else ""),
           "|---|---|---|---|" + ("---|" if extra else "")]
    for t in range(1, n + 1):
        g = med([r["per_thread"][str(t)]["count"] for r in runs["global"]])
        rw = med([r["per_thread"][str(t)]["count"] for r in runs["rw"]])
        row = f"| {t} | {'запис' if t % 2 else 'читання'} | {i(g)} | {i(rw)} |"
        if extra:
            row += f" {i(med([r['per_thread'][str(t)]['count'] for r in extra]))} |"
        md.append(row)
    md.append("")

    # ---------------------------------------------------------------- table 5
    XG, XR = S["global"]["X"], S["rw"]["X"]
    t5 = {
        "S_G": XG / X1["global"], "E_G": XG / X1["global"] / n,
        "S_RW": XR / X1["rw"], "E_RW": XR / X1["rw"] / n,
        "S_w": S["rw"]["write"]["X"] / S["global"]["write"]["X"],
        "S_r": S["rw"]["read"]["X"] / S["global"]["read"]["X"],
        "S_sum": XR / XG, "rho": XG * t_bar / 1e6,
    }
    md += ["## Таблиця 5. Метрики прискорення", "", "| Метрика | Формула | Значення |", "|---|---|---|",
           f"| S(n), глобальний замок | X(G) / X_1 = {f(XG)} / {f(X1['global'])} | {t5['S_G']:.3f} |",
           f"| E(n), глобальний замок | S(n) / n | {t5['E_G']:.3f} |",
           f"| S(n), RWLock | X(RW) / X_1 = {f(XR)} / {f(X1['rw'])} | {t5['S_RW']:.3f} |",
           f"| E(n), RWLock | S(n) / n | {t5['E_RW']:.3f} |",
           f"| S_w | X_w(RW) / X_w(G) | {t5['S_w']:.3f} |",
           f"| S_r | X_r(RW) / X_r(G) | {t5['S_r']:.3f} |",
           f"| S_Σ | X(RW) / X(G) | {t5['S_sum']:.3f} |",
           f"| ρ, завантаженість глобального замка | X(G) · t̄_cs = {f(XG)} · {t_bar:.3f} мкс | {t5['rho']:.3f} |",
           "", f"X(G) = {f(XG)} оп/с проти межі 1 / t̄_cs = {f(bound)} оп/с ({XG / bound * 100:.1f} % межі).", ""]

    # ---------------------------------------------------------------- table 6
    rows6 = [("X, оп/с", "X", i), ("x, на один потік ролі, оп/с", "x", i), ("p50, мкс", "p50_us", f),
             ("p99, мкс", "p99_us", f), ("h = p99 / p50", "h", f), ("Найбільший t_wait, мс", "wait_max_ms", f),
             ("w", "w", lambda v: f(v, 3)), ("cv", "cv", lambda v: f(v, 3)), ("φ, частка ролі в усіх операціях", "phi", lambda v: f(v, 3))]
    md += ["## Таблиця 6. Порівняння доступу до даних", "",
           "| Показник | Глобальний замок: запис | Глобальний замок: читання | RWLock: запис | RWLock: читання | RW / G: запис | RW / G: читання |",
           "|---|---|---|---|---|---|---|"]
    t6 = {}
    for name, key, fm in rows6:
        gw, gr = S["global"]["write"][key], S["global"]["read"][key]
        rw, rr = S["rw"]["write"][key], S["rw"]["read"][key]
        t6[key] = {"G_w": gw, "G_r": gr, "RW_w": rw, "RW_r": rr}
        md.append(f"| {name} | {fm(gw)} | {fm(gr)} | {fm(rw)} | {fm(rr)} | {ratio(rw, gw)} | {ratio(rr, gr)} |")
    md.append("")

    # ---------------------------------------------------------------- spread of the 3 runs
    md += ["## Розкид трьох прогонів (X, оп/с)", "", "| Прогін | X_w | X_r | X |", "|---|---|---|---|"]
    for lk, rs in (("global", runs["global"]), ("rw", runs["rw"]), ("rw reader", extra)):
        for k, r in enumerate(rs, 1):
            md.append(f"| {lk} #{k} | {i(r['write']['X'])} | {i(r['read']['X'])} | {i(r['X'])} |")
    md += ["", "| X_1 | #1 | #2 | #3 |", "|---|---|---|---|"]
    for lk in ("global", "rw"):
        md.append(f"| {lk} | " + " | ".join(i(r["X1"]) for r in x1[lk]) + " |")
    md += ["", "| t_cs | #1 | #2 | #3 |", "|---|---|---|---|",
           "| запис, мкс | " + " | ".join(f(r["t_cs_w_us"], 3) for r in tcs) + " |",
           "| читання, мкс | " + " | ".join(f(r["t_cs_r_us"], 3) for r in tcs) + " |", ""]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tables.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (OUT / "tables.json").write_text(json.dumps({"S": S, "X1": X1, "t_cs_w_us": t_w, "t_cs_r_us": t_r,
                                                 "t_bar_us": t_bar, "bound": bound, "t5": t5, "t6": t6,
                                                 "host": host}, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n".join(md))

    # ---------------------------------------------------------------- figures
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: no figures")
        return
    FIG.mkdir(parents=True, exist_ok=True)
    cfg = [("global", "Глобальний замок", "#4C72B0"), ("rw", "RWLock (phase)", "#DD8452")]
    if "rwreader" in S:
        cfg.append(("rwreader", "RWLock (reader)", "#55A868"))
    fig, ax = plt.subplots(figsize=(6.4, 3.4), dpi=150)
    width = 0.8 / len(cfg)
    for j, (lk, lab, col) in enumerate(cfg):
        vals = [S[lk]["write"]["X"], S[lk]["read"]["X"]]
        ax.bar([k + (j - (len(cfg) - 1) / 2) * width for k in range(2)], vals, width, label=lab, color=col)
    ax.set_xticks([0, 1], ["запис X_w", "читання X_r"])
    ax.set_ylabel("оп/с")
    ax.set_title("Швидкість запису і читання, n = 9", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG / "throughput.png")
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(7.6, 3.2), dpi=150, sharey=True)
    qs = ["0.1", "0.25", "0.5", "0.75", "0.9", "0.95", "0.99", "0.999", "0.9999"]
    for ax, role, title in ((axes[0], "write", "Запис"), (axes[1], "read", "Читання")):
        for lk, lab, col in cfg:
            rs = {"global": runs["global"], "rw": runs["rw"], "rwreader": extra}[lk]
            vals = [med([r[role]["percentiles_us"][q] for r in rs]) for q in qs]
            ax.plot([float(q) for q in qs], vals, marker="o", ms=3, color=col, label=lab)
        ax.set_xscale("logit")
        ax.set_yscale("log")
        ax.set_title(f"{title}: перцентилі τ", fontsize=10)
        ax.set_xlabel("рівень перцентиля q", fontsize=8)
        ax.grid(alpha=0.3, which="both")
    axes[0].set_ylabel("τ, мкс")
    axes[0].legend(fontsize=7, frameon=False)
    fig.tight_layout()
    fig.savefig(FIG / "percentiles.png")
    plt.close(fig)
    print(f"-> {OUT / 'tables.md'}, {FIG}")


if __name__ == "__main__":
    main()
