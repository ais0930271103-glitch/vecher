#!/usr/bin/env bash
# Обновить тестовый сервер: свежий код из GitHub → пересборка → проверка.
set -euo pipefail
cd "$(dirname "$0")"
git -C ../.. pull --ff-only
# vecher.apk и version.json: пишет сервер в контейнере (пользователь 10001) — автосборка GitHub
mkdir -p /opt/vecher-test/downloads && chown -R 10001:10001 /opt/vecher-test/downloads
# Ключ Firebase читает сервер в контейнере (пользователь 10001)
mkdir -p secrets && chown -R 10001:10001 secrets && chmod 700 secrets && { chmod 600 secrets/* 2>/dev/null || true; }
docker compose up -d --build
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8092/health >/dev/null 2>&1 && { echo "OK: $(curl -fsS http://127.0.0.1:8092/health)"; exit 0; }
  sleep 2
done
echo "API не поднялся:"; docker compose logs --tail=60 api; exit 1
