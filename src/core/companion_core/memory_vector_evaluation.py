from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
import statistics
import time
import tempfile

from core.product_core.companion_memory_boundary import (
    IndexSourceRecord,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5ActiveIndex,
    SqliteFts5DryRunIndex,
    create_index_rebuild_request,
    create_sqlite_fts5_manifest,
    evaluate_index_freshness,
    select_default_recall_backend_policy,
    sqlite_fts5_manifest_payload,
)


_TRUST = {"user_confirmed", "trusted"}
_MAX_DOCUMENTS = 10_000
_MAX_QUERIES = 2_000


class CompanionMemoryEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EvaluationDocument:
    document_id: str
    content: str
    source_id: str
    trust: str
    published: bool
    withdrawn: bool


@dataclass(frozen=True, slots=True)
class EvaluationQuery:
    query_id: str
    text: str
    relevant_ids: tuple[str, ...]
    excluded_ids: tuple[str, ...]
    no_answer: bool


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    document_id: str
    score: float


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    backend: str
    query_count: int
    recall_at_1: float
    recall_at_4: float
    mrr: float
    excluded_hit_rate: float
    no_answer_injection_rate: float
    latency_p50_ms: float
    latency_p95_ms: float
    mean_retrieved_items: float
    mean_retrieved_chars: float
    p95_retrieved_chars: float


@dataclass(frozen=True, slots=True)
class EvaluationCorpus:
    version: str
    documents: tuple[EvaluationDocument, ...]
    queries: tuple[EvaluationQuery, ...]

    @property
    def eligible_documents(self) -> tuple[EvaluationDocument, ...]:
        return tuple(
            item for item in self.documents
            if item.published and not item.withdrawn and item.trust in _TRUST and bool(item.source_id)
        )


Search = Callable[[str, int], Sequence[RetrievalHit]]


def evaluate_fts5_baseline(
    corpus: EvaluationCorpus,
    *,
    work_root: Path | None = None,
) -> tuple[EvaluationMetrics, int, float]:
    """Evaluate the repository's real FTS5 adapter in an isolated directory."""

    owner = None
    if work_root is None:
        owner = tempfile.TemporaryDirectory(prefix="chriptmas-cp-f04-")
        work_root = Path(owner.name)
    started = time.perf_counter()
    database_path = work_root / "recall-fts5.db"
    entries = tuple(
        RecallIndexEntry(
            object_id=item.document_id,
            project_id="cp-f04-fictional",
            layer="l1_atom",
            content=item.content,
            source_refs=(f"{item.source_id}#fictional",),
            trust_status=item.trust,
            base_score=0.5,
        )
        for item in corpus.eligible_documents
    )
    manifest = _fts5_manifest(corpus)
    try:
        built = SqliteFts5DryRunIndex(database_path).rebuild_and_query(
            entries,
            manifest=manifest,
            query=RecallQuery(
                text="CP_F04_BUILD_ONLY_NO_MATCH",
                project_id="cp-f04-fictional",
                layers=("l1_atom",),
                allowed_trust_statuses=("user_confirmed", "trusted"),
                limit=4,
            ),
        )
        rebuild_ms = (time.perf_counter() - started) * 1_000
        active = SqliteFts5ActiveIndex({**manifest, "database_uri": built.database_uri})

        def search(text: str, limit: int) -> tuple[RetrievalHit, ...]:
            hits = active.recall(
                RecallQuery(
                    text=text,
                    project_id="cp-f04-fictional",
                    layers=("l1_atom",),
                    allowed_trust_statuses=("user_confirmed", "trusted"),
                    limit=limit,
                )
            )
            return tuple(RetrievalHit(item.object_id, item.score) for item in hits)

        metrics = evaluate_retriever(corpus, backend="sqlite_fts5", search=search)
        disk_bytes = database_path.stat().st_size
        return metrics, disk_bytes, rebuild_ms
    finally:
        if owner is not None:
            owner.cleanup()


