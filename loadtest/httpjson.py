from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RESOURCE = "/trial-participants"
REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "results"
RAW_DIR = RESULTS_DIR / "raw"


def request(base_url: str, method: str, path: str, body=None, timeout: float = 10.0):
    """Повертає (status, headers: dict, parsed_body | text | None)."""
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        if isinstance(body, (bytes, str)):
            data = body.encode("utf-8") if isinstance(body, str) else body
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, hdrs, raw = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, dict(e.headers or {}), e.read()
    text = raw.decode("utf-8", errors="replace") if raw else ""
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text
    return status, hdrs, parsed


def wait_healthy(base_url: str, timeout: float = 120.0, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, _ = request(base_url, "GET", "/healthz", timeout=2)
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(interval)
    return False


def fetch_ids(base_url: str) -> list[int]:
    """Збирає id усіх записів через перелік з пагінацією."""
    ids: list[int] = []
    offset = 0
    while True:
        status, _, body = request(base_url, "GET", f"{RESOURCE}?limit=100&offset={offset}")
        if status != 200:
            raise RuntimeError(f"GET {RESOURCE} returned {status}: {body}")
        items = body["items"]
        ids.extend(int(it["id"]) for it in items)
        if not items or len(ids) >= body["total"]:
            break
        offset += len(items)
    return ids


def quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")
