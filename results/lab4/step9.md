# Крок 9: FAIL_RATE = 0.15 і сценарій Ф3 (отруєні задачі)

Запуск: 2026-11-19 19:49:39 (час хоста)

* `2026-11-19 19:49:39` --- прогін nopoison: FAIL_RATE = 0.15, 57.8 задач/с, 300 с ---
* `2026-11-19 19:49:39` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:49:42` готово воркерів: 0 (—), очікування 2.3 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:49:42` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:49:42` `kubectl set env deployment/worker FAIL_RATE=0.15` і rollout status
* `2026-11-19 19:49:43` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 19:49:45` готово воркерів: 4 (worker-7d87b58fb5-gnt8m, worker-7d87b58fb5-phvcb, worker-7d87b58fb5-znqfq, worker-7d87b58fb5-zt7js), очікування 2.3 с
* `2026-11-19 19:49:45` воркери стартували з 0 подів, тому стовпчик RESTARTS нових подів починається з нуля

**kubectl get pods -l app=worker (початок прогону)**

```text
NAME                      READY   STATUS    RESTARTS   AGE   IP           NODE                 NOMINATED NODE   READINESS GATES
worker-7d87b58fb5-gnt8m   1/1     Running   0          2s    10.42.1.54   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-phvcb   1/1     Running   0          2s    10.42.0.59   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-znqfq   1/1     Running   0          2s    10.42.1.55   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-zt7js   1/1     Running   0          2s    10.42.0.58   k3d-trials-agent-0   <none>           <none>
```

* `2026-11-19 19:52:16` 150-та секунда (2026-11-19T18:52:16.045+00:00): у цьому прогоні отруєних задач немає
* `2026-11-19 19:54:47` step9_nopoison: 57.8 задач/с протягом 300 с; генератор: 17341 відповідей, коди {'202': 17341}; кінець о 2026-11-19T18:54:47.373+00:00
* `2026-11-19 19:54:47` на кінець генерації: {'queued': 17130, 'running': 0, 'done': 211, 'failed': 0, 'cancelled': 0, 'total': 17341}
* `2026-11-19 19:54:47` після генерації чекаємо до 300 с, поки воркери дообслуговують чергу
* `2026-11-19 19:59:47` НЕ дочекались порожньої черги за 300 с: {'queued': 17102, 'running': 0, 'done': 239, 'failed': 0, 'cancelled': 0, 'total': 17341}
* `2026-11-19 19:59:47` завершення контейнерів воркера за прогін (kubectl get pods -w): коди виходу {}

**kubectl get pods -l app=worker (nopoison, кінець прогону)**

```text
NAME                      READY   STATUS             RESTARTS        AGE   IP           NODE                 NOMINATED NODE   READINESS GATES
worker-7d87b58fb5-gnt8m   0/1     CrashLoopBackOff   6 (4m6s ago)    10m   10.42.1.54   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-phvcb   0/1     CrashLoopBackOff   6 (4m1s ago)    10m   10.42.0.59   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-znqfq   0/1     CrashLoopBackOff   6 (4m3s ago)    10m   10.42.1.55   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-zt7js   0/1     CrashLoopBackOff   6 (4m13s ago)   10m   10.42.0.58   k3d-trials-agent-0   <none>           <none>
```


| Величина | Значення |
|---|---|
| Перезапуски контейнерів worker (RESTARTS, сума) | 24 |
| Розподіл задач за attempts | {0: 17098, 1: 219, 2: 24} |
| Частка задач з attempts > 1 (без отруєних) / f | 0.0014 / 0.15 |
| Частка failed (без отруєних) / f³ | 0.00000 / 0.00337 |
| Задач з більше ніж одним результатом | 0 |
| Завершено задач за секунду генерації / X(R) | 0.79 / 82.60 |
| Очікування p50 / p99 (started − created), с | 85.84732913970947 / 355.83593487739563 |
| Стан черги наприкінці | {'queued': 17102, 'running': 0, 'done': 239, 'failed': 0, 'cancelled': 0, 'total': 17341} |

