"""Real API flows: private interests, explicit sharing, stale devices and reversible feedback."""
import asyncio
import json
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, text

from app import push, video_catalogue as content
from app.config import get_settings
from app.db import SessionLocal
from app.deps import today_date
from app.main import app
from app.models import (Consent, DeliveryOutbox, SharedVideoPreferences, VideoPreferences,
                        VideoState, WishCard, WishSwipe)
from conftest import login, make_pair


def item(video_id="cat", topic="animals", **extra):
    return dict(id=video_id, title=f"Ролик {video_id}", description="Проверенный тестовый сюжет",
                author=f"Автор {video_id}", source_url=f"https://commons.wikimedia.org/wiki/File:{video_id}.webm",
                media_url=f"https://upload.wikimedia.org/wikipedia/commons/{video_id}.webm",
                thumbnail_url=None, license_name="CC BY 4.0",
                license_url="https://creativecommons.org/licenses/by/4.0/", topics=[topic], language="none",
                duration_seconds=20, aspect_ratio=9 / 16, reviewed_at="2026-10-22", active=True, **extra)


@pytest.fixture(autouse=True)
def catalogue(monkeypatch):
    entries = [item(), item("coffee", "cooking", plan_deck="home", plan_text="Завтрак в постель"),
               item("mountains", "travel"), item("pottery", "hobbies"),
               item("dog", "animals"), item("cartoon", "films"), item("old", "home")]
    entries[-1]["active"] = False
    data = list(content.validate_catalogue(entries))
    monkeypatch.setattr(content, "catalogue", lambda: tuple(data))
    app.dependency_overrides[today_date] = lambda: date(2026, 10, 22)
    return data


async def uid(client, headers):
    return (await client.get("/auth/me", headers=headers)).json()["id"]


async def get_feed(client, headers, mode="personal"):
    response = await client.get("/videos/feed", headers=headers, params={"mode": mode})
    assert response.status_code == 200, response.text
    return response.json()


def ids(data):
    return [video["id"] for video in data["items"]]


async def test_personal_feed_requires_consent_but_does_not_require_pair_or_special_consent(client):
    assert (await client.get("/videos/feed")).status_code == 401
    h = await login(client, "single@example.ru", consent=False)
    assert (await client.get("/videos/settings", headers=h)).status_code == 403
    from app.consent import CONSENT_VERSION
    await client.post("/auth/consent", headers=h, json={"version": CONSENT_VERSION, "personal": True})
    data = await get_feed(client, h)
    assert data["mode"] == "personal" and data["total"] == 6 and "old" not in ids(data)
    assert all(video["plan_card_id"] is None for video in data["items"])
    settings = (await client.get("/videos/settings", headers=h)).json()
    assert not settings["has_pair"] and settings["shared_revision"] == 0
    assert (await client.get("/videos/feed?mode=shared", headers=h)).status_code == 409
    assert (await client.get("/videos/shared", headers=h)).status_code == 409
    assert (await client.put("/videos/settings/shared", headers=h, json={"topics": [], "revision": 0})).status_code == 409


async def test_settings_private_shared_explicit_and_stale_revisions_fail_without_overwrite(client):
    ha, hb = await make_pair(client)
    a = await client.put("/videos/settings/personal", headers=ha, json={"topics": ["animals"], "revision": 0})
    assert a.status_code == 200 and a.json()["personal_revision"] == 1
    assert a.json()["personal_topics"] == ["animals"] and a.json()["shared_topics"] == []
    b = (await client.get("/videos/settings", headers=hb)).json()
    assert b["personal_topics"] == b["shared_topics"] == []
    shared = await client.put("/videos/settings/shared", headers=hb, json={"topics": ["travel"], "revision": 0})
    assert shared.status_code == 200 and shared.json()["shared_revision"] == 1
    stale = await client.put("/videos/settings/shared", headers=ha, json={"topics": ["cooking"], "revision": 0})
    assert stale.status_code == 409
    a = (await client.get("/videos/settings", headers=ha)).json()
    assert a["personal_topics"] == ["animals"] and a["shared_topics"] == ["travel"]
    assert (await client.put("/videos/settings/personal", headers=ha,
                             json={"topics": [], "revision": 0})).status_code == 409
    for body in ({"topics": ["unknown"], "revision": 1}, {"topics": ["animals", "animals"], "revision": 1},
                 {"topics": ["home"], "revision": 1}, {"topics": [], "revision": True},
                 {"topics": [], "revision": 1, "user_id": "another-user"}):
        assert (await client.put("/videos/settings/personal", headers=ha, json=body)).status_code == 422


