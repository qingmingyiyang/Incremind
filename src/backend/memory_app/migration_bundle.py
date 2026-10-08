"""Portable, review-first recognition migration bundles.

The bundle is deliberately a small, JSON-only projection of the recognition
authority.  It is not a database dump: pending work, task output, documents,
configuration and binary attachments are excluded by construction.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from uuid import uuid4

from backend.recognition import ExperienceProvenance, ExperienceProvenanceError, RecognitionError, WorkScope
from backend.recognition.import_evidence import unresolved_import_evidence, unresolved_provenance_id
from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore, SQLiteStructuredRecordUnitOfWork


SCHEMA = "recognition-migration-v1"
_RECOGNITIONS = "recognitions"
_EXPERIENCES = "recognition_experiences"
_VERSIONS = "recognition_versions"
_RELATIONS = "recognition_relations"
_RELATION_PROPOSALS = "recognition_relation_proposals"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SAFE_ENVELOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,139}$")
_MAX_RECOGNITIONS = 100
_MAX_EXPERIENCES = 500
_MAX_VERSIONS = 2_000
_MAX_BYTES = 8 * 1024 * 1024

_TOP_KEYS = frozenset({"schema", "bundle_id", "created_at", "source_scope", "recognitions", "experiences", "versions", "relations", "approved_relation_proposals"})
_RECORD_KEYS = frozenset({"id", "revision", "payload"})
_SCOPE_KEYS = frozenset({"user_id", "project_id"})
_RECOGNITION_KEYS = frozenset({
    "id", "scope", "project_id", "content", "state", "version", "source_experience_ids",
    "source_recognition_ids", "source_experience_revisions", "source_recognition_revisions",
    "parent_ids", "successor_ids", "conditions", "published_by", "created_at", "updated_at", "revoked_at", "stale_reason", "revocation_reason",
})
_EXPERIENCE_KEYS = frozenset({"id", "scope", "project_id", "content", "state", "created_at", "provenance", "revoked_at"})
_VERSION_KEYS = frozenset({"id", "recognition_id", "recognition_revision", "version", "action", "snapshot", "recorded_at"})
_RELATION_KEYS = frozenset({"id", "scope", "project_id", "from_id", "to_id", "relation", "created_at"})
_RELATION_PROPOSAL_KEYS = frozenset({"id", "scope", "project_id", "from_id", "to_id", "relation", "evidence", "source", "from_revision", "to_revision", "state", "created_at", "reviewed_at"})
_RELATION_TYPES = frozenset({"supports", "refutes", "supplements", "supersedes", "derived_from"})


class MigrationBundleError(RecognitionError):
    """A migration bundle is malformed, incomplete, or outside its boundary."""


class MigrationBundleService:
    """Export a bounded, dependency-honest selection without mutating storage."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    def export(
        self,
        scope: WorkScope,
        recognition_ids: Sequence[str],
        allowed_experience_ids: Sequence[str],
    ) -> dict[str, object]:
        ids = _ids(recognition_ids, "recognition_ids", 1, _MAX_RECOGNITIONS)
        allowed_experiences = _ids(allowed_experience_ids, "allowed_experience_ids", 0, _MAX_EXPERIENCES)
        with self._records.begin() as uow:
            recognitions = [_require_scoped(uow, _RECOGNITIONS, scope, item_id) for item_id in ids]
            versions = [
                record for record in uow.list(_VERSIONS)
                if record.payload.get("recognition_id") in set(ids)
            ]
            if len(versions) > _MAX_VERSIONS:
                raise MigrationBundleError("too many recognition versions")
            referenced_experience_ids = _referenced_experience_ids(recognitions, versions)
            selected_experience_ids = tuple(item_id for item_id in allowed_experiences if item_id in referenced_experience_ids)
            experiences = [_require_scoped(uow, _EXPERIENCES, scope, item_id) for item_id in selected_experience_ids]
            selected = set(ids)
            relations = [
                record for record in uow.list(_RELATIONS)
                if _same_scope(record.payload, scope)
                and record.payload.get("from_id") in selected and record.payload.get("to_id") in selected
            ]
            proposals = [
                record for record in uow.list(_RELATION_PROPOSALS)
                if _same_scope(record.payload, scope) and record.payload.get("state") == "approved"
                and record.payload.get("from_id") in selected and record.payload.get("to_id") in selected
            ]
            bundle = {
                "schema": SCHEMA,
                "bundle_id": str(uuid4()),
                "created_at": _now(),
                "source_scope": _scope(scope),
                "recognitions": [_record(record, _recognition_payload, scope) for record in recognitions],
                "experiences": [_record(record, _experience_payload, scope) for record in experiences],
                "versions": [_record(record, _version_payload, scope) for record in versions],
                "relations": [_record(record, _relation_payload, scope) for record in relations],
                "approved_relation_proposals": [_record(record, _relation_proposal_payload, scope) for record in proposals],
                "prior_versions": _imported_history(uow, scope, selected),
            }
            _preserve_unresolved_import_refs(uow, scope, bundle["experiences"])
            uow.rollback()
        normalized = validate_bundle(bundle)
        issues = inspect_bundle(normalized)
        return {"bundle": normalized, "issues": issues, "summary": _summary(normalized, requested_experience_ids=allowed_experiences)}


