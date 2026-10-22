"""Validated public editorial snapshot, independent of private couple data.

Only an administrator-selected local file is read. No URL is fetched by the
API. The atomic publisher validates before replacing this file; workers detect
the new inode without a restart. A corrupt selected file fails closed.
"""
import hashlib
import ipaddress
import json
import os
import stat
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_PATH = Path(__file__).parent / "content" / "publications.json"
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
Id = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
LANGUAGE_TITLES = {"ru": "Русский", "en": "English", "none": "Без слов"}


def plain(value: str) -> str:
    if (not value.strip() or value != value.strip() or "<" in value or ">" in value
            or any(ord(c) < 32 and c not in "\n\t" for c in value)):
        raise ValueError("Editorial text must be non-empty plain text")
    return value


def public_https(value: str) -> str:
    """External attribution only; never a media/authenticated request target."""
    try:
        p = urlsplit(value)
        host = (p.hostname or "").encode("idna").decode("ascii").lower()
        port = p.port
    except (ValueError, UnicodeError) as e:
        raise ValueError("Invalid source URL") from e
    if (p.scheme != "https" or p.username is not None or p.password is not None
            or port not in (None, 443) or not host or "." not in host
            or host.endswith((".localhost", ".local", ".internal"))
            or host.rsplit(".", 1)[-1].isdigit()
            or any(ord(c) <= 32 or c == "\\" for c in value)):
        raise ValueError("Use a public HTTPS source without credentials")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP literals are not editorial sources")
    if not all(part and len(part) <= 63 and all(c.isalnum() or c == "-" for c in part)
               and not part.startswith("-") and not part.endswith("-") for part in host.split(".")):
        raise ValueError("Invalid source hostname")
    return value


class EditorialModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Topic(EditorialModel):
    id: Id
    title: str = Field(min_length=1, max_length=100)
    _title = field_validator("title")(plain)


class Author(EditorialModel):
    id: Id
    name: str = Field(min_length=1, max_length=120)
    bio: str = Field(min_length=1, max_length=1600)
    url: str | None = Field(default=None, max_length=2000)
    _text = field_validator("name", "bio")(plain)

    @field_validator("url")
    @classmethod
    def source(cls, value):
        return public_https(value) if value is not None else None


class TextBlock(EditorialModel):
    type: Literal["paragraph", "heading"]
    text: str = Field(min_length=1, max_length=5000)
    _text = field_validator("text")(plain)


class ListBlock(EditorialModel):
    type: Literal["list"]
    items: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(min_length=1, max_length=40)

    @field_validator("items")
    @classmethod
    def text(cls, values):
        return [plain(v) for v in values]


Block = Annotated[TextBlock | ListBlock, Field(discriminator="type")]


class Source(EditorialModel):
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    _title = field_validator("title")(plain)
    _url = field_validator("url")(public_https)


class Publication(EditorialModel):
    id: Id
    kind: Literal["post", "guide", "video"]
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=800)
    author_id: Id
    topics: list[Id] = Field(min_length=1, max_length=6)
    language: Literal["ru", "en", "none"] = "ru"
    blocks: list[Block] = Field(min_length=1, max_length=60)
    sources: list[Source] = Field(default_factory=list, max_length=12)
    published_at: datetime
    updated_at: datetime
    reviewed_at: date
    active: bool = Field(strict=True)
    reading_minutes: int = Field(strict=True, ge=1, le=60)
    series_id: Id | None = None
    series_title: str | None = Field(default=None, min_length=1, max_length=160)
    series_order: int | None = Field(default=None, strict=True, ge=1, le=1000)
    video_id: Id | None = None
    _text = field_validator("title", "summary")(plain)

    @field_validator("id")
    @classmethod
    def routable_id(cls, value):
        if value in {"settings", "feed", "authors"}:
            raise ValueError("Publication ID conflicts with a reserved API route")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if len(self.topics) != len(set(self.topics)):
            raise ValueError("Repeated topics")
        if self.published_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise ValueError("Publication timestamps need a timezone")
        if self.updated_at < self.published_at:
            raise ValueError("Updated time precedes publication")
        series = (self.series_id, self.series_title, self.series_order)
        if any(v is not None for v in series) and not all(v is not None for v in series):
            raise ValueError("Complete series identity/title/order together")
        if self.series_title is not None:
            plain(self.series_title)
        if (self.kind == "video") != (self.video_id is not None):
            raise ValueError("Only explicit video publications refer to a legacy video ID")
        return self


class Catalogue(EditorialModel):
    version: int = Field(strict=True, ge=1, le=1)
    topics: list[Topic] = Field(min_length=1, max_length=80)
    authors: list[Author] = Field(min_length=1, max_length=500)
    items: list[Publication] = Field(max_length=5000)

    @model_validator(mode="after")
    def references(self):
        for values in (self.topics, self.authors, self.items):
            if len({v.id for v in values}) != len(values):
                raise ValueError("Duplicate editorial ID")
        topics = {t.id for t in self.topics}
        authors = {a.id for a in self.authors}
        series = {}
        positions = set()
        for item in self.items:
            if item.author_id not in authors or not set(item.topics).issubset(topics):
                raise ValueError("Unknown author or topic")
            if item.series_id:
                identity = (item.author_id, item.series_title)
                if series.setdefault(item.series_id, identity) != identity:
                    raise ValueError("Series author and title must be consistent")
                position = (item.series_id, item.series_order)
                if position in positions:
                    raise ValueError("Duplicate series position")
                positions.add(position)
        return self

    @property
    def revision(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def validate_bytes(raw: bytes) -> Catalogue:
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Editorial snapshot exceeds size limit")
    try:
        return Catalogue.model_validate(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))
    except RecursionError:
        raise ValueError("Editorial snapshot nesting exceeds safe limits") from None


def read_snapshot(path: Path) -> tuple[bytes, tuple]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("Editorial snapshot must be a bounded regular file")
        raw = source.read(MAX_SNAPSHOT_BYTES + 1)
    return raw, (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, hashlib.sha256(raw).digest())


@lru_cache(maxsize=4)
def _validated(identity: tuple, raw: bytes) -> Catalogue:
    return validate_bytes(raw)


def catalogue() -> Catalogue:
    path = Path(os.environ.get("VECHER_CONTENT_CATALOGUE") or DEFAULT_PATH)
    raw, identity = read_snapshot(path)
    return _validated(identity, raw)


def published(snapshot: Catalogue) -> list[Publication]:
    # Editorial scheduling is not enabled by a date alone; publication is an
    # explicit active flag in an atomically approved snapshot.
    return [item for item in snapshot.items if item.active]


def author(snapshot: Catalogue, author_id: str) -> Author | None:
    return next((a for a in snapshot.authors if a.id == author_id), None)


def publication(snapshot: Catalogue, item_id: str) -> Publication | None:
    return next((item for item in snapshot.items if item.id == item_id and item.active), None)