async def test_personal_interest_remains_selected_after_viewing_and_does_not_change_partner_feed(client):
    ha, hb = await make_pair(client)
    shared_before = ids(await get_feed(client, hb, "shared"))
    own_b_before = await get_feed(client, hb)
    await client.put("/videos/settings/personal", headers=ha, json={"topics": ["cooking"], "revision": 0})
    ranked = ids(await get_feed(client, ha))
    assert ranked == ["coffee"]
    assert (await client.post("/videos/coffee/viewed", headers=ha)).json() == {"ok": True}
    assert ids(await get_feed(client, ha)) == ["coffee"]
    assert ids(await get_feed(client, hb, "shared")) == shared_before
    assert await get_feed(client, hb) == own_b_before
    assert ids(await get_feed(client, ha, "shared")) == shared_before
    assert "last_viewed_day" not in (await client.get("/videos/coffee", headers=ha)).text


@pytest.mark.parametrize("mode", ["personal", "shared"])
async def test_selected_topics_form_union_before_limit_and_keep_existing_response_shape(client, catalogue, mode):
    ha, _ = await make_pair(client)
    # A video matching both chosen topics is still a single candidate; each
    # topic also has a video of its own, so intersection filtering would fail.
    catalogue[0] = catalogue[0].model_copy(update={"topics": ["animals", "travel"]})
    response = await client.put(f"/videos/settings/{mode}", headers=ha,
                                json={"topics": ["animals", "travel"], "revision": 0})
    assert response.status_code == 200
    assert (await client.patch("/videos/coffee/state", headers=ha,
                               json={"liked": True, "saved": True})).status_code == 200
    data = await get_feed(client, ha, mode)
    assert set(data) == {"items", "mode", "catalogue_revision", "total"}
    assert data["mode"] == mode and data["total"] == 3
    assert len(ids(data)) == 3 and set(ids(data)) == {"cat", "dog", "mountains"}
    assert set(data["items"][0]) == {
        "id", "title", "description", "author", "source_url", "media_url", "thumbnail_url",
        "license_name", "license_url", "topics", "language", "duration_seconds", "aspect_ratio",
        "liked", "saved", "hidden", "plan_card_id",
    }
    limited = await client.get("/videos/feed", headers=ha, params={"mode": mode, "limit": 1})
    assert limited.status_code == 200
    assert limited.json() == {**data, "items": data["items"][:1]}


@pytest.mark.parametrize("mode", ["personal", "shared"])
async def test_empty_selection_preserves_starter_feed_and_clearing_interests_restores_it(client, mode):
    ha, _ = await make_pair(client)
    starter = await get_feed(client, ha, mode)
    assert starter["total"] == 6
    chosen = await client.put(f"/videos/settings/{mode}", headers=ha,
                             json={"topics": ["animals"], "revision": 0})
    assert chosen.status_code == 200
    assert set(ids(await get_feed(client, ha, mode))) == {"cat", "dog"}
    cleared = await client.put(f"/videos/settings/{mode}", headers=ha,
                              json={"topics": [], "revision": 1})
    assert cleared.status_code == 200
    assert await get_feed(client, ha, mode) == starter


@pytest.mark.parametrize("mode", ["personal", "shared"])
async def test_hiding_all_matching_videos_returns_empty_without_affecting_partner(client, mode):
    ha, hb = await make_pair(client)
    selected = await client.put(f"/videos/settings/{mode}", headers=ha,
                               json={"topics": ["animals"], "revision": 0})
    assert selected.status_code == 200
    partner_before = await get_feed(client, hb, mode)
    for video_id in ("cat", "dog"):
        hidden = await client.patch(f"/videos/{video_id}/state", headers=ha, json={"hidden": True})
        assert hidden.status_code == 200
    empty = await get_feed(client, ha, mode)
    assert empty["items"] == [] and empty["total"] == 0 and empty["mode"] == mode
    assert await get_feed(client, hb, mode) == partner_before
    assert (await client.patch("/videos/cat/state", headers=ha, json={"hidden": False})).status_code == 200
    assert ids(await get_feed(client, ha, mode)) == ["cat"]


@pytest.mark.parametrize("mode", ["personal", "shared"])
async def test_disabling_last_selected_topic_video_does_not_fall_back_to_other_topics(client, catalogue, mode):
    ha, _ = await make_pair(client)
    selected = await client.put(f"/videos/settings/{mode}", headers=ha,
                               json={"topics": ["cooking"], "revision": 0})
    assert selected.status_code == 200
    coffee_index = next(i for i, video in enumerate(catalogue) if video.id == "coffee")
    catalogue[coffee_index] = catalogue[coffee_index].model_copy(update={"active": False})
    assert "cooking" not in {topic["id"] for topic in content.available_topics()}
    empty = await get_feed(client, ha, mode)
    assert empty["items"] == [] and empty["total"] == 0
    catalogue[coffee_index] = catalogue[coffee_index].model_copy(update={"active": True})
    assert ids(await get_feed(client, ha, mode)) == ["coffee"]


