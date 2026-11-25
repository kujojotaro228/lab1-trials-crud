#!/usr/bin/env python3
"""Lab 6 orchestrator (variant 25: DermaMNIST, width W2, P_max = 2, B = 192, K_c = 20, s_f = 50).

Run from the repository root:

    py lab6/lab6.py prepare     # check packages, download data/dermamnist.npz, host and model info
    py lab6/lab6.py check       # torchrun + gloo smoke test on 2 processes (~10 s)
    py lab6/lab6.py step1       # 1 process without torch.distributed: training, t_step, X(1)
    py lab6/lab6.py step2       # P_max processes: gradient check, manual sync, DDP, DDP no_sync, DDP training
    py lab6/lab6.py step3       # strong scaling (P = 1 with DDP; P = 2 from step 2)
    py lab6/lab6.py step4       # weak scaling and quality for different global batches
    py lab6/lab6.py step5       # checkpoints, failure of rank P_max - 1 at step s_f, restart
    py lab6/lab6.py all         # step1 .. step5
    py lab6/analyze.py          # tables 1-6 and figures

Every torchrun run uses a static rendezvous on 127.0.0.1 (as the assignment requires for the
restart run), backend gloo. Outputs: results/lab6/raw/<run>.json, console output of every run
in results/lab6/logs/<run>.out, the log of all commands in results/lab6/logs/lab6.log.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

TRAIN = Path(__file__).resolve().parent / "train.py"
TORCHRUN = Path(__file__).resolve().parent / "torchrun_win.py"
LOGS = common.OUT / "logs"
CKPT = common.OUT / "ckpt"
_port = [random.randint(29500, 29900)]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "lab6.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def host() -> dict:
    p = common.OUT / "host.json"
    if not p.exists():
        raise SystemExit("run first: py lab6/lab6.py prepare")
    return json.loads(p.read_text(encoding="utf-8"))


def child_env() -> dict:
    nth = host()["n_th"]
    env = dict(os.environ)
    env.update({"LAB6_NTH": str(nth), "OMP_NUM_THREADS": str(nth), "MKL_NUM_THREADS": str(nth),
                "USE_LIBUV": "0", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def _run(name: str, cmd: list[str], expect_ok: bool = True) -> dict | None:
    common.RAW.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    out = common.RAW / f"{name}.json"
    if out.exists():
        out.unlink()
    cmd = cmd + ["--out", str(out)]
    log(f"start {name}: {' '.join(str(c) for c in cmd[1:])}")
    t0 = time.time()
    with open(LOGS / f"{name}.out", "w", encoding="utf-8") as f:
        # own process group: console control events inside the torchrun group do not reach this script
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=child_env(), cwd=str(common.REPO),
                           creationflags=flags)
    dt = time.time() - t0
    if p.returncode != 0 or not out.exists():
        tail = (LOGS / f"{name}.out").read_text(encoding="utf-8", errors="replace")[-3000:]
        log(f"FAILED {name} (exit code {p.returncode}, {dt:.1f} s). Last output:\n{tail}")
        if expect_ok:
            raise SystemExit(1)
        return None
    r = json.loads(out.read_text(encoding="utf-8"))
    short = {k: v for k, v in r.items() if k not in ("losses", "step_times_ms", "per_tensor", "weight_sums_step50")}
    log(f"done  {name} ({dt:.1f} s): {json.dumps(short, ensure_ascii=False)}")
    return r


def plain(name: str, args: list[str]) -> dict:
    return _run(name, [sys.executable, str(TRAIN), *args])


def torchrun(name: str, P: int, args: list[str], restarts: int = 0) -> dict:
    _port[0] += 1
    # lab6/torchrun_win.py = torchrun (torch.distributed.run) with a libuv-free agent store, see its docstring
    cmd = [sys.executable, str(TORCHRUN), "--nnodes=1", f"--nproc_per_node={P}",
           f"--max-restarts={restarts}", "--rdzv-backend=static", "--master-addr=127.0.0.1",
           f"--master-port={_port[0]}", "--node-rank=0", str(TRAIN), *args]
    return _run(name, cmd)


# ---------------------------------------------------------------------------- prepare / check
def cmd_prepare() -> None:
    missing = []
    for mod in ("torch", "numpy", "sklearn"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        raise SystemExit(f"missing packages {missing}: py -m pip install torch numpy scikit-learn psutil "
                         "--index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple")
    path = common.data_path()
    if not path.exists():
        common.DATA_DIR.mkdir(parents=True, exist_ok=True)
        url = f"{common.ZENODO}/{path.name}?download=1"
        log(f"downloading {url}")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, path)
    import numpy as np
    d = np.load(path)
    shapes = {k: list(d[k].shape) for k in d.files}
    log(f"data {path.name}: {shapes}")
    import torch
    c_phys = common.physical_cores()
    nth = common.n_threads(c_phys)
    name, K, C, c1, c2, h = common.dataset_info()
    n_params = common.n_params_formula(C, c1, c2, h, K)
    torch.manual_seed(0)
    model = common.make_net(C, c1, c2, h, K)
    counted = sum(p.numel() for p in model.parameters())
    info = {"cpu": common.cpu_name(), "C_phys": c_phys, "logical": os.cpu_count(),
            "memory_gb": common.memory_gb(), **common.env_versions(), "n_th": nth,
            "dataset": f"{common.VARIANT['dataset']} {name}", "K": K, "C": C, "c1": c1, "c2": c2, "h": h,
            "n_params": n_params, "n_params_counted": counted, "n_tensors": len(list(model.parameters())),
            "M_bytes": 4 * n_params, "M_MiB": 4 * n_params / 2 ** 20, "N": common.N_TRAIN,
            "B": common.VARIANT["B"], "P_max": common.VARIANT["P_max"], "K_c": common.VARIANT["K_c"],
            "s_f": common.VARIANT["s_f"], "data_shapes": shapes,
            "n_test": shapes.get("test_labels", [None])[0]}
    common.OUT.mkdir(parents=True, exist_ok=True)
    (common.OUT / "host.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"host and model: {json.dumps(info, ensure_ascii=False)}")
    if counted != n_params:
        raise SystemExit("n_params formula does not match the model")


def cmd_check() -> None:
    r = torchrun("check_smoke", common.VARIANT["P_max"], ["--mode", "smoke", "--sync", "ddp"])
    ok = r["all_reduce"] == 3.0 and len(set(r["weight_sums"])) == 1
    log(f"smoke test {'OK' if ok else 'FAILED'}: all_reduce = {r['all_reduce']} (expected 3), "
        f"weight sums after 2 DDP steps {r['weight_sums']}")
    if not ok:
        raise SystemExit(1)


# ---------------------------------------------------------------------------- steps
B = common.VARIANT["B"]
PM = common.VARIANT["P_max"]


def cmd_step1() -> None:
    plain("s1_single_train", ["--mode", "train", "--sync", "none", "--B", str(B)])
    plain("s1_single_speed", ["--mode", "speed", "--sync", "none", "--B", str(B)])


def cmd_step2() -> None:
    torchrun("s2_gradcheck", PM, ["--mode", "gradcheck", "--sync", "manual", "--B", str(B)])
    torchrun("s2_manual_speed", PM, ["--mode", "speed", "--sync", "manual", "--B", str(B)])
    torchrun("s2_ddp_speed", PM, ["--mode", "speed", "--sync", "ddp", "--B", str(B)])
    torchrun("s2_ddp_nosync_speed", PM, ["--mode", "speed", "--sync", "ddp", "--nosync", "--B", str(B)])
    torchrun("s2_ddp_train", PM, ["--mode", "train", "--sync", "ddp", "--B", str(B)])


def cmd_step3() -> None:
    # P = P_max rows are the DDP runs of step 2 (same B, same n_th)
    for P in range(1, PM):
        torchrun(f"s3_ddp_speed_p{P}", P, ["--mode", "speed", "--sync", "ddp", "--B", str(B)])
        torchrun(f"s3_ddp_nosync_speed_p{P}", P, ["--mode", "speed", "--sync", "ddp", "--nosync", "--B", str(B)])


def cmd_step4() -> None:
    b = B // PM
    for P in range(1, PM):
        torchrun(f"s4_ddp_speed_p{P}", P, ["--mode", "speed", "--sync", "ddp", "--B", str(P * b)])
    torchrun("s4_train_p1_b", 1, ["--mode", "train", "--sync", "ddp", "--B", str(b), "--lr", str(common.LR0)])
    lr_p = common.LR0 * PM                         # linear rule: eta_P = eta_0 * B_P / B_0, B_0 = b
    torchrun("s4_train_pmax_lrP", PM, ["--mode", "train", "--sync", "ddp", "--B", str(B), "--lr", str(lr_p)])


def cmd_step5() -> None:
    if CKPT.exists():
        shutil.rmtree(CKPT)
    CKPT.mkdir(parents=True)
    events = common.RAW / "s5_events"
    for f in common.RAW.glob("s5_events*.jsonl"):
        f.unlink()
    marker = CKPT / "failed.marker"
    log(f"step 5: K_c = {common.VARIANT['K_c']}, s_f = {common.VARIANT['s_f']}, rank {PM - 1} calls "
        f"os._exit(1) at the start of step s_f; torchrun --max-restarts=1")
    torchrun("s5_fail_train", PM, ["--mode", "train", "--sync", "ddp", "--B", str(B),
                                  "--ckpt-every", str(common.VARIANT["K_c"]), "--ckpt-dir", str(CKPT),
                                  "--fail-step", str(common.VARIANT["s_f"]), "--fail-marker", str(marker),
                                  "--events", str(events)], restarts=1)
    ev = []
    for f in sorted(common.RAW.glob("s5_events*.jsonl")):
        ev += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    ev.sort(key=lambda e: e["t"])
    fail = next((e for e in ev if e["event"] == "fail"), None)
    if fail is None:
        log("WARNING: no failure event — the failure was not simulated")
        return
    ckpts = [e for e in ev if e["event"] == "ckpt" and e["t"] < fail["t"]]
    last = ckpts[-1] if ckpts else None
    restored = next((e for e in ev if e["event"] == "restored"), None)
    first = next((e for e in ev if e["event"] == "first_step_after_restore"), None)
    starts = [e for e in ev if e["event"] == "start"]
    summary = {"K_c": common.VARIANT["K_c"], "s_f": common.VARIANT["s_f"], "fail_step": fail["step"],
               "t_f": fail["t"], "last_ckpt_step": last["step"] if last else None, "t_c": last["t"] if last else None,
               "restored_step": restored["step"] if restored else None,
               "steps_redone": (fail["step"] - 1 - last["step"]) if last else None,
               "t_first_step_after_restore": first["t"] if first else None,
               "restart_start_t": starts[-1]["t"] if len(starts) > 1 else None}
    if last and first:
        summary["t_f_minus_t_c"] = fail["t"] - last["t"]
        summary["t_r"] = first["t"] - fail["t"]
        summary["t_lost"] = summary["t_f_minus_t_c"] + summary["t_r"]
    (common.RAW / "s5_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    log(f"step 5 summary: {json.dumps(summary)}")


STEPS = {"prepare": cmd_prepare, "check": cmd_check, "step1": cmd_step1, "step2": cmd_step2,
         "step3": cmd_step3, "step4": cmd_step4, "step5": cmd_step5}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if len(sys.argv) != 2 or sys.argv[1] not in [*STEPS, "all"]:
        print(__doc__)
        raise SystemExit(2)
    c = sys.argv[1]
    order = ["step1", "step2", "step3", "step4", "step5"] if c == "all" else [c]
    for s in order:
        log(f"==== {s}")
        STEPS[s]()
    log("==== finished")


if __name__ == "__main__":
    main()
