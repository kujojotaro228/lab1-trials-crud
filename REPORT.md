<div align="center">



<br>

# Лабораторна робота №1
## «Проєктування і розгортання CRUD-сервісу»

Дисципліна: «Високопродуктивні розподілені обчислювальні системи»

**Варіант №25**

</div>

<br>

| | |
|---|---|
| **Виконав:** | студент Тарасенко І.О. |

<div align="center">

Київ — 2026

</div>

---

## Зміст

- [Параметри варіанта 25](#параметри-варіанта-25)
- [Інструкція запуску](#інструкція-запуску)
- [Тестовий стенд](#тестовий-стенд)
- [Етап 1. Проєктування](#етап-1-проєктування)
- [Етап 2. Сервіс](#етап-2-сервіс)
- [Етап 3. Образ](#етап-3-образ)
- [Етап 4. Стенд](#етап-4-стенд)
- [Етапи 5–6. Навантаження](#етапи-56-навантаження)
- [Етап 7. Розрахунки](#етап-7-розрахунки)
- [Етап 8. Додаткова вимога варіанта](#етап-8-додаткова-вимога-варіанта)
- [Етап 9. Перевірка](#етап-9-перевірка)
- [11. Відкинуті рішення](#11-відкинуті-рішення)
- [12. Використання генеративних моделей](#12-використання-генеративних-моделей)
- [Відповіді на контрольні питання](#відповіді-на-контрольні-питання)

---

## Параметри варіанта 25

| Параметр | Значення |
|---|---|
| Сутність і шлях | Учасник клінічного дослідження, `/trial-participants` |
| Обовʼязкові поля | код учасника, протокол, група рандомізації, дата включення, статус, дата завершення |
| Фільтр у переліку | за групою рандомізації (`?randomization_group=`) |
| Правило валідації | група тільки з переліку `A`, `B`, `контроль` |
| Базовий образ фінальної стадії | `python:3.12-alpine` |
| Ліміти на контейнер сервісу | 0.5 CPU, 512 МБ |
| Додаткова вимога | схема накочується окремим разовим контейнером міграції, а не файлом у теці ініціалізації бази |

## Інструкція запуску

```bash
cp .env.example .env                  # задати POSTGRES_PASSWORD
docker compose up -d --build          # стенд з 1 вебінстансом — одна команда
docker compose up -d --scale web=2    # 2 вебінстанси, без правок у файлах
```

Стенд доступний на `http://localhost:8080` (єдиний опублікований порт — балансувальник).

## Тестовий стенд

| Параметр | Значення |
|---|---|
| Процесор | 12th Gen Intel® Core™ i5-12450H, 8 ядер / 12 потоків |
| RAM | 15.7 ГБ |
| ОС | Windows 11 Pro |
| Docker | Docker Desktop (WSL 2), Engine 29.8.2, Compose v5.6.0 |
| Ресурси, доступні Docker | 12 CPU, 7.6 ГБ RAM (8 173 436 928 байт) |
| Генератор навантаження | з хоста: Windows, PowerShell, Python 3.12.3 |

Генератор навантаження і стенд працюють **на одній машині** — на верхніх рівнях інтенсивності
вони конкурують за ядра процесора.

<details>
<summary>Команди, якими зібрано ці дані</summary>

```powershell
PS> Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors
Name                                     NumberOfCores NumberOfLogicalProcessors
----                                     ------------- -------------------------
12th Gen Intel(R) Core(TM) i5-12450H                 8                        12

PS> [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
15,7

PS> (Get-CimInstance Win32_OperatingSystem).Caption
Майкрософт Windows 11 Pro

PS> docker version --format "Docker Engine {{.Server.Version}}"
Docker Engine 29.8.2

PS> docker compose version
Docker Compose version v5.6.0

PS> docker info --format "CPU для Docker: {{.NCPU}}, RAM для Docker: {{.MemTotal}} байт"
CPU для Docker: 12, RAM для Docker: 8173436928 байт
```

</details>

---

## Етап 1. Проєктування

### 1. Схема таблиці

```sql
-- Схема сутності «Учасник клінічного дослідження» (варіант 25).
-- Накочується разовим контейнером migrate (python -m app.migrate),
-- а НЕ через /docker-entrypoint-initdb.d.

CREATE TABLE trial_participants (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    participant_code    VARCHAR(32)  NOT NULL,
    protocol            VARCHAR(64)  NOT NULL,
    randomization_group VARCHAR(16)  NOT NULL,
    enrollment_date     DATE         NOT NULL,
    status              VARCHAR(16)  NOT NULL,
    completion_date     DATE         NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_trial_participants_code
        UNIQUE (participant_code),
    CONSTRAINT ck_trial_participants_group
        CHECK (randomization_group IN ('A', 'B', 'контроль')),
    CONSTRAINT ck_trial_participants_status
        CHECK (status IN ('screening', 'active', 'completed', 'withdrawn')),
    CONSTRAINT ck_trial_participants_dates
        CHECK (completion_date IS NULL OR completion_date >= enrollment_date)
);

-- Фільтр переліку за групою + стабільна пагінація ORDER BY id
CREATE INDEX ix_trial_participants_group_id
    ON trial_participants (randomization_group, id);
```

| Поле | Тип | Обовʼязковість | Обмеження / індекс | Чому такий тип |
|---|---|---|---|---|
| `id` | `BIGINT GENERATED ALWAYS AS IDENTITY` | генерує база | `PRIMARY KEY` | Ідентифікатор видає база; `ALWAYS` забороняє клієнту підставити свій. BIGINT — запас без переповнення |
| `participant_code` | `VARCHAR(32)` | NOT NULL | `UNIQUE` | Код учасника — природний ключ, дубль неприпустимий (409). Обмеження довжини відсікає сміття |
| `protocol` | `VARCHAR(64)` | NOT NULL | — | Шифр протоколу дослідження |
| `randomization_group` | `VARCHAR(16)` | NOT NULL | `CHECK IN ('A','B','контроль')`, індекс `(randomization_group, id)` | Правило варіанта. VARCHAR + CHECK, а не ENUM — перелік змінюється простою міграцією. Складений індекс обслуговує фільтр і `ORDER BY id` для пагінації одним проходом |
| `enrollment_date` | `DATE` | NOT NULL | — | Важливий день, а не момент часу |
| `status` | `VARCHAR(16)` | NOT NULL | `CHECK IN ('screening','active','completed','withdrawn')` | Скінченний набір станів |
| `completion_date` | `DATE` | NULL | `CHECK (completion_date IS NULL OR completion_date >= enrollment_date)` | Поле входить до сутності, але значення зʼявляється лише після завершення/вибуття учасника |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | NOT NULL, `DEFAULT now()` | — | Службові поля аудиту; з часовою зоною |

Окремий індекс на `participant_code` не потрібен — його створює `UNIQUE`. Первинний ключ обслуговує
`GET` / `PUT` / `DELETE /{id}`.

### 2. Спроєктоване API

Формат помилки (усі 4xx і 503):

```json
{"error": "<код>", "message": "<текст>", "details": [{"field": "...", "location": "...", "message": "..."}]}
```

Тіло запиту POST і PUT:

```json
{"participant_code": "TP-0001", "protocol": "CT-2025-001", "randomization_group": "A",
 "enrollment_date": "2025-03-01", "status": "active", "completion_date": null}
```

Тіло запису у відповіді — ті самі поля плюс `id`, `created_at`, `updated_at`.

| Ендпоінт | Метод | Тіло запиту | Тіло відповіді | Успіх | Помилки |
|---|---|---|---|---|---|
| `/trial-participants` | POST | запис без `id` | створений запис, заголовок `Location` | **201** | **400** — невалідні дані (група не з переліку, відсутнє поле, дата не в форматі, `completion_date` < `enrollment_date`, зайве поле, некоректний JSON); **409** — `participant_code` вже існує; **503** — база недоступна |
| `/trial-participants?randomization_group=&limit=&offset=` | GET | — | `{"items": [...], "total": N, "limit": L, "offset": O}` | **200** | **400** — фільтр не з переліку, `limit` ∉ [1; 100], `offset` < 0; **503** |
| `/trial-participants/{id}` | GET | — | запис | **200** | **400** — `id` не ціле додатне число; **404** — запису немає; **503** |
| `/trial-participants/{id}` | PUT | повний запис без `id` | оновлений запис | **200** | **400**; **404**; **409** — код належить іншому запису; **503** |
| `/trial-participants/{id}` | DELETE | — | — | **204** | **400**; **404** — запису немає (у т.ч. повторне видалення); **503** |
| `/healthz` | GET | — | `{"status": "ok", "instance": "<host>"}` | **200** | **503** — `{"status": "unavailable", "reason": "database unreachable" / "schema not migrated"}` |

`limit` за замовчуванням **20**, верхня межа **100** (більше — 400, а не мовчазне обрізання, щоб
клієнт бачив, що отримав не те, що просив). Сортування за `id`. Запит без фільтра повертає всі
записи з урахуванням пагінації.

---

## Етап 2. Сервіс

- Python 3.12, FastAPI, драйвер asyncpg з пулом підключень (2–10 на інстанс), uvicorn з
  uvloop/httptools, один процес на контейнер (ліміт 0.5 CPU — кілька воркерів лише конкурували б
  за ту саму пів-ядра).
- Валідація — у сервісі (модель pydantic + власні валідатори з українськими повідомленнями);
  стандартну відповідь FastAPI 422 перевизначено на **400** з полем і причиною. Помилки бази не
  виходять назовні як 500: дубль коду → 409, порушення CHECK (якщо раптом) → 400, недоступність
  бази → 503.
- Сервіс чекає на базу: при старті — до 30 с з експоненційною затримкою; якщо база зникла пізніше,
  пул перепідключається, а запити отримують 503, а не обрив зʼєднання.
- Стан у памʼяті процесу не тримається; єдине «службове» — заголовок `X-Instance` з іменем
  контейнера, обчислюваний з hostname (потрібен для підтвердження балансування).

**Перевірка локально з базою в контейнері.** Сервіс запущено на хості
(`uvicorn app.main:app --port 8000`), PostgreSQL — у контейнері
(`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db migrate`). Повний цикл
API пройдено скриптом `loadtest/verify_cycle.py` — 19 з 19 кроків дали очікуваний код (журнал —
у розділі 9). Той самий скрипт проти стенда в Docker (`http://localhost:8080`):

```text
PS> py loadtest/verify_cycle.py
 1. GET    /healthz                                              -> 200 (expect 200) OK
 2. POST   /trial-participants                                   -> 201 (expect 201) OK
 3. POST   /trial-participants                                   -> 400 (expect 400) OK
 4. POST   /trial-participants                                   -> 400 (expect 400) OK
 5. POST   /trial-participants                                   -> 400 (expect 400) OK
 6. POST   /trial-participants                                   -> 409 (expect 409) OK
 7. GET    /trial-participants?randomization_group=A&limit=100   -> 200 (expect 200) OK
 8. GET    /trial-participants?randomization_group=D             -> 400 (expect 400) OK
 9. GET    /trial-participants?limit=1000                        -> 400 (expect 400) OK
10. GET    /trial-participants/1                                 -> 200 (expect 200) OK
11. PUT    /trial-participants/1                                 -> 200 (expect 200) OK
12. PUT    /trial-participants/1                                 -> 400 (expect 400) OK
13. GET    /trial-participants/1                                 -> 200 (expect 200) OK
14. GET    /trial-participants?randomization_group=контроль&limit=5 -> 200 (expect 200) OK
15. DELETE /trial-participants/1                                 -> 204 (expect 204) OK
16. DELETE /trial-participants/1                                 -> 404 (expect 404) OK
17. GET    /trial-participants/1                                 -> 404 (expect 404) OK
18. PUT    /trial-participants/1                                 -> 404 (expect 404) OK
19. GET    /trial-participants/abc                               -> 400 (expect 400) OK
```

---

## Етап 3. Образ

### 3а. Dockerfile

```dockerfile
# ---------------------------------------------------------------------------
# Стадія 1 (builder): встановлення залежностей у віртуальне середовище.
# Компілятори й заголовки потрібні лише тут: якщо для якогось пакета немає
# готового musllinux-колеса, pip збере його з сирців. У фінальний образ
# потрапляє тільки готове /opt/venv.
# ---------------------------------------------------------------------------
FROM python:3.12-alpine AS builder

RUN apk add --no-cache build-base linux-headers libffi-dev

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# Спершу лише requirements.txt: шар із залежностями кешується і не
# перезбирається, коли змінюється тільки код сервісу.
COPY requirements.txt .
RUN pip install -r requirements.txt


# ---------------------------------------------------------------------------
# Стадія 2 (runtime): тільки інтерпретатор, venv і код. Без компіляторів.
# ---------------------------------------------------------------------------
FROM python:3.12-alpine AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN addgroup -S -g 10001 app \
 && adduser -S -D -H -u 10001 -G app app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY migrations ./migrations
RUN python -m compileall -q /app/app

USER app
EXPOSE 8000

# За замовчуванням — вебсервіс. Контейнер міграції запускає той самий образ
# з іншою командою: python -m app.migrate
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--loop", "uvloop", "--http", "httptools", \
     "--no-access-log", "--timeout-keep-alive", "75", "--backlog", "4096"]
```

Збірка багатоетапна: компілятори (`build-base`, `linux-headers`) встановлені лише в стадії
`builder`; у фінальну стадію копіюється тільки готове віртуальне середовище `/opt/venv`.
Залежності встановлюються під час збірки, а не під час старту контейнера. `requirements.txt`
копіюється окремо від коду, тож при зміні коду шар із залежностями береться з кешу. Процес працює
від непривілейованого користувача `app` (uid 10001).

Перевірка фінального образу (`py scripts/collect_evidence.py` → `results/evidence.md`) — компілятора
немає, процес працює не від root:

```text
$ docker run --rm --entrypoint sh trials-web -c 'command -v gcc; id; python --version'
gcc: немає
cc: немає
uid=10001(app) gid=10001(app) groups=10001(app),10001(app)
Python 3.12.15
```

Шари фінального образу (`docker history trials-web`, верхня частина) — у ньому лише venv (55.4 MB),
код і базовий `python:3.12-alpine`, шарів з `build-base` немає:

```text
IMAGE          CREATED BY                                      SIZE
4178f013107b   CMD ["uvicorn" "app.main:app" "--host" "0.0.…   0B
<missing>      EXPOSE [8000/tcp]                               0B
<missing>      USER app                                        0B
<missing>      RUN /bin/sh -c python -m compileall -q /app/…   61.4kB
<missing>      COPY migrations ./migrations # buildkit         16.4kB
<missing>      COPY app ./app # buildkit                       45.1kB
<missing>      COPY /opt/venv /opt/venv # buildkit             55.4MB
<missing>      WORKDIR /app                                    8.19kB
<missing>      RUN /bin/sh -c addgroup -S -g 10001 app  && …   32.8kB
<missing>      ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFER…   0B
               ... далі шари базового образу python:3.12-alpine
```

### 3б. .dockerignore

```text
# У контекст збірки потрапляють лише requirements.txt, app/ і migrations/.
# Насамперед виключено .env — пароль не має потрапити в образ.
.env
.env.*
.git
.gitignore
.idea
.vscode
**/__pycache__
**/*.pyc
.venv
venv

results
loadtest
scripts
nginx

*.md
Dockerfile
.dockerignore
docker-compose*.yml
```

### 4. Розмір образу і час до першої відповіді

**Розмір образу** вебсервісу `trials-web`: **156.6 MB** (149.4 MiB, 156 645 903 байт).

```text
$ docker image ls trials-web
IMAGE               ID             DISK USAGE   CONTENT SIZE   EXTRA
trials-web:latest   4178f013107b   157MB        38.7MB         U
```

(`DISK USAGE` — розпакований образ на диску, `CONTENT SIZE` — стиснуті шари, які передаються
при `pull`/`push`.)

**Час від `docker compose up -d` до першої відповіді 200 на `/healthz`** — образи зібрані, томи
порожні (перед кожним повтором `docker compose down -v`), команда `py scripts/measure_startup.py`:

| Повтор | До 200 на `/healthz`, с | `up -d` завершився, с |
|---:|---:|---:|
| 1 | 7.26 | 7.26 |
| 2 | 6.73 | 6.73 |
| 3 | 6.75 | 6.75 |
| **Медіана** | **6.75** | |

Складові часу старту: створення мережі й тому → `initdb` PostgreSQL на порожньому томі →
healthcheck бази → разовий контейнер міграції → старт uvicorn і перший healthcheck web → старт
nginx. Команда `up -d` повертає керування лише після старту балансувальника (він залежить від
`web: service_healthy`), тому обидва часи збігаються.

---

## Етап 4. Стенд

### 3в. docker-compose.yml

```yaml
# Стенд: база (db) + разова міграція (migrate) + вебінстанс (web) + балансувальник (lb).
#
#   docker compose up -d --build              # один вебінстанс
#   docker compose up -d --scale web=2        # два вебінстанси, без правок у файлах
#
# Пароль до бази береться з .env (POSTGRES_PASSWORD); у цьому файлі його немає.
name: trials

x-db-env: &db-env
  DB_HOST: db
  DB_PORT: "5432"
  DB_NAME: ${POSTGRES_DB:-trials}
  DB_USER: ${POSTGRES_USER:-trials}
  DB_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-trials}
      POSTGRES_USER: ${POSTGRES_USER:-trials}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
    volumes:
      - pgdata:/var/lib/postgresql/data      # том під дані; теки initdb.d немає
    healthcheck:
      # -h 127.0.0.1: перевірка по TCP. Під час initdb тимчасовий сервер
      # слухає лише unix-сокет, тому "healthy" настає тільки для справжнього сервера.
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U \"$${POSTGRES_USER}\" -d \"$${POSTGRES_DB}\""]
      interval: 5s
      timeout: 3s
      retries: 30
      start_period: 30s
      start_interval: 1s
    networks: [backend]
    restart: unless-stopped

  # Додаткова вимога варіанта 25: схема накочується окремим разовим контейнером.
  migrate:
    build: .
    command: ["python", "-m", "app.migrate"]
    environment: *db-env
    depends_on:
      db:
        condition: service_healthy
    networks: [backend]
    restart: "no"

  web:
    build: .
    environment:
      <<: *db-env
      DB_POOL_MIN: "2"
      DB_POOL_MAX: "10"
      LIST_DEFAULT_LIMIT: "20"
      LIST_MAX_LIMIT: "100"
    expose:
      - "8000"                                # порт НЕ публікується на хост
    depends_on:
      db:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "-T", "2", "http://127.0.0.1:8000/healthz"]
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 30s
      start_interval: 1s
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 512M
    ulimits:
      nofile: { soft: 65535, hard: 65535 }
    networks: [backend, frontend]
    restart: unless-stopped

  lb:
    image: nginx:1.28-alpine
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
    ports:
      - "${LB_PORT:-8080}:80"                 # єдиний порт, опублікований на хост
    depends_on:
      web:
        condition: service_healthy
    ulimits:
      nofile: { soft: 65535, hard: 65535 }
    networks: [frontend]
    restart: unless-stopped

  # Не частина стенда: генератор навантаження в контейнері (запасний варіант,
  # якщо з хоста генератор не дотягує до потрібної інтенсивності).
  # Запускається лише явно: docker compose run --rm loadgen loadtest/run_series.py ...
  loadgen:
    image: python:3.12-alpine
    profiles: ["tools"]
    working_dir: /work
    entrypoint: ["python", "-u"]
    volumes:
      - ./loadtest:/work/loadtest:ro
      - ./results:/work/results
    ulimits:
      nofile: { soft: 65535, hard: 65535 }
    networks: [frontend]

networks:
  frontend: {}
  backend: {}

volumes:
  pgdata: {}
```

### Конфігурація балансувальника (`nginx/nginx.conf`)

```nginx
worker_processes auto;
worker_rlimit_nofile 65535;
error_log /dev/stderr warn;

events {
    worker_connections 16384;
    multi_accept on;
}

http {
    # У лог пишеться адреса інстансу, який обслужив запит ($upstream_addr):
    # з нього видно, як запити розходяться між репліками.
    log_format upstreamlog '$time_iso8601 $remote_addr "$request" $status '
                           'rt=$request_time upstream=$upstream_addr '
                           'urt=$upstream_response_time';
    access_log /var/log/nginx/access.log upstreamlog buffer=64k flush=1s;

    sendfile          on;
    tcp_nodelay       on;
    keepalive_timeout 75s;
    keepalive_requests 100000;

    # Вбудований DNS Docker. Імʼя "web" резолвиться в адреси ВСІХ реплік,
    # перевіряється кожні 5 с, тож --scale web=N підхоплюється без перезапуску.
    resolver 127.0.0.11 valid=5s ipv6=off;

    upstream api {
        zone api 64k;
        server web:8000 resolve;     # round-robin між усіма адресами "web"
        keepalive 256;               # постійні зʼєднання до інстансів
        keepalive_timeout 60s;       # менше, ніж --timeout-keep-alive 75 в uvicorn
        keepalive_requests 100000;
    }

    server {
        listen 80;

        # Ендпоінт, що нічого не робить: для перевірки самого генератора
        location = /noop {
            access_log off;
            default_type text/plain;
            return 200 "ok\n";
        }

        location / {
            proxy_pass http://api;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_connect_timeout 3s;
            proxy_send_timeout 120s;
            proxy_read_timeout 120s;
            add_header X-Upstream $upstream_addr always;
        }
    }
}
```

### .env.example

```dotenv
# Скопіюйте цей файл у .env і задайте власний пароль.
# Файл .env у репозиторій не комітиться (див. .gitignore).
POSTGRES_DB=trials
POSTGRES_USER=trials
POSTGRES_PASSWORD=change-me-to-a-long-random-string

# Порт балансувальника на хості (єдиний опублікований порт стенда)
LB_PORT=8080
```

### Що реалізовано

- **Мережі:** `frontend` (lb ↔ web) і `backend` (web/migrate ↔ db). Назовні опублікований лише порт
  балансувальника; `web` має тільки `expose: 8000`, `db` портів не публікує. Вебінстанси
  звертаються до бази за іменем сервісу `db` через вбудований DNS Docker.
- **Том** `pgdata` під дані бази.
- **Пароль** — лише в `.env` (не комітиться), у compose — тільки посилання
  `${POSTGRES_PASSWORD:?...}`; без пароля стенд не підніметься з явним повідомленням. Перевірку,
  що пароля немає в образі, історії збірки і compose-файлі, наведено в `results/evidence.md`.
- **Healthcheck і порядок старту:** `db` (pg_isready по TCP) → `migrate`
  (`service_completed_successfully`) → `web` (healthcheck на `/healthz`) → `lb` (`service_healthy`).
- **Ліміти:** `deploy.resources.limits: cpus 0.5, memory 512M` для `web`; підтвердження
  (`NanoCpus=500000000`, `Memory=536870912`) — нижче і в `results/evidence.md`.
- **Кількість інстансів** — параметр запуску `--scale web=N`; nginx резолвить імʼя `web` у всі
  адреси реплік і перевіряє DNS кожні 5 с (`server web:8000 resolve`), тож правок у файлах не потрібно.

**Підтвердження лімітів** (стенд з `--scale web=2`; `NanoCpus=500000000` — це 0.5 CPU,
`Memory=536870912` — 512 MiB):

```text
$ docker inspect --format '{{.Name}} NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}' <web>
/trials-web-1 NanoCpus=500000000 Memory=536870912
/trials-web-2 NanoCpus=500000000 Memory=536870912

$ docker stats --no-stream
NAME           CPU %     MEM USAGE / LIMIT
trials-web-1   0.35%     70.61MiB / 512MiB
trials-web-2   0.29%     52.83MiB / 512MiB
```

**Перевірка пароля** (скрипт порівнює значення `POSTGRES_PASSWORD` з `.env` з вмістом артефактів):

- ✅ пароль відсутній в історії збірки (`docker history --no-trunc`)
- ✅ пароль відсутній у змінних середовища образу
- ✅ пароль відсутній у `docker-compose.yml`
- ✅ пароль відсутній у `Dockerfile`
- ✅ у змінних середовища образу немає `DB_PASSWORD` / `POSTGRES_PASSWORD`
- ✅ файл `.env` не відстежується git

```text
$ docker image inspect trials-web --format '{{json .Config.Env}}'
["PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
 "LANG=C.UTF-8","GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305","PYTHON_VERSION=3.12.15",
 "PYTHON_SHA256=c2c4321961fab0fb999d66e0cecf521c2ab3994c7992873ea99e306c1094fd5a",
 "PYTHONDONTWRITEBYTECODE=1","PYTHONUNBUFFERED=1"]
```

---

## Етапи 5–6. Навантаження

### Сценарій і команда запуску

Генератор — `loadtest/loadgen.py` (стандартна бібліотека Python). Відкритий контур:

- задана інтенсивність R ділиться між P процесами (P = кількість логічних ядер);
- процес *i* відправляє запити в моменти `t0 + i/R + k·P/R` — обчислені **наперед** від початку
  прогону, незалежно від відповідей;
- запит віддається пулу потоків, тому цикл планування не чекає відповіді; у кожного потоку своє
  keep-alive зʼєднання;
- затримка рахується від **запланованого** моменту до отримання відповіді (якщо генератор
  запізнився з відправкою, це теж входить у затримку);
- досягнута інтенсивність = кількість відповідей 2xx, що прийшли у вікні вимірювання, / 30 с;
- запит: `GET /trial-participants/{id}`, `id` випадково з 100 записів, занесених `seed.py`;
- 10 с прогріву (не враховується) + 30 с вимірювання, 3 повтори на рівень, у таблицю — медіана;
  тайм-аут запиту 30 с (тайм-аут = не-2xx).

Команди:

```powershell
py loadtest/seed.py                                           # 100 записів
py loadtest/run_series.py --instances 1                       # 10, 100, 500 rps × 3
py loadtest/run_series.py --instances 1 --rates 1000 1500     # додаткові рівні
docker compose up -d --scale web=2
py loadtest/run_series.py --instances 2                       # 10, 100, 500 rps × 3
py loadtest/run_series.py --instances 2 --rates 1000 1500     # додаткові рівні
```

Генератор запускався з хоста (Windows, PowerShell, Python 3.12.3), звертаючись до балансувальника
через опублікований порт `http://localhost:8080`. Генератор і стенд працюють на одній машині й на
верхніх рівнях конкурують за ядра процесора.

### Перевірка генератора на ендпоінті, що нічого не робить

Команда `py loadtest/check_generator.py` — генератор проти `/noop` (nginx `return 200`), без сервісу
і бази. Python 3.12.3, Windows-11-10.0.26200-SP0, логічних CPU: 12. Прогін 10 с + прогрів 3 с.

| Задана, rps | Процесів | Відправлено, rps | Досягнута (2xx), rps | p50, мс | p99, мс | Запізнення відправки p99, мс | не-2xx, % |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 10 | 10.0 | 10.0 | 1.512 | 2.647 | 0.952 | 0.0 |
| 100 | 12 | 100.0 | 100.0 | 1.322 | 2.092 | 0.819 | 0.0 |
| 500 | 12 | 500.0 | 499.9 | 0.891 | 1.344 | 0.697 | 0.0 |
| 1000 | 12 | 1000.0 | 999.9 | 0.869 | 1.432 | 0.913 | 0.0 |
| 2000 | 12 | 1999.8 | 1999.4 | 0.735 | 1.181 | 0.622 | 0.0 |

**Висновок.** На всіх перевірених рівнях, аж до 2000 rps, генератор тримає задану інтенсивність:
досягнута відрізняється від заданої не більше ніж на 0.03 % (1999.4 проти 2000 rps), запізнення
відправки p99 не перевищує 1 мс, а відповідей не-2xx немає. Межу генератора в цій перевірці не
досягнуто, тобто вона лежить вище за 2000 rps. Це щонайменше вчетверо більше за найвищий
обовʼязковий рівень серії (500 rps). Отже, якщо в серії досягнута інтенсивність відстає від
заданої, обмеження задає сервіс, а не генератор.

### 5. Таблиця вимірювань

Обовʼязкові рівні — 10, 100 і 500 rps (6 конфігурацій). На 500 rps один інстанс ще не насичувався
(p99 = 2.3 мс, 0 % помилок), тому, щоб знайти межу пропускної здатності і порахувати прискорення,
серію доповнено рівнями 1000 і 1500 rps. Процедура та сама: 10 с прогріву + 30 с вимірювання,
3 повтори, у таблиці медіана.

**Відкритий контур, `GET /trial-participants/{id}`** (медіана з 3 повторів):

| Інстансів | Задана, rps | Досягнута, rps | p50, мс | p95, мс | p99, мс | не-2xx, % | Відправлено, rps |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 10 | 10.0 | 4.294 | 5.64 | 6.517 | 0.0 | 10.0 |
| 1 | 100 | 100.0 | 2.407 | 3.139 | 3.649 | 0.0 | 100.0 |
| 1 | 500 | 499.9 | 1.605 | 1.964 | 2.345 | 0.0 | 500.0 |
| 2 | 10 | 10.0 | 3.995 | 5.354 | 6.293 | 0.0 | 10.0 |
| 2 | 100 | 100.0 | 2.346 | 2.978 | 3.399 | 0.0 | 100.0 |
| 2 | 500 | 499.93 | 1.757 | 2.104 | 2.395 | 0.0 | 500.0 |
| *1* | *1000* | *972.63* | *21.244* | *3 672.773* | *6 063.18* | *7.057* | *999.9* |
| *1* | *1500* | *813.1* | *18.667* | *4 475.906* | *6 804.653* | *45.924* | *1 499.87* |
| *2* | *1000* | *999.8* | *1.794* | *2.28* | *2.678* | *0.0* | *999.93* |
| *2* | *1500* | *1 499.73* | *1.786* | *2.338* | *15.378* | *0.0* | *1 499.93* |

*Курсивом — додаткові рівні.* Сирі результати кожного прогону — `results/raw/*.json` і `*.csv.gz`;
підсумки — `results/summary.md`, `results/summary.csv`.

<details>
<summary>Усі окремі прогони (results/runs.md)</summary>

| Файл | Режим | Інст. | Рівень | C | Відправлено | Досягнута | p50 | p95 | p99 | не-2xx % | L | λ·W | Статуси |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| closed_n1_l1000_c601_rep1 | closed | 1 | 1000 | 601 | 1067.23 | 1067.23 | 60.304 | 2175.3 | 3599.443 | 0.0 | 601.0 | 588.73 | 200: 32017 |
| closed_n1_l1500_c1846_rep1 | closed | 1 | 1500 | 1846 | 1111.93 | 1111.93 | 59.363 | 6485.047 | 9937.718 | 0.0 | 1846.0 | 1745.67 | 200: 33358 |
| open_n1_r10_rep1 | open | 1 | 10 | | 10.0 | 10.0 | 3.46 | 5.755 | 6.858 | 0.0 | 0.0327 | 0.0315 | 200: 300 |
| open_n1_r10_rep2 | open | 1 | 10 | | 10.0 | 10.0 | 4.294 | 5.448 | 5.97 | 0.0 | 0.0293 | 0.0357 | 200: 300 |
| open_n1_r10_rep3 | open | 1 | 10 | | 10.0 | 10.0 | 4.311 | 5.64 | 6.517 | 0.0 | 0.0303 | 0.0358 | 200: 300 |
| open_n1_r100_rep1 | open | 1 | 100 | | 100.0 | 100.0 | 2.28 | 3.022 | 38.443 | 0.0 | 0.2062 | 0.3067 | 200: 3000 |
| open_n1_r100_rep2 | open | 1 | 100 | | 100.0 | 100.0 | 2.407 | 3.139 | 3.578 | 0.0 | 0.1295 | 0.199 | 200: 3000 |
| open_n1_r100_rep3 | open | 1 | 100 | | 100.0 | 100.0 | 2.425 | 3.193 | 3.649 | 0.0 | 0.1214 | 0.2027 | 200: 3000 |
| open_n1_r500_rep1 | open | 1 | 500 | | 500.0 | 499.9 | 1.575 | 1.924 | 2.281 | 0.0 | 0.0243 | 0.6037 | 200: 15000 |
| open_n1_r500_rep2 | open | 1 | 500 | | 500.0 | 499.87 | 1.608 | 1.991 | 2.365 | 0.0 | 0.0315 | 0.6217 | 200: 15000 |
| open_n1_r500_rep3 | open | 1 | 500 | | 500.0 | 499.93 | 1.605 | 1.964 | 2.345 | 0.0 | 0.0524 | 0.6406 | 200: 15000 |
| open_n1_r1000_rep1 | open | 1 | 1000 | | 999.9 | 999.77 | 2.164 | 36.794 | 59.318 | 0.0 | 8.8049 | 9.2709 | 200: 30000 |
| open_n1_r1000_rep2 | open | 1 | 1000 | | 999.83 | 766.73 | 21.244 | 5267.829 | 9203.494 | 18.517 | 601.3734 | 681.04 | 200: 24445, ConnectionReset: 5363, ConnectionAborted: 192 |
| open_n1_r1000_rep3 | open | 1 | 1000 | | 999.97 | 972.63 | 63.23 | 3672.773 | 6063.18 | 7.057 | 929.5813 | 877.85 | 200: 27883, ConnectionReset: 1239, ConnectionAborted: 878 |
| open_n1_r1500_rep1 | open | 1 | 1500 | | 1499.93 | 820.83 | 18.667 | 4419.305 | 6804.534 | 45.756 | 1841.7027 | 1749.89 | 200: 24410, ConnectionReset: 14646, ConnectionAborted: 5944 |
| open_n1_r1500_rep2 | open | 1 | 1500 | | 1499.87 | 813.1 | 18.84 | 4650.495 | 7134.22 | 45.924 | 1853.6765 | 1799.03 | 200: 24334, ConnectionReset: 14553, ConnectionAborted: 6113 |
| open_n1_r1500_rep3 | open | 1 | 1500 | | 1499.83 | 806.07 | 18.62 | 4475.906 | 6804.653 | 46.604 | 1846.2808 | 1752.43 | 200: 24028, ConnectionReset: 14822, ConnectionAborted: 6150 |
| open_n2_r10_rep1 | open | 2 | 10 | | 10.0 | 10.0 | 4.087 | 5.354 | 6.643 | 0.0 | 0.0276 | 0.0342 | 200: 300 |
| open_n2_r10_rep2 | open | 2 | 10 | | 10.0 | 10.0 | 3.995 | 5.15 | 6.293 | 0.0 | 0.0263 | 0.0342 | 200: 300 |
| open_n2_r10_rep3 | open | 2 | 10 | | 10.0 | 10.0 | 3.962 | 5.468 | 6.14 | 0.0 | 0.0277 | 0.0329 | 200: 300 |
| open_n2_r100_rep1 | open | 2 | 100 | | 100.0 | 100.0 | 2.346 | 3.116 | 3.537 | 0.0 | 0.1062 | 0.2002 | 200: 3000 |
| open_n2_r100_rep2 | open | 2 | 100 | | 100.0 | 100.0 | 2.334 | 2.942 | 3.303 | 0.0 | 0.0976 | 0.1956 | 200: 3000 |
| open_n2_r100_rep3 | open | 2 | 100 | | 100.0 | 100.0 | 2.362 | 2.978 | 3.399 | 0.0 | 0.1086 | 0.1973 | 200: 3000 |
| open_n2_r500_rep1 | open | 2 | 500 | | 500.0 | 499.93 | 1.757 | 2.113 | 2.44 | 0.0 | 0.0261 | 0.6996 | 200: 15000 |
| open_n2_r500_rep2 | open | 2 | 500 | | 500.0 | 499.93 | 1.769 | 2.104 | 2.395 | 0.0 | 0.0211 | 0.6959 | 200: 15000 |
| open_n2_r500_rep3 | open | 2 | 500 | | 500.0 | 499.93 | 1.717 | 2.056 | 2.372 | 0.0 | 0.0253 | 0.6774 | 200: 15000 |
| open_n2_r1000_rep1 | open | 2 | 1000 | | 999.93 | 999.8 | 1.865 | 2.408 | 2.926 | 0.0 | 1.1747 | 1.5236 | 200: 30000 |
| open_n2_r1000_rep2 | open | 2 | 1000 | | 1000.0 | 999.93 | 1.794 | 2.28 | 2.678 | 0.0 | 1.059 | 1.4391 | 200: 30000 |
| open_n2_r1000_rep3 | open | 2 | 1000 | | 999.93 | 999.6 | 1.785 | 2.22 | 2.596 | 0.0 | 1.0604 | 1.4138 | 200: 30000 |
| open_n2_r1500_rep1 | open | 2 | 1500 | | 1499.93 | 1499.73 | 1.786 | 2.338 | 15.378 | 0.0 | 2.0602 | 2.6966 | 200: 45000 |
| open_n2_r1500_rep2 | open | 2 | 1500 | | 1499.9 | 1499.77 | 1.778 | 2.269 | 13.637 | 0.0 | 2.0495 | 2.667 | 200: 45000 |
| open_n2_r1500_rep3 | open | 2 | 1500 | | 1500.0 | 1499.73 | 1.84 | 15.881 | 37.493 | 0.0 | 4.1196 | 4.7327 | 200: 45000 |

Затримки — у мс. L — вибіркова оцінка кількості запитів у системі (див. 8б).

</details>

### 6. Підтвердження розподілу запитів між двома інстансами

Розподіл підтверджено двома незалежними способами (команда `py scripts/lb_distribution.py`,
стенд з `--scale web=2`, 1000 запитів).

**1. Лічильник на боці клієнта** — заголовок `X-Instance`, який додає кожен інстанс:

| Інстанс (hostname контейнера) | Відповідей | Частка |
|---|---:|---:|
| `bd9ae4353ba7` (trials-web-2) | 500 | 50.0 % |
| `93b05b6d388a` (trials-web-1) | 500 | 50.0 % |

**2. Лог балансувальника** — поле `upstream=` в access-лозі nginx:

| Адреса інстансу | Контейнер | Запитів | Частка |
|---|---|---:|---:|
| `172.19.0.2:8000` | trials-web-1 | 501 | 50.0 % |
| `172.19.0.4:8000` | trials-web-2 | 500 | 50.0 % |

```text
$ docker compose logs lb
2026-10-14T14:46:00+00:00 172.19.0.1 "GET /trial-participants/90 HTTP/1.1" 200 rt=0.001 upstream=172.19.0.4:8000 urt=0.001
2026-10-14T14:46:00+00:00 172.19.0.1 "GET /trial-participants/67 HTTP/1.1" 200 rt=0.001 upstream=172.19.0.2:8000 urt=0.001
2026-10-14T14:46:00+00:00 172.19.0.1 "GET /trial-participants/70 HTTP/1.1" 200 rt=0.001 upstream=172.19.0.4:8000 urt=0.001
2026-10-14T14:46:00+00:00 172.19.0.1 "GET /trial-participants/83 HTTP/1.1" 200 rt=0.001 upstream=172.19.0.2:8000 urt=0.001
2026-10-14T14:46:00+00:00 172.19.0.1 "GET /trial-participants/30 HTTP/1.1" 200 rt=0.001 upstream=172.19.0.4:8000 urt=0.001
2026-10-14T14:46:00+00:00 172.19.0.1 "GET /trial-participants/14 HTTP/1.1" 200 rt=0.001 upstream=172.19.0.2:8000 urt=0.001
```

Запити чергуються між двома адресами (round-robin).

**3. Розподіл у самій серії** (заголовок `X-Instance`, сума за 3 повтори):

| Конфігурація | Інстанс 1 | Інстанс 2 |
|---|---:|---:|
| 2 інст., 10 rps | 450 (50.0 %) | 450 (50.0 %) |
| 2 інст., 100 rps | 4 500 (50.0 %) | 4 500 (50.0 %) |
| 2 інст., 500 rps | 22 500 (50.0 %) | 22 500 (50.0 %) |
| 2 інст., 1000 rps | 45 000 (50.0 %) | 45 000 (50.0 %) |
| 2 інст., 1500 rps | 67 500 (50.0 %) | 67 500 (50.0 %) |

У конфігураціях з одним інстансом усі відповіді прийшли від одного контейнера (`93b05b6d388a`, 100 %).

### 7. Висновок про межу пропускної здатності і ефект другого інстансу

**10, 100, 500 rps — обидві конфігурації тримають навантаження.** Досягнута інтенсивність дорівнює
заданій, не-2xx 0 %, p99 не перевищує 6.5 мс. На 500 rps один інстанс дає 499.9 rps при
p50/p95/p99 = 1.6 / 2.0 / 2.3 мс, два — 499.9 rps при 1.8 / 2.1 / 2.4 мс. Другий інстанс тут
нічого не змінює, бо один ще не насичений. З ростом інтенсивності затримка навіть трохи
зменшується (p50 4.3 → 2.4 → 1.6 мс для одного інстансу). Ймовірна причина: при рідких запитах
процесор встигає переходити в енергозберігальні стани між ними.

**Межа одного інстансу — близько 1000 rps.** На 1000 rps перший повтор ще утримав навантаження
(999.8 rps, p99 = 59 мс), а другий і третій — ні: досягнута 766.7 і 972.6 rps, p95 5.3 і 3.7 с,
не-2xx 18.5 і 7.1 %. Така розбіжність повторів характерна для роботи на самій межі (ρ ≈ 1).
Будь-яке коливання, наприклад фонові процеси чи конкуренція з генератором за ядра, запускає чергу,
яка у відкритому контурі вже не розсмоктується, бо запити продовжують надходити за розкладом.
Медіана на 1000 rps: досягнута 972.6 rps, p50 / p95 / p99 = 21 мс / 3.7 с / 6.1 с. Це в
~1 800–2 600 разів більше, ніж p95/p99 на 500 rps.

**1500 rps — стабільне перевантаження.** Усі три повтори дали досягнуту 806–821 rps
(медіана 813.1, тобто 54 % від заданої), p95 = 4.5 с, p99 = 6.8 с, не-2xx 46 %. У системі
одночасно в середньому ≈ 1 846 запитів, хоча на 500 rps їх було менше одного. Перевантаження
видно з трьох ознак: досягнута інтенсивність зупинилась далеко нижче заданої, затримка зросла
на три порядки, а кількість запитів у системі — на чотири, тобто запити накопичуються в черзі.

Досягнута на 1500 rps **менша**, ніж на 1000 rps (813 проти 973). Отже, перевантаження не просто
впирає пропускну здатність у стелю, а знижує її (congestion collapse). Сервіс витрачає процесор
на тисячі одночасних зʼєднань і на запити, клієнт яких уже отримав розрив.

**Природа помилок.** Жодна з помилок не є HTTP-відповіддю сервісу (5xx немає). Усі вони — розриви
TCP-зʼєднання на боці клієнта: `ConnectionResetError` (≈ 14.6 тис. за прогін) і
`ConnectionAbortedError` (≈ 6.1 тис.), див. колонку «Статуси» в `results/runs.md`. Розриви
зʼявляються лише тоді, коли черга розростається: генератор відкриває окреме зʼєднання для кожного
потоку, що чекає відповіді, і тисячі одночасних зʼєднань починає скидати проміжна ланка.
Найімовірніше, це проброс порту Docker Desktop на Windows або черга прийому зʼєднань.
На 2 інстансах, де черги немає, таких помилок 0.

**Пропускна здатність одного інстансу.** У закритому контурі, де черга обмежена і помилок немає,
один інстанс віддав 1 067–1 112 rps. Отже, його межа становить ≈ 1 000–1 100 rps.

**Що обмежило.** Межу одного інстансу задає ліміт процесора контейнера web — 0.5 CPU. Це видно з
результату другого інстансу: він додає ще 0.5 CPU і не додає нічого іншого. При цьому пропускна
здатність майже подвоїлась (див. нижче), а помилки зникли. Якби вузьким місцем була спільна база
PostgreSQL або балансувальник nginx, другий вебінстанс майже нічого б не змінив.

**Два інстанси.** 1000 і 1500 rps тримаються повністю: 999.8 і 1 499.7 rps, p95 2.3 мс, 0 % помилок.
На 1500 rps другий інстанс змінив картину так:

| 1500 rps | 1 інстанс | 2 інстанси |
|---|---:|---:|
| Досягнута, rps | 813.1 | 1 499.7 |
| p95, мс | 4 475.9 | 2.3 |
| p99, мс | 6 804.7 | 15.4 |
| не-2xx, % | 45.9 | 0.0 |
| Запитів у системі, L | ≈ 1 846 | ≈ 2 |

У серії два інстанси утримали 1500 rps повністю, але вже з ознаками наближення до межі: «хвіст»
затримки виріс (p99 = 13.6–37.5 мс проти 2.4 мс на 500 rps), а в третьому повторі вже й
p95 = 15.9 мс.

Пропускна здатність майже подвоїлась: межа одного інстансу ≈ 1 000–1 100 rps, двох — близько
1 500 rps, виміряне прискорення на 1500 rps S = 1.84.

На те, що 1500 rps — це вже межа стенда з двома інстансами, вказує й контрольний прогін на тому самому
рівні (`py loadtest/loadgen.py --rate 1500 --duration 30 --warmup 5`). Він дав досягнуту 1 497.4 rps,
p50 / p95 / p99 = 1.8 / 7.5 / 60.8 мс і 0.9 % розривів зʼєднань (287 `ConnectionResetError` +
118 `ConnectionAbortedError`). У серії на тому самому рівні було p99 = 15.4 мс і 0 % помилок. Повтори
розходяться, як перед тим у одного інстансу на 1000 rps, тобто стенд працює на межі (ρ ≈ 1).

Подвоєння не повне, бо частина стенда лишилась спільною для обох інстансів: одна база PostgreSQL,
один балансувальник nginx і процесор хоста. 12 логічних ядер ділять між собою обидва вебінстанси,
база, nginx і генератор, який на 1500 rps сам займає 12 процесів. Розподіл навантаження при цьому
рівний: 22 298 і 22 297 відповідей від двох інстансів.

---

## Етап 7. Розрахунки

Команда: `py loadtest/calc.py` → `results/calculations.md`.

### 8а. Прискорення і метрика Карпа-Флатта

S = X₂ / X₁,  e = (1/S − 1/N) / (1 − 1/N),  для N = 2:  e = (1/S − 0.5) / 0.5

| Рівень, rps | X₁, rps | X₂, rps | S | e | Чи інтерпретується |
|---:|---:|---:|---:|---:|---|
| 10 | 10.0 | 10.0 | 1.000 | — | ні: обидві конфігурації не насичені |
| 100 | 100.0 | 100.0 | 1.000 | — | ні: обидві не насичені |
| 500 | 499.9 | 499.93 | 1.000 | — | ні: обидві не насичені |
| 1000 | 972.63 | 999.8 | 1.028 | 0.946 | ні: один інстанс на самій межі й віддає 97 % заданого, тому S ≈ 1 |
| **1500** | **813.1** | **1 499.73** | **1.844** | **0.084** | **так: один інстанс насичений** |

Підстановка для 1500 rps:

S = 1 499.73 / 813.1 = **1.844**;  e = (1/1.844 − 0.5) / 0.5 = (0.5422 − 0.5) / 0.5 = **0.084**

Тобто приблизно **8 %** роботи не отримали виграшу від другого інстансу. Спільна для обох складова —
одна база PostgreSQL (а також один nginx і ядра хоста, спільні з генератором).

Обмеження цієї оцінки (вони діють у протилежні боки):

* X₂ = 1 499.7 дорівнює поданому навантаженню: два інстанси ще встигали, хоч і на межі. Їхня
  справжня межа може бути трохи вищою, тому S може бути **занижене**.
* X₁ = 813.1 на 1500 rps нижче за справжню межу одного інстансу (≈ 1 000–1 100 rps) через
  congestion collapse. Через це S **завищене**.

На рівнях 10–1000 rps S ≈ 1 не означає поганої масштабованості. Навантаження там не перевищує
(або ледь сягає) можливостей одного інстансу, і обидві конфігурації просто віддають задане.

### 8б. Перевірка закону Літтла (1 інстанс, 100 rps)

| Прогін | λ, rps | W, с | λ·W | L (середнє N(t) за часом) | Розбіжність, % | L (вибірка кожні 2 мс) |
|---|---:|---:|---:|---:|---:|---:|
| open_n1_r100_rep1 | 100.0 | 0.003067 | 0.3067 | 0.3067 | 0.000 | 0.2062 |
| open_n1_r100_rep2 | 100.0 | 0.001990 | 0.1990 | 0.1990 | 0.000 | 0.1295 |
| open_n1_r100_rep3 | 100.0 | 0.002027 | 0.2027 | 0.2027 | 0.000 | 0.1214 |

Підстановка (rep1):

λ = 100.0 rps,  W = 0.003067 с,  λ·W = 100.0 × 0.003067 = **0.3067**;
виміряне L = 0.3067;  розбіжність = |0.3067 − 0.3067| / 0.3067 × 100 % ≈ **0 %**

L — це середнє за часом N(t) = відправлені(t) − завершені(t). Його пораховано з моментів відправки
й завершення кожного запиту, які записав генератор. Закон Літтла виконується з точністю до
крайових ефектів вікна вимірювання.

Окремо генератор знімав N(t) вибіркою кожні 2 мс, і ця оцінка вийшла меншою: 0.12–0.21 замість
0.20–0.31. Причина в методі вибірки, а не в законі. Запит триває ≈ 2 мс, тобто стільки ж, скільки
крок вибірки. До того ж потік вибірки і цикл планування запитів прокидаються від того самого
системного таймера Windows. Вибірка «синхронізується» з моментами відправки і систематично
пропускає короткі запити. Під перевантаженням, коли запити тривають секунди, обидві оцінки
близькі: на 1500 rps L = 1 842–1 854 проти λ·W = 1 750–1 799, розбіжність ≈ 3–5 %.

### 8в. Порівняння з закритим контуром

Основне порівняння — на 1000 rps, першому рівні, де один інстанс не тримає задану інтенсивність.
C = 601 — середня кількість запитів у системі у відкритому контурі на цьому рівні.

| Контур | Інстансів | Навантаження | Досягнута, rps | p50, мс | p95, мс | p99, мс | не-2xx, % |
|---|---:|---|---:|---:|---:|---:|---:|
| відкритий | 1 | 1000 rps | 972.6 | 21.2 | 3 672.8 | 6 063.2 | 7.1 |
| закритий | 1 | C = 601 | 1 067.2 | 60.3 | **2 175.3** | 3 599.4 | 0.0 |

Команда: `py loadtest/run_closed.py --instances 1 --level 1000`

На тому самому сервісі закритий контур показав p95 на **41 % меншу** (2.2 с проти 3.7 с), без
помилок і з дещо більшою досягнутою інтенсивністю. Причини:

* **У закритому контурі черга обмежена.** Наступний запит іде лише після відповіді на попередній,
  тому в системі ніколи не буває більше за C = 601 запит. Генератор сам сповільнюється до
  пропускної здатності сервісу, і середня затримка за законом Літтла стабілізується на
  W ≈ C / X = 601 / 1 067 ≈ 0.56 с.
* **У відкритому контурі черга росте весь прогін.** Запити надходять за розкладом незалежно від
  відповідей, надлишок (λ − X) накопичується, і пізні запити чекають усе довше. p95 відображає
  саме цей хвіст.
* **Закритий контур «ховає» перевантаження.** Реальні незалежні клієнти не чекають один одного,
  тож вони побачили б саме картину відкритого контуру. Закритий контур занижує затримку, бо не
  відправляє запити, які мали б прийти, поки сервіс зайнятий (coordinated omission).

**Додатково, 1500 rps (C = 1 846):**

| Контур | Навантаження | Досягнута, rps | p50, мс | p95, мс | p99, мс | не-2xx, % |
|---|---|---:|---:|---:|---:|---:|
| відкритий | 1500 rps | 813.1 | 18.7 | 4 475.9 | 6 804.7 | 45.9 |
| закритий | C = 1 846 | 1 111.9 | 59.4 | 6 485.0 | 9 937.7 | 0.0 |

Тут p95 закритого контуру більша. Це не суперечить поясненню вище, а показує дві особливості
сильного перевантаження:

1. У відкритому контурі 46 % запитів завершились швидким розривом зʼєднання. Вони теж входять у
   вибірку перцентилів, тому p50 = 18.7 мс, хоча половина запитів невдала. Через це p95
   відкритого контуру занижена й не порівнювана напряму.
2. При C = 1 846 у закритому контурі й самий середній час за Літтлом великий:
   W ≈ 1 846 / 1 112 ≈ 1.7 с. Розподіл при цьому двогорбий (p50 = 59 мс, p95 = 6.5 с), що
   типово для переповненої черги прийому зʼєднань.

Тому коректне порівняння — на 1000 rps, де відкритий контур ще не руйнується масово.

---

## Етап 8. Додаткова вимога варіанта

### 10. Схема накочується окремим разовим контейнером міграції

**Як виконано:**

- Сервіс `migrate` у `docker-compose.yml` запускає той самий образ з командою
  `python -m app.migrate`, `restart: "no"`, і завершується.
- Скрипт `app/migrate.py` чекає на базу, бере `pg_advisory_lock` (захист від двох одночасних
  запусків), створює службову таблицю `schema_migrations`, застосовує по черзі файли
  `migrations/*.sql`, яких у ній ще немає (кожен у транзакції), і зберігає контрольну суму.
  Повторний запуск нічого не робить; зміна вже застосованого файлу — помилка з кодом 1.
- `web` стартує лише після успішного завершення міграції:
  `depends_on: migrate: condition: service_completed_successfully`.
- Теку `/docker-entrypoint-initdb.d` не використано: у контейнер бази змонтовано лише том з даними
  (`pgdata:/var/lib/postgresql/data`).

**Чим підтверджено** (`py scripts/collect_evidence.py` → `results/evidence.md`):

1. **Контейнер міграції завершився з кодом 0**, вебінстанси працюють:

   ```text
   $ docker compose ps -a
   NAME               IMAGE                SERVICE   STATUS
   trials-db-1        postgres:16-alpine   db        Up About an hour (healthy)
   trials-lb-1        nginx:1.28-alpine    lb        Up About an hour
   trials-migrate-1   trials-migrate       migrate   Exited (0) 38 minutes ago
   trials-web-1       trials-web           web       Up About an hour (healthy)
   trials-web-2       trials-web           web       Up 38 minutes (healthy)
   ```

2. **Логи міграції** — перший запуск накотив схему, наступний (після `down`/`up` без `-v`) нічого
   не застосував:

   ```text
   $ docker compose logs --no-log-prefix migrate
   2026-10-14 15:04:12,618 INFO migrate: applied 001_create_trial_participants.sql
   2026-10-14 15:04:12,618 INFO migrate: done: 1 new migration(s) applied, 1 file(s) total
   2026-10-14 15:26:35,919 INFO migrate: skip 001_create_trial_participants.sql (already applied)
   2026-10-14 15:26:35,919 INFO migrate: done: 0 new migration(s) applied, 1 file(s) total
   ```

3. **Журнал застосованих міграцій** у базі:

   ```text
   $ docker compose exec -T db psql -U trials -d trials -c "SELECT version, left(checksum, 12) AS checksum, applied_at FROM schema_migrations"
                 version              |   checksum   |          applied_at
   -----------------------------------+--------------+-------------------------------
    001_create_trial_participants.sql | a7f6d4eac37e | 2026-10-14 15:04:12.602989+00
   (1 row)
   ```

4. **Тека ініціалізації бази порожня, у контейнер бази змонтовано лише том з даними:**

   ```text
   $ docker compose exec -T db ls -la /docker-entrypoint-initdb.d
   total 8
   drwxr-xr-x    2 root     root          4096 Sep 17 21:29 .
   drwxr-xr-x    1 root     root          4096 Oct 14 15:04 ..

   $ docker inspect --format '{{range .Mounts}}{{.Type}} {{.Name}} -> {{.Destination}}{{end}}' trials-db-1
   volume trials_pgdata -> /var/lib/postgresql/data
   ```

5. **Таблиця створена міграцією** з усіма обмеженнями та індексами:

   ```text
   $ docker compose exec -T db psql -U trials -d trials -c "\d trial_participants"
          Column        |           Type           | Nullable |           Default
   ---------------------+--------------------------+----------+------------------------------
    id                  | bigint                   | not null | generated always as identity
    participant_code    | character varying(32)    | not null |
    protocol            | character varying(64)    | not null |
    randomization_group | character varying(16)    | not null |
    enrollment_date     | date                     | not null |
    status              | character varying(16)    | not null |
    completion_date     | date                     |          |
    created_at          | timestamp with time zone | not null | now()
    updated_at          | timestamp with time zone | not null | now()
   Indexes:
       "trial_participants_pkey" PRIMARY KEY, btree (id)
       "ix_trial_participants_group_id" btree (randomization_group, id)
       "uq_trial_participants_code" UNIQUE CONSTRAINT, btree (participant_code)
   Check constraints:
       "ck_trial_participants_dates" CHECK (completion_date IS NULL OR completion_date >= enrollment_date)
       "ck_trial_participants_group" CHECK (randomization_group IN ('A', 'B', 'контроль'))
       "ck_trial_participants_status" CHECK (status IN ('screening', 'active', 'completed', 'withdrawn'))
   ```

   (Вирази CHECK скорочено; PostgreSQL показує їх у формі `= ANY (ARRAY[...])`.)

6. **Повторний запуск міграції ідемпотентний:**

   ```text
   $ docker compose run --rm --no-deps migrate
   2026-10-14 16:04:56,978 INFO migrate: skip 001_create_trial_participants.sql (already applied)
   2026-10-14 16:04:56,978 INFO migrate: done: 0 new migration(s) applied, 1 file(s) total
   ```

7. **На порожньому томі схема накочується знову:** після `docker compose down -v` і `up -d`
   контейнер `migrate` знову завершився з `Exited (0)`, після чого стартував `web`, а `/healthz`
   (який перевіряє наявність таблиці) відповів 200 — див. розділ «Збереження даних».

---

## Етап 9. Перевірка

### 9. Журнал перевірки

Команда `py loadtest/verify_cycle.py`. Журнал нижче знято при запуску сервісу на хості (порт 8000)
з базою в контейнері; на стенді в Docker (порт 8080) результат той самий — 19/19 (див. етап 2).
Повний журнал з тілами запитів і відповідей — `results/verify_log.md`.

| № | Крок | Запит | Очікувано | Отримано | Відповідь сервісу |
|---:|---|---|:---:|:---:|---|
| 1 | Готовність сервісу | `GET /healthz` | 200 | ✅ 200 | `{"status": "ok", "instance": "DESKTOP-82E2TOM"}` |
| 2 | Створення запису | `POST /trial-participants` (код `TP-VERIFY-8769A5`, група A) | 201 | ✅ 201 | запис з `"id": 1`, `created_at`, `updated_at` |
| 3 | Невалідна група рандомізації `"C"` | `POST /trial-participants` | 400 | ✅ 400 | `randomization_group`: «група рандомізації має бути одним із значень: A, B, контроль; отримано 'C'» |
| 4 | Дата завершення раніше дати включення | `POST /trial-participants` | 400 | ✅ 400 | `completion_date`: «дата завершення не може бути раніше дати включення (2025-03-01)» |
| 5 | Без обовʼязкового поля `protocol` | `POST /trial-participants` | 400 | ✅ 400 | `protocol`: «поле обовʼязкове» |
| 6 | Наявний код учасника | `POST /trial-participants` | 409 | ✅ 409 | `conflict`: «значення 'TP-VERIFY-8769A5' вже використовується» |
| 7 | Перелік з фільтром | `GET /trial-participants?randomization_group=A&limit=100` | 200 | ✅ 200 | `items` містить запис `id=1`, `total: 1` |
| 8 | Невалідний фільтр | `GET /trial-participants?randomization_group=D` | 400 | ✅ 400 | `randomization_group` (query): «фільтр має бути одним із значень: A, B, контроль; отримано 'D'» |
| 9 | `limit` понад межу | `GET /trial-participants?limit=1000` | 400 | ✅ 400 | `limit`: «значення має бути не більше за 100» |
| 10 | Отримання за id | `GET /trial-participants/1` | 200 | ✅ 200 | запис `id=1`, група A, статус `active` |
| 11 | Повна заміна | `PUT /trial-participants/1` (група «контроль», статус `completed`, протокол CT-2025-002) | 200 | ✅ 200 | оновлений запис з новими значеннями всіх полів |
| 12 | Заміна з невалідною групою «плацебо» | `PUT /trial-participants/1` | 400 | ✅ 400 | `randomization_group`: «… отримано 'плацебо'» |
| 13 | Отримання після заміни | `GET /trial-participants/1` | 200 | ✅ 200 | група «контроль», `completion_date: "2025-09-30"` |
| 14 | Перелік з фільтром «контроль» | `GET /trial-participants?randomization_group=контроль&limit=5` | 200 | ✅ 200 | `items` містить запис `id=1`, `total: 1` |
| 15 | Видалення | `DELETE /trial-participants/1` | 204 | ✅ 204 | без тіла |
| 16 | **Повторне видалення** | `DELETE /trial-participants/1` | 404 | ✅ 404 | `not_found`: «Учасника з id=1 не знайдено» |
| 17 | Отримання видаленого | `GET /trial-participants/1` | 404 | ✅ 404 | `not_found` |
| 18 | Заміна неіснуючого | `PUT /trial-participants/1` | 404 | ✅ 404 | `not_found` |
| 19 | Некоректний id | `GET /trial-participants/abc` | 400 | ✅ 400 | `id` (path): «очікується ціле число» |

**Підсумок:** 19 з 19 кроків дали очікуваний код.

<details>
<summary>Приклад повного запису журналу (крок 3)</summary>

**Запит:** `POST /trial-participants`

```json
{
  "participant_code": "TP-VERIFY-8769A5-X",
  "protocol": "CT-2025-001",
  "randomization_group": "C",
  "enrollment_date": "2025-03-01",
  "status": "active",
  "completion_date": null
}
```

**Відповідь:** `400`

```json
{
  "error": "validation_error",
  "message": "Невалідні дані запиту",
  "details": [
    {
      "field": "randomization_group",
      "location": "body",
      "message": "група рандомізації має бути одним із значень: A, B, контроль; отримано 'C'"
    }
  ]
}
```

</details>

### Збереження даних

Команда `py scripts/persistence_check.py` (повний лог — `results/persistence_log.md`).

| Крок | Дія | Результат |
|---|---|---|
| 1 | `docker compose up -d`, `POST /trial-participants` | ✅ запис створено (201), `id = 103`, код `TP-PERSIST-70A17D`; записів у базі — **101** |
| 2 | `docker compose down` | контейнери й мережі видалено, том `trials_pgdata` **лишився** |
| 3 | `docker compose up -d` | ✅ `/healthz` → 200; `GET /trial-participants/103` → **200**, `TP-PERSIST-70A17D`; записів — **101** (не змінилось) |
| 4 | `docker compose down -v` | том `trials_pgdata` **видалено** |
| 5 | `docker compose up -d` | ✅ стенд піднявся без помилок, міграція `Exited (0)`, `/healthz` → 200; записів — **0** |

```text
$ docker compose down
 ✔ Container trials-lb-1       Removed
 ✔ Container trials-web-1      Removed
 ✔ Container trials-migrate-1  Removed
 ✔ Container trials-db-1       Removed
 ✔ Network trials_backend      Removed
 ✔ Network trials_frontend     Removed

$ docker volume ls --filter name=trials
DRIVER    VOLUME NAME
local     trials_pgdata

$ docker compose down -v
 ✔ Container trials-lb-1       Removed
 ✔ Container trials-web-1      Removed
 ✔ Container trials-migrate-1  Removed
 ✔ Container trials-db-1       Removed
 ✔ Volume trials_pgdata        Removed
 ✔ Network trials_frontend     Removed
 ✔ Network trials_backend      Removed

$ docker volume ls --filter name=trials
DRIVER    VOLUME NAME

$ docker compose up -d          # на порожньому томі
 ✔ Volume trials_pgdata         Created
 ✔ Container trials-db-1        Healthy
 ✔ Container trials-migrate-1   Exited
 ✔ Container trials-web-1       Healthy
 ✔ Container trials-lb-1        Started

$ docker compose ps -a migrate
NAME               IMAGE            COMMAND                  SERVICE   CREATED         STATUS
trials-migrate-1   trials-migrate   "python -m app.migra…"   migrate   6 seconds ago   Exited (0) 3 seconds ago
```

**Підсумок:** усі перевірки пройдено — після `down`/`up` дані на місці, після `down -v` база
порожня, а стенд піднімається без помилок.

---

## 11. Відкинуті рішення

| Рішення | Чому відмовились |
|---|---|
| Накочувати схему SQL-файлом у `/docker-entrypoint-initdb.d` | Заборонено варіантом; крім того, initdb виконує ці файли лише на **порожньому** томі — нові міграції на наявну базу так не потрапляють, і немає журналу застосованих змін |
| Накочувати схему при старті вебсервісу (`CREATE TABLE IF NOT EXISTS` у lifespan) | Із `--scale web=2` дві репліки накочують схему одночасно (гонка); змішується відповідальність; старт кожної репліки повільніший |
| Alembic / ORM SQLAlchemy | Для однієї таблиці — зайва залежність і більший образ; власний мігратор на 80 рядків з журналом і advisory-lock закриває потребу |
| Публікувати порт web (`8000:8000`) і масштабувати через діапазон портів | Методичка вимагає єдину точку входу; з опублікованим портом `--scale` конфліктує за порт хоста |
| `depends_on` без умов | Лише порядок створення контейнерів, без очікування готовності — перші запити отримували б помилки підключення |
| Генератор із фіксованою кількістю віртуальних користувачів (locust / ab / wrk без `-R`) | Це закритий контур: під перевантаженням генератор сам сповільнюється і ховає затримку; методичка вимагає фіксовану інтенсивність і власний генератор |
| Генератор на asyncio в одному процесі | Планування й розбір відповідей упираються в одне ядро; процеси + пул потоків, як у методичці |
| Кілька воркерів uvicorn у контейнері | Ліміт 0.5 CPU: воркери ділили б ту саму пів-ядра, додаючи лише перемикання контекстів і памʼять |

---

## 12. Використання генеративних моделей

Для генерації коду та скриптів вимірювань використано модель Claude (Anthropic). Числа у звіті
отримано з власних прогонів на моєму компʼютері.

**Що я перевірив після моделі:**

1. Прочитав код кожного файлу; стенд піднімається однією командою на порожніх томах
   (`docker compose down -v` → `docker compose up -d`), міграція завершується з кодом 0.
2. Усі коди відповідей (201, 200, 204, 400, 404, 409, 503) — через Swagger `/docs` і скрипт
   `verify_cycle.py` (19/19 кроків).
3. Відсутність пароля в образі та історії збірки (`docker history --no-trunc`), ліміти
   0.5 CPU / 512 МБ (`docker inspect`), розподіл запитів 50/50 між двома інстансами
   (заголовок `X-Instance` і лог nginx).
4. Формули Карпа-Флатта і закону Літтла звірив з методичкою й перерахував одне значення вручну:
   S = 1 499.73 / 813.1 = 1.844; e = (1/1.844 − 0.5) / 0.5 = 0.084.

**Що виявилось хибним і довелось змінити:**

1. **Помилка в перевірці закону Літтла.** Перша версія скрипта розрахунків брала як «виміряне L»
   вибірку кількості запитів у системі кожні 2 мс і давала розбіжність з λ·W 49–67 %. Перерахувавши
   вручну, я побачив, що в тих самих даних λ·W = 0.3067, а вибірка дала 0.2062. Тобто помилка не
   в законі, а у способі вимірювання. Запит на 100 rps триває ≈ 2–3 мс, як і крок вибірки, а потік
   вибірки на Windows прокидається від того самого системного таймера, що й цикл відправки
   запитів. Тому вибірка систематично «пропускала» короткі запити. Разом з моделлю розрахунок
   змінено: L тепер обчислюється як точне середнє за часом N(t) = відправлені(t) − завершені(t) за
   моментами відправки й завершення кожного запиту. Вибіркову оцінку залишено в таблиці для
   порівняння. Під перевантаженням (1500 rps), коли запити тривають секунди, обидві оцінки
   розходяться лише на 3–5 %, що підтверджує пояснення.
2. **Закритий контур спочатку знято на неправильному рівні.** Перший прогін я зробив на 500 rps,
   де один інстанс ще не перевантажений. Середнє L там 0.03, тому C = 1, і порівняння втратило
   сенс. Цей прогін видалено й повторено на 1000 і 1500 rps, де сервіс уже не тримає навантаження.
3. **Обовʼязкових рівнів забракло для розрахунків.** На 500 rps один інстанс не насичувався
   (p99 = 2.3 мс), тому прискорення на 10–500 rps дорівнює 1 і нічого не показує. Серію доповнено
   рівнями 1000 і 1500 rps, де знайдено межу одного інстансу. Скрипт розрахунків доповнено
   поясненнями, на яких рівнях S і e мають сенс.
4. **Локальний запуск на Windows.** Пакет `uvloop` з `requirements.txt` не встановлюється під
   Windows. Для запуску поза Docker використано окремий список залежностей без нього; в образі
   (Linux, alpine) `uvloop` лишився.

---

## Відповіді на контрольні питання

1. **PUT і PATCH.** PUT передає повне представлення ресурсу і замінює запис цілком: поле, якого
   немає в тілі, стає значенням за замовчуванням (`completion_date` → `null`). Тому PUT
   ідемпотентний і валідується як створення. PATCH передає лише зміни (JSON Merge Patch / JSON
   Patch), решта полів лишається; валідувати треба результат злиття з поточним станом, і в
   загальному випадку PATCH не ідемпотентний.

2. **Створення з наявним ідентифікатором.** `id` генерує база, клієнт його не передає (зайве поле →
   400). Якщо ж конфліктує унікальний природний ключ (`participant_code`) — **409 Conflict**: запит
   коректний синтаксично, але суперечить поточному стану ресурсу; 400 означав би помилку в самому
   запиті, а 200/201 — що створено новий запис, чого не сталося.

3. **Шари образу.** Кожна інструкція `RUN` / `COPY` / `ADD` створює шар — знімок змін файлової
   системи. Шари кешуються; якщо змінився вхід інструкції, перезбираються вона і **всі наступні**.
   Тому спочатку копіюється `requirements.txt` і ставляться залежності (змінюються рідко), а код —
   останнім: зміна коду перезбирає лише останні шари за секунди.

4. **Дані після down / down -v / видалення контейнера.** `down` видаляє контейнери і мережі,
   іменований том `pgdata` лишається — дані на місці. `down -v` видаляє й томи — база порожня.
   Ручне видалення контейнера бази (`docker rm`) не чіпає іменований том; новий контейнер підхопить
   ті самі дані (з анонімним томом дані «загубились» би, хоча фізично лишились би до `volume prune`).

5. **depends_on без умови** чекає лише створення/запуску контейнера бази, а не готовності PostgreSQL
   (initdb, відкриття порту, відновлення після збою). Потрібен healthcheck бази і
   `condition: service_healthy`; для міграції — `service_completed_successfully`.

6. **EXPOSE і публікація порту.** `EXPOSE` — лише метадані образу (документація), порт ним не
   відкривається. Публікація (`ports:` / `-p`) прокидає порт контейнера на інтерфейс хоста. Сервіс
   дістається бази через спільну мережу Compose: контейнери в одній мережі бачать усі порти один
   одного, а імʼя `db` резолвиться вбудованим DNS Docker (127.0.0.11) у IP контейнера.

7. **Фіксована швидкість проти фіксованих VU.** Відкритий контур подає запити за розкладом незалежно
   від відповідей — як реальні незалежні клієнти. При N віртуальних користувачах кожен чекає
   відповідь, тож при уповільненні сервісу падає і швидкість подачі запитів; черга не росте, а
   запити, які мали б прийти під час затримки, просто не відправляються (coordinated omission).
   Затримка виглядає обмеженою (≈ N/X), хоча реальні користувачі під тим самим навантаженням
   чекали б значно довше. У цій роботі це видно напряму: на 1000 rps закритий контур показав p95
   2.2 с проти 3.7 с у відкритому.

8. **Що змінилось після додавання другого вебінстансу.** На 10–500 rps — нічого: один інстанс ще
   не насичений. На 1500 rps досягнута зросла з 813 до 1 500 rps (S = 1.84), p95 впала з 4.5 с до
   2.3 мс, помилки — з 46 % до 0. Межа одного інстансу ≈ 1 000–1 100 rps, двох — близько 1 500 rps.
   Спільними лишились одна база PostgreSQL, один nginx і процесор хоста, на якому працює
   й генератор.

9. **Що означає e = 0.084.** Близько 8 % роботи не прискорюється від другого інстансу: це частка,
   що припадає на спільні ресурси (база, балансувальник, ядра хоста). Значення близьке до нуля,
   отже, на двох інстансах стенд масштабується майже лінійно. Якби в кожного інстансу була своя
   база, спільна частина зменшилась би, і e наблизилось би до 0 (лишились би nginx і ядра хоста).
   З однією базою при додаванні нових інстансів вона рано чи пізно стане вузьким місцем, S
   перестане рости, а e збільшуватиметься.

10. **Чому час відповіді зростає швидше за інтенсивність.** За моделлю масового обслуговування
    (M/M/1) середній час перебування W = 1/(μ − λ) = (1/μ)/(1 − ρ), де ρ = λ/μ — завантаження.
    При ρ → 1 знаменник прямує до нуля, і W зростає гіперболічно; за ρ ≥ 1 черга росте без
    обмеження, і затримка лінійно збільшується з часом прогону. Тому поблизу межі невеликий приріст
    λ дає непропорційно великий приріст затримки. У цій роботі: від 500 до 1000 rps (×2) p95 одного
    інстансу зросла з 2.0 мс до 3.7 с (≈ ×1 900).
