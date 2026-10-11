# Вечер

Приватное приложение для двоих. Монорепозиторий:

| Папка | Что внутри |
|---|---|
| `app/` | Мобильное приложение (Flutter, iOS + Android) |
| `backend/` | API (FastAPI + PostgreSQL) |
| `infra/` | Docker Compose: PostgreSQL, Redis, Centrifugo, Directus, API |

Сейчас готово (этап 0 → начало этапа 1): вход по коду на e-mail, приглашение партнёра по коду, экран «Сегодня» с состоянием пары.

---

## 1. Установить инструменты (Windows, один раз)

1. **Git** — https://git-scm.com/download/win (всё по умолчанию).
2. **Docker Desktop** — https://www.docker.com/products/docker-desktop/ (после установки перезагрузить ПК и запустить Docker Desktop).
3. **Flutter SDK** — https://docs.flutter.dev/get-started/install/windows/mobile
   - распакуйте в `C:\dev\flutter` (не в Program Files);
   - добавьте `C:\dev\flutter\bin` в переменную PATH;
   - откройте новое окно PowerShell и выполните `flutter doctor`.
4. **Android Studio** — https://developer.android.com/studio
   - при первом запуске поставьте Android SDK;
   - More Actions → Virtual Device Manager → создайте эмулятор (например, Pixel 7);
   - выполните `flutter doctor --android-licenses` и согласитесь со всем.
5. **Python 3.12** (для тестов сервера без Docker) — https://www.python.org/downloads/ (галочка «Add to PATH»).

Проверка: `flutter doctor` должен показать зелёные галочки у Flutter, Android toolchain и Android Studio.

## 2. Запустить сервер

### Вариант А — быстро, без Docker (Python + локальная база SQLite)

```powershell
cd C:\dev\vecher\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8090
```

Окно не закрывайте — сервер работает, пока оно открыто. Проверка: http://localhost:8090/docs

### Вариант Б — полный набор в Docker (PostgreSQL, Redis, Centrifugo, Directus)

```powershell
cd vecher\infra
copy .env.example .env
notepad .env          # замените все «замените-меня» на случайные строки
docker compose --env-file .env up -d --build
```

Проверка в браузере:
- http://localhost:8090/health — `{"ok": true, ...}`
- http://localhost:8090/docs — все методы API, можно пробовать прямо там
- http://localhost:8055 — админка контента Directus (логин и пароль из `.env`)

Остановить: `docker compose down` (данные сохраняются).

## 3. Тесты сервера

```powershell
cd vecher\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
```

## 4. Запустить приложение

Первый раз — сгенерировать папки платформ (android/ios), код в `lib/` не тронется:

```powershell
cd vecher\app
flutter create --org online.vecher --project-name vecher_app --platforms android,ios,web .
flutter pub get
```

Самый быстрый способ посмотреть приложение — в браузере (Android и эмулятор не нужны):

```powershell
flutter run -d chrome
```

Разрешить HTTP к локальному серверу в отладке: откройте `app\android\app\src\debug\AndroidManifest.xml`
и в тег `<manifest ...>` добавьте внутрь:

```xml
<application android:usesCleartextTraffic="true" />
```

Запуск (эмулятор должен быть открыт):

```powershell
flutter run
```

Вход: введите любой e-mail → в режиме разработки код появится в консоли `flutter run`
(строка `DEV-код входа: 123456`) и в логах сервера (`docker compose logs backend`).

Чтобы проверить пару, запустите приложение на двух эмуляторах (или эмулятор + телефон) с разными e-mail:
один нажимает «Получить код», второй вводит его в «У меня есть код».

---

## Правила

- Секреты только в `.env`, в git не попадают.
- krug на общем сервере не трогаем: у «Вечера» свой Docker-проект `vecher` и свои порты (8090, 8055, 8100, 5433), все на 127.0.0.1, у каждого сервиса лимит памяти.
