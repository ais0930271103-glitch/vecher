"""Public authors, exact joint consent, review CAS and reader compatibility."""
import asyncio
import hashlib
import json
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app import blog_media, blogs
from app.blog_review import review_post
from app.config import get_settings
from app.db import SessionLocal
from app.models import (BlogMediaAsset, ContentPreferences, ContentState, PublicBlog,
                        PublicBlogConsent, PublicPost, PublicPostApproval, PublicPostReport, VideoState)
from conftest import login, make_pair


@pytest.fixture(autouse=True)
def enabled_public_blogs(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "blogs_enabled", True)
    monkeypatch.setattr(get_settings(), "blog_media_dir", str(tmp_path / "public-media"))


def blog_body(kind="individual", **changes):
    return {"client_id": str(uuid.uuid4()), "kind": kind, "name": "Наши прогулки",
            "bio": "Готовые идеи для прогулок вдвоём", "public_consent": True,
            "consent_version": "public-blog-v1", **changes}


async def make_blog(client, headers, kind="individual", **changes):
    response = await client.post("/blogs", headers=headers, json=blog_body(kind, **changes))
    assert response.status_code == 201, response.text
    return response.json()


async def agree_blog(client, headers, blog):
    response = await client.post(f"/blogs/{blog['id']}/consent", headers=headers,
        json={"revision": blog["revision"], "accepted": True, "consent_version": "public-blog-v1"})
    assert response.status_code == 200, response.text
    return response.json()


async def ready_media(client, headers, blog):
    """A worker result fixture. Actual media normalization has separate tests."""
    uid = (await client.get("/auth/me", headers=headers)).json()["id"]
    data, thumb = b"normalized-test-video", b"test-thumbnail"
    source_key, output_key, thumb_key = (uuid.uuid4().hex for _ in range(3))
    for key, kind, raw in ((output_key, "video", data), (thumb_key, "thumbnail", thumb)):
        fd = blog_media.open_blob(key, kind, create=True)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
    async with SessionLocal() as session:
        asset = BlogMediaAsset(blog_id=blog["id"], uploader_id=uid, client_upload_id=uuid.uuid4().hex,
            status="ready", source_key=source_key, output_key=output_key, thumbnail_key=thumb_key,
            source_bytes=len(data), output_bytes=len(data), thumbnail_bytes=len(thumb),
            reserved_bytes=len(data) + len(thumb), sha256=hashlib.sha256(data).hexdigest(),
            mime="video/mp4", width=320, height=240, duration_ms=10000)
        session.add(asset)
        await session.commit()
        return {"id": asset.id, "sha256": asset.sha256, "key": output_key}


def post_body(media, **changes):
    return {"client_id": str(uuid.uuid4()), "title": "Как сохранить фотографии прогулки",
            "description": "Сохраним фотографии и резервную копию вместе. Три простых шага.",
            "topics": ["digital"], "language": "ru", "media_id": media["id"], **changes}


async def make_post(client, headers, blog, media, **changes):
    response = await client.post(f"/blogs/{blog['id']}/posts", headers=headers, json=post_body(media, **changes))
    assert response.status_code == 201, response.text
    return response.json()


async def submit(client, headers, blog, post):
    response = await client.post(f"/blogs/{blog['id']}/posts/{post['id']}/submit", headers=headers,
        json={"revision": post["revision"], "public_consent": True, "consent_version": "public-post-v1"})
    assert response.status_code == 200, response.text
    return response.json()


async def approve(blog, post, media, **changes):
    async with SessionLocal() as session:
        return await review_post(session, post["id"], action="approve", **{
            "revision": post["revision"], "blog_revision": blog["revision"], "sha256": media["sha256"], **changes})


async def published(client, headers, *, partner=None, **changes):
    blog = await make_blog(client, headers, "couple" if partner else "individual")
    if partner:
        blog = await agree_blog(client, partner, blog)
    media = await ready_media(client, headers, blog)
    post = await make_post(client, headers, blog, media, **changes)
    post = await submit(client, headers, blog, post)
    if partner:
        post = await submit(client, partner, blog, post)
    await approve(blog, post, media)
    return blog, post, media


