#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import gzip
import http.client
import json
import math
import multiprocessing as mp
import os
import platform
import random
import statistics
import sys
import threading
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpjson import RAW_DIR, RESOURCE, fetch_ids  # noqa: E402

pc = time.perf_counter

# --------------------------------------------------------------------------
# HTTP-клієнт: одне keep-alive зʼєднання на потік
# --------------------------------------------------------------------------
_tls = threading.local()


def raise_fd_limit() -> None:
    """На Linux/macOS піднімає ліміт відкритих файлів (кожне зʼєднання — fd)."""
    try:
        import resource
    except ImportError:  # Windows
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        for target in (hard, 65535, 10240):
            if target == resource.RLIM_INFINITY or target <= soft:
                continue
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
                return
            except (ValueError, OSError):
                continue
    except Exception:  # noqa: BLE001
        pass


def _drop_conn() -> None:
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _tls.conn = None


def http_get(host: str, port: int, path: str, timeout: float):
    """GET path. Повертає (status, instance, error); status=0 — транспортна помилка."""
    for attempt in range(2):
        conn = getattr(_tls, "conn", None)
        if conn is None:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            _tls.conn = conn
        reused = conn.sock is not None
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            return resp.status, resp.getheader("X-Instance", ""), ""
        except (ConnectionResetError, BrokenPipeError,
                http.client.RemoteDisconnected, http.client.CannotSendRequest,
                http.client.ResponseNotReady) as exc:
            _drop_conn()
            if reused and attempt == 0:
                continue  # сервер закрив простоюване keep-alive зʼєднання — повтор
            return 0, "", type(exc).__name__
        except TimeoutError:
            _drop_conn()
            return 0, "", "timeout"
        except (OSError, http.client.HTTPException) as exc:
            _drop_conn()
            return 0, "", type(exc).__name__
    return 0, "", "retry_failed"


# --------------------------------------------------------------------------
# Робочі процеси
# --------------------------------------------------------------------------
def _sampler(state, lock, t0, warmup, total, step, samples, stop):
    """Кожні step секунд у вікні вимірювання записує sent − done."""
    start, end = t0 + warmup, t0 + total
    nxt = start
    while not stop.is_set():
        now = pc()
        if now >= end:
            break
        if now >= nxt:
            with lock:
                samples.append(state[0] - state[1])
            nxt += step
            if nxt < now:
                nxt = now + step
        else:
            time.sleep(min(step, nxt - now))


def _paths(cfg, ids):
    if cfg["target"] == "noop":
        return [cfg["prefix"] + "/noop"]
    return [f"{cfg['prefix']}{RESOURCE}/{i}" for i in ids]


def _open_worker(idx, nprocs, cfg, ids, ready_q, start_ev, start_val, out_q):
    raise_fd_limit()
    host, port, timeout = cfg["host"], cfg["port"], cfg["timeout"]
    rate, warmup, duration = cfg["rate"], cfg["warmup"], cfg["duration"]
    total = warmup + duration
    interval = nprocs / rate              # інтервал між запитами цього процесу
    offset = idx / rate                   # зсув, щоб процеси не стріляли одночасно
    n = max(0, math.ceil((total - offset) / interval))
    paths = _paths(cfg, ids)
    rnd = random.Random(cfg["seed"] * 1000 + idx)

    records = [None] * n
    state = [0, 0]                        # [sent, done]
    lock = threading.Lock()
    samples: list[int] = []
    stop = threading.Event()
    max_workers = max(4, min(cfg["max_inflight"] // nprocs,
                             math.ceil(rate / nprocs * timeout) + 16))
    pool = ThreadPoolExecutor(max_workers=max_workers)

    def task(k, sched, path):
        t_send = pc()
        with lock:
            state[0] += 1
        status, inst, err = http_get(host, port, path, timeout)
        t_end = pc()
        with lock:
            state[1] += 1
        records[k] = (sched, t_send, t_end, status, inst, err)

    # прогріваємо одне зʼєднання і повідомляємо батьківський процес про готовність
    ready_q.put(idx)
    start_ev.wait()
    t0 = start_val.value

    smp = threading.Thread(target=_sampler, daemon=True,
                           args=(state, lock, t0, warmup, total,
                                 cfg["sample_ms"] / 1000.0, samples, stop))
    smp.start()

    for k in range(n):
        sched = t0 + offset + k * interval
        delay = sched - pc()
        if delay > 0:
            time.sleep(delay)
        pool.submit(task, k, sched, rnd.choice(paths))

    pool.shutdown(wait=True)
    stop.set()
    smp.join()
    rel = [(r[0] - t0, r[1] - t0, r[2] - t0, r[3], r[4], r[5]) for r in records if r]
    out_q.put({"idx": idx, "records": rel, "samples": samples,
               "max_workers": max_workers})


