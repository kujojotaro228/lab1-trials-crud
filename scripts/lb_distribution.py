#!/usr/bin/env python3

from __future__ import annotations

import argparse
import random
import re
import time
from collections import Counter

from _util import RESULTS_DIR, compose, md_block, sh

import loadgen  # з теки loadtest (шлях додає _util)
from httpjson import fetch_ids

UP_RE = re.compile(r"upstream=(\S+)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--requests", type=int, default=1000)
    ap.add_argument("--since", default="", help="аналізувати лог за період (напр. 10m), без генерації")
    args = ap.parse_args()

    # IP -> імʼя контейнера web
    ip_to_name, id_to_name = {}, {}
    web_ids = compose("ps", "-q", "web").stdout.split()
    for cid in web_ids:
        info = sh(["docker", "inspect", "--format",
                   "{{.Name}}|{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", cid]).stdout
        name, _, ips = info.strip().partition("|")
        id_to_name[cid[:12]] = name.lstrip("/")
        for ip in ips.split():
            ip_to_name[ip] = name.lstrip("/")

    since = args.since
    inst_counter = Counter()
    if args.requests > 0:
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
        u = loadgen.make_cfg(args.base_url)
        ids = fetch_ids(args.base_url)
        rnd = random.Random(7)
        for _ in range(args.requests):
            st, inst, _err = loadgen.http_get(u["host"], u["port"],
                                              f"{u['prefix']}/trial-participants/{rnd.choice(ids)}", 10)
            inst_counter[inst or f"status {st}"] += 1
        time.sleep(2.5)  # nginx скидає буфер логу щосекунди

    log = compose("logs", "--no-log-prefix", "--since", since or "15m", "lb").stdout
    counts = Counter()
    for line in log.splitlines():
        if "/trial-participants" not in line:
            continue
        m = UP_RE.search(line)
        if not m or m.group(1) == "-":
            continue
        for addr in m.group(1).split(","):
            addr = addr.strip()
            if addr:
                counts[addr] += 1

    total = sum(counts.values()) or 1
    lines = ["# Розподіл запитів між інстансами", "",
             f"Запущено реплік web: {len(web_ids)}.", ""]
    if inst_counter:
        n = sum(inst_counter.values())
        lines += [f"## 1. Лічильник на боці клієнта (заголовок X-Instance), {n} запитів", "",
                  "| Інстанс (hostname контейнера) | Відповідей | Частка |", "|---|---:|---:|"]
        for k, v in inst_counter.most_common():
            lines.append(f"| {k} ({id_to_name.get(k, '?')}) | {v} | {100.0 * v / n:.1f}% |")
        lines.append("")
    lines += [f"## 2. Лог балансувальника (поле upstream), з {since or 'останніх 15m'}", "",
              "| Адреса інстансу | Контейнер | Запитів | Частка |", "|---|---|---:|---:|"]
    for addr, n in counts.most_common():
        ip = addr.rsplit(":", 1)[0]
        lines.append(f"| {addr} | {ip_to_name.get(ip, '?')} | {n} | {100.0 * n / total:.1f}% |")
    sample = "\n".join([l for l in log.splitlines() if "upstream=" in l][-6:])
    lines += ["", md_block("Приклад рядків логу", "docker compose logs lb", sample)]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "distribution.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("saved results/distribution.md")


if __name__ == "__main__":
    main()
