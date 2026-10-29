
import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import db, metrics
from .config import settings
from .schemas import (
    ALLOWED_GROUPS,
    ParticipantIn,
    cross_field_errors,
    translate_pydantic_errors,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

RESOURCE = "/trial-participants"
# імʼя екземпляра: POD_NAME, а якщо змінну не задано — hostname контейнера
INSTANCE = os.environ.get("POD_NAME") or socket.gethostname()
MAX_ID = 2 ** 63 - 1                     # межа BIGINT
MAX_OFFSET = 1_000_000_000

COLUMNS = ("id, participant_code, protocol, randomization_group, enrollment_date, "
           "status, completion_date, created_at, updated_at")

SQL_INSERT = f"""
INSERT INTO trial_participants
    (participant_code, protocol, randomization_group, enrollment_date, status, completion_date)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING {COLUMNS}
"""
SQL_GET = f"SELECT {COLUMNS} FROM trial_participants WHERE id = $1"
SQL_UPDATE = f"""
UPDATE trial_participants
SET participant_code = $2, protocol = $3, randomization_group = $4,
    enrollment_date = $5, status = $6, completion_date = $7, updated_at = now()
WHERE id = $1
RETURNING {COLUMNS}
"""
SQL_DELETE = "DELETE FROM trial_participants WHERE id = $1 RETURNING id"


# --------------------------------------------------------------------------
# Допоміжні функції
# --------------------------------------------------------------------------
def row_to_dict(r: asyncpg.Record) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "participant_code": r["participant_code"],
        "protocol": r["protocol"],
        "randomization_group": r["randomization_group"],
        "enrollment_date": r["enrollment_date"].isoformat(),
        "status": r["status"],
        "completion_date": r["completion_date"].isoformat() if r["completion_date"] else None,
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    }


def error_response(status: int, code: str, message: str,
                   details: Optional[List[Dict[str, str]]] = None) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, "details": details or []},
                        status_code=status)


def validation_error(details: List[Dict[str, str]]) -> JSONResponse:
    return error_response(400, "validation_error", "Невалідні дані запиту", details)


def not_found(participant_id: int) -> JSONResponse:
    return error_response(404, "not_found",
                          f"Учасника з id={participant_id} не знайдено")


def conflict(code: str) -> JSONResponse:
    return error_response(409, "conflict", "Учасник з таким кодом уже існує", [
        {"field": "participant_code", "location": "body",
         "message": f"значення '{code}' вже використовується"}])


def values(p: ParticipantIn) -> tuple:
    return (p.participant_code, p.protocol, p.randomization_group,
            p.enrollment_date, p.status, p.completion_date)


# --------------------------------------------------------------------------
# Застосунок
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    metrics.POD_INFO.labels(INSTANCE).set(1)
    ready = await db.wait_for_db(settings.db_startup_timeout)
    log.info("instance=%s started, database ready=%s, pool size=%d, cpu.stat=%s",
             INSTANCE, ready, settings.pool_size, metrics.CPU_STAT_PATH)
    yield
    # Сюди потрапляємо під час коректної зупинки: uvicorn уже перестав приймати
    # зʼєднання і дочекався запитів в обробці. Лишається закрити пул.
    await db.close_pool()
    log.info("instance=%s: connection pool closed", INSTANCE)


app = FastAPI(title="Trial participants CRUD", version="1.0.0", lifespan=lifespan)


class InstanceHeaderMiddleware:
    """Чиста ASGI-обгортка: додає до кожної відповіді заголовок X-Pod з іменем
    екземпляра (і X-Instance з тим самим значенням — його читають скрипти ЛР 1).

    Дає змогу генератору навантаження порахувати, скільки відповідей дав
    кожен екземпляр (підтвердження балансування). Стану не тримає.
    """

    def __init__(self, app):
        self.app = app
        self.headers = [(b"x-pod", INSTANCE.encode()), (b"x-instance", INSTANCE.encode())]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + self.headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(InstanceHeaderMiddleware)
app.add_middleware(metrics.MetricsMiddleware)   # додано останнім — зовнішній шар


# --------------------------------------------------------------------------
# Обробники помилок: жодна очікувана помилка не виходить назовні як 500
# --------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def on_request_validation_error(_request: Request, exc: RequestValidationError):
    return validation_error(translate_pydantic_errors(exc.errors()))


def _asyncpg_exc(name: str):
    return getattr(asyncpg.exceptions, name, None)


# Усе, що означає «база недоступна» → 503
DB_UNAVAILABLE = tuple(e for e in (
    OSError,                       # включно з ConnectionRefusedError і TimeoutError
    asyncio.TimeoutError,
    _asyncpg_exc("PostgresConnectionError"),
    _asyncpg_exc("CannotConnectNowError"),
    _asyncpg_exc("AdminShutdownError"),
    _asyncpg_exc("InterfaceError"),   # у т.ч. ConnectionDoesNotExistError
) if e is not None)


async def on_db_unavailable(_request: Request, exc: Exception):
    log.warning("database unavailable: %r", exc)
    return error_response(503, "database_unavailable",
                          "База даних тимчасово недоступна, повторіть запит пізніше")


