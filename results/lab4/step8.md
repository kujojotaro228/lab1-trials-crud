# Крок 8: коректна зупинка воркерів (kubectl rollout restart)

Запуск: 2026-11-19 19:47:22 (час хоста)

* `2026-11-19 19:47:22` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:47:24` готово воркерів: 0 (—), очікування 1.2 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:47:24` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:47:24` змінні воркера вже: CLAIM_MODE=skip_locked, STORE_MODE=idempotent, LEASE_SECONDS=15, POLL_MS=10, FAIL_RATE=0, PROCESS_PASSES=1
* `2026-11-19 19:47:24` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 19:47:26` готово воркерів: 4 (worker-7f87c4cfbc-9b6k8, worker-7f87c4cfbc-ck64t, worker-7f87c4cfbc-whqvq, worker-7f87c4cfbc-zp5nw), очікування 1.2 с
* `2026-11-19 19:48:07` `kubectl rollout restart deployment/worker` о 2026-11-19T18:48:06.983+00:00 (40-ва секунда прогону)
* `2026-11-19 19:48:13` rollout завершено о 2026-11-19T18:48:13.639+00:00: ['worker-7cd7cbf497-2w86q', 'worker-7cd7cbf497-drzxw', 'worker-7cd7cbf497-dwnxp', 'worker-7cd7cbf497-x4z8s']
* `2026-11-19 19:49:28` step8: 57.8 задач/с протягом 120 с; генератор: 6936 відповідей, коди {'202': 6936}; кінець о 2026-11-19T18:49:28.205+00:00
* `2026-11-19 19:49:28` на кінець генерації: {'queued': 0, 'running': 0, 'done': 6936, 'failed': 0, 'cancelled': 0, 'total': 6936}
* `2026-11-19 19:49:28` черга порожня: {'queued': 0, 'running': 0, 'done': 6936, 'failed': 0, 'cancelled': 0, 'total': 6936} (очікування 0.2 с)

| Под | exitCode | reason | finishedAt |
|---|---|---|---|


**Рядки журналу воркерів про зупинку (shutdown / exit)**

```text
{"ts": "2026-11-19T18:48:08.213+00:00", "worker": "worker-7f87c4cfbc-9b6k8", "event": "shutdown", "msg": "SIGTERM: нові задачі не захоплюються", "in_flight": 2327, "pod": "worker-7f87c4cfbc-9b6k8"}
{"ts": "2026-11-19T18:48:08.280+00:00", "worker": "worker-7f87c4cfbc-9b6k8", "event": "exit", "code": 0, "pod": "worker-7f87c4cfbc-9b6k8"}
{"ts": "2026-11-19T18:48:11.782+00:00", "worker": "worker-7f87c4cfbc-ck64t", "event": "shutdown", "msg": "SIGTERM: нові задачі не захоплюються", "in_flight": null, "pod": "worker-7f87c4cfbc-ck64t"}
{"ts": "2026-11-19T18:48:11.849+00:00", "worker": "worker-7f87c4cfbc-ck64t", "event": "exit", "code": 0, "pod": "worker-7f87c4cfbc-ck64t"}
{"ts": "2026-11-19T18:48:09.793+00:00", "worker": "worker-7f87c4cfbc-whqvq", "event": "shutdown", "msg": "SIGTERM: нові задачі не захоплюються", "in_flight": 2418, "pod": "worker-7f87c4cfbc-whqvq"}
{"ts": "2026-11-19T18:48:09.858+00:00", "worker": "worker-7f87c4cfbc-whqvq", "event": "exit", "code": 0, "pod": "worker-7f87c4cfbc-whqvq"}
{"ts": "2026-11-19T18:48:11.218+00:00", "worker": "worker-7f87c4cfbc-zp5nw", "event": "shutdown", "msg": "SIGTERM: нові задачі не захоплюються", "in_flight": null, "pod": "worker-7f87c4cfbc-zp5nw"}
{"ts": "2026-11-19T18:48:11.223+00:00", "worker": "worker-7f87c4cfbc-zp5nw", "event": "exit", "code": 0, "pod": "worker-7f87c4cfbc-zp5nw"}
```


| Величина | Значення |
|---|---|
| Контейнерів, що завершились, і їхні коди |  |
| Задач, повернутих у чергу воркерами під час зупинки | 0 |
| Задач у статусі failed | 0 |
| Задач з більше ніж одним результатом | 0 |
| Задач з attempts > 1 | 0 |
| Усього задач | 6936 |
| Розподіл attempts | {1: 6936} |

