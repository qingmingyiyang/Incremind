from __future__ import annotations

import pytest
from pathlib import Path
import sqlite3
import sys
from threading import Barrier, Thread
from types import SimpleNamespace

from core.effect_log import (
    CoordinationTaskRegistration,
    EffectRecoveryCoordinator,
    EffectState,
    GateDecision,
    GateDecisionFact,
    build_effect_runtime,
)
from core.external_extension_runtime.lifecycle_commands import (
    ExternalExtensionLifecycleCommandConflict,
    ExternalExtensionLifecycleCommandService,
    _intent_payload,
)
from core.external_extension_runtime.terminal_receipts import (
    ExternalExtensionLifecycleHandler,
    register_external_extension_lifecycle_handlers,
)
from core.external_extension_runtime.installation import (
    ExternalExtensionInstallationConflict,
)

# Reuse the established immutable-intake fixture rather than inventing a second
# lifecycle transport fixture in this command-authority test.
sys.path.insert(0, str(Path(__file__).parent))
from test_external_extension_installation import (
    LifecycleProbeOutcome,
    _install_one,
    _record_intake,
    _services,
    _skill_body,
)


def _commands(services):
    return ExternalExtensionLifecycleCommandService(
        services.records, services.store, services.receipt_facts, services.runtime,
        gate_authority=_TestGateAuthority(),
        now=lambda: 10,
    )


class _TestGateAuthority:
    """Lifecycle projection fixtures isolate Core terminal behavior from Gate IO."""

    def authorize(self, _request):
        decision_id = "gate:lifecycle-command-test"
        return SimpleNamespace(
            authorization_ref="confirmation:test",
            decision_id=decision_id,
            fact=GateDecisionFact(
                decision=GateDecision.ALLOW,
                rule_ref="rule:test-lifecycle-gate",
                scope_ref="scope:test-lifecycle-gate",
                budget_after={"network_bytes": 0},
                secret_scope="scope:secret/none",
                policy_revision="external-extension-lifecycle-policy-v1",
            ),
        )


class _CountingGateAuthority(_TestGateAuthority):
    def __init__(self) -> None:
        self.calls = 0

    def authorize(self, request):
        self.calls += 1
        return super().authorize(request)


def _authorization():
    return _TestGateAuthority().authorize(None)


def _write_legacy_intent_only(services, commands, semantic) -> tuple[str, object]:
    """Persist the pre-atomic crash shape without creating a Core Effect."""

    module = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )
    command_id = module._derived("command", semantic)
    intent_id = module._derived("intent", semantic)
    revision = services.store.load_revision(semantic["revision_ref"])
    intent = services.store.build_lifecycle_intent(
        revision.revision_ref, action=semantic["action"], intent_id=intent_id,
    )
    authorization = _authorization()
    effect_intent = commands._effect_intent(intent, semantic, authorization.decision_id)
    with services.records.begin() as uow:
        uow.put(
            "external_extension_lifecycle_reservations",
            module._reservation_id(semantic),
            commands._reservation_payload(semantic, command_id, effect_intent.operation_id),
            expected_revision=0,
        )
        uow.put(
            "external_extension_lifecycle_intents", intent_id,
            _intent_payload(intent), expected_revision=0,
        )
        uow.put(
            "external_extension_lifecycle_commands", command_id,
            {
                "schema_version": "1.0.0", "semantic": dict(semantic),
                "intent_ref": intent.intent_ref,
                "operation_id": effect_intent.operation_id,
                "gate_decision_id": authorization.decision_id,
                "authorization_ref": authorization.authorization_ref,
                "snapshot": None,
            },
            expected_revision=0,
        )
        uow.commit()
    return command_id, effect_intent


def _terminal_projection_coordinator(services, commands, *, limit=10):
    coordinator = EffectRecoveryCoordinator(services.runtime)
    coordinator.register_coordination(CoordinationTaskRegistration(
        kind="external-extension-terminal-projections",
        run=lambda: commands.reconcile_terminal_projections(limit=limit),
    ))
    return coordinator


