# Тестовый сервер «Вечера» (api-test.вечер.online)

Живёт в `/opt/vecher-test/repo` (клон репозитория). Docker-проект `vecher-test`, API на `127.0.0.1:8092`,
наружу — через Caddy отдельным файлом `/etc/caddy/vecher-api-test.caddy`. krug не трогаем.

- `install.sh` — первая установка (секреты, сборка, запуск).
- `caddy-add.sh` — публикация через Caddy с бэкапом, проверкой krug и автооткатом.
- `update.sh` — обновление после `git push`.
- `prepare-storage.sh` — постоянные downloads/uploads, владелец API uid/gid 10001.
- `backup.sh` — прежний бэкап БД без аргументов; `--consistent` сохраняет БД, uploads и downloads с короткой остановкой только нашего API.
- Секреты: `.env` (создаёт install.sh) и `secrets/firebase-key.json` — только на сервере, в git не попадают.
- Логи: `docker compose logs -f api`. Код входа без почты: `docker compose logs api | grep "код для"`.

## Чат, файлы и звонки

Приватные файлы хранятся отдельно в `/opt/vecher-test/uploads` и монтируются в `/srv/uploads`. Этот каталог не раздаётся Caddy как статика: API проверяет участника пары и расшифровывает вложение. Лимиты по умолчанию: 25 MiB на файл, 250 MiB на пару. Downloads остаётся отдельным публичным каналом обновления APK.

Собственный TURN описан в [`../turn/README.md`](../turn/README.md), он запускается отдельным профилем и отдельным Docker-проектом. Без TURN звонки в staging выключены. Существующие лимиты API/PostgreSQL и конфигурация Caddy остаются прежними.

Порядок выпуска, сохранение Android signing key, бэкапы вложений и проверка двух телефонов — [`ROLLOUT_CHAT_CALLS.md`](ROLLOUT_CHAT_CALLS.md).