def evaluate_vector_embeddings(
    corpus: EvaluationCorpus,
    *,
    embed: Callable[[Sequence[str]], Sequence[Sequence[float]]],
) -> tuple[EvaluationMetrics, EvaluationMetrics, int, float, float]:
    """Evaluate vector and RRF hybrid retrieval with an injected offline embedder."""

    documents = corpus.eligible_documents
    rebuild_started = time.perf_counter()
    vectors = tuple(_unit_vector(item) for item in embed([doc.content for doc in documents]))
    if len(vectors) != len(documents):
        raise CompanionMemoryEvaluationError("embedding result count is invalid")
    rebuild_ms = (time.perf_counter() - rebuild_started) * 1_000
    disk_bytes = sum(len(vector) * 4 for vector in vectors)

    def vector_search(text: str, limit: int) -> tuple[RetrievalHit, ...]:
        query_vectors = tuple(embed([text]))
        if len(query_vectors) != 1:
            raise CompanionMemoryEvaluationError("query embedding result is invalid")
        query_vector = _unit_vector(query_vectors[0])
        ranked = sorted(
            (
                RetrievalHit(document.document_id, _dot(query_vector, vector))
                for document, vector in zip(documents, vectors, strict=True)
            ),
            key=lambda item: (-item.score, item.document_id),
        )
        return tuple(item for item in ranked if item.score >= 0.5)[:limit]

    fts_metrics, _, _ = evaluate_fts5_baseline(corpus)
    with tempfile.TemporaryDirectory(prefix="chriptmas-cp-f04-hybrid-") as raw_root:
        root = Path(raw_root)
        entries = tuple(
            RecallIndexEntry(
                object_id=item.document_id,
                project_id="cp-f04-fictional",
                layer="l1_atom",
                content=item.content,
                source_refs=(f"{item.source_id}#fictional",),
                trust_status=item.trust,
                base_score=0.5,
            )
            for item in documents
        )
        manifest = _fts5_manifest(corpus)
        built = SqliteFts5DryRunIndex(root / "recall-fts5.db").rebuild_and_query(
            entries,
            manifest=manifest,
            query=RecallQuery("CP_F04_BUILD_ONLY_NO_MATCH", "cp-f04-fictional", ("l1_atom",), ("user_confirmed", "trusted"), 4),
        )
        active = SqliteFts5ActiveIndex({**manifest, "database_uri": built.database_uri})

        def lexical_search(text: str, limit: int) -> tuple[RetrievalHit, ...]:
            hits = active.recall(RecallQuery(text, "cp-f04-fictional", ("l1_atom",), ("user_confirmed", "trusted"), limit))
            return tuple(RetrievalHit(item.object_id, item.score) for item in hits)

        vector_metrics = evaluate_retriever(corpus, backend="vector", search=vector_search)
        hybrid_metrics = evaluate_retriever(
            corpus,
            backend="hybrid_rrf",
            search=lambda text, limit: hybrid_hits(
                lexical_search(text, limit), vector_search(text, limit), limit=limit
            ),
        )
    return vector_metrics, hybrid_metrics, disk_bytes, rebuild_ms, fts_metrics.latency_p95_ms


def load_evaluation_corpus(path: Path) -> EvaluationCorpus:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CompanionMemoryEvaluationError("evaluation corpus is unavailable") from exc
    if len(raw) > 2 * 1024 * 1024:
        raise CompanionMemoryEvaluationError("evaluation corpus is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionMemoryEvaluationError("evaluation corpus is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"version", "documents", "queries"}:
        raise CompanionMemoryEvaluationError("evaluation corpus schema is invalid")
    version = value.get("version")
    documents_value, queries_value = value.get("documents"), value.get("queries")
    if not isinstance(version, str) or not version or not isinstance(documents_value, list) or not isinstance(queries_value, list):
        raise CompanionMemoryEvaluationError("evaluation corpus schema is invalid")
    if not 1 <= len(documents_value) <= _MAX_DOCUMENTS or not 1 <= len(queries_value) <= _MAX_QUERIES:
        raise CompanionMemoryEvaluationError("evaluation corpus size is invalid")

    documents: list[EvaluationDocument] = []
    document_ids: set[str] = set()
    for item in documents_value:
        if not isinstance(item, dict) or set(item) != {"id", "content", "source_id", "trust", "published", "withdrawn"}:
            raise CompanionMemoryEvaluationError("evaluation document schema is invalid")
        document_id = _text(item.get("id"), 80, "document id")
        content = _text(item.get("content"), 2_000, "document content")
        source_id = _text(item.get("source_id"), 120, "document source")
        trust = item.get("trust")
        if document_id in document_ids or trust not in {"user_confirmed", "trusted", "unreviewed"}:
            raise CompanionMemoryEvaluationError("evaluation document identity is invalid")
        if not isinstance(item.get("published"), bool) or not isinstance(item.get("withdrawn"), bool):
            raise CompanionMemoryEvaluationError("evaluation document state is invalid")
        document_ids.add(document_id)
        documents.append(EvaluationDocument(document_id, content, source_id, trust, item["published"], item["withdrawn"]))

    queries: list[EvaluationQuery] = []
    query_ids: set[str] = set()
    for item in queries_value:
        if not isinstance(item, dict) or set(item) != {"id", "text", "relevant_ids", "excluded_ids", "no_answer"}:
            raise CompanionMemoryEvaluationError("evaluation query schema is invalid")
        query_id = _text(item.get("id"), 80, "query id")
        query_text = _text(item.get("text"), 500, "query text")
        relevant = _id_list(item.get("relevant_ids"), document_ids)
        excluded = _id_list(item.get("excluded_ids"), document_ids)
        no_answer = item.get("no_answer")
        if query_id in query_ids or not isinstance(no_answer, bool) or set(relevant) & set(excluded):
            raise CompanionMemoryEvaluationError("evaluation query identity is invalid")
        if no_answer != (len(relevant) == 0):
            raise CompanionMemoryEvaluationError("evaluation no-answer contract is invalid")
        query_ids.add(query_id)
        queries.append(EvaluationQuery(query_id, query_text, relevant, excluded, no_answer))
    return EvaluationCorpus(version, tuple(documents), tuple(queries))


