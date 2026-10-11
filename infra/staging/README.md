# Тестовый сервер «Вечера» (api-test.вечер.online)

Живёт в `/opt/vecher-test/repo` (клон репозитория). Docker-проект `vecher-test`, API на `127.0.0.1:8092`,
наружу — через Caddy отдельным файлом `/etc/caddy/vecher-api-test.caddy`. krug не трогаем.

- `install.sh` — первая установка (секреты, сборка, запуск).
- `caddy-add.sh` — публикация через Caddy с бэкапом, проверкой krug и автооткатом.
- `update.sh` — обновление после `git push`.
- `backup.sh` — бэкап базы (cron раз в сутки).
- Секреты: `.env` (создаёт install.sh) и `secrets/firebase-key.json` — только на сервере, в git не попадают.
- Логи: `docker compose logs -f api`. Код входа без почты: `docker compose logs api | grep "код для"`.
