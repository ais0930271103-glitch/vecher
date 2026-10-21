"""«Выбрать конкретное»: места и события к плану совпадения.

Подборка — для города, где каждый сейчас (с учётом поездки), с оглядкой на памятку обоих:
«Аллергии и что не ем» убирает неподходящее, «Еда» и «Музыка и артисты» поднимают подходящее наверх.
Каждый отмечает, что нравится; совпало у обоих — место выбрано. Дальше — бронь/билеты по ссылке.
"""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.places import CARD_KINDS, DEMO_PLACES, KIND_LABELS
from ..db import get_session
from ..deps import PairContext, special_pair, today_date
from ..models import Place, PlaceVote, ProfileFact, User, UserCity
from ..push import notify
from ..realtime import invalidate_pair
from .city import current_city
from .plans import _matched_plan, _unlock

router = APIRouter(prefix="/plans", tags=["places"])

def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def ensure_places(session: AsyncSession) -> None:
    """Сверяет примеры мест в базе с app/content/places.py."""
    existing = {p.key: p for p in (await session.execute(select(Place))).scalars()}
    for d in DEMO_PLACES:
        p = existing.get(d["key"])
        if p is None:
            p = Place(key=d["key"])
            session.add(p)
        p.kind, p.title, p.description = d["kind"], d["title"], d.get("description", "")
        p.city, p.price, p.url, p.address = d.get("city", "*"), d.get("price", 2), d.get("url"), d.get("address")
        p.starts_at = datetime.fromisoformat(d["date"]) if d.get("date") else None
        p.demo = d.get("demo", True)
    await session.flush()


def _meta(key: str) -> dict:
    return next((d for d in DEMO_PLACES if d["key"] == key), {})


class PlaceOut(BaseModel):
    id: int
    kind: str
    kind_label: str
    title: str
    description: str
    address: str | None
    city: str | None
    price: int
    url: str | None
    starts_at: datetime | None
    demo: bool
    reasons: list[str]
    mine: bool
    partner: bool


class PlacesOut(BaseModel):
    card_id: int
    kinds: list[str]
    cities: list[str]
    places: list[PlaceOut]
    chosen: list[int]  # места, которые отметили оба
    hidden: int        # сколько вариантов убрано из-за «Аллергии и что не ем»


async def _facts(session: AsyncSession, user_id: str) -> dict[str, str]:
    rows = (await session.execute(
        select(ProfileFact.key, ProfileFact.value).where(
            ProfileFact.user_id == user_id, ProfileFact.key.in_(("food", "allergies", "music")))
    )).all()
    return {k: (v or "").lower() for k, v in rows}


def _short(text: str, n: int = 50) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


async def _places_out(session: AsyncSession, ctx: PairContext, card_id: int, today: date) -> PlacesOut:
    card, _, _ = await _matched_plan(session, ctx, card_id)
    if await _unlock(session, ctx, card_id) is None:
        raise HTTPException(402, "Сначала откройте план")
    kinds = CARD_KINDS.get(card.text)
    if not kinds:
        raise HTTPException(404, "Для этого плана места пока не подбираются")
    await ensure_places(session)

    me_city = current_city(await session.get(UserCity, ctx.user.id), today)
    partner_city = current_city(await session.get(UserCity, ctx.partner_id), today)
    cities = [c for c in dict.fromkeys([me_city, partner_city]) if c]

    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(Place).where(Place.kind.in_(kinds), Place.active.is_(True), Place.city.in_([*cities, "*"]))
    )).scalars().all()
    rows = [p for p in rows if p.starts_at is None or _aware(p.starts_at) >= now - timedelta(hours=3)]

    partner_user = await session.get(User, ctx.partner_id)
    partner_name = partner_user.display_name if partner_user and partner_user.display_name else "Партнёр"
    people = [("Ты", await _facts(session, ctx.user.id), "любишь", "слушаешь"),
              (partner_name, await _facts(session, ctx.partner_id), "любит", "слушает")]

    votes = (await session.execute(
        select(PlaceVote.user_id, PlaceVote.place_id).where(PlaceVote.pair_id == ctx.pair.id, PlaceVote.card_id == card_id)
    )).all()
    mine = {pid for uid, pid in votes if uid == ctx.user.id}
    theirs = {pid for uid, pid in votes if uid == ctx.partner_id}

    out, hidden = [], 0
    for p in rows:
        meta = _meta(p.key)
        if any(word in facts.get("allergies", "") for _, facts, _, _ in people for word in meta.get("avoid", [])):
            hidden += 1
            continue
        reasons = []
        for name, facts, loves, listens in people:
            food = facts.get("food", "")
            if food and any(w in food for w in meta.get("cuisine", [])):
                reasons.append(f"{name} {loves}: {_short(food)}")
            music = facts.get("music", "")
            if music and any(w in music for w in meta.get("artist", [])):
                reasons.append(f"{name} {listens}: {_short(music)}")
        out.append(PlaceOut(
            id=p.id, kind=p.kind, kind_label=KIND_LABELS.get(p.kind, p.kind), title=p.title,
            description=p.description, address=p.address, city=None if p.city == "*" else p.city, price=p.price,
            url=p.url, starts_at=_aware(p.starts_at), demo=p.demo, reasons=reasons,
            mine=p.id in mine, partner=p.id in theirs,
        ))
    out.sort(key=lambda x: (-len(x.reasons), x.demo, x.starts_at or now + timedelta(days=365), x.title))
    await session.commit()
    return PlacesOut(card_id=card_id, kinds=[KIND_LABELS.get(k, k) for k in kinds], cities=cities, places=out,
                     chosen=sorted(mine & theirs), hidden=hidden)


@router.get("/{card_id}/places", response_model=PlacesOut)
async def places(card_id: int, ctx: PairContext = Depends(special_pair),
                 session: AsyncSession = Depends(get_session), today: date = Depends(today_date)):
    return await _places_out(session, ctx, card_id, today)


@router.post("/{card_id}/places/{place_id}/vote", response_model=PlacesOut)
async def vote(card_id: int, place_id: int, ctx: PairContext = Depends(special_pair),
               session: AsyncSession = Depends(get_session), today: date = Depends(today_date)):
    """Отметить / снять «нравится». Совпало у обоих — сообщаем партнёру."""
    current = await _places_out(session, ctx, card_id, today)  # проверяет совпадение, открытый план и место
    place = next((p for p in current.places if p.id == place_id), None)
    if place is None:
        raise HTTPException(404, "Такого места в подборке нет")
    row = (await session.execute(
        select(PlaceVote).where(PlaceVote.pair_id == ctx.pair.id, PlaceVote.user_id == ctx.user.id,
                                PlaceVote.card_id == card_id, PlaceVote.place_id == place_id)
    )).scalar_one_or_none()
    if row is None:
        session.add(PlaceVote(pair_id=ctx.pair.id, user_id=ctx.user.id, card_id=card_id, place_id=place_id))
    else:
        await session.delete(row)
    if row is None and place.partner:
        await notify(session, ctx.partner_id, "Вечер", f"Совпало место для вашего плана: {place.title}",
                     {"open": "plan", "card_id": card_id}, key=f"place:{ctx.pair.id}:{card_id}:{place_id}",
                     pair_id=ctx.pair.id)
    await invalidate_pair(session, ctx, ["plans"])
    await session.commit()
    return await _places_out(session, ctx, card_id, today)
