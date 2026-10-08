from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, replace
import os
from threading import Event, Thread
import time

import pytest

from core.effect_log import (
    EffectClass,
    EffectHandlerRegistration,
    EffectLog,
    EffectRecoveryRegistration,
    EffectState,
    build_effect_runtime,
)
from core.plugin_hands.contained_host import WindowsContainedPluginHandsHost
from core.plugin_hands.contracts import PluginHandsInvocation, PluginHandsLaunch, PluginHandsLease, PluginHandsOutcome
from core.plugin_hands.durable_lifecycle import PluginHandsDurableLifecycle, PluginHandsDurableLifecycleError, PluginHandsLifecycleBinding, PluginHandsLifecycleReader, PluginHandsLifecycleRecovery, backfill_plugin_hands_execution_effects, execute_plugin_hands_workspace_cleanup, load_plugin_hands_outcome_fact, verify_plugin_hands_effect
from core.plugin_hands.workspace import PluginHandsWorkspaceManager
from core.plugin_hands.workspace import PluginHandsWorkspaceError
from core.storage_provider import SQLiteStructuredRecordStore


def _invocation(tmp_path: Path, *, lease_id: str = "lease-00000001") -> PluginHandsInvocation:
    launch = PluginHandsLaunch("launch-0000001", (tmp_path / "runner.exe").resolve(), (), {})
    lease = PluginHandsLease(lease_id, "invoke-0000001", 1, "project-0000001", "turn-0000000001", 1, "recipe-0000001", (), "2099-08-26T00:00:00Z")
    return PluginHandsInvocation("invoke-0000001", "plugin-0000001", launch, lease, 1000, {"task": "fixture"})


@dataclass
class _Authority:
    launch: PluginHandsLaunch
    calls: int = 0
    def resolve(self, binding, scope):
        self.calls += 1
        assert binding.hand_id == "1h" and binding.containment_profile_revision == "containment-r1"
        assert scope.project_id == "project-0000001" and scope.boundary_revision == 1
        return self.launch
    def prepare_workspace(self, binding, scope, workspace):
        assert workspace.lease.lease_id == scope.lease_id
        return None
    def verify_workspace(self, binding, scope, workspace):
        assert workspace.lease.invocation_id == scope.invocation_id
        return None
    def validate_outcome(self, binding, scope, outcome):
        assert outcome.invocation_id == scope.invocation_id
        return None


def _service(tmp_path: Path, authority=None, *, effect_runner=None) -> PluginHandsDurableLifecycle:
    launch = PluginHandsLaunch("launch-0000001", (tmp_path / "runner.exe").resolve(), (), {})
    return PluginHandsDurableLifecycle(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"),
        authority or _Authority(launch),
        now=lambda: "2026-08-26T00:00:00Z",
        effect_runner=effect_runner,
    )


def _binding() -> PluginHandsLifecycleBinding:
    return PluginHandsLifecycleBinding("intent-0000001", "capability-0001", "artifact-000001", "plugin-0000001", "1h", "package-000001", 1, 1, 1, "containment-r1", "recipe-0000001")


def _manager(tmp_path: Path) -> PluginHandsWorkspaceManager:
    root = tmp_path / "workspaces"
    root.mkdir()
    return PluginHandsWorkspaceManager(root.resolve())


