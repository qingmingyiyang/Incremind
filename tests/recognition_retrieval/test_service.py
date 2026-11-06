from __future__ import annotations

from collections.abc import Mapping, Sequence
from random import Random

import pytest

from backend.recognition_retrieval import (
    EmbeddingCache,
    HttpEmbeddingProvider,
    RecognitionRetrievalError,
    SQLiteEmbeddingCache,
    SqliteVecCandidateIndex,
    retrieve,
)


def test_batched_cosine_preserves_rounded_scores_and_order():
    from backend.recognition_retrieval.service import _cosine, _cosine_batch, _ordered_ids

    random = Random(706)
    query = tuple(random.uniform(-1, 1) for _ in range(256))
    vectors = tuple(tuple(random.uniform(-1, 1) for _ in range(256)) for _ in range(1000))
    vectors += ((0.0,) * len(query), vectors[0], query)
    expected = tuple(_cosine(query, vector) for vector in vectors)
    actual = _cosine_batch(query, vectors)
    assert actual == expected
    keys = [f'record-{index:04}' for index in range(len(vectors))]
    assert _ordered_ids(dict(zip(keys, actual, strict=True))) == _ordered_ids(dict(zip(keys, expected, strict=True)))
    assert _cosine_batch(query, ()) == ()
    assert _cosine_batch((0.0, 0.0), ((1.0, 1.0),)) == (0.0,)
    with pytest.raises(RecognitionRetrievalError, match='inconsistent dimensions'):
        _cosine_batch(query, ((0.0, 0.0),))
    with pytest.raises(RecognitionRetrievalError, match='finite values'):
        _cosine_batch((float('nan'),), ((0.0,),))


def test_sqlite_vector_batch_uses_all_four_cache_keys(tmp_path):
    from backend.recognition_retrieval.service import _RecognitionEntry

    cache = SQLiteEmbeddingCache(str(tmp_path / 'batch-vectors.sqlite3'))
    first = _RecognitionEntry('first', 2, '原文', 'project-a', ())
    second = _RecognitionEntry('second', 3, '其他', 'project-b', ())
    wrong_project = _RecognitionEntry('first', 2, '原文', 'project-b', ())
    old_revision = _RecognitionEntry('second', 2, '其他', 'project-b', ())
    try:
        cache.write(model_id='model-a', entry=first, vector=(1.0, 0.0))
        cache.write(model_id='model-a', entry=second, vector=(0.0, 1.0))
        cache.write(model_id='model-b', entry=first, vector=(1.0, 1.0))
        assert cache.read_many(model_id='model-a', entries=(first, second)) == {
            'first': (1.0, 0.0), 'second': (0.0, 1.0)}
        assert cache.read_many(model_id='model-a', entries=(wrong_project, old_revision)) == {}
        assert cache.read_many(model_id='model-b', entries=(first, second)) == {'first': (1.0, 1.0)}
    finally:
        cache.close()


def _entry(item_id: str, content: str, **overrides: object) -> dict[str, object]:
    return {
        "id": item_id,
        "revision": 2,
        "current_revision": 2,
        "project_id": "project-a",
        "content": content,
        "status": "published",
        "authorized": True,
        "source_refs": [f"source-{item_id}#note"],
        **overrides,
    }


