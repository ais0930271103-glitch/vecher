"""Local operator review of exact public-post revisions; no HTTP admin secret.

python -m app.blog_review list
python -m app.blog_review show POST_ID
python -m app.blog_review approve POST_ID --revision 1 --blog-revision 1 --sha256 SHA256
python -m app.blog_review reject POST_ID --revision 1 --blog-revision 1 --sha256 SHA256 --note 'Reason'
python -m app.blog_review withdraw POST_ID --revision 1 --blog-revision 1 --sha256 SHA256
"""
import argparse
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from weakref import WeakKeyDictionary

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text

from . import blogs
from .blog_media import verify_ready_asset
from .config import get_settings
from .db import SessionLocal, engine
from .migrate import check
from .models import BlogMediaAsset, PublicBlog, PublicPost, PublicPostApproval, PublicPostReport, utcnow
from .routers.blogs import changed

_locks: WeakKeyDictionary = WeakKeyDictionary()
_SHA = re.compile(r"^[0-9a-f]{64}$")


@asynccontextmanager
async def catalogue_lock(session):
    lock = _locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        if session.bind.dialect.name == "postgresql":
            await session.execute(text("SELECT pg_advisory_xact_lock(726834902137)"))
        yield


async def review_post(session, post_id: str, *, action: str, revision: int,
                      blog_revision: int, sha256: str, note: str | None = None) -> dict:
    """The operator's pins are CAS inputs, never guessed from a changed row."""
    if action not in {"approve", "reject", "withdraw"} or not _SHA.fullmatch(sha256):
        raise ValueError("Invalid review action or media fingerprint")
    if note is not None:
        if not 1 <= len(note) <= 500:
            raise ValueError("Review note must contain 1 to 500 characters")
        from .content_catalogue import plain
        plain(note)
    if action == "reject" and note is None:
        raise ValueError("A rejection needs a reason for the author")
    post_id = post_id.removeprefix("post-")
    initial = await session.get(PublicPost, post_id)
    if initial is None:
        raise HTTPException(404, "Публикация не найдена")
    async with catalogue_lock(session), blogs.blog_write_lock(session, initial.blog_id):
        post = await session.get(PublicPost, post_id, populate_existing=True)
        blog = await session.get(PublicBlog, initial.blog_id, populate_existing=True)
        if post is None or blog is None:
            raise HTTPException(404, "Публикация не найдена")
        blogs.revision_matches(revision, post.revision)
        blogs.revision_matches(blog_revision, blog.revision)
        if post.media_sha256 != sha256:
            raise HTTPException(409, "Проверенное видео не совпало с текущей публикацией")
        if action == "approve":
            if post.status not in {"pending_review", "published"}:
                raise HTTPException(409, "Публикация ещё не подана обоими владельцами")
            if not await blogs.profile_is_public(session, blog) or await blogs.is_paused(session, blog):
                raise HTTPException(409, "Публичные согласия блога изменились или пара на паузе")
            owners = await blogs.owner_ids(session, blog)
            if not owners or owners != await blogs.approved_owners(session, blog, post):
                raise HTTPException(409, "Нужны согласия всех владельцев на эту версию")
            asset = await blogs.require_ready_blog_media(session, blog.id, post.media_id)
            if asset.sha256 != sha256:
                raise HTTPException(409, "Видео изменилось после согласия владельцев")
            await verify_ready_asset(asset)
            if post.status != "published":
                visible = select(PublicPost).join(PublicBlog, PublicBlog.id == PublicPost.blog_id).where(
                    PublicPost.status == "published", PublicBlog.status == "active")
                total = await session.scalar(select(func.count()).select_from(visible.subquery()))
                authors = await session.scalar(select(func.count(func.distinct(PublicPost.blog_id)))
                    .join(PublicBlog, PublicBlog.id == PublicPost.blog_id)
                    .where(PublicPost.status == "published", PublicBlog.status == "active"))
                has_published = await session.scalar(select(PublicPost.id).where(
                    PublicPost.blog_id == blog.id, PublicPost.status == "published").limit(1))
                if total >= blogs.MAX_PUBLISHED_POSTS or (not has_published and authors >= blogs.MAX_PUBLIC_BLOGS):
                    raise HTTPException(409, "Достигнут размер проверенного каталога первого выпуска")
                now = utcnow()
                post.status, post.published_at, post.updated_at = "published", now, now
                post.reviewed_at, post.reviewed_revision, post.reviewed_media_sha256 = now, post.revision, sha256
                post.review_note = None
                await changed(session, blog)
        else:
            if action == "reject" and post.status != "pending_review":
                raise HTTPException(409, "Эта версия не находится на проверке")
            post.status = "rejected" if action == "reject" else "withdrawn"
            post.revision, post.updated_at = post.revision + 1, utcnow()
            post.reviewed_revision = post.reviewed_media_sha256 = post.reviewed_at = None
            post.review_note = note
            await session.execute(delete(PublicPostApproval).where(PublicPostApproval.post_id == post.id))
            await changed(session, blog)
        await session.commit()
        return {"id": post.id, "content_id": blogs.content_id(post.id), "status": post.status,
                "revision": post.revision, "sha256": post.media_sha256}


