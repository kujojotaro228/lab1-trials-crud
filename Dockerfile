# ---------------------------------------------------------------------------
# Стадія 1 (builder): встановлення залежностей у віртуальне середовище.
# ---------------------------------------------------------------------------
FROM python:3.12-alpine AS builder

RUN apk add --no-cache build-base linux-headers libffi-dev

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# Спершу лише requirements.txt: шар із залежностями кешується і не
# перезбирається, коли змінюється тільки код сервісу.
COPY requirements.txt .
RUN pip install -r requirements.txt


# ---------------------------------------------------------------------------
# Стадія 2 (runtime): тільки інтерпретатор, venv і код. Без компіляторів.
# ---------------------------------------------------------------------------
FROM python:3.12-alpine AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN addgroup -S -g 10001 app \
 && adduser -S -D -H -u 10001 -G app app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY migrations ./migrations
RUN python -m compileall -q /app/app

USER app
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--loop", "uvloop", "--http", "httptools", \
     "--no-access-log", "--timeout-keep-alive", "75", "--backlog", "4096"]
