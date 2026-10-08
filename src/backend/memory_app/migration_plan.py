"""Read-only import planning for selected recognition bundles.

Source history keeps its source revision identity.  Imported live records will
start at target CAS revision one; the preview never pretends these are equal.
"""

from collections.abc import Mapping
from copy import deepcopy
from uuid import UUID

from backend.recognition import RecognitionError, WorkScope
from backend.recognition.import_evidence import unresolved_provenance_id
from .migration_bundle import inspect_bundle, validate_bundle
from .source_egress import POLICY_COLLECTIONS


_COLLECTIONS = {
    "recognitions": "recognitions",
    "experiences": "recognition_experiences",
    "relations": "recognition_relations",
    "approved_relation_proposals": "recognition_relation_proposals",
    "versions": "recognition_migration_versions",
}
_PREFIXES = {"recognitions": "r", "experiences": "e", "relations": "rel",
             "approved_relation_proposals": "rp", "versions": "h", "prior_versions": "hp"}


class ImportPreviewService:
    def __init__(self, records):
        self._records = records

    def preview(self, *, scope: WorkScope, bundle, import_id: str, strategy: str = "reject"):
        value = validate_bundle(bundle)
        with self.records.begin() as tx:
            result = self.preview_in_uow(tx, scope=scope, bundle=value, import_id=import_id, strategy=strategy)
            tx.rollback()
        return result

    @property
    def records(self):
        return self._records

    def preview_in_uow(self, tx, *, scope: WorkScope, bundle, import_id: str, strategy: str = "reject"):
        """Build the exact import plan against an already-open snapshot.

        The commit boundary calls this routine inside its write transaction.
        Keeping target-slot inspection here prevents a preview from being
        accepted after another writer has occupied one of its slots.
        """
        value = validate_bundle(bundle)
        try:
            canonical_id = str(UUID(import_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise RecognitionError("migration import_id must be a UUID") from exc
        if import_id != canonical_id:
            raise RecognitionError("migration import_id must be a canonical UUID")
        if strategy not in {"reject", "copy"}:
            raise RecognitionError("migration collision strategy is invalid")

        mapping = {}
        for group in _COLLECTIONS:
            mapping[group] = {
                record["id"]: (record["id"] if strategy == "reject" and group != "versions"
                               else f"migration-{canonical_id}-{_PREFIXES[group]}-{index}")
                for index, record in enumerate(value[group])
            }
        mapping["prior_versions"] = {
            str(index): f"migration-{canonical_id}-{_PREFIXES['prior_versions']}-{index}"
            for index, _ in enumerate(value.get("prior_versions", ()))
        }
        slots = [(collection, target) for group, collection in _COLLECTIONS.items()
                 for target in mapping[group].values()]
        slots.extend(("recognition_versions", f"{target}~v1")
                     for target in mapping["recognitions"].values())
        # Prior imported history has no portable envelope ID of its own.  Its
        # stable address is its position in the canonical prior_versions list.
        slots.extend(("recognition_migration_versions", target)
                     for target in mapping["prior_versions"].values())
        slots.append(("recognition_migration_imports", canonical_id))
        # Domain graph nodes share an ID namespace in the projection even
        # though the underlying structured store keys also contain collection.
        for group in ("recognitions", "experiences"):
            for target in mapping[group].values():
                slots.extend((collection, target) for collection in
                             ("recognitions", "recognition_experiences", "recognition_questions"))
        # Policy records share their source ID as their object ID.  They are
        # never portable permission grants, so importing a fresh r1 source
        # into a slot occupied by an old policy could otherwise accidentally
        # revive that policy.  Include both typed policy collections in the
        # reviewed snapshot for reject and copy mappings alike.
        for source_type, group in (("experience", "experiences"),
                                   ("recognition", "recognitions")):
            policy_collection = POLICY_COLLECTIONS[source_type]
            slots.extend((policy_collection, target)
                         for target in mapping[group].values())
        slots = list(dict.fromkeys(slots))
        conflicts, target_snapshot = [], []
        for collection, target in slots:
            if len(target) > 128:
                target_snapshot.append({"collection": collection, "id": target,
                                        "unavailable": "target_id_too_long"})
                conflicts.append({"collection": collection, "id": target,
                                  "reason": "target_id_too_long"})
                continue
            existing = tx.read(collection, target)
            tombstone = tx.read("recognition_tombstones", target)
            target_snapshot.append({"collection": collection, "id": target,
                                    "revision": existing.revision if existing else 0,
                                    "tombstone_revision": tombstone.revision if tombstone else 0})
            if existing or tombstone:
                conflicts.append({"collection": collection, "id": target,
                                  "reason": "erased_id" if tombstone else "id_exists"})

        issues = inspect_bundle(value)
        plan_basis = {"import_id": canonical_id, "mapping": mapping}
        _, projected_recognitions = project_target_payloads(value, plan_basis, scope, issues)
        return {
            "schema": "recognition-import-preview-v1", "import_id": canonical_id,
            "bundle_id": value["bundle_id"], "strategy": strategy,
            "source_scope": value["source_scope"],
            "target_scope": {"user_id": scope.user_id, "project_id": scope.project_id},
            "mapping": mapping, "target_snapshot": target_snapshot, "conflicts": conflicts,
            "issues": issues,
            "recognitions": [{"source_id": record["id"],
                              "target_id": mapping["recognitions"][record["id"]],
                              "source_revision": record["revision"], "target_initial_revision": 1,
                              "source_state": record["payload"]["state"],
                              "planned_state": projected_recognitions[record["id"]]["state"],
                              "reason": _planned_reason(projected_recognitions[record["id"]]),
                              "content": record["payload"]["content"],
                              "conditions": record["payload"].get("conditions", [])}
                             for record in value["recognitions"]],
            "counts": {**{group: len(value[group]) for group in _COLLECTIONS},
                       "prior_versions": len(value.get("prior_versions", ()))},
            "history_mode": "preserve_source_history_with_separate_target_baseline",
            "can_commit_without_collision": not conflicts,
            "planned_state": "ready" if not conflicts else "blocked",
            "commit_implemented": True,
        }


def project_target_payloads(value, plan, scope: WorkScope, issues: list[dict[str, object]]):
    """Pure target projection shared verbatim by preview and atomic commit."""
    mapping = plan["mapping"]
    imported_experiences = {row["id"]: row for row in value["experiences"]}
    imported_recognitions = {row["id"]: row for row in value["recognitions"]}
    experience_payloads: dict[str, dict[str, object]] = {}
    for row in value["experiences"]:
        source_id = row["id"]
        target_id = mapping["experiences"][source_id]
        payload = deepcopy(row["payload"])
        payload.update({"id": target_id, "scope": _scope(scope), "project_id": scope.project_id})
        provenance = payload.get("provenance")
        if isinstance(provenance, Mapping):
            payload["provenance"] = _map_provenance(provenance, mapping, source_id, plan["import_id"], issues)
        experience_payloads[source_id] = payload

    unresolved_experiences = {issue["record_id"] for issue in issues
        if issue.get("location") == "provenance"
        and issue.get("code") in {"external_provenance_reference", "source_revision_mismatch"}
        and issue.get("record_id") in imported_experiences}
    recognition_payloads: dict[str, dict[str, object]] = {}
    invalid: set[str] = set()
    for row in value["recognitions"]:
        source_id = row["id"]
        payload = deepcopy(row["payload"])
        target_id = mapping["recognitions"][source_id]
        payload.update({"id": target_id, "scope": _scope(scope), "project_id": scope.project_id, "version": 1})
        exp_ids, rec_ids, reasons = _map_current_sources(
            payload, mapping, imported_experiences, imported_recognitions, plan["import_id"]
        )
        if unresolved_experiences.intersection(payload.get("source_experience_ids", ())):
            reasons.append("unresolved_source_provenance")
        payload["source_experience_ids"] = exp_ids
        payload["source_recognition_ids"] = rec_ids
        payload["source_experience_revisions"] = {item: 1 for item in exp_ids}
        payload["source_recognition_revisions"] = {item: 1 for item in rec_ids}
        payload["parent_ids"] = [mapping["recognitions"].get(item, _placeholder(plan["import_id"], "parent", index))
                                 for index, item in enumerate(payload.get("parent_ids", ()))]
        if "successor_ids" in payload:
            payload["successor_ids"] = [mapping["recognitions"].get(item, _placeholder(plan["import_id"], "successor", index))
                                        for index, item in enumerate(payload["successor_ids"])]
        if payload.get("state") != "active":
            invalid.add(source_id)
        if reasons:
            invalid.add(source_id)
            issues.extend({"code": reason, "record_id": source_id, "location": "current"} for reason in reasons)
        recognition_payloads[source_id] = payload
    dependencies = {source_id: [item for item in row["payload"].get("source_recognition_ids", ())
                                 if item in recognition_payloads]
                    for source_id, row in imported_recognitions.items()}
    invalid.update(_cyclic_nodes(dependencies))
    changed = True
    while changed:
        changed = False
        for source_id, sources in dependencies.items():
            if source_id not in invalid and any(item in invalid for item in sources):
                invalid.add(source_id)
                changed = True
    for source_id, payload in recognition_payloads.items():
        if source_id in invalid:
            if payload.get("state") == "active":
                payload["state"] = "stale"
                payload["stale_reason"] = "migration source is unavailable, changed, or cyclic"
            issues.append({"code": "not_recallable_after_import", "record_id": source_id,
                           "location": "current", "reason": payload.get("state")})
    return experience_payloads, recognition_payloads


def _map_current_sources(payload, mapping, experiences, recognitions, import_id):
    reasons, source_experiences, source_recognitions = [], [], []
    for index, source_id in enumerate(payload.get("source_experience_ids", ())):
        source, expected = experiences.get(source_id), payload.get("source_experience_revisions", {}).get(source_id)
        if source is None:
            source_experiences.append(_placeholder(import_id, "experience", index)); reasons.append("missing_source")
        else:
            source_experiences.append(mapping["experiences"][source_id])
            if source["payload"].get("state") != "active": reasons.append("source_not_active")
            if source.get("revision") != expected: reasons.append("source_revision_mismatch")
    for index, source_id in enumerate(payload.get("source_recognition_ids", ())):
        source, expected = recognitions.get(source_id), payload.get("source_recognition_revisions", {}).get(source_id)
        if source is None:
            source_recognitions.append(_placeholder(import_id, "recognition", index)); reasons.append("missing_source")
        else:
            source_recognitions.append(mapping["recognitions"][source_id])
            if source["payload"].get("state") != "active": reasons.append("source_not_active")
            if source.get("revision") != expected: reasons.append("source_revision_mismatch")
    return source_experiences, source_recognitions, list(dict.fromkeys(reasons))


def _map_provenance(provenance, mapping, record_id, import_id, issues):
    result, refs = deepcopy(dict(provenance)), []
    mismatched = {(issue.get("source_type"), issue.get("source_id"), issue.get("source_revision"))
                  for issue in issues if issue.get("record_id") == record_id
                  and issue.get("location") == "provenance"
                  and issue.get("code") == "source_revision_mismatch"}
    for index, ref in enumerate(result.get("source_refs", ())):
        if not isinstance(ref, Mapping):
            continue
        updated = dict(ref)
        group = "experiences" if ref.get("type") == "experience" else "recognitions" if ref.get("type") == "recognition" else None
        if (group and ref.get("id") in mapping[group]
                and (ref.get("type"), ref.get("id"), ref.get("revision")) not in mismatched):
            updated["id"] = mapping[group][ref["id"]]
            if "revision" in updated: updated["revision"] = 1
        else:
            updated["id"] = unresolved_provenance_id(import_id, index)
            issue = {"code": "external_provenance_reference", "record_id": record_id,
                     "source_type": ref.get("type"), "source_id": ref.get("id"), "location": "provenance"}
            if "revision" in ref:
                issue["source_revision"] = ref["revision"]
            issues.append(issue)
        refs.append(updated)
    result["source_refs"] = refs
    return result


def _planned_reason(payload):
    reason = payload.get("stale_reason") or payload.get("revocation_reason")
    if reason:
        return reason
    state = payload.get("state")
    return None if state == "active" else f"source recognition remains {state} after import"


def _scope(scope: WorkScope):
    return {"user_id": scope.user_id, "project_id": scope.project_id}


def _placeholder(import_id: str, kind: str, index: int) -> str:
    return f"migration-{import_id}-external-{kind}-{index}"


def _cyclic_nodes(dependencies: Mapping[str, list[str]]) -> set[str]:
    result, visiting, visited = set(), set(), set()
    def visit(node: str, path: list[str]) -> None:
        if node in visiting:
            result.update(path[path.index(node):]); return
        if node in visited: return
        visiting.add(node)
        for child in dependencies.get(node, ()): visit(child, [*path, child])
        visiting.remove(node); visited.add(node)
    for node in dependencies: visit(node, [node])
    return result
