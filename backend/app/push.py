"""Push-уведомления.

Тексты нейтральные: без интимных подробностей и без текста карточек — телефон может
оказаться в чужих руках. Если ключа Firebase нет (разработка, тесты), уведомления
только пишутся в лог и в `outbox` — приложение работает как обычно.
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import DeviceToken, PairMember, PairPause, PushLog, User, UserSettings

log = logging.getLogger("vecher.push")

# Последние уведомления (для тестов и отладки): (user_id, title, body, data).
outbox: list[tuple[str, str, str, dict]] = []


class FcmSender:
    """FCM HTTP v1. Токен доступа берём по ключу сервисного аккаунта и кэшируем."""

    SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

    def __init__(self, key_path: Path):
        from google.oauth2 import service_account  # импорт только при наличии ключа

        info = json.loads(key_path.read_text(encoding="utf-8"))
        if info.get("type") != "service_account":
            raise ValueError(
                "это не ключ сервисного аккаунта"
                + (" (похоже на google-services.json приложения)" if "project_info" in info else "")
                + ". Нужен файл из Firebase → Настройки проекта → Сервисные аккаунты → «Создать новый закрытый ключ»")
        self.project_id = info["project_id"]
        self.creds = service_account.Credentials.from_service_account_info(info, scopes=[self.SCOPE])

    def _token(self) -> str:
        from google.auth.transport.requests import Request

        if not self.creds.valid:
            self.creds.refresh(Request())
        return self.creds.token

    async def send(self, token: str, title: str, body: str, data: dict) -> bool:
        """True — доставлено в FCM; False — токен больше не действует (удалить)."""
        status, text = await self.send_raw(token, title, body, data)
        if status == 200:
            return True
        if status in (400, 404) and ("UNREGISTERED" in text or "INVALID_ARGUMENT" in text):
            return False
        log.warning("FCM %s: %s", status, text[:300])
        return True

    async def send_raw(self, token: str, title: str, body: str, data: dict) -> tuple[int, str]:
        """Отправка как есть: (код ответа FCM, текст ответа) — для диагностики."""
        access = await asyncio.to_thread(self._token)
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        payload = {"message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in data.items()},
            "android": {"priority": "high"},
        }}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {access}"})
        return r.status_code, r.text


_sender: FcmSender | None = None
_sender_checked = False
BACKEND_DIR = Path(__file__).resolve().parent.parent


def find_key() -> Path:
    """Где лежит ключ Firebase.

    Относительный путь считаем от папки backend (а не от папки, где запущен сервер).
    Если файла с таким именем нет — берём единственный скачанный из Firebase
    `*firebase-adminsdk*.json` в папке backend (его не обязательно переименовывать).
    """
    path = Path(get_settings().firebase_credentials)
    if not path.is_absolute():
        path = BACKEND_DIR / path
    if path.exists() or path.name != "firebase-key.json":  # своё имя в настройках — без догадок (и в тестах)
        return path
    for candidate in (path.with_name(path.name + ".json"), *sorted(BACKEND_DIR.glob("*firebase-adminsdk*.json"))):
        if candidate.exists():
            return candidate
    return path


def get_sender() -> FcmSender | None:
    """Ключ ищем при первом уведомлении; если не нашли — ищем снова при следующем (без перезапуска сервера)."""
    global _sender, _sender_checked
    if _sender is None:
        _sender_checked = False
    if not _sender_checked:
        _sender_checked = True
        path = find_key()
        if path.exists():
            try:
                _sender = FcmSender(path)
                log.info("Push: Firebase подключён (проект %s)", _sender.project_id)
            except Exception as e:  # noqa: BLE001
                log.error("Push: не удалось прочитать ключ Firebase: %s", e)
        else:
            log.warning("Push: ключа Firebase нет (%s) — уведомления только в логе", path)
    return _sender


async def display_name(session: AsyncSession, user_id: str) -> str:
    user = await session.get(User, user_id)
    return (user.display_name if user and user.display_name else "Партнёр")


def local_when(dt: datetime) -> str:
    """«12.10 в 19:00» по часовому поясу проекта."""
    d = dt.astimezone(ZoneInfo(get_settings().timezone))
    return d.strftime("%d.%m в %H:%M")


PRIVATE_BODY = "Есть что-то новое для тебя"


async def is_paused(session: AsyncSession, user_id: str) -> bool:
    return (await session.execute(
        select(PairPause.id).join(PairMember, PairMember.pair_id == PairPause.pair_id)
        .where(PairMember.user_id == user_id)
    )).first() is not None


async def notify(session: AsyncSession, user_id: str, title: str, body: str,
                 data: dict | None = None, key: str | None = None, force: bool = False) -> bool:
    """Отправить уведомление человеку на все его устройства.

    key — защита от повторов: событие с тем же ключом второй раз не отправится.
    force — отправить и на паузе (сообщения о самой паузе, завершении пары).
    Учитывает настройки: уведомления выключены — не шлём; «без текста» — нейтральный текст.
    Возвращает False, если уведомление не отправлялось.
    """
    if key is not None:
        if (await session.execute(select(PushLog.id).where(PushLog.key == key))).scalar_one_or_none():
            return False
        session.add(PushLog(key=key))
        await session.commit()

    prefs = await session.get(UserSettings, user_id)
    if prefs is not None and not prefs.push_enabled:
        return False
    if not force and await is_paused(session, user_id):
        return False
    if prefs is not None and prefs.push_private:
        title, body = "Вечер", PRIVATE_BODY

    data = data or {}
    outbox.append((user_id, title, body, data))
    del outbox[:-200]
    log.info("PUSH → %s: %s — %s", user_id, title, body)

    sender = get_sender()
    if sender is None:
        return True
    tokens = list((await session.execute(
        select(DeviceToken.token).where(DeviceToken.user_id == user_id))).scalars().all())
    if tokens:
        # Отправка в Google — в фоне: человек не ждёт ответа FCM (он бывает долгим),
        # а сбой уведомления никогда не превращается в ошибку его действия.
        task = asyncio.create_task(_deliver(sender, tokens, title, body, data))
        _push_tasks.add(task)
        task.add_done_callback(_push_tasks.discard)
    return True


_push_tasks: set[asyncio.Task] = set()


async def _deliver(sender: "FcmSender", tokens: list[str], title: str, body: str, data: dict) -> None:
    dead = []
    for t in tokens:
        try:
            if not await sender.send(t, title, body, data):
                dead.append(t)
        except Exception as e:  # noqa: BLE001 — сбой push не должен ломать действие пользователя
            log.warning("Push не отправлен: %s", e)
    if dead:
        try:
            from .db import SessionLocal

            async with SessionLocal() as s:
                await s.execute(delete(DeviceToken).where(DeviceToken.token.in_(dead)))
                await s.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось убрать старые токены: %s", e)
