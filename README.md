# CRUD-сервіс «Учасник клінічного дослідження» (ЛР 1, варіант 25)

Python 3.12 · FastAPI · asyncpg · PostgreSQL 16 · nginx · Docker Compose

## Запуск однією командою

```bash
cp .env.example .env              # Windows PowerShell: Copy-Item .env.example .env
# відредагуйте пароль у .env
docker compose up -d --build
```

Стенд доступний на `http://localhost:8080` (порт задається `LB_PORT` у `.env`).
Два вебінстанси: `docker compose up -d --scale web=2`.

Звіт — [REPORT.md](REPORT.md).

---

## ЛР 2: метрики і межа масштабованості

Звіт — [REPORT2.md](REPORT2.md). Стенд доповнено Prometheus (`http://localhost:9090/targets`).

```powershell
docker compose up -d --build
py loadtest/seed.py
py loadtest/lab2.py all          # межа генератора G, серія 1..4 екземпляри, коректна зупинка
py loadtest/lab2_analyze.py      # results/lab2/analysis.md і діаграми results/lab2/charts/
```

| Що | Де |
|---|---|
| Метрики Prometheus, М3 (`cpu.stat`) | `app/metrics.py`, `GET /metrics` |
| Живучість без бази | `GET /livez` |
| Коректна зупинка за SIGTERM (код 0) | `app/serve.py`, `CMD ["python", "-m", "app.serve"]` |
| Конфігурація збору | `prometheus/prometheus.yml` (`dns_sd_configs`, 5 с) |
| Ступінчастий генератор | `loadtest/stepgen.py` |
| Оркестратор і аналіз | `loadtest/lab2.py`, `loadtest/lab2_analyze.py` |
| Стенд без Docker (емуляція) | `scripts/emu_stand.py`, `emu_run.py`, `emu_dns.py` |

---

## ЛР 3: Kubernetes (k3d) з автомасштабуванням і без нього

Звіт — [REPORT3.md](REPORT3.md). Потрібні Docker Desktop, k3d, kubectl, helm.

```powershell
docker compose down                                              # звільнити 8080/9090
powershell -ExecutionPolicy Bypass -File scripts\k8s\deploy.ps1   # кластер, образ, база, web, моніторинг
py loadtest/seed.py
py loadtest/lab3.py all                                          # перевірки і всі прогони
py loadtest/lab3_analyze.py                                      # results/lab3/analysis.md, charts/
powershell -ExecutionPolicy Bypass -File scripts\k8s\destroy.ps1  # видалити кластер
```

Web — `http://localhost:8080`, Prometheus — `http://localhost:9090`, Ingress (Traefik) — `http://localhost:8081`.

---

## Етап 1. Проєктування

### Таблиця `trial_participants`

| Поле | Тип | Обовʼязкове | Обмеження / індекс | Чому такий тип |
|---|---|---|---|---|
| `id` | `BIGINT GENERATED ALWAYS AS IDENTITY` | так (генерує база) | `PRIMARY KEY` | Ідентифікатор видає база, клієнт його передати не може (`ALWAYS`). BIGINT — без ризику переповнення |
| `participant_code` | `VARCHAR(32)` | так | `UNIQUE` (`uq_trial_participants_code`) | Код учасника в дослідженні (напр. `TP-0001`); унікальний, бо один учасник — один запис. Обмеження довжини захищає від сміттєвих даних |
| `protocol` | `VARCHAR(64)` | так | — | Шифр протоколу дослідження (напр. `CT-2025-001`) |
| `randomization_group` | `VARCHAR(16)` | так | `CHECK IN ('A','B','контроль')`; індекс `(randomization_group, id)` | Правило варіанта. Рядок, а не ENUM: перелік легко змінити міграцією. Індекс обслуговує фільтр переліку і сортування за `id` для пагінації |
| `enrollment_date` | `DATE` | так | — | Дата без часу: важливий день включення, а не момент |
| `status` | `VARCHAR(16)` | так | `CHECK IN ('screening','active','completed','withdrawn')` | Скінченний набір станів учасника |
| `completion_date` | `DATE` | ні (`NULL`) | `CHECK (completion_date IS NULL OR completion_date >= enrollment_date)` | Поле обовʼязкове у складі сутності, але значення зʼявляється лише після завершення/вибуття; до того — `null` |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | так (заповнює база/сервіс) | `DEFAULT now()` | Службові поля аудиту; з часовою зоною, щоб не залежати від налаштувань сервера |

