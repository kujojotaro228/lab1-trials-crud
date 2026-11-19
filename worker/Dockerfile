# Lab 4: worker image (separate from web). Built from the repository root:
#   docker build -f worker/Dockerfile -t trials-worker:lab4 .
FROM python:3.12-alpine AS builder
RUN apk add --no-cache build-base linux-headers
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY worker/requirements.txt /build/requirements.txt
RUN pip install -r /build/requirements.txt

FROM python:3.12-alpine AS runtime
# tini as PID 1: forwards SIGTERM to the worker and reaps its exit code.
# The worker itself is then not PID 1, so `kill -STOP <worker pid>` really stops it
# (the kernel ignores STOP/KILL sent to PID 1 of a PID namespace from inside it).
RUN apk add --no-cache tini \
 && addgroup -S -g 10001 app && adduser -S -D -H -u 10001 -G app app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY worker/worker.py ./worker.py
USER app
EXPOSE 9100
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python", "worker.py"]
