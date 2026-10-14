#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time

from _util import REPO_ROOT, RESULTS_DIR, compose, image_name, request, sh

pc = time.perf_counter


def poll_healthz(base_url: str, t0: float, timeout: float) -> float | None:
    while pc() - t0 < timeout:
        try:
            status, _, _ = request(base_url, "GET", "/healthz", timeout=1)
            if status == 200:
                return pc() - t0
        except OSError:
            pass
        time.sleep(0.05)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args()

    print("збираю образи (якщо вже зібрані — береться кеш)...")
    r = compose("build")
    if r.returncode != 0:
        sys.exit(r.stdout + r.stderr)
    print("підтягую образи postgres і nginx...")
    compose("pull", "db", "lb")

    img = image_name()
    size_b = int(sh(["docker", "image", "inspect", img, "--format", "{{.Size}}"]).stdout.strip() or 0)
    ls_line = sh(["docker", "image", "ls", img]).stdout

    times, up_returned = [], []
    for i in range(1, args.repeats + 1):
        print(f"[{i}/{args.repeats}] docker compose down -v ...")
        compose("down", "-v", "--remove-orphans")
        t0 = pc()
        proc = subprocess.Popen(["docker", "compose", "up", "-d"], cwd=REPO_ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        t = poll_healthz(args.base_url, t0, args.timeout)
        proc.communicate()
        t_up = pc() - t0
        if t is None:
            sys.exit("не дочекався 200 на /healthz; дивіться docker compose logs")
        times.append(t)
        up_returned.append(t_up)
        print(f"    перша 200 на /healthz через {t:.2f} с (команда up -d завершилась за {t_up:.2f} с)")

    med = statistics.median(times)
    result = {"image": img, "image_size_bytes": size_b,
              "image_size_mb": round(size_b / 1e6, 1), "image_size_mib": round(size_b / 2**20, 1),
              "healthz_times_s": [round(x, 3) for x in times],
              "healthz_median_s": round(med, 3),
              "up_command_times_s": [round(x, 3) for x in up_returned]}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "startup.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    md = [
        "# Розмір образу і час старту", "",
        f"Образ вебсервісу `{img}`: **{result['image_size_mb']} MB** "
        f"({result['image_size_mib']} MiB, {size_b} байт).", "",
        "```", f"$ docker image ls {img}", ls_line.rstrip(), "```", "",
        "Час від `docker compose up -d` до першої відповіді 200 на `/healthz` "
        "(образи зібрані, томи порожні — перед кожним повтором `docker compose down -v`):", "",
        "| Повтор | До 200 на /healthz, с | `up -d` завершився, с |", "|---:|---:|---:|",
    ]
    for i, (a, b) in enumerate(zip(times, up_returned), 1):
        md.append(f"| {i} | {a:.2f} | {b:.2f} |")
    md += ["", f"**Медіана: {med:.2f} с**", ""]
    (RESULTS_DIR / "startup.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    print("saved results/startup.md")


if __name__ == "__main__":
    main()
