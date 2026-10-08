from __future__ import annotations

import json
import sqlite3
import pytest

from backend.api.agent_capabilities import agent_capability_definition
from backend.security.turn_frozen_authorization import FROZEN_TOOL_AUTHORIZATION_KIND, TurnFrozenAuthorizationAuthority
from core.ai_kernel import CapabilityDefinition, CodexHookHost, HookHandlerManifest, HookPolicyCatalog, HookPolicySnapshot, RevisionPinnedHookRunner, SQLiteAITurnStore, ScopedCapabilityRegistry, SynchronousAIRuntime, ToolExecutionBoundaryDecision
from core.ai_kernel.codex_hook_parity import HookEvent, HookRun


class _Planner:
    def __init__(self, capability_id="memory.recall"): self.capability_id = capability_id
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if any(item["type"] == "tool.completed" for item in events):
            return {"type": "complete", "summary": "done"}
        return {"type": "tool", "capability_id": self.capability_id, "arguments": {"query": "safe"}}


class _Provider:
    def __init__(self): self.calls = []
    def invoke(self, request): self.calls.append(request); return {"summary": "ok", "result": {"ok": True}, "receipt_ref": "crp://default/receipts/test"}


class _Boundary:
    def __init__(self): self.evaluations = 0; self.fences = 0; self.host_sanitizations = 0
    def evaluate(self, request, capability, decision):
        self.evaluations += 1
        return ToolExecutionBoundaryDecision("allow", ("test_allow",), (), 1, False, False, dict(decision["arguments"]))
    def sanitize_candidate_arguments(self, capability, arguments, *, turn_id): return dict(arguments)
    def sanitize_host_candidate_arguments(self, capability, arguments, *, turn_id):
        self.host_sanitizations += 1
        return dict(arguments)
    def dispatch_fence(self, request):
        self.fences += 1
        from contextlib import nullcontext
        return nullcontext()


class _CrashOnceDuringFactsIssue:
    def __init__(self, delegate):
        self._delegate = delegate
        self._armed = True

    def issue(self, *args, **kwargs):
        if self._armed:
            self._armed = False
            raise SystemExit("simulated crash before frozen facts persistence")
        return self._delegate.issue(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._delegate, name)


class _CrashOnceAfterApprovalFact:
    def __init__(self, delegate):
        self._delegate = delegate
        self._armed = True

    def create_approval(self, *args, **kwargs):
        result = self._delegate.create_approval(*args, **kwargs)
        if self._armed:
            self._armed = False
            raise SystemExit("simulated crash after approval fact persistence")
        return result

    def __getattr__(self, name):
        return getattr(self._delegate, name)


def test_hook_runtime_issues_sqlite_immutable_facts_and_restart_rehydrates(tmp_path):
    db = tmp_path / "turns.sqlite3"; provider = _Provider(); boundary = _Boundary()
    first, store = _runtime(db, provider, boundary)
    accepted = first.accept_turn(_request())
    assert store.get_immutable_payload(accepted.turn_id, FROZEN_TOOL_AUTHORIZATION_KIND) is None
    restarted, second_store = _runtime(db, provider, boundary)
    result = restarted.run_accepted_turn(accepted.turn_id)
    assert result.status == "completed" and provider.calls
    frozen = second_store.get_immutable_payload(accepted.turn_id, FROZEN_TOOL_AUTHORIZATION_KIND)
    assert frozen is not None and frozen[1]["schema_version"] == "1.1.0"
    assert boundary.evaluations == 1
    assert boundary.fences == 0


def test_restart_idempotently_issues_facts_after_context_event_crash(tmp_path):
    db = tmp_path / "context-gap.sqlite3"; provider = _Provider(); boundary = _Boundary()
    first, store = _runtime(db, provider, boundary)
    first._frozen_authorization = _CrashOnceDuringFactsIssue(first._frozen_authorization)

    with pytest.raises(SystemExit, match="before frozen facts"):
        first.submit_turn(_request())

    turn_id = "turn-0123456789abcdef0123456789abcdef"
    assert tuple(store.events_after(turn_id))[-1]["type"] == "context.resolved"
    assert store.get_immutable_payload(turn_id, FROZEN_TOOL_AUTHORIZATION_KIND) is None

    restarted, restarted_store = _runtime(db, provider, boundary)
    result = restarted.run_accepted_turn(turn_id)

    assert result.status == "completed" and len(provider.calls) == 1
    assert restarted_store.get_immutable_payload(turn_id, FROZEN_TOOL_AUTHORIZATION_KIND) is not None
    assert boundary.evaluations == 1


