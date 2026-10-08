from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

from core.effect_log import (
    EffectClass,
    EffectHandlerRegistration,
    EffectIntent,
    EffectPurpose,
    EffectRecoveryRegistration,
    EffectState,
    build_effect_runtime,
)


def _intent() -> EffectIntent:
    return EffectIntent(
        session_id="project-0000001",
        turn_id="turn-0000000001",
        root_id="invoke-0000001",
        parent_id="tool-call-0000001",
        step_key="plugin-hands:plugin-0000001:hand-00000001",
        kind="plugin_hands_execution",
        effect_class=EffectClass.AT_MOST_ONCE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref="crp://session/turn-0000000001/tool-intent/1",
        gate_decision_id="boundary-decision-0001",
        rev_set={"capability_revision": 1},
        payload={"capability_id": "plugin.hand.plugin-0000001.hand-00000001"},
        operation_id_override="plugin-hands-effect-invoke-0000001",
    )


def _append_witness(path: str, owner: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, f"{owner}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _dispatch_worker(
    database: str, witness: str, owner: str, start,
) -> None:
    runtime = build_effect_runtime(Path(database), owner_id=owner)

    def handle(_effect):
        _append_witness(witness, owner)
        return f"plugin-hands-outcome:{owner}:r1"

    runtime.handlers.register(EffectHandlerRegistration(
        kind="plugin_hands_execution",
        effect_class=EffectClass.AT_MOST_ONCE,
        handler=handle,
    ))
    if not start.wait(10):
        raise RuntimeError("worker start gate timed out")
    runtime.dispatch_planned(now=2)


def _claim_then_crash_worker(database: str, witness: str) -> None:
    runtime = build_effect_runtime(Path(database), owner_id="crashing-hands-worker")

    def crash(_effect):
        _append_witness(witness, "crashing-hands-worker")
        os._exit(73)

    runtime.handlers.register(EffectHandlerRegistration(
        kind="plugin_hands_execution",
        effect_class=EffectClass.AT_MOST_ONCE,
        handler=crash,
    ))
    runtime.dispatch_planned(now=2)


def test_spawned_workers_execute_one_planned_hands_effect_once(tmp_path: Path) -> None:
    database = tmp_path / "records.sqlite3"
    witness = tmp_path / "host-invocations.log"
    runtime = build_effect_runtime(database, owner_id="planner")
    runtime.log.plan(_intent(), now=1)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    workers = [
        context.Process(
            target=_dispatch_worker,
            args=(str(database), str(witness), f"hands-worker-{index}", start),
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(20)

    assert [worker.exitcode for worker in workers] == [0, 0]
    assert witness.read_text(encoding="ascii").splitlines() in (
        ["hands-worker-0"], ["hands-worker-1"],
    )
    assert runtime.log.get(_intent().operation_id).state is EffectState.SETTLED_OK


def test_spawned_worker_crash_after_claim_becomes_unknown_without_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "records.sqlite3"
    witness = tmp_path / "host-invocations.log"
    runtime = build_effect_runtime(database, owner_id="planner")
    runtime.log.plan(_intent(), now=1)
    context = multiprocessing.get_context("spawn")
    worker = context.Process(
        target=_claim_then_crash_worker, args=(str(database), str(witness)),
    )
    worker.start()
    worker.join(20)

    assert worker.exitcode == 73
    assert witness.read_text(encoding="ascii").splitlines() == ["crashing-hands-worker"]
    inflight = runtime.log.get(_intent().operation_id)
    assert inflight.state is EffectState.INFLIGHT

    restarted = build_effect_runtime(database, owner_id="restarted-core-reaper")
    restarted.recoveries.register(EffectRecoveryRegistration(
        kind="plugin_hands_execution",
        effect_class=EffectClass.AT_MOST_ONCE,
        verify=lambda _effect: (EffectState.UNKNOWN, "plugin_hands.execution_unconfirmed"),
    ))
    outcomes = restarted.recover_expired(now=100)

    assert len(outcomes) == 1
    assert restarted.log.get(_intent().operation_id).state is EffectState.UNKNOWN
    assert witness.read_text(encoding="ascii").splitlines() == ["crashing-hands-worker"]
