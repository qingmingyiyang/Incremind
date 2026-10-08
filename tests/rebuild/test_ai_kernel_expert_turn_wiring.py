from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.api.expert_memory_proposal_runtime import ExpertMemoryProposalRuntime
from backend.api.expert_turn_binding_runtime import ExpertTurnBindingRuntime
from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    ScopedCapabilityRegistry,
    SQLiteAITurnStore,
    SynchronousAIRuntime,
    V1TurnContextManifestResolver,
    V1TurnPolicyCapabilityManifestResolver,
    context_manifest_from_payload,
    context_manifest_to_payload,
    manifest_to_payload,
)
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    default_video_research_expert_profile,
)
from core.ai_kernel.recovery import classify_recovery
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
MODEL_ROUTE_REVISION = "a" * 64
EXPERT_TOOLS = ("analyze_source", "memory.recall", "document.draft.propose")


class _Provider:
    def invoke(self, request):
        return {"summary": "unused"}


class _ExpertManifestResolver:
    def resolve(self, request, capabilities):
        manifest = V1TurnPolicyCapabilityManifestResolver().resolve(request, capabilities)
        return replace(
            manifest,
            model_routing_snapshot_ref=(
                f"crp://session/{request['turn_id']}/turn-model-routing-snapshot-v1/frozen"
            ),
            model_routing_snapshot_revision=MODEL_ROUTE_REVISION,
        )


class _RecordingPlanner:
    def __init__(self) -> None:
        self.capability_sets: list[tuple[str, ...]] = []

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        self.capability_sets.append(tuple(item.capability_id for item in capabilities))
        return {"type": "complete", "summary": "expert wiring observed"}


class _OutOfBindingPlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        assert "memory.recall" not in {item.capability_id for item in capabilities}
        return {"type": "tool", "capability_id": "memory.recall", "arguments": {}}


class _SensitiveSummaryPlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        return {
            "type": "complete",
            "summary": "result token=1234567890abcdefghijklmnop",
            "evidence_refs": ["source:src-1#t=42"],
        }


class _CountingBindingRuntime(ExpertTurnBindingRuntime):
    def __init__(self, root_dir: Path, *, fail_verify: bool = False) -> None:
        super().__init__(root_dir)
        self.select_calls = 0
        self.freeze_calls = 0
        self.verify_calls = 0
        self.fail_verify = fail_verify

    def select(self, request, capability_manifest, capabilities):
        self.select_calls += 1
        return super().select(request, capability_manifest, capabilities)

    def freeze(self, request, selection_receipt, capability_manifest, context_manifest, capabilities):
        self.freeze_calls += 1
        return super().freeze(
            request, selection_receipt, capability_manifest, context_manifest, capabilities
        )

    def verify_replay(self, request, selection_receipt, snapshot, capability_manifest, context_manifest, capabilities):
        self.verify_calls += 1
        if self.fail_verify:
            raise RuntimeError("authoritative expert replay verifier failed")
        return super().verify_replay(
            request, selection_receipt, snapshot, capability_manifest, context_manifest, capabilities
        )


class _IdempotentProposalOutbox:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.commit_calls = 0

    def prepare(self, snapshot, expert_receipt, expert_result):
        self.prepare_calls += 1
        return {
            "schema_version": "1.0.0",
            "proposal_id": f"expert-proposal-{expert_receipt['receipt_id']}",
            "proposal_type": "memory_candidate_proposal",
            "project_id": snapshot["project_id"],
            "target_layer": "atom",
            "candidate_type": "answer_fact",
            "content": expert_result["summary"],
            "evidence_refs": list(expert_result["evidence_refs"]),
        }

    def commit(self, proposal, expert_receipt):
        self.commit_calls += 1
        return {
            "schema_version": "1.0.0",
            "proposal_id": proposal["proposal_id"],
            "proposal_type": proposal["proposal_type"],
            "project_id": proposal["project_id"],
            "memory_candidate_id": f"candidate-{expert_receipt['receipt_id']}",
            "status": "pending_review",
            "review_state": "pending_review",
            "memory_publication_state": "not_published",
            "blocked_operations": ["automatic_memory_publication"],
            "expert_receipt_id": expert_receipt["receipt_id"],
        }


