from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from threading import Event, Thread
from time import monotonic, sleep

import pytest
from backend.api.app import create_app
from backend.api.effect_partition_inventory import (
    AI_TURNS_EFFECT_PARTITION,
    EFFECT_PARTITION_INVENTORY,
    PRIMARY_EFFECT_PARTITION,
    PPT_MASTER_EFFECT_PARTITION,
    register_enabled_effect_partitions,
)

from core.effect_log import (
    EffectClass,
    EffectBackfillRegistration,
    EffectHandlerRegistration,
    CoordinationTaskRegistration,
    EffectRecoveryCoordinator,
    EffectRecoveryRegistration,
    EffectRecoveryService,
    EffectIntent,
    EffectPurpose,
    EffectRuntime,
    EffectState,
    RecoveryPreparationRegistration,
    build_effect_runtime,
)
from core.external_extension_runtime.lifecycle import (
    ACQUIRE_EFFECT_KIND,
    RESOLVE_EFFECT_KIND,
)


ROOT = Path(__file__).resolve().parents[4]


def test_production_composition_has_only_core_recovery_stages(tmp_path) -> None:
    application = create_app(SimpleNamespace(root_dir=tmp_path))

    assert not hasattr(application.state.effect_recovery_coordinator, "register_legacy")
    assert application.state.effect_recovery_coordinator.backfill_kinds()
    assert application.state.effect_recovery_coordinator.preparation_kinds()
    assert "external-extension-terminal-projections" in (
        application.state.effect_recovery_coordinator.coordination_kinds()
    )
    assert "external-extension-lifecycle-intents" in (
        application.state.effect_recovery_coordinator.backfill_kinds()
    )
    assert "external-extension-resolve-intents" in (
        application.state.effect_recovery_coordinator.backfill_kinds()
    )
    assert RESOLVE_EFFECT_KIND in application.state.effect_runtime.handlers.kinds()
    assert ACQUIRE_EFFECT_KIND in application.state.effect_runtime.handlers.kinds()
    assert tuple(application.state.external_extension_runtime.__dataclass_fields__) == (
        "install_workflow",
        "active_packages",
        "active_sources",
        "_register_recovery",
    )
    assert application.state.external_extension_install_workflow is (
        application.state.external_extension_runtime.install_workflow
    )
    assert "mcp_call" in application.state.ai_effect_runtime.recoveries.kinds()
    assert (
        application.state.ai_effect_runtime.recoveries.probes()["mcp_call"]
        .__class__.__name__ == "MCPRemoteEffectRecoveryProbe"
    )
    assert application.state.effect_recovery_coordinator.partition_names() == (
        "ai-turns", "primary",
    )
    assert Path(application.state.effect_runtime.log.database) == PRIMARY_EFFECT_PARTITION.database(tmp_path)
    assert Path(application.state.ai_effect_runtime.log.database) == AI_TURNS_EFFECT_PARTITION.database(tmp_path)
    assert application.state.effect_runtime.runner.owner_id.startswith(PRIMARY_EFFECT_PARTITION.owner_prefix)
    assert application.state.effect_runtime.runner.lease_heartbeat_seconds == 10
    assert application.state.ai_effect_runtime.runner.owner_id.startswith(AI_TURNS_EFFECT_PARTITION.owner_prefix)


def test_effect_partition_inventory_is_complete_and_non_overlapping(tmp_path) -> None:
    assert tuple(spec.name for spec in EFFECT_PARTITION_INVENTORY) == (
        "primary", "ai-turns", "ppt-master",
    )
    assert len({spec.database(tmp_path) for spec in EFFECT_PARTITION_INVENTORY}) == 3
    assert PPT_MASTER_EFFECT_PARTITION.conditional_capability == "ppt_master_capability"
    assert PPT_MASTER_EFFECT_PARTITION.lease_heartbeat_seconds == 60


