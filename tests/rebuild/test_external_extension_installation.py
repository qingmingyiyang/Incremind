from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Barrier, Thread

import pytest

from core.effect_log import (
    V2_REVISION_KEYS,
    EffectState,
    GateDecision,
    GateDecisionFact,
    build_effect_runtime,
)
from core.external_extension_runtime.artifact_evidence import (
    ImmutableQuarantineArtifactStore,
)
from core.external_extension_runtime.fact_store import (
    ExternalExtensionFactStore,
    artifact_ref,
    intent_ref,
    resolution_ref,
)
from core.external_extension_runtime.installation import (
    ExternalExtensionInstallationConflict,
    ExternalExtensionInstallationError,
    ExternalExtensionInstallationStore,
    installation_revision_ref,
)
from core.external_extension_runtime.terminal_receipts import (
    ExternalExtensionLifecycleIntent,
    ExternalExtensionTerminalReceiptConflict,
    ExternalExtensionTerminalReceiptStore,
    ExternalExtensionTerminalReceiptVerifier,
    LifecycleOutcome,
    LifecycleProbeOutcome,
    build_lifecycle_effect_intent,
    lifecycle_contract,
    register_external_extension_lifecycle_handlers,
)
from core.external_extensions import (
    ArtifactInventory,
    ResolvedSource,
    derive_review_plan,
    inspect_extension,
    parse_install_intent,
)
from core.storage_provider import SQLiteStructuredRecordStore


REVISION_A = "a" * 40
REVISION_B = "b" * 40
SHA_B = "b" * 64
_REV_SET = {
    key: ("policy-1" if key == "policy" else "not_applicable")
    for key in V2_REVISION_KEYS
}


@dataclass
class _Executor:
    health_passed: bool = True
    probe_outcomes: dict[str, LifecycleProbeOutcome] = field(default_factory=dict)
    execute_calls: list[str] = field(default_factory=list)
    probe_calls: list[str] = field(default_factory=list)

    def execute(self, intent, _effect) -> LifecycleOutcome:
        self.execute_calls.append(intent.intent_id)
        if intent.action == "health":
            return LifecycleOutcome(
                passed=self.health_passed,
                observed_checks=intent.health_checks,
            )
        return LifecycleOutcome()

    def probe(self, intent, _effect) -> LifecycleProbeOutcome:
        self.probe_calls.append(intent.intent_id)
        return self.probe_outcomes.get(
            intent.intent_id,
            LifecycleProbeOutcome(
                EffectState.UNKNOWN,
                "error:external-extension-probe-unconfigured",
            ),
        )


@dataclass(frozen=True)
class _Services:
    database: Path
    records: SQLiteStructuredRecordStore
    facts: ExternalExtensionFactStore
    runtime: object
    receipt_facts: ExternalExtensionTerminalReceiptStore
    store: ExternalExtensionInstallationStore
    executor: _Executor


@dataclass(frozen=True)
class _Intake:
    reference: str
    confirmation_ref: str | None


def _services(
    tmp_path: Path,
    *,
    owner_id: str = "extension-worker-1",
    executor: _Executor | None = None,
) -> _Services:
    database = tmp_path / "jobs.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    facts = ExternalExtensionFactStore(
        records,
        ImmutableQuarantineArtifactStore(tmp_path / "quarantine"),
    )
    runtime = build_effect_runtime(
        database,
        owner_id=owner_id,
        lease_seconds=1,
    )
    receipt_facts = ExternalExtensionTerminalReceiptStore(records)
    concrete_executor = executor or _Executor()
    register_external_extension_lifecycle_handlers(
        runtime.handlers,
        receipt_facts,
        concrete_executor,
        # This low-level receipt fixture plans Effects directly to exercise
        # receipt storage.  Production wiring supplies the command authority;
        # command-path tests cover that durable reconstruction separately.
        frozen_identity_validator=lambda _effect: None,
    )
    store = ExternalExtensionInstallationStore(
        records,
        facts,
        ExternalExtensionTerminalReceiptVerifier(runtime.log, receipt_facts),
    )
    return _Services(
        database,
        records,
        facts,
        runtime,
        receipt_facts,
        store,
        concrete_executor,
    )


