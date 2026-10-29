#!/usr/bin/env python3
"""Ступінчастий генератор навантаження у відкритому контурі (ЛР 2, крок 5).

Профіль: прогрів --warmup с на рівні --warmup-rate, далі сходинки по --step-dur с
від --start з приростом --step запитів за секунду. Прогін зупиняється, коли на двох
сходинках поспіль досягнута інтенсивність нижча за 0.9 заданої, або після
сходинки, задана інтенсивність якої досягла межі генератора --gen-limit (G).

Посекундно записуються: задана інтенсивність, фактична інтенсивність
надсилання, досягнута інтенсивність (2xx, завершені в цю секунду), кількість
відповідей 2xx і не 2xx, помилки зʼєднання, p50/p95/p99 клієнта.

* Затримка рахується від ЗАПЛАНОВАНОГО моменту надсилання до відповіді.
* Тайм-аут запиту 5 с; запит, що перевищив тайм-аут, — відповідь не 2xx.
* Клієнт асинхронний (asyncio, власний мінімальний HTTP/1.1 з keep-alive): запит
  відправляється в запланований момент незалежно від того, скільки попередніх
  ще чекають відповіді; для нього береться вільне зʼєднання або відкривається нове.
* Навантаження ділиться між --procs процесами (кожен — свій цикл подій).

Приклади:
  py loadtest/stepgen.py --target noop --name step_noop             # межа G
  py loadtest/stepgen.py --gen-limit 2600 --name step_n1             # сервіс
"""
from __future__ import annotations

import argparse
import asyncio
import threading
from collections import deque
import csv
import json
import math
import multiprocessing as mp
import os
import platform
import random
import statistics
import sys
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpjson import RAW_DIR, RESOURCE, fetch_ids  # noqa: E402

LAB2_RAW = RAW_DIR.parent / "lab2" / "raw"
pc = time.perf_counter


