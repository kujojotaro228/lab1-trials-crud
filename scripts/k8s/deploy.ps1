# Labs 3–4: deploy the stand to k3d with one command (from the repository root):
#   powershell -ExecutionPolicy Bypass -File scripts\k8s\deploy.ps1
# Requires: Docker Desktop (running), k3d, kubectl, helm; a .env file with POSTGRES_PASSWORD.
param([switch]$SkipCluster)            # -SkipCluster: the cluster already exists, only images and manifests

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root
$Cluster = "trials"
$Image = "trials-web:lab4"
$WorkerImage = "trials-worker:lab4"     # Lab 4

function Run {                        # run an external command and check its exit code
    Write-Host "> $($args -join ' ')" -ForegroundColor Cyan
    $rest = @()
    if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }
    & $args[0] @rest
    if ($LASTEXITCODE -ne 0) { throw "команда завершилась з кодом ${LASTEXITCODE}: $($args -join ' ')" }
}

# --- 1. cluster (step 1) and the role=infra label on the server (step 2) ---
if (-not $SkipCluster) {
    Run k3d cluster create --config k8s/k3d-cluster.yaml
}
Run kubectl label node "k3d-$Cluster-server-0" role=infra --overwrite
Run kubectl wait --for=condition=Ready nodes --all --timeout=180s

# --- 2. service images: build and import into the cluster ---
Run docker build -t $Image .
Run docker build -f worker/Dockerfile -t $WorkerImage .
Run k3d image import $Image $WorkerImage -c $Cluster

# --- 3. namespace and Secret from .env (the password never enters the repository) ---
Run kubectl apply -f k8s/namespace.yaml
$line = Get-Content .env | Where-Object { $_ -match '^\s*POSTGRES_PASSWORD\s*=' } | Select-Object -First 1
if (-not $line) { throw "у .env немає POSTGRES_PASSWORD" }
$pw = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
$secret = kubectl -n trials create secret generic db-secret --from-literal=POSTGRES_PASSWORD=$pw --dry-run=client -o yaml
$secret | kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw "не вдалося створити Secret db-secret" }

# --- 4. monitoring (step 5): first, because PodMonitor needs the CRDs ---
Run helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
Run helm repo update
Run helm upgrade --install kps prometheus-community/kube-prometheus-stack `
    -n monitoring --create-namespace -f k8s/monitoring/kps-values.yaml --wait --timeout 15m

# --- 5. database and migration (step 3) ---
Run kubectl apply -f k8s/web-config.yaml -f k8s/db.yaml
Run kubectl -n trials rollout status statefulset/db --timeout=300s
Run kubectl -n trials delete job migrate --ignore-not-found
Run kubectl apply -f k8s/migrate-job.yaml
Run kubectl -n trials wait --for=condition=complete job/migrate --timeout=300s
Run kubectl -n trials logs job/migrate

# --- 6. web service (step 4), PodMonitor, Ingress (D3), postgres-exporter ---
Run kubectl apply -f k8s/web.yaml -f k8s/web-podmonitor.yaml -f k8s/ingress.yaml
Run kubectl -n trials rollout restart deployment/web          # pick up the new image
Run kubectl -n trials rollout status deployment/web --timeout=300s
Run helm upgrade --install pg-exporter prometheus-community/prometheus-postgres-exporter `
    -n trials -f k8s/monitoring/pg-exporter-values.yaml --wait --timeout 10m

# --- 7. Lab 4: worker pool and its PodMonitor; web — 2 replicas, Lab 3 HPA removed ---
Run kubectl -n trials delete hpa web --ignore-not-found
Run kubectl -n trials scale deployment/web --replicas=2
Run kubectl -n trials rollout status deployment/web --timeout=300s
Run kubectl apply -f k8s/worker.yaml
Run kubectl -n trials rollout restart deployment/worker       # pick up the new image
Run kubectl -n trials rollout status deployment/worker --timeout=300s

Run kubectl get nodes -o wide --show-labels
Run kubectl get pods -A -o wide
Write-Host "Готово: web http://localhost:8080, Prometheus http://localhost:9090, Ingress http://localhost:8081" -ForegroundColor Green