def test_write_approval_is_exactly_bound_and_survives_runtime_restart(tmp_path):
    db = tmp_path / "approval.sqlite3"; provider = _Provider(); boundary = _Boundary()
    definition = CapabilityDefinition("document.draft", 1, "write", True, "receipt_required", "crp://default/in", "crp://default/out")
    first, _store = _runtime(db, provider, boundary, definition=definition)
    request = _request(); request["capability_policy"] = {"allowed": ["document.draft"], "denied": [], "require_approval": ["document.draft"]}
    waiting = first.submit_turn(request)
    approval_event = tuple(first.events_after(waiting.turn_id))[-1]
    assert waiting.status == "waiting_approval" and provider.calls == []
    restarted, _store = _runtime(db, provider, boundary, definition=definition)
    action = {"schema_version": "1.0.0", "action_id": "action-0123456789abcdef0123456789abcdef", "turn_id": waiting.turn_id, "type": "approve", "target_event_id": approval_event["event_id"], "reason": "approved", "actor": "user", "expected_sequence": waiting.current_sequence, "idempotency_key": "frozen-approval-0001", "created_at": "2026-08-23T06:00:00Z"}
    assert restarted.apply_action(action).status == "completed"
    assert len(provider.calls) == 1
    intent = next(item for item in restarted.events_after(waiting.turn_id) if item["type"] == "tool.intent.recorded")
    intent_ref = intent["data"]["payload_ref"]
    frozen_intent = _store.get(intent_ref)
    assert frozen_intent["approval_fact_ref"].startswith("crp://session/")
    provider_request = provider.calls[0]
    assert provider_request["intent_ref"] == intent_ref
    assert provider_request["capability_id"] == frozen_intent["capability_id"]
    assert provider_request["capability_version"] == frozen_intent["capability_version"]
    assert provider_request["authorization_facts_ref"] == frozen_intent["authorization_facts_ref"]
    assert provider_request["authorization_facts_revision"] == frozen_intent["authorization_facts_revision"]
    assert provider_request["approval_fact_ref"] == frozen_intent["approval_fact_ref"]


def test_approval_fact_gap_replays_same_action_binding_after_restart(tmp_path):
    db = tmp_path / "approval-gap.sqlite3"; provider = _Provider(); boundary = _Boundary()
    definition = CapabilityDefinition("document.draft", 1, "write", True, "receipt_required", "crp://default/in", "crp://default/out")
    first, store = _runtime(db, provider, boundary, definition=definition)
    request = _request(); request["capability_policy"] = {"allowed": ["document.draft"], "denied": [], "require_approval": ["document.draft"]}
    waiting = first.submit_turn(request); approval_event = tuple(first.events_after(waiting.turn_id))[-1]
    action = {"schema_version": "1.0.0", "action_id": "action-0123456789abcdef0123456789abcdef", "turn_id": waiting.turn_id, "type": "approve", "target_event_id": approval_event["event_id"], "reason": "approved", "actor": "user", "expected_sequence": waiting.current_sequence, "idempotency_key": "approval-gap-0001", "created_at": "2026-08-23T06:00:00Z"}
    first._frozen_authorization = _CrashOnceAfterApprovalFact(first._frozen_authorization)

    with pytest.raises(SystemExit, match="after approval fact"):
        first.apply_action(action)

    assert tuple(store.events_after(waiting.turn_id))[-1]["type"] == "approval.required"
    restarted, _ = _runtime(db, provider, boundary, definition=definition)
    assert restarted.apply_action(action).status == "completed"
    assert len(provider.calls) == 1


