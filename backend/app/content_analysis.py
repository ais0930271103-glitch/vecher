"""Deterministic analysis of public editorial text.

The default analyser is a small, explainable lexical baseline, not a neural
model and not a video/audio transcription service. It reads only an explicit
allowlist of publication fields. An optional local sentence-transformer adapter
can build a versioned embedding index offline; importing this module never
loads a model, downloads weights, or makes network requests.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

ANALYZER_VERSION = "lexical-ru-en-v1"
MAX_TEXT_CHARS = 60_000
MAX_INDEX_BYTES = 32 * 1024 * 1024
TOPIC_ALIASES = {"cooking": "food", "science": "curiosity"}

# Prefixes are deliberately specific. This is a bounded vocabulary, not a
# morphological model. Unknown topics, including hobbies/nature, stay distinct.
TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "digital": ("парол", "резервн", "смартфон", "компьютер", "программ",
                "настройк", "цифров", "шифрован", "backup", "password", "software"),
    "home": ("уборк", "ремонт", "хранени", "квартир", "интерьер", "мебел",
             "расхлам", "cleaning", "furniture", "household"),
    "food": ("рецепт", "кулинар", "ингредиент", "готовить", "приготов",
             "выпечк", "ужин", "завтрак", "recipe", "cooking", "ingredient"),
    "travel": ("путешеств", "поездк", "маршрут", "экскурс", "туризм",
               "достопримечательн", "travel", "itinerary", "tourism"),
    "creativity": ("рисован", "рисунк", "фотограф", "композиц", "акварел",
                   "вышива", "рукодел", "drawing", "photography", "watercolor"),
    "curiosity": ("наук", "научн", "эксперимент", "астроном", "физик",
                  "биологи", "science", "experiment", "astronomy"),
    "hobbies": ("хобби", "коллекционир", "моделизм", "hobby", "hobbies"),
    "nature": ("природ", "птиц", "растени", "животн", "экологи",
               "nature", "wildlife", "botany"),
}
STOPWORDS = frozenset("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы
где да даже для до его ее если есть еще же за здесь и из или им их к как ко
когда кто ли либо мне мы на надо нас не него нее нет ни но ну о об один она
они оно от по под при про с со так такой там те тем то того тоже только том
ты у уже что чтобы эта эти это этот я будет могут можно нужно ваш ваша ваши
the a an and are as at be been by can for from has have how if in into is it
its not of on or our that their there these they this to was we were what
when which will with you your about more also
""".split())
TOKEN_RE = re.compile(r"[a-zа-яё][a-zа-яё0-9]{1,39}", re.IGNORECASE)
QUERY_TOKEN_RE = re.compile(r"[a-zа-яё0-9][a-zа-яё0-9]{0,39}", re.IGNORECASE)
_QUERY_NEGATIONS = frozenset({"без", "не", "not", "without"})
_UNINFLECTED = frozenset({"видео", "кино", "такси", "кофе", "меню", "хобби"})
_QUERY_SYNONYMS = {
    "бэкап": "копия", "бекап": "копия", "backup": "копия",
    "рецепты": "рецепт", "recipe": "рецепт", "recipes": "рецепт",
    "dinner": "ужин", "breakfast": "завтрак", "oven": "духовка",
    "фотографии": "фото", "фотография": "фото", "снимок": "фото", "снимки": "фото",
    "photography": "фото", "photo": "фото", "photos": "фото",
    "идеи": "идея", "идею": "идея", "идей": "идея", "ideas": "идея",
}
_RUSSIAN_ENDINGS = tuple(sorted({
    "иями", "ями", "ами", "его", "ого", "ему", "ому", "ыми", "ими", "иях",
    "ать", "ять", "ить", "ах", "ях", "ов", "ев", "ом", "ем", "ам", "ям",
    "ой", "ей", "ую", "юю", "ая", "яя", "ое", "ее", "ые", "ие", "ия",
    "ию", "ии", "ый", "ий", "ых", "их", "а", "я", "у", "ю", "ы", "и", "е", "о",
}, key=lambda suffix: (-len(suffix), suffix)))


def normalize_topic(value: str) -> str:
    topic = value.strip().lower()
    return TOPIC_ALIASES.get(topic, topic)


