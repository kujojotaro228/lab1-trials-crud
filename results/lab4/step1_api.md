# Крок 1: ендпоінти задач і вимірювачі

Запуск: 2026-11-19 19:33:43 (час хоста)

* `2026-11-19 19:33:43` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:33:45` готово воркерів: 0 (—), очікування 1.2 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:33:45` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:33:45` змінні воркера вже: CLAIM_MODE=skip_locked, STORE_MODE=idempotent, LEASE_SECONDS=15, POLL_MS=10, FAIL_RATE=0, PROCESS_PASSES=1
* `2026-11-19 19:33:45` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:33:46` готово воркерів: 0 (—), очікування 0.1 с

| Запит | Очікувано | Отримано |  | Тіло (початок) |
|---|---|---|---|---|
| `POST /trial-participants/1/tasks` | 202 | 202 | ✅ | `{"id": 1, "record_id": 1, "status": "queued", "seed": 639106670, "attempts": 0, "max_attempts": 3, "token": 0, "lease_until": null, "worker_id": null, "created_` |
| `POST /trial-participants/999999/tasks` | 404 | 404 | ✅ | `{"error": "not_found", "message": "Учасника з id=999999 не знайдено", "details": []}` |
| `GET /tasks/1` | 200 | 200 | ✅ | `{"id": 1, "record_id": 1, "status": "queued", "seed": 639106670, "attempts": 0, "max_attempts": 3, "token": 0, "lease_until": null, "worker_id": null, "created_` |
| `GET /tasks/999999` | 404 | 404 | ✅ | `{"error": "not_found", "message": "Задачу з id=999999 не знайдено", "details": []}` |
| `GET /tasks?status=queued&limit=2&offset=1` | 200 | 200 | ✅ | `{"items": [{"id": 2, "record_id": 2, "status": "queued", "seed": 640923411, "attempts": 0, "max_attempts": 3, "token": 0, "lease_until": null, "worker_id": null` |
| `GET /tasks?status=bad` | 400 | 400 | ✅ | `{"error": "validation_error", "message": "Невалідні дані запиту", "details": [{"field": "status", "location": "query", "message": "фільтр має бути одним із знач` |


**tasks_queued / tasks_running у Prometheus (по репліці web)**

```text
tasks_queued{pod=web-6444ff675d-tx9nm} = 5
tasks_queued{pod=web-6444ff675d-d5drl} = 5
tasks_running{pod=web-6444ff675d-tx9nm} = 0
tasks_running{pod=web-6444ff675d-d5drl} = 0
```

* `2026-11-19 19:33:58` для порівняння — стан таблиці tasks у той самий момент: {'queued': 5, 'running': 0, 'done': 0, 'failed': 0, 'cancelled': 0, 'total': 5} (вимірювачі оновлюються кожні 5 с, Prometheus збирає кожні 5 с)
* `2026-11-19 19:33:58` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
