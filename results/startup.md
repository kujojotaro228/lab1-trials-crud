# Розмір образу і час старту

Образ вебсервісу `trials-web`: **156.6 MB** (149.4 MiB, 156645903 байт).

```
$ docker image ls trials-web
IMAGE               ID             DISK USAGE   CONTENT SIZE   EXTRA
trials-web:latest   4178f013107b        157MB         38.7MB   U
```

Час від `docker compose up -d` до першої відповіді 200 на `/healthz` (образи зібрані, томи порожні — перед кожним повтором `docker compose down -v`):

| Повтор | До 200 на /healthz, с | `up -d` завершився, с |
|---:|---:|---:|
| 1 | 7.26 | 7.26 |
| 2 | 6.73 | 6.73 |
| 3 | 6.75 | 6.75 |

**Медіана: 6.75 с**
