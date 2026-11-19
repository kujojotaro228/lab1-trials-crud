#!/usr/bin/env python3
"""Lab 4 task queue worker (variant 25).

One process = one task at a time. Loop: claim a task from the tasks table ->
fetch the record over HTTP from the web service -> process the synthetic slice ->
store the result. Pool parallelism is set by the number of pods (W).

Modes (environment variables), so steps 3-5 run with the same code:
  CLAIM_MODE  = naive | for_update | skip_locked (default)
  STORE_MODE  = plain (INSERT without ON CONFLICT, no token) | idempotent (default)
  LEASE_SECONDS = L, 0 — no lease and no heartbeat (steps 3-4); variant 25: L = 15
  POLL_MS     = T_poll; variant 25: 10 ms
  FAIL_RATE   = probability of os._exit(1) after processing and before storing the result
  PROCESS_PASSES = k processing passes; between passes the cancellation flag (D6)
                   and the graceful shutdown deadline are checked
Other: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, WEB_URL, WORKER_ID,
Q_LO, Q_HI, SLICE_SIZE, METRICS_PORT, SHUTDOWN_DEADLINE, PID_FILE.

Log — JSON lines on stdout (kubectl logs): events claim, done, zero_rows, lost,
failed, cancelled, returned, crash, duplicate_result, shutdown, exit.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import os
import random
import signal
import socket
import sys
import time
import urllib.parse

import asyncpg
from prometheus_client import Counter, Gauge, Histogram, start_http_server


def env(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else cast(default)


WORKER_ID = env("WORKER_ID", socket.gethostname())
CLAIM_MODE = env("CLAIM_MODE", "skip_locked")
STORE_MODE = env("STORE_MODE", "idempotent")
LEASE = env("LEASE_SECONDS", "15", float)
POLL = env("POLL_MS", "10", float) / 1000.0
FAIL_RATE = env("FAIL_RATE", "0", float)
PASSES = max(1, env("PROCESS_PASSES", "1", int))
Q_LO = env("Q_LO", "0.02", float)
Q_HI = env("Q_HI", "0.98", float)
S = env("SLICE_SIZE", "256", int)
WEB_URL = env("WEB_URL", "http://web:8000")
SHUTDOWN_DEADLINE = env("SHUTDOWN_DEADLINE", "25", float)   # < terminationGracePeriodSeconds
RESOURCE = "/trial-participants"

if CLAIM_MODE not in ("naive", "for_update", "skip_locked"):
    sys.exit(f"bad CLAIM_MODE={CLAIM_MODE}")
if STORE_MODE not in ("plain", "idempotent"):
    sys.exit(f"bad STORE_MODE={STORE_MODE}")

# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
           5.0, 10.0, 30.0)
TASKS = Counter("worker_tasks_total", "Завершені задачі за результатом", ["result"])
CLAIMS = Counter("worker_claims_total", "Спроби захоплення за результатом", ["result"])
DURATION = Histogram("worker_task_duration_seconds", "Тривалість кроку задачі, с", ["step"],
                     buckets=BUCKETS)
IN_FLIGHT = Gauge("worker_tasks_in_flight", "Задачі в обробці цим воркером")
RETURNED = Counter("worker_tasks_returned_total",
                   "Задачі, повернуті в чергу під час коректної зупинки")
CANCELLED = Counter("worker_tasks_cancelled_total", "Задачі, зупинені через скасування (Д6)")
ZERO_ROWS = Counter("worker_zero_rows_total",
                    "Команди з умовою за токеном, що оновили нуль рядків", ["op"])
DUPLICATES = Counter("worker_duplicate_result_total",
                     "Спроби записати другий результат (лише STORE_MODE=plain з первинним ключем)")
for _r in ("done", "failed", "lost"):
    TASKS.labels(_r)
for _r in ("task", "empty"):
    CLAIMS.labels(_r)
for _s in ("claim", "fetch", "process", "store"):
    DURATION.labels(_s)


def log(event: str, **kw) -> None:
    rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
           "worker": WORKER_ID, "event": event, **kw}
    print(json.dumps(rec, ensure_ascii=False), flush=True)


# --------------------------------------------------------------------------
# Synthetic slice and processing step (pure Python, no NumPy)
# --------------------------------------------------------------------------
class PoisonTask(Exception):
    pass


def make_slice(seed: int, n: int) -> list:
    """x(0) = seed, x(i+1) = (1103515245·x(i) + 12345) mod 2^31, p(i) = x(i+1) >> 19."""
    out = [0] * n
    x = seed
    for i in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        out[i] = x >> 19
    return out


def process_slice(seed: int, q_lo: float = Q_LO, q_hi: float = Q_HI, size: int = S) -> dict:
    """Brightness window from histogram quantiles (a first-order transform).

    p_lo — the smallest level v for which the share of pixels with p <= v is at least q_lo;
    p_hi — the same for q_hi. p' = 0 outside [p_lo, p_hi], otherwise
    p' = ⌊4095·(p − p_lo)/(p_hi − p_lo)⌉ (round to nearest, half up).
    """
    if seed == 0:
        # scenario F3 (variant 25): a "poisoned" task — the processing step raises an exception
        raise PoisonTask("seed = 0: зріз не обробляється (отруєна задача)")
    n = size * size
    px = make_slice(seed, n)
    hist = [0] * 4096
    for p in px:
        hist[p] += 1
    lo_need, hi_need = q_lo * n, q_hi * n
    cum, p_lo, p_hi = 0, None, 4095
    for v in range(4096):
        cum += hist[v]
        if p_lo is None and cum >= lo_need:
            p_lo = v
        if cum >= hi_need:
            p_hi = v
            break
    span = p_hi - p_lo
    if span <= 0:
        raise ValueError(f"порожнє вікно яскравості: p_lo = p_hi = {p_lo}")
    two_span = 2 * span
    excluded = 0
    total = 0
    total_sq = 0
    kept = 0
    for p in px:
        if p < p_lo or p > p_hi:
            excluded += 1
        else:
            v = (8190 * (p - p_lo) + span) // two_span
            total += v
            total_sq += v * v
            kept += 1
    mean = total / kept
    var = max(0.0, total_sq / kept - mean * mean)
    return {"p_lo": p_lo, "p_hi": p_hi, "excluded": excluded, "kept": kept,
            "mean": round(mean, 4), "std": round(math.sqrt(var), 4),
            "q_lo": q_lo, "q_hi": q_hi, "size": size}


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------
_CLAIM_SET = """
UPDATE tasks SET status = 'running', worker_id = $1, token = token + 1,
       lease_until = CASE WHEN $2::float8 > 0
                          THEN clock_timestamp() + make_interval(secs => $2::float8) END,
       attempts = attempts + 1, started_at = clock_timestamp()
