"""A small reviewed video feed, own feedback, and explicitly shared couple topics."""
import hashlib
import json
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import content_catalogue as editorial, recommendations, video_catalogue as content
from ..db import get_session
from ..deps import PairContext, active_pair, consented_user, has_consent, today_date
from ..models import (ChatMessage, ContentState, Pair, PairMember, SharedVideoPreferences, User,
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


def _topics(row: VideoPreferences | SharedVideoPreferences | None,
            playback: content.Playback = "legacy") -> list[str]:
    present = {topic["id"] for topic in content.available_topics(playback)}
    return [topic for topic in json.loads(row.topics) if topic in present] if row else []


async def _settings(session: AsyncSession, user: User, playback: content.Playback = "legacy") -> dict:
    ctx = await _context(session, user)
    own = await session.get(VideoPreferences, user.id)
    shared = await session.get(SharedVideoPreferences, ctx.pair.id) if ctx else None
    return {
        "topics": content.available_topics(playback),
        "personal_topics": _topics(own, playback), "personal_revision": own.revision if own else 0,
        "shared_topics": _topics(shared, playback), "shared_revision": shared.revision if shared else 0,
        "has_pair": ctx is not None,
    }


def _validate_topics(body: SettingsIn, playback: content.Playback = "legacy") -> list[str]:
    valid = {topic["id"] for topic in content.available_topics(playback)}
    if len(body.topics) != len(set(body.topics)) or not set(body.topics).issubset(valid):
        raise HTTPException(422, "Выбери темы из текущей подборки без повторов")
    return [topic for topic in content.TOPIC_TITLES if topic in body.topics]


def _supported(video: content.CatalogueVideo, playback: content.Playback) -> bool:
    return playback == "platform-v1" or video.playback_kind == "direct"


def _require_video(video_id: str, playback: content.Playback = "legacy") -> content.CatalogueVideo:
    video = content.find_video(video_id)
    if video is None:
        raise HTTPException(404, "Этот ролик сейчас недоступен")
    if not _supported(video, playback):
        raise HTTPException(409, "Для этого видео обнови «Вечер» до новой версии")
    return video


async def _own_states(session: AsyncSession, user_id: str) -> dict[str, VideoState]:
    rows = (await session.execute(select(VideoState).where(VideoState.user_id == user_id))).scalars()
    states = {row.video_id: row for row in rows}
    try:
        snapshot = editorial.catalogue()
    except (OSError, ValueError, UnicodeError):
        return states
    linked = {item.id: item.video_id for item in snapshot.items if item.video_id is not None}
    if not linked:
        return states
    publications = (await session.execute(select(ContentState).where(
        ContentState.user_id == user_id, ContentState.content_id.in_(linked)))).scalars().all()
    groups: dict[str, list[ContentState]] = {}
    for row in publications:
        groups.setdefault(linked[row.content_id], []).append(row)
    for video_id, marks in groups.items():
        original = states.get(video_id)
        data = _state(original)
        effective = recommendations.merge_video_feedback(data, [json.loads(mark.data) for mark in marks])
        # Detached values only: GET must not assign into an attached ORM row or
        # flush a migration while merely reading a saved/hidden collection.
        modified = [mark.updated_at for mark in marks]
        if original and recommendations.has_explicit_video_feedback(data):
            modified.append(original.updated_at)
        states[video_id] = VideoState(user_id=user_id, video_id=video_id,
                                     data=json.dumps({**data, **effective}), updated_at=max(modified))
    return states


async def _out(session: AsyncSession, video: content.CatalogueVideo, state: dict,
               ctx: PairContext | None, playback: content.Playback = "legacy", *, user_id: str) -> dict:
    excluded = {"reviewed_at", "active", "plan_deck", "plan_text"}
    if playback == "legacy":
        excluded |= content.PROVIDER_FIELDS
    result = video.model_dump(exclude=excluded)
    if playback == "platform-v1":
        result["embed_url"] = video.embed_url
    linked, _ = await recommendations.video_publication_states(session, user_id, video.id)
    effective = recommendations.merge_video_feedback(state, [json.loads(row.data) for row in linked])
    result.update(effective)
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
                      ctx: PairContext | None, playback: content.Playback = "legacy") -> dict:
    return {"items": [await _out(session, video, _state(states.get(video.id)), ctx, playback, user_id=user.id)
                      for video in items[:limit]],
            "mode": mode, "catalogue_revision": content.catalogue_revision(playback), "total": len(items)}


@router.get("/settings")
async def settings(playback: content.Playback = "legacy", user: User = Depends(consented_user),
                   session: AsyncSession = Depends(get_session)):
    return await _settings(session, user, playback)


@router.put("/settings/personal")
async def personal_settings(body: SettingsIn, user: User = Depends(consented_user),
                            session: AsyncSession = Depends(get_session), playback: content.Playback = "legacy"):
    topics = _validate_topics(body, playback)
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
        return await _settings(session, user, playback)


@router.put("/settings/shared")
async def shared_settings(body: SettingsIn, ctx: PairContext = Depends(active_pair),
                          session: AsyncSession = Depends(get_session), playback: content.Playback = "legacy"):
    topics = _validate_topics(body, playback)
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
        return await _settings(session, ctx.user, playback)


