# Тестовый сервер «Вечера» (api-test.вечер.online)

Живёт в `/opt/vecher-test/repo` (клон репозитория). Docker-проект `vecher-test`, API на `127.0.0.1:8092`,
наружу — через Caddy отдельным файлом `/etc/caddy/vecher-api-test.caddy`. krug не трогаем.

- `install.sh` — первая установка (секреты, сборка, запуск).
- `caddy-add.sh` — публикация через Caddy с бэкапом, проверкой krug и автооткатом.
- `update.sh` — обновление выбранного checkout: бэкап, явная миграция, запуск и возврат старого API при ошибке.
- `deploy-realtime.sh --enable-realtime` — тот же порядок с настройкой Centrifugo и проверкой публичного WebSocket.
- `prepare-realtime.py` — создание/повторное использование отдельных секретов с приватным бэкапом конфигурации.
- `prepare-storage.sh` — постоянные downloads/uploads, владелец API uid/gid 10001.
- `backup.sh` — прежний бэкап БД без аргументов; `--consistent` сохраняет БД, uploads и downloads с короткой остановкой только нашего API.
- Секреты: `.env` (создаёт install.sh) и `secrets/firebase-key.json` — только на сервере, в git не попадают.
- Логи: `docker compose logs -f api`. Коды входа поступают только на почту; SMTP задаётся в `.env`.

## Чат, файлы и звонки

Приватные файлы хранятся отдельно в `/opt/vecher-test/uploads` и монтируются в `/srv/uploads`. Этот каталог не раздаётся Caddy как статика: API проверяет участника пары и расшифровывает вложение. Лимиты по умолчанию: 25 MiB на файл, 250 MiB на пару. Downloads остаётся отдельным публичным каналом обновления APK.

Собственный TURN описан в [`../turn/README.md`](../turn/README.md), он запускается отдельным профилем и отдельным Docker-проектом. Без TURN звонки в staging выключены. Существующие лимиты API/PostgreSQL и конфигурация Caddy остаются прежними.

Порядок выпуска, сохранение Android signing key, бэкапы вложений и проверка двух телефонов — [`ROLLOUT_CHAT_CALLS.md`](ROLLOUT_CHAT_CALLS.md).

С версии 0.3.0 схема обновляется через Alembic до запуска API. Новый порядок
установки и проверки синхронизации: [`ROLLOUT_REALTIME.md`](ROLLOUT_REALTIME.md).
