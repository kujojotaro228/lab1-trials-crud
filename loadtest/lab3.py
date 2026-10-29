#!/usr/bin/env python3
"""Оркестратор вимірювань ЛР 3 (k3d, варіант 25: R = 4, 250m/500m, HPA 50 %, Д3).

Запуск з кореня репозиторію після scripts\\k8s\\deploy.ps1:

    py loadtest/lab3.py host                # вузли, версії, ресурси Docker
    py loadtest/lab3.py crud                # крок 4: повний цикл через :8080 і X-Pod
    py loadtest/lab3.py persist             # крок 3: дані після kubectl delete pod db-0
    py loadtest/lab3.py monitoring          # крок 5: ряди в Prometheus, kubectl top pods
    py loadtest/lab3.py series              # крок 7: ступінчасті прогони N = 1, 2, 3, 4
    py loadtest/lab3.py fixed               # крок 10: профіль зі стрибком на 4 репліках
    py loadtest/lab3.py hpa                 # крок 11: той самий профіль з HPA
    py loadtest/lab3.py ingress             # Д3: дисбаланс через NodePort і через Ingress
    py loadtest/lab3.py all                 # усе підряд (≈ 50 хв)

Модель зʼєднань генератора (крок 6): 8·R = 32 постійних зʼєднання, кожне
закривається після 100 запитів; сходинка 30 с, середні — за останні 20 с.
Результати: results/lab3/raw/*.json, *_seconds.csv; results/lab3/prom/*.json.gz;
results/lab3/*.md; журнал results/lab3/logs/lab3.log.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepgen  # noqa: E402
from httpjson import REPO_ROOT, RESOURCE, fetch_ids, request  # noqa: E402

LAB3 = REPO_ROOT / "results" / "lab3"
RAW, PROM = LAB3 / "raw", LAB3 / "prom"
NS = "trials"
R = 4                                  # 3 + (25 mod 3)
CONNS = 8 * R                          # модель зʼєднань, крок 6
REQS_PER_CONN = 100
SPIKE_FACTOR = 2.5                     # рівень стрибка варіанта: 2.5 · X(1)
ITEM_PATH = RESOURCE + "/{participant_id}"


# --------------------------------------------------------------------------
# Допоміжне: kubectl, Prometheus, журнал
# --------------------------------------------------------------------------
def sh(*cmd, check=True, timeout=600) -> str:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def kc(*args, check=True, timeout=600) -> str:
    return sh("kubectl", "-n", NS, *args, check=check, timeout=timeout)


def kc_json(*args):
    return json.loads(kc(*args, "-o", "json"))


def prom_get(prom_url: str, path: str, **params):
    url = prom_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        body = json.loads(r.read().decode("utf-8"))
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus: {body}")
    return body["data"]


def prom_instant(prom_url: str, q: str) -> list:
    return prom_get(prom_url, "/api/v1/query", query=q)["result"]


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")


def write_md(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"   -> {path.relative_to(REPO_ROOT)}")


def header(resp_headers: dict, name: str) -> str:
    for k, v in resp_headers.items():
        if k.lower() == name.lower():
            return v
    return ""


# --------------------------------------------------------------------------
# Стан Deployment web
# --------------------------------------------------------------------------
def web_pods() -> list[dict]:
    out = []
    for p in kc_json("get", "pods", "-l", "app=web")["items"]:
        conds = {c["type"]: c["status"] for c in p["status"].get("conditions", [])}
        out.append({"pod": p["metadata"]["name"], "node": p["spec"].get("nodeName"),
                    "phase": p["status"].get("phase"),
                    "terminating": p["metadata"].get("deletionTimestamp") is not None,
                    "ready": conds.get("Ready") == "True"})
    return out


def delete_hpa() -> None:
    kc("delete", "hpa", "web", "--ignore-not-found")


def scale(n: int, prom_url: str, timeout: float = 300) -> list[dict]:
    """kubectl scale + очікування N готових подів і N цілей web у Prometheus."""
    delete_hpa()
    kc("scale", "deployment/web", f"--replicas={n}")
    t_end = time.time() + timeout
    while True:
        pods = web_pods()
        ready = [p for p in pods if p["ready"] and not p["terminating"]]
        if len(ready) == n and len(pods) == n:
            break
        if time.time() > t_end:
            raise RuntimeError(f"не дочекались {n} готових подів: {pods}")
        time.sleep(2)
    q = f'count(up{{namespace="{NS}",pod=~"web-.*"}} == 1)'
    while True:
        res = prom_instant(prom_url, q)
        up = int(float(res[0]["value"][1])) if res else 0
        if up == n:
            break
        if time.time() > t_end:
            raise RuntimeError(f"Prometheus: {up} цілей web UP замість {n}")
        time.sleep(2)
    time.sleep(10)                     # стабілізація: перші збори метрик нових подів
    return ready


# --------------------------------------------------------------------------
# Експорт сирих зразків Prometheus за час прогону
# --------------------------------------------------------------------------
EXPORT = [
    f'http_requests_total{{namespace="{NS}"}}',
    f'http_request_duration_seconds_bucket{{namespace="{NS}",path="{ITEM_PATH}"}}',
    f'http_requests_in_progress{{namespace="{NS}"}}',
    f'process_cpu_seconds_total{{namespace="{NS}",pod=~"web-.*"}}',
    f'cgroup_cpu_nr_periods_total{{namespace="{NS}"}}',
    f'cgroup_cpu_nr_throttled_total{{namespace="{NS}"}}',
    f'container_cpu_cfs_periods_total{{namespace="{NS}",container="web"}}',
    f'container_cpu_cfs_throttled_periods_total{{namespace="{NS}",container="web"}}',
    f'container_cpu_usage_seconds_total{{namespace="{NS}",container="web"}}',
    f'kube_deployment_status_replicas_ready{{namespace="{NS}",deployment="web"}}',
    f'kube_deployment_status_replicas{{namespace="{NS}",deployment="web"}}',
    f'kube_horizontalpodautoscaler_status_desired_replicas{{namespace="{NS}"}}',
    f'kube_horizontalpodautoscaler_status_current_replicas{{namespace="{NS}"}}',
    f'kube_pod_info{{namespace="{NS}",pod=~"web-.*"}}',
    f'pg_stat_activity_count{{namespace="{NS}"}}',
]


def export_prom(prom_url: str, run: dict, pad: float = 30) -> Path:
    t_a, t_b = run["t0_epoch"] - pad, run["t_end_epoch"] + pad
    dur = int(t_b - t_a) + 1
    out = {"run": run["name"], "t_from": t_a, "t_to": t_b, "series": {}}
    for sel in EXPORT:
        metric = sel.split("{", 1)[0]
        try:
            data = prom_get(prom_url, "/api/v1/query", query=f"{sel}[{dur}s]", time=f"{t_b:.3f}")
        except Exception as exc:  # noqa: BLE001
            print(f"   ! {metric}: {exc}")
            continue
        out["series"][metric] = [{"labels": r["metric"],
                                  "values": [[float(t), float(v)] for t, v in r["values"]]}
                                 for r in data["result"]]
    PROM.mkdir(parents=True, exist_ok=True)
    path = PROM / f"{run['name']}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(out, f)
    return path


# --------------------------------------------------------------------------
# Генератор
# --------------------------------------------------------------------------
def gen_cfg(args, name: str, base_url: str | None = None, **over) -> dict:
    ns = stepgen.build_argparser().parse_args(["--name", name])
    ns.base_url = base_url or args.base_url
    ns.procs = args.procs
    ns.step_dur = 30.0                 # крок 6: сходинка 30 с
    ns.settle = 10.0                   # середні — за останні 20 с
    ns.max_conns = CONNS               # 8·R постійних зʼєднань
    ns.reqs_per_conn = REQS_PER_CONN   # нове зʼєднання після 100 запитів
    ns.timeout = 5.0
    for k, v in over.items():
        setattr(ns, k, v)
    return stepgen.make_cfg(ns)


def run_gen(args, cfg: dict, extra: dict) -> dict:
    ids = fetch_ids(args.base_url)
    res = stepgen.run(cfg, ids)
    res["extra"] = extra
    stepgen.save(res, RAW)
    path = export_prom(args.prom_url, res)
    print(f"   збережено raw/{res['name']}.json, {path.name}")
    return res


def load_run(name: str) -> dict:
    p = RAW / f"{name}.json"
    if not p.exists():
        raise SystemExit(f"немає {p}: спершу виконайте відповідну команду")
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Команди
# --------------------------------------------------------------------------
def cmd_host(args) -> None:
    info = json.loads(sh("docker", "info", "--format", "{{json .}}"))
    nodes = json.loads(sh("kubectl", "get", "nodes", "-o", "json"))["items"]
    out = {
        "docker": {"ncpu": info.get("NCPU"), "mem_bytes": info.get("MemTotal"),
                   "server": info.get("ServerVersion"), "kernel": info.get("KernelVersion")},
        "k3d": sh("k3d", "version", check=False).strip(),
        "kubectl": sh("kubectl", "version", "-o", "json", check=False),
        "helm": sh("helm", "list", "-A", "-o", "json", check=False),
        "nodes": [{"name": n["metadata"]["name"],
                   "labels": {k: v for k, v in n["metadata"]["labels"].items()
                              if k in ("role", "node-role.kubernetes.io/control-plane")},
                   "taints": n["spec"].get("taints", []),
                   "kubelet": n["status"]["nodeInfo"]["kubeletVersion"],
                   "cpu": n["status"]["capacity"]["cpu"],
                   "memory": n["status"]["capacity"]["memory"]} for n in nodes],
        "pods": sh("kubectl", "get", "pods", "-A", "-o", "wide"),
    }
    save_json(LAB3 / "host.json", out)
    print(json.dumps({k: out[k] for k in ("docker", "nodes")}, ensure_ascii=False, indent=1))


def cmd_crud(args) -> None:
    print("== крок 4: повний цикл через localhost:8080, под за X-Pod")
    base = args.base_url
    body = {"participant_code": f"TP-K8S-{int(time.time()) % 100000:05d}",
            "protocol": "CT-2025-001", "randomization_group": "A",
            "enrollment_date": "2025-03-01", "status": "active", "completion_date": None}
    rows = []

    def step(title, method, path, data=None, expect=200):
        st, hd, js = request(base, method, path, data)
        pod = header(hd, "X-Pod")
        ok = st == expect
        rows.append(f"| {len(rows) + 1} | {title} | `{method} {path}` | {expect} | "
                    f"{'✅' if ok else '❌'} {st} | `{pod}` |")
        print(f"   {method:6} {path:40} -> {st} (expect {expect}) X-Pod={pod}")
        return js

    created = step("Створення", "POST", RESOURCE, body, 201)
    rid = created["id"]
    step("Читання", "GET", f"{RESOURCE}/{rid}")
    body2 = dict(body, randomization_group="контроль", status="completed",
                 completion_date="2025-09-30")
    step("Заміна", "PUT", f"{RESOURCE}/{rid}", body2)
    step("Читання після заміни", "GET", f"{RESOURCE}/{rid}")
    step("Видалення", "DELETE", f"{RESOURCE}/{rid}", expect=204)
    step("Читання видаленого", "GET", f"{RESOURCE}/{rid}", expect=404)
    pods = web_pods()
    write_md(LAB3 / "crud_check.md", [
        "# Крок 4: повний цикл через localhost:8080", "",
        f"Реплік web: {len(pods)} ({', '.join(p['pod'] + ' @ ' + str(p['node']) for p in pods)}).",
        "", "| № | Крок | Запит | Очікувано | Отримано | Под (X-Pod) |",
        "|---:|---|---|---:|---|---|", *rows])


def cmd_persist(args) -> None:
    print("== крок 3: збереження даних після kubectl delete pod db-0")
    base = args.base_url
    code = f"TP-PERSIST-{int(time.time()) % 100000:05d}"
    body = {"participant_code": code, "protocol": "CT-2025-001", "randomization_group": "B",
            "enrollment_date": "2025-03-01", "status": "active", "completion_date": None}
    st, _, js = request(base, "POST", RESOURCE, body)
    rid = js["id"]
    _, _, lst = request(base, "GET", f"{RESOURCE}?limit=1")
    total_before = lst["total"]
    uid_before = kc("get", "pod", "db-0", "-o", "jsonpath={.metadata.uid}")
    pvc_before = kc("get", "pvc", "data-db-0", "-o", "jsonpath={.spec.volumeName}")
    t0 = time.time()
    kc("delete", "pod", "db-0", "--wait=true")
    while True:
        r = kc("get", "pod", "db-0", "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
               check=False).strip()
        if r == "True":
            break
        if time.time() - t0 > 300:
            raise RuntimeError("db-0 не став Ready за 5 хв")
        time.sleep(2)
    t_db = time.time() - t0
    while True:                         # web знову готовий (readiness /healthz ходить у базу)
        try:
            st2, _, _ = request(base, "GET", "/healthz")
            if st2 == 200:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    uid_after = kc("get", "pod", "db-0", "-o", "jsonpath={.metadata.uid}")
    pvc_after = kc("get", "pvc", "data-db-0", "-o", "jsonpath={.spec.volumeName}")
    st3, _, rec = request(base, "GET", f"{RESOURCE}/{rid}")
    _, _, lst2 = request(base, "GET", f"{RESOURCE}?limit=1")
    write_md(LAB3 / "persistence.md", [
        "# Крок 3: дані після видалення пода бази", "",
        "| Крок | Результат |", "|---|---|",
        f"| `POST {RESOURCE}` | {st}, id = {rid}, код `{code}`; записів у базі — {total_before} |",
        f"| `kubectl delete pod db-0` | под `{uid_before}` видалено |",
        f"| StatefulSet створив новий под | `{uid_after}`, Ready через {t_db:.1f} с |",
        f"| PersistentVolume | до: `{pvc_before}`, після: `{pvc_after}` |",
        f"| `GET {RESOURCE}/{rid}` | **{st3}**, `{rec.get('participant_code') if isinstance(rec, dict) else rec}` |",
        f"| Кількість записів | {lst2['total']} (було {total_before}) |"])


def cmd_monitoring(args) -> None:
    print("== крок 5: перевірка рядів Prometheus і kubectl top pods")
    for i in range(300):                # трохи трафіку, щоб були швидкості
        request(args.base_url, "GET", f"{RESOURCE}?limit=1")
    time.sleep(12)
    p = args.prom_url
    checks = [
        ("запити кожного пода web",
         f'sum by (pod) (rate(http_requests_total{{namespace="{NS}",pod=~"web-.*"}}[1m]))'),
        ("лічильник CFS: періоди (cAdvisor)",
         f'container_cpu_cfs_periods_total{{namespace="{NS}",container="web"}}'),
        ("лічильник CFS: дроселювання (cAdvisor)",
         f'container_cpu_cfs_throttled_periods_total{{namespace="{NS}",container="web"}}'),
        ("готові репліки (kube-state-metrics)",
         f'kube_deployment_status_replicas_ready{{namespace="{NS}",deployment="web"}}'),
        ("postgres-exporter", f'pg_up{{namespace="{NS}"}}'),
        ("зʼєднання з базою за станом",
         f'sum by (state) (pg_stat_activity_count{{namespace="{NS}",datname="trials"}})'),
    ]
    lines = ["# Крок 5: моніторинг", "", "| Що | Запит PromQL | Рядів | Значення |",
             "|---|---|---:|---|"]
    for title, q in checks:
        try:
            res = prom_instant(p, q)
            vals = "; ".join(
                f"{r['metric'].get('pod') or r['metric'].get('state') or r['metric'].get('deployment') or ''}"
                f"={float(r['value'][1]):.3g}" for r in res[:6])
            lines.append(f"| {title} | `{q}` | {len(res)} | {vals} |")
            print(f"   {title}: {len(res)} рядів")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"| {title} | `{q}` | помилка | {exc} |")
    targets = prom_get(p, "/api/v1/targets", state="active")["activeTargets"]
    jobs = {}
    for t in targets:
        j = t["labels"].get("job", "?")
        jobs.setdefault(j, [0, 0])
        jobs[j][0 if t["health"] == "up" else 1] += 1
    lines += ["", "Цілі Prometheus (UP / не UP):", "",
              *[f"* `{j}`: {u} / {d}" for j, (u, d) in sorted(jobs.items())]]
    top = sh("kubectl", "top", "pods", "-n", NS, check=False)
    lines += ["", "```text", "$ kubectl top pods -n trials", top.rstrip(), "```"]
    write_md(LAB3 / "monitoring_check.md", lines)


def cmd_series(args) -> None:
    for n in args.replicas:
        print(f"== крок 7: {n} реплік(и), без автомасштабування")
        pods = scale(n, args.prom_url)
        print("   поди:", ", ".join(f"{p['pod']}@{p['node']}" for p in pods))
        cfg = gen_cfg(args, f"k8s_n{n}")
        res = run_gen(args, cfg, {"replicas": n, "pods": pods, "conns": CONNS,
                                  "reqs_per_conn": REQS_PER_CONN})
        print(f"   X({n}) = {res['X_rps']} ± {res['X_sd']} rps на сходинці "
              f"{res['X_target_rps']}; зупинка: {res['stop_reason']}")
        time.sleep(args.pause)


def profile_levels() -> tuple[float, float, float, float]:
    x1 = load_run("k8s_n1")["X_rps"]
    xr = load_run(f"k8s_n{R}")["X_rps"]
    low = round(0.3 * x1)
    spike = round(min(SPIKE_FACTOR * x1, 0.9 * xr))
    return x1, xr, low, spike


def profile_cfg(args, name):
    x1, xr, low, spike = profile_levels()
    prof = f"30:{low},60:{spike},90:{low}"
    print(f"   X(1) = {x1}, X({R}) = {xr}: профіль {prof} "
          f"(стрибок = min({SPIKE_FACTOR}·X(1) = {SPIKE_FACTOR * x1:.0f}, 0.9·X({R}) = {0.9 * xr:.0f}))")
    return gen_cfg(args, name, profile=prof), {"X1": x1, "XR": xr, "low": low, "spike": spike,
                                               "profile": prof}


def cmd_fixed(args) -> None:
    print(f"== крок 10: профіль зі стрибком на фіксованих {R} репліках")
    pods = scale(R, args.prom_url)
    cfg, meta = profile_cfg(args, "prof_fixed")
    run_gen(args, cfg, {"replicas": R, "pods": pods, "mode": "fixed", **meta})


class HpaLogger(threading.Thread):
    """kubectl get hpa кожні 5 с протягом прогону (перевірка ряду kube-state-metrics)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.rows, self.stop_ev = [], threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            try:
                h = kc_json("get", "hpa", "web")
                st = h.get("status", {})
                cur = (st.get("currentMetrics") or [{}])[0].get("resource", {}).get("current", {})
                self.rows.append({"t_epoch": time.time(),
                                  "current": st.get("currentReplicas"),
                                  "desired": st.get("desiredReplicas"),
                                  "cpu_util": cur.get("averageUtilization")})
            except Exception as exc:  # noqa: BLE001
                self.rows.append({"t_epoch": time.time(), "error": str(exc)})
            self.stop_ev.wait(5)


