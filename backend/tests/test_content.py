"""Reader privacy, explicit discovery, cursor stability and editorial publication."""
import asyncio
import base64
import copy
import json

import pytest
from sqlalchemy import delete, select, text

from app import content_catalogue as content, video_catalogue
from app.config import get_settings
from app.db import SessionLocal
from app.models import ContentPreferences, ContentState, DeliveryOutbox, SharedContentPreferences
from app.publish_content import PublicationConflict, publish, validate_source
from conftest import login, make_pair


def publication(item_id, topic="digital", author="practicum", **extra):
    return dict(id=item_id, kind="guide", title=f"Практика {item_id}", summary="Конкретная инструкция для проверки",
                author_id=author, topics=[topic], language="ru", blocks=[
                    {"type": "heading", "text": "Перед началом"},
                    {"type": "paragraph", "text": "Сохрани резервную копию. Проверка занимает несколько минут."},
                    {"type": "list", "items": ["Открой настройки", "Проверь результат"]}],
                sources=[{"title": "Справка", "url": "https://support.google.com/accounts/"}],
                published_at="2026-10-22T09:00:00Z", updated_at="2026-10-22T09:00:00Z",
                reviewed_at="2026-10-22", active=True, reading_minutes=3, **extra)


def sample():
    items = [publication("backup"), publication("camera", "creativity"),
             publication("walk", "travel", "weekend"), publication("map", "travel", "weekend"),
             publication("english", "digital", "weekend"), publication("withdrawn", "food")]
    items[4]["language"] = "en"
    items[5]["active"] = False
    for order, item in enumerate(items[:2], 1):
        item.update(series_id="small-skills", series_title="Маленькие навыки", series_order=order)
    return dict(version=1, topics=[{"id": key, "title": key} for key in ("digital", "creativity", "travel", "food")],
                authors=[{"id": "practicum", "name": "Практикум", "bio": "Тестовая редакция"},
                         {"id": "weekend", "name": "Выходной", "bio": "Тестовая редакция"}], items=items)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    path = tmp_path / "publications.json"
    path.write_text(json.dumps(sample(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("VECHER_CONTENT_CATALOGUE", str(path))
    return path


def edit_snapshot(path, change):
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def feed(client, h, **params):
    response = await client.get("/content/feed", headers=h, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def ids(data):
    return [item["id"] for item in data["items"]]


async def choose_all(client, headers, mode="personal"):
    settings = (await client.get("/content/settings", headers=headers)).json()
    response = await client.put(f"/content/settings/{mode}", headers=headers,
                                json={"topics": ["digital", "creativity", "travel"], "language": "ru",
                                      "revision": settings[f"{mode}_revision"]})
    assert response.status_code == 200, response.text


async def test_content_requires_consent_not_a_pair_and_reading_records_no_history(client, snapshot):
    assert (await client.get("/content/feed")).status_code == 401
    h = await login(client, "reader@example.ru", consent=False)
    assert (await client.get("/content/feed", headers=h)).status_code == 403
    from app.consent import CONSENT_VERSION
    await client.post("/auth/consent", headers=h, json={"version": CONSENT_VERSION, "personal": True})
    data = await feed(client, h)
    assert ids(data) == []  # no arbitrary recommendations before choosing interests
    assert set(ids(await feed(client, h, author_id="practicum"))) == {"backup", "camera"}
    for path in ("/content/settings", "/content/authors", "/content/authors/practicum", "/content/backup"):
        response = await client.get(path, headers=h)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
    assert (await client.get("/content/feed?mode=shared", headers=h)).status_code == 409
    assert (await client.put("/content/settings/shared", headers=h,
                             json={"topics": [], "language": "ru", "revision": 0})).status_code == 409
    async with SessionLocal() as session:
        assert (await session.execute(select(ContentState))).scalars().all() == []
        assert (await session.execute(select(ContentPreferences))).scalars().all() == []
    await choose_all(client, h)
    assert set(ids(await feed(client, h))) == {"backup", "camera", "walk", "map"}


async def test_private_topics_follows_saves_never_change_partner_or_explicit_shared_choices(client, snapshot):
    ha, hb = await make_pair(client)
    await choose_all(client, hb)
    partner_before = await feed(client, hb)
    common_before = await feed(client, hb, mode="shared")
    selected = await client.put("/content/settings/personal", headers=ha,
                                json={"topics": ["digital"], "language": "ru", "revision": 0})
    assert selected.status_code == 200 and selected.json()["personal_revision"] == 1
    assert ids(await feed(client, ha)) == ["backup"]
    await client.patch("/content/authors/weekend/state", headers=ha, json={"followed": True})
    await client.patch("/content/walk/state", headers=ha, json={"useful": True, "saved": True})
    assert await feed(client, hb) == partner_before
    assert await feed(client, hb, mode="shared") == common_before
    partner = (await client.get("/content/settings", headers=hb)).json()
    assert partner["personal_topics"] == ["digital", "creativity", "travel"]
    assert partner["shared_topics"] == partner["followed_author_ids"] == []
    assert not (await client.get("/content/walk", headers=hb)).json()["saved"]
    shared = await client.put("/content/settings/shared", headers=hb,
                              json={"topics": ["creativity"], "language": "ru", "revision": 0})
    assert shared.status_code == 200 and shared.json()["shared_revision"] == 1
    for h in (ha, hb):
        assert ids(await feed(client, h, mode="shared")) == ["camera"]
    assert (await client.put("/content/settings/shared", headers=ha,
                             json={"topics": ["travel"], "language": "ru", "revision": 0})).status_code == 409
    assert (await client.put("/content/settings/personal", headers=ha,
                             json={"topics": [], "language": "ru", "revision": 1})).status_code == 409
    async with SessionLocal() as session:
        for table in ("content_preferences", "shared_content_preferences", "content_states"):
            rows = (await session.execute(text(f"SELECT data FROM {table}"))).scalars().all()
            assert rows and all(value.startswith("enc1:") for value in rows)
            assert all("digital" not in value and "weekend" not in value and "saved" not in value for value in rows)


async def test_following_author_series_and_saved_are_explicit_browsing_choices(client, snapshot):
    h = await login(client, "browse@example.ru")
    assert ids(await feed(client, h, mode="following")) == []
    await client.put("/content/settings/personal", headers=h,
                     json={"topics": ["digital"], "language": "en", "revision": 0})
    assert ids(await feed(client, h)) == ["english"]
    await client.patch("/content/authors/practicum/state", headers=h, json={"followed": True, "hidden": True})
    # Explicit author/series browsing ignores personal language, topics and
    # author hide; the normal feed/following still respects those choices.
    assert set(ids(await feed(client, h, author_id="practicum"))) == {"backup", "camera"}
    assert ids(await feed(client, h, series_id="small-skills")) == ["backup", "camera"]
    assert ids(await feed(client, h, mode="following")) == []
    await client.patch("/content/camera/state", headers=h, json={"saved": True, "hidden": True})
    assert ids(await feed(client, h, series_id="small-skills")) == ["backup"]
    assert ids(await feed(client, h, mode="saved")) == ["camera"]
    detail = (await client.get("/content/camera", headers=h)).json()
    assert detail["saved"] and detail["hidden"] and detail["author_hidden"] and detail["author_followed"]
    authors = (await client.get("/content/authors", headers=h)).json()["items"]
    assert len(authors) == 2 and next(a for a in authors if a["id"] == "practicum")["hidden"]
    await client.patch("/content/authors/practicum/state", headers=h, json={"hidden": False})
    settings = (await client.get("/content/settings", headers=h)).json()
    await client.put("/content/settings/personal", headers=h,
                     json={"topics": ["travel"], "language": "all", "revision": settings["personal_revision"]})
    assert ids(await feed(client, h, mode="following")) == ["backup"]
    assert ids(await feed(client, h, author_id="practicum", q="резервную копию")) == ["backup"]


async def test_topics_are_union_and_withdrawing_last_matching_item_never_broadens_feed(client, snapshot):
    ha, hb = await make_pair(client)
    await choose_all(client, hb)
    for mode in ("personal", "shared"):
        result = await client.put(f"/content/settings/{mode}", headers=ha,
                                  json={"topics": ["digital", "creativity"], "language": "ru", "revision": 0})
        assert result.status_code == 200
        assert set(ids(await feed(client, ha, mode=mode))) == {"backup", "camera"}
    edit_snapshot(snapshot, lambda value: [item.update(active=False) for item in value["items"]
                                           if item["id"] in {"backup", "camera", "english"}])
    assert ids(await feed(client, ha)) == ids(await feed(client, ha, mode="shared")) == []
    selected = (await client.get("/content/settings", headers=ha)).json()
    assert selected["personal_topics"] == ["digital", "creativity"]
    assert {topic["id"] for topic in selected["topics"] if topic["available"]} == {"travel"}
    assert {topic["id"] for topic in selected["topics"] if not topic["available"]} == {"digital", "creativity", "food"}
    assert set(ids(await feed(client, hb))) == {"walk", "map"}


async def test_stable_cursor_no_duplicates_bound_to_reader_and_all_filters(client, snapshot):
    ha, hb = await make_pair(client)
    await choose_all(client, ha)
    await choose_all(client, hb)
    await choose_all(client, ha, "shared")
    first = await feed(client, ha, limit=2)
    second = await feed(client, ha, limit=2, cursor=first["next_cursor"])
    assert len(ids(first)) == len(ids(second)) == 2
    assert not (set(ids(first)) & set(ids(second))) and second["next_cursor"] is None
    raw = base64.urlsafe_b64decode(first["next_cursor"].split(".")[0] + "==")
    assert set(json.loads(raw)) == {"v", "f", "o"}
    assert b"digital" not in raw and b"practicum" not in raw
    for h, params in ((hb, {"limit": 2}), (ha, {"limit": 3}), (ha, {"limit": 2, "mode": "shared"}),
                       (ha, {"limit": 2, "q": "backup"}), (ha, {"limit": 2, "author_id": "practicum"}),
                       (ha, {"limit": 2, "series_id": "small-skills"})):
        response = await client.get("/content/feed", headers=h, params={**params, "cursor": first["next_cursor"]})
        assert response.status_code == 409, response.text
    invalid = first["next_cursor"][:-1] + ("0" if first["next_cursor"][-1] != "0" else "1")
    assert (await client.get("/content/feed", headers=ha, params={"cursor": invalid})).status_code == 422
    assert (await client.get("/content/feed", headers=ha, params={"cursor": "malformed"})).status_code == 422


@pytest.mark.parametrize("change", ["personal", "follow", "item", "catalogue", "shared"])
async def test_cursor_detects_revisions_even_if_boolean_changes_back(client, snapshot, change):
    ha, hb = await make_pair(client)
    mode = "shared" if change == "shared" else "personal"
    await choose_all(client, ha, mode)
    first = await feed(client, ha, mode=mode, limit=1)
    if change == "personal":
        await client.put("/content/settings/personal", headers=ha,
                         json={"topics": ["travel"], "language": "ru", "revision": 1})
    elif change == "follow":
        await client.patch("/content/authors/practicum/state", headers=ha, json={"followed": True})
    elif change == "item":
        for value in (True, False):
            await client.patch("/content/backup/state", headers=ha, json={"useful": value})
    elif change == "shared":
        await client.put("/content/settings/shared", headers=hb,
                         json={"topics": ["travel"], "language": "ru", "revision": 1})
    else:
        edit_snapshot(snapshot, lambda value: value["items"][0].update(title="Новая редакция инструкции"))
    response = await client.get("/content/feed", headers=ha,
                                params={"mode": mode, "limit": 1, "cursor": first["next_cursor"]})
    assert response.status_code == 409


async def test_private_partner_change_does_not_invalidate_shared_cursor(client, snapshot):
    ha, hb = await make_pair(client)
    await choose_all(client, ha, "shared")
    first = await feed(client, hb, mode="shared", limit=1)
    await client.patch("/content/walk/state", headers=ha, json={"saved": True, "hidden": True})
    await client.patch("/content/authors/practicum/state", headers=ha, json={"hidden": True})
    assert (await client.get("/content/feed", headers=hb,
                            params={"mode": "shared", "limit": 1, "cursor": first["next_cursor"]})).status_code == 200


async def test_idempotent_states_merge_concurrent_fields_and_realtime_is_private(client, snapshot, monkeypatch):
    ha, hb = await make_pair(client)
    uid = (await client.get("/auth/me", headers=ha)).json()["id"]
    monkeypatch.setattr(get_settings(), "realtime_enabled", True)
    responses = await asyncio.gather(
        client.patch("/content/backup/state", headers=ha, json={"saved": True}),
        client.patch("/content/backup/state", headers=ha, json={"useful": True}),
    )
    assert [r.status_code for r in responses] == [200, 200]
    detail = (await client.get("/content/backup", headers=ha)).json()
    assert detail["saved"] and detail["useful"]
    async with SessionLocal() as session:
        own = await session.get(ContentState, (uid, "backup"))
        revision, modified = own.revision, own.updated_at
        deliveries = (await session.execute(select(DeliveryOutbox).where(DeliveryOutbox.kind == "realtime"))).scalars().all()
        assert len(deliveries) == 2 and {row.user_id for row in deliveries} == {uid}
        assert all(json.loads(row.payload)["scopes"] == ["solo"] for row in deliveries)
        await session.execute(delete(DeliveryOutbox))
        await session.commit()
    await client.patch("/content/backup/state", headers=ha, json={"useful": True, "saved": True})
    async with SessionLocal() as session:
        own = await session.get(ContentState, (uid, "backup"))
        assert (own.revision, own.updated_at) == (revision, modified)
        assert (await session.execute(select(DeliveryOutbox))).scalars().all() == []
    await client.put("/content/settings/shared", headers=ha,
                     json={"topics": ["travel"], "language": "ru", "revision": 0})
    async with SessionLocal() as session:
        deliveries = (await session.execute(select(DeliveryOutbox).where(DeliveryOutbox.kind == "realtime"))).scalars().all()
        assert len(deliveries) == 2 and len({row.user_id for row in deliveries}) == 2
        assert all(json.loads(row.payload)["scopes"] == ["us"] for row in deliveries)


async def test_settings_reject_stale_concurrent_writes_and_state_rejects_implicit_values(client, snapshot):
    h = await login(client, "concurrent@example.ru")
    responses = await asyncio.gather(*[
        client.put("/content/settings/personal", headers=h, json={"topics": [topic], "language": "ru", "revision": 0})
        for topic in ("digital", "travel")])
    assert sorted(r.status_code for r in responses) == [200, 409]
    for body in ({}, {"saved": None}, {"saved": 1}, {"saved": "true"}, {"watched_seconds": 30},
                 {"saved": True, "user_id": "other"}):
        assert (await client.patch("/content/backup/state", headers=h, json=body)).status_code == 422
    for body in ({}, {"followed": None}, {"hidden": "false"}, {"followed": True, "pair_id": "other"}):
        assert (await client.patch("/content/authors/practicum/state", headers=h, json=body)).status_code == 422
    for body in ({"topics": ["unknown"], "language": "ru", "revision": 1},
                 {"topics": ["digital", "digital"], "language": "ru", "revision": 1},
                 {"topics": [], "language": "bad", "revision": 1},
                 {"topics": [], "language": "ru", "revision": True}):
        assert (await client.put("/content/settings/personal", headers=h, json=body)).status_code == 422
    assert (await client.patch("/content/withdrawn/state", headers=h, json={"saved": True})).status_code == 404


async def test_pair_and_account_erasure_remove_only_appropriate_content_choices(client, snapshot):
    ha, hb = await make_pair(client)
    for h in (ha, hb):
        await client.patch("/content/authors/practicum/state", headers=h, json={"followed": True})
        await client.patch("/content/backup/state", headers=h, json={"saved": True})
    await client.put("/content/settings/shared", headers=ha,
                     json={"topics": ["digital"], "language": "ru", "revision": 0})
    assert (await client.post("/pairs/leave", headers=ha)).status_code == 200
    async with SessionLocal() as session:
        assert (await session.execute(select(SharedContentPreferences))).scalars().all() == []
        assert len((await session.execute(select(ContentState))).scalars().all()) == 2
    assert (await client.delete("/auth/me", headers=ha)).status_code == 200
    async with SessionLocal() as session:
        assert len((await session.execute(select(ContentPreferences))).scalars().all()) == 1
        assert len((await session.execute(select(ContentState))).scalars().all()) == 1
    assert ids(await feed(client, hb, mode="saved")) == ["backup"]


async def test_legacy_video_saves_and_ids_remain_separate_without_auto_import(client, snapshot):
    h = await login(client, "old-client@example.ru")
    await choose_all(client, h)
    video = next(video for video in video_catalogue.catalogue() if video.active)
    assert (await client.patch(f"/videos/{video.id}/state", headers=h, json={"saved": True})).status_code == 200
    assert all(item["kind"] != "video" for item in (await feed(client, h))["items"])
    assert ids(await feed(client, h, mode="saved")) == []
    old = await client.get("/videos/feed?mode=saved", headers=h)
    assert old.status_code == 200 and video.id in ids(old.json())
    edit_snapshot(snapshot, lambda value: value["items"].append(publication(
        "reviewed-film", video_id=video.id)))
    # A reference only makes sense when its kind is explicitly video.
    assert (await client.get("/content/feed", headers=h)).status_code == 503
    edit_snapshot(snapshot, lambda value: value["items"][-1].update(kind="video"))
    assert "reviewed-film" in ids(await feed(client, h))
    assert (await client.get(f"/videos/{video.id}", headers=h)).json()["saved"]


@pytest.mark.parametrize("url", ["http://example.org/page", "https://u:p@example.org/page", "https://localhost/x",
                                "https://127.0.0.1/x", "https://127.1/x", "https://0177.0.0.1/x",
                                "https://0x7f.1/x", "https://[::1]/x", "https://example.org:8443/x",
                                "https://app.internal/x", "https://app.local/x", "https://example.org\\@evil.org/x",
                                "javascript:alert(1)", "https://example.org/\nx"])
def test_catalogue_rejects_untrusted_source_and_author_urls(url):
    for target in ("source", "author"):
        data = sample()
        if target == "source":
            data["items"][0]["sources"][0]["url"] = url
        else:
            data["authors"][0]["url"] = url
        with pytest.raises(ValueError):
            content.validate_bytes(json.dumps(data).encode())


@pytest.mark.parametrize("change", [
    lambda d: d.update(version=True),
    lambda d: d["items"].append(copy.deepcopy(d["items"][0])),
    lambda d: d["items"][0].update(author_id="missing"),
    lambda d: d["items"][0].update(topics=["missing"]),
    lambda d: d["items"][0].update(topics=["digital", "digital"]),
    lambda d: d["items"][0].update(series_order=None),
    lambda d: d["items"][1].update(series_order=1),
    lambda d: d["items"][0].update(published_at="2026-10-22T09:00:00"),
    lambda d: d["items"][0].update(title="<script>bad</script>"),
    lambda d: d["items"][0].update(blocks=[{"type": "iframe", "src": "https://example.org/"}]),
    lambda d: d["items"][0].update(active="true"),
    lambda d: d["items"][0].update(reading_minutes=True),
    lambda d: d["items"][0].update(video_id="legacy-video"),
    lambda d: d["items"][0].update(id="settings"),
])
def test_catalogue_rejects_ambiguous_or_unrenderable_publications(change):
    data = sample()
    change(data)
    with pytest.raises(ValueError):
        content.validate_bytes(json.dumps(data).encode())


def test_snapshot_rejects_duplicate_fields_oversize_symlinks_and_excessive_nesting(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        content.validate_bytes(b'{"version":1,"version":1}')
    with pytest.raises(ValueError):
        content.validate_bytes(b"[" * 3000 + b"]" * 3000)
    with pytest.raises(ValueError):
        content.validate_bytes(b"x" * (content.MAX_SNAPSHOT_BYTES + 1))
    target = tmp_path / "target.json"
    target.write_text(json.dumps(sample()), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        content.read_snapshot(link)


async def test_bad_selected_snapshot_fails_closed_without_server_path_or_effect_on_other_api(client, snapshot):
    h = await login(client, "failclosed@example.ru")
    snapshot.write_text("private unpublished malformed draft", encoding="utf-8")
    response = await client.get("/content/feed", headers=h)
    assert response.status_code == 503
    assert str(snapshot) not in response.text and "unpublished" not in response.text
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/videos/settings", headers=h)).status_code == 200


def test_atomic_publication_cas_validation_and_runtime_refresh_without_restart(tmp_path, monkeypatch):
    source, destination = tmp_path / "candidate.json", tmp_path / "installed.json"
    source.write_text(json.dumps(sample()), encoding="utf-8")
    original = publish(source, destination, create=True)
    monkeypatch.setenv("VECHER_CONTENT_CATALOGUE", str(destination))
    assert content.catalogue().revision == original.revision
    assert destination.stat().st_mode & 0o777 == 0o644
    with pytest.raises(PublicationConflict):
        publish(source, destination, create=True)
    edit_snapshot(source, lambda d: d["items"][0].update(title="Отредактированная инструкция"))
    with pytest.raises(PublicationConflict):
        publish(source, destination, expected_revision="0" * 64)
    assert content.catalogue().revision == original.revision
    edited = publish(source, destination, expected_revision=original.revision)
    assert edited.revision != original.revision and content.catalogue().revision == edited.revision
    before = destination.read_bytes()
    source.write_text("not JSON", encoding="utf-8")
    with pytest.raises(ValueError):
        publish(source, destination, expected_revision=edited.revision)
    assert destination.read_bytes() == before


def test_publisher_requires_existing_reviewed_video_and_preserves_file_on_replace_error(tmp_path, monkeypatch):
    source, destination = tmp_path / "candidate.json", tmp_path / "installed.json"
    source.write_text(json.dumps(sample()), encoding="utf-8")
    original = publish(source, destination, create=True)
    edit_snapshot(source, lambda d: d["items"][0].update(kind="video", video_id="unavailable"))
    with pytest.raises(ValueError, match="unavailable reviewed video"):
        validate_source(source)
    source.write_text(json.dumps(sample()), encoding="utf-8")
    edit_snapshot(source, lambda d: d["items"][0].update(title="Новое название"))
    before = destination.read_bytes()

    def fail_replace(*_):
        raise OSError("injected storage failure")

    monkeypatch.setattr("app.publish_content.os.replace", fail_replace)
    with pytest.raises(OSError):
        publish(source, destination, expected_revision=original.revision)
    assert destination.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


async def test_real_pilot_snapshot_has_complete_feed_detail_author_and_series_contract(client, monkeypatch):
    # Exercise the actual bundled editorial release, not only synthetic cases.
    monkeypatch.delenv("VECHER_CONTENT_CATALOGUE", raising=False)
    snapshot = validate_source(content.DEFAULT_PATH)
    h = await login(client, "pilot-reader@example.ru")
    settings = (await client.get("/content/settings", headers=h)).json()
    assert settings["topics"] and settings["catalogue_revision"] == snapshot.revision
    chosen = settings["topics"][0]["id"]
    selected = await client.put("/content/settings/personal", headers=h,
                                json={"topics": [chosen], "language": "all", "revision": 0})
    assert selected.status_code == 200
    data = await feed(client, h)
    expected = {item.id for item in snapshot.items if item.active and chosen in item.topics}
    assert set(ids(data)) == expected and expected
    first = data["items"][0]
    detail = (await client.get(f"/content/{first['id']}", headers=h)).json()
    assert detail == first and detail["blocks"] and detail["summary"]
    author = (await client.get(f"/content/authors/{first['author']['id']}", headers=h)).json()
    assert author["name"] == first["author"]["name"]
    assert author["published_count"] == sum(item.active and item.author_id == author["id"] for item in snapshot.items)
    if first["series_id"]:
        series = await feed(client, h, series_id=first["series_id"])
        assert len(series["items"]) >= 1
        assert [item["series_order"] for item in series["items"]] == sorted(item["series_order"] for item in series["items"])
