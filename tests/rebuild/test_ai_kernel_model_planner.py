from __future__ import annotations

from core.ai_kernel import (
    CapabilityDefinition,
    ContextCompaction,
    ContextEntry,
    ContextManifest,
    InMemoryTurnPayloadStore,
    ModelGatewayAgentPlanner,
    ModelPlannerError,
    context_manifest_to_payload,
)
from core.ai_kernel.model_planner import (
    _materialize_model_context,
    _model_entry_content,
    _turn_routing_parameters,
)
from core.ai_kernel.model_routing_snapshot_contract import (
    planner_routing_snapshot_revision,
)
from core.context_graph import (
    ContextCompiler,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextPermissionGrant,
    ContextProvenance,
    FrozenContextRevisions,
    StalenessEvaluationInput,
    context_binding_to_payload,
)
from core.model_gateway import ModelResult

import pytest


class _Gateway:
    def __init__(self, output) -> None:
        self.output = output
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return ModelResult(self.output, "test", "model", {})


class _ExecutionControl:
    remaining_timeout_ms = 10_000
    cancel_requested = False

    def checkpoint(self) -> None:
        return None


def test_planner_materializes_referenced_tool_result_and_selects_registered_tool() -> None:
    payloads = InMemoryTurnPayloadStore()
    payload_ref = payloads.put("turn-0123456789abcdef0123456789abcdef", "tool-result", [{"content": "evidence"}])
    gateway = _Gateway({"type": "tool", "capability_id": "memory.recall", "arguments": {"query": "question"}})
    planner = ModelGatewayAgentPlanner(gateway)
    decision = planner.plan(
        _request(allow_remote=True),
        [_event(payload_ref)],
        [_definition()],
        payloads,
    )
    assert decision == {"type": "tool", "capability_id": "memory.recall", "arguments": {"query": "question"}}
    assert gateway.requests[0].privacy_scope == "remote_allowed"
    assert "evidence" in gateway.requests[0].input


def test_planner_propagates_local_only_policy_to_model_gateway() -> None:
    gateway = _Gateway({"type": "complete", "summary": "local", "evidence_refs": []})
    planner = ModelGatewayAgentPlanner(gateway)
    assert planner.plan(_request(allow_remote=False), [], [_definition()], InMemoryTurnPayloadStore())["type"] == "complete"
    assert gateway.requests[0].privacy_scope == "local_only"


def test_planner_requires_explicit_remote_mode_before_allowing_remote_egress() -> None:
    gateway = _Gateway({"type": "complete", "summary": "local", "evidence_refs": []})
    planner = ModelGatewayAgentPlanner(gateway)

    request = _request(allow_remote=True)
    request["privacy"] = {"allow_remote": True, "mode": "local_only"}
    assert planner.plan(request, [], [_definition()], InMemoryTurnPayloadStore())["type"] == "complete"

    assert gateway.requests[0].privacy_scope == "local_only"


def test_planner_materializes_model_disclosed_context_binding() -> None:
    turn_id = "turn-0123456789abcdef0123456789abcdef"
    payloads = InMemoryTurnPayloadStore()
    capability_ref = payloads.put(turn_id, "capability-manifest", {"kind": "fixture"})
    binding_record = {
        "schema_version": "1.0.0",
        "binding_id": "binding-1",
        "project_id": "project-alpha",
        "capability_id": "thought_graph_context",
        "capability_revision": "4.0.0",
        "registry_revision": 1,
        "binding": _compiled_binding_payload(),
    }
    binding_ref = payloads.put(turn_id, "context-binding-v1", binding_record)
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}",
        turn_id=turn_id,
        resolver_id="fixture",
        project_id="project-alpha",
        series_id=None,
        project_profile_id="profile-1",
        project_profile_revision=1,
        boundary_profile_id="boundary-1",
        boundary_profile_revision=1,
        capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="context-entry-binding",
            kind="context_binding",
            source_ref="crp://context-bindings/project-alpha/binding-1",
            payload_ref=binding_ref,
            source_project_id="project-alpha",
            revision_identity="g1:1",
            content_fingerprint=None,
            provenance_refs=(),
            disclosure="model",
            selection_reason="explicit_linemap_context_binding",
            content_bytes=128,
        ),),
        compactions=(),
        excluded_reason_counts=(),
        max_context_bytes=4096,
        selected_context_bytes=128,
    )
    manifest_ref = payloads.put(
        turn_id, "context-manifest", context_manifest_to_payload(manifest),
    )
    gateway = _Gateway({"type": "complete", "summary": "used binding", "evidence_refs": []})

    ModelGatewayAgentPlanner(gateway).plan(
        _request(allow_remote=True),
        [{"type": "context.resolved", "data": {"payload_ref": manifest_ref}}],
        [_definition()],
        payloads,
    )

    assert "context_binding" in gateway.requests[0].input
    assert "verified evidence" in gateway.requests[0].input
    assert '"layers"' not in gateway.requests[0].input
    assert '"trimmed_nodes"' not in gateway.requests[0].input
    assert '"budget_explanation"' not in gateway.requests[0].input
    assert '"source_ref"' not in gateway.requests[0].input
    assert '"revision_identity"' not in gateway.requests[0].input


