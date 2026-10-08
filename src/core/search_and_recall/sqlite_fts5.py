from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import RecallIndexEntry, RecallQuery
from .runtime import DEFAULT_RECALL_LAYERS
from .sqlite_manifest import sqlite_fts5_manifest_payload


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class SqliteFts5DryRunError(ValueError):
    """Raised when a SQLite FTS5 dry-run would weaken index guarantees."""


@dataclass(frozen=True, slots=True)
class SqliteFts5DryRunResult:
    status: str
    backend_kind: str
    database_uri: str
    manifest_id: str
    entry_count: int
    hit_count: int
    hit_object_ids: tuple[str, ...]
    vector_enabled: bool
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SqliteFts5DryRunIndex:
    """Builds and queries a candidate SQLite FTS5 index without activating it."""

    database_path: Path

    def rebuild_and_query(
        self,
        entries: Sequence[RecallIndexEntry | Mapping[str, object]],
        *,
        manifest: Mapping[str, object],
        query: RecallQuery,
    ) -> SqliteFts5DryRunResult:
        payload = sqlite_fts5_manifest_payload(manifest)
        if payload.get("backend_kind") != "sqlite_fts5":
            raise SqliteFts5DryRunError("sqlite_fts5 dry-run requires sqlite_fts5 manifest")
        vector = payload.get("vector")
        if not isinstance(vector, Mapping) or vector.get("enabled") is not False:
            raise SqliteFts5DryRunError("sqlite_fts5 dry-run keeps vector disabled")
        normalized = tuple(_index_entry(entry) for entry in entries)
        for entry in normalized:
            _validate_dry_run_entry(entry)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            _ensure_fts5(connection)
            connection.execute("DROP TABLE IF EXISTS recall_fts")
            connection.execute(
                """
                CREATE VIRTUAL TABLE recall_fts USING fts5(
                    object_id UNINDEXED,
                    project_id UNINDEXED,
                    layer UNINDEXED,
                    trust_status UNINDEXED,
                    content,
                    search_text,
                    source_refs UNINDEXED,
                    tokenize='unicode61'
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO recall_fts(
                    object_id, project_id, layer, trust_status, content, search_text, source_refs
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        entry.object_id,
                        entry.project_id,
                        entry.layer,
                        entry.trust_status,
                        entry.content,
                        _search_text(entry.content),
                        "\n".join(entry.source_refs),
                    )
                    for entry in normalized
                ),
            )
            rows = _query_rows(connection, query)
            connection.commit()
        finally:
            connection.close()
        source_refs = tuple(ref for entry in normalized for ref in entry.source_refs)
        manifest_id = payload.get("id")
        if not isinstance(manifest_id, str):
            raise SqliteFts5DryRunError("sqlite_fts5 dry-run manifest requires id")
        return SqliteFts5DryRunResult(
            status="ready",
            backend_kind="sqlite_fts5",
            database_uri=self.database_path.resolve(strict=False).as_uri(),
            manifest_id=manifest_id,
            entry_count=len(normalized),
            hit_count=len(rows),
            hit_object_ids=tuple(row["object_id"] for row in rows),
            vector_enabled=False,
            source_refs=source_refs,
        )


def _ensure_fts5(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(content, tokenize='unicode61')"
        )
        connection.execute("DROP TABLE temp.__fts5_probe")
    except sqlite3.DatabaseError as exc:
        raise SqliteFts5DryRunError("sqlite fts5 is unavailable") from exc


def _query_rows(connection: sqlite3.Connection, query: RecallQuery) -> tuple[Mapping[str, str], ...]:
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
        SELECT object_id, source_refs
        FROM recall_fts
        WHERE {" AND ".join(clauses)}
        ORDER BY bm25(recall_fts), object_id
        LIMIT ?
        """,
        params,
    )
    try:
        fetched = cursor.fetchall()
    finally:
        cursor.close()
    rows: list[Mapping[str, str]] = []
    for object_id, source_refs in fetched:
        if not isinstance(object_id, str) or not isinstance(source_refs, str):
            raise SqliteFts5DryRunError("sqlite_fts5 dry-run returned invalid row")
        rows.append({"object_id": object_id, "source_refs": source_refs})
    return tuple(rows)


def _match_expression(text: str) -> str:
    tokens = tuple(match.group(0).lower() for match in _TOKEN_PATTERN.finditer(text))
    return " OR ".join(f'"{token}"' for token in tokens)


def sqlite_fts5_verification_query(text: str) -> str:
    """Return one token guaranteed to use the same parsing as FTS queries."""

    match = _TOKEN_PATTERN.search(text)
    return match.group(0) if match is not None else ""


def _search_text(text: str) -> str:
    tokens = tuple(match.group(0).lower() for match in _TOKEN_PATTERN.finditer(text))
    return " ".join(tokens)


def _placeholders(values: Sequence[str]) -> str:
    return ", ".join("?" for _ in values)


def _index_entry(entry: RecallIndexEntry | Mapping[str, object]) -> RecallIndexEntry:
    if isinstance(entry, RecallIndexEntry):
        return entry
    source_refs = entry.get("source_refs")
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
        raise SqliteFts5DryRunError("sqlite_fts5 dry-run entry requires source_refs")
    return RecallIndexEntry(
        object_id=_required_string(entry, "object_id"),
        project_id=_required_string(entry, "project_id"),
        layer=_required_string(entry, "layer"),
        content=_required_string(entry, "content"),
        source_refs=tuple(str(ref) for ref in source_refs if isinstance(ref, str) and ref),
        trust_status=_required_string(entry, "trust_status"),
        base_score=_score_value(entry.get("base_score", 0.5)),
    )


def _validate_dry_run_entry(entry: RecallIndexEntry) -> None:
    if not entry.object_id:
        raise SqliteFts5DryRunError("sqlite_fts5 dry-run entry requires object_id")
    if not entry.project_id:
        raise SqliteFts5DryRunError("sqlite_fts5 dry-run entry requires project_id")
    if entry.layer not in DEFAULT_RECALL_LAYERS:
        raise SqliteFts5DryRunError("sqlite_fts5 dry-run entry layer is not supported")
    if not entry.content:
        raise SqliteFts5DryRunError("sqlite_fts5 dry-run entry requires content")
    if not entry.trust_status:
        raise SqliteFts5DryRunError("sqlite_fts5 dry-run entry requires trust_status")
    if not 0 <= entry.base_score <= 1:
        raise SqliteFts5DryRunError("sqlite_fts5 dry-run entry base_score must be between 0 and 1")
    for ref in entry.source_refs:
        if "#" not in ref:
            raise SqliteFts5DryRunError("sqlite_fts5 dry-run source_ref must use source_id#locator format")
        source_id, locator = ref.split("#", 1)
        if not source_id or not locator:
            raise SqliteFts5DryRunError("sqlite_fts5 dry-run source_ref requires source_id and locator")


def _required_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SqliteFts5DryRunError(f"sqlite_fts5 dry-run entry requires {key}")
    return value


def _score_value(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise SqliteFts5DryRunError("sqlite_fts5 dry-run entry base_score must be numeric")