def test_approval_bundle_commit_recovers_before_first_dispatch(tmp_path):
    db = tmp_path / "approval-bundle-gap.sqlite3"; provider = _Provider(); boundary = _Boundary()
    definition = CapabilityDefinition("document.draft", 1, "write", True, "receipt_required", "crp://default/in", "crp://default/out")
    first, _ = _runtime(db, provider, boundary, definition=definition)
    request = _request(); request["capability_policy"] = {"allowed": ["document.draft"], "denied": [], "require_approval": ["document.draft"]}
    waiting = first.submit_turn(request); approval_event = tuple(first.events_after(waiting.turn_id))[-1]
    action = {"schema_version": "1.0.0", "action_id": "action-0123456789abcdef0123456789abcdef", "turn_id": waiting.turn_id, "type": "approve", "target_event_id": approval_event["event_id"], "reason": "approved", "actor": "user", "expected_sequence": waiting.current_sequence, "idempotency_key": "approval-bundle-gap-0001", "created_at": "2026-08-23T06:00:00Z"}
    first._invoke_tool = lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("after approval bundle"))

    with pytest.raises(SystemExit, match="after approval bundle"):
        first.apply_action(action)

    restarted, _ = _runtime(db, provider, boundary, definition=definition)
    assert restarted.apply_action(action).status == "completed"
    assert len(provider.calls) == 1
    assert boundary.fences == 0


def test_hook_deny_after_control_plane_issue_skips_dynamic_boundary_and_provider(tmp_path):
    db = tmp_path / "deny.sqlite3"; provider = _Provider(); boundary = _Boundary()
    runtime, store = _runtime(db, provider, boundary, deny=True)
    result = runtime.submit_turn(_request())
    assert result.status == "failed" and provider.calls == []
    assert boundary.evaluations == 1 and boundary.fences == 0
    assert store.get_immutable_payload(result.turn_id, FROZEN_TOOL_AUTHORIZATION_KIND) is not None


def test_host_agent_capability_fails_closed_without_durable_authorizer(tmp_path):
    provider, boundary = _Provider(), _Boundary()
    definition = agent_capability_definition("agent.plan")
    runtime, store = _runtime(
        tmp_path / "agent-no-authorizer.sqlite3", provider, boundary,
        definition=definition,
    )

    result = runtime.submit_turn(_agent_request())

    assert result.status == "failed"
    assert provider.calls == []
    assert boundary.evaluations == 0
    assert boundary.host_sanitizations == 0
    assert store.get_immutable_payload(
        result.turn_id, FROZEN_TOOL_AUTHORIZATION_KIND,
    ) is None


def test_host_agent_capability_uses_durable_authorizer_and_local_scanner(tmp_path):
    from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
    from backend.memory_app.kernel.agent_runtime_composition import build_agent_runtime_composition

    provider, boundary, observed = _Provider(), _Boundary(), []
    definition = agent_capability_definition("agent.plan")

    def authorize(request, capability_id):
        observed.append((request["turn_id"], capability_id, request["agent_binding"]["run_id"]))
        return request["agent_binding"]

    runtime, store = _runtime(
        tmp_path / ".rebuild-data" / "ai-turns.sqlite3", provider, boundary,
        definition=definition, agent_capability_authorizer=authorize,
    )
    composition = build_agent_runtime_composition(runtime_root=tmp_path, session_store=store,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(ScopedCapabilityRegistry())))
    composition.bind_runtime(runtime)
    profile = composition.profiles.get("main.orchestrator")
    request = _agent_request()
    del request["agent_binding"]
    prepared = composition.coordinator.accept_and_register_main(request)
    assert prepared.run.run_id == "main-run-turn-0123456789abcdef0123456789abcdef"
    assert composition.store.get_run(prepared.run.run_id) == prepared.run
    assert prepared.request["agent_binding"]["profile_id"] == "main.orchestrator"
    assert prepared.request["agent_binding"]["profile_revision"] == prepared.run.profile_revision == 1
    assert prepared.request["agent_binding"]["budget_snapshot_ref"] == prepared.run.budget_snapshot_ref == (
        "crp://turn-0123456789abcdef0123456789abcdef/agent/profiles/main.orchestrator/revisions/1")
    assert prepared.run.budget_limit == profile.budget_limit
    assert composition._frozen_tool_call_limit(prepared.request) == prepared.run.budget_limit.tool_calls == 24

    result = runtime.run_accepted_turn(prepared.run.turn_id)

    assert result.status == "completed"
    assert observed == [(
        "turn-0123456789abcdef0123456789abcdef",
        "agent.plan",
        "main-run-turn-0123456789abcdef0123456789abcdef",
    )]
    assert len(provider.calls) == 1
    assert boundary.evaluations == 0
    # Candidate preparation and the final dispatch each re-scan the detached
    # arguments against the same frozen host contract.
    assert boundary.host_sanitizations == 2
    assert store.get_immutable_payload(
        result.turn_id, FROZEN_TOOL_AUTHORIZATION_KIND,
    ) is not None


