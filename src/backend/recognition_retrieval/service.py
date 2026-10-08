"""Recognition retrieval with explicit, inspectable degradation paths.

No environment variables, model routes, or credentials are read here.  Model
providers are injected by the application composition layer.  The lexical
baseline always remains available when optional embedding, sqlite-vec, or
reranking capabilities are unavailable.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import json
import re
import sqlite3
from typing import Protocol
from core.storage_provider.observability import stage, observe_connection


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
_CURRENT_STATUSES = frozenset({"active", "current", "published"})
_STALE_STATUSES = frozenset({"superseded", "archived", "deleted", "invalid", "draft"})
# Character-level Chinese overlap makes nearly every ordinary sentence look
# relevant.  These ubiquitous two-character units carry no retrieval signal;
# the list is deliberately conservative and applies before scoring.
_CHINESE_STOP_TERMS = frozenset({
    "一个", "一些", "可以", "应该", "当前", "怎样", "怎么", "什么", "是否", "需要",
    "项目", "认识", "记忆", "内容", "这条", "一条", "用户", "系统", "时候", "现在",
    "如果", "因为", "以及", "进行", "问题", "相关", "直接", "自动", "所有", "每次",
    # Presentation-only language is deliberately not enough to inject a
    # long-lived recognition into an Agent context. It remains available when
    # the user explicitly selects it on the canvas.
    "页面", "背景", "浅色", "圆角", "卡片", "配色", "样式",
})
_MIN_KEYWORD_SCORE = 0.10


class RecognitionRetrievalError(ValueError):
    """Raised for caller input that cannot safely be used for retrieval."""


class EmbeddingProvider(Protocol):
    """Explicitly configured embedding capability; never a hidden fallback."""

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one finite vector for each input, in the same order."""


class EmbeddingCachePort(Protocol):
    """A derived-vector cache. It never authorizes a recognition entry."""

    def read(self, *, model_id: str, entry: "_RecognitionEntry") -> tuple[float, ...] | None:
        ...

    def write(self, *, model_id: str, entry: "_RecognitionEntry", vector: Sequence[float]) -> None:
        ...


@dataclass(slots=True)
class EmbeddingCache:
    """Process-local derived-vector cache keyed by authority version and model.

    A changed recognition revision necessarily uses a fresh key.  Persistence,
    eviction, and index rebuilding remain composition concerns, so this small
    cache cannot become another authority store.
    """

    values: dict[tuple[str, str, str, int], tuple[float, ...]]

    def __init__(self) -> None:
        self.values = {}

    def read(self, *, model_id: str, entry: "_RecognitionEntry") -> tuple[float, ...] | None:
        return self.values.get((model_id, entry.project_id, entry.id, entry.revision))

    def write(self, *, model_id: str, entry: "_RecognitionEntry", vector: Sequence[float]) -> None:
        self.values[(model_id, entry.project_id, entry.id, entry.revision)] = _vector(vector, "entry vector")