def _preserve_unresolved_import_refs(reader, scope, experiences):
    """Keep old imports from laundering mismatched references through export."""
    targets = {row["id"]: row["payload"] for row in experiences}
    for imported, target_id, issue in unresolved_import_evidence(reader):
        if target_id not in targets or not _same_scope(imported, scope):
            continue
        original = next((row for row in imported["bundle"]["experiences"]
                         if row["id"] == issue["record_id"]), None)
        if original is None:
            continue
        refs = original["payload"].get("provenance", {}).get("source_refs", ())
        for index, ref in enumerate(refs):
            if (ref["type"] != issue.get("source_type") or ref["id"] != issue.get("source_id")
                    or ("source_revision" in issue and ref.get("revision") != issue["source_revision"])):
                continue
            group = "experiences" if ref["type"] == "experience" else "recognitions" if ref["type"] == "recognition" else None
            source = next((row for row in imported["bundle"].get(group, ()) if row["id"] == ref["id"]), None)
            if source is not None and ("revision" not in ref or ref["revision"] == source["revision"]):
                continue
            unresolved_id = unresolved_provenance_id(imported["id"], index)
            mapped_id = imported["mapping"].get(group, {}).get(ref["id"], unresolved_id)
            current_refs = targets[target_id].get("provenance", {}).get("source_refs", ())
            if index >= len(current_refs):
                continue
            current = current_refs[index]
            if current["type"] == ref["type"] and current["id"] in {mapped_id, unresolved_id}:
                current["id"] = unresolved_id
                if "revision" in ref:
                    current["revision"] = ref["revision"]