class _FakeEmbedding:
    def __init__(self, vectors: Sequence[Sequence[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        assert texts
        return self._vectors


class _FakeReranker:
    def rerank(self, *, query: str, candidates: Sequence[Mapping[str, object]]) -> Mapping[str, float]:
        assert query
        return {str(candidate["id"]): 1.0 if candidate["id"] == "semantic" else 0.0 for candidate in candidates}


class _CaptureReranker:
    def __init__(self) -> None:
        self.candidates: tuple[Mapping[str, object], ...] = ()

    def rerank(self, *, query: str, candidates: Sequence[Mapping[str, object]]) -> Mapping[str, float]:
        self.candidates = tuple(candidates)
        return {str(candidate["id"]): 1.0 for candidate in candidates}


class _BrokenReranker:
    def rerank(self, *, query: str, candidates: Sequence[Mapping[str, object]]) -> Mapping[str, float]:
        raise RuntimeError("not available")


class _CountingEmbedding:
    model_id = "test-model"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls.append(tuple(texts))
        return tuple((1.0, 0.0) for _ in texts)


class _OutOfOrderEmbeddingClient:
    def post_json(self, *, endpoint: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        assert endpoint == "https://example.test/embeddings"
        assert payload["input"] == ["first", "second"]
        return {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        }


def test_keyword_baseline_filters_project_authorization_and_stale_revisions() -> None:
    result = retrieve(
        "project-a",
        "网页优先",
        [
            _entry("current", "当前阶段网页优先，桌面壳后置。"),
            _entry("other-project", "网页优先", project_id="project-b"),
            _entry("unauthorized", "网页优先", authorized=False),
            _entry("stale", "网页优先", status="superseded"),
            _entry("old-revision", "网页优先", revision=1, current_revision=2),
        ],
    )

    assert [hit.id for hit in result.hits] == ["current"]
    assert result.trace["excluded"] == {"other_project": 1, "unauthorized": 1, "stale": 2, "invalid": 0}
    assert result.trace["vector"] == {"status": "not_configured"}


def test_embedding_fuses_semantic_candidate_without_claiming_a_real_model() -> None:
    result = retrieve(
        "project-a",
        "如何加快迭代",
        [
            _entry("literal", "如何加快迭代，减少重复步骤。"),
            _entry("semantic", "当前采用网页业务链，避免在桌面构建上耗时。"),
        ],
        embedding_provider=_FakeEmbedding(((1.0, 0.0), (0.1, 0.9), (1.0, 0.0))),
    )

    assert result.hits[0].id == "semantic"
    assert result.hits[0].vector_score == 1.0
    assert result.trace["vector"]["status"] == "used"
    assert result.trace["vector"]["backend"] == "provider_cosine"


def test_embedding_failure_degrades_to_keyword_without_fabricating_vector_scores() -> None:
    result = retrieve(
        "project-a",
        "网页",
        [_entry("match", "网页优先。")],
        embedding_provider=_FakeEmbedding(()),
    )

    assert [hit.id for hit in result.hits] == ["match"]
    assert result.hits[0].vector_score is None
    assert result.trace["vector"] == {"status": "degraded", "reason": "embedding provider returned an unexpected vector count"}


def test_rerank_changes_bounded_candidates_and_failure_preserves_hybrid_order() -> None:
    entries = [_entry("literal", "网页项目的网页阶段。"), _entry("semantic", "桌面构建延后。")]
    embedding = _FakeEmbedding(((1.0, 0.0), (1.0, 0.0), (0.1, 0.9)))

    reranked = retrieve("project-a", "网页", entries, embedding_provider=embedding, reranker=_FakeReranker())
    degraded = retrieve("project-a", "网页", entries, embedding_provider=embedding, reranker=_BrokenReranker())

    assert reranked.hits[0].id == "semantic"
    assert reranked.hits[0].rerank_score == 1.0
    assert reranked.trace["rerank"]["status"] == "used"
    assert degraded.hits[0].id == "literal"
    assert degraded.hits[0].rerank_score is None
    assert degraded.trace["rerank"] == {"status": "degraded", "reason": "provider_or_index_failed"}


def test_empty_and_invalid_limits_are_explicit() -> None:
    empty = retrieve("project-a", "无匹配", [])
    assert empty.hits == ()
    assert empty.trace["result"] == {"status": "empty", "hit_ids": []}

    try:
        retrieve("project-a", "x", [], limit=0)
    except RecognitionRetrievalError as error:
        assert str(error) == "limits must be positive"
    else:
        raise AssertionError("expected invalid retrieval limit to fail")


def test_zero_keyword_scores_do_not_become_lexical_hits() -> None:
    result = retrieve("project-a", "完全不相干", [_entry("stored", "网页优先，桌面壳后置。")])

    assert result.hits == ()
    assert result.trace["result"] == {"status": "empty", "hit_ids": []}


def test_chinese_lexical_baseline_requires_meaningful_phrase_overlap() -> None:
    entries = [
        _entry("web", "当前原型优先通过浏览器网页验证完整业务链；在最终版本可行前不投入 Electron 打包。"),
        _entry("style", "测试页面使用浅色背景和圆角卡片。"),
    ]

    no_answer = retrieve("project-a", "下周虚构团队午餐订在哪里？", entries)
    style_only = retrieve("project-a", "记忆系统首版最重要的是不是浅色圆角卡片？", entries)
    positive = retrieve("project-a", "原型阶段从哪里打开和验证完整链路？", entries)

    assert no_answer.hits == ()
    assert style_only.hits == ()
    assert [hit.id for hit in positive.hits] == ["web"]
    assert positive.trace["keyword"]["calibration"] == "development_tuned_unvalidated"


def test_vector_search_can_recall_authorized_candidate_outside_lexical_candidates() -> None:
    result = retrieve(
        "project-a",
        "网页",
        [
            _entry("lexical", "网页优先。"),
            _entry("semantic-only", "暂缓桌面构建以缩短验证周期。"),
        ],
        embedding_provider=_FakeEmbedding(((1.0, 0.0), (0.0, 1.0), (1.0, 0.0))),
        vector_candidate_limit=1,
    )

    assert [hit.id for hit in result.hits] == ["semantic-only", "lexical"]
    assert result.trace["vector"]["candidate_ids"] == ["semantic-only"]


def test_embedding_cache_keys_vectors_by_model_id_entry_id_and_revision() -> None:
    provider = _CountingEmbedding()
    cache = EmbeddingCache()
    first = _entry("recognition", "网页优先。", revision=1, current_revision=1)

    retrieve("project-a", "网页", [first], embedding_provider=provider, embedding_cache=cache)
    retrieve("project-a", "网页", [first], embedding_provider=provider, embedding_cache=cache)
    changed = _entry("recognition", "网页优先，继续保留网页入口。", revision=2, current_revision=2)
    retrieve("project-a", "网页", [changed], embedding_provider=provider, embedding_cache=cache)

    assert provider.calls == [
        ("网页", "网页优先。"),
        ("网页",),
        ("网页", "网页优先，继续保留网页入口。"),
    ]


def test_openai_compatible_embedding_adapter_sorts_by_response_index() -> None:
    provider = HttpEmbeddingProvider(
        client=_OutOfOrderEmbeddingClient(),
        endpoint="https://example.test/embeddings",
        model="test-embedding",
    )

    assert provider.embed(("first", "second")) == ((1.0, 0.0), (0.0, 1.0))


def test_structured_recognition_sources_are_retained_in_hit_trace() -> None:
    reranker = _CaptureReranker()
    result = retrieve(
        "project-a",
        "网页",
        [_entry("current", "网页优先。", source_refs=[{"type": "experience", "id": "exp-1", "revision": 4}, {"type": "recognition", "id": "rec-0"}])],
        reranker=reranker,
    )

    assert result.hits[0].source_refs == ("experience:exp-1@revision:4", "recognition:rec-0")
    assert reranker.candidates[0]["source_refs"] == ["experience:exp-1@revision:4", "recognition:rec-0"]


def test_sqlite_embedding_cache_survives_restart_is_project_isolated_and_can_purge(tmp_path) -> None:
    path = tmp_path / "vectors.sqlite3"
    first_provider = _CountingEmbedding()
    project_a = _entry("same-id", "网页优先。", revision=1, current_revision=1)
    project_b = _entry("same-id", "桌面构建后置。", project_id="project-b", revision=1, current_revision=1)
    cache = SQLiteEmbeddingCache(str(path))
    retrieve("project-a", "网页", [project_a], embedding_provider=first_provider, embedding_cache=cache)
    cache.close()

    restarted = SQLiteEmbeddingCache(str(path))
    second_provider = _CountingEmbedding()
    retrieve("project-a", "网页", [project_a], embedding_provider=second_provider, embedding_cache=restarted)
    retrieve("project-b", "网页", [project_b], embedding_provider=second_provider, embedding_cache=restarted)

    assert second_provider.calls == [("网页",), ("网页", "桌面构建后置。")]
    assert restarted.purge_invalid(project_id="project-a", current_revisions={}) == 1
    assert restarted.delete_recognition(project_id="project-b", recognition_id="same-id") == 1
    restarted.close()


def test_actual_sqlite_vec_cosine_scores_match_fallback() -> None:
    pytest.importorskip("sqlite_vec")
    entries = [_entry("aligned", "网页优先。"), _entry("orthogonal", "桌面构建后置。")]
    provider = _FakeEmbedding(((1.0, 0.0), (1.0, 0.0), (0.0, 1.0)))

    fallback = retrieve("project-a", "网页", entries, embedding_provider=provider, vector_candidate_limit=2)
    indexed = retrieve(
        "project-a",
        "网页",
        entries,
        embedding_provider=provider,
        sqlite_vec_index=SqliteVecCandidateIndex(),
        vector_candidate_limit=2,
    )

    assert {hit.id: hit.vector_score for hit in indexed.hits} == {
        hit.id: hit.vector_score for hit in fallback.hits
    }
