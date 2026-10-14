#!/usr/bin/env python3

from __future__ import annotations

import argparse
import random
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpjson import RESOURCE, quote, request, wait_healthy  # noqa: E402

GROUPS = ["A", "B", "контроль"]
PROTOCOLS = ["CT-2025-001", "CT-2025-002", "ONC-17", "CARD-3B"]


def make_participant(rnd: random.Random) -> dict:
    enrolled = date(2024, 1, 1) + timedelta(days=rnd.randint(0, 600))
    status = rnd.choice(["screening", "active", "active", "completed", "withdrawn"])
    completion = None
    if status in ("completed", "withdrawn"):
        completion = (enrolled + timedelta(days=rnd.randint(1, 365))).isoformat()
    return {
        "participant_code": "TP-" + uuid.uuid4().hex[:10].upper(),
        "protocol": rnd.choice(PROTOCOLS),
        "randomization_group": rnd.choice(GROUPS),
        "enrollment_date": enrolled.isoformat(),
        "status": status,
        "completion_date": completion,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--seed", type=int, default=25)
    args = ap.parse_args()

    if not wait_healthy(args.base_url, timeout=60):
        sys.exit("сервіс не відповідає 200 на /healthz")

    status, _, body = request(args.base_url, "GET", f"{RESOURCE}?limit=1")
    if status != 200:
        sys.exit(f"GET {RESOURCE} -> {status}: {body}")
    total = body["total"]
    need = args.count - total
    if need < 0:
        print(f"УВАГА: у базі вже {total} записів (> {args.count}). "
              "Для чистого старту виконайте: docker compose down -v; docker compose up -d")
        return

    rnd = random.Random(args.seed)
    created = 0
    while created < need:
        st, _, b = request(args.base_url, "POST", RESOURCE, make_participant(rnd))
        if st == 201:
            created += 1
        elif st == 409:
            continue  # збіг коду — пробуємо інший
        else:
            sys.exit(f"POST -> {st}: {b}")

    _, _, body = request(args.base_url, "GET", f"{RESOURCE}?limit=1")
    print(f"створено {created}, усього записів у базі: {body['total']}")
    for g in GROUPS:
        _, _, b = request(args.base_url, "GET",
                          f"{RESOURCE}?limit=1&randomization_group={quote(g)}")
        print(f"  група {g!r}: {b['total']}")


if __name__ == "__main__":
    main()