@pytest.mark.parametrize(
    ("path", "expected_mode"),
    (("explicit", "explicit"), ("default", "project_default"), ("legacy-confirm", "explicit")),
)
def test_selected_expert_is_selected_and_frozen_once_then_replay_only_verifies(
    tmp_path: Path, path: str, expected_mode: str,
) -> None:
    _bind_video_expert(tmp_path, path=path)
    request = _request(path)
    planner = _RecordingPlanner()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    binding = _CountingBindingRuntime(tmp_path)
    runtime = SynchronousAIRuntime(
        planner=planner,
        registry=_registry(),
        events=events,
        payloads=payloads,
        manifest_resolver=_ExpertManifestResolver(),
        expert_binding=binding,
    )

    receipt = runtime.submit_turn(request)

    assert receipt.status == "completed"
    assert binding.select_calls == binding.freeze_calls == 1
    assert binding.verify_calls == 0
    selection_ref, selection = payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-selection-receipt-v1"
    )
    snapshot_ref, snapshot = payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-binding-snapshot-v1"
    )
    assert selection_ref and snapshot_ref
    assert selection["selection_mode"] == expected_mode
    assert selection["selected"]["expert_id"] == "video-research-expert"
    assert snapshot["context_manifest_revision"] == f"context-manifest-{request['turn_id']}"
    assert snapshot["model_route_revision"] == MODEL_ROUTE_REVISION
    assert snapshot["boundary_revision"] == 1
    assert snapshot["role"] == "视频内容研究与证据整理"
    assert "字幕优先" in snapshot["method"]
    assert snapshot["prohibited"]
    assert snapshot["tool_capability_revisions"] == {
        "analyze_source": 3, "memory.recall": 1, "document.draft.propose": 4,
    }
    assert planner.capability_sets == [tuple(sorted(EXPERT_TOOLS))]
    assert [event["type"] for event in events.events_after(str(request["turn_id"]))].count(
        "expert.selection.recorded"
    ) == 1
    assert [event["type"] for event in events.events_after(str(request["turn_id"]))].count(
        "expert.binding.frozen"
    ) == 1
    execution_ref, execution = payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-execution-receipt-v1"
    )
    result_ref, result = payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-result-v1"
    )
    assert execution_ref and result_ref
    assert execution["snapshot_id"] == snapshot["snapshot_id"]
    assert execution["context_manifest_revision"] == snapshot["context_manifest_revision"]
    assert execution["output_refs"] == [result_ref]
    assert result["context_manifest_revision"] == snapshot["context_manifest_revision"]
    assert [event["type"] for event in events.events_after(str(request["turn_id"]))].count(
        "expert.execution.receipted"
    ) == 1
    stream = tuple(events.events_after(str(request["turn_id"])))
    receipt_index = next(
        index for index, event in enumerate(stream)
        if event["type"] == "expert.execution.receipted"
    )
    assert classify_recovery(
        str(request["turn_id"]), 2, stream[: receipt_index + 1],
        payload_loader=payloads.get,
    ).disposition == "safe_resume"

    # Recovery has a durable receipt/snapshot. It must verify those facts and
    # must never ask the selector to make a fresh choice.
    manifest = runtime._manifest_for(str(request["turn_id"]), request)
    context_event = next(
        event for event in events.events_after(str(request["turn_id"]))
        if event["type"] == "context.resolved"
    )
    context = context_manifest_from_payload(payloads.get(context_event["data"]["payload_ref"]))
    runtime._ensure_expert_binding_snapshot(
        str(request["turn_id"]), request, selection, manifest, context,
    )
    assert binding.select_calls == 1
    assert binding.freeze_calls == 1
    assert binding.verify_calls == 1


def test_no_selection_keeps_generic_turn_capabilities(tmp_path: Path) -> None:
    request = _request("unselected")
    planner = _RecordingPlanner()
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=planner,
        registry=_registry(),
        events=InMemoryTurnEventStore(),
        payloads=payloads,
        manifest_resolver=_ExpertManifestResolver(),
        expert_binding=_CountingBindingRuntime(tmp_path),
    )

    receipt = runtime.submit_turn(request)

    assert receipt.status == "completed"
    assert planner.capability_sets == [
        ("analyze_source", "document.draft.propose", "memory.recall")
    ]
    assert payloads.get_immutable_payload(str(request["turn_id"]), "expert-binding-snapshot-v1") is None


