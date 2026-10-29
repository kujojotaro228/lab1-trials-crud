#!/usr/bin/env python3
"""Супервізор одного «контейнера» стенда-емуляції.

Запускає команду в контрольних групах cpu і memory (аналог лімітів
deploy.resources.limits), пересилає їй SIGTERM і після завершення записує код
виходу у файл так, як його показує docker inspect: 128 + номер сигналу, якщо
процес завершено сигналом (SIGTERM -> 143, SIGKILL -> 137).

    python scripts/emu_run.py --name web-1 --cpus 0.5 --mem 512M --state DIR -- cmd ...
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CG_CPU = Path("/sys/fs/cgroup/cpu")
CG_MEM = Path("/sys/fs/cgroup/memory")


def setup_cgroups(name: str, cpus: float | None, mem: int | None) -> list[Path]:
    groups = []
    if cpus:
        g = CG_CPU / f"emu-{name}"
        g.mkdir(exist_ok=True)
        (g / "cpu.cfs_period_us").write_text("100000")
        (g / "cpu.cfs_quota_us").write_text(str(int(cpus * 100000)))
        groups.append(g)
    if mem:
        g = CG_MEM / f"emu-{name}"
        g.mkdir(exist_ok=True)
        (g / "memory.limit_in_bytes").write_text(str(mem))
        groups.append(g)
    return groups


def parse_mem(s: str | None) -> int | None:
    if not s:
        return None
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    return int(float(s[:-1]) * mult[s[-1].upper()]) if s[-1].upper() in mult else int(s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--cpus", type=float, default=None)
    ap.add_argument("--mem", default=None)
    ap.add_argument("--state", required=True)
    ap.add_argument("--grace", type=float, default=30.0, help="stop_grace_period, с")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    state = Path(a.state)
    state.mkdir(parents=True, exist_ok=True)
    groups = setup_cgroups(a.name, a.cpus, parse_mem(a.mem))

    def enter_groups():
        for g in groups:
            (g / "cgroup.procs").write_text(str(os.getpid()))

    log = open(state / f"{a.name}.log", "ab", buffering=0)
    child = subprocess.Popen(cmd, stdout=log, stderr=log, preexec_fn=enter_groups)
    info = {"name": a.name, "pid": child.pid, "supervisor_pid": os.getpid(),
            "started_epoch": time.time(), "running": True, "exit_code": None}
    (state / f"{a.name}.json").write_text(json.dumps(info))

    stop = {"t": None}

    def on_term(signum, _frame):
        if stop["t"] is None:
            stop["t"] = time.time()
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    while True:
        try:
            rc = child.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if stop["t"] is not None and time.time() - stop["t"] > a.grace:
                child.kill()                    # як Docker після stop_grace_period
    code = 128 - rc if rc < 0 else rc
    info.update({"running": False, "exit_code": code, "finished_epoch": time.time(),
                 "stop_requested_epoch": stop["t"]})
    (state / f"{a.name}.json").write_text(json.dumps(info))
    sys.exit(0)


if __name__ == "__main__":
    main()
