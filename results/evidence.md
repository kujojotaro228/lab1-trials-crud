# Докази виконання вимог

## Середовище

### Версія Docker

```
$ docker version --format Client {{.Client.Version}} / Server {{.Server.Version}}
Client 29.8.2 / Server 29.8.2
```

### Версія Compose

```
$ docker compose version
Docker Compose version v5.6.0
```

### Стан стенда

```
$ docker compose ps -a
NAME               IMAGE                COMMAND                  SERVICE   CREATED             STATUS                       PORTS
trials-db-1        postgres:16-alpine   "docker-entrypoint.s…"   db        About an hour ago   Up About an hour (healthy)   5432/tcp
trials-lb-1        nginx:1.28-alpine    "/docker-entrypoint.…"   lb        About an hour ago   Up About an hour             0.0.0.0:8080->80/tcp, [::]:8080->80/tcp
trials-migrate-1   trials-migrate       "python -m app.migra…"   migrate   About an hour ago   Exited (0) 38 minutes ago    
trials-web-1       trials-web           "uvicorn app.main:ap…"   web       About an hour ago   Up About an hour (healthy)   8000/tcp
trials-web-2       trials-web           "uvicorn app.main:ap…"   web       38 minutes ago      Up 38 minutes (healthy)      8000/tcp
```

## Додаткова вимога: схема накочується окремим разовим контейнером

### Контейнер міграції завершився з кодом 0

```
$ docker compose ps -a migrate
NAME               IMAGE            COMMAND                  SERVICE   CREATED             STATUS                      PORTS
trials-migrate-1   trials-migrate   "python -m app.migra…"   migrate   About an hour ago   Exited (0) 38 minutes ago
```

### Логи контейнера міграції

```
$ docker compose logs --no-log-prefix migrate
2026-10-14 15:04:12,618 INFO migrate: applied 001_create_trial_participants.sql
2026-10-14 15:04:12,618 INFO migrate: done: 1 new migration(s) applied, 1 file(s) total
2026-10-14 15:26:35,919 INFO migrate: skip 001_create_trial_participants.sql (already applied)
2026-10-14 15:26:35,919 INFO migrate: done: 0 new migration(s) applied, 1 file(s) total
```

### Тека ініціалізації бази порожня

```
$ docker compose exec -T db ls -la /docker-entrypoint-initdb.d
total 8
drwxr-xr-x    2 root     root          4096 Sep 17 21:29 .
drwxr-xr-x    1 root     root          4096 Oct 14 15:04 ..
```

### Монтування контейнера бази (лише том з даними)

```
$ docker inspect --format '{{range .Mounts}}...{{end}}' <db>
volume trials_pgdata -> /var/lib/postgresql/data
```

### Журнал застосованих міграцій

```
$ docker compose exec -T db psql -U trials -d trials -c SELECT version, left(checksum, 12) AS checksum, applied_at FROM schema_migrations
              version              |   checksum   |          applied_at           
-----------------------------------+--------------+-------------------------------
 001_create_trial_participants.sql | a7f6d4eac37e | 2026-10-14 15:04:12.602989+00
(1 row)
```

### Структура таблиці

```
$ docker compose exec -T db psql -U trials -d trials -c \d trial_participants
                                  Table "public.trial_participants"
       Column        |           Type           | Collation | Nullable |           Default            
---------------------+--------------------------+-----------+----------+------------------------------
 id                  | bigint                   |           | not null | generated always as identity
 participant_code    | character varying(32)    |           | not null | 
 protocol            | character varying(64)    |           | not null | 
 randomization_group | character varying(16)    |           | not null | 
 enrollment_date     | date                     |           | not null | 
 status              | character varying(16)    |           | not null | 
 completion_date     | date                     |           |          | 
 created_at          | timestamp with time zone |           | not null | now()
 updated_at          | timestamp with time zone |           | not null | now()
Indexes:
    "trial_participants_pkey" PRIMARY KEY, btree (id)
    "ix_trial_participants_group_id" btree (randomization_group, id)
    "uq_trial_participants_code" UNIQUE CONSTRAINT, btree (participant_code)
Check constraints:
    "ck_trial_participants_dates" CHECK (completion_date IS NULL OR completion_date >= enrollment_date)
    "ck_trial_participants_group" CHECK (randomization_group::text = ANY (ARRAY['A'::character varying, 'B'::character varying, 'контроль'::character varying]::text[]))
    "ck_trial_participants_status" CHECK (status::text = ANY (ARRAY['screening'::character varying, 'active'::character varying, 'completed'::character varying, 'withdrawn'::character varying]::text[]))
```

### Повторний запуск міграції — ідемпотентний (нічого не застосовує)

```
$ docker compose run --rm --no-deps migrate

 Container trials-migrate-run-015abe4ab040 Creating 
 Container trials-migrate-run-015abe4ab040 Created 
2026-10-14 16:04:56,978 INFO migrate: skip 001_create_trial_participants.sql (already applied)
2026-10-14 16:04:56,978 INFO migrate: done: 0 new migration(s) applied, 1 file(s) total
```

## Ліміти ресурсів вебінстансу

### Ліміти контейнера 885e9b4b8ddb