# --------------------------------------------------------------------------
# Профіль
# --------------------------------------------------------------------------
class Profile:
    """Ступінчастий профіль (прогрів + сходинки) або профіль з відрізків.

    segments — список [тривалість, інтенсивність]; якщо задано, прогріву немає,
    а «сходинка» k — це k-й відрізок (ЛР 3, крок 10).
    """

    def __init__(self, warmup: float, warmup_rate: float, start: float, step: float,
                 step_dur: float, max_steps: int, segments=None):
        self.segments = [(float(d), float(r)) for d, r in segments] if segments else None
        if self.segments:
            warmup, max_steps = 0.0, len(self.segments)
            self._bounds, t = [], 0.0
            for d, _ in self.segments:
                self._bounds.append((t, t + d))
                t += d
        self.warmup, self.warmup_rate = warmup, warmup_rate
        self.start, self.step, self.step_dur = start, step, step_dur
        self.max_steps = max_steps

    def step_of(self, t: float) -> int:
        """-1 — прогрів, k ≥ 0 — номер сходинки (відрізка)."""
        if self.segments:
            for k, (a, b) in enumerate(self._bounds):
                if t < b:
                    return k
            return len(self._bounds)
        if t < self.warmup:
            return -1
        return int((t - self.warmup) // self.step_dur)

    def rate_of_step(self, k: int) -> float:
        if self.segments:
            if k < 0:
                return 0.0
            return self.segments[min(k, len(self.segments) - 1)][1]
        return self.warmup_rate if k < 0 else self.start + self.step * k

    def step_bounds(self, k: int) -> tuple[float, float]:
        if self.segments:
            if k < 0:
                return 0.0, 0.0
            return self._bounds[min(k, len(self._bounds) - 1)]
        if k < 0:
            return 0.0, self.warmup
        a = self.warmup + k * self.step_dur
        return a, a + self.step_dur

    @property
    def total(self) -> float:
        if self.segments:
            return self._bounds[-1][1]
        return self.warmup + self.max_steps * self.step_dur


# --------------------------------------------------------------------------
# Мінімальний HTTP/1.1-клієнт (GET, keep-alive)
# --------------------------------------------------------------------------
class HttpConn(asyncio.Protocol):
    __slots__ = ("transport", "buf", "fut", "closed", "pool", "nreq")

    def __init__(self, pool: "ConnPool"):
        self.pool = pool
        self.transport = None
        self.buf = bytearray()
        self.fut: asyncio.Future | None = None
        self.closed = False
        self.nreq = 0

    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        self.closed = True
        if self.fut is not None and not self.fut.done():
            self.fut.set_exception(ConnectionResetError("connection closed by peer"))

    def data_received(self, data: bytes):
        self.buf += data
        if self.fut is None or self.fut.done():
            return
        hdr_end = self.buf.find(b"\r\n\r\n")
        if hdr_end < 0:
            return
        head = bytes(self.buf[:hdr_end]).decode("latin-1")
        lines = head.split("\r\n")
        try:
            status = int(lines[0].split(" ", 2)[1])
        except (IndexError, ValueError):
            self.fut.set_exception(ConnectionError("bad status line"))
            return
        clen = 0
        chunked = False
        keep = True
        pod = ""
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            k = k.strip().lower()
            if k == "content-length":
                clen = int(v.strip())
            elif k == "transfer-encoding" and "chunked" in v.lower():
                chunked = True
            elif k == "connection" and "close" in v.lower():
                keep = False
            elif k == "x-pod":
                pod = v.strip()
        body_start = hdr_end + 4
        if chunked:
            end = self.buf.find(b"\r\n0\r\n\r\n", body_start - 2)
            if end < 0:
                return
            total = end + 7
        else:
            total = body_start + clen
            if len(self.buf) < total:
                return
        del self.buf[:total]
        self.fut.set_result((status, keep, pod))


class ConnPool:
    def __init__(self, host: str, port: int, max_conns: int):
        self.host, self.port = host, port
        self.idle: list[HttpConn] = []
        self.open = 0
        self.max_conns = max_conns
        self.waiters: deque[asyncio.Future] = deque()

    async def acquire(self) -> HttpConn:
        while self.idle:
            c = self.idle.pop()
            if not c.closed:
                return c
            self.open -= 1
        if self.open >= self.max_conns:
            fut = asyncio.get_running_loop().create_future()
            self.waiters.append(fut)
            try:
                return await fut
            except asyncio.CancelledError:
                # Тайм-аут запиту (wait_for) міг настати вже після того, як release()
                # передав цьому очікувачу зʼєднання. Тоді зʼєднання треба повернути в
                # пул, інакше місце в пулі втрачається назавжди: під тривалим
                # перевантаженням усі місця «витікають», і генератор зупиняється.
                if fut.done() and not fut.cancelled() and fut.exception() is None:
                    self.release(fut.result(), True)
                raise
        self.open += 1
        loop = asyncio.get_running_loop()
        try:
            _, proto = await loop.create_connection(lambda: HttpConn(self), self.host, self.port)
        except BaseException:
            self.open -= 1
            raise
        return proto

    def release(self, c: HttpConn, reusable: bool) -> None:
        if not reusable or c.closed:
            if c.transport is not None:
                c.transport.close()
            self.open -= 1
            # місце звільнилось — відкриємо нове зʼєднання для того, хто чекає
            while self.waiters:
                w = self.waiters.popleft()
                if not w.done():
                    asyncio.ensure_future(self._open_for(w))
                    break
            return
        while self.waiters:
            w = self.waiters.popleft()
            if not w.done():
                w.set_result(c)
                return
        self.idle.append(c)

    async def _open_for(self, w: asyncio.Future) -> None:
        try:
            c = await self.acquire()
        except BaseException as exc:  # noqa: BLE001
            if not w.done():
                w.set_exception(exc)
            return
        if not w.done():
            w.set_result(c)
        else:
            self.release(c, True)


# --------------------------------------------------------------------------
# Робочий процес
# --------------------------------------------------------------------------
def _raise_fd_limit() -> None:
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = hard if hard != resource.RLIM_INFINITY else 65535
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(target, 1 << 20), hard))
    except Exception:  # noqa: BLE001
        pass


