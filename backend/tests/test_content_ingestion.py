"""Allowed author ingestion cannot publish, escape to local hosts or self-approve."""
import hashlib
import hmac
import json
import socket
import time
from datetime import date

import pytest
from pydantic import ValidationError

from app import content_catalogue
from app import content_ingestion as ingestion
from app.publish_content import publish, validate_source


def source(**kwargs):
    return ingestion.AuthorFeedSource(id="author", url="https://author.example.org/feed.json",
                                     author_id="practicum", **kwargs)


def item(**kwargs):
    return {"id": "dinner", "kind": "guide", "title": "Рецепт простого ужина",
            "summary": "Ингредиенты и приготовление", "topics": ["cooking"], "language": "ru",
            "blocks": [{"type": "paragraph", "text": "Приготовьте ингредиенты для ужина."}],
            "published_at": "2026-10-22T09:00:00Z", "updated_at": "2026-10-22T09:00:00Z",
            "reading_minutes": 2, **kwargs}


def response(items=None):
    return ingestion.FeedResponse(json.dumps({"version": 1, "items": items or [item()]},
                                             ensure_ascii=False).encode(), {})


def snapshot():
    return content_catalogue.Catalogue.model_validate({"version": 1,
            "topics": [{"id": "food", "title": "Еда и кухня"}],
            "authors": [{"id": "practicum", "name": "Практикум", "bio": "Тестовая редакция"}],
            "items": []})


def test_sources_require_public_https_and_no_redirectable_credentials():
    for url in ("http://author.example.org/feed.json", "https://127.0.0.1/a", "https://[::1]/a",
                "https://user:password@author.example.org/a", "https://metadata.internal/a",
                "https://author.example.org:444/a", "https://author.example.org/a#fragment"):
        with pytest.raises(ValidationError):
            ingestion.AuthorFeedSource(id="test", url=url, author_id="practicum")


def test_dns_mixed_public_private_addresses_are_rejected_before_connect(monkeypatch):
    def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                for address in ("8.8.8.8", "169.254.169.254")]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(ValueError, match="non-public"):
        ingestion._resolve_public("author.example.org", time.monotonic() + 1)
    assert not ingestion._is_public("::ffff:127.0.0.1")
    assert not ingestion._is_public("224.0.0.1")


def test_http_redirects_compression_and_oversize_responses_are_rejected(monkeypatch):
    monkeypatch.setattr(ingestion, "_resolve_public", lambda *args: ("8.8.8.8",))

    class FakeSocket:
        def settimeout(self, value):
            pass

    class FakeResponse:
        status = 302
        headers = [("Content-Type", "application/json"), ("Location", "https://127.0.0.1/")]

        def getheaders(self):
            return self.headers

    class FakeConnection:
        calls = 0

        def __init__(self, host, address, deadline):
            assert host == "author.example.org" and address == "8.8.8.8"
            self.sock = FakeSocket()
            FakeConnection.calls += 1

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return FakeResponse()

        def finish(self):
            pass

    monkeypatch.setattr(ingestion, "_PinnedHTTPSConnection", FakeConnection)
    with pytest.raises(ValueError, match="HTTP 302"):
        ingestion.fetch_source(source())
    assert FakeConnection.calls == 1
    FakeResponse.status = 200
    FakeResponse.headers = [("Content-Type", "application/json"), ("Content-Encoding", "gzip")]
    with pytest.raises(ValueError, match="Compressed"):
        ingestion.fetch_source(source())
    FakeResponse.headers = [("Content-Type", "application/json"),
                            ("Content-Length", str(ingestion.MAX_FEED_BYTES + 1))]
    with pytest.raises(ValueError, match="size limit"):
        ingestion.fetch_source(source())