async def reader_feed(client, headers, **params):
    response = await client.get("/content/feed", headers=headers,
        params={"hosted_video": True, "discover": True, **params})
    assert response.status_code == 200, response.text
    return response.json()


def ids(data):
    return [item["id"] for item in data["items"]]


async def test_disabled_feature_is_not_an_authoring_bypass_and_old_content_still_works(client, monkeypatch):
    h = await login(client, "off@example.ru")
    monkeypatch.setattr(get_settings(), "blogs_enabled", False)
    assert (await client.get("/blogs/mine", headers=h)).status_code == 404
    assert (await client.post("/blogs", headers=h, json=blog_body())).status_code == 404
    settings = (await client.get("/content/settings", headers=h)).json()
    assert settings["public_blogs_enabled"] is False
    assert (await reader_feed(client, h))["total"] > 0


async def test_explicit_identity_ownership_encrypted_drafts_and_bounded_create_idempotence(client):
    ha, hb = await make_pair(client)
    stranger = await login(client, "stranger@example.ru")
    await client.put("/auth/me", headers=ha, json={"display_name": "Private account name"})
    body = blog_body(name="Written public name")
    first, repeat = await asyncio.gather(*[
        client.post("/blogs", headers=ha, json=body) for _ in range(2)])
    assert first.status_code == repeat.status_code == 201
    blog = first.json()
    assert repeat.json()["id"] == blog["id"]
    assert blog["name"] == "Written public name" and blog["kind"] == "individual"
    assert "Private account name" not in first.text
    assert blog["my_consent"] and blog["partner_consent"] is None
    assert (await client.post("/blogs", headers=ha, json={**body, "name": "Different"})).status_code == 409
    assert (await client.post("/blogs", headers=ha, json=blog_body())).status_code == 409
    assert (await client.post("/blogs", headers=hb, json={**blog_body(), "owner_user_id": "injected"})).status_code == 422
    for headers in (hb, stranger):
        assert (await client.get(f"/blogs/{blog['id']}", headers=headers)).status_code == 404
        assert (await client.get(f"/blogs/{blog['id']}/posts", headers=headers)).status_code == 404
        assert (await client.get("/blogs/mine", headers=headers)).json()["items"] == []
    assert (await client.get(f"/content/authors/{blog['author_id']}", headers=stranger)).status_code == 404
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(PublicBlog)) == 1
        raw = (await session.execute(text("SELECT name,bio FROM public_blogs"))).one()
        assert all(value.startswith("enc1:") for value in raw)