def test_terminal_projection_scan_cursor_rotates_persistently_instead_of_starving_tail(
    tmp_path,
) -> None:
    services = _services(tmp_path)
    commands = _commands(services)
    with services.records.begin() as uow:
        for command_id in ("command-a-terminal-scan", "command-z-terminal-scan"):
            uow.put(
                "external_extension_lifecycle_commands",
                command_id,
                {"fixture": command_id},
                expected_revision=0,
            )
        uow.commit()

    first = commands._reconciliation_batch(1)
    assert [record.object_id for record in first] == ["command-a-terminal-scan"]
    commands._advance_reconciliation_cursor(first[0].object_id)

    restarted = _commands(services)
    second = restarted._reconciliation_batch(1)
    assert [record.object_id for record in second] == ["command-z-terminal-scan"]
    restarted._advance_reconciliation_cursor(second[0].object_id)
    assert [
        record.object_id for record in _commands(services)._reconciliation_batch(1)
    ] == ["command-a-terminal-scan"]


def test_corrupt_head_projection_reports_failure_but_cursor_reaches_terminal_tail(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services,
        _record_intake(services, suffix="corrupt-head", body=_skill_body("corrupt head")),
    )
    commands = _commands(services)
    original_projection = commands._record_projection
    commands._record_projection = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("crash before lifecycle projection")
    )
    with pytest.raises(RuntimeError, match="crash before lifecycle projection"):
        commands.execute(revision.revision_ref, "health", installed.state_revision)
    commands._record_projection = original_projection

    # This malformed record sorts before the valid command.  It must remain
    # observable as a coordination failure without blocking the terminal tail.
    with services.records.begin() as uow:
        uow.put(
            "external_extension_lifecycle_commands",
            "000-corrupt-terminal-projection",
            {"schema_version": "not-a-command"},
            expected_revision=0,
        )
        uow.commit()

    coordinator = _terminal_projection_coordinator(services, commands, limit=1)
    failed = coordinator.recover_once(now=11)
    assert failed.coordination_failed == ("external-extension-terminal-projections",)
    cursor = services.records.read(
        "external_extension_lifecycle_coordination", "terminal-projection-scan-v1",
    )
    assert cursor is not None
    assert cursor.payload["last_command_id"] == "000-corrupt-terminal-projection"

    recovered = coordinator.recover_once(now=12)
    assert recovered.coordination_completed == ("external-extension-terminal-projections",)
    assert services.store.load("fixture-skill", root_id="project-001").candidate_status == (
        "health_verified_disabled"
    )
    assert not [
        record for record in services.records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]


def test_health_command_owns_identity_and_replays_finalized_snapshot(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="1001", body=_skill_body("command fixture")),
    )
    commands = _commands(services)

    first = commands.execute(revision.revision_ref, "health", installed.state_revision)
    replay = commands.execute(revision.revision_ref, "health", installed.state_revision)

    assert first.completed
    assert replay.completed
    assert first.command_id == replay.command_id
    assert first.effect.operation_id == replay.effect.operation_id
    assert replay.snapshot == first.snapshot
    assert first.snapshot.candidate_status == "health_verified_disabled"
    assert services.executor.execute_calls == [first.effect.intent_ref.rsplit("/", 1)[-1]]


def test_command_rejects_semantic_state_drift_and_never_projects_inflight(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="2001", body=_skill_body("drift fixture")),
    )
    commands = _commands(services)
    result = commands.execute(revision.revision_ref, "health", installed.state_revision)
    assert result.effect.state is EffectState.SETTLED_OK
    loaded = commands.load(revision.revision_ref, "health", installed.state_revision)
    assert loaded is not None and loaded.completed

    with services.records.begin() as uow:
        command = uow.read("external_extension_lifecycle_commands", result.command_id)
        assert command is not None
        payload = dict(command.payload)
        payload["semantic"] = {**payload["semantic"], "expected_state_revision": 999}
        uow.put(
            "external_extension_lifecycle_commands", result.command_id, payload,
            expected_revision=command.revision,
        )
        uow.commit()

    with pytest.raises(ExternalExtensionLifecycleCommandConflict, match="semantic drifted"):
        commands.execute(revision.revision_ref, "health", installed.state_revision)


