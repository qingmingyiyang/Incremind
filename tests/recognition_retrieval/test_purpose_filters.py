from __future__ import annotations

from collections.abc import Mapping, Sequence

from backend.recognition_retrieval import retrieve


def _entry(item_id: str, content: str) -> dict[str, object]:
    return {
        "id": item_id,
        "revision": 1,
        "current_revision": 1,
        "project_id": "project-a",
        "content": content,
        "status": "active",
        "authorized": True,
        "source_refs": [],
    }


class _CaptureEmbedding:
    model_id = "purpose-filter-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls.append(tuple(texts))
        return tuple((1.0, 0.0) for _ in texts)


class _CaptureReranker:
    def __init__(self) -> None:
        self.candidates: tuple[Mapping[str, object], ...] = ()

    def rerank(self, *, query: str, candidates: Sequence[Mapping[str, object]]) -> Mapping[str, float]:
        self.candidates = tuple(candidates)
        return {str(candidate["id"]): 1.0 for candidate in candidates}


class _FailingCache:
    def __init__(self) -> None:
        self.reads = 0

    def read(self, *, model_id: str, entry: object) -> Sequence[float] | None:
        self.reads += 1
        raise AssertionError("policy-denied entry must not be read from the embedding cache")

    def write(self, *, model_id: str, entry: object, vector: Sequence[float]) -> None:
        raise AssertionError("policy-denied entry must not be written to the embedding cache")


def test_mixed_policy_keeps_lexical_hits_while_external_providers_only_receive_allowed_content() -> None:
    embedding = _CaptureEmbedding()
    reranker = _CaptureReranker()
    result = retrieve(
        "project-a",
        "网页 优先",
        [_entry("allowed", "网页 优先可以继续验证。"), _entry("denied", "网页 优先仍保留在本地。")],
        embedding_provider=embedding,
        reranker=reranker,
        embedding_allowed_ids={"allowed"},
        rerank_allowed_ids={"allowed"},
    )

    assert set(hit.id for hit in result.hits) == {"allowed", "denied"}
    assert embedding.calls == [("网页 优先", "网页 优先可以继续验证。")]
    assert [candidate["id"] for candidate in reranker.candidates] == ["allowed"]
    denied = next(hit for hit in result.hits if hit.id == "denied")
    assert denied.vector_score is None
    assert denied.rerank_score is None
    assert result.trace["vector"]["excluded_by_policy"] == 1
    assert result.trace["rerank"]["excluded_by_policy"] == 1


def test_empty_embedding_allow_list_skips_provider_and_cache_reads() -> None:
    embedding = _CaptureEmbedding()
    reranker = _CaptureReranker()
    cache = _FailingCache()
    result = retrieve(
        "project-a",
        "网页 优先",
        [_entry("denied", "网页 优先仍保留在本地。")],
        embedding_provider=embedding,
        reranker=reranker,
        embedding_cache=cache,
        embedding_allowed_ids=set(),
        rerank_allowed_ids=set(),
    )

    assert embedding.calls == []
    assert cache.reads == 0
    assert reranker.candidates == ()
    assert result.trace["vector"] == {
        "status": "policy_restricted",
        "candidate_ids": [],
        "excluded_by_policy": 1,
    }
    assert result.trace["rerank"] == {
        "status": "policy_restricted",
        "candidate_ids": [],
        "excluded_by_policy": 1,
    }


def test_embedding_and_rerank_allow_lists_are_independent() -> None:
    embedding = _CaptureEmbedding()
    reranker = _CaptureReranker()
    result = retrieve(
        "project-a",
        "网页 优先",
        [_entry("embedding-only", "网页 优先用于嵌入。"), _entry("rerank-only", "网页 优先用于重排。")],
        embedding_provider=embedding,
        reranker=reranker,
        embedding_allowed_ids={"embedding-only"},
        rerank_allowed_ids={"rerank-only"},
    )

    assert embedding.calls == [("网页 优先", "网页 优先用于嵌入。")]
    assert [candidate["id"] for candidate in reranker.candidates] == ["rerank-only"]
    assert result.trace["vector"]["candidate_ids"] == ["embedding-only"]
    assert result.trace["rerank"]["candidate_ids"] == ["rerank-only"]
