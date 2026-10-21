#!/usr/bin/env bash
# Сначала checkout проверенного коммита, затем бэкап → миграция → запуск.
# Без скрытого git pull: разворачивается именно выбранный исходный код.
exec bash "$(dirname "$0")/deploy-realtime.sh" "$@"
