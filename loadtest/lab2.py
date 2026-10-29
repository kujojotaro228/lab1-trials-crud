#!/usr/bin/env python3
"""Оркестратор вимірювань ЛР 2 (кроки 3, 4, 6, 7, 11).

    py loadtest/lab2.py all                 # усе підряд: G, серія 1..4, зупинка, експорт
    py loadtest/lab2.py noop                # крок 6: межа генератора G
    py loadtest/lab2.py series --instances 1 2 3 4
    py loadtest/lab2.py stop-test           # крок 3: docker stop під навантаженням
    py loadtest/lab2.py export              # сирі ряди Prometheus -> results/lab2/prom/

Бекенд --backend docker (за замовчуванням) керує стендом через docker compose і
docker stats; --backend emu — стендом-емуляцією з scripts/emu_stand.py (ті самі
процеси без Docker, див. README). Генератор — loadtest/stepgen.py.

Результати: results/lab2/raw/<run>.json, <run>_seconds.csv, <run>_dstats.json,
results/lab2/prom/<run>.json.gz, results/lab2/host.json, results/lab2/stop_test.json.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepgen  # noqa: E402
from httpjson import REPO_ROOT, fetch_ids  # noqa: E402

LAB2 = REPO_ROOT / "results" / "lab2"
RAW = LAB2 / "raw"
PROM = LAB2 / "prom"


# --------------------------------------------------------------------------
# Prometheus
# --------------------------------------------------------------------------
def prom_get(prom_url: str, path: str, **params) -> dict:
    url = prom_url.rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=60) as r:
        body = json.loads(r.read().decode("utf-8"))
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus: {body}")
    return body["data"]


def prom_targets_up(prom_url: str, job: str = "web") -> tuple[int, list]:
    data = prom_get(prom_url, "/api/v1/targets", state="active")
    ts = [t for t in data["activeTargets"] if t["labels"].get("job") == job]
    return sum(1 for t in ts if t["health"] == "up"), ts


def wait_targets(prom_url: str, n: int, timeout: float = 120) -> list:
    t_end = time.time() + timeout
    while True:
        up, ts = prom_targets_up(prom_url)
        if up == n and len(ts) == n:
            return ts
        if time.time() > t_end:
            raise RuntimeError(f"Prometheus: очікувалось {n} цілей web у стані UP, є {up}/{len(ts)}")
        time.sleep(2)


EXPORT_METRICS = [
    "http_requests_total", "http_request_duration_seconds_bucket",
    "http_request_duration_seconds_count", "http_requests_in_progress",
    "process_cpu_seconds_total", "cgroup_cpu_nr_periods_total",
    "cgroup_cpu_nr_throttled_total", "cgroup_cpu_throttled_seconds_total", "app_pod_info",
    "process_resident_memory_bytes",
]


def export_prom(prom_url: str, run: dict) -> Path:
    """Сирі зразки (кожен збір, без агрегації) усіх потрібних метрик за час прогону."""
    t_a = run["t0_epoch"] - 20
    t_b = run["t_end_epoch"] + 20
    dur = int(t_b - t_a) + 1
    out = {"run": run["name"], "t_from": t_a, "t_to": t_b, "series": {}}
    for m in EXPORT_METRICS:
        data = prom_get(prom_url, "/api/v1/query", query=f'{m}{{job="web"}}[{dur}s]',
                        time=f"{t_b:.3f}")
        out["series"][m] = [{"labels": r["metric"],
                             "values": [[float(t), float(v)] for t, v in r["values"]]}
                            for r in data["result"]]
    PROM.mkdir(parents=True, exist_ok=True)
    path = PROM / f"{run['name']}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(out, f)
    return path


# --------------------------------------------------------------------------
# Бекенди
# --------------------------------------------------------------------------
class DockerBackend:
    name = "docker"

    def __init__(self, args):
        self.cwd = REPO_ROOT
        self.base_url = args.base_url
        self.prom_url = args.prom_url

    def _run(self, *cmd, check=True) -> str:
        r = subprocess.run(cmd, cwd=self.cwd, capture_output=True, text=True, encoding="utf-8")
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.strip()}")
        return r.stdout

    def host_info(self) -> dict:
        info = json.loads(self._run("docker", "info", "--format", "{{json .}}"))
        return {"backend": "docker", "ncpu": info.get("NCPU"),
                "mem_bytes": info.get("MemTotal"), "os": info.get("OperatingSystem"),
                "kernel": info.get("KernelVersion"), "server": info.get("ServerVersion"),
                "web_limits": self.web_limits()}

    def web_ids(self) -> list[str]:
        return sorted(self._run("docker", "compose", "ps", "-q", "web").split())

    def web_limits(self) -> list:
        out = []
        for cid in self.web_ids():
            out.append(self._run("docker", "inspect", "--format",
                                 "{{.Name}} NanoCpus={{.HostConfig.NanoCpus}} "
                                 "Memory={{.HostConfig.Memory}}", cid).strip())
        return out

    def scale(self, n: int) -> None:
        self._run("docker", "compose", "up", "-d", "--no-recreate", "--scale", f"web={n}",
                  "--wait", "web")
        # балансувальник перезапускається після кожної зміни кількості екземплярів
        self._run("docker", "compose", "restart", "lb")
        self._wait_lb()

    def _wait_lb(self, timeout: float = 60) -> None:
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                with urllib.request.urlopen(self.base_url + "/healthz", timeout=3) as r:
                    if r.status == 200:
                        return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        raise RuntimeError("балансувальник не відповідає")

    def stats_snapshot(self) -> list[dict]:
        out = self._run("docker", "stats", "--no-stream", "--format", "{{json .}}")
        rows = []
        for line in out.splitlines():
            if line.strip():
                d = json.loads(line)
                rows.append({"name": d.get("Name"), "cpu_pct": d.get("CPUPerc"),
                             "mem": d.get("MemUsage"), "mem_pct": d.get("MemPerc")})
        return rows

    def stop_one(self) -> dict:
        cid = self.web_ids()[-1]
        name = self._run("docker", "inspect", "--format", "{{.Name}}", cid).strip().lstrip("/")
        t0 = time.time()
        self._run("docker", "stop", cid)
        t1 = time.time()
        code = int(self._run("docker", "inspect", "--format", "{{.State.ExitCode}}", cid).strip())
        r = subprocess.run(["docker", "logs", "--tail", "15", cid], cwd=self.cwd,
                           capture_output=True, text=True, encoding="utf-8")
        logs = r.stdout + r.stderr                     # uvicorn пише журнал у stderr
        return {"container": name, "t_stop_epoch": t0, "t_stopped_epoch": t1,
                "exit_code": code, "log_tail": logs.splitlines()[-15:]}

    def close(self) -> None:
        pass


def make_backend(args):
    if args.backend == "docker":
        return DockerBackend(args)
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import emu_stand  # noqa: E402
    return emu_stand.EmuBackend(args)


class StatsSampler(threading.Thread):
    """Знімки docker stats --no-stream протягом прогону (крок 11)."""

    def __init__(self, backend, period: float = 5.0):
        super().__init__(daemon=True)
        self.backend, self.period = backend, period
        self.samples: list[dict] = []
        self.stop_ev = threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            t = time.time()
            try:
                rows = self.backend.stats_snapshot()
                self.samples.append({"t_epoch": (t + time.time()) / 2, "rows": rows})
            except Exception as exc:  # noqa: BLE001
                self.samples.append({"t_epoch": t, "error": str(exc)})
            self.stop_ev.wait(max(0.0, self.period - (time.time() - t)))


# --------------------------------------------------------------------------
# Команди
# --------------------------------------------------------------------------
def gen_args(args, **over) -> argparse.Namespace:
    ns = stepgen.build_argparser().parse_args(["--name", "x"])
    ns.base_url = args.base_url
    ns.procs = args.procs
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_noop(args, be) -> float:
    print("== крок 6: межа генератора G (/noop, без обмеження G)")
    res = stepgen.run(stepgen.make_cfg(gen_args(args, target="noop", name="step_noop")), [])
    stepgen.save(res)
    print(f"G = {res['X_rps']} rps (сходинка {res['X_target_rps']} rps)")
    return res["X_rps"]


def load_G() -> float:
    p = RAW / "step_noop.json"
    if not p.exists():
        raise SystemExit("спершу виконайте: lab2.py noop")
    return json.loads(p.read_text(encoding="utf-8"))["X_rps"]


def cmd_series(args, be) -> None:
    G = args.gen_limit or load_G()
    for n in args.instances:
        print(f"== крок 7: {n} екземпляр(и), G = {G}")
        be.scale(n)
        targets = wait_targets(args.prom_url, n)
        print("   Prometheus Targets web:", ", ".join(
            f"{t['labels']['instance']}={t['health']}" for t in targets))
        ids = fetch_ids(args.base_url)
        sampler = StatsSampler(be)
        sampler.start()
        name = f"step_n{n}{args.suffix}"
        res = stepgen.run(stepgen.make_cfg(gen_args(args, name=name, gen_limit=G)), ids)
        sampler.stop_ev.set()
        sampler.join()
        res["extra"] = {"instances": n, "G": G, "backend": be.name,
                        "targets": [t["labels"]["instance"] for t in targets]}
        stepgen.save(res)
        save_json(RAW / f"{name}_dstats.json", sampler.samples)
        path = export_prom(args.prom_url, res)
        print(f"   X({n}) = {res['X_rps']} ± {res['X_sd']} rps, сходинка {res['X_target_rps']}, "
              f"зупинка: {res['stop_reason']}; Prometheus -> {path.name}")
        time.sleep(args.pause)


def cmd_stop_test(args, be) -> None:
    print("== крок 3: коректна зупинка одного з двох екземплярів під навантаженням")
    be.scale(2)
    wait_targets(args.prom_url, 2)
    ids = fetch_ids(args.base_url)
    rate = args.stop_rate
    if not rate:
        p = RAW / f"step_n1{args.suffix}.json"
        x1 = json.loads(p.read_text(encoding="utf-8"))["X_rps"] if p.exists() else 400
        rate = max(100, round(0.6 * x1 / 50) * 50)   # нижче за межу ОДНОГО екземпляра
    dur = 40.0
    # 5 с розгону на 200 rps: без нього генератор на старті одночасно відкриває сотні
    # зʼєднань, і перші секунди дають помилки зʼєднання, не повʼязані з зупинкою
    cfg = stepgen.make_cfg(gen_args(args, name="stop_test", warmup=5.0, warmup_rate=200.0,
                                    start=rate, step=0.0, step_dur=dur - 5.0, max_steps=1))
    result = {}

    def stopper():
        time.sleep(cfg_t0_wait())
        result.update(be.stop_one())

    # t0 генератора відомий лише всередині run(); зупинка через 15 с після старту
    started = time.time()

    def cfg_t0_wait():
        return 15.0 + 1.0 + 0.2 * args.procs - (time.time() - started)

    th = threading.Thread(target=stopper, daemon=True)
    th.start()
    res = stepgen.run(cfg, ids)
    th.join()
    t_rel = result["t_stop_epoch"] - res["t0_epoch"]
    win = [s for s in res["seconds"] if t_rel - 5 <= s["t_s"] < t_rel + 5]
    out = {
        "rate_rps": rate, "duration_s": dur, **result, "t_stop_rel_s": round(t_rel, 2),
        "t_stopped_rel_s": round(result["t_stopped_epoch"] - res["t0_epoch"], 2),
        "window_s": [int(t_rel - 5), int(t_rel + 5)],
        "window_ok": sum(s["ok"] for s in win), "window_non2xx": sum(s["non2xx"] for s in win),
        "window_connerr": sum(s["connerr"] for s in win),
        "window_p99_max_ms": max((s["p99_ms"] or 0) for s in win) if win else None,
        "total_non2xx": sum(s["non2xx"] for s in res["seconds"]),
        "total_connerr": sum(s["connerr"] for s in res["seconds"]),
        "seconds": res["seconds"], "pods": res["pods"],
    }
    save_json(LAB2 / "stop_test.json", out)
    print(f"   exit code {out['exit_code']}; у вікні ±5 с навколо зупинки: 2xx={out['window_ok']}, "
          f"не 2xx={out['window_non2xx']}, помилок зʼєднання={out['window_connerr']}")
    be.scale(2)


def cmd_export(args, be) -> None:
    for p in sorted(RAW.glob("step_n*.json")):
        if p.name.endswith("_dstats.json"):
            continue
        run = json.loads(p.read_text(encoding="utf-8"))
        print("export", export_prom(args.prom_url, run))


class _Tee:
    """Дублює вивід у results/lab2/logs/lab2.log у UTF-8 (Tee-Object у PowerShell
    записує UTF-16 і псує кирилицю)."""

    def __init__(self, stream, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream, self.f = stream, open(path, "a", encoding="utf-8")

    def write(self, s):
        try:
            self.stream.write(s)
        except UnicodeEncodeError:
            self.stream.write(s.encode("ascii", "replace").decode())
        self.f.write(s)
        self.f.flush()
        return len(s)

    def flush(self):
        self.stream.flush()
        self.f.flush()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.stdout = _Tee(sys.stdout, LAB2 / "logs" / "lab2.log")
    print(f"\n#### {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(sys.argv)}")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["all", "host", "noop", "series", "stop-test", "export"])
    p.add_argument("--backend", choices=["docker", "emu"], default="docker")
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--prom-url", default="http://localhost:9090")
    p.add_argument("--instances", type=int, nargs="+", default=[1, 2, 3, 4])
    p.add_argument("--gen-limit", type=float, default=None)
    p.add_argument("--procs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    p.add_argument("--stop-rate", type=float, default=None)
    p.add_argument("--suffix", default="", help="суфікс імен прогонів (повторна серія)")
    p.add_argument("--pause", type=float, default=10.0)
    args = p.parse_args()
    be = make_backend(args)
    try:
        if args.command in ("all", "host"):
            info = be.host_info()
            save_json(LAB2 / "host.json", info)
            print("host:", info)
        if args.command in ("all", "noop"):
            cmd_noop(args, be)
        if args.command in ("all", "series"):
            cmd_series(args, be)
        if args.command in ("all", "stop-test"):
            cmd_stop_test(args, be)
        if args.command == "export":
            cmd_export(args, be)
    finally:
        be.close()


if __name__ == "__main__":
    main()
