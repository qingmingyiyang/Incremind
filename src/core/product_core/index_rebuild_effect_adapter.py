"""Real SQLite FTS5 domain adapter for the Index Rebuild Effect-v2 handler.

This module deliberately owns only the rebuild artifact and active-manifest
side effect.  It never reads or writes a Job, Job status, or lease: Core
``EffectRunner`` owns execution fencing and the immutable Effect receipt is
written by ``index_rebuild_effect_execution``.
"""

from __future__ import annotations

import os
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.search_and_recall import (
    ObjectStoreSqliteFts5ActivationRepository,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5DryRunIndex,
    build_recall_authority_ledger,
    evaluate_index_freshness,
    sqlite_fts5_manifest_payload,
    sqlite_fts5_verification_query,
)

from .index_rebuild_effect_admission import canonical
from .index_rebuild_effect_execution import (
    IndexRebuildEffectExecutionHandler,
    IndexRebuildEffectExecutionProbe,
    IndexRebuildQueryOutcome,
)


ARTIFACT_COLLECTION = "recall_index_effect_artifacts"
MANIFEST_COLLECTION = "recall_index_manifests"


class IndexRebuildEffectObjectStore(Protocol):
    @property
    def namespace_id(self) -> str: ...

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...

    def write(self, collection: str, object_id: str, payload: Mapping[str, object], expected_revision: int | None) -> int: ...