Схема: [`migrations/001_create_trial_participants.sql`](migrations/001_create_trial_participants.sql).
Валідація виконується **у сервісі** (400 з поясненням); `CHECK` у базі — другий рубіж.

### API

Формат помилки для всіх 4xx/503:

```json
{"error": "validation_error", "message": "Невалідні дані запиту",
 "details": [{"field": "randomization_group", "location": "body",
              "message": "група рандомізації має бути одним із значень: A, B, контроль; отримано 'C'"}]}
```

Тіло запиту POST/PUT (PUT — повна заміна, усі поля; не передане `completion_date` стає `null`):

```json
{"participant_code": "TP-0001", "protocol": "CT-2025-001", "randomization_group": "A",
 "enrollment_date": "2025-03-01", "status": "active", "completion_date": null}
```

Тіло запису у відповіді:

```json
{"id": 1, "participant_code": "TP-0001", "protocol": "CT-2025-001", "randomization_group": "A",
 "enrollment_date": "2025-03-01", "status": "active", "completion_date": null,
 "created_at": "2025-10-13T18:00:00.123456+00:00", "updated_at": "2025-10-13T18:00:00.123456+00:00"}
```

| Метод | Шлях | Тіло запиту | Успіх | Помилки |
|---|---|---|---|---|
| POST | `/trial-participants` | запис без `id` | **201** + запис, заголовок `Location` | **400** невалідні дані / некоректний JSON / зайве поле; **409** `participant_code` вже існує; **503** база недоступна |
| GET | `/trial-participants?randomization_group=&limit=&offset=` | — | **200** `{"items":[...], "total":N, "limit":L, "offset":O}` | **400** фільтр не з переліку, `limit` поза 1..100, `offset` < 0; **503** |
| GET | `/trial-participants/{id}` | — | **200** + запис | **400** `id` не ціле додатне; **404** немає запису; **503** |
| PUT | `/trial-participants/{id}` | повний запис без `id` | **200** + оновлений запис | **400**; **404**; **409** код зайнятий іншим записом; **503** |
| DELETE | `/trial-participants/{id}` | — | **204** без тіла | **400**; **404** (зокрема при повторному видаленні); **503** |
| GET | `/healthz` | — | **200** `{"status":"ok","instance":"<host>"}` | **503** `{"status":"unavailable","reason":"database unreachable" \| "schema not migrated"}` |

Пагінація: `limit` за замовчуванням **20**, верхня межа **100** (`LIST_DEFAULT_LIMIT`, `LIST_MAX_LIMIT`);
більше значення — 400. Сортування за `id`. Без фільтра повертаються всі записи з урахуванням пагінації.

`/healthz` виконує запит до бази (`to_regclass('public.trial_participants')`), тобто перевіряє і
доступність бази, і те, що міграція вже накотила схему.

## Структура репозиторію

```
app/                  код сервісу (main.py, schemas.py, db.py, config.py) і міграції (migrate.py)
migrations/           SQL-схема, яку накочує контейнер migrate
nginx/nginx.conf      балансувальник
Dockerfile            багатоетапна збірка, фінальна стадія python:3.12-alpine
docker-compose.yml    db + migrate + web + lb (+ loadgen у профілі tools)
loadtest/             генератор навантаження і скрипти серій/розрахунків (лише stdlib)
scripts/              скрипти, яким потрібен docker: старт, докази, збереження даних, розподіл
results/              сирі результати прогонів і підсумкові таблиці
```