def validate_bundle(value: object) -> dict[str, object]:
    """Return a JSON-normalized bundle after strict structural validation.

    This routine accepts no hidden collections or extension fields.  It is safe
    for an import preview to call before making any migration decision.
    """
    bundle = _json_object(value, "bundle")
    _exact_keys({key: item for key, item in bundle.items() if key != "prior_versions"}, _TOP_KEYS, "bundle")
    if bundle.get("schema") != SCHEMA:
        raise MigrationBundleError("bundle schema is invalid")
    _uuid(bundle.get("bundle_id"), "bundle_id")
    _iso_time(bundle.get("created_at"), "created_at")
    scope = _scope_from_payload(bundle.get("source_scope"), "source_scope")
    normalized: dict[str, object] = {
        "schema": SCHEMA, "bundle_id": str(bundle["bundle_id"]), "created_at": str(bundle["created_at"]),
        "source_scope": _scope(scope),
    }
    recognitions = _records(bundle.get("recognitions"), "recognitions", _recognition_payload, scope, 1, _MAX_RECOGNITIONS)
    experiences = _records(bundle.get("experiences"), "experiences", _experience_payload, scope, 0, _MAX_EXPERIENCES)
    versions = _records(bundle.get("versions"), "versions", _version_payload, scope, 0, _MAX_VERSIONS)
    relations = _records(bundle.get("relations"), "relations", _relation_payload, scope, 0, _MAX_RECOGNITIONS * _MAX_RECOGNITIONS)
    proposals = _records(bundle.get("approved_relation_proposals"), "approved_relation_proposals", _relation_proposal_payload, scope, 0, _MAX_RECOGNITIONS * _MAX_RECOGNITIONS)
    normalized.update({"recognitions": recognitions, "experiences": experiences, "versions": versions, "relations": relations, "approved_relation_proposals": proposals})
    normalized["prior_versions"] = _prior_versions(bundle.get("prior_versions", []), {row["id"] for row in recognitions})
    if len(versions) + len(normalized["prior_versions"]) > _MAX_VERSIONS:
        raise MigrationBundleError("too many recognition versions")
    _validate_cross_references(normalized, scope)
    _ensure_size(normalized)
    return normalized


def inspect_bundle(bundle: object) -> list[dict[str, object]]:
    """Report unresolved references without granting them a stronger status."""
    value = validate_bundle(bundle)
    recognitions = {row["id"]: row for row in value["recognitions"]}
    experiences = {row["id"]: row for row in value["experiences"]}
    issues: list[dict[str, object]] = []
    for row in value["recognitions"]:
        _inspect_recognition_sources(issues, row["id"], row["payload"], "current", recognitions, experiences)
    history = {
        (row["payload"]["recognition_id"], row["payload"]["recognition_revision"])
        for row in value["versions"]
    }
    for row in value["versions"]:
        payload = row["payload"]
        _inspect_recognition_sources(issues, row["id"], payload["snapshot"], "history", recognitions, experiences, history)
    for row in value["experiences"]:
        provenance = row["payload"].get("provenance")
        if provenance is None:
            issues.append({"code": "legacy_provenance_unspecified", "record_id": row["id"], "source_type": "experience", "source_id": row["id"], "location": "provenance"})
            continue
        for ref in provenance["source_refs"]:
            source_type, source_id = ref["type"], ref["id"]
            records = experiences if source_type == "experience" else recognitions if source_type == "recognition" else {}
            source = records.get(source_id)
            if source is None:
                item: dict[str, object] = {"code": "external_provenance_reference", "record_id": row["id"], "source_type": source_type, "source_id": source_id, "location": "provenance"}
                if "revision" in ref:
                    item["source_revision"] = ref["revision"]
                issues.append(item)
            elif "revision" in ref and source["revision"] != ref["revision"]:
                issues.append({"code": "source_revision_mismatch", "record_id": row["id"], "source_type": source_type, "source_id": source_id, "source_revision": ref["revision"], "location": "provenance"})
    for row in value["approved_relation_proposals"]:
        payload = row["payload"]
        for field, revision_field in (("from_id", "from_revision"), ("to_id", "to_revision")):
            source_id = payload[field]
            if recognitions[source_id]["revision"] != payload[revision_field]:
                issues.append({"code": "approved_relation_endpoint_revision_mismatch", "record_id": row["id"], "source_type": "recognition", "source_id": source_id, "source_revision": payload[revision_field], "location": "relation"})
    return issues


def _record(record: SQLiteStructuredRecord, payload_validator, scope: WorkScope) -> dict[str, object]:
    return {"id": record.object_id, "revision": record.revision, "payload": payload_validator(record.payload, scope)}


