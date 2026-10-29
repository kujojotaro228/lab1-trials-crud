# ЛР 3: розгортання стенда в k3d однією командою (з кореня репозиторію):
#   powershell -ExecutionPolicy Bypass -File scripts\k8s\deploy.ps1
# Потрібні: Docker Desktop (запущений), k3d, kubectl, helm; файл .env з POSTGRES_PASSWORD.
param([switch]$SkipCluster)            # -SkipCluster: кластер уже є, лише образ і маніфести

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root
$Cluster = "trials"
$Image = "trials-web:lab3"

function Run {                        # запуск зовнішньої команди з перевіркою коду виходу
    Write-Host "> $($args -join ' ')" -ForegroundColor Cyan
    $rest = @()
    if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }
    & $args[0] @rest
    if ($LASTEXITCODE -ne 0) { throw "команда завершилась з кодом ${LASTEXITCODE}: $($args -join ' ')" }
}

# --- 1. кластер (крок 1) і мітка role=infra на server (крок 2) ---
if (-not $SkipCluster) {
    Run k3d cluster create --config k8s/k3d-cluster.yaml
}
Run kubectl label node "k3d-$Cluster-server-0" role=infra --overwrite
Run kubectl wait --for=condition=Ready nodes --all --timeout=180s

# --- 2. образ сервісу: збірка і завантаження в кластер ---
Run docker build -t $Image .
Run k3d image import $Image -c $Cluster

# --- 3. простір імен і Secret з .env (пароль не потрапляє в репозиторій) ---
Run kubectl apply -f k8s/namespace.yaml
$line = Get-Content .env | Where-Object { $_ -match '^\s*POSTGRES_PASSWORD\s*=' } | Select-Object -First 1
if (-not $line) { throw "у .env немає POSTGRES_PASSWORD" }
$pw = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
$secret = kubectl -n trials create secret generic db-secret --from-literal=POSTGRES_PASSWORD=$pw --dry-run=client -o yaml
$secret | kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw "не вдалося створити Secret db-secret" }

# --- 4. моніторинг (крок 5): спершу, бо PodMonitor потребує CRD ---
Run helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
Run helm repo update
Run helm upgrade --install kps prometheus-community/kube-prometheus-stack `
    -n monitoring --create-namespace -f k8s/monitoring/kps-values.yaml --wait --timeout 15m

# --- 5. база і міграція (крок 3) ---
Run kubectl apply -f k8s/web-config.yaml -f k8s/db.yaml
Run kubectl -n trials rollout status statefulset/db --timeout=300s
Run kubectl -n trials delete job migrate --ignore-not-found
Run kubectl apply -f k8s/migrate-job.yaml
Run kubectl -n trials wait --for=condition=complete job/migrate --timeout=300s
Run kubectl -n trials logs job/migrate

# --- 6. сервіс web (крок 4), PodMonitor, Ingress (Д3), postgres-exporter ---
Run kubectl apply -f k8s/web.yaml -f k8s/web-podmonitor.yaml -f k8s/ingress.yaml
Run kubectl -n trials rollout restart deployment/web          # підхопити новий образ
Run kubectl -n trials rollout status deployment/web --timeout=300s
Run helm upgrade --install pg-exporter prometheus-community/prometheus-postgres-exporter `
    -n trials -f k8s/monitoring/pg-exporter-values.yaml --wait --timeout 10m

Run kubectl get nodes -o wide --show-labels
Run kubectl get pods -A -o wide
Write-Host "Готово: web http://localhost:8080, Prometheus http://localhost:9090, Ingress http://localhost:8081" -ForegroundColor Green
