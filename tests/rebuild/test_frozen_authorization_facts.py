from __future__ import annotations

import json
from dataclasses import replace

import pytest

from core.ai_kernel.codex_hook_parity import HookEvent, HookRun, PreToolUseBlocked, dispatch_pre_tool_use_hot_path, evaluate_hook_event
from core.ai_kernel.frozen_authorization_facts import (
    FrozenApprovalFact, FrozenAuthorizationFacts, FrozenAuthorizationFactsAuthority,
    FrozenAuthorizationFactsError, FrozenCapabilityAuthorization,
    frozen_approval_fact_from_payload, frozen_approval_fact_to_payload,
    frozen_authorization_facts_from_payload, frozen_authorization_facts_to_payload,
)


FACTS_REF = "crp://session/turn-1/frozen-authorization-facts/facts-r1"
APPROVAL_REF = "crp://session/turn-1/approvals/approval-r1"


def test_facts_payload_roundtrip_has_no_self_reference_and_restart_uses_external_ref() -> None:
    facts = _read_facts()
    payload = json.loads(json.dumps(frozen_authorization_facts_to_payload(facts)))
    assert "facts_ref" not in payload
    first = FrozenAuthorizationFactsAuthority()
    first.publish(FACTS_REF, facts)
    restarted = FrozenAuthorizationFactsAuthority()
    restored = restarted.publish(FACTS_REF, frozen_authorization_facts_from_payload(payload))
    assert restored == facts
    assert _allows_read(restarted) is True
    assert _allows_read(restarted, revision="facts-r2") is False


def test_approval_payload_roundtrip_has_no_self_reference_and_exact_binding_allows() -> None:
    authority = _write_authority()
    approval = _approval()
    payload = json.loads(json.dumps(frozen_approval_fact_to_payload(approval)))
    assert "approval_ref" not in payload
    restored = frozen_approval_fact_from_payload(payload)
    authority.publish_approval(APPROVAL_REF, restored)
    assert restored == approval
    assert authority.allows(**_write_kwargs(approval_fact=restored)) is True


@pytest.mark.parametrize("payload", [None, {}, {"schema_version": "1.0.0", "kind": "frozen-authorization-facts"}, {"schema_version": "9.0.0", "kind": "frozen-authorization-facts"}])
def test_missing_or_malformed_facts_never_become_authorization(payload: object) -> None:
    authority = FrozenAuthorizationFactsAuthority()
    assert _allows_read(authority) is False
    with pytest.raises(FrozenAuthorizationFactsError):
        frozen_authorization_facts_from_payload(payload)


def test_hook_pass_is_not_authorization_when_local_frozen_facts_deny() -> None:
    authority = _write_authority()
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [HookRun(0, 0, True, stdout="", hook_id="pass")])
    checked: list[object] = []
    dispatched: list[object] = []
    with pytest.raises(PreToolUseBlocked, match="frozen authorization"):
        dispatch_pre_tool_use_hot_path(
            outcome, {"title": "draft"},
            frozen_authorization_check=lambda value: checked.append(value) or authority.allows(**_write_kwargs(approval_fact=None)),
            dispatch=lambda value: dispatched.append(value) or {"ok": True},
        )
    assert checked == [{"title": "draft"}]
    assert dispatched == []


def test_hook_deny_short_circuits_before_local_facts_checker_and_dispatch() -> None:
    output = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "blocked"}}
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [HookRun(0, 0, True, stdout=json.dumps(output), hook_id="deny")])
    checked: list[object] = []
    dispatched: list[object] = []
    with pytest.raises(PreToolUseBlocked, match="blocked"):
        dispatch_pre_tool_use_hot_path(
            outcome, {"query": "safe"},
            frozen_authorization_check=lambda value: checked.append(value) or True,
            dispatch=lambda value: dispatched.append(value) or {"ok": True},
        )
    assert checked == []
    assert dispatched == []