def _record_intake(
    services: _Services,
    *,
    suffix: str,
    body: bytes,
    project_id: str = "project-001",
    requested_ref: str = REVISION_A,
    confirmation_id: str | None = None,
) -> _Intake:
    install = parse_install_intent(
        "安装技能 https://github.com/example/fixture",
        intent_id=f"install-intent-{suffix}",
        project_id=project_id,
        requested_ref=requested_ref,
    )
    services.facts.record_intent(
        install,
        command_id=f"record-intent-{suffix}",
    )
    operation = f"acquire-operation-{suffix}"
    source = ResolvedSource(
        "github_repository",
        "https://github.com/example/fixture",
        requested_ref,
        artifact_ref(install.intent_id, operation),
        trust_tier="untrusted",
    )
    intent_reference = intent_ref(install.intent_id)
    services.facts.record_resolution_observation(
        operation_id=operation,
        intent_reference=intent_reference,
        source=source,
    )
    services.facts.record_resolution(
        operation_id=operation,
        intent_reference=intent_reference,
        source=source,
    )
    inventory = ArtifactInventory.capture({"SKILL.md": body})
    services.facts.commit_artifact(
        operation_id=operation,
        source=source,
        inventory=inventory,
    )
    inspected = inspect_extension(inventory, source)
    review_plan = derive_review_plan(inspected.manifest)
    intake = services.facts.record_intake(
        operation_id=operation,
        resolution_reference=resolution_ref(operation),
        manifest=inspected.manifest,
        review_plan=review_plan,
    )
    confirmation = None
    if review_plan.confirmation_ids:
        confirmation = services.store.confirm_review(
            intake,
            confirmation_id=confirmation_id or f"review-confirmation-{suffix}",
            confirmation_ids=review_plan.confirmation_ids,
            actor="local-operator",
            reason="Reviewed the exact artifact and frozen review plan.",
        )
    return _Intake(intake, confirmation)


def _skill_body(description: str) -> bytes:
    return (
        "---\n"
        "name: fixture-skill\n"
        f"description: {description}\n"
        "trigger_boundary: explicit_or_semantic\n"
        "validation: deterministic_fixture\n"
        "maturity: stable\n"
        "---\n"
        f"Use the fixture for {description}.\n"
    ).encode()


def _gate(decision_id: str) -> GateDecisionFact:
    return GateDecisionFact(
        GateDecision.ALLOW,
        "crp://rules/extensions/lifecycle",
        f"crp://scopes/extensions/{decision_id}",
        {"network_bytes": 0},
        "scope:secret/none",
        "policy-1",
    )


def _effect_intent(
    services: _Services,
    revision_reference: str,
    *,
    action: str,
    intent_id: str,
):
    lifecycle_intent = services.store.build_lifecycle_intent(
        revision_reference,
        action=action,
        intent_id=intent_id,
    )
    services.receipt_facts.record_intent(lifecycle_intent)
    effect_intent = build_lifecycle_effect_intent(
        lifecycle_intent,
        session_id="session-extension-001",
        step_key=f"{action}-{intent_id}",
        gate_decision_id=f"gate-{intent_id}",
        rev_set=_REV_SET,
    )
    return lifecycle_intent, effect_intent


def _execute(
    services: _Services,
    revision_reference: str,
    *,
    action: str,
    intent_id: str,
    now: int,
):
    _lifecycle, effect_intent = _effect_intent(
        services,
        revision_reference,
        action=action,
        intent_id=intent_id,
    )
    return services.runtime.execute_v2(
        effect_intent,
        gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_gate(effect_intent.gate_decision_id),
        now=now,
    )


def _execute_explicit_intent(
    services: _Services,
    lifecycle_intent: ExternalExtensionLifecycleIntent,
    *,
    now: int,
):
    services.receipt_facts.record_intent(lifecycle_intent)
    effect_intent = build_lifecycle_effect_intent(
        lifecycle_intent,
        session_id="session-extension-001",
        step_key=f"explicit-{lifecycle_intent.intent_id}",
        gate_decision_id=f"gate-{lifecycle_intent.intent_id}",
        rev_set=_REV_SET,
    )
    return services.runtime.execute_v2(
        effect_intent,
        gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_gate(effect_intent.gate_decision_id),
        now=now,
    )


def _overwrite_record(
    records: SQLiteStructuredRecordStore,
    collection: str,
    object_id: str,
    **changes: object,
) -> None:
    with records.begin() as uow:
        current = uow.read(collection, object_id)
        assert current is not None
        payload = dict(current.payload)
        payload.update(changes)
        uow.put(
            collection,
            object_id,
            payload,
            expected_revision=current.revision,
        )
        uow.commit()


