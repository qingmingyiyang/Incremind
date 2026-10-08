from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from core.storage_provider import ObjectStorePort

from .backend_policy import RecallBackendSelection
from .index_lifecycle import IndexRebuildRequest


SqliteFts5ManifestStatus = Literal["planned", "ready"]


@dataclass(frozen=True, slots=True)
class SqliteFts5Manifest:
    manifest_id: str
    status: SqliteFts5ManifestStatus
    backend_kind: str
    source_fingerprint: str
    source_count: int
    source_refs: tuple[str, ...]
    vector_enabled: bool
    created_at: str


class SqliteFts5ManifestError(ValueError):
    """Raised when a SQLite FTS5 manifest would weaken Recall index guarantees."""


@dataclass(frozen=True, slots=True)
class ObjectStoreSqliteFts5ManifestRepository:
    """Persists the SQLite FTS5 candidate manifest without replacing active index runtime."""

    object_store: ObjectStorePort
    manifest_collection: str = "recall_index_manifests"
    manifest_id: str = "sqlite_fts5_candidate"

    def save_candidate_manifest(
        self,
        manifest: SqliteFts5Manifest | Mapping[str, object],
    ) -> Mapping[str, object]:
        payload = sqlite_fts5_manifest_payload(manifest)
        object_id = _required_string(payload, "id")
        if object_id == "active":
            raise SqliteFts5ManifestError("sqlite_fts5 candidate manifest must not replace active index")
        self.object_store.write(self.manifest_collection, object_id, payload, expected_revision=None)
        return payload

    def get_candidate_manifest(self) -> Mapping[str, object] | None:
        payload = self.object_store.read(self.manifest_collection, self.manifest_id)
        return dict(payload) if payload is not None else None


def create_sqlite_fts5_manifest(
    *,
    rebuild_request: IndexRebuildRequest,
    backend_selection: RecallBackendSelection,
    manifest_id: str = "sqlite_fts5_candidate",
    status: SqliteFts5ManifestStatus = "planned",
    created_at: str | None = None,
) -> SqliteFts5Manifest:
    if backend_selection.selected_backend != "sqlite_fts5":
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires sqlite_fts5 selected backend")
    if rebuild_request.backend_kind != "sqlite_fts5":
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires sqlite_fts5 rebuild request")
    _validate_source_refs(rebuild_request.source_refs)
    if rebuild_request.source_count != len(rebuild_request.source_refs):
        raise SqliteFts5ManifestError("sqlite_fts5 manifest source_count must match source_refs")
    if manifest_id == "active":
        raise SqliteFts5ManifestError("sqlite_fts5 candidate manifest must not replace active index")
    return SqliteFts5Manifest(
        manifest_id=manifest_id,
        status=status,
        backend_kind="sqlite_fts5",
        source_fingerprint=rebuild_request.source_fingerprint,
        source_count=rebuild_request.source_count,
        source_refs=rebuild_request.source_refs,
        vector_enabled=False,
        created_at=created_at or _utc_now(),
    )


def sqlite_fts5_manifest_payload(manifest: SqliteFts5Manifest | Mapping[str, object]) -> dict[str, object]:
    if isinstance(manifest, SqliteFts5Manifest):
        payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "id": manifest.manifest_id,
            "status": manifest.status,
            "backend_kind": manifest.backend_kind,
            "source": "index_rebuild_request",
            "source_fingerprint": manifest.source_fingerprint,
            "source_count": manifest.source_count,
            "source_refs": list(manifest.source_refs),
            "index_role": "candidate_manifest",
            "fts": {
                "engine": "sqlite",
                "module": "fts5",
                "table": "recall_fts",
                "content_columns": ["content", "search_text"],
                "tokenizer": "unicode61",
                "ranker": "bm25",
                "filters": ["project_id", "layer", "trust_status"],
            },
            "vector": {
                "enabled": manifest.vector_enabled,
                "provider": None,
                "dimension": None,
            },
            "created_at": manifest.created_at,
        }
    else:
        payload = dict(manifest)
    _validate_payload(payload)
    return payload


def _validate_payload(payload: Mapping[str, object]) -> None:
    if payload.get("schema_version") != "1.0.0":
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires schema_version 1.0.0")
    if _required_string(payload, "id") == "active":
        raise SqliteFts5ManifestError("sqlite_fts5 candidate manifest must not replace active index")
    if payload.get("status") not in {"planned", "ready"}:
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires planned or ready status")
    if payload.get("backend_kind") != "sqlite_fts5":
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires backend_kind sqlite_fts5")
    if payload.get("index_role") != "candidate_manifest":
        raise SqliteFts5ManifestError("sqlite_fts5 manifest must be a candidate manifest")
    source_count = _required_int(payload, "source_count")
    source_refs = _required_string_sequence(payload, "source_refs")
    if source_count != len(source_refs):
        raise SqliteFts5ManifestError("sqlite_fts5 manifest source_count must match source_refs")
    _validate_source_refs(source_refs)
    _required_string(payload, "source_fingerprint")
    _required_string(payload, "created_at")
    fts = payload.get("fts")
    if not isinstance(fts, Mapping):
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires fts settings")
    if fts.get("engine") != "sqlite" or fts.get("module") != "fts5":
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires sqlite fts5 engine")
    if fts.get("ranker") != "bm25":
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires bm25 ranker")
    filters = fts.get("filters")
    if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires filters")
    for item in ("project_id", "layer", "trust_status"):
        if item not in filters:
            raise SqliteFts5ManifestError("sqlite_fts5 manifest must preserve project/layer/trust filters")
    vector = payload.get("vector")
    if not isinstance(vector, Mapping) or vector.get("enabled") is not False:
        raise SqliteFts5ManifestError("sqlite_fts5 manifest must keep vector disabled")


def _validate_source_refs(source_refs: Sequence[str]) -> None:
    if not source_refs:
        raise SqliteFts5ManifestError("sqlite_fts5 manifest requires source refs")
    for ref in source_refs:
        if not isinstance(ref, str) or "#rev:" not in ref:
            raise SqliteFts5ManifestError("sqlite_fts5 manifest source refs must use source_id#rev:<revision>")


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SqliteFts5ManifestError(f"sqlite_fts5 manifest requires {key}")
    return value


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SqliteFts5ManifestError(f"sqlite_fts5 manifest requires non-negative {key}")
    return value


def _required_string_sequence(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SqliteFts5ManifestError(f"sqlite_fts5 manifest requires {key}")
    return tuple(item for item in value if isinstance(item, str))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
