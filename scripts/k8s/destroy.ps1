# ЛР 3: повне видалення кластера разом з томами (з кореня репозиторію):
#   powershell -ExecutionPolicy Bypass -File scripts\k8s\destroy.ps1
k3d cluster delete trials
docker image rm trials-web:lab3 2>$null
