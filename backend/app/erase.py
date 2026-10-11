"""Удаление данных: завершение пары и удаление аккаунта (152-ФЗ — право на удаление)."""
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Activity, ChatMessage, PlaceVote, ChatRead, Consent, DailyAnswer, DailyMood, Invite, DeviceToken, Letter, LoginCode, Pair, PairDate, PairLevel, PairMember, PairPause,
    PairSubscription, PartnerNote, PlanUnlock, PlanVote, ProfileFact, QuietWish, RadarSignal, Reminder, Surprise,
    User, UserCity, UserSettings, WishSwipe, WishView,
)

# Всё, что относится к паре: ответы, выборы в свайпере, планы, даты, письма, сюрпризы, заметки.
PAIR_TABLES = (DailyAnswer, DailyMood, Activity, RadarSignal, WishSwipe, WishView, PairSubscription, PlanUnlock, PlanVote,
               PartnerNote, PairDate, Reminder, Letter, Surprise, PairPause, PairLevel, Invite, ChatMessage, ChatRead, PlaceVote)
# Личное, не связанное с парой: анкета «о себе», тихие желания, устройства, согласия, настройки.
USER_TABLES = ((ProfileFact, ProfileFact.user_id), (QuietWish, QuietWish.user_id),
               (DeviceToken, DeviceToken.user_id), (Consent, Consent.user_id), (UserSettings, UserSettings.user_id),
               (UserCity, UserCity.user_id))


async def erase_pair(session: AsyncSession, pair_id: str) -> None:
    """Пара завершена — удаляем всё общее у обоих. Анкеты «о себе» остаются у каждого."""
    for model in PAIR_TABLES:
        await session.execute(delete(model).where(model.pair_id == pair_id))
    await session.execute(delete(PairMember).where(PairMember.pair_id == pair_id))
    await session.execute(delete(Pair).where(Pair.id == pair_id))


async def erase_user(session: AsyncSession, user: User) -> None:
    """Удалить аккаунт и все данные человека (вместе с парой, если она есть)."""
    pair_id = (await session.execute(
        select(PairMember.pair_id).where(PairMember.user_id == user.id)
    )).scalar_one_or_none()
    if pair_id:
        await erase_pair(session, pair_id)
    for model, column in USER_TABLES:
        await session.execute(delete(model).where(column == user.id))
    await session.execute(delete(LoginCode).where(LoginCode.email == user.email))
    await session.execute(delete(User).where(User.id == user.id))
