"""Inventory and narrowly repair historical audio workflow project projections.

Only projection project fields are mutable here. This module never runs ASR or
touches an original asset. The JSON store's CAS is not cross-process atomic;
operators must stop all writers before applying a reviewed inventory.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.storage_provider import SQLiteStructuredRecordStore


def _revision(store: object, collection: str, object_id: str) -> int:
    return store.revision(collection, object_id)


def _job_revision(jobs: object, job_id: str) -> int | None:
    sqlite = getattr(jobs, "sqlite", None)
    if sqlite is not None:
        record = sqlite.read(job_id)
        if record is not None:
            return record.revision
    legacy = getattr(jobs, "legacy", None)
    object_store = getattr(legacy, "object_store", None)
    if object_store is not None:
        revision = object_store.revision("jobs", job_id)
        return revision if revision > 0 else None
    return None


def _job_binds_source(job: Mapping[str, object], source_id: str) -> bool:
    if job.get("source_id") == source_id:
        return True
    outputs = job.get("outputs")
    if not isinstance(outputs, (list, tuple)):
        return False
    return any(
        isinstance(output, Mapping)
        and output.get("kind") == "link_sources"
        and isinstance(output.get("source_ids"), list)
        and source_id in output["source_ids"]
        for output in outputs
    )


def inspect_one(store: object, records: object, jobs: object, workflow_id: str) -> dict[str, object]:
    """Return identifiers and revision metadata only, never source material."""
    row: dict[str, object] = {
        "workflow_id": workflow_id, "source_id": None, "source_project_id": None,
        "workflow_project_id": None, "embedded_project_id": None,
        "workflow_revision": None, "source_revision": None,
        "intent_id": None, "intent_revision": None, "job_id": None,
        "job_revision": None,
        "status": "skip", "reason": "workflow_missing",
    }
    workflow = store.read("audio_auto_workflows", workflow_id)
    if not isinstance(workflow, Mapping):
        return row
    source_id = workflow.get("source_id")
    row.update({"source_id": source_id, "workflow_project_id": workflow.get("project_id"),
                "workflow_revision": _revision(store, "audio_auto_workflows", workflow_id)})
    if not isinstance(source_id, str) or not source_id or workflow.get("workflow_id") != workflow_id:
        row["reason"] = "workflow_identity_mismatch"
        return row
    if workflow_id != f"audio-auto-workflow-{source_id}":
        row["reason"] = "workflow_identity_mismatch"
        return row
    if workflow.get("projection_source") != "effect_tree":
        row["reason"] = "projection_origin_unconfirmed"
        return row
    source = store.read("sources", source_id)
    if not isinstance(source, Mapping) or source.get("id") != source_id:
        row["reason"] = "source_missing_or_deleted"
        return row
    target = source.get("project_id")
    row.update({"source_project_id": target,
                "source_revision": _revision(store, "sources", source_id)})
    if source.get("identity_method") == "workspace_confirmation":
        row["reason"] = "source_immutable_authority"
        return row
    if not isinstance(target, str) or not target or target == "default":
        row["reason"] = "source_project_not_explicit_nondefault"
        return row
    metadata = source.get("metadata")
    nested = metadata.get("audio_auto_workflow") if isinstance(metadata, Mapping) else None
    if not isinstance(nested, Mapping):
        row["reason"] = "embedded_projection_missing"
        return row
    row["embedded_project_id"] = nested.get("project_id")
    workflow_ref = nested.get("workflow_ref")
    if (nested.get("workflow_id") != workflow_id or nested.get("source_id") != source_id
            or nested.get("projection_source") != "effect_tree"
            or not isinstance(workflow_ref, str)
            or not workflow_ref.endswith(f"/audio-auto-workflows/{workflow_id}.json")
            or any(nested.get(key) != value for key, value in workflow.items()
                   if key != "project_id")):
        row["reason"] = "embedded_projection_mismatch"
        return row
    if any(value not in ("default", target) for value in
           (workflow.get("project_id"), nested.get("project_id"))):
        row["reason"] = "projection_project_conflict"
        return row
    intent_id = f"review-{source_id}"
    row["intent_id"] = intent_id
    intent_row = records.read("workspace_review_intents", intent_id)
    if intent_row is None:
        row["reason"] = "review_intent_missing"
        return row
    intent = intent_row.payload
    row["intent_revision"] = intent_row.revision
    if (intent.get("id") != intent_id or intent.get("source_id") != source_id
            or intent.get("project_id") != target
            or not isinstance(intent.get("source_revision"), int)
            or intent["source_revision"] < 1
            or intent["source_revision"] > row["source_revision"]):
        row["reason"] = "review_intent_conflict"
        return row
    job_id = intent.get("job_id")
    row["job_id"] = job_id
    if not isinstance(job_id, str) or not job_id:
        row["reason"] = "review_job_missing"
        return row
    job = jobs.get(job_id)
    row["job_revision"] = _job_revision(jobs, job_id)
    if (not isinstance(job, Mapping) or job.get("id") != job_id
            or not _job_binds_source(job, source_id)):
        row["reason"] = "review_job_binding_conflict"
        return row
    if row["job_revision"] is None:
        row["reason"] = "review_job_revision_unavailable"
        return row
    if job.get("project_id") not in (None, target):
        row["reason"] = "review_job_project_conflict"
        return row
    row["status"] = "ready" if (workflow.get("project_id") != target
                                or nested.get("project_id") != target) else "already_repaired"
    row["reason"] = "confirmed_projection_project" if row["status"] == "ready" else "already_target_project"
    return row


def repair(store: object, records: object, jobs: object, *, apply: bool = False,
           before_write: Any = None) -> dict[str, object]:
    """Enumerate all workflow projections and optionally repair confirmed rows."""
    workflow_ids = sorted({str(value.get("workflow_id")) for value in
                           store.list("audio_auto_workflows")
                           if isinstance(value.get("workflow_id"), str) and value.get("workflow_id")})
    items: list[dict[str, object]] = []
    for workflow_id in workflow_ids:
        initial = inspect_one(store, records, jobs, workflow_id)
        if not apply or initial["status"] != "ready":
            items.append(initial)
            continue
        frozen_job = jobs.get(str(initial["job_id"]))
        # Every write rechecks the independent Source, Job and intent authorities.
        for collection in ("audio_auto_workflows", "sources"):
            source_id = str(initial["source_id"])
            if before_write is not None:
                before_write(collection, workflow_id if collection == "audio_auto_workflows" else source_id)
            current = inspect_one(store, records, jobs, workflow_id)
            if (current["status"] not in {"ready", "already_repaired"}
                    or current["intent_revision"] != initial["intent_revision"]
                    or current["job_id"] != initial["job_id"]
                    or current["job_revision"] != initial["job_revision"]
                    or jobs.get(str(initial["job_id"])) != frozen_job
                    or current["source_project_id"] != initial["source_project_id"]
                    or (collection == "audio_auto_workflows" and
                        current["workflow_revision"] != initial["workflow_revision"])
                    or (collection == "sources" and
                        current["source_revision"] != initial["source_revision"])):
                initial.update(status="skip", reason="evidence_or_revision_changed")
                break
            target = str(initial["source_project_id"])
            if collection == "audio_auto_workflows":
                if current["workflow_project_id"] == target:
                    continue
                payload = store.read(collection, workflow_id)
                object_id = workflow_id
                expected = int(current["workflow_revision"])
                update = dict(payload) | {"project_id": target}
            else:
                if current["embedded_project_id"] == target:
                    continue
                payload = store.read(collection, source_id)
                object_id = source_id
                expected = int(current["source_revision"])
                metadata = dict(payload["metadata"])
                metadata["audio_auto_workflow"] = dict(metadata["audio_auto_workflow"]) | {"project_id": target}
                update = dict(payload) | {"metadata": metadata}
            try:
                store.write(collection, object_id, update, expected_revision=expected)
            except Exception as error:
                if not store.is_revision_conflict(error):
                    raise
                initial.update(status="skip", reason="revision_conflict")
                break
            if collection == "audio_auto_workflows":
                initial["workflow_revision"] = expected + 1
            else:
                initial["source_revision"] = expected + 1
        else:
            final = inspect_one(store, records, jobs, workflow_id)
            initial.update(status="repaired" if final["status"] == "already_repaired" else "skip",
                           reason="projection_projects_updated" if final["status"] == "already_repaired"
                           else "postwrite_evidence_conflict",
                           workflow_revision=final["workflow_revision"],
                           source_revision=final["source_revision"])
        items.append(initial)
    for source in store.list("sources"):
        if not isinstance(source, Mapping) or not isinstance(source.get("id"), str):
            continue
        metadata = source.get("metadata")
        nested = metadata.get("audio_auto_workflow") if isinstance(metadata, Mapping) else None
        if not isinstance(nested, Mapping):
            continue
        nested_id = nested.get("workflow_id")
        if isinstance(nested_id, str) and nested_id in workflow_ids:
            continue
        items.append({
            "workflow_id": nested_id if isinstance(nested_id, str) else None,
            "source_id": source["id"], "source_project_id": source.get("project_id"),
            "workflow_project_id": None, "embedded_project_id": nested.get("project_id"),
            "workflow_revision": None,
            "source_revision": _revision(store, "sources", str(source["id"])),
            "intent_id": f"review-{source['id']}", "intent_revision": None,
            "job_id": None, "job_revision": None,
            "status": "skip", "reason": "standalone_projection_missing",
        })
    return {"mode": "apply" if apply else "preview", "items": items,
            "counts": {status: sum(item["status"] == status for item in items)
                       for status in ("ready", "already_repaired", "repaired", "skip")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-offline", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.confirm_offline:
        parser.error("--apply requires --confirm-offline after stopping every writer")
    if args.confirm_offline and not args.apply:
        parser.error("--confirm-offline is only valid with --apply")
    root = args.runtime_root.resolve(strict=True)
    store, _settings = build_rebuild_object_store(root)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
    jobs = build_rebuild_job_repository(root, store)
    print(json.dumps(repair(store, records, jobs, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
