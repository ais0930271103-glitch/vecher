"""Public publications with private, explicit reader choices.

The partner's private preferences and feedback are never queried. Shared mode
reads a separate pair row, and only deliberate shared-setting changes notify
the pair. Reading a post does not record a view, duration, or scroll history.
"""
import base64
import binascii
import hashlib
import hmac
import json
from datetime import timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import content_catalogue as content, content_analysis, recommendations, video_catalogue
from ..config import get_settings
from ..db import get_session
from ..deps import PairContext, active_pair, consented_user
from ..models import (ContentPreferences, ContentState, Pair, PairMember,
                      SharedContentPreferences, SharedVideoPreferences, User,
                      VideoPreferences, VideoState, utcnow)
from ..pair_lock import pair_lock
from ..realtime import enqueue_invalidation, invalidate_pair
from ..user_lock import user_lock


def _no_store(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"


router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(_no_store)])
Language = Literal["ru", "en", "none", "all"]
Mode = Literal["personal", "following", "shared", "saved"]
Format = Literal["all", "video", "article"]
Identifier = Annotated[str, Query(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topics: list[content.Id] = Field(max_length=80)
    language: Language
    revision: int = Field(strict=True, ge=0)


class ExplicitState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def supplied_booleans(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Provide one or more explicit boolean values")
        return self


class StateIn(ExplicitState):
    useful: Annotated[bool | None, Field(strict=True)] = None
    saved: Annotated[bool | None, Field(strict=True)] = None
    hidden: Annotated[bool | None, Field(strict=True)] = None


class AuthorStateIn(ExplicitState):
    followed: Annotated[bool | None, Field(strict=True)] = None
    hidden: Annotated[bool | None, Field(strict=True)] = None


def _snapshot() -> content.Catalogue:
    try:
        return recommendations.with_reviewed_videos(content.catalogue())
    except (OSError, ValueError, UnicodeError):
        # Do not expose a server path or an invalid, possibly private editorial
        # draft in a validation traceback returned to a reader.
        raise HTTPException(503, "Подборка временно недоступна. Попробуй обновить позже") from None


def _available(snapshot: content.Catalogue, *, deduplicate: bool = True) -> list[content.Publication]:
    items = content.published(snapshot)
    if any(item.kind == "video" for item in items):
        # The bridge uses the already reviewed catalogue. Existing /videos,
        # player IDs and private feedback remain available to older clients.
        videos = {video.id for video in video_catalogue.catalogue() if video.active}
        items = [item for item in items if item.kind != "video" or item.video_id in videos]
    return recommendations.unique_items(items) if deduplicate else items


def _require_item(snapshot: content.Catalogue, item_id: str) -> content.Publication:
    item = next((item for item in _available(snapshot, deduplicate=False) if item.id == item_id), None)
    if item is None:
        raise HTTPException(404, "Эта публикация сейчас недоступна")
    return item


def _require_author(snapshot: content.Catalogue, author_id: str) -> content.Author:
    author = content.author(snapshot, author_id)
    if author is None:
        raise HTTPException(404, "Автор не найден")
    return author


def _profile(row: ContentPreferences | None) -> dict:
    return json.loads(row.data) if row else {
        "topics": [], "language": "ru", "followed_author_ids": [], "hidden_author_ids": [],
    }


def _shared_profile(row: SharedContentPreferences | None) -> dict:
    return json.loads(row.data) if row else {"topics": [], "language": "ru"}


async def _reader_profile(session: AsyncSession, user_id: str, row: ContentPreferences | None) -> dict:
    result = _profile(row)
    if row is None:
        legacy = await session.get(VideoPreferences, user_id)
        if legacy:
            result["topics"] = list(dict.fromkeys(content_analysis.normalize_topic(topic)
                                                  for topic in json.loads(legacy.topics)))
    return result


async def _pair_profile(session: AsyncSession, pair_id: str,
                        row: SharedContentPreferences | None) -> dict:
    result = _shared_profile(row)
    if row is None:
        legacy = await session.get(SharedVideoPreferences, pair_id)
        if legacy:
            result["topics"] = list(dict.fromkeys(content_analysis.normalize_topic(topic)
                                                  for topic in json.loads(legacy.topics)))
    return result


def _state(row: ContentState | None) -> dict:
    return json.loads(row.data) if row else {"useful": False, "saved": False, "hidden": False}


async def _context(session: AsyncSession, user: User) -> PairContext | None:
    pair = (await session.execute(select(Pair).join(PairMember, PairMember.pair_id == Pair.id)
                                  .where(PairMember.user_id == user.id, Pair.status == "active"))).scalar_one_or_none()
    if pair is None:
        return None
    partner = (await session.execute(select(PairMember.user_id).where(
        PairMember.pair_id == pair.id, PairMember.user_id != user.id))).scalar_one_or_none()
    return PairContext(user=user, pair=pair, partner_id=partner) if partner else None


async def _states(session: AsyncSession, user_id: str) -> dict[str, ContentState]:
    rows = (await session.execute(select(ContentState).where(ContentState.user_id == user_id))).scalars()
    return {row.content_id: row for row in rows}


async def _video_states(session: AsyncSession, user_id: str) -> dict[str, VideoState]:
    rows = (await session.execute(select(VideoState).where(VideoState.user_id == user_id))).scalars()
    return {row.video_id: row for row in rows}


def _item_state(item: content.Publication, snapshot: content.Catalogue,
                states: dict[str, ContentState], videos: dict[str, VideoState]) -> dict:
    if item.video_id is None:
        return _state(states.get(item.id))
    legacy = json.loads(videos[item.video_id].data) if item.video_id in videos else {}
    # Earlier editorial video publications had independent explicit marks.
    # Preserve them without a GET-time migration; one video is shown once.
    linked = [_state(states[publication.id]) for publication in snapshot.items
              if publication.video_id == item.video_id and publication.id in states]
    merged = recommendations.merge_video_feedback(legacy, linked)
    return {field: merged[legacy_field] for field, legacy_field in recommendations.VIDEO_FIELDS.items()}


def _last_changed(item: content.Publication, snapshot: content.Catalogue,
                  states: dict[str, ContentState], videos: dict[str, VideoState]) -> float:
    rows = [states[item.id]] if item.id in states else []
    if item.video_id:
        rows = [states[linked.id] for linked in snapshot.items
                if linked.video_id == item.video_id and linked.id in states]
        if (item.video_id in videos
                and recommendations.has_explicit_video_feedback(json.loads(videos[item.video_id].data))):
            rows.append(videos[item.video_id])
    return max((row.updated_at.replace(tzinfo=timezone.utc).timestamp() if row.updated_at.tzinfo is None
                else row.updated_at.timestamp() for row in rows), default=0.0)


def _video_fingerprint(rows: dict[str, VideoState], relevant_ids: set[str]) -> list:
    result = []
    for video_id, row in sorted(rows.items()):
        if video_id not in relevant_ids:
            continue
        data = json.loads(row.data)
        if not recommendations.has_explicit_video_feedback(data):
            continue
        # New platform writes have an explicit counter. Older positive rows
        # retain their old explicit-write timestamp; viewed never changes it.
        revision = data.get("content_revision") or row.updated_at.isoformat()
        result.append((video_id, {field: bool(data.get(field, False))
                                  for field in ("liked", "saved", "hidden")},
                       revision, data.get("content_overrides", [])))
    return result


def _author_out(author: content.Author, profile: dict, items: list[content.Publication]) -> dict:
    return {**author.model_dump(), "published_count": sum(item.author_id == author.id for item in items),
            "followed": author.id in profile["followed_author_ids"],
            "hidden": author.id in profile["hidden_author_ids"]}


def _out(item: content.Publication, snapshot: content.Catalogue, profile: dict, state: dict,
         reason: str | None = None) -> dict:
    result = item.model_dump(mode="json", exclude={"author_id", "active", "reviewed_at"})
    result["author"] = _require_author(snapshot, item.author_id).model_dump()
    result.update({key: bool(state.get(key, False)) for key in ("useful", "saved", "hidden")})
    result["author_followed"] = item.author_id in profile["followed_author_ids"]
    result["author_hidden"] = item.author_id in profile["hidden_author_ids"]
    if reason is not None:
        result["recommendation_reason"] = reason
    if item.video_id:
        video = video_catalogue.find_video(item.video_id)
        if video:
            result.update(duration_seconds=round(video.duration_seconds), thumbnail_url=video.thumbnail_url,
                          source_url=video.source_url, playback_kind=video.playback_kind)
    return result


async def _settings(session: AsyncSession, user: User, snapshot: content.Catalogue) -> dict:
    ctx = await _context(session, user)
    own = await session.get(ContentPreferences, user.id)
    shared = await session.get(SharedContentPreferences, ctx.pair.id) if ctx else None
    private = await _reader_profile(session, user.id, own)
    common = await _pair_profile(session, ctx.pair.id, shared) if ctx else _shared_profile(None)
    available = _available(snapshot)
    topic_counts = {topic.id: sum(topic.id in item.topics for item in available) for topic in snapshot.topics}
    return {
        # Keep declared topics visible when their last post is withdrawn, so
        # the reader can still remove an old selection instead of being stuck.
        "topics": [{**topic.model_dump(), "available": topic_counts[topic.id] > 0,
                    "published_count": topic_counts[topic.id]} for topic in snapshot.topics],
        "languages": [{"id": key, "title": title} for key, title in content.LANGUAGE_TITLES.items()
                      ] + [{"id": "all", "title": "Все языки"}],
        # Preserve an unavailable selected ID rather than silently broadening
        # the next feed when an editor temporarily withdraws its last post.
        "personal_topics": private["topics"], "personal_language": private["language"],
        "personal_revision": own.revision if own else 0,
        "shared_topics": common["topics"], "shared_language": common["language"],
        "shared_revision": shared.revision if shared else 0,
        "followed_author_ids": private["followed_author_ids"],
        "hidden_author_ids": private["hidden_author_ids"], "has_pair": ctx is not None,
        "catalogue_revision": snapshot.revision, "unified_feed": True,
        "recommendation_signals": ["topics", "useful", "saved", "followed_authors", "hidden"],
    }


def _selected(body: SettingsIn, snapshot: content.Catalogue) -> dict:
    valid = {topic.id for topic in snapshot.topics}
    if len(body.topics) != len(set(body.topics)) or not set(body.topics).issubset(valid):
        raise HTTPException(422, "Выбери темы из подборки без повторов")
    return {"topics": [topic.id for topic in snapshot.topics if topic.id in body.topics],
            "language": body.language}


@router.get("/settings")
async def settings(user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    return await _settings(session, user, _snapshot())


@router.put("/settings/personal")
async def personal_settings(body: SettingsIn, user: User = Depends(consented_user),
                            session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    selected = _selected(body, snapshot)
    async with user_lock(session, user.id):
        row = await session.get(ContentPreferences, user.id, populate_existing=True)
        if body.revision != (row.revision if row else 0):
            raise HTTPException(409, "Личные настройки изменились — открой выбор заново")
        data = await _reader_profile(session, user.id, row)
        if row is None or any(data[key] != value for key, value in selected.items()):
            data.update(selected)
            if row is None:
                session.add(ContentPreferences(user_id=user.id, data=json.dumps(data), revision=1))
            else:
                row.data, row.revision, row.updated_at = json.dumps(data), row.revision + 1, utcnow()
            await enqueue_invalidation(session, [user.id], ["solo"])
        await session.commit()
    return await _settings(session, user, snapshot)


@router.put("/settings/shared")
async def shared_settings(body: SettingsIn, ctx: PairContext = Depends(active_pair),
                          session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    selected = _selected(body, snapshot)
    async with pair_lock(session, ctx.pair.id):
        row = await session.get(SharedContentPreferences, ctx.pair.id, populate_existing=True)
        if body.revision != (row.revision if row else 0):
            raise HTTPException(409, "Общие настройки изменились — открой выбор заново")
        if row is None or _shared_profile(row) != selected:
            if row is None:
                session.add(SharedContentPreferences(pair_id=ctx.pair.id, data=json.dumps(selected), revision=1))
            else:
                row.data, row.revision, row.updated_at = json.dumps(selected), row.revision + 1, utcnow()
            await invalidate_pair(session, ctx, ["us"])
        await session.commit()
    return await _settings(session, ctx.user, snapshot)


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _signature(raw: bytes) -> str:
    return hmac.new(get_settings().jwt_secret.encode(), b"vecher-content-cursor-v1:" + raw,
                    hashlib.sha256).hexdigest()


def _cursor(fingerprint: str, offset: int) -> str:
    raw = _json({"v": 1, "f": fingerprint, "o": offset})
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode() + "." + _signature(raw)


def _offset(cursor: str | None, fingerprint: str) -> int:
    if cursor is None:
        return 0
    try:
        encoded, signature = cursor.split(".")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if not hmac.compare_digest(_signature(raw), signature):
            raise ValueError("signature")
        value = json.loads(raw)
        if (set(value) != {"v", "f", "o"} or type(value["v"]) is not int or value["v"] != 1
                or type(value["o"]) is not int or not 0 <= value["o"] <= 5000
                or not isinstance(value["f"], str)):
            raise ValueError("shape")
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise HTTPException(422, "Не удалось прочитать следующую страницу. Обнови подборку") from None
    if not hmac.compare_digest(value["f"], fingerprint):
        raise HTTPException(409, "Подборка изменилась. Обнови её перед продолжением")
    return value["o"]


def _language_matches(item: content.Publication, language: str) -> bool:
    return language == "all" or item.language in {language, "none"}


def _search(item: content.Publication, snapshot: content.Catalogue, query: str) -> bool:
    return recommendations.search_score(item, _require_author(snapshot, item.author_id).name, query) >= 0


@router.get("/feed")
async def feed(mode: Mode = "personal", limit: int = Query(20, ge=1, le=50),
               cursor: str | None = Query(None, max_length=2048), q: str = Query("", max_length=120),
               author_id: Identifier | None = None, series_id: Identifier | None = None,
               format: Format = "all", discover: bool = False,
               user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    if discover and mode != "personal":
        raise HTTPException(422, "Знакомство с темами доступно в личной подборке")
    snapshot = _snapshot()
    all_items = _available(snapshot)
    # Old licensed direct clips keep their own saved archive and direct links.
    # New recommendations contain only the reviewed platform video catalogue.
    platform_ids = {video.id for video in video_catalogue.catalogue()
                    if video.active and video.playback_kind == "rutube_embed"}
    items = [item for item in all_items if item.kind != "video" or item.video_id in platform_ids]
    available_ids = [item.id for item in items]
    if format == "video":
        items = [item for item in items if item.kind == "video"]
    elif format == "article":
        items = [item for item in items if item.kind != "video"]
    own = await session.get(ContentPreferences, user.id)
    private = await _reader_profile(session, user.id, own)
    states = await _states(session, user.id)
    video_states = await _video_states(session, user.id)
    effective_states = {item.id: _item_state(item, snapshot, states, video_states) for item in all_items}
    ctx, shared = None, None
    if mode == "shared":
        ctx = await _context(session, user)
        if ctx is None:
            raise HTTPException(409, "Сначала соединись с партнёром")
        shared = await session.get(SharedContentPreferences, ctx.pair.id)
    if author_id:
        _require_author(snapshot, author_id)
        items = [item for item in items if item.author_id == author_id]
    if series_id:
        if not any(item.series_id == series_id for item in _available(snapshot)):
            raise HTTPException(404, "Серия сейчас недоступна")
        items = [item for item in items if item.series_id == series_id]
    explicit_browse = mode == "personal" and bool(author_id or series_id)
    settings = await _pair_profile(session, ctx.pair.id, shared) if mode == "shared" else private
    if mode == "saved":
        # A save remains reachable even after the reader hides its author or
        # changes their interests. Only editorial withdrawal removes it.
        items = [item for item in items if effective_states[item.id]["saved"]]
    else:
        items = [item for item in items if not effective_states[item.id]["hidden"]]
        if not explicit_browse:
            items = [item for item in items if item.author_id not in private["hidden_author_ids"]]
        if mode == "following":
            items = [item for item in items if item.author_id in private["followed_author_ids"]]
        if not explicit_browse:
            items = [item for item in items if _language_matches(item, settings["language"])]
            if mode != "following":
                topics = set(settings["topics"])
                # Only the explicit personal exploration switch can broaden
                # topics; it changes no stored choice and never affects shared.
                if not discover:
                    items = [item for item in items if topics.intersection(item.topics)]
    query = " ".join(q.casefold().split())
    embedding_index, embedding_revision = content_analysis.load_embedding_index()
    query_scores = {}
    if query:
        query_scores = {item.id: recommendations.search_score(
            item, _require_author(snapshot, item.author_id).name, query) for item in items}
        items = [item for item in items if query_scores[item.id] >= 0]
    reasons = {}
    if series_id:
        items.sort(key=lambda item: (item.series_order, item.id))
        reasons = {item.id: "Из выбранной вами серии" for item in items}
    elif mode == "saved":
        items.sort(key=lambda item: (_last_changed(item, snapshot, states, video_states), item.id), reverse=True)
        reasons = {item.id: "Вы сохранили этот материал" for item in items}
    else:
        # Public analysis can cross article/video formats. Private feedback
        # never enters the shared rank or explanations; own hides are filters.
        evidence_items = [item for item in all_items
                          if item.author_id not in private["hidden_author_ids"]]
        items, reasons = recommendations.rank(
            items, all_items=evidence_items, states=effective_states,
            topics=settings["topics"] if mode != "following" else [],
            followed_authors=private["followed_author_ids"],
            topic_titles={topic.id: topic.title for topic in snapshot.topics},
            mode=mode, query_scores=query_scores, discover=discover,
            embedding_index=embedding_index,
        )
    # Opaque authenticated cursor: no interests, author IDs or search terms
    # inside it. All filters and revisions are bound, including explicit
    # availability of referenced videos; a stale page cannot silently skip.
    fingerprint = hashlib.sha256(_json({
        "u": user.id, "p": ctx.pair.id if ctx else None, "m": mode, "q": query,
        "a": author_id, "s": series_id, "l": limit, "c": snapshot.revision,
        "format": format, "discover": discover,
        "ranker": recommendations.RECOMMENDER_VERSION,
        "analyser": content_analysis.ANALYZER_VERSION,
        "embeddings": embedding_revision,
        "video_catalogue": video_catalogue.catalogue_revision("platform-v1"),
        "available": available_ids, "r": own.revision if own else 0,
        "shared_r": shared.revision if shared else 0,
        "personal_profile": private, "selected_profile": settings,
        "states": [(key, row.revision) for key, row in sorted(states.items())],
        "video_states": _video_fingerprint(video_states, {item.video_id for item in all_items if item.video_id}),
    })).hexdigest()
    offset = _offset(cursor, fingerprint)
    page = items[offset:offset + limit]
    return {"items": [_out(item, snapshot, private, effective_states[item.id], reasons.get(item.id)) for item in page],
            "mode": mode, "total": len(items), "catalogue_revision": snapshot.revision,
            "format": format, "discovery": discover,
            "next_cursor": _cursor(fingerprint, offset + limit) if offset + limit < len(items) else None}


@router.get("/authors")
async def authors(user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    items = _available(snapshot)
    profile = _profile(await session.get(ContentPreferences, user.id))
    # Explicit author browser includes hidden authors, so hiding is reversible.
    return {"items": [_author_out(author, profile, items) for author in sorted(
        snapshot.authors, key=lambda author: (author.name.casefold(), author.id))]}


@router.get("/authors/{author_id}")
async def author(author_id: str, user: User = Depends(consented_user),
                 session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    return _author_out(_require_author(snapshot, author_id),
                       _profile(await session.get(ContentPreferences, user.id)), _available(snapshot))


@router.patch("/authors/{author_id}/state")
async def set_author_state(author_id: str, body: AuthorStateIn, user: User = Depends(consented_user),
                           session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    author = _require_author(snapshot, author_id)
    async with user_lock(session, user.id):
        row = await session.get(ContentPreferences, user.id, populate_existing=True)
        profile = await _reader_profile(session, user.id, row)
        changed = False
        for field, enabled in body.model_dump(exclude_unset=True).items():
            key = "followed_author_ids" if field == "followed" else "hidden_author_ids"
            values = set(profile[key])
            if (author_id in values) != enabled:
                changed = True
                values.add(author_id) if enabled else values.discard(author_id)
                profile[key] = sorted(values)
        if changed:
            if row is None:
                session.add(ContentPreferences(user_id=user.id, data=json.dumps(profile), revision=1))
            else:
                row.data, row.revision, row.updated_at = json.dumps(profile), row.revision + 1, utcnow()
            await enqueue_invalidation(session, [user.id], ["solo"])
        await session.commit()
    return _author_out(author, profile, _available(snapshot))


@router.get("/{content_id}")
async def publication(content_id: str, user: User = Depends(consented_user),
                      session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    item = _require_item(snapshot, content_id)
    return _out(item, snapshot,
                _profile(await session.get(ContentPreferences, user.id)),
                _item_state(item, snapshot, await _states(session, user.id), await _video_states(session, user.id)))


@router.patch("/{content_id}/state")
async def set_state(content_id: str, body: StateIn, user: User = Depends(consented_user),
                    session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    item = _require_item(snapshot, content_id)
    async with user_lock(session, user.id):
        if item.video_id:
            # /videos and /content share this lock and the existing video row.
            # Older editorial wrappers may also have ContentState marks; an
            # explicit undo clears the same field in those rows atomically.
            video_row = await session.get(VideoState, (user.id, item.video_id), populate_existing=True)
            linked_ids = [linked.id for linked in snapshot.items if linked.video_id == item.video_id]
            linked_rows = (await session.execute(select(ContentState).where(
                ContentState.user_id == user.id, ContentState.content_id.in_(linked_ids))
                .execution_options(populate_existing=True))).scalars().all()
            linked_states = {row.content_id: row for row in linked_rows}
            videos = {item.video_id: video_row} if video_row else {}
            state = _item_state(item, snapshot, linked_states, videos)
            changes = body.model_dump(exclude_unset=True)
            if any(state[key] != value for key, value in changes.items()):
                data = json.loads(video_row.data) if video_row else {"liked": False, "saved": False, "hidden": False}
                data.update({"liked" if key == "useful" else key: value for key, value in changes.items()})
                data["content_overrides"] = sorted(set(data.get("content_overrides", []))
                                                    | {recommendations.VIDEO_FIELDS[key] for key in changes})
                data["content_revision"] = int(data.get("content_revision", 0)) + 1
                if video_row is None:
                    session.add(VideoState(user_id=user.id, video_id=item.video_id, data=json.dumps(data)))
                else:
                    video_row.data, video_row.updated_at = json.dumps(data), utcnow()
                for linked in linked_rows:
                    prior = _state(linked)
                    if any(prior.get(key, False) != value for key, value in changes.items()):
                        prior.update(changes)
                        linked.data, linked.revision, linked.updated_at = json.dumps(prior), linked.revision + 1, utcnow()
                state.update(changes)
                await enqueue_invalidation(session, [user.id], ["solo"])
            await session.commit()
            return _out(item, snapshot, _profile(await session.get(ContentPreferences, user.id)), state)
        row = await session.get(ContentState, (user.id, content_id), populate_existing=True)
        state = _state(row)
        changes = body.model_dump(exclude_unset=True)
        if any(state[key] != value for key, value in changes.items()):
            state.update(changes)
            if row is None:
                session.add(ContentState(user_id=user.id, content_id=content_id,
                                         data=json.dumps(state), revision=1))
            else:
                row.data, row.revision, row.updated_at = json.dumps(state), row.revision + 1, utcnow()
            await enqueue_invalidation(session, [user.id], ["solo"])
        await session.commit()
        return _out(item, snapshot, _profile(await session.get(ContentPreferences, user.id)), state)