class Stats:
    """Посекундні лічильники процесу; індекс секунди відраховується від t0."""

    def __init__(self):
        self.sent = defaultdict(int)
        self.ok = defaultdict(int)
        self.non2xx = defaultdict(int)
        self.connerr = defaultdict(int)
        self.timeouts = defaultdict(int)
        self.lat = defaultdict(list)          # затримки завершених запитів, с
        self.lag = defaultdict(list)          # запізнення відправки відносно розкладу, с
        self.ok_dirty: dict[int, int] = defaultdict(int)
        self.pods = defaultdict(int)
        self.pod_sec = defaultdict(lambda: defaultdict(int))   # секунда -> под -> 2xx
        self.max_inflight = 0


async def _worker_main(idx: int, nprocs: int, cfg: dict, ids: list[int], t0_wall: float,
                       ctl, out_q) -> None:
    prof = Profile(**cfg["profile"])
    loop = asyncio.get_running_loop()
    pool = ConnPool(cfg["host"], cfg["port"], cfg["max_conns"] // nprocs)
    rnd = random.Random(cfg["seed"] * 1000 + idx)
    paths = ([cfg["prefix"] + "/noop"] if cfg["target"] == "noop"
             else [f"{cfg['prefix']}{RESOURCE}/{i}" for i in ids])
    reqs = [f"GET {p} HTTP/1.1\r\nHost: {cfg['host']}:{cfg['port']}\r\n"
            f"User-Agent: stepgen\r\nAccept: */*\r\n\r\n".encode() for p in paths]
    st = Stats()
    timeout = cfg["timeout"]
    rpc = int(cfg.get("reqs_per_conn") or 0)
    inflight = 0

    # перехід від perf_counter до «секунд від t0»: t0 задано настінним годинником
    pc0 = pc() - (time.time() - t0_wall)

    async def one(sched_rel: float, req: bytes) -> None:
        nonlocal inflight
        inflight += 1
        st.max_inflight = max(st.max_inflight, inflight)
        st.sent[int(sched_rel)] += 1
        conn = None
        reusable = False
        try:
            try:
                wait = timeout - (pc() - pc0 - sched_rel)
                conn = await asyncio.wait_for(pool.acquire(), max(0.001, wait))
                remaining = timeout - (pc() - pc0 - sched_rel)
                if remaining <= 0:              # зʼєднання дісталось уже після тайм-ауту
                    reusable = True
                    raise asyncio.TimeoutError
                fut = loop.create_future()
                conn.fut = fut
                conn.transport.write(req)
                st.lag[int(sched_rel)].append(pc() - pc0 - sched_rel)
                try:
                    status, keep, pod = await asyncio.wait_for(asyncio.shield(fut), remaining)
                except asyncio.TimeoutError:
                    # Відповідь не прийшла за тайм-аут: запит зараховано як не 2xx, але
                    # зʼєднання не закривається — його дочитує окрема задача і повертає в
                    # пул. Так генератор справді тримає сталу кількість зʼєднань, а сервер
                    # не виконує марно роботу для вже закритих зʼєднань.
                    asyncio.ensure_future(drain(conn, fut))
                    conn = None
                    raise
                conn.nreq += 1
                # модель зʼєднань ЛР 3: після reqs_per_conn запитів зʼєднання
                # закривається, замість нього відкривається нове
                reusable = keep and (rpc <= 0 or conn.nreq < rpc)
                now = pc() - pc0
                sec = int(now)
                st.lat[sec].append(now - sched_rel)
                if 200 <= status < 300:
                    st.ok[sec] += 1
                    st.ok_dirty[sec] += 1
                    if pod:
                        st.pods[pod] += 1
                        st.pod_sec[sec][pod] += 1
                else:
                    st.non2xx[sec] += 1
            except asyncio.TimeoutError:
                now = pc() - pc0
                sec = int(now)
                st.non2xx[sec] += 1               # тайм-аут = відповідь не 2xx
                st.timeouts[sec] += 1
                st.lat[sec].append(now - sched_rel)
            except (OSError, ConnectionError):
                sec = int(pc() - pc0)
                st.connerr[sec] += 1
        finally:
            inflight -= 1
            if conn is not None:
                conn.fut = None
                pool.release(conn, reusable)

    async def drain(conn: HttpConn, fut: asyncio.Future) -> None:
        reusable = False
        try:
            _, keep, _ = await asyncio.wait_for(fut, 120)
            conn.nreq += 1
            reusable = keep and (rpc <= 0 or conn.nreq < rpc)
        except BaseException:  # noqa: BLE001
            reusable = False
        finally:
            conn.fut = None
            pool.release(conn, reusable)

    async def flusher() -> None:
        while True:
            await asyncio.sleep(0.25)
            if st.ok_dirty:
                out_q.put(("ok", idx, dict(st.ok_dirty)))
                st.ok_dirty.clear()

    # процесорний час генератора раз на секунду — окремим потоком, щоб вимірювання
    # не залежало від завантаженості циклу подій
    cpu_stop = threading.Event()

    def cpu_reporter() -> None:
        while not cpu_stop.wait(1.0):
            out_q.put(("cpu", idx, (pc() - pc0, time.process_time())))

    threading.Thread(target=cpu_reporter, daemon=True).start()

    fl = asyncio.ensure_future(flusher())
    tasks: set[asyncio.Task] = set()

    # Розклад: у сходинці з інтенсивністю R процес idx відправляє в моменти
    # a + (idx + j·P) / R, де a — початок сходинки.
    #
    # Моменти відміряє окремий потік через time.sleep, а не asyncio.sleep: на
    # Windows до Python 3.13 годинник циклу подій (time.monotonic) має роздільність
    # 15.6 мс, і таймери asyncio спрацьовують до 15 мс раніше або пізніше. Тоді запит
    # іде раніше запланованого моменту, і затримка «від запланованого моменту»
    # спотворюється (аж до відʼємних значень). time.sleep з Python 3.11 на Windows
    # використовує таймер високої роздільності. Потік ніколи не відправляє запит
    # раніше його моменту; запізнення відправки записується (send_lag).
    def fire(batch):
        for t_s, req in batch:
            task = asyncio.ensure_future(one(t_s, req))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    sched_done = loop.create_future()

    def scheduler() -> None:
        k = -1
        batch = []
        try:
            while k < prof.max_steps:
                a, b = prof.step_bounds(k)
                rate = prof.rate_of_step(k)
                if a >= ctl["stop_at"].value:
                    break
                if rate <= 0 or b <= a:          # немає прогріву (профіль з відрізків)
                    k += 1
                    continue
                interval = nprocs / rate
                t = a + idx / rate
                while t < min(b, ctl["stop_at"].value):
                    now = pc() - pc0
                    if t > now:
                        if batch:
                            loop.call_soon_threadsafe(fire, batch)
                            batch = []
                        time.sleep(min(t - now, 0.05))
                        continue
                    batch.append((t, reqs[rnd.randrange(len(reqs))]))
                    t += interval
                if ctl["stop_at"].value <= b:
                    break
                k += 1
            if batch:
                loop.call_soon_threadsafe(fire, batch)
        finally:
            loop.call_soon_threadsafe(sched_done.set_result, None)

    threading.Thread(target=scheduler, daemon=True).start()
    await sched_done
    await asyncio.sleep(0.05)              # дати циклу створити останні задачі

    if tasks:
        await asyncio.wait(tasks, timeout=timeout + 2)
    fl.cancel()
    cpu_stop.set()
    if st.ok_dirty:
        out_q.put(("ok", idx, dict(st.ok_dirty)))
    out_q.put(("final", idx, {
        "sent": dict(st.sent), "ok": dict(st.ok), "non2xx": dict(st.non2xx),
        "connerr": dict(st.connerr), "timeouts": dict(st.timeouts),
        "lat": {s: v for s, v in st.lat.items()}, "pods": dict(st.pods),
        "lag": {s: v for s, v in st.lag.items()},
        "pod_sec": {s: dict(v) for s, v in st.pod_sec.items()},
        "max_inflight": st.max_inflight, "max_conns_open": pool.open,
    }))


def _worker(idx, nprocs, cfg, ids, t0_wall, stop_at, out_q):
    _raise_fd_limit()
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(_worker_main(idx, nprocs, cfg, ids, t0_wall, {"stop_at": stop_at}, out_q))


# --------------------------------------------------------------------------
# Батьківський процес: контроль зупинки і підсумки
# --------------------------------------------------------------------------
def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = max(0, math.ceil(p / 100.0 * len(sorted_vals)) - 1)
    return sorted_vals[k]


def step_summary(prof: Profile, k: int, per_sec: dict, lat_by_sec: dict,
                 lag_by_sec: dict | None = None, settle: float = 5.0,
                 pod_sec: dict | None = None) -> dict:
    a, b = prof.step_bounds(k)
    # перші settle секунд сходинки — перехідні (ЛР 2: 5 с з 20, ЛР 3: 10 с з 30)
    secs = [s for s in range(int(a) + int(settle), int(b))]
    ach = [per_sec["ok"].get(s, 0) for s in secs]
    lat = sorted(x for s in secs for x in lat_by_sec.get(s, []))
    lag = sorted(x for s in secs for x in (lag_by_sec or {}).get(s, []))
    tot = sum(per_sec["ok"].get(s, 0) + per_sec["non2xx"].get(s, 0) for s in secs)
    non = sum(per_sec["non2xx"].get(s, 0) for s in secs)
    rate = prof.rate_of_step(k)
    mean = statistics.fmean(ach) if ach else 0.0
    return {
        "step": k, "target_rps": rate, "t_start_s": a, "t_end_s": b,
        "window_s": [secs[0], secs[-1] + 1] if secs else None,
        "achieved_rps": round(mean, 2),
        "achieved_sd": round(statistics.stdev(ach), 2) if len(ach) > 1 else 0.0,
        "sent_rps": round(statistics.fmean([per_sec["sent"].get(s, 0) for s in secs]), 2)
        if secs else 0.0,
        "ok": sum(ach), "non2xx": non,
        "connerr": sum(per_sec["connerr"].get(s, 0) for s in secs),
        "non2xx_pct": round(100.0 * non / tot, 3) if tot else 0.0,
        "p50_ms": round(percentile(lat, 50) * 1000, 2) if lat else None,
        "p95_ms": round(percentile(lat, 95) * 1000, 2) if lat else None,
        "p99_ms": round(percentile(lat, 99) * 1000, 2) if lat else None,
        "send_lag_p99_ms": round(percentile(lag, 99) * 1000, 2) if lag else None,
        "ratio": round(mean / rate, 4) if rate else None,
        # 2xx-відповіді кожного пода за вікно (заголовок X-Pod), запитів/с
        "pods_rps": {pod: round(sum((pod_sec or {}).get(s, {}).get(pod, 0) for s in secs)
                                / len(secs), 2)
                     for pod in sorted({p for s in secs for p in (pod_sec or {}).get(s, {})})}
        if secs else {},
    }


def run(cfg: dict, ids: list[int], log=print) -> dict:
    prof = Profile(**cfg["profile"])
    nprocs = cfg["procs"]
    ctx = mp.get_context("spawn")
    out_q = ctx.Queue()
    stop_at = ctx.Value("d", prof.total)
    t0_wall = time.time() + 1.0 + 0.2 * nprocs     # час на запуск процесів
    procs = [ctx.Process(target=_worker, daemon=True,
                         args=(i, nprocs, cfg, ids, t0_wall, stop_at, out_q))
             for i in range(nprocs)]
    for p in procs:
        p.start()

    live_ok: dict[int, int] = defaultdict(int)
    cpu_samples: dict[int, list] = defaultdict(list)
    finals = {}
    evaluated = set()
    below = 0
    stop_reason = "max_steps"
    G = cfg.get("gen_limit")
    settle = cfg.get("settle", 5.0)
    while len(finals) < nprocs:
        try:
            kind, idx, data = out_q.get(timeout=0.2)
            if kind == "ok":
                for s, v in data.items():
                    live_ok[int(s)] += v
            elif kind == "cpu":
                cpu_samples[idx].append(data)
            else:
                finals[idx] = data
        except Exception:  # noqa: BLE001 — queue.Empty
            pass
        now_rel = time.time() - t0_wall
        k_now = prof.step_of(now_rel - 0.8)             # 0.8 с на доставку лічильників
        for k in range(0, k_now):
            if prof.segments or k in evaluated or stop_at.value < prof.total:
                continue
            evaluated.add(k)
            a, b = prof.step_bounds(k)
            ach = statistics.fmean(live_ok.get(s, 0)
                                   for s in range(int(a) + int(settle), int(b)))
            rate = prof.rate_of_step(k)
            log(f"  step {k:2d}: target {rate:7.0f} rps  achieved {ach:8.1f} rps "
                f"({100 * ach / rate:5.1f} %)")
            below = below + 1 if ach < 0.9 * rate else 0
            if below >= 2:
                stop_reason = "two_steps_below_0.9"
            elif G is not None and rate >= G:
                stop_reason = "reached_generator_limit"
            else:
                continue
            stop_at.value = b                            # далі нових запитів не планувати
            log(f"  stop: {stop_reason} after step {k}")
    for p in procs:
        p.join()

    per_sec = {key: defaultdict(int) for key in ("sent", "ok", "non2xx", "connerr", "timeouts")}
    lat_by_sec: dict[int, list] = defaultdict(list)
    lag_by_sec: dict[int, list] = defaultdict(list)
    pods: dict[str, int] = defaultdict(int)
    pod_sec: dict[int, dict] = defaultdict(lambda: defaultdict(int))
    for f in finals.values():
        for s, d in f.get("pod_sec", {}).items():
            for pod, v in d.items():
                pod_sec[int(s)][pod] += v
        for key in per_sec:
            for s, v in f[key].items():
                per_sec[key][int(s)] += v
        for s, v in f["lat"].items():
            lat_by_sec[int(s)].extend(v)
        for s, v in f.get("lag", {}).items():
            lag_by_sec[int(s)].extend(v)
        for pod, v in f["pods"].items():
            pods[pod] += v

    last_sec = int(min(stop_at.value, prof.total))
    seconds = []
    for s in range(0, last_sec):
        k = prof.step_of(s)
        lat = sorted(lat_by_sec.get(s, []))
        seconds.append({
            "t_s": s, "step": k, "target_rps": prof.rate_of_step(k),
            "sent_rps": per_sec["sent"].get(s, 0),
            "achieved_rps": per_sec["ok"].get(s, 0),
            "ok": per_sec["ok"].get(s, 0),
            "non2xx": per_sec["non2xx"].get(s, 0),
            "timeouts": per_sec["timeouts"].get(s, 0),
            "connerr": per_sec["connerr"].get(s, 0),
            "p50_ms": round(percentile(lat, 50) * 1000, 3) if lat else None,
            "p95_ms": round(percentile(lat, 95) * 1000, 3) if lat else None,
            "p99_ms": round(percentile(lat, 99) * 1000, 3) if lat else None,
            "send_lag_p99_ms": round(percentile(sorted(lag_by_sec.get(s, [])), 99) * 1000, 3)
            if lag_by_sec.get(s) else None,
            "pods": dict(pod_sec.get(s, {})),
        })
    # використання CPU генератором (сума по процесах), ядер, посекундно
    gen_cpu = defaultdict(float)
    for smp in cpu_samples.values():
        for (t1, c1), (t2, c2) in zip(smp, smp[1:]):
            if t2 > t1:
                gen_cpu[int(t2)] += (c2 - c1) / (t2 - t1)
    for row in seconds:
        row["gen_cpu_cores"] = round(gen_cpu.get(row["t_s"], 0.0), 3)
    n_steps = prof.step_of(last_sec - 1) + 1 if last_sec > prof.warmup else 0
    steps = [step_summary(prof, k, per_sec, lat_by_sec, lag_by_sec, settle, pod_sec)
             for k in range(n_steps)]
    best = max(steps, key=lambda x: x["achieved_rps"]) if steps else None
    return {
        "name": cfg["name"], "target": cfg["target"], "base_url": cfg["base_url"],
        "t0_epoch": t0_wall, "t_end_epoch": t0_wall + last_sec,
        "profile": cfg["profile"], "timeout_s": cfg["timeout"], "gen_limit": G,
        "stop_reason": stop_reason,
        "X_rps": best["achieved_rps"] if best else None,
        "X_sd": best["achieved_sd"] if best else None,
        "X_step": best["step"] if best else None,
        "X_target_rps": best["target_rps"] if best else None,
        "steps": steps, "seconds": seconds, "pods": dict(pods),
        "generator": {
            "procs": nprocs, "python": platform.python_version(),
            "platform": platform.platform(), "cpu_count": os.cpu_count(),
            "max_inflight_per_proc": [f["max_inflight"] for f in finals.values()],
            "max_conns": cfg["max_conns"],
            "reqs_per_conn": cfg.get("reqs_per_conn") or None,
        },
        "settle_s": settle,
        "extra": cfg.get("extra", {}),
    }


def save(result: dict, out_dir: Path = LAB2_RAW) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = result["name"]
    (out_dir / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    with open(out_dir / f"{name}_seconds.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(result["seconds"][0].keys()))
        w.writeheader()
        w.writerows({**r, "pods": json.dumps(r["pods"], ensure_ascii=False)}
                    if isinstance(r.get("pods"), dict) else r for r in result["seconds"])
    return out_dir / f"{name}.json"


def make_cfg(args) -> dict:
    u = urllib.parse.urlparse(args.base_url)
    return {
        "name": args.name, "base_url": args.base_url.rstrip("/"),
        "host": u.hostname or "localhost", "port": u.port or 80,
        "prefix": u.path.rstrip("/"), "target": args.target,
        "timeout": args.timeout, "seed": args.seed, "procs": args.procs,
        "max_conns": args.max_conns, "gen_limit": args.gen_limit,
        "profile": {"warmup": args.warmup, "warmup_rate": args.warmup_rate,
                    "start": args.start, "step": args.step, "step_dur": args.step_dur,
                    "max_steps": args.max_steps,
                    "segments": parse_segments(getattr(args, "profile", None))},
        "settle": getattr(args, "settle", 5.0),
        "reqs_per_conn": getattr(args, "reqs_per_conn", 0),
    }


def parse_segments(text):
    """'30:300,60:2500,90:300' -> [[30, 300], [60, 2500], [90, 300]]."""
    if not text:
        return None
    if isinstance(text, (list, tuple)):
        return [list(x) for x in text]
    out = []
    for part in str(text).split(","):
        d, r = part.split(":")
        out.append([float(d), float(r)])
    return out


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--target", choices=["item", "noop"], default="item")
    p.add_argument("--name", required=True, help="імʼя прогону (файли results/lab2/raw/<name>.*)")
    p.add_argument("--gen-limit", type=float, default=None, help="межа генератора G, rps")
    p.add_argument("--warmup", type=float, default=10.0)
    p.add_argument("--warmup-rate", type=float, default=200.0)
    p.add_argument("--start", type=float, default=200.0)
    p.add_argument("--step", type=float, default=200.0)
    p.add_argument("--step-dur", type=float, default=20.0)
    p.add_argument("--max-steps", type=int, default=60)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--procs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    p.add_argument("--max-conns", type=int, default=4000,
                   help="найбільше відкритих зʼєднань (сумарно на всі процеси); понад це "
                        "запити чекають вільного зʼєднання, затримка все одно рахується від "
                        "запланованого моменту")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--settle", type=float, default=5.0,
                   help="перехідні секунди на початку сходинки (ЛР 3: 10)")
    p.add_argument("--reqs-per-conn", type=int, default=0,
                   help="закривати зʼєднання після стількох запитів (ЛР 3: 100; 0 — ніколи)")
    p.add_argument("--profile", default=None,
                   help="профіль з відрізків 'тривалість:інтенсивність,...' замість сходинок")
    return p


def main() -> None:
    args = build_argparser().parse_args()
    ids = [] if args.target == "noop" else fetch_ids(args.base_url)
    cfg = make_cfg(args)
    print(f"stepgen {args.name}: target={args.target} procs={args.procs} G={args.gen_limit}")
    res = run(cfg, ids)
    path = save(res)
    print(f"X = {res['X_rps']} ± {res['X_sd']} rps on step {res['X_step']} "
          f"(target {res['X_target_rps']}), stop: {res['stop_reason']}; saved {path}")


if __name__ == "__main__":
    main()
