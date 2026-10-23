"""Unified discovery, reversible explicit learning and legacy upgrade contracts."""
import asyncio
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app import content_analysis, content_catalogue, recommendations, video_catalogue
from app.db import SessionLocal
from app.deps import today_date
from app.main import app
from app.models import ContentPreferences, ContentState, SharedContentPreferences, VideoState
from conftest import login, make_pair
from test_content import edit_snapshot, feed, ids, publication, sample


def platform(number, title, description, topic="cooking", **extra):
    provider_id = f"{number:032x}"
    return video_catalogue.CatalogueVideo(
        id=f"rutube-{provider_id}", playback_kind="rutube_embed", provider_video_id=provider_id,
        title=title, description=description, author=f"Автор {number}",
        source_url=f"https://rutube.ru/video/{provider_id}/", topics=[topic], language="ru",
        duration_seconds=125, aspect_ratio=16 / 9, reviewed_at="2026-10-23", active=True, **extra,
    )


@pytest.fixture
def unified(tmp_path, monkeypatch):
    videos = [
        platform(0, "Кухонная практика", "Продукты и техника приготовления"),
        platform(1, "Рецепт лимонада", "Лимон вода мёд. Домашний напиток без духовки."),
        platform(2, "Маршрут выходного дня", "Пешая прогулка рядом с рекой", "travel"),
    ]
    legacy = next(video for video in video_catalogue.catalogue() if video.playback_kind == "direct")
    monkeypatch.setattr(video_catalogue, "catalogue", lambda: tuple([*videos, legacy]))
    data = sample()
    data["items"] = [publication("citrus", "food"), publication("route", "travel"),
                     publication("oven", "food")]
    data["items"][0].update(title="Лимонный напиток", summary="Лимон вода мёд: домашний лимонад",
                             blocks=[{"type": "paragraph", "text": "Лимон вода мёд. Рецепт лимонада без духовки."}])
    data["items"][1].update(title="Маршрут прогулки", summary="Пешая прогулка у реки",
                             blocks=[{"type": "paragraph", "text": "Пешая прогулка по берегу реки."}])
    data["items"][2].update(title="Рецепт пирога", summary="Выпечка с духовкой",
                             blocks=[{"type": "paragraph", "text": "Разогрейте духовку и выпекайте пирог."}])
    path = tmp_path / "content.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("VECHER_CONTENT_CATALOGUE", str(path))
    monkeypatch.delenv("VECHER_CONTENT_EMBEDDINGS", raising=False)
    return path, videos, legacy


async def choose(client, h, topics, *, mode="personal", language="ru"):
    current = (await client.get("/content/settings", headers=h)).json()
    response = await client.put(f"/content/settings/{mode}", headers=h,
                                json={"topics": topics, "language": language,
                                      "revision": current[f"{mode}_revision"]})
    assert response.status_code == 200, response.text


async def test_unified_formats_discovery_are_explicit_and_do_not_mutate_preferences(client, unified):
    h = await login(client, "discover@example.ru")
    assert ids(await feed(client, h)) == []
    initial = (await client.get("/content/settings", headers=h)).json()
    assert initial["unified_feed"] is True
    assert len((await feed(client, h, discover=True))["items"]) == 6
    assert (await client.get("/content/settings", headers=h)).json() == initial
    await choose(client, h, ["food"])
    normal = await feed(client, h)
    assert all("food" in item["topics"] for item in normal["items"])
    assert "route" not in ids(normal) and "route" in ids(await feed(client, h, discover=True))
    assert all(item["kind"] == "video" for item in (await feed(client, h, format="video"))["items"])
    assert set(ids(await feed(client, h, format="article"))) == {"citrus", "oven"}
    for params in ({"mode": "saved", "discover": True}, {"format": "anything"}):
        assert (await client.get("/content/feed", headers=h, params=params)).status_code == 422


async def test_personal_feedback_moves_related_video_and_undo_restores_original_order(client, unified):
    _, videos, _ = unified
    h = await login(client, "explicit@example.ru")
    await choose(client, h, ["food"])
    first = ids(await feed(client, h, format="video"))
    assert first == [recommendations.bridge_id(videos[0].id), recommendations.bridge_id(videos[1].id)]
    assert (await client.patch("/content/citrus/state", headers=h, json={"useful": True})).status_code == 200
    selected = await feed(client, h, format="video")
    assert ids(selected)[0] == recommendations.bridge_id(videos[1].id)
    assert "полезными" in selected["items"][0]["recommendation_reason"]
    await client.patch("/content/citrus/state", headers=h, json={"useful": True})
    assert await feed(client, h, format="video") == selected
    await client.patch("/content/citrus/state", headers=h, json={"useful": False})
    assert ids(await feed(client, h, format="video")) == first
    await client.patch("/content/citrus/state", headers=h, json={"saved": True})
    assert ids(await feed(client, h, format="video"))[0] == recommendations.bridge_id(videos[1].id)