def test_same_semantic_command_is_single_flight_across_threads(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="3001", body=_skill_body("thread fixture")),
    )
    barrier = Barrier(3)
    results = []

    def run() -> None:
        commands = _commands(services)
        barrier.wait()
        results.append(commands.execute(revision.revision_ref, "health", installed.state_revision))

    threads = [Thread(target=run), Thread(target=run)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert {item.command_id for item in results} == {results[0].command_id}
    assert {item.effect.operation_id for item in results} == {results[0].effect.operation_id}
    assert services.executor.execute_calls == [results[0].effect.intent_ref.rsplit("/", 1)[-1]]


def test_stale_state_revision_is_rejected_before_effect_execution(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="4001", body=_skill_body("stale fixture")),
    )

    with pytest.raises(
        ExternalExtensionLifecycleCommandConflict, match="revision is stale",
    ):
        _commands(services).execute(
            revision.revision_ref, "health", installed.state_revision + 1,
        )

    assert services.executor.execute_calls == []


def test_reserved_lifecycle_state_rejects_concurrent_upgrade_before_old_handler_runs(
    tmp_path,
) -> None:
    services = _services(tmp_path)
    first_intake = _record_intake(
        services, suffix="4101", body=_skill_body("reserved revision one"),
    )
    installed, revision = _install_one(services, first_intake)
    upgrade_intake = _record_intake(
        services,
        suffix="4102",
        body=_skill_body("reserved revision two"),
        requested_ref="b" * 40,
    )
    commands = _commands(services)
    semantic = commands._semantic(
        revision.revision_ref, "health", installed.state_revision,
    )
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands",
        fromlist=["_derived"],
    )._derived("command", semantic)
    commands._ensure_intent(semantic, command_id, _authorization())

    with pytest.raises(
        ExternalExtensionInstallationConflict,
        match="reserved by a lifecycle command",
    ):
        services.store.install_disabled(
            upgrade_intake.reference,
            command_id="upgrade-after-reservation-4102",
            expected_state_revision=installed.state_revision,
            review_confirmation_ref=upgrade_intake.confirmation_ref,
        )

    assert services.store.load("fixture-skill", root_id="project-001") == installed
    assert services.executor.execute_calls == []


def test_irrelevant_malformed_reservation_does_not_block_another_installation(tmp_path) -> None:
    services = _services(tmp_path)
    first_intake = _record_intake(
        services, suffix="4111", body=_skill_body("irrelevant reservation one"),
    )
    installed, _revision = _install_one(services, first_intake)
    upgrade_intake = _record_intake(
        services,
        suffix="4112",
        body=_skill_body("irrelevant reservation two"),
        requested_ref="c" * 40,
    )
    with services.records.begin() as uow:
        uow.put(
            "external_extension_lifecycle_reservations",
            "reservation-unrelated-malformed-4112",
            {"root_id": "project-other"},
            expected_revision=0,
        )
        uow.commit()

    upgraded = services.store.install_disabled(
        upgrade_intake.reference,
        command_id="upgrade-after-unrelated-reservation-4112",
        expected_state_revision=installed.state_revision,
        review_confirmation_ref=upgrade_intake.confirmation_ref,
    )

    assert upgraded.candidate_revision == 2


def test_relevant_reservation_requires_matching_command_and_effect_projection(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="4121", body=_skill_body("linked reservation")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"]
    )._derived("command", semantic)
    commands._ensure_intent(semantic, command_id, _authorization())
    with services.records.begin() as uow:
        command = uow.read("external_extension_lifecycle_commands", command_id)
        assert command is not None
        payload = dict(command.payload)
        payload["operation_id"] = "effect-foreign-reservation-4121"
        uow.put(
            "external_extension_lifecycle_commands", command_id, payload,
            expected_revision=command.revision,
        )
        uow.commit()

    upgrade_intake = _record_intake(
        services, suffix="4122", body=_skill_body("linked reservation upgrade"), requested_ref="d" * 40,
    )
    with pytest.raises(ExternalExtensionInstallationConflict, match="reservation authority is invalid"):
        services.store.install_disabled(
            upgrade_intake.reference,
            command_id="upgrade-after-linked-reservation-4122",
            expected_state_revision=installed.state_revision,
            review_confirmation_ref=upgrade_intake.confirmation_ref,
        )


