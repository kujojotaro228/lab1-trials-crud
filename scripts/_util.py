
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "results"

sys.path.insert(0, str(REPO_ROOT / "loadtest"))
from httpjson import RESOURCE, request, wait_healthy  # noqa: E402,F401


def sh(args: list[str], check: bool = False, timeout: float | None = 600) -> subprocess.CompletedProcess:
    """Запускає команду в корені репозиторію, повертає результат з stdout/stderr."""
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check, timeout=timeout)


def compose(*args: str, check: bool = False, timeout: float | None = 600) -> subprocess.CompletedProcess:
    return sh(["docker", "compose", *args], check=check, timeout=timeout)


def read_env() -> dict[str, str]:
    env = {}
    p = REPO_ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def image_name() -> str:
    """Імʼя образу вебсервісу, яке дав Compose (trials-web або trials_web)."""
    for name in ("trials-web", "trials_web"):
        if sh(["docker", "image", "inspect", name]).returncode == 0:
            return name
    return "trials-web"


def md_block(title: str, cmd: str, output: str) -> str:
    return f"### {title}\n\n```\n$ {cmd}\n{output.rstrip()}\n```\n"
