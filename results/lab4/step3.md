# Крок 3: наївне захоплення (SELECT, потім окремий UPDATE)

Запуск: 2026-11-19 19:34:11 (час хоста)

* `2026-11-19 19:34:12` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:34:13` готово воркерів: 0 (—), очікування 1.2 с
* `2026-11-19 19:34:13` первинний ключ task_results тимчасово замінено звичайним індексом ix_task_results_task_id (крок 3)

**Обмеження і індекси task_results**

```text
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
ix_task_results_task_id	CREATE INDEX ix_task_results_task_id ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:34:13` --- T_poll = 10 мс ---
* `2026-11-19 19:34:14` `kubectl set env deployment/worker CLAIM_MODE=naive STORE_MODE=plain LEASE_SECONDS=0` і rollout status
* `2026-11-19 19:34:14` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:34:14` готово воркерів: 0 (—), очікування 0.1 с
* `2026-11-19 19:34:14` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:34:14` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 19:34:15` готово воркерів: 4 (worker-685f9c6d57-95fnm, worker-685f9c6d57-c9qhh, worker-685f9c6d57-trrdr, worker-685f9c6d57-vm9zf), очікування 1.2 с
* `2026-11-19 19:34:32` генератор: 300 POST за 3 с (100/с), коди {'202': 300}
* `2026-11-19 19:34:33` черга порожня: {'queued': 0, 'running': 0, 'done': 300, 'failed': 0, 'cancelled': 0, 'total': 300} (очікування 0.9 с)
* `2026-11-19 19:34:42` T_poll = 10 мс: n = 300, рядків у task_results = 335, дублів за таблицею = 33, за журналами (done > 1 разу) = 33, разом n_dup = 33, **D = 0.1100**; приклади задач: [1, 20, 42, 54, 55, 65, 74, 78, 80, 107]
* `2026-11-19 19:34:42` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:34:44` готово воркерів: 0 (—), очікування 1.2 с
* `2026-11-19 19:34:44` --- T_poll = 1 мс ---
* `2026-11-19 19:34:44` `kubectl set env deployment/worker POLL_MS=1` і rollout status
* `2026-11-19 19:34:44` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:34:44` готово воркерів: 0 (—), очікування 0.1 с
* `2026-11-19 19:34:44` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:34:45` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 19:34:46` готово воркерів: 4 (worker-5fc4d5999-2kljr, worker-5fc4d5999-554jd, worker-5fc4d5999-c57s9, worker-5fc4d5999-nhzsh), очікування 1.2 с
* `2026-11-19 19:35:03` генератор: 300 POST за 3 с (100/с), коди {'202': 300}
* `2026-11-19 19:35:03` черга порожня: {'queued': 0, 'running': 0, 'done': 300, 'failed': 0, 'cancelled': 0, 'total': 300} (очікування 0.9 с)
* `2026-11-19 19:35:13` T_poll = 1 мс: n = 300, рядків у task_results = 344, дублів за таблицею = 41, за журналами (done > 1 разу) = 41, разом n_dup = 41, **D = 0.1367**; приклади задач: [1, 2, 9, 14, 25, 33, 35, 44, 49, 51]
* `2026-11-19 19:35:13` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:35:14` готово воркерів: 0 (—), очікування 1.2 с

| T_poll, мс | n | Рядків результатів | n_dup (таблиця) | n_dup (журнали) | n_dup (таблиця або журнали) | **D = n_dup / n** |
|---|---|---|---|---|---|---|
| 10 | 300 | 335 | 33 | 33 | 33 | **0.1100** |
| 1 | 300 | 344 | 41 | 41 | 41 | **0.1367** |


**Рядки task_results задачі 1 (T_poll = 1 мс)**

```text
1	1	worker-5fc4d5999-c57s9	2026-11-19 18:34:59.989019+00
1	2	worker-5fc4d5999-2kljr	2026-11-19 18:34:59.989526+00
1	3	worker-5fc4d5999-nhzsh	2026-11-19 18:34:59.99254+00
1	4	worker-5fc4d5999-554jd	2026-11-19 18:34:59.994314+00
```

