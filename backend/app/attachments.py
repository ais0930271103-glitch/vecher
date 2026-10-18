"""Приватные вложения чата: проверка пары, квота и AES-GCM на диске.

Имена на диске случайны, оригинальное имя — зашифрованная колонка БД. Удаление
файла выполняется только после успешного COMMIT. Старые несвязанные файлы и
брошенные загрузки убирает планировщик; исходники не раздаются как статика.
"""
import asyncio
import logging
import mimetypes
import os
import re
import stat
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from weakref import WeakKeyDictionary

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import StreamingResponse

from .config import get_settings
from .crypto import _keys
from .db import SessionLocal, get_session
from .deps import PairContext, active_pair
from .models import ChatAttachment, ChatMessage, utcnow
from .pair_lock import pair_lock

router = APIRouter(prefix="/chat/attachments", tags=["chat"])
log = logging.getLogger("vecher.attachments")
_MAGIC = b"VCHFILE1"
_KEY = re.compile(r"^[0-9a-f]{32}$")
_MAX_MULTIPART_OVERHEAD = 64 * 1024
_TOO_LARGE = "Файл превышает допустимый размер"
_file_slots: WeakKeyDictionary = WeakKeyDictionary()
_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf",
          "video/mp4", "video/webm", "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav",
          "text/plain", "application/zip", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


class AttachmentOut(BaseModel):
    id: str
    name: str
    mime: str
    size: int


def _slots() -> asyncio.Semaphore:
    # Буферы ограничены не только на файл, но и на процесс (API контейнер 320 MiB).
    loop = asyncio.get_running_loop()
    return _file_slots.setdefault(loop, asyncio.Semaphore(2))


class _DownloadResponse(StreamingResponse):
    """Слот занят до конца передачи, включая медленного клиента / отключение."""

    def __init__(self, data: bytes, slots: asyncio.Semaphore, **kwargs):
        async def chunks():
            for offset in range(0, len(data), 64 * 1024):
                yield data[offset:offset + 64 * 1024]

        super().__init__(chunks(), **kwargs)
        self._slots = slots
        self.headers["content-length"] = str(len(data))

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._slots.release()


def attachment_out(row: ChatAttachment) -> AttachmentOut:
    return AttachmentOut(id=row.id, name=row.name, mime=row.mime, size=row.size)


def safe_name(raw: str | None) -> str:
    name = unicodedata.normalize("NFC", raw or "file").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    name = re.sub(r'[<>:"|?*]', "_", name).strip(" .") or "file"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                                     *[f"LPT{i}" for i in range(1, 10)]}:
        name = "_" + name
    suffix = Path(name).suffix[:20]
    stem = name[:-len(suffix)] if suffix else name
    while len((stem + suffix).encode("utf-8")) > 220:
        stem = stem[:-1]
    return (stem + suffix) or "file"


def _dir() -> Path:
    path = Path(get_settings().uploads_dir)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    path = path.absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.resolve() != path:
        raise RuntimeError("Папка вложений не должна содержать символических ссылок")
    return path


def _name(key: str) -> str:
    if not _KEY.fullmatch(key):
        raise ValueError("Некорректный идентификатор файла")
    return key + ".bin"


def _open(key: str, flags: int) -> int:
    directory, name = _dir(), _name(key)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if os.open in os.supports_dir_fd:
        dfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            return os.open(name, flags, 0o600, dir_fd=dfd)
        finally:
            os.close(dfd)
    path = directory / name  # Windows dev; каталог принадлежит приложению.
    if path.is_symlink():
        raise OSError("Символическая ссылка вместо вложения")
    return os.open(path, flags, 0o600)


def _write(key: str, data: bytes) -> None:
    kid, secret = _keys()[0]
    encoded_id, nonce = kid.encode(), os.urandom(12)
    if len(encoded_id) > 255:
        raise RuntimeError("Слишком длинный идентификатор ключа DATA_KEYS")
    header = _MAGIC + bytes([len(encoded_id)]) + encoded_id + nonce
    ciphertext = AESGCM(secret).encrypt(nonce, data, header + key.encode())
    fd = _open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(header)
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _unlink(key)
        raise