def _install_one(services: _Services, intake: _Intake):
    installed = services.store.install_disabled(
        intake.reference,
        command_id="install-command-1001",
        expected_state_revision=0,
        review_confirmation_ref=intake.confirmation_ref,
    )
    revision = services.store.load_revision(
        installation_revision_ref("project-001", "fixture-skill", 1)
    )
    return installed, revision


def _health_and_activate(
    services: _Services,
    revision,
    snapshot,
    *,
    suffix: str,
    now: int,
):
    health_effect = _execute(
        services,
        revision.revision_ref,
        action="health",
        intent_id=f"health-intent-{suffix}",
        now=now,
    )
    healthy = services.store.record_health(
        revision.revision_ref,
        effect_operation_id=health_effect.operation_id,
        command_id=f"health-command-{suffix}",
        expected_state_revision=snapshot.state_revision,
    )
    activation_effect = _execute(
        services,
        revision.revision_ref,
        action="activation",
        intent_id=f"activation-intent-{suffix}",
        now=now + 1,
    )
    active = services.store.finalize_activation(
        revision.revision_ref,
        effect_operation_id=activation_effect.operation_id,
        command_id=f"activation-command-{suffix}",
        expected_state_revision=healthy.state_revision,
    )
    return healthy, active, activation_effect


