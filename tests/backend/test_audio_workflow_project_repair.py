from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from core.storage_provider import JsonObjectStore


_TOOL = Path(__file__).resolve().parents[2] / "tools" / "migrations" / "repair_audio_workflow_projects.py"
_SPEC = importlib.util.spec_from_file_location("repair_audio_workflow_projects", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
repair_tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(repair_tool)


class _Records:
    def __init__(self, source_id: str, project_id: str, source_revision: int) -> None:
        self.payload = {"id": f"review-{source_id}", "source_id": source_id,
                        "project_id": project_id, "job_id": f"job-capture-{source_id}",
                        "source_revision": source_revision}
        self.revision = 1

    def read(self, collection: str, object_id: str):
        assert collection == "workspace_review_intents"
        return SimpleNamespace(payload=copy.deepcopy(self.payload), revision=self.revision) \
            if object_id == self.payload["id"] else None


class _Jobs:
    def __init__(self, source_id: str, project_id: str) -> None:
        self.job = {"id": f"job-capture-{source_id}", "source_id": source_id,
                    "job_type": "capture", "project_id": project_id}
        self.sqlite = SimpleNamespace(read=lambda job_id: SimpleNamespace(revision=1)
                                      if job_id == self.job["id"] else None)

    def get(self, job_id: str):
        return copy.deepcopy(self.job) if job_id == self.job["id"] else None


def _fixture(tmp_path: Path):
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    source_id, target = "audio-source", "project-a"
    workflow_id = f"audio-auto-workflow-{source_id}"
    workflow = {"workflow_id": workflow_id, "source_id": source_id,
                "project_id": "default", "projection_source": "effect_tree",
                "status": "blocked", "steps": [{"name": "transcribe_audio", "status": "blocked"}]}
    embedded = dict(workflow) | {"workflow_ref": f"crp://default/audio-auto-workflows/{workflow_id}.json"}
    store.write("sources", source_id, {"id": source_id, "project_id": target,
                                       "metadata": {"audio_auto_workflow": embedded}},
                expected_revision=None)
    store.write("audio_auto_workflows", workflow_id, workflow, expected_revision=None)
    return store, _Records(source_id, target, 1), _Jobs(source_id, target), workflow_id, source_id


def test_preview_is_read_only_and_reports_no_content(tmp_path: Path) -> None:
    store, records, jobs, workflow_id, source_id = _fixture(tmp_path)
    before = (store.revision("sources", source_id),
              store.revision("audio_auto_workflows", workflow_id))
    report = repair_tool.repair(store, records, jobs)
    assert report["counts"]["ready"] == 1
    assert report["items"][0]["reason"] == "confirmed_projection_project"
    assert "steps" not in str(report)
    assert before == (store.revision("sources", source_id),
                      store.revision("audio_auto_workflows", workflow_id))


def test_apply_changes_only_two_project_fields_then_is_idempotent(tmp_path: Path) -> None:
    store, records, jobs, workflow_id, source_id = _fixture(tmp_path)
    prior_source = dict(store.read("sources", source_id))
    prior_workflow = dict(store.read("audio_auto_workflows", workflow_id))
    report = repair_tool.repair(store, records, jobs, apply=True)
    assert report["counts"]["repaired"] == 1
    source = store.read("sources", source_id)
    workflow = store.read("audio_auto_workflows", workflow_id)
    assert workflow == prior_workflow | {"project_id": "project-a"}
    assert source == prior_source | {"metadata": {
        "audio_auto_workflow": prior_source["metadata"]["audio_auto_workflow"] | {"project_id": "project-a"}}}
    assert (store.revision("sources", source_id),
            store.revision("audio_auto_workflows", workflow_id)) == (2, 2)
    again = repair_tool.repair(store, records, jobs, apply=True)
    assert again["counts"]["already_repaired"] == 1
    assert (store.revision("sources", source_id),
            store.revision("audio_auto_workflows", workflow_id)) == (2, 2)


def test_conflicted_authorities_and_revision_drift_skip(tmp_path: Path) -> None:
    store, records, jobs, workflow_id, source_id = _fixture(tmp_path)
    jobs.job["project_id"] = "other"
    assert repair_tool.repair(store, records, jobs, apply=True)["items"][0]["reason"] == "review_job_project_conflict"
    jobs.job["project_id"] = "project-a"
    records.payload["project_id"] = "other"
    assert repair_tool.repair(store, records, jobs, apply=True)["items"][0]["reason"] == "review_intent_conflict"
    records.payload["project_id"] = "project-a"

    def drift(collection: str, _object_id: str) -> None:
        if collection == "sources":
            source = store.read("sources", source_id)
            store.write("sources", source_id, dict(source) | {"title": "changed"}, expected_revision=1)

    result = repair_tool.repair(store, records, jobs, apply=True, before_write=drift)
    assert result["items"][0]["reason"] == "evidence_or_revision_changed"
    assert store.read("sources", source_id)["metadata"]["audio_auto_workflow"]["project_id"] == "default"
    assert store.read("audio_auto_workflows", workflow_id)["project_id"] == "project-a"


def test_partial_write_can_resume_without_extra_revision(tmp_path: Path) -> None:
    store, records, jobs, workflow_id, source_id = _fixture(tmp_path)
    workflow = store.read("audio_auto_workflows", workflow_id)
    store.write("audio_auto_workflows", workflow_id,
                dict(workflow) | {"project_id": "project-a"}, expected_revision=1)
    report = repair_tool.repair(store, records, jobs, apply=True)
    assert report["counts"]["repaired"] == 1
    assert store.revision("audio_auto_workflows", workflow_id) == 2
    assert store.revision("sources", source_id) == 2


def test_orphan_embedded_projection_is_in_inventory_without_writes(tmp_path: Path) -> None:
    store, records, jobs, workflow_id, source_id = _fixture(tmp_path)
    store.delete("audio_auto_workflows", workflow_id)
    report = repair_tool.repair(store, records, jobs, apply=True)
    assert report["counts"]["skip"] == 1
    assert report["items"][0]["reason"] == "standalone_projection_missing"
    assert report["items"][0]["source_id"] == source_id
    assert store.revision("sources", source_id) == 1


def test_immutable_confirmed_source_is_skipped_before_workflow_write(tmp_path: Path) -> None:
    store, records, jobs, workflow_id, source_id = _fixture(tmp_path)
    source = store.read("sources", source_id)
    store.write("sources", source_id, dict(source) | {"identity_method": "workspace_confirmation"}, expected_revision=1)
    report = repair_tool.repair(store, records, jobs, apply=True)
    assert report["items"][0]["reason"] == "source_immutable_authority"
    assert store.revision("audio_auto_workflows", workflow_id) == 1


def test_job_without_confirmed_revision_is_skipped(tmp_path: Path) -> None:
    store, records, jobs, workflow_id, _source_id = _fixture(tmp_path)
    jobs.sqlite = SimpleNamespace(read=lambda _job_id: None)
    report = repair_tool.repair(store, records, jobs, apply=True)
    assert report["items"][0]["reason"] == "review_job_revision_unavailable"
    assert store.revision("audio_auto_workflows", workflow_id) == 1
