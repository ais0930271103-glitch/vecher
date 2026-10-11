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


# ---------- Этап 3: свайпер желаний ----------

class WishCard(Base):
    """Карточка желания. Стартовый набор — app/content/wishes.py, позже — Directus."""

    __tablename__ = "wish_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck: Mapped[str] = mapped_column(String(24), index=True)
    text: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    level: Mapped[str] = mapped_column(String(8), default="warm", server_default="warm")  # warm | spicy | hot


class WishSwipe(Base):
    """Выбор одного человека по карточке. Партнёру никогда не показывается напрямую —
    только через совпадения (оба «да» / «да» и «может быть»)."""

    __tablename__ = "wish_swipes"
    __table_args__ = (UniqueConstraint("pair_id", "user_id", "card_id", name="uq_one_swipe_per_card"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    card_id: Mapped[int] = mapped_column(ForeignKey("wish_cards.id", ondelete="CASCADE"))
    choice: Mapped[str] = mapped_column(String(8))  # yes | maybe | no
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WishView(Base):
    """Когда человек последний раз открывал список совпадений — для пометки «новое»."""

    __tablename__ = "wish_views"
    __table_args__ = (UniqueConstraint("pair_id", "user_id", name="uq_wish_view"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- Планы к совпадениям и подписка ----------

class PairSubscription(Base):
    """Подписка «Вечер+» — одна на пару, открывается обоим. Оплата подключается на этапе 5."""

    __tablename__ = "pair_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), unique=True)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="dev")  # dev | store
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanUnlock(Base):
    """План к совпадению открыт для пары (бесплатный или по подписке)."""

    __tablename__ = "plan_unlocks"
    __table_args__ = (UniqueConstraint("pair_id", "card_id", name="uq_plan_unlock"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("wish_cards.id", ondelete="CASCADE"))
    free: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanVote(Base):
    """Какой вариант плана выбрал каждый. Совпали голоса — вариант выбран."""

    __tablename__ = "plan_votes"
    __table_args__ = (UniqueConstraint("pair_id", "user_id", "card_id", name="uq_plan_vote"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    card_id: Mapped[int] = mapped_column(ForeignKey("wish_cards.id", ondelete="CASCADE"))
    option: Mapped[int] = mapped_column(Integer)


# ---------- Этап 4: «Мой человек» — памятка, даты, напоминания ----------

class ProfileFact(Base):
    """Факт «о себе» (размер, любимые цветы…). Видит партнёр — это его памятка."""

    __tablename__ = "profile_facts"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_profile_fact"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PartnerNote(Base):
    """Личная заметка о партнёре. Видит только автор."""

    __tablename__ = "partner_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PairDate(Base):
    """Важная дата пары: знакомство, дни рождения, годовщины."""

    __tablename__ = "pair_dates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(80))
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))  # met | birthday | anniversary | other
    yearly: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Reminder(Base):
    """Напоминание: себе (me), партнёру (partner) или обоим (both)."""

    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    target: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(String(200))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- Вкладка «Я»: моё пространство ----------

class Letter(Base):
    """Письмо партнёру с отложенной доставкой (по умолчанию — к 8:00 утра)."""

    __tablename__ = "letters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    from_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(String(2000))
    deliver_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuietWish(Base):
    """Тихое желание — видно только автору."""

    __tablename__ = "quiet_wishes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Surprise(Base):
    """Сюрприз для партнёра: до назначенного времени он видит только, что сюрприз будет и когда."""

    __tablename__ = "surprises"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    from_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- Push-уведомления ----------

class DeviceToken(Base):
    """Токен устройства для push (FCM; позже RuStore и APNs)."""

    __tablename__ = "device_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(512), unique=True)
    platform: Mapped[str] = mapped_column(String(16), default="android")  # android | ios | rustore
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushLog(Base):
    """Что уже отправлено — чтобы одно событие не пришло дважды (ключ вида letter:<id>)."""

    __tablename__ = "push_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- Приватность и 152-ФЗ ----------

class Consent(Base):
    """Согласие на обработку ПДн: personal — обычные данные, special — сведения об интимной жизни и здоровье
    (ст. 10 152-ФЗ, отдельное согласие). Храним версию текста, на которую человек согласился."""

    __tablename__ = "consents"
    __table_args__ = (UniqueConstraint("user_id", "kind", "version", name="uq_consent"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # personal | special
    version: Mapped[str] = mapped_column(String(16))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSettings(Base):
    """Настройки человека, которые нужны серверу (PIN и запрет скриншотов живут только на телефоне)."""

    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    push_private: Mapped[bool] = mapped_column(Boolean, default=False)  # уведомления без текста


class PairPause(Base):
    """Пара на паузе: уведомления друг другу не приходят. Снимает тот, кто поставил."""

    __tablename__ = "pair_pauses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), unique=True)
    by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyMood(Base):
    """«Состояние сегодня» одного человека. Видно партнёру только в этот день."""

    __tablename__ = "daily_moods"
    __table_args__ = (UniqueConstraint("pair_id", "user_id", "day", name="uq_mood_per_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PairLevel(Base):
    """Уровень смелости карточек, который выбрал человек. Паре открыт меньший из двух.
    Свой выбор партнёру не показывается."""

    __tablename__ = "pair_levels"
    __table_args__ = (UniqueConstraint("pair_id", "user_id", name="uq_pair_level"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pair_id: Mapped[str] = mapped_column(ForeignKey("pairs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(8), default="warm")
