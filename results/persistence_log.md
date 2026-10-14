# Перевірка збереження даних

### Стенд піднято

```
$ docker compose up -d
 Container trials-lb-1 Running 
 Container trials-db-1 Running 
 Container trials-web-1 Running 
 Container trials-db-1 Waiting 
 Container trials-db-1 Healthy 
 Container trials-migrate-1 Starting 
 Container trials-migrate-1 Started 
 Container trials-db-1 Waiting 
 Container trials-migrate-1 Waiting 
 Container trials-db-1 Healthy 
 Container trials-migrate-1 Exited 
 Container trials-web-1 Waiting 
 Container trials-web-1 Healthy
```

**POST /trial-participants** → `201`, id = 103

- ✅ запис створено (201)

Записів у базі до down: **101**

### Зупинка без видалення томів

```
$ docker compose down
 Container trials-lb-1 Stopping 
 Container trials-lb-1 Stopped 
 Container trials-lb-1 Removing 
 Container trials-lb-1 Removed 
 Container trials-web-1 Stopping 
 Container trials-web-1 Stopped 
 Container trials-web-1 Removing 
 Container trials-web-1 Removed 
 Container trials-migrate-1 Stopping 
 Container trials-migrate-1 Stopped 
 Container trials-migrate-1 Removing 
 Container trials-migrate-1 Removed 
 Container trials-db-1 Stopping 
 Container trials-db-1 Stopped 
 Container trials-db-1 Removing 
 Container trials-db-1 Removed 
 Network trials_frontend Removing 
 Network trials_backend Removing 
 Network trials_backend Removed 
 Network trials_frontend Removed
```

### Том із даними на місці

```
$ docker volume ls --filter name=trials
DRIVER    VOLUME NAME
local     trials_pgdata
```

### Повторне підняття

```
$ docker compose up -d
 Network trials_backend Creating 
 Network trials_backend Creating 
 Network trials_frontend Creating 
 Network trials_frontend Creating 
 Network trials_backend Created 
 Network trials_backend Created 
 Container trials-db-1 Creating 
 Network trials_frontend Created 
 Network trials_frontend Created 
 Container trials-db-1 Created 
 Container trials-migrate-1 Creating 
 Container trials-migrate-1 Created 
 Container trials-web-1 Creating 
 Container trials-web-1 Created 
 Container trials-lb-1 Creating 
 Container trials-lb-1 Created 
 Container trials-db-1 Starting 
 Container trials-db-1 Started 
 Container trials-db-1 Waiting 
 Container trials-db-1 Healthy 
 Container trials-migrate-1 Starting 
 Container trials-migrate-1 Started 
 Container trials-db-1 Waiting 
 Container trials-migrate-1 Waiting 
 Container trials-db-1 Healthy 
 Container trials-migrate-1 Exited 
 Container trials-web-1 Starting 
 Container trials-web-1 Started 
 Container trials-web-1 Waiting 
 Container trials-web-1 Healthy 
 Container trials-lb-1 Starting 
 Container trials-lb-1 Started
```

- ✅ після down/up стенд відповідає 200 на /healthz

**GET /trial-participants/103** після down/up → `200`: `TP-PERSIST-70A17D`

- ✅ запис на місці після down/up

Записів у базі після down/up: **101**

- ✅ кількість записів не змінилась

### Зупинка з видаленням томів

```
$ docker compose down -v
 Container trials-lb-1 Stopping 
 Container trials-lb-1 Stopped 
 Container trials-lb-1 Removing 
 Container trials-lb-1 Removed 
 Container trials-web-1 Stopping 
 Container trials-web-1 Stopped 
 Container trials-web-1 Removing 
 Container trials-web-1 Removed 
 Container trials-migrate-1 Stopping 
 Container trials-migrate-1 Stopped 
 Container trials-migrate-1 Removing 
 Container trials-migrate-1 Removed 
 Container trials-db-1 Stopping 
 Container trials-db-1 Stopped 
 Container trials-db-1 Removing 
 Container trials-db-1 Removed 
 Network trials_backend Removing 
 Volume trials_pgdata Removing 
 Network trials_frontend Removing 
 Volume trials_pgdata Removed 
 Network trials_frontend Removed 
 Network trials_backend Removed
```

### Тому більше немає

```
$ docker volume ls --filter name=trials
DRIVER    VOLUME NAME
```

### Підняття на порожньому томі

```
$ docker compose up -d
 Network trials_backend Creating 
 Network trials_frontend Creating 
 Volume trials_pgdata Creating 
 Network trials_backend Creating 
 Network trials_frontend Creating 
 Volume trials_pgdata Creating 
 Volume trials_pgdata Created 
 Volume trials_pgdata Created 
 Network trials_backend Created 
 Network trials_backend Created 
 Container trials-db-1 Creating 
 Network trials_frontend Created 
 Network trials_frontend Created 
 Container trials-db-1 Created 
 Container trials-migrate-1 Creating 
 Container trials-migrate-1 Created 
 Container trials-web-1 Creating 
 Container trials-web-1 Created 
 Container trials-lb-1 Creating 
 Container trials-lb-1 Created 
 Container trials-db-1 Starting 
 Container trials-db-1 Started 
 Container trials-db-1 Waiting 
 Container trials-db-1 Healthy 
 Container trials-migrate-1 Starting 
 Container trials-migrate-1 Started 
 Container trials-db-1 Waiting 
 Container trials-migrate-1 Waiting 
 Container trials-db-1 Healthy 
 Container trials-migrate-1 Exited 
 Container trials-web-1 Starting 
 Container trials-web-1 Started 
 Container trials-web-1 Waiting 
 Container trials-web-1 Healthy 
 Container trials-lb-1 Starting 
 Container trials-lb-1 Started
```

- ✅ docker compose up -d завершився без помилок

- ✅ стенд відповідає 200 на /healthz

### Міграція знову відпрацювала на порожній базі

```
$ docker compose ps -a migrate
NAME               IMAGE            COMMAND                  SERVICE   CREATED         STATUS                     PORTS
trials-migrate-1   trials-migrate   "python -m app.migra…"   migrate   6 seconds ago   Exited (0) 3 seconds ago
```

Записів у базі після down -v / up: **0**

- ✅ база порожня після down -v


**Підсумок:** усі перевірки пройдено