def test_feed_validates_signature_schema_and_cannot_self_approve(monkeypatch):
    snap = snapshot()
    signed_source = source(signing_key_env="VECHER_AUTHOR_KEY")
    monkeypatch.setenv("VECHER_AUTHOR_KEY", "a" * 32)
    raw = response()
    with pytest.raises(ValueError, match="signature"):
        ingestion.parse_author_feed(raw, signed_source, snap)
    signature = "sha256=" + hmac.new(b"a" * 32, raw.body, hashlib.sha256).hexdigest()
    signed = ingestion.FeedResponse(raw.body, {"x-vecher-signature": signature})
    drafts = ingestion.parse_author_feed(signed, signed_source, snap)
    assert drafts[0].publication["topics"] == ["food"]
    assert drafts[0].status == "pending"
    assert "active" not in drafts[0].publication and "reviewed_at" not in drafts[0].publication
    for field, value in (("active", True), ("reviewed_at", "2026-10-23"), ("author_id", "forged")):
        with pytest.raises(ValueError, match="approval"):
            ingestion.parse_author_feed(response([item(**{field: value})]), source(), snap)
    with pytest.raises(ValueError, match="Duplicate source item"):
        ingestion.parse_author_feed(response([item(), item()]), source(), snap)
    with pytest.raises(ValueError, match="Duplicate JSON"):
        ingestion.parse_author_feed(ingestion.FeedResponse(b'{"version":1,"version":1,"items":[]}', {}), source(), snap)


def test_automatic_topic_classification_requires_actual_evidence():
    inferred = ingestion.parse_author_feed(response([item(topics=[])]), source(), snapshot())
    assert inferred[0].publication["topics"] == ["food"]
    with pytest.raises(ValueError, match="No supported topic evidence"):
        ingestion.parse_author_feed(response([item(topics=[], title="Материал", summary="Описание",
                              blocks=[{"type": "paragraph", "text": "Небольшой текст."}])]), source(), snapshot())


def test_refresh_is_idempotent_isolates_failure_and_does_not_publish(tmp_path):
    snap = snapshot()
    config = ingestion.FeedConfiguration(version=1, sources=[source()])
    drafts_path = tmp_path / "drafts.json"
    first = ingestion.refresh_once(config, snap, drafts_path, fetcher=lambda *args: response())
    second = ingestion.refresh_once(config, snap, drafts_path, fetcher=lambda *args: response())
    assert len(first.drafts) == len(second.drafts) == 1
    assert first.drafts[0].content_hash == second.drafts[0].content_hash
    assert snap.revision == snapshot().revision
    assert first.drafts[0].id not in {published.id for published in snap.items}

    def fail(*args):
        raise RuntimeError("secret response body is not logged")

    failed = ingestion.refresh_once(config, snap, drafts_path, fetcher=fail)
    assert failed.drafts == second.drafts
    assert failed.sources[0].error == "Refresh rejected (RuntimeError)"
    assert "secret response" not in drafts_path.read_text()


def test_only_exact_reviewed_draft_exports_and_existing_publisher_validates(tmp_path):
    snap = snapshot()
    draft = ingestion.parse_author_feed(response(), source(), snap)[0]
    bundle = ingestion.DraftBundle(drafts=[draft])
    with pytest.raises(ValueError, match="Explicit"):
        ingestion.export_reviewed(snap, bundle, [], date(2026, 10, 23))
    with pytest.raises(ValueError, match="changed"):
        ingestion.export_reviewed(snap, bundle, [f"{draft.id}:{'0' * 64}"], date(2026, 10, 23))
    candidate = ingestion.export_reviewed(snap, bundle,
                     [f"{draft.id}:{draft.content_hash}"], date(2026, 10, 23))
    accepted = next(item for item in candidate.items if item.id == draft.id)
    assert accepted.active and accepted.reviewed_at == date(2026, 10, 23)
    proposed = tmp_path / "reviewed.json"
    proposed.write_text(candidate.model_dump_json(), encoding="utf-8")
    assert validate_source(proposed).revision == candidate.revision
    live = tmp_path / "live.json"
    live.write_text(snap.model_dump_json(), encoding="utf-8")
    assert publish(proposed, live, expected_revision=snap.revision).revision == candidate.revision
    assert draft.status == "pending"  # review/export never mutates source drafts