def test_terminal_error_releases_reservation_after_reaper_and_restart(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="4131", body=_skill_body("terminal error")),
    )
    first = _commands(services)
    semantic = first._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"]
    )._derived("command", semantic)
    intent = first._ensure_intent(semantic, command_id, _authorization())
    effect_intent = first._effect_intent(intent, semantic, "gate:lifecycle-command-test")
    planned, created = services.runtime.log.plan_v2(
        effect_intent,
        gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_authorization().fact,
        now=10,
    )
    assert created is False
    _inflight, claimed = services.runtime.runner.claim_planned(planned.operation_id, now=10)
    assert claimed is True
    services.executor.probe_outcomes[intent.intent_id] = LifecycleProbeOutcome(
        EffectState.SETTLED_ERR, "error:external-extension-terminal-error",
    )
    assert [item.state for item in services.runtime.recover_expired(now=12)] == [EffectState.SETTLED_ERR]

    restarted = _commands(services)
    result = restarted.load(revision.revision_ref, "health", installed.state_revision)
    assert result is not None and result.effect.state is EffectState.SETTLED_ERR
    assert not [
        record for record in services.records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]
    upgrade = _record_intake(
        services, suffix="4132", body=_skill_body("terminal error upgrade"), requested_ref="e" * 40,
    )
    assert services.store.install_disabled(
        upgrade.reference,
        command_id="upgrade-after-terminal-error-4132",
        expected_state_revision=installed.state_revision,
        review_confirmation_ref=upgrade.confirmation_ref,
    ).candidate_revision == 2


def test_unknown_reservation_releases_only_after_explicit_core_abandonment(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="4141", body=_skill_body("unknown recovery")),
    )
    first = _commands(services)
    semantic = first._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"]
    )._derived("command", semantic)
    intent = first._ensure_intent(semantic, command_id, _authorization())
    effect_intent = first._effect_intent(intent, semantic, "gate:lifecycle-command-test")
    planned, _created = services.runtime.log.plan_v2(
        effect_intent,
        gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_authorization().fact,
        now=10,
    )
    _inflight, claimed = services.runtime.runner.claim_planned(planned.operation_id, now=10)
    assert claimed is True
    services.executor.probe_outcomes[intent.intent_id] = LifecycleProbeOutcome(
        EffectState.UNKNOWN, "error:external-extension-unknown",
    )
    assert [item.state for item in services.runtime.recover_expired(now=12)] == [EffectState.UNKNOWN]

    assert first.load(revision.revision_ref, "health", installed.state_revision) is not None
    assert [
        record for record in services.records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]
    abandoned = services.runtime.runner.abandon_unknown(
        services.runtime.log.get(planned.operation_id),
        decision_ref="decision:external-extension-abandon-4141",
        now=13,
    )
    assert abandoned.state is EffectState.ABANDONED
    restarted = _commands(services)
    result = restarted.load(revision.revision_ref, "health", installed.state_revision)
    assert result is not None and result.effect.state is EffectState.ABANDONED
    assert not [
        record for record in services.records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]


def test_legacy_intent_only_crash_backfills_core_then_execute_dispatches(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="5001", body=_skill_body("intent crash")),
    )
    first = _commands(services)
    semantic = first._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id, effect_intent = _write_legacy_intent_only(services, first, semantic)
    with pytest.raises(KeyError):
        services.runtime.log.get(effect_intent.operation_id)

    restarted = _commands(services)
    assert restarted.backfill_intent_only_effects(limit=10) == 1
    assert services.runtime.log.get(effect_intent.operation_id).state is EffectState.PLANNED
    assert services.executor.execute_calls == []
    recovered = restarted.execute(
        revision.revision_ref, "health", installed.state_revision,
    )

    assert recovered.completed
    assert services.executor.execute_calls == [recovered.effect.intent_ref.rsplit("/", 1)[-1]]