async def test_views_reorder_within_chosen_topics_without_reintroducing_unrelated_videos(client):
    ha, _ = await make_pair(client)
    selected = await client.put("/videos/settings/personal", headers=ha,
                               json={"topics": ["animals"], "revision": 0})
    assert selected.status_code == 200
    assert (await client.post("/videos/cat/viewed", headers=ha)).status_code == 200
    assert ids(await get_feed(client, ha)) == ["dog", "cat"]
    assert (await client.post("/videos/dog/viewed", headers=ha)).status_code == 200
    assert set(ids(await get_feed(client, ha))) == {"cat", "dog"}


async def test_shared_filter_uses_only_explicit_shared_topics_and_no_private_feedback(client):
    ha, hb = await make_pair(client)
    for h, topics in ((ha, ["animals"]), (hb, ["travel"])):
        selected = await client.put("/videos/settings/personal", headers=h,
                                   json={"topics": topics, "revision": 0})
        assert selected.status_code == 200
    selected = await client.put("/videos/settings/shared", headers=ha,
                               json={"topics": ["cooking"], "revision": 0})
    assert selected.status_code == 200
    assert set(ids(await get_feed(client, ha))) == {"cat", "dog"}
    partner_personal = await get_feed(client, hb)
    assert ids(partner_personal) == ["mountains"]
    for h in (ha, hb):
        assert ids(await get_feed(client, h, "shared")) == ["coffee"]
    assert (await client.put("/videos/settings/personal", headers=ha,
                             json={"topics": ["films"], "revision": 1})).status_code == 200
    assert (await client.patch("/videos/cat/state", headers=ha,
                               json={"liked": True, "saved": True})).status_code == 200
    assert (await client.post("/videos/coffee/viewed", headers=ha)).status_code == 200
    for h in (ha, hb):
        assert ids(await get_feed(client, h, "shared")) == ["coffee"]
    assert await get_feed(client, hb) == partner_personal


async def test_selected_interests_do_not_restrict_saved_sent_or_directly_opened_videos(client):
    ha, hb = await make_pair(client)
    for h, topics in ((ha, ["travel"]), (hb, ["hobbies"])):
        assert (await client.put("/videos/settings/personal", headers=h,
                                json={"topics": topics, "revision": 0})).status_code == 200
    assert (await client.put("/videos/settings/shared", headers=ha,
                            json={"topics": ["cooking"], "revision": 0})).status_code == 200
    assert (await client.patch("/videos/cat/state", headers=ha,
                              json={"saved": True, "hidden": True})).status_code == 200
    assert ids(await get_feed(client, ha, "saved")) == ["cat"]
    assert ids(await get_feed(client, hb, "saved")) == []
    for h in (ha, hb):
        assert "cat" not in ids(await get_feed(client, h))
        assert "cat" not in ids(await get_feed(client, h, "shared"))
        detail = await client.get("/videos/cat", headers=h)
        assert detail.status_code == 200 and detail.json()["id"] == "cat"
    posted = await client.post("/chat", headers=ha,
                               json={"ref_kind": "video", "ref_id": "cat", "client_message_id": "outside-topics"})
    assert posted.status_code == 201 and posted.json()["ref_id"] == "cat"
    for h in (ha, hb):
        collection = await client.get("/videos/shared", headers=h)
        assert collection.status_code == 200
        assert ids(collection.json()) == ["cat"]


async def test_explicit_feedback_persists_private_and_hidden_save_can_be_recovered(client):
    ha, hb = await make_pair(client)
    push.outbox.clear()
    patched = await client.patch("/videos/cat/state", headers=ha, json={"liked": True, "saved": True})
    assert patched.status_code == 200 and patched.json()["liked"] and patched.json()["saved"]
    assert (await client.patch("/videos/cat/state", headers=ha, json={"liked": True})).json() == patched.json()
    hidden = (await client.patch("/videos/cat/state", headers=ha, json={"hidden": True})).json()
    assert hidden["liked"] and hidden["saved"] and hidden["hidden"]
    assert "cat" not in ids(await get_feed(client, ha))
    assert "cat" not in ids(await get_feed(client, ha, "shared"))
    assert ids(await get_feed(client, ha, "saved")) == ["cat"]
    partner = (await client.get("/videos/cat", headers=hb)).json()
    assert not partner["liked"] and not partner["saved"] and not partner["hidden"]
    assert ids(await get_feed(client, hb, "saved")) == []
    assert (await client.get("/videos/shared", headers=hb)).json()["items"] == []
    assert push.outbox == []
    await client.patch("/videos/cat/state", headers=ha, json={"hidden": False})
    assert "cat" in ids(await get_feed(client, ha))
    for invalid in ({}, {"liked": None}, {"saved": 1}, {"hidden": "true"}, {"mine": True}):
        assert (await client.patch("/videos/cat/state", headers=ha, json=invalid)).status_code == 422


