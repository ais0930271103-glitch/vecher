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
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import content_catalogue as content, video_catalogue
from ..config import get_settings
from ..db import get_session
from ..deps import PairContext, active_pair, consented_user
from ..models import (ContentPreferences, ContentState, Pair, PairMember,
                      SharedContentPreferences, User, utcnow)
from ..pair_lock import pair_lock
from ..realtime import enqueue_invalidation, invalidate_pair
from ..user_lock import user_lock


def _no_store(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"


router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(_no_store)])
Language = Literal["ru", "en", "none", "all"]
Mode = Literal["personal", "following", "shared", "saved"]
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
        return content.catalogue()
    except (OSError, ValueError, UnicodeError):
        # Do not expose a server path or an invalid, possibly private editorial
        # draft in a validation traceback returned to a reader.
        raise HTTPException(503, "Подборка временно недоступна. Попробуй обновить позже") from None


def _available(snapshot: content.Catalogue) -> list[content.Publication]:
    items = content.published(snapshot)
    if any(item.kind == "video" for item in items):
        # Only intentional, validated references enter the new feed. Existing
        # /videos, IDs and feedback remain available to old and new clients.
        videos = {video.id for video in video_catalogue.catalogue() if video.active}
        items = [item for item in items if item.kind != "video" or item.video_id in videos]
    return items


def _require_item(snapshot: content.Catalogue, item_id: str) -> content.Publication:
    item = next((item for item in _available(snapshot) if item.id == item_id), None)
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


def _author_out(author: content.Author, profile: dict, items: list[content.Publication]) -> dict:
    return {**author.model_dump(), "published_count": sum(item.author_id == author.id for item in items),
            "followed": author.id in profile["followed_author_ids"],
            "hidden": author.id in profile["hidden_author_ids"]}


def _out(item: content.Publication, snapshot: content.Catalogue, profile: dict, state: dict) -> dict:
    result = item.model_dump(mode="json", exclude={"author_id", "active", "reviewed_at"})
    result["author"] = _require_author(snapshot, item.author_id).model_dump()
    result.update({key: bool(state.get(key, False)) for key in ("useful", "saved", "hidden")})
    result["author_followed"] = item.author_id in profile["followed_author_ids"]
    result["author_hidden"] = item.author_id in profile["hidden_author_ids"]
    return result


async def _settings(session: AsyncSession, user: User, snapshot: content.Catalogue) -> dict:
    ctx = await _context(session, user)
    own = await session.get(ContentPreferences, user.id)
    shared = await session.get(SharedContentPreferences, ctx.pair.id) if ctx else None
    private, common = _profile(own), _shared_profile(shared)
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
        "catalogue_revision": snapshot.revision,
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
        data = _profile(row)
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
    text = [item.title, item.summary, _require_author(snapshot, item.author_id).name]
    for block in item.blocks:
        text.extend(block.items if isinstance(block, content.ListBlock) else [block.text])
    haystack = " ".join(text).casefold()
    return all(term in haystack for term in query.split())


@router.get("/feed")
async def feed(mode: Mode = "personal", limit: int = Query(20, ge=1, le=50),
               cursor: str | None = Query(None, max_length=2048), q: str = Query("", max_length=120),
               author_id: Identifier | None = None, series_id: Identifier | None = None,
               user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    items = _available(snapshot)
    available_ids = [item.id for item in items]
    own = await session.get(ContentPreferences, user.id)
    private = _profile(own)
    states = await _states(session, user.id)
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
    if mode == "saved":
        # A save remains reachable even after the reader hides its author or
        # changes their interests. Only editorial withdrawal removes it.
        items = [item for item in items if _state(states.get(item.id))["saved"]]
    else:
        items = [item for item in items if not _state(states.get(item.id))["hidden"]]
        if not explicit_browse:
            items = [item for item in items if item.author_id not in private["hidden_author_ids"]]
        if mode == "following":
            items = [item for item in items if item.author_id in private["followed_author_ids"]]
        settings = _shared_profile(shared) if mode == "shared" else private
        if not explicit_browse:
            items = [item for item in items if _language_matches(item, settings["language"])]
            if mode != "following":
                topics = set(settings["topics"])
                items = [item for item in items if topics.intersection(item.topics)]
    query = " ".join(q.casefold().split())
    if query:
        items = [item for item in items if _search(item, snapshot, query)]
    if series_id:
        items.sort(key=lambda item: (item.series_order, item.id))
    elif mode == "saved":
        items.sort(key=lambda item: (states[item.id].updated_at, item.id), reverse=True)
    else:
        items.sort(key=lambda item: (item.published_at, item.id), reverse=True)
    # Opaque authenticated cursor: no interests, author IDs or search terms
    # inside it. All filters and revisions are bound, including explicit
    # availability of referenced videos; a stale page cannot silently skip.
    fingerprint = hashlib.sha256(_json({
        "u": user.id, "p": ctx.pair.id if ctx else None, "m": mode, "q": query,
        "a": author_id, "s": series_id, "l": limit, "c": snapshot.revision,
        "available": available_ids, "r": own.revision if own else 0,
        "shared_r": shared.revision if shared else 0,
        "states": [(key, row.revision) for key, row in sorted(states.items())],
    })).hexdigest()
    offset = _offset(cursor, fingerprint)
    page = items[offset:offset + limit]
    return {"items": [_out(item, snapshot, private, _state(states.get(item.id))) for item in page],
            "mode": mode, "total": len(items), "catalogue_revision": snapshot.revision,
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
        profile = _profile(row)
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
    return _out(_require_item(snapshot, content_id), snapshot,
                _profile(await session.get(ContentPreferences, user.id)),
                _state(await session.get(ContentState, (user.id, content_id))))


@router.patch("/{content_id}/state")
async def set_state(content_id: str, body: StateIn, user: User = Depends(consented_user),
                    session: AsyncSession = Depends(get_session)):
    snapshot = _snapshot()
    item = _require_item(snapshot, content_id)
    async with user_lock(session, user.id):
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
