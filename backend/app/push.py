"""Push-уведомления.

Тексты нейтральные: без интимных подробностей и без текста карточек — телефон может
оказаться в чужих руках. Уведомление попадает в зашифрованную durable outbox в
транзакции действия пользователя. Фоновый worker отправляет его после commit.
"""
import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import PairMember, PairPause, PushLog, User, UserSettings

log = logging.getLogger("vecher.push")

# Только dev: последние committed intents (не доказательство доставки в FCM).
# В staging/prod личные тексты не сохраняются в памяти и не пишутся в лог.
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
        self._refresh_lock = threading.Lock()

    def _token(self) -> str:
        from google.auth.transport.requests import Request
        from google.auth.exceptions import TransportError

        if self.creds.valid:
            return self.creds.token
        if not self._refresh_lock.acquire(timeout=3):
            raise TransportError("OAuth refresh busy; retry through delivery queue")
        try:
            if self.creds.valid:
                return self.creds.token
            transport = Request()
            attempted = False

            def bounded_request(*args, **kwargs):
                nonlocal attempted
                if attempted:
                    # google-auth normally retries 5xx responses internally.
                    # Durable retries belong to our queue, not a detached thread.
                    raise TransportError("OAuth refresh retry delegated to delivery queue")
                attempted = True
                kwargs["timeout"] = (2.0, 4.0)  # connect/read, instead of the SDK's 120 seconds
                return transport(*args, **kwargs)

            try:
                self.creds.refresh(bounded_request)
            finally:
                transport.session.close()
            return self.creds.token
        finally:
            self._refresh_lock.release()

    async def send(self, token: str, title: str, body: str, data: dict,
                   *, expires_at: datetime | None = None) -> bool:
        """True means accepted by FCM; False means a confirmed unregistered token.

        A 400 INVALID_ARGUMENT can describe a payload, so it must not delete a
        working device. All other errors remain retryable and never count as ack.
        """
        response = await self._request(token, title, body, data, expires_at=expires_at)
        if response.status_code == 200:
            return True
        try:
            error = response.json().get("error", {})
            codes = {item.get("errorCode") for item in error.get("details", [])
                     if item.get("@type") == "type.googleapis.com/google.firebase.fcm.v1.FcmError"}
        except (ValueError, AttributeError, TypeError):
            codes = set()
        if response.status_code in (400, 404) and "UNREGISTERED" in codes:
            return False
        retry_after = 60.0 if response.status_code == 429 else 0.0
        hint = response.headers.get("retry-after", "")
        try:
            retry_after = max(retry_after, float(hint))
        except ValueError:
            try:
                retry_after = max(retry_after, (parsedate_to_datetime(hint) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
        raise FcmDeliveryError(response.status_code, retry_after)

    async def _request(self, token: str, title: str, body: str, data: dict,
                       *, expires_at: datetime | None = None) -> httpx.Response:
        access = await asyncio.to_thread(self._token)
        android = {"priority": "high"}
        if expires_at is not None:
            deadline = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise FcmDeliveryExpired()
            # Compute after OAuth refresh: an incoming-call notification must
            # not use FCM's default weeks-long retention after the call expires.
            android["ttl"] = f"{min(int(remaining), 2419200)}s"
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        payload = {"message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in data.items()},
            "android": android,
        }}
        async with httpx.AsyncClient(timeout=10) as client:
            return await client.post(url, json=payload, headers={"Authorization": f"Bearer {access}"})

    async def send_raw(self, token: str, title: str, body: str, data: dict) -> tuple[int, str]:
        """Low-level diagnostic API; callers must not log response bodies or tokens."""
        response = await self._request(token, title, body, data)
        return response.status_code, response.text


class FcmDeliveryError(Exception):
    def __init__(self, status: int, retry_after: float = 0):
        super().__init__(f"FCM status {status}")
        self.retry_after = retry_after


class FcmDeliveryExpired(Exception):
    """Deadline passed during credential refresh; no provider request was sent."""


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
        try:
            path = find_key()
            found = path.exists()
        except OSError as e:  # например, нет прав на папку с ключом
            log.error("Push: нет доступа к ключу Firebase (%s)", type(e).__name__)
            return None
        if found:
            try:
                _sender = FcmSender(path)
                log.info("Push: Firebase подключён (проект %s)", _sender.project_id)
            except Exception as e:  # noqa: BLE001
                log.error("Push: не удалось прочитать ключ Firebase (%s)", type(e).__name__)
        else:
            log.warning("Push: ключа Firebase нет — отправка остаётся в очереди до retry/TTL")
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


@event.listens_for(Session, "after_commit")
def _capture_committed_dev_intents(session):
    pending = session.info.pop("vecher_dev_push", [])
    if get_settings().is_dev:
        outbox.extend(pending)
        del outbox[:-200]


@event.listens_for(Session, "after_rollback")
def _discard_rolled_back_dev_intents(session):
    session.info.pop("vecher_dev_push", None)


async def notify(session: AsyncSession, user_id: str, title: str, body: str,
                 data: dict | None = None, key: str | None = None, force: bool = False,
                 pair_id: str | None = None, expires_at: datetime | None = None,
                 available_at: datetime | None = None) -> bool:
    """Stage a push in the caller's transaction. The caller MUST commit it.

    key is idempotent; force only bypasses pair pause, never user preferences.
    A legacy PushLog key is still respected after migration. Fresh keys are kept
    by terminal outbox rows, without retaining their private payload.
    """
    from .outbox import enqueue_delivery, suppress_push

    if key is not None and (await session.execute(
        select(PushLog.id).where(PushLog.key == key)
    )).scalar_one_or_none():
        return False
    prefs = await session.get(UserSettings, user_id)
    if prefs is not None and not prefs.push_enabled:
        await suppress_push(session, user_id=user_id, key=key, pair_id=pair_id, reason="push_disabled")
        return False
    if not force and await is_paused(session, user_id):
        await suppress_push(session, user_id=user_id, key=key, pair_id=pair_id, reason="pair_paused")
        return False
    if prefs is not None and prefs.push_private:
        title, body = "Вечер", PRIVATE_BODY
    data = dict(data or {})
    added = await enqueue_delivery(
        session, kind="push", user_id=user_id,
        payload={"title": title, "body": body, "data": data}, key=key,
        force=force, pair_id=pair_id, expires_at=expires_at, available_at=available_at,
    )
    if added and get_settings().is_dev:
        session.info.setdefault("vecher_dev_push", []).append((user_id, title, body, data))
    return added
