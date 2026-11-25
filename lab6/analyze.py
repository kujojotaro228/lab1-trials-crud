#!/usr/bin/env python3
"""Lab 6 analysis: results/lab6/raw/*.json -> results/lab6/tables.md, tables.json, fig/*.png.

    py lab6/analyze.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

RAW = common.RAW
FIG = common.OUT / "fig"


def load(name):
    p = RAW / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def f(x, nd=2):
    if x is None:
        return "—"
    if isinstance(x, (int,)) and not isinstance(x, bool):
        return f"{x:,}".replace(",", " ")
    if abs(x) >= 1000:
        return f"{x:,.0f}".replace(",", " ")
    return f"{x:.{nd}f}"


def g(x):
    return "—" if x is None else f"{x:.2e}"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    host = json.loads((common.OUT / "host.json").read_text(encoding="utf-8"))
    PM, B = host["P_max"], host["B"]
    b = B // PM
    md = ["# ЛР 6: таблиці", ""]
    out = {"host": host}

    # ---------------------------------------------------------------- table 1
    md += ["## Таблиця 1. Стенд", "",
           "| Процесор | Фізичних ядер C_phys | Логічних ядер | Памʼять, ГБ | ОС | Python | PyTorch | n_th |",
           "|---|---|---|---|---|---|---|---|",
           f"| {host['cpu']} | {host['C_phys']} | {host['logical']} | {f(host['memory_gb'], 1)} | {host['os']} | "
           f"{host['python']} | {host['torch']} | {host['n_th']} = ⌊{host['C_phys']} / {PM}⌋ |", "",
           "| Датасет | K | C | c1 / c2 / h | n_params | M, МіБ | N | B | P_max |",
           "|---|---|---|---|---|---|---|---|---|",
           f"| {host['dataset']} | {host['K']} | {host['C']} | {host['c1']} / {host['c2']} / {host['h']} | "
           f"{f(host['n_params'])} | {host['M_MiB']:.2f} | {f(host['N'])} | {B} | {PM} |", "",
           f"n_params за формулою = {f(host['n_params'])}, порахована в моделі = {f(host['n_params_counted'])}, "
           f"тензорів параметрів — {host['n_tensors']}; M = 4 · n_params = {f(host['M_bytes'])} байт; "
           f"тестова частина — {host.get('n_test')} прикладів.", ""]

    # ---------------------------------------------------------------- table 2
    s1t, s1s = load("s1_single_train"), load("s1_single_speed")
    gc, man = load("s2_gradcheck"), load("s2_manual_speed")
    ddp, ns, dtr = load("s2_ddp_speed"), load("s2_ddp_nosync_speed"), load("s2_ddp_train")
    t2 = {}
    if s1s and man and ddp and ns:
        t_comm = ddp["t_step_ms"] - ns["t_step_ms"]
        t2 = {"single": s1s, "manual": man, "ddp": ddp, "nosync": ns, "t_comm_exp_ms": t_comm,
              "c": t_comm / ddp["t_step_ms"], "manual_share": man["t_sync_ms"] / man["t_step_ms"]}
        md += ["## Таблиця 2. Синхронізація градієнтів", "",
               "| Режим | t_step, мс | Час обміну на крок, мс | Частка кроку в обміні | X, прикл./с |",
               "|---|---|---|---|---|",
               f"| 1 процес, батч B | {f(s1s['t_step_ms'])} | 0 | 0 | {f(s1s['X'], 1)} |",
               f"| {PM} процеси, ручна синхронізація | {f(man['t_step_ms'])} | {f(man['t_sync_ms'], 3)} (t_sync) | "
               f"{t2['manual_share']:.3f} | {f(man['X'], 1)} |",
               f"| {PM} процеси, DDP | {f(ddp['t_step_ms'])} | {f(t_comm, 3)} (t_comm,exp) | {t2['c']:.3f} | {f(ddp['X'], 1)} |",
               f"| {PM} процеси, DDP, no_sync | {f(ns['t_step_ms'])} | 0 | 0 | {f(ns['X'], 1)} |", ""]
        md += ["| max \\|g − ḡ\\| / max \\|g\\| | Різниця сум ваг між процесами після 50 кроків | ACC, 1 процес | "
               f"ACC, DDP на {PM} | AUC, 1 процес | AUC, DDP на {PM} |", "|---|---|---|---|---|---|",
               f"| {g(gc['max_ratio']) if gc else '—'} | {g(man['weight_sum_spread'])} | "
               f"{f(s1t['acc'], 4) if s1t else '—'} | {f(dtr['acc'], 4) if dtr else '—'} | "
               f"{f(s1t['auc'], 4) if s1t else '—'} | {f(dtr['auc'], 4) if dtr else '—'} |", ""]
        if gc:
            md += ["Відношення за тензорами параметрів (крок еквівалентності):", "",
                   "| Тензор | Форма | max \\|g − ḡ\\| | max \\|g\\| | Відношення |", "|---|---|---|---|---|"]
            md += [f"| {x['tensor']} | {x['shape']} | {g(x['max_abs_diff'])} | {g(x['max_abs_g'])} | {g(x['ratio'])} |"
                   for x in gc["per_tensor"]]
            md.append("")
        md += [f"Суми ваг після 50 кроків: ручна синхронізація {man['weight_sums_step50']}, DDP "
               f"{ddp['weight_sums_step50']}, DDP no_sync {ns['weight_sums_step50']} "
               f"(різниця {g(ns['weight_sum_spread'])} — без обміну ваги процесів розходяться).", ""]
        if s1t and dtr:
            n = min(len(s1t["losses"]), len(dtr["losses"]))
            dl = [abs(s1t["losses"][k]["loss"] - dtr["losses"][k]["loss"]) for k in range(n)]
            t2["loss_diff_max"] = max(dl)
            md += [f"Втрата 1 процесу і DDP на {PM} процесах на однакових кроках (один і той самий батч B, "
                   f"бо DistributedSampler ділить ту саму перестановку): найбільша різниця за {n} кроків — "
                   f"{g(max(dl))}; кроків оптимізатора {s1t['steps_total']} і {dtr['steps_total']}.", ""]
    out["t2"] = t2

    # ---------------------------------------------------------------- table 3
    rows3 = {}
    for P in range(1, PM + 1):
        sp = load(f"s3_ddp_speed_p{P}") if P < PM else ddp
        nsp = load(f"s3_ddp_nosync_speed_p{P}") if P < PM else ns
        if sp and nsp:
            rows3[P] = {"b": B // P, "t": sp["t_step_ms"], "tns": nsp["t_step_ms"], "X": sp["X"]}
    if 1 in rows3:
        X1 = rows3[1]["X"]
        md += ["## Таблиця 3. Strong scaling, B = const", "",
               "| P | b | t_step, мс | t_step,ns, мс | X, прикл./с | S | E | e | c |", "|---|---|---|---|---|---|---|---|---|"]
        for P in range(1, 5):
            r = rows3.get(P)
            if r is None:
                md.append(f"| {P} | — | — | — | — | — | — | — | — |")
                continue
            S = r["X"] / X1
            E = S / P
            e = (1 / S - 1 / P) / (1 - 1 / P) if P > 1 else None
            c = (r["t"] - r["tns"]) / r["t"]
            r.update({"S": S, "E": E, "e": e, "c": c})
            md.append(f"| {P} | {r['b']} | {f(r['t'])} | {f(r['tns'])} | {f(r['X'], 1)} | {S:.3f} | {E:.3f} | "
                      f"{'−' if e is None else f'{e:.3f}'} | {c:.3f} |")
        md += ["", f"Рядки з P > {PM} не заповнюються (P_max = {PM}).", ""]
    out["t3"] = rows3

    # ---------------------------------------------------------------- table 4
    rows4 = {}
    w1 = load("s4_ddp_speed_p1")
    if w1 and PM in rows3:
        rows4[1] = {"B": b, "t": w1["t_step_ms"], "X": w1["X"]}
        rows4[PM] = {"B": B, "t": rows3[PM]["t"], "X": rows3[PM]["X"]}
        for P in range(2, PM):
            r = load(f"s4_ddp_speed_p{P}")
            if r:
                rows4[P] = {"B": P * b, "t": r["t_step_ms"], "X": r["X"]}
        md += ["## Таблиця 4. Weak scaling, b = const", "", f"b = B / P_max = {b}.", "",
               "| P | B_P | t_step, мс | X, прикл./с | S_w | E_w |", "|---|---|---|---|---|---|"]
        for P in range(1, 5):
            r = rows4.get(P)
            if r is None:
                md.append(f"| {P} | — | — | — | — | — |")
                continue
            Sw = r["X"] / rows4[1]["X"]
            r.update({"S_w": Sw, "E_w": Sw / P})
            md.append(f"| {P} | {r['B']} | {f(r['t'])} | {f(r['X'], 1)} | {Sw:.3f} | {Sw / P:.3f} |")
        md += ["", f"Рядок P = {PM} — з таблиці 3.", ""]
    out["t4"] = rows4

    # ---------------------------------------------------------------- table 5
    q1, q2, q3 = load("s4_train_p1_b"), dtr, load("s4_train_pmax_lrP")
    if q1 and q2 and q3:
        md += ["## Таблиця 5. Якість за різних глобальних батчів", "",
               "| Режим | B_P | η | Кроків оптимізатора за n_ep епох | ACC | AUC | Найбільша втрата за останню епоху |",
               "|---|---|---|---|---|---|---|"]
        for lab, r in (("P = 1", q1), (f"P = {PM}, з кроку 2", q2), (f"P = {PM}, η_P = η₀ · {PM}", q3)):
            md.append(f"| {lab} | {r['B']} | {r['lr']:g} | {r['steps_total']} | {r['acc']:.4f} | {r['auc']:.4f} | "
                      f"{r['max_loss_last_epoch']:.4f} |")
        md.append("")
        for lab, r in (("P = 1, b", q1), ("P_max, η₀", q2), ("P_max, η_P", q3)):
            ls = [x["loss"] for x in r["losses"]]
            bad = [x for x in ls if not math.isfinite(x)]
            md.append(f"* {lab}: n_e = {r['n_e']} кроків за епоху, втрата першого кроку {ls[0]:.4f}, "
                      f"найбільша за весь прогін {max(ls):.4f}, останнього кроку {ls[-1]:.4f}"
                      + (f", нескінченних/NaN значень {len(bad)}" if bad else ""))
        md.append("")
    out["t5"] = {"p1": q1 and {k: q1[k] for k in ("B", "lr", "steps_total", "acc", "auc", "max_loss_last_epoch")},
                 "pm": q2 and {k: q2[k] for k in ("B", "lr", "steps_total", "acc", "auc", "max_loss_last_epoch")},
                 "pm_lr": q3 and {k: q3[k] for k in ("B", "lr", "steps_total", "acc", "auc", "max_loss_last_epoch")}}

    # ---------------------------------------------------------------- table 6
    s5 = load("s5_summary")
    s5r = load("s5_fail_train")
    if s5 and s5r and dtr:
        ev = []
        for fp in sorted(RAW.glob("s5_events*.jsonl")):
            ev += [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]
        steps_before = {e["step"]: e["loss"] for e in ev if e["event"] == "step" and e["restart"] == 0}
        steps_after = {e["step"]: e["loss"] for e in ev if e["event"] == "step" and e["restart"] >= 1}
        nofail = {x["step"]: x["loss"] for x in dtr["losses"]}
        sf = s5["s_f"]
        ks = list(range(sf - 2, sf + 3))

        def loss_fail(k):
            return steps_after.get(k, steps_before.get(k))
        md += ["## Таблиця 6. Відмова і відновлення", "",
               "| K_c | s_f | Крок останньої контрольної точки | Кроків виконано повторно | t_lost, с |", "|---|---|---|---|---|",
               f"| {s5['K_c']} | {sf} | {s5['last_ckpt_step']} | {s5['steps_redone']} | {f(s5.get('t_lost'), 2)} |", "",
               "| Крок | " + " | ".join(f"{k}" for k in ks) + " | ACC після n_ep епох |",
               "|---|" + "---|" * (len(ks) + 1),
               "| Втрата, прогін з відмовою | " + " | ".join(f"{loss_fail(k):.6f}" if loss_fail(k) is not None else "—" for k in ks)
               + f" | {s5r['acc']:.4f} |",
               "| Втрата, прогін без відмови | " + " | ".join(f"{nofail[k]:.6f}" if k in nofail else "—" for k in ks)
               + f" | {dtr['acc']:.4f} |", ""]
        md += [f"t_f − t_c = {f(s5.get('t_f_minus_t_c'), 3)} с, t_r = {f(s5.get('t_r'), 3)} с; повторно виконано кроки "
               f"{s5['last_ckpt_step'] + 1}–{s5['fail_step'] - 1}; навчання продовжилось з кроку {s5['restored_step']}.", ""]
        redo = sorted(k for k in steps_before if k in steps_after)
        if redo:
            md += ["Повторно виконані кроки — втрата до відмови і після відновлення:", "",
                   "| Крок | До відмови | Після відновлення | Без відмови |", "|---|---|---|---|"]
            md += [f"| {k} | {steps_before[k]:.6f} | {steps_after[k]:.6f} | {nofail.get(k, float('nan')):.6f} |" for k in redo]
            md.append("")
        all_steps = sorted(set(steps_before) | set(steps_after))
        diffs = [abs(loss_fail(k) - nofail[k]) for k in all_steps if k in nofail]
        md.append(f"Найбільша різниця втрати прогону з відмовою і без відмови за всі {len(diffs)} кроків: "
                  f"{g(max(diffs)) if diffs else '—'}; ACC {s5r['acc']:.4f} проти {dtr['acc']:.4f}, "
                  f"AUC {s5r['auc']:.4f} проти {dtr['auc']:.4f}.")
        md.append("")
        out["t6"] = {"summary": s5, "loss_fail": {k: loss_fail(k) for k in ks},
                     "loss_nofail": {k: nofail.get(k) for k in ks}, "max_diff": max(diffs) if diffs else None,
                     "acc_fail": s5r["acc"], "acc_nofail": dtr["acc"]}

    common.OUT.mkdir(parents=True, exist_ok=True)
    (common.OUT / "tables.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (common.OUT / "tables.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
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
    if rows3:
        Ps = sorted(rows3)
        fig, ax = plt.subplots(figsize=(5.2, 3.4), dpi=150)
        ax.plot(Ps, [rows3[P]["X"] for P in Ps], "o-", label="X(P), DDP, B = const")
        ax.plot(Ps, [P * rows3[1]["X"] for P in Ps], "--", color="gray", label="ідеальне P · X(1)")
        if rows4:
            P4 = sorted(rows4)
            ax.plot(P4, [rows4[P]["X"] for P in P4], "s-", label="X(P), weak scaling, b = const")
        ax.set_xticks(Ps)
        ax.set_xlabel("P, процесів")
        ax.set_ylabel("прикладів за секунду")
        ax.set_title("Пропускна здатність навчання", fontsize=10)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, frameon=False)
        fig.tight_layout()
        fig.savefig(FIG / "scaling.png")
        plt.close(fig)
    runs = [("1 процес, B = 192, η₀", s1t), (f"DDP {PM} процеси, B = 192, η₀", dtr),
            ("DDP 1 процес, B = 96, η₀", q1), (f"DDP {PM} процеси, B = 192, η_P", q3)]
    runs = [(lab, r) for lab, r in runs if r]
    if runs:
        fig, ax = plt.subplots(figsize=(6.4, 3.4), dpi=150)
        for lab, r in runs:
            ex = [x["step"] / r["n_e"] for x in r["losses"]]
            ax.plot(ex, [x["loss"] for x in r["losses"]], lw=1, label=lab)
        ax.set_xlabel("епоха (крок / n_e)")
        ax.set_ylabel("втрата на кроці")
        ax.set_title("Втрата навчання", fontsize=10)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7, frameon=False)
        fig.tight_layout()
        fig.savefig(FIG / "loss.png")
        plt.close(fig)
    print(f"-> {common.OUT / 'tables.md'}, {FIG}")


if __name__ == "__main__":
    main()