async def test_invalid_or_disabled_catalogue_video_never_creates_feedback_or_chat(client):
    ha, _ = await make_pair(client)
    for video_id in ("missing", "old"):
        assert (await client.get(f"/videos/{video_id}", headers=ha)).status_code == 404
        assert (await client.patch(f"/videos/{video_id}/state", headers=ha, json={"liked": True})).status_code == 404
        assert (await client.post(f"/videos/{video_id}/viewed", headers=ha)).status_code == 404
        assert (await client.post("/chat", headers=ha, json={"ref_kind": "video", "ref_id": video_id})).status_code == 404
    assert (await client.post("/chat", headers=ha, json={"ref_kind": "video"})).status_code == 404
    assert (await client.get("/chat", headers=ha)).json()["messages"] == []
    async with SessionLocal() as session:
        assert not (await session.execute(select(VideoState))).scalars().all()


async def test_shared_collection_requires_explicit_chat_send_and_obeys_delete_and_pair_isolation(client, catalogue):
    ha, hb = await make_pair(client)
    await client.patch("/videos/cat/state", headers=ha, json={"saved": True})
    assert (await client.get("/videos/shared", headers=hb)).json()["items"] == []
    message = {"ref_kind": "video", "ref_id": "cat", "ref_title": "Forged title", "client_message_id": "video-1"}
    posted = await client.post("/chat", headers=ha, json=message)
    assert posted.status_code == 201 and posted.json()["ref_title"] == "Ролик cat"
    assert ids((await client.get("/videos/shared", headers=hb)).json()) == ["cat"]
    await client.post("/chat", headers=hb, json={"ref_kind": "video", "ref_id": "cat"})
    assert ids((await client.get("/videos/shared", headers=ha)).json()) == ["cat"]
    hc, _ = await make_pair(client, "other@example.ru", "another@example.ru")
    assert (await client.get("/videos/shared", headers=hc)).json()["items"] == []
    # Replaying an existing send remains idempotent if its source later disappears.
    catalogue[0] = catalogue[0].model_copy(update={"active": False})
    retry = await client.post("/chat", headers=ha, json=message)
    assert retry.status_code == 201 and retry.json()["id"] == posted.json()["id"]
    assert (await client.get("/videos/shared", headers=hb)).json()["items"] == []
    catalogue[0] = catalogue[0].model_copy(update={"active": True})
    for h in (ha, hb):
        messages = (await client.get("/chat", headers=h)).json()["messages"]
        for entry in messages:
            if entry["mine"]:
                await client.delete(f"/chat/{entry['id']}", headers=h)
    assert (await client.get("/videos/shared", headers=hb)).json()["items"] == []
    assert ids(await get_feed(client, ha, "saved")) == ["cat"]


async def test_private_rows_encrypted_and_viewed_is_daily_idempotent(client):
    h = await login(client, "private@example.ru")
    await client.put("/videos/settings/personal", headers=h, json={"topics": ["animals"], "revision": 0})
    await client.patch("/videos/cat/state", headers=h, json={"saved": True})
    await client.post("/videos/cat/viewed", headers=h)
    async with SessionLocal() as session:
        raw = (await session.execute(text("SELECT topics FROM video_preferences"))).scalar_one()
        state_raw = (await session.execute(text("SELECT data FROM video_states"))).scalar_one()
        assert raw.startswith("enc1:") and "animals" not in raw
        assert state_raw.startswith("enc1:") and "saved" not in state_raw
        data = json.loads((await session.execute(select(VideoState.data))).scalar_one())
        assert data == {"liked": False, "saved": True, "hidden": False, "last_viewed_day": "2026-10-22"}
    await client.post("/videos/cat/viewed", headers=h)
    async with SessionLocal() as session:
        assert (await session.execute(text("SELECT data FROM video_states"))).scalar_one() == state_raw


