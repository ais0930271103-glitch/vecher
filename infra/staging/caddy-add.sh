#!/usr/bin/env bash
# Публикует API через Caddy: кладёт отдельный файл сайта, проверяет конфиг, перезагружает Caddy
# и убеждается, что krug (chat. и call.вечер.online) работает как раньше. При любой проблеме — откат.
set -euo pipefail
cd "$(dirname "$0")"

SITE_FILE=/etc/caddy/vecher-api-test.caddy
MAIN=/etc/caddy/Caddyfile
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/root/backups/caddy-$STAMP
mkdir -p "$BACKUP"
cp -a /etc/caddy/. "$BACKUP/"
echo "Бэкап /etc/caddy → $BACKUP"

code() { curl -s -o /dev/null -m 10 -w '%{http_code}' "$1" || echo 000; }
KRUG_URLS=(https://chat.xn--b1aga8b7a.online https://call.xn--b1aga8b7a.online)
declare -A BEFORE
for u in "${KRUG_URLS[@]}"; do BEFORE[$u]=$(code "$u"); echo "krug до: $u → ${BEFORE[$u]}"; done

rollback() {
  echo "ОТКАТ: возвращаю /etc/caddy из $BACKUP"
  rm -f "$SITE_FILE"
  cp -a "$BACKUP/." /etc/caddy/
  caddy validate --config "$MAIN" --adapter caddyfile && systemctl reload caddy
  exit 1
}

cp api-test.caddy "$SITE_FILE"
if ! grep -qE "^\s*import\s+(/etc/caddy/)?vecher-api-test\.caddy|^\s*import\s+/etc/caddy/\*\.caddy|^\s*import\s+\*\.caddy" "$MAIN"; then
  echo "import $SITE_FILE" >> "$MAIN"
  echo "Добавлен import в $MAIN"
fi

caddy validate --config "$MAIN" --adapter caddyfile || rollback
systemctl reload caddy || rollback
sleep 5

for u in "${KRUG_URLS[@]}"; do
  now=$(code "$u"); echo "krug после: $u → $now"
  [ "$now" = "${BEFORE[$u]}" ] || { echo "krug отвечает иначе, чем до изменений"; rollback; }
done

echo "Проверяю https://api-test.вечер.online (сертификат может выпускаться до минуты)…"
for i in $(seq 1 20); do
  if curl -fsS -m 10 https://api-test.xn--b1aga8b7a.online/health; then echo; echo "Готово."; exit 0; fi
  sleep 5
done
echo "API по HTTPS пока не отвечает. Проверь DNS-запись api-test → IP сервера (krug при этом в порядке)."