def test_legacy_backfill_reauthorizes_once_before_uow_and_never_inside_lock(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="backfill-one-gate", body=_skill_body("backfill one gate")),
    )
    authority = _CountingGateAuthority()
    commands = ExternalExtensionLifecycleCommandService(
        services.records, services.store, services.receipt_facts, services.runtime,
        gate_authority=authority, now=lambda: 10,
    )
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    _command_id, effect_intent = _write_legacy_intent_only(services, commands, semantic)

    assert commands.backfill_intent_only_effects(limit=10) == 1
    assert authority.calls == 1
    assert services.runtime.log.get(effect_intent.operation_id).state is EffectState.PLANNED


def test_atomic_plan_rolls_back_command_intent_and_reservation_on_effect_failure(
    tmp_path, monkeypatch,
) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="atomic-rollback", body=_skill_body("atomic rollback")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)

    monkeypatch.setattr(
        services.runtime.log, "_plan_v2_in_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("planned effect write failed")),
    )
    with pytest.raises(RuntimeError, match="planned effect write failed"):
        commands.execute(revision.revision_ref, "health", installed.state_revision)

    assert services.records.read("external_extension_lifecycle_commands", command_id) is None
    assert not [
        record for record in services.records.list_all()
        if record.collection in {
            "external_extension_lifecycle_intents",
            "external_extension_lifecycle_reservations",
        }
    ]
    with sqlite3.connect(services.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0


def test_atomic_planned_command_is_idempotent_without_second_effect_plan(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="atomic-idempotent", body=_skill_body("atomic idempotent")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)

    first = commands._ensure_intent(semantic, command_id, _authorization())
    replay = commands._ensure_intent(semantic, command_id, _authorization())
    effect_intent = commands._effect_intent(first, semantic, _authorization().decision_id)

    assert replay == first
    assert services.runtime.log.get(effect_intent.operation_id).state is EffectState.PLANNED
    assert services.records.read("external_extension_lifecycle_commands", command_id).revision == 1
    assert services.executor.execute_calls == []


def test_legacy_backfill_reports_tamper_after_planning_valid_tail_and_advancing_cursor(
    tmp_path, caplog,
) -> None:
    services = _services(tmp_path)
    first_installed, first_revision = _install_one(
        services, _record_intake(services, suffix="backfill-tampered", body=_skill_body("tampered backfill")),
    )
    commands = _commands(services)
    first_semantic = commands._semantic(
        first_revision.revision_ref, "health", first_installed.state_revision,
    )
    first_command, first_effect = _write_legacy_intent_only(services, commands, first_semantic)
    with services.records.begin() as uow:
        record = uow.read("external_extension_lifecycle_commands", first_command)
        assert record is not None
        payload = dict(record.payload)
        payload["gate_decision_id"] = "gate:tampered-legacy-backfill"
        uow.put(
            "external_extension_lifecycle_commands", "000-tampered-legacy-backfill", payload,
            expected_revision=0,
        )
        uow.commit()

    with pytest.raises(
        ExternalExtensionLifecycleCommandConflict,
        match="intent-only backfill encountered invalid durable records",
    ):
        commands.backfill_intent_only_effects(limit=10)
    warnings = [
        entry for entry in caplog.records
        if entry.getMessage() == "external_extension_lifecycle_intent_only_backfill_invalid"
    ]
    assert len(warnings) == 1
    assert warnings[0].event == "external_extension_lifecycle_intent_only_backfill_invalid"
    assert warnings[0].command_id == "000-tampered-legacy-backfill"
    assert warnings[0].error_type == "ExternalExtensionLifecycleCommandConflict"
    assert "gate:tampered-legacy-backfill" not in caplog.text
    assert "confirmation:test" not in caplog.text
    assert services.runtime.log.get(first_effect.operation_id).state is EffectState.PLANNED
    cursor = services.records.read(
        "external_extension_lifecycle_coordination", "intent-only-backfill-scan-v1",
    )
    assert cursor is not None and cursor.payload["last_command_id"] == first_command
    assert services.executor.execute_calls == []


def test_settled_effect_projection_crash_replays_without_second_handler(
    tmp_path, monkeypatch,
) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="6001", body=_skill_body("projection crash")),
    )
    commands = _commands(services)

    def crash(*_args, **_kwargs):
        raise RuntimeError("crash-after-projection-finalization")

    monkeypatch.setattr(commands, "_record_projection", crash)
    with pytest.raises(RuntimeError, match="projection-finalization"):
        commands.execute(revision.revision_ref, "health", installed.state_revision)

    restarted = _commands(services)
    recovered = restarted.execute(
        revision.revision_ref, "health", installed.state_revision,
    )
    assert recovered.completed
    assert len(services.executor.execute_calls) == 1
    assert not [
        record for record in services.records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]