* `2026-11-19 19:59:56` --- прогін poison: FAIL_RATE = 0.15, 57.8 задач/с, 300 с ---
* `2026-11-19 19:59:56` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 19:59:57` готово воркерів: 0 (—), очікування 1.2 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 19:59:58` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 19:59:58` змінні воркера вже: CLAIM_MODE=skip_locked, STORE_MODE=idempotent, LEASE_SECONDS=15, POLL_MS=10, FAIL_RATE=0.15, PROCESS_PASSES=1
* `2026-11-19 19:59:58` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 20:00:01` готово воркерів: 4 (worker-7d87b58fb5-24hdd, worker-7d87b58fb5-mg7wr, worker-7d87b58fb5-mrvz4, worker-7d87b58fb5-zxnc5), очікування 2.3 с
* `2026-11-19 20:00:01` воркери стартували з 0 подів, тому стовпчик RESTARTS нових подів починається з нуля

**kubectl get pods -l app=worker (початок прогону)**

```text
NAME                      READY   STATUS    RESTARTS   AGE   IP           NODE                 NOMINATED NODE   READINESS GATES
worker-7d87b58fb5-24hdd   1/1     Running   0          3s    10.42.0.60   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-mg7wr   1/1     Running   0          3s    10.42.1.56   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-mrvz4   1/1     Running   0          3s    10.42.0.61   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-zxnc5   1/1     Running   0          3s    10.42.1.57   k3d-trials-agent-1   <none>           <none>
```

* `2026-11-19 20:02:31` Ф3: о 2026-11-19T19:02:31.681+00:00 (150-та секунда) створено 10 задач з seed = 0 (INSERT у tasks; через API seed генерує сервіс, і 0 він не видає)
* `2026-11-19 20:05:03` step9_poison: 57.8 задач/с протягом 300 с; генератор: 17341 відповідей, коди {'202': 17341}; кінець о 2026-11-19T19:05:03.040+00:00
* `2026-11-19 20:05:03` на кінець генерації: {'queued': 17239, 'running': 0, 'done': 111, 'failed': 1, 'cancelled': 0, 'total': 17351}
* `2026-11-19 20:05:03` після генерації чекаємо до 300 с, поки воркери дообслуговують чергу
* `2026-11-19 20:10:04` НЕ дочекались порожньої черги за 300 с: {'queued': 17229, 'running': 0, 'done': 121, 'failed': 1, 'cancelled': 0, 'total': 17351}
* `2026-11-19 20:10:04` завершення контейнерів воркера за прогін (kubectl get pods -w): коди виходу {}

**kubectl get pods -l app=worker (poison, кінець прогону)**

```text
NAME                      READY   STATUS             RESTARTS        AGE   IP           NODE                 NOMINATED NODE   READINESS GATES
worker-7d87b58fb5-24hdd   0/1     CrashLoopBackOff   6 (4m30s ago)   10m   10.42.0.60   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-mg7wr   0/1     CrashLoopBackOff   6 (4m8s ago)    10m   10.42.1.56   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-mrvz4   0/1     CrashLoopBackOff   6 (4m10s ago)   10m   10.42.0.61   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-zxnc5   0/1     CrashLoopBackOff   6 (4m13s ago)   10m   10.42.1.57   k3d-trials-agent-1   <none>           <none>
```


| id | status | attempts | Від створення до failed, с |
|---|---|---|---|
| 8622 | queued | 0 | — |
| 8623 | queued | 0 | — |
| 8624 | queued | 0 | — |
| 8625 | queued | 0 | — |
| 8626 | queued | 0 | — |
| 8627 | queued | 0 | — |
| 8628 | queued | 0 | — |
| 8629 | queued | 0 | — |
| 8630 | queued | 0 | — |
| 8631 | queued | 0 | — |


| Величина | Значення |
|---|---|
| Перезапуски контейнерів worker (RESTARTS, сума) | 24 |
| Розподіл задач за attempts | {0: 17226, 1: 103, 2: 20, 3: 2} |
| Частка задач з attempts > 1 (без отруєних) / f | 0.0013 / 0.15 |
| Частка failed (без отруєних) / f³ | 0.00006 / 0.00337 |
| Задач з більше ніж одним результатом | 0 |
| Завершено задач за секунду генерації / X(R) | 0.40 / 82.60 |
| Очікування p50 / p99 (started − created), с | 45.67708492279053 / 351.83689999580383 |
| Стан черги наприкінці | {'queued': 17229, 'running': 0, 'done': 121, 'failed': 1, 'cancelled': 0, 'total': 17351} |

* `2026-11-19 20:10:17` `kubectl set env deployment/worker FAIL_RATE=0` і rollout status
* `2026-11-19 20:10:17` FAIL_RATE повернуто в 0
