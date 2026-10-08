"""Cross-process lifecycle command recovery gates.

These tests intentionally use Windows-compatible ``spawn`` workers.  Each
worker reconstructs its own EffectRuntime against the same SQLite database and
quarantine root; no in-memory fixture is shared across process boundaries.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from queue import Empty
import sys
from typing import Any

import pytest

from core.external_extension_runtime.installation import (
    ExternalExtensionInstallationConflict,
)
from core.external_extension_runtime.fact_store import intent_ref
from core.external_extension_runtime.gate_authority import (
    ExternalExtensionGateAuthority,
    ExternalExtensionGateRequest,
)
from core.external_extension_runtime.lifecycle_commands import (
    ExternalExtensionLifecycleCommandService,
)
from core.external_extension_runtime.lifecycle import ACQUIRE_EFFECT_KIND
from core.effect_log import EffectState, build_effect_runtime
from backend.api.external_extension_runtime_startup import (
    register_external_extension_runtime,
    register_external_extension_recovery,
)

# Reuse the immutable intake fixture and its Core-runtime composition.  The
# child target stays module-level so the Windows spawn start method can pickle
# it without importing a test-local closure.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "unit" / "api"))
from test_external_extension_installation import (
    REVISION_B,
    _install_one,
    _record_intake,
    _services,
    _skill_body,
)
from test_external_extension_runtime_startup import (
    _install as _startup_install,
    _record_resolution_without_acquire,
    _registered_runtime,
    _source_authorization as _startup_source_authorization,
)


_PROCESS_TIMEOUT_SECONDS = 20


def _commands(services: Any) -> ExternalExtensionLifecycleCommandService:
    return ExternalExtensionLifecycleCommandService(
        services.records,
        services.store,
        services.receipt_facts,
        services.runtime,
        gate_authority=ExternalExtensionGateAuthority(services.facts),
    )


def _source_authorization(services: Any, revision: Any, *, suffix: str) -> str:
    """Create the same immutable source confirmation consumed in a child."""

    intake = services.facts.load_intake(revision.intake_ref)
    install = services.facts.load_intent(intent_ref(str(intake["intent_id"])))
    proposal = services.facts.record_proposal(
        install, proposal_id=f"multiprocess-proposal-{suffix}",
    )
    return services.facts.confirm_source_proposal(
        proposal,
        confirmation_id=f"multiprocess-source-confirmation-{suffix}",
        confirmation_ids=("approve_initial_network_source",),
        actor="local-operator",
        reason="Approved the exact lifecycle fixture source.",
    )


def _reserve_authorization(
    commands: ExternalExtensionLifecycleCommandService,
    revision_ref: str,
    semantic: dict[str, object],
    authorization_ref: str,
):
    revision = commands._installations.load_revision(revision_ref)
    return commands._gate_authority.authorize(ExternalExtensionGateRequest(
        phase=f"lifecycle_{semantic['action']}",
        project_id=str(semantic["root_id"]),
        subject_ref=revision.intake_ref,
        authorization_ref=authorization_ref,
        policy_revision="external-extension-lifecycle-policy-v1",
        revision_ref=revision.revision_ref,
        expected_state_revision=int(semantic["expected_state_revision"]),
    ))


def _lifecycle_worker(
    root: str,
    revision_ref: str,
    expected_state_revision: int,
    action: str,
    mode: str,
    start: Any,
    output: Any,
    owner_id: str,
    authorization_ref: str,
) -> None:
    """Reconstruct one independent runtime and emit only serializable facts."""

    try:
        services = _services(Path(root), owner_id=owner_id)
        commands = _commands(services)
        if mode == "reserve_then_exit":
            semantic = commands._semantic(
                revision_ref, action, expected_state_revision,
            )
            command_id = __import__(
                "core.external_extension_runtime.lifecycle_commands",
                fromlist=["_derived"],
            )._derived("command", semantic)
            lifecycle_intent = commands._ensure_intent(
                semantic,
                command_id,
                _reserve_authorization(
                    commands, revision_ref, semantic, authorization_ref,
                ),
            )
            output.put({
                "status": "reserved",
                "command_id": command_id,
                "intent_ref": lifecycle_intent.intent_ref,
            })
            return
        if mode != "execute":
            raise ValueError("unsupported lifecycle worker mode")
        if not start.wait(_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("lifecycle workers were never released")
        result = commands.execute(
            revision_ref,
            action,
            expected_state_revision,
            authorization_ref=authorization_ref,
        )
        output.put({
            "status": "completed",
            "command_id": result.command_id,
            "operation_id": result.effect.operation_id,
            "receipt_ref": result.effect.result_ref,
            "completed": result.completed,
        })
    except BaseException as error:  # send child failure to the parent assertion
        output.put({"status": "error", "error": repr(error)})


def _confirmed_acquire_backfill_worker(
    root: str,
    start: Any,
    output: Any,
    owner_id: str,
) -> None:
    """Run only the Core pre-dispatch acquire backfill in an isolated worker."""

    try:
        runtime_root = Path(root)
        runtime = build_effect_runtime(
            runtime_root / ".rebuild-data" / "jobs.sqlite3",
            owner_id=owner_id,
        )
        handles = register_external_extension_runtime(runtime_root, runtime)
        if not start.wait(_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("confirmed-acquire workers were never released")
        from core.effect_log import EffectRecoveryCoordinator

        coordinator = EffectRecoveryCoordinator(runtime)
        register_external_extension_recovery(coordinator, handles)
        backfill_completed, backfill_failed = coordinator._run_backfills()
        effects = tuple(runtime.log.planned_for_kinds((ACQUIRE_EFFECT_KIND,), limit=10))
        output.put({
            "status": "completed",
            "backfill_completed": backfill_completed,
            "backfill_failed": backfill_failed,
            "operation_ids": tuple(effect.operation_id for effect in effects),
            "states": tuple(effect.state.name for effect in effects),
        })
    except BaseException as error:  # send child failure to the parent assertion
        output.put({"status": "error", "error": repr(error)})


def _start_processes(*processes: multiprocessing.Process) -> None:
    for process in processes:
        process.start()


def _join_processes(*processes: multiprocessing.Process) -> None:
    timed_out: list[int | None] = []
    for process in processes:
        process.join(_PROCESS_TIMEOUT_SECONDS)
        if process.is_alive():
            timed_out.append(process.pid)
            process.terminate()
            process.join(_PROCESS_TIMEOUT_SECONDS)
    assert not timed_out, f"lifecycle worker timed out: {timed_out}"
    assert all(process.exitcode == 0 for process in processes), [
        process.exitcode for process in processes
    ]


def _outputs(queue: Any, count: int) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for _ in range(count):
        try:
            results.append(queue.get(timeout=_PROCESS_TIMEOUT_SECONDS))
        except Empty as error:
            raise AssertionError("lifecycle worker did not emit a result") from error
    errors = [item for item in results if item["status"] == "error"]
    assert not errors, errors
    return results


def test_same_lifecycle_semantic_is_one_effect_receipt_and_projection_across_processes(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services,
        _record_intake(
            services, suffix="multiprocess-1001", body=_skill_body("two process"),
        ),
    )
    authorization_ref = _source_authorization(
        services, revision, suffix="multiprocess-1001",
    )
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    workers = tuple(
        context.Process(
            target=_lifecycle_worker,
            args=(
                str(tmp_path), revision.revision_ref, installed.state_revision,
                "health", "execute", start, output, f"multiprocess-worker-{index}", authorization_ref,
            ),
        )
        for index in (1, 2)
    )
    try:
        _start_processes(*workers)
        start.set()
        _join_processes(*workers)
        results = _outputs(output, 2)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(_PROCESS_TIMEOUT_SECONDS)
        output.close()
        output.join_thread()

    assert {item["status"] for item in results} == {"completed"}
    assert {item["command_id"] for item in results} == {results[0]["command_id"]}
    assert {item["operation_id"] for item in results} == {results[0]["operation_id"]}

    rejoined = _commands(_services(tmp_path, owner_id="multiprocess-verifier")).load(
        revision.revision_ref, "health", installed.state_revision,
    )
    assert rejoined is not None and rejoined.completed
    assert rejoined.command_id == results[0]["command_id"]
    assert rejoined.effect.operation_id == results[0]["operation_id"]
    assert rejoined.effect.result_ref is not None
    assert rejoined.snapshot is not None
    assert rejoined.snapshot.candidate_status == "health_verified_disabled"


def test_same_uninstall_semantic_is_one_effect_receipt_and_tombstone_across_processes(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services,
        _record_intake(
            services, suffix="multiprocess-uninstall-1001",
            body=_skill_body("two process uninstall"),
        ),
    )
    source_authorization = _source_authorization(
        services, revision, suffix="multiprocess-uninstall-1001",
    )
    commands = _commands(services)
    healthy = commands.execute(
        revision.revision_ref, "health", installed.state_revision,
        authorization_ref=source_authorization,
    )
    assert healthy.snapshot is not None
    active = commands.execute(
        revision.revision_ref, "activation", healthy.snapshot.state_revision,
        authorization_ref=source_authorization,
    )
    assert active.snapshot is not None
    authorization_ref = services.facts.confirm_lifecycle_action(
        confirmation_id="multiprocess-uninstall-confirmation-1001",
        project_id="project-001",
        action="uninstall",
        revision_ref=revision.revision_ref,
        subject_ref=revision.intake_ref,
        expected_state_revision=active.snapshot.state_revision,
        actor="local-operator",
        reason="Uninstall the exact active revision in two concurrent workers.",
    )

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    workers = tuple(
        context.Process(
            target=_lifecycle_worker,
            args=(
                str(tmp_path), revision.revision_ref, active.snapshot.state_revision,
                "uninstall", "execute", start, output,
                f"multiprocess-uninstall-worker-{index}", authorization_ref,
            ),
        )
        for index in (1, 2)
    )
    try:
        _start_processes(*workers)
        start.set()
        _join_processes(*workers)
        results = _outputs(output, 2)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(_PROCESS_TIMEOUT_SECONDS)
        output.close()
        output.join_thread()

    assert {item["command_id"] for item in results} == {results[0]["command_id"]}
    assert {item["operation_id"] for item in results} == {results[0]["operation_id"]}
    assert {item["receipt_ref"] for item in results} == {results[0]["receipt_ref"]}
    verifier_services = _services(tmp_path, owner_id="uninstall-verifier")
    verifier = _commands(verifier_services)
    loaded = verifier.load(
        revision.revision_ref, "uninstall", active.snapshot.state_revision,
    )
    assert loaded is not None and loaded.completed
    assert loaded.snapshot is not None and loaded.snapshot.status == "uninstalled"
    tombstone = verifier_services.store.load_uninstall_tombstone(
        str(results[0]["operation_id"]),
    )
    assert tombstone["revision_ref"] == revision.revision_ref


def test_reservation_only_process_can_exit_and_later_process_rejoins_core(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services,
        _record_intake(
            services, suffix="multiprocess-2001", body=_skill_body("restart"),
        ),
    )
    authorization_ref = _source_authorization(
        services, revision, suffix="multiprocess-2001",
    )
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    reservation_output = context.Queue()
    reserver = context.Process(
        target=_lifecycle_worker,
        args=(
            str(tmp_path), revision.revision_ref, installed.state_revision,
            "health", "reserve_then_exit", start, reservation_output, "reservation-process", authorization_ref,
        ),
    )
    try:
        _start_processes(reserver)
        _join_processes(reserver)
        reservation = _outputs(reservation_output, 1)[0]
    finally:
        if reserver.is_alive():
            reserver.terminate()
            reserver.join(_PROCESS_TIMEOUT_SECONDS)
        reservation_output.close()
        reservation_output.join_thread()
    assert reservation["status"] == "reserved"

    execute_output = context.Queue()
    rejoiner = context.Process(
        target=_lifecycle_worker,
        args=(
            str(tmp_path), revision.revision_ref, installed.state_revision,
            "health", "execute", start, execute_output, "rejoin-process", authorization_ref,
        ),
    )
    try:
        _start_processes(rejoiner)
        start.set()
        _join_processes(rejoiner)
        result = _outputs(execute_output, 1)[0]
    finally:
        if rejoiner.is_alive():
            rejoiner.terminate()
            rejoiner.join(_PROCESS_TIMEOUT_SECONDS)
        execute_output.close()
        execute_output.join_thread()

    assert result["status"] == "completed"
    assert result["command_id"] == reservation["command_id"]
    assert result["completed"] is True
    verifier = _commands(_services(tmp_path, owner_id="restart-verifier"))
    loaded = verifier.load(revision.revision_ref, "health", installed.state_revision)
    assert loaded is not None and loaded.completed
    assert loaded.effect.operation_id == result["operation_id"]
    assert loaded.effect.result_ref == result["receipt_ref"]


def test_reservation_serializes_direct_upgrade_and_old_health_handler_cannot_bind(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    installed, revision = _install_one(
        services,
        _record_intake(
            services, suffix="multiprocess-3001", body=_skill_body("old revision"),
        ),
    )
    authorization_ref = _source_authorization(
        services, revision, suffix="multiprocess-3001",
    )
    upgrade = _record_intake(
        services,
        suffix="multiprocess-3002",
        body=_skill_body("new revision"),
        requested_ref=REVISION_B,
    )
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    reservation_output = context.Queue()
    reserver = context.Process(
        target=_lifecycle_worker,
        args=(
            str(tmp_path), revision.revision_ref, installed.state_revision,
            "health", "reserve_then_exit", start, reservation_output, "upgrade-reserver", authorization_ref,
        ),
    )
    try:
        _start_processes(reserver)
        _join_processes(reserver)
        reservation = _outputs(reservation_output, 1)[0]
    finally:
        if reserver.is_alive():
            reserver.terminate()
            reserver.join(_PROCESS_TIMEOUT_SECONDS)
        reservation_output.close()
        reservation_output.join_thread()
    assert reservation["status"] == "reserved"

    with pytest.raises(
        ExternalExtensionInstallationConflict,
        match="reserved by a lifecycle command",
    ):
        services.store.install_disabled(
            upgrade.reference,
            command_id="multiprocess-upgrade-command-3002",
            expected_state_revision=installed.state_revision,
            review_confirmation_ref=upgrade.confirmation_ref,
        )

    result = _commands(_services(tmp_path, owner_id="old-health-runner")).execute(
        revision.revision_ref, "health", installed.state_revision,
        authorization_ref=authorization_ref,
    )
    assert result.completed
    final = _services(tmp_path, owner_id="upgrade-verifier").store.load(
        "fixture-skill", root_id="project-001",
    )
    assert final.candidate_revision == 1
    assert final.candidate_revision_ref == revision.revision_ref
    assert final.candidate_status == "health_verified_disabled"
    # Health receives no binding authority.  The concurrent upgrade was denied
    # before it could replace the candidate, and the old handler did not make
    # any Application Skill binding durable.
    assert services.records.list("application_skill_bindings") == ()


def test_confirmed_fixed_acquire_backfill_is_single_effect_across_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Core workers may plan one confirmed acquire, never fetch it early."""

    from core.effect_log import EffectRecoveryCoordinator

    runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)
    install = _startup_install("multiprocess-confirmed-acquire-1001")
    _startup_source_authorization(handles, install)
    resolution_operation_id = "multiprocess-resolution-confirmed-acquire-1001"
    _record_resolution_without_acquire(
        handles,
        install,
        operation_id=resolution_operation_id,
    )

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    workers = tuple(
        context.Process(
            target=_confirmed_acquire_backfill_worker,
            args=(
                str(tmp_path), start, output,
                f"confirmed-acquire-backfill-worker-{index}",
            ),
        )
        for index in (1, 2)
    )
    try:
        _start_processes(*workers)
        start.set()
        _join_processes(*workers)
        results = _outputs(output, 2)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(_PROCESS_TIMEOUT_SECONDS)
        output.close()
        output.join_thread()

    assert {item["status"] for item in results} == {"completed"}
    assert fetcher.calls == []
    assert all(not item["backfill_failed"] for item in results)
    assert all(
        "external-extension-confirmed-acquire-intents" in item["backfill_completed"]
        for item in results
    )
    assert {
        operation_id
        for item in results
        for operation_id in item["operation_ids"]
    } == {results[0]["operation_ids"][0]}
    assert {
        state
        for item in results
        for state in item["states"]
    } == {EffectState.PLANNED.name}

    effects = tuple(runtime.log.planned_for_kinds((ACQUIRE_EFFECT_KIND,), limit=10))
    assert len(effects) == 1
    assert effects[0].state is EffectState.PLANNED

    coordinator = EffectRecoveryCoordinator(runtime)
    coordinator.recover_once(now=10)
    assert runtime.log.get(effects[0].operation_id).state is EffectState.SETTLED_OK
    assert fetcher.calls == [
        "https://codeload.github.com/example/extensions/zip/"
        + "a" * 40,
    ]