class SQLiteEmbeddingCache:
    """Optional durable cache for derived vectors, isolated by project and model.

    The cache contains no authorization state. Retrieval always filters fresh
    authority records before invoking it. ``purge_invalid`` accepts that fresh
    record view and removes stale/deleted revisions without content hashing.
    """

    def __init__(self, database_path: str, *, read_only: bool = False) -> None:
        self._database_path = database_path
        self._read_only = read_only
        self._connection = sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True) if read_only else sqlite3.connect(database_path)
        observe_connection(self._connection)
        if read_only:
            return
        try:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recognition_embedding_cache (
                    project_id TEXT NOT NULL,
                    recognition_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    model_id TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    PRIMARY KEY(project_id, recognition_id, revision, model_id)
                )
                """
            )
            self._connection.commit()
        except BaseException:
            # A failed constructor never reaches the caller's cache.close().
            self._connection.close()
            raise

    def read(self, *, model_id: str, entry: "_RecognitionEntry") -> tuple[float, ...] | None:
        row = self._connection.execute(
            """
            SELECT vector_json FROM recognition_embedding_cache
            WHERE project_id = ? AND recognition_id = ? AND revision = ? AND model_id = ?
            """,
            (entry.project_id, entry.id, entry.revision, model_id),
        ).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(row[0])
            if not isinstance(parsed, list):
                raise RecognitionRetrievalError("cached vector is invalid")
            return _vector(parsed, "cached vector")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            if self._read_only:
                raise RecognitionRetrievalError("cached vector is invalid") from exc
            self._connection.execute(
                """
                DELETE FROM recognition_embedding_cache
                WHERE project_id = ? AND recognition_id = ? AND revision = ? AND model_id = ?
                """,
                (entry.project_id, entry.id, entry.revision, model_id),
            )
            self._connection.commit()
            raise RecognitionRetrievalError("cached vector is invalid") from exc

    def write(self, *, model_id: str, entry: "_RecognitionEntry", vector: Sequence[float]) -> None:
        normalized = _vector(vector, "entry vector")
        self._connection.execute(
            """
            INSERT INTO recognition_embedding_cache(project_id, recognition_id, revision, model_id, vector_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, recognition_id, revision, model_id)
            DO UPDATE SET vector_json = excluded.vector_json
            """,
            (entry.project_id, entry.id, entry.revision, model_id, json.dumps(normalized, separators=(",", ":"))),
        )
        self._connection.commit()

    def delete_recognition(self, *, project_id: str, recognition_id: str) -> int:
        from core.search_and_recall.vector_cache_invalidation import delete_cached_recognition
        removed = delete_cached_recognition(self._connection, _required_string(project_id, 'project_id'),
                                           _required_string(recognition_id, 'recognition_id'))
        self._connection.commit()
        return removed

    def delete_namespace(self, *, project_id: str) -> int:
        """Invalidate one material's exact cache namespace, across its models."""
        from core.search_and_recall.vector_cache_invalidation import delete_cached_namespace
        removed = delete_cached_namespace(self._connection, _required_string(project_id, 'project_id'))
        self._connection.commit()
        return removed

    def namespaces(self) -> tuple[str, ...]:
        """Enumerate derived-cache scopes for scheduled maintenance only."""
        return tuple(row[0] for row in self._connection.execute(
            "SELECT DISTINCT project_id FROM recognition_embedding_cache ORDER BY project_id"
        ))

    def parents(self) -> tuple[tuple[str, str], ...]:
        """Enumerate cached identities for write-time invalidation only."""
        from core.search_and_recall.vector_cache_invalidation import cached_parents
        return cached_parents(self._connection)

    def purge_invalid(self, *, project_id: str, current_revisions: Mapping[str, int],
                      current_model_id: str | None = None, model_id_pattern: str | None = None) -> int:
        """Remove cached revisions not present in the caller's authority view."""

        project_id = _required_string(project_id, "project_id")
        if (current_model_id is None) != (model_id_pattern is None):
            raise RecognitionRetrievalError("cache model scope requires identity and pattern")
        if current_model_id is not None:
            current_model_id = _required_string(current_model_id, "current_model_id")
            model_id_pattern = re.compile(_required_string(model_id_pattern, "model_id_pattern"))
        normalized = {
            _required_string(recognition_id, "recognition_id"): revision
            for recognition_id, revision in current_revisions.items()
            if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1
        }
        rows = self._connection.execute(
            "SELECT recognition_id, revision, model_id FROM recognition_embedding_cache WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        stale = [
            (project_id, recognition_id, revision, model_id)
            for recognition_id, revision, model_id in rows
            if (normalized.get(recognition_id) != revision
                or current_model_id is not None and model_id_pattern.fullmatch(model_id) is not None
                and model_id != current_model_id)
        ]
        self._connection.executemany(
            """
            DELETE FROM recognition_embedding_cache
            WHERE project_id = ? AND recognition_id = ? AND revision = ? AND model_id = ?
            """,
            stale,
        )
        self._connection.commit()
        return len(stale)

    def close(self) -> None:
        self._connection.close()