WHERE id = (SELECT id FROM tasks WHERE status = 'queued'
            ORDER BY created_at LIMIT 1 {lock})
RETURNING id, record_id, seed, token, attempts, max_attempts
"""
SQL_CLAIM = {
    "skip_locked": _CLAIM_SET.format(lock="FOR UPDATE SKIP LOCKED"),
    "for_update": _CLAIM_SET.format(lock="FOR UPDATE"),
}
SQL_NAIVE_SELECT = ("SELECT id, record_id, seed FROM tasks WHERE status = 'queued' "
                    "ORDER BY created_at LIMIT 1")
SQL_NAIVE_UPDATE = """
UPDATE tasks SET status = 'running', worker_id = $2, token = token + 1,
       attempts = attempts + 1, started_at = clock_timestamp()
WHERE id = $1
RETURNING id, record_id, seed, token, attempts, max_attempts
"""
SQL_HEARTBEAT = """
UPDATE tasks SET lease_until = clock_timestamp() + make_interval(secs => $3::float8)
WHERE id = $1 AND token = $2 AND status = 'running'
RETURNING cancel_requested
"""
SQL_DONE = """
UPDATE tasks SET status = 'done', finished_at = clock_timestamp(), lease_until = NULL
WHERE id = $1 AND token = $2 AND status = 'running'
"""
SQL_RESULT_IDEMPOTENT = """
INSERT INTO task_results (task_id, token, worker_id, result) VALUES ($1, $2, $3, $4::jsonb)
ON CONFLICT (task_id) DO NOTHING
"""
SQL_RESULT_PLAIN = ("INSERT INTO task_results (task_id, token, worker_id, result) "
                    "VALUES ($1, $2, $3, $4::jsonb)")
SQL_DONE_PLAIN = "UPDATE tasks SET status = 'done', finished_at = clock_timestamp() WHERE id = $1"
SQL_FAIL = """
UPDATE tasks SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
       worker_id = NULL, lease_until = NULL,
       finished_at = CASE WHEN attempts >= max_attempts THEN clock_timestamp() END