def test_pre_fence_restart_safely_cleans_orphan_without_execution(tmp_path: Path) -> None:
    lifecycle, manager, invocation = _service(tmp_path), _manager(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    manager.create(invocation.lease)

    restarted = _service(tmp_path)
    repaired = PluginHandsLifecycleRecovery(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"),
        now=lambda: "2026-08-26T00:00:00Z",
    ).reconcile(_manager_reopened(tmp_path))
    assert repaired[0].state == "pre_fence_cleaned"
    assert not (tmp_path / "workspaces" / invocation.lease.lease_id).exists()
    assert restarted.load(invocation.invocation_id).state == "pre_fence_cleaned"  # type: ignore[union-attr]
    with pytest.raises(PluginHandsDurableLifecycleError, match="command replay"):
        restarted.execute(WindowsContainedPluginHandsHost(), manager, _binding(), invocation)
    assert prepared.state == "prepared"


def test_fenced_restart_is_unknown_and_never_auto_replays(tmp_path: Path) -> None:
    lifecycle, manager, invocation = _service(tmp_path), _manager(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    manager.create(invocation.lease)
    lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)

    repaired = PluginHandsLifecycleRecovery(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"),
        now=lambda: "2026-08-26T00:00:00Z",
    ).reconcile(manager)
    assert repaired[0].state == "unknown"
    assert repaired[0].workspace_ref == "plugin-hands-workspace:lease-00000001:1"
    assert (tmp_path / "workspaces" / invocation.lease.lease_id).is_dir()
    with pytest.raises(PluginHandsDurableLifecycleError, match="command replay"):
        _service(tmp_path).execute(WindowsContainedPluginHandsHost(), manager, _binding(), invocation)


def test_lazy_recovery_uses_one_factory_for_cleanup_and_defers_late_snapshot_record(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    lifecycle.prepare(_binding(), invocation)
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    recovery = PluginHandsLifecycleRecovery(records, now=lambda: "2026-08-26T00:00:00Z")
    root = tmp_path / "workspaces"
    calls = 0

    def factory() -> PluginHandsWorkspaceManager:
        nonlocal calls
        calls += 1
        root.mkdir(exist_ok=True)
        late_invocation = _invocation(tmp_path, lease_id="lease-00000002")
        late_invocation = replace(
            late_invocation, invocation_id="invoke-0000002",
            lease=replace(late_invocation.lease, invocation_id="invoke-0000002"),
        )
        late = _service(tmp_path).prepare(_binding(), late_invocation)
        _service(tmp_path).fence(late.invocation_id, expected_revision=late.revision)
        return PluginHandsWorkspaceManager(root.resolve())

    repaired = recovery.reconcile_lazy(factory)

    assert calls == 1
    assert repaired[0].state == "pre_fence_cleaned"
    assert _service(tmp_path).load(invocation.invocation_id).state == "pre_fence_cleaned"  # type: ignore[union-attr]
    # The late record was not in this snapshot and is safely handled on the
    # next pass without an additional cleanup manager construction.
    late = _service(tmp_path).load("invoke-0000002")
    assert late is not None and late.lease_id == "lease-00000002" and late.state == "fenced"
    repaired = recovery.reconcile_lazy(factory)
    assert calls == 1 and repaired[0].state == "unknown"


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer durable shutdown fixture")
def test_host_shutdown_kills_active_child_and_durably_retains_unknown(tmp_path: Path) -> None:
    executable = Path(os.environ["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    hello = '{"protocol":"plugin-hands/1","type":"hello","launch_id":"launch-0000001","lease_id":"lease-00000001","invocation_id":"invoke-0000001"}'
    command = f"[Console]::Out.WriteLine('{hello}'); [Console]::In.ReadLine() | Out-Null; while ($true) {{}}"
    launch = PluginHandsLaunch(
        "launch-0000001", executable.resolve(),
        ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command), {},
    )
    invocation = replace(_invocation(tmp_path), launch=launch, deadline_ms=10_000)
    lifecycle = _service(tmp_path, _Authority(launch))
    manager = _manager(tmp_path)
    host = WindowsContainedPluginHandsHost()
    results = []
    worker = Thread(target=lambda: results.append(lifecycle.execute(
        host, manager, _binding(), invocation,
    )))
    worker.start()
    time.sleep(0.5)

    host.close()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert results[0].outcome.status == "unknown"
    record = lifecycle.load(invocation.invocation_id)
    assert record is not None and record.state == "unknown"
    assert record.workspace_ref == "plugin-hands-workspace:lease-00000001:1"
    assert (tmp_path / "workspaces" / invocation.lease.lease_id).is_dir()


def test_known_outcome_cleanup_pending_is_retried_on_restart_without_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle, manager, invocation = _service(tmp_path), _manager(tmp_path), _invocation(tmp_path)
    host = WindowsContainedPluginHandsHost()
    calls = 0

    def successful(_self, _workspace, _launch, current, _control):
        nonlocal calls
        calls += 1
        return PluginHandsOutcome(current.invocation_id, current.lease.lease_id, "success", {"ok": True})

    monkeypatch.setattr(WindowsContainedPluginHandsHost, "execute_prepared", successful)
    failed_once = True

    def fail_once(_lease_id, _invocation_id):
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise PluginHandsWorkspaceError("injected")

    monkeypatch.setattr(manager, "cleanup_known_identity", fail_once)
    result = lifecycle.execute(host, manager, _binding(), invocation)
    assert result.outcome.status == "success"
    pending = lifecycle.load(invocation.invocation_id)
    assert pending is not None and pending.state == "cleanup_pending" and pending.outcome_status == "success"
    assert calls == 1

    cleanup_effect = EffectLog(tmp_path / "records.sqlite3").get(
        f"plugin-hands-cleanup-{invocation.invocation_id}"
    )
    assert cleanup_effect.state is EffectState.INFLIGHT
    reopened = _manager_reopened(tmp_path)
    monkeypatch.setattr(reopened, "cleanup_known_identity", lambda *_args: None)
    runtime = build_effect_runtime(tmp_path / "records.sqlite3", owner_id="core-cleanup-reaper")
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    handler = lambda effect: execute_plugin_hands_workspace_cleanup(records, reopened, effect)
    runtime.handlers.register(EffectHandlerRegistration(
        kind="plugin_hands_workspace_cleanup",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handler,
    ))
    recovered = runtime.recover_expired(now=4_091_385_901)
    assert recovered, cleanup_effect.lease_expires_at
    dispatched = runtime.dispatch_planned(now=4_091_385_901)
    assert tuple(effect.operation_id for effect in dispatched) == (
        f"plugin-hands-cleanup-{invocation.invocation_id}",
    )
    cleaned = lifecycle.load(invocation.invocation_id)
    cleanup_effect = runtime.log.get(f"plugin-hands-cleanup-{invocation.invocation_id}")
    assert cleaned is not None
    assert (cleaned.state, cleanup_effect.state, cleanup_effect.error_ref) == (
        "cleaned", EffectState.SETTLED_OK, None,
    )
    assert cleanup_effect.parent_id == f"plugin-hands-effect-{invocation.invocation_id}"
    assert calls == 1


def test_binding_drift_cas_and_expiry_fail_closed(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    changed = PluginHandsLifecycleBinding("intent-0000001", "capability-0001", "artifact-000001", "plugin-0000001", "1h", "package-000001", 2, 1, 1, "containment-r1", "recipe-0000001")
    with pytest.raises(PluginHandsDurableLifecycleError, match="binding drift"):
        lifecycle.prepare(changed, invocation)
    with pytest.raises(PluginHandsDurableLifecycleError, match="transition"):
        lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision + 1)
    expired = PluginHandsLease("lease-00000002", "invoke-0000002", 1, "project-0000001", "turn-0000000002", 1, "recipe-0000001", (), "2020-08-26T00:00:00Z")
    other = PluginHandsInvocation("invoke-0000002", "plugin-0000001", invocation.launch, expired, 1000, {})
    with pytest.raises(PluginHandsDurableLifecycleError, match="expired"):
        lifecycle.prepare(_binding(), other)


def test_plugin_hands_effect_owns_execution_lease_and_terminal_receipt(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    effects = EffectLog(tmp_path / "records.sqlite3")
    operation_id = f"plugin-hands-effect-{invocation.invocation_id}"

    planned = effects.get(operation_id)
    assert planned.state is EffectState.PLANNED
    assert planned.lease_owner is None

    fenced = lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)
    inflight = effects.get(operation_id)
    assert inflight.state is EffectState.INFLIGHT
    assert inflight.lease_owner == lifecycle._runner.owner_id
    assert inflight.lease_expires_at == pytest.approx(4_091_385_600)

    outcome = PluginHandsOutcome(
        invocation.invocation_id, invocation.lease.lease_id, "success", {"ok": True},
    )
    recorded = lifecycle.record_outcome(
        invocation.invocation_id, outcome, expected_revision=fenced.revision,
    )
    settled = effects.get(operation_id)
    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == f"plugin-hands-outcome:{invocation.invocation_id}:r1"
    receipt = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3").read(
        "plugin_hands_outcome_receipts", invocation.invocation_id,
    )
    assert receipt is not None and receipt.revision == 1
    assert receipt.payload["status"] == "success"
    assert "output" not in str(dict(receipt.payload)).lower()
    rebuilt = load_plugin_hands_outcome_fact(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"),
        invocation.invocation_id,
    )
    assert rebuilt.status == "success"
    assert rebuilt.output == {"ok": True}


def test_live_plugin_hands_execution_uses_core_runner_not_domain_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, manager, invocation = _service(tmp_path), _manager(tmp_path), _invocation(tmp_path)
    host = WindowsContainedPluginHandsHost()
    monkeypatch.setattr(
        lifecycle, "fence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("domain fence must not run")),
    )
    monkeypatch.setattr(
        WindowsContainedPluginHandsHost, "execute_prepared",
        lambda _self, _workspace, _launch, current, _control: PluginHandsOutcome(
            current.invocation_id, current.lease.lease_id, "success", {"ok": True},
        ),
    )

    result = lifecycle.execute(host, manager, _binding(), invocation)
    effect = EffectLog(tmp_path / "records.sqlite3").get(
        f"plugin-hands-effect-{invocation.invocation_id}"
    )

    assert result.outcome.status == "success"
    assert effect.state is EffectState.SETTLED_OK
    assert effect.result_ref == f"plugin-hands-outcome:{invocation.invocation_id}:r1"
    assert effect.lease_owner is None


def test_plugin_hands_effect_is_bound_to_outer_tool_effect(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)

    lifecycle.prepare(_binding(), invocation)

    effect = EffectLog(tmp_path / "records.sqlite3").get(
        f"plugin-hands-effect-{invocation.invocation_id}"
    )
    assert effect.parent_id == invocation.invocation_id


def test_invalid_success_output_is_not_persisted_as_terminal_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = _invocation(tmp_path)

    class RejectingAuthority(_Authority):
        def validate_outcome(self, _binding, _scope, _outcome):
            raise ValueError("invalid output schema")

    lifecycle = _service(tmp_path, RejectingAuthority(invocation.launch))
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        WindowsContainedPluginHandsHost, "execute_prepared",
        lambda _self, _workspace, _launch, current, _control: PluginHandsOutcome(
            current.invocation_id, current.lease.lease_id, "success", {"bad": True},
        ),
    )

    with pytest.raises(PluginHandsDurableLifecycleError, match="outcome authority"):
        lifecycle.execute(
            WindowsContainedPluginHandsHost(), manager, _binding(), invocation,
        )

    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    assert records.read("plugin_hands_outcome_receipts", invocation.invocation_id) is None
    assert records.read("plugin_hands_result_payloads", invocation.invocation_id) is None
    assert lifecycle.load(invocation.invocation_id).state == "fenced"  # type: ignore[union-attr]
    assert EffectLog(tmp_path / "records.sqlite3").get(
        f"plugin-hands-effect-{invocation.invocation_id}"
    ).state is EffectState.INFLIGHT


def test_two_core_workers_dispatch_one_planned_hand_exactly_once(tmp_path: Path) -> None:
    runtime_a = build_effect_runtime(tmp_path / "records.sqlite3", owner_id="hands-worker-a")
    runtime_b = build_effect_runtime(tmp_path / "records.sqlite3", owner_id="hands-worker-b")
    invocation = _invocation(tmp_path)
    authority = _Authority(invocation.launch)
    lifecycle_a = _service(tmp_path, authority, effect_runner=runtime_a.runner)
    lifecycle_b = _service(tmp_path, authority, effect_runner=runtime_b.runner)
    lifecycle_a.prepare(_binding(), invocation)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    manager_a = PluginHandsWorkspaceManager(workspace_root.resolve())
    manager_b = PluginHandsWorkspaceManager(workspace_root.resolve())
    entered, release = Event(), Event()

    class BlockingHost(WindowsContainedPluginHandsHost):
        calls = 0

        def execute_prepared(self, _workspace, _launch, current, _control):
            self.calls += 1
            entered.set()
            assert release.wait(3)
            return PluginHandsOutcome(
                current.invocation_id, current.lease.lease_id,
                "success", {"ok": True},
            )

    host = BlockingHost()
    runtime_a.handlers.register(EffectHandlerRegistration(
        kind="plugin_hands_execution", effect_class=EffectClass.AT_MOST_ONCE,
        handler=lambda effect: lifecycle_a.handle_claimed(
            effect, host, manager_a, _binding(), invocation,
        ),
    ))
    runtime_b.handlers.register(EffectHandlerRegistration(
        kind="plugin_hands_execution", effect_class=EffectClass.AT_MOST_ONCE,
        handler=lambda effect: lifecycle_b.handle_claimed(
            effect, host, manager_b, _binding(), invocation,
        ),
    ))
    first_result: list[object] = []
    first = Thread(target=lambda: first_result.extend(runtime_a.dispatch_planned(now=1)))
    first.start()
    assert entered.wait(2)

    assert runtime_b.dispatch_planned(now=1) == ()
    release.set()
    first.join(4)

    assert not first.is_alive()
    assert host.calls == 1
    assert len(first_result) == 1
    assert runtime_a.log.get("plugin-hands-effect-invoke-0000001").state is EffectState.SETTLED_OK


def test_plugin_hands_restart_marks_effect_unknown_without_replay(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    invocation = replace(
        invocation,
        lease=replace(invocation.lease, expires_at="2026-08-26T00:00:02Z"),
    )
    lifecycle = _service(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    manager = _manager(tmp_path)
    manager.create(invocation.lease)
    lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)

    repaired = PluginHandsLifecycleRecovery(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"),
        now=lambda: "2026-08-26T00:00:01Z",
    ).reconcile(manager)

    assert repaired[0].state == "unknown"
    runtime = build_effect_runtime(
        tmp_path / "records.sqlite3", owner_id="core-reaper-test",
    )
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    backfill_plugin_hands_execution_effects(records, runtime.log, now=1_787_702_403)
    runtime.recoveries.register(EffectRecoveryRegistration(
        kind="plugin_hands_execution", effect_class=EffectClass.AT_MOST_ONCE,
        verify=lambda effect: verify_plugin_hands_effect(records, effect),
    ))
    runtime.recover_expired(now=1_787_702_403)
    effect = runtime.log.get(
        f"plugin-hands-effect-{invocation.invocation_id}"
    )
    assert effect.state is EffectState.UNKNOWN
    assert effect.error_ref == "plugin-hands.outcome-receipt-missing"


def test_plugin_hands_retained_receipt_repairs_crash_before_effect_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = _invocation(tmp_path)
    invocation = replace(
        invocation,
        lease=replace(invocation.lease, expires_at="2026-08-26T00:00:02Z"),
    )
    lifecycle = _service(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    fenced = lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)
    outcome = PluginHandsOutcome(
        invocation.invocation_id, invocation.lease.lease_id, "success", {"ok": True},
    )

    def crash_before_settle(*_args, **_kwargs):
        raise RuntimeError("injected crash before Effect settle")

    monkeypatch.setattr(lifecycle._runner, "settle_ok", crash_before_settle)
    with pytest.raises(RuntimeError, match="injected crash"):
        lifecycle.record_outcome(
            invocation.invocation_id, outcome, expected_revision=fenced.revision,
        )

    retained = lifecycle.load(invocation.invocation_id)
    assert retained is not None and retained.state == "cleanup_pending"
    assert EffectLog(tmp_path / "records.sqlite3").get(
        f"plugin-hands-effect-{invocation.invocation_id}"
    ).state is EffectState.INFLIGHT

    runtime = build_effect_runtime(
        tmp_path / "records.sqlite3", owner_id="core-reaper-test",
    )
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    backfill_plugin_hands_execution_effects(records, runtime.log, now=1_787_702_403)
    runtime.recoveries.register(EffectRecoveryRegistration(
        kind="plugin_hands_execution", effect_class=EffectClass.AT_MOST_ONCE,
        verify=lambda effect: verify_plugin_hands_effect(records, effect),
    ))
    runtime.recover_expired(now=1_787_702_403)
    repaired = runtime.log.get(
        f"plugin-hands-effect-{invocation.invocation_id}"
    )
    assert repaired.state is EffectState.SETTLED_OK
    assert repaired.result_ref == f"plugin-hands-outcome:{invocation.invocation_id}:r1"


def test_core_reaper_uses_plugin_verify_without_domain_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    fenced = lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)
    outcome = PluginHandsOutcome(
        invocation.invocation_id, invocation.lease.lease_id, "success", {"ok": True},
    )
    monkeypatch.setattr(
        lifecycle._runner, "settle_ok",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected crash")),
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        lifecycle.record_outcome(
            invocation.invocation_id, outcome, expected_revision=fenced.revision,
        )

    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    runtime = build_effect_runtime(tmp_path / "records.sqlite3", owner_id="reaper")
    runtime.recoveries.register(EffectRecoveryRegistration(
        kind="plugin_hands_execution", effect_class=EffectClass.AT_MOST_ONCE,
        verify=lambda effect: verify_plugin_hands_effect(records, effect),
    ))
    recovered = runtime.recover_expired(now=4_091_385_601)

    assert recovered[0].state is EffectState.SETTLED_OK
    assert recovered[0].reason == "verifier_resolved"


def test_core_reaper_ignores_mutable_cleanup_projection_when_receipt_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    fenced = lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)
    monkeypatch.setattr(
        lifecycle._runner, "settle_ok",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected crash")),
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        lifecycle.record_outcome(
            invocation.invocation_id,
            PluginHandsOutcome(
                invocation.invocation_id, invocation.lease.lease_id, "success", {"ok": True},
            ),
            expected_revision=fenced.revision,
        )

    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    projection = records.read("plugin_hands_lifecycles", invocation.invocation_id)
    assert projection is not None
    drifted = dict(projection.payload)
    drifted["outcome_status"] = "failed"
    drifted["error_code"] = "projection-only-drift"
    with records.begin() as uow:
        uow.put(
            "plugin_hands_lifecycles", invocation.invocation_id, drifted,
            expected_revision=projection.revision,
        )
        uow.commit()

    effect = EffectLog(tmp_path / "records.sqlite3").get(
        f"plugin-hands-effect-{invocation.invocation_id}"
    )
    state, fact_ref = verify_plugin_hands_effect(records, effect)

    assert state is EffectState.SETTLED_OK
    assert fact_ref == f"plugin-hands-outcome:{invocation.invocation_id}:r1"


def test_record_has_no_launch_input_or_output_payload(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    lifecycle.prepare(_binding(), invocation)
    raw = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3").read("plugin_hands_lifecycles", invocation.invocation_id)
    assert raw is not None
    text = str(dict(raw.payload)).lower()
    for forbidden in ("input", "executable", "argv", "environment", "output", "runner.exe"):
        assert forbidden not in text
    assert raw.payload["binding"]["hand_id"] == "1h"
    assert raw.payload["binding"]["activation_revision"] == 1
    assert raw.payload["binding"]["containment_profile_revision"] == "containment-r1"
    assert raw.payload["lease"] == {"invocation_id": "invoke-0000001", "lease_id": "lease-00000001", "generation": 1, "project_id": "project-0000001", "turn_id": "turn-0000000001", "boundary_revision": 1, "recipe_revision": "recipe-0000001", "allowed_resources": [], "resource_policy_revision": "plugin-hands-resource-v1", "expires_at": "2099-08-26T00:00:00Z"}


def test_legacy_record_without_resource_policy_decodes_only_for_recovery(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    lifecycle.prepare(_binding(), invocation)
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    raw = records.read("plugin_hands_lifecycles", invocation.invocation_id)
    assert raw is not None
    payload = dict(raw.payload)
    payload["lease"] = dict(payload["lease"])
    payload["lease"].pop("resource_policy_revision")
    with records.begin() as uow:
        uow.put("plugin_hands_lifecycles", invocation.invocation_id, payload, expected_revision=raw.revision)
        uow.commit()
    legacy = lifecycle.load(invocation.invocation_id)
    assert legacy is not None and legacy.resource_policy_revision == "legacy-unbounded-v0"
    with pytest.raises(PluginHandsDurableLifecycleError, match="binding drift"):
        lifecycle.prepare(_binding(), invocation)


def test_lifecycle_reader_projects_one_safe_attempt_without_recovery(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)

    projection = PluginHandsLifecycleReader(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    ).read_attempt(invocation.invocation_id)

    assert projection is not None
    assert projection.attempt_id == invocation.invocation_id
    assert projection.plugin_id == "plugin-0000001"
    assert projection.hand_id == "1h"
    assert projection.state == "prepared"
    assert projection.revision == prepared.revision
    assert projection.outcome_status is None
    assert projection.error_code is None
    assert projection.updated_at == "2026-08-26T00:00:00Z"
    assert projection.workspace_retention == "not_yet_terminal"
    assert lifecycle.load(invocation.invocation_id) == prepared
    assert PluginHandsLifecycleReader(SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")).read_attempt("invoke-9999999") is None


def test_lifecycle_reader_reports_unknown_workspace_as_retained_without_recovery(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    fenced = lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)
    lifecycle.record_outcome(
        invocation.invocation_id,
        PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "unknown", error_code="host_lost"),
        expected_revision=fenced.revision,
    )

    projection = PluginHandsLifecycleReader(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    ).read_attempt(invocation.invocation_id)

    assert projection is not None
    assert projection.state == "unknown"
    assert projection.workspace_retention == "retained"


def test_lease_scope_drift_fails_closed(tmp_path: Path) -> None:
    lifecycle, invocation = _service(tmp_path), _invocation(tmp_path)
    lifecycle.prepare(_binding(), invocation)
    drifted_lease = PluginHandsLease(invocation.lease.lease_id, invocation.lease.invocation_id, 1, invocation.lease.project_id, invocation.lease.turn_id, 2, invocation.lease.recipe_revision, invocation.lease.allowed_resources, invocation.lease.expires_at)
    drifted = PluginHandsInvocation(invocation.invocation_id, invocation.plugin_id, invocation.launch, drifted_lease, invocation.deadline_ms, invocation.input)
    with pytest.raises(PluginHandsDurableLifecycleError, match="binding drift"):
        lifecycle.prepare(_binding(), drifted)


def test_prepare_then_expiry_before_fence_cleans_without_host_execution(tmp_path: Path) -> None:
    clock = ["2026-08-26T00:00:00Z"]
    lifecycle = PluginHandsDurableLifecycle(SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"), _Authority(_invocation(tmp_path).launch), now=lambda: clock[0])
    manager, invocation = _manager(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    manager.create(invocation.lease)
    clock[0] = "2100-08-26T00:00:00Z"
    with pytest.raises(PluginHandsDurableLifecycleError, match="expired"):
        lifecycle.fence(invocation.invocation_id, expected_revision=prepared.revision)
    # The execution coordinator performs this same pre-fence cleanup path.
    manager.cleanup_pre_fence_identity(invocation.lease.lease_id)
    assert not (tmp_path / "workspaces" / invocation.lease.lease_id).exists()


def test_competing_fence_failure_never_deletes_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle, manager, invocation = _service(tmp_path), _manager(tmp_path), _invocation(tmp_path)
    original = lifecycle.fence
    def competing(invocation_id: str, *, expected_revision: int):
        original(invocation_id, expected_revision=expected_revision)
        raise PluginHandsDurableLifecycleError("injected competing fence")
    monkeypatch.setattr(lifecycle, "fence", competing)
    with pytest.raises(PluginHandsDurableLifecycleError, match="competing"):
        lifecycle.execute(WindowsContainedPluginHandsHost(), manager, _binding(), invocation)
    record = lifecycle.load(invocation.invocation_id)
    assert record is not None and record.state == "fenced"
    assert (tmp_path / "workspaces" / invocation.lease.lease_id).is_dir()


def test_cleanup_claim_loses_to_fence_before_cas_and_never_deletes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle, manager, invocation = _service(tmp_path), _manager(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    manager.create(invocation.lease)
    original = lifecycle._replace
    def interleaved(record, **kwargs):
        if kwargs.get("state") == "pre_fence_cleanup_pending":
            lifecycle.fence(invocation.invocation_id, expected_revision=record.revision)
        return original(record, **kwargs)
    monkeypatch.setattr(lifecycle, "_replace", interleaved)
    lifecycle._cleanup_if_still_pre_fence(manager, prepared)
    record = lifecycle.load(invocation.invocation_id)
    assert record is not None and record.state == "fenced"
    assert (tmp_path / "workspaces" / invocation.lease.lease_id).is_dir()


def test_restart_finishes_only_already_claimed_pre_fence_cleanup(tmp_path: Path) -> None:
    lifecycle, manager, invocation = _service(tmp_path), _manager(tmp_path), _invocation(tmp_path)
    prepared = lifecycle.prepare(_binding(), invocation)
    manager.create(invocation.lease)
    claimed = lifecycle._claim_pre_fence_cleanup(prepared)
    assert claimed.state == "pre_fence_cleanup_pending"
    restarted = _service(tmp_path)
    repaired = restarted.reconcile(_manager_reopened(tmp_path))
    assert repaired[0].state == "pre_fence_cleaned"
    assert not (tmp_path / "workspaces" / invocation.lease.lease_id).exists()


@dataclass(frozen=True)
class _ManagedArtifactFixture:
    artifact_opaque_ref: str = "artifact-000001"
    plugin_id: str = "plugin-0000001"
    hand_id: str = "1h"
    package_record_id: str = "package-000001"
    review_revision: int = 1
    materialization_revision: int = 1
    activation_revision: int = 1
    containment_profile_revision: str = "containment-r1"
    launch_recipe_revision: str = "recipe-0000001"


class _ArtifactAuthority(_Authority):
    def __init__(self, launch: PluginHandsLaunch, artifact: _ManagedArtifactFixture, *, drift_after_first: bool = False) -> None:
        super().__init__(launch); self.artifact, self.drift_after_first = artifact, drift_after_first
    def resolve(self, binding, scope):
        assert binding.artifact_opaque_ref == self.artifact.artifact_opaque_ref
        for field in ("plugin_id", "hand_id", "package_record_id", "review_revision", "materialization_revision", "activation_revision", "containment_profile_revision", "launch_recipe_revision"):
            assert getattr(binding, field) == getattr(self.artifact, field)
        launch = super().resolve(binding, scope)
        if self.drift_after_first and self.calls > 2:
            return PluginHandsLaunch("launch-0000002", launch.executable, launch.argv, launch.environment)
        return launch


def test_artifact_authority_rejects_each_binding_drift_and_launch_drift_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = _invocation(tmp_path)
    artifact = _ManagedArtifactFixture()
    authority = _ArtifactAuthority(invocation.launch, artifact, drift_after_first=True)
    lifecycle, manager = _service(tmp_path, authority), _manager(tmp_path)
    host_called = False
    def forbidden(*_args, **_kwargs):
        nonlocal host_called
        host_called = True
        raise AssertionError("must not execute after authority drift")
    monkeypatch.setattr(WindowsContainedPluginHandsHost, "execute_prepared", forbidden)
    result = lifecycle.execute(WindowsContainedPluginHandsHost(), manager, _binding(), invocation)
    assert result.outcome.status == "unknown" and result.outcome.error_code == "authority-staging-drift"
    assert not host_called and (tmp_path / "workspaces" / invocation.lease.lease_id).is_dir()
    for index, (field, value) in enumerate((("artifact_opaque_ref", "artifact-000002"), ("review_revision", 2), ("materialization_revision", 2), ("activation_revision", 2), ("containment_profile_revision", "containment-r2"))):
        other_root = tmp_path / f"drift-{index}"
        other_root.mkdir()
        other = _service(other_root)
        current = _invocation(other_root)
        other.prepare(_binding(), current)
        with pytest.raises(PluginHandsDurableLifecycleError, match="binding drift"):
            other.prepare(replace(_binding(), **{field: value}), current)


class _WorkspaceFailureAuthority(_Authority):
    def __init__(self, launch: PluginHandsLaunch, *, fail_prepare: bool = False, fail_verify: bool = False) -> None:
        super().__init__(launch); self.fail_prepare, self.fail_verify = fail_prepare, fail_verify
    def prepare_workspace(self, binding, scope, workspace):
        if self.fail_prepare:
            raise RuntimeError("injected preparation crash")
        return super().prepare_workspace(binding, scope, workspace)
    def verify_workspace(self, binding, scope, workspace):
        if self.fail_verify:
            raise RuntimeError("injected verification drift")
        return super().verify_workspace(binding, scope, workspace)


def test_pre_fence_workspace_preparation_crash_claims_and_cleans(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    authority = _WorkspaceFailureAuthority(invocation.launch, fail_prepare=True)
    lifecycle, manager = _service(tmp_path, authority), _manager(tmp_path)
    with pytest.raises(PluginHandsDurableLifecycleError, match="preparation"):
        lifecycle.execute(WindowsContainedPluginHandsHost(), manager, _binding(), invocation)
    record = lifecycle.load(invocation.invocation_id)
    assert record is not None and record.state == "pre_fence_cleaned"
    assert not (tmp_path / "workspaces" / invocation.lease.lease_id).exists()


def test_post_fence_workspace_verification_drift_is_unknown_without_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = _invocation(tmp_path)
    lifecycle = _service(tmp_path, _WorkspaceFailureAuthority(invocation.launch, fail_verify=True))
    manager = _manager(tmp_path)
    called = False
    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("host must not run")
    monkeypatch.setattr(WindowsContainedPluginHandsHost, "execute_prepared", forbidden)
    result = lifecycle.execute(WindowsContainedPluginHandsHost(), manager, _binding(), invocation)
    assert (result.outcome.status, result.outcome.error_code) == ("unknown", "authority-staging-drift")
    assert not called and (tmp_path / "workspaces" / invocation.lease.lease_id).is_dir()


def _manager_reopened(tmp_path: Path) -> PluginHandsWorkspaceManager:
    return PluginHandsWorkspaceManager((tmp_path / "workspaces").resolve())