def test_command_projection_cannot_redirect_to_a_foreign_effect(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="7001", body=_skill_body("foreign effect")),
    )
    commands = _commands(services)
    result = commands.execute(revision.revision_ref, "health", installed.state_revision)
    with services.records.begin() as uow:
        record = uow.read("external_extension_lifecycle_commands", result.command_id)
        assert record is not None
        payload = dict(record.payload)
        payload["operation_id"] = "foreign-effect-operation-0001"
        uow.put(
            "external_extension_lifecycle_commands", result.command_id, payload,
            expected_revision=record.revision,
        )
        uow.commit()

    with pytest.raises(ExternalExtensionLifecycleCommandConflict, match="operation drifted"):
        commands.load(revision.revision_ref, "health", installed.state_revision)


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("identity_algorithm", "tampered-identity-algorithm"),
        ("revision_schema_version", "tampered-revision-schema"),
        ("gate_decision_id", "gate:tampered-frozen-identity"),
        ("purpose", "aux"),
        ("turn_id", "tampered-turn-identity"),
        ("parent_id", "tampered-parent-identity"),
    ),
)
def test_handler_and_probe_reject_tampered_frozen_effect_before_executor_or_receipt(
    tmp_path, column, value,
) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix=f"frozen-{column}", body=_skill_body("frozen identity")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)
    intent = commands._ensure_intent(semantic, command_id, _authorization())
    effect_intent = commands._effect_intent(intent, semantic, "gate:lifecycle-command-test")

    with sqlite3.connect(services.database) as connection:
        connection.execute(
            f"UPDATE effect SET {column}=? WHERE operation_id=?",
            (value, effect_intent.operation_id),
        )
        connection.commit()
    tampered = services.runtime.log.get(effect_intent.operation_id)
    handler = ExternalExtensionLifecycleHandler(
        services.receipt_facts,
        services.executor,
        frozen_identity_validator=commands.validate_frozen_effect_identity,
    )

    with pytest.raises(ExternalExtensionLifecycleCommandConflict, match="frozen Core authority"):
        handler(tampered)
    with pytest.raises(ExternalExtensionLifecycleCommandConflict, match="frozen Core authority"):
        handler.probe(tampered)
    assert services.executor.execute_calls == []
    assert services.executor.probe_calls == []
    assert services.receipt_facts.find_receipt(tampered.operation_id) is None


def test_handler_rejects_coherent_authority_set_drift_before_executor_or_receipt(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="frozen-authority", body=_skill_body("authority drift")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)
    intent = commands._ensure_intent(semantic, command_id, _authorization())
    effect_intent = commands._effect_intent(intent, semantic, "gate:lifecycle-command-test")
    with sqlite3.connect(services.database) as connection:
        connection.execute(
            "UPDATE effect SET rev_set=?, authority_set_id=? WHERE operation_id=?",
            ('{"policy":"tampered-policy","boundary":"not_applicable","capability":"not_applicable","handler":"not_applicable","model":"not_applicable","provider":"not_applicable"}', "auth_tampered", effect_intent.operation_id),
        )
        connection.commit()
    handler = ExternalExtensionLifecycleHandler(
        services.receipt_facts, services.executor,
        frozen_identity_validator=commands.validate_frozen_effect_identity,
    )
    with pytest.raises(ExternalExtensionLifecycleCommandConflict, match="frozen Core authority"):
        handler(services.runtime.log.get(effect_intent.operation_id))
    assert services.executor.execute_calls == []
    assert services.receipt_facts.find_receipt(effect_intent.operation_id) is None


