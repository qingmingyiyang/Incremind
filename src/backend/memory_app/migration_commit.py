"""Atomic selected-recognition migration commit boundary.

The bundle is retained once, in the import receipt, so source history can be
inspected without copying historical bodies into the live record collections.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope

from .migration_bundle import inspect_bundle, validate_bundle
from .migration_plan import ImportPreviewService, _placeholder, project_target_payloads


_RECOGNITIONS = "recognitions"
_EXPERIENCES = "recognition_experiences"
_VERSIONS = "recognition_versions"
_RELATIONS = "recognition_relations"
_PROPOSALS = "recognition_relation_proposals"
_IMPORTS = "recognition_migration_imports"
_HISTORY = "recognition_migration_versions"


class ImportService:
    """Commit an exact reviewed plan in one SQLite unit of work."""

    def __init__(self, records) -> None:
        self.records = records
        self._plans = ImportPreviewService(records)

    def commit(self, *, scope: WorkScope, bundle, import_id: str, strategy: str, expected_plan) -> dict[str, object]:
        value = validate_bundle(bundle)
        # Validate import ID and strategy before looking up a receipt.  The
        # shared planner owns those validators and has no write side effects.
        with self.records.begin() as uow:
            existing = uow.read(_IMPORTS, import_id) if isinstance(import_id, str) else None
            if existing is not None:
                replay = self._replay(existing, scope=scope, bundle=value, strategy=strategy)
                uow.rollback()
                return replay

            plan = self._plans.preview_in_uow(
                uow, scope=scope, bundle=value, import_id=import_id, strategy=strategy
            )
            if expected_plan != plan:
                raise RecognitionConflict("migration preview changed; preview again before importing")
            if plan["conflicts"]:
                raise RecognitionConflict("migration target slots are no longer available")

            # Rebuild the same base issue list used by preview, then let the
            # shared projection append its deterministic import-state issues.
            issue_rows: list[dict[str, object]] = inspect_bundle(value)
            target_payloads = self._target_payloads(value, plan, scope, issue_rows)
            self._write_current(uow, value, plan, target_payloads)
            self._write_history(uow, value, plan, scope)
            self._write_relations(uow, value, plan, scope, issue_rows)
            receipt = self._receipt(scope, value, plan, target_payloads, issue_rows)
            import_payload = {
                "id": plan["import_id"], "scope": _scope(scope), "project_id": scope.project_id,
                "bundle": value, "strategy": strategy, "mapping": deepcopy(plan["mapping"]),
                "recognition_ids": receipt["recognition_ids"], "experience_ids": receipt["experience_ids"],
                "receipt": receipt,
            }
            uow.put(_IMPORTS, plan["import_id"], import_payload, expected_revision=0)
            uow.commit()
        return receipt

    def _replay(self, record, *, scope: WorkScope, bundle: Mapping[str, object], strategy: str) -> dict[str, object]:
        payload = record.payload
        if _as_scope(payload) != scope:
            raise RecognitionConflict("migration import id belongs to another work scope")
        if payload.get("strategy") != strategy or payload.get("bundle") != bundle:
            raise RecognitionConflict("migration import id cannot be reused for another request")
        receipt = payload.get("receipt")
        if not isinstance(receipt, Mapping):
            raise RecognitionError("stored migration receipt is invalid")
        return deepcopy(dict(receipt))

    def _target_payloads(self, value, plan, scope, issues):
        return project_target_payloads(value, plan, scope, issues)

    def _write_current(self, uow, value, plan, payloads):
        experience_payloads, recognition_payloads = payloads
        mapping = plan["mapping"]
        for row in value["experiences"]:
            uow.put(_EXPERIENCES, mapping["experiences"][row["id"]], experience_payloads[row["id"]], expected_revision=0)
        for row in value["recognitions"]:
            target = mapping["recognitions"][row["id"]]
            payload = recognition_payloads[row["id"]]
            record = uow.put(_RECOGNITIONS, target, payload, expected_revision=0)
            baseline_id = f"{target}~v1"
            uow.put(_VERSIONS, baseline_id, {"id": baseline_id, "recognition_id": target,
                    "recognition_revision": record.revision, "version": 1, "action": "import",
                    "snapshot": deepcopy(payload), "recorded_at": payload["updated_at"]}, expected_revision=0)

    def _write_history(self, uow, value, plan, scope):
        mapping = plan["mapping"]
        groups = (("versions", value.get("versions", ())), ("prior_versions", value.get("prior_versions", ())))
        for source_group, rows in groups:
            for source_index, row in enumerate(rows):
                source = row.get("payload", {}) if source_group == "versions" and isinstance(row, Mapping) else (
                    row.get("source_record", {}).get("payload", {}) if isinstance(row, Mapping) else {}
                )
                recognition_id = source.get("recognition_id")
                target_recognition = mapping["recognitions"].get(recognition_id)
                if target_recognition is None:
                    continue
                target_id = (mapping["versions"][row["id"]] if source_group == "versions"
                             else mapping["prior_versions"][str(source_index)])
                pointer = {"id": target_id, "scope": _scope(scope), "project_id": scope.project_id,
                           "recognition_id": target_recognition, "import_id": plan["import_id"],
                           "source_group": source_group, "source_index": source_index}
                uow.put(_HISTORY, target_id, pointer, expected_revision=0)

    def _write_relations(self, uow, value, plan, scope, issues):
        mapping = plan["mapping"]
        for row in value.get("relations", ()):
            payload = deepcopy(row["payload"])
            payload.update({"id": mapping["relations"][row["id"]], "scope": _scope(scope), "project_id": scope.project_id,
                            "from_id": mapping["recognitions"][payload["from_id"]],
                            "to_id": mapping["recognitions"][payload["to_id"]]})
            uow.put(_RELATIONS, payload["id"], payload, expected_revision=0)
        source_recognitions = {row["id"]: row for row in value["recognitions"]}
        for row in value.get("approved_relation_proposals", ()):
            source = row["payload"]
            payload = deepcopy(source)
            payload.update({"id": mapping["approved_relation_proposals"][row["id"]], "scope": _scope(scope), "project_id": scope.project_id,
                            "from_id": mapping["recognitions"][source["from_id"]],
                            "to_id": mapping["recognitions"][source["to_id"]]})
            from_current = source_recognitions[source["from_id"]]["revision"]
            to_current = source_recognitions[source["to_id"]]["revision"]
            if source["from_revision"] == from_current and source["to_revision"] == to_current:
                payload.update({"from_revision": 1, "to_revision": 1})
            else:
                # Do not leave an old source revision alongside a fresh
                # target ID: source revision one can accidentally equal the
                # new target's initial CAS revision.  A non-existent stable
                # endpoint keeps the approved historical proposal expired.
                if source["from_revision"] != from_current:
                    payload["from_id"] = _placeholder(plan["import_id"], "relation-from", 0)
                if source["to_revision"] != to_current:
                    payload["to_id"] = _placeholder(plan["import_id"], "relation-to", 0)
                issues.append({"code": "approved_relation_endpoint_revision_mismatch",
                               "record_id": row["id"], "location": "relation"})
            # Otherwise retain non-matching source revisions: the proposal is
            # historical evidence, not a newly-valid target edge.
            uow.put(_PROPOSALS, payload["id"], payload, expected_revision=0)

    def _receipt(self, scope, value, plan, payloads, issues):
        _, recognitions = payloads
        return {"schema": "recognition-migration-receipt-v1", "import_id": plan["import_id"],
                "bundle_id": value["bundle_id"], "strategy": plan["strategy"],
                "source_scope": deepcopy(value["source_scope"]), "target_scope": _scope(scope),
                "mapping": deepcopy(plan["mapping"]), "recognition_ids": [plan["mapping"]["recognitions"][row["id"]] for row in value["recognitions"]],
                "experience_ids": [plan["mapping"]["experiences"][row["id"]] for row in value["experiences"]],
                "not_recallable_ids": [plan["mapping"]["recognitions"][source_id] for source_id, payload in recognitions.items() if payload.get("state") != "active"],
                "issues": issues, "planned_state": "committed", "commit_implemented": True}


def _scope(scope: WorkScope):
    return {"user_id": scope.user_id, "project_id": scope.project_id}


def _as_scope(payload):
    value = payload.get("scope") if isinstance(payload, Mapping) else None
    if not isinstance(value, Mapping):
        raise RecognitionError("stored migration scope is invalid")
    scope = WorkScope(value.get("user_id"), value.get("project_id"))
    if payload.get("project_id") != scope.project_id:
        raise RecognitionError("stored migration project scope is invalid")
    return scope