@dataclass(frozen=True, slots=True)
class IndexRebuildEffectAdapter:
    """Maps frozen Effect facts to the existing FTS build/verify/activate APIs."""

    object_store: IndexRebuildEffectObjectStore
    runtime_root: Path
    recall_entries_loader: Callable[[], Sequence[RecallIndexEntry]]
    activated_by: str = "index_rebuild_effect_v2"
    after_artifact_write: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_root", Path(self.runtime_root))

    def handler(self, database: Path | str) -> IndexRebuildEffectExecutionHandler:
        return IndexRebuildEffectExecutionHandler(
            database, self.load_manifest, self.load_ledger, self.execute, self.query_completion,
        )

    def probe(self, database: Path | str) -> IndexRebuildEffectExecutionProbe:
        return IndexRebuildEffectExecutionProbe(
            database, self.load_manifest, self.load_ledger, self.query_completion,
        )

    def load_manifest(self, manifest_ref: str) -> Mapping[str, object] | None:
        candidate = self.object_store.read(MANIFEST_COLLECTION, _ref_id(manifest_ref, "manifest"))
        if not isinstance(candidate, Mapping):
            return None
        try:
            payload = sqlite_fts5_manifest_payload(candidate)
        except ValueError:
            return None
        return _manifest_evidence(payload)

    def load_ledger(self, ledger_ref: str) -> Mapping[str, object] | None:
        try:
            entries = tuple(self.recall_entries_loader())
            return _ledger_evidence(_ref_id(ledger_ref, "ledger"), entries)
        except (TypeError, ValueError):
            return None

    def execute(
        self, operation_id: str, request: Mapping[str, object], manifest: Mapping[str, object], ledger: Mapping[str, object],
    ) -> Mapping[str, object]:
        candidate = self._candidate(manifest, request)
        entries = tuple(self.recall_entries_loader())
        self._assert_fresh(entries, ledger)
        existing = self._artifact(operation_id)
        if existing is not None:
            result = _artifact_result(existing, operation_id)
            self._activate(
                candidate, operation_id, str(existing.get("database_uri", "")),
            )
            return result
        if not entries:
            raise ValueError("index rebuild requires at least one traceable Recall entry")
        final_path = _candidate_database_path(self.runtime_root, operation_id)
        part_path = final_path.with_suffix(".sqlite3.part")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if part_path.exists():
            part_path.unlink()
        query_text = sqlite_fts5_verification_query(entries[0].content)
        if not query_text:
            raise ValueError("index rebuild entry has no searchable content")
        verification = SqliteFts5DryRunIndex(part_path).rebuild_and_query(
            entries, manifest=candidate,
            query=RecallQuery(text=query_text, project_id=None, layers=(), allowed_trust_statuses=(), limit=max(1, min(20, len(entries)))),
        )
        if verification.hit_count < 1:
            raise ValueError("index rebuild verification query returned no traceable hit")
        self._assert_fresh(tuple(self.recall_entries_loader()), ledger)
        os.replace(part_path, final_path)
        database_uri = final_path.resolve(strict=False).as_uri()
        artifact = _new_artifact(operation_id, candidate, verification, database_uri)
        self.object_store.write(ARTIFACT_COLLECTION, operation_id, artifact, expected_revision=None)
        if self.after_artifact_write is not None:
            self.after_artifact_write()
        self._activate(candidate, operation_id, database_uri)
        return _artifact_result(artifact, operation_id)

    def query_completion(self, domain: Mapping[str, object]) -> IndexRebuildQueryOutcome:
        try:
            request = _mapping(domain, "request")
            manifest = _mapping(domain, "manifest")
            ledger = _mapping(domain, "ledger")
            operation_id = _operation_id_from_domain(domain)
            artifact = self._artifact(operation_id)
            if artifact is None:
                return IndexRebuildQueryOutcome("not_completed")
            result = _artifact_result(artifact, operation_id)
            candidate = self._candidate(manifest, request)
            self._assert_fresh(tuple(self.recall_entries_loader()), ledger)
            active = self.object_store.read(MANIFEST_COLLECTION, "active")
            if active is None:
                # The immutable artifact proves the expensive build finished,
                # while the missing active pointer proves the remaining
                # activation step did not. Re-entering execute is safe because
                # it reuses this exact artifact and only performs activation.
                return IndexRebuildQueryOutcome("not_completed")
            if not _active_matches(active, candidate, operation_id, artifact):
                return IndexRebuildQueryOutcome("unknown")
            return IndexRebuildQueryOutcome("completed", result)
        except (TypeError, ValueError, OSError):
            return IndexRebuildQueryOutcome("unknown")

    def _candidate(self, manifest: Mapping[str, object], request: Mapping[str, object]) -> Mapping[str, object]:
        candidate = self.object_store.read(MANIFEST_COLLECTION, str(manifest.get("id", "")))
        if not isinstance(candidate, Mapping):
            raise ValueError("index rebuild candidate manifest was not found")
        payload = sqlite_fts5_manifest_payload(candidate)
        if _manifest_evidence(payload) != dict(manifest):
            raise ValueError("index rebuild candidate manifest drifted")
        refs = payload.get("source_refs")
        if not isinstance(refs, list) or refs != request.get("source_refs"):
            raise ValueError("index rebuild candidate request drifted")
        return payload

    def _assert_fresh(self, entries: Sequence[RecallIndexEntry], ledger: Mapping[str, object]) -> None:
        current = _ledger_evidence(str(ledger.get("id", "")), entries)
        if current != dict(ledger):
            raise ValueError("index rebuild authority ledger drifted")

    def _artifact(self, operation_id: str) -> Mapping[str, object] | None:
        value = self.object_store.read(ARTIFACT_COLLECTION, operation_id)
        return dict(value) if isinstance(value, Mapping) else None

    def _activate(self, candidate: Mapping[str, object], operation_id: str, database_uri: str) -> None:
        verification_ref = f"crp://{self.object_store.namespace_id}/recall/index-verifications/{operation_id}"
        artifact = self._artifact(operation_id)
        if artifact is None:
            raise ValueError("index rebuild artifact was not persisted")
        ObjectStoreSqliteFts5ActivationRepository(self.object_store).activate_effect_v2(
            candidate_manifest=candidate,
            operation_id=operation_id,
            verification_ref=verification_ref,
            artifact_revision=str(artifact.get("artifact_revision", "")),
            activated_by=self.activated_by,
            database_uri=database_uri,
        )