def test_core_reaper_enters_strict_handler_and_blocks_tampered_effect_before_executor(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="frozen-reaper", body=_skill_body("reaper drift")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)
    intent = commands._ensure_intent(semantic, command_id, _authorization())
    effect_intent = commands._effect_intent(intent, semantic, "gate:lifecycle-command-test")
    with sqlite3.connect(services.database) as connection:
        connection.execute(
            "UPDATE effect SET purpose='aux' WHERE operation_id=?",
            (effect_intent.operation_id,),
        )
        connection.commit()

    reaper_runtime = build_effect_runtime(
        services.database, owner_id="frozen-identity-reaper", lease_seconds=1,
    )
    register_external_extension_lifecycle_handlers(
        reaper_runtime.handlers,
        services.receipt_facts,
        services.executor,
        frozen_identity_validator=commands.validate_frozen_effect_identity,
    )
    EffectRecoveryCoordinator(reaper_runtime).recover_once(now=10)

    assert services.executor.execute_calls == []
    assert services.executor.probe_calls == []
    assert services.receipt_facts.find_receipt(effect_intent.operation_id) is None
    assert reaper_runtime.log.get(effect_intent.operation_id).state is EffectState.INFLIGHT


def test_expired_lease_reaper_marks_tampered_lifecycle_effect_unknown_without_domain_io(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="frozen-expired", body=_skill_body("expired drift")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)
    intent = commands._ensure_intent(semantic, command_id, _authorization())
    effect_intent = commands._effect_intent(intent, semantic, "gate:lifecycle-command-test")
    _inflight, claimed = services.runtime.runner.claim_planned(effect_intent.operation_id, now=10)
    assert claimed is True
    with sqlite3.connect(services.database) as connection:
        connection.execute(
            "UPDATE effect SET identity_algorithm=? WHERE operation_id=?",
            ("tampered-expired-identity", effect_intent.operation_id),
        )
        connection.commit()

    reaper_runtime = build_effect_runtime(
        services.database, owner_id="frozen-identity-expiry-reaper", lease_seconds=1,
    )
    register_external_extension_lifecycle_handlers(
        reaper_runtime.handlers,
        services.receipt_facts,
        services.executor,
        frozen_identity_validator=commands.validate_frozen_effect_identity,
    )
    report = EffectRecoveryCoordinator(reaper_runtime).recover_once(now=12)

    assert len(report.effect_outcomes) == 1
    outcome = report.effect_outcomes[0]
    assert outcome.operation_id == effect_intent.operation_id
    assert outcome.state is EffectState.UNKNOWN
    assert outcome.reason == "probe_failed"
    assert reaper_runtime.log.get(effect_intent.operation_id).state is EffectState.UNKNOWN
    assert services.executor.execute_calls == []
    assert services.executor.probe_calls == []
    assert services.receipt_facts.find_receipt(effect_intent.operation_id) is None


@pytest.mark.parametrize("mutation", ("command_id", "schema"))
def test_handler_rejects_noncanonical_command_authority_before_domain_io(tmp_path, mutation) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix=f"command-{mutation}", body=_skill_body("command authority")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)
    intent = commands._ensure_intent(semantic, command_id, _authorization())
    effect_intent = commands._effect_intent(intent, semantic, "gate:lifecycle-command-test")
    with services.records.begin() as uow:
        record = uow.read("external_extension_lifecycle_commands", command_id)
        assert record is not None
        payload = dict(record.payload)
        if mutation == "schema":
            payload["schema_version"] = "9.9.9"
            uow.put("external_extension_lifecycle_commands", command_id, payload, expected_revision=record.revision)
        else:
            uow.delete("external_extension_lifecycle_commands", command_id, expected_revision=record.revision)
            uow.put("external_extension_lifecycle_commands", "counterfeit-command-identity", payload, expected_revision=0)
        uow.commit()
    handler = ExternalExtensionLifecycleHandler(
        services.receipt_facts, services.executor,
        frozen_identity_validator=commands.validate_frozen_effect_identity,
    )
    with pytest.raises(ExternalExtensionLifecycleCommandConflict, match="identity or schema drifted"):
        handler(services.runtime.log.get(effect_intent.operation_id))
    assert services.executor.execute_calls == []
    assert services.receipt_facts.find_receipt(effect_intent.operation_id) is None


