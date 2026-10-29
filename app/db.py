"""Пул підключень до PostgreSQL (asyncpg) з очікуванням готовності бази."""
import asyncio
import logging
from typing import Optional

import asyncpg

from .config import settings

log = logging.getLogger("app.db")

_pool: Optional[asyncpg.Pool] = None
_lock = asyncio.Lock()


async def _create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        min_size=settings.pool_min,
        max_size=settings.pool_size,      # DB_POOL_SIZE: зʼєднань понад пул не буває
        timeout=3,            # тайм-аут встановлення одного підключення, с
        command_timeout=5,    # тайм-аут одного запиту до бази, с
        max_inactive_connection_lifetime=300,
    )


async def get_pool() -> asyncpg.Pool:
    """Повертає пул; якщо його ще немає (база була недоступна) — пробує створити.

    Якщо база недоступна, кидає виняток (OSError / asyncpg.*), який обробник
    у main.py перетворює на 503.
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _lock:
        if _pool is None:
            _pool = await _create_pool()
            log.info("connection pool created (%s:%s/%s)",
                     settings.db_host, settings.db_port, settings.db_name)
    return _pool


async def wait_for_db(timeout: float) -> bool:
    """Чекає на базу з експоненційною затримкою не довше за timeout секунд."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    delay = 0.2
    while True:
        try:
            await get_pool()
            return True
        except Exception as exc:  # noqa: BLE001 — будь-яка помилка підключення
            if loop.time() >= deadline:
                log.warning("database is still unavailable after %.0fs: %r", timeout, exc)
                return False
            log.info("waiting for database: %r (retry in %.1fs)", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