@pytest.mark.parametrize("field,value", [
    ("turn_id", "turn-2"), ("tool_call_id", "tool-call-2"),
    ("capability_id", "document.publish"), ("facts_revision", "facts-r2"),
    ("tool_contract_binding", "contract-binding-r2"),
    ("action_ref", "crp://session/turn-1/actions/action-r2"), ("target_event_id", "event-r2"),
])
def test_approval_identity_drift_is_denied(field: str, value: str) -> None:
    authority = _write_authority()
    altered = replace(_approval(), **{field: value})
    if field == "facts_revision":
        with pytest.raises(FrozenAuthorizationFactsError, match="unavailable"):
            authority.publish_approval(APPROVAL_REF, altered)
        return
    authority.publish_approval(APPROVAL_REF, altered)
    assert authority.allows(**_write_kwargs(approval_fact=altered)) is False


@pytest.mark.parametrize("field,value", [
    ("approval_ref", "crp://session/turn-1/approvals/approval-r2"),
    ("revision", "approval-r2"),
    ("facts_ref", "crp://session/turn-1/frozen-authorization-facts/facts-r2"),
    ("contract_ref", "crp://default/contracts/document-draft-r2.schema.json"),
    ("contract_revision", "contract-r2"),
])
def test_approval_ref_facts_ref_or_contract_drift_is_denied(field: str, value: str) -> None:
    authority = _write_authority()
    approval = _approval()
    authority.publish_approval(APPROVAL_REF, approval)
    assert authority.allows(**{**_write_kwargs(approval_fact=approval), field: value}) is False


def test_hot_path_authority_check_uses_only_published_local_facts() -> None:
    authority = FrozenAuthorizationFactsAuthority()
    authority.publish(FACTS_REF, _read_facts())
    assert _allows_read(authority) is True
    assert len(authority._facts) == 1  # type: ignore[attr-defined]


def _read_facts(*, revision: str = "facts-r1") -> FrozenAuthorizationFacts:
    return FrozenAuthorizationFacts("facts-id-r1", revision, "turn-1", "project-1", "crp://session/turn-1/capability-manifest/r1", "crp://session/turn-1/context-manifest/r1", "capability-profile-r1", 1, "boundary-profile-r1", 1, (
        FrozenCapabilityAuthorization("memory.recall", "crp://default/contracts/memory-recall.schema.json", "contract-r1", False, "local", "read", "read_only"),
    ))


def _write_facts() -> FrozenAuthorizationFacts:
    return FrozenAuthorizationFacts("facts-id-r1", "facts-r1", "turn-1", "project-1", "crp://session/turn-1/capability-manifest/r1", "crp://session/turn-1/context-manifest/r1", "capability-profile-r1", 1, "boundary-profile-r1", 1, (
        FrozenCapabilityAuthorization("document.draft", "crp://default/contracts/document-draft.schema.json", "contract-r1", True, "provider", "write", "receipt_required"),
    ))


def _approval() -> FrozenApprovalFact:
    return FrozenApprovalFact("approval-id-r1", "approval-r1", "turn-1", "tool-call-1", "document.draft", FACTS_REF, "facts-r1", "contract-binding-r1", "crp://session/turn-1/actions/action-r1", "event-r1")


def _write_authority() -> FrozenAuthorizationFactsAuthority:
    authority = FrozenAuthorizationFactsAuthority()
    authority.publish(FACTS_REF, _write_facts())
    return authority


def _write_kwargs(*, approval_fact: FrozenApprovalFact | None) -> dict[str, object]:
    return {"facts_ref": FACTS_REF, "revision": "facts-r1", "capability_id": "document.draft", "contract_ref": "crp://default/contracts/document-draft.schema.json", "contract_revision": "contract-r1", "requires_approval": True, "destination": "provider", "effect": "write", "operation_semantics": "receipt_required", "tool_call_id": "tool-call-1", "tool_contract_binding": "contract-binding-r1", "action_ref": "crp://session/turn-1/actions/action-r1", "target_event_id": "event-r1", "approval_ref": APPROVAL_REF, "approval_fact": approval_fact}


def _allows_read(authority: FrozenAuthorizationFactsAuthority, *, revision: str = "facts-r1") -> bool:
    return authority.allows(facts_ref=FACTS_REF, revision=revision, capability_id="memory.recall", contract_ref="crp://default/contracts/memory-recall.schema.json", contract_revision="contract-r1", requires_approval=False, destination="local", effect="read", operation_semantics="read_only")
