"""Lab 6, variant 25: data, model and helpers shared by train.py, lab6.py and analyze.py.

Variant 25: dataset D1 (DermaMNIST, K = 7, C = 3), width W2 (c1/c2/h = 32/64/256),
P_max = 2, global batch B = 192, checkpoint interval K_c = 20, failure step s_f = 50.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = REPO / "data"
OUT = REPO / "results" / "lab6"
RAW = OUT / "raw"

DATASETS = {               # label: (file, classes K, channels C)
    "Д1": ("dermamnist", 7, 3),
    "Д2": ("bloodmnist", 8, 3),
    "Д3": ("organamnist", 11, 1),
    "Д4": ("organcmnist", 11, 1),
    "Д5": ("pathmnist", 9, 3),
    "Д6": ("octmnist", 4, 1),
}
WIDTHS = {"Ш1": (16, 32, 128), "Ш2": (32, 64, 256), "Ш3": (64, 128, 512)}

VARIANT = {"dataset": "Д1", "width": "Ш2", "P_max": 2, "B": 192, "K_c": 20, "s_f": 50}
N_TRAIN = 7000
LR0 = 0.02
MOMENTUM = 0.9
N_EPOCHS = 3
WARMUP_STEPS = 10
MEASURE_STEPS = 50
ZENODO = "https://zenodo.org/records/10519652/files"


def dataset_info():
    name, K, C = DATASETS[VARIANT["dataset"]]
    c1, c2, h = WIDTHS[VARIANT["width"]]
    return name, K, C, c1, c2, h


def data_path() -> Path:
    return DATA_DIR / f"{dataset_info()[0]}.npz"


def load_split(path, split, n=None):
    """Split ("train" or "test") of the .npz file as a TensorDataset (code of the assignment)."""
    import numpy as np
    import torch
    from torch.utils.data import TensorDataset
    d = np.load(path)
    x = d[f"{split}_images"][:n]                 # (N, 28, 28) or (N, 28, 28, 3), uint8
    y = d[f"{split}_labels"][:n].reshape(-1)     # (N, 1) -> (N,)
    if x.ndim == 3:
        x = x[:, None, :, :]                     # (N, 1, 28, 28)
    else:
        x = x.transpose(0, 3, 1, 2)              # (N, 3, 28, 28)
    x = torch.from_numpy(np.ascontiguousarray(x)).float()
    x = (x / 255 - 0.5) / 0.5
    return TensorDataset(x, torch.from_numpy(y.astype(np.int64)))


def make_net(C, c1, c2, h, K):
    from torch import nn

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(C, c1, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),    # 28 -> 14
                nn.Conv2d(c1, c2, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),   # 14 -> 7
            )
            self.head = nn.Sequential(
                nn.Flatten(), nn.Linear(7 * 7 * c2, h), nn.ReLU(), nn.Linear(h, K),
            )

        def forward(self, x):
            return self.head(self.features(x))

    return Net()


def n_params_formula(C, c1, c2, h, K):
    return 9 * C * c1 + c1 + 9 * c1 * c2 + c2 + 49 * c2 * h + h + h * K + K


def physical_cores() -> int | None:
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return n
    except ImportError:
        pass
    if platform.system() == "Windows":
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            return int(out)
        except Exception:  # noqa: BLE001
            return None
    try:
        cores = set()
        phys = core = None
        for ln in open("/proc/cpuinfo"):
            if ln.startswith("physical id"):
                phys = ln.split(":")[1].strip()
            elif ln.startswith("core id"):
                core = ln.split(":")[1].strip()
                cores.add((phys, core))
        return len(cores) or None
    except OSError:
        return None


def memory_gb() -> float | None:
    try:
        import psutil
        return psutil.virtual_memory().total / 1e9
    except ImportError:
        pass
    if platform.system() == "Windows":
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            return int(out) / 1e9
        except Exception:  # noqa: BLE001
            return None
    return None


def cpu_name() -> str:
    if platform.system() == "Windows":
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
    try:
        for ln in open("/proc/cpuinfo"):
            if ln.startswith("model name"):
                return ln.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def n_threads(c_phys: int | None) -> int:
    """n_th = floor(C_phys / P_max), at least 1."""
    if not c_phys:
        return 1
    return max(1, c_phys // VARIANT["P_max"])


def env_versions() -> dict:
    import torch
    return {"python": sys.version.split()[0], "torch": torch.__version__,
            "os": platform.platform(), "gloo": torch.distributed.is_gloo_available()}


def nth_from_env() -> int:
    return int(os.environ.get("LAB6_NTH", "1"))
