from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.expert_turn_binding_runtime import (
    ExpertAwarePlanner,
    ExpertBindingSnapshotError,
    build_expert_turn_binding_runtime,
)
from backend.api.ai_runtime import build_ai_runtime
from core.ai_kernel import (
    CapabilityDefinition,
    V1TurnContextManifestResolver,
    V1TurnPolicyCapabilityManifestResolver,
)
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    default_video_research_expert_profile,
)


ROOT = Path(__file__).resolve().parents[4]
MODEL_ROUTE_REVISION = "b" * 64
TOOLS = ("analyze_source", "memory.recall", "document.draft.propose")


def test_runtime_exposes_the_same_read_authorities_used_for_selection(
    tmp_path: Path,
) -> None:
    runtime = build_expert_turn_binding_runtime(tmp_path)

    assert runtime.catalog is runtime._catalog
    assert runtime.bindings is runtime._bindings


@pytest.mark.parametrize(
    ("path", "expected_selection"),
    (("explicit", "explicit"), ("default", "project_default")),
)
def test_adapter_freezes_real_project_binding_and_authority_identities(
    tmp_path: Path, path: str, expected_selection: str,
) -> None:
    _bind(tmp_path, path)
    request = _request(path)
    capabilities = _capabilities()
    manifest = _manifest(request, capabilities)
    context = V1TurnContextManifestResolver().resolve(
        request, f"crp://session/{request['turn_id']}/capability-manifest/frozen", manifest,
    )
    adapter = build_expert_turn_binding_runtime(tmp_path)

    selection = adapter.select(request, manifest, capabilities)
    snapshot = adapter.freeze(request, selection, manifest, context, capabilities)

    assert selection["selection_mode"] == expected_selection
    assert selection["selected"]["expert_id"] == "video-research-expert"
    assert snapshot is not None
    assert snapshot["project_id"] == "project-alpha"
    assert snapshot["context_manifest_revision"] == context.manifest_id
    assert snapshot["boundary_revision"] == context.boundary_profile_revision
    assert snapshot["model_route_revision"] == MODEL_ROUTE_REVISION
    assert snapshot["tool_capability_revisions"] == {
        "analyze_source": 3, "memory.recall": 2, "document.draft.propose": 4,
    }
    adapter.verify_replay(request, selection, snapshot, manifest, context, capabilities)


def test_adapter_rejects_selected_expert_when_one_frozen_tool_is_not_in_turn_manifest(tmp_path: Path) -> None:
    _bind(tmp_path, "default")
    request = _request("default")
    capabilities = tuple(item for item in _capabilities() if item.capability_id != "document.draft.propose")
    manifest = _manifest(request, capabilities)
    context = V1TurnContextManifestResolver().resolve(
        request, f"crp://session/{request['turn_id']}/capability-manifest/frozen", manifest,
    )
    adapter = build_expert_turn_binding_runtime(tmp_path)
    selection = adapter.select(request, manifest, capabilities)

    with pytest.raises(ExpertBindingSnapshotError, match="absent from the Turn capability manifest"):
        adapter.freeze(request, selection, manifest, context, capabilities)


def test_dispatch_assignment_resolves_explicit_active_project_bound_expert(tmp_path: Path) -> None:
    _bind(tmp_path, "explicit")
    adapter = build_expert_turn_binding_runtime(tmp_path)
    assert adapter.resolve_dispatch_assignment("project-alpha", {
        "expert_id": "video-research-expert",
        "task_intents": ["media_analysis"], "budget": "research-2k",
    }, "expert") == (
        "crp://expert-dispatch/project-alpha/experts/video-research-expert/expert-revisions/1/binding-revisions/1",
        1,
    )


