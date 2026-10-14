
import asyncio
import hashlib
import logging
import sys
import time
from pathlib import Path

import asyncpg

from .config import settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s migrate: %(message)s")
log = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 250_001  # довільне число, однакове для всіх запусків міграції


async def connect_with_retry(timeout: float = 60.0) -> asyncpg.Connection:
    deadline = time.monotonic() + timeout
    delay = 0.5
    while True:
        try:
            return await asyncpg.connect(
                host=settings.db_host, port=settings.db_port,
                user=settings.db_user, password=settings.db_password,
                database=settings.db_name, timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() >= deadline:
                raise
            log.info("database not ready (%r), retry in %.1fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 3.0)


async def migrate() -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        log.error("no migration files in %s", MIGRATIONS_DIR)
        return 1

    conn = await connect_with_retry()
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                checksum   TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        applied = {r["version"]: r["checksum"]
                   for r in await conn.fetch("SELECT version, checksum FROM schema_migrations")}

        count = 0
        for f in files:
            sql = f.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if f.name in applied:
                if applied[f.name] != checksum:
                    log.error("migration %s was changed after it had been applied", f.name)
                    return 1
                log.info("skip %s (already applied)", f.name)
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)",
                    f.name, checksum)
            log.info("applied %s", f.name)
            count += 1

        log.info("done: %d new migration(s) applied, %d file(s) total", count, len(files))
        return 0
    finally:
        await conn.close()  # advisory-lock звільняється разом із сесією


def main() -> None:
    try:
        code = asyncio.run(migrate())
    except Exception as exc:  # noqa: BLE001
        log.exception("migration failed: %r", exc)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