def test_inventory_registers_exact_enabled_runtimes_and_fails_closed_on_drift(tmp_path) -> None:
    primary = build_effect_runtime(
        PRIMARY_EFFECT_PARTITION.database(tmp_path),
        owner_id=PRIMARY_EFFECT_PARTITION.owner_id(1),
        lease_seconds=PRIMARY_EFFECT_PARTITION.lease_seconds,
    )
    ai = build_effect_runtime(
        AI_TURNS_EFFECT_PARTITION.database(tmp_path),
        owner_id=AI_TURNS_EFFECT_PARTITION.owner_id(1),
        lease_seconds=AI_TURNS_EFFECT_PARTITION.lease_seconds,
    )
    ppt = build_effect_runtime(
        PPT_MASTER_EFFECT_PARTITION.database(tmp_path),
        owner_id=PPT_MASTER_EFFECT_PARTITION.owner_id(1),
        lease_seconds=PPT_MASTER_EFFECT_PARTITION.lease_seconds,
        lease_heartbeat_seconds=PPT_MASTER_EFFECT_PARTITION.lease_heartbeat_seconds,
    )
    coordinator = EffectRecoveryCoordinator(primary)
    state = SimpleNamespace(
        effect_runtime=primary,
        ai_effect_runtime=ai,
        ppt_master_effect_runtime=ppt,
        ppt_master_capability=None,
    )

    assert register_enabled_effect_partitions(
        coordinator, state, root_dir=tmp_path,
    ) == ("primary", "ai-turns")
    assert coordinator.partition_names() == ("ai-turns", "primary")
    assert coordinator.expected_partition_names() == ("ai-turns", "primary")
    with pytest.raises(RuntimeError, match="unapproved"):
        coordinator.register_partition("ppt-master", ppt)


@pytest.mark.parametrize(
    ("database_name", "owner_id", "lease_seconds", "error"),
    (
        ("wrong.sqlite3", AI_TURNS_EFFECT_PARTITION.owner_id(1), 30, "database drifted"),
        (".rebuild-data/ai-turns.sqlite3", "wrong-owner:1", 30, "owner drifted"),
        (".rebuild-data/ai-turns.sqlite3", AI_TURNS_EFFECT_PARTITION.owner_id(1), 31, "lease drifted"),
    ),
)
def test_inventory_rejects_runtime_composition_drift(
    tmp_path, database_name, owner_id, lease_seconds, error,
) -> None:
    primary = build_effect_runtime(
        PRIMARY_EFFECT_PARTITION.database(tmp_path),
        owner_id=PRIMARY_EFFECT_PARTITION.owner_id(1),
        lease_seconds=PRIMARY_EFFECT_PARTITION.lease_seconds,
    )
    ai = build_effect_runtime(
        tmp_path / database_name, owner_id=owner_id, lease_seconds=lease_seconds,
    )
    state = SimpleNamespace(
        effect_runtime=primary, ai_effect_runtime=ai,
        ppt_master_effect_runtime=None, ppt_master_capability=None,
    )

    with pytest.raises(RuntimeError, match=error):
        register_enabled_effect_partitions(
            EffectRecoveryCoordinator(primary), state, root_dir=tmp_path,
        )


def test_inventory_rejects_conditional_partition_heartbeat_drift(tmp_path) -> None:
    primary = build_effect_runtime(
        PRIMARY_EFFECT_PARTITION.database(tmp_path),
        owner_id=PRIMARY_EFFECT_PARTITION.owner_id(1),
        lease_seconds=PRIMARY_EFFECT_PARTITION.lease_seconds,
    )
    ai = build_effect_runtime(
        AI_TURNS_EFFECT_PARTITION.database(tmp_path),
        owner_id=AI_TURNS_EFFECT_PARTITION.owner_id(1),
        lease_seconds=AI_TURNS_EFFECT_PARTITION.lease_seconds,
    )
    ppt = build_effect_runtime(
        PPT_MASTER_EFFECT_PARTITION.database(tmp_path),
        owner_id=PPT_MASTER_EFFECT_PARTITION.owner_id(1),
        lease_seconds=PPT_MASTER_EFFECT_PARTITION.lease_seconds,
        lease_heartbeat_seconds=30,
    )
    state = SimpleNamespace(
        effect_runtime=primary, ai_effect_runtime=ai,
        ppt_master_effect_runtime=ppt, ppt_master_capability=object(),
    )

    with pytest.raises(RuntimeError, match="heartbeat drifted"):
        register_enabled_effect_partitions(
            EffectRecoveryCoordinator(primary), state, root_dir=tmp_path,
        )