def test_dispatch_assignment_rejects_unknown_unbound_and_drifting_expert(tmp_path: Path) -> None:
    adapter = build_expert_turn_binding_runtime(tmp_path)
    proposal = {"expert_id": "video-research-expert", "task_intents": ["media_analysis"], "budget": "research-2k"}
    with pytest.raises(ExpertBindingSnapshotError, match="unavailable"):
        adapter.resolve_dispatch_assignment("project-alpha", proposal, "expert")
    _bind(tmp_path, "explicit")
    with pytest.raises(ExpertBindingSnapshotError, match="kind"):
        adapter.resolve_dispatch_assignment("project-alpha", proposal, "skill")
    with pytest.raises(ExpertBindingSnapshotError, match="shape"):
        adapter.resolve_dispatch_assignment("project-alpha", {**proposal, "prompt": "forbidden"}, "expert")
    catalog = ExpertCatalog(tmp_path)
    catalog.set_status("video-research-expert", "disabled", reason="test", expected_expert_revision=1, expected_registry_revision=1)
    with pytest.raises(ExpertBindingSnapshotError, match="unavailable"):
        adapter.resolve_dispatch_assignment("project-alpha", proposal, "expert")


def test_dispatch_assignment_rejects_project_binding_expert_revision_drift(tmp_path: Path) -> None:
    _bind(tmp_path, "explicit")
    catalog = ExpertCatalog(tmp_path)
    catalog.upgrade(
        "video-research-expert", default_video_research_expert_profile(),
        expected_expert_revision=1, expected_registry_revision=1,
    )
    with pytest.raises(ExpertBindingSnapshotError, match="unavailable"):
        build_expert_turn_binding_runtime(tmp_path).resolve_dispatch_assignment(
            "project-alpha",
            {"expert_id": "video-research-expert", "task_intents": ["media_analysis"], "budget": "research-2k"},
            "expert",
        )


def test_video_expert_planner_uses_frozen_recipe_and_analyze_source() -> None:
    class _Delegate:
        def plan(self, *args, **kwargs):
            raise AssertionError("selected expert must not delegate to generic planner")

    class _Payloads:
        def get_immutable_payload(self, turn_id, kind):
            assert turn_id == "turn-expert"
            if kind == "expert-job-terminal-snapshot-v1":
                return None
            assert kind == "expert-binding-snapshot-v1"
            return "crp://session/turn-expert/expert-binding-snapshot-v1/frozen", {
                "expert_id": "video-research-expert", "expert_revision": 1,
                "role": "视频内容研究与证据整理", "method": "字幕优先",
                "output_contract": "证据可追溯",
            }

    planner = ExpertAwarePlanner(_Delegate(), job_resume_supported=True)
    decision = planner.plan(
        {
            "turn_id": "turn-expert",
            "input": {"text": "https://example.test/video", "refs": []},
        },
        [],
        _capabilities(),
        _Payloads(),
    )

    assert decision["capability_id"] == "analyze_source"
    assert decision["arguments"]["input"] == {
        "kind": "text", "text": "https://example.test/video", "source_ref": None,
    }
    assert decision["arguments"]["intent"] == "summarize"


def test_completed_terminal_requires_project_memory_recall_before_completion() -> None:
    snapshot = {
        "snapshot_id": "expert-snapshot-1", "expert_id": "video-research-expert",
        "expert_revision": 1, "role": "role", "method": "method",
        "output_contract": "contract",
    }
    terminal = {
        "turn_id": "turn-expert", "status": "completed",
        "receipt_ref": "crp://default/receipts/media-1",
        "source_manifest_ref": "crp://default/source-manifests/source-1",
        "source_manifest_revision": "manifest-1",
    }

    class _Payloads:
        def get_immutable_payload(self, _turn_id, kind):
            if kind == "expert-binding-snapshot-v1":
                return "crp://session/turn-expert/expert-binding-snapshot-v1/frozen", snapshot
            return "crp://session/turn-expert/expert-job-terminal-snapshot-v1/frozen", terminal

    planner = ExpertAwarePlanner(_DelegateNever(), job_resume_supported=True)
    first = planner.plan(
        {"turn_id": "turn-expert", "input": {"text": "研究这个视频", "refs": []}},
        [], _capabilities(), _Payloads(),
    )
    assert first == {
        "type": "tool", "capability_id": "memory.recall",
        "arguments": {
            "query": "研究这个视频",
            "allowed_trust_statuses": ["verified", "published"], "limit": 12,
        },
    }
    completed = planner.plan(
        {"turn_id": "turn-expert", "input": {"text": "研究这个视频", "refs": []}},
        [{"type": "tool.completed", "data": {
            "capability_id": "memory.recall",
            "payload_ref": "crp://session/turn-expert/tool-result/recall",
            "evidence_refs": ["crp://default/memory/atom-1"],
        }}], _capabilities(), _Payloads(),
    )
    assert completed["type"] == "complete"
    assert "crp://default/memory/atom-1" in completed["evidence_refs"]