def lexical_query_terms(text: str) -> tuple[str, ...]:
    """Light lexical query normalisation; preserves negation and token order.

    Callers must interpret negation scope themselves. This bounded suffix and
    synonym heuristic is not a linguistic parser; use it identically on the
    query and the public document. Repeated negations are intentionally kept.
    """
    terms = []
    for token in QUERY_TOKEN_RE.findall(text.lower().replace("ё", "е")):
        if token in STOPWORDS and token not in _QUERY_NEGATIONS:
            continue
        word = _QUERY_SYNONYMS.get(token, token)
        if word not in _UNINFLECTED and len(word) >= 5 and re.fullmatch("[а-я]+", word):
            for ending in _RUSSIAN_ENDINGS:
                if word.endswith(ending) and len(word) - len(ending) >= 3:
                    word = word[:-len(ending)]
                    break
        terms.append(word)
    return tuple(terms)


def _field(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)


def _segments(item: Any) -> tuple[tuple[str, str, int], ...]:
    result: list[tuple[str, str, int]] = []
    remaining = MAX_TEXT_CHARS

    def add(basis: str, value: Any, weight: int) -> None:
        nonlocal remaining
        if isinstance(value, str) and value.strip() and remaining > 0:
            text = value.strip()[:remaining]
            result.append((basis, text, weight))
            remaining -= len(text)

    add("title", _field(item, "title"), 3)
    add("summary", _field(item, "summary") or _field(item, "description"), 2)
    # Explicitly provided public transcripts only. No URL is fetched here.
    add("provided_transcript", _field(item, "transcript"), 1)
    for block in (_field(item, "blocks", ()) or ())[:60]:
        if _field(block, "type") in {"paragraph", "heading"}:
            add("article_text", _field(block, "text"), 1)
        elif _field(block, "type") == "list":
            for value in (_field(block, "items", ()) or ())[:40]:
                add("article_text", value, 1)
    return tuple(result)


def content_text(item: Any) -> str:
    """Only supplied public title/summary/body/transcript; never arbitrary attrs."""
    return "\n".join(text for _, text, _ in _segments(item))


def _tokens(text: str) -> list[str]:
    return [word for word in TOKEN_RE.findall(text.lower().replace("ё", "е"))
            if word not in STOPWORDS]


@dataclass(frozen=True)
class ContentAnalysis:
    topics: tuple[str, ...]
    keywords: tuple[str, ...]
    language: str
    analysis_basis: tuple[str, ...]
    confidence: float
    evidence: tuple[tuple[str, tuple[str, ...]], ...]
    analyzer_version: str
    text_fingerprint: str
    term_weights: tuple[tuple[str, float], ...]

    def to_dict(self) -> dict[str, Any]:
        # Canonical JSON-compatible types keep staged drafts identical after
        # a disk round trip, including when the next source refresh fails.
        return {"topics": list(self.topics), "keywords": list(self.keywords),
                "language": self.language, "analysis_basis": list(self.analysis_basis),
                "confidence": self.confidence,
                "evidence": [[topic, list(words)] for topic, words in self.evidence],
                "analyzer_version": self.analyzer_version, "text_fingerprint": self.text_fingerprint,
                "term_weights": [[term, weight] for term, weight in self.term_weights]}


def analyze_content(item: Any) -> ContentAnalysis:
    segments = _segments(item)
    counts: Counter[str] = Counter()
    for _, text, weight in segments:
        counts.update({term: min(count, 4) * weight for term, count in Counter(_tokens(text)).items()})
    declared = tuple(dict.fromkeys(normalize_topic(str(topic))
                                  for topic in (_field(item, "topics", ()) or ())
                                  if isinstance(topic, str) and topic.strip()))
    evidence = []
    inferred = []
    for topic, stems in TOPIC_TERMS.items():
        matches = tuple(sorted(term for term in counts if any(term.startswith(stem) for stem in stems)))
        if matches:
            evidence.append((topic, matches[:12]))
        # Two distinct pieces of lexical evidence prevent a lone ambiguous word
        # from silently assigning an extra topic. Declared topics are preserved.
        if len(matches) >= 2:
            inferred.append(topic)
    ranked = sorted(counts, key=lambda term: (-counts[term], term))
    # Log weights prevent repeated keywords from dominating similarity.
    weights = tuple(sorted((term, round(1 + math.log(count), 6)) for term, count in counts.items()))
    text = "\n".join(text for _, text, _ in segments)
    confidence = min(0.85, 0.2 + 0.1 * sum(len(words) for _, words in evidence)) if counts else 0.0
    return ContentAnalysis(
        topics=tuple(dict.fromkeys((*declared, *inferred))), keywords=tuple(ranked[:32]),
        language=str(_field(item, "language", "unknown")),
        analysis_basis=tuple(dict.fromkeys(basis for basis, _, _ in segments)),
        # An uncalibrated lexical evidence indicator, NOT a probability of truth.
        confidence=round(confidence, 2), evidence=tuple(evidence),
        analyzer_version=ANALYZER_VERSION,
        text_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(), term_weights=weights,
    )


