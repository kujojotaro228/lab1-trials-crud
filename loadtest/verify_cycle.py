#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from httpjson import RESOURCE, RESULTS_DIR, quote, request  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--out", default=str(RESULTS_DIR / "verify_log.md"))
    args = ap.parse_args()
    base = args.base_url

    log: list[str] = ["# Журнал перевірки (етап 9)", "", f"Базова адреса: `{base}`", ""]
    failures = 0
    step_no = 0

    def step(title, method, path, body=None, expect=200):
        nonlocal failures, step_no
        step_no += 1
        status, headers, resp = request(base, method, path, body)
        ok = status == expect
        failures += 0 if ok else 1
        log.append(f"### {step_no}. {title}")
        log.append("")
        log.append(f"**Запит:** `{method} {path}`")
        if body is not None:
            log.append("")
            log.append("```json\n" + json.dumps(body, ensure_ascii=False, indent=2) + "\n```")
        log.append("")
        extra = ""
        if headers.get("Location"):
            extra = f", Location: `{headers['Location']}`"
        log.append(f"**Відповідь:** `{status}` (очікувалось {expect}) "
                   f"{'+' if ok else '-'}{extra}")
        if resp not in (None, ""):
            log.append("")
            log.append("```json\n" + json.dumps(resp, ensure_ascii=False, indent=2) + "\n```")
        log.append("")
        print(f"{step_no:>2}. {method:6} {path:55} -> {status} "
              f"(expect {expect}) {'OK' if ok else 'FAIL'}")
        return status, resp

    code = "TP-VERIFY-" + uuid.uuid4().hex[:6].upper()
    new = {
        "participant_code": code,
        "protocol": "CT-2025-001",
        "randomization_group": "A",
        "enrollment_date": "2025-03-01",
        "status": "active",
        "completion_date": None,
    }

    step("Готовність сервісу", "GET", "/healthz", expect=200)
    _, created = step("Створення запису", "POST", RESOURCE, new, expect=201)
    if not isinstance(created, dict) or "id" not in created:
        print("створення не вдалося — зупиняюсь")
        sys.exit(1)
    pid = created["id"]

    bad = dict(new, participant_code=code + "-X", randomization_group="C")
    step("Створення з невалідною групою рандомізації (правило варіанта)", "POST", RESOURCE,
         bad, expect=400)
    step("Створення з датою завершення раніше дати включення", "POST", RESOURCE,
         dict(new, participant_code=code + "-Y", status="completed",
              completion_date="2025-01-01"), expect=400)
    step("Створення без обовʼязкового поля protocol", "POST", RESOURCE,
         {k: v for k, v in new.items() if k != "protocol"} | {"participant_code": code + "-Z"},
         expect=400)
    step("Створення з уже наявним кодом учасника", "POST", RESOURCE, new, expect=409)

    _, lst = step("Перелік з фільтром за групою A", "GET",
                  f"{RESOURCE}?randomization_group=A&limit=100", expect=200)
    if isinstance(lst, dict):
        found = any(it.get("id") == pid for it in lst.get("items", []))
        log.append(f"_Створений запис id={pid} {'присутній' if found else 'ВІДСУТНІЙ'} "
                   f"у переліку (total={lst.get('total')})._")
        log.append("")
        if lst.get("total", 0) > 100 and not found:
            log.append("_(записів у групі більше за 100 — запис може бути на іншій сторінці)_")
            log.append("")
    step("Перелік з невалідним фільтром", "GET",
         f"{RESOURCE}?randomization_group={quote('D')}", expect=400)
    step("Перелік з limit понад верхню межу", "GET", f"{RESOURCE}?limit=1000", expect=400)

    step("Отримання запису за id", "GET", f"{RESOURCE}/{pid}", expect=200)

    replaced = dict(new, randomization_group="контроль", status="completed",
                    completion_date="2025-09-30", protocol="CT-2025-002")
    step("Повна заміна запису (PUT)", "PUT", f"{RESOURCE}/{pid}", replaced, expect=200)
    step("Заміна з невалідною групою", "PUT", f"{RESOURCE}/{pid}",
         dict(replaced, randomization_group="плацебо"), expect=400)
    step("Отримання після заміни", "GET", f"{RESOURCE}/{pid}", expect=200)
    step("Перелік з фільтром за групою «контроль»", "GET",
         f"{RESOURCE}?randomization_group={quote('контроль')}&limit=5", expect=200)

    step("Видалення", "DELETE", f"{RESOURCE}/{pid}", expect=204)
    step("Повторне видалення того самого id", "DELETE", f"{RESOURCE}/{pid}", expect=404)
    step("Отримання видаленого запису", "GET", f"{RESOURCE}/{pid}", expect=404)
    step("Заміна неіснуючого запису", "PUT", f"{RESOURCE}/{pid}", replaced, expect=404)
    step("Некоректний id у шляху", "GET", f"{RESOURCE}/abc", expect=400)

    log.append(f"**Підсумок:** {step_no - failures} з {step_no} кроків дали очікуваний код.")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(log) + "\n", encoding="utf-8")
    print(f"\n{step_no - failures}/{step_no} OK; журнал: {args.out}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