def test_planner_rejects_legacy_context_binding_model_disclosure() -> None:
    binding = _compiled_binding_payload()
    binding["compiler_revision"] = "1.0.0"
    legacy_total = sum(binding["layer_token_costs"].values())
    binding["total_token_cost"] = legacy_total
    binding["budget_explanation"] = {
        **binding["budget_explanation"],
        "original_token_estimate": legacy_total,
        "final_token_estimate": legacy_total,
    }
    record = {
        "schema_version": "1.0.0",
        "binding_id": "binding-legacy",
        "project_id": "project-alpha",
        "capability_id": "thought_graph_context",
        "capability_revision": "4.0.0",
        "registry_revision": 1,
        "binding": binding,
    }

    with pytest.raises(ModelPlannerError, match="compiler revision is unsupported"):
        _model_entry_content("context_binding", record)


def test_model_context_omits_compacted_sources_even_for_legacy_model_disclosure() -> None:
    turn_id = "turn-0123456789abcdef0123456789abcdef"
    payloads = InMemoryTurnPayloadStore()
    capability_ref = payloads.put(turn_id, "capability", {"kind": "fixture"})
    source_ref = payloads.put(turn_id, "memory-source", {"markdown": "DO-NOT-MATERIALIZE"})
    summary_ref = payloads.put(turn_id, "memory-summary", _context_summary_payload(turn_id))
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}", turn_id=turn_id, resolver_id="fixture",
        project_id="project-alpha", series_id=None, project_profile_id="profile", project_profile_revision=1,
        boundary_profile_id="boundary", boundary_profile_revision=1, capability_manifest_ref=capability_ref,
        entries=(
            ContextEntry("memory-source", "memory_r1", "crp://memory/project-alpha/source", source_ref,
                         "project-alpha", "1", None, (), "model", "legacy", 18),
            ContextEntry("memory-summary", "context_summary", "crp://memory/project-alpha/context-summary", summary_ref,
                         "project-alpha", "memory-summary/1", None, (), "model", "compacted", len("BRIEF".encode("utf-8"))),
        ),
        compactions=(ContextCompaction("compact-1", "deterministic", ("memory-source",), "memory-summary", 18, len("BRIEF".encode("utf-8"))),),
        excluded_reason_counts=(), max_context_bytes=1024, selected_context_bytes=18 + len("BRIEF".encode("utf-8")),
    )
    manifest_ref = payloads.put(turn_id, "context-manifest", context_manifest_to_payload(manifest))

    context = _materialize_model_context(
        [{"type": "context.resolved", "data": {"payload_ref": manifest_ref}}], payloads,
    )

    encoded = str(context)
    assert "BRIEF" in encoded
    assert "DO-NOT-MATERIALIZE" not in encoded


@pytest.mark.parametrize("mutate", [
    lambda value: {**value, "unexpected": True},
    lambda value: {**value, "projection_authority": "asserted_fact"},
    lambda value: {**value, "turn_id": "turn-other"},
    lambda value: {**value, "output_bytes": value["input_bytes"]},
    lambda value: {**value, "source_revisions": []},
])
def test_context_summary_model_payload_rejects_shape_or_authority_drift(mutate) -> None:
    value = mutate(_context_summary_payload("turn-0123456789abcdef0123456789abcdef"))

    with pytest.raises(ModelPlannerError):
        _model_entry_content(
            "context_summary", value,
            turn_id="turn-0123456789abcdef0123456789abcdef", project_id="project-alpha",
        )


