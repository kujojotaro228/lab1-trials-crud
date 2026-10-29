# Крок 5: моніторинг

| Що | Запит PromQL | Рядів | Значення |
|---|---|---:|---|
| запити кожного пода web | `sum by (pod) (rate(http_requests_total{namespace="trials",pod=~"web-.*"}[1m]))` | 1 | web-7d4db69db6-z2twp=5.74 |
| лічильник CFS: періоди (cAdvisor) | `container_cpu_cfs_periods_total{namespace="trials",container="web"}` | 1 | web-7d4db69db6-z2twp=381 |
| лічильник CFS: дроселювання (cAdvisor) | `container_cpu_cfs_throttled_periods_total{namespace="trials",container="web"}` | 1 | web-7d4db69db6-z2twp=22 |
| готові репліки (kube-state-metrics) | `kube_deployment_status_replicas_ready{namespace="trials",deployment="web"}` | 1 | kps-kube-state-metrics-f79f59f5-gkxzg=1 |
| postgres-exporter | `pg_up{namespace="trials"}` | 1 | pg-exporter-prometheus-postgres-exporter-7dfcdf4b77-6nfsw=1 |
| зʼєднання з базою за станом | `sum by (state) (pg_stat_activity_count{namespace="trials",datname="trials"})` | 6 | active=0; disabled=0; fastpath function call=0; idle in transaction=0; idle in transaction (aborted)=0; idle=3 |

Цілі Prometheus (UP / не UP):

* `kps-kube-prometheus-stack-operator`: 1 / 0
* `kps-kube-prometheus-stack-prometheus`: 2 / 0
* `kube-state-metrics`: 1 / 0
* `kubelet`: 6 / 0
* `pg-exporter-prometheus-postgres-exporter`: 1 / 0
* `trials/web`: 1 / 0

```text
$ kubectl top pods -n trials
NAME                                                        CPU(cores)   MEMORY(bytes)   
db-0                                                        19m          25Mi            
pg-exporter-prometheus-postgres-exporter-7dfcdf4b77-6nfsw   2m           8Mi             
web-7d4db69db6-z2twp                                        3m           42Mi
```
