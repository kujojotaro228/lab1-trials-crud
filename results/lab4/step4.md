# Крок 4: FOR UPDATE SKIP LOCKED і FOR UPDATE без SKIP LOCKED

Запуск: 2026-11-19 19:35:14 (час хоста)

* `2026-11-19 19:35:14` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:35:14` готово воркерів: 0 (—), очікування 0.1 с
* `2026-11-19 19:35:15` первинний ключ task_results(task_id) повернуто (таблиці очищено)

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:35:15` --- CLAIM_MODE = skip_locked, T_poll = 1 мс ---
* `2026-11-19 19:35:15` `kubectl set env deployment/worker CLAIM_MODE=skip_locked` і rollout status
* `2026-11-19 19:35:16` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:35:16` готово воркерів: 0 (—), очікування 0.1 с
* `2026-11-19 19:35:16` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:35:16` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 19:35:18` готово воркерів: 4 (worker-65bd9685d-c58kj, worker-65bd9685d-g75d7, worker-65bd9685d-gq4pp, worker-65bd9685d-mhc6l), очікування 2.3 с
* `2026-11-19 19:35:36` генератор: 300 POST за 3 с (100/с), коди {'202': 300}
* `2026-11-19 19:35:37` черга порожня: {'queued': 0, 'running': 0, 'done': 300, 'failed': 0, 'cancelled': 0, 'total': 300} (очікування 0.9 с)
* `2026-11-19 19:35:47` skip_locked: з /metrics подів напряму (різниця знімків до і після серії): порожніх захоплень 5930, захоплень задачі 300, p99 захоплення за гістограмою 2.48 мс
* `2026-11-19 19:35:47` skip_locked: D = 0.0000 (таблиця 0, журнали 0), помилок дубля ключа 0; p99 захоплення (гістограма) = 0.002413617034267181; порожніх захоплень = 24170.03395310234; макс. рядків Lock = 0 за 8 знімків
* `2026-11-19 19:35:47` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:35:48` готово воркерів: 0 (—), очікування 1.2 с
* `2026-11-19 19:35:48` --- CLAIM_MODE = for_update, T_poll = 1 мс ---
* `2026-11-19 19:35:48` `kubectl set env deployment/worker CLAIM_MODE=for_update` і rollout status
* `2026-11-19 19:35:49` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:35:49` готово воркерів: 0 (—), очікування 0.1 с
* `2026-11-19 19:35:49` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:35:49` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 19:35:51` готово воркерів: 4 (worker-5c7c5d49bc-4p5wl, worker-5c7c5d49bc-c5t2t, worker-5c7c5d49bc-jj52c, worker-5c7c5d49bc-wmk2d), очікування 2.3 с
* `2026-11-19 19:36:09` генератор: 300 POST за 3 с (100/с), коди {'202': 300}
* `2026-11-19 19:36:10` черга порожня: {'queued': 0, 'running': 0, 'done': 300, 'failed': 0, 'cancelled': 0, 'total': 300} (очікування 0.9 с)
* `2026-11-19 19:36:20` for_update: з /metrics подів напряму (різниця знімків до і після серії): порожніх захоплень 6189, захоплень задачі 300, p99 захоплення за гістограмою 2.47 мс
* `2026-11-19 19:36:20` for_update: D = 0.0000 (таблиця 0, журнали 0), помилок дубля ключа 0; p99 захоплення (гістограма) = 0.002404137083560894; порожніх захоплень = 24510.6; макс. рядків Lock = 0 за 8 знімків
* `2026-11-19 19:36:20` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:36:21` готово воркерів: 0 (—), очікування 1.2 с

| Захоплення | n | n_dup | D | p99 захоплення, мс (Prometheus / /metrics подів) | Порожніх захоплень (Prometheus / /metrics) | Захоплень задачі (Prometheus / /metrics) | Макс. рядків Lock у pg_stat_activity | Макс. pg_locks granted = false | Знімків |
|---|---|---|---|---|---|---|---|---|---|
| skip_locked | 300 | 0 | 0.0000 | 2.41 / 2.48 | 24170.03 / 5930 | 373.48 / 300 | 0 | 0 | 8 |
| for_update | 300 | 0 | 0.0000 | 2.40 / 2.47 | 24510.60 / 6189 | 374.97 / 300 | 0 | 0 | 8 |

* `2026-11-19 19:36:21` p99 захоплення і лічильники — гістограма worker_task_duration_seconds{step="claim"} і лічильник worker_claims_total воркерів: через Prometheus (increase за вікно серії) і різницею знімків /metrics подів від старту генератора до порожньої черги
* `2026-11-19 19:36:22` `kubectl set env deployment/worker CLAIM_MODE=skip_locked STORE_MODE=idempotent LEASE_SECONDS=15 POLL_MS=10` і rollout status
* `2026-11-19 19:36:22` для наступних кроків повернуто SKIP LOCKED, T_poll = 10 мс, lease 15 с, ON CONFLICT