def evaluate_retriever(corpus: EvaluationCorpus, *, backend: str, search: Search, limit: int = 4) -> EvaluationMetrics:
    if not isinstance(backend, str) or not backend or not 1 <= limit <= 20:
        raise CompanionMemoryEvaluationError("evaluation backend contract is invalid")
    recalls_1: list[float] = []
    recalls_4: list[float] = []
    reciprocals: list[float] = []
    excluded_hits = 0
    no_answer_hits = 0
    latencies: list[float] = []
    retrieved_items: list[int] = []
    retrieved_chars: list[int] = []
    eligible = {item.document_id for item in corpus.eligible_documents}
    content_chars = {item.document_id: len(item.content) for item in corpus.eligible_documents}
    for query in corpus.queries:
        started = time.perf_counter()
        hits = tuple(search(query.text, limit))
        latencies.append((time.perf_counter() - started) * 1_000)
        _validate_hits(hits, eligible, limit)
        ids = tuple(hit.document_id for hit in hits)
        retrieved_items.append(len(ids))
        retrieved_chars.append(sum(content_chars[item] for item in ids))
        if query.no_answer:
            no_answer_hits += int(bool(ids))
        else:
            relevant = set(query.relevant_ids)
            recalls_1.append(len(relevant & set(ids[:1])) / len(relevant))
            recalls_4.append(len(relevant & set(ids[:4])) / len(relevant))
            rank = next((index for index, document_id in enumerate(ids, 1) if document_id in relevant), None)
            reciprocals.append(0.0 if rank is None else 1.0 / rank)
        excluded_hits += int(bool(set(ids) & set(query.excluded_ids)))
    return EvaluationMetrics(
        backend=backend,
        query_count=len(corpus.queries),
        recall_at_1=_mean(recalls_1), recall_at_4=_mean(recalls_4), mrr=_mean(reciprocals),
        excluded_hit_rate=excluded_hits / len(corpus.queries),
        no_answer_injection_rate=no_answer_hits / max(1, sum(query.no_answer for query in corpus.queries)),
        latency_p50_ms=_percentile(latencies, 0.50), latency_p95_ms=_percentile(latencies, 0.95),
        mean_retrieved_items=_mean(retrieved_items), mean_retrieved_chars=_mean(retrieved_chars),
        p95_retrieved_chars=_percentile(retrieved_chars, 0.95),
    )


def hybrid_hits(lexical: Sequence[RetrievalHit], vector: Sequence[RetrievalHit], *, limit: int = 4, vector_threshold: float = 0.5) -> tuple[RetrievalHit, ...]:
    if not 1 <= limit <= 20 or not math.isfinite(vector_threshold):
        raise CompanionMemoryEvaluationError("hybrid settings are invalid")
    scores: dict[str, float] = {}
    for rank, hit in enumerate(lexical, 1):
        _validate_hit(hit)
        scores[hit.document_id] = scores.get(hit.document_id, 0.0) + 1.0 / (60 + rank)
    for rank, hit in enumerate(vector, 1):
        _validate_hit(hit)
        if hit.score < vector_threshold:
            continue
        scores[hit.document_id] = scores.get(hit.document_id, 0.0) + 1.0 / (60 + rank)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return tuple(RetrievalHit(document_id, score) for document_id, score in ranked)