@pytest.mark.parametrize(
    ("job_status", "expected_type"),
    (("pending", "wait_job"), ("running", "wait_job"), ("completed", "complete")),
)
def test_video_expert_planner_tracks_media_job_terminal_truth(
    job_status: str, expected_type: str,
) -> None:
    snapshot = {
        "snapshot_id": "expert-snapshot-1",
        "expert_id": "video-research-expert", "expert_revision": 1,
        "role": "role", "method": "method", "output_contract": "contract",
    }

    class _Payloads:
        def get_immutable_payload(self, *_args):
            return "crp://default/expert/snapshot", snapshot

        def get(self, ref):
            assert ref == "crp://default/tool/outcome"
            return {"result": {
                "status": "admitted", "job_id": "job-1",
                "job_ref": "crp://default/jobs/job-1", "job_revision": 1,
            }}

    job = {
        "status": job_status, "revision": 2,
        "published_outputs": (
            [] if job_status != "completed"
            else [{"kind": "document", "uri": "crp://default/documents/video-1"}]
        ),
    }
    planner = ExpertAwarePlanner(
        _DelegateNever(), job_reader=lambda _job_id: job, job_resume_supported=True,
    )
    decision = planner.plan(
        {"turn_id": "turn-expert", "input": {"text": "source", "refs": []}},
        [{"type": "tool.completed", "data": {
            "capability_id": "analyze_source", "payload_ref": "crp://default/tool/outcome",
            "evidence_refs": ["crp://default/jobs/job-1"],
        }}], _capabilities(), _Payloads(),
    )
    assert decision["type"] == expected_type
    if expected_type == "wait_job":
        assert decision["job_id"] == "job-1"
        assert decision["job_revision"] == 2
    else:
        assert decision["evidence_refs"] == ["crp://default/documents/video-1"]


def test_video_expert_planner_accepts_direct_persisted_admission_result() -> None:
    snapshot = {
        "snapshot_id": "expert-snapshot-1", "expert_id": "video-research-expert",
        "expert_revision": 1, "role": "role", "method": "method", "output_contract": "contract",
    }

    class _Payloads:
        def get_immutable_payload(self, *_args):
            return "crp://default/expert/snapshot", snapshot

        def get(self, _ref):
            return {
                "status": "admitted", "job_id": "job-1",
                "canonical_job_ref": "crp://jobs/source-1", "job_revision": 3,
            }

    decision = ExpertAwarePlanner(
        _DelegateNever(), job_reader=lambda _job_id: {"status": "running", "revision": 4},
        job_resume_supported=True,
    ).plan(
        {"turn_id": "turn-expert"},
        [{"type": "tool.completed", "data": {
            "capability_id": "analyze_source", "payload_ref": "crp://default/tool/outcome",
        }}], _capabilities(), _Payloads(),
    )

    assert decision["type"] == "wait_job"
    assert decision["job_ref"] == "crp://jobs/source-1"