def _context_summary_payload(turn_id: str) -> dict[str, object]:
    summary = "BRIEF"
    return {
        "schema_version": "1.0.0",
        "snapshot_kind": "turn_frozen_deterministic_memory_summary",
        "projection_authority": "derived_only",
        "turn_id": turn_id,
        "project_id": "project-alpha",
        "source_entry_ids": ["memory-source"],
        "source_revisions": [{"entry_id": "memory-source", "object_id": "atom-1", "revision": "1"}],
        "provenance_refs": [],
        "input_bytes": 18,
        "output_bytes": len(summary.encode("utf-8")),
        "summary": summary,
    }


def test_planner_propagates_execution_control_to_model_gateway() -> None:
    control = _ExecutionControl()
    gateway = _Gateway({"type": "complete", "summary": "controlled", "evidence_refs": []})

    ModelGatewayAgentPlanner(gateway).plan(
        _request(allow_remote=True), [], [_definition()], InMemoryTurnPayloadStore(), control
    )

    assert gateway.requests[0].execution_control is control
    assert gateway.requests[0].metadata_sink is control


def test_planner_rejects_unregistered_tool_decision() -> None:
    planner = ModelGatewayAgentPlanner(_Gateway({"type": "tool", "capability_id": "vault.write", "arguments": {}}))
    with pytest.raises(ModelPlannerError, match="not registered"):
        planner.plan(_request(allow_remote=True), [], [_definition()], InMemoryTurnPayloadStore())