def test_regular_capability_never_uses_host_agent_authorizer(tmp_path):
    provider, boundary, observed = _Provider(), _Boundary(), []
    runtime, _store = _runtime(
        tmp_path / "regular-authority.sqlite3", provider, boundary,
        agent_capability_authorizer=lambda *_args: observed.append(True),
    )

    result = runtime.submit_turn(_request())

    assert result.status == "completed"
    assert observed == []
    assert boundary.evaluations == 1
    assert boundary.host_sanitizations == 0


@pytest.mark.parametrize("mode", ["delete", "tamper"])
def test_missing_or_tampered_frozen_facts_fail_closed_before_approved_provider(tmp_path, mode):
    db = tmp_path / f"{mode}.sqlite3"; provider = _Provider(); boundary = _Boundary()
    definition = CapabilityDefinition("document.draft", 1, "write", True, "receipt_required", "crp://default/in", "crp://default/out")
    first, _ = _runtime(db, provider, boundary, definition=definition)
    request = _request(); request["capability_policy"] = {"allowed":["document.draft"],"denied":[],"require_approval":["document.draft"]}
    waiting = first.submit_turn(request); approval = tuple(first.events_after(waiting.turn_id))[-1]
    with sqlite3.connect(db) as connection:
        if mode == "delete": connection.execute("DELETE FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?", (waiting.turn_id, FROZEN_TOOL_AUTHORIZATION_KIND))
        else: connection.execute("UPDATE ai_turn_immutable_payloads SET payload_json=? WHERE turn_id=? AND kind=?", ('{}', waiting.turn_id, FROZEN_TOOL_AUTHORIZATION_KIND))
    restarted, _ = _runtime(db, provider, boundary, definition=definition)
    action = {"schema_version":"1.0.0","action_id":"action-0123456789abcdef0123456789abcdef","turn_id":waiting.turn_id,"type":"approve","target_event_id":approval["event_id"],"reason":"approved","actor":"user","expected_sequence":waiting.current_sequence,"idempotency_key":f"bad-{mode}-0001","created_at":"2026-08-23T06:00:00Z"}
    assert restarted.apply_action(action).status == "failed" and provider.calls == []


def _runtime(
    db, provider, boundary, definition=None, deny=False,
    agent_capability_authorizer=None,
):
    store = SQLiteAITurnStore(db); registry = ScopedCapabilityRegistry()
    definition = definition or CapabilityDefinition("memory.recall", 1, "read", False, "read_only", "crp://default/in", "crp://default/out")
    registry.register(definition, provider)
    snapshot = HookPolicySnapshot("hook-policy-r1", (HookHandlerManifest("pass", "pass-r1", HookEvent.PRE_TOOL_USE, 0),))
    output = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "blocked"}}) if deny else ""
    host = CodexHookHost(catalog=HookPolicyCatalog(snapshot), runner=RevisionPinnedHookRunner({("pass", "pass-r1"): lambda m, p: HookRun(0, 0, True, stdout=output, hook_id=m.hook_id)}))
    frozen = TurnFrozenAuthorizationAuthority(
        payloads=store, execution_boundary=boundary,
        agent_capability_authorizer=agent_capability_authorizer,
    )
    return SynchronousAIRuntime(planner=_Planner(definition.capability_id), registry=registry, events=store, payloads=store, state=store, execution_boundary=boundary, hook_host=host, frozen_authorization=frozen), store


def _request():
    return json.loads((__import__("pathlib").Path(__file__).resolve().parents[2] / "core-contracts/ai/fixtures/turn-request/valid-project-answer.json").read_text(encoding="utf-8"))


def _agent_request():
    request = _request()
    request["capability_policy"] = {
        "allowed": ["agent.plan"], "denied": [], "require_approval": [],
    }
    request["agent_binding"] = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "agent-run-main-frozen-auth-001",
        "role": "main",
        "profile_id": "main.orchestrator",
        "profile_revision": 1,
        "model_tier": "standard",
        "depth": 0,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-main-frozen-auth-001/budget-snapshot",
    }
    return request
