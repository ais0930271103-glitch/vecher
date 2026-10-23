"""Refresh explicitly registered author JSON feeds into review-only drafts.

This module is an administrator/worker CLI, never a public URL-fetch endpoint.
It does not scrape platforms, fetch media, publish drafts, or inspect user data.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .content_analysis import analyze_content, normalize_topic
from .content_catalogue import (Catalogue, EditorialModel, Id, Publication,
                                _unique_object, catalogue, public_https,
                                read_snapshot, validate_bytes)
from .publish_content import _publication_lock

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_ITEMS = 100
MAX_DRAFT_BYTES = 8 * 1024 * 1024
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class AuthorFeedSource(EditorialModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,19}$")
    url: str = Field(min_length=1, max_length=2000)
    author_id: Id
    signing_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{1,99}$")
    _url = field_validator("url")(public_https)

    @model_validator(mode="after")
    def no_fragment(self):
        if urlsplit(self.url).fragment:
            raise ValueError("Feed URLs must not contain fragments")
        return self


class FeedConfiguration(EditorialModel):
    version: Literal[1]
    sources: list[AuthorFeedSource] = Field(default_factory=list, max_length=20)
    timeout_seconds: int = Field(default=15, strict=True, ge=1, le=30)

    @model_validator(mode="after")
    def unique_sources(self):
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be unique")
        return self


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class DraftCandidate(EditorialModel):
    id: Id
    source_id: str
    source_item_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication: dict[str, Any]
    analysis: dict[str, Any]
    status: Literal["pending"] = "pending"

    @model_validator(mode="after")
    def unreviewed(self):
        if "reviewed_at" in self.publication or "active" in self.publication:
            raise ValueError("A draft cannot contain publication approval")
        if self.publication.get("id") != self.id or _canonical_hash(self.publication) != self.content_hash:
            raise ValueError("Draft identity/content hash mismatch")
        # Temporary values exist only to reuse structural validation; they are
        # never exported or represented as an actual editorial review.
        Publication.model_validate({**self.publication, "active": False, "reviewed_at": "0001-01-01"})
        return self


class SourceRefresh(EditorialModel):
    source_id: str
    checked_at: datetime
    staged: int = Field(ge=0, le=MAX_FEED_ITEMS)
    error: str | None = None


class DraftBundle(EditorialModel):
    version: Literal[1] = 1
    drafts: list[DraftCandidate] = Field(default_factory=list, max_length=5000)
    sources: list[SourceRefresh] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({draft.id for draft in self.drafts}) != len(self.drafts):
            raise ValueError("Duplicate draft IDs")
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("Duplicate source status")
        return self


@dataclass(frozen=True)
class FeedResponse:
    body: bytes
    headers: dict[str, str]


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Author feed deadline exceeded")
    return remaining


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified
                                 or ip.is_loopback or ip.is_link_local or ip.is_private)


def _resolve_public(host: str, deadline: float) -> tuple[str, ...]:
    """Bound DNS waiting; pin the verified address for the following TLS socket."""
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("Author feed DNS resolver busy")
    result: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put((True, socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except Exception as exc:
            result.put((False, exc))
        finally:
            _DNS_SLOTS.release()

    # A stalled resolver cannot hold up shutdown or create unbounded workers.
    threading.Thread(target=resolve, daemon=True).start()
    try:
        okay, value = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TimeoutError("Author feed DNS deadline exceeded") from None
    if not okay:
        raise ValueError("Author feed hostname could not be resolved") from None
    addresses = tuple(dict.fromkeys(entry[4][0] for entry in value))
    if not addresses or not all(_is_public(address) for address in addresses):
        raise ValueError("Author feed resolved to a non-public address")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, deadline: float):
        super().__init__(host, port=443, timeout=_remaining(deadline), context=ssl.create_default_context())
        self._address = address
        self._deadline = deadline
        self._deadline_guard: socket.socket | None = None
        self._deadline_timer: threading.Timer | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, 443), timeout=_remaining(self._deadline))
        try:
            self.sock.settimeout(_remaining(self._deadline))
            # Validate the certificate and SNI against the registered hostname,
            # not the pinned address. No second hostname resolution occurs.
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
            # A peer dripping HTTP header/body bytes must not reset the total
            # deadline. Retain a duplicate descriptor so the watchdog can shut
            # down the connection even after HTTPResponse owns its file handle.
            self._deadline_guard = socket.socket(self.sock.family, self.sock.type,
                                                  self.sock.proto, fileno=os.dup(self.sock.fileno()))
            self._deadline_timer = threading.Timer(_remaining(self._deadline), self._expire)
            self._deadline_timer.daemon = True
            self._deadline_timer.start()
        except Exception:
            self.sock.close()
            self.sock = None
            raise

    def _expire(self) -> None:
        try:
            if self._deadline_guard is not None:
                self._deadline_guard.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self) -> None:
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
        if self._deadline_guard is not None:
            self._deadline_guard.close()
        self.close()


def fetch_source(source: AuthorFeedSource, timeout_seconds: int = 15) -> FeedResponse:
    parsed = urlsplit(source.url)
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    deadline = time.monotonic() + timeout_seconds
    addresses = _resolve_public(host, deadline)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last_error: Exception | None = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(host, address, deadline)
        try:
            connection.request("GET", path, headers={"Accept": "application/json",
                               "Accept-Encoding": "identity", "User-Agent": "Vecher-AuthorFeed/1.0"})
            connection.sock.settimeout(_remaining(deadline))
            response = connection.getresponse()
            # Redirects are rejected, not resolved or followed.
            if response.status != 200:
                raise ValueError(f"Author feed returned HTTP {response.status}; only 200 is accepted")
            headers = {key.lower(): value for key, value in response.getheaders()}
            if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("Author feed must have application/json Content-Type")
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("Compressed author feeds are not accepted")
            if "content-length" in headers and not 0 <= int(headers["content-length"]) <= MAX_FEED_BYTES:
                raise ValueError("Author feed exceeds size limit")
            chunks, size = [], 0
            while True:
                _remaining(deadline)
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                chunk = response.read1(min(64 * 1024, MAX_FEED_BYTES + 1 - size))
                _remaining(deadline)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise ValueError("Author feed exceeds size limit")
            return FeedResponse(b"".join(chunks), headers)
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.finish()
    raise ValueError("Author feed connection failed") from last_error


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON constants are not accepted")


def _json(raw: bytes, limit: int) -> Any:
    if len(raw) > limit:
        raise ValueError("JSON size limit exceeded")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError("JSON nesting exceeds safe limits") from None


def parse_author_feed(response: FeedResponse, source: AuthorFeedSource,
                      snapshot: Catalogue) -> list[DraftCandidate]:
    if len(response.body) > MAX_FEED_BYTES:
        raise ValueError("Author feed exceeds size limit")
    if source.author_id not in {author.id for author in snapshot.authors}:
        raise ValueError("Register the source author in the editorial catalogue first")
    if source.signing_key_env:
        key = os.environ.get(source.signing_key_env, "")
        if len(key.encode("utf-8")) < 32:
            raise ValueError("Configured author-feed signing key is missing or too short")
        expected = "sha256=" + hmac.new(key.encode("utf-8"), response.body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(response.headers.get("x-vecher-signature", ""), expected):
            raise ValueError("Author feed signature is invalid")
    feed = _json(response.body, MAX_FEED_BYTES)
    if (not isinstance(feed, dict) or set(feed) != {"version", "items"}
            or type(feed["version"]) is not int or feed["version"] != 1
            or not isinstance(feed["items"], list) or len(feed["items"]) > MAX_FEED_ITEMS):
        raise ValueError("Author feed needs version 1 and at most 100 items")
    seen, drafts = set(), []
    known_topics = {topic.id for topic in snapshot.topics}
    for raw in feed["items"]:
        if not isinstance(raw, dict) or {"reviewed_at", "active", "author_id"} & raw.keys():
            raise ValueError("A source cannot supply editorial approval or change its author")
        original_id = raw.get("id")
        if not isinstance(original_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", original_id):
            raise ValueError("Source items need stable slug IDs")
        if original_id in seen:
            raise ValueError("Duplicate source item ID")
        seen.add(original_id)
        candidate_id = f"feed-{source.id}-{original_id[:24]}-{hashlib.sha256(original_id.encode()).hexdigest()[:8]}"
        proposed = dict(raw)
        incoming_topics = raw.get("topics", [])
        if not isinstance(incoming_topics, list) or not all(isinstance(topic, str) for topic in incoming_topics):
            raise ValueError("Source topics must be strings")
        topics = list(dict.fromkeys(normalize_topic(topic) for topic in incoming_topics))
        if topics and not set(topics).issubset(known_topics):
            raise ValueError("Source item refers to an unregistered topic")
        if not topics:
            topics = [topic for topic in analyze_content(raw).topics if topic in known_topics]
        if not topics:
            raise ValueError("No supported topic evidence; author/editor must assign a topic")
        proposed.update(id=candidate_id, author_id=source.author_id, topics=topics,
                        active=False, reviewed_at="0001-01-01")
        item = Publication.model_validate(proposed)
        publication = item.model_dump(mode="json", exclude={"reviewed_at", "active"})
        drafts.append(DraftCandidate(id=candidate_id, source_id=source.id,
                      source_item_id=original_id, source_sha256=hashlib.sha256(response.body).hexdigest(),
                      content_hash=_canonical_hash(publication), publication=publication,
                      analysis=analyze_content(item).to_dict()))
    return drafts


def _write_json(value: Any, destination: Path) -> None:
    if destination.is_symlink() or not destination.parent.is_dir():
        raise ValueError("Use an existing managed directory and a regular destination")
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_DRAFT_BYTES:
        raise ValueError("Draft/review output exceeds size limit")
    fd, name = tempfile.mkstemp(prefix=".content-drafts-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def read_drafts(path: Path) -> DraftBundle:
    return DraftBundle.model_validate(_json(read_snapshot(path)[0], MAX_DRAFT_BYTES))


def refresh_once(config: FeedConfiguration, snapshot: Catalogue, destination: Path,
                 *, fetcher: Callable = fetch_source) -> DraftBundle:
    candidates, statuses = [], []
    for source in config.sources:
        try:
            drafts = parse_author_feed(fetcher(source, config.timeout_seconds), source, snapshot)
            candidates.extend(drafts)
            statuses.append(SourceRefresh(source_id=source.id, checked_at=datetime.now(timezone.utc),
                                          staged=len(drafts)))
        except Exception as exc:
            # No response bodies, query credentials, signing keys, or verbose
            # validator input values are written to worker logs/status files.
            statuses.append(SourceRefresh(source_id=source.id, checked_at=datetime.now(timezone.utc),
                                          staged=0, error=f"Refresh rejected ({type(exc).__name__})"))
    with _publication_lock(destination.parent / ("." + destination.name + ".refresh.lock")):
        previous = read_drafts(destination) if destination.exists() else DraftBundle()
        existing = {draft.id: draft for draft in previous.drafts}
        existing.update({draft.id: draft for draft in candidates})
        # Removing a source never withdraws already published content. Pending
        # drafts of removed sources are dropped from this managed queue.
        configured = {source.id for source in config.sources}
        bundle = DraftBundle(drafts=[draft for _, draft in sorted(existing.items())
                                    if draft.source_id in configured], sources=statuses)
        _write_json(bundle.model_dump(mode="json"), destination)
    return bundle


def export_reviewed(snapshot: Catalogue, bundle: DraftBundle, approvals: list[str],
                    reviewed_at: date) -> Catalogue:
    """Prepare a candidate snapshot for the existing atomic publication CLI.

    Every approval binds the exact inspected draft ID and SHA-256 content hash.
    This function neither writes the live catalogue nor clears review drafts.
    """
    if not approvals or reviewed_at > datetime.now(timezone.utc).date():
        raise ValueError("Explicit draft approvals and a non-future review date are required")
    available = {draft.id: draft for draft in bundle.drafts}
    selected, seen = [], set()
    for token in approvals:
        candidate_id, separator, digest = token.partition(":")
        if not separator or not _DIGEST.fullmatch(digest) or candidate_id in seen:
            raise ValueError("Approval must be one unique draft-id:content-sha256")
        seen.add(candidate_id)
        draft = available.get(candidate_id)
        if draft is None or not hmac.compare_digest(draft.content_hash, digest):
            raise ValueError("Draft changed or disappeared; review the exact candidate again")
        selected.append(Publication.model_validate({**draft.publication, "active": True,
                                                    "reviewed_at": reviewed_at}))
    merged = {item.id: item for item in snapshot.items}
    merged.update({item.id: item for item in selected})
    return Catalogue.model_validate({**snapshot.model_dump(mode="json"),
                                     "items": [item.model_dump(mode="json") for item in merged.values()]})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="Fetch registered sources into review-only drafts")
    refresh.add_argument("--config", required=True, type=Path)
    refresh.add_argument("--drafts", required=True, type=Path)
    refresh.add_argument("--interval-seconds", type=int, default=0,
                         help="0: one run; otherwise at least 300 seconds between completed runs")
    review = commands.add_parser("review-export", help="Export exact manually reviewed drafts")
    review.add_argument("--drafts", required=True, type=Path)
    review.add_argument("--approve", action="append", required=True, help="draft-id:content-sha256")
    review.add_argument("--reviewed-at", type=date.fromisoformat, required=True)
    review.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "refresh":
        if args.interval_seconds != 0 and args.interval_seconds < 300:
            parser.error("--interval-seconds must be 0 or at least 300")
        while True:
            config = FeedConfiguration.model_validate(_json(read_snapshot(args.config)[0], 128 * 1024))
            bundle = refresh_once(config, catalogue(), args.drafts)
            failures = sum(status.error is not None for status in bundle.sources)
            print(f"CONTENT_SOURCES={len(bundle.sources)};PENDING_DRAFTS={len(bundle.drafts)};FAILED={failures}", flush=True)
            if args.interval_seconds == 0:
                if failures:
                    raise SystemExit(1)
                return
            time.sleep(args.interval_seconds)
    else:
        snapshot = catalogue()
        candidate = export_reviewed(snapshot, read_drafts(args.drafts), args.approve, args.reviewed_at)
        # Validate the exact written representation against runtime size rules.
        data = candidate.model_dump(mode="json")
        validate_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        _write_json(data, args.output)
        print(f"CONTENT_REVIEW_EXPORTED={candidate.revision};EXPECTED_PREVIOUS_REVISION={snapshot.revision}")


if __name__ == "__main__":
    main()