async def test_pair_end_erases_only_shared_choices_account_delete_erases_personal_choices(client):
    ha, hb = await make_pair(client)
    a, b = await uid(client, ha), await uid(client, hb)
    for h in (ha, hb):
        await client.put("/videos/settings/personal", headers=h, json={"topics": ["animals"], "revision": 0})
        await client.patch("/videos/cat/state", headers=h, json={"saved": True})
    await client.put("/videos/settings/shared", headers=ha, json={"topics": ["travel"], "revision": 0})
    await client.post("/chat", headers=ha, json={"ref_kind": "video", "ref_id": "cat"})
    assert (await client.post("/pairs/leave", headers=ha)).status_code == 200
    async with SessionLocal() as session:
        assert not (await session.execute(select(SharedVideoPreferences))).scalars().all()
        assert set((await session.execute(select(VideoState.user_id))).scalars()) == {a, b}
    settings = (await client.get("/videos/settings", headers=hb)).json()
    assert settings["personal_topics"] == ["animals"] and settings["shared_topics"] == [] and not settings["has_pair"]
    assert (await client.delete("/auth/me", headers=ha)).status_code == 200
    async with SessionLocal() as session:
        assert set((await session.execute(select(VideoState.user_id))).scalars()) == {b}
        assert set((await session.execute(select(VideoPreferences.user_id))).scalars()) == {b}