def _read(key: str, expected_size: int) -> bytes:
    fd = _open(key, os.O_RDONLY)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > expected_size + 300:
            raise ValueError("Некорректный файл вложения")
        blob = stream.read(expected_size + 301)
    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + 1:
        raise ValueError("Некорректный формат вложения")
    n = blob[len(_MAGIC)]
    end = len(_MAGIC) + 1 + n + 12
    header, ciphertext = blob[:end], blob[end:]
    kid = header[len(_MAGIC) + 1:len(_MAGIC) + 1 + n].decode()
    secret = dict(_keys()).get(kid)
    if secret is None:
        raise RuntimeError("Нет ключа DATA_KEYS для вложения")
    data = AESGCM(secret).decrypt(header[-12:], ciphertext, header + key.encode())
    if len(data) != expected_size:
        raise ValueError("Размер вложения не совпал")
    return data


def _unlink(key: str) -> None:
    try:
        (_dir() / _name(key)).unlink(missing_ok=True)  # unlink не следует по ссылке.
    except (OSError, RuntimeError, ValueError):
        log.warning("Не удалось удалить вложение; очистка повторит попытку")


def queue_file_delete(session: AsyncSession, key: str) -> None:
    session.info.setdefault("attachment_delete_after_commit", set()).add(key)


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    for key in session.info.pop("attachment_delete_after_commit", set()):
        _unlink(key)


@event.listens_for(Session, "after_rollback")
def _rolled_back(session: Session) -> None:
    session.info.pop("attachment_delete_after_commit", None)


async def delete_attachments(session: AsyncSession, *conditions) -> None:
    rows = (await session.execute(select(ChatAttachment).where(*conditions))).scalars().all()
    for row in rows:
        queue_file_delete(session, row.storage_key)
        await session.delete(row)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _expired(row: ChatAttachment) -> bool:
    return row.message_id is None and _aware(row.created_at) < utcnow() - timedelta(
        hours=get_settings().attachment_pending_ttl_hours)


