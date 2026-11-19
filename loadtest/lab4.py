#!/usr/bin/env python3
"""Lab 4 measurement orchestrator (task queue and worker pool, variant 25).

Run from the repository root after scripts\\k8s\\deploy.ps1 (Lab 3 cluster + worker):

    py loadtest/lab4.py setup        # cluster state, versions, 2 web replicas
    py loadtest/lab4.py api          # step 1: task endpoints, tasks_queued/tasks_running
    py loadtest/lab4.py step2        # step 2: 20 tasks on 1 worker -> t_io, t_cpu
    py loadtest/lab4.py step3        # step 3: naive claim, T_poll = 10 ms and 1 ms
    py loadtest/lab4.py step4        # step 4: FOR UPDATE SKIP LOCKED and FOR UPDATE
    py loadtest/lab4.py step5        # step 5: lease, heartbeat, fencing token, kill -STOP
    py loadtest/lab4.py step6        # step 6: X(W) for W = 1, 2, 4
    py loadtest/lab4.py step7        # step 7: Little's law, 0.7·X(R) and 1.3·X(R)
    py loadtest/lab4.py step8        # step 8: rollout restart under load
    py loadtest/lab4.py step9        # step 9: FAIL_RATE = 0.15 + scenario F3
    py loadtest/lab4.py d6           # step 10: D6, task cancellation
    py loadtest/lab4.py step9b       # supplementary step 9 run (finite load, F3 values)
    py loadtest/lab4.py all          # everything in order

All evidence goes to results/lab4/: <step>.md (what was done, commands, timestamps,
outputs), <step>.json (numbers), raw/ (tasks and task_results rows, worker logs,
Prometheus series), log of all commands — logs/lab4.log.

Run time boundaries come from the database clock (clock_timestamp() in pod db-0): the same
clock stores created_at/started_at/finished_at, and Prometheus timestamps use the same
clock (the Docker VM).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepgen  # noqa: E402
from httpjson import REPO_ROOT, fetch_ids, request  # noqa: E402

LAB4 = REPO_ROOT / "results" / "lab4"
RAW = LAB4 / "raw"
NS = "trials"
R = 4                                  # from Lab 3
T_POLL_MS = 10                         # variant 25
LEASE_S = 15                           # variant 25
FAIL_F = 0.15
DEFAULT_ENV = {"CLAIM_MODE": "skip_locked", "STORE_MODE": "idempotent",
               "LEASE_SECONDS": str(LEASE_S), "POLL_MS": str(T_POLL_MS), "FAIL_RATE": "0",
               "PROCESS_PASSES": "1"}

TASK_CSV_SQL = ("SELECT id, record_id, status, seed, attempts, max_attempts, token, worker_id, "
                "extract(epoch from created_at) AS created, extract(epoch from started_at) AS started, "
                "extract(epoch from finished_at) AS finished, cancel_requested, "
                "extract(epoch from cancel_requested_at) AS cancel_requested_at "
                "FROM tasks ORDER BY id")
RESULTS_CSV_SQL = ("SELECT task_id, token, worker_id, extract(epoch from created_at) AS created "
                   "FROM task_results ORDER BY task_id, created_at")


# --------------------------------------------------------------------------
# Log and evidence
# --------------------------------------------------------------------------
def now_s() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Evidence:
    """Markdown evidence file of one step: every line carries a host timestamp."""

    def __init__(self, name: str, title: str):
        self.path = LAB4 / f"{name}.md"
        self.lines = [f"# {title}", "", f"Запуск: {now_s()} (час хоста)", ""]
        self.data: dict = {}
        self.name = name

    def say(self, text: str) -> None:
        print(f"   {text}")
        self.lines.append(f"* `{now_s()}` {text}")

    def block(self, title: str, text: str, lang: str = "text") -> None:
        self.lines += ["", f"**{title}**", "", f"```{lang}", text.rstrip(), "```", ""]

    def table(self, header: list, rows: list) -> None:
        self.lines += ["", "| " + " | ".join(header) + " |",
                       "|" + "|".join("---" for _ in header) + "|"]
        self.lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
        self.lines.append("")

    def save(self) -> None:
        LAB4.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        (LAB4 / f"{self.name}.json").write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"   -> {self.path.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------
# Cluster
# --------------------------------------------------------------------------
class K8s:
    def __init__(self, args):
        self.args = args

    def sh(self, *cmd, check=True, timeout=900, stdin=None) -> str:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, input=stdin)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(map(str, cmd))}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def kc(self, *a, check=True, timeout=900) -> str:
        return self.sh("kubectl", "-n", NS, *a, check=check, timeout=timeout)

    # ---------------------------------------------------------------- database
    def psql(self, sql: str) -> list[list[str]]:
        out = self.kc("exec", "db-0", "-c", "postgres", "--", "psql", "-U", "trials", "-d",
                      "trials", "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t", "-c", sql)
        return [ln.split("\t") for ln in out.splitlines() if ln != ""]

    def psql_copy(self, sql: str) -> str:
        return self.kc("exec", "db-0", "-c", "postgres", "--", "psql", "-U", "trials", "-d",
                       "trials", "-v", "ON_ERROR_STOP=1", "-c",
                       f"\\copy ({sql}) TO STDOUT WITH CSV HEADER")

    def lock_sampler(self, stop: threading.Event, out: list) -> threading.Thread:
        """Every second: number of pg_stat_activity rows with wait_event_type = 'Lock'
        and pg_locks rows with granted = false (one psql session with \\watch 1)."""
        sql = ("SELECT extract(epoch from clock_timestamp()), "
               "(SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'), "
               "(SELECT count(*) FROM pg_locks WHERE NOT granted)\n\\watch 1\n")

        def run():
            p = subprocess.Popen(["kubectl", "-n", NS, "exec", "-i", "db-0", "-c", "postgres", "--",
                                  "psql", "-U", "trials", "-d", "trials", "-At", "-F", "\t"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
            p.stdin.write(sql)
            p.stdin.flush()

            def reader():
                for ln in p.stdout:
                    parts = ln.strip().split("\t")
                    if len(parts) == 3:
                        try:
                            out.append((float(parts[0]), int(parts[1]), int(parts[2])))
                        except ValueError:
                            pass
            th = threading.Thread(target=reader, daemon=True)
            th.start()
            stop.wait()
            p.terminate()
            th.join(timeout=5)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------ workers
    def worker_pods(self) -> list[dict]:
        out = []
        for p in json.loads(self.kc("get", "pods", "-l", "app=worker", "-o", "json"))["items"]:
            cs = (p["status"].get("containerStatuses") or [{}])[0]
            conds = {c["type"]: c["status"] for c in p["status"].get("conditions", [])}
            out.append({"pod": p["metadata"]["name"], "node": p["spec"].get("nodeName"),
                        "terminating": p["metadata"].get("deletionTimestamp") is not None,
                        "ready": conds.get("Ready") == "True",
                        "restarts": cs.get("restartCount", 0), "state": cs.get("state", {}),
                        "last_state": cs.get("lastState", {}),
                        "rs": p["metadata"].get("labels", {}).get("pod-template-hash")})
        return out

    def wait_workers(self, n: int, ev: Evidence | None = None, timeout=300) -> list[dict]:
        t0, t_end = time.time(), time.time() + timeout
        while True:
            pods = self.worker_pods()
            live = [p for p in pods if not p["terminating"]]
            if len(pods) == n and all(p["ready"] for p in live) and len(live) == n:
                if ev:
                    ev.say(f"готово воркерів: {n} ({', '.join(p['pod'] for p in live) or '—'}), "
                           f"очікування {time.time() - t0:.1f} с")
                return live
            if time.time() > t_end:
                raise RuntimeError(f"не дочекались {n} воркерів: {pods}")
            time.sleep(1)

    def scale_workers(self, n: int, ev: Evidence | None = None) -> list[dict]:
        self.kc("scale", "deployment/worker", f"--replicas={n}")
        if ev:
            ev.say(f"`kubectl scale deployment/worker --replicas={n}`")
        return self.wait_workers(n, ev)

    def set_worker_env(self, ev: Evidence | None = None, **kw) -> None:
        cur = self.worker_env()
        changes = {k: str(v) for k, v in kw.items() if cur.get(k) != str(v)}
        if not changes:
            if ev:
                ev.say("змінні воркера вже: " + ", ".join(f"{k}={v}" for k, v in kw.items()))
            return
        self.kc("set", "env", "deployment/worker", *[f"{k}={v}" for k, v in changes.items()])
        self.kc("rollout", "status", "deployment/worker", "--timeout=300s")
        if ev:
            ev.say("`kubectl set env deployment/worker " +
                   " ".join(f"{k}={v}" for k, v in changes.items()) + "` і rollout status")

    def worker_env(self) -> dict:
        d = json.loads(self.kc("get", "deployment", "worker", "-o", "json"))
        c = d["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: e.get("value") for e in c.get("env", []) if "value" in e}

    def worker_replicas(self) -> int:
        d = json.loads(self.kc("get", "deployment", "worker", "-o", "json"))
        return int(d["spec"].get("replicas") or 0)

    def logs(self, pod: str, previous=False) -> list[dict]:
        args = ["logs", pod, "-c", "worker", "--tail=-1"]
        if previous:
            args.append("--previous")
        out = self.kc(*args, check=False)
        recs = []
        for ln in out.splitlines():
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    rec = json.loads(ln)
                    recs.append(rec)
                except ValueError:
                    pass
        return recs

    def all_worker_logs(self, since_iso: str | None = None) -> list[dict]:
        recs = []
        for p in self.worker_pods():
            for r in self.logs(p["pod"]):
                if since_iso is None or r.get("ts", "") >= since_iso:
                    r["pod"] = p["pod"]
                    recs.append(r)
        return recs

    def worker_metrics(self, pod: str) -> str:
        return self.kc("exec", pod, "-c", "worker", "--", "sh", "-c",
                       "wget -qO- http://127.0.0.1:9100/metrics")

    def kill(self, pod: str, sig: str, target: str = "worker") -> str:
        cmd = (f"kill -{sig} 1" if target == "pid1"
               else f"kill -{sig} $(cat /tmp/worker.pid)")
        self.kc("exec", pod, "-c", "worker", "--", "sh", "-c", cmd)
        return f"kubectl exec {pod} -- sh -c '{cmd}'"

    def rollout_restart(self) -> None:
        self.kc("rollout", "restart", "deployment/worker")

    def get_pods_text(self) -> str:
        return self.kc("get", "pods", "-l", "app=worker", "-o", "wide")

    def prom(self, q: str, t: float | None = None):
        params = {"query": q}
        if t is not None:
            params["time"] = f"{t:.3f}"
        url = self.args.prom_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=60) as r:
            body = json.loads(r.read().decode())
        return body["data"]["result"]

    def prom_range(self, q: str, a: float, b: float, step: float = 5):
        params = {"query": q, "start": f"{a:.3f}", "end": f"{b:.3f}", "step": str(step)}
        url = self.args.prom_url.rstrip("/") + "/api/v1/query_range?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=60) as r:
            body = json.loads(r.read().decode())
        return body["data"]["result"]


# --------------------------------------------------------------------------
# Shared operations
# --------------------------------------------------------------------------
def db_now(k: K8s) -> float:
    return float(k.psql("SELECT extract(epoch from clock_timestamp())")[0][0])


def iso_utc(epoch: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="milliseconds")


def reset_tables(k: K8s, ev: Evidence) -> None:
    k.psql("TRUNCATE task_results, tasks RESTART IDENTITY")
    ev.say("`TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні")


def results_pk(k: K8s, on: bool, ev: Evidence) -> None:
    has_pk = k.psql("SELECT count(*) FROM pg_constraint WHERE conname = 'task_results_pkey'")[0][0] == "1"
    if on and not has_pk:
        k.psql("TRUNCATE task_results, tasks RESTART IDENTITY; "
               "DROP INDEX IF EXISTS ix_task_results_task_id; "
               "ALTER TABLE task_results ADD CONSTRAINT task_results_pkey PRIMARY KEY (task_id)")
        ev.say("первинний ключ task_results(task_id) повернуто (таблиці очищено)")
    elif not on and has_pk:
        k.psql("ALTER TABLE task_results DROP CONSTRAINT task_results_pkey; "
               "CREATE INDEX ix_task_results_task_id ON task_results (task_id)")
        ev.say("первинний ключ task_results тимчасово замінено звичайним індексом "
               "ix_task_results_task_id (крок 3)")
    ev.block("Обмеження і індекси task_results", "\n".join(
        "\t".join(r) for r in k.psql(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'task_results'::regclass UNION ALL "
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'task_results'")))


def fresh_start(k: K8s, ev: Evidence, n: int, pk: bool | None = True, **env) -> list[dict]:
    """Clean start of a run: 0 workers -> (task_results primary key) -> empty tables ->
    worker env -> n fresh pods. Leftovers of the previous step do not get into the run,
    /metrics counters and RESTARTS of the new pods start from zero."""
    k.scale_workers(0, ev)
    if pk is not None:
        results_pk(k, pk, ev)
    reset_tables(k, ev)
    if env:
        k.set_worker_env(ev, **env)
    return k.scale_workers(n, ev)


def counts(k: K8s) -> dict:
    r = k.psql("SELECT count(*) FILTER (WHERE status='queued'), count(*) FILTER (WHERE status='running'), "
               "count(*) FILTER (WHERE status='done'), count(*) FILTER (WHERE status='failed'), "
               "count(*) FILTER (WHERE status='cancelled'), count(*) FROM tasks")[0]
    return dict(zip(("queued", "running", "done", "failed", "cancelled", "total"), map(int, r)))


def wait_drained(k: K8s, ev: Evidence, timeout: float = 900, expect_total: int | None = None) -> dict:
    """Waits until no queued or running tasks are left; writes the moments to the evidence."""
    t0 = time.time()
    last_print = 0
    while True:
        c = counts(k)
        if c["queued"] == 0 and c["running"] == 0 and (expect_total is None or c["total"] >= expect_total):
            ev.say(f"черга порожня: {c} (очікування {time.time() - t0:.1f} с)")
            return c
        if time.time() - last_print > 15:
            print(f"   ... {c}")
            last_print = time.time()
        if time.time() - t0 > timeout:
            ev.say(f"НЕ дочекались порожньої черги за {timeout:.0f} с: {c}")
            return c
        time.sleep(0.5)


def export_tables(k: K8s, name: str) -> tuple[list[dict], list[dict]]:
    RAW.mkdir(parents=True, exist_ok=True)
    t_csv = k.psql_copy(TASK_CSV_SQL)
    r_csv = k.psql_copy(RESULTS_CSV_SQL)
    (RAW / f"{name}_tasks.csv").write_text(t_csv, encoding="utf-8")
    (RAW / f"{name}_results.csv").write_text(r_csv, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(t_csv))), list(csv.DictReader(io.StringIO(r_csv)))


def save_logs(name: str, recs: list[dict]) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    with open(RAW / f"{name}_worker_logs.jsonl", "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def fnum(x):
    return None if x in (None, "") else float(x)


def fmt2(x):
    return "—" if x is None else f"{x:.2f}"


def percentile(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[max(0, math.ceil(p / 100 * len(vals)) - 1)]


_IDS: list[int] = []


def rec_ids(args) -> list[int]:
    """First 100 ids of existing trial_participants records (tasks reference them)."""
    if not _IDS:
        _IDS.extend(sorted(fetch_ids(args.base_url))[:100])
        if not _IDS:
            raise SystemExit("у trial_participants немає записів: спершу заповніть базу (ЛР 2/3)")
    return _IDS


SQL_INSERT_N = ("INSERT INTO tasks (record_id, seed) SELECT a.ids[1 + g % array_length(a.ids, 1)], "
                "1 + floor(random() * 2147483646)::bigint FROM generate_series(1, {n}) g, "
                "(SELECT array_agg(id) AS ids FROM (SELECT id FROM trial_participants ORDER BY id "
                "LIMIT 100) s) a")


def gen(args, name: str, profile: str, extra: dict | None = None) -> dict:
    """Generator in task-creation mode: POST /trial-participants/{id}/tasks."""
    ns = stepgen.build_argparser().parse_args(["--name", name])
    ns.base_url, ns.target, ns.procs = args.base_url, "task", args.procs
    ns.profile, ns.settle, ns.timeout, ns.max_conns = profile, 0.0, 5.0, 64
    ids = rec_ids(args)
    res = stepgen.run(stepgen.make_cfg(ns), ids)
    res["extra"] = extra or {}
    stepgen.save(res, RAW)
    created = [c for c in res["created_tasks"] if c[0] is not None]
    print(f"   генератор {name}: створено {len(created)} задач, коди {res['codes']}")
    return res


def prom_hist_p99(k: K8s, step: str, a: float, b: float) -> float | None:
    dur = max(10, int(b - a) + 12)
    q = (f'histogram_quantile(0.99, sum by (le) (increase(worker_task_duration_seconds_bucket'
         f'{{namespace="{NS}",step="{step}"}}[{dur}s])))')
    res = k.prom(q, b + 7)
    try:
        v = float(res[0]["value"][1])
        return None if math.isnan(v) else v
    except (IndexError, KeyError, ValueError):
        return None


def prom_increase(k: K8s, selector: str, a: float, b: float) -> float | None:
    dur = max(10, int(b - a) + 12)
    res = k.prom(f"sum(increase({selector}[{dur}s]))", b + 7)
    try:
        return float(res[0]["value"][1])
    except (IndexError, KeyError, ValueError):
        return None


def parse_metrics(txt: str) -> dict:
    """/metrics text -> {(name, frozenset(labels)): value}."""
    import re
    out = {}
    for ln in txt.splitlines():
        m = re.match(r'^([a-zA-Z_:][\w:]*)(\{([^}]*)\})?\s+(\S+)$', ln)
        if not m:
            continue
        labels = frozenset(re.findall(r'(\w+)="([^"]*)"', m.group(3) or ""))
        try:
            out[(m.group(1), labels)] = float(m.group(4))
        except ValueError:
            pass
    return out


def metrics_snapshot(k: K8s, pods: list[str]) -> dict:
    """Sum of /metrics of all worker pods (read directly from the pods)."""
    tot: dict = defaultdict(float)
    for pod in pods:
        try:
            for key, v in parse_metrics(k.worker_metrics(pod)).items():
                tot[key] += v
        except Exception:  # noqa: BLE001
            pass
    return dict(tot)


def snapshot_delta(a: dict, b: dict) -> dict:
    """Snapshot difference: empty/successful claims and claim p99 from the histogram."""
    def val(snap, name, **lab):
        want = set(lab.items())
        return sum(v for (n, l), v in snap.items() if n == name and want <= set(l))
    res = {"claims_empty": val(b, "worker_claims_total", result="empty") - val(a, "worker_claims_total", result="empty"),
           "claims_task": val(b, "worker_claims_total", result="task") - val(a, "worker_claims_total", result="task")}
    buckets = defaultdict(float)
    for (n, l), v in b.items():
        d = dict(l)
        if n == "worker_task_duration_seconds_bucket" and d.get("step") == "claim":
            buckets[d["le"]] += v
    for (n, l), v in a.items():
        d = dict(l)
        if n == "worker_task_duration_seconds_bucket" and d.get("step") == "claim":
            buckets[d["le"]] -= v
    les = sorted(buckets, key=lambda x: float("inf") if x == "+Inf" else float(x))
    total = buckets.get("+Inf", 0)
    res["claim_count"] = total
    res["claim_p99_s"] = None
    if total > 0:
        target, prev_le, prev_c = 0.99 * total, 0.0, 0.0
        for le in les:
            c = buckets[le]
            if c >= target:
                if le == "+Inf":
                    res["claim_p99_s"] = prev_le
                else:
                    hi = float(le)
                    res["claim_p99_s"] = prev_le + (hi - prev_le) * ((target - prev_c) / (c - prev_c) if c > prev_c else 1)
                break
            prev_le, prev_c = (float(le) if le != "+Inf" else prev_le), c
    return res


def export_prom(k: K8s, name: str, a: float, b: float, queries: dict) -> dict:
    out = {"from": a, "to": b, "series": {}}
    for key, q in queries.items():
        try:
            out["series"][key] = k.prom_range(q, a, b, 5)
        except Exception as exc:  # noqa: BLE001
            out["series"][key] = {"error": str(exc)}
    (RAW / f"{name}_prom.json").write_text(json.dumps(out), encoding="utf-8")
    return out


def dup_stats(tasks: list[dict], results: list[dict], logs: list[dict]) -> dict:
    """D = n_dup / n; a task is a duplicate if task_results has more than one row with its id
    OR the worker logs mark it as completed (event = done) more than once."""
    n = len(tasks)
    per_task = Counter(int(r["task_id"]) for r in results)
    dup_db = {t for t, c in per_task.items() if c > 1}
    done_log = Counter(int(r["task_id"]) for r in logs if r.get("event") == "done")
    dup_log = {t for t, c in done_log.items() if c > 1}
    dup = dup_db | dup_log
    return {"n": n, "results_rows": len(results), "n_dup": len(dup), "D": len(dup) / n if n else None,
            "n_dup_db": len(dup_db), "D_db": len(dup_db) / n if n else None,
            "n_dup_log": len(dup_log), "D_log": len(dup_log) / n if n else None,
            "dup_examples": sorted(dup)[:10], "only_db": len(dup_db - dup_log),
            "only_log": len(dup_log - dup_db),
            "duplicate_result_errors": sum(1 for r in logs if r.get("event") == "duplicate_result"),
            "done_log_lines": sum(done_log.values())}


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------
def cmd_setup(k: K8s, args) -> None:
    ev = Evidence("setup", "ЛР 4: стан кластера перед вимірюваннями")
    k.kc("scale", "deployment/web", "--replicas=2")
    k.kc("rollout", "status", "deployment/web", "--timeout=300s")
    ev.say("`kubectl scale deployment/web --replicas=2` — воркери звертаються до web за записами")
    k.kc("delete", "hpa", "web", "--ignore-not-found")
    ev.block("kubectl get nodes", k.sh("kubectl", "get", "nodes", "-o", "wide"))
    ev.block("kubectl get pods -n trials -o wide", k.kc("get", "pods", "-o", "wide"))
    ev.block("kubectl get deployment worker -o yaml (resources, env)",
             k.kc("get", "deployment", "worker", "-o",
                  "jsonpath={.spec.template.spec.containers[0].resources}{'\\n'}"
                  "{.spec.template.spec.terminationGracePeriodSeconds}{'\\n'}"
                  "{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{.valueFrom.fieldRef.fieldPath}{'\\n'}{end}"))
    ev.block("Міграції", "\n".join("\t".join(r) for r in k.psql(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version")))
    ev.block("\\d tasks", k.kc("exec", "db-0", "-c", "postgres", "--", "psql", "-U", "trials", "-d",
                                "trials", "-c", "\\d tasks"))
    ev.block("\\d task_results", k.kc("exec", "db-0", "-c", "postgres", "--", "psql", "-U", "trials",
                                       "-d", "trials", "-c", "\\d task_results"))
    t = k.prom('count by (job) (up{namespace="trials"} == 1)')
    ev.block("Цілі Prometheus у просторі trials (UP)",
             "\n".join(f"{r['metric'].get('job')}: {r['value'][1]}" for r in t))
    ev.save()


def cmd_api(k: K8s, args) -> None:
    ev = Evidence("step1_api", "Крок 1: ендпоінти задач і вимірювачі")
    fresh_start(k, ev, 0, **DEFAULT_ENV)
    base = args.base_url
    rows = []

    def call(method, path, expect):
        st, _, js = request(base, method, path)
        rows.append([f"`{method} {path}`", expect, st, "✅" if st == expect else "❌",
                     f"`{json.dumps(js, ensure_ascii=False)[:160]}`"])
        return js

    ids = rec_ids(args)
    t1 = call("POST", f"/trial-participants/{ids[0]}/tasks", 202)
    call("POST", "/trial-participants/999999/tasks", 404)
    call("GET", f"/tasks/{t1['id']}", 200)
    call("GET", "/tasks/999999", 404)
    for i in ids[1:5]:
        request(base, "POST", f"/trial-participants/{i}/tasks")
    call("GET", "/tasks?status=queued&limit=2&offset=1", 200)
    call("GET", "/tasks?status=bad", 400)
    ev.table(["Запит", "Очікувано", "Отримано", "", "Тіло (початок)"], rows)
    time.sleep(12)
    q = k.prom(f'tasks_queued{{namespace="{NS}"}}')
    r_ = k.prom(f'tasks_running{{namespace="{NS}"}}')
    ev.block("tasks_queued / tasks_running у Prometheus (по репліці web)",
             "\n".join(f"tasks_queued{{pod={x['metric'].get('pod')}}} = {x['value'][1]}" for x in q) + "\n" +
             "\n".join(f"tasks_running{{pod={x['metric'].get('pod')}}} = {x['value'][1]}" for x in r_))
    ev.say(f"для порівняння — стан таблиці tasks у той самий момент: {counts(k)} (вимірювачі "
           "оновлюються кожні 5 с, Prometheus збирає кожні 5 с)")
    ev.data = {"rows": rows, "queued_gauge": [x["value"][1] for x in q]}
    reset_tables(k, ev)
    ev.save()


def cmd_step2(k: K8s, args) -> None:
    ev = Evidence("step2", "Крок 2: 20 задач на одному воркері, t_io і t_cpu")
    pods = fresh_start(k, ev, 1, **DEFAULT_ENV)
    t_a = db_now(k)
    since = iso_utc(t_a - 2)
    for i in rec_ids(args)[:20]:
        request(args.base_url, "POST", f"/trial-participants/{i}/tasks")
    ev.say("створено 20 задач (20 запитів POST /trial-participants/{id}/tasks)")
    wait_drained(k, ev, expect_total=20)
    t_b = db_now(k)
    time.sleep(8)
    logs = [r for r in k.all_worker_logs(since)]
    save_logs("step2", logs)
    tasks, results = export_tables(k, "step2")
    done = [r for r in logs if r.get("event") == "done"]
    fetch = [r["fetch_ms"] for r in done]
    proc = [r["process_ms"] for r in done]
    store = [r["store_ms"] for r in done]
    claim = [r["claim_ms"] for r in logs if r.get("event") == "claim"]
    # Primary source — the worker histogram worker_task_duration_seconds (sum/count per step),
    # read from the pod's /metrics (the pod was just started, so the counters cover only these 20 tasks).
    # Cross-check — the same histogram in Prometheus and the *_ms fields in the worker log.
    mtxt = k.worker_metrics(pods[0]["pod"])
    (RAW / "step2_worker_metrics.txt").write_text(mtxt, encoding="utf-8")
    hist = {}
    for ln in mtxt.splitlines():
        for kind in ("sum", "count"):
            pre = f"worker_task_duration_seconds_{kind}{{"
            if ln.startswith(pre):
                step = ln.split('step="')[1].split('"')[0]
                hist.setdefault(step, {})[kind] = float(ln.rsplit(" ", 1)[1])
    hmean = {st: v["sum"] / v["count"] * 1000 for st, v in hist.items() if v.get("count")}
    prom_mean = {}
    for st in ("claim", "fetch", "process", "store"):
        sm = prom_increase(k, f'worker_task_duration_seconds_sum{{namespace="{NS}",step="{st}"}}', t_a, t_b)
        cn = prom_increase(k, f'worker_task_duration_seconds_count{{namespace="{NS}",step="{st}"}}', t_a, t_b)
        prom_mean[st] = sm / cn * 1000 if sm is not None and cn else None
    lmean = {"fetch": statistics.fmean(fetch), "process": statistics.fmean(proc),
             "store": statistics.fmean(store), "claim": statistics.fmean(claim)}
    src = hmean if all(st in hmean for st in ("fetch", "process", "store")) else lmean
    t_io = src["fetch"] + src["store"]
    t_cpu = src["process"]
    ev.say("t_io і t_cpu взято з гістограми воркера worker_task_duration_seconds (sum/count з "
           "/metrics пода)" if src is hmean else "УВАГА: /metrics пода недоступний, t_io і t_cpu — з журналу")
    names = {"fetch": "fetch (GET запису з web)", "process": "process (обробка зрізу 256×256)",
             "store": "store (транзакція завершення + результат)",
             "claim": "claim (захоплення; у гістограмі — разом із порожніми)"}
    ev.table(["Крок", "Гістограма воркера (/metrics), мс", "count", "Prometheus increase, мс",
              "Журнал воркера, мс (n)"],
             [[names[st], fmt2(hmean.get(st)), int(hist.get(st, {}).get("count", 0)),
               fmt2(prom_mean.get(st)), f"{lmean[st]:.2f} ({len(claim) if st == 'claim' else len(done)})"]
              for st in ("fetch", "process", "store", "claim")])
    ev.table(["Величина", "мс"], [["**t_io = fetch + store**", f"**{t_io:.2f}**"],
                                  ["**t_cpu = process**", f"**{t_cpu:.2f}**"]])
    claim_task_ms = lmean["claim"]
    fetch_ms, store_ms = src["fetch"], src["store"]
    pr = {"prom_mean_ms": prom_mean, "hist": hist, "log_mean_ms": lmean}
    ev.block("Приклад рядка журналу воркера (done)", json.dumps(done[0], ensure_ascii=False))
    ev.data = {"pod": pods[0]["pod"], "n_done": len(done), "t_io_ms": t_io, "t_cpu_ms": t_cpu,
               "fetch_ms": fetch_ms, "store_ms": store_ms, "claim_ms": claim_task_ms,
               "source": "histogram" if src is hmean else "log", "detail": pr,
               "results": len(results), "statuses": Counter(t["status"] for t in tasks)}
    ev.save()


def run_batch(k: K8s, args, ev: Evidence, name: str, n_tasks: int, rate: float,
              snap: bool = False) -> dict:
    """Series of steps 3–4: R workers, n tasks at the given rate, until the queue is empty.
    snap=True: /metrics snapshots of the pods right before the generator and right after
    the queue empties (claim counters and the claim histogram for exactly this series)."""
    pods = fresh_start(k, ev, R, pk=None)
    time.sleep(12)                       # Prometheus has time to discover and scrape the new pods
    snap_a = metrics_snapshot(k, [p["pod"] for p in pods]) if snap else None
    t_a = db_now(k)
    since = iso_utc(t_a - 2)
    dur = n_tasks / rate
    res = gen(args, name, f"{dur:g}:{rate:g}")
    ev.say(f"генератор: {len(res['created_tasks'])} POST за {dur:g} с ({rate:g}/с), коди {res['codes']}")
    accepted = int(res["codes"].get("202", 0))
    if accepted != n_tasks:
        ev.say(f"УВАГА: прийнято {accepted} з {n_tasks} запитів створення")
    c = wait_drained(k, ev, expect_total=accepted)
    t_b = db_now(k)
    direct = snapshot_delta(snap_a, metrics_snapshot(k, [p["pod"] for p in pods])) if snap else None
    time.sleep(8)
    logs = k.all_worker_logs(since)
    save_logs(name, logs)
    tasks, results = export_tables(k, name)
    d = dup_stats(tasks, results, logs)
    d.update({"t_from": t_a, "t_to": t_b, "pods": [p["pod"] for p in pods], "counts": c,
              "direct": direct})
    return d


def cmd_step3(k: K8s, args) -> None:
    ev = Evidence("step3", "Крок 3: наївне захоплення (SELECT, потім окремий UPDATE)")
    k.scale_workers(0, ev)
    results_pk(k, False, ev)
    out = {}
    for poll in (T_POLL_MS, 1):
        ev.say(f"--- T_poll = {poll} мс ---")
        k.set_worker_env(ev, CLAIM_MODE="naive", STORE_MODE="plain", LEASE_SECONDS="0",
                         POLL_MS=str(poll), FAIL_RATE="0", PROCESS_PASSES="1")
        d = run_batch(k, args, ev, f"step3_poll{poll}", 300, 100)
        out[poll] = d
        ev.say(f"T_poll = {poll} мс: n = {d['n']}, рядків у task_results = {d['results_rows']}, "
               f"дублів за таблицею = {d['n_dup_db']}, за журналами (done > 1 разу) = {d['n_dup_log']}, "
               f"разом n_dup = {d['n_dup']}, **D = {d['D']:.4f}**; приклади задач: {d['dup_examples']}")
        k.scale_workers(0, ev)
    ev.table(["T_poll, мс", "n", "Рядків результатів", "n_dup (таблиця)", "n_dup (журнали)",
              "n_dup (таблиця або журнали)", "**D = n_dup / n**"],
             [[p, d["n"], d["results_rows"], d["n_dup_db"], d["n_dup_log"], d["n_dup"],
               f"**{d['D']:.4f}**"] for p, d in out.items()])
    if out.get(1, {}).get("dup_examples"):
        tid = out[1]["dup_examples"][0]
        ev.block(f"Рядки task_results задачі {tid} (T_poll = 1 мс)", "\n".join(
            "\t".join(x) for x in k.psql(
                f"SELECT task_id, token, worker_id, created_at FROM task_results WHERE task_id = {tid}")))
    ev.data = out
    ev.save()


def cmd_step4(k: K8s, args) -> None:
    ev = Evidence("step4", "Крок 4: FOR UPDATE SKIP LOCKED і FOR UPDATE без SKIP LOCKED")
    k.scale_workers(0, ev)
    results_pk(k, True, ev)
    out = {}
    for mode in ("skip_locked", "for_update"):
        ev.say(f"--- CLAIM_MODE = {mode}, T_poll = 1 мс ---")
        k.set_worker_env(ev, CLAIM_MODE=mode, STORE_MODE="plain", LEASE_SECONDS="0",
                         POLL_MS="1", FAIL_RATE="0", PROCESS_PASSES="1")
        stop, samples = threading.Event(), []
        th = k.lock_sampler(stop, samples)
        d = run_batch(k, args, ev, f"step4_{mode}", 300, 100, snap=True)
        stop.set()
        th.join(timeout=10)
        win = [s for s in samples if d["t_from"] - 1 <= s[0] <= d["t_to"] + 1]
        d["lock_samples"] = len(win)
        d["lock_wait_max"] = max((s[1] for s in win), default=None)
        d["locks_not_granted_max"] = max((s[2] for s in win), default=None)
        (RAW / f"step4_{mode}_locks.json").write_text(json.dumps(samples), encoding="utf-8")
        d["claim_p99_s"] = prom_hist_p99(k, "claim", d["t_from"], d["t_to"])
        d["claims_empty"] = prom_increase(
            k, f'worker_claims_total{{namespace="{NS}",result="empty"}}', d["t_from"], d["t_to"])
        d["claims_task"] = prom_increase(
            k, f'worker_claims_total{{namespace="{NS}",result="task"}}', d["t_from"], d["t_to"])
        logs = [json.loads(x) for x in (RAW / f"step4_{mode}_worker_logs.jsonl").read_text(
            encoding="utf-8").splitlines()]
        d["claim_p99_log_ms"] = percentile([r["claim_ms"] for r in logs if r.get("event") == "claim"], 99)
        out[mode] = d
        ev.say(f"{mode}: з /metrics подів напряму (різниця знімків до і після серії): порожніх "
               f"захоплень {d['direct']['claims_empty']:.0f}, захоплень задачі {d['direct']['claims_task']:.0f}, "
               f"p99 захоплення за гістограмою {fmt2((d['direct']['claim_p99_s'] or 0) * 1000)} мс")
        ev.say(f"{mode}: D = {d['D']:.4f} (таблиця {d['n_dup_db']}, журнали {d['n_dup_log']}), помилок дубля ключа "
               f"{d['duplicate_result_errors']}; p99 захоплення (гістограма) = {d['claim_p99_s']}; "
               f"порожніх захоплень = {d['claims_empty']}; макс. рядків Lock = {d['lock_wait_max']} "
               f"за {d['lock_samples']} знімків")
        k.scale_workers(0, ev)
    ev.table(["Захоплення", "n", "n_dup", "D", "p99 захоплення, мс (Prometheus / /metrics подів)",
              "Порожніх захоплень (Prometheus / /metrics)", "Захоплень задачі (Prometheus / /metrics)",
              "Макс. рядків Lock у pg_stat_activity", "Макс. pg_locks granted = false", "Знімків"],
             [[m, d["n"], d["n_dup"], f"{d['D']:.4f}",
               f"{fmt2(d['claim_p99_s'] * 1000 if d['claim_p99_s'] is not None else None)} / "
               f"{fmt2(d['direct']['claim_p99_s'] * 1000 if d['direct']['claim_p99_s'] is not None else None)}",
               f"{fmt2(d['claims_empty'])} / {d['direct']['claims_empty']:.0f}",
               f"{fmt2(d['claims_task'])} / {d['direct']['claims_task']:.0f}",
               d["lock_wait_max"], d["locks_not_granted_max"], d["lock_samples"]] for m, d in out.items()])
    ev.say("p99 захоплення і лічильники — гістограма worker_task_duration_seconds{step=\"claim\"} і "
           "лічильник worker_claims_total воркерів: через Prometheus (increase за вікно серії) і "
           "різницею знімків /metrics подів від старту генератора до порожньої черги")
    k.set_worker_env(ev, **DEFAULT_ENV)
    ev.say("для наступних кроків повернуто SKIP LOCKED, T_poll = 10 мс, lease 15 с, ON CONFLICT")
    ev.data = out
    ev.save()


def cmd_step5(k: K8s, args) -> None:
    ev = Evidence("step5", f"Крок 5: lease {LEASE_S} с, heartbeat {LEASE_S / 3:g} с, fencing token, kill -STOP")
    pods = fresh_start(k, ev, 2, **DEFAULT_ENV)
    a, b = pods[0]["pod"], pods[1]["pod"]
    t_a = db_now(k)
    since = iso_utc(t_a - 2)

    def proc_state() -> str:
        return k.kc("exec", a, "-c", "worker", "--", "sh", "-c",
                    "for p in 1 $(cat /tmp/worker.pid); do printf 'PID %s: ' $p; "
                    "tr '\\0' ' ' < /proc/$p/cmdline; grep '^State' /proc/$p/status; done").strip()

    def done_by(pod: str) -> int:
        return int(k.psql(f"SELECT count(*) FROM task_results WHERE worker_id = '{pod}'")[0][0])

    ins = SQL_INSERT_N

    # 1) Demonstration: kill -STOP 1 has no effect on PID 1 of the namespace (tini), the worker keeps running
    ev.block(f"Процеси в контейнері {a} (PID 1 — tini, воркер — дочірній процес)", proc_state())
    k.psql(ins.format(n=1000))
    ev.say("демонстрація: додано 1000 задач (INSERT … generate_series), щоб воркери були зайняті")
    time.sleep(1.0)
    before = done_by(a)
    cmd1 = k.kill(a, "STOP", target="pid1")
    ev.say(f"`{cmd1}`")
    ev.block("Стан процесів одразу після kill -STOP 1", proc_state())
    time.sleep(3)
    after = done_by(a)
    k.kill(a, "CONT", target="pid1")
    ev.say(f"за 3 с після `kill -STOP 1` воркер {a} записав ще {after - before} результатів: сигнал "
           "STOP до PID 1 свого простору імен PID ядро не доставляє (init захищений від сигналів "
           "зсередини простору імен), тому процес не зупинився. Далі зупиняємо сам процес воркера "
           "за PID з /tmp/worker.pid — це той самий ефект «завислого» воркера, який мала дати "
           "команда із завдання")
    wait_drained(k, ev)
    reset_tables(k, ev)

    # 2) Main check: 100 tasks on two workers, worker A stopped for 2·L
    since = iso_utc(db_now(k) - 2)
    k.psql(ins.format(n=100))
    ev.say("створено 100 задач (один INSERT, щоб на момент зупинки черга ще не спорожніла)")
    time.sleep(0.2)
    victim = None
    for attempt in range(10):
        cmd = k.kill(a, "STOP")
        t_stop = db_now(k)
        r = k.psql(f"SELECT id, attempts, token, status, worker_id, "
                   f"extract(epoch from lease_until) FROM tasks WHERE status = 'running' "
                   f"AND worker_id = '{a}'")
        if r:
            victim = r[0]
            break
        k.kill(a, "CONT")
        ev.say(f"спроба {attempt + 1}: у момент зупинки {a} не мав задачі в обробці, повтор")
        if counts(k)["queued"] < 10:
            k.psql(ins.format(n=100))
            ev.say("черга майже спорожніла — додано ще 100 задач")
        time.sleep(0.3)
    if victim is None:
        raise RuntimeError("не вдалося зупинити воркер посеред задачі")
    tid = int(victim[0])
    ev.say(f"`{cmd}` о {iso_utc(t_stop)} (годинник бази): у {a} в обробці задача {tid}, "
           f"attempts = {victim[1]}, token = {victim[2]}, lease_until = {iso_utc(float(victim[5]))}")
    t_cont_due = t_stop + 2 * LEASE_S
    while db_now(k) < t_cont_due:
        time.sleep(0.5)
    mid = k.psql(f"SELECT status, attempts, token, worker_id FROM tasks WHERE id = {tid}")[0]
    ev.block("Стан процесів воркера A перед kill -CONT (State: T — зупинений)", proc_state())
    cmd2 = k.kill(a, "CONT")
    t_cont = db_now(k)
    ev.say(f"`{cmd2}` о {iso_utc(t_cont)} (через {t_cont - t_stop:.1f} с = 2·L); "
           f"на цей момент задача {tid}: status = {mid[0]}, attempts = {mid[1]}, token = {mid[2]}, "
           f"worker_id = {mid[3]}")
    wait_drained(k, ev)
    time.sleep(8)
    logs = k.all_worker_logs(since)
    save_logs("step5", logs)
    tasks, results = export_tables(k, "step5")
    fin = k.psql(f"SELECT status, attempts, token, worker_id, extract(epoch from finished_at) "
                 f"FROM tasks WHERE id = {tid}")[0]
    res_rows = k.psql(f"SELECT task_id, token, worker_id, created_at FROM task_results WHERE task_id = {tid}")
    reaped_log = [r for r in logs if r.get("task_id") == tid]
    zero = [r for r in logs if r.get("event") == "zero_rows" and r.get("task_id") == tid]
    ev.table(["Величина", "Значення"], [
        ["Задача в обробці на момент зупинки", tid],
        ["attempts / token до зупинки", f"{victim[1]} / {victim[2]}"],
        ["attempts / token після", f"{fin[1]} / {fin[2]}"],
        ["Статус після", fin[0]],
        ["Воркер, що завершив", fin[3]],
        ["Рядків у task_results для задачі", len(res_rows)],
        ["Рядків журналу зупиненого воркера про нуль оновлених рядків", len(zero)]])
    ev.block(f"Рядки task_results для задачі {tid}", "\n".join("\t".join(x) for x in res_rows))
    ev.block(f"Журнал воркерів для задачі {tid}", "\n".join(
        json.dumps({k_: v for k_, v in r.items() if k_ != "result"}, ensure_ascii=False)
        for r in sorted(reaped_log, key=lambda r: r.get("ts", ""))))
    ev.data = {"task_id": tid, "before": victim, "after": fin, "mid": mid, "stopped_pod": a,
               "other_pod": b, "t_stop": t_stop, "t_cont": t_cont, "results_rows": len(res_rows),
               "zero_rows_lines": zero, "statuses": Counter(t["status"] for t in tasks),
               "multi_result": sum(1 for c in Counter(r["task_id"] for r in results).values() if c > 1),
               "pid1_demo": {"before": before, "after": after}}
    ev.save()


def est_x(n_workers: int) -> float:
    p = LAB4 / "step2.json"
    if not p.exists():
        return 20.0 * n_workers
    d = json.loads(p.read_text(encoding="utf-8"))
    return n_workers * 1000.0 / (d["t_io_ms"] + d["t_cpu_ms"] + d["claim_ms"])


def cmd_step6(k: K8s, args) -> None:
    ev = Evidence("step6", "Крок 6: пропускна здатність пулу X(W)")
    fresh_start(k, ev, 0, **DEFAULT_ENV)
    out = {}
    for w in (1, 2, R):
        ev.say(f"--- W = {w} ---")
        k.scale_workers(0, ev)
        reset_tables(k, ev)
        # (a) literally as in the assignment: 400 queued tasks, X over the whole drain time
        k.psql(SQL_INSERT_N.format(n=400))
        ev.say(f"(а) при 0 воркерах додано 400 задач; queued = {counts(k)['queued']}")
        k.scale_workers(w, ev)
        wait_drained(k, ev, timeout=900, expect_total=400)
        t400, _ = export_tables(k, f"step6_w{w}_400")
        st4 = [fnum(t["started"]) for t in t400 if t["started"]]
        fi4 = [fnum(t["finished"]) for t in t400 if t["finished"]]
        span4 = max(fi4) - min(st4)
        full4 = int(span4)
        ps4 = [sum(1 for f_ in fi4 if min(st4) + i <= f_ < min(st4) + i + 1) for i in range(full4)]
        x400 = len(fi4) / span4 if span4 > 0 else None
        ev.say(f"(а) 400 задач спорожнено за {span4:.2f} с: X = 400 / {span4:.2f} = {fmt2(x400)} задач/с; "
               f"повних секунд {full4} — вікна 30 с до спорожнення при 400 задачах немає, тому основне "
               f"вимірювання (б) — з більшою чергою")
        k.scale_workers(0, ev)
        reset_tables(k, ev)
        # (b) main: a queue of ≈45 s of work, X(W) over the last 30 s before it empties
        n_q = max(400, int(math.ceil(45 * est_x(w) / 100.0)) * 100)
        k.psql(SQL_INSERT_N.format(n=n_q))
        c = counts(k)
        ev.say(f"при 0 воркерах додано {n_q} задач одним INSERT; у черзі queued = {c['queued']} "
               f"(потрібно ≥ 400; узято max(400, ≈45 с роботи пулу за оцінкою з кроку 2), щоб "
               f"вікно 30 с до спорожнення черги лежало в сталому режимі, а не на розгоні)")
        t_a = db_now(k)
        pods = k.scale_workers(w, ev)
        c = wait_drained(k, ev, timeout=1800, expect_total=n_q)
        t_b = db_now(k)
        time.sleep(10)
        tasks, results = export_tables(k, f"step6_w{w}")
        started = sorted(fnum(t["started"]) for t in tasks if t["started"])
        t_empty = started[-1]                 # the moment the queue became empty
        w0 = t_empty - 30
        fin = [fnum(t["finished"]) for t in tasks if t["finished"]]
        per_sec = [sum(1 for f in fin if w0 + i <= f < w0 + i + 1) for i in range(30)]
        x = statistics.fmean(per_sec)
        sd = statistics.stdev(per_sec)
        claim_p99 = prom_hist_p99(k, "claim", w0, t_empty)
        xact = k.prom(f'sum(rate(pg_stat_database_xact_commit{{namespace="{NS}",datname="trials"}}[30s])) + '
                      f'sum(rate(pg_stat_database_xact_rollback{{namespace="{NS}",datname="trials"}}[30s]))',
                      t_empty)
        cpu = k.prom(f'sum(rate(container_cpu_usage_seconds_total{{namespace="{NS}",pod="db-0",'
                     f'container="postgres"}}[30s]))', t_empty)
        wcpu = k.prom(f'avg(rate(container_cpu_usage_seconds_total{{namespace="{NS}",container="worker"}}[30s]))',
                      t_empty)
        export_prom(k, f"step6_w{w}", t_a - 10, t_b + 10, {
            "xact": f'sum(rate(pg_stat_database_xact_commit{{namespace="{NS}",datname="trials"}}[15s]))',
            "db_cpu": f'sum(rate(container_cpu_usage_seconds_total{{namespace="{NS}",pod="db-0",container="postgres"}}[15s]))',
            "worker_cpu": f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{NS}",container="worker"}}[15s]))'})

        def v(res):
            try:
                return float(res[0]["value"][1])
            except (IndexError, KeyError, ValueError):
                return None
        d = {"W": w, "n_tasks": n_q, "X": x, "sd": sd, "X_400": x400, "span_400_s": span4,
             "per_sec_400": ps4, "per_sec": per_sec, "t_empty": t_empty,
             "window": [w0, t_empty], "drain_s": t_empty - min(started), "claim_p99_s": claim_p99,
             "xact_per_s": v(xact), "db_cpu_cores": v(cpu), "worker_cpu_cores": v(wcpu),
             "pods": [p["pod"] for p in pods], "multi_result": sum(
                 1 for c_ in Counter(r["task_id"] for r in results).values() if c_ > 1)}
        out[w] = d
        ev.say(f"W = {w}: X = {x:.2f} ± {sd:.2f} задач/с (вікно 30 с до спорожнення), p99 захоплення "
               f"{claim_p99}, транзакцій/с {d['xact_per_s']}, CPU db-0 {d['db_cpu_cores']}, "
               f"CPU воркера {d['worker_cpu_cores']}")
    k.scale_workers(R, ev)
    ev.table(["W", "(а) 400 задач: час спорожнення, с", "(а) X = 400 / час, задач/с"],
             [[w, f"{d['span_400_s']:.2f}", fmt2(d["X_400"])] for w, d in out.items()])
    ev.table(["W", "Задач у черзі", "X(W), задач/с", "Розкид", "p99 захоплення, мс",
              "Транзакцій/с бази", "CPU пода бази, ядер", "CPU воркера (сер.), ядер"],
             [[w, d["n_tasks"], f"{d['X']:.2f}", f"{d['sd']:.2f}",
               f"{d['claim_p99_s'] * 1000:.2f}" if d["claim_p99_s"] is not None else "—",
               f"{d['xact_per_s']:.1f}" if d["xact_per_s"] is not None else "—",
               f"{d['db_cpu_cores']:.3f}" if d["db_cpu_cores"] is not None else "—",
               f"{d['worker_cpu_cores']:.3f}" if d["worker_cpu_cores"] is not None else "—"]
              for w, d in out.items()])
    ev.data = out
    ev.save()


def x_r() -> float:
    p = LAB4 / "step6.json"
    if not p.exists():
        raise SystemExit("спершу виконайте step6")
    return json.loads(p.read_text(encoding="utf-8"))[str(R)]["X"]


def steady_run(k: K8s, args, ev: Evidence, name: str, rate: float, dur: float,
               during=None, drain=True) -> dict:
    t_a = db_now(k)
    th = None
    if during is not None:
        th = threading.Thread(target=during, args=(t_a,), daemon=True)
        th.start()
    res = gen(args, name, f"{dur:g}:{rate:g}")
    t_gen_end = db_now(k)
    if th is not None:
        th.join()
    ev.say(f"{name}: {rate:.1f} задач/с протягом {dur:g} с; генератор: {sum(res['codes'].values())} "
           f"відповідей, коди {res['codes']}; кінець о {iso_utc(t_gen_end)}")
    q_end = counts(k)
    ev.say(f"на кінець генерації: {q_end}")
    c = wait_drained(k, ev, timeout=1800) if drain else q_end
    t_b = db_now(k)
    return {"t_from": t_a, "t_gen_end": t_gen_end, "t_to": t_b, "counts_end": q_end,
            "counts_drained": c, "gen_codes": res["codes"]}


def cmd_step7(k: K8s, args) -> None:
    ev = Evidence("step7", "Крок 7: закон Літтла для черги")
    xr = x_r()
    out = {}
    for label, factor, dur in (("low", 0.7, 180), ("high", 1.3, 120)):
        fresh_start(k, ev, R, **DEFAULT_ENV)
        rate = round(factor * xr, 1)
        d = steady_run(k, args, ev, f"step7_{label}", rate, dur)
        time.sleep(8)
        tasks, results = export_tables(k, f"step7_{label}")
        export_prom(k, f"step7_{label}", d["t_from"] - 10, d["t_to"] + 10, {
            "tasks_queued": f'max(tasks_queued{{namespace="{NS}"}})',
            "tasks_running": f'max(tasks_running{{namespace="{NS}"}})'})
        d["rate"] = rate
        if label == "low":
            t_first = min(fnum(t["created"]) for t in tasks)
            w0, w1 = t_first + 60, t_first + 180         # the last 120 s of the 180 s run
            win = [t for t in tasks if w0 <= fnum(t["created"]) < w1]
            lam = len(win) / 120.0
            wq = statistics.fmean(fnum(t["started"]) - fnum(t["created"]) for t in win)
            lq_res = k.prom(f'avg(avg_over_time(tasks_queued{{namespace="{NS}"}}[120s]))', w1)
            lq = float(lq_res[0]["value"][1]) if lq_res else None
            samples = k.prom_range(f'max(tasks_queued{{namespace="{NS}"}})', w0, w1, 5)
            d.update({"window": [w0, w1], "lambda": lam, "W_q_s": wq, "lambda_W_q": lam * wq,
                      "L_q": lq, "n_window": len(win),
                      "discrepancy_pct": abs(lq - lam * wq) / (lam * wq) * 100 if lq is not None and wq else None,
                      "tasks_queued_samples": [s[1] for s in (samples[0]["values"] if samples else [])]})
            ev.say(f"вікно останніх 120 с: λ = {lam:.3f} задач/с, W_q = {wq * 1000:.2f} мс, "
                   f"λ·W_q = {lam * wq:.4f}, L_q (середнє tasks_queued) = {lq}, розбіжність "
                   f"{d['discrepancy_pct']}")
        else:
            by_created = sorted(tasks, key=lambda t: fnum(t["created"]))
            first = [fnum(t["started"]) - fnum(t["created"]) for t in by_created[:100]]
            last = [fnum(t["started"]) - fnum(t["created"]) for t in by_created[-100:]]
            d.update({"queue_at_end": d["counts_end"]["queued"], "W_q_first100_s": statistics.fmean(first),
                      "W_q_last100_s": statistics.fmean(last), "n_tasks": len(tasks)})
            ev.say(f"1.3·X(R): у черзі на кінець генерації {d['queue_at_end']}; W_q перших 100 = "
                   f"{d['W_q_first100_s']:.3f} с, останніх 100 = {d['W_q_last100_s']:.3f} с")
        out[label] = d
    ev.data = out
    ev.save()


class PodWatcher(threading.Thread):
    """`kubectl get pods -w` with jsonpath: every change of a worker pod (including the terminated
    state with exitCode before the pod is deleted) is written to raw/<name>_pods_watch.tsv."""

    # fields are separated by a literal "|": a tab escape inside the jsonpath template does
    # not survive Windows argument quoting (step 8/9 of the first run got spaces instead)
    JP = ('{.metadata.name}|{.metadata.deletionTimestamp}|'
          '{.status.containerStatuses[0].state.terminated.exitCode}|'
          '{.status.containerStatuses[0].state.terminated.reason}|'
          '{.status.containerStatuses[0].state.terminated.finishedAt}|'
          '{.status.containerStatuses[0].lastState.terminated.exitCode}|'
          '{.status.containerStatuses[0].lastState.terminated.finishedAt}|'
          '{.status.containerStatuses[0].restartCount}{"\\n"}')

    def __init__(self, k: K8s, name: str = "step8"):
        super().__init__(daemon=True)
        self.k, self.name = k, name
        self.lines: list[str] = []
        self.proc = None

    def run(self):
        self.proc = subprocess.Popen(["kubectl", "-n", NS, "get", "pods", "-l", "app=worker",
                                      "-w", "-o", f"jsonpath={self.JP}"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8")
        for ln in self.proc.stdout:
            self.lines.append(f"{now_s()}|{ln.strip()}")

    @property
    def terminated(self) -> dict:
        return {(e["pod"], e["finishedAt"]): e for e in parse_pod_watch(self.lines)}

    def stop(self):
        if self.proc:
            self.proc.terminate()
        self.join(timeout=5)
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / f"{self.name}_pods_watch.tsv").write_text(
            "host_time|pod|deletionTimestamp|exitCode|reason|finishedAt|lastExitCode|"
            "lastFinishedAt|restarts\n" + "\n".join(self.lines) + "\n", encoding="utf-8")


def parse_pod_watch(lines: list[str]) -> list[dict]:
    """Container terminations from `kubectl get pods -w` lines: the final exit of a pod being
    deleted (state.terminated) and every restart (lastState.terminated). Understands both the
    "|" format and the whitespace-padded format of the first run."""
    import re
    out = {}
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("host_time"):
            continue
        if "|" in ln:
            f = ln.split("|")
            if len(f) < 9:
                continue
            _, pod, _del, ec, reason, fin, lec, lfin, _rc = f[:9]
            if ec:
                out[(pod, fin)] = {"pod": pod, "exitCode": int(ec), "reason": reason,
                                   "finishedAt": fin, "kind": "final"}
            if lec and lfin:
                out.setdefault((pod, lfin), {"pod": pod, "exitCode": int(lec), "reason": "restart",
                                             "finishedAt": lfin, "kind": "restart"})
        else:
            m = re.match(r"^\S+ \S+\t(\S+)\s+(?:\d{4}-\d\d-\d\dT\S+Z\s+)?(-?\d+)\s+([A-Za-z]+)\s+"
                         r"(\d{4}-\d\d-\d\dT\S+Z)", ln)
            if m:
                out[(m.group(1), m.group(4))] = {"pod": m.group(1), "exitCode": int(m.group(2)),
                                                 "reason": m.group(3), "finishedAt": m.group(4),
                                                 "kind": "final"}
    return sorted(out.values(), key=lambda e: e["finishedAt"])


class LogFollower:
    """kubectl logs -f for every worker pod: the log is kept even after the pod is deleted."""

    def __init__(self, k: K8s, name: str):
        self.k, self.name, self.procs, self.files = k, name, {}, {}

    def follow_new(self):
        for p in self.k.worker_pods():
            if p["pod"] not in self.procs:
                f = open(RAW / f"{self.name}_{p['pod']}.log", "w", encoding="utf-8")
                self.procs[p["pod"]] = subprocess.Popen(
                    ["kubectl", "-n", NS, "logs", "-f", p["pod"], "-c", "worker", "--tail=-1"],
                    stdout=f, stderr=subprocess.DEVNULL)
                self.files[p["pod"]] = f

    def stop(self):
        for pr in self.procs.values():
            pr.terminate()
        for f in self.files.values():
            f.close()

    def records(self) -> list[dict]:
        out = []
        for pod in self.procs:
            for ln in (RAW / f"{self.name}_{pod}.log").read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.startswith("{"):
                    try:
                        r = json.loads(ln)
                        r["pod"] = pod
                        out.append(r)
                    except ValueError:
                        pass
        return out


def task_summary(tasks: list[dict], results: list[dict]) -> dict:
    per = Counter(r["task_id"] for r in results)
    return {"total": len(tasks), "statuses": dict(Counter(t["status"] for t in tasks)),
            "failed": sum(1 for t in tasks if t["status"] == "failed"),
            "multi_result": sum(1 for c in per.values() if c > 1),
            "attempts_gt1": sum(1 for t in tasks if int(t["attempts"]) > 1),
            "attempts_dist": dict(sorted(Counter(int(t["attempts"]) for t in tasks).items()))}


def cmd_step8(k: K8s, args) -> None:
    ev = Evidence("step8", "Крок 8: коректна зупинка воркерів (kubectl rollout restart)")
    fresh_start(k, ev, R, **DEFAULT_ENV)
    xr = x_r()
    rate = round(0.7 * xr, 1)
    RAW.mkdir(parents=True, exist_ok=True)
    follower = LogFollower(k, "step8")
    follower.follow_new()
    watcher = PodWatcher(k, "step8")
    watcher.start()
    info = {}

    def during(t_a):
        while db_now(k) < t_a + 40:
            time.sleep(0.5)
        info["t_restart"] = db_now(k)
        k.rollout_restart()
        ev.say(f"`kubectl rollout restart deployment/worker` о {iso_utc(info['t_restart'])} "
               "(40-ва секунда прогону)")
        t_end = time.time() + 240
        while time.time() < t_end:
            follower.follow_new()
            pods = k.worker_pods()
            if len([p for p in pods if not p["terminating"] and p["ready"]]) == R and len(pods) == R \
                    and len({p["rs"] for p in pods}) == 1:
                break
            time.sleep(1)
        info["t_restarted"] = db_now(k)
        ev.say(f"rollout завершено о {iso_utc(info['t_restarted'])}: {[p['pod'] for p in k.worker_pods()]}")

    d = steady_run(k, args, ev, "step8", rate, 120, during=during)
    time.sleep(8)
    watcher.stop()
    follower.follow_new()
    time.sleep(2)
    follower.stop()
    recs = follower.records()
    save_logs("step8", recs)
    tasks, results = export_tables(k, "step8")
    exits = sorted(watcher.terminated.values(), key=lambda x: x["finishedAt"] or "")
    returned = [r for r in recs if r.get("event") == "returned"]
    shut = [r for r in recs if r.get("event") in ("shutdown", "exit")]
    s = task_summary(tasks, results)
    ev.table(["Под", "exitCode", "reason", "finishedAt"],
             [[e["pod"], e["exitCode"], e["reason"], e["finishedAt"]] for e in exits])
    ev.block("Рядки журналу воркерів про зупинку (shutdown / exit)", "\n".join(
        json.dumps(r, ensure_ascii=False) for r in shut))
    ev.table(["Величина", "Значення"], [
        ["Контейнерів, що завершились, і їхні коди", ", ".join(f"{e['exitCode']}" for e in exits)],
        ["Задач, повернутих у чергу воркерами під час зупинки", len(returned)],
        ["Задач у статусі failed", s["failed"]],
        ["Задач з більше ніж одним результатом", s["multi_result"]],
        ["Задач з attempts > 1", s["attempts_gt1"]],
        ["Усього задач", s["total"]], ["Розподіл attempts", s["attempts_dist"]]])
    d.update({"rate": rate, "exits": exits, "returned": len(returned), **s, **info})
    ev.data = d
    ev.save()


def cmd_step9(k: K8s, args) -> None:
    ev = Evidence("step9", "Крок 9: FAIL_RATE = 0.15 і сценарій Ф3 (отруєні задачі)")
    xr = x_r()
    rate = round(0.7 * xr, 1)
    out = {}
    for label, poison in (("nopoison", False), ("poison", True)):
        ev.say(f"--- прогін {label}: FAIL_RATE = {FAIL_F}, {rate} задач/с, 300 с ---")
        fresh_start(k, ev, R, **{**DEFAULT_ENV, "FAIL_RATE": str(FAIL_F)})
        ev.say("воркери стартували з 0 подів, тому стовпчик RESTARTS нових подів починається з нуля")
        ev.block("kubectl get pods -l app=worker (початок прогону)", k.get_pods_text())
        info = {}
        watcher = PodWatcher(k, f"step9_{label}")
        watcher.start()

        def during(t_a, poison=poison):
            while db_now(k) < t_a + 150:
                time.sleep(0.5)
            info["t_scenario"] = db_now(k)
            if poison:
                k.psql("INSERT INTO tasks (record_id, seed) SELECT id, 0 FROM trial_participants "
                       "ORDER BY id LIMIT 10")
                ev.say(f"Ф3: о {iso_utc(info['t_scenario'])} (150-та секунда) створено 10 задач з "
                       "seed = 0 (INSERT у tasks; через API seed генерує сервіс, і 0 він не видає)")
            else:
                ev.say(f"150-та секунда ({iso_utc(info['t_scenario'])}): у цьому прогоні отруєних задач немає")

        d = steady_run(k, args, ev, f"step9_{label}", rate, 300, during=during, drain=False)
        ev.say("після генерації чекаємо до 300 с, поки воркери дообслуговують чергу")
        c = wait_drained(k, ev, timeout=300)
        d["counts_drained"] = c
        d["t_to"] = db_now(k)
        watcher.stop()
        d["container_exits"] = dict(Counter(str(e["exitCode"]) for e in watcher.terminated.values()))
        ev.say(f"завершення контейнерів воркера за прогін (kubectl get pods -w): коди виходу "
               f"{d['container_exits']}")
        pods_txt = k.get_pods_text()
        ev.block(f"kubectl get pods -l app=worker ({label}, кінець прогону)", pods_txt)
        pods = k.worker_pods()
        d["restarts"] = {p["pod"]: p["restarts"] for p in pods}
        d["restarts_total"] = sum(p["restarts"] for p in pods)
        time.sleep(8)
        tasks, results = export_tables(k, f"step9_{label}")
        export_prom(k, f"step9_{label}", d["t_from"] - 10, d["t_to"] + 10, {
            "tasks_queued": f'max(tasks_queued{{namespace="{NS}"}})',
            "tasks_running": f'max(tasks_running{{namespace="{NS}"}})',
            "restarts": f'sum(kube_pod_container_status_restarts_total{{namespace="{NS}",container="worker"}})'})
        s = task_summary(tasks, results)
        norm = [t for t in tasks if t["seed"] != "0"]
        fin = [t for t in norm if t["status"] == "done"]
        n = len(norm)
        d.update({"rate": rate, **s, **info,
                  "share_attempts_gt1": sum(1 for t in norm if int(t["attempts"]) > 1) / n if n else None,
                  "share_failed": sum(1 for t in norm if t["status"] == "failed") / n if n else None,
                  "wait_p99_s": percentile([fnum(t["started"]) - fnum(t["created"]) for t in norm if t["started"]], 99),
                  "wait_p50_s": percentile([fnum(t["started"]) - fnum(t["created"]) for t in norm if t["started"]], 50),
                  "X_done_per_s": len(fin) / (d["t_gen_end"] - d["t_from"])})
        if poison:
            pt = [t for t in tasks if t["seed"] == "0"]
            d["poison"] = [{"id": t["id"], "status": t["status"], "attempts": t["attempts"],
                            "created_to_final_s": (fnum(t["finished"]) - fnum(t["created"]))
                            if t["finished"] else None, "worker": t["worker_id"]} for t in pt]
            ev.table(["id", "status", "attempts", "Від створення до failed, с"],
                     [[p["id"], p["status"], p["attempts"],
                       f"{p['created_to_final_s']:.2f}" if p["created_to_final_s"] is not None else "—"]
                      for p in d["poison"]])
        ev.table(["Величина", "Значення"], [
            ["Перезапуски контейнерів worker (RESTARTS, сума)", d["restarts_total"]],
            ["Розподіл задач за attempts", s["attempts_dist"]],
            ["Частка задач з attempts > 1 (без отруєних) / f", f"{d['share_attempts_gt1']:.4f} / {FAIL_F}"],
            ["Частка failed (без отруєних) / f³", f"{d['share_failed']:.5f} / {FAIL_F ** 3:.5f}"],
            ["Задач з більше ніж одним результатом", s["multi_result"]],
            ["Завершено задач за секунду генерації / X(R)", f"{d['X_done_per_s']:.2f} / {xr:.2f}"],
            ["Очікування p50 / p99 (started − created), с",
             f"{d['wait_p50_s']} / {d['wait_p99_s']}"],
            ["Стан черги наприкінці", d["counts_drained"]]])
        out[label] = d
    k.set_worker_env(ev, **DEFAULT_ENV)
    ev.say("FAIL_RATE повернуто в 0")
    ev.data = out
    ev.save()


def crash_timeline(exits: list[dict]) -> dict:
    """Per pod: moments of container exits and the pauses between them (CrashLoopBackOff)."""
    import datetime as dt
    per = defaultdict(list)
    for e in exits:
        per[e["pod"]].append(dt.datetime.fromisoformat(e["finishedAt"].replace("Z", "+00:00")).timestamp())
    return {pod: {"exits": len(ts), "gaps_s": [round(b - a) for a, b in zip(sorted(ts), sorted(ts)[1:])]}
            for pod, ts in sorted(per.items())}


def cmd_step9b(k: K8s, args) -> None:
    """Supplementary step 9 run (the main run is kept as is).

    In the main run (0.7·X(R) for 300 s) every worker crashed after ~1/f ≈ 7 tasks, Kubernetes
    put the pods into CrashLoopBackOff (10, 20, 40, 80, 160 s pauses), the pool stopped and the
    poisoned tasks created at second 150 stayed in the queue behind ~8600 tasks. Here the same
    FAIL_RATE = 0.15 and R workers get a finite load the pool can finish despite the restarts:
    12 tasks every 5 s for 50 s (120 tasks), in the poison run 10 tasks with seed = 0 are added
    at second 25 (the middle of the arrivals). The run lasts until every task reaches a final
    status, so the attempts distribution can be compared with f and f³ and the F3 values exist."""
    ev = Evidence("step9b", "Крок 9 (додатковий прогін): FAIL_RATE = 0.15, скінченне навантаження, Ф3")
    ev.say("основний прогін кроку 9 збережено без змін (step9.md); цей прогін — додатковий, бо в "
           "основному пул зупинився в CrashLoopBackOff і отруєні задачі не дійшли до обробки")
    out = {}
    for label, poison in (("nopoison", False), ("poison", True)):
        ev.say(f"--- прогін {label}: FAIL_RATE = {FAIL_F}, R = {R}, 12 задач кожні 5 с протягом 50 с ---")
        fresh_start(k, ev, R, **{**DEFAULT_ENV, "FAIL_RATE": str(FAIL_F)})
        ev.block(f"kubectl get pods -l app=worker ({label}, початок)", k.get_pods_text())
        watcher = PodWatcher(k, f"step9b_{label}")
        watcher.start()
        t_a = db_now(k)
        d = {"t_from": t_a}
        for i in range(10):
            while db_now(k) < t_a + 5 * i:
                time.sleep(0.2)
            k.psql(SQL_INSERT_N.format(n=12))
            if poison and i == 5:
                d["t_scenario"] = db_now(k)
                k.psql("INSERT INTO tasks (record_id, seed) SELECT id, 0 FROM trial_participants "
                       "ORDER BY id LIMIT 10")
                ev.say(f"Ф3: о {iso_utc(d['t_scenario'])} (25-та секунда) створено 10 задач з seed = 0")
        if not poison:
            d["t_scenario"] = t_a + 25
        ev.say("додано 120 задач (12 кожні 5 с)" + (" + 10 отруєних" if poison else ""))
        c = wait_drained(k, ev, timeout=2400)
        d["t_to"] = db_now(k)
        d["counts_drained"] = c
        watcher.stop()
        exits = parse_pod_watch(watcher.lines)
        d["container_exits"] = dict(Counter(str(e["exitCode"]) for e in exits))
        d["crash_timeline"] = crash_timeline([e for e in exits if e["exitCode"] != 0])
        pods_txt = k.get_pods_text()
        ev.block(f"kubectl get pods -l app=worker ({label}, кінець)", pods_txt)
        pods = k.worker_pods()
        d["restarts"] = {p["pod"]: p["restarts"] for p in pods}
        d["restarts_total"] = sum(p["restarts"] for p in pods)
        ev.say(f"RESTARTS: {d['restarts']} (сума {d['restarts_total']}); завершення контейнерів за "
               f"kubectl get pods -w: коди {d['container_exits']}")
        ev.table(["Под", "Падінь (exitCode 1)", "Паузи між падіннями, с"],
                 [[p, v["exits"], v["gaps_s"]] for p, v in d["crash_timeline"].items()])
        time.sleep(8)
        logs = k.all_worker_logs(iso_utc(t_a - 2))
        save_logs(f"step9b_{label}", logs)
        tasks, results = export_tables(k, f"step9b_{label}")
        export_prom(k, f"step9b_{label}", t_a - 10, d["t_to"] + 10, {
            "tasks_queued": f'max(tasks_queued{{namespace="{NS}"}})',
            "tasks_running": f'max(tasks_running{{namespace="{NS}"}})',
            "restarts": f'sum(kube_pod_container_status_restarts_total{{namespace="{NS}",container="worker"}})'})
        s_ = task_summary(tasks, results)
        norm = [t for t in tasks if t["seed"] != "0"]
        n = len(norm)
        claims_per_done = [int(t["attempts"]) for t in norm if t["status"] == "done"]
        waits = [fnum(t["started"]) - fnum(t["created"]) for t in norm if t["started"]]
        d.update({**s_, "n_normal": n,
                  "share_attempts_gt1": sum(1 for t in norm if int(t["attempts"]) > 1) / n,
                  "share_failed": sum(1 for t in norm if t["status"] == "failed") / n,
                  "mean_attempts_done": statistics.fmean(claims_per_done) if claims_per_done else None,
                  "wait_p50_s": percentile(waits, 50), "wait_p99_s": percentile(waits, 99),
                  "X_done_per_s": sum(1 for t in norm if t["status"] == "done") / (d["t_to"] - t_a),
                  })
        if poison:
            pt = [t for t in tasks if t["seed"] == "0"]
            d["poison"] = [{"id": t["id"], "status": t["status"], "attempts": t["attempts"],
                            "created_to_final_s": (fnum(t["finished"]) - fnum(t["created"]))
                            if t["finished"] else None} for t in pt]
            ev.table(["id", "status", "attempts", "Від створення до failed, с"],
                     [[p["id"], p["status"], p["attempts"], fmt2(p["created_to_final_s"])] for p in d["poison"]])
            ev.block("Журнал воркерів для першої отруєної задачі", "\n".join(
                json.dumps({kk: vv for kk, vv in r.items() if kk != "result"}, ensure_ascii=False)
                for r in sorted(logs, key=lambda r: r.get("ts", ""))
                if str(r.get("task_id")) == str(pt[0]["id"])))
        ev.table(["Величина", "Значення"], [
            ["Звичайних задач", n],
            ["Перезапуски контейнерів worker (RESTARTS, сума)", d["restarts_total"]],
            ["Розподіл задач за attempts", s_["attempts_dist"]],
            ["Частка задач з attempts > 1 (без отруєних) / f", f"{d['share_attempts_gt1']:.4f} / {FAIL_F}"],
            ["Частка failed (без отруєних) / f³", f"{d['share_failed']:.5f} / {FAIL_F ** 3:.5f}"],
            ["Середня кількість захоплень задачі done / 1/(1 − f)",
             f"{fmt2(d['mean_attempts_done'])} / {1 / (1 - FAIL_F):.2f}"],
            ["Задач з більше ніж одним результатом", s_["multi_result"]],
            ["Очікування p50 / p99 (started − created), с", f"{fmt2(d['wait_p50_s'])} / {fmt2(d['wait_p99_s'])}"],
            ["Тривалість прогону до останньої задачі, с", f"{d['t_to'] - t_a:.1f}"],
            ["Стан черги наприкінці", c]])
        out[label] = d
    k.set_worker_env(ev, **DEFAULT_ENV)
    ev.say("FAIL_RATE повернуто в 0")
    ev.data = out
    ev.save()


def cmd_d6(k: K8s, args) -> None:
    ev = Evidence("d6", "Крок 10, Д6: скасування задач (DELETE /tasks/{id})")
    passes = 100
    fresh_start(k, ev, R, **{**DEFAULT_ENV, "PROCESS_PASSES": str(passes)})
    ev.say(f"PROCESS_PASSES = {passes}: задача виконує обробку зрізу {passes} разів, між проходами "
           "воркер перевіряє прапорець cancel_requested")
    for i in rec_ids(args)[:40]:
        request(args.base_url, "POST", f"/trial-participants/{i}/tasks")
    ev.say("створено 40 задач")
    cancelled_running, cancelled_queued, codes = [], [], Counter()
    t_end = time.time() + 600
    while len(cancelled_running) < 20 and time.time() < t_end:
        st, _, js = request(args.base_url, "GET", "/tasks?status=running&limit=10")
        for t in js.get("items", []):
            if t["id"] in cancelled_running or t["cancel_requested"]:
                continue
            time.sleep(0.2)                    # the task is already being processed
            st2, _, js2 = request(args.base_url, "DELETE", f"/tasks/{t['id']}")
            codes[st2] += 1
            if st2 == 202:
                cancelled_running.append(t["id"])
            if len(cancelled_running) >= 20:
                break
        time.sleep(0.2)
    st, _, js = request(args.base_url, "GET", "/tasks?status=queued&limit=5")
    for t in js.get("items", []):
        st2, _, _ = request(args.base_url, "DELETE", f"/tasks/{t['id']}")
        codes[st2] += 1
        if st2 == 200:
            cancelled_queued.append(t["id"])
    ev.say(f"DELETE для {len(cancelled_running)} задач у running (202) і {len(cancelled_queued)} "
           f"у queued (200); коди відповідей {dict(codes)}")
    wait_drained(k, ev, timeout=900)
    time.sleep(8)
    tasks, results = export_tables(k, "d6")
    logs = k.all_worker_logs()
    save_logs("d6", logs)
    per = Counter(r["task_id"] for r in results)
    rows, lat = [], []
    for t in tasks:
        tid = int(t["id"])
        if tid in cancelled_running:
            dl = fnum(t["finished"]) - fnum(t["cancel_requested_at"]) if t["finished"] and t["cancel_requested_at"] else None
            lat.append(dl)
            rows.append([tid, "running", t["status"], per.get(t["id"], 0),
                         f"{dl * 1000:.1f}" if dl is not None else "—"])
        elif tid in cancelled_queued:
            rows.append([tid, "queued", t["status"], per.get(t["id"], 0), "—"])
    ev.table(["id", "Стан під час DELETE", "Статус після", "Результатів", "Від запиту до зупинки, мс"], rows)
    lat_ok = [x for x in lat if x is not None]
    d = {"passes": passes, "cancelled_running": cancelled_running, "cancelled_queued": cancelled_queued,
         "codes": dict(codes), "latency_p50_s": percentile(lat_ok, 50), "latency_p99_s": percentile(lat_ok, 99),
         "latencies_s": lat, "statuses_cancelled": dict(Counter(t["status"] for t in tasks
                                                                if int(t["id"]) in cancelled_running + cancelled_queued)),
         "results_for_cancelled": sum(per.get(str(i), 0) for i in cancelled_running + cancelled_queued),
         "all_statuses": dict(Counter(t["status"] for t in tasks)),
         "cancel_log_lines": sum(1 for r in logs if r.get("event") == "cancelled")}
    ev.say(f"час від запиту скасування до зупинки обробки (cancel_requested_at -> finished_at, "
           f"годинник бази): p50 = {d['latency_p50_s']}, p99 = {d['latency_p99_s']} с; статуси "
           f"скасованих {d['statuses_cancelled']}, результатів у них {d['results_for_cancelled']}")
    k.set_worker_env(ev, **DEFAULT_ENV)
    ev.data = d
    ev.save()


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


STEPS = {"setup": cmd_setup, "api": cmd_api, "step2": cmd_step2, "step3": cmd_step3,
         "step4": cmd_step4, "step5": cmd_step5, "step6": cmd_step6, "step7": cmd_step7,
         "step8": cmd_step8, "step9": cmd_step9, "d6": cmd_d6, "step9b": cmd_step9b}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.stdout = _Tee(sys.stdout, LAB4 / "logs" / "lab4.log")
    print(f"\n#### {now_s()} {' '.join(sys.argv)}")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["all", *STEPS])
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--prom-url", default="http://localhost:9090")
    ap.add_argument("--procs", type=int, default=2)
    args = ap.parse_args()
    k = K8s(args)
    order = list(STEPS) if args.command == "all" else [args.command]
    for c in order:
        print(f"== {c}")
        STEPS[c](k, args)


if __name__ == "__main__":
    main()
