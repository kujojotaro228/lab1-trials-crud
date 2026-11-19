# Крок 9 (додатковий прогін): FAIL_RATE = 0.15, скінченне навантаження, Ф3

Запуск: 2026-11-19 20:28:37 (час хоста)

* `2026-11-19 20:28:37` основний прогін кроку 9 збережено без змін (step9.md); цей прогін — додатковий, бо в основному пул зупинився в CrashLoopBackOff і отруєні задачі не дійшли до обробки
* `2026-11-19 20:28:37` --- прогін nopoison: FAIL_RATE = 0.15, R = 4, 12 задач кожні 5 с протягом 50 с ---
* `2026-11-19 20:28:37` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 20:28:39` готово воркерів: 0 (—), очікування 2.3 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 20:28:40` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 20:28:40` `kubectl set env deployment/worker FAIL_RATE=0.15` і rollout status
* `2026-11-19 20:28:40` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 20:28:43` готово воркерів: 4 (worker-7d87b58fb5-5n75b, worker-7d87b58fb5-76bw4, worker-7d87b58fb5-gd977, worker-7d87b58fb5-wqfv7), очікування 2.3 с

**kubectl get pods -l app=worker (nopoison, початок)**

```text
NAME                      READY   STATUS    RESTARTS   AGE   IP           NODE                 NOMINATED NODE   READINESS GATES
worker-7d87b58fb5-5n75b   1/1     Running   0          3s    10.42.1.65   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-76bw4   1/1     Running   0          3s    10.42.1.64   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-gd977   1/1     Running   0          3s    10.42.0.69   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-wqfv7   1/1     Running   0          3s    10.42.0.68   k3d-trials-agent-0   <none>           <none>
```

* `2026-11-19 20:29:28` додано 120 задач (12 кожні 5 с)
* `2026-11-19 20:30:40` черга порожня: {'queued': 0, 'running': 0, 'done': 120, 'failed': 0, 'cancelled': 0, 'total': 120} (очікування 71.4 с)

**kubectl get pods -l app=worker (nopoison, кінець)**

```text
NAME                      READY   STATUS             RESTARTS      AGE   IP           NODE                 NOMINATED NODE   READINESS GATES
worker-7d87b58fb5-5n75b   1/1     Running            4 (62s ago)   2m    10.42.1.65   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-76bw4   0/1     CrashLoopBackOff   4 (17s ago)   2m    10.42.1.64   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-gd977   0/1     CrashLoopBackOff   4 (28s ago)   2m    10.42.0.69   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-wqfv7   1/1     Running            4 (55s ago)   2m    10.42.0.68   k3d-trials-agent-0   <none>           <none>
```

* `2026-11-19 20:30:40` RESTARTS: {'worker-7d87b58fb5-5n75b': 4, 'worker-7d87b58fb5-76bw4': 4, 'worker-7d87b58fb5-gd977': 4, 'worker-7d87b58fb5-wqfv7': 4} (сума 16); завершення контейнерів за kubectl get pods -w: коди {'1': 18}

| Под | Падінь (exitCode 1) | Паузи між падіннями, с |
|---|---|---|
| worker-7d87b58fb5-5n75b | 4 | [5, 17, 28] |
| worker-7d87b58fb5-76bw4 | 5 | [6, 16, 25, 43] |
| worker-7d87b58fb5-gd977 | 5 | [5, 14, 29, 41] |
| worker-7d87b58fb5-wqfv7 | 4 | [11, 15, 31] |


| Величина | Значення |
|---|---|
| Звичайних задач | 120 |
| Перезапуски контейнерів worker (RESTARTS, сума) | 16 |
| Розподіл задач за attempts | {1: 106, 2: 10, 3: 4} |
| Частка задач з attempts > 1 (без отруєних) / f | 0.1167 / 0.15 |
| Частка failed (без отруєних) / f³ | 0.00000 / 0.00337 |
| Середня кількість захоплень задачі done / 1/(1 − f) | 1.15 / 1.18 |
| Задач з більше ніж одним результатом | 0 |
| Очікування p50 / p99 (started − created), с | 26.27 / 69.07 |
| Тривалість прогону до останньої задачі, с | 116.8 |
| Стан черги наприкінці | {'queued': 0, 'running': 0, 'done': 120, 'failed': 0, 'cancelled': 0, 'total': 120} |

* `2026-11-19 20:30:49` --- прогін poison: FAIL_RATE = 0.15, R = 4, 12 задач кожні 5 с протягом 50 с ---
* `2026-11-19 20:30:49` `kubectl scale deployment/worker --replicas=0`
* `2026-11-19 20:30:50` готово воркерів: 0 (—), очікування 1.2 с

**Обмеження і індекси task_results**

```text
task_results_pkey	PRIMARY KEY (task_id)
task_results_task_id_fkey	FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
task_results_pkey	CREATE UNIQUE INDEX task_results_pkey ON public.task_results USING btree (task_id)
```

