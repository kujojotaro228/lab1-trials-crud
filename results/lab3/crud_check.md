# Крок 4: повний цикл через localhost:8080

Реплік web: 1 (web-7d4db69db6-z2twp @ k3d-trials-agent-1).

| № | Крок | Запит | Очікувано | Отримано | Под (X-Pod) |
|---:|---|---|---:|---|---|
| 1 | Створення | `POST /trial-participants` | 201 | ✅ 201 | `web-7d4db69db6-z2twp` |
| 2 | Читання | `GET /trial-participants/101` | 200 | ✅ 200 | `web-7d4db69db6-z2twp` |
| 3 | Заміна | `PUT /trial-participants/101` | 200 | ✅ 200 | `web-7d4db69db6-z2twp` |
| 4 | Читання після заміни | `GET /trial-participants/101` | 200 | ✅ 200 | `web-7d4db69db6-z2twp` |
| 5 | Видалення | `DELETE /trial-participants/101` | 204 | ✅ 204 | `web-7d4db69db6-z2twp` |
| 6 | Читання видаленого | `GET /trial-participants/101` | 404 | ✅ 404 | `web-7d4db69db6-z2twp` |
