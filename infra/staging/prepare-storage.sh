#!/usr/bin/env bash
# Каталоги только проекта vecher-test. Запускать от root перед обновлением API.
set -euo pipefail
if [ "$(id -u)" != 0 ]; then
  echo "Нужен root для назначения каталогов пользователю API 10001." >&2
  exit 1
fi
install -d -o 10001 -g 10001 -m 755 /opt/vecher-test/downloads
install -d -o 10001 -g 10001 -m 700 /opt/vecher-test/uploads
if [ -L /opt/vecher-test/blog-media ]; then
  echo "blog-media must be a separate real directory, not a symlink." >&2
  exit 1
fi
install -d -o 10001 -g 10001 -m 700 /opt/vecher-test/blog-media
# Новые вложения создаёт сам API с uid 10001. Содержимое uploads не переименовываем и не удаляем.
python3 "$(dirname "$0")/prepare-content.py"