* `2026-11-19 20:30:51` `TRUNCATE task_results, tasks RESTART IDENTITY` — таблиці черги порожні
* `2026-11-19 20:30:51` змінні воркера вже: CLAIM_MODE=skip_locked, STORE_MODE=idempotent, LEASE_SECONDS=15, POLL_MS=10, FAIL_RATE=0.15, PROCESS_PASSES=1
* `2026-11-19 20:30:51` `kubectl scale deployment/worker --replicas=4`
* `2026-11-19 20:30:52` готово воркерів: 4 (worker-7d87b58fb5-4j85r, worker-7d87b58fb5-cvt5g, worker-7d87b58fb5-ftqmx, worker-7d87b58fb5-wxsbq), очікування 1.2 с

**kubectl get pods -l app=worker (poison, початок)**

```text
NAME                      READY   STATUS    RESTARTS   AGE   IP           NODE                 NOMINATED NODE   READINESS GATES
worker-7d87b58fb5-4j85r   1/1     Running   0          1s    10.42.0.71   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-cvt5g   1/1     Running   0          1s    10.42.1.67   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-ftqmx   1/1     Running   0          1s    10.42.1.66   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-wxsbq   1/1     Running   0          1s    10.42.0.70   k3d-trials-agent-0   <none>           <none>
```

* `2026-11-19 20:31:18` Ф3: о 2026-11-19T19:31:18.765+00:00 (25-та секунда) створено 10 задач з seed = 0
* `2026-11-19 20:31:38` додано 120 задач (12 кожні 5 с) + 10 отруєних
* `2026-11-19 20:34:19` черга порожня: {'queued': 0, 'running': 0, 'done': 120, 'failed': 10, 'cancelled': 0, 'total': 130} (очікування 161.4 с)

**kubectl get pods -l app=worker (poison, кінець)**

```text
NAME                      READY   STATUS             RESTARTS       AGE     IP           NODE                 NOMINATED NODE   READINESS GATES
worker-7d87b58fb5-4j85r   1/1     Running            5 (91s ago)    3m29s   10.42.0.71   k3d-trials-agent-0   <none>           <none>
worker-7d87b58fb5-cvt5g   0/1     CrashLoopBackOff   5 (19s ago)    3m29s   10.42.1.67   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-ftqmx   1/1     Running            5 (96s ago)    3m29s   10.42.1.66   k3d-trials-agent-1   <none>           <none>
worker-7d87b58fb5-wxsbq   1/1     Running            5 (100s ago)   3m29s   10.42.0.70   k3d-trials-agent-0   <none>           <none>
```

* `2026-11-19 20:34:20` RESTARTS: {'worker-7d87b58fb5-4j85r': 5, 'worker-7d87b58fb5-cvt5g': 5, 'worker-7d87b58fb5-ftqmx': 5, 'worker-7d87b58fb5-wxsbq': 5} (сума 20); завершення контейнерів за kubectl get pods -w: коди {'1': 21}

| Под | Падінь (exitCode 1) | Паузи між падіннями, с |
|---|---|---|
| worker-7d87b58fb5-4j85r | 5 | [10, 16, 28, 52] |
| worker-7d87b58fb5-cvt5g | 6 | [15, 12, 30, 42, 89] |
| worker-7d87b58fb5-ftqmx | 5 | [10, 17, 27, 42] |
| worker-7d87b58fb5-wxsbq | 5 | [5, 20, 28, 54] |


| id | status | attempts | Від створення до failed, с |
|---|---|---|---|
| 73 | failed | 3 | 9.96 |
| 74 | failed | 3 | 10.00 |
| 75 | failed | 3 | 10.02 |
| 76 | failed | 3 | 10.03 |
| 77 | failed | 3 | 10.04 |
| 78 | failed | 3 | 10.06 |
| 79 | failed | 3 | 10.07 |
| 80 | failed | 3 | 10.08 |
| 81 | failed | 3 | 10.10 |
| 82 | failed | 3 | 10.11 |


**Журнал воркерів для першої отруєної задачі**

```text

```


| Величина | Значення |
|---|---|
| Звичайних задач | 120 |
| Перезапуски контейнерів worker (RESTARTS, сума) | 20 |
| Розподіл задач за attempts | {1: 102, 2: 15, 3: 13} |
| Частка задач з attempts > 1 (без отруєних) / f | 0.1500 / 0.15 |
| Частка failed (без отруєних) / f³ | 0.00000 / 0.00337 |
| Середня кількість захоплень задачі done / 1/(1 − f) | 1.18 / 1.18 |
| Задач з більше ніж одним результатом | 0 |
| Очікування p50 / p99 (started − created), с | 1.78 / 147.63 |
| Тривалість прогону до останньої задачі, с | 206.9 |
| Стан черги наприкінці | {'queued': 0, 'running': 0, 'done': 120, 'failed': 10, 'cancelled': 0, 'total': 130} |

* `2026-11-19 20:34:32` `kubectl set env deployment/worker FAIL_RATE=0` і rollout status
* `2026-11-19 20:34:32` FAIL_RATE повернуто в 0
