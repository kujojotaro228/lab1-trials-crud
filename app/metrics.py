"""Метрики Prometheus (ЛР 2, варіант 25: бакети Г1, додаткова метрика М3).

Метрики реєструються один раз під час імпорту модуля (тобто під час запуску
сервісу) і оновлюються в проміжному обробнику MetricsMiddleware.

* http_requests_total{method, path, code}         — лічильник запитів;
* http_request_duration_seconds{method, path}      — гістограма тривалості обробки;
* http_requests_in_progress                        — вимірювач запитів в обробці;
* cgroup_cpu_nr_periods_total / _nr_throttled_total — М3: лічильники з cpu.stat
  контрольної групи контейнера (cgroup v2 або v1);
* process_cpu_seconds_total та інші process_*      — стандартний ProcessCollector.

У мітці path — шаблон маршруту (/trial-participants/{participant_id}), а не
конкретний ідентифікатор, щоб кількість рядів не росла з кількістю записів.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable, Optional

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import REGISTRY, CounterMetricFamily

# Набір Г1: 5, 10, 25, 50, 100, 250, 500, 1000 мс (у секундах за конвенцією Prometheus)
BUCKETS_G1 = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)

# шляхи, які не входять у вимірювач «в обробці»: сам запит збору метрик завжди
# «в обробці» в момент збору і додавав би до кожного значення одиницю
_NOT_IN_PROGRESS = {"/metrics"}

REQUESTS = Counter(
    "http_requests_total", "Кількість HTTP-запитів",
    ["method", "path", "code"])
DURATION = Histogram(
    "http_request_duration_seconds", "Тривалість обробки HTTP-запиту, с",
    ["method", "path"], buckets=BUCKETS_G1)
IN_PROGRESS = Gauge(
    "http_requests_in_progress", "Кількість HTTP-запитів в обробці")
POD_INFO = Gauge(
    "app_pod_info", "Імʼя екземпляра (POD_NAME або hostname)", ["pod"])


# --------------------------------------------------------------------------
# М3: дроселювання CPU з cpu.stat контрольної групи
# --------------------------------------------------------------------------
def _own_cgroup_paths() -> tuple[Optional[str], Optional[str]]:
    """(шлях у cgroup v2, шлях у cgroup v1 контролера cpu) з /proc/self/cgroup."""
    v2 = v1 = None
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            hid, ctrls, path = line.split(":", 2)
            if hid == "0" and ctrls == "":
                v2 = path
            elif "cpu" in ctrls.split(","):
                v1 = path
    except OSError:
        pass
    return v2, v1


def find_cpu_stat() -> Optional[Path]:
    """Файл cpu.stat власної контрольної групи процесу.

    У контейнері Docker власна група змонтована як корінь /sys/fs/cgroup, тому
    спершу перевіряється шлях з /proc/self/cgroup, потім — корінь.
    Шлях можна задати явно змінною CGROUP_CPU_STAT.
    """
    explicit = os.environ.get("CGROUP_CPU_STAT")
    if explicit:
        return Path(explicit)
    v2, v1 = _own_cgroup_paths()
    cands = []
    if v2 is not None:
        cands += [Path("/sys/fs/cgroup" + v2.rstrip("/")) / "cpu.stat",
                  Path("/sys/fs/cgroup/cpu.stat")]
    for root in ("/sys/fs/cgroup/cpu,cpuacct", "/sys/fs/cgroup/cpu"):
        if v1 is not None:
            cands.append(Path(root + v1.rstrip("/")) / "cpu.stat")
        cands.append(Path(root) / "cpu.stat")
    for c in cands:
        try:
            text = c.read_text()
        except OSError:
            continue
        if "nr_periods" in text:
            return c
    return None


class CgroupCpuCollector:
    """Віддає nr_periods і nr_throttled з cpu.stat як лічильники.

    Частка дроселювання за інтервал = приріст nr_throttled / приріст nr_periods.
    """

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path

    def collect(self) -> Iterable:
        if self.path is None:
            return
        try:
            stat = dict(line.split() for line in self.path.read_text().splitlines()
                        if len(line.split()) == 2)
        except (OSError, ValueError):
            return
        periods = CounterMetricFamily(
            "cgroup_cpu_nr_periods", "Кількість періодів CFS (cpu.stat nr_periods)")
        throttled = CounterMetricFamily(
            "cgroup_cpu_nr_throttled",
            "Кількість періодів CFS, у яких групу було обмежено (cpu.stat nr_throttled)")
        periods.add_metric([], float(stat.get("nr_periods", 0)))
        throttled.add_metric([], float(stat.get("nr_throttled", 0)))
        yield periods
        yield throttled
        # сумарний час дроселювання: v2 — throttled_usec, v1 — throttled_time (нс)
        if "throttled_usec" in stat:
            secs = float(stat["throttled_usec"]) / 1e6
        elif "throttled_time" in stat:
            secs = float(stat["throttled_time"]) / 1e9
        else:
            return
        t = CounterMetricFamily("cgroup_cpu_throttled_seconds",
                                "Сумарний час дроселювання групи, с")
        t.add_metric([], secs)
        yield t


CPU_STAT_PATH = find_cpu_stat()
REGISTRY.register(CgroupCpuCollector(CPU_STAT_PATH))


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# --------------------------------------------------------------------------
# Проміжний обробник
# --------------------------------------------------------------------------
class MetricsMiddleware:
    """Чиста ASGI-обгортка: рахує запити, тривалість і запити в обробці.

    Шаблон шляху береться з scope["route"], який FastAPI заповнює під час
    маршрутизації; для запитів, що не потрапили в жоден маршрут, path="<unmatched>".
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        counted = scope.get("path") not in _NOT_IN_PROGRESS
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        if counted:
            IN_PROGRESS.inc()
        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            if counted:
                IN_PROGRESS.dec()
            route = scope.get("route")
            path = getattr(route, "path", None) or "<unmatched>"
            method = scope.get("method", "")
            REQUESTS.labels(method, path, str(status["code"])).inc()
            DURATION.labels(method, path).observe(elapsed)
