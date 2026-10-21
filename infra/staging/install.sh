#!/usr/bin/env bash
# Первая установка тестового сервера «Вечера». Запускать от root из /opt/vecher-test/repo/infra/staging.
# Ничего не трогает в krug: только свой проект Docker, свой порт 127.0.0.1:8092, свой файл Caddy.
set -euo pipefail
cd "$(dirname "$0")"
bash ./prepare-storage.sh

say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }

say "1/4 Секреты (.env)"
if [ ! -f .env ]; then
  cp env.example .env
  rnd() { openssl rand -base64 48 | tr -d '\n/+=' | head -c "$1"; }
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rnd 32)|" .env
  sed -i "s|^VECHER_JWT_SECRET=.*|VECHER_JWT_SECRET=$(rnd 64)|" .env
  sed -i "s|^VECHER_DATA_KEYS=.*|VECHER_DATA_KEYS=s1:$(openssl rand -base64 32)|" .env
  chmod 600 .env
  echo "Создан .env со случайными секретами. СОХРАНИ строку VECHER_DATA_KEYS в менеджер паролей:"
  grep '^VECHER_DATA_KEYS=' .env
else
  echo ".env уже есть — не трогаю."
fi
mkdir -p secrets && chmod 700 secrets
# Сервер в контейнере работает от пользователя 10001 — ключ должен быть читаем только им.
chown -R 10001:10001 secrets && chmod 600 secrets/* 2>/dev/null || true
[ -f secrets/firebase-key.json ] || echo "ВНИМАНИЕ: нет secrets/firebase-key.json — push работать не будет (можно положить позже)."

say "2/4 Сборка и запуск (проект vecher-test)"
docker compose build api
docker compose up -d db
docker compose run --rm api python -m app.migrate upgrade </dev/null
docker compose up -d --no-deps --no-build api

say "3/4 Проверка API"
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8092/health >/dev/null 2>&1; then echo "API отвечает: $(curl -fsS http://127.0.0.1:8092/health)"; break; fi
  sleep 2
  [ "$i" = 30 ] && { echo "API не поднялся. Лог:"; docker compose logs --tail=60 api; exit 1; }
done

say "4/4 Готово"
echo "Дальше: bash caddy-add.sh  (публикует https://api-test.вечер.online через Caddy с проверкой krug)."