async def show_post(session, post_id: str) -> dict:
    post = await session.get(PublicPost, post_id.removeprefix("post-"))
    if post is None:
        raise HTTPException(404, "Публикация не найдена")
    blog = await session.get(PublicBlog, post.blog_id)
    asset = await session.get(BlogMediaAsset, post.media_id)
    reports = (await session.execute(select(PublicPostReport.reason, func.count())
        .where(PublicPostReport.post_id == post.id).group_by(PublicPostReport.reason))).all()
    preview = None
    if asset and asset.output_key:
        from .blog_media import _filename
        preview = str(Path(os.path.abspath(get_settings().blog_media_dir)) / _filename(asset.output_key, "video"))
    return {"id": post.id, "revision": post.revision, "status": post.status,
            "author": {"id": blogs.author_id(blog.id), "kind": blog.kind, "name": blog.name,
                       "bio": blog.bio, "revision": blog.revision},
            "title": post.title, "description": post.description, "topics": json.loads(post.topics),
            "language": post.language, "media": blogs.media_out(asset) if asset else None,
            "sha256": post.media_sha256, "local_video_path": preview,
            "owner_approvals": len(await blogs.approved_owners(session, blog, post)),
            "reports": {reason: count for reason, count in reports}}


async def run(args):
    await check(engine)
    async with SessionLocal() as session:
        if args.action == "list":
            statement = select(PublicPost).order_by(PublicPost.updated_at, PublicPost.id)
            if args.status == "reported":
                statement = statement.where(select(PublicPostReport.id).where(
                    PublicPostReport.post_id == PublicPost.id).exists())
            else:
                statement = statement.where(PublicPost.status == args.status)
            rows = (await session.execute(statement.limit(args.limit))).scalars().all()
            result = {"items": [{"id": post.id, "revision": post.revision, "title": post.title,
                                  "status": post.status, "sha256": post.media_sha256} for post in rows]}
        elif args.action == "show":
            result = await show_post(session, args.post_id)
        else:
            result = await review_post(session, args.post_id, action=args.action, revision=args.revision,
                                       blog_revision=args.blog_revision, sha256=args.sha256, note=args.note)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", default="pending_review", choices=["pending_review", "published", "reported",
        "draft", "pending_partner", "rejected", "withdrawn"])
    listing.add_argument("--limit", type=int, default=50, choices=range(1, 101))
    commands.add_parser("show").add_argument("post_id")
    for action in ("approve", "reject", "withdraw"):
        command = commands.add_parser(action)
        command.add_argument("post_id")
        command.add_argument("--revision", type=int, required=True)
        command.add_argument("--blog-revision", type=int, required=True)
        command.add_argument("--sha256", required=True)
        command.add_argument("--note", required=action == "reject")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, HTTPException) as error:
        message = error.detail if isinstance(error, HTTPException) else str(error)
        parser.exit(1, "BLOG_REVIEW_STOPPED: " + message + "\n")
    except Exception:
        # SQL errors may embed encrypted/plain draft parameters or DB URLs.
        parser.exit(1, "BLOG_REVIEW_STOPPED: operation failed; no approval confirmed\n")


if __name__ == "__main__":
    main()
