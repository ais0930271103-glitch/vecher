#!/usr/bin/env bash
# Deploy the checked-out source. Never pull an unreviewed branch or downgrade DB.
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")"
VECHER_ENABLE=0
if [ "$#" -gt 0 ]; then
  [ "$#" = 1 ] && [ "$1" = --enable-realtime ] || {
    echo 'Usage: bash deploy-realtime.sh [--enable-realtime]' >&2; exit 2;
  }
  VECHER_ENABLE=1
fi
[ "$(id -u)" = 0 ] || { echo 'Run as root on the staging server.' >&2; exit 1; }
export COMPOSE_PROJECT_NAME=vecher-test
export COMPOSE_FILE="$PWD/docker-compose.yml"
test -f .env && test ! -L .env
test -z "$(git -C ../.. status --porcelain)" || {
  echo 'DEPLOY_STOPPED: commit or save local changes before deployment.' >&2; exit 1;
}
docker compose config --quiet
install -d -m 700 /root/backups/vecher-test
exec 8>/root/backups/vecher-test/.deploy.lock
flock -n 8 || { echo 'DEPLOY_STOPPED: another deployment is running.' >&2; exit 1; }
VECHER_CONTAINER=$(docker compose ps -q api)
test -n "$VECHER_CONTAINER" || { echo 'Use install.sh for a new server.' >&2; exit 1; }
VECHER_OLD_IMAGE=$(docker inspect --format '{{.Image}}' "$VECHER_CONTAINER")
VECHER_SAVE=$(mktemp -d /root/backups/vecher-test/deploy-XXXXXXXX)
VECHER_ROLLBACK_IMAGE="vecher-test-api:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
docker image tag "$VECHER_OLD_IMAGE" "$VECHER_ROLLBACK_IMAGE"
printf 'services:\n  api:\n    image: %s\n' "$VECHER_ROLLBACK_IMAGE" > "$VECHER_SAVE/rollback-compose.yml"
printf '%s\n' "$VECHER_OLD_IMAGE" > "$VECHER_SAVE/old-image-id"
git -C ../.. rev-parse HEAD > "$VECHER_SAVE/source.sha"
install -m 600 .env "$VECHER_SAVE/staging.env"
VECHER_CADDY=/etc/caddy/vecher-api-test.caddy
VECHER_CENTRI_WAS_RUNNING=$(docker compose --profile realtime ps --status running -q centrifugo)
if [ -n "$VECHER_CENTRI_WAS_RUNNING" ]; then
  VECHER_OLD_CENTRI=$(docker inspect --format '{{.Image}}' "$VECHER_CENTRI_WAS_RUNNING")
  VECHER_CENTRI_ROLLBACK="vecher-test-centrifugo:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
  docker image tag "$VECHER_OLD_CENTRI" "$VECHER_CENTRI_ROLLBACK"
  printf '  centrifugo:\n    image: %s\n' "$VECHER_CENTRI_ROLLBACK" >> "$VECHER_SAVE/rollback-compose.yml"
fi
if [ -f secrets/centrifugo.json ]; then
  install -m 600 secrets/centrifugo.json "$VECHER_SAVE/centrifugo.json"
fi
if [ "$VECHER_ENABLE" = 1 ]; then
  test -f "$VECHER_CADDY" && test ! -L "$VECHER_CADDY"
  command -v caddy >/dev/null
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  install -m 600 "$VECHER_CADDY" "$VECHER_SAVE/site.caddy"
  if [ -z "$VECHER_CENTRI_WAS_RUNNING" ] && [ -n "$(ss -H -lnt 'sport = :8093')" ]; then
    echo 'DEPLOY_STOPPED: port8093 is already occupied.' >&2; exit 1;
  fi
  docker compose --profile realtime pull centrifugo
