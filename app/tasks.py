"""Lab 4: task processing queue (endpoints, tasks_queued/tasks_running gauges,
returning tasks with an expired lease).

Endpoints:
  POST   /trial-participants/{id}/tasks   create a processing task for a record: 202 / 404
  GET    /tasks/{id}                      task state together with its result: 200 / 404
  GET    /tasks?status=&limit=&offset=    list with filter and pagination
  DELETE /tasks/{id}                      D6: cancellation (queued -> cancelled,
                                          running -> cancel_requested flag)

Background loop (every 5 s, in every web replica):
  * updates the tasks_queued and tasks_running gauges with a database query;
  * returns tasks with an expired lease to the queue (attempts < max_attempts) or
    moves them to failed (attempts >= max_attempts); if cancellation was requested
    for the task — to cancelled.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Dict, Optional

import asyncpg
from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge

from . import db
from .config import settings

log = logging.getLogger("app.tasks")
router = APIRouter()

RESOURCE = "/trial-participants"
MAX_ID = 2 ** 63 - 1
TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")
REAPER_INTERVAL = 5.0

TASKS_QUEUED = Gauge("tasks_queued", "Кількість задач у статусі queued (оновлюється кожні 5 с)")
TASKS_RUNNING = Gauge("tasks_running", "Кількість задач у статусі running (оновлюється кожні 5 с)")
TASKS_REAPED = Counter("tasks_reaped_total",
                       "Задачі зі спливлим lease, повернуті фоновим циклом", ["to"])

TASK_COLUMNS = ("t.id, t.record_id, t.status, t.seed, t.attempts, t.max_attempts, t.token, "
                "t.lease_until, t.worker_id, t.created_at, t.started_at, t.finished_at, "
                "t.cancel_requested, t.cancel_requested_at")
TASK_COLS_PLAIN = TASK_COLUMNS.replace("t.", "")

SQL_CREATE = """
INSERT INTO tasks (record_id, seed)
SELECT p.id, $2 FROM trial_participants p WHERE p.id = $1
RETURNING id, record_id, status, seed, attempts, max_attempts, token, lease_until, worker_id,
          created_at, started_at, finished_at, cancel_requested, cancel_requested_at
"""

SQL_REAP = """
UPDATE tasks
SET status = CASE WHEN cancel_requested THEN 'cancelled'
                  WHEN attempts >= max_attempts THEN 'failed'
                  ELSE 'queued' END,
    worker_id = NULL,
    lease_until = NULL,
    finished_at = CASE WHEN cancel_requested OR attempts >= max_attempts
                       THEN clock_timestamp() END
