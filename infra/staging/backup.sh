#!/usr/bin/env bash
# Без параметров сохраняет прежний режим: только БД, без остановки API.
# --consistent: останавливает api и работающий public-video-worker ТОЛЬКО
# проекта vecher-test; сохраняет БД + uploads + downloads + blog-media.
set -euo pipefail
cd "$(dirname "$0")"
MODE=database-only
if [ "$#" -gt 0 ]; then
  if [ "$#" != 1 ] || [ "$1" != --consistent ]; then
    echo "Использование: bash backup.sh [--consistent]" >&2
    exit 2
  fi
  MODE=consistent
fi
umask 077
BACKUP_DIR=/root/backups/vecher-test
install -d -m 700 "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.backup.lock"
flock -n 9 || { echo "Бэкап vecher-test уже выполняется." >&2; exit 1; }
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORK_DIR=$(mktemp -d "$BACKUP_DIR/.working-XXXXXXXX")
RESUME_API=0
RESUME_MEDIA=0
cleanup() {
  local result=$?
  trap - EXIT
  rm -rf -- "$WORK_DIR"
  if [ "$RESUME_API" = 1 ]; then
    if ! docker compose start api </dev/null >/dev/null; then
      echo "Не удалось возобновить api vecher-test; проверьте docker compose ps." >&2
      result=1
    fi
  fi
  if [ "$RESUME_MEDIA" = 1 ]; then
    if ! docker compose --profile public-video start public-video-worker </dev/null >/dev/null; then
      echo "Не удалось возобновить public-video-worker vecher-test." >&2
      result=1
    fi
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ "$MODE" = consistent ] && [ -n "$(docker compose ps --status running -q api)" ]; then
  RESUME_API=1
  docker compose stop --timeout 30 api </dev/null >/dev/null
fi
if [ "$MODE" = consistent ] && [ -n "$(docker compose --profile public-video ps --status running -q public-video-worker)" ]; then
  RESUME_MEDIA=1
  docker compose --profile public-video stop --timeout 30 public-video-worker </dev/null >/dev/null
fi
# -T отключает TTY, но не stdin: не отдаём Docker оставшиеся команды вызывающего heredoc.
docker compose exec -T db pg_dump -U vecher vecher </dev/null | gzip > "$WORK_DIR/vecher-$STAMP.sql.gz"
gzip -t "$WORK_DIR/vecher-$STAMP.sql.gz"
if [ "$MODE" = consistent ]; then
  for part in uploads downloads blog-media; do
    if [ -L "/opt/vecher-test/$part" ]; then
      echo "Отказ от бэкапа: $part должен быть настоящим каталогом." >&2
      exit 1
    fi
    if [ -d "/opt/vecher-test/$part" ]; then
      tar --numeric-owner -czf "$WORK_DIR/vecher-$STAMP.$part.tar.gz" -C /opt/vecher-test "$part"
    fi
  done
fi
{
  printf 'mode=%s\ncreated_utc=%s\n' "$MODE" "$STAMP"
  printf 'commit=%s\n' "$(git -C ../.. rev-parse HEAD 2>/dev/null || echo unknown)"
  printf 'data_keys=keep the matching VECHER_DATA_KEYS separately in protected storage\n'
  printf 'media_worker_was_running=%s\n' "$RESUME_MEDIA"
} > "$WORK_DIR/vecher-$STAMP.manifest.txt"
for part in "$WORK_DIR"/*; do
  case "$part" in *.manifest.txt) continue ;; esac
  mv -- "$part" "$BACKUP_DIR/"
done
# Manifest публикуем последним: его наличие означает, что все части бэкапа перенесены.
mv -- "$WORK_DIR/vecher-$STAMP.manifest.txt" "$BACKUP_DIR/"
find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'vecher-*.sql.gz' -o -name 'vecher-*.uploads.tar.gz' -o -name 'vecher-*.downloads.tar.gz' -o -name 'vecher-*.blog-media.tar.gz' -o -name 'vecher-*.manifest.txt' \) \
  -mtime +14 -delete
printf 'BACKUP_OK: %s/vecher-%s (%s)\n' "$BACKUP_DIR" "$STAMP" "$MODE"
