# Крок 3: дані після видалення пода бази

| Крок | Результат |
|---|---|
| `POST /trial-participants` | 201, id = 102, код `TP-PERSIST-01899`; записів у базі — 101 |
| `kubectl delete pod db-0` | под `a0d29f49-0201-477c-98dd-4acd58620a7f` видалено |
| StatefulSet створив новий под | `135cdd48-5245-4527-a026-7de480a0b5fd`, Ready через 3.2 с |
| PersistentVolume | до: `pvc-98013285-93dc-4ebc-bbec-87ca85687397`, після: `pvc-98013285-93dc-4ebc-bbec-87ca85687397` |
| `GET /trial-participants/102` | **200**, `TP-PERSIST-01899` |
| Кількість записів | 101 (було 101) |
