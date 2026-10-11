#!/usr/bin/env bash
# Обновить тестовый сервер: свежий код из GitHub → пересборка → проверка.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p /opt/vecher-test/downloads  # vecher.apk и version.json для обновления приложения
git -C ../.. pull --ff-only
docker compose up -d --build
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8092/health >/dev/null 2>&1 && { echo "OK: $(curl -fsS http://127.0.0.1:8092/health)"; exit 0; }
  sleep 2
done
echo "API не поднялся:"; docker compose logs --tail=60 api; exit 1
