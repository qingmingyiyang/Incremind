from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from core.product_core.external_import_framework import ExternalImportResult
from core.product_core.dual_time import (
    optional_rfc3339_timestamp,
    require_rfc3339_timestamp,
)
from core.storage_provider import JsonObjectStore


MAX_EXTERNAL_SOURCES = 20_000
MAX_SOURCE_CONTENT_BYTES = 8 * 1024 * 1024


class ExternalImportPersistenceError(ValueError):
    """Raised when a validated external result cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class ExternalImportPersistenceResult:
    sources: tuple[dict[str, object], ...]
    candidates: tuple[dict[str, object], ...]
    skipped_candidates: int
    failures: tuple[dict[str, str], ...]


def persist_external_import(
    *,
    store: JsonObjectStore,
    result: ExternalImportResult,
    namespace_id: str,
    project_id: str,
    batch_id: str,
    created_at: str,
) -> ExternalImportPersistenceResult:
    # ``created_at`` is retained as a compatibility parameter for callers.  It
    # is the local persistence clock, not an external fact timestamp.
    recorded_at = _required_recorded_at(created_at)
    if len(result.sources) > MAX_EXTERNAL_SOURCES:
        raise ExternalImportPersistenceError(
            f"external import exceeds {MAX_EXTERNAL_SOURCES} sources"
        )

    persisted_sources: list[dict[str, object]] = []
    source_uri_by_ref: dict[str, str] = {}
    failures: list[dict[str, str]] = []
    for source in result.sources:
        encoded = source.content.encode("utf-8")
        if not encoded:
            failures.append({"id": source.source_id, "error": "source_content_empty"})
            continue
        if len(encoded) > MAX_SOURCE_CONTENT_BYTES:
            failures.append({"id": source.source_id, "error": "source_content_too_large"})
            continue
        source_id = _source_id(source, project_id)
        source_uri = f"crp://{namespace_id}/sources/{source_id}"
        occurred_at = _occurred_at(source.occurred_at, source.created_at)
        record = {
            "schema_version": "1.1.0",
            "id": source_id,
            "type": "text",
            "title": source.title or "外部 LLM 资料",
            "capture_mode": "snapshot",
            "storage_uri": source_uri,
            "original_url": None,
            "content_hash": hashlib.sha256(encoded).hexdigest(),
            "media_type": "text/plain",
            "size_bytes": len(encoded),
            "parser_version": f"external-import:{result.platform}",
            "processing_state": "captured",
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            # Legacy aliases are derived only at the persistence boundary.
            "created_at": occurred_at or recorded_at,
            "observed_at": recorded_at,
            "imported_from_legacy": False,
            "trust_status": "imported_unverified",
            "project_id": project_id,
            "metadata": {
                "content": source.content,
                "source_role": source.source_role,
                "external_source_type": source.source_type,
                "external_source_id": source.source_id,
                "external_source_ref": source.source_ref,
                "external_evidence_refs": list(source.evidence_refs),
                "raw_format": source.raw_format,
                "capture_origin": "external_import",
                "import_batch_id": batch_id,
            },
        }
        existing = store.read("sources", source_id)
        if existing is None:
            store.write("sources", source_id, record, expected_revision=0)
        elif not _same_source(existing, record):
            raise ExternalImportPersistenceError(
                f"external source identity collision for {source_id}"
            )
        # A retry must report the original record rather than a freshly
        # constructed payload whose recorded_at would otherwise drift.
        persisted_sources.append(dict(existing) if existing is not None else record)
        source_uri_by_ref[source.source_ref] = source_uri
        source_uri_by_ref[source.source_id] = source_uri

    candidates: list[dict[str, object]] = []
    skipped = 0
    for candidate in result.candidates:
        source_uri = source_uri_by_ref.get(candidate.source_ref)
        if not source_uri:
            failures.append({
                "id": candidate.memory_id,
                "error": "candidate_source_not_persisted",
            })
            continue
        candidate_id = _candidate_id(candidate, source_uri, project_id)
        source_id = source_uri.rsplit("/", 1)[-1]
        target_layer = _target_layer(candidate.layer, candidate.type)
        occurred_at = _occurred_at(candidate.occurred_at, candidate.created_at)
        payload = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "memory_id": candidate_id,
            "layer": candidate.layer,
            "type": candidate.type,
            "target_layer": target_layer,
            "candidate_type": candidate.type,
            "content": candidate.content,
            "proposed_content": candidate.content,
            "summary": candidate.summary,
            "confidence": candidate.confidence,
            "trust_level": candidate.trust_level,
            "source_platform": candidate.source_platform,
            "source_type": candidate.source_type,
            "source_role": candidate.source_role,
            "source_ref": source_uri,
            "source_id": source_id,
            "source_refs": [
                {
                    "source_id": source_id,
                    "locator": candidate.source_ref,
                }
            ],
            "evidence_refs": [source_uri],
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "created_at": occurred_at or recorded_at,
            "observed_at": recorded_at,
            "conflict_refs": list(candidate.conflict_refs),
            "status": "pending_review",
            "import_batch_id": batch_id,
            "privacy_level": "private",
            "provider_boundary": "local",
            "group": "low_trust" if candidate.trust_level == "low" else "needs_review",
            "project_id": project_id,
            "external_memory_id": candidate.memory_id,
        }
        existing = store.read("memory_candidates", candidate_id)
        if existing is not None:
            if not _same_candidate(existing, payload):
                raise ExternalImportPersistenceError(
                    f"external candidate identity collision for {candidate_id}"
                )
            skipped += 1
            continue
        try:
            store.write("memory_candidates", candidate_id, payload, expected_revision=0)
            candidates.append(payload)
        except Exception as exc:  # noqa: BLE001
            failures.append({"id": candidate_id, "error": f"candidate_save_failed: {exc}"})

    return ExternalImportPersistenceResult(
        sources=tuple(persisted_sources),
        candidates=tuple(candidates),
        skipped_candidates=skipped,
        failures=tuple(failures),
    )


def _source_id(source, project_id: str) -> str:
    payload = json.dumps({
        "project_id": project_id,
        "source_type": source.source_type,
        "source_role": source.source_role,
        "title": source.title,
        "content": source.content,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"source-external-{hashlib.sha256(payload).hexdigest()[:20]}"


def _required_recorded_at(value: str) -> str:
    try:
        return require_rfc3339_timestamp(value, field="recorded_at")
    except ValueError as exc:
        raise ExternalImportPersistenceError(str(exc)) from exc


def _occurred_at(primary: object, legacy_created_at: object) -> str | None:
    """Read dual time first, with created_at retained only for legacy input."""
    candidate = primary if primary is not None else legacy_created_at
    try:
        return optional_rfc3339_timestamp(candidate, field="occurred_at")
    except ValueError:
        # External clocks are untrusted facts.  An invalid value is unknown,
        # never a locally invented occurrence time.
        return None


def _candidate_id(candidate, source_uri: str, project_id: str) -> str:
    payload = json.dumps({
        "project_id": project_id,
        "layer": candidate.layer,
        "type": candidate.type,
        "content": candidate.content,
        "summary": candidate.summary,
        "source_uri": source_uri,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"memory-external-{hashlib.sha256(payload).hexdigest()[:20]}"


def _target_layer(layer: str, candidate_type: str) -> str:
    if candidate_type in {"persona", "preference", "rule"} and layer in {"L3", "L4"}:
        return "persona"
    if candidate_type in {"persona", "preference", "rule"}:
        raise ExternalImportPersistenceError(
            "external Persona candidate must target L4"
        )
    return {
        "L1": "atom",
        "L2": "scenario",
        "L3": "series_memory",
    }.get(layer, "source")


def _same_source(existing: dict[str, object], incoming: dict[str, object]) -> bool:
    return all(
        existing.get(key) == incoming.get(key)
        for key in ("id", "content_hash", "storage_uri", "project_id")
    )


def _same_candidate(existing: dict[str, object], incoming: dict[str, object]) -> bool:
    return all(
        existing.get(key) == incoming.get(key)
        for key in (
            "id",
            "layer",
            "type",
            "content",
            "summary",
            "source_ref",
            "project_id",
        )
    )
