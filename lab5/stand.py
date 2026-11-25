#!/usr/bin/env python3
"""Lab 5 stand (variant 25: C5 aggregator, n = 9 threads, 25 buckets).

Threads 1..n: odd numbers only write (add), even numbers only read (snapshot). Operations go
back to back with no pauses; the argument (value 0..24) is generated before t_call.

    py lab5/stand.py all                      # everything for the report (~4 min)
    py lab5/stand.py run --lock global        # one n-thread run, prints the summary
    py lab5/stand.py run --lock rw [--policy reader]
    py lab5/stand.py x1 --lock global|rw      # X_1: one thread, writes:reads = n_w:n_r
    py lab5/stand.py tcs                      # t_cs,w and t_cs,r: one thread, global lock
    py lab5/stand.py selftest                 # snapshot() changes nothing; exclusion checks

Timing: perf_counter_ns. t_call — before the lock is requested, t_acq — right after it is
obtained (returned by the structure), t_ret — after the operation returned, i.e. after the
lock was released. tau = t_ret - t_call, t_wait = t_acq - t_call. Every run: 1 s warm-up
that is not counted, then D = 10 s; an operation belongs to the measurement if its t_call
lies in [t_start, t_end).

"all" runs every measurement in a separate process (fresh interpreter for each run) and
writes results/lab5/raw/*.json and the log results/lab5/logs/lab5.log.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from array import array
from pathlib import Path
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregator  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "results" / "lab5"
RAW = OUT / "raw"

N_THREADS = 9                     # variant 25
D_SEC = 10.0
WARMUP_SEC = 1.0
REPEATS = 3
QS = (0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 0.999, 0.9999)


def roles(n: int) -> tuple[int, int]:
    return math.ceil(n / 2), n // 2


def percentile_sorted(sorted_vals, q: float):
    """p_q = tau_(ceil(q*N)), 1-based, on an ascending list."""
    n = len(sorted_vals)
    if n == 0:
        return None
    k = max(1, math.ceil(q * n))
    return int(sorted_vals[k - 1])


def sort_values(arrs: list[array]):
    try:
        import numpy as np
        if not arrs:
            return np.zeros(0, dtype=np.int64)
        v = np.concatenate([np.frombuffer(a, dtype=np.int64) for a in arrs]) if arrs else None
        v.sort()
        return v
    except ImportError:
        out = []
        for a in arrs:
            out.extend(a)
        out.sort()
        return out


def role_stats(threads: list[dict], key_tau: str, key_wait: str, D: float) -> dict:
    k = len(threads)
    counts = [t["count"] for t in threads]
    N = sum(counts)
    taus = sort_values([t[key_tau] for t in threads])
    wait_sum = sum(t["wait_sum"] for t in threads)
    wait_max = max((t["wait_max"] for t in threads), default=0)
    mu = N / k if k else 0
    sigma = math.sqrt(sum((c - mu) ** 2 for c in counts) / k) if k else 0
    pq = {str(q): percentile_sorted(taus, q) for q in QS}
    tau_sum = int(sum(t["tau_sum"] for t in threads))
    return {
        "threads": k, "N": N, "X": N / D, "x": N / D / k if k else None,
        "counts": counts, "mu": mu, "sigma": sigma, "cv": sigma / mu if mu else None,
        "p50_us": pq["0.5"] / 1000 if pq["0.5"] is not None else None,
        "p99_us": pq["0.99"] / 1000 if pq["0.99"] is not None else None,
        "h": (pq["0.99"] / pq["0.5"]) if pq["0.5"] else None,
        "tau_mean_us": tau_sum / N / 1000 if N else None,
        "percentiles_us": {q: (v / 1000 if v is not None else None) for q, v in pq.items()},
        "wait_sum_s": wait_sum / 1e9, "w": wait_sum / 1e9 / (k * D) if k else None,
        "wait_max_ms": wait_max / 1e6,
    }


# ---------------------------------------------------------------------------- n-thread run
def cmd_run(args) -> dict:
    n = args.n
    n_w, n_r = roles(n)
    agg = aggregator.make(args.lock, args.policy)
    times = {}

    def set_times():
        now = perf_counter_ns()
        times["start"] = now + int(WARMUP_SEC * 1e9)
        times["end"] = times["start"] + int(args.D * 1e9)
        times["launch"] = now

    barrier = threading.Barrier(n, action=set_times)
    results: dict[int, dict] = {}

    def writer(idx: int) -> None:
        r = random.Random(1000 + idx).randrange
        add = agg.add
        pc = perf_counter_ns
        taus, waits = array("q"), array("q")
        tap, wap = taus.append, waits.append
        calls = count = wait_sum = wait_max = tau_sum = 0
        barrier.wait()
        t_start, t_end = times["start"], times["end"]
        while True:
            v = r(25)
            tc = pc()
            if tc >= t_end:
                break
            ta = add(v)
            tr = pc()
            calls += 1
            if tc >= t_start:
                w = ta - tc
                tau = tr - tc
                tap(tau)
                wap(w)
                count += 1
                wait_sum += w
                tau_sum += tau
                if w > wait_max:
                    wait_max = w
        results[idx] = {"role": "w", "calls": calls, "count": count, "taus": taus,
                        "wait_sum": wait_sum, "wait_max": wait_max, "tau_sum": tau_sum}

    def reader(idx: int) -> None:
        snap = agg.snapshot
        pc = perf_counter_ns
        taus = array("q")
        tap = taus.append
        calls = count = wait_sum = wait_max = tau_sum = 0
        bad_sum = bad_mono = 0
        last_count = -1
        barrier.wait()
        t_start, t_end = times["start"], times["end"]
        while True:
            tc = pc()
            if tc >= t_end:
                break
            ta, s = snap()
            tr = pc()
            calls += 1
            # invariant of every snapshot: sum of buckets == count; counts never go back
            if sum(s[0]) != s[1]:
                bad_sum += 1
            if s[1] < last_count:
                bad_mono += 1
            last_count = s[1]
            if tc >= t_start:
                w = ta - tc
                tau = tr - tc
                tap(tau)
                count += 1
                wait_sum += w
                tau_sum += tau
                if w > wait_max:
                    wait_max = w
        results[idx] = {"role": "r", "calls": calls, "count": count, "taus": taus,
                        "wait_sum": wait_sum, "wait_max": wait_max, "tau_sum": tau_sum,
                        "bad_sum": bad_sum, "bad_mono": bad_mono}

    ths = []
    for i in range(1, n + 1):
        f = writer if i % 2 == 1 else reader
        ths.append(threading.Thread(target=f, args=(i,), name=f"t{i}"))
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    D = (times["end"] - times["start"]) / 1e9

    W = [results[i] for i in range(1, n + 1, 2)]
    Rr = [results[i] for i in range(2, n + 1, 2)]
    state = agg.state()
    bins, count, total, total_sq, mutations = state
    add_calls = sum(t["calls"] for t in W)
    inv = {
        "count_eq_add_calls": count == add_calls,
        "mutations_eq_add_calls": mutations == add_calls,
        "sum_bins_eq_count": sum(bins) == count,
        "sum_eq_weighted_bins": total == sum(i * b for i, b in enumerate(bins)),
        "sumsq_eq_weighted_bins": total_sq == sum(i * i * b for i, b in enumerate(bins)),
        "snapshots_sum_bins_ne_count": sum(t["bad_sum"] for t in Rr),
        "snapshots_count_decreased": sum(t["bad_mono"] for t in Rr),
        "add_calls_total": add_calls, "count_after": count,
        "snapshot_calls_total": sum(t["calls"] for t in Rr),
    }
    inv["ok"] = (inv["count_eq_add_calls"] and inv["mutations_eq_add_calls"] and inv["sum_bins_eq_count"]
                 and inv["sum_eq_weighted_bins"] and inv["sumsq_eq_weighted_bins"]
                 and inv["snapshots_sum_bins_ne_count"] == 0 and inv["snapshots_count_decreased"] == 0)
    wst = role_stats(W, "taus", None, D)
    rst = role_stats(Rr, "taus", None, D)
    allst = role_stats(W + Rr, "taus", None, D)     # row "total" of tables 2 and 3
    res = {
        "mode": "run", "lock": args.lock, "policy": args.policy if args.lock == "rw" else None,
        "n": n, "n_w": n_w, "n_r": n_r, "D": D, "warmup": WARMUP_SEC,
        "write": wst, "read": rst, "total": allst,
        "X": wst["X"] + rst["X"],
        "per_thread": {str(i): {"role": results[i]["role"], "count": results[i]["count"],
                                "calls": results[i]["calls"]} for i in range(1, n + 1)},
        "invariants": inv, "env": env_info(),
    }
    return res


# ---------------------------------------------------------------------------- X_1
def cmd_x1(args) -> dict:
    """One thread alternates writes and reads in the proportion n_w : n_r under the lock."""
    n_w, n_r = roles(args.n)
    agg = aggregator.make(args.lock, args.policy)
    # pattern W R W R ... with n_w writes and n_r reads per cycle (9 threads: 5 W + 4 R)
    pattern = []
    for i in range(1, args.n + 1):
        pattern.append(i % 2 == 1)
    r = random.Random(7).randrange
    add, snap, pc = agg.add, agg.snapshot, perf_counter_ns
    now = pc()
    t_start = now + int(WARMUP_SEC * 1e9)
    t_end = t_start + int(args.D * 1e9)
    nw = nr = calls_w = 0
    i = 0
    L = len(pattern)
    while True:
        is_w = pattern[i]
        i = i + 1 if i + 1 < L else 0
        if is_w:
            v = r(25)
            tc = pc()
            if tc >= t_end:
                break
            add(v)
            calls_w += 1
            if tc >= t_start:
                nw += 1
        else:
            tc = pc()
            if tc >= t_end:
                break
            _, s = snap()
            if tc >= t_start:
                nr += 1
    D = (t_end - t_start) / 1e9
    st = agg.state()
    return {"mode": "x1", "lock": args.lock, "policy": args.policy if args.lock == "rw" else None,
            "n": args.n, "pattern": "".join("W" if p else "R" for p in pattern),
            "D": D, "N_w": nw, "N_r": nr, "X1": (nw + nr) / D, "X1_w": nw / D, "X1_r": nr / D,
            "invariants": {"count_eq_add_calls": st[1] == calls_w, "sum_bins_eq_count": sum(st[0]) == st[1]},
            "env": env_info()}


# ---------------------------------------------------------------------------- t_cs
def cmd_tcs(args) -> dict:
    """Mean hold time of the global lock in a write and in a read, one thread, no contention."""
    agg = aggregator.GlobalLockAggregator()
    r = random.Random(3).randrange
    pc = perf_counter_ns
    out = {}
    for role in ("w", "r"):
        t_end_warm = pc() + int(WARMUP_SEC * 1e9)
        t_end = t_end_warm + int(args.D * 1e9)
        hold_sum = n = 0
        while True:
            if role == "w":
                v = r(25)
                if pc() >= t_end:
                    break
                ta, te = agg.add_hold(v)
            else:
                if pc() >= t_end:
                    break
                ta, te = agg.snapshot_hold()
            if ta >= t_end_warm:
                hold_sum += te - ta
                n += 1
        out[role] = {"N": n, "t_cs_us": hold_sum / n / 1000}
    return {"mode": "tcs", "lock": "global", "D": args.D, "t_cs_w_us": out["w"]["t_cs_us"],
            "t_cs_r_us": out["r"]["t_cs_us"], "N_w": out["w"]["N"], "N_r": out["r"]["N"],
            "env": env_info()}


# ---------------------------------------------------------------------------- self-test
def cmd_selftest(args) -> dict:
    res = {"mode": "selftest", "checks": {}}
    ok = True
    for lock in ("global", "rw"):
        agg = aggregator.make(lock)
        rnd = random.Random(11)
        for _ in range(10_000):
            agg.add(rnd.randrange(25))
        before = agg.state()
        for _ in range(100_000):
            agg.snapshot()
        after = agg.state()
        same = before == after
        res["checks"][f"{lock}: 100000 snapshot() calls leave all fields unchanged"] = same
        ok &= same
    # RWLock: two readers inside at the same time; a writer excluded while they are inside
    lk = aggregator.RWLock("phase")
    lk.acquire_read()
    lk.acquire_read()
    st = lk.debug_state()
    two = st["readers"] == 2
    got = threading.Event()

    def w():
        lk.acquire_write()
        got.set()
        lk.release_write()
    th = threading.Thread(target=w)
    th.start()
    time.sleep(0.2)
    blocked = not got.is_set()
    lk.release_read()
    lk.release_read()
    th.join(timeout=2)
    res["checks"]["rw: two readers hold the lock at once"] = two
    res["checks"]["rw: a writer waits while readers are inside"] = blocked and got.is_set()
    ok &= two and blocked and got.is_set()
    # phase policy: a reader arriving while a writer waits must wait (writer preference)
    lk = aggregator.RWLock("phase")
    lk.acquire_read()
    wgot, rgot = threading.Event(), threading.Event()

    def w2():
        lk.acquire_write()
        wgot.set()
        time.sleep(0.1)
        lk.release_write()

    def r2():
        lk.acquire_read()
        rgot.set()
        lk.release_read()
    tw = threading.Thread(target=w2)
    tw.start()
    time.sleep(0.1)
    tr = threading.Thread(target=r2)
    tr.start()
    time.sleep(0.1)
    reader_waited = not rgot.is_set()
    lk.release_read()
    tw.join(2)
    tr.join(2)
    res["checks"]["rw phase: a new reader waits while a writer is waiting"] = reader_waited and rgot.is_set()
    ok &= reader_waited and rgot.is_set()
    res["ok"] = ok
    return res


# ---------------------------------------------------------------------------- environment
def env_info() -> dict:
    gil = None
    if hasattr(sys, "_is_gil_enabled"):
        gil = sys._is_gil_enabled()
    try:
        import numpy
        npv = numpy.__version__
    except ImportError:
        npv = None
    return {"python": sys.version.split()[0], "implementation": platform.python_implementation(),
            "gil_enabled": True if gil is None else gil, "switch_interval_s": sys.getswitchinterval(),
            "platform": platform.platform(), "processor": platform.processor(),
            "logical_cpus": os.cpu_count(), "numpy": npv}


def summary_line(r: dict) -> str:
    if r["mode"] == "run":
        w, rd = r["write"], r["read"]
        return (f"{r['lock']}{'/' + r['policy'] if r['policy'] else ''} n={r['n']}: "
                f"X_w={w['X']:.0f} X_r={rd['X']:.0f} X={r['X']:.0f} op/s | "
                f"p50/p99 w={w['p50_us']:.2f}/{w['p99_us']:.2f} us r={rd['p50_us']:.2f}/{rd['p99_us']:.2f} us | "
                f"max t_wait w={w['wait_max_ms']:.2f} ms | w_w={w['w']:.3f} w_r={rd['w']:.3f} | "
                f"invariants {'OK' if r['invariants']['ok'] else 'FAILED'} | per thread "
                + " ".join(f"{i}{v['role']}:{v['count']}" for i, v in r["per_thread"].items()))
    if r["mode"] == "x1":
        return f"X_1 ({r['lock']}, {r['pattern']}): {r['X1']:.0f} op/s (N_w={r['N_w']}, N_r={r['N_r']})"
    if r["mode"] == "tcs":
        return f"t_cs,w = {r['t_cs_w_us']:.3f} us, t_cs,r = {r['t_cs_r_us']:.3f} us (global lock, 1 thread)"
    if r["mode"] == "selftest":
        return "selftest " + ("OK" if r["ok"] else "FAILED") + ": " + json.dumps(r["checks"], ensure_ascii=False)
    return json.dumps(r)


# ---------------------------------------------------------------------------- orchestration
def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    (OUT / "logs").mkdir(parents=True, exist_ok=True)
    with open(OUT / "logs" / "lab5.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def child(name: str, argv: list[str]) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / f"{name}.json"
    cmd = [sys.executable, str(Path(__file__).resolve()), *argv, "--out", str(out)]
    log(f"start {name}: {' '.join(argv)}")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        log(f"FAILED {name}: {p.stderr.strip()[-2000:]}")
        raise SystemExit(1)
    r = json.loads(out.read_text(encoding="utf-8"))
    log(f"done  {name}: {summary_line(r)}")
    return r


def cmd_all(args) -> None:
    log(f"==== lab5 all: n={args.n}, D={args.D} s, warm-up {WARMUP_SEC} s, {REPEATS} repeats")
    (OUT).mkdir(parents=True, exist_ok=True)
    env = env_info()
    (OUT / "host.json").write_text(json.dumps(env, indent=1), encoding="utf-8")
    log(f"host: {json.dumps(env, ensure_ascii=False)}")
    child("selftest", ["selftest"])
    for k in range(1, REPEATS + 1):
        child(f"tcs_{k}", ["tcs", "--D", str(args.D)])
    for lock in ("global", "rw"):
        for k in range(1, REPEATS + 1):
            child(f"x1_{lock}_{k}", ["x1", "--lock", lock, "--n", str(args.n), "--D", str(args.D)])
    # n-thread runs: global and RWLock alternate, so slow drift of the machine hits both equally
    for k in range(1, REPEATS + 1):
        for lock in ("global", "rw"):
            child(f"run_{lock}_{k}", ["run", "--lock", lock, "--n", str(args.n), "--D", str(args.D)])
    if not args.skip_extra:
        log("supplementary: RWLock with reader preference (policy=reader)")
        for k in range(1, REPEATS + 1):
            child(f"run_rwreader_{k}", ["run", "--lock", "rw", "--policy", "reader",
                                        "--n", str(args.n), "--D", str(args.D)])
    log("==== all done; next: py lab5/analyze.py")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["run", "x1", "tcs", "selftest", "all"])
    ap.add_argument("--lock", choices=["global", "rw"], default="global")
    ap.add_argument("--policy", choices=["phase", "reader"], default="phase")
    ap.add_argument("--n", type=int, default=N_THREADS)
    ap.add_argument("--D", type=float, default=D_SEC)
    ap.add_argument("--out")
    ap.add_argument("--skip-extra", action="store_true", help="all: no reader-preference runs")
    args = ap.parse_args()
    if args.mode == "all":
        cmd_all(args)
        return
    r = {"run": cmd_run, "x1": cmd_x1, "tcs": cmd_tcs, "selftest": cmd_selftest}[args.mode](args)
    if args.out:
        Path(args.out).write_text(json.dumps(r, indent=1), encoding="utf-8")
    print(summary_line(r))
    if r.get("mode") == "selftest" and not r["ok"]:
        raise SystemExit(1)
    if r.get("mode") == "run" and not r["invariants"]["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
