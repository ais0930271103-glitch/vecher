"""Reviewed, licensed video metadata. Media is streamed by the client, never fetched here.

The allowlist is deliberately independent of user input and catalogue edits fail
closed. Adding a provider requires a code review, not an arbitrary URL field.
"""
import hashlib
import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .content.plans import plan_for
from .content.wishes import ALL_CARDS

TOPIC_TITLES = {
    "humor": "Посмеяться", "animals": "Животные", "nature": "Природа",
    "science": "Удивительное рядом", "cooking": "Еда и напитки",
    "travel": "Путешествия", "hobbies": "Увлечения", "home": "Уют и дом",
    "music": "Музыка", "games": "Игры", "films": "Короткое кино",
    "ideas": "Попробовать вместе",
}
CATALOGUE_PATH = Path(__file__).parent / "content" / "videos.json"


def _https(value: str, hosts: set[str]) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in hosts
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment
            or any(ord(ch) < 32 or ch == "\\" for ch in value)):
        raise ValueError("Video URL must use an approved HTTPS host")
    return value


class CatalogueVideo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1200)
    author: str = Field(min_length=1, max_length=600)
    source_url: str
    media_url: str
    thumbnail_url: str | None = None
    license_name: str = Field(min_length=1, max_length=120)
    license_url: str
    topics: list[str] = Field(min_length=1, max_length=5)
    language: str = Field(pattern=r"^(none|[a-z]{2})$")
    duration_seconds: float = Field(gt=0, le=180)
    aspect_ratio: float = Field(gt=0.1, le=5)
    reviewed_at: date
    active: bool = Field(strict=True)
    # WishCard IDs are assigned by each database; only canonical source text is stable.
    plan_deck: str | None = None
    plan_text: str | None = None

    @model_validator(mode="after")
    def plan_mapping(self):
        if self.plan_deck is None and self.plan_text is None:
            return self
        if ((self.plan_deck, self.plan_text) not in {(deck, text) for deck, text, _ in ALL_CARDS}
                or plan_for(self.plan_text) is None):
            raise ValueError("Video must link to an existing canonical plan")
        return self

    @field_validator("media_url")
    @classmethod
    def media(cls, value: str) -> str:
        value = _https(value, {"upload.wikimedia.org"})
        if not urlsplit(value).path.lower().endswith((".webm", ".mp4")):
            raise ValueError("Only reviewed WebM/MP4 media are supported")
        return value

    @field_validator("thumbnail_url")
    @classmethod
    def thumbnail(cls, value: str | None) -> str | None:
        return _https(value, {"upload.wikimedia.org", "thumb.wikimedia.org"}) if value else None

    @field_validator("source_url")
    @classmethod
    def source(cls, value: str) -> str:
        return _https(value, {"commons.wikimedia.org"})

    @field_validator("license_url")
    @classmethod
    def license_link(cls, value: str) -> str:
        return _https(value, {"creativecommons.org", "commons.wikimedia.org"})

    @field_validator("topics")
    @classmethod
    def known_topics(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(topic not in TOPIC_TITLES for topic in value):
            raise ValueError("Unknown or repeated video topic")
        return value

    @field_validator("title", "description", "author", "license_name")
    @classmethod
    def plain_text(cls, value: str) -> str:
        if value != value.strip() or "<" in value or ">" in value or not value.strip():
            raise ValueError("Catalogue text must be plain, non-empty text")
        return value


def validate_catalogue(data: object) -> tuple[CatalogueVideo, ...]:
    if not isinstance(data, list) or len(data) > 300:
        raise ValueError("Video catalogue must be a reviewed list with at most 300 items")
    videos = tuple(CatalogueVideo.model_validate(item) for item in data)
    if len({video.id for video in videos}) != len(videos):
        raise ValueError("Video IDs must be unique")
    return videos


@lru_cache(maxsize=1)
def catalogue() -> tuple[CatalogueVideo, ...]:
    return validate_catalogue(json.loads(CATALOGUE_PATH.read_text(encoding="utf-8")))


def active_catalogue() -> tuple[CatalogueVideo, ...]:
    return tuple(video for video in catalogue() if video.active)


def catalogue_revision() -> str:
    payload = json.dumps([v.model_dump(mode="json") for v in active_catalogue()],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def available_topics() -> list[dict[str, str]]:
    present = {topic for video in active_catalogue() for topic in video.topics}
    return [{"id": key, "title": title} for key, title in TOPIC_TITLES.items() if key in present]


def find_video(video_id: str | None) -> CatalogueVideo | None:
    return next((video for video in active_catalogue() if video.id == video_id), None)


if __name__ == "__main__":
    # An offline editorial/build gate: schema, mapping and URL policy only.
    # Network availability and the actual contents require separate review.
    videos = active_catalogue()
    if not videos:
        raise SystemExit("VIDEO_CATALOGUE_EMPTY")
    print(f"VIDEO_CATALOGUE_OK={len(videos)};REVISION={catalogue_revision()}")