def test_recovery_report_attributes_safe_partition_passes(tmp_path) -> None:
    primary = build_effect_runtime(tmp_path / "jobs.sqlite3", owner_id="primary")
    ai = build_effect_runtime(tmp_path / "ai.sqlite3", owner_id="ai")
    coordinator = EffectRecoveryCoordinator(primary)
    coordinator.register_partition("ai-turns", ai)
    coordinator.configure_expected_partitions(("primary", "ai-turns"))

    report = coordinator.recover_once(now=101)

    assert report.effect_outcomes == ()
    assert [(item.partition, item.recovered_count, item.status) for item in report.partition_recovery] == [
        ("ai-turns", 0, "idle"), ("primary", 0, "idle"),
    ]
    assert coordinator.partition_recovery_snapshot() == report.partition_recovery
    coordinator = EffectRecoveryCoordinator(primary)
    coordinator.configure_expected_partitions(("primary", "ai-turns"))
    with pytest.raises(RuntimeError, match="registration drift"):
        coordinator.recover_once(now=102)


def test_production_effect_runtime_composes_all_core_execution_authorities(tmp_path) -> None:
    database = tmp_path / "nested" / "jobs.sqlite3"
    runtime = build_effect_runtime(
        database,
        owner_id="api-sidecar:test",
    )

    assert isinstance(runtime, EffectRuntime)
    assert runtime.runner.log is runtime.log
    assert runtime.reaper.log is runtime.log
    assert runtime.handlers.kinds() == ()
    assert runtime.recoveries.kinds() == ()
    assert database.is_file()


