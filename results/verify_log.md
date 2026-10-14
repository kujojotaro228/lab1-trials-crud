# Журнал перевірки (етап 9)

Базова адреса: `http://localhost:8080`

### 1. Готовність сервісу

**Запит:** `GET /healthz`

**Відповідь:** `200` (очікувалось 200) +

```json
{
  "status": "ok",
  "instance": "93b05b6d388a"
}
```

### 2. Створення запису

**Запит:** `POST /trial-participants`

```json
{
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-001",
  "randomization_group": "A",
  "enrollment_date": "2025-03-01",
  "status": "active",
  "completion_date": null
}
```

**Відповідь:** `201` (очікувалось 201) +

```json
{
  "id": 1,
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-001",
  "randomization_group": "A",
  "enrollment_date": "2025-03-01",
  "status": "active",
  "completion_date": null,
  "created_at": "2026-10-14T13:35:57.996682+00:00",
  "updated_at": "2026-10-14T13:35:57.996682+00:00"
}
```

### 3. Створення з невалідною групою рандомізації (правило варіанта)

**Запит:** `POST /trial-participants`

```json
{
  "participant_code": "TP-VERIFY-BAD754-X",
  "protocol": "CT-2025-001",
  "randomization_group": "C",
  "enrollment_date": "2025-03-01",
  "status": "active",
  "completion_date": null
}
```

**Відповідь:** `400` (очікувалось 400) +

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

### 4. Створення з датою завершення раніше дати включення

**Запит:** `POST /trial-participants`

```json
{
  "participant_code": "TP-VERIFY-BAD754-Y",
  "protocol": "CT-2025-001",
  "randomization_group": "A",
  "enrollment_date": "2025-03-01",
  "status": "completed",
  "completion_date": "2025-01-01"
}
```

**Відповідь:** `400` (очікувалось 400) +

```json
{
  "error": "validation_error",
  "message": "Невалідні дані запиту",
  "details": [
    {
      "field": "completion_date",
      "location": "body",
      "message": "дата завершення не може бути раніше дати включення (2025-03-01)"
    }
  ]
}
```

### 5. Створення без обовʼязкового поля protocol

**Запит:** `POST /trial-participants`

```json
{
  "participant_code": "TP-VERIFY-BAD754-Z",
  "randomization_group": "A",
  "enrollment_date": "2025-03-01",
  "status": "active",
  "completion_date": null
}
```

**Відповідь:** `400` (очікувалось 400) +

```json
{
  "error": "validation_error",
  "message": "Невалідні дані запиту",
  "details": [
    {
      "field": "protocol",
      "location": "body",
      "message": "поле обовʼязкове"
    }
  ]
}
```

### 6. Створення з уже наявним кодом учасника

**Запит:** `POST /trial-participants`

```json
{
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-001",
  "randomization_group": "A",
  "enrollment_date": "2025-03-01",
  "status": "active",
  "completion_date": null
}
```

**Відповідь:** `409` (очікувалось 409) +

```json
{
  "error": "conflict",
  "message": "Учасник з таким кодом уже існує",
  "details": [
    {
      "field": "participant_code",
      "location": "body",
      "message": "значення 'TP-VERIFY-BAD754' вже використовується"
    }
  ]
}
```

### 7. Перелік з фільтром за групою A

**Запит:** `GET /trial-participants?randomization_group=A&limit=100`

**Відповідь:** `200` (очікувалось 200) +

```json
{
  "items": [
    {
      "id": 1,
      "participant_code": "TP-VERIFY-BAD754",
      "protocol": "CT-2025-001",
      "randomization_group": "A",
      "enrollment_date": "2025-03-01",
      "status": "active",
      "completion_date": null,
      "created_at": "2026-10-14T13:35:57.996682+00:00",
      "updated_at": "2026-10-14T13:35:57.996682+00:00"
    }
  ],
  "total": 1,
  "limit": 100,
  "offset": 0
}
```

_Створений запис id=1 присутній у переліку (total=1)._

### 8. Перелік з невалідним фільтром

**Запит:** `GET /trial-participants?randomization_group=D`

**Відповідь:** `400` (очікувалось 400) +

```json
{
  "error": "validation_error",
  "message": "Невалідні дані запиту",
  "details": [
    {
      "field": "randomization_group",
      "location": "query",
      "message": "фільтр має бути одним із значень: A, B, контроль; отримано 'D'"
    }
  ]
}
```

### 9. Перелік з limit понад верхню межу

**Запит:** `GET /trial-participants?limit=1000`

**Відповідь:** `400` (очікувалось 400) +

```json
{
  "error": "validation_error",
  "message": "Невалідні дані запиту",
  "details": [
    {
      "field": "limit",
      "location": "query",
      "message": "значення має бути не більше за 100"
    }
  ]
}
```