@router.get("/feed")
async def feed(mode: Literal["personal", "shared", "saved"] = "personal",
               limit: int = Query(48, ge=1, le=96), user: User = Depends(consented_user),
               session: AsyncSession = Depends(get_session), day: date = Depends(today_date),
               playback: content.Playback = "legacy"):
    ctx = await _context(session, user)
    if mode == "shared" and ctx is None:
        raise HTTPException(409, "Для общей подборки сначала соединись с партнёром")
    states = await _own_states(session, user.id)
    items = list(content.active_catalogue(playback))
    if mode == "saved":
        # Hidden saved items remain reachable here so hiding is reversible.
        items = [video for video in items if _state(states.get(video.id)).get("saved")]
        items.sort(key=lambda video: states[video.id].updated_at, reverse=True)
    else:
        # New recommendations use the reviewed Russian platform catalogue.
        # Previously saved/sent direct clips remain accessible as an archive.
        if playback == "platform-v1":
            items = [video for video in items if video.playback_kind == "rutube_embed"]
        items = [video for video in items if not _state(states.get(video.id)).get("hidden")]
        prefs = await session.get(SharedVideoPreferences, ctx.pair.id) if mode == "shared" else await session.get(VideoPreferences, user.id)
        # Preserve a non-empty choice even if its last video was disabled.
        # Falling back to all topics would silently replace the user's choice.
        topics = json.loads(prefs.topics) if prefs else []
        selected_topics = set(topics)
        if selected_topics:
            items = [video for video in items if selected_topics.intersection(video.topics)]
        items = _rank(items, topics, states, seed=ctx.pair.id if mode == "shared" else user.id,
                      day=day, personal=mode == "personal")
    return await _collection(session, user, items, states, mode, limit, ctx, playback)


@router.get("/shared")
async def shared_collection(limit: int = Query(48, ge=1, le=96),
                            ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session),
                            playback: content.Playback = "legacy"):
    # Explicit chat sharing is the boundary: personal saves/likes never enter.
    rows = (await session.execute(select(ChatMessage.ref_id).where(
        ChatMessage.pair_id == ctx.pair.id, ChatMessage.ref_kind == "video", ChatMessage.deleted.is_(False))
        .group_by(ChatMessage.ref_id)
        .order_by(func.max(ChatMessage.created_at).desc(), ChatMessage.ref_id))).scalars()
    ids = dict.fromkeys(rows)
    items = [video for video_id in ids if (video := content.find_video(video_id)) is not None
             and _supported(video, playback)]
    states = await _own_states(session, ctx.user.id)
    return await _collection(session, ctx.user, items, states, "shared_collection", limit, ctx, playback)


@router.get("/{video_id}")
async def video(video_id: str, user: User = Depends(consented_user), session: AsyncSession = Depends(get_session),
                playback: content.Playback = "legacy"):
    item = _require_video(video_id, playback)
    state = await session.get(VideoState, (user.id, video_id))
    return await _out(session, item, _state(state), await _context(session, user), playback, user_id=user.id)


@router.patch("/{video_id}/state")
async def set_state(video_id: str, body: StateIn, user: User = Depends(consented_user),
                    session: AsyncSession = Depends(get_session), playback: content.Playback = "legacy"):
    item = _require_video(video_id, playback)
    async with user_lock(session, user.id):
        row = await session.get(VideoState, (user.id, video_id), populate_existing=True)
        data = _state(row)
        linked, editorial_available = await recommendations.video_publication_states(session, user.id, video_id)
        effective = recommendations.merge_video_feedback(data, [json.loads(state.data) for state in linked])
        changes = body.model_dump(exclude_unset=True)
        pending_reconciliation = not editorial_available and not set(changes).issubset(data.get("content_overrides", []))
        if pending_reconciliation or any(effective[key] != value for key, value in changes.items()):
            data.update(changes)
            if item.playback_kind == "rutube_embed" or linked or not editorial_available or data.get("content_overrides"):
                data["content_overrides"] = sorted(set(data.get("content_overrides", [])) | set(changes))
                data["content_revision"] = int(data.get("content_revision", 0)) + 1
            if row is None:
                row = VideoState(user_id=user.id, video_id=video_id, data=json.dumps(data))
                session.add(row)
            else:
                row.data, row.updated_at = json.dumps(data), utcnow()
            mapped = {"useful" if key == "liked" else key: value for key, value in changes.items()}
            for state in linked:
                old = json.loads(state.data)
                if any(old.get(key, False) != value for key, value in mapped.items()):
                    old.update(mapped)
                    state.data, state.revision, state.updated_at = json.dumps(old), state.revision + 1, utcnow()
            await enqueue_invalidation(session, [user.id], ["solo"])
            await session.commit()
        else:
            await session.commit()
        return await _out(session, item, data, await _context(session, user), playback, user_id=user.id)


@router.post("/{video_id}/viewed")
async def viewed(video_id: str, user: User = Depends(consented_user),
                 session: AsyncSession = Depends(get_session), day: date = Depends(today_date),
                 playback: content.Playback = "legacy"):
    _require_video(video_id, playback)
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
