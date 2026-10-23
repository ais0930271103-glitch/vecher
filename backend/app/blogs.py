"""Public-blog ownership, exact-version consent and normalized reader content.

This module never reads private interests, chat attachments, messages or couple
answers. A public identity is typed explicitly by its owners. The editorial
snapshot remains a separate, read-only source.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from weakref import WeakValueDictionary

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import content_catalogue as content
from .config import get_settings
from .models import (BlogMediaAsset, Pair, PairMember, PairPause, PublicBlog,
                     PublicBlogConsent, PublicPost, PublicPostApproval,
                     PublicPostReport, User, utcnow)
from .pair_lock import pair_lock
from .user_lock import user_lock

BLOG_CONSENT_VERSION = "public-blog-v1"
POST_CONSENT_VERSION = "public-post-v1"
MAX_POSTS_PER_BLOG = 100
MAX_PUBLIC_BLOGS = 500
MAX_PUBLISHED_POSTS = 1000
_SHA = re.compile(r"^[0-9a-f]{64}$")
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def enabled() -> bool:
    return bool(getattr(get_settings(), "blogs_enabled", False))


def require_enabled() -> None:
    if not enabled():
        raise HTTPException(404, "Публичные блоги пока недоступны")


def request_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def author_id(blog_id: str) -> str:
    return "blog-" + blog_id


def content_id(post_id: str) -> str:
    return "post-" + post_id


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def owner_ids(session: AsyncSession, blog: PublicBlog) -> set[str]:
    if blog.kind == "individual":
        found = await session.scalar(select(User.id).where(User.id == blog.owner_user_id))
        return {found} if found else set()
    pair = await session.get(Pair, blog.pair_id, populate_existing=True)
    if pair is None or pair.status != "active":
        return set()
    members = set((await session.execute(select(PairMember.user_id).where(
        PairMember.pair_id == blog.pair_id))).scalars())
    return members if len(members) == 2 else set()


async def consented_owners(session: AsyncSession, blog: PublicBlog) -> set[str]:
    return set((await session.execute(select(PublicBlogConsent.user_id).where(
        PublicBlogConsent.blog_id == blog.id,
        PublicBlogConsent.blog_revision == blog.revision,
        PublicBlogConsent.consent_version == BLOG_CONSENT_VERSION,
        PublicBlogConsent.accepted.is_(True)))).scalars())


async def profile_is_public(session: AsyncSession, blog: PublicBlog) -> bool:
    if blog.status != "active":
        return False
    owners = await owner_ids(session, blog)
    return bool(owners) and owners == await consented_owners(session, blog)


async def is_paused(session: AsyncSession, blog: PublicBlog) -> bool:
    return bool(blog.pair_id and await session.scalar(select(PairPause.id).where(
        PairPause.pair_id == blog.pair_id)))


async def require_blog_owner(session: AsyncSession, user_id: str, blog_id: str,
                             *, write: bool = False) -> PublicBlog:
    require_enabled()
    blog = await session.get(PublicBlog, blog_id, populate_existing=True)
    if blog is None or user_id not in await owner_ids(session, blog):
        # A guessed ID never exposes another person's unfinished public draft.
        raise HTTPException(404, "Блог не найден")
    if write:
        if await is_paused(session, blog):
            raise HTTPException(409, "Пара на паузе — новые публикации пока недоступны")
        if blog.status == "withdrawn" or user_id not in await consented_owners(session, blog):
            raise HTTPException(409, "Сначала подтверди эту публичную версию блога")
    return blog


@dataclass(frozen=True)
class BlogMediaIdentity:
    id: str
    kind: str
    owner_user_id: str | None
    pair_id: str | None
    status: str
    revision: int


def media_identity_query(blog_id: str):
    return select(PublicBlog.id, PublicBlog.kind, PublicBlog.owner_user_id,
                  PublicBlog.pair_id, PublicBlog.status, PublicBlog.revision).where(PublicBlog.id == blog_id)


async def require_blog_media_owner(session: AsyncSession, user_id: str, blog_id: str) -> BlogMediaIdentity:
    """Worker authorization without reading any encrypted author text."""
    require_enabled()
    row = (await session.execute(media_identity_query(blog_id))).one_or_none()
    blog = BlogMediaIdentity(*row) if row else None
    if blog is None or user_id not in await owner_ids(session, blog):
        raise HTTPException(404, "Блог не найден")
    return blog


@asynccontextmanager
async def blog_media_write_lock(session: AsyncSession, blog_id: str):
    """The restricted media worker needs identity locks, never DATA_KEYS."""
    row = (await session.execute(media_identity_query(blog_id))).one_or_none()
    blog = BlogMediaIdentity(*row) if row else None
    if blog is None:
        raise HTTPException(404, "Блог не найден")
    parent = (pair_lock(session, blog.pair_id, active=False) if blog.pair_id
              else user_lock(session, blog.owner_user_id))
    async with parent:
        lock = _locks.setdefault(blog_id, asyncio.Lock())
        async with lock:
            current = (await session.execute(media_identity_query(blog_id).with_for_update())).one_or_none()
            if current is None:
                raise HTTPException(404, "Блог не найден")
            yield BlogMediaIdentity(*current)


@asynccontextmanager
async def blog_write_lock(session: AsyncSession, blog_id: str):
    """API owner -> blog lock order also serializes pair erasure and quotas."""
    async with blog_media_write_lock(session, blog_id):
        current = await session.get(PublicBlog, blog_id, populate_existing=True)
        if current is None:
            raise HTTPException(404, "Блог не найден")
        yield current


def revision_matches(expected: int, actual: int) -> None:
    if expected != actual:
        raise HTTPException(409, "Версия изменилась — обнови экран перед действием")


async def require_ready_blog_media(session: AsyncSession, blog_id: str,
                                   media_id: str) -> BlogMediaAsset:
    asset = await session.get(BlogMediaAsset, media_id, populate_existing=True)
    if asset is None or asset.blog_id != blog_id:
        raise HTTPException(404, "Видео не найдено в этом блоге")
    if (asset.status != "ready" or not asset.sha256 or not _SHA.fullmatch(asset.sha256)
            or not asset.output_key or not asset.thumbnail_key or not asset.duration_ms
            or not asset.width or not asset.height or not asset.output_bytes):
        raise HTTPException(409, "Видео ещё не готово к публикации")
    return asset


def media_out(asset: BlogMediaAsset) -> dict:
    return {"id": asset.id, "status": asset.status,
            "url": f"/blogs/media/{asset.id}/video",
            "thumbnail_url": f"/blogs/media/{asset.id}/thumbnail",
            "duration_seconds": math.ceil((asset.duration_ms or 0) / 1000),
            "width": asset.width, "height": asset.height}


async def approved_owners(session: AsyncSession, blog: PublicBlog, post: PublicPost) -> set[str]:
    return set((await session.execute(select(PublicPostApproval.user_id).where(
        PublicPostApproval.post_id == post.id,
        PublicPostApproval.revision == post.revision,
        PublicPostApproval.blog_revision == blog.revision,
        PublicPostApproval.media_sha256 == post.media_sha256,
        PublicPostApproval.consent_version == POST_CONSENT_VERSION))).scalars())


async def post_is_public(session: AsyncSession, blog: PublicBlog, post: PublicPost,
                         asset: BlogMediaAsset | None = None) -> bool:
    if (post.status != "published" or post.reviewed_revision != post.revision
            or post.reviewed_media_sha256 != post.media_sha256 or post.published_at is None
            or post.reviewed_at is None or not await profile_is_public(session, blog)):
        return False
    asset = asset or await session.get(BlogMediaAsset, post.media_id, populate_existing=True)
    if (asset is None or asset.blog_id != blog.id or asset.status != "ready"
            or asset.sha256 != post.media_sha256):
        return False
    return await approved_owners(session, blog, post) == await owner_ids(session, blog)


async def can_read_blog_media(session: AsyncSession, user_id: str, asset: BlogMediaAsset) -> bool:
    if not enabled() or asset.status != "ready":
        return False
    blog = await session.get(PublicBlog, asset.blog_id, populate_existing=True)
    if blog is None:
        return False
    if user_id in await owner_ids(session, blog):
        return True
    posts = (await session.execute(select(PublicPost).where(
        PublicPost.media_id == asset.id, PublicPost.status == "published"))).scalars()
    for post in posts:
        if await post_is_public(session, blog, post, asset):
            return True
    return False


async def blog_out(session: AsyncSession, blog: PublicBlog, user_id: str) -> dict:
    owners = await owner_ids(session, blog)
    accepted = await consented_owners(session, blog)
    paused = await is_paused(session, blog)
    active = blog.status == "active" and bool(owners) and owners == accepted
    return {"id": blog.id, "author_id": author_id(blog.id), "kind": blog.kind,
            "name": blog.name, "bio": blog.bio, "status": blog.status,
            "revision": blog.revision, "my_consent": user_id in accepted,
            "partner_consent": bool((owners - {user_id}) & accepted) if blog.kind == "couple" else None,
            "can_edit": user_id in owners and not paused,
            "can_publish": active and not paused, "paused": paused,
            "consent_version": BLOG_CONSENT_VERSION,
            "post_consent_version": POST_CONSENT_VERSION,
            "created_at": aware(blog.created_at), "updated_at": aware(blog.updated_at)}


async def post_out(session: AsyncSession, blog: PublicBlog, post: PublicPost, user_id: str) -> dict:
    approved = await approved_owners(session, blog, post)
    owners = await owner_ids(session, blog)
    active = await profile_is_public(session, blog)
    paused = await is_paused(session, blog)
    asset = await session.get(BlogMediaAsset, post.media_id)
    can_write = (user_id in owners and user_id in await consented_owners(session, blog)
                 and blog.status != "withdrawn" and not paused)
    return {"id": post.id, "content_id": content_id(post.id), "blog_id": blog.id,
            "title": post.title, "description": post.description,
            "topics": json.loads(post.topics), "language": post.language,
            "media_id": post.media_id, "media": media_out(asset) if asset else None,
            "status": post.status, "revision": post.revision,
            "my_approved": user_id in approved,
            "partner_approved": bool((owners - {user_id}) & approved) if blog.kind == "couple" else None,
            "created_at": aware(post.created_at), "updated_at": aware(post.updated_at),
            "published_at": aware(post.published_at) if post.published_at else None,
            "review_note": post.review_note, "consent_version": POST_CONSENT_VERSION,
            "can_submit": can_write and active and post.status in {"draft", "pending_partner", "rejected", "withdrawn"},
            "can_edit": can_write, "can_withdraw": user_id in owners and post.status != "withdrawn"}


async def withdraw_posts(session: AsyncSession, blog_id: str, *, identity_changed: bool = False) -> None:
    posts = (await session.execute(select(PublicPost).where(PublicPost.blog_id == blog_id)
                                   .execution_options(populate_existing=True))).scalars().all()
    for post in posts:
        if identity_changed or post.status != "withdrawn":
            post.status, post.revision, post.updated_at = "withdrawn", post.revision + 1, utcnow()
        post.reviewed_revision = post.reviewed_media_sha256 = post.reviewed_at = None
    if posts:
        await session.execute(delete(PublicPostApproval).where(
            PublicPostApproval.post_id.in_([post.id for post in posts])))


async def erase_blogs(session: AsyncSession, *, pair_id: str | None = None,
                      user_id: str | None = None) -> None:
    """Revoke database access in the erasure transaction; media GC is separate."""
    condition = PublicBlog.pair_id == pair_id if pair_id else PublicBlog.owner_user_id == user_id
    blogs = (await session.execute(select(PublicBlog).where(condition))).scalars().all()
    if not blogs:
        return
    blog_ids = [blog.id for blog in blogs]
    post_ids = select(PublicPost.id).where(PublicPost.blog_id.in_(blog_ids))
    await session.execute(delete(PublicPostReport).where(PublicPostReport.post_id.in_(post_ids)))
    await session.execute(delete(PublicPostApproval).where(PublicPostApproval.post_id.in_(post_ids)))
    assets = list((await session.execute(select(BlogMediaAsset.id).where(
        BlogMediaAsset.blog_id.in_(blog_ids)))).scalars())
    if assets:
        from .blog_media import schedule_blog_media_deletion
        await schedule_blog_media_deletion(session, assets)
    await session.execute(delete(PublicPost).where(PublicPost.blog_id.in_(blog_ids)))
    await session.execute(delete(BlogMediaAsset).where(BlogMediaAsset.blog_id.in_(blog_ids)))
    await session.execute(delete(PublicBlogConsent).where(PublicBlogConsent.blog_id.in_(blog_ids)))
    await session.execute(delete(PublicBlog).where(PublicBlog.id.in_(blog_ids)))


class BlogAuthor(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    bio: str
    kind: Literal["individual", "couple"]
    url: None = None


class HostedMedia(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    status: Literal["ready"]
    url: str
    thumbnail_url: str
    duration_seconds: int = Field(ge=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class BlogPublication(content.Publication):
    """Runtime SQL projection; the editorial JSON schema stays unchanged."""
    kind: Literal["video"] = "video"
    video_id: None = None
    media: HostedMedia

    @model_validator(mode="after")
    def coherent(self):
        # Override the editorial-only legacy video reference validator.
        if len(self.topics) != len(set(self.topics)):
            raise ValueError("Repeated topics")
        if self.published_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise ValueError("Publication timestamps need a timezone")
        if self.updated_at < self.published_at or any(
                value is not None for value in (self.series_id, self.series_title, self.series_order)):
            raise ValueError("Invalid public-blog publication")
        return self


@dataclass(frozen=True)
class ReaderSnapshot:
    version: int
    topics: list
    authors: list
    items: list
    revision: str


async def with_public_blogs(session: AsyncSession, snapshot: content.Catalogue,
                           *, hosted_video: bool = False) -> content.Catalogue | ReaderSnapshot:
    if not enabled():
        return snapshot
    authors, items, revision_parts = list(snapshot.authors), list(snapshot.items), []
    # The pilot is bounded, but empty profiles must not cause one query chain
    # each. At most six bounded queries build an entirely public graph.
    # Unreviewed/empty profiles remain owner drafts. They cannot fill a public
    # catalogue or make ordinary reader requests scan arbitrary registrations.
    reviewed_posts = select(PublicPost.id).where(
        PublicPost.blog_id == PublicBlog.id, PublicPost.status == "published",
        PublicPost.reviewed_revision == PublicPost.revision,
        PublicPost.reviewed_media_sha256 == PublicPost.media_sha256).exists()
    rows = (await session.execute(select(PublicBlog).where(PublicBlog.status == "active", reviewed_posts)
                                  .order_by(PublicBlog.id).limit(MAX_PUBLIC_BLOGS + 1))).scalars().all()
    if len(rows) > MAX_PUBLIC_BLOGS:
        raise HTTPException(503, "Подборка блогов временно недоступна: требуется обновление каталога")
    owners = {blog.id: set() for blog in rows}
    individual_ids = [blog.owner_user_id for blog in rows if blog.owner_user_id]
    existing_users = set((await session.execute(select(User.id).where(User.id.in_(individual_ids)))).scalars()) if individual_ids else set()
    pair_ids = [blog.pair_id for blog in rows if blog.pair_id]
    members: dict[str, set[str]] = {}
    if pair_ids:
        for pair_id, user_id in (await session.execute(select(PairMember.pair_id, PairMember.user_id)
                .join(Pair, Pair.id == PairMember.pair_id).where(
                    PairMember.pair_id.in_(pair_ids), Pair.status == "active"))).all():
            members.setdefault(pair_id, set()).add(user_id)
    blog_ids = [blog.id for blog in rows]
    consents = (await session.execute(select(PublicBlogConsent).where(
        PublicBlogConsent.blog_id.in_(blog_ids), PublicBlogConsent.accepted.is_(True),
        PublicBlogConsent.consent_version == BLOG_CONSENT_VERSION))).scalars().all() if blog_ids else []
    consent_map: dict[tuple[str, int], set[str]] = {}
    for consent in consents:
        consent_map.setdefault((consent.blog_id, consent.blog_revision), set()).add(consent.user_id)
    public = {}
    for blog in rows:
        if blog.kind == "individual" and blog.owner_user_id in existing_users:
            owners[blog.id] = {blog.owner_user_id}
        elif blog.kind == "couple" and len(members.get(blog.pair_id, set())) == 2:
            owners[blog.id] = members[blog.pair_id]
        if owners[blog.id] and owners[blog.id] == consent_map.get((blog.id, blog.revision), set()):
            public[blog.id] = blog
            authors.append(BlogAuthor(id=author_id(blog.id), kind=blog.kind, name=blog.name, bio=blog.bio))
            revision_parts.append((blog.id, blog.revision, blog.name, blog.bio))
    if hosted_video and public:
        posts = (await session.execute(select(PublicPost, BlogMediaAsset).join(
            BlogMediaAsset, BlogMediaAsset.id == PublicPost.media_id).where(
            PublicPost.blog_id.in_(public), PublicPost.status == "published",
            PublicPost.reviewed_revision == PublicPost.revision,
            PublicPost.reviewed_media_sha256 == PublicPost.media_sha256,
            PublicPost.published_at.is_not(None), PublicPost.reviewed_at.is_not(None),
            BlogMediaAsset.blog_id == PublicPost.blog_id, BlogMediaAsset.status == "ready",
            BlogMediaAsset.sha256 == PublicPost.media_sha256).order_by(PublicPost.id)
            .limit(MAX_PUBLISHED_POSTS + 1))).all()
        if len(posts) > MAX_PUBLISHED_POSTS:
            raise HTTPException(503, "Подборка блогов временно недоступна: требуется обновление каталога")
        post_ids = [post.id for post, _ in posts]
        approvals = (await session.execute(select(PublicPostApproval).where(
            PublicPostApproval.post_id.in_(post_ids),
            PublicPostApproval.consent_version == POST_CONSENT_VERSION))).scalars().all() if post_ids else []
        approval_map: dict[tuple, set[str]] = {}
        for approval in approvals:
            key = (approval.post_id, approval.revision, approval.blog_revision, approval.media_sha256)
            approval_map.setdefault(key, set()).add(approval.user_id)
        for post, asset in posts:
            blog = public[post.blog_id]
            key = (post.id, post.revision, blog.revision, post.media_sha256)
            if owners[blog.id] != approval_map.get(key, set()):
                continue
            topics = json.loads(post.topics)
            if not set(topics).issubset({topic.id for topic in snapshot.topics}):
                continue
            published_at = aware(post.published_at)
            items.append(BlogPublication(
                id=content_id(post.id), title=post.title,
                summary=post.description[:799] + "…" if len(post.description) > 800 else post.description,
                author_id=author_id(blog.id), topics=topics, language=post.language,
                blocks=[content.TextBlock(type="paragraph", text=post.description)],
                published_at=published_at, updated_at=max(published_at, aware(post.updated_at)),
                reviewed_at=aware(post.reviewed_at).date(), active=True,
                reading_minutes=min(60, max(1, math.ceil(asset.duration_ms / 60000))),
                media=HostedMedia(**media_out(asset))))
            revision_parts.append((post.id, post.revision, post.reviewed_revision,
                                   post.media_sha256, published_at.isoformat()))
    revision = request_hash({"editorial": snapshot.revision, "public": revision_parts,
                             "hosted_video": hosted_video, "version": "public-blogs-v1"})
    return ReaderSnapshot(snapshot.version, snapshot.topics, authors, items, revision)
