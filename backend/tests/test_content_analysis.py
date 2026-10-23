"""Public content analysis, honest fallbacks and versioned offline embeddings."""
import copy
import json
import math

import pytest

from app.content_analysis import (ANALYZER_VERSION, analyze_content,
                                  build_embedding_index, content_text,
                                  embedding_similarity, lexical_query_terms, load_embedding_index, normalize_topic,
                                  similarity)


def material(item_id, title, body="", topics=()):
    return {"id": item_id, "title": title, "summary": "Короткое описание",
            "topics": list(topics), "language": "ru",
            "blocks": [{"type": "paragraph", "text": body}] if body else []}


def test_public_fields_only_and_transparent_topic_evidence():
    item = material("dinner", "Рецепт: простой ужин", "Ингредиенты для приготовления", ["cooking"])
    analysis = analyze_content(item)
    assert analysis.topics == ("food",)
    assert dict(analysis.evidence)["food"]
    assert analysis.analysis_basis == ("title", "summary", "article_text")
    assert analysis.analyzer_version == ANALYZER_VERSION
    assert 0 <= analysis.confidence <= 1
    private = {**item, "messages": ["Путешествие, поездка, маршрут"],
               "private_answers": "Наука физика", "partner_interests": ["travel"]}
    assert analyze_content(private) == analysis
    assert "Путешествие" not in content_text(private)
    assert normalize_topic("science") == "curiosity"
    assert normalize_topic("hobbies") == "hobbies"
    assert normalize_topic("nature") == "nature"


def test_lexical_matching_is_deterministic_and_does_not_invent_video_analysis():
    a = analyze_content(material("backup", "Резервная копия смартфона", "Сохраняйте резервную копию"))
    b = analyze_content(material("copy", "Копия смартфона", "Резервная копия"))
    unrelated = analyze_content(material("meal", "Рецепт ужина", "Ингредиенты выпечки"))
    assert similarity(a, b) > similarity(a, unrelated)
    assert similarity(a, a) == pytest.approx(1)
    assert similarity(a, b) == similarity(b, a)
    assert similarity(analyze_content({}), a) == 0
    video = analyze_content({"title": "Красивый ролик", "description": "Прогулка", "topics": ["travel"],
                             "media_url": "https://example.org/video.mp4"})
    assert video.analysis_basis == ("title", "summary")
    assert "provided_transcript" not in video.analysis_basis
    assert "article_text" not in video.analysis_basis


def test_query_normalization_keeps_negation_and_matches_simple_inflections():
    assert lexical_query_terms("без духовки") == ("без", "духовк")
    assert lexical_query_terms("духовка") == ("духовк",)
    assert lexical_query_terms("без духовки и без выпечки").count("без") == 2
    assert "not" in lexical_query_terms("not cooking")
    assert "видео" in lexical_query_terms("найти видео")
    assert lexical_query_terms("бэкап") == lexical_query_terms("копия")
    assert "20" in lexical_query_terms("ужин за 20 минут")
    assert lexical_query_terms("10 минут") != lexical_query_terms("40 минут")


class FakeEmbeddingProvider:
    model_id = "test-local-model-v1"

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return [[1, len(text) % 5 + 1] for text in texts]


def test_embeddings_reuse_only_same_public_text_and_same_model_version():
    provider = FakeEmbeddingProvider()
    a = material("a", "Рецепт ужина")
    b = material("b", "Рецепт завтрака")
    first = build_embedding_index([a, b], provider)
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 2
    unchanged = build_embedding_index([a, b], provider, previous=first)
    assert len(provider.calls) == 1 and unchanged == first
    updated = {**b, "title": "Физика и научный эксперимент"}
    second = build_embedding_index([a, updated], provider, previous=first)
    assert len(provider.calls) == 2 and len(provider.calls[1]) == 1
    aa, ba = analyze_content(a), analyze_content(b)
    assert embedding_similarity(first, "a", aa, "b", ba) is not None
    assert embedding_similarity(second, "a", aa, "b", ba) is None
    provider.model_id = "test-local-model-v2"
    build_embedding_index([a, updated], provider, previous=second)
    assert len(provider.calls[-1]) == 2


def test_embedding_cache_rejects_bad_shapes_and_missing_or_changed_entries():
    provider = FakeEmbeddingProvider()
    a = material("a", "Рецепт ужина")
    analysis = analyze_content(a)
    index = build_embedding_index([a], provider)
    assert embedding_similarity(index, "a", analysis, "absent", analysis) is None
    broken = copy.deepcopy(index)
    broken["items"]["a"]["vector"] = [math.nan, 1]
    assert embedding_similarity(broken, "a", analysis, "a", analysis) is None
    with pytest.raises(ValueError, match="unique IDs"):
        build_embedding_index([a, a], provider)

    class BadProvider:
        model_id = "bad"

        def encode(self, texts):
            return [[1, math.inf] for _ in texts]

    with pytest.raises(ValueError, match="non-finite"):
        build_embedding_index([a], BadProvider())


def test_optional_embedding_file_has_revision_and_safe_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("VECHER_CONTENT_EMBEDDINGS", raising=False)
    assert load_embedding_index() == (None, "lexical")
    path = tmp_path / "index.json"
    monkeypatch.setenv("VECHER_CONTENT_EMBEDDINGS", str(path))
    assert load_embedding_index() == (None, "lexical")
    a = material("a", "Рецепт ужина")
    provider = FakeEmbeddingProvider()
    index = build_embedding_index([a], provider)
    path.write_text(json.dumps(index), encoding="utf-8")
    loaded, first_revision = load_embedding_index()
    assert loaded is not None and first_revision.startswith("embeddings:")
    assert embedding_similarity(loaded, "a", analyze_content(a), "a", analyze_content(a)) == pytest.approx(1)
    changed = {**a, "title": "Научный эксперимент"}
    index = build_embedding_index([changed], provider)
    path.write_text(json.dumps(index), encoding="utf-8")
    loaded, second_revision = load_embedding_index()
    assert second_revision != first_revision
    assert embedding_similarity(loaded, "a", analyze_content(a), "a", analyze_content(a)) is None
    path.write_text('{"version":1,"private_history":[]}', encoding="utf-8")
    assert load_embedding_index() == (None, "lexical")
    path.unlink()
    path.symlink_to(tmp_path)
    assert load_embedding_index() == (None, "lexical")