def test_legacy_affinity_binding_keeps_generic_turn_without_freezing_expert(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path, path="affinity")
    request = _request("affinity")
    planner = _RecordingPlanner()
    payloads = InMemoryTurnPayloadStore()
    binding = _CountingBindingRuntime(tmp_path)
    runtime = SynchronousAIRuntime(
        planner=planner, registry=_registry(), events=InMemoryTurnEventStore(),
        payloads=payloads, manifest_resolver=_ExpertManifestResolver(),
        expert_binding=binding,
    )

    receipt = runtime.submit_turn(request)

    assert receipt.status == "completed"
    assert binding.select_calls == 1
    assert binding.freeze_calls == 1  # Real freeze returns None for no selection.
    assert binding.verify_calls == 0
    assert planner.capability_sets == [tuple(sorted(EXPERT_TOOLS))]
    _, selection = payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-selection-receipt-v1",
    )
    assert selection["selection_mode"] == "none"
    assert selection["selected"] is None
    assert selection["candidates"][0]["reason"] == "not_project_default"
    assert payloads.get_immutable_payload(str(request["turn_id"]), "expert-binding-snapshot-v1") is None
    assert payloads.get_immutable_payload(str(request["turn_id"]), "expert-memory-proposal-v1") is None


def test_expert_proposal_outbox_recovers_after_external_commit_before_local_result(
    tmp_path: Path,
) -> None:
    _bind_video_expert(tmp_path, path="explicit")
    request = _request("explicit")
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    outbox = _IdempotentProposalOutbox()
    runtime = SynchronousAIRuntime(
        planner=_RecordingPlanner(), registry=_registry(), events=events,
        payloads=payloads, manifest_resolver=_ExpertManifestResolver(),
        expert_binding=_CountingBindingRuntime(tmp_path),
        expert_memory_proposal_sink=outbox,
    )
    original_append = runtime._append_expert_immutable_event
    failed_once = False

    def fail_after_external_commit(turn_id, **kwargs):
        nonlocal failed_once
        if kwargs.get("immutable_kind") == "expert-memory-proposal-v1" and not failed_once:
            failed_once = True
            raise RuntimeError("fixture crash after external proposal commit")
        return original_append(turn_id, **kwargs)

    runtime._append_expert_immutable_event = fail_after_external_commit
    first = runtime.submit_turn(request)

    assert first.status == "running", events.events_after(str(request["turn_id"]))
    assert outbox.commit_calls == 1
    assert payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-memory-proposal-intent-v1",
    ) is not None
    assert payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-memory-proposal-v1",
    ) is None
    first_events = tuple(events.events_after(str(request["turn_id"])))
    assert not [event for event in first_events if event["type"] == "turn.failed"]
    assert classify_recovery(
        str(request["turn_id"]), 2, first_events, payload_loader=payloads.get,
    ).disposition == "safe_resume"

    runtime._append_expert_immutable_event = original_append
    resumed = runtime.run_accepted_turn(str(request["turn_id"]))

    assert resumed.status == "completed"
    assert outbox.prepare_calls == 1
    assert outbox.commit_calls == 2
    assert payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-memory-proposal-v1",
    ) is not None
    resumed_events = tuple(events.events_after(str(request["turn_id"])))
    assert sum(event["type"] == "expert.memory.proposal.intent.recorded" for event in resumed_events) == 1
    assert sum(event["type"] == "expert.memory.proposed" for event in resumed_events) == 1
    assert not [event for event in resumed_events if event["type"] == "turn.failed"]


def test_sensitive_expert_summary_never_enters_proposal_intent_or_object_store(
    tmp_path: Path,
) -> None:
    _bind_video_expert(tmp_path, path="explicit")
    request = _request("explicit")
    payloads = InMemoryTurnPayloadStore()
    store = JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="default")
    runtime = SynchronousAIRuntime(
        planner=_SensitiveSummaryPlanner(), registry=_registry(),
        events=InMemoryTurnEventStore(), payloads=payloads,
        manifest_resolver=_ExpertManifestResolver(),
        expert_binding=_CountingBindingRuntime(tmp_path),
        expert_memory_proposal_sink=ExpertMemoryProposalRuntime(
            tmp_path, store, namespace_id="default",
        ),
    )

    receipt = runtime.submit_turn(request)

    assert receipt.status == "failed"
    assert payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-memory-proposal-intent-v1",
    ) is None
    assert payloads.get_immutable_payload(
        str(request["turn_id"]), "expert-memory-proposal-v1",
    ) is None
    assert store.list("external_agent_proposals") == ()
    assert store.list("memory_candidates") == ()


