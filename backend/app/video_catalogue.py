"""Reviewed video metadata for licensed files and official platform players.

Media is streamed by the client, never downloaded or proxied by this module.

The allowlist is deliberately independent of user input and catalogue edits fail
closed. Adding a provider requires a code review, not an arbitrary URL field.
"""
import argparse
import hashlib
import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal
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
PLATFORM_CATALOGUE_PATH = Path(__file__).parent / "content" / "rutube_videos.json"
Playback = Literal["legacy", "platform-v1"]
PROVIDER_FIELDS = {"playback_kind", "provider_video_id"}


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
    playback_kind: Literal["direct", "rutube_embed"] = "direct"
    provider_video_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    media_url: str | None = None
    thumbnail_url: str | None = None
    license_name: str | None = Field(default=None, min_length=1, max_length=120)
    license_url: str | None = None
    topics: list[str] = Field(min_length=1, max_length=5)
    language: str = Field(pattern=r"^(none|[a-z]{2})$")
    duration_seconds: float = Field(gt=0, le=7200)
    aspect_ratio: float = Field(gt=0.1, le=5)
    reviewed_at: date
    active: bool = Field(strict=True)
    # WishCard IDs are assigned by each database; only canonical source text is stable.
    plan_deck: str | None = None
    plan_text: str | None = None

    @property
    def embed_url(self) -> str | None:
        if self.playback_kind == "rutube_embed":
            return f"https://rutube.ru/play/embed/{self.provider_video_id}"
        return None

    @model_validator(mode="after")
    def playback_source(self):
        if self.playback_kind == "direct":
            if self.provider_video_id is not None or self.id.startswith("rutube-"):
                raise ValueError("Direct media cannot claim a platform identity")
            if self.media_url is None or self.license_name is None or self.license_url is None:
                raise ValueError("Direct media requires its reviewed source and license")
            media = _https(self.media_url, {"upload.wikimedia.org"})
            if not urlsplit(media).path.lower().endswith((".webm", ".mp4")):
                raise ValueError("Only reviewed WebM/MP4 media are supported")
            _https(self.source_url, {"commons.wikimedia.org"})
            _https(self.license_url, {"creativecommons.org", "commons.wikimedia.org"})
            if self.thumbnail_url is not None:
                _https(self.thumbnail_url, {"upload.wikimedia.org", "thumb.wikimedia.org"})
            if self.duration_seconds > 180:
                raise ValueError("Legacy direct clips must remain at most 180 seconds")
        else:
            if (self.provider_video_id is None or self.id != "rutube-" + self.provider_video_id
                    or self.source_url != f"https://rutube.ru/video/{self.provider_video_id}/"):
                raise ValueError("RUTUBE requires one canonical public video identity")
            if any(value is not None for value in (self.media_url, self.license_name, self.license_url)):
                raise ValueError("Platform playback must not claim a direct stream or a reuse license")
            if self.thumbnail_url is not None:
                _https(self.thumbnail_url, {"pic.rutube.ru"})
        return self

    @model_validator(mode="after")
    def plan_mapping(self):
        if self.plan_deck is None and self.plan_text is None:
            return self
        if ((self.plan_deck, self.plan_text) not in {(deck, text) for deck, text, _ in ALL_CARDS}
                or plan_for(self.plan_text) is None):
            raise ValueError("Video must link to an existing canonical plan")
        return self

    @field_validator("topics")
    @classmethod
    def known_topics(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(topic not in TOPIC_TITLES for topic in value):
            raise ValueError("Unknown or repeated video topic")
        return value

    @field_validator("title", "description", "author", "license_name")
    @classmethod
    def plain_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
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
    legacy = validate_catalogue(json.loads(CATALOGUE_PATH.read_text(encoding="utf-8")))
    platform = validate_catalogue(json.loads(PLATFORM_CATALOGUE_PATH.read_text(encoding="utf-8")))
    if any(video.playback_kind != "direct" for video in legacy):
        raise ValueError("Legacy catalogue may only contain direct media")
    if any(video.playback_kind != "rutube_embed" for video in platform):
        raise ValueError("Platform catalogue may only contain approved platform players")
    combined = (*legacy, *platform)
    if len(combined) > 300 or len({video.id for video in combined}) != len(combined):
        raise ValueError("Combined video catalogue must have unique IDs and at most 300 entries")
    return combined


def active_catalogue(playback: Playback = "legacy") -> tuple[CatalogueVideo, ...]:
    return tuple(video for video in catalogue() if video.active and (
        playback == "platform-v1" or video.playback_kind == "direct"))


def catalogue_revision(playback: Playback = "legacy") -> str:
    # Preserve the original direct-catalogue revision and response contract for
    # installed APKs. The new capability covers both kinds, including archives.
    exclude = PROVIDER_FIELDS if playback == "legacy" else set()
    payload = json.dumps([v.model_dump(mode="json", exclude=exclude) for v in active_catalogue(playback)],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def available_topics(playback: Playback = "legacy") -> list[dict]:
    videos = active_catalogue(playback)
    if playback == "platform-v1":
        videos = tuple(video for video in videos if video.playback_kind == "rutube_embed")
    present = {topic for video in videos for topic in video.topics}
    if playback == "platform-v1":
        return [{"id": key, "title": title, "available": key in present}
                for key, title in TOPIC_TITLES.items()]
    return [{"id": key, "title": title} for key, title in TOPIC_TITLES.items() if key in present]


def find_video(video_id: str | None) -> CatalogueVideo | None:
    # Chat references can point to both providers. Playback support is checked
    # by the video route before returning a DTO or changing private marks.
    return next((video for video in active_catalogue("platform-v1") if video.id == video_id), None)


if __name__ == "__main__":
    # An offline editorial/build gate: schema, mapping and URL policy only.
    # Network availability and the actual contents require separate review.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", action="store_true")
    args = parser.parse_args()
    playback: Playback = "platform-v1" if args.platform else "legacy"
    videos = active_catalogue(playback)
    if args.platform:
        videos = tuple(video for video in videos if video.playback_kind == "rutube_embed")
    if not videos:
        raise SystemExit("VIDEO_CATALOGUE_EMPTY")
    prefix = "PLATFORM_VIDEO_CATALOGUE_OK" if args.platform else "VIDEO_CATALOGUE_OK"
    print(f"{prefix}={len(videos)};REVISION={catalogue_revision(playback)}")
