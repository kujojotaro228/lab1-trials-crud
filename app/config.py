"""Конфігурація сервісу. Усе читається зі змінних середовища.

Пароль до бази значення за замовчуванням не має: якщо DB_PASSWORD не задано,
процес завершується з помилкою одразу при старті.
"""
import os


def _env(name: str, default=None, cast=str):
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Environment variable {name} is required")
    return cast(value)


class Settings:
    def __init__(self) -> None:
        self.db_host: str = _env("DB_HOST", "localhost")
        self.db_port: int = _env("DB_PORT", "5432", int)
        self.db_name: str = _env("DB_NAME", "trials")
        self.db_user: str = _env("DB_USER", "trials")
        self.db_password: str = _env("DB_PASSWORD")  # без значення за замовчуванням

        # DB_POOL_SIZE — найбільша кількість відкритих зʼєднань екземпляра
        # (варіант 25: 10). Зʼєднань понад пул немає: max_size пулу asyncpg і є межею.
        # DB_POOL_MAX лишено як синонім для сумісності з ЛР 1.
        self.pool_size: int = _env("DB_POOL_SIZE", os.environ.get("DB_POOL_MAX") or "10", int)
        self.pool_min: int = min(_env("DB_POOL_MIN", "2", int), self.pool_size)
        # скільки секунд сервіс при старті чекає на базу, перш ніж почати
        # приймати запити (далі /healthz просто віддає 503, поки бази немає)
        self.db_startup_timeout: float = _env("DB_STARTUP_TIMEOUT", "30", float)

        # пагінація переліку
        self.list_default_limit: int = _env("LIST_DEFAULT_LIMIT", "20", int)
        self.list_max_limit: int = _env("LIST_MAX_LIMIT", "100", int)


settings = Settings()
