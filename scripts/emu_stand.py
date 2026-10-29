#!/usr/bin/env python3
"""Стенд-емуляція docker-compose.yml без Docker (Linux, root, cgroup v1).

Використовувався для вимірювань ЛР 2 у середовищі, де Docker недоступний.
Процеси й параметри ті самі, що в Compose:

| Compose  | Емуляція                                                             |
|----------|----------------------------------------------------------------------|
| db       | PostgreSQL 16, 127.0.0.1:5432, без лімітів                            |
| migrate  | python -m app.migrate (разово)                                        |
| web × N  | python -m app.serve на 127.0.1.i:8000, cgroup cpu 0.5 CPU, memory 512M|
| lb       | nginx 1.28 з nginx/nginx.conf (listen 8080, resolver 127.0.0.11)      |
| prometheus | Prometheus 2.53 з prometheus/prometheus.yml (dns_sd_configs: web)   |
| DNS Docker | scripts/emu_dns.py на 127.0.0.11: web -> адреси запущених web-i    |

    python scripts/emu_stand.py up [--web 1]
    python scripts/emu_stand.py scale 3
    python scripts/emu_stand.py status
    python scripts/emu_stand.py down
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EMU = Path(os.environ.get("EMU_DIR", "/tmp/trials-emu"))
PG_BIN = Path(os.environ.get("PG_BIN", "/opt/pg/bin"))
NGINX = os.environ.get("NGINX_BIN", "/opt/nginx/sbin/nginx")
PROMETHEUS = os.environ.get("PROMETHEUS_BIN", "/opt/prometheus/prometheus")
PY = os.environ.get("EMU_PYTHON", "/opt/lab2venv/bin/python")
PG_USER = "postgres"
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "emu-lab2-password")
WEB_CPUS = 0.5
WEB_MEM = "512M"


def web_ip(i: int) -> str:
    return f"127.0.1.{i}"


def sh(*cmd, check=True, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))}: {r.stdout}{r.stderr}")
    return r


def wait_http(url: str, timeout: float = 60, ok=(200,)) -> None:
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status in ok:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} не відповідає")


def spawn(name: str, cmd: list, cpus=None, mem=None, env=None) -> int:
    sup = [sys.executable, str(REPO / "scripts" / "emu_run.py"), "--name", name,
           "--state", str(EMU / "state")]
    if cpus:
        sup += ["--cpus", str(cpus)]
    if mem:
        sup += ["--mem", mem]
    p = subprocess.Popen(sup + ["--"] + [str(c) for c in cmd], cwd=REPO,
                         env={**os.environ, **(env or {})}, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if (EMU / "state" / f"{name}.json").exists():
            break
        time.sleep(0.05)
    return p.pid


def state(name: str) -> dict | None:
    p = EMU / "state" / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def running(name: str) -> bool:
    s = state(name)
    if not s or not s.get("running"):
        return False
    try:
        os.kill(s["supervisor_pid"], 0)
        return True
    except OSError:
        return False


def stop(name: str, wait: bool = True) -> dict | None:
    s = state(name)
    if not s or not running(name):
        return s
    os.kill(s["supervisor_pid"], signal.SIGTERM)
    if wait:
        for _ in range(700):
            s2 = state(name)
            if s2 and not s2.get("running"):
                return s2
            time.sleep(0.05)
    return state(name)


# --------------------------------------------------------------------------
def write_hosts(n_ips: list[str]) -> None:
    (EMU / "hosts.json").write_text(json.dumps({"web": n_ips}))


def running_webs() -> list[int]:
    out = []
    for p in sorted((EMU / "state").glob("web-*.json")):
        i = int(p.stem.split("-")[1])
        if running(p.stem):
            out.append(i)
    return sorted(out)


def db_env() -> dict:
    return {"DB_HOST": "127.0.0.1", "DB_PORT": "5432", "DB_NAME": "trials",
            "DB_USER": "trials", "DB_PASSWORD": DB_PASSWORD}


def start_db() -> None:
    data = EMU / "pgdata"
    if sh("id", PG_USER, check=False).returncode != 0:
        sh("useradd", "-M", PG_USER)
    logdir = EMU / "pglog"
    logdir.mkdir(exist_ok=True)
    shutil.chown(logdir, PG_USER)
    if not data.exists():
        data.mkdir(parents=True)
        shutil.chown(data, PG_USER)
        pw = EMU / "pgpw"
        pw.write_text(DB_PASSWORD)
        shutil.chown(pw, PG_USER)
        sh("runuser", "-u", PG_USER, "--", PG_BIN / "initdb", "-D", data, "-U", "trials",
           "--pwfile", pw, "-A", "scram-sha-256", "-E", "UTF8")
    sh("runuser", "-u", PG_USER, "--", PG_BIN / "pg_ctl", "-D", data, "-l", logdir / "pg.log",
       "-o", "-c listen_addresses=127.0.0.1 -p 5432 -c max_connections=200", "-w", "start")
    r = sh(PG_BIN / "psql", "-h", "127.0.0.1", "-U", "trials", "-d", "postgres", "-tAc",
           "SELECT 1 FROM pg_database WHERE datname='trials'",
           env={**os.environ, "PGPASSWORD": DB_PASSWORD})
    if r.stdout.strip() != "1":
        sh(PG_BIN / "psql", "-h", "127.0.0.1", "-U", "trials", "-d", "postgres", "-c",
           "CREATE DATABASE trials", env={**os.environ, "PGPASSWORD": DB_PASSWORD})


def stop_db() -> None:
    sh("runuser", "-u", PG_USER, "--", PG_BIN / "pg_ctl", "-D", EMU / "pgdata", "-m", "fast",
       "stop", check=False)


def nginx_conf() -> Path:
    src = (REPO / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    conf = src.replace("listen 80;", "listen 127.0.0.1:8080;")
    conf = conf.replace("access_log /var/log/nginx/access.log",
                        f"access_log {EMU}/nginx-access.log")
    conf = conf.replace("error_log /dev/stderr warn;", f"error_log {EMU}/nginx-error.log warn;\n"
                        f"pid {EMU}/nginx.pid;")
    p = EMU / "nginx.conf"
    p.write_text(conf, encoding="utf-8")
    return p


def start_lb() -> None:
    sh(NGINX, "-c", nginx_conf(), "-p", EMU)
    wait_http("http://127.0.0.1:8080/noop", ok=(204,))


def stop_lb() -> None:
    sh(NGINX, "-c", EMU / "nginx.conf", "-p", EMU, "-s", "quit", check=False)
    for _ in range(100):
        if not (EMU / "nginx.pid").exists():
            return
        time.sleep(0.1)


def restart_lb() -> None:
    """Аналог docker compose restart lb: зупинка і новий запуск."""
    stop_lb()
    start_lb()


def start_prometheus() -> None:
    (EMU / "prom").mkdir(exist_ok=True)
    resolv = EMU / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.11\n")
    cmd = (f"mount --bind {resolv} /etc/resolv.conf && exec {PROMETHEUS} "
           f"--config.file={REPO / 'prometheus' / 'prometheus.yml'} "
           f"--storage.tsdb.path={EMU / 'prom'} --storage.tsdb.retention.time=15d "
           f"--web.listen-address=127.0.0.1:9090 --web.enable-lifecycle")
    # окремий простір імен монтування: лише Prometheus бачить DNS 127.0.0.11
    spawn("prometheus", ["unshare", "-m", "--propagation", "private", "sh", "-c", cmd])
    wait_http("http://127.0.0.1:9090/-/ready")


def start_web(i: int) -> None:
    env = {**db_env(), "HOST": web_ip(i), "PORT": "8000", "POD_NAME": f"web-{i}",
           "DB_POOL_MIN": "2", "DB_POOL_SIZE": "10", "LIST_DEFAULT_LIMIT": "20",
           "LIST_MAX_LIMIT": "100", "SHUTDOWN_TIMEOUT": "20", "PYTHONUNBUFFERED": "1"}
    spawn(f"web-{i}", [PY, "-m", "app.serve"], cpus=WEB_CPUS, mem=WEB_MEM, env=env)
    wait_http(f"http://{web_ip(i)}:8000/healthz", timeout=60)


def scale(n: int) -> None:
    cur = running_webs()
    for i in range(1, n + 1):
        if i not in cur:
            start_web(i)
    for i in cur:
        if i > n:
            stop(f"web-{i}")
    write_hosts([web_ip(i) for i in running_webs()])


def up(n: int) -> None:
    EMU.mkdir(parents=True, exist_ok=True)
    (EMU / "state").mkdir(exist_ok=True)
    write_hosts([])
    if not running("dns"):
        spawn("dns", [sys.executable, REPO / "scripts" / "emu_dns.py", "--hosts",
                      EMU / "hosts.json"])
    start_db()
    r = subprocess.run([PY, "-m", "app.migrate"], cwd=REPO, env={**os.environ, **db_env()},
                       capture_output=True, text=True)
    print("migrate exit code", r.returncode, (r.stdout + r.stderr).strip()[-300:])
    scale(n)
    start_lb()
    if not running("prometheus"):
        start_prometheus()


def down() -> None:
    for i in running_webs():
        stop(f"web-{i}")
    stop_lb()
    for n in ("prometheus", "dns"):
        stop(n)
    stop_db()


# --------------------------------------------------------------------------
# Бекенд для loadtest/lab2.py
# --------------------------------------------------------------------------
def _proc_cpu_mem(pids: list[int]) -> tuple[float, int]:
    import psutil
    cpu, rss = 0.0, 0
    for pid in pids:
        try:
            p = psutil.Process(pid)
            t = p.cpu_times()
            cpu += t.user + t.system + t.children_user + t.children_system
            rss += p.memory_info().rss
        except psutil.Error:
            pass
    return cpu, rss


def _groups() -> dict[str, list[int]]:
    """«Контейнери» стенда -> PID процесів."""
    import psutil
    g: dict[str, list[int]] = {"db": [], "lb": [], "prometheus": []}
    for p in psutil.process_iter(["pid", "name", "cmdline", "username"]):
        n = p.info["name"] or ""
        if n.startswith("postgres"):
            g["db"].append(p.pid)
        elif n.startswith("nginx"):
            g["lb"].append(p.pid)
        elif n == "prometheus":
            g["prometheus"].append(p.pid)
    for i in running_webs():
        s = state(f"web-{i}")
        g[f"web-{i}"] = [s["pid"]]
    return g


class EmuBackend:
    name = "emu"

    def __init__(self, args):
        self.args = args

    def host_info(self) -> dict:
        mem = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1))
        limits = []
        for i in running_webs():
            q = (Path("/sys/fs/cgroup/cpu") / f"emu-web-{i}" / "cpu.cfs_quota_us").read_text()
            m = (Path("/sys/fs/cgroup/memory") / f"emu-web-{i}" /
                 "memory.limit_in_bytes").read_text()
            limits.append(f"web-{i} cfs_quota_us={q.strip()}/100000 "
                          f"memory.limit_in_bytes={m.strip()}")
        cpu_model = next((ln.split(":", 1)[1].strip() for ln in
                          Path("/proc/cpuinfo").read_text().splitlines()
                          if ln.startswith("model name")), "")
        return {"backend": "emu", "ncpu": os.cpu_count(), "mem_bytes": mem * 1024,
                "cpu_model": cpu_model, "kernel": os.uname().release, "web_limits": limits}

    def scale(self, n: int) -> None:
        scale(n)
        restart_lb()

    def stats_snapshot(self) -> list[dict]:
        """Аналог docker stats --no-stream: CPU % за ≈1 с і RSS кожного «контейнера»."""
        g = _groups()
        t1 = time.time()
        a = {k: _proc_cpu_mem(v)[0] for k, v in g.items()}
        time.sleep(1.0)
        dt = time.time() - t1
        rows = []
        for k, v in g.items():
            c2, rss = _proc_cpu_mem(v)
            rows.append({"name": k, "cpu_pct": f"{100.0 * (c2 - a[k]) / dt:.2f}%",
                         "mem": f"{rss / 2**20:.1f}MiB"
                                + (f" / {WEB_MEM[:-1]}MiB" if k.startswith("web") else ""),
                         "mem_pct": ""})
        return rows

    def stop_one(self) -> dict:
        i = running_webs()[-1]
        name = f"web-{i}"
        t0 = time.time()
        # як docker stop: спершу адреса зникає з DNS, потім процес отримує SIGTERM
        write_hosts([web_ip(j) for j in running_webs() if j != i])
        s = stop(name)
        t1 = time.time()
        log = (EMU / "state" / f"{name}.log").read_text(errors="replace").splitlines()[-15:]
        return {"container": name, "t_stop_epoch": t0, "t_stopped_epoch": t1,
                "exit_code": s["exit_code"], "log_tail": log}

    def close(self) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["up", "down", "scale", "status"])
    ap.add_argument("n", nargs="?", type=int, default=1)
    a = ap.parse_args()
    if a.cmd == "up":
        up(a.n)
    elif a.cmd == "down":
        down()
    elif a.cmd == "scale":
        scale(a.n)
        restart_lb()
    print("web:", running_webs(), "hosts:", (EMU / "hosts.json").read_text()
          if (EMU / "hosts.json").exists() else "-")


if __name__ == "__main__":
    main()