for _exc_cls in DB_UNAVAILABLE:
    app.add_exception_handler(_exc_cls, on_db_unavailable)


@app.exception_handler(asyncpg.CheckViolationError)
async def on_check_violation(_request: Request, exc: asyncpg.CheckViolationError):
    # Сюди потрапити не повинно: сервіс перевіряє дані раніше за базу.
    # CHECK у таблиці — другий рубіж; навіть тоді відповідаємо 400, а не 500.
    return validation_error([{"field": getattr(exc, "constraint_name", "") or "body",
                              "location": "body",
                              "message": "порушено обмеження бази даних"}])


async def on_bad_encoding(_request: Request, _exc: Exception):
    # Другий рубіж для рядків, які база не може зберегти (наприклад, \x00).
    # Схема відсіює їх раніше, але навіть тоді клієнт отримує 400, а не 500.
    return validation_error([{"field": "body", "location": "body",
                              "message": "рядок містить символ, який не можна зберегти"}])


for _name in ("CharacterNotInRepertoireError", "UntranslatableCharacterError"):
    _cls = _asyncpg_exc(_name)
    if _cls is not None:
        app.add_exception_handler(_cls, on_bad_encoding)


# --------------------------------------------------------------------------
# Ендпоінти
# --------------------------------------------------------------------------
@app.post(RESOURCE, status_code=201)
async def create_participant(body: ParticipantIn):
    errors = cross_field_errors(body)
    if errors:
        return validation_error(errors)
    pool = await db.get_pool()
    try:
        row = await pool.fetchrow(SQL_INSERT, *values(body))
    except asyncpg.UniqueViolationError:
        return conflict(body.participant_code)
    return JSONResponse(row_to_dict(row), status_code=201,
                        headers={"Location": f"{RESOURCE}/{row['id']}"})


@app.get(RESOURCE)
async def list_participants(
    randomization_group: Optional[str] = Query(None, description="A, B або контроль"),
    limit: int = Query(settings.list_default_limit, ge=1, le=settings.list_max_limit),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
):
    if randomization_group is not None and randomization_group not in ALLOWED_GROUPS:
        return validation_error([{
            "field": "randomization_group", "location": "query",
            "message": "фільтр має бути одним із значень: A, B, контроль; "
                       f"отримано '{randomization_group}'"}])

    pool = await db.get_pool()
    if randomization_group is None:
        total = await pool.fetchval("SELECT count(*) FROM trial_participants")
        rows = await pool.fetch(
            f"SELECT {COLUMNS} FROM trial_participants ORDER BY id LIMIT $1 OFFSET $2",
            limit, offset)
    else:
        total = await pool.fetchval(
            "SELECT count(*) FROM trial_participants WHERE randomization_group = $1",
            randomization_group)
        rows = await pool.fetch(
            f"SELECT {COLUMNS} FROM trial_participants WHERE randomization_group = $1 "
            "ORDER BY id LIMIT $2 OFFSET $3",
            randomization_group, limit, offset)

    return JSONResponse({
        "items": [row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@app.get(RESOURCE + "/{participant_id}")
async def get_participant(participant_id: int = Path(ge=1, le=MAX_ID)):
    pool = await db.get_pool()
    row = await pool.fetchrow(SQL_GET, participant_id)
    if row is None:
        return not_found(participant_id)
    return JSONResponse(row_to_dict(row))


@app.put(RESOURCE + "/{participant_id}")
async def replace_participant(body: ParticipantIn,
                              participant_id: int = Path(ge=1, le=MAX_ID)):
    errors = cross_field_errors(body)
    if errors:
        return validation_error(errors)
    pool = await db.get_pool()
    try:
        row = await pool.fetchrow(SQL_UPDATE, participant_id, *values(body))
    except asyncpg.UniqueViolationError:
        return conflict(body.participant_code)
    if row is None:
        return not_found(participant_id)
    return JSONResponse(row_to_dict(row))


@app.delete(RESOURCE + "/{participant_id}", status_code=204)
async def delete_participant(participant_id: int = Path(ge=1, le=MAX_ID)):
    pool = await db.get_pool()
    deleted = await pool.fetchval(SQL_DELETE, participant_id)
    if deleted is None:
        return not_found(participant_id)
    return Response(status_code=204)


@app.get("/livez")
async def livez():
    """Живучість процесу: 200 без звернення до бази."""
    return JSONResponse({"status": "alive", "instance": INSTANCE})


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


@app.get("/healthz")
async def healthz():
    """Готовність: база відповідає і схема накочена міграцією."""
    try:
        pool = await db.get_pool()
        schema_ok = await asyncio.wait_for(
            pool.fetchval("SELECT to_regclass('public.trial_participants') IS NOT NULL"),
            timeout=2,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "unavailable", "reason": "database unreachable",
                             "detail": type(exc).__name__, "instance": INSTANCE},
                            status_code=503)
    if not schema_ok:
        return JSONResponse({"status": "unavailable", "reason": "schema not migrated",
                             "instance": INSTANCE}, status_code=503)
    return JSONResponse({"status": "ok", "instance": INSTANCE})