def cmd_hpa(args) -> None:
    print("== крок 11: той самий профіль з HPA (ціль 50 %, 1..4, scaleDown 30 с)")
    kc("scale", "deployment/web", "--replicas=1")
    sh("kubectl", "apply", "-f", str(REPO_ROOT / "k8s" / "hpa.yaml"))
    print("   чекаю, поки HPA показує 1 репліку 60 с поспіль ...")
    stable_since, t_end = None, time.time() + 900
    while True:
        h = kc_json("get", "hpa", "web").get("status", {})
        ready = [p for p in web_pods() if p["ready"] and not p["terminating"]]
        one = h.get("currentReplicas") == 1 and h.get("desiredReplicas") == 1 and len(ready) == 1
        now = time.time()
        stable_since = (stable_since or now) if one else None
        if stable_since and now - stable_since >= 60:
            break
        if now > t_end:
            raise RuntimeError(f"HPA не стабілізувався на 1 репліці: {h}")
        time.sleep(5)
    print("   " + sh("kubectl", "-n", NS, "get", "hpa", "web").strip().replace("\n", "\n   "))
    cfg, meta = profile_cfg(args, "prof_hpa")
    log = HpaLogger()
    log.start()
    try:
        run_gen(args, cfg, {"mode": "hpa", **meta, "hpa": sh("kubectl", "-n", NS, "get", "hpa",
                                                             "web", "-o", "json")})
    finally:
        log.stop_ev.set()
        log.join()
    save_json(RAW / "prof_hpa_kubectl_hpa.json", log.rows)
    print("   " + sh("kubectl", "-n", NS, "get", "hpa", "web").strip().replace("\n", "\n   "))
    delete_hpa()