async def test_couple_needs_separate_profile_and_both_exact_post_approvals_before_review(client):
    ha, hb = await make_pair(client)
    outsider = await login(client, "reader@example.ru")
    blog = await make_blog(client, ha, "couple")
    assert blog["status"] == "pending_consent" and not blog["can_publish"]
    other = (await client.get("/blogs/mine", headers=hb)).json()["items"][0]
    assert not other["my_consent"] and other["partner_consent"]
    assert (await client.post(f"/blogs/{blog['id']}/consent", headers=outsider,
        json={"revision": 1, "accepted": True, "consent_version": "public-blog-v1"})).status_code == 404
    media = await ready_media(client, ha, blog)
    post = await make_post(client, ha, blog, media)
    path = f"/blogs/{blog['id']}/posts/{post['id']}/submit"
    assert (await client.post(path, headers=ha, json={"revision": 1, "public_consent": True})).status_code == 409
    blog = await agree_blog(client, hb, blog)
    first = await submit(client, ha, blog, post)
    assert first["status"] == "pending_partner" and first["my_approved"] and not first["partner_approved"]
    with pytest.raises(HTTPException):
        await approve(blog, post, media)
    second = await submit(client, hb, blog, post)
    assert second["status"] == "pending_review" and second["my_approved"] and second["partner_approved"]
    assert post["content_id"] not in ids(await reader_feed(client, outsider))
    for pins in ({"revision": 2}, {"blog_revision": 2}, {"sha256": "0" * 64}):
        with pytest.raises(HTTPException) as error:
            await approve(blog, post, media, **pins)
        assert error.value.status_code == 409
    result = await approve(blog, post, media)
    assert result["status"] == "published"
    assert await approve(blog, post, media) == result
    detail = await client.get(f"/content/{post['content_id']}", headers=outsider, params={"hosted_video": True})
    assert detail.status_code == 200, detail.text
    value = detail.json()
    assert value["kind"] == "video" and value["video_id"] is None and value["playback_kind"] == "hosted"
    assert value["author"]["kind"] == "couple" and value["media"]["id"] == media["id"]
    assert not ({"owner_user_id", "pair_id", "created_by_user_id", "media_sha256", "review_note"} & value.keys())
    assert (await client.get(f"/content/{post['content_id']}", headers=outsider)).status_code == 404
    assert (await client.get(f"/content/{post['content_id']}?hosted_video=true")).status_code == 401
    assert post["content_id"] not in ids(await reader_feed(client, outsider, hosted_video=False))
    assert post["content_id"] in ids(await reader_feed(client, outsider, format="video"))
    assert post["content_id"] not in ids(await reader_feed(client, outsider, format="article"))


async def test_edits_revoke_both_approvals_keep_stable_reader_state_and_reject_stale_actions(client):
    ha, hb = await make_pair(client)
    reader = await login(client, "saved@example.ru")
    blog, post, media = await published(client, ha, partner=hb)
    state = await client.patch(f"/content/{post['content_id']}/state?hosted_video=true", headers=reader,
                               json={"saved": True, "useful": True})
    assert state.status_code == 200
    edit = await client.patch(f"/blogs/{blog['id']}/posts/{post['id']}", headers=ha,
                              json={"revision": 1, "description": "Новая инструкция о резервной копии фотографий."})
    assert edit.status_code == 200, edit.text
    edited = edit.json()
    assert edited["revision"] == 2 and edited["status"] == "draft"
    assert not edited["my_approved"] and not edited["partner_approved"]
    assert (await client.get(f"/content/{post['content_id']}?hosted_video=true", headers=reader)).status_code == 404
    assert (await client.post(f"/blogs/{blog['id']}/posts/{post['id']}/submit", headers=hb,
        json={"revision": 1, "public_consent": True})).status_code == 409
    with pytest.raises(HTTPException):
        await approve(blog, post, media)
    await submit(client, ha, blog, edited)
    await submit(client, hb, blog, edited)
    await approve(blog, edited, media)
    shown = (await client.get(f"/content/{post['content_id']}?hosted_video=true", headers=reader)).json()
    assert shown["saved"] and shown["useful"] and "Новая инструкция" in shown["summary"]
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(ContentState)) == 1
        assert await session.scalar(select(func.count()).select_from(VideoState)) == 0


