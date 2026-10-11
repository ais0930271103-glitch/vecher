import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import LoginCode, User
from ..security import create_token, current_user, hash_otp, new_otp

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("vecher.auth")


class CodeRequest(BaseModel):
    email: EmailStr


class CodeVerify(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TokenOut(BaseModel):
    access_token: str
    user_id: str
    is_new: bool
    has_name: bool  # false — приложение сразу спросит имя


class NameIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)


class MeOut(BaseModel):
    id: str
    email: str
    display_name: str | None


@router.post("/request-code")
async def request_code(body: CodeRequest, session: AsyncSession = Depends(get_session)) -> dict:
    s = get_settings()
    email = body.email.lower()
    now = datetime.now(timezone.utc)
    recent = (await session.execute(
        select(LoginCode.created_at).where(LoginCode.email == email, LoginCode.created_at >= now - timedelta(hours=1))
        .order_by(LoginCode.created_at.desc())
    )).scalars().all()
    if recent:
        last = recent[0] if recent[0].tzinfo else recent[0].replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < s.otp_resend_seconds:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Код уже отправлен — подожди минуту")
    if len(recent) >= s.otp_codes_per_hour:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Слишком много запросов кода. Попробуй через час")
    code = new_otp()
    session.add(LoginCode(
        email=email,
        code_hash=hash_otp(email, code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=s.otp_ttl_minutes),
    ))
    await session.commit()
    # TODO(этап 1): отправка письма через SMTP рег.ру. Пока в dev код виден в логе и ответе.
    if s.is_dev:
        log.warning("DEV login code for %s: %s", email, code)
        return {"sent": True, "dev_code": code}
    return {"sent": True}


@router.post("/verify", response_model=TokenOut)
async def verify(body: CodeVerify, session: AsyncSession = Depends(get_session)) -> TokenOut:
    s = get_settings()
    email = body.email.lower()
    now = datetime.now(timezone.utc)
    # Ошибки считаем по всем кодам за час: новый код не обнуляет счётчик перебора.
    failed = (await session.execute(
        select(func.coalesce(func.sum(LoginCode.attempts), 0))
        .where(LoginCode.email == email, LoginCode.created_at >= now - timedelta(hours=1))
    )).scalar_one()
    if failed >= s.otp_failed_per_hour:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Слишком много неверных попыток. Попробуй через час")
    row = (await session.execute(
        select(LoginCode)
        .where(LoginCode.email == email, LoginCode.used_at.is_(None))
        .order_by(LoginCode.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    expires = row.expires_at if row else None
    if expires is not None and expires.tzinfo is None:  # SQLite drops tzinfo
        expires = expires.replace(tzinfo=timezone.utc)
    if row is None or expires < now or row.attempts >= s.otp_max_attempts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Код устарел, запроси новый")
    if row.code_hash != hash_otp(email, body.code):
        row.attempts += 1
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный код")

    row.used_at = now
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    is_new = user is None
    if is_new:
        user = User(email=email)
        session.add(user)
    await session.commit()
    return TokenOut(access_token=create_token(user.id), user_id=user.id, is_new=is_new,
                    has_name=bool(user.display_name))


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(current_user)) -> MeOut:
    return MeOut(id=user.id, email=user.email, display_name=user.display_name)


@router.put("/me", response_model=MeOut)
async def set_my_name(body: NameIn, user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)) -> MeOut:
    """Имя при регистрации — пара для этого не нужна."""
    name = body.display_name.strip()
    if not name:
        raise HTTPException(422, "Введи имя")
    user.display_name = name
    await session.commit()
    return MeOut(id=user.id, email=user.email, display_name=user.display_name)