def test_video_expert_planner_rejects_malformed_admission_result() -> None:
    class _Payloads:
        def get_immutable_payload(self, *_args):
            return "crp://default/expert/snapshot", {
                "snapshot_id": "expert-snapshot-1", "expert_id": "video-research-expert",
                "expert_revision": 1, "role": "role", "method": "method", "output_contract": "contract",
            }

        def get(self, _ref):
            return {"status": "admitted", "canonical_job_ref": "crp://jobs/source-1"}

    with pytest.raises(ExpertBindingSnapshotError, match="missing Job identity"):
        ExpertAwarePlanner(_DelegateNever(), job_reader=lambda _job_id: None, job_resume_supported=True).plan(
            {"turn_id": "turn-expert"},
            [{"type": "tool.completed", "data": {
                "capability_id": "analyze_source", "payload_ref": "crp://default/tool/outcome",
            }}], _capabilities(), _Payloads(),
        )


@pytest.mark.parametrize("job_status", ("failed", "cancelled"))
def test_video_expert_planner_rejects_unsuccessful_media_job(job_status: str) -> None:
    class _Payloads:
        def get_immutable_payload(self, *_args):
            return "crp://default/expert/snapshot", {
                "snapshot_id": "expert-snapshot-1",
                "expert_id": "video-research-expert", "expert_revision": 1,
                "role": "role", "method": "method", "output_contract": "contract",
            }

        def get(self, _ref):
            return {"result": {
                "status": "admitted", "job_id": "job-1",
                "job_ref": "crp://default/jobs/job-1", "job_revision": 1,
            }}

    planner = ExpertAwarePlanner(
        _DelegateNever(), job_reader=lambda _job_id: {"status": job_status, "revision": 2},
        job_resume_supported=True,
    )
    with pytest.raises(ExpertBindingSnapshotError, match=f"status {job_status}"):
        planner.plan(
            {"turn_id": "turn-expert"},
            [{"type": "tool.completed", "data": {
                "capability_id": "analyze_source", "payload_ref": "crp://default/tool/outcome",
            }}], _capabilities(), _Payloads(),
        )


def test_pilot_expert_tools_are_real_production_registry_capabilities(tmp_path: Path) -> None:
    runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path))
    registered = {
        item.capability_id for item in runtime.capability_registry_snapshot().definitions
    }
    profile = default_video_research_expert_profile()

    assert set(profile["tools"]) <= registered
    assert profile["tools"] == [
        "analyze_source", "memory.recall", "document.draft.propose",
    ]


class _DelegateNever:
    def plan(self, *args, **kwargs):
        raise AssertionError("selected expert must not delegate")


def _bind(root: Path, path: str) -> None:
    catalog = ExpertCatalog(root)
    catalog.create(default_video_research_expert_profile() | {"status": "active"}, expected_registry_revision=0)
    ExpertProjectBindingStore(root).bind(
        "project-alpha", "video-research-expert", catalog=catalog,
        enabled_expert_revision=1,
        intent_affinity=[] if path == "default" else ["media_analysis"],
        selection_mode="auto", default=path == "default", reason="test", expected_store_revision=0,
    )


def _capabilities() -> tuple[CapabilityDefinition, ...]:
    return tuple(CapabilityDefinition(
        capability_id, version, "read", False, "read_only",
        "crp://default/contracts/input", "crp://default/contracts/output",
    ) for capability_id, version in (
        ("analyze_source", 3), ("memory.recall", 2), ("document.draft.propose", 4),
    ))


def _manifest(request: dict[str, object], capabilities: tuple[CapabilityDefinition, ...]):
    manifest = V1TurnPolicyCapabilityManifestResolver().resolve(request, capabilities)
    return replace(
        manifest,
        model_routing_snapshot_ref=(
            f"crp://session/{request['turn_id']}/turn-model-routing-snapshot-v1/frozen"
        ),
        model_routing_snapshot_revision=MODEL_ROUTE_REVISION,
    )


def _request(path: str) -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request["turn_id"] = f"adapter-{path}-turn"
    request["idempotency_key"] = f"adapter-{path}-key"
    request["desired_outcome"] = "media_analysis"
    request["capability_policy"]["allowed"] = list(TOOLS)
    if path == "explicit":
        request["expert_request"] = {
            "expert_id": "video-research-expert", "task_intents": ["media_analysis"],
            "budget": "research-2k",
        }
    return request