WHERE status = 'running' AND lease_until < clock_timestamp()
RETURNING id, status
"""


def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None else None


def task_to_dict(r: asyncpg.Record) -> Dict[str, Any]:
    return {
        "id": r["id"], "record_id": r["record_id"], "status": r["status"], "seed": r["seed"],
        "attempts": r["attempts"], "max_attempts": r["max_attempts"], "token": r["token"],
        "lease_until": _iso(r["lease_until"]), "worker_id": r["worker_id"],
        "created_at": _iso(r["created_at"]), "started_at": _iso(r["started_at"]),
        "finished_at": _iso(r["finished_at"]),
        "cancel_requested": r["cancel_requested"],
        "cancel_requested_at": _iso(r["cancel_requested_at"]),
    }


def _err(status: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, "details": details or []},
                        status_code=status)


def _task_not_found(task_id: int) -> JSONResponse:
    return _err(404, "not_found", f"Задачу з id={task_id} не знайдено")


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.post(RESOURCE + "/{participant_id}/tasks", status_code=202)
async def create_task(participant_id: int = Path(ge=1, le=MAX_ID)):
    # the service generates seed; 0 is reserved for "poisoned" tasks of scenario F3
    seed = random.randint(1, 2 ** 31 - 1)
    pool = await db.get_pool()
    row = await pool.fetchrow(SQL_CREATE, participant_id, seed)
    if row is None:
        return _err(404, "not_found", f"Учасника з id={participant_id} не знайдено")
    return JSONResponse(task_to_dict(row), status_code=202,
                        headers={"Location": f"/tasks/{row['id']}"})


@router.get("/tasks/{task_id}")
async def get_task(task_id: int = Path(ge=1, le=MAX_ID)):
    pool = await db.get_pool()
    row = await pool.fetchrow(f"SELECT {TASK_COLUMNS} FROM tasks t WHERE t.id = $1", task_id)
    if row is None:
        return _task_not_found(task_id)
    # in step 3 (no primary key) a task may have several results: show the first one
    # and the count, so duplication is visible through the API as well
    results = await pool.fetch(
        "SELECT token, worker_id, result::text AS result, created_at FROM task_results "
        "WHERE task_id = $1 ORDER BY created_at", task_id)
    body = task_to_dict(row)
    body["results_count"] = len(results)
    if results:
        first = results[0]
        body["result"] = {"token": first["token"], "worker_id": first["worker_id"],
                          "created_at": _iso(first["created_at"]),
                          **json.loads(first["result"])}
    else:
        body["result"] = None
    return JSONResponse(body)


@router.get("/tasks")
async def list_tasks(
    status: Optional[str] = Query(None, description="queued, running, done, failed, cancelled"),
    limit: int = Query(settings.list_default_limit, ge=1, le=settings.list_max_limit),
    offset: int = Query(0, ge=0, le=1_000_000_000),
):
    if status is not None and status not in TASK_STATUSES:
        return _err(400, "validation_error", "Невалідні дані запиту", [{
            "field": "status", "location": "query",
            "message": f"фільтр має бути одним із значень: {', '.join(TASK_STATUSES)}; "
                       f"отримано '{status}'"}])
    pool = await db.get_pool()
    if status is None:
        total = await pool.fetchval("SELECT count(*) FROM tasks")
        rows = await pool.fetch(f"SELECT {TASK_COLUMNS} FROM tasks t ORDER BY t.id "
                                "LIMIT $1 OFFSET $2", limit, offset)
    else:
        total = await pool.fetchval("SELECT count(*) FROM tasks WHERE status = $1", status)
        rows = await pool.fetch(f"SELECT {TASK_COLUMNS} FROM tasks t WHERE t.status = $1 "
                                "ORDER BY t.id LIMIT $2 OFFSET $3", status, limit, offset)
    return JSONResponse({"items": [task_to_dict(r) for r in rows], "total": total,
                         "limit": limit, "offset": offset})


@router.delete("/tasks/{task_id}")
async def cancel_task(task_id: int = Path(ge=1, le=MAX_ID)):
    """D6: queued -> cancelled immediately; running -> a flag the worker checks between
    processing passes. The request time is stored in cancel_requested_at (database clock)."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT status FROM tasks WHERE id = $1 FOR UPDATE",
                                      task_id)
            if row is None:
                return _task_not_found(task_id)
            if row["status"] == "queued":
                r = await conn.fetchrow(
                    "UPDATE tasks SET status = 'cancelled', cancel_requested = true, "
                    "cancel_requested_at = clock_timestamp(), finished_at = clock_timestamp() "
                    f"WHERE id = $1 RETURNING {TASK_COLS_PLAIN}", task_id)
                return JSONResponse(task_to_dict(r), status_code=200)
            if row["status"] == "running":
                r = await conn.fetchrow(
                    "UPDATE tasks SET cancel_requested = true, "
                    "cancel_requested_at = COALESCE(cancel_requested_at, clock_timestamp()) "
                    f"WHERE id = $1 RETURNING {TASK_COLS_PLAIN}", task_id)
                return JSONResponse(task_to_dict(r), status_code=202)
    return _err(409, "conflict",
                f"Задачу в статусі {row['status']} скасувати не можна")


# --------------------------------------------------------------------------
# Background loop: gauges and returning tasks with an expired lease
# --------------------------------------------------------------------------
async def background_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            pool = await db.get_pool()
            if await pool.fetchval("SELECT to_regclass('public.tasks') IS NOT NULL"):
                reaped = await pool.fetch(SQL_REAP)
                for r in reaped:
                    TASKS_REAPED.labels(r["status"]).inc()
                if reaped:
                    log.info("lease expired: %s", ", ".join(f"{r['id']}->{r['status']}"
                                                            for r in reaped))
                row = await pool.fetchrow(
                    "SELECT count(*) FILTER (WHERE status = 'queued') AS q, "
                    "count(*) FILTER (WHERE status = 'running') AS r FROM tasks")
                TASKS_QUEUED.set(row["q"])
                TASKS_RUNNING.set(row["r"])
        except Exception as exc:  # noqa: BLE001 — the database may have been unavailable
            log.warning("tasks background loop: %r", exc)
        try:
            await asyncio.wait_for(stop.wait(), REAPER_INTERVAL)
        except asyncio.TimeoutError:
            pass
