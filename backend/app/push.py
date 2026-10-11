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
from .models import DeviceToken, PushLog, User

log = logging.getLogger("vecher.push")

# Последние уведомления (для тестов и отладки): (user_id, title, body, data).
outbox: list[tuple[str, str, str, dict]] = []


class FcmSender:
    """FCM HTTP v1. Токен доступа берём по ключу сервисного аккаунта и кэшируем."""

    SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

    def __init__(self, key_path: Path):
        from google.oauth2 import service_account  # импорт только при наличии ключа

        info = json.loads(key_path.read_text(encoding="utf-8"))
        self.project_id = info["project_id"]
        self.creds = service_account.Credentials.from_service_account_info(info, scopes=[self.SCOPE])

    def _token(self) -> str:
        from google.auth.transport.requests import Request

        if not self.creds.valid:
            self.creds.refresh(Request())
        return self.creds.token

    async def send(self, token: str, title: str, body: str, data: dict) -> bool:
        """True — доставлено в FCM; False — токен больше не действует (удалить)."""
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
        if r.status_code == 200:
            return True
        if r.status_code in (400, 404) and ("UNREGISTERED" in r.text or "INVALID_ARGUMENT" in r.text):
            return False
        log.warning("FCM %s: %s", r.status_code, r.text[:300])
        return True


_sender: FcmSender | None = None
_sender_checked = False


def get_sender() -> FcmSender | None:
    global _sender, _sender_checked
    if not _sender_checked:
        _sender_checked = True
        path = Path(get_settings().firebase_credentials)
        if path.exists():
            try:
                _sender = FcmSender(path)
                log.info("Push: Firebase подключён (проект %s)", _sender.project_id)
            except Exception as e:  # noqa: BLE001
                log.error("Push: не удалось прочитать ключ Firebase: %s", e)
        else:
            log.info("Push: ключа Firebase нет (%s) — уведомления только в логе", path)
    return _sender


async def display_name(session: AsyncSession, user_id: str) -> str:
    user = await session.get(User, user_id)
    return (user.display_name if user and user.display_name else "Партнёр")


def local_when(dt: datetime) -> str:
    """«12.10 в 19:00» по часовому поясу проекта."""
    d = dt.astimezone(ZoneInfo(get_settings().timezone))
    return d.strftime("%d.%m в %H:%M")


async def notify(session: AsyncSession, user_id: str, title: str, body: str,
                 data: dict | None = None, key: str | None = None) -> bool:
    """Отправить уведомление человеку на все его устройства.

    key — защита от повторов: событие с тем же ключом второй раз не отправится.
    Возвращает False, если уведомление уже отправлялось.
    """
    if key is not None:
        if (await session.execute(select(PushLog.id).where(PushLog.key == key))).scalar_one_or_none():
            return False
        session.add(PushLog(key=key))
        await session.commit()

    data = data or {}
    outbox.append((user_id, title, body, data))
    del outbox[:-200]
    log.info("PUSH → %s: %s — %s", user_id, title, body)

    sender = get_sender()
    if sender is None:
        return True
    tokens = (await session.execute(select(DeviceToken.token).where(DeviceToken.user_id == user_id))).scalars().all()
    dead = []
    for t in tokens:
        try:
            if not await sender.send(t, title, body, data):
                dead.append(t)
        except Exception as e:  # noqa: BLE001 — сбой push не должен ломать действие пользователя
            log.warning("Push не отправлен: %s", e)
    if dead:
        await session.execute(delete(DeviceToken).where(DeviceToken.token.in_(dead)))
        await session.commit()
    return True
