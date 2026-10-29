# Код завершення за SIGTERM: uvicorn CLI проти app.serve

Обидва процеси запущено в стенді-емуляції (cgroup 0.5 CPU, 512 МБ), до бази
підключено, потім надіслано SIGTERM (аналог `docker stop`). Код виходу
записано так, як його показує `docker inspect --format '{{.State.ExitCode}}'`
(128 + номер сигналу, якщо процес завершено сигналом).

| Команда (CMD) | Код завершення |
|---|---:|
| `uvicorn app.main:app ...` (CMD ЛР 1) | 143 |
| `python -m app.serve` (CMD ЛР 2) | 0 |

Журнал `python -m app.serve`:

```text
INFO:     Uvicorn running on http://127.0.2.2:8000 (Press CTRL+C to quit)
INFO:     Shutting down
INFO:     Waiting for application shutdown.
2026-10-29 16:56:54,718 INFO app: instance=t-serve: connection pool closed
INFO:     Application shutdown complete.
INFO:     Finished server process [4343]
2026-10-29 16:56:54,719 INFO app.serve: signal SIGTERM handled: graceful shutdown complete
```