class Reranker(Protocol):
    """Optional RAG reranker over a bounded candidate set."""

    def rerank(self, *, query: str, candidates: Sequence[Mapping[str, object]]) -> Mapping[str, float]:
        """Return a 0..1 relevance score for every candidate id."""


class JsonHttpClient(Protocol):
    """Small transport seam; the composition layer owns endpoint configuration."""

    def post_json(self, *, endpoint: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Make one configured JSON request."""


@dataclass(frozen=True, slots=True)
class HttpEmbeddingProvider:
    """Adapter for an OpenAI-compatible ``/embeddings`` response shape."""

    client: JsonHttpClient
    endpoint: str
    model: str
    config_revision: str = "1"

    @property
    def cache_identity(self) -> str:
        return f"{self.endpoint}|{self.model}|{self.config_revision}"

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        response = self.client.post_json(
            endpoint=self.endpoint,
            payload={"model": self.model, "input": list(texts)},
        )
        data = response.get("data")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise RecognitionRetrievalError("embedding provider response requires data")
        indexed: dict[int, Sequence[float]] = {}
        for item in data:
            if not isinstance(item, Mapping) or not isinstance(item.get("embedding"), Sequence):
                raise RecognitionRetrievalError("embedding provider response has invalid embedding")
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(texts) or index in indexed:
                raise RecognitionRetrievalError("embedding provider response has invalid or duplicate index")
            indexed[index] = item["embedding"]
        if set(indexed) != set(range(len(texts))):
            raise RecognitionRetrievalError("embedding provider response does not cover every input")
        return tuple(_vector(indexed[index], "provider embedding") for index in range(len(texts)))


@dataclass(frozen=True, slots=True)
class HttpReranker:
    """Adapter for a configured JSON reranking endpoint.

    The endpoint uses the common RAG shape ``documents`` and returns
    ``results[index, relevance_score]``.  Candidate ids stay local, avoiding
    accidental trust in an endpoint-provided identity.
    """

    client: JsonHttpClient
    endpoint: str
    model: str

    def rerank(self, *, query: str, candidates: Sequence[Mapping[str, object]]) -> Mapping[str, float]:
        response = self.client.post_json(
            endpoint=self.endpoint,
            payload={"model": self.model, "query": query, "documents": [candidate["content"] for candidate in candidates]},
        )
        raw_scores = response.get("results")
        if not isinstance(raw_scores, Sequence) or isinstance(raw_scores, (str, bytes)):
            raise RecognitionRetrievalError("reranker response requires results")
        scores: dict[str, float] = {}
        for item in raw_scores:
            if not isinstance(item, Mapping):
                raise RecognitionRetrievalError("reranker response score is invalid")
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(candidates):
                raise RecognitionRetrievalError("reranker response index is invalid")
            item_id = _required_string(candidates[index].get("id"), "reranker candidate id")
            if item_id in scores:
                raise RecognitionRetrievalError("reranker response index is duplicated")
            scores[item_id] = _score(item.get("relevance_score"), "reranker relevance_score")
        if set(scores) != {str(candidate["id"]) for candidate in candidates}:
            raise RecognitionRetrievalError("reranker response does not cover every candidate")
        return scores


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    id: str
    revision: int
    content: str
    project_id: str
    score: float
    keyword_score: float
    vector_score: float | None
    rerank_score: float | None
    source_refs: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "revision": self.revision,
            "content": self.content,
            "project_id": self.project_id,
            "score": self.score,
            "keyword_score": self.keyword_score,
            "vector_score": self.vector_score,
            "rerank_score": self.rerank_score,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    hits: tuple[RetrievalHit, ...]
    trace: Mapping[str, object]

    def payload(self) -> dict[str, object]:
        return {"hits": [hit.payload() for hit in self.hits], "trace": dict(self.trace)}


@dataclass(frozen=True, slots=True)
class _RecognitionEntry:
    id: str
    revision: int
    content: str
    project_id: str
    source_refs: tuple[str, ...]
    recall_state: str = "normal"


class SqliteVecCandidateIndex:
    """Optional actual sqlite-vec KNN query over already-authorized candidates.

    It imports and loads sqlite-vec only when used.  Failure is intentionally
    propagated to ``retrieve`` so the trace can record a lexical downgrade.
    The transient table is a capability proof and bounded request index, not
    the authority for recognition data or revisions.
    """

    def search(
        self,
        *,
        entries: Sequence[_RecognitionEntry],
        vectors: Sequence[Sequence[float]],
        query_vector: Sequence[float],
        limit: int,
    ) -> Mapping[str, float]:
        try:
            import sqlite_vec  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RecognitionRetrievalError("sqlite_vec_unavailable") from exc
        normalized_query = _vector(query_vector, "query vector")
        normalized_vectors = tuple(_vector(vector, "entry vector") for vector in vectors)
        if len(entries) != len(normalized_vectors):
            raise RecognitionRetrievalError("sqlite-vec entry/vector count mismatch")
        if not entries or limit <= 0:
            return {}
        dimension = len(normalized_query)
        if any(len(vector) != dimension for vector in normalized_vectors):
            raise RecognitionRetrievalError("sqlite-vec vector dimensions differ")
        connection = sqlite3.connect(":memory:")
        observe_connection(connection)
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            connection.execute(
                f"CREATE VIRTUAL TABLE recognition_vectors USING vec0(embedding float[{dimension}] distance_metric=cosine)"
            )
            connection.executemany(
                "INSERT INTO recognition_vectors(rowid, embedding) VALUES (?, ?)",
                ((index + 1, sqlite_vec.serialize_float32(vector)) for index, vector in enumerate(normalized_vectors)),
            )
            cursor = connection.execute(
                "SELECT rowid, distance FROM recognition_vectors WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (sqlite_vec.serialize_float32(normalized_query), min(limit, len(entries))),
            )
            rows = cursor.fetchall()
            return {
                entries[int(rowid) - 1].id: round(max(0.0, min(1.0, 1.0 - float(distance) / 2.0)), 6)
                for rowid, distance in rows
            }
        except sqlite3.DatabaseError as exc:
            raise RecognitionRetrievalError("sqlite_vec_query_failed") from exc
        finally:
            connection.close()


def retrieve(
    project_id: str,
    query: str,
    entries: Sequence[Mapping[str, object]],
    *,
    limit: int = 8,
    embedding_provider: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
    sqlite_vec_index: SqliteVecCandidateIndex | None = None,
    embedding_cache: EmbeddingCachePort | None = None,
    vector_candidate_limit: int = 24,
    keyword_enabled: bool = True,
    embedding_allowed_ids: Collection[str] | None = None,
    rerank_allowed_ids: Collection[str] | None = None,
) -> RetrievalResult:
    """Retrieve only current, authorized project recognitions and explain rank paths.

    Keyword retrieval remains deterministic.  Vector and rerank failures never
    fabricate semantic scores: their stage is omitted and recorded in trace.
    """

    project_id = _required_string(project_id, "project_id")
    query = _required_string(query, "query")
    if limit <= 0 or vector_candidate_limit <= 0:
        raise RecognitionRetrievalError("limits must be positive")
    if type(keyword_enabled) is not bool or (not keyword_enabled and embedding_provider is None):
        raise RecognitionRetrievalError("vector-only retrieval requires an embedding provider")
    embedding_allowed = _allowed_ids(embedding_allowed_ids, "embedding_allowed_ids")
    rerank_allowed = _allowed_ids(rerank_allowed_ids, "rerank_allowed_ids")
    accepted, excluded = _eligible_entries(project_id, entries)
    keyword_trace: dict[str, object] = {
        "status": "used",
        "backend": "sqlite_fts5",
        "minimum_score": _MIN_KEYWORD_SCORE,
        "minimum_matches": "min(2, meaningful_query_token_count)",
        "calibration": "development_tuned_unvalidated",
    }
    keyword_scores: dict[str, float] = {}
    if keyword_enabled:
        with stage("keyword"):
            try:
                fts_candidate_ids = _fts_keyword_candidate_ids(accepted, _tokens(query))
                keyword_entries = tuple(entry for entry in accepted if entry.id in fts_candidate_ids)
            except RecognitionRetrievalError as exc:
                # FTS5 is only a request-local candidate projection.  It must never
                # decide authority or make a successful lexical result disappear.
                keyword_entries = accepted
                keyword_trace.update({"status": "degraded", "backend": "python", "reason": str(exc)})
            keyword_scores = {
                entry.id: score
                for entry in keyword_entries
                if (score := _keyword_score(query, entry.content)) > 0.0
            }
    else:
        keyword_trace = {"status": "disabled"}
    lexical = _ordered_ids(keyword_scores)
    keyword_trace["candidate_ids"] = lexical[:vector_candidate_limit]
    trace: dict[str, object] = {
        "project_id": project_id,
        "candidate_count": len(accepted),
        "excluded": excluded,
        "keyword": keyword_trace,
        "vector": {"status": "not_configured"},
        "rerank": {"status": "not_configured"},
    }
    vector_scores: dict[str, float] = {}
    if embedding_provider is not None:
        vector_entries = accepted if embedding_allowed is None else tuple(
            entry for entry in accepted if entry.id in embedding_allowed
        )
        vector_excluded = len(accepted) - len(vector_entries)
        if not vector_entries:
            if embedding_allowed is None:
                trace["vector"] = {"status": "empty", "backend": "sqlite_vec" if sqlite_vec_index is not None else "provider_cosine", "candidate_ids": []}
            else:
                trace["vector"] = {
                    "status": "policy_restricted" if vector_excluded else "empty",
                    "candidate_ids": [],
                    "excluded_by_policy": vector_excluded,
                }
        else:
            try:
                with stage("vector"):
                    vector_scores = _vector_scores(
                        provider=embedding_provider,
                        index=sqlite_vec_index,
                        query=query,
                        entries=vector_entries,
                        vector_limit=vector_candidate_limit,
                        cache=embedding_cache,
                    )
                vector_trace: dict[str, object] = {
                    "status": "used" if vector_scores else "empty",
                    "backend": "sqlite_vec" if sqlite_vec_index is not None else "provider_cosine",
                    "candidate_ids": _ordered_ids(vector_scores),
                }
                if embedding_allowed is not None:
                    vector_trace["excluded_by_policy"] = vector_excluded
                trace["vector"] = vector_trace
            except Exception as exc:
                vector_trace = {"status": "degraded", "reason": _safe_failure_reason(exc)}
                if embedding_allowed is not None:
                    vector_trace["excluded_by_policy"] = vector_excluded
                trace["vector"] = vector_trace
    fused = _fuse(keyword_scores, vector_scores)
    rerank_scores: dict[str, float] = {}
    if reranker is not None and fused:
        bounded_candidate_ids = _ordered_ids(fused)[:vector_candidate_limit]
        candidate_ids = bounded_candidate_ids if rerank_allowed is None else tuple(
            item_id for item_id in bounded_candidate_ids if item_id in rerank_allowed
        )
        rerank_excluded = len(bounded_candidate_ids) - len(candidate_ids)
        if not candidate_ids:
            if rerank_allowed is not None:
                trace["rerank"] = {
                    "status": "policy_restricted" if rerank_excluded else "empty",
                    "candidate_ids": [],
                    "excluded_by_policy": rerank_excluded,
                }
        else:
            try:
                by_id = {entry.id: entry for entry in accepted}
                candidate_payloads = tuple(_candidate_payload(by_id[item_id]) for item_id in candidate_ids)
                raw_scores = reranker.rerank(query=query, candidates=candidate_payloads)
                if set(raw_scores) != set(candidate_ids):
                    raise RecognitionRetrievalError("reranker must score every supplied candidate exactly once")
                rerank_scores = {item_id: _score(score, "reranker score") for item_id, score in raw_scores.items()}
                # A configured cross-encoder evaluates the same bounded candidate set
                # with the query.  It is therefore allowed to correct a lexical or
                # embedding ordering, while failures still retain the fused baseline.
                fused = _fuse(fused, rerank_scores, primary_weight=0.40, secondary_weight=0.60)
                rerank_trace: dict[str, object] = {"status": "used", "candidate_ids": _ordered_ids(rerank_scores)}
                if rerank_allowed is not None:
                    rerank_trace["excluded_by_policy"] = rerank_excluded
                trace["rerank"] = rerank_trace
            except Exception as exc:
                rerank_trace = {"status": "degraded", "reason": _safe_failure_reason(exc)}
                if rerank_allowed is not None:
                    rerank_trace["excluded_by_policy"] = rerank_excluded
                trace["rerank"] = rerank_trace
    cooled_ids = {entry.id for entry in accepted if entry.recall_state == "cooled"}
    fused = {item_id: score * (0.5 if item_id in cooled_ids else 1.0) for item_id, score in fused.items()}
    trace["recall_priority"] = {"cooled_ids": sorted(cooled_ids), "cooled_multiplier": 0.5,
                              "policy": "manual_priority_only_not_validity"}
    selected = _ordered_ids(fused)[:limit]
    by_id = {entry.id: entry for entry in accepted}
    hits = tuple(
        RetrievalHit(
            id=item_id,
            revision=by_id[item_id].revision,
            content=by_id[item_id].content,
            project_id=project_id,
            score=round(fused[item_id], 6),
            keyword_score=keyword_scores.get(item_id, 0.0),
            vector_score=vector_scores.get(item_id),
            rerank_score=rerank_scores.get(item_id),
            source_refs=by_id[item_id].source_refs,
        )
        for item_id in selected
    )
    trace["result"] = {"status": "empty" if not hits else "ok", "hit_ids": list(selected)}
    return RetrievalResult(hits=hits, trace=trace)


def _eligible_entries(project_id: str, entries: Sequence[Mapping[str, object]]) -> tuple[tuple[_RecognitionEntry, ...], dict[str, int]]:
    accepted: list[_RecognitionEntry] = []
    excluded = {"other_project": 0, "unauthorized": 0, "stale": 0, "invalid": 0}
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, Mapping):
            excluded["invalid"] += 1
            continue
        try:
            entry = _entry(raw)
        except RecognitionRetrievalError:
            excluded["invalid"] += 1
            continue
        if entry.project_id != project_id:
            excluded["other_project"] += 1
            continue
        if raw.get("authorized") is False:
            excluded["unauthorized"] += 1
            continue
        status = raw.get("status", "active")
        if not isinstance(status, str) or status.lower() in _STALE_STATUSES or status.lower() not in _CURRENT_STATUSES:
            excluded["stale"] += 1
            continue
        current_revision = raw.get("current_revision", entry.revision)
        if isinstance(current_revision, bool) or not isinstance(current_revision, int) or current_revision != entry.revision:
            excluded["stale"] += 1
            continue
        if entry.id in seen:
            excluded["invalid"] += 1
            continue
        seen.add(entry.id)
        if entry.recall_state == "forgotten":
            continue
        accepted.append(entry)
    return tuple(accepted), excluded


def _allowed_ids(value: Collection[str] | None, field: str) -> frozenset[str] | None:
    """Normalize a caller-supplied disclosure allow-list without widening it."""

    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise RecognitionRetrievalError(f"{field} must be a collection of ids")
    return frozenset(_required_string(item_id, field) for item_id in value)


def _entry(raw: Mapping[str, object]) -> _RecognitionEntry:
    item_id = raw.get("id", raw.get("recognition_id", raw.get("object_id")))
    source_refs = raw.get("source_refs", ())
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
        raise RecognitionRetrievalError("source_refs must be a sequence")
    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise RecognitionRetrievalError("revision must be a positive integer")
    recall_state = raw.get("recall_state", "normal")
    if not isinstance(recall_state, str) or recall_state not in {"normal", "cooled", "forgotten"}:
        raise RecognitionRetrievalError("recall state is invalid")
    return _RecognitionEntry(
        id=_required_string(item_id, "entry id"),
        revision=revision,
        recall_state=recall_state,
        content=_required_string(raw.get("content"), "content"),
        project_id=_required_string(raw.get("project_id"), "entry project_id"),
        source_refs=tuple(
            normalized
            for ref in source_refs
            if (normalized := _source_ref(ref)) is not None
        ),
    )


def _vector_scores(*, provider: EmbeddingProvider, index: SqliteVecCandidateIndex | None, query: str, entries: Sequence[_RecognitionEntry], vector_limit: int, cache: EmbeddingCachePort | None) -> dict[str, float]:
    if not entries:
        return {}
    model_id = _provider_model_id(provider)
    cached = {entry.id: cache.read(model_id=model_id, entry=entry) for entry in entries} if cache is not None else {}
    missing = tuple(entry for entry in entries if cached.get(entry.id) is None)
    vectors = provider.embed((query, *(entry.content for entry in missing)))
    if len(vectors) != len(missing) + 1:
        raise RecognitionRetrievalError("embedding provider returned an unexpected vector count")
    query_vector = _vector(vectors[0], "query vector")
    computed = tuple(_vector(vector, "entry vector") for vector in vectors[1:])
    for entry, vector in zip(missing, computed, strict=True):
        if cache is not None:
            cache.write(model_id=model_id, entry=entry, vector=vector)
        cached[entry.id] = vector
    entry_vectors = tuple(cached[entry.id] for entry in entries)
    if any(len(vector) != len(query_vector) for vector in entry_vectors):
        raise RecognitionRetrievalError("embedding vectors have inconsistent dimensions")
    if index is not None:
        return dict(index.search(entries=entries, vectors=entry_vectors, query_vector=query_vector, limit=vector_limit))
    scores = {entry.id: _cosine(query_vector, vector) for entry, vector in zip(entries, entry_vectors, strict=True)}
    return {item_id: scores[item_id] for item_id in _ordered_ids(scores)[:vector_limit]}


def _keyword_score(query: str, content: str) -> float:
    query_tokens, content_tokens = _tokens(query), _tokens(content)
    if not query_tokens or not content_tokens:
        return 0.0
    matches = query_tokens.intersection(content_tokens)
    required_matches = min(2, len(query_tokens))
    score = len(matches) / len(query_tokens)
    if len(matches) < required_matches or score < _MIN_KEYWORD_SCORE:
        return 0.0
    return round(score, 6)


def _fts_keyword_candidate_ids(entries: Sequence[_RecognitionEntry], query_tokens: set[str]) -> set[str]:
    """Return a transient FTS5 superset for the deterministic scorer.

    Callers have already performed project, authorization, status and revision
    checks.  The projection stores only the current request's eligible entries
    and is closed before this function returns.  Search text uses the same
    bigram/English token set as ``_keyword_score``; FTS only saves scoring work,
    while the existing scorer remains the relevance and ordering authority.
    """

    if not entries or not query_tokens:
        return set()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(":memory:")
        observe_connection(connection)
        # unicode61 does not stem English terms.  Disabling diacritic removal
        # also prevents SQLite's tokenizer from silently widening a token that
        # the Python baseline treats as exact.
        connection.execute(
            """
            CREATE VIRTUAL TABLE recognition_keyword_candidates USING fts5(
                recognition_id UNINDEXED,
                search_text,
                tokenize='unicode61 remove_diacritics 0'
            )
            """
        )
        connection.executemany(
            "INSERT INTO recognition_keyword_candidates(recognition_id, search_text) VALUES (?, ?)",
            ((entry.id, " ".join(sorted(_tokens(entry.content)))) for entry in entries),
        )
        expression = " OR ".join(_fts_exact_token(token) for token in sorted(query_tokens))
        rows = connection.execute(
            """
            SELECT recognition_id
            FROM recognition_keyword_candidates
            WHERE recognition_keyword_candidates MATCH ?
            """,
            (expression,),
        ).fetchall()
        candidate_ids = {row[0] for row in rows if isinstance(row[0], str)}
        if len(candidate_ids) != len(rows):
            raise RecognitionRetrievalError("sqlite_fts5_invalid_candidate")
        return candidate_ids
    except sqlite3.DatabaseError as exc:
        raise RecognitionRetrievalError("sqlite_fts5_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


def _fts_exact_token(token: str) -> str:
    """Quote one normalized token for a literal FTS5 OR expression."""

    return '"' + token.replace('"', '""') + '"'


def _fuse(primary: Mapping[str, float], secondary: Mapping[str, float], *, primary_weight: float = 0.45, secondary_weight: float = 0.55) -> dict[str, float]:
    if not secondary:
        return dict(primary)
    primary_normalized, secondary_normalized = _normalize(primary), _normalize(secondary)
    return {
        item_id: round(primary_weight * primary_normalized.get(item_id, 0.0) + secondary_weight * secondary_normalized.get(item_id, 0.0), 6)
        for item_id in set(primary).union(secondary)
    }


def _normalize(scores: Mapping[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    ordered = _ordered_ids(scores)
    if len(ordered) == 1:
        return {ordered[0]: 1.0}
    return {item_id: round(1.0 - index / (len(ordered) - 1), 6) for index, item_id in enumerate(ordered)}


def _ordered_ids(scores: Mapping[str, float]) -> list[str]:
    return [item_id for item_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def _candidate_payload(entry: _RecognitionEntry) -> dict[str, object]:
    return {"id": entry.id, "revision": entry.revision, "content": entry.content, "source_refs": list(entry.source_refs)}


def _provider_model_id(provider: EmbeddingProvider) -> str:
    declared = getattr(provider, "cache_identity", None) or getattr(provider, "model_id", None)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return f"{type(provider).__module__}.{type(provider).__qualname__}"


def _source_ref(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        kind, item_id = value.get("type"), value.get("id")
        if isinstance(kind, str) and kind and isinstance(item_id, str) and item_id:
            revision = value.get("revision")
            if revision is None:
                return f"{kind}:{item_id}"
            if type(revision) is int and revision >= 1:
                return f"{kind}:{item_id}@revision:{revision}"
    return None


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _TOKEN_PATTERN.finditer(text):
        unit = match.group(0).lower()
        if unit[0].isascii():
            if len(unit) >= 2:
                tokens.add(unit)
            continue
        tokens.update(
            unit[index:index + 2]
            for index in range(len(unit) - 1)
            if unit[index:index + 2] not in _CHINESE_STOP_TERMS
        )
    return tokens


def _vector(value: Sequence[float], name: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not value:
        raise RecognitionRetrievalError(f"{name} must be a non-empty numeric sequence")
    vector = tuple(float(item) for item in value)
    if not all(isfinite(item) for item in vector):
        raise RecognitionRetrievalError(f"{name} must contain finite values")
    return vector


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_norm = sum(item * item for item in left) ** 0.5
    right_norm = sum(item * item for item in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return round(max(0.0, min(1.0, (sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm) + 1.0) / 2.0)), 6)


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecognitionRetrievalError(f"{field} must be a non-empty string")
    return value.strip()


def _score(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
        raise RecognitionRetrievalError(f"{field} must be a finite score between 0 and 1")
    return float(value)


def _safe_failure_reason(exc: Exception) -> str:
    if isinstance(exc, RecognitionRetrievalError):
        return str(exc)
    return "provider_or_index_failed"