async def test_revocation_hides_all_posts_and_media_and_reconsent_never_republishes(client):
    ha, hb = await make_pair(client)
    reader = await login(client, "outside@example.ru")
    blog, post, media = await published(client, ha, partner=hb)
    assert (await client.get(f"/blogs/media/{media['id']}/video", headers=reader)).status_code == 200
    revoked = await client.post(f"/blogs/{blog['id']}/consent", headers=hb,
        json={"revision": 1, "accepted": False, "consent_version": "public-blog-v1"})
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "withdrawn" and revoked.json()["revision"] == 2
    assert (await client.get(f"/blogs/media/{media['id']}/video", headers=reader)).status_code == 404
    assert (await client.get(f"/content/{post['content_id']}?hosted_video=true", headers=reader)).status_code == 404
    assert (await client.get(f"/content/authors/{blog['author_id']}", headers=reader)).status_code == 404
    blog = await agree_blog(client, ha, revoked.json())
    blog = await agree_blog(client, hb, blog)
    assert blog["status"] == "active"
    assert post["content_id"] not in ids(await reader_feed(client, reader))
    old = (await client.get(f"/blogs/{blog['id']}/posts/{post['id']}", headers=ha)).json()
    assert old["status"] == "withdrawn" and old["revision"] == 2
    # Even an already withdrawn post gets a fresh revision when its public identity changes.
    profile = await client.patch(f"/blogs/{blog['id']}", headers=ha,
        json={"revision": blog["revision"], "name": "Новое публичное имя"})
    assert profile.status_code == 200 and not profile.json()["my_consent"]
    updated = (await client.get(f"/blogs/{blog['id']}/posts/{post['id']}", headers=ha)).json()
    assert updated["revision"] == 3 and updated["status"] == "withdrawn"
    with pytest.raises(HTTPException):
        await approve(blog, updated, media)


async def test_pair_pause_blocks_new_actions_but_preserves_published_and_allows_withdrawal(client):
    ha, hb = await make_pair(client)
    reader = await login(client, "pause-reader@example.ru")
    blog, post, media = await published(client, ha, partner=hb)
    assert (await client.post("/pairs/pause", headers=ha, json={"on": True})).status_code == 200
    assert post["content_id"] in ids(await reader_feed(client, reader))
    assert (await client.post(f"/blogs/{blog['id']}/posts", headers=hb, json=post_body(media))).status_code == 409
    assert (await client.patch(f"/blogs/{blog['id']}", headers=hb,
        json={"revision": 1, "bio": "Новое описание"})).status_code == 409
    withdrawn = await client.post(f"/blogs/{blog['id']}/posts/{post['id']}/withdraw", headers=hb, json={"revision": 1})
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    assert (await client.post(f"/blogs/{blog['id']}/posts/{post['id']}/withdraw", headers=hb,
        json={"revision": 1})).json()["revision"] == 2
    assert (await client.get(f"/blogs/media/{media['id']}/video", headers=reader)).status_code == 404


async def test_following_recommendations_reports_and_private_choices_do_not_leak_to_partner(client):
    author = await login(client, "author@example.ru")
    ha, hb = await make_pair(client)
    blog, post, _ = await published(client, author)
    settings = (await client.get("/content/settings", headers=hb)).json()
    await client.put("/content/settings/shared", headers=hb,
        json={"topics": ["digital"], "language": "ru", "revision": settings["shared_revision"]})
    before = await reader_feed(client, hb, mode="shared", discover=False)
    assert post["content_id"] in ids(before)
    await client.patch(f"/content/authors/{blog['author_id']}/state", headers=ha, json={"followed": True})
    await client.patch(f"/content/{post['content_id']}/state?hosted_video=true", headers=ha,
        json={"useful": True, "saved": True})
    assert await reader_feed(client, hb, mode="shared", discover=False) == before
    assert (await client.get("/content/settings", headers=hb)).json()["followed_author_ids"] == []
    following = await reader_feed(client, ha, mode="following", discover=False)
    assert ids(following) == [post["content_id"]]
    assert following["items"][0]["recommendation_reason"] == "Новая публикация автора из ваших подписок"
    report_path = f"/content/{post['content_id']}/report"
    for headers, reason in ((ha, "privacy"), (ha, "spam"), (hb, "rights")):
        response = await client.post(report_path, headers=headers, json={"reason": reason})
        assert response.status_code == 200 and response.json() == {"ok": True, "can_hide": True}
    assert (await client.get(report_path, headers=author)).status_code == 405
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(PublicPostReport)) == 2
    await client.patch(f"/content/authors/{blog['author_id']}/state", headers=ha, json={"hidden": True})
    assert post["content_id"] not in ids(await reader_feed(client, ha))
    assert post["content_id"] in ids(await reader_feed(client, hb, mode="shared", discover=False))


