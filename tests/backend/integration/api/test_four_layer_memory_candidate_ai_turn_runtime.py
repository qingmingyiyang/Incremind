from __future__ import annotations

from core.ai_kernel import CapabilityDefinition, InMemoryTurnEventStore, InMemoryTurnPayloadStore, InMemoryTurnStateStore, ScopedCapabilityRegistry, SynchronousAIRuntime
from backend.api.four_layer_memory_candidate_ai_runtime import (
    FOUR_LAYER_MEMORY_CANDIDATE_OUTCOME, FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY, FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY,
    FourLayerMemoryCandidateProposalCapability, FourLayerMemoryCandidateTurnPlanner, FourLayerMemoryEvidenceCapability,
)


def _runtime(*, revisions=None, output=None, cancel_after_generate=False):
    revisions = revisions or {"prompt": 1, "route": 1, "provider": 1}; writes = [] ; payloads = InMemoryTurnPayloadStore(); registry = ScopedCapabilityRegistry()
    evidence = {"status": "completed", "evidence_id": "evidence-1", "source_id": "source-1", "kind": "source_content_read", "revision": 1, "summary": "private source api_key=sk-abcdefghijklmnop C:\\secret.txt", "refs": ["crp://default/sources/source-1"], "provider_request": {"system_prompt": "private prompt", "user_payload": {"content_preview": "private source"}}}
    authority = lambda: {"prompt_revision": revisions["prompt"], "route_revision": revisions["route"], "provider_revision": revisions["provider"], "provider_id": "fixture"}
    controls = []
    def generator(current_evidence, current_authority, gateway, execution_control):
        assert gateway is None
        controls.append(execution_control)
        if cancel_after_generate:
            execution_control.cancellation.request()
        return output or {"candidates": [{"target_layer": "atom", "proposed_content": "候选"}], "insufficient_evidence": [], "provider_boundary": {"mode": "fixture"}}
    def importer(provider_output, current_evidence, project_id):
        if "api_key" in str(provider_output): raise ValueError("provider output includes forbidden secret")
        writes.append((provider_output, current_evidence, project_id))
        return {"candidate_ids": ["candidate-1"], "memory_publication_state": "candidates_created_not_published"}
    loader = lambda evidence_id: evidence
    registry.register(CapabilityDefinition(FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY, 1, "read", False, "read_only", "crp://in", "crp://out"), FourLayerMemoryEvidenceCapability(evidence_loader=loader, authority_loader=authority))
    registry.register(CapabilityDefinition(FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY, 1, "external", True, "receipt_required", "crp://in", "crp://out"), FourLayerMemoryCandidateProposalCapability(evidence_loader=loader, authority_loader=authority, importer=importer, proposal_generator=generator, receipt_store=payloads))
    runtime = SynchronousAIRuntime(planner=FourLayerMemoryCandidateTurnPlanner(), registry=registry, events=InMemoryTurnEventStore(), payloads=payloads, state=InMemoryTurnStateStore())
    return runtime, payloads, writes, revisions, controls


def _request():
    return {"schema_version": "1.0.0", "turn_id": "turn-memory-001", "session_id": "session-memory-001", "operation_id": "memory-propose-001", "idempotency_key": "memory-propose-001", "scope": {"kind": "project", "project_id": "default", "series_id": None}, "input": {"kind": "text", "text": "propose reviewable memory candidates", "refs": [{"kind": "memory_evidence", "object_id": "evidence-1", "uri": "crp://default/sources/source-1"}]}, "desired_outcome": FOUR_LAYER_MEMORY_CANDIDATE_OUTCOME, "privacy": {"mode": "local_only", "allow_remote": False, "pii": "possible", "consent_refs": [], "retention": "local_durable"}, "capability_policy": {"allowed": [FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY, FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY], "denied": [], "require_approval": [FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY]}, "context_policy": {"include_project_skill": False, "include_memory": False, "include_session_history": False, "max_context_bytes": 4096}, "approval_policy": {"mode": "explicit", "auto_approve_read_only": True}, "created_at": "2026-08-23T00:00:00+00:00"}


def _approve(runtime, waiting):
    event = next(item for item in reversed(list(runtime.events_after(waiting.turn_id))) if item["type"] == "approval.required")
    return runtime.apply_action({"schema_version": "1.0.0", "action_id": "action-memory-001", "turn_id": waiting.turn_id, "type": "approve", "target_event_id": event["event_id"], "reason": "create pending-review candidates", "actor": "user", "expected_sequence": event["sequence"], "idempotency_key": "approve-memory-001", "created_at": "2026-08-23T00:00:01+00:00"})


def _redacted(value):
    text = str(value).lower(); assert "sk-abcdefghijklmnop" not in text and "c:\\secret.txt" not in text and "private prompt" not in text


def test_evidence_read_is_zero_write_and_persisted_context_is_redacted():
    runtime, payloads, writes, _, _controls = _runtime(); waiting = runtime.submit_turn(_request())
    assert waiting.status == "waiting_approval" and writes == []
    event = next(item for item in runtime.events_after(waiting.turn_id) if item["type"] == "tool.completed" and item["data"]["capability_id"] == FOUR_LAYER_MEMORY_EVIDENCE_CAPABILITY)
    _redacted(payloads.get(event["data"]["payload_ref"]))


def test_approval_imports_pending_review_once_and_action_replays_without_duplicate_write():
    runtime, payloads, writes, _, controls = _runtime(); waiting = runtime.submit_turn(_request()); complete = _approve(runtime, waiting); replay = _approve(runtime, waiting)
    assert complete.status == replay.status == "completed" and replay.replayed is True and len(writes) == 1
    assert len(controls) == 1 and controls[0] is not None
    assert runtime.presentation_for(complete.turn_id)["memory_publication_state"] == "candidates_created_not_published"
    receipt = next(item for item in runtime.events_after(complete.turn_id) if item["type"] == "tool.completed" and item["data"]["capability_id"] == FOUR_LAYER_MEMORY_PROPOSE_CAPABILITY)
    _redacted(payloads.get(receipt["data"]["receipt_ref"]))


def test_baseline_drift_and_forbidden_output_fail_without_domain_write():
    runtime, _payloads, writes, revisions, _controls = _runtime(); waiting = runtime.submit_turn(_request()); revisions["provider"] = 2
    assert _approve(runtime, waiting).status == "failed" and writes == []
    forbidden_runtime, _payloads, forbidden_writes, _, _controls = _runtime(output={"api_key": "bad"})
    assert _approve(forbidden_runtime, forbidden_runtime.submit_turn(_request())).status == "failed" and forbidden_writes == []


def test_model_postflight_cancel_stops_before_memory_import():
    runtime, _payloads, writes, _revisions, _controls = _runtime(cancel_after_generate=True)
    failed = _approve(runtime, runtime.submit_turn(_request()))
    assert failed.status == "failed" and writes == []
    assert list(runtime.events_after(failed.turn_id))[-1]["data"]["error_code"] == "ai.tool_cancel_unconfirmed"
