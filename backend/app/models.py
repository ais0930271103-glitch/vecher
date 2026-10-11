import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginCode(Base):
    """One-time e-mail code. Only a keyed hash of the code is stored."""

    __tablename__ = "login_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Pair(Base):
    __tablename__ = "pairs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | active | paused
    invite_code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PairMember(Base):
    __tablename__ = "pair_members"
    __table_args__ = (UniqueConstraint("user_id", name="uq_one_pair_per_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))  # initiator | invited
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- Этап 2: вопрос дня, радар, огонёк ----------

class Question(Base):
    """Вопрос дня. Позже управляется через Directus; стартовый набор — app/content/questions.py."""

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(16))  # warm | fun | flirt | deep
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DailyAnswer(Base):
    __tablename__ = "daily_answers"
    __table_args__ = (UniqueConstraint("pair_id", "user_id", "day", name="uq_one_answer_per_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Activity(Base):
    """Отметка «человек что-то сделал в этот день» — из неё считается огонёк пары."""

    __tablename__ = "activity"
    __table_args__ = (UniqueConstraint("pair_id", "user_id", "day", name="uq_activity_per_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)


class RadarSignal(Base):
    __tablename__ = "radar_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    from_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24))
    day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