def decision(fts: EvaluationMetrics, hybrid: EvaluationMetrics, *, cold_start_ms: float, rebuild_ms: float, disk_bytes: int) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if hybrid.recall_at_4 + 1e-9 < fts.recall_at_4 + 0.10: reasons.append("recall_gain_below_10pp")
    if hybrid.mrr + 1e-9 < fts.mrr: reasons.append("mrr_regressed")
    if hybrid.no_answer_injection_rate > 0.05: reasons.append("no_answer_injection_above_5pct")
    if hybrid.excluded_hit_rate != 0: reasons.append("excluded_canary_recalled")
    if hybrid.latency_p95_ms > 250: reasons.append("warm_p95_above_250ms")
    if hybrid.mean_retrieved_items > 4: reasons.append("mean_retrieved_items_above_4")
    if hybrid.p95_retrieved_chars > 2_000: reasons.append("retrieved_chars_p95_above_2000")
    if not math.isfinite(cold_start_ms) or cold_start_ms > 5_000: reasons.append("cold_start_above_5s")
    if not math.isfinite(rebuild_ms) or rebuild_ms > 30_000: reasons.append("rebuild_above_30s")
    if not isinstance(disk_bytes, int) or disk_bytes < 0 or disk_bytes > 250 * 1024 * 1024: reasons.append("disk_above_250mib")
    return ("ready", ()) if not reasons else ("deferred", tuple(reasons))


def _fts5_manifest(
    corpus: EvaluationCorpus,
) -> dict[str, object]:
    sources = tuple(
        IndexSourceRecord(
            source_id=item.source_id,
            revision=1,
            content_hash=f"fictional-{item.document_id}",
            updated_at="2026-07-23T00:00:00+08:00",
        )
        for item in corpus.eligible_documents
    )
    selection = select_default_recall_backend_policy()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, sources),
        backend_selection=selection,
        sources=sources,
        requested_at="2026-07-23T00:00:01+08:00",
    )
    return sqlite_fts5_manifest_payload(
        create_sqlite_fts5_manifest(
            rebuild_request=request,
            backend_selection=selection,
            created_at="2026-07-23T00:00:02+08:00",
        )
    )


def _unit_vector(vector: Sequence[float]) -> tuple[float, ...]:
    try:
        values = tuple(float(value) for value in vector)
    except (TypeError, ValueError) as exc:
        raise CompanionMemoryEvaluationError("embedding vector is invalid") from exc
    if not values or any(not math.isfinite(value) for value in values):
        raise CompanionMemoryEvaluationError("embedding vector is invalid")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        raise CompanionMemoryEvaluationError("embedding vector is empty")
    return tuple(value / norm for value in values)


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise CompanionMemoryEvaluationError("embedding dimensions do not match")
    return sum(first * second for first, second in zip(left, right, strict=True))


def _validate_hits(hits: Sequence[RetrievalHit], eligible: set[str], limit: int) -> None:
    if len(hits) > limit or len({item.document_id for item in hits}) != len(hits):
        raise CompanionMemoryEvaluationError("retriever returned invalid hit count")
    for hit in hits:
        _validate_hit(hit)
        if hit.document_id not in eligible:
            raise CompanionMemoryEvaluationError("retriever returned ineligible evidence")


def _validate_hit(hit: RetrievalHit) -> None:
    if not isinstance(hit, RetrievalHit) or not hit.document_id or not math.isfinite(hit.score):
        raise CompanionMemoryEvaluationError("retriever returned an invalid hit")


def _text(value: object, maximum: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise CompanionMemoryEvaluationError(f"evaluation {label} is invalid")
    return value.strip()


def _id_list(value: object, known: set[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 20 or any(not isinstance(item, str) or item not in known for item in value):
        raise CompanionMemoryEvaluationError("evaluation query references are invalid")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise CompanionMemoryEvaluationError("evaluation query references are duplicated")
    return result


def _mean(values: Sequence[float]) -> float:
    return 0.0 if not values else statistics.fmean(values)


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values: return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]