def _manifest_evidence(candidate: Mapping[str, object]) -> dict[str, object]:
    return {"id": candidate["id"], "backend_kind": candidate["backend_kind"], "source_fingerprint": candidate["source_fingerprint"]}


def _ledger_evidence(ledger_id: str, entries: Sequence[RecallIndexEntry]) -> dict[str, object]:
    ledger = build_recall_authority_ledger(entries)
    from core.search_and_recall import source_ledger_fingerprint
    return {"id": ledger_id, "source_fingerprint": source_ledger_fingerprint(ledger), "entry_count": len(ledger)}


def _new_artifact(operation_id, candidate, verification, database_uri) -> dict[str, object]:
    artifact = {"schema_version": "index-rebuild-artifact-v2", "operation_id": operation_id, "status": "ready",
            "manifest_id": candidate["id"], "source_fingerprint": candidate["source_fingerprint"],
            "database_uri": database_uri, "entry_count": verification.entry_count,
            "hit_count": verification.hit_count, "artifact_ref": f"facts:index-rebuild-artifact-{operation_id}",
            "artifact_revision": ""}
    digest_payload = dict(artifact)
    digest_payload.pop("artifact_revision")
    artifact["artifact_revision"] = f"index-rebuild-artifact:sha256:{hashlib.sha256(canonical(digest_payload).encode('utf-8')).hexdigest()}"
    return artifact


def _artifact_result(artifact: Mapping[str, object], operation_id: str) -> dict[str, object]:
    required = {"schema_version", "operation_id", "status", "manifest_id", "source_fingerprint", "database_uri", "entry_count", "hit_count", "artifact_ref", "artifact_revision"}
    if set(artifact) != required or artifact.get("operation_id") != operation_id or artifact.get("status") != "ready":
        raise ValueError("index rebuild artifact drifted")
    if not isinstance(artifact.get("entry_count"), int) or not isinstance(artifact.get("artifact_revision"), str) or not artifact["artifact_revision"].startswith("index-rebuild-artifact:sha256:"):
        raise ValueError("index rebuild artifact revision is invalid")
    return {"status": "ready", "artifact_ref": artifact["artifact_ref"], "artifact_revision": artifact["artifact_revision"], "entry_count": artifact["entry_count"]}


def _active_matches(active, candidate, operation_id, artifact) -> bool:
    return (isinstance(active, Mapping) and active.get("status") == "active"
            and active.get("source") == "verified_index_rebuild_effect"
            and active.get("verified_operation_id") == operation_id
            and active.get("artifact_revision") == artifact.get("artifact_revision")
            and active.get("source_fingerprint") == candidate.get("source_fingerprint")
            and active.get("database_uri") == artifact.get("database_uri"))


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise ValueError("index rebuild domain is invalid")
    return result


def _operation_id_from_domain(domain: Mapping[str, object]) -> str:
    request_ref = domain.get("request_ref")
    if not isinstance(request_ref, str):
        raise ValueError("index rebuild request reference is invalid")
    # The Effect handler owns the authoritative operation id; callers attach it
    # before querying through this private marker.
    operation_id = domain.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("index rebuild operation identity is unavailable")
    return operation_id


def _ref_id(value: str, kind: str) -> str:
    prefix = f"facts:index-rebuild/{kind}/"
    if not isinstance(value, str) or not value.startswith(prefix) or not value[len(prefix):]:
        raise ValueError("index rebuild fact reference is invalid")
    return value[len(prefix):]


def _candidate_database_path(runtime_root: Path, operation_id: str) -> Path:
    safe = canonical({"operation_id": operation_id}).encode("utf-8").hex()[:48]
    return runtime_root / ".rebuild-data" / "recall-indexes" / f"effect-{safe}.sqlite3"