def test_default_clock_records_real_epoch_time(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="8001", body=_skill_body("clock fixture")),
    )
    commands = ExternalExtensionLifecycleCommandService(
        services.records, services.store, services.receipt_facts, services.runtime,
        gate_authority=_TestGateAuthority(),
    )

    result = commands.execute(
        revision.revision_ref, "health", installed.state_revision,
    )

    assert result.effect.recorded_at > 1_700_000_000


def test_terminal_error_reaper_projection_reconciles_without_command_load(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="reconcile-error", body=_skill_body("reconcile error")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)
    intent = commands._ensure_intent(semantic, command_id, _authorization())
    effect_intent = commands._effect_intent(intent, semantic, "gate:lifecycle-command-test")
    planned, _created = services.runtime.log.plan_v2(
        effect_intent, gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_authorization().fact, now=10,
    )
    _inflight, claimed = services.runtime.runner.claim_planned(planned.operation_id, now=10)
    assert claimed is True
    services.executor.probe_outcomes[intent.intent_id] = LifecycleProbeOutcome(
        EffectState.SETTLED_ERR, "error:reconcile-terminal-error",
    )

    report = _terminal_projection_coordinator(services, commands).recover_once(now=12)
    assert [item.state for item in report.effect_outcomes] == [EffectState.SETTLED_ERR]
    assert report.coordination_completed == ("external-extension-terminal-projections",)
    assert not [
        record for record in services.records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]


def test_settled_ok_projection_reconciles_without_second_handler_call(tmp_path, monkeypatch) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="reconcile-ok", body=_skill_body("reconcile ok")),
    )
    commands = _commands(services)
    original_record = commands._record_projection
    monkeypatch.setattr(
        commands, "_record_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection crash")),
    )
    with pytest.raises(RuntimeError, match="projection crash"):
        commands.execute(revision.revision_ref, "health", installed.state_revision)
    monkeypatch.setattr(commands, "_record_projection", original_record)

    report = _terminal_projection_coordinator(services, commands).recover_once(now=11)
    assert report.coordination_completed == ("external-extension-terminal-projections",)
    assert len(services.executor.execute_calls) == 1
    assert not [
        record for record in services.records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]


def test_abandoned_projection_reconciles_and_concurrent_replays_are_idempotent(tmp_path) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services, _record_intake(services, suffix="reconcile-abandoned", body=_skill_body("reconcile abandoned")),
    )
    commands = _commands(services)
    semantic = commands._semantic(revision.revision_ref, "health", installed.state_revision)
    command_id = __import__(
        "core.external_extension_runtime.lifecycle_commands", fromlist=["_derived"],
    )._derived("command", semantic)
    intent = commands._ensure_intent(semantic, command_id, _authorization())
    effect_intent = commands._effect_intent(intent, semantic, "gate:lifecycle-command-test")
    planned, _created = services.runtime.log.plan_v2(
        effect_intent, gate_decision_id=effect_intent.gate_decision_id,
        gate_fact=_authorization().fact, now=10,
    )
    _inflight, claimed = services.runtime.runner.claim_planned(planned.operation_id, now=10)
    assert claimed is True
    services.executor.probe_outcomes[intent.intent_id] = LifecycleProbeOutcome(
        EffectState.UNKNOWN, "error:reconcile-unknown",
    )
    assert [item.state for item in services.runtime.recover_expired(now=12)] == [EffectState.UNKNOWN]
    services.runtime.runner.abandon_unknown(
        services.runtime.log.get(planned.operation_id),
        decision_ref="decision:reconcile-abandon", now=13,
    )

    report = _terminal_projection_coordinator(services, commands).recover_once(now=14)
    assert report.coordination_completed == ("external-extension-terminal-projections",)

    barrier = Barrier(3)
    outcomes: list[int] = []
    failures: list[BaseException] = []

    def reconcile() -> None:
        try:
            barrier.wait()
            outcomes.append(_commands(services).reconcile_terminal_projections(limit=10))
        except BaseException as error:  # asserted below; worker must surface no drift.
            failures.append(error)

    threads = [Thread(target=reconcile), Thread(target=reconcile)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(outcomes) == [1, 1]
    assert not [
        record for record in services.records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]