WHERE id = $1 AND token = $2 AND status = 'running'
RETURNING status
"""
SQL_RETURN = """
UPDATE tasks SET status = 'queued', worker_id = NULL, lease_until = NULL
WHERE id = $1 AND token = $2 AND status = 'running'
"""
SQL_CHECK = "SELECT cancel_requested FROM tasks WHERE id = $1 AND token = $2 AND status = 'running'"
SQL_CANCELLED = """
UPDATE tasks SET status = 'cancelled', finished_at = clock_timestamp(), lease_until = NULL
WHERE id = $1 AND token = $2 AND status = 'running'
"""


def rows(status: str) -> int:
    """'UPDATE 1' -> 1"""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


# --------------------------------------------------------------------------
# HTTP client to web (keep-alive)
# --------------------------------------------------------------------------
class Web:
    def __init__(self, base: str):
        u = urllib.parse.urlparse(base)
        self.host, self.port = u.hostname, u.port or 80
        self.reader = self.writer = None

    async def get(self, path: str) -> tuple[int, bytes]:
        for attempt in (1, 2):
            try:
                if self.writer is None:
                    self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                self.writer.write(f"GET {path} HTTP/1.1\r\nHost: {self.host}\r\n"
                                  f"User-Agent: worker\r\n\r\n".encode())
                head = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), 10)
                lines = head.decode("latin-1").split("\r\n")
                status = int(lines[0].split()[1])
                clen, close = 0, False
                for ln in lines[1:]:
                    k, _, v = ln.partition(":")
                    if k.lower() == "content-length":
                        clen = int(v)
                    elif k.lower() == "connection" and "close" in v.lower():
                        close = True
                body = await asyncio.wait_for(self.reader.readexactly(clen), 10)
                if close:
                    self.close()
                return status, body
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
                self.close()
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def close(self):
        if self.writer is not None:
            self.writer.close()
        self.reader = self.writer = None


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class TaskState:
    def __init__(self, row):
        self.id, self.record_id, self.seed = row["id"], row["record_id"], row["seed"]
        self.token, self.attempts = row["token"], row["attempts"]
        self.lost = False
        self.cancel = False


class Worker:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None
        self.web = Web(WEB_URL)
        self.stop = asyncio.Event()
        self.sigterm_at: float | None = None
        self.current: TaskState | None = None

    # ---------------------------------------------------------------- database
    async def connect(self):
        while True:
            try:
                self.pool = await asyncpg.create_pool(
                    host=env("DB_HOST", "db"), port=env("DB_PORT", "5432", int),
                    user=env("DB_USER", "trials"), password=env("DB_PASSWORD", ""),
                    database=env("DB_NAME", "trials"), min_size=2, max_size=3,
                    command_timeout=30)
                return
            except Exception as exc:  # noqa: BLE001
                log("db_unavailable", error=repr(exc))
                await asyncio.sleep(1)

    async def claim(self):
        async with self.pool.acquire() as c:
            if CLAIM_MODE == "naive":
                # Naive claim: separate SELECT and UPDATE, no transaction and no locks
                r = await c.fetchrow(SQL_NAIVE_SELECT)
                if r is None:
                    return None
                return await c.fetchrow(SQL_NAIVE_UPDATE, r["id"], WORKER_ID)
            return await c.fetchrow(SQL_CLAIM[CLAIM_MODE], WORKER_ID, LEASE)

    # ------------------------------------------------------------ heartbeat
    async def heartbeat(self, st: TaskState):
        period = LEASE / 3.0
        while True:
            await asyncio.sleep(period)
            try:
                async with self.pool.acquire() as c:
                    r = await c.fetchrow(SQL_HEARTBEAT, st.id, st.token, LEASE)
            except Exception as exc:  # noqa: BLE001
                log("heartbeat_error", task_id=st.id, token=st.token, error=repr(exc))
                continue
            if r is None:
                st.lost = True
                ZERO_ROWS.labels("heartbeat").inc()
                log("zero_rows", op="heartbeat", task_id=st.id, token=st.token,
                    msg="heartbeat оновив 0 рядків: задача вже не належить цьому воркеру")
                return
            if r["cancel_requested"]:
                st.cancel = True

    # --------------------------------------------------------- helpers
    def deadline_passed(self) -> bool:
        return self.sigterm_at is not None and time.monotonic() - self.sigterm_at > SHUTDOWN_DEADLINE

    async def give_back(self, st: TaskState):
        async with self.pool.acquire() as c:
            n = rows(await c.execute(SQL_RETURN, st.id, st.token))
        RETURNED.inc()
        log("returned", task_id=st.id, token=st.token, rows=n,
            msg="SIGTERM: задача не завершилась до строку, повернута в чергу")

    async def stop_cancelled(self, st: TaskState):
        async with self.pool.acquire() as c:
            n = rows(await c.execute(SQL_CANCELLED, st.id, st.token))
        CANCELLED.inc()
        log("cancelled", task_id=st.id, token=st.token, rows=n)

    async def between_passes(self, st: TaskState) -> str | None:
        """Check between processing passes: cancellation (D6), lost task, shutdown deadline."""
        if self.deadline_passed():
            await self.give_back(st)
            return "returned"
        async with self.pool.acquire() as c:
            r = await c.fetchrow(SQL_CHECK, st.id, st.token)
        if r is None:
            st.lost = True
            ZERO_ROWS.labels("check").inc()
            log("zero_rows", op="check", task_id=st.id, token=st.token)
            return "lost"
        if r["cancel_requested"] or st.cancel:
            await self.stop_cancelled(st)
            return "cancelled"
        return None

    # ------------------------------------------------------------- task
    async def execute(self, st: TaskState):
        loop = asyncio.get_running_loop()
        hb = asyncio.create_task(self.heartbeat(st)) if LEASE > 0 else None
        t_fetch = t_proc = t_store = 0.0
        try:
            t0 = time.perf_counter()
            status, body = await self.web.get(f"{RESOURCE}/{st.record_id}")
            t_fetch = time.perf_counter() - t0
            DURATION.labels("fetch").observe(t_fetch)
            if status != 200:
                raise RuntimeError(f"GET {RESOURCE}/{st.record_id} -> {status}")
            result = None
            for k in range(PASSES):
                if k > 0:
                    outcome = await self.between_passes(st)
                    if outcome:
                        if outcome == "lost":
                            TASKS.labels("lost").inc()
                        return
                t1 = time.perf_counter()
                result = await loop.run_in_executor(None, process_slice, st.seed)
                t_proc += time.perf_counter() - t1
            DURATION.labels("process").observe(t_proc)
            if st.cancel:
                await self.stop_cancelled(st)
                return
            if self.deadline_passed():
                await self.give_back(st)
                return
            if FAIL_RATE > 0 and random.random() < FAIL_RATE:
                log("crash", task_id=st.id, token=st.token, fail_rate=FAIL_RATE,
                    msg="FAIL_RATE: os._exit(1) після обробки, до запису результату")
                os._exit(1)
            payload = json.dumps(result)
            t2 = time.perf_counter()
            async with self.pool.acquire() as c:
                if STORE_MODE == "plain":
                    try:
                        await c.execute(SQL_RESULT_PLAIN, st.id, st.token, WORKER_ID, payload)
                    except asyncpg.UniqueViolationError:
                        DUPLICATES.inc()
                        log("duplicate_result", task_id=st.id, token=st.token)
                    n = rows(await c.execute(SQL_DONE_PLAIN, st.id))
                else:
                    async with c.transaction():
                        n = rows(await c.execute(SQL_DONE, st.id, st.token))
                        if n == 1:
                            await c.execute(SQL_RESULT_IDEMPOTENT, st.id, st.token, WORKER_ID,
                                            payload)
            t_store = time.perf_counter() - t2
            DURATION.labels("store").observe(t_store)
            if n == 0:
                ZERO_ROWS.labels("store").inc()
                TASKS.labels("lost").inc()
                log("zero_rows", op="store", task_id=st.id, token=st.token,
                    msg="завершення оновило 0 рядків: токен застарів, результат не записано")
                return
            TASKS.labels("done").inc()
            log("done", task_id=st.id, token=st.token, attempts=st.attempts,
                fetch_ms=round(t_fetch * 1000, 3), process_ms=round(t_proc * 1000, 3),
                store_ms=round(t_store * 1000, 3), result=result)
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError) as exc:
            log("db_error", task_id=st.id, token=st.token, error=repr(exc))
        except Exception as exc:  # noqa: BLE001 — processing error (including a poisoned task)
            TASKS.labels("failed").inc()
            try:
                async with self.pool.acquire() as c:
                    r = await c.fetchrow(SQL_FAIL, st.id, st.token)
                new_status = r["status"] if r else None
            except Exception as exc2:  # noqa: BLE001
                new_status = f"error: {exc2!r}"
            log("failed", task_id=st.id, token=st.token, attempts=st.attempts,
                error=f"{type(exc).__name__}: {exc}", new_status=new_status)
        finally:
            if hb is not None:
                hb.cancel()

    # ------------------------------------------------------------- loop
    def on_sigterm(self):
        if self.sigterm_at is None:
            self.sigterm_at = time.monotonic()
            log("shutdown", msg="SIGTERM: нові задачі не захоплюються",
                in_flight=self.current.id if self.current else None)
        self.stop.set()

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.on_sigterm)
        await self.connect()
        log("start", claim_mode=CLAIM_MODE, store_mode=STORE_MODE, lease_s=LEASE,
            poll_ms=POLL * 1000, fail_rate=FAIL_RATE, passes=PASSES, q_lo=Q_LO, q_hi=Q_HI,
            size=S, pid=os.getpid())
        while not self.stop.is_set():
            t0 = time.perf_counter()
            try:
                row = await self.claim()
            except Exception as exc:  # noqa: BLE001 — database unavailable
                log("claim_error", error=repr(exc))
                await asyncio.sleep(1)
                continue
            DURATION.labels("claim").observe(time.perf_counter() - t0)
            if row is None:
                CLAIMS.labels("empty").inc()
                try:
                    await asyncio.wait_for(self.stop.wait(), POLL)
                except asyncio.TimeoutError:
                    pass
                continue
            CLAIMS.labels("task").inc()
            st = TaskState(row)
            self.current = st
            log("claim", task_id=st.id, token=st.token, attempts=st.attempts,
                claim_ms=round((time.perf_counter() - t0) * 1000, 3))
            IN_FLIGHT.set(1)
            try:
                await self.execute(st)
            finally:
                IN_FLIGHT.set(0)
                self.current = None
        await self.pool.close()
        self.web.close()
        log("exit", code=0)
        return 0


def main() -> None:
    pid_file = env("PID_FILE", "/tmp/worker.pid")
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    start_http_server(env("METRICS_PORT", "9100", int))
    sys.exit(asyncio.run(Worker().run()))


if __name__ == "__main__":
    main()