async def test_private_learning_does_not_change_shared_rank_reasons_or_partner(client, unified):
    ha, hb = await make_pair(client)
    for h in (ha, hb):
        await choose(client, h, ["food", "travel"])
    await choose(client, ha, ["food", "travel"], mode="shared")
    partner = await feed(client, hb)
    shared = await feed(client, hb, mode="shared")
    await client.patch("/content/citrus/state", headers=ha, json={"useful": True, "saved": True})
    await client.patch("/content/authors/practicum/state", headers=ha, json={"followed": True})
    assert await feed(client, hb) == partner
    assert await feed(client, hb, mode="shared") == shared
    own_shared = await feed(client, ha, mode="shared")
    assert ids(own_shared) == ids(shared)
    assert [item["recommendation_reason"] for item in own_shared["items"]] == [
        item["recommendation_reason"] for item in shared["items"]]


async def test_discovery_keeps_hides_and_language_and_normal_feed_never_broadens(client, unified):
    path, videos, _ = unified
    h = await login(client, "filters@example.ru")
    await choose(client, h, ["food"])
    await client.patch(f"/content/{recommendations.bridge_id(videos[0].id)}/state", headers=h,
                       json={"hidden": True})
    await client.patch("/content/authors/practicum/state", headers=h, json={"hidden": True})
    data = await feed(client, h, discover=True)
    assert "citrus" not in ids(data) and recommendations.bridge_id(videos[0].id) not in ids(data)
    await choose(client, h, ["food"], language="en")
    assert ids(await feed(client, h, discover=True)) == []
    await choose(client, h, ["digital"])
    assert ids(await feed(client, h)) == []
    assert ids(await feed(client, h, discover=True))


async def test_existing_video_interests_project_only_when_unified_row_absent(client, unified):
    ha, hb = await make_pair(client)
    for route, topics in (("personal", ["cooking"]), ("shared", ["travel"])):
        response = await client.put(f"/videos/settings/{route}?playback=platform-v1", headers=ha,
                                    json={"topics": topics, "revision": 0})
        assert response.status_code == 200, response.text
    own = (await client.get("/content/settings", headers=ha)).json()
    partner = (await client.get("/content/settings", headers=hb)).json()
    assert own["personal_topics"] == ["food"] and own["shared_topics"] == ["travel"]
    assert partner["personal_topics"] == [] and partner["shared_topics"] == ["travel"]
    assert all("food" in item["topics"] for item in (await feed(client, ha))["items"])
    assert all("travel" in item["topics"] for item in (await feed(client, hb, mode="shared"))["items"])
    async with SessionLocal() as session:
        assert not (await session.execute(select(ContentPreferences))).scalars().all()
        assert not (await session.execute(select(SharedContentPreferences))).scalars().all()
    await choose(client, ha, [])
    await choose(client, ha, [], mode="shared")
    assert ids(await feed(client, ha)) == ids(await feed(client, hb, mode="shared")) == []
    assert (await client.get("/content/settings", headers=ha)).json()["personal_topics"] == []


async def test_bridge_reuses_saved_state_and_changes_work_from_both_apis(client, unified):
    _, videos, legacy = unified
    h = await login(client, "bridge@example.ru")
    video = videos[1]
    bridge = recommendations.bridge_id(video.id)
    old = f"/videos/{video.id}/state?playback=platform-v1"
    await client.patch(old, headers=h, json={"saved": True, "liked": True})
    saved = await feed(client, h, mode="saved")
    assert ids(saved) == [bridge]
    assert saved["items"][0]["useful"] and saved["items"][0]["duration_seconds"] == 125
    await client.patch(f"/content/{bridge}/state", headers=h, json={"saved": False, "useful": False})
    detail = (await client.get(f"/videos/{video.id}?playback=platform-v1", headers=h)).json()
    assert not detail["saved"] and not detail["liked"]
    await client.patch(f"/content/{bridge}/state", headers=h, json={"saved": True})
    await client.patch(old, headers=h, json={"saved": False})
    assert ids(await feed(client, h, mode="saved")) == []
    await client.patch(f"/videos/{legacy.id}/state", headers=h, json={"saved": True})
    assert ids(await feed(client, h, mode="saved")) == []
    assert legacy.id in ids((await client.get("/videos/feed?mode=saved", headers=h)).json())