def _records(value: object, label: str, payload_validator, scope: WorkScope, minimum: int, maximum: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise MigrationBundleError(f"{label} count is invalid")
    result: list[dict[str, object]] = []
    ids: set[str] = set()
    for item in value:
        record = _json_object(item, f"{label} record")
        _exact_keys(record, _RECORD_KEYS, f"{label} record")
        item_id = _envelope_id(record.get("id"), f"{label} record id")
        if item_id in ids:
            raise MigrationBundleError(f"{label} contains duplicate ids")
        ids.add(item_id)
        revision = _revision(record.get("revision"), f"{label} record revision")
        payload = payload_validator(record.get("payload"), scope)
        if payload["id"] != item_id:
            raise MigrationBundleError(f"{label} record identity is invalid")
        result.append({"id": item_id, "revision": revision, "payload": payload})
    return result


def _recognition_payload(value: object, scope: WorkScope) -> dict[str, object]:
    payload = _json_object(value, "recognition payload")
    _allowed_keys(payload, _RECOGNITION_KEYS, "recognition payload")
    _payload_scope(payload, scope, "recognition payload")
    result = _recognition_common(payload, scope, "recognition payload")
    for name in ("published_by", "created_at", "updated_at", "revoked_at", "stale_reason", "revocation_reason"):
        if name in payload:
            result[name] = _nullable_text(payload[name], name) if name in {"revoked_at"} else _text_or_none(payload[name], name)
    if "created_at" not in result or "updated_at" not in result:
        raise MigrationBundleError("recognition payload timestamps are required")
    _iso_time(result["created_at"], "created_at")
    _iso_time(result["updated_at"], "updated_at")
    if result.get("revoked_at") is not None:
        _iso_time(result["revoked_at"], "revoked_at")
    return result


def _recognition_common(payload: Mapping[str, object], scope: WorkScope, label: str) -> dict[str, object]:
    result: dict[str, object] = {
        "id": _id(payload.get("id"), f"{label} id"), "scope": _scope(scope), "project_id": scope.project_id,
        "content": _content(payload.get("content"), f"{label} content"),
        "state": _enum(payload.get("state"), {"active", "stale", "superseded", "revoked"}, f"{label} state"),
        "version": _version_number(payload.get("version"), f"{label} version"),
        "source_experience_ids": _ids(payload.get("source_experience_ids"), f"{label} source experience ids", 0, _MAX_EXPERIENCES),
        "source_recognition_ids": _ids(payload.get("source_recognition_ids"), f"{label} source recognition ids", 0, _MAX_RECOGNITIONS),
        "parent_ids": _ids(payload.get("parent_ids"), f"{label} parent ids", 0, _MAX_RECOGNITIONS),
        "conditions": _texts(payload.get("conditions"), f"{label} conditions", 0, 100),
    }
    result["source_experience_revisions"] = _revision_map(payload.get("source_experience_revisions"), result["source_experience_ids"], f"{label} source experience revisions")
    result["source_recognition_revisions"] = _revision_map(payload.get("source_recognition_revisions"), result["source_recognition_ids"], f"{label} source recognition revisions")
    if "successor_ids" in payload:
        result["successor_ids"] = _ids(payload["successor_ids"], f"{label} successor ids", 1, _MAX_RECOGNITIONS)
    if result["id"] in result["source_recognition_ids"] or result["id"] in result["parent_ids"]:
        raise MigrationBundleError(f"{label} cannot reference itself")
    return result


def _experience_payload(value: object, scope: WorkScope) -> dict[str, object]:
    payload = _json_object(value, "experience payload")
    _allowed_keys(payload, _EXPERIENCE_KEYS, "experience payload")
    _payload_scope(payload, scope, "experience payload")
    result: dict[str, object] = {"id": _id(payload.get("id"), "experience payload id"), "scope": _scope(scope), "project_id": scope.project_id, "content": _content(payload.get("content"), "experience payload content"), "state": _enum(payload.get("state"), {"active", "revoked"}, "experience payload state"), "created_at": _text(payload.get("created_at"), "experience payload created_at"), "revoked_at": _nullable_text(payload.get("revoked_at"), "experience payload revoked_at")}
    _iso_time(result["created_at"], "experience payload created_at")
    if result["revoked_at"] is not None:
        _iso_time(result["revoked_at"], "experience payload revoked_at")
    if "provenance" in payload:
        try:
            result["provenance"] = ExperienceProvenance.from_payload(payload["provenance"]).to_payload()
        except ExperienceProvenanceError as exc:
            raise MigrationBundleError("experience provenance is invalid") from exc
    return result


def _version_payload(value: object, scope: WorkScope) -> dict[str, object]:
    payload = _json_object(value, "version payload")
    _exact_keys(payload, _VERSION_KEYS, "version payload")
    snapshot = _recognition_payload(payload.get("snapshot"), scope)
    recognition_id = _id(payload.get("recognition_id"), "version recognition id")
    version = _version_number(payload.get("version"), "version")
    result = {"id": _version_id(payload.get("id"), recognition_id, version), "recognition_id": recognition_id, "recognition_revision": _revision(payload.get("recognition_revision"), "version recognition revision"), "version": version, "action": _text(payload.get("action"), "version action"), "snapshot": snapshot, "recorded_at": _text(payload.get("recorded_at"), "version recorded_at")}
    _iso_time(result["recorded_at"], "version recorded_at")
    if snapshot["id"] != result["recognition_id"] or snapshot["version"] != result["version"]:
        raise MigrationBundleError("version snapshot identity is invalid")
    return result


def _relation_payload(value: object, scope: WorkScope) -> dict[str, object]:
    payload = _json_object(value, "relation payload")
    _exact_keys(payload, _RELATION_KEYS, "relation payload")
    _payload_scope(payload, scope, "relation payload")
    result = {"id": _id(payload.get("id"), "relation id"), "scope": _scope(scope), "project_id": scope.project_id, "from_id": _id(payload.get("from_id"), "relation from_id"), "to_id": _id(payload.get("to_id"), "relation to_id"), "relation": _enum(payload.get("relation"), _RELATION_TYPES, "relation type"), "created_at": _text(payload.get("created_at"), "relation created_at")}
    _iso_time(result["created_at"], "relation created_at")
    if result["from_id"] == result["to_id"]:
        raise MigrationBundleError("relation endpoints are invalid")
    return result


def _relation_proposal_payload(value: object, scope: WorkScope) -> dict[str, object]:
    payload = _json_object(value, "relation proposal payload")
    _exact_keys(payload, _RELATION_PROPOSAL_KEYS, "relation proposal payload")
    _payload_scope(payload, scope, "relation proposal payload")
    result = {"id": _id(payload.get("id"), "relation proposal id"), "scope": _scope(scope), "project_id": scope.project_id, "from_id": _id(payload.get("from_id"), "relation proposal from_id"), "to_id": _id(payload.get("to_id"), "relation proposal to_id"), "relation": _enum(payload.get("relation"), _RELATION_TYPES, "relation proposal type"), "evidence": _content(payload.get("evidence"), "relation proposal evidence"), "source": _enum(payload.get("source"), {"manual"}, "relation proposal source"), "from_revision": _revision(payload.get("from_revision"), "relation proposal from_revision"), "to_revision": _revision(payload.get("to_revision"), "relation proposal to_revision"), "state": _enum(payload.get("state"), {"approved"}, "relation proposal state"), "created_at": _text(payload.get("created_at"), "relation proposal created_at"), "reviewed_at": _text(payload.get("reviewed_at"), "relation proposal reviewed_at")}
    _iso_time(result["created_at"], "relation proposal created_at")
    _iso_time(result["reviewed_at"], "relation proposal reviewed_at")
    if result["from_id"] == result["to_id"]:
        raise MigrationBundleError("relation proposal endpoints are invalid")
    return result


def _validate_cross_references(bundle: Mapping[str, object], scope: WorkScope) -> None:
    recognitions = {row["id"]: row for row in bundle["recognitions"]}
    experiences = {row["id"]: row for row in bundle["experiences"]}
    versions = bundle["versions"]
    for row in bundle["relations"] + bundle["approved_relation_proposals"]:
        payload = row["payload"]
        if payload["from_id"] not in recognitions or payload["to_id"] not in recognitions:
            raise MigrationBundleError("relation endpoint is outside selected recognitions")
    versions_by_recognition: dict[str, list[dict[str, object]]] = {item_id: [] for item_id in recognitions}
    for row in versions:
        payload = row["payload"]
        recognition_id = payload["recognition_id"]
        if recognition_id not in recognitions:
            raise MigrationBundleError("version belongs to an unselected recognition")
        versions_by_recognition[recognition_id].append(row)
    for recognition_id, record in recognitions.items():
        expected_versions = set(range(1, record["payload"]["version"] + 1))
        actual_versions = [row["payload"]["version"] for row in versions_by_recognition[recognition_id]]
        if set(actual_versions) != expected_versions or len(actual_versions) != len(expected_versions):
            raise MigrationBundleError("recognition version history is incomplete")
        ordered = sorted(versions_by_recognition[recognition_id], key=lambda row: row["payload"]["version"])
        revisions = [row["payload"]["recognition_revision"] for row in ordered]
        if any(before >= after for before, after in zip(revisions, revisions[1:])):
            raise MigrationBundleError("recognition history revisions must increase")
        current_row = next(row for row in versions_by_recognition[recognition_id] if row["payload"]["version"] == record["payload"]["version"])
        if current_row["payload"]["snapshot"] != record["payload"] or current_row["payload"]["recognition_revision"] != record["revision"]:
            raise MigrationBundleError("current recognition version snapshot is invalid")
    # The source maps are retained even if their source was not selected; the
    # resulting unresolved edge is an explicit inspection issue, not a claim
    # that migration includes a hidden dependency closure.
    for row in recognitions.values():
        _source_maps(row["payload"])
    for row in versions:
        _source_maps(row["payload"]["snapshot"])


def _inspect_recognition_sources(issues, record_id, payload, location, recognitions, experiences, history=()):
    experience_ids, recognition_ids, experience_revisions, recognition_revisions = _source_maps(payload)
    for source_id in experience_ids:
        _append_source_issue(issues, record_id, "experience", source_id, experience_revisions[source_id], location, experiences)
    for source_id in recognition_ids:
        _append_source_issue(issues, record_id, "recognition", source_id, recognition_revisions[source_id], location, recognitions, history)


def _append_source_issue(issues, record_id, source_type, source_id, source_revision, location, available, history=()):
    source = available.get(source_id)
    if source is None:
        issues.append({"code": "missing_source", "record_id": record_id, "source_type": source_type, "source_id": source_id, "source_revision": source_revision, "location": location})
        return
    if location == "current" and source["payload"]["state"] != "active":
        issues.append({"code": "source_not_active", "record_id": record_id, "source_type": source_type, "source_id": source_id, "source_revision": source_revision, "location": location})
    if source["revision"] != source_revision and not (location == "history" and (source_id, source_revision) in history):
        issues.append({"code": "source_revision_mismatch", "record_id": record_id, "source_type": source_type, "source_id": source_id, "source_revision": source_revision, "location": location})


def _referenced_experience_ids(recognitions: Iterable[SQLiteStructuredRecord], versions: Iterable[SQLiteStructuredRecord]) -> set[str]:
    result: set[str] = set()
    for record in recognitions:
        value = record.payload.get("source_experience_ids")
        if isinstance(value, list):
            result.update(item for item in value if isinstance(item, str))
    for record in versions:
        snapshot = record.payload.get("snapshot")
        if isinstance(snapshot, Mapping):
            value = snapshot.get("source_experience_ids")
            if isinstance(value, list):
                result.update(item for item in value if isinstance(item, str))
    return result


def _source_maps(payload: Mapping[str, object]):
    experience_ids = tuple(payload["source_experience_ids"])
    recognition_ids = tuple(payload["source_recognition_ids"])
    return experience_ids, recognition_ids, payload["source_experience_revisions"], payload["source_recognition_revisions"]


def _summary(bundle: Mapping[str, object], *, requested_experience_ids: Sequence[str]) -> dict[str, int]:
    return {"recognitions": len(bundle["recognitions"]), "experiences": len(bundle["experiences"]), "versions": len(bundle["versions"]), "prior_versions": len(bundle["prior_versions"]), "relations": len(bundle["relations"]), "approved_relation_proposals": len(bundle["approved_relation_proposals"]), "requested_experiences": len(requested_experience_ids)}


def _prior_versions(value, selected):
    if not isinstance(value, list) or len(value) > _MAX_VERSIONS:
        raise MigrationBundleError("prior versions count is invalid")
    result, seen = [], set()
    for raw in value:
        item = _json_object(raw, "prior version")
        _exact_keys(item, {"recognition_id", "origin_bundle_id", "source_scope", "source_record"}, "prior version")
        recognition_id = _id(item["recognition_id"], "prior version recognition id")
        if recognition_id not in selected:
            raise MigrationBundleError("prior version belongs to an unselected recognition")
        _uuid(item["origin_bundle_id"], "origin bundle id")
        scope = _scope_from_payload(item["source_scope"], "prior version source scope")
        source = _records([item["source_record"]], "prior versions", _version_payload, scope, 1, 1)[0]
        identity = (recognition_id, item["origin_bundle_id"], source["id"])
        if identity in seen:
            raise MigrationBundleError("duplicate prior version")
        seen.add(identity)
        result.append({"recognition_id": recognition_id, "origin_bundle_id": item["origin_bundle_id"],
                       "source_scope": _scope(scope), "source_record": source})
    return result


def _imported_history(uow, scope, selected):
    result, archives = [], {}
    for pointer in uow.list("recognition_migration_versions"):
        payload = pointer.payload
        if not _same_scope(payload, scope) or payload.get("recognition_id") not in selected:
            continue
        import_id = payload.get("import_id")
        if import_id not in archives:
            archive = _require_scoped(uow, "recognition_migration_imports", scope, import_id)
            archives[import_id] = (validate_bundle(archive.payload.get("bundle")), archive.payload.get("mapping", {}))
        bundle, mapping = archives[import_id]
        group, index = payload.get("source_group"), payload.get("source_index")
        if group not in {"versions", "prior_versions"} or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(bundle[group]):
            raise MigrationBundleError("imported history pointer is invalid")
        source = bundle[group][index]
        source_id = source["payload"]["recognition_id"] if group == "versions" else source["recognition_id"]
        if mapping.get("recognitions", {}).get(source_id) != payload["recognition_id"]:
            raise MigrationBundleError("imported history binding is invalid")
        result.append({"recognition_id": payload["recognition_id"],
                       "origin_bundle_id": bundle["bundle_id"] if group == "versions" else source["origin_bundle_id"],
                       "source_scope": bundle["source_scope"] if group == "versions" else source["source_scope"],
                       "source_record": source if group == "versions" else source["source_record"]})
    return _prior_versions(result, selected)


def read_imported_history(records, scope, recognition_id):
    with records.begin() as uow:
        _require_scoped(uow, _RECOGNITIONS, scope, recognition_id)
        result = _imported_history(uow, scope, {recognition_id})
        uow.rollback()
    return result


def _require_scoped(uow: SQLiteStructuredRecordUnitOfWork, collection: str, scope: WorkScope, object_id: str) -> SQLiteStructuredRecord:
    record = uow.read(collection, object_id)
    if record is None or not _same_scope(record.payload, scope):
        raise MigrationBundleError("record is unavailable in this work scope")
    return record


def _same_scope(payload: Mapping[str, object], scope: WorkScope) -> bool:
    value = payload.get("scope")
    return isinstance(value, Mapping) and value.get("user_id") == scope.user_id and value.get("project_id") == scope.project_id and payload.get("project_id") == scope.project_id


def _scope(scope: WorkScope) -> dict[str, str | None]:
    return {"user_id": scope.user_id, "project_id": scope.project_id}


def _scope_from_payload(value: object, label: str) -> WorkScope:
    payload = _json_object(value, label)
    _exact_keys(payload, _SCOPE_KEYS, label)
    try:
        return WorkScope(payload.get("user_id"), payload.get("project_id"))
    except RecognitionError as exc:
        raise MigrationBundleError(f"{label} is invalid") from exc


def _payload_scope(payload: Mapping[str, object], scope: WorkScope, label: str) -> None:
    if _scope_from_payload(payload.get("scope"), f"{label} scope") != scope or payload.get("project_id") != scope.project_id:
        raise MigrationBundleError(f"{label} scope is invalid")


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise MigrationBundleError(f"{label} is invalid")
    _finite(value, label)
    return dict(value)


def _finite(value: object, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise MigrationBundleError(f"{label} contains non-finite JSON")
    if isinstance(value, Mapping):
        for item in value.values(): _finite(item, label)
    elif isinstance(value, (list, tuple)):
        for item in value: _finite(item, label)


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise MigrationBundleError(f"{label} fields are invalid")


def _allowed_keys(value: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    if set(value).difference(allowed):
        raise MigrationBundleError(f"{label} contains unsupported fields")


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise MigrationBundleError(f"{label} is invalid")
    return value


def _envelope_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ENVELOPE_ID.fullmatch(value):
        raise MigrationBundleError(f"{label} is invalid")
    return value


def _version_id(value: object, recognition_id: str, version: int) -> str:
    expected = f"{recognition_id}~v{version}"
    if value != expected:
        raise MigrationBundleError("version payload id is invalid")
    return expected


def _ids(value: object, label: str, minimum: int, maximum: int) -> list[str]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)) or not minimum <= len(value) <= maximum:
        raise MigrationBundleError(f"{label} count is invalid")
    result = [_id(item, label) for item in value]
    if len(result) != len(set(result)):
        raise MigrationBundleError(f"{label} contains duplicates")
    return result


def _revision(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MigrationBundleError(f"{label} is invalid")
    return value


def _version_number(value: object, label: str) -> int:
    number = _revision(value, label)
    if number > _MAX_VERSIONS:
        raise MigrationBundleError(f"{label} is too large")
    return number


def _revision_map(value: object, ids: Sequence[str], label: str) -> dict[str, int]:
    mapping = _json_object(value, label)
    if set(mapping) != set(ids):
        raise MigrationBundleError(f"{label} is invalid")
    return {item_id: _revision(mapping[item_id], label) for item_id in ids}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 100_000:
        raise MigrationBundleError(f"{label} is invalid")
    return value


def _content(value: object, label: str) -> str:
    return _text(value, label)


def _texts(value: object, label: str, minimum: int, maximum: int) -> list[str]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)) or not minimum <= len(value) <= maximum:
        raise MigrationBundleError(f"{label} count is invalid")
    result = [_text(item, label) for item in value]
    if len(result) != len(set(result)):
        raise MigrationBundleError(f"{label} contains duplicates")
    return result


def _nullable_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _text_or_none(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _enum(value: object, allowed: set[str] | frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise MigrationBundleError(f"{label} is invalid")
    return value


def _iso_time(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise MigrationBundleError(f"{label} is invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationBundleError(f"{label} is invalid") from exc


def _uuid(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise MigrationBundleError(f"{label} is invalid")
    try:
        from uuid import UUID
        UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MigrationBundleError(f"{label} is invalid") from exc


def _ensure_size(value: Mapping[str, object]) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise MigrationBundleError("bundle is not JSON serializable") from exc
    if size > _MAX_BYTES:
        raise MigrationBundleError("bundle exceeds maximum size")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
