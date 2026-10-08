"""原边界引擎只接受当次宿主证明，不把调用方声明当权限。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from backend.security.ai_tool_execution_boundary import AIToolExecutionBoundary
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.turn_boundary_adapter import TurnBoundaryRequestFactory
from core.ai_boundary import BoundaryGrant
from core.ai_kernel import (
    InMemoryTurnEventStore, InMemoryTurnPayloadStore, InMemoryTurnStateStore,
    ScopedCapabilityRegistry, SynchronousAIRuntime,
)
from core.ai_kernel.contracts import AIKernelContractError
from core.ai_kernel.dispatcher import SynchronousToolDispatcher, ToolDispatchRequest
from tests.backend.unit.security.test_turn_boundary_adapter import _capability, _remote_turn, _sanitize
from tests.rebuild.test_external_execution_manifest import CAPABILITY_ID, external_capability
from tests.rebuild.test_ai_kernel_dispatcher import _Observer, _Provider


def task():
    request = _remote_turn(CAPABILITY_ID)
    request["desired_outcome"] = "project.task"
    request["capability_policy"] = {"allowed": [CAPABILITY_ID], "denied": [], "require_approval": []}
    request["capability_request"] = {
        "mode": "execute_exact_v1", "capability_id": CAPABILITY_ID,
        "arguments": {"binding_ref": f"crp://session/{request['turn_id']}/external-task-run-v1"},
    }
    request["execution_policy"] = {
        "template_version": 2, "purpose": "primary",
        "budget": {"max_steps": 1, "planner_timeout_ms": 1_230_000},
    }
    for name in ("include_project_skill", "include_memory", "include_session_history"):
        request["context_policy"][name] = False
    return request


def grant_for(request, *, revision=1):
    return BoundaryGrant(
        grant_id="external-execution-" + request.turn_id,
        subject_id=request.actor_id,
        project_id=request.project_id,
        target_id=request.target_id,
        actions=(request.effect,),
        data_classes=request.data_classes,
        destinations=(request.destination_kind,),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        revision=revision,
        redaction_required=request.scan_state == "redacted",
    )


def profiles(root, *, mode="guarded", remote_default="review", denied_effects=(), grants=()):
    store = ProjectBoundaryProfileStore(root)
    store.update(
        "project-alpha", mode=mode, remote_default=remote_default,
        denied_effects=denied_effects, persistent_grants=grants, expected_revision=0,
    )
    return store


def evaluate(factory, *, request=None, capability=None, text="项目正文"):
    return factory.evaluate(
        task() if request is None else request,
        external_capability() if capability is None else capability,
        destination_id="provider-runtime:" + CAPABILITY_ID,
        sanitization=_sanitize(text),
    )


def test_trusted_grant_is_ephemeral_precise_and_keeps_true_external_effect(tmp_path):
    store = profiles(tmp_path)
    before = store.get("project-alpha")
    seen = []

    def authority(frozen_turn, definition, boundary_request):
        seen.append((frozen_turn, definition, boundary_request))
        return grant_for(boundary_request)

    result = evaluate(TurnBoundaryRequestFactory(store, external_execution_authority=authority))
    assert result.decision.outcome == "allow"
    assert result.decision.requires_receipt is True
    assert result.decision.matched_grant_ids == ("external-execution-" + task()["turn_id"],)
    assert result.request.effect == "external"
    assert result.request.reversible is False
    assert seen == [(task(), external_capability(), result.request)]
    assert result.profile == before
    assert store.get("project-alpha") == before


def test_wrapper_forwards_authority_and_preserves_soft_pii_redaction(tmp_path):
    result = AIToolExecutionBoundary(
        profiles(tmp_path), external_execution_authority=lambda turn, definition, request: grant_for(request),
    ).evaluate(task(), external_capability(), {"arguments": {"prompt": "联系 alice@example.com"}})
    assert result.outcome == "allow_redacted"
    assert result.redaction_required is True
    assert result.requires_receipt is True
    assert "alice@example.com" not in repr(result.arguments)
    assert "[[CRP:EMAIL:" in str(result.arguments["prompt"])


@pytest.mark.parametrize("answer", [None, True, False, {"authorized": True}])
def test_authority_boolean_or_unproved_answer_is_fixed_denial(tmp_path, answer):
    factory = TurnBoundaryRequestFactory(
        profiles(tmp_path, mode="open", remote_default="allow"),
        external_execution_authority=lambda turn, definition, request: answer,
    )
    result = evaluate(factory)
    assert result.decision.outcome == "deny"
    assert result.decision.reason_codes == ("external_execution_not_authorized",)


def test_missing_authority_cannot_use_open_profile_or_old_persistent_grant(tmp_path):
    store = profiles(tmp_path, mode="open", remote_default="allow")
    request = evaluate(TurnBoundaryRequestFactory(store)).request
    old_grant = grant_for(request)
    store.update(
        "project-alpha", mode="open", remote_default="allow", persistent_grants=(old_grant,),
        expected_revision=1,
    )
    result = evaluate(TurnBoundaryRequestFactory(store))
    assert result.decision.outcome == "deny"
    assert result.decision.reason_codes == ("external_execution_not_authorized",)
    assert store.get("project-alpha").profile.persistent_grants == (old_grant,)


def test_authority_failure_has_fixed_nonsecret_denial(tmp_path):
    def authority(*args):
        raise RuntimeError("synthetic sensitive authority diagnostic")

    result = evaluate(TurnBoundaryRequestFactory(profiles(tmp_path), external_execution_authority=authority))
    assert result.decision.outcome == "deny"
    assert result.decision.reason_codes == ("external_execution_not_authorized",)
    assert "synthetic sensitive" not in repr(result)


@pytest.mark.parametrize("field,value", [
    ("grant_id", "external-execution-another-turn"),
    ("subject_id", "another-actor"),
    ("project_id", "another-project"),
    ("target_id", "another-tool"),
    ("actions", ("external", "write")),
    ("destinations", ("provider", "local")),
    ("data_classes", ()),
    ("data_classes", ("project_content", "credential")),
    ("revision", 2),
    ("revision", True),
    ("revoked", True),
    ("expires_at", None),
    ("expires_at", datetime(2000, 1, 1, tzinfo=timezone.utc)),
    ("redaction_required", True),
])
def test_grant_cannot_expand_scope_outlive_turn_or_mismatch_snapshot(tmp_path, field, value):
    def authority(turn, definition, request):
        return replace(grant_for(request), **{field: value})

    result = evaluate(TurnBoundaryRequestFactory(profiles(tmp_path), external_execution_authority=authority))
    assert result.decision.outcome == "deny"
    assert result.decision.reason_codes == ("external_execution_not_authorized",)


@pytest.mark.parametrize("mode,remote_default,denied_effects,reason", [
    ("sealed", "allow", (), "sealed_remote_denied"),
    ("guarded", "deny", (), "profile_remote_denied"),
    ("open", "deny", (), "profile_remote_denied"),
    ("open", "allow", ("external",), "profile_explicit_deny"),
])
def test_original_hard_denies_precede_authority(tmp_path, mode, remote_default, denied_effects, reason):
    seen = []

    def authority(turn, definition, request):
        seen.append(request)
        return grant_for(request)

    result = evaluate(TurnBoundaryRequestFactory(
        profiles(tmp_path, mode=mode, remote_default=remote_default, denied_effects=denied_effects),
        external_execution_authority=authority,
    ))
    assert result.decision.outcome == "deny"
    assert result.decision.reason_codes == (reason,)
    assert seen == []


def test_sensitive_remote_input_is_denied_before_authority_without_plaintext(tmp_path):
    seen = []

    def authority(turn, definition, request):
        seen.append(request)
        return grant_for(request)

    result = AIToolExecutionBoundary(
        profiles(tmp_path), external_execution_authority=authority,
    ).evaluate(task(), external_capability(), {"arguments": {"prompt": "sk-" + "Q" * 24}})
    assert result.outcome == "deny"
    assert result.arguments == {}
    assert seen == []
    assert "Q" * 24 not in repr(result)


def test_frozen_local_only_request_cannot_be_granted(tmp_path):
    request = task()
    request["privacy"].update(mode="local_only", allow_remote=False)
    seen = []

    def authority(turn, definition, boundary_request):
        seen.append(boundary_request)
        return grant_for(boundary_request)

    result = evaluate(
        TurnBoundaryRequestFactory(profiles(tmp_path), external_execution_authority=authority),
        request=request,
    )
    assert result.decision.outcome == "deny"
    assert result.decision.reason_codes == ("turn_remote_not_allowed",)
    assert seen == []


def test_unknown_scan_cannot_be_widened_by_host_grant(tmp_path):
    factory = TurnBoundaryRequestFactory(
        profiles(tmp_path), external_execution_authority=lambda turn, definition, request: grant_for(request),
    )
    result = factory.evaluate(task(), external_capability(), destination_id="external-cli")
    assert result.decision.outcome == "deny"


@pytest.mark.parametrize("kind", ["answer", "global"])
def test_external_execution_rejects_unbound_task_scope(tmp_path, kind):
    request = task()
    if kind == "answer":
        request["desired_outcome"] = "project.answer"
    else:
        request["scope"] = {"kind": "global", "project_id": None, "series_id": None}
    factory = TurnBoundaryRequestFactory(
        profiles(tmp_path), external_execution_authority=lambda turn, definition, request: grant_for(request),
    )
    if kind == "answer":
        with pytest.raises(AIKernelContractError):
            evaluate(factory, request=request)
        return
    result = evaluate(factory, request=request)
    assert result.decision.outcome == "deny"


@pytest.mark.parametrize("kind", ["legacy", "other", "missing_scope", "version"])
def test_external_identity_cannot_fake_native_scope(tmp_path, kind):
    capability = external_capability()
    if kind == "legacy":
        capability = replace(capability, tool_definition=None)
    elif kind == "missing_scope":
        capability = replace(capability, tool_definition=replace(
            capability.tool_definition, boundary_requirements=(), execution_mode="exclusive",
        ))
    elif kind == "version":
        capability = replace(capability, version=2, tool_definition=replace(
            capability.tool_definition, version=2, execution_mode="exclusive",
        ))
    else:
        capability = replace(
            capability, capability_id="external.other.execute",
            tool_definition=replace(
                capability.tool_definition, tool_id="external.other.execute", execution_mode="exclusive",
            ),
        )
    request = task() if kind != "other" else _remote_turn(capability.capability_id)
    result = evaluate(TurnBoundaryRequestFactory(
        profiles(tmp_path), external_execution_authority=lambda turn, definition, request: grant_for(request),
    ), request=request, capability=capability)
    assert result.decision.outcome == "deny"
    assert result.decision.reason_codes == ("external_execution_contract_invalid",)


def test_revision_change_during_host_check_denies_instead_of_guessing_current_snapshot(tmp_path):
    store = profiles(tmp_path)

    def authority(turn, definition, request):
        changed = store.update("project-alpha", mode="open", remote_default="allow", expected_revision=1)
        return grant_for(request, revision=changed.profile.revision)

    result = evaluate(TurnBoundaryRequestFactory(store, external_execution_authority=authority))
    assert result.decision.outcome == "deny"
    assert result.profile.profile.revision == 1
    assert store.get("project-alpha").profile.revision == 2


@pytest.mark.parametrize("mode", ["write", "external"])
def test_ordinary_capability_never_uses_external_authority_to_erase_approval(tmp_path, mode):
    seen = []

    def authority(*args):
        seen.append(args)
        return True

    request = _remote_turn("ordinary.operation")
    result = evaluate(TurnBoundaryRequestFactory(
        profiles(tmp_path, mode="open", remote_default="allow"), external_execution_authority=authority,
    ), request=request, capability=_capability("ordinary.operation", mode=mode, approval=True))
    assert result.decision.outcome == "ask"
    assert seen == []


@pytest.mark.parametrize("approval_source", ["definition", "turn"])
def test_explicit_approval_requirement_remains_effective(tmp_path, approval_source):
    request = task()
    capability = external_capability(approval=approval_source == "definition")
    if approval_source == "turn":
        request["capability_policy"]["require_approval"].append(CAPABILITY_ID)
    factory = TurnBoundaryRequestFactory(
        profiles(tmp_path), external_execution_authority=lambda turn, definition, request: grant_for(request),
    )
    if approval_source == "turn":
        with pytest.raises(AIKernelContractError):
            evaluate(factory, request=request, capability=capability)
        return
    result = evaluate(factory, request=request, capability=capability)
    assert result.decision.outcome == "ask"
    assert result.decision.reason_codes == ("legacy_turn_approval_required",)


def test_real_kernel_uses_original_receipted_intent_without_user_approval(tmp_path):
    payloads = InMemoryTurnPayloadStore()

    class Provider:
        calls = 0

        def invoke(self, request):
            self.calls += 1
            receipt_ref = payloads.put(request["turn_id"], "external-operation", {
                "operation": "external_execute", "status": "completed",
            })
            return {"summary": "external task finished", "result": {"exit_code": 0}, "receipt_ref": receipt_ref}

    provider = Provider()
    registry = ScopedCapabilityRegistry()
    registry.register(external_capability(), provider)
    events = InMemoryTurnEventStore()
    runtime = SynchronousAIRuntime(
        planner=None, registry=registry, events=events,
        payloads=payloads, state=InMemoryTurnStateStore(),
        execution_boundary=AIToolExecutionBoundary(
            profiles(tmp_path), external_execution_authority=lambda turn, definition, request: grant_for(request),
        ),
    )
    receipt = runtime.submit_turn(task())
    assert receipt.status == "completed"
    assert provider.calls == 1
    actual = tuple(events.events_after(receipt.turn_id))
    assert "approval.required" not in {event["type"] for event in actual}
    assert sum(event["type"] == "tool.completed" for event in actual) == 1
    assert sum(event["type"] == "tool.intent.recorded" for event in actual) == 1
    completed = next(event for event in actual if event["type"] == "tool.completed")
    assert payloads.get(completed["data"]["receipt_ref"]) == {
        "operation": "external_execute", "status": "completed",
    }


def test_long_external_provider_does_not_hold_unrelated_parallel_dispatch():
    dispatcher = SynchronousToolDispatcher()
    native = external_capability().tool_definition
    external_started, external_release = Event(), Event()
    other_submitted, other_completed = Event(), Event()

    def external_provider(request):
        external_started.set()
        assert external_release.wait(timeout=10)
        return {"receipt_ref": "crp://default/external-operation"}

    def other_provider(request):
        other_completed.set()
        return {"result": {"parallel": True}}

    external_request = ToolDispatchRequest(
        provider_request={"capability_id": CAPABILITY_ID},
        execution_mode=native.execution_mode, resource_locks=native.resource_locks,
        invocation_id="tool-call-external-blocking", attempt=1, timeout_ms=native.timeout_ms,
    )
    other_request = ToolDispatchRequest(
        provider_request={"capability_id": "unrelated.read"},
        execution_mode="parallel", resource_locks=("unrelated-read",),
        invocation_id="tool-call-unrelated-read", attempt=1, timeout_ms=5_000,
    )

    def dispatch_other():
        other_submitted.set()
        return dispatcher.dispatch(_Provider(other_provider), other_request, _Observer())

    with ThreadPoolExecutor(max_workers=2) as workers:
        external = workers.submit(dispatcher.dispatch, _Provider(external_provider), external_request, _Observer())
        other = None
        try:
            assert external_started.wait(timeout=2)
            other = workers.submit(dispatch_other)
            assert other_submitted.wait(timeout=2)
            assert other_completed.wait(timeout=2)
            assert not external_release.is_set()
            assert not external.done()
            assert other.result(timeout=2) == {"result": {"parallel": True}}
        finally:
            external_release.set()
            assert external.result(timeout=3) == {"receipt_ref": "crp://default/external-operation"}
            if other is not None:
                assert other.result(timeout=3) == {"result": {"parallel": True}}
