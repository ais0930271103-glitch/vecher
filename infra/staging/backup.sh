#!/usr/bin/env bash
# Ежедневный бэкап базы тестового сервера (хранится 14 дней). Cron:  15 4 * * * /opt/vecher-test/repo/infra/staging/backup.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR=/root/backups/vecher-test
mkdir -p "$DIR"
docker compose exec -T db pg_dump -U vecher vecher | gzip > "$DIR/vecher-$(date +%Y%m%d-%H%M).sql.gz"
find "$DIR" -name 'vecher-*.sql.gz' -mtime +14 -delete