async def test_editorial_wrapper_saved_marks_are_deduplicated_and_player_can_undo(client, unified):
    path, videos, _ = unified
    video = videos[1]
    wrapper = publication("existing-editorial", "food", video_id=video.id)
    wrapper["kind"] = "video"
    edit_snapshot(path, lambda data: data["items"].append(wrapper))
    h = await login(client, "wrapper@example.ru")
    user_id = (await client.get("/auth/me", headers=h)).json()["id"]
    async with SessionLocal() as session:
        session.add(ContentState(user_id=user_id, content_id=wrapper["id"],
                                 data=json.dumps({"saved": True, "useful": True, "hidden": False}), revision=1))
        await session.commit()
    assert ids(await feed(client, h, mode="saved")) == [wrapper["id"]]
    direct = (await client.get(f"/videos/{video.id}?playback=platform-v1", headers=h)).json()
    assert direct["saved"] and direct["liked"]
    old_saved = (await client.get("/videos/feed?mode=saved&playback=platform-v1", headers=h)).json()
    assert video.id in ids(old_saved)
    async with SessionLocal() as session:
        row = await session.get(ContentState, (user_id, wrapper["id"]))
        row.data = json.dumps({"saved": True, "useful": True, "hidden": True})
        await session.commit()
    old_personal = (await client.get("/videos/feed?playback=platform-v1", headers=h)).json()
    assert video.id not in ids(old_personal)
    assert video.id in ids((await client.get("/videos/feed?mode=saved&playback=platform-v1", headers=h)).json())
    async with SessionLocal() as session:
        assert await session.get(VideoState, (user_id, video.id)) is None  # GET did not migrate data
    await client.patch(f"/videos/{video.id}/state?playback=platform-v1", headers=h,
                       json={"saved": False, "liked": False, "hidden": False})
    assert ids(await feed(client, h, mode="saved")) == []
    assert not (await client.get(f"/content/{wrapper['id']}", headers=h)).json()["useful"]
    for item_id in (wrapper["id"], recommendations.bridge_id(video.id)):
        assert (await client.get(f"/content/{item_id}", headers=h)).status_code == 200


async def test_old_player_undo_survives_temporarily_invalid_editorial_catalogue(client, unified):
    path, videos, _ = unified
    video = videos[0]
    wrapper = publication("old-wrapper", "food", video_id=video.id)
    wrapper["kind"] = "video"
    edit_snapshot(path, lambda data: data["items"].append(wrapper))
    raw = path.read_bytes()
    h = await login(client, "repair@example.ru")
    user_id = (await client.get("/auth/me", headers=h)).json()["id"]
    async with SessionLocal() as session:
        session.add(ContentState(user_id=user_id, content_id=wrapper["id"],
                                 data=json.dumps({"saved": True, "useful": False, "hidden": False}), revision=1))
        await session.commit()
    path.write_text("broken editorial draft", encoding="utf-8")
    response = await client.patch(f"/videos/{video.id}/state?playback=platform-v1", headers=h,
                                  json={"saved": False})
    assert response.status_code == 200 and not response.json()["saved"]
    path.write_bytes(raw)
    assert ids(await feed(client, h, mode="saved")) == []


async def test_video_state_concurrent_fields_idempotence_and_cursor_filters(client, unified):
    _, videos, _ = unified
    h = await login(client, "concurrent-bridge@example.ru")
    await choose(client, h, ["food", "travel"])
    bridge = recommendations.bridge_id(videos[0].id)
    responses = await asyncio.gather(
        client.patch(f"/content/{bridge}/state", headers=h, json={"saved": True}),
        client.patch(f"/videos/{videos[0].id}/state?playback=platform-v1", headers=h, json={"liked": True}),
    )
    assert [response.status_code for response in responses] == [200, 200]
    detail = (await client.get(f"/content/{bridge}", headers=h)).json()
    assert detail["saved"] and detail["useful"]
    first = await feed(client, h, limit=2)
    await client.patch(f"/content/{bridge}/state", headers=h, json={"saved": True, "useful": True})
    second = await feed(client, h, limit=2, cursor=first["next_cursor"])
    assert not set(ids(first)).intersection(ids(second))
    for params in ({"format": "video"}, {"discover": True}):
        response = await client.get("/content/feed", headers=h,
                                    params={"limit": 2, "cursor": first["next_cursor"], **params})
        assert response.status_code == 409
    for value in (False, True):
        await client.patch(f"/videos/{videos[0].id}/state?playback=platform-v1", headers=h,
                           json={"liked": value})
    stale = await client.get("/content/feed", headers=h,
                             params={"limit": 2, "cursor": first["next_cursor"]})
    assert stale.status_code == 409