def similarity(left: ContentAnalysis, right: ContentAnalysis) -> float:
    """Weighted lexical cosine in [0, 1]; no claim of neural semantic matching."""
    a, b = dict(left.term_weights), dict(right.term_weights)
    denominator = math.sqrt(sum(x * x for x in a.values()) * sum(x * x for x in b.values()))
    if not denominator:
        return 0.0
    return min(1.0, max(0.0, sum(value * b.get(term, 0.0) for term, value in a.items()) / denominator))


class EmbeddingProvider(Protocol):
    model_id: str

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class LocalSentenceTransformer:
    """Optional offline adapter. Call from a worker/CLI, never a feed request."""

    def __init__(self, model_path: Path, *, model_id: str, device: str = "cpu"):
        if not model_path.is_dir() or not model_id.strip():
            raise ValueError("An existing local model directory and immutable model ID are required")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("Install sentence-transformers in the separate analysis worker") from exc
        self.model_id = model_id
        self._model = SentenceTransformer(str(model_path.resolve()), device=device,
                                          local_files_only=True, trust_remote_code=False)

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return self._model.encode(list(texts), batch_size=16, normalize_embeddings=True,
                                  show_progress_bar=False, convert_to_numpy=True).tolist()


def _unit_vector(values: Sequence[float]) -> list[float]:
    if not 1 <= len(values) <= 8192 or any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values):
        raise ValueError("Invalid embedding dimensions")
    vector = [float(value) for value in values]
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("Embedding contains non-finite numbers")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Embedding must have a finite non-zero norm")
    if math.isclose(norm, 1.0, rel_tol=1e-12, abs_tol=1e-12):
        return vector
    return [value / norm for value in vector]