async def test_media_owner_scope_and_post_idempotence_caps_and_concurrent_approvals(client, monkeypatch):
    ha, hb = await make_pair(client)
    own = await make_blog(client, ha)
    media = await ready_media(client, ha, own)
    joint = await make_blog(client, ha, "couple")
    joint = await agree_blog(client, hb, joint)
    assert (await client.post(f"/blogs/{joint['id']}/posts", headers=ha, json=post_body(media))).status_code == 404
    joint_media = await ready_media(client, ha, joint)
    body = post_body(joint_media)
    first, duplicate = await asyncio.gather(*[client.post(f"/blogs/{joint['id']}/posts", headers=ha,
                                                        json=body) for _ in range(2)])
    assert first.status_code == duplicate.status_code == 201
    post = first.json()
    assert duplicate.json()["id"] == post["id"]
    assert (await client.post(f"/blogs/{joint['id']}/posts", headers=ha,
        json={**body, "description": "Changed payload"})).status_code == 409
    results = await asyncio.gather(submit(client, ha, joint, post), submit(client, hb, joint, post))
    assert {value["status"] for value in results} == {"pending_partner", "pending_review"}
    assert (await submit(client, ha, joint, post))["revision"] == 1
    monkeypatch.setattr(blogs, "MAX_POSTS_PER_BLOG", 1)
    assert (await client.post(f"/blogs/{joint['id']}/posts", headers=ha, json=post_body(joint_media))).status_code == 409
    assert (await client.post(f"/blogs/{joint['id']}/posts", headers=ha, json=body)).json()["id"] == post["id"]


async def test_operator_checks_actual_file_hash_not_only_database_pin(client):
    ha = await login(client, "hash@example.ru")
    blog = await make_blog(client, ha)
    media = await ready_media(client, ha, blog)
    post = await make_post(client, ha, blog, media)
    await submit(client, ha, blog, post)
    path = os.path.join(get_settings().blog_media_dir, media["key"] + ".mp4")
    with open(path, "r+b") as target:
        target.write(b"x")
    with pytest.raises(ValueError, match="media_hash_mismatch"):
        await approve(blog, post, media)
    async with SessionLocal() as session:
        assert (await session.get(PublicPost, post["id"])).status == "pending_review"
    assert post["content_id"] not in ids(await reader_feed(client, ha))


async def test_empty_profiles_cannot_fill_reader_catalogue_and_review_enforces_its_limit(client, monkeypatch):
    ha, hb = await make_pair(client)
    monkeypatch.setattr(blogs, "MAX_PUBLIC_BLOGS", 1)
    one, two = await make_blog(client, ha), await make_blog(client, hb)
    assert (await reader_feed(client, ha))["total"] > 0
    media1, media2 = await ready_media(client, ha, one), await ready_media(client, hb, two)
    post1, post2 = await make_post(client, ha, one, media1), await make_post(client, hb, two, media2)
    await submit(client, ha, one, post1)
    await submit(client, hb, two, post2)
    await approve(one, post1, media1)
    with pytest.raises(HTTPException) as error:
        await approve(two, post2, media2)
    assert error.value.status_code == 409
    assert post1["content_id"] in ids(await reader_feed(client, hb))


async def test_publication_changes_invalidate_cursor_but_unpublished_drafts_do_not(client):
    ha, reader = await login(client, "cursor-author@example.ru"), await login(client, "cursor-reader@example.ru")
    first = await reader_feed(client, reader, limit=2)
    assert first["next_cursor"]
    blog = await make_blog(client, ha)
    media = await ready_media(client, ha, blog)
    post = await make_post(client, ha, blog, media)
    assert (await client.get("/content/feed", headers=reader, params={"hosted_video": True,
        "discover": True, "limit": 2, "cursor": first["next_cursor"]})).status_code == 200
    await submit(client, ha, blog, post)
    await approve(blog, post, media)
    assert (await client.get("/content/feed", headers=reader, params={"hosted_video": True,
        "discover": True, "limit": 2, "cursor": first["next_cursor"]})).status_code == 409