async def test_search_understands_inflections_but_keeps_negative_constraints(client, unified):
    h = await login(client, "search@example.ru")
    await choose(client, h, ["food"])
    result = await feed(client, h, q="как сделать рецепт без духовки")
    assert "oven" not in ids(result)
    assert "citrus" in ids(result)
    assert all("поиском" in item["recommendation_reason"] for item in result["items"])
    assert ids(await feed(client, h, q="банановая подлодка")) == []
    assert ids(await feed(client, h, q="прогулка")) == []  # search does not broaden topics


async def test_configured_semantic_index_changes_rank_and_invalidates_cursor(client, unified, tmp_path, monkeypatch):
    _, videos, _ = unified
    h = await login(client, "semantic@example.ru")
    await choose(client, h, ["food"])
    await client.patch("/content/citrus/state", headers=h, json={"useful": True})
    lexical = await feed(client, h, format="video", limit=1)
    assert ids(lexical) == [recommendations.bridge_id(videos[1].id)]
    snapshot = recommendations.with_reviewed_videos(content_catalogue.catalogue())
    items = recommendations.unique_items(content_catalogue.published(snapshot))
    index = {"version": 1, "model_id": "test-explicit-v1", "analyzer_version": content_analysis.ANALYZER_VERSION,
             "dimensions": 2, "items": {}}
    for item in items:
        vector = [1.0, 0.0] if item.id in {"citrus", recommendations.bridge_id(videos[0].id)} else [0.0, 1.0]
        index["items"][item.id] = {"text_fingerprint": content_analysis.analyze_content(item).text_fingerprint,
                                  "vector": vector}
    path = tmp_path / "public-vectors.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setenv("VECHER_CONTENT_EMBEDDINGS", str(path))
    semantic = await feed(client, h, format="video", limit=1)
    assert ids(semantic) == [recommendations.bridge_id(videos[0].id)]
    stale = await client.get("/content/feed", headers=h,
                             params={"format": "video", "limit": 1, "cursor": lexical["next_cursor"]})
    assert stale.status_code == 409
    path.write_text("broken public cache", encoding="utf-8")
    assert ids(await feed(client, h, format="video", limit=1)) == ids(lexical)


async def test_viewed_marker_never_changes_unified_ranking(client, unified):
    _, videos, _ = unified
    h = await login(client, "no-watch-profile@example.ru")
    await choose(client, h, ["food"])
    before = await feed(client, h, limit=1)
    await client.post(f"/videos/{videos[0].id}/viewed?playback=platform-v1", headers=h)
    assert await feed(client, h, limit=1) == before
    assert (await client.get("/content/feed", headers=h,
                            params={"limit": 1, "cursor": before["next_cursor"]})).status_code == 200
    await client.patch(f"/content/{recommendations.bridge_id(videos[0].id)}/state", headers=h,
                       json={"saved": True})
    explicit_before = await feed(client, h, limit=1)
    app.dependency_overrides[today_date] = lambda: date(2026, 10, 24)
    await client.post(f"/videos/{videos[0].id}/viewed?playback=platform-v1", headers=h)
    assert await feed(client, h, limit=1) == explicit_before


async def test_view_only_row_does_not_reorder_editorial_saved_alias_or_cursor(client, unified):
    path, videos, _ = unified
    video = videos[0]
    wrapper = publication("saved-wrapper", "food", video_id=video.id)
    wrapper["kind"] = "video"
    edit_snapshot(path, lambda data: data["items"].append(wrapper))
    h = await login(client, "saved-view@example.ru")
    user_id = (await client.get("/auth/me", headers=h)).json()["id"]
    async with SessionLocal() as session:
        for day, item_id in ((21, wrapper["id"]), (22, "citrus")):
            session.add(ContentState(user_id=user_id, content_id=item_id,
                                     data=json.dumps({"saved": True, "useful": False, "hidden": False}),
                                     updated_at=datetime(2026, 10, day, tzinfo=timezone.utc), revision=1))
        await session.commit()
    before = await feed(client, h, mode="saved", limit=1)
    assert ids(before) == ["citrus"]
    await client.post(f"/videos/{video.id}/viewed?playback=platform-v1", headers=h)
    assert await feed(client, h, mode="saved", limit=1) == before
    second = await feed(client, h, mode="saved", limit=1, cursor=before["next_cursor"])
    assert ids(second) == [wrapper["id"]]
