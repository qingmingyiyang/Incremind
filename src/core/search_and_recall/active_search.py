from __future__ import annotations

import sqlite3
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .index_lifecycle import IndexSourceRecord, evaluate_index_freshness
from .ports import RecallHit, RecallIndexEntry, RecallQuery
from .runtime import InMemoryRecallIndexAdapter, ObjectStoreRecallIndex
from .sqlite_fts5 import _TOKEN_PATTERN, _ensure_fts5, _match_expression, _placeholders


class LibrarySearchError(ValueError):
    """Raised when a library search request would violate isolation guarantees."""


@dataclass(frozen=True, slots=True)
class LibrarySearchHit:
    object_id: str
    layer: str
    content: str
    source_refs: tuple[str, ...]
    trust_status: str
    score: float
    backend: str


@dataclass(frozen=True, slots=True)
class LibrarySearchResult:
    status: str
    backend: str
    query_text: str
    hits: tuple[LibrarySearchHit, ...]
    total: int
    index_stale: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class SqliteFts5ActiveIndex:
    """Queries a live SQLite FTS5 database described by the active manifest.

    The active manifest stores a ``database_uri`` (file:// URI) that points to
    a SQLite database built and verified by a completed ``rebuild_index`` job.
    This adapter opens that database read-only and runs a BM25-ranked FTS5
    query. It never writes to the database and never activates a new manifest.
    """

    active_manifest: Mapping[str, object]

    def recall(self, query: RecallQuery) -> tuple[RecallHit, ...]:
        database_uri = self.active_manifest.get("database_uri")
        if not isinstance(database_uri, str) or not database_uri.startswith("file://"):
            return ()
        database_path = _database_path_from_uri(database_uri)
        if not database_path.exists():
            return ()
        connection = sqlite3.connect(str(database_path))
        try:
            _ensure_fts5(connection)
            rows = _active_query_rows(connection, query)
        finally:
            connection.close()
        return tuple(
            RecallHit(
                object_id=row["object_id"],
                layer=row["layer"],
                content=row["content"],
                source_refs=tuple(
                    ref for ref in str(row.get("source_refs", "")).split("\n") if ref
                ),
                trust_status=row["trust_status"],
                score=_bm25_score(row),
            )
            for row in rows
        )


@dataclass(frozen=True, slots=True)
class LibrarySearchService:
    """Searches the library, preferring active SQLite FTS5 over ObjectStore.

    When the active manifest is missing, has no ``database_uri``, the database
    file is unavailable, or SQLite/FTS5 is not supported, the service
    transparently falls back to :class:`ObjectStoreRecallIndex` so the library
    search remains available.
    """

    recall_index: ObjectStoreRecallIndex
    active_manifest: Mapping[str, object] | None
    # 验收3：传入当前 source ledger 让 _is_index_stale 能调用 evaluate_index_freshness。
    # None 时 fallback 到旧的 manifest-only 检查（向后兼容）。
    source_ledger: tuple[IndexSourceRecord | Mapping[str, object], ...] | None = None
    current_entries: tuple[RecallIndexEntry, ...] | None = None

    def search(
        self,
        *,
        query: str,
        project_id: str | None = None,
        layers: Sequence[str] | None = None,
        trust_statuses: Sequence[str] | None = None,
        limit: int = 12,
    ) -> LibrarySearchResult:
        if not query or not query.strip():
            raise LibrarySearchError("library search requires non-empty query")
        if limit <= 0:
            raise LibrarySearchError("library search limit must be positive")
        selected_layers = tuple(layers) if layers else _default_search_layers()
        selected_trust = tuple(trust_statuses) if trust_statuses else _default_search_trust()
        recall_query = RecallQuery(
            text=query,
            project_id=project_id,
            layers=selected_layers,
            allowed_trust_statuses=selected_trust,
            limit=limit,
        )
        index_stale = self._is_index_stale()
        # An active FTS database is only a projection.  Once the current
        # authority ledger drifts (for example after a hard forget), querying
        # that old database could resurrect content that no longer exists.
        if self._fts5_available() and not index_stale:
            fts5_status: tuple[str, str | None] = ("unavailable", None)
            try:
                hits = SqliteFts5ActiveIndex(self.active_manifest).recall(recall_query)
                if hits:
                    return _build_result(
                        status="ready",
                        backend="sqlite_fts5",
                        query_text=query,
                        hits=hits,
                        index_stale=index_stale,
                        reason=None,
                    )
                fts5_status = ("zero_hits", None)
            except sqlite3.DatabaseError as exc:
                fts5_status = ("database_error", str(exc))
            except OSError as exc:
                fts5_status = ("database_unreachable", str(exc))
            fallback_hits = self._fallback_recall(recall_query)
            return _build_result(
                status="ready" if fallback_hits else "empty",
                backend="object_store_lexical",
                query_text=query,
                hits=fallback_hits,
                index_stale=index_stale,
                reason=_fts5_degraded_reason(fts5_status[0]),
            )
        fallback_hits = self._fallback_recall(recall_query)
        return _build_result(
            status="ready" if fallback_hits else "empty",
            backend="object_store_lexical",
            query_text=query,
            hits=fallback_hits,
            index_stale=index_stale,
            reason=(
                "sqlite_fts5_stale"
                if self._fts5_available() and index_stale
                else (
                    "sqlite_fts5_unavailable"
                    if self.active_manifest is not None
                    else "no_active_manifest"
                )
            ),
        )

    def _fts5_available(self) -> bool:
        if self.active_manifest is None:
            return False
        if self.active_manifest.get("backend_kind") != "sqlite_fts5":
            return False
        if self.active_manifest.get("status") != "active":
            return False
        database_uri = self.active_manifest.get("database_uri")
        return isinstance(database_uri, str) and database_uri.startswith("file://")

    def _is_index_stale(self) -> bool:
        if self.active_manifest is None:
            return True
        # None means a legacy caller did not supply current authority evidence.
        # An empty tuple means the current authority is genuinely empty and
        # must invalidate a previously non-empty active index.
        if self.source_ledger is not None:
            try:
                freshness = evaluate_index_freshness(self.active_manifest, self.source_ledger)
            except Exception:
                # source ledger 里有坏数据时 fallback 到旧逻辑，不让搜索崩溃。
                return self._legacy_index_stale()
            return freshness.status != "fresh"
        return self._legacy_index_stale()

    def _fallback_recall(self, query: RecallQuery) -> tuple[RecallHit, ...]:
        if self.current_entries is not None:
            return InMemoryRecallIndexAdapter(self.current_entries).recall(query)
        return self.recall_index.recall(query)

    def _legacy_index_stale(self) -> bool:
        source_count = self.active_manifest.get("source_count")
        if not isinstance(source_count, int) or source_count <= 0:
            return True
        return False


