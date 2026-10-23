"""Deterministic recommendations from public content and explicit reader choices.

There is no playback, dwell-time, scroll, chat, partner-profile or popularity
input. Public-content analysis is reusable; private signals are recomputed from
the current boolean states, so undoing an action also removes its influence.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, time, timezone
from typing import Mapping, Sequence

from . import content_catalogue as content, video_catalogue
from .content_analysis import (analyze_content, embedding_similarity, lexical_query_terms,
                               normalize_topic, similarity)

RECOMMENDER_VERSION = "explicit-content-v1"
VIDEO_FIELDS = {"useful": "liked", "saved": "saved", "hidden": "hidden"}


def has_explicit_video_feedback(data: Mapping) -> bool:
    """A row created only by the legacy viewed marker is not reader feedback."""
    return (any(bool(data.get(field)) for field in VIDEO_FIELDS.values())
            or bool(data.get("content_revision")) or bool(data.get("content_overrides")))


def merge_video_feedback(legacy: Mapping, publications: Sequence[Mapping]) -> dict:
    """Retain old publication marks until a deliberate canonical update.

    The per-field override marker also preserves an explicit undo made through
    the old player while the optional editorial file was temporarily broken.
    It stores no history or behavioural data, only which current fields have
    been explicitly reconciled with the shared video state.
    """
    result = {field: bool(legacy.get(field, False)) for field in VIDEO_FIELDS.values()}
    overrides = legacy.get("content_overrides", [])
    for publication in publications:
        for old, field in VIDEO_FIELDS.items():
            if field not in overrides:
                result[field] = result[field] or bool(publication.get(old, False))
    return result


async def video_publication_states(session, user_id: str, video_id: str):
    """Old /videos remains usable if the independent editorial file is broken."""
    from sqlalchemy import select
    from .models import ContentState

    try:
        snapshot = content.catalogue()
    except (OSError, ValueError, UnicodeError):
        return [], False
    linked = [item.id for item in snapshot.items if item.video_id == video_id]
    if not linked:
        return [], True
    rows = (await session.execute(select(ContentState).where(
        ContentState.user_id == user_id, ContentState.content_id.in_(linked))
        .execution_options(populate_existing=True))).scalars().all()
    return rows, True


def bridge_id(video_id: str) -> str:
    """Stable publication identity without changing the existing player ID."""
    return "video-" + (video_id if len(video_id) <= 58 else hashlib.sha256(video_id.encode()).hexdigest()[:48])


def bridge_author_id(video: video_catalogue.CatalogueVideo) -> str:
    # The old catalogue has only a display name, not a provider channel ID.
    # This is an internal attribution group, not a verified platform identity.
    identity = f"{video.playback_kind}:{video.author.casefold()}"
    return "video-author-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def with_reviewed_videos(snapshot: content.Catalogue) -> content.Catalogue:
    """Bridge only the reviewed platform catalogue, retaining editorial IDs.

    Legacy direct files remain available through /videos and its archive. Both
    IDs for a manually linked publication and its bridge stay routable; list
    deduplication is separate, so introducing an editorial wrapper never breaks
    an older link. No remote discovery or media download runs in this function.
    """
    topics = {topic.id: topic for topic in snapshot.topics}
    # Preserve old explicit choices even when their last platform video is
    # unavailable, so the settings screen can still show and remove them.
    for old_id, title in video_catalogue.TOPIC_TITLES.items():
        topic_id = normalize_topic(old_id)
        topics.setdefault(topic_id, content.Topic(id=topic_id, title=title))
    authors = {author.id: author for author in snapshot.authors}
    items = list(snapshot.items)
    existing = {item.id: item for item in items}
    for video in video_catalogue.catalogue():
        if video.playback_kind != "rutube_embed":
            continue
        # A published editorial withdrawal of this video must not silently be
        # replaced with an automatic bridge of the same source.
        wrappers = [item for item in snapshot.items if item.video_id == video.id]
        active = video.active and (not wrappers or any(item.active for item in wrappers))
        topic_ids = list(dict.fromkeys(normalize_topic(topic) for topic in video.topics))
        for original, topic_id in ((t, normalize_topic(t)) for t in video.topics):
            topics.setdefault(topic_id, content.Topic(id=topic_id, title=video_catalogue.TOPIC_TITLES[original]))
        author_id = bridge_author_id(video)
        authors.setdefault(author_id, content.Author(
            id=author_id, name=video.author[:120],
            bio=f"Подпись автора в проверенном каталоге: {video.author}. Источник каждого выпуска указан в его карточке.",
        ))
        item_id = bridge_id(video.id)
        if item_id in existing:
            raise ValueError("Editorial publication uses a reserved video bridge identity")
        # The source catalogue stores a review date, not the source's upload
        # timestamp. These dates describe admission to the Vecher catalogue.
        admitted_at = datetime.combine(video.reviewed_at, time.min, tzinfo=timezone.utc)
        items.append(content.Publication(
            id=item_id, kind="video", title=video.title,
            summary=video.description if len(video.description) <= 800 else video.description[:799] + "…",
            author_id=author_id, topics=topic_ids, language=video.language,
            blocks=[content.TextBlock(type="paragraph", text=video.description)],
            sources=[content.Source(title="Видео автора на RUTUBE", url=video.source_url)],
            published_at=admitted_at, updated_at=admitted_at, reviewed_at=video.reviewed_at,
            active=active, reading_minutes=min(60, max(1, math.ceil(video.duration_seconds / 60))),
            video_id=video.id,
        ))
    return content.Catalogue(version=snapshot.version, topics=list(topics.values()),
                             authors=list(authors.values()), items=items)


def is_bridge(item: content.Publication) -> bool:
    return item.video_id is not None and item.id == bridge_id(item.video_id)


def unique_items(items: Sequence[content.Publication]) -> list[content.Publication]:
    """Prefer an existing editorial publication when two cards play one video."""
    output: dict[str, content.Publication] = {}
    for item in items:
        key = "video:" + item.video_id if item.video_id else "item:" + item.id
        previous = output.get(key)
        if previous is None or (is_bridge(previous) and not is_bridge(item)):
            output[key] = item
    return list(output.values())


def search_score(item: content.Publication, author_name: str, query: str) -> float:
    """Conservative lexical search with inflections, aliases and negation.

    Negative conditions require explicit textual evidence; absence of a word
    in short metadata is not proof of absence of an ingredient or tool.
    """
    if not query:
        return 0.0
    body = " ".join(
        " ".join(block.items) if isinstance(block, content.ListBlock) else block.text
        for block in item.blocks
    )
    title = set(lexical_query_terms(item.title))
    lead = set(lexical_query_terms(f"{item.summary} {author_name}"))
    actual = lexical_query_terms(f"{item.title}. {item.summary}. {body}")
    haystack = set(actual) | lead
    positive: list[str] = []
    negative: list[tuple[str, ...]] = []
    markers = {"без", "не", "not", "without"}
    request_words = set(lexical_query_terms("найти найди показать покажи сделать хочу узнать расскажи"))
    # A clause following "without" stays a negative condition until a clear
    # separator. Unsupported complex grammar fails conservatively, rather than
    # silently dropping a restriction such as "without an oven".
    for clause in re.split(r"[,;.!?]|\b(?:с|with)\b", query.casefold()):
        terms = tuple(term for term in lexical_query_terms(clause) if term not in request_words)
        marker = next((index for index, term in enumerate(terms) if term in markers), None)
        if marker is None:
            positive.extend(terms)
        else:
            positive.extend(terms[:marker])
            group = tuple(term for term in terms[marker + 1:] if term not in markers)
            if not group:
                return -1.0
            negative.append(group)
    if not positive and not negative:
        return -1.0
    if not all(term in haystack for term in positive):
        return -1.0
    for group in negative:
        covered = set()
        for index, term in enumerate(actual):
            if term in markers and tuple(actual[index + 1:index + 1 + len(group)]) == group:
                covered.update(range(index + 1, index + 1 + len(group)))
        if not covered or any(term in group and index not in covered for index, term in enumerate(actual)):
            return -1.0
    return sum(3.0 if term in title else 1.5 if term in lead else 0.5 for term in positive) + len(negative)


def rank(
    items: Sequence[content.Publication], *, all_items: Sequence[content.Publication],
    states: Mapping[str, Mapping[str, bool]], topics: Sequence[str],
    followed_authors: Sequence[str], topic_titles: Mapping[str, str],
    mode: str, query_scores: Mapping[str, float] | None = None, discover: bool = False,
    embedding_index: dict | None = None,
) -> tuple[list[content.Publication], dict[str, str]]:
    """Rank only prefiltered candidates; never relax topics, language or hides.

    Shared mode takes only the explicit common topics. Personal useful/save
    states and subscriptions are not read in shared mode, including reasons.
    Weights below are documented initial rules, not learned probabilities.
    """
    analyses = {item.id: analyze_content(item) for item in (*all_items, *items)}
    selected = {normalize_topic(topic) for topic in topics}
    personal = mode == "personal"
    feedback = []
    if personal:
        for item in all_items:
            state = states.get(item.id, {})
            if state.get("hidden"):
                continue
            strength = 2.0 if state.get("useful") else 1.0 if state.get("saved") else 0.0
            if strength:
                feedback.append((item, strength))
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for item in items:
        matches = selected.intersection(normalize_topic(topic) for topic in item.topics)
        topical = 2.0 * min(2, len(matches))
        followed = personal and item.author_id in followed_authors
        related = 0.0
        related_kind = ""
        if personal:
            for known, strength in feedback:
                # A saved item is not its own evidence of a new discovery.
                if known.id == item.id or (item.video_id and item.video_id == known.video_id):
                    continue
                semantic = embedding_similarity(embedding_index, item.id, analyses[item.id],
                                                known.id, analyses[known.id]) if embedding_index else None
                value = strength * (max(0.0, semantic) if semantic is not None
                                    else similarity(analyses[item.id], analyses[known.id]))
                if value > related:
                    related = value
                    related_kind = "useful" if strength == 2.0 else "saved"
        search = (query_scores or {}).get(item.id, 0.0)
        # The strongest related explicit example is bounded; many repeated
        # marks from one topic do not create an unbounded popularity counter.
        scores[item.id] = topical + (1.0 if followed else 0.0) + 3.0 * related + 5.0 * search
        if search > 0:
            reasons[item.id] = "Совпадает с вашим поиском"
        elif mode == "shared":
            title = topic_titles.get(sorted(matches)[0], sorted(matches)[0]) if matches else "Общие интересы"
            reasons[item.id] = f"Ваша общая тема: {title}"
        elif mode == "following":
            reasons[item.id] = "Новая публикация автора из ваших подписок"
        elif related >= 0.25:
            reasons[item.id] = ("Похоже на материалы, которые вы отметили полезными" if related_kind == "useful"
                                else "Похоже на сохранённые вами материалы")
        elif followed:
            reasons[item.id] = "Автор из ваших подписок"
        elif matches:
            title = topic_titles.get(sorted(matches)[0], sorted(matches)[0])
            reasons[item.id] = f"Вы выбрали тему: {title}"
        elif discover:
            reasons[item.id] = "Из подборки для знакомства с темами"
        else:
            reasons[item.id] = "Из выбранной вами подборки"
    remaining = sorted(items, key=lambda item: (-scores[item.id], -item.published_at.timestamp(), item.id))
    output: list[content.Publication] = []
    while remaining:
        # Prefer a different author within a bounded relevance gap; never
        # insert an unrelated/hidden candidate merely to satisfy diversity.
        index = 0
        if output:
            best = scores[remaining[0].id]
            for candidate, item in enumerate(remaining[:6]):
                if best - scores[item.id] > 1.0:
                    break
                repeated_topic = len(output) >= 2 and bool(
                    set(item.topics) & set(output[-1].topics) & set(output[-2].topics))
                if item.author_id != output[-1].author_id and not repeated_topic:
                    index = candidate
                    break
        output.append(remaining.pop(index))
    return output, reasons