async def test_pair_and_account_erasure_revoke_public_media_and_keep_unrelated_solo_blog(client):
    ha, hb = await make_pair(client)
    reader = await login(client, "erasure-reader@example.ru")
    joint, post, media = await published(client, ha, partner=hb)
    solo, solo_post, solo_media = await published(client, ha)
    await client.post(f"/content/{post['content_id']}/report", headers=reader, json={"reason": "privacy"})
    assert (await client.post("/pairs/leave", headers=hb)).status_code == 200
    for asset in (media,):
        assert (await client.get(f"/blogs/media/{asset['id']}/video", headers=reader)).status_code == 404
    assert solo_post["content_id"] in ids(await reader_feed(client, reader))
    async with SessionLocal() as session:
        assert await session.get(PublicBlog, joint["id"]) is None
        assert await session.get(BlogMediaAsset, media["id"]) is None
        assert await session.get(PublicPost, post["id"]) is None
        assert await session.scalar(select(func.count()).select_from(PublicPostReport)) == 0
        assert await session.scalar(select(func.count()).select_from(PublicBlogConsent)) == 1
        assert await session.scalar(select(func.count()).select_from(PublicPostApproval)) == 1
    assert (await client.delete("/auth/me", headers=ha)).status_code == 200
    assert (await client.get(f"/blogs/media/{solo_media['id']}/video", headers=reader)).status_code == 404
    async with SessionLocal() as session:
        for model in (PublicBlog, PublicBlogConsent, PublicPost, BlogMediaAsset, PublicPostApproval, PublicPostReport):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_switching_feature_off_hides_existing_publication_and_media_without_erasing_it(client, monkeypatch):
    owner = await login(client, "feature-owner@example.ru")
    reader = await login(client, "feature-reader@example.ru")
    blog, post, media = await published(client, owner)
    monkeypatch.setattr(get_settings(), "blogs_enabled", False)
    assert post["content_id"] not in ids(await reader_feed(client, reader))
    assert (await client.get(f"/content/{post['content_id']}?hosted_video=true", headers=reader)).status_code == 404
    assert (await client.get(f"/content/authors/{blog['author_id']}", headers=reader)).status_code == 404
    assert (await client.get(f"/blogs/media/{media['id']}/video", headers=reader)).status_code == 404
    assert (await client.get("/blogs/mine", headers=owner)).status_code == 404
    async with SessionLocal() as session:
        assert (await session.get(PublicPost, post["id"])).status == "published"


async def test_rejected_revision_cannot_be_resubmitted_by_a_stale_approval_retry(client):
    ha, hb = await make_pair(client)
    blog = await agree_blog(client, hb, await make_blog(client, ha, "couple"))
    media = await ready_media(client, ha, blog)
    post = await make_post(client, ha, blog, media)
    await submit(client, ha, blog, post)
    await submit(client, hb, blog, post)
    async with SessionLocal() as session:
        rejected = await review_post(session, post["id"], action="reject", revision=1,
            blog_revision=1, sha256=media["sha256"], note="Уточните описание занятия")
    assert rejected["status"] == "rejected" and rejected["revision"] == 2
    assert (await client.post(f"/blogs/{blog['id']}/posts/{post['id']}/submit", headers=ha,
        json={"revision": 1, "public_consent": True})).status_code == 409
    current = (await client.get(f"/blogs/{blog['id']}/posts/{post['id']}", headers=hb)).json()
    assert current["review_note"] == "Уточните описание занятия"
    assert not current["my_approved"] and not current["partner_approved"]
    await submit(client, ha, blog, current)
    with pytest.raises(HTTPException):
        await approve(blog, current, media)
    await submit(client, hb, blog, current)
    assert (await approve(blog, current, media))["status"] == "published"