fi
VECHER_CHANGED=0
VECHER_CADDY_CHANGED=0
VECHER_OK=0
rollback() {
  local result=$?
  trap - EXIT INT TERM
  if [ "$VECHER_CHANGED" = 1 ] && [ "$VECHER_OK" != 1 ]; then
    # Attempt every recovery step even if an earlier restore fails.
    set +e
    local restore_failed=0
    echo "DEPLOY_FAILED: restoring API image and settings from $VECHER_SAVE" >&2
    docker compose stop --timeout 30 api >/dev/null || true
    install -m 600 "$VECHER_SAVE/staging.env" .env || {
      echo 'ROLLBACK_ENV_RESTORE_FAILED' >&2; restore_failed=1;
    }
    if [ "$VECHER_ENABLE" = 1 ]; then
      if [ -f "$VECHER_SAVE/centrifugo.json" ]; then
        install -o 10001 -g 10001 -m 600 "$VECHER_SAVE/centrifugo.json" secrets/centrifugo.json || {
          echo 'ROLLBACK_CENTRIFUGO_CONFIG_FAILED' >&2; restore_failed=1;
        }
      fi
      if [ -n "$VECHER_CENTRI_WAS_RUNNING" ]; then
        docker compose -f docker-compose.yml -f "$VECHER_SAVE/rollback-compose.yml" \
          --profile realtime up -d --no-deps --no-build --force-recreate centrifugo || {
          echo 'ROLLBACK_CENTRIFUGO_START_FAILED' >&2; restore_failed=1;
        }
      else
        docker compose --profile realtime stop centrifugo >/dev/null || true
      fi
    fi
    if [ "$VECHER_CADDY_CHANGED" = 1 ]; then
      if install -m 644 "$VECHER_SAVE/site.caddy" "$VECHER_CADDY" && \
          caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
        systemctl reload caddy || restore_failed=1
      else
        echo 'ROLLBACK_CADDY_RESTORE_FAILED' >&2; restore_failed=1
      fi
    fi
    if docker compose -f docker-compose.yml -f "$VECHER_SAVE/rollback-compose.yml" \
        up -d --no-deps --no-build api; then
      local healthy=0
      for attempt in {1..20}; do
        if docker compose exec -T api python -c \
          'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3)' \
          </dev/null >/dev/null 2>&1; then healthy=1; break; fi
        sleep 2
      done
      if [ "$healthy" = 1 ]; then
        echo 'PREVIOUS_API_HEALTHY: the additive DB migration was retained.' >&2
      else
        echo 'PREVIOUS_API_NOT_HEALTHY: manual recovery needed.' >&2; restore_failed=1
      fi
    else
      echo "ROLLBACK_START_FAILED: saved image/config are in $VECHER_SAVE" >&2
      restore_failed=1
    fi
    [ "$restore_failed" = 0 ] || echo "ROLLBACK_INCOMPLETE: inspect $VECHER_SAVE locally." >&2
    [ "$result" -ne 0 ] || result=1
  fi
  exit "$result"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
bash prepare-storage.sh
docker compose build api
# This script restores the previous API image on failure. Starting with 0.4.0,
# the previous strict-head API cannot run after a schema transition. Stop before
# backup/write interruption until the release has a compatible recovery path.
docker compose run --rm --no-deps api python - < check-deploy-schema.py
# Login codes must go to the user's mailbox, never to server logs. Check only
# configuration presence here; this command does not send mail or print secrets.
docker compose run --rm --no-deps api python -c \
  'from app.mailer import smtp_configured; import sys; sys.exit(0 if smtp_configured() else "SMTP_NOT_CONFIGURED: configure SMTP in staging .env before updating")' </dev/null
# The backup resumes the previous API; stop it again before schema changes.
VECHER_CHANGED=1
bash backup.sh --consistent | tee "$VECHER_SAVE/backup.log"
docker compose stop --timeout 30 api
if [ "$VECHER_ENABLE" = 1 ]; then
  python3 prepare-realtime.py --enable
  docker compose --profile realtime run --rm --no-deps centrifugo \
    centrifugo checkconfig --strict --config=/centrifugo/config.json </dev/null
fi
docker compose run --rm --no-deps api python -m app.migrate upgrade </dev/null
if [ "$VECHER_ENABLE" = 1 ]; then
  docker compose --profile realtime up -d --no-deps --force-recreate centrifugo
  VECHER_CADDY_CHANGED=1
  install -m 644 api-test.caddy "$VECHER_CADDY"
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  systemctl reload caddy
fi
docker compose up -d --no-deps --no-build api
VECHER_HEALTH=0
for VECHER_ATTEMPT in {1..30}; do
  if docker compose exec -T api python -c \
    'import json, urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3)); assert d.get("ok") is True and d.get("env") == "staging"' \
    </dev/null >/dev/null 2>&1; then VECHER_HEALTH=1; break; fi
  sleep 2
done
[ "$VECHER_HEALTH" = 1 ] || { echo 'API_NOT_READY' >&2; exit 1; }
docker compose exec -T api python -m app.migrate check </dev/null
if [ "$VECHER_ENABLE" = 1 ]; then
  docker compose exec -T api python - < smoke-realtime.py
fi
VECHER_OK=1
docker compose --profile realtime ps
printf 'DEPLOY_BACKUP=%s\n' "$VECHER_SAVE"
printf 'VECHER_SERVER_UPDATE_OK\n'
