# ЛР 4: стан кластера перед вимірюваннями

Запуск: 2026-11-19 19:33:42 (час хоста)

* `2026-11-19 19:33:42` `kubectl scale deployment/web --replicas=2` — воркери звертаються до web за записами

**kubectl get nodes**

```text
NAME                  STATUS   ROLES                  AGE   VERSION        INTERNAL-IP   EXTERNAL-IP   OS-IMAGE           KERNEL-VERSION                      CONTAINER-RUNTIME
k3d-trials-agent-0    Ready    <none>                 20d   v1.31.4+k3s1   172.18.0.2    <none>        K3s v1.31.4+k3s1   6.18.58.1-microsoft-standard-WSL2   containerd://1.7.23-k3s2
k3d-trials-agent-1    Ready    <none>                 20d   v1.31.4+k3s1   172.18.0.3    <none>        K3s v1.31.4+k3s1   6.18.58.1-microsoft-standard-WSL2   containerd://1.7.23-k3s2
k3d-trials-server-0   Ready    control-plane,master   20d   v1.31.4+k3s1   172.18.0.5    <none>        K3s v1.31.4+k3s1   6.18.58.1-microsoft-standard-WSL2   containerd://1.7.23-k3s2
```


**kubectl get pods -n trials -o wide**

```text
NAME                                                       READY   STATUS      RESTARTS        AGE     IP           NODE                  NOMINATED NODE   READINESS GATES
db-0                                                       1/1     Running     1 (3m46s ago)   5d16h   10.42.2.28   k3d-trials-server-0   <none>           <none>
migrate-hhnnq                                              0/1     Completed   0               88s     10.42.0.28   k3d-trials-agent-0    <none>           <none>
pg-exporter-prometheus-postgres-exporter-969d4d47d-dw7vb   1/1     Running     0               65s     10.42.2.34   k3d-trials-server-0   <none>           <none>
web-6444ff675d-d5drl                                       1/1     Running     0               84s     10.42.1.28   k3d-trials-agent-1    <none>           <none>
web-6444ff675d-tx9nm                                       1/1     Running     0               78s     10.42.0.30   k3d-trials-agent-0    <none>           <none>
worker-7f87c4cfbc-4v7nt                                    1/1     Running     0               59s     10.42.0.32   k3d-trials-agent-0    <none>           <none>
```


**kubectl get deployment worker -o yaml (resources, env)**

```text
{"limits":{"cpu":"500m","memory":"512Mi"},"requests":{"cpu":"250m","memory":"128Mi"}}
30
DB_PASSWORD=
WORKER_ID=metadata.name
WEB_URL=http://web:8000
CLAIM_MODE=skip_locked
STORE_MODE=idempotent
POLL_MS=10
LEASE_SECONDS=15
FAIL_RATE=0
PROCESS_PASSES=1
Q_LO=0.02
Q_HI=0.98
SLICE_SIZE=256
SHUTDOWN_DEADLINE=25
```


**Міграції**

```text
001_create_trial_participants.sql	2026-10-29 19:23:40.780694+00
002_create_tasks.sql	2026-11-19 18:32:16.488917+00
```


**\d tasks**

```text
                                         Table "public.tasks"
       Column        |           Type           | Collation | Nullable |           Default            
---------------------+--------------------------+-----------+----------+------------------------------
 id                  | bigint                   |           | not null | generated always as identity
 record_id           | bigint                   |           | not null | 
 status              | character varying(16)    |           | not null | 'queued'::character varying
 seed                | bigint                   |           | not null | 
 attempts            | integer                  |           | not null | 0
 max_attempts        | integer                  |           | not null | 3
 token               | bigint                   |           | not null | 0
 lease_until         | timestamp with time zone |           |          | 
 worker_id           | character varying(128)   |           |          | 
 created_at          | timestamp with time zone |           | not null | clock_timestamp()
 started_at          | timestamp with time zone |           |          | 
 finished_at         | timestamp with time zone |           |          | 
 cancel_requested    | boolean                  |           | not null | false
 cancel_requested_at | timestamp with time zone |           |          | 
Indexes:
    "tasks_pkey" PRIMARY KEY, btree (id)
    "ix_tasks_status_created" btree (status, created_at)
Check constraints:
    "ck_tasks_seed" CHECK (seed >= 0 AND seed < '2147483648'::bigint)
    "ck_tasks_status" CHECK (status::text = ANY (ARRAY['queued'::character varying, 'running'::character varying, 'done'::character varying, 'failed'::character varying, 'cancelled'::character varying]::text[]))
Foreign-key constraints:
    "tasks_record_id_fkey" FOREIGN KEY (record_id) REFERENCES trial_participants(id) ON DELETE CASCADE
Referenced by:
    TABLE "task_results" CONSTRAINT "task_results_task_id_fkey" FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
```


**\d task_results**

```text
                           Table "public.task_results"
   Column   |           Type           | Collation | Nullable |      Default      
------------+--------------------------+-----------+----------+-------------------
 task_id    | bigint                   |           | not null | 
 token      | bigint                   |           | not null | 
 worker_id  | character varying(128)   |           | not null | 
 result     | jsonb                    |           | not null | 
 created_at | timestamp with time zone |           | not null | clock_timestamp()
Indexes:
    "task_results_pkey" PRIMARY KEY, btree (task_id)
Foreign-key constraints:
    "task_results_task_id_fkey" FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
```


**Цілі Prometheus у просторі trials (UP)**

```text
trials/web: 2
pg-exporter-prometheus-postgres-exporter: 1
trials/worker: 1
```

