"""A small reviewed video feed, own feedback, and explicitly shared couple topics."""
import hashlib
import json
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import video_catalogue as content
from ..db import get_session
from ..deps import PairContext, active_pair, consented_user, has_consent, today_date
from ..models import (ChatMessage, Pair, PairMember, SharedVideoPreferences, User,
                      VideoPreferences, VideoState, WishCard, utcnow)
from ..pair_lock import pair_lock
from ..realtime import enqueue_invalidation, invalidate_pair
from ..user_lock import user_lock
from .evenings import _visible

router = APIRouter(prefix="/videos", tags=["videos"])


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topics: list[str] = Field(max_length=len(content.TOPIC_TITLES))
    revision: Annotated[int, Field(strict=True, ge=0)]


class StateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    liked: Annotated[bool | None, Field(strict=True)] = None
    saved: Annotated[bool | None, Field(strict=True)] = None
    hidden: Annotated[bool | None, Field(strict=True)] = None

    @model_validator(mode="after")
    def supplied_booleans(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Provide one or more explicit boolean values")
        return self


async def _context(session: AsyncSession, user: User) -> PairContext | None:
    row = (await session.execute(select(Pair).join(PairMember, PairMember.pair_id == Pair.id)
                                 .where(PairMember.user_id == user.id, Pair.status == "active"))).scalar_one_or_none()
    if row is None:
        return None
    partner = (await session.execute(select(PairMember.user_id).where(
        PairMember.pair_id == row.id, PairMember.user_id != user.id))).scalar_one_or_none()
    return PairContext(user=user, pair=row, partner_id=partner) if partner else None


def _state(row: VideoState | None) -> dict:
    return json.loads(row.data) if row else {"liked": False, "saved": False, "hidden": False}


def _topics(row: VideoPreferences | SharedVideoPreferences | None) -> list[str]:
    present = {topic["id"] for topic in content.available_topics()}
    return [topic for topic in json.loads(row.topics) if topic in present] if row else []


async def _settings(session: AsyncSession, user: User) -> dict:
    ctx = await _context(session, user)
    own = await session.get(VideoPreferences, user.id)
    shared = await session.get(SharedVideoPreferences, ctx.pair.id) if ctx else None
    return {
        "topics": content.available_topics(),
        "personal_topics": _topics(own), "personal_revision": own.revision if own else 0,
        "shared_topics": _topics(shared), "shared_revision": shared.revision if shared else 0,
        "has_pair": ctx is not None,
    }


def _validate_topics(body: SettingsIn) -> list[str]:
    valid = {topic["id"] for topic in content.available_topics()}
    if len(body.topics) != len(set(body.topics)) or not set(body.topics).issubset(valid):
        raise HTTPException(422, "Выбери темы из текущей подборки без повторов")
    return [topic for topic in content.TOPIC_TITLES if topic in body.topics]


def _require_video(video_id: str) -> content.CatalogueVideo:
    video = content.find_video(video_id)
    if video is None:
        raise HTTPException(404, "Этот ролик сейчас недоступен")
    return video


async def _own_states(session: AsyncSession, user_id: str) -> dict[str, VideoState]:
    rows = (await session.execute(select(VideoState).where(VideoState.user_id == user_id))).scalars()
    return {row.video_id: row for row in rows}


async def _out(session: AsyncSession, video: content.CatalogueVideo, state: dict,
               ctx: PairContext | None) -> dict:
    result = video.model_dump(exclude={"reviewed_at", "active", "plan_deck", "plan_text"})
    result.update({field: bool(state.get(field, False)) for field in ("liked", "saved", "hidden")})
    result["plan_card_id"] = None
    if (ctx and video.plan_text and await has_consent(session, ctx.user.id, ("personal", "special"))):
        # Source identities are text + deck, never guessed autoincrement IDs.
        card_id = (await session.execute(select(WishCard.id).where(
            WishCard.deck == video.plan_deck, WishCard.text == video.plan_text,
            WishCard.active.is_(True)))).scalars().first()
        if card_id is not None and await _visible(session, ctx, card_id):
            result["plan_card_id"] = card_id
    return result


def _rank(videos: list[content.CatalogueVideo], topics: list[str], states: dict[str, VideoState],
          *, seed: str, day: date, personal: bool) -> list[content.CatalogueVideo]:
    """Small deterministic ranking, with varied authors/topics and limited repetition.

    Personal boosts read only this user's explicit likes/saves. Shared mode
    does not use either partner's private interest, feedback or viewing data.
    """
    positive_topics: dict[str, int] = {}
    if personal:
        for video in videos:
            data = _state(states.get(video.id))
            if data.get("liked") or data.get("saved"):
                for topic in video.topics:
                    positive_topics[topic] = positive_topics.get(topic, 0) + 1

    def score(video: content.CatalogueVideo):
        weight = 4 * bool(set(video.topics) & set(topics))
        if personal:
            weight += min(2, sum(positive_topics.get(topic, 0) for topic in video.topics))
            seen = _state(states.get(video.id)).get("last_viewed_day")
            if seen and seen >= (day - timedelta(days=2)).isoformat():
                weight -= 8
        shuffle = hashlib.sha256(f"{seed}:{day.isoformat()}:{video.id}".encode()).digest()
        return (-weight, shuffle)

    remaining = sorted(videos, key=score)
    output = []
    while remaining:
        # Keep the best-ranked group, but avoid an author/topic monopolising
        # consecutive cards when another similarly ranked video is available.
        index = 0
        if output:
            best = score(remaining[0])[0]
            for candidate, video in enumerate(remaining[:6]):
                if score(video)[0] > best + 2:
                    break
                repeated_topic = len(output) >= 2 and bool(
                    set(video.topics) & set(output[-1].topics) & set(output[-2].topics))
                if video.author != output[-1].author and not repeated_topic:
                    index = candidate
                    break
        output.append(remaining.pop(index))
    return output


async def _collection(session: AsyncSession, user: User, items: list[content.CatalogueVideo],
                      states: dict[str, VideoState], mode: str, limit: int,
                      ctx: PairContext | None) -> dict:
    return {"items": [await _out(session, video, _state(states.get(video.id)), ctx) for video in items[:limit]],
            "mode": mode, "catalogue_revision": content.catalogue_revision(), "total": len(items)}


@router.get("/settings")
async def settings(user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    return await _settings(session, user)


@router.put("/settings/personal")
async def personal_settings(body: SettingsIn, user: User = Depends(consented_user),
                            session: AsyncSession = Depends(get_session)):
    topics = _validate_topics(body)
    async with user_lock(session, user.id):
        row = await session.get(VideoPreferences, user.id, populate_existing=True)
        if body.revision != (row.revision if row else 0):
            raise HTTPException(409, "Темы изменились — открой выбор заново")
        if row is None:
            row = VideoPreferences(user_id=user.id, topics=json.dumps(topics), revision=1)
            session.add(row)
        else:
            row.topics, row.revision, row.updated_at = json.dumps(topics), row.revision + 1, utcnow()
        await enqueue_invalidation(session, [user.id], ["solo"])
        await session.commit()
        return await _settings(session, user)


@router.put("/settings/shared")
async def shared_settings(body: SettingsIn, ctx: PairContext = Depends(active_pair),
                          session: AsyncSession = Depends(get_session)):
    topics = _validate_topics(body)
    async with pair_lock(session, ctx.pair.id):
        row = await session.get(SharedVideoPreferences, ctx.pair.id, populate_existing=True)
        if body.revision != (row.revision if row else 0):
            raise HTTPException(409, "Общие темы уже изменились — открой выбор заново")
        if row is None:
            row = SharedVideoPreferences(pair_id=ctx.pair.id, topics=json.dumps(topics), revision=1)
            session.add(row)
        else:
            row.topics, row.revision, row.updated_at = json.dumps(topics), row.revision + 1, utcnow()
        await invalidate_pair(session, ctx, ["us"])
        await session.commit()
        return await _settings(session, ctx.user)


@router.get("/feed")
async def feed(mode: Literal["personal", "shared", "saved"] = "personal",
               limit: int = Query(48, ge=1, le=96), user: User = Depends(consented_user),
               session: AsyncSession = Depends(get_session), day: date = Depends(today_date)):
    ctx = await _context(session, user)
    if mode == "shared" and ctx is None:
        raise HTTPException(409, "Для общей подборки сначала соединись с партнёром")
    states = await _own_states(session, user.id)
    items = list(content.active_catalogue())
    if mode == "saved":
        # Hidden saved items remain reachable here so hiding is reversible.
        items = [video for video in items if _state(states.get(video.id)).get("saved")]
        items.sort(key=lambda video: states[video.id].updated_at, reverse=True)
    else:
        items = [video for video in items if not _state(states.get(video.id)).get("hidden")]
        prefs = await session.get(SharedVideoPreferences, ctx.pair.id) if mode == "shared" else await session.get(VideoPreferences, user.id)
        items = _rank(items, _topics(prefs), states, seed=ctx.pair.id if mode == "shared" else user.id,
                      day=day, personal=mode == "personal")
    return await _collection(session, user, items, states, mode, limit, ctx)


@router.get("/shared")
async def shared_collection(limit: int = Query(48, ge=1, le=96),
                            ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    # Explicit chat sharing is the boundary: personal saves/likes never enter.
    rows = (await session.execute(select(ChatMessage.ref_id).where(
        ChatMessage.pair_id == ctx.pair.id, ChatMessage.ref_kind == "video", ChatMessage.deleted.is_(False))
        .group_by(ChatMessage.ref_id)
        .order_by(func.max(ChatMessage.created_at).desc(), ChatMessage.ref_id))).scalars()
    ids = dict.fromkeys(rows)
    items = [video for video_id in ids if (video := content.find_video(video_id)) is not None]
    states = await _own_states(session, ctx.user.id)
    return await _collection(session, ctx.user, items, states, "shared_collection", limit, ctx)


@router.get("/{video_id}")
async def video(video_id: str, user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    item = _require_video(video_id)
    state = await session.get(VideoState, (user.id, video_id))
    return await _out(session, item, _state(state), await _context(session, user))


@router.patch("/{video_id}/state")
async def set_state(video_id: str, body: StateIn, user: User = Depends(consented_user),
                    session: AsyncSession = Depends(get_session)):
    item = _require_video(video_id)
    async with user_lock(session, user.id):
        row = await session.get(VideoState, (user.id, video_id), populate_existing=True)
        data = _state(row)
        changes = body.model_dump(exclude_unset=True)
        if any(data.get(key, False) != value for key, value in changes.items()):
            data.update(changes)
            if row is None:
                row = VideoState(user_id=user.id, video_id=video_id, data=json.dumps(data))
                session.add(row)
            else:
                row.data, row.updated_at = json.dumps(data), utcnow()
            await enqueue_invalidation(session, [user.id], ["solo"])
            await session.commit()
        else:
            await session.commit()
        return await _out(session, item, data, await _context(session, user))


@router.post("/{video_id}/viewed")
async def viewed(video_id: str, user: User = Depends(consented_user),
                 session: AsyncSession = Depends(get_session), day: date = Depends(today_date)):
    _require_video(video_id)
    async with user_lock(session, user.id):
        row = await session.get(VideoState, (user.id, video_id), populate_existing=True)
        data = _state(row)
        if data.get("last_viewed_day") != day.isoformat():
            data["last_viewed_day"] = day.isoformat()
            if row is None:
                session.add(VideoState(user_id=user.id, video_id=video_id, data=json.dumps(data)))
            else:
                row.data = json.dumps(data)
                # A view must not reorder the user's explicitly saved collection.
        await session.commit()
    return {"ok": True}
