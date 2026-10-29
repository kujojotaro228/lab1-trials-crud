"""Точка входу контейнера: python -m app.serve (CMD в exec-формі, PID 1).

Коректна зупинка за SIGTERM:
  1. uvicorn перехоплює SIGTERM, закриває сокет, що слухає (нові зʼєднання не
     приймаються), закриває простоювані keep-alive зʼєднання і чекає завершення
     запитів в обробці (не довше SHUTDOWN_TIMEOUT секунд);
  2. виконується завершальна частина lifespan — закривається пул зʼєднань з базою;
  3. процес завершується з кодом 0.

Чому не CMD ["uvicorn", ...]: починаючи з версії 0.29 uvicorn після коректної
зупинки повторно надсилає собі перехоплений сигнал (signal.raise_signal), щоб
відтворити «стандартну» поведінку. Для SIGTERM стандартна дія — завершення
процесу сигналом, і Docker бачить код 143, хоча всі запити вже дообслуговано.
Тут до запуску сервера встановлено власний обробник SIGTERM: uvicorn
відновлює саме його і повторний сигнал лише фіксується в журналі.
"""
from __future__ import annotations

import logging
import os
import signal
import sys

import uvicorn

log = logging.getLogger("app.serve")


def _after_shutdown(signum, _frame) -> None:
    log.info("signal %s handled: graceful shutdown complete", signal.Signals(signum).name)


def main() -> int:
    try:
        import uvloop  # noqa: F401
        loop = "uvloop"
    except ImportError:          # Windows / середовище без uvloop
        loop = "asyncio"
    try:
        import httptools  # noqa: F401
        http = "httptools"
    except ImportError:
        http = "h11"

    config = uvicorn.Config(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        loop=loop,
        http=http,
        access_log=False,
        timeout_keep_alive=75,
        backlog=4096,
        # скільки секунд дочікуватися запитів в обробці; має бути меншим за
        # stop_grace_period у docker-compose.yml (30 с), інакше прийде SIGKILL
        timeout_graceful_shutdown=float(os.environ.get("SHUTDOWN_TIMEOUT", "20")),
        lifespan="on",
    )
    server = uvicorn.Server(config)

    signal.signal(signal.SIGTERM, _after_shutdown)
    signal.signal(signal.SIGINT, _after_shutdown)
    server.run()
    # server.started == False означає, що сервер не піднявся (наприклад, порт зайнятий)
    return 0 if server.started else 3


if __name__ == "__main__":
    sys.exit(main())
