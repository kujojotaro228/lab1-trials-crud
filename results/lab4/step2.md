# Крок 2: 20 задач на одному воркері, t_io і t_cpu

Запуск: 2026-11-19 19:33:58 (час хоста)

* `2026-11-19 19:33:58` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:33:58` готово воркерів: 0 (—), очікування 0.1 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:33:59` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:33:59` змінні воркера вже: CLAIM_MODE=skip_locked, STORE_MODE=idempotent, LEASE_SECONDS=15, POLL_MS=10, FAIL_RATE=0, PROCESS_PASSES=1
* `2026-11-19 19:33:59` `kubectl scale deployment/worker --replicas=1`
* `2026-11-19 19:34:00` готово воркерів: 1 (worker-7f87c4cfbc-7x4vx), очікування 1.2 с
* `2026-11-19 19:34:01` створено 20 задач (20 запитів POST /trial-participants/{id}/tasks)
* `2026-11-19 19:34:02` черга порожня: {'queued': 0, 'running': 0, 'done': 20, 'failed': 0, 'cancelled': 0, 'total': 20} (очікування 1.6 с)
* `2026-11-19 19:34:11` t_io і t_cpu взято з гістограми воркера worker_task_duration_seconds (sum/count з /metrics пода)

| Крок | Гістограма воркера (/metrics), мс | count | Prometheus increase, мс | Журнал воркера, мс (n) |
|---|---|---|---|---|
| fetch (GET запису з web) | 1.65 | 20 | 1.60 | 1.65 (20) |
| process (обробка зрізу 256×256) | 33.54 | 20 | 34.12 | 33.54 (20) |
| store (транзакція завершення + результат) | 7.75 | 20 | 7.14 | 7.75 (20) |
| claim (захоплення; у гістограмі — разом із порожніми) | 1.18 | 848 | 1.25 | 2.65 (20) |


| Величина | мс |
|---|---|
| **t_io = fetch + store** | **9.40** |
| **t_cpu = process** | **33.54** |


**Приклад рядка журналу воркера (done)**

```text
{"ts": "2026-11-19T18:34:01.433+00:00", "worker": "worker-7f87c4cfbc-7x4vx", "event": "done", "task_id": 1, "token": 1, "attempts": 1, "fetch_ms": 4.049, "process_ms": 44.166, "store_ms": 4.096, "result": {"p_lo": 80, "p_hi": 4012, "excluded": 2592, "kept": 62944, "mean": 2045.7818, "std": 1177.6432, "q_lo": 0.02, "q_hi": 0.98, "size": 256}, "pod": "worker-7f87c4cfbc-7x4vx"}
```