async def _parse_upload(request: Request) -> tuple[str, bytes]:
    limit = get_settings().attachment_max_bytes
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "Некорректная длина запроса")
    if length > limit + _MAX_MULTIPART_OVERHEAD:
        raise HTTPException(413, _TOO_LARGE)
    received = 0

    async def bounded_receive():
        nonlocal received
        message = await request.receive()
        received += len(message.get("body", b""))
        if received > limit + _MAX_MULTIPART_OVERHEAD:
            # MultiPartException закрывает spool-файлы уже прочитанных частей.
            raise MultiPartException(_TOO_LARGE)
        return message

    bounded = Request(request.scope, receive=bounded_receive)
    try:
        async with bounded.form(max_files=1, max_fields=0) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile) or len(form) != 1:
                raise HTTPException(422, "Нужен один файл в поле file")
            if file.size is not None and file.size > limit:
                raise HTTPException(413, _TOO_LARGE)
            data = bytearray()
            while chunk := await file.read(1024 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise HTTPException(413, _TOO_LARGE)
            return safe_name(file.filename), bytes(data)
    except StarletteHTTPException as exc:
        if exc.detail == _TOO_LARGE:
            raise HTTPException(413, _TOO_LARGE) from None
        raise


@router.post("", response_model=AttachmentOut, status_code=201)
async def upload(request: Request, ctx: PairContext = Depends(active_pair),
                 session: AsyncSession = Depends(get_session)):
    await session.commit()  # ожидание слота не удерживает соединение БД
    async with _slots():
        return await _upload(request, ctx, session)


async def _upload(request: Request, ctx: PairContext, session: AsyncSession):
    name, data = await _parse_upload(request)  # авторизация проверена до чтения тела.
    key = os.urandom(16).hex()
    written = False
    try:
        async with pair_lock(session, ctx.pair.id):
            cutoff = utcnow() - timedelta(hours=get_settings().attachment_pending_ttl_hours)
            await delete_attachments(session, ChatAttachment.pair_id == ctx.pair.id,
                                     ChatAttachment.message_id.is_(None), ChatAttachment.created_at < cutoff)
            await session.flush()
            used, count = (await session.execute(select(func.coalesce(func.sum(ChatAttachment.size), 0),
                                                        func.count(ChatAttachment.id))
                .where(ChatAttachment.pair_id == ctx.pair.id))).one()
            if used + len(data) > get_settings().attachment_pair_quota_bytes or count >= 1000:
                raise HTTPException(413, "Место для файлов вашей пары заполнено — удалите ненужные сообщения с файлами")
            guessed, _ = mimetypes.guess_type(name)
            row = ChatAttachment(pair_id=ctx.pair.id, from_id=ctx.user.id, storage_key=key,
                                 name=name, mime=guessed if guessed in _MIMES else "application/octet-stream", size=len(data))
            session.add(row)
            await session.flush()
            # Размер ограничен до шифрования; шифрование и fsync не блокируют event loop.
            writing = asyncio.create_task(asyncio.to_thread(_write, key, data))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                # Поток записи не отменяется вместе с HTTP-задачей. Сначала дождёмся
                # закрытия файла, затем откатим metadata и удалим неподтверждённый blob.
                await writing
                written = True
                raise
            written = True
            await session.commit()
            return attachment_out(row)
    except BaseException:
        await session.rollback()
        if written:
            # Ошибка/отмена во время COMMIT может произойти уже ПОСЛЕ сохранения БД.
            # Нельзя удалить файл принятой загрузки: проверяем отдельной транзакцией.
            try:
                async with SessionLocal() as check:
                    committed = (await check.execute(select(ChatAttachment.id).where(ChatAttachment.storage_key == key))).first()
                if not committed:
                    _unlink(key)
            except Exception:
                log.warning("Не удалось проверить COMMIT загрузки; отложенная очистка проверит файл")
        raise


@router.get("/{attachment_id}")
async def download(attachment_id: str, ctx: PairContext = Depends(active_pair),
                   session: AsyncSession = Depends(get_session)):
    await session.commit()
    slots = _slots()
    await slots.acquire()
    try:
        # Перепроверяем пару после ожидания: её могли завершить, пока слоты заняты.
        async with pair_lock(session, ctx.pair.id):
            response = await _download(attachment_id, ctx, session, slots)
            await session.commit()
            return response
    except BaseException:
        slots.release()
        raise


async def _download(attachment_id: str, ctx: PairContext, session: AsyncSession, slots: asyncio.Semaphore):
    row = await session.get(ChatAttachment, attachment_id)
    if row is None or row.pair_id != ctx.pair.id or _expired(row):
        raise HTTPException(404, "Файл не найден")
    if row.message_id is None and row.from_id != ctx.user.id:
        raise HTTPException(404, "Файл не найден")
    if row.message_id:
        message = await session.get(ChatMessage, row.message_id)
        if message is None or message.deleted or message.pair_id != ctx.pair.id:
            raise HTTPException(404, "Файл не найден")
    try:
        reading = asyncio.create_task(asyncio.to_thread(_read, row.storage_key, row.size))
        try:
            data = await asyncio.shield(reading)
        except asyncio.CancelledError:
            # Не освобождаем лимит памяти, пока поток ещё расшифровывает большой файл.
            try:
                await reading
            except Exception:
                pass
            raise
    except (OSError, ValueError, InvalidTag):
        raise HTTPException(404, "Файл недоступен") from None
    # Аутентификация AES-GCM полностью проверена до выдачи первого байта.
    return _DownloadResponse(data, slots, media_type=row.mime, headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(safe_name(row.name), safe=""),
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


@router.delete("/{attachment_id}", status_code=204)
async def discard(attachment_id: str, ctx: PairContext = Depends(active_pair),
                  session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        row = await session.get(ChatAttachment, attachment_id)
        if row is None or row.pair_id != ctx.pair.id or row.from_id != ctx.user.id:
            raise HTTPException(404, "Файл не найден")
        if row.message_id is not None:
            raise HTTPException(409, "Отправленный файл удаляется вместе с сообщением")
        await delete_attachments(session, ChatAttachment.id == row.id)
        await session.commit()
    return Response(status_code=204)


async def cleanup_attachments(session: AsyncSession, now: datetime | None = None) -> None:
    now = now or utcnow()
    cutoff = now - timedelta(hours=get_settings().attachment_pending_ttl_hours)
    pair_ids = (await session.execute(select(ChatAttachment.pair_id).where(
        ChatAttachment.message_id.is_(None), ChatAttachment.created_at < cutoff).distinct().limit(100))).scalars().all()
    for pair_id in pair_ids:
        try:
            async with pair_lock(session, pair_id, active=False):
                await delete_attachments(session, ChatAttachment.pair_id == pair_id,
                                         ChatAttachment.message_id.is_(None), ChatAttachment.created_at < cutoff)
                await session.commit()
        except HTTPException:
            await session.rollback()
    # Страховка на случай остановки процесса между записью файла и COMMIT / удаления после COMMIT.
    directory = _dir()
    for path in directory.iterdir():
        if not path.name.endswith(".bin") or not _KEY.fullmatch(path.stem):
            continue
        try:
            if path.lstat().st_mtime >= cutoff.timestamp():
                continue
            exists = (await session.execute(select(ChatAttachment.id).where(ChatAttachment.storage_key == path.stem))).first()
            if not exists:
                _unlink(path.stem)
        except FileNotFoundError:
            continue
