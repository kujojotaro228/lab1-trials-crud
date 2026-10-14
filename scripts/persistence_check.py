#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
import uuid

from _util import RESOURCE, RESULTS_DIR, compose, md_block, request, sh, wait_healthy


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    args = ap.parse_args()
    base = args.base_url
    out = ["# Перевірка збереження даних", ""]
    ok_all = True

    def run(title, *cargs):
        r = compose(*cargs)
        out.append(md_block(title, "docker compose " + " ".join(cargs), r.stdout + r.stderr))
        print(f"$ docker compose {' '.join(cargs)} -> rc={r.returncode}")
        return r

    def check(title, cond):
        nonlocal ok_all
        ok_all &= bool(cond)
        out.append(f"- {'✅' if cond else '❌'} {title}\n")
        print(("OK   " if cond else "FAIL ") + title)

    run("Стенд піднято", "up", "-d")
    if not wait_healthy(base, 180):
        sys.exit("стенд не піднявся")

    rec = {"participant_code": "TP-PERSIST-" + uuid.uuid4().hex[:6].upper(),
           "protocol": "CT-2025-001", "randomization_group": "B",
           "enrollment_date": "2025-05-05", "status": "active", "completion_date": None}
    st, _, body = request(base, "POST", RESOURCE, rec)
    out.append(f"**POST {RESOURCE}** → `{st}`, id = {body.get('id') if isinstance(body, dict) else body}\n")
    check("запис створено (201)", st == 201)
    pid = body["id"]
    _, _, lst = request(base, "GET", f"{RESOURCE}?limit=1")
    total_before = lst["total"]
    out.append(f"Записів у базі до down: **{total_before}**\n")

    # --- down / up: том лишається
    run("Зупинка без видалення томів", "down")
    vols = sh(["docker", "volume", "ls", "--filter", "name=trials"]).stdout
    out.append(md_block("Том із даними на місці", "docker volume ls --filter name=trials", vols))
    run("Повторне підняття", "up", "-d")
    check("після down/up стенд відповідає 200 на /healthz", wait_healthy(base, 180))
    st, _, got = request(base, "GET", f"{RESOURCE}/{pid}")
    out.append(f"**GET {RESOURCE}/{pid}** після down/up → `{st}`: "
               f"`{got.get('participant_code') if isinstance(got, dict) else got}`\n")
    check("запис на місці після down/up", st == 200 and got.get("participant_code") == rec["participant_code"])
    _, _, lst = request(base, "GET", f"{RESOURCE}?limit=1")
    out.append(f"Записів у базі після down/up: **{lst['total']}**\n")
    check("кількість записів не змінилась", lst["total"] == total_before)

    # --- down -v / up: том видалено, база порожня
    run("Зупинка з видаленням томів", "down", "-v")
    vols = sh(["docker", "volume", "ls", "--filter", "name=trials"]).stdout
    out.append(md_block("Тому більше немає", "docker volume ls --filter name=trials", vols))
    r = run("Підняття на порожньому томі", "up", "-d")
    check("docker compose up -d завершився без помилок", r.returncode == 0)
    check("стенд відповідає 200 на /healthz", wait_healthy(base, 180))
    migr = compose("ps", "-a", "migrate")
    out.append(md_block("Міграція знову відпрацювала на порожній базі",
                        "docker compose ps -a migrate", migr.stdout))
    _, _, lst = request(base, "GET", f"{RESOURCE}?limit=1")
    out.append(f"Записів у базі після down -v / up: **{lst['total']}**\n")
    check("база порожня після down -v", lst["total"] == 0)

    out.append(f"\n**Підсумок:** {'усі перевірки пройдено' if ok_all else 'є помилки'}\n")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "persistence_log.md").write_text("\n".join(out), encoding="utf-8")
    print("saved results/persistence_log.md")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