def _definition() -> CapabilityDefinition:
    return CapabilityDefinition("memory.recall", 1, "read", False, "read_only", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json")


def _compiled_binding_payload() -> dict[str, object]:
    timestamp = "2026-08-30T00:00:00Z"
    node = ContextGraphNode(
        "evidence-a", "evidence", "Evidence A", "content:evidence-a", "r1",
        ("source:a",), "verified", timestamp, timestamp,
        metadata={"content": "verified evidence"},
    )
    snapshot = ContextGraphSnapshot(
        "1.0.0", "graph-1", "g1", "project-alpha", "fixture", "source-r1",
        timestamp, (node,), (), (node.node_id,), 5,
        ContextProvenance(
            "fixture", "source-r1", timestamp, "fixture-importer", "1.0.0",
            "fixture:graph-1",
        ),
    )
    revisions = FrozenContextRevisions(
        "4.0.0", "1", "provider-r1", "4", ContextCompiler.compiler_revision,
    )
    binding = ContextCompiler().compile(
        snapshot,
        revisions=revisions,
        expected_revisions=revisions,
        permission_grant=ContextPermissionGrant(
            "project-alpha", "permission-r1", frozenset({node.content_ref}),
        ),
        token_budget=500,
        staleness_input=StalenessEvaluationInput.baseline(snapshot, revisions),
    )
    return context_binding_to_payload(binding)


def _request(*, allow_remote: bool) -> dict[str, object]:
    return {
        "desired_outcome": "answer",
        "input": {"kind": "text", "text": "question", "refs": []},
        "scope": {"kind": "project", "project_id": "project-alpha", "series_id": None},
        "privacy": {
            "allow_remote": allow_remote,
            "mode": "remote_allowed" if allow_remote else "local_only",
        },
    }


def _event(payload_ref: str) -> dict[str, object]:
    return {"type": "tool.completed", "data": {"payload_ref": payload_ref}}


def test_planner_binds_frozen_routing_through_kernel_contract() -> None:
    turn_id = "turn-0123456789abcdef0123456789abcdef"
    payloads = InMemoryTurnPayloadStore()
    snapshot = {
        "turn": {"turn_id": turn_id},
        "project": {"project_id": "project-alpha"},
        "requirement": {
            "required_capability": "structured", "modality": "text",
            "output_contract": "json_object", "privacy_scope": "remote_allowed",
        },
    }
    snapshot_ref = payloads.get_or_create_immutable_payload(
        turn_id, "turn-model-routing-snapshot-v1", snapshot,
    )

    parameters = _turn_routing_parameters(_request(allow_remote=True) | {"turn_id": turn_id}, payloads)

    assert parameters["_model_routing_snapshot_ref"] == snapshot_ref
    assert parameters["_model_routing_snapshot"] == snapshot
    assert parameters["_model_routing_snapshot_revision"] == planner_routing_snapshot_revision(snapshot)



def _role_brief():
    return {"schema_version": "1.0.0", "kind": "agent.role-brief.v1",
            "profile_id": "subagent.custom.audit", "profile_revision": 2,
            "organization_role": "审计专家", "work_description": "核验资料。",
            "instructions": "只读核验\n交回证据。"}


def test_planner_adds_only_frozen_role_and_absence_preserves_json():
    import json
    request = {**_request(allow_remote=False), "turn_id": "turn-role-0001"}
    payloads = InMemoryTurnPayloadStore()
    gateway = _Gateway({"type": "complete", "summary": "done", "payload_ref": None, "evidence_refs": []})
    planner = ModelGatewayAgentPlanner(gateway)
    planner.plan(request, [], [], payloads)
    original = gateway.requests[-1].input
    assert "role" not in json.loads(original)
    brief = _role_brief()
    payloads.get_or_create_immutable_payload(request["turn_id"], "agent-role-brief-v1", brief)
    planner.plan(request, [], [], payloads)
    actual = json.loads(gateway.requests[-1].input)
    assert actual.pop("role") == {key: brief[key] for key in ("organization_role", "work_description", "instructions")}
    assert json.dumps(actual, ensure_ascii=False, separators=(",", ":")) == original


@pytest.mark.parametrize("field,value", [("instructions", None), ("instructions", "x" * 2001),
    ("instructions", "a\t"), ("organization_role", "x" * 81), ("work_description", "x" * 241),
    ("profile_revision", True), ("kind", "wrong"), ("schema_version", "9.0.0")])
def test_planner_rejects_corrupt_frozen_role_before_model(field, value):
    request = {**_request(allow_remote=False), "turn_id": "turn-role-0002"}
    payloads = InMemoryTurnPayloadStore()
    payloads.get_or_create_immutable_payload(request["turn_id"], "agent-role-brief-v1", {**_role_brief(), field: value})
    gateway = _Gateway({"type": "complete", "summary": "done", "payload_ref": None, "evidence_refs": []})
    with pytest.raises(ModelPlannerError):
        ModelGatewayAgentPlanner(gateway).plan(request, [], [], payloads)
    assert gateway.requests == []


def test_planner_rejects_incomplete_frozen_role():
    request = {**_request(allow_remote=False), "turn_id": "turn-role-0003"}
    payloads = InMemoryTurnPayloadStore()
    brief = _role_brief(); brief.pop("instructions")
    payloads.get_or_create_immutable_payload(request["turn_id"], "agent-role-brief-v1", brief)
    gateway = _Gateway({"type": "complete", "summary": "done", "payload_ref": None, "evidence_refs": []})
    with pytest.raises(ModelPlannerError):
        ModelGatewayAgentPlanner(gateway).plan(request, [], [], payloads)
    assert gateway.requests == []


def test_planner_accepts_actual_sqlite_frozen_routing_reference(tmp_path):
    from core.ai_kernel import SQLiteAITurnStore
    turn_id = "turn-sqlite-routing-0001"
    request = _request(allow_remote=True) | {
        "turn_id": turn_id, "session_id": "session-sqlite-routing",
        "operation_id": "operation-sqlite-routing", "idempotency_key": "sqlite-routing-key",
    }
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    store.claim_turn(request)
    snapshot = {"turn": {"turn_id": turn_id}, "project": {"project_id": "project-alpha"},
                "requirement": {"required_capability": "structured", "modality": "text",
                                "output_contract": "json_object", "privacy_scope": "remote_allowed"}}
    ref = store.get_or_create_immutable_payload(turn_id, "turn-model-routing-snapshot-v1", snapshot)
    assert ref == f"crp://session/{turn_id}/turn-model-routing-snapshot-v1"
    parameters = _turn_routing_parameters(request, store)
    assert parameters["_model_routing_snapshot_ref"] == ref
    assert parameters["_model_routing_snapshot"] == snapshot
    assert parameters["_model_routing_snapshot_revision"] == planner_routing_snapshot_revision(snapshot)


@pytest.mark.parametrize("ref", [
    "crp://session/turn-other/turn-model-routing-snapshot-v1",
    "crp://session/turn-sqlite-routing-0001/private-v1",
    "crp://session/turn-sqlite-routing-0001/turn-model-routing-snapshot-v1-other",
])
def test_planner_stable_routing_reference_rejects_wrong_identity(ref):
    class WrongIdentityStore(InMemoryTurnPayloadStore):
        def get_immutable_payload(self, turn_id, kind):
            return ref, {}
    request = _request(allow_remote=True) | {"turn_id": "turn-sqlite-routing-0001"}
    with pytest.raises(ModelPlannerError, match="routing snapshot reference is invalid"):
        _turn_routing_parameters(request, WrongIdentityStore())