def test_core_recovery_registry_accepts_policy_without_domain_scheduler(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")

    def probe(_effect):
        return EffectState.UNKNOWN, None

    runtime.recoveries.register(EffectRecoveryRegistration(
        kind="remote_write", effect_class=EffectClass.QUERYABLE, probe=probe,
    ))

    assert runtime.recoveries.kinds() == ("remote_write",)
    assert runtime.recoveries.probes() == {"remote_write": probe}
    assert runtime.recoveries.reauthorizers() == {}
    with pytest.raises(ValueError, match="already registered"):
        runtime.recoveries.register(EffectRecoveryRegistration(
            kind="remote_write", effect_class=EffectClass.QUERYABLE, probe=probe,
        ))


def test_recovery_registration_requires_class_specific_strategy() -> None:
    with pytest.raises(ValueError, match="QUERYABLE recovery requires a probe"):
        EffectRecoveryRegistration(
            kind="queryable", effect_class=EffectClass.QUERYABLE,
            verify=lambda _effect: (EffectState.UNKNOWN, None),
        )
    with pytest.raises(ValueError, match="NEEDS_REAUTH recovery requires reauthorize"):
        EffectRecoveryRegistration(
            kind="reauth", effect_class=EffectClass.NEEDS_REAUTH,
            verify=lambda _effect: (EffectState.UNKNOWN, None),
        )


def test_core_coordinator_orders_migration_then_reaper_then_coordination(tmp_path) -> None:
    primary = build_effect_runtime(tmp_path / "jobs.sqlite3", owner_id="jobs")
    ai = build_effect_runtime(tmp_path / "ai.sqlite3", owner_id="ai")
    coordinator = EffectRecoveryCoordinator(primary)
    coordinator.register_partition("ai-turns", ai)
    calls: list[str] = []
    coordinator.register_preparation(RecoveryPreparationRegistration(
        kind="local-outbox", run=lambda: calls.append("prepare"),
    ))
    coordinator.register_backfill(EffectBackfillRegistration(
        kind="intent-backfill", run=lambda: calls.append("backfill"),
    ))
    coordinator.register_coordination(CoordinationTaskRegistration(
        kind="turn-scan", run=lambda: calls.append("turn-scan"),
    ))
    report = coordinator.recover_once(now=100)

    assert coordinator.partition_names() == ("ai-turns", "primary")
    assert coordinator.preparation_kinds() == ("local-outbox",)
    assert coordinator.backfill_kinds() == ("intent-backfill",)
    assert coordinator.coordination_kinds() == ("turn-scan",)
    assert calls == ["prepare", "backfill", "turn-scan"]
    assert report.preparation_completed == ("local-outbox",)
    assert report.preparation_failed == ()
    assert report.backfill_completed == ("intent-backfill",)
    assert report.backfill_failed == ()
    assert report.coordination_completed == ("turn-scan",)
    assert report.coordination_failed == ()


def test_periodic_core_reaper_recovers_after_lease_expiry(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="runner")
    intent = EffectIntent(
        session_id="session", root_id="root", step_key="step", kind="pure-work",
        effect_class=EffectClass.PURE, purpose=EffectPurpose.PRIMARY,
        intent_ref="intent", gate_decision_id="gate", rev_set={"revision": 1}, payload={},
    )
    planned, _ = runtime.log.plan(intent, now=1)
    runtime.log.transition(
        planned.operation_id, expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT, now=1, lease_owner="dead",
        lease_expires_at=2, increment_attempt=True,
    )
    coordinator = EffectRecoveryCoordinator(runtime)
    service = EffectRecoveryService(coordinator, interval_seconds=0.01, clock=lambda: 3)

    service.start()
    deadline = monotonic() + 1
    while monotonic() < deadline and runtime.log.get(planned.operation_id).state is not EffectState.PLANNED:
        sleep(0.01)

    assert service.shutdown(timeout_seconds=1)
    assert runtime.log.get(planned.operation_id).state is EffectState.PLANNED
    snapshot = service.snapshot()
    assert snapshot["status"] == "ready"
    assert snapshot["last_started_at"] == 3
    assert snapshot["last_finished_at"] == 3
    assert snapshot["last_failure_type"] is None


def test_core_coordinator_dispatches_recovered_planned_effect_via_registry(tmp_path) -> None:
    runtime = build_effect_runtime(
        tmp_path / "registered-effects.sqlite3", owner_id="core-runner", lease_seconds=1,
    )
    calls: list[str] = []
    runtime.handlers.register(EffectHandlerRegistration(
        kind="registered-work",
        effect_class=EffectClass.IDEMPOTENT,
        handler=lambda effect: calls.append(effect.operation_id) or "receipt:registered",
    ))
    intent = EffectIntent(
        session_id="session", root_id="root", step_key="step", kind="registered-work",
        effect_class=EffectClass.IDEMPOTENT, purpose=EffectPurpose.PRIMARY,
        intent_ref="intent", gate_decision_id="gate", rev_set={"revision": 1}, payload={},
    )
    planned, _ = runtime.log.plan(intent, now=1)
    runtime.log.transition(
        planned.operation_id, expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT, now=1, lease_owner="dead",
        lease_expires_at=2, increment_attempt=True,
    )

    EffectRecoveryCoordinator(runtime).recover_once(now=3)

    settled = runtime.log.get(planned.operation_id)
    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == "receipt:registered"
    assert calls == [planned.operation_id]


def test_low_latency_dispatch_still_uses_registry_and_runner_cas(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="core")
    calls: list[str] = []
    runtime.handlers.register(EffectHandlerRegistration(
        kind="job_execution",
        effect_class=EffectClass.QUERYABLE,
        handler=lambda effect: calls.append(effect.operation_id) or "receipt:job",
        probe=lambda _effect: (EffectState.UNKNOWN, None),
    ))
    planned, _ = runtime.log.plan(EffectIntent(
        session_id="job-session", root_id="job-1", step_key="job:attempt:0:execution",
        kind="job_execution", effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY, intent_ref="intent:job",
        gate_decision_id="job-gate", rev_set={"job_attempt": "0"}, payload={},
    ), now=1)

    first = runtime.dispatch_operation(planned.operation_id, now=2)
    replay = runtime.dispatch_operation(planned.operation_id, now=3)

    assert first.state is EffectState.SETTLED_OK
    assert replay.state is EffectState.SETTLED_OK
    assert calls == [planned.operation_id]


def test_core_heartbeat_keeps_live_handler_out_of_reaper_dispatch(tmp_path) -> None:
    runtime = build_effect_runtime(
        tmp_path / "heartbeat-effects.sqlite3",
        owner_id="heartbeat-runner",
        lease_seconds=0.12,
        lease_heartbeat_seconds=0.02,
    )
    started, finish = Event(), Event()
    calls: list[str] = []

    def slow_handler(effect):
        calls.append(effect.operation_id)
        started.set()
        assert finish.wait(timeout=2)
        return "receipt:heartbeat"

    runtime.handlers.register(EffectHandlerRegistration(
        kind="heartbeat-work",
        effect_class=EffectClass.IDEMPOTENT,
        handler=slow_handler,
    ))
    planned, _ = runtime.log.plan(EffectIntent(
        session_id="session", root_id="root", step_key="heartbeat",
        kind="heartbeat-work", effect_class=EffectClass.IDEMPOTENT,
        purpose=EffectPurpose.PRIMARY, intent_ref="intent:heartbeat",
        gate_decision_id="gate", rev_set={"revision": 1}, payload={},
    ), now=100)
    result: list[object] = []
    worker = Thread(
        target=lambda: result.append(runtime.dispatch_operation(planned.operation_id, now=100)),
        daemon=True,
    )

    worker.start()
    assert started.wait(timeout=1)
    deadline = monotonic() + 1
    while (
        monotonic() < deadline
        and float(runtime.log.get(planned.operation_id).lease_expires_at or 0) <= 100.2
    ):
        sleep(0.01)
    assert float(runtime.log.get(planned.operation_id).lease_expires_at or 0) > 100.2
    report = EffectRecoveryCoordinator(runtime).recover_once(now=100.2)
    assert report.effect_outcomes == ()
    assert runtime.log.get(planned.operation_id).state is EffectState.INFLIGHT
    finish.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result[0].state is EffectState.SETTLED_OK
    assert result[0].result_ref == "receipt:heartbeat"
    assert calls == [planned.operation_id]


def test_api_app_owns_the_production_effect_runtime_composition() -> None:
    app_path = ROOT / "src/backend/api/app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    create_app = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )
    rendered = ast.unparse(create_app)

    assert "application.state.effect_runtime = build_effect_runtime" in rendered
    assert (
        "external_extension_runtime = register_external_extension_runtime("
        "Path(root_dir), application.state.effect_runtime)"
    ) in rendered
    assert (
        "application.state.external_extension_runtime = external_extension_runtime"
    ) in rendered
    assert (
        "application.state.external_extension_install_workflow = "
        "external_extension_runtime.install_workflow"
    ) in rendered
    assert "application.state.effect_recovery_coordinator = effect_recovery" in rendered
    assert (
        "register_enabled_effect_partitions(effect_recovery, application.state, "
        "root_dir=Path(root_dir))" in rendered
    )
    assert "effect_recovery.register_partition" not in rendered
    assert "add_event_handler('startup', recover_external_effects)" in rendered
    assert "'.rebuild-data' / 'jobs.sqlite3'" in rendered
    assert rendered.index("register_external_extension_runtime") < rendered.index(
        "effect_recovery = EffectRecoveryCoordinator"
    )
    assert "register_external_extension_recovery" in rendered