def _closed_worker(idx, nprocs, cfg, ids, ready_q, start_ev, start_val, out_q):
    raise_fd_limit()
    host, port, timeout = cfg["host"], cfg["port"], cfg["timeout"]
    warmup, duration = cfg["warmup"], cfg["duration"]
    total = warmup + duration
    conc = cfg["concurrency"]
    mine = conc // nprocs + (1 if idx < conc % nprocs else 0)
    paths = _paths(cfg, ids)

    per_thread: list[list] = [[] for _ in range(mine)]
    state = [0, 0]
    lock = threading.Lock()
    samples: list[int] = []
    stop = threading.Event()
    t0_box = [0.0]

    def loop(j):
        rnd = random.Random(cfg["seed"] * 100000 + idx * 1000 + j)
        recs = per_thread[j]
        end = t0_box[0] + total
        while True:
            t_send = pc()
            if t_send >= end:
                break
            with lock:
                state[0] += 1
            status, inst, err = http_get(host, port, rnd.choice(paths), timeout)
            t_end = pc()
            with lock:
                state[1] += 1
            recs.append((t_send, t_send, t_end, status, inst, err))

    ready_q.put(idx)
    start_ev.wait()
    t0 = start_val.value
    t0_box[0] = t0
    delay = t0 - pc()
    if delay > 0:
        time.sleep(delay)

    smp = threading.Thread(target=_sampler, daemon=True,
                           args=(state, lock, t0, warmup, total,
                                 cfg["sample_ms"] / 1000.0, samples, stop))
    smp.start()
    threads = [threading.Thread(target=loop, args=(j,), daemon=True) for j in range(mine)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    stop.set()
    smp.join()
    rel = [(r[0] - t0, r[1] - t0, r[2] - t0, r[3], r[4], r[5])
           for recs in per_thread for r in recs]
    out_q.put({"idx": idx, "records": rel, "samples": samples, "max_workers": mine})


# --------------------------------------------------------------------------
# Запуск і підсумки
# --------------------------------------------------------------------------
def default_procs(cfg) -> int:
    cpus = os.cpu_count() or 1
    if cfg["mode"] == "closed":
        return max(1, min(cpus, cfg["concurrency"]))
    return max(1, min(cpus, int(cfg["rate"])))


def make_cfg(base_url: str, **kw) -> dict:
    u = urllib.parse.urlparse(base_url)
    cfg = {
        "base_url": base_url.rstrip("/"),
        "host": u.hostname or "localhost",
        "port": u.port or 80,
        "prefix": u.path.rstrip("/"),
        "mode": "open",
        "target": "item",          # item → GET /trial-participants/{id}; noop → GET /noop
        "rate": 100.0,
        "concurrency": 0,
        "duration": 30.0,
        "warmup": 10.0,
        "timeout": 30.0,
        "max_inflight": 6000,
        "sample_ms": 2.0,
        "seed": 1,
        "procs": 0,
    }
    cfg.update({k: v for k, v in kw.items() if v is not None})
    if not cfg["procs"]:
        cfg["procs"] = default_procs(cfg)
    return cfg


def run(cfg: dict, ids: list[int]) -> tuple[dict, list]:
    """Виконує один прогін. Повертає (summary, records)."""
    if cfg["target"] == "item" and not ids:
        raise RuntimeError("немає id для запитів: спершу заповніть базу (loadtest/seed.py)")
    nprocs = cfg["procs"]
    ctx = mp.get_context("spawn")
    ready_q, out_q = ctx.Queue(), ctx.Queue()
    start_ev, start_val = ctx.Event(), ctx.Value("d", 0.0)
    target = _open_worker if cfg["mode"] == "open" else _closed_worker
    procs = [ctx.Process(target=target, daemon=True,
                         args=(i, nprocs, cfg, ids, ready_q, start_ev, start_val, out_q))
             for i in range(nprocs)]
    for p in procs:
        p.start()
    for _ in procs:
        ready_q.get(timeout=120)
    start_val.value = pc() + 0.5
    start_ev.set()
    results = [out_q.get() for _ in procs]
    for p in procs:
        p.join()

    records = [r for res in results for r in res["records"]]
    # L — сума середніх по процесах (середнє суми = сума середніх)
    l_sampled = sum(statistics.fmean(res["samples"]) for res in results if res["samples"])
    n_samples = min((len(res["samples"]) for res in results), default=0)
    summary = summarize(cfg, records, l_sampled, n_samples)
    summary["generator"] = {
        "procs": nprocs,
        "threads_per_proc_max": max(res["max_workers"] for res in results),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }
    return summary, records


def percentile(sorted_vals: list[float], p: float):
    """Перцентиль методом найближчого рангу."""
    if not sorted_vals:
        return None
    k = max(0, math.ceil(p / 100.0 * len(sorted_vals)) - 1)
    return sorted_vals[k]


def _ms(x):
    return None if x is None else round(x * 1000.0, 3)


def summarize(cfg: dict, records: list, l_sampled: float, n_samples: int) -> dict:
    w0, dur = cfg["warmup"], cfg["duration"]
    w1 = w0 + dur
    # запити, ЗАПЛАНОВАНІ у вікні вимірювання (прогрів відкинуто)
    win = [r for r in records if w0 <= r[0] < w1]
    lat = sorted(r[2] - r[0] for r in win)            # end − sched
    svc = [r[2] - r[1] for r in win]                  # end − sent
    lag = sorted(r[1] - r[0] for r in win)            # запізнення відправки
    ok = sum(1 for r in win if 200 <= r[3] < 300)
    non2xx = len(win) - ok

    sent_in_win = sum(1 for r in records if w0 <= r[1] < w1)
    ok_done_in_win = sum(1 for r in records if w0 <= r[2] < w1 and 200 <= r[3] < 300)
    # точне середнє за часом кількості запитів у системі (з тих самих подій)
    l_exact = sum(max(0.0, min(r[2], w1) - max(r[1], w0)) for r in records) / dur

    lam = sent_in_win / dur
    w_mean = statistics.fmean(svc) if svc else 0.0
    statuses = Counter(str(r[3]) if r[3] else (r[5] or "error") for r in win)
    instances = Counter(r[4] for r in win if r[4])

    return {
        "mode": cfg["mode"],
        "target": cfg["target"],
        "rate": cfg["rate"] if cfg["mode"] == "open" else None,
        "concurrency": cfg["concurrency"] if cfg["mode"] == "closed" else None,
        "duration_s": dur,
        "warmup_s": w0,
        "timeout_s": cfg["timeout"],
        "requests_in_window": len(win),
        "sent_rps": round(lam, 2),                     # фактична інтенсивність відправки
        "achieved_rps": round(ok_done_in_win / dur, 2),  # 2xx-відповідей за секунду
        "p50_ms": _ms(percentile(lat, 50)),
        "p95_ms": _ms(percentile(lat, 95)),
        "p99_ms": _ms(percentile(lat, 99)),
        "max_ms": _ms(lat[-1] if lat else None),
        "mean_ms": _ms(statistics.fmean(lat) if lat else None),
        "non2xx_pct": round(100.0 * non2xx / len(win), 3) if win else None,
        "send_lag_p99_ms": _ms(percentile(lag, 99)),
        "statuses": dict(statuses),
        "instances": dict(instances),
        "little": {
            "lambda_rps": round(lam, 3),
            "W_s": round(w_mean, 6),
            "lambda_x_W": round(lam * w_mean, 4),
            "L_sampled": round(l_sampled, 4),
            "L_exact": round(l_exact, 4),
            "samples_per_proc": n_samples,
            "diff_pct": round(100.0 * abs(l_sampled - lam * w_mean) / l_sampled, 2)
            if l_sampled > 0 else None,
        },
    }


def save_run(summary: dict, records: list, name: str, out_dir: Path = RAW_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / f"{name}.json"
    jpath.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with gzip.open(out_dir / f"{name}.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sched_s", "sent_s", "end_s", "status", "instance", "error"])
        for r in sorted(records, key=lambda x: x[0]):
            w.writerow([f"{r[0]:.6f}", f"{r[1]:.6f}", f"{r[2]:.6f}", r[3], r[4], r[5]])
    return jpath


def format_line(s: dict) -> str:
    head = (f"rate={s['rate']}" if s["mode"] == "open" else f"C={s['concurrency']}")
    return (f"[{s['mode']}] {head:>10}  sent={s['sent_rps']:>8} rps  "
            f"achieved={s['achieved_rps']:>8} rps  p50={s['p50_ms']} ms  "
            f"p95={s['p95_ms']} ms  p99={s['p99_ms']} ms  non2xx={s['non2xx_pct']}%  "
            f"L={s['little']['L_sampled']}  inst={len(s['instances'])}")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--mode", choices=["open", "closed"], default="open")
    p.add_argument("--target", choices=["item", "noop"], default="item")
    p.add_argument("--rate", type=float, default=100.0, help="запитів/с (open)")
    p.add_argument("--concurrency", type=int, default=0, help="одночасних запитів (closed)")
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--warmup", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--procs", type=int, default=0, help="0 = за кількістю ядер")
    p.add_argument("--max-inflight", type=int, default=6000)
    p.add_argument("--sample-ms", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--name", default="", help="імʼя файлів у results/raw (без розширення)")
    return p


def main() -> None:
    args = build_argparser().parse_args()
    cfg = make_cfg(args.base_url, mode=args.mode, target=args.target, rate=args.rate,
                   concurrency=args.concurrency, duration=args.duration,
                   warmup=args.warmup, timeout=args.timeout, procs=args.procs,
                   max_inflight=args.max_inflight, sample_ms=args.sample_ms, seed=args.seed)
    if cfg["mode"] == "closed" and cfg["concurrency"] < 1:
        sys.exit("--concurrency must be >= 1 for closed mode")
    ids = fetch_ids(cfg["base_url"]) if cfg["target"] == "item" else []
    summary, records = run(cfg, ids)
    print(format_line(summary))
    if args.name:
        path = save_run(summary, records, args.name)
        print("saved", path)
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