def _fts5_degraded_reason(status: str) -> str:
    """把 FTS5 失败状态映射成用户友好的 degraded reason（验收2）。"""
    if status == "zero_hits":
        return "sqlite_fts5_zero_hits"
    if status == "database_error":
        return "sqlite_fts5_database_error"
    if status == "database_unreachable":
        return "sqlite_fts5_database_unreachable"
    return "sqlite_fts5_unavailable"


def _active_query_rows(
    connection: sqlite3.Connection,
    query: RecallQuery,
) -> tuple[Mapping[str, object], ...]:
    if query.limit <= 0:
        return ()
    match_expression = _match_expression(query.text)
    if not match_expression:
        return ()
    clauses = ["recall_fts MATCH ?"]
    params: list[object] = [match_expression]
    if query.project_id is not None:
        clauses.append("project_id = ?")
        params.append(query.project_id)
    if query.layers:
        clauses.append(f"layer IN ({_placeholders(query.layers)})")
        params.extend(query.layers)
    if query.allowed_trust_statuses:
        clauses.append(f"trust_status IN ({_placeholders(query.allowed_trust_statuses)})")
        params.extend(query.allowed_trust_statuses)
    params.append(query.limit)
    cursor = connection.execute(
        f"""
        SELECT object_id, project_id, layer, trust_status, content, source_refs, bm25(recall_fts) AS rank
        FROM recall_fts
        WHERE {" AND ".join(clauses)}
        ORDER BY rank, object_id
        LIMIT ?
        """,
        params,
    )
    try:
        fetched = cursor.fetchall()
    finally:
        cursor.close()
    rows: list[Mapping[str, object]] = []
    for object_id, project_id, layer, trust_status, content, source_refs, rank in fetched:
        rows.append(
            {
                "object_id": object_id,
                "project_id": project_id,
                "layer": layer,
                "trust_status": trust_status,
                "content": content,
                "source_refs": source_refs,
                "rank": rank,
            }
        )
    return tuple(rows)


def _bm25_score(row: Mapping[str, object]) -> float:
    rank = row.get("rank")
    if isinstance(rank, (int, float)):
        normalized = max(0.0, min(1.0, 1.0 / (1.0 + abs(float(rank)))))
        return round(normalized, 6)
    return 0.5


def _database_path_from_uri(database_uri: str) -> Path:
    parsed = urllib.parse.urlparse(database_uri)
    if parsed.scheme != "file":
        raise LibrarySearchError("sqlite_fts5 active database_uri must use file:// scheme")
    path = urllib.parse.unquote(parsed.path)
    # On Windows, file:///C:/path produces path="/C:/path" but Path needs "C:/path"
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _default_search_layers() -> tuple[str, ...]:
    from .runtime import DEFAULT_RECALL_LAYERS
    return DEFAULT_RECALL_LAYERS


def _default_search_trust() -> tuple[str, ...]:
    from .runtime import DEFAULT_TRUST_INCLUDE
    return DEFAULT_TRUST_INCLUDE


def _build_result(
    *,
    status: str,
    backend: str,
    query_text: str,
    hits: Sequence[RecallHit],
    index_stale: bool,
    reason: str | None,
) -> LibrarySearchResult:
    search_hits = tuple(
        LibrarySearchHit(
            object_id=hit.object_id,
            layer=hit.layer,
            content=hit.content,
            source_refs=hit.source_refs,
            trust_status=hit.trust_status,
            score=hit.score,
            backend=backend,
        )
        for hit in hits
    )
    return LibrarySearchResult(
        status=status,
        backend=backend,
        query_text=query_text,
        hits=search_hits,
        total=len(search_hits),
        index_stale=index_stale,
        reason=reason,
    )


__all__ = [
    "LibrarySearchError",
    "LibrarySearchHit",
    "LibrarySearchResult",
    "LibrarySearchService",
    "SqliteFts5ActiveIndex",
]
