from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from threading import Event, RLock, Thread
from time import time
from typing import Callable
from uuid import uuid4

from .core import (
    Effect,
    EffectClass,
    EffectIntent,
    GateDecisionFact,
    EffectReceipt,
    EffectLeaseFence,
    InvalidEffectTransition,
    EFFECT_V2,
    LEGACY_V1,
    EffectLog,
    EffectReaper,
    EffectRunner,
    EffectState,
    Handler,
    Probe,
    RecoveryOutcome,
)


RecoveryAction = Callable[[Effect], tuple[EffectState, str | None]]
CoordinationTask = Callable[[], object]
EffectBackfillTask = Callable[[], object]
RecoveryPreparationTask = Callable[[], object]

_SHARED_RUNNER_LOCK = RLock()
_SHARED_RUNNERS: dict[tuple[str, str, int], EffectRunner] = {}


class EffectExecutionCancelled(RuntimeError):
    """A Core cancellation fact stopped a cooperative domain operation."""


@dataclass(slots=True)
class EffectLeaseCheckpoint:
    """Cooperative domain checkpoint backed only by Core Effect authority.

    The checkpoint retains the claim generation. Each use proves that the
    same INFLIGHT generation is still owned and checks the Core cancellation
    fact. Lease renewal remains exclusively in the Core Runner loop: a domain
    checkpoint never mutates a fence behind the Runner's back.
    It deliberately has no Job lease, Job state, or domain recovery state.
    """

    runtime: "EffectRuntime"
    _fence: EffectLeaseFence
    _clock: Callable[[], float] = time

    @classmethod
    def for_claim(
        cls, runtime: "EffectRuntime", effect: Effect,
        *, clock: Callable[[], float] = time,
    ) -> "EffectLeaseCheckpoint":
        return cls(runtime, EffectLeaseFence.from_effect(effect), clock)

    def checkpoint(self) -> None:
        now = int(self._clock())
        log = self.runtime.log
        with log._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = log.get_in_connection(connection, self._fence.operation_id)
                if (
                    current.state is not EffectState.INFLIGHT
                    or current.lease_owner != self._fence.owner_id
                    or current.attempt != self._fence.generation
                    or current.lease_expires_at is None
                    or current.lease_expires_at < self._fence.lease_expires_at
                ):
                    raise InvalidEffectTransition("Effect lease fence is stale or expired")
                log.assert_active_fence_in_connection(
                    connection, EffectLeaseFence.from_effect(current), now=now,
                )
                cancelled = connection.execute(
                    "SELECT 1 FROM effect_cancellation_request WHERE operation_id=?",
                    (self._fence.operation_id,),
                ).fetchone()
                if cancelled is not None:
                    raise EffectExecutionCancelled(
                        "effect execution was cancelled by a Core cancellation fact"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise


def _new_effect_runner(
    log: EffectLog, *, owner_id: str, lease_seconds: int | float,
    lease_heartbeat_seconds: int | float | None = None,
) -> EffectRunner:
    return EffectRunner(log, owner_id=owner_id, lease_seconds=lease_seconds, lease_heartbeat_seconds=lease_heartbeat_seconds)


def shared_effect_runner(
    database: str | Path, *, owner_role: str, lease_seconds: int = 30,
) -> EffectRunner:
    """Return the process-owned Runner for one database and execution role.

    Standalone composition and focused tests use this boundary. Production
    application composition injects its already-built Runner explicitly.
    """

    path = str(Path(database).expanduser().resolve(strict=False))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    key = (path, owner_role, lease_seconds)
    with _SHARED_RUNNER_LOCK:
        runner = _SHARED_RUNNERS.get(key)
        if runner is None:
            runner = _new_effect_runner(
                EffectLog(path),
                owner_id=f"{owner_role}:{os.getpid()}:{uuid4().hex}",
                lease_seconds=lease_seconds,
            )
            _SHARED_RUNNERS[key] = runner
        return runner


@dataclass(frozen=True, slots=True)
class EffectRecoveryRegistration:
    """Domain policy loaded by Core without granting scheduling ownership."""

    kind: str
    effect_class: EffectClass
    probe: Probe | None = None
    verify: RecoveryAction | None = None
    compensate: RecoveryAction | None = None
    reauthorize: RecoveryAction | None = None
    contract_version: str = LEGACY_V1

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("effect recovery kind must be non-empty")
        if not isinstance(self.effect_class, EffectClass):
            raise TypeError("effect recovery class must be an EffectClass")
        if self.contract_version not in {LEGACY_V1, EFFECT_V2}:
            raise ValueError("unsupported Effect recovery contract version")
        actions = (self.probe, self.verify, self.compensate, self.reauthorize)
        if not any(callable(action) for action in actions):
            raise ValueError("effect recovery requires at least one domain strategy")
        if any(action is not None and not callable(action) for action in actions):
            raise TypeError("effect recovery strategies must be callable")
        if self.effect_class is EffectClass.QUERYABLE and self.probe is None:
            raise ValueError("QUERYABLE recovery requires a probe")
        if self.effect_class is EffectClass.NEEDS_REAUTH and self.reauthorize is None:
            raise ValueError("NEEDS_REAUTH recovery requires reauthorize")


@dataclass(frozen=True, slots=True)
class EffectHandlerRegistration:
    kind: str
    effect_class: EffectClass
    handler: Handler
    probe: Probe | None = None
    contract_version: str = LEGACY_V1
    intent_schema_version: str = "legacy-v1"
    receipt_kind: str | None = None
    receipt_schema_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("effect handler kind must be non-empty")
        if not isinstance(self.effect_class, EffectClass):
            raise TypeError("effect handler class must be an EffectClass")
        if not callable(self.handler):
            raise TypeError("effect handler must be callable")
        if self.contract_version not in {LEGACY_V1, EFFECT_V2}:
            raise ValueError("unsupported Effect handler contract version")
        if self.contract_version == EFFECT_V2:
            if not self.receipt_kind or not self.receipt_schema_version:
                raise ValueError("v2 handler requires immutable receipt schema declaration")
        if self.effect_class is EffectClass.QUERYABLE and not callable(self.probe):
            raise ValueError("QUERYABLE effect handlers require a probe")
        if self.effect_class is not EffectClass.QUERYABLE and self.probe is not None:
            raise ValueError("only QUERYABLE effect handlers may register a probe")


class EffectHandlerRegistry:
    """Core-owned loading protocol for effect handlers and recovery strategies."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._registrations: dict[tuple[str, str], EffectHandlerRegistration] = {}

    def register(self, registration: EffectHandlerRegistration) -> None:
        if not isinstance(registration, EffectHandlerRegistration):
            raise TypeError("registration must be an EffectHandlerRegistration")
        with self._lock:
            key = (registration.kind, registration.contract_version)
            if key in self._registrations:
                raise ValueError(f"effect handler already registered: {registration.kind}/{registration.contract_version}")
            self._registrations[key] = registration

    def resolve(self, intent: EffectIntent) -> EffectHandlerRegistration:
        with self._lock:
            registration = self._registrations.get((intent.kind, intent.contract_version))
        if registration is None:
            raise KeyError(f"effect handler is not registered: {intent.kind}/{intent.contract_version}")
        if registration.effect_class is not intent.effect_class:
            raise ValueError("effect intent class drifted from handler registration")
        if intent.contract_version == EFFECT_V2 and (
            registration.intent_schema_version != intent.intent_schema_version
            or registration.receipt_kind != intent.expected_receipt_kind
            or registration.receipt_schema_version != intent.expected_receipt_schema_version
        ):
            raise ValueError("v2 handler contract drifted from frozen Effect intent")
        return registration

    def probes(self) -> dict[tuple[str, str], Probe]:
        with self._lock:
            return {
                (kind, version): registration.probe
                for (kind, version), registration in self._registrations.items()
                if registration.probe is not None
            }

    def kinds(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted({kind for kind, _version in self._registrations}))


class EffectRecoveryRegistry:
    """Core-owned registry; domains supply policy functions, never schedulers."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._registrations: dict[tuple[str, str], EffectRecoveryRegistration] = {}

    def register(self, registration: EffectRecoveryRegistration) -> None:
        if not isinstance(registration, EffectRecoveryRegistration):
            raise TypeError("registration must be an EffectRecoveryRegistration")
        with self._lock:
            key = (registration.kind, registration.contract_version)
            if key in self._registrations:
                raise ValueError(f"effect recovery already registered: {registration.kind}/{registration.contract_version}")
            self._registrations[key] = registration

    def probes(self) -> dict[object, Probe]:
        with self._lock:
            return {
                (kind if version == LEGACY_V1 else (kind, version)): registration.probe
                for (kind, version), registration in self._registrations.items()
                if registration.probe is not None
            }

    def verifiers(self) -> dict[object, RecoveryAction]:
        with self._lock:
            return {
                (kind if version == LEGACY_V1 else (kind, version)): registration.verify
                for (kind, version), registration in self._registrations.items()
                if registration.verify is not None
            }

    def reauthorizers(self) -> dict[object, RecoveryAction]:
        with self._lock:
            return {
                (kind if version == LEGACY_V1 else (kind, version)): registration.reauthorize
                for (kind, version), registration in self._registrations.items()
                if registration.reauthorize is not None
            }

    def kinds(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted({kind for kind, _version in self._registrations}))


@dataclass(frozen=True, slots=True)
class CoordinationTaskRegistration:
    """Non-effect orchestration allowed only after the Core Reaper pass."""

    kind: str
    run: CoordinationTask

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("coordination task kind must be non-empty")
        if not callable(self.run):
            raise TypeError("coordination task must be callable")


@dataclass(frozen=True, slots=True)
class EffectBackfillRegistration:
    """Intent-only migration source run before Core recovery.

    The callback may create missing Effect intents from legacy durable records.
    It must not call a Handler, settle an Effect, or mutate external authority.
    """

    kind: str
    run: EffectBackfillTask

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("effect backfill kind must be non-empty")
        if not callable(self.run):
            raise TypeError("effect backfill task must be callable")


@dataclass(frozen=True, slots=True)
class RecoveryPreparationRegistration:
    """Local projection/outbox preparation run before Effect backfill."""

    kind: str
    run: RecoveryPreparationTask

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("recovery preparation kind must be non-empty")
        if not callable(self.run):
            raise TypeError("recovery preparation task must be callable")


@dataclass(frozen=True, slots=True)
class RecoveryPassReport:
    preparation_completed: tuple[str, ...]
    preparation_failed: tuple[str, ...]
    backfill_completed: tuple[str, ...]
    backfill_failed: tuple[str, ...]
    effect_outcomes: tuple[RecoveryOutcome, ...]
    coordination_completed: tuple[str, ...]
    coordination_failed: tuple[str, ...]
    partition_recovery: tuple["PartitionRecoveryPass", ...] = ()


@dataclass(frozen=True, slots=True)
class PartitionRecoveryPass:
    """Bounded partition attribution for one recovery pass.

    Operation identifiers and domain error details remain on the internal
    ``effect_outcomes`` compatibility field.  This aggregate is safe for
    service snapshots and health projections.
    """

    partition: str
    recovered_count: int
    status: str
    completed_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.partition, str) or not self.partition.strip():
            raise ValueError("recovery partition name is invalid")
        if self.recovered_count < 0:
            raise ValueError("recovery count is invalid")
        if self.status not in {"idle", "recovered"}:
            raise ValueError("recovery partition status is invalid")