def test_legacy_confirmation_receipt_fails_closed_before_planner(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path, path="explicit")
    request = _request("explicit")
    planner = _RecordingPlanner()
    events = InMemoryTurnEventStore()
    binding = _CountingBindingRuntime(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    resolver = _ExpertManifestResolver()
    runtime = SynchronousAIRuntime(
        planner=planner, registry=_registry(), events=events,
        payloads=payloads, manifest_resolver=resolver,
        expert_binding=binding,
    )
    runtime.accept_turn(request)
    # Existing durable legacy facts retain their confirmation requirement.
    # New legacy-confirm bindings instead migrate to manual explicit selection.
    selection = dict(binding.select(request, resolver.resolve(request, _registry().list()), _registry().list()))
    selection["selected"] = dict(selection["selected"], requires_confirmation=True)
    runtime._append_expert_immutable_event(
        str(request["turn_id"]), event_type="expert.selection.recorded",
        summary="legacy selection recorded", immutable_kind="expert-selection-receipt-v1",
        payload=selection,
    )

    receipt = runtime.run_accepted_turn(str(request["turn_id"]))

    assert receipt.status == "failed"
    assert planner.capability_sets == []
    assert events.events_after(str(request["turn_id"]))[-1]["type"] == "turn.failed"
    assert payloads.get_immutable_payload(str(request["turn_id"]), "expert-binding-snapshot-v1") is None


def test_replay_verifier_failure_converges_turn_to_failed(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path, path="explicit")
    request = _request("explicit")
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    binding = _CountingBindingRuntime(tmp_path, fail_verify=True)
    resolver = _ExpertManifestResolver()
    runtime = SynchronousAIRuntime(
        planner=_RecordingPlanner(), registry=_registry(), events=events, payloads=payloads,
        manifest_resolver=resolver, expert_binding=binding,
    )
    runtime.accept_turn(request)
    manifest = resolver.resolve(request, _registry().list())
    manifest_ref = payloads.put(str(request["turn_id"]), "capability-manifest", {
        "schema_version": "1.0.0", "manifest_id": manifest.manifest_id,
        "turn_id": manifest.turn_id, "resolver_id": manifest.resolver_id,
        "profile_id": manifest.profile_id, "profile_revision": manifest.profile_revision,
        "boundary_profile_id": manifest.boundary_profile_id,
        "boundary_profile_revision": manifest.boundary_profile_revision,
        "capability_ids": list(manifest.capability_ids), "excluded_reason_counts": [],
        "descriptor_bytes": manifest.descriptor_bytes,
        "model_routing_snapshot_ref": manifest.model_routing_snapshot_ref,
        "model_routing_snapshot_revision": manifest.model_routing_snapshot_revision,
    })
    context = V1TurnContextManifestResolver().resolve(request, manifest_ref, manifest)
    context_ref = payloads.put(
        str(request["turn_id"]), "context-manifest", context_manifest_to_payload(context),
    )
    selection = binding.select(request, manifest, _registry().list())
    snapshot = binding.freeze(request, selection, manifest, context, _registry().list())
    payloads.get_or_create_immutable_payload(
        str(request["turn_id"]), "expert-selection-receipt-v1", selection,
    )
    payloads.get_or_create_immutable_payload(
        str(request["turn_id"]), "expert-binding-snapshot-v1", snapshot,
    )
    runtime._append(str(request["turn_id"]), "context.resolved", "running", "restored", payload_ref=context_ref)

    receipt = runtime.run_accepted_turn(str(request["turn_id"]))

    assert receipt.status == "failed"
    assert binding.select_calls == 1
    assert binding.verify_calls == 1
    assert events.events_after(str(request["turn_id"]))[-1]["type"] == "turn.failed"


def test_planner_cannot_execute_capability_outside_frozen_expert_binding(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path, path="explicit")
    request = _request("explicit")
    events = InMemoryTurnEventStore()
    runtime = SynchronousAIRuntime(
        planner=_OutOfBindingPlanner(), registry=_registry(), events=events,
        payloads=InMemoryTurnPayloadStore(), manifest_resolver=_ExpertManifestResolver(),
        expert_binding=_CountingBindingRuntime(tmp_path),
    )

    receipt = runtime.submit_turn(request)

    assert receipt.status == "failed"
    terminal = events.events_after(str(request["turn_id"]))[-1]
    assert terminal["type"] == "turn.failed"
    assert terminal["data"]["error_code"] == "ai.execution_failed"


def test_sqlite_restart_verifies_original_snapshot_without_reselection(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path, path="explicit")
    request = _request("explicit")
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    first_binding = _CountingBindingRuntime(tmp_path)
    first = SynchronousAIRuntime(
        planner=_RecordingPlanner(), registry=_registry(), events=store, payloads=store,
        state=store, manifest_resolver=_ExpertManifestResolver(),
        expert_binding=first_binding,
    )
    first.accept_turn(request)
    manifest = first._resolve_manifest(request)
    manifest_ref = store.put(
        str(request["turn_id"]), "capability-manifest", manifest_to_payload(manifest)
    )
    selection = first._ensure_expert_selection(
        str(request["turn_id"]), request, manifest, allow_create=True,
    )
    context = V1TurnContextManifestResolver().resolve(request, manifest_ref, manifest)
    context_ref = store.put(
        str(request["turn_id"]), "context-manifest", context_manifest_to_payload(context)
    )
    first._append(
        str(request["turn_id"]), "context.resolved", "running", "context resolved",
        payload_ref=context_ref,
    )
    first._ensure_expert_binding_snapshot(
        str(request["turn_id"]), request, selection, manifest, context,
    )

    restarted_store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    restarted_binding = _CountingBindingRuntime(tmp_path)
    restarted = SynchronousAIRuntime(
        planner=_RecordingPlanner(), registry=_registry(), events=restarted_store,
        payloads=restarted_store, state=restarted_store,
        manifest_resolver=_ExpertManifestResolver(), expert_binding=restarted_binding,
    )
    receipt = restarted.run_accepted_turn(str(request["turn_id"]))

    assert receipt.status == "completed"
    assert restarted_binding.select_calls == 0
    assert restarted_binding.freeze_calls == 0
    assert restarted_binding.verify_calls == 1
    assert restarted_store.get_immutable_payload(
        str(request["turn_id"]), "expert-binding-snapshot-v1"
    ) is not None


def test_explicit_expert_request_is_part_of_the_canonical_turn_schema() -> None:
    schema = json.loads(
        (ROOT / "core-contracts" / "ai" / "turn-request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    request = _request("explicit")
    request["turn_id"] = "turn-" + "a" * 32
    request["operation_id"] = "op-expert-schema-0001"
    request["idempotency_key"] = "expert-schema-key-0001"

    assert list(Draft202012Validator(schema).iter_errors(request)) == []


def _bind_video_expert(root: Path, *, path: str) -> None:
    catalog = ExpertCatalog(root)
    catalog.create(default_video_research_expert_profile() | {"status": "active"}, expected_registry_revision=0)
    if path == "unselected":
        return
    ExpertProjectBindingStore(root).bind(
        "project-alpha", "video-research-expert", catalog=catalog,
        enabled_expert_revision=1,
        intent_affinity=["media_analysis"] if path != "default" else [],
        selection_mode=("disabled" if path == "disabled" else
                        "confirm" if path == "legacy-confirm" else "manual"),
        default=path == "default", reason="test binding", expected_store_revision=0,
    )


def _registry() -> ScopedCapabilityRegistry:
    registry = ScopedCapabilityRegistry()
    for capability_id, version in (
        ("analyze_source", 3), ("memory.recall", 1),
        ("document.draft.propose", 4),
    ):
        registry.register(CapabilityDefinition(
            capability_id, version, "read", False, "read_only",
            "crp://default/contracts/input", "crp://default/contracts/output",
        ), _Provider())
    return registry


def _request(path: str) -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request["turn_id"] = f"expert-{path}-turn"
    request["idempotency_key"] = f"expert-{path}-key"
    request["desired_outcome"] = "media_analysis"
    request["capability_policy"]["allowed"] = list(EXPERT_TOOLS)
    if path in {"explicit", "disabled", "legacy-confirm"}:
        request["expert_request"] = {
            "expert_id": "video-research-expert", "task_intents": ["media_analysis"],
            "budget": "research-2k",
        }
    return request