def test_full_lifecycle_upgrade_rollback_restart_and_command_replay(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    intake_one = _record_intake(
        services,
        suffix="1001",
        body=_skill_body("first revision"),
    )
    installed_one, one = _install_one(services, intake_one)
    _healthy_one, active_one, _activation_one = _health_and_activate(
        services,
        one,
        installed_one,
        suffix="1001",
        now=1,
    )

    intake_two = _record_intake(
        services,
        suffix="2001",
        body=_skill_body("second revision with different content"),
        requested_ref=REVISION_B,
    )
    candidate_two = services.store.install_disabled(
        intake_two.reference,
        command_id="install-command-2001",
        expected_state_revision=active_one.state_revision,
        review_confirmation_ref=intake_two.confirmation_ref,
    )
    two = services.store.load_revision(
        installation_revision_ref("project-001", "fixture-skill", 2)
    )
    assert two.artifact_content_sha256 != one.artifact_content_sha256
    assert candidate_two.active_revision == 1
    assert candidate_two.candidate_revision == 2

    _healthy_two, active_two, activation_two = _health_and_activate(
        services,
        two,
        candidate_two,
        suffix="2001",
        now=3,
    )
    history = services.store.revision_history(
        "fixture-skill", root_id="project-001",
    )
    assert [item.revision for item in history] == [2, 1]
    assert [item.health_verified for item in history] == [True, True]
    assert [item.active for item in history] == [True, False]
    assert all(not hasattr(item, "artifact_ref") for item in history)
    with pytest.raises(ExternalExtensionInstallationError):
        services.store.revision_history(
            "fixture-skill", root_id="project-foreign",
        )
    rollback_effect = _execute(
        services,
        one.revision_ref,
        action="rollback",
        intent_id="rollback-intent-1001",
        now=5,
    )
    rolled_back = services.store.finalize_rollback(
        "fixture-skill",
        target_revision_reference=one.revision_ref,
        effect_operation_id=rollback_effect.operation_id,
        command_id="rollback-command-1001",
        expected_state_revision=active_two.state_revision,
    )
    assert rolled_back.active_revision == 1

    assert services.store.finalize_activation(
        two.revision_ref,
        effect_operation_id=activation_two.operation_id,
        command_id="activation-command-2001",
        expected_state_revision=active_two.state_revision - 1,
    ) == active_two

    disable_effect = _execute(
        services,
        one.revision_ref,
        action="disable",
        intent_id="disable-intent-1001",
        now=6,
    )
    disabled = services.store.finalize_disable(
        one.revision_ref,
        effect_operation_id=disable_effect.operation_id,
        command_id="disable-command-1001",
        expected_state_revision=rolled_back.state_revision,
    )
    assert disabled.active_revision is None

    restarted = _services(tmp_path, owner_id="extension-worker-2")
    third = restarted.store.install_disabled(
        intake_one.reference,
        command_id="install-command-3001",
        expected_state_revision=disabled.state_revision,
        review_confirmation_ref=intake_one.confirmation_ref,
    )
    assert third.latest_allocated_revision == 3
    assert third.candidate_revision == 3


def test_review_confirmation_exact_binding_and_project_root_fence(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    one = _record_intake(
        services,
        suffix="1001",
        body=_skill_body("first review"),
    )
    two = _record_intake(
        services,
        suffix="2001",
        body=_skill_body("second review"),
        requested_ref=REVISION_B,
    )
    with pytest.raises(ExternalExtensionInstallationError):
        services.store.install_disabled(
            one.reference,
            command_id="missing-confirmation-command",
            expected_state_revision=0,
        )
    with pytest.raises(ExternalExtensionInstallationConflict):
        services.store.install_disabled(
            two.reference,
            command_id="wrong-confirmation-command",
            expected_state_revision=0,
            review_confirmation_ref=one.confirmation_ref,
        )
    installed, _revision = _install_one(services, one)

    another_root = _record_intake(
        services,
        suffix="3001",
        body=_skill_body("different project"),
        project_id="project-002",
        requested_ref=REVISION_B,
    )
    with pytest.raises(ExternalExtensionInstallationConflict):
        services.store.install_disabled(
            another_root.reference,
            command_id="cross-project-install-command",
            expected_state_revision=installed.state_revision,
            review_confirmation_ref=another_root.confirmation_ref,
        )


def test_failed_health_stays_disabled_and_cannot_form_activation_work(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path, executor=_Executor(health_passed=False))
    intake = _record_intake(
        services,
        suffix="1001",
        body=_skill_body("failed health"),
    )
    installed, revision = _install_one(services, intake)
    health_effect = _execute(
        services,
        revision.revision_ref,
        action="health",
        intent_id="health-intent-failed-1001",
        now=1,
    )
    failed = services.store.record_health(
        revision.revision_ref,
        effect_operation_id=health_effect.operation_id,
        command_id="health-command-failed-1001",
        expected_state_revision=installed.state_revision,
    )
    assert failed.status == "disabled"
    assert failed.active_revision is None
    with pytest.raises(ExternalExtensionInstallationError):
        services.store.build_lifecycle_intent(
            revision.revision_ref,
            action="activation",
            intent_id="activation-intent-forbidden-1001",
        )


@pytest.mark.parametrize(
    "terminal_state",
    (
        EffectState.PLANNED,
        EffectState.INFLIGHT,
        EffectState.SETTLED_ERR,
        EffectState.UNKNOWN,
    ),
)
def test_non_success_effect_states_cannot_finalize_health(
    tmp_path: Path,
    terminal_state: EffectState,
) -> None:
    services = _services(tmp_path)
    intake = _record_intake(
        services,
        suffix="1001",
        body=_skill_body("state matrix"),
    )
    installed, revision = _install_one(services, intake)
    lifecycle, effect_intent = _effect_intent(
        services,
        revision.revision_ref,
        action="health",
        intent_id="health-state-matrix-1001",
    )
    planned, created = services.runtime.log.plan_v2(
        effect_intent,
        gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_gate(effect_intent.gate_decision_id),
        now=1,
    )
    assert created is True
    current = planned
    if terminal_state is not EffectState.PLANNED:
        current, claimed = services.runtime.runner.claim_planned(
            planned.operation_id,
            now=1,
        )
        assert claimed is True
    if terminal_state in {EffectState.SETTLED_ERR, EffectState.UNKNOWN}:
        services.executor.probe_outcomes[lifecycle.intent_id] = LifecycleProbeOutcome(
            terminal_state,
            (
                "error:external-extension-health-terminal"
                if terminal_state is EffectState.SETTLED_ERR
                else "error:external-extension-health-unknown"
            ),
        )
        outcome = services.runtime.recover_expired(now=3)
        assert len(outcome) == 1
        current = services.runtime.log.get(planned.operation_id)
    assert current.state is terminal_state
    with pytest.raises(ExternalExtensionInstallationConflict):
        services.store.record_health(
            revision.revision_ref,
            effect_operation_id=planned.operation_id,
            command_id=f"health-state-command-{terminal_state.value}",
            expected_state_revision=installed.state_revision,
        )


def test_receipt_before_settlement_recovers_after_real_runtime_restart(
    tmp_path: Path,
) -> None:
    first = _services(tmp_path, owner_id="extension-worker-first")
    intake = _record_intake(
        first,
        suffix="1001",
        body=_skill_body("receipt recovery"),
    )
    installed, revision = _install_one(first, intake)
    _lifecycle, effect_intent = _effect_intent(
        first,
        revision.revision_ref,
        action="health",
        intent_id="health-crash-after-receipt-1001",
    )
    planned, created = first.runtime.log.plan_v2(
        effect_intent,
        gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_gate(effect_intent.gate_decision_id),
        now=10,
    )
    assert created is True
    inflight, claimed = first.runtime.runner.claim_planned(
        planned.operation_id,
        now=10,
    )
    assert claimed is True
    registration = first.runtime.handlers.resolve(effect_intent)
    handler_receipt = registration.handler(inflight)
    assert first.runtime.log.get(inflight.operation_id).state is EffectState.INFLIGHT

    second_executor = _Executor()
    second = _services(
        tmp_path,
        owner_id="extension-worker-second",
        executor=second_executor,
    )
    outcomes = second.runtime.recover_expired(now=12)
    assert [(item.state, item.reason) for item in outcomes] == [
        (EffectState.SETTLED_OK, "probe_resolved")
    ]
    restored = second.runtime.log.get(inflight.operation_id)
    assert restored.result_ref == handler_receipt.receipt_ref
    assert second_executor.probe_calls == []
    healthy = second.store.record_health(
        revision.revision_ref,
        effect_operation_id=inflight.operation_id,
        command_id="health-recovered-command-1001",
        expected_state_revision=installed.state_revision,
    )
    assert healthy.status == "health_verified_disabled"


def test_queryable_probe_can_reconstruct_missing_terminal_receipt(
    tmp_path: Path,
) -> None:
    first = _services(tmp_path, owner_id="extension-worker-first")
    intake = _record_intake(
        first,
        suffix="1001",
        body=_skill_body("probe recovery"),
    )
    installed, revision = _install_one(first, intake)
    lifecycle, effect_intent = _effect_intent(
        first,
        revision.revision_ref,
        action="health",
        intent_id="health-crash-before-receipt-1001",
    )
    planned, _created = first.runtime.log.plan_v2(
        effect_intent,
        gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_gate(effect_intent.gate_decision_id),
        now=10,
    )
    inflight, claimed = first.runtime.runner.claim_planned(
        planned.operation_id,
        now=10,
    )
    assert claimed is True
    assert first.receipt_facts.find_receipt(inflight.operation_id) is None

    probe_executor = _Executor()
    probe_executor.probe_outcomes[lifecycle.intent_id] = LifecycleProbeOutcome(
        EffectState.SETTLED_OK,
        "facts:external-extension-probes/completed-1001",
        passed=True,
        observed_checks=lifecycle.health_checks,
    )
    second = _services(
        tmp_path,
        owner_id="extension-worker-second",
        executor=probe_executor,
    )
    outcomes = second.runtime.recover_expired(now=12)
    assert [item.state for item in outcomes] == [EffectState.SETTLED_OK]
    assert probe_executor.execute_calls == []
    assert probe_executor.probe_calls == [lifecycle.intent_id]
    receipt = second.receipt_facts.load_receipt(inflight.operation_id)
    assert receipt.passed is True
    assert second.store.record_health(
        revision.revision_ref,
        effect_operation_id=inflight.operation_id,
        command_id="health-probe-recovered-command-1001",
        expected_state_revision=installed.state_revision,
    ).status == "health_verified_disabled"


@pytest.mark.parametrize(
    "changes",
    (
        {"root_id": "project-999"},
        {"revision_ref": "crp://external-extension-installation-revisions/project-001~fixture-skill~999"},
        {"intake_ref": "crp://external-extension-intakes/intake-forged-999"},
        {"artifact_ref": "crp://external-extension-artifacts/artifact-forged-999"},
        {"artifact_content_sha256": SHA_B},
        {"review_plan_identity": SHA_B},
        {"health_plan_identity": SHA_B},
        {"action": "activation", "health_checks": ()},
    ),
)
def test_terminal_receipt_must_bind_authoritative_revision(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    services = _services(tmp_path)
    intake = _record_intake(
        services,
        suffix="1001",
        body=_skill_body("binding matrix"),
    )
    installed, revision = _install_one(services, intake)
    base = services.store.build_lifecycle_intent(
        revision.revision_ref,
        action="health",
        intent_id="health-binding-base-1001",
    )
    forged = replace(
        base,
        intent_id="health-binding-forged-1001",
        **changes,
    )
    effect = _execute_explicit_intent(services, forged, now=1)
    with pytest.raises(ExternalExtensionInstallationConflict):
        services.store.record_health(
            revision.revision_ref,
            effect_operation_id=effect.operation_id,
            command_id="health-binding-command-1001",
            expected_state_revision=installed.state_revision,
        )


def test_lifecycle_effect_contract_is_closed_and_payload_is_reference_only(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    intake = _record_intake(
        services,
        suffix="1001",
        body=_skill_body("fixed contract"),
    )
    _installed, revision = _install_one(services, intake)
    lifecycle, effect_intent = _effect_intent(
        services,
        revision.revision_ref,
        action="health",
        intent_id="health-fixed-contract-1001",
    )
    contract = lifecycle_contract("health")
    assert effect_intent.kind == contract.effect_kind
    assert effect_intent.expected_receipt_kind == contract.receipt_kind
    assert effect_intent.expected_receipt_schema_version == contract.receipt_schema
    assert effect_intent.payload["kind"] == "health"
    assert effect_intent.payload["health_check_ids"] == list(lifecycle.health_checks)
    assert effect_intent.payload["manifest_digest"] == lifecycle.manifest_identity
    assert effect_intent.payload["activation_plan_digest"] == lifecycle.activation_plan_identity
    assert "action" not in effect_intent.payload
    assert "health_checks" not in effect_intent.payload

    with pytest.raises(ValueError):
        services.runtime.execute_v2(
            replace(
                effect_intent,
                expected_receipt_schema_version=lifecycle_contract(
                    "activation"
                ).receipt_schema,
            ),
            gate_decision_id=effect_intent.gate_decision_id,
            gate_fact=_gate(effect_intent.gate_decision_id),
            now=1,
        )

    wrong_kind = replace(
        effect_intent,
        kind=lifecycle_contract("activation").effect_kind,
        expected_receipt_kind=lifecycle_contract("activation").receipt_kind,
        expected_receipt_schema_version=lifecycle_contract(
            "activation"
        ).receipt_schema,
    )
    with pytest.raises(ExternalExtensionTerminalReceiptConflict):
        services.runtime.execute_v2(
            wrong_kind,
            gate_decision_id=wrong_kind.gate_decision_id,
            gate_fact=_gate(wrong_kind.gate_decision_id),
            now=1,
        )


def test_installation_revision_freezes_host_owned_skill_activation_plan(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    intake = _record_intake(
        services,
        suffix="plan-1001",
        body=_skill_body("activation plan"),
    )
    _installed, revision = _install_one(services, intake)

    plan = services.store.load_activation_plan(revision.revision_ref)

    assert plan.root_id == "project-001"
    assert plan.extension_id == "fixture-skill"
    assert plan.is_pure_application_skill is True
    assert plan.identity == revision.activation_plan_identity
    assert plan.manifest_identity == revision.manifest_identity
    assert [
        (
            binding.skill_id,
            binding.source_path,
            binding.package_layout,
            binding.allowed_consumers,
            binding.priority,
        )
        for binding in plan.skill_bindings
    ] == [
        (
            "fixture-skill",
            "SKILL.md",
            "package_directory",
            (
                "answer.model-request",
                "document.generate",
                "turn.workbench-question",
            ),
            500,
        )
    ]

    _overwrite_record(
        services.records,
        "external_extension_installation_revisions",
        "project-001~fixture-skill~1",
        activation_plan_identity=SHA_B,
    )
    with pytest.raises(ExternalExtensionInstallationConflict):
        services.store.load_activation_plan(revision.revision_ref)


def test_health_projection_keeps_only_refs_and_tampering_blocks_activation(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    intake = _record_intake(
        services,
        suffix="1001",
        body=_skill_body("projection integrity"),
    )
    installed, revision = _install_one(services, intake)
    health_effect = _execute(
        services,
        revision.revision_ref,
        action="health",
        intent_id="health-projection-1001",
        now=1,
    )
    healthy = services.store.record_health(
        revision.revision_ref,
        effect_operation_id=health_effect.operation_id,
        command_id="health-projection-command-1001",
        expected_state_revision=installed.state_revision,
    )
    projection = services.records.read(
        "external_extension_health_projections",
        "project-001~fixture-skill~1",
    )
    assert projection is not None
    assert set(projection.payload) == {
        "schema_version",
        "revision_ref",
        "receipt_ref",
    }
    activation_effect = _execute(
        services,
        revision.revision_ref,
        action="activation",
        intent_id="activation-before-tamper-1001",
        now=2,
    )
    _overwrite_record(
        services.records,
        "external_extension_health_projections",
        "project-001~fixture-skill~1",
        passed=True,
    )
    with pytest.raises(ExternalExtensionInstallationConflict):
        services.store.finalize_activation(
            revision.revision_ref,
            effect_operation_id=activation_effect.operation_id,
            command_id="activation-after-tamper-command-1001",
            expected_state_revision=healthy.state_revision,
        )


def test_dangling_state_pointer_and_confirmation_drift_are_rejected(
    tmp_path: Path,
) -> None:
    pointer_root = tmp_path / "pointer"
    pointer_services = _services(pointer_root)
    pointer_intake = _record_intake(
        pointer_services,
        suffix="1001",
        body=_skill_body("pointer integrity"),
    )
    _installed, _revision = _install_one(pointer_services, pointer_intake)
    _overwrite_record(
        pointer_services.records,
        "external_extension_installations",
        "project-001~fixture-skill",
        latest_allocated_revision=2,
    )
    with pytest.raises(ExternalExtensionInstallationError):
        pointer_services.store.load("fixture-skill")

    confirmation_root = tmp_path / "confirmation"
    confirmation_services = _services(confirmation_root)
    confirmation_intake = _record_intake(
        confirmation_services,
        suffix="1001",
        body=_skill_body("confirmation integrity"),
    )
    _installed, revision = _install_one(
        confirmation_services,
        confirmation_intake,
    )
    assert confirmation_intake.confirmation_ref is not None
    confirmation_id = confirmation_intake.confirmation_ref.rsplit("/", 1)[-1]
    _overwrite_record(
        confirmation_services.records,
        "external_extension_review_confirmations",
        confirmation_id,
        root_id="project-999",
    )
    with pytest.raises(ExternalExtensionInstallationConflict):
        confirmation_services.store.load_revision(revision.revision_ref)


def test_terminal_receipt_tampering_is_not_accepted_as_health_evidence(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    intake = _record_intake(
        services,
        suffix="1001",
        body=_skill_body("receipt integrity"),
    )
    installed, revision = _install_one(services, intake)
    health_effect = _execute(
        services,
        revision.revision_ref,
        action="health",
        intent_id="health-receipt-tamper-1001",
        now=1,
    )
    _overwrite_record(
        services.records,
        "external_extension_terminal_receipts",
        health_effect.operation_id,
        artifact_content_sha256=SHA_B,
    )
    with pytest.raises(ExternalExtensionInstallationConflict):
        services.store.record_health(
            revision.revision_ref,
            effect_operation_id=health_effect.operation_id,
            command_id="health-receipt-tamper-command-1001",
            expected_state_revision=installed.state_revision,
        )


def test_two_independent_stores_allocate_only_one_first_revision(
    tmp_path: Path,
) -> None:
    first = _services(tmp_path, owner_id="extension-worker-first")
    intake = _record_intake(
        first,
        suffix="1001",
        body=_skill_body("concurrent install"),
    )
    second = _services(tmp_path, owner_id="extension-worker-second")
    barrier = Barrier(2)
    outcomes: list[object] = []
    errors: list[BaseException] = []

    def install(services: _Services, command_id: str) -> None:
        barrier.wait()
        try:
            outcomes.append(
                services.store.install_disabled(
                    intake.reference,
                    command_id=command_id,
                    expected_state_revision=0,
                    review_confirmation_ref=intake.confirmation_ref,
                )
            )
        except BaseException as error:  # captured for exact concurrency assertion
            errors.append(error)

    workers = (
        Thread(
            target=install,
            args=(first, "concurrent-install-command-1001"),
        ),
        Thread(
            target=install,
            args=(second, "concurrent-install-command-2001"),
        ),
    )
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert all(not worker.is_alive() for worker in workers)
    assert len(outcomes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ExternalExtensionInstallationConflict)
    snapshot = first.store.load("fixture-skill")
    assert snapshot.latest_allocated_revision == 1
    assert snapshot.candidate_revision == 1


def test_same_extension_isolated_by_project_root_for_state_and_command_replay(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    first_intake = _record_intake(
        services,
        suffix="root-one-1001",
        body=_skill_body("root one"),
        project_id="project-001",
    )
    second_intake = _record_intake(
        services,
        suffix="root-two-2001",
        body=_skill_body("root two"),
        project_id="project-002",
    )

    first_installed = services.store.install_disabled(
        first_intake.reference,
        command_id="shared-install-command",
        expected_state_revision=0,
        review_confirmation_ref=first_intake.confirmation_ref,
    )
    second_installed = services.store.install_disabled(
        second_intake.reference,
        command_id="shared-install-command",
        expected_state_revision=0,
        review_confirmation_ref=second_intake.confirmation_ref,
    )
    assert services.store.install_disabled(
        first_intake.reference,
        command_id="shared-install-command",
        expected_state_revision=0,
        review_confirmation_ref=first_intake.confirmation_ref,
    ) == first_installed

    first = services.store.load_revision(
        installation_revision_ref("project-001", "fixture-skill", 1)
    )
    second = services.store.load_revision(
        installation_revision_ref("project-002", "fixture-skill", 1)
    )
    assert first.revision_ref != second.revision_ref
    assert services.store.load("fixture-skill", root_id="project-001") == first_installed
    assert services.store.load("fixture-skill", root_id="project-002") == second_installed
    with pytest.raises(ExternalExtensionInstallationError):
        services.store.load("fixture-skill")

    _healthy_one, active_one, _ = _health_and_activate(
        services, first, first_installed, suffix="root-one-1001", now=1,
    )
    _healthy_two, active_two, _ = _health_and_activate(
        services, second, second_installed, suffix="root-two-2001", now=3,
    )
    first_disable = _execute(
        services,
        first.revision_ref,
        action="disable",
        intent_id="disable-intent-root-one-1001",
        now=5,
    )
    first_disabled = services.store.finalize_disable(
        first.revision_ref,
        effect_operation_id=first_disable.operation_id,
        command_id="shared-disable-command",
        expected_state_revision=active_one.state_revision,
    )
    second_disable = _execute(
        services,
        second.revision_ref,
        action="disable",
        intent_id="disable-intent-root-two-2001",
        now=6,
    )
    second_disabled = services.store.finalize_disable(
        second.revision_ref,
        effect_operation_id=second_disable.operation_id,
        command_id="shared-disable-command",
        expected_state_revision=active_two.state_revision,
    )
    assert services.store.finalize_disable(
        first.revision_ref,
        effect_operation_id=first_disable.operation_id,
        command_id="shared-disable-command",
        expected_state_revision=active_one.state_revision,
    ) == first_disabled
    assert first_disabled.active_revision is None
    assert second_disabled.active_revision is None
    assert services.store.active_revisions(root_id="project-001") == ()
    assert services.store.active_revisions(root_id="project-002") == ()


def test_repository_keys_preserve_legacy_ids_and_encode_only_previously_unwritable_ids(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    root_id = "tenant:" + ("p" * 150)
    confirmation_id = "review:" + ("c" * 150)
    intake = _record_intake(
        services,
        suffix="wide-storage-1001",
        body=_skill_body("wide storage identity"),
        project_id=root_id,
        confirmation_id=confirmation_id,
    )
    installed = services.store.install_disabled(
        intake.reference,
        command_id="install-" + ("x" * 119),
        expected_state_revision=0,
        review_confirmation_ref=intake.confirmation_ref,
    )
    assert installed.root_id == root_id
    assert installed.candidate_revision_ref is not None
    assert installed.candidate_revision_ref.startswith(
        "crp://external-extension-installation-revisions/r-"
    )
    revision = services.store.load_revision(installed.candidate_revision_ref)
    assert revision.root_id == root_id

    governed_collections = {
        "external_extension_installations",
        "external_extension_installation_revisions",
        "external_extension_review_confirmations",
        "external_extension_installation_commands",
    }
    governed_ids = [
        record.object_id
        for record in services.records.list_all()
        if record.collection in governed_collections
    ]
    assert governed_ids
    assert all(
        len(object_id) <= 128
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]*", object_id)
        for object_id in governed_ids
    )

    restarted = _services(tmp_path, owner_id="extension-worker-restarted")
    assert restarted.store.load("fixture-skill", root_id=root_id) == installed
    assert restarted.store.load_revision(revision.revision_ref) == revision

    # The compatibility encoder must not relocate already-writable legacy
    # records.  Existing installations remain reachable at their old keys.
    legacy = _record_intake(
        restarted,
        suffix="legacy-storage-2001",
        body=_skill_body("legacy storage identity"),
        project_id="project-legacy",
        requested_ref=REVISION_B,
    )
    restarted.store.install_disabled(
        legacy.reference,
        command_id="legacy-install-command",
        expected_state_revision=0,
        review_confirmation_ref=legacy.confirmation_ref,
    )
    assert restarted.records.read(
        "external_extension_installations",
        "project-legacy~fixture-skill",
    ) is not None