def cmd_ingress(args) -> None:
    print(f"== Д3: дисбаланс на {R} репліках через NodePort і через Ingress (Traefik)")
    xr = load_run(f"k8s_n{R}")["X_rps"]
    rate = round(0.5 * xr / 50) * 50
    for name, url in (("ing_nodeport", args.base_url), ("ing_ingress", args.ingress_url)):
        pods = scale(R, args.prom_url)
        st, _, _ = request(url, "GET", "/healthz")
        print(f"   {name}: {url} /healthz -> {st}; {rate} rps, 90 с")
        cfg = gen_cfg(args, name, base_url=url, profile=f"90:{rate}", settle=30.0)
        res = run_gen(args, cfg, {"replicas": R, "pods": pods, "rate": rate, "via": url})
        st0 = res["steps"][0]
        print(f"   досягнута {st0['achieved_rps']} rps, p99 {st0['p99_ms']} мс, "
              f"X-Pod: {st0['pods_rps']}")
        time.sleep(args.pause)


class _Tee:
    def __init__(self, stream, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream, self.f = stream, open(path, "a", encoding="utf-8")

    def write(self, s):
        try:
            self.stream.write(s)
        except UnicodeEncodeError:
            self.stream.write(s.encode("ascii", "replace").decode())
        self.f.write(s)
        self.f.flush()
        return len(s)

    def flush(self):
        self.stream.flush()
        self.f.flush()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.stdout = _Tee(sys.stdout, LAB3 / "logs" / "lab3.log")
    print(f"\n#### {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(sys.argv)}")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["all", "host", "crud", "persist", "monitoring", "series",
                                        "fixed", "hpa", "ingress"])
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--ingress-url", default="http://localhost:8081")
    ap.add_argument("--prom-url", default="http://localhost:9090")
    ap.add_argument("--replicas", type=int, nargs="+", default=[1, 2, 3, R])
    ap.add_argument("--procs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    ap.add_argument("--pause", type=float, default=15.0)
    args = ap.parse_args()
    steps = {"host": cmd_host, "crud": cmd_crud, "persist": cmd_persist,
             "monitoring": cmd_monitoring, "series": cmd_series, "fixed": cmd_fixed,
             "hpa": cmd_hpa, "ingress": cmd_ingress}
    order = (["host", "crud", "persist", "monitoring", "series", "fixed", "hpa", "ingress"]
             if args.command == "all" else [args.command])
    for c in order:
        steps[c](args)


if __name__ == "__main__":
    main()
