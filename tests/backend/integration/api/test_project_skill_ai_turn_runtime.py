from __future__ import annotations

import json
from pathlib import Path

from backend.api.project_skill_ai_runtime import ProjectSkillDraftPlanner, ProjectSkillDraftProposalCapability
from core.ai_kernel import CapabilityDefinition, InMemoryTurnEventStore, InMemoryTurnPayloadStore, InMemoryTurnStateStore, ScopedCapabilityRegistry, SynchronousAIRuntime
from core.model_gateway import ModelResult
from core.storage_provider import JsonObjectStore
from tests.backend.integration.api.turn_model_routing_fixture import RoutingSnapshotFixture


ROOT = Path(__file__).resolve().parents[4]


class _Evidence:
    def invoke(self, request):
        project_id = request["scope"]["project_id"]
        refs = [{"source_id": "source-1", "locator": "source:summary"}]
        return {
            "summary": "evidence ready",
            "evidence_refs": ["crp://default/sources/source-1"],
            "result": {
                "schema_version": "1.0.0",
                "kind": "project_skill.evidence",
                "project_id": project_id,
                "goal": "生成项目规则",
                "operation": "create",
                "expected_project_skill_revision": 0,
                "model_input": {"project_id": project_id, "allowed_source_refs": refs},
                "source_refs": refs,
            },
        }


class _Gateway:
    calls = 0

    def invoke(self, request):
        self.calls += 1
        assert request.privacy_scope == "remote_allowed"
        assert request.parameters["messages"][0]["role"] == "system"
        assert request.parameters["messages"][1]["role"] == "user"
        return ModelResult({
            "name": "项目工作规则",
            "purpose": "把结论和证据作为默认回答结构。",
            "output_rules": [{"rule": "先给结论与证据。", "priority": "must", "source_refs": [{"source_id": "source-1", "locator": "source:summary"}]}],
            "style_preferences": {"voice": "直接"},
            "update_rules": {"patch_strategy": "patch_existing_first", "allowed_auto_updates": []},
            "outline": [],
            "markdown": "# 项目工作规则",
        }, "provider-test", "model-test", {})


def test_project_skill_turn_requires_approval_and_returns_domain_receipt(tmp_path: Path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    registry = ScopedCapabilityRegistry()
    registry.register(CapabilityDefinition("project_skill.evidence.read", 1, "read", False, "read_only", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"), _Evidence())
    registry.register(CapabilityDefinition("project_skill.draft.propose", 1, "write", True, "receipt_required", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"), ProjectSkillDraftProposalCapability(runtime_root=tmp_path, store=store, namespace_id="default"))
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    gateway = _Gateway()
    routing = RoutingSnapshotFixture(payloads, required_capability="structured", egress_purpose="memory_candidate")
    runtime = SynchronousAIRuntime(
        planner=ProjectSkillDraftPlanner(gateway),
        registry=registry,
        events=events,
        payloads=payloads,
        state=InMemoryTurnStateStore(),
        manifest_resolver=routing,
        context_manifest_resolver=routing.context_resolver,
    )
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request["desired_outcome"] = "project_skill.draft.generate"
    request["input"]["text"] = "生成项目规则"
    request["capability_policy"] = {"allowed": ["project_skill.evidence.read", "project_skill.draft.propose"], "denied": [], "require_approval": ["project_skill.draft.propose"]}
    request["privacy"] = {"mode": "remote_allowed", "allow_remote": True, "pii": "possible", "consent_refs": ["crp://default/consent/provider-egress-policy"], "retention": "local_durable"}

    waiting = runtime.submit_turn(request)
    assert waiting.status == "waiting_approval"
    assert store.list("memory_candidates") == ()
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    action = {
        "schema_version": "1.0.0",
        "action_id": "action-0123456789abcdef0123456789abcdef",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "provider call and pending review candidate confirmed",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-project-skill-draft-001",
        "created_at": "2026-08-23T08:00:00Z",
    }

    completed = runtime.apply_action(action)
    replay = runtime.apply_action(action)

    assert completed.status == "completed" and replay.replayed is True
    candidates = store.list("memory_candidates")
    assert len(candidates) == 1 and candidates[0]["status"] == "pending_review"
    assert candidates[0]["project_skill_draft"]["status"] == "draft"
    assert candidates[0]["project_skill_draft"]["trust_status"] == "system_generated"
    assert candidates[0]["project_skill_draft"]["output_rules"][0]["origin"] == "ai"
    assert runtime.presentation_for(completed.turn_id)["active_project_skill_changed"] is False
    tool_event = next(event for event in runtime.events_after(completed.turn_id) if event["type"] == "tool.completed" and event["data"]["capability_id"] == "project_skill.draft.propose")
    assert tool_event["data"]["receipt_ref"].startswith("crp://default/project-skill-ai-drafts/")
    assert gateway.calls == 1