class EffectRecoveryCoordinator:
    """The only production scheduler for durable effect recovery."""

    def __init__(self, runtime: "EffectRuntime") -> None:
        self._lock = RLock()
        self._partitions: dict[str, EffectRuntime] = {"primary": runtime}
        self._coordination: dict[str, CoordinationTaskRegistration] = {}
        self._backfills: dict[str, EffectBackfillRegistration] = {}
        self._preparations: dict[str, RecoveryPreparationRegistration] = {}
        self._expected_partition_names: frozenset[str] | None = None
        self._last_partition_passes: dict[str, PartitionRecoveryPass] = {}

    def register_partition(self, name: str, runtime: "EffectRuntime") -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("effect recovery partition name must be non-empty")
        if not isinstance(runtime, EffectRuntime):
            raise TypeError("effect recovery partition must be an EffectRuntime")
        with self._lock:
            if (
                self._expected_partition_names is not None
                and name not in self._expected_partition_names
            ):
                raise RuntimeError(f"unapproved Effect recovery partition: {name}")
            if name in self._partitions:
                raise ValueError(f"effect recovery partition already registered: {name}")
            self._partitions[name] = runtime

    def runtime_for_partition(self, name: str) -> "EffectRuntime | None":
        if not isinstance(name, str) or not name.strip():
            raise ValueError("effect recovery partition name must be non-empty")
        with self._lock:
            return self._partitions.get(name)

    def configure_expected_partitions(self, names: tuple[str, ...]) -> None:
        """Freeze the exact production partition set before scheduling.

        Standalone callers retain the historical unconfigured coordinator;
        production composition calls this through the inventory registry.
        """

        if (
            not isinstance(names, tuple)
            or not names
            or any(not isinstance(name, str) or not name.strip() for name in names)
            or len(set(names)) != len(names)
            or "primary" not in names
        ):
            raise ValueError("expected Effect recovery partitions are invalid")
        expected = frozenset(names)
        with self._lock:
            if self._expected_partition_names not in {None, expected}:
                raise RuntimeError("Effect recovery partition expectations already configured")
            self._expected_partition_names = expected

    def expected_partition_names(self) -> tuple[str, ...] | None:
        with self._lock:
            if self._expected_partition_names is None:
                return None
            return tuple(sorted(self._expected_partition_names))

    def assert_expected_partitions(self) -> None:
        with self._lock:
            expected = self._expected_partition_names
            actual = frozenset(self._partitions)
        if expected is not None and actual != expected:
            raise RuntimeError("Effect recovery partition registration drift")

    def partition_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._partitions))

    def partition_recovery_snapshot(self) -> tuple[PartitionRecoveryPass, ...]:
        """Safe per-partition pass facts; never exposes operation data."""

        with self._lock:
            return tuple(
                self._last_partition_passes[name]
                for name in sorted(self._last_partition_passes)
            )

    def register_backfill(self, registration: EffectBackfillRegistration) -> None:
        if not isinstance(registration, EffectBackfillRegistration):
            raise TypeError("effect backfill must be an EffectBackfillRegistration")
        with self._lock:
            if registration.kind in self._backfills:
                raise ValueError(f"effect backfill already registered: {registration.kind}")
            self._backfills[registration.kind] = registration

    def register_preparation(self, registration: RecoveryPreparationRegistration) -> None:
        if not isinstance(registration, RecoveryPreparationRegistration):
            raise TypeError("recovery preparation must be a RecoveryPreparationRegistration")
        with self._lock:
            if registration.kind in self._preparations:
                raise ValueError(f"recovery preparation already registered: {registration.kind}")
            self._preparations[registration.kind] = registration

    def preparation_kinds(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._preparations))

    def backfill_kinds(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._backfills))

    def register_coordination(self, registration: CoordinationTaskRegistration) -> None:
        if not isinstance(registration, CoordinationTaskRegistration):
            raise TypeError("registration must be a CoordinationTaskRegistration")
        with self._lock:
            if registration.kind in self._coordination:
                raise ValueError(f"coordination task already registered: {registration.kind}")
            self._coordination[registration.kind] = registration

    def coordination_kinds(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._coordination))

    def recover_once(self, *, now: int, limit: int = 100) -> RecoveryPassReport:
        self.assert_expected_partitions()
        preparation_completed, preparation_failed = self._run_preparations()
        backfill_completed, backfill_failed = self._run_backfills()
        effect_outcomes, partition_recovery = self._recover_effects(now=now, limit=limit)
        self._dispatch_planned(now=now, limit=limit)
        with self._lock:
            coordination = tuple(self._coordination.values())
        coordination_completed: list[str] = []
        coordination_failed: list[str] = []
        for task in coordination:
            try:
                task.run()
            except Exception:
                coordination_failed.append(task.kind)
            else:
                coordination_completed.append(task.kind)
        return RecoveryPassReport(
            preparation_completed, preparation_failed,
            backfill_completed, backfill_failed, effect_outcomes,
            tuple(coordination_completed), tuple(coordination_failed), partition_recovery,
        )

    def recover_effects_and_coordinate(
        self, *, now: int, limit: int = 100,
    ) -> RecoveryPassReport:
        self.assert_expected_partitions()
        preparation_completed, preparation_failed = self._run_preparations()
        backfill_completed, backfill_failed = self._run_backfills()
        effect_outcomes, partition_recovery = self._recover_effects(now=now, limit=limit)
        self._dispatch_planned(now=now, limit=limit)
        with self._lock:
            coordination = tuple(self._coordination.values())
        completed: list[str] = []
        failed: list[str] = []
        for task in coordination:
            try:
                task.run()
            except Exception:
                failed.append(task.kind)
            else:
                completed.append(task.kind)
        return RecoveryPassReport(
            preparation_completed, preparation_failed,
            backfill_completed, backfill_failed, effect_outcomes,
            tuple(completed), tuple(failed), partition_recovery,
        )

    def _run_preparations(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self._lock:
            preparations = tuple(self._preparations.values())
        completed: list[str] = []
        failed: list[str] = []
        for preparation in preparations:
            try:
                preparation.run()
            except Exception:
                failed.append(preparation.kind)
            else:
                completed.append(preparation.kind)
        return tuple(completed), tuple(failed)

    def _run_backfills(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self._lock:
            backfills = tuple(self._backfills.values())
        completed: list[str] = []
        failed: list[str] = []
        for backfill in backfills:
            try:
                backfill.run()
            except Exception:
                failed.append(backfill.kind)
            else:
                completed.append(backfill.kind)
        return tuple(completed), tuple(failed)

    def _recover_effects(
        self, *, now: int, limit: int,
    ) -> tuple[tuple[RecoveryOutcome, ...], tuple[PartitionRecoveryPass, ...]]:
        with self._lock:
            partitions = tuple(sorted(self._partitions.items()))
        outcomes: list[RecoveryOutcome] = []
        passes: list[PartitionRecoveryPass] = []
        for name, runtime in partitions:
            recovered = tuple(runtime.recover_expired(now=now, limit=limit))
            outcomes.extend(recovered)
            passes.append(PartitionRecoveryPass(
                partition=name,
                recovered_count=len(recovered),
                status=("recovered" if recovered else "idle"),
                completed_at=now,
            ))
        with self._lock:
            self._last_partition_passes = {item.partition: item for item in passes}
        return tuple(outcomes), tuple(passes)

    def _dispatch_planned(self, *, now: int, limit: int) -> None:
        with self._lock:
            partitions = tuple(self._partitions.values())
        for runtime in partitions:
            runtime.dispatch_planned(now=now, limit=limit)


class EffectRecoveryService:
    """Single bounded periodic scheduler for all Core Effect partitions."""

    def __init__(
        self, coordinator: EffectRecoveryCoordinator, *, interval_seconds: float = 1.0,
        clock: Callable[[], float],
    ) -> None:
        if not isinstance(coordinator, EffectRecoveryCoordinator):
            raise TypeError("recovery service coordinator is invalid")
        if interval_seconds <= 0:
            raise ValueError("recovery service interval must be positive")
        if not callable(clock):
            raise TypeError("recovery service clock is invalid")
        self._coordinator = coordinator
        self._interval = float(interval_seconds)
        self._clock = clock
        self._stop = Event()
        self._snapshot_lock = RLock()
        self._last_started_at: int | None = None
        self._last_finished_at: int | None = None
        self._last_failure_type: str | None = None
        self._last_status = "not_started"
        self._thread = Thread(target=self._run, name="core-effect-reaper", daemon=True)

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._thread.start()

    def shutdown(self, *, timeout_seconds: float = 2.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("recovery service shutdown timeout must be non-negative")
        self._stop.set()
        self._thread.join(timeout=float(timeout_seconds))
        return not self._thread.is_alive()

    def snapshot(self) -> dict[str, object]:
        """Safe aggregate-only view for health endpoints; no operation data."""
        with self._snapshot_lock:
            return {
                "running": self._thread.is_alive() and not self._stop.is_set(),
                "status": self._last_status,
                "last_outcome": {
                    "not_started": "not_run",
                    "ready": "ok",
                    "failed": "failed",
                }.get(self._last_status, "failed"),
                "last_started_at": self._last_started_at,
                "last_completed_at": self._last_finished_at,
                "last_finished_at": self._last_finished_at,
                "last_failure_type": self._last_failure_type,
            }

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            started_at = int(self._clock())
            with self._snapshot_lock:
                self._last_started_at = started_at
            try:
                self._coordinator.recover_effects_and_coordinate(
                    now=started_at, limit=100,
                )
            except Exception as error:
                with self._snapshot_lock:
                    self._last_status = "failed"
                    self._last_finished_at = int(self._clock())
                    self._last_failure_type = type(error).__name__
                continue
            with self._snapshot_lock:
                self._last_status = "ready"
                self._last_finished_at = int(self._clock())
                self._last_failure_type = None


@dataclass(frozen=True, slots=True)
class EffectRuntime:
    log: EffectLog
    runner: EffectRunner
    reaper: EffectReaper
    handlers: EffectHandlerRegistry
    recoveries: EffectRecoveryRegistry

    def execute(self, intent: EffectIntent, *, now: int) -> Effect:
        if intent.contract_version == EFFECT_V2:
            raise ValueError("effect-v2 requires EffectRuntime.execute_v2 and GateDecisionFact")
        registration = self.handlers.resolve(intent)
        return self.runner.execute(intent, registration.handler, now=now)

    def execute_v2(
        self, intent: EffectIntent, *, gate_decision_id: str,
        gate_fact: GateDecisionFact, now: int,
    ) -> Effect:
        if intent.contract_version != EFFECT_V2:
            raise ValueError("execute_v2 requires an effect-v2 intent")
        registration = self.handlers.resolve(intent)
        return self.runner.execute_v2(
            intent, registration.handler, gate_decision_id=gate_decision_id,
            gate_fact=gate_fact, now=now,
        )

    def recover_expired(self, *, now: int, limit: int = 100) -> list[RecoveryOutcome]:
        probes = self.handlers.probes()
        probes.update(self.recoveries.probes())
        return self.reaper.recover_expired(
            now=now,
            probes=probes,
            verifiers=self.recoveries.verifiers(),
            reauthorizers=self.recoveries.reauthorizers(),
            limit=limit,
        )

    def dispatch_planned(self, *, now: int, limit: int = 100) -> tuple[Effect, ...]:
        """Consume only registered PLANNED kinds through the Core Runner."""

        dispatched: list[Effect] = []
        for effect in self.log.planned_for_kinds(self.handlers.kinds(), limit=limit):
            registration = self.handlers.resolve(effect)
            try:
                result = self.runner.execute_planned(
                    effect.operation_id,
                    registration.handler,
                    now=now,
                    receipt_kind=(effect.expected_receipt_kind if effect.contract_version == EFFECT_V2
                                  else f"{effect.kind}-receipt"),
                )
            except Exception:
                # The claimed Effect remains INFLIGHT. Core Reaper owns the
                # next decision after its lease expires.
                continue
            dispatched.append(result)
        return tuple(dispatched)

    def dispatch_operation(self, operation_id: str, *, now: int) -> Effect:
        """Dispatch one registered operation through the Core Runner.

        Callers may use this for low-latency wake-up after durably planning an
        Effect.  It never invokes an unregistered Handler and the Runner CAS is
        still the only execution claim.
        """

        effect = self.log.get(operation_id)
        registration = self.handlers.resolve(effect)
        return self.runner.execute_planned(
            operation_id,
            registration.handler,
            now=now,
            receipt_kind=(effect.expected_receipt_kind if effect.contract_version == EFFECT_V2
                          else f"{effect.kind}-receipt"),
        )


def build_effect_runtime(
    database: str | Path,
    *,
    owner_id: str,
    lease_seconds: int | float = 30,
    lease_heartbeat_seconds: int | float | None = None,
) -> EffectRuntime:
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    log = EffectLog(database_path)
    return EffectRuntime(
        log=log,
        runner=_new_effect_runner(log, owner_id=owner_id, lease_seconds=lease_seconds, lease_heartbeat_seconds=lease_heartbeat_seconds),
        reaper=EffectReaper(log),
        handlers=EffectHandlerRegistry(),
        recoveries=EffectRecoveryRegistry(),
    )
