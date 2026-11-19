# Крок 7: закон Літтла для черги

Запуск: 2026-11-19 19:41:18 (час хоста)

* `2026-11-19 19:41:18` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:41:19` готово воркерів: 0 (—), очікування 1.2 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:41:20` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:41:20` змінні воркера вже: CLAIM_MODE=skip_locked, STORE_MODE=idempotent, LEASE_SECONDS=15, POLL_MS=10, FAIL_RATE=0, PROCESS_PASSES=1
* `2026-11-19 19:41:20` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 19:41:22` готово воркерів: 4 (worker-7f87c4cfbc-4cnlz, worker-7f87c4cfbc-96788, worker-7f87c4cfbc-tgc4m, worker-7f87c4cfbc-zzk4r), очікування 2.3 с
* `2026-11-19 19:44:24` step7_low: 57.8 задач/с протягом 180 с; генератор: 10404 відповідей, коди {'202': 10404}; кінець о 2026-11-19T18:44:24.410+00:00
* `2026-11-19 19:44:24` на кінець генерації: {'queued': 0, 'running': 0, 'done': 10404, 'failed': 0, 'cancelled': 0, 'total': 10404}
* `2026-11-19 19:44:24` черга порожня: {'queued': 0, 'running': 0, 'done': 10404, 'failed': 0, 'cancelled': 0, 'total': 10404} (очікування 0.2 с)
* `2026-11-19 19:44:33` вікно останніх 120 с: λ = 57.792 задач/с, W_q = 4.40 мс, λ·W_q = 0.2542, L_q (середнє tasks_queued) = 0.14583333333333331, розбіжність 42.640624998778975
* `2026-11-19 19:44:33` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:44:34` готово воркерів: 0 (—), очікування 1.2 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:44:35` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:44:35` змінні воркера вже: CLAIM_MODE=skip_locked, STORE_MODE=idempotent, LEASE_SECONDS=15, POLL_MS=10, FAIL_RATE=0, PROCESS_PASSES=1
* `2026-11-19 19:44:35` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 19:44:36` готово воркерів: 4 (worker-7f87c4cfbc-49d9g, worker-7f87c4cfbc-wg2qz, worker-7f87c4cfbc-xl5v4, worker-7f87c4cfbc-zx5jg), очікування 1.2 с
* `2026-11-19 19:46:38` step7_high: 107.4 задач/с протягом 120 с; генератор: 12888 відповідей, коди {'202': 12888}; кінець о 2026-11-19T18:46:38.732+00:00
* `2026-11-19 19:46:38` на кінець генерації: {'queued': 2879, 'running': 4, 'done': 10005, 'failed': 0, 'cancelled': 0, 'total': 12888}
* `2026-11-19 19:47:14` черга порожня: {'queued': 0, 'running': 0, 'done': 12888, 'failed': 0, 'cancelled': 0, 'total': 12888} (очікування 35.1 с)
* `2026-11-19 19:47:22` 1.3·X(R): у черзі на кінець генерації 2879; W_q перших 100 = 0.114 с, останніх 100 = 34.960 с