async def test_concurrent_shared_revisions_and_independent_state_fields_do_not_overwrite(client):
    ha, hb = await make_pair(client)
    results = await asyncio.gather(
        client.put("/videos/settings/shared", headers=ha, json={"topics": ["travel"], "revision": 0}),
        client.put("/videos/settings/shared", headers=hb, json={"topics": ["animals"], "revision": 0}),
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    states = await asyncio.gather(
        client.patch("/videos/cat/state", headers=ha, json={"liked": True}),
        client.patch("/videos/cat/state", headers=ha, json={"saved": True}),
    )
    assert all(response.status_code == 200 for response in states)
    state = (await client.get("/videos/cat", headers=ha)).json()
    assert state["liked"] and state["saved"]


async def test_private_actions_only_invalidate_own_user_shared_topics_invalidate_both(client, monkeypatch):
    ha, hb = await make_pair(client)
    a, b = await uid(client, ha), await uid(client, hb)
    monkeypatch.setattr(get_settings(), "realtime_enabled", True)
    await client.patch("/videos/cat/state", headers=ha, json={"saved": True})
    async with SessionLocal() as session:
        deliveries = (await session.execute(select(DeliveryOutbox).where(DeliveryOutbox.kind == "realtime"))).scalars().all()
        assert deliveries and {row.user_id for row in deliveries} == {a}
        assert all("cat" not in row.payload and "saved" not in row.payload for row in deliveries)
        await session.execute(delete(DeliveryOutbox))
        await session.commit()
    await client.put("/videos/settings/shared", headers=ha, json={"topics": ["animals"], "revision": 0})
    async with SessionLocal() as session:
        deliveries = (await session.execute(select(DeliveryOutbox).where(DeliveryOutbox.kind == "realtime"))).scalars().all()
        assert {row.user_id for row in deliveries} == {a, b}
        assert all("animals" not in row.payload for row in deliveries)


async def test_plan_mapping_resolves_actual_db_id_only_after_mutuality_and_both_consents(client):
    ha, hb = await make_pair(client)
    a, b = await uid(client, ha), await uid(client, hb)
    pair_id = (await client.get("/pairs/me", headers=ha)).json()["pair_id"]
    async with SessionLocal() as session:
        # Deliberately unrelated to the catalogue's source order.
        session.add(WishCard(id=791, deck="home", text="Завтрак в постель", level="warm", active=True))
        await session.commit()
    assert (await client.get("/videos/coffee", headers=ha)).json()["plan_card_id"] is None
    async with SessionLocal() as session:
        session.add(WishSwipe(pair_id=pair_id, user_id=a, card_id=791, choice="yes"))
        await session.commit()
    assert (await client.get("/videos/coffee", headers=hb)).json()["plan_card_id"] is None
    async with SessionLocal() as session:
        session.add(WishSwipe(pair_id=pair_id, user_id=b, card_id=791, choice="yes"))
        await session.commit()
    assert (await client.get("/videos/coffee", headers=ha)).json()["plan_card_id"] == 791
    async with SessionLocal() as session:
        await session.execute(delete(Consent).where(Consent.user_id == b, Consent.kind == "special"))
        await session.commit()
    assert (await client.get("/videos/coffee", headers=ha)).json()["plan_card_id"] is None
    assert (await client.get("/videos/coffee", headers=hb)).json()["plan_card_id"] is None


@pytest.mark.parametrize("field,value", [
    ("media_url", "http://upload.wikimedia.org/video.webm"),
    ("media_url", "https://upload.wikimedia.org.evil.example/video.webm"),
    ("media_url", "https://user:pass@upload.wikimedia.org/video.webm"),
    ("media_url", "https://127.0.0.1/private.webm"),
    ("media_url", "https://upload.wikimedia.org/video.html"),
    ("source_url", "javascript:alert(1)"),
    ("author", "<script>alert(1)</script>"),
    ("topics", ["unreviewed"]),
    ("plan_text", "Not an actual plan"),
])
async def test_catalogue_rejects_unreviewed_targets_or_markup(field, value):
    data = item()
    data[field] = value
    with pytest.raises(ValidationError):
        content.validate_catalogue([data])


async def test_catalogue_rejects_duplicate_identity_and_surfaces_only_populated_topics():
    with pytest.raises(ValueError, match="unique"):
        content.validate_catalogue([item(), item()])
    assert "home" not in {topic["id"] for topic in content.available_topics()}
    assert "films" in {topic["id"] for topic in content.available_topics()}


async def test_shipped_catalogue_has_playable_reviewed_entries_not_the_test_fixture():
    # The API tests deliberately replace catalogue(); CI must also validate
    # the real file shipped inside the API image, independently of that fake.
    shipped = content.validate_catalogue(json.loads(content.CATALOGUE_PATH.read_text(encoding="utf-8")))
    assert any(video.active for video in shipped)
    assert len({topic for video in shipped if video.active for topic in video.topics}) >= 4


async def test_shipped_platform_metadata_uses_canonical_players_and_distinct_stable_ids():
    # The fixture cannot catch a missing file in the image or invalid real
    # metadata. This validates the shipped entries; it makes no playback claim.
    shipped = content.validate_catalogue(json.loads(content.PLATFORM_CATALOGUE_PATH.read_text(encoding="utf-8")))
    legacy = content.validate_catalogue(json.loads(content.CATALOGUE_PATH.read_text(encoding="utf-8")))
    assert shipped and any(video.active for video in shipped)
    assert {video.id for video in shipped}.isdisjoint(video.id for video in legacy)
    assert all(video.playback_kind == "rutube_embed" for video in shipped)
    assert all(video.embed_url == f"https://rutube.ru/play/embed/{video.provider_video_id}" for video in shipped)
    assert all(video.media_url is video.license_name is video.license_url is None for video in shipped)
    assert len({topic for video in shipped if video.active for topic in video.topics}) >= 4


PLATFORM = {"playback": "platform-v1"}


def platform_item(provider_id="1" * 32, topic="cooking", **changes):
    value = dict(id="rutube-" + provider_id, playback_kind="rutube_embed",
                 provider_video_id=provider_id, title="Проверенный урок на RUTUBE",
                 description="Полезный тестовый материал", author="Тестовый автор",
                 source_url=f"https://rutube.ru/video/{provider_id}/", media_url=None,
                 thumbnail_url=None, license_name=None, license_url=None,
                 topics=[topic], language="ru", duration_seconds=400,
                 aspect_ratio=16 / 9, reviewed_at="2026-10-23", active=True)
    value.update(changes)
    return value


@pytest.fixture
def platform_videos(catalogue):
    additions = list(content.validate_catalogue([
        platform_item(), platform_item("2" * 32, "hobbies"),
        platform_item("3" * 32, "science", duration_seconds=60),
    ]))
    catalogue.extend(additions)
    return additions


async def platform_feed(client, headers, mode="personal"):
    response = await client.get("/videos/feed", headers=headers, params={**PLATFORM, "mode": mode})
    assert response.status_code == 200, response.text
    return response.json()


async def test_platform_feed_uses_only_rutube_and_legacy_shape_is_unchanged(client, platform_videos):
    h = await login(client, "platform@example.ru")
    modern = await platform_feed(client, h)
    assert set(ids(modern)) == {video.id for video in platform_videos}
    assert modern["total"] == 3
    for video in modern["items"]:
        assert video["playback_kind"] == "rutube_embed"
        assert video["embed_url"] == "https://rutube.ru/play/embed/" + video["provider_video_id"]
        assert video["source_url"] == "https://rutube.ru/video/" + video["provider_video_id"] + "/"
        assert video["media_url"] is video["license_name"] is video["license_url"] is None
    legacy = await get_feed(client, h)
    assert set(ids(legacy)) == {"cat", "coffee", "mountains", "pottery", "dog", "cartoon"}
    assert all(isinstance(video["media_url"], str) for video in legacy["items"])
    assert not any(set(video) & {"playback_kind", "provider_video_id", "embed_url"} for video in legacy["items"])
    archived = await client.get("/videos/cat", headers=h, params=PLATFORM)
    assert archived.status_code == 200 and archived.json()["playback_kind"] == "direct"
    assert archived.json()["embed_url"] is None and archived.json()["provider_video_id"] is None


async def test_legacy_cannot_mutate_or_parse_new_provider_without_capability(client, platform_videos):
    h = await login(client, "old-device@example.ru")
    video_id = platform_videos[0].id
    for method, suffix, body in (("GET", "", None), ("PATCH", "/state", {"saved": True}),
                                 ("POST", "/viewed", None)):
        response = await client.request(method, f"/videos/{video_id}{suffix}", headers=h, json=body)
        assert response.status_code == 409 and "обнови" in response.json()["detail"]
    for path, method, body in (("/videos/feed", "GET", None),
                               (f"/videos/{video_id}/state", "PATCH", {"saved": True}),
                               (f"/videos/{video_id}/viewed", "POST", None)):
        response = await client.request(method, path, headers=h,
                                         params={"playback": "unreviewed-provider"}, json=body)
        assert response.status_code == 422
    async with SessionLocal() as session:
        assert not (await session.execute(select(VideoState))).scalars().all()


async def test_platform_capability_keeps_consent_and_auth_boundaries(client, platform_videos):
    video_id = platform_videos[0].id
    assert (await client.get("/videos/feed", params=PLATFORM)).status_code == 401
    h = await login(client, "platform-no-consent@example.ru", consent=False)
    assert (await client.get("/videos/settings", headers=h, params=PLATFORM)).status_code == 403
    assert (await client.patch(f"/videos/{video_id}/state", headers=h, params=PLATFORM,
                               json={"saved": True})).status_code == 403
    async with SessionLocal() as session:
        assert not (await session.execute(select(VideoState))).scalars().all()


@pytest.mark.parametrize("mode", ["personal", "shared"])
async def test_platform_topics_are_strict_and_preserve_unavailable_choices(client, catalogue, platform_videos, mode):
    ha, _ = await make_pair(client)
    route = f"/videos/settings/{mode}"
    field = mode + "_topics"
    first = await client.put(route, headers=ha, params=PLATFORM,
                              json={"topics": ["cooking"], "revision": 0})
    assert first.status_code == 200 and first.json()[field] == ["cooking"]
    assert ids(await platform_feed(client, ha, mode)) == [platform_videos[0].id]
    # Withdrawal must not replace the explicitly chosen topic with another
    # provider or unrelated materials, nor silently erase it in settings.
    at = next(index for index, video in enumerate(catalogue) if video.id == platform_videos[0].id)
    catalogue[at] = catalogue[at].model_copy(update={"active": False})
    assert ids(await platform_feed(client, ha, mode)) == []
    settings = (await client.get("/videos/settings", headers=ha, params=PLATFORM)).json()
    assert settings[field] == ["cooking"]
    assert next(topic for topic in settings["topics"] if topic["id"] == "cooking")["available"] is False
    # A known topic with no current platform material remains an honest empty
    # selection, and clearing topics restores only the platform starter feed.
    empty = await client.put(route, headers=ha, params=PLATFORM,
                              json={"topics": ["home"], "revision": 1})
    assert empty.status_code == 200 and empty.json()[field] == ["home"]
    assert ids(await platform_feed(client, ha, mode)) == []
    clear = await client.put(route, headers=ha, params=PLATFORM, json={"topics": [], "revision": 2})
    assert clear.status_code == 200 and clear.json()[field] == []
    assert set(ids(await platform_feed(client, ha, mode))) == {video.id for video in platform_videos[1:]}


async def test_platform_personal_and_shared_settings_remain_separate_and_revision_guarded(client, platform_videos):
    ha, hb = await make_pair(client)
    personal = await client.put("/videos/settings/personal", headers=ha, params=PLATFORM,
                                 json={"topics": ["cooking"], "revision": 0})
    assert personal.status_code == 200
    shared = await client.put("/videos/settings/shared", headers=hb, params=PLATFORM,
                               json={"topics": ["hobbies"], "revision": 0})
    assert shared.status_code == 200 and shared.json()["shared_revision"] == 1
    # An older device still observes and uses the same concurrency revision.
    stale = await client.put("/videos/settings/shared", headers=ha,
                              json={"topics": ["cooking"], "revision": 0})
    assert stale.status_code == 409
    assert ids(await platform_feed(client, ha)) == [platform_videos[0].id]
    assert ids(await platform_feed(client, ha, "shared")) == [platform_videos[1].id]
    assert ids(await platform_feed(client, hb, "shared")) == [platform_videos[1].id]
    assert len(ids(await platform_feed(client, hb))) == 3
    settings = (await client.get("/videos/settings", headers=hb, params=PLATFORM)).json()
    assert settings["personal_topics"] == [] and settings["shared_topics"] == ["hobbies"]


async def test_platform_saved_and_explicit_shared_collections_preserve_old_references(client, platform_videos):
    ha, hb = await make_pair(client)
    video_id = platform_videos[0].id
    await client.patch("/videos/cat/state", headers=ha, json={"saved": True})
    patched = await client.patch(f"/videos/{video_id}/state", headers=ha, params=PLATFORM,
                                  json={"saved": True, "liked": True, "hidden": True})
    assert patched.status_code == 200 and patched.json()["playback_kind"] == "rutube_embed"
    assert patched.json()["saved"] and patched.json()["hidden"]
    assert video_id not in ids(await platform_feed(client, ha))
    assert set(ids(await platform_feed(client, ha, "saved"))) == {"cat", video_id}
    assert ids(await get_feed(client, ha, "saved")) == ["cat"]
    assert ids(await platform_feed(client, hb, "saved")) == []
    assert (await client.get("/videos/shared", headers=hb, params=PLATFORM)).json()["items"] == []
    partner = (await client.get(f"/videos/{video_id}", headers=hb, params=PLATFORM)).json()
    assert not partner["liked"] and not partner["saved"] and not partner["hidden"]
    sent = await client.post("/chat", headers=ha,
                              json={"ref_kind": "video", "ref_id": video_id, "ref_title": "forged"})
    assert sent.status_code == 201 and sent.json()["ref_title"] == platform_videos[0].title
    await client.post("/chat", headers=hb, json={"ref_kind": "video", "ref_id": "cat"})
    assert set(ids((await client.get("/videos/shared", headers=hb, params=PLATFORM)).json())) == {"cat", video_id}
    assert ids((await client.get("/videos/shared", headers=hb)).json()) == ["cat"]
    hc, _ = await make_pair(client, "third@example.ru", "fourth@example.ru")
    assert (await client.get("/videos/shared", headers=hc, params=PLATFORM)).json()["items"] == []
    assert (await client.delete("/chat/" + sent.json()["id"], headers=ha)).status_code == 200
    assert ids((await client.get("/videos/shared", headers=hb, params=PLATFORM)).json()) == ["cat"]
    assert set(ids(await platform_feed(client, ha, "saved"))) == {"cat", video_id}
    unhidden = await client.patch(f"/videos/{video_id}/state", headers=ha, params=PLATFORM, json={"hidden": False})
    assert unhidden.status_code == 200 and video_id in ids(await platform_feed(client, ha))


async def test_platform_viewed_is_daily_and_does_not_reorder_saved_or_leak_to_partner(client, platform_videos):
    ha, hb = await make_pair(client)
    video_id = platform_videos[0].id
    await client.patch(f"/videos/{video_id}/state", headers=ha, params=PLATFORM, json={"saved": True})
    user_id = await uid(client, ha)
    async with SessionLocal() as session:
        before = (await session.get(VideoState, (user_id, video_id))).updated_at
    for _ in range(2):
        response = await client.post(f"/videos/{video_id}/viewed", headers=ha, params=PLATFORM)
        assert response.status_code == 200 and response.json() == {"ok": True}
    async with SessionLocal() as session:
        row = await session.get(VideoState, (user_id, video_id))
        assert row.updated_at == before
        assert json.loads(row.data)["last_viewed_day"] == "2026-10-22"
    assert "last_viewed_day" not in (await client.get(f"/videos/{video_id}", headers=hb, params=PLATFORM)).text


@pytest.mark.parametrize("field,value", [
    ("playback_kind", "vk_unreviewed"),
    ("provider_video_id", "1" * 31),
    ("provider_video_id", "A" * 32),
    ("provider_video_id", "1" * 32 + "?secret=1"),
    ("id", "rutube-" + "2" * 32),
    ("source_url", "https://rutube.ru.evil.example/video/" + "1" * 32 + "/"),
    ("source_url", "https://user:password@rutube.ru/video/" + "1" * 32 + "/"),
    ("source_url", "http://rutube.ru/video/" + "1" * 32 + "/"),
    ("source_url", "https://rutube.ru/video/" + "1" * 32 + "/?redirect=elsewhere"),
    ("embed_url", "https://evil.example/player"),
    ("media_url", "https://rutube.ru/extracted-stream.m3u8"),
    ("license_name", "CC0"),
    ("license_url", "https://creativecommons.org/publicdomain/zero/1.0/"),
    ("thumbnail_url", "https://pic.rutube.ru.evil.example/image.jpg"),
    ("duration_seconds", 7201),
])
async def test_platform_catalogue_rejects_unreviewed_players_and_false_provenance(field, value):
    data = platform_item()
    data[field] = value
    with pytest.raises(ValidationError):
        content.validate_catalogue([data])


async def test_legacy_revision_is_unchanged_by_platform_material_and_new_revision_tracks_it(catalogue):
    legacy_before = content.catalogue_revision()
    modern_before = content.catalogue_revision("platform-v1")
    catalogue.extend(content.validate_catalogue([platform_item()]))
    assert content.catalogue_revision() == legacy_before
    assert content.catalogue_revision("platform-v1") != modern_before
    # Existing clips keep their original maximum; long material is explicit
    # platform playback and cannot accidentally reach an old native decoder.
    data = item()
    data["duration_seconds"] = 181
    with pytest.raises(ValidationError):
        content.validate_catalogue([data])
