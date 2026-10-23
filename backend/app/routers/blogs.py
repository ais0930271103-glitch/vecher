"""Owner-only authoring; public reads remain in the existing content API."""
import json
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import blogs, content_catalogue, recommendations
from ..db import get_session
from ..deps import consented_user
from ..models import (Pair, PairMember, PairPause, PublicBlog, PublicBlogConsent,
                      PublicPost, PublicPostApproval, User, utcnow)
from ..pair_lock import pair_lock
from ..realtime import enqueue_invalidation
from ..user_lock import user_lock


def no_store(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"


router = APIRouter(prefix="/blogs", tags=["public-blogs"],
                   dependencies=[Depends(no_store), Depends(blogs.require_enabled)])
ClientId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9-]{8,64}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
ExplicitTrue = Annotated[bool, Field(strict=True)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileFields(Input):
    name: str = Field(min_length=1, max_length=120)
    bio: str = Field(default="", max_length=1600)
    _name = field_validator("name")(content_catalogue.plain)

    @field_validator("bio")
    @classmethod
    def plain_bio(cls, value):
        return content_catalogue.plain(value) if value else value


class BlogCreate(ProfileFields):
    client_id: ClientId
    kind: Literal["individual", "couple"]
    public_consent: ExplicitTrue
    consent_version: Literal["public-blog-v1"]

    @field_validator("public_consent")
    @classmethod
    def accepted(cls, value):
        if value is not True:
            raise ValueError("Explicit public consent is required")
        return value


class BlogEdit(Input):
    revision: Revision
    name: str | None = Field(default=None, min_length=1, max_length=120)
    bio: str | None = Field(default=None, max_length=1600)

    @model_validator(mode="after")
    def explicit_changes(self):
        fields = self.model_fields_set - {"revision"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("Provide a public profile change")
        for field in fields:
            value = getattr(self, field)
            if value:
                content_catalogue.plain(value)
        return self


class RevisionIn(Input):
    revision: Revision


class ConsentIn(RevisionIn):
    accepted: Annotated[bool, Field(strict=True)]
    consent_version: Literal["public-blog-v1"]


class PostCreate(Input):
    client_id: ClientId
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    topics: list[content_catalogue.Id] = Field(min_length=1, max_length=6)
    language: Literal["ru", "en", "none"]
    media_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    _text = field_validator("title", "description")(content_catalogue.plain)


class PostEdit(Input):
    revision: Revision
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    topics: list[content_catalogue.Id] | None = Field(default=None, min_length=1, max_length=6)
    language: Literal["ru", "en", "none"] | None = None
    media_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")

    @model_validator(mode="after")
    def explicit_changes(self):
        fields = self.model_fields_set - {"revision"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("Provide one or more explicit post changes")
        for field in fields & {"title", "description"}:
            content_catalogue.plain(getattr(self, field))
        return self


class SubmitIn(RevisionIn):
    public_consent: ExplicitTrue
    consent_version: Literal["public-post-v1"] = "public-post-v1"

    @field_validator("public_consent")
    @classmethod
    def accepted(cls, value):
        if value is not True:
            raise ValueError("Explicit approval of this publication is required")
        return value


def selected_topics(topics: list[str]) -> list[str]:
    try:
        snapshot = recommendations.with_reviewed_videos(content_catalogue.catalogue())
    except (OSError, ValueError, UnicodeError):
        raise HTTPException(503, "Список тем временно недоступен") from None
    valid = {topic.id for topic in snapshot.topics}
    if len(topics) != len(set(topics)) or not set(topics).issubset(valid):
        raise HTTPException(422, "Выбери темы из списка без повторов")
    return sorted(topics)


async def changed(session: AsyncSession, blog: PublicBlog):
    owners = await blogs.owner_ids(session, blog)
    await enqueue_invalidation(session, sorted(owners), ["solo", "us"] if blog.pair_id else ["solo"],
                               pair_id=blog.pair_id)


async def own_post(session: AsyncSession, blog: PublicBlog, post_id: str) -> PublicPost:
    post = await session.get(PublicPost, post_id, populate_existing=True)
    if post is None or post.blog_id != blog.id:
        raise HTTPException(404, "Публикация не найдена")
    return post


@asynccontextmanager
async def creation_lock(session, user_id, pair_id):
    # The create nonce belongs to the creator across both author kinds.
    if pair_id:
        async with pair_lock(session, pair_id), user_lock(session, user_id):
            yield
    else:
        async with user_lock(session, user_id):
            yield


@router.get("/mine")
async def mine(user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    pair_ids = select(PairMember.pair_id).where(PairMember.user_id == user.id)
    rows = (await session.execute(select(PublicBlog).where(or_(
        PublicBlog.owner_user_id == user.id, PublicBlog.pair_id.in_(pair_ids)))
        .order_by(PublicBlog.created_at, PublicBlog.id))).scalars().all()
    return {"items": [await blogs.blog_out(session, blog, user.id) for blog in rows],
            "consent_version": blogs.BLOG_CONSENT_VERSION,
            "post_consent_version": blogs.POST_CONSENT_VERSION}


@router.post("", status_code=201)
async def create(body: BlogCreate, user: User = Depends(consented_user),
                 session: AsyncSession = Depends(get_session)):
    pair_id = None
    if body.kind == "couple":
        pair_id = await session.scalar(select(PairMember.pair_id).join(Pair, Pair.id == PairMember.pair_id)
            .where(PairMember.user_id == user.id, Pair.status == "active"))
        if pair_id is None:
            raise HTTPException(409, "Для общего блога сначала соединись с партнёром")
    async with creation_lock(session, user.id, pair_id):
        if pair_id and await session.scalar(select(PairPause.id).where(PairPause.pair_id == pair_id)):
            raise HTTPException(409, "Пара на паузе — общий блог пока недоступен")
        condition = PublicBlog.pair_id == pair_id if pair_id else PublicBlog.owner_user_id == user.id
        existing = await session.scalar(select(PublicBlog).where(condition))
        fingerprint = blogs.request_hash(body.model_dump())
        if existing:
            if (existing.created_by_user_id == user.id and existing.client_id == body.client_id
                    and existing.creation_hash == fingerprint):
                return await blogs.blog_out(session, existing, user.id)
            raise HTTPException(409, "У тебя уже есть такой блог — открой его из списка")
        reused = await session.scalar(select(PublicBlog.id).where(
            PublicBlog.created_by_user_id == user.id, PublicBlog.client_id == body.client_id))
        if reused:
            raise HTTPException(409, "Ключ запроса уже использован для другого блога")
        blog = PublicBlog(kind=body.kind, owner_user_id=user.id if pair_id is None else None,
                          pair_id=pair_id, created_by_user_id=user.id, client_id=body.client_id,
                          creation_hash=fingerprint, name=body.name, bio=body.bio,
                          status="active" if pair_id is None else "pending_consent", revision=1)
        session.add(blog)
        await session.flush()
        session.add(PublicBlogConsent(blog_id=blog.id, user_id=user.id, blog_revision=1,
                                     consent_version=blogs.BLOG_CONSENT_VERSION, accepted=True))
        await changed(session, blog)
        await session.commit()
        return await blogs.blog_out(session, blog, user.id)


@router.get("/{blog_id}")
async def detail(blog_id: str, user: User = Depends(consented_user),
                 session: AsyncSession = Depends(get_session)):
    blog = await blogs.require_blog_owner(session, user.id, blog_id)
    return await blogs.blog_out(session, blog, user.id)


@router.patch("/{blog_id}")
async def edit_profile(blog_id: str, body: BlogEdit, user: User = Depends(consented_user),
                       session: AsyncSession = Depends(get_session)):
    await blogs.require_blog_owner(session, user.id, blog_id)
    async with blogs.blog_write_lock(session, blog_id):
        blog = await blogs.require_blog_owner(session, user.id, blog_id)
        if await blogs.is_paused(session, blog):
            raise HTTPException(409, "Пара на паузе — изменение общего блога пока недоступно")
        blogs.revision_matches(body.revision, blog.revision)
        changes = body.model_dump(exclude_unset=True, exclude={"revision"})
        if any(getattr(blog, field) != value for field, value in changes.items()):
            for field, value in changes.items():
                setattr(blog, field, value)
            blog.status, blog.revision, blog.updated_at = "pending_consent", blog.revision + 1, utcnow()
            await blogs.withdraw_posts(session, blog.id, identity_changed=True)
            await changed(session, blog)
            await session.commit()
        return await blogs.blog_out(session, blog, user.id)


async def withdraw_profile(session: AsyncSession, blog: PublicBlog, user_id: str,
                           expected_revision: int):
    if blog.status == "withdrawn" and expected_revision in {blog.revision, blog.revision - 1}:
        return
    blogs.revision_matches(expected_revision, blog.revision)
    blog.status, blog.revision, blog.updated_at = "withdrawn", blog.revision + 1, utcnow()
    row = await session.get(PublicBlogConsent, (blog.id, user_id))
    if row is None:
        row = PublicBlogConsent(blog_id=blog.id, user_id=user_id)
        session.add(row)
    row.blog_revision, row.consent_version, row.accepted = blog.revision, blogs.BLOG_CONSENT_VERSION, False
    row.updated_at = utcnow()
    await blogs.withdraw_posts(session, blog.id, identity_changed=True)
    await changed(session, blog)


@router.post("/{blog_id}/withdraw")
async def withdraw_blog(blog_id: str, body: RevisionIn, user: User = Depends(consented_user),
                        session: AsyncSession = Depends(get_session)):
    await blogs.require_blog_owner(session, user.id, blog_id)
    async with blogs.blog_write_lock(session, blog_id):
        blog = await blogs.require_blog_owner(session, user.id, blog_id)
        await withdraw_profile(session, blog, user.id, body.revision)
        await session.commit()
        return await blogs.blog_out(session, blog, user.id)


@router.post("/{blog_id}/consent")
async def consent(blog_id: str, body: ConsentIn, user: User = Depends(consented_user),
                  session: AsyncSession = Depends(get_session)):
    await blogs.require_blog_owner(session, user.id, blog_id)
    async with blogs.blog_write_lock(session, blog_id):
        blog = await blogs.require_blog_owner(session, user.id, blog_id)
        if not body.accepted:
            await withdraw_profile(session, blog, user.id, body.revision)
        else:
            blogs.revision_matches(body.revision, blog.revision)
            if await blogs.is_paused(session, blog):
                raise HTTPException(409, "Пара на паузе — подтверждение общего блога пока недоступно")
            row = await session.get(PublicBlogConsent, (blog.id, user.id))
            if row is None:
                row = PublicBlogConsent(blog_id=blog.id, user_id=user.id)
                session.add(row)
            row.blog_revision, row.consent_version, row.accepted = blog.revision, body.consent_version, True
            row.updated_at = utcnow()
            await session.flush()
            owners = await blogs.owner_ids(session, blog)
            blog.status = "active" if owners == await blogs.consented_owners(session, blog) else "pending_consent"
            blog.updated_at = utcnow()
            await changed(session, blog)
        await session.commit()
        return await blogs.blog_out(session, blog, user.id)


@router.get("/{blog_id}/posts")
async def posts(blog_id: str, user: User = Depends(consented_user),
                session: AsyncSession = Depends(get_session)):
    blog = await blogs.require_blog_owner(session, user.id, blog_id)
    rows = (await session.execute(select(PublicPost).where(PublicPost.blog_id == blog.id)
                                   .order_by(PublicPost.created_at.desc(), PublicPost.id))).scalars().all()
    return {"items": [await blogs.post_out(session, blog, post, user.id) for post in rows],
            "limit": blogs.MAX_POSTS_PER_BLOG}


@router.get("/{blog_id}/posts/{post_id}")
async def post_detail(blog_id: str, post_id: str, user: User = Depends(consented_user),
                      session: AsyncSession = Depends(get_session)):
    blog = await blogs.require_blog_owner(session, user.id, blog_id)
    return await blogs.post_out(session, blog, await own_post(session, blog, post_id), user.id)


@router.post("/{blog_id}/posts", status_code=201)
async def create_post(blog_id: str, body: PostCreate, user: User = Depends(consented_user),
                      session: AsyncSession = Depends(get_session)):
    await blogs.require_blog_owner(session, user.id, blog_id, write=True)
    topics = selected_topics(body.topics)
    async with blogs.blog_write_lock(session, blog_id):
        blog = await blogs.require_blog_owner(session, user.id, blog_id, write=True)
        fingerprint = blogs.request_hash({**body.model_dump(), "topics": topics})
        prior = await session.scalar(select(PublicPost).where(
            PublicPost.blog_id == blog.id, PublicPost.client_id == body.client_id))
        if prior:
            if prior.created_by_user_id != user.id or prior.creation_hash != fingerprint:
                raise HTTPException(409, "Ключ запроса уже использован для другой публикации")
            return await blogs.post_out(session, blog, prior, user.id)
        if await session.scalar(select(func.count()).select_from(PublicPost).where(
                PublicPost.blog_id == blog.id)) >= blogs.MAX_POSTS_PER_BLOG:
            raise HTTPException(409, "Достигнут лимит публикаций первого выпуска блогов")
        asset = await blogs.require_ready_blog_media(session, blog.id, body.media_id)
        post = PublicPost(blog_id=blog.id, created_by_user_id=user.id, client_id=body.client_id,
                          creation_hash=fingerprint, title=body.title, description=body.description,
                          topics=json.dumps(topics), language=body.language, media_id=asset.id,
                          media_sha256=asset.sha256, status="draft", revision=1)
        session.add(post)
        await session.flush()
        await changed(session, blog)
        await session.commit()
        return await blogs.post_out(session, blog, post, user.id)


@router.patch("/{blog_id}/posts/{post_id}")
async def edit_post(blog_id: str, post_id: str, body: PostEdit, user: User = Depends(consented_user),
                    session: AsyncSession = Depends(get_session)):
    await blogs.require_blog_owner(session, user.id, blog_id, write=True)
    changes = body.model_dump(exclude_unset=True, exclude={"revision"})
    if "topics" in changes:
        changes["topics"] = json.dumps(selected_topics(changes["topics"]))
    async with blogs.blog_write_lock(session, blog_id):
        blog = await blogs.require_blog_owner(session, user.id, blog_id, write=True)
        post = await own_post(session, blog, post_id)
        blogs.revision_matches(body.revision, post.revision)
        if "media_id" in changes:
            asset = await blogs.require_ready_blog_media(session, blog.id, changes["media_id"])
            changes["media_sha256"] = asset.sha256
        if any(getattr(post, field) != value for field, value in changes.items()):
            for field, value in changes.items():
                setattr(post, field, value)
            post.revision, post.status, post.updated_at = post.revision + 1, "draft", utcnow()
            post.reviewed_revision = post.reviewed_media_sha256 = post.reviewed_at = post.review_note = None
            await session.execute(delete(PublicPostApproval).where(PublicPostApproval.post_id == post.id))
            await changed(session, blog)
            await session.commit()
        return await blogs.post_out(session, blog, post, user.id)


@router.post("/{blog_id}/posts/{post_id}/submit")
async def submit_post(blog_id: str, post_id: str, body: SubmitIn, user: User = Depends(consented_user),
                      session: AsyncSession = Depends(get_session)):
    await blogs.require_blog_owner(session, user.id, blog_id, write=True)
    async with blogs.blog_write_lock(session, blog_id):
        blog = await blogs.require_blog_owner(session, user.id, blog_id, write=True)
        post = await own_post(session, blog, post_id)
        blogs.revision_matches(body.revision, post.revision)
        if not await blogs.profile_is_public(session, blog):
            raise HTTPException(409, "Сначала оба подтвердите публичную версию блога")
        asset = await blogs.require_ready_blog_media(session, blog.id, post.media_id)
        if asset.sha256 != post.media_sha256:
            raise HTTPException(409, "Видео изменилось — обнови публикацию")
        if user.id in await blogs.approved_owners(session, blog, post):
            return await blogs.post_out(session, blog, post, user.id)
        row = await session.get(PublicPostApproval, (post.id, user.id))
        if row is None:
            row = PublicPostApproval(post_id=post.id, user_id=user.id)
            session.add(row)
        row.revision, row.blog_revision = post.revision, blog.revision
        row.media_sha256, row.consent_version, row.approved_at = post.media_sha256, body.consent_version, utcnow()
        await session.flush()
        complete = await blogs.approved_owners(session, blog, post) == await blogs.owner_ids(session, blog)
        post.status = "pending_review" if complete else "pending_partner"
        post.review_note, post.updated_at = None, utcnow()
        await changed(session, blog)
        await session.commit()
        return await blogs.post_out(session, blog, post, user.id)


@router.post("/{blog_id}/posts/{post_id}/withdraw")
async def withdraw_post(blog_id: str, post_id: str, body: RevisionIn,
                        user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    await blogs.require_blog_owner(session, user.id, blog_id)
    async with blogs.blog_write_lock(session, blog_id):
        blog = await blogs.require_blog_owner(session, user.id, blog_id)
        post = await own_post(session, blog, post_id)
        if post.status == "withdrawn" and body.revision in {post.revision, post.revision - 1}:
            return await blogs.post_out(session, blog, post, user.id)
        blogs.revision_matches(body.revision, post.revision)
        post.status, post.revision, post.updated_at = "withdrawn", post.revision + 1, utcnow()
        post.reviewed_revision = post.reviewed_media_sha256 = post.reviewed_at = None
        await session.execute(delete(PublicPostApproval).where(PublicPostApproval.post_id == post.id))
        await changed(session, blog)
        await session.commit()
        return await blogs.post_out(session, blog, post, user.id)