def build_embedding_index(items: Sequence[Any], provider: EmbeddingProvider,
                          *, previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Encode only changed public items, keyed by model and text fingerprints."""
    if len(items) > 5000:
        raise ValueError("Too many embedding documents")
    if not isinstance(provider.model_id, str) or not 1 <= len(provider.model_id.strip()) <= 200:
        raise ValueError("Embedding provider needs a bounded immutable model ID")
    previous = previous or {}
    reusable = previous.get("items", {}) if (previous.get("model_id") == provider.model_id
                  and previous.get("analyzer_version") == ANALYZER_VERSION) else {}
    entries: dict[str, Any] = {}
    pending: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(_field(item, "id", ""))
        if not item_id or item_id in seen:
            raise ValueError("Embedding documents need unique IDs")
        seen.add(item_id)
        analysis = analyze_content(item)
        old = reusable.get(item_id, {})
        if old.get("text_fingerprint") == analysis.text_fingerprint:
            entries[item_id] = {"text_fingerprint": analysis.text_fingerprint,
                                "vector": _unit_vector(old.get("vector", []))}
        else:
            pending.append((item_id, analysis.text_fingerprint, content_text(item)))
    vectors = provider.encode([text for _, _, text in pending]) if pending else []
    if len(vectors) != len(pending):
        raise ValueError("Embedding provider returned a different document count")
    for (item_id, fingerprint, _), vector in zip(pending, vectors):
        entries[item_id] = {"text_fingerprint": fingerprint, "vector": _unit_vector(vector)}
    dimensions = {len(entry["vector"]) for entry in entries.values()}
    if len(dimensions) > 1:
        raise ValueError("Embedding dimensions are inconsistent")
    return {"version": 1, "model_id": provider.model_id, "analyzer_version": ANALYZER_VERSION,
            "dimensions": next(iter(dimensions), 0), "items": entries}


def embedding_similarity(index: Mapping[str, Any], left_id: str, left: ContentAnalysis,
                         right_id: str, right: ContentAnalysis) -> float | None:
    """A cache miss/stale content returns None so callers keep the lexical path."""
    if index.get("version") != 1 or index.get("analyzer_version") != ANALYZER_VERSION:
        return None
    items = index.get("items", {})
    a, b = items.get(left_id, {}), items.get(right_id, {})
    if a.get("text_fingerprint") != left.text_fingerprint or b.get("text_fingerprint") != right.text_fingerprint:
        return None
    try:
        av, bv = _unit_vector(a["vector"]), _unit_vector(b["vector"])
        if len(av) != len(bv):
            return None
        return min(1.0, max(0.0, sum(x * y for x, y in zip(av, bv))))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


@lru_cache(maxsize=2)
def _validated_embedding_bytes(identity: tuple, raw: bytes) -> dict[str, Any]:
    from .content_catalogue import _unique_object

    index = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(index, dict)
            or set(index) != {"version", "model_id", "analyzer_version", "dimensions", "items"}
            or type(index["version"]) is not int or index["version"] != 1
            or index["analyzer_version"] != ANALYZER_VERSION
            or not isinstance(index["model_id"], str) or not 1 <= len(index["model_id"]) <= 200
            or type(index["dimensions"]) is not int or not 0 <= index["dimensions"] <= 8192
            or not isinstance(index["items"], dict) or len(index["items"]) > 5000):
        raise ValueError("Invalid public embedding index")
    for item_id, entry in index["items"].items():
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item_id)
                or not isinstance(entry, dict) or set(entry) != {"text_fingerprint", "vector"}
                or not isinstance(entry["text_fingerprint"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["text_fingerprint"])
                or not isinstance(entry["vector"], list)
                or len(entry["vector"]) != index["dimensions"]):
            raise ValueError("Invalid public embedding entry")
        entry["vector"] = _unit_vector(entry["vector"])
    if bool(index["items"]) != bool(index["dimensions"]):
        raise ValueError("Embedding index dimensions do not match its contents")
    return index


def load_embedding_index() -> tuple[dict[str, Any] | None, str]:
    """Read an optional administrator-selected public index; never load a model.

    Failure means the exact same lexical baseline as an unconfigured index.
    The returned revision must be included in recommendation cursor identity.
    Treat the cached dictionary as immutable. Per-item staleness is checked by
    embedding_similarity against current public-content fingerprints.
    """
    selected = os.environ.get("VECHER_CONTENT_EMBEDDINGS")
    if not selected:
        return None, "lexical"
    try:
        fd = os.open(selected, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INDEX_BYTES:
                return None, "lexical"
            raw = source.read(MAX_INDEX_BYTES + 1)
        if len(raw) > MAX_INDEX_BYTES:
            return None, "lexical"
        digest = hashlib.sha256(raw).hexdigest()
        identity = (selected, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, digest)
        return _validated_embedding_bytes(identity, raw), "embeddings:" + digest
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None, "lexical"


def write_index(index: Mapping[str, Any], destination: Path) -> None:
    raw = (json.dumps(index, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_INDEX_BYTES:
        raise ValueError("Embedding index is too large")
    fd, name = tempfile.mkstemp(prefix=".embeddings-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def main() -> None:
    import argparse
    from .content_catalogue import published, read_snapshot, validate_bytes
    from .recommendations import unique_items, with_reviewed_videos

    parser = argparse.ArgumentParser(description="Build public-content embeddings with a preinstalled local model")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--model-path", required=True, type=Path)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snapshot = validate_bytes(read_snapshot(args.source)[0])
    previous = None
    if args.output.is_file() and args.output.stat().st_size <= MAX_INDEX_BYTES:
        try:
            raw = args.output.read_bytes()
            if len(raw) <= MAX_INDEX_BYTES:
                previous = _validated_embedding_bytes((str(args.output), hashlib.sha256(raw).hexdigest()), raw)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            # A broken previous index is rebuilt, never treated as a model.
            previous = None
    provider = LocalSentenceTransformer(args.model_path, model_id=args.model_id)
    # Same reviewed unified catalogue as the feed, including video bridges.
    index = build_embedding_index(unique_items(published(with_reviewed_videos(snapshot))),
                                  provider, previous=previous)
    write_index(index, args.output)
    print(f"CONTENT_EMBEDDINGS_BUILT={len(index['items'])};MODEL={provider.model_id}")


if __name__ == "__main__":
    main()