```
$ docker inspect --format '{{.Name}} NanoCpus=... Memory=...' 885e9b4b8ddb
/trials-web-1 NanoCpus=500000000 Memory=536870912
```

### Ліміти контейнера 930a7d1cac9b

```
$ docker inspect --format '{{.Name}} NanoCpus=... Memory=...' 930a7d1cac9b
/trials-web-2 NanoCpus=500000000 Memory=536870912
```

### docker stats (миттєвий знімок)

```
$ docker stats --no-stream --format table {{.Name}}	{{.CPUPerc}}	{{.MemUsage}} 885e9b4b8ddb162ad23426b141839dbd9c9ba89c01a64d8a5a0efc9c2dc0c565 930a7d1cac9b52cd70645687710c9e13edc9d63344086705106bc9f1fba2a41f
NAME           CPU %     MEM USAGE / LIMIT
trials-web-1   0.35%     70.61MiB / 512MiB
trials-web-2   0.29%     52.83MiB / 512MiB
```

NanoCpus=500000000 означає 0.5 CPU; Memory=536870912 — 512 MiB.

## Образ: багатоетапна збірка і відсутність пароля

### Розмір образу

```
$ docker image ls trials-web
IMAGE               ID             DISK USAGE   CONTENT SIZE   EXTRA
trials-web:latest   4178f013107b        157MB         38.7MB   U
```

### Шари образу

```
$ docker history trials-web
IMAGE          CREATED       CREATED BY                                      SIZE      COMMENT
4178f013107b   4 hours ago   CMD ["uvicorn" "app.main:app" "--host" "0.0.…   0B        buildkit.dockerfile.v0
<missing>      4 hours ago   EXPOSE [8000/tcp]                               0B        buildkit.dockerfile.v0
<missing>      4 hours ago   USER app                                        0B        buildkit.dockerfile.v0
<missing>      4 hours ago   RUN /bin/sh -c python -m compileall -q /app/…   61.4kB    buildkit.dockerfile.v0
<missing>      4 hours ago   COPY migrations ./migrations # buildkit         16.4kB    buildkit.dockerfile.v0
<missing>      4 hours ago   COPY app ./app # buildkit                       45.1kB    buildkit.dockerfile.v0
<missing>      4 hours ago   COPY /opt/venv /opt/venv # buildkit             55.4MB    buildkit.dockerfile.v0
<missing>      4 hours ago   WORKDIR /app                                    8.19kB    buildkit.dockerfile.v0
<missing>      4 hours ago   RUN /bin/sh -c addgroup -S -g 10001 app  && …   32.8kB    buildkit.dockerfile.v0
<missing>      4 hours ago   ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFER…   0B        buildkit.dockerfile.v0
<missing>      12 days ago   CMD ["python3"]                                 0B        buildkit.dockerfile.v0
<missing>      12 days ago   RUN /bin/sh -c set -eux;  for src in idle3 p…   16.4kB    buildkit.dockerfile.v0
<missing>      12 days ago   RUN /bin/sh -c set -eux;   apk add --no-cach…   50.4MB    buildkit.dockerfile.v0
<missing>      12 days ago   ENV PYTHON_SHA256=c2c4321961fab0fb999d66e0ce…   0B        buildkit.dockerfile.v0
<missing>      12 days ago   ENV PYTHON_VERSION=3.12.15                      0B        buildkit.dockerfile.v0
<missing>      12 days ago   ENV GPG_KEY=7169605F62C751356D054A26A821E680…   0B        buildkit.dockerfile.v0
<missing>      12 days ago   RUN /bin/sh -c set -eux;  apk add --no-cache…   2.83MB    buildkit.dockerfile.v0
<missing>      12 days ago   ENV LANG=C.UTF-8                                0B        buildkit.dockerfile.v0
<missing>      12 days ago   ENV PATH=/usr/local/bin:/usr/local/sbin:/usr…   0B        buildkit.dockerfile.v0
<missing>      3 weeks ago   CMD ["/bin/sh"]                                 0B        buildkit.dockerfile.v0
<missing>      3 weeks ago   ADD alpine-minirootfs-3.24.2-x86_64.tar.gz /…   9.08MB    buildkit.dockerfile.v0
```

### У фінальній стадії немає компілятора; процес не від root

```
$ docker run --rm --entrypoint sh trials-web -c 'command -v gcc; id; python --version'
gcc: немає
cc: немає
uid=10001(app) gid=10001(app) groups=10001(app),10001(app)
Python 3.12.15
```

### Перевірки пароля

- ✅ пароль з .env відсутній в історії збірки (docker history --no-trunc)
- ✅ пароль з .env відсутній у змінних середовища образу
- ✅ пароль з .env відсутній у docker-compose.yml
- ✅ пароль з .env відсутній у Dockerfile
- ✅ у змінних середовища образу немає DB_PASSWORD / POSTGRES_PASSWORD
- ✅ файл .env не відстежується git

### Змінні середовища образу

```
$ docker image inspect trials-web --format '{{json .Config.Env}}'
["PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin","LANG=C.UTF-8","GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305","PYTHON_VERSION=3.12.15","PYTHON_SHA256=c2c4321961fab0fb999d66e0cecf521c2ab3994c7992873ea99e306c1094fd5a","PYTHONDONTWRITEBYTECODE=1","PYTHONUNBUFFERED=1"]
```

