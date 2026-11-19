# Крок 5: lease 15 с, heartbeat 5 с, fencing token, kill -STOP

Запуск: 2026-11-19 19:36:22 (час хоста)

* `2026-11-19 19:36:22` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:36:22` готово воркерів: 0 (—), очікування 0.1 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:36:22` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:36:22` змінні воркера вже: CLAIM_MODE=skip_locked, STORE_MODE=idempotent, LEASE_SECONDS=15, POLL_MS=10, FAIL_RATE=0, PROCESS_PASSES=1
* `2026-11-19 19:36:23` `kubectl scale deployment/worker --replicas=2`
* `2026-11-19 19:36:25` готово воркерів: 2 (worker-7f87c4cfbc-smtwz, worker-7f87c4cfbc-whk8h), очікування 2.3 с

**Процеси в контейнері worker-7f87c4cfbc-smtwz (PID 1 — tini, воркер — дочірній процес)**

```text
PID 1: /sbin/tini -- python worker.py State:	S (sleeping)
PID 7: python worker.py State:	R (running)
```

* `2026-11-19 19:36:25` демонстрація: додано 1000 задач (INSERT … generate_series), щоб воркери були зайняті
* `2026-11-19 19:36:27` `kubectl exec worker-7f87c4cfbc-smtwz -- sh -c 'kill -STOP 1'`

**Стан процесів одразу після kill -STOP 1**

```text
PID 1: /sbin/tini -- python worker.py State:	S (sleeping)
PID 7: python worker.py State:	S (sleeping)
```

* `2026-11-19 19:36:30` за 3 с після `kill -STOP 1` воркер worker-7f87c4cfbc-smtwz записав ще 80 результатів: сигнал STOP до PID 1 свого простору імен PID ядро не доставляє (init захищений від сигналів зсередини простору імен), тому процес не зупинився. Далі зупиняємо сам процес воркера за PID з /tmp/worker.pid — це той самий ефект «завислого» воркера, який мала дати команда із завдання
* `2026-11-19 19:36:48` черга порожня: {'queued': 0, 'running': 0, 'done': 1000, 'failed': 0, 'cancelled': 0, 'total': 1000} (очікування 18.0 с)
* `2026-11-19 19:36:49` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:36:49` створено 100 задач (один INSERT, щоб на момент зупинки черга ще не спорожніла)
* `2026-11-19 19:36:50` `kubectl exec worker-7f87c4cfbc-smtwz -- sh -c 'kill -STOP $(cat /tmp/worker.pid)'` о 2026-11-19T18:36:50.036+00:00 (годинник бази): у worker-7f87c4cfbc-smtwz в обробці задача 19, attempts = 1, token = 1, lease_until = 2026-11-19T18:37:04.840+00:00

**Стан процесів воркера A перед kill -CONT (State: T — зупинений)**

```text
PID 1: /sbin/tini -- python worker.py State:	S (sleeping)
PID 7: python worker.py State:	T (stopped)
```

* `2026-11-19 19:37:20` `kubectl exec worker-7f87c4cfbc-smtwz -- sh -c 'kill -CONT $(cat /tmp/worker.pid)'` о 2026-11-19T18:37:20.927+00:00 (через 30.9 с = 2·L); на цей момент задача 19: status = done, attempts = 2, token = 2, worker_id = worker-7f87c4cfbc-whk8h
* `2026-11-19 19:37:21` черга порожня: {'queued': 0, 'running': 0, 'done': 100, 'failed': 0, 'cancelled': 0, 'total': 100} (очікування 0.2 с)

| Величина | Значення |
|---|---|
| Задача в обробці на момент зупинки | 19 |
| attempts / token до зупинки | 1 / 1 |
| attempts / token після | 2 / 2 |
| Статус після | done |
| Воркер, що завершив | worker-7f87c4cfbc-whk8h |
| Рядків у task_results для задачі | 1 |
| Рядків журналу зупиненого воркера про нуль оновлених рядків | 2 |


**Рядки task_results для задачі 19**

```text
19	2	worker-7f87c4cfbc-whk8h	2026-11-19 18:37:08.089426+00
```


**Журнал воркерів для задачі 19**

```text
{"ts": "2026-11-19T18:36:49.841+00:00", "worker": "worker-7f87c4cfbc-smtwz", "event": "claim", "task_id": 19, "token": 1, "attempts": 1, "claim_ms": 1.598, "pod": "worker-7f87c4cfbc-smtwz"}
{"ts": "2026-11-19T18:37:08.066+00:00", "worker": "worker-7f87c4cfbc-whk8h", "event": "claim", "task_id": 19, "token": 2, "attempts": 2, "claim_ms": 2.187, "pod": "worker-7f87c4cfbc-whk8h"}
{"ts": "2026-11-19T18:37:08.091+00:00", "worker": "worker-7f87c4cfbc-whk8h", "event": "done", "task_id": 19, "token": 2, "attempts": 2, "fetch_ms": 1.322, "process_ms": 19.858, "store_ms": 3.595, "pod": "worker-7f87c4cfbc-whk8h"}
{"ts": "2026-11-19T18:37:20.770+00:00", "worker": "worker-7f87c4cfbc-smtwz", "event": "zero_rows", "op": "heartbeat", "task_id": 19, "token": 1, "msg": "heartbeat оновив 0 рядків: задача вже не належить цьому воркеру", "pod": "worker-7f87c4cfbc-smtwz"}
{"ts": "2026-11-19T18:37:20.774+00:00", "worker": "worker-7f87c4cfbc-smtwz", "event": "zero_rows", "op": "store", "task_id": 19, "token": 1, "msg": "завершення оновило 0 рядків: токен застарів, результат не записано", "pod": "worker-7f87c4cfbc-smtwz"}
```