### 10. Отримання запису за id

**Запит:** `GET /trial-participants/1`

**Відповідь:** `200` (очікувалось 200) +

```json
{
  "id": 1,
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-001",
  "randomization_group": "A",
  "enrollment_date": "2025-03-01",
  "status": "active",
  "completion_date": null,
  "created_at": "2026-10-14T13:35:57.996682+00:00",
  "updated_at": "2026-10-14T13:35:57.996682+00:00"
}
```

### 11. Повна заміна запису (PUT)

**Запит:** `PUT /trial-participants/1`

```json
{
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-002",
  "randomization_group": "контроль",
  "enrollment_date": "2025-03-01",
  "status": "completed",
  "completion_date": "2025-09-30"
}
```

**Відповідь:** `200` (очікувалось 200) +

```json
{
  "id": 1,
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-002",
  "randomization_group": "контроль",
  "enrollment_date": "2025-03-01",
  "status": "completed",
  "completion_date": "2025-09-30",
  "created_at": "2026-10-14T13:35:57.996682+00:00",
  "updated_at": "2026-10-14T13:35:58.148118+00:00"
}
```

### 12. Заміна з невалідною групою

**Запит:** `PUT /trial-participants/1`

```json
{
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-002",
  "randomization_group": "плацебо",
  "enrollment_date": "2025-03-01",
  "status": "completed",
  "completion_date": "2025-09-30"
}
```

**Відповідь:** `400` (очікувалось 400) +

```json
{
  "error": "validation_error",
  "message": "Невалідні дані запиту",
  "details": [
    {
      "field": "randomization_group",
      "location": "body",
      "message": "група рандомізації має бути одним із значень: A, B, контроль; отримано 'плацебо'"
    }
  ]
}
```

### 13. Отримання після заміни

**Запит:** `GET /trial-participants/1`

**Відповідь:** `200` (очікувалось 200) +

```json
{
  "id": 1,
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-002",
  "randomization_group": "контроль",
  "enrollment_date": "2025-03-01",
  "status": "completed",
  "completion_date": "2025-09-30",
  "created_at": "2026-10-14T13:35:57.996682+00:00",
  "updated_at": "2026-10-14T13:35:58.148118+00:00"
}
```

### 14. Перелік з фільтром за групою «контроль»

**Запит:** `GET /trial-participants?randomization_group=%D0%BA%D0%BE%D0%BD%D1%82%D1%80%D0%BE%D0%BB%D1%8C&limit=5`

**Відповідь:** `200` (очікувалось 200) +

```json
{
  "items": [
    {
      "id": 1,
      "participant_code": "TP-VERIFY-BAD754",
      "protocol": "CT-2025-002",
      "randomization_group": "контроль",
      "enrollment_date": "2025-03-01",
      "status": "completed",
      "completion_date": "2025-09-30",
      "created_at": "2026-10-14T13:35:57.996682+00:00",
      "updated_at": "2026-10-14T13:35:58.148118+00:00"
    }
  ],
  "total": 1,
  "limit": 5,
  "offset": 0
}
```

### 15. Видалення

**Запит:** `DELETE /trial-participants/1`

**Відповідь:** `204` (очікувалось 204) +

### 16. Повторне видалення того самого id

**Запит:** `DELETE /trial-participants/1`

**Відповідь:** `404` (очікувалось 404) +

```json
{
  "error": "not_found",
  "message": "Учасника з id=1 не знайдено",
  "details": []
}
```

### 17. Отримання видаленого запису

**Запит:** `GET /trial-participants/1`

**Відповідь:** `404` (очікувалось 404) +

```json
{
  "error": "not_found",
  "message": "Учасника з id=1 не знайдено",
  "details": []
}
```

### 18. Заміна неіснуючого запису

**Запит:** `PUT /trial-participants/1`

```json
{
  "participant_code": "TP-VERIFY-BAD754",
  "protocol": "CT-2025-002",
  "randomization_group": "контроль",
  "enrollment_date": "2025-03-01",
  "status": "completed",
  "completion_date": "2025-09-30"
}
```

**Відповідь:** `404` (очікувалось 404) +

```json
{
  "error": "not_found",
  "message": "Учасника з id=1 не знайдено",
  "details": []
}
```

### 19. Некоректний id у шляху

**Запит:** `GET /trial-participants/abc`

**Відповідь:** `400` (очікувалось 400) +

```json
{
  "error": "validation_error",
  "message": "Невалідні дані запиту",
  "details": [
    {
      "field": "id",
      "location": "path",
      "message": "очікується ціле число"
    }
  ]
}
```

**Підсумок:** 19 з 19 кроків дали очікуваний код.
