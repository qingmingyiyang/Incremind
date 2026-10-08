"""Authoritative physical inventory for production Effect partitions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.effect_log import EffectRecoveryCoordinator, EffectRuntime


@dataclass(frozen=True, slots=True)
class EffectPartitionSpec:
    name: str
    database_relative_path: str
    owner_prefix: str
    lease_seconds: int
    state_attribute: str
    lease_heartbeat_seconds: int | None = None
    conditional_capability: str | None = None

    def __post_init__(self) -> None:
        relative = PurePosixPath(self.database_relative_path)
        if (
            not self.name
            or relative.is_absolute()
            or ".." in relative.parts
            or not self.owner_prefix
            or self.lease_seconds <= 0
            or not self.state_attribute
        ):
            raise ValueError("Effect partition specification is invalid")
        if (
            self.lease_heartbeat_seconds is not None
            and not 0 < self.lease_heartbeat_seconds < self.lease_seconds
        ):
            raise ValueError("Effect partition heartbeat is invalid")

    def database(self, root_dir: Path) -> Path:
        return Path(root_dir).resolve(strict=False).joinpath(*PurePosixPath(self.database_relative_path).parts)

    def owner_id(self, process_id: int) -> str:
        if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
            raise ValueError("Effect partition process identity is invalid")
        return f"{self.owner_prefix}{process_id}"


PRIMARY_EFFECT_PARTITION = EffectPartitionSpec(
    name="primary",
    database_relative_path=".rebuild-data/jobs.sqlite3",
    owner_prefix="api-sidecar:",
    lease_seconds=30,
    state_attribute="effect_runtime",
    lease_heartbeat_seconds=10,
)
AI_TURNS_EFFECT_PARTITION = EffectPartitionSpec(
    name="ai-turns",
    database_relative_path=".rebuild-data/ai-turns.sqlite3",
    owner_prefix="api-sidecar-ai:",
    lease_seconds=30,
    state_attribute="ai_effect_runtime",
)
PPT_MASTER_EFFECT_PARTITION = EffectPartitionSpec(
    name="ppt-master",
    database_relative_path=".rebuild-data/ppt-master-effects.sqlite3",
    owner_prefix="api-sidecar-ppt-master:",
    lease_seconds=360,
    lease_heartbeat_seconds=60,
    state_attribute="ppt_master_effect_runtime",
    conditional_capability="ppt_master_capability",
)

EFFECT_PARTITION_INVENTORY = (
    PRIMARY_EFFECT_PARTITION,
    AI_TURNS_EFFECT_PARTITION,
    PPT_MASTER_EFFECT_PARTITION,
)


def effect_partition_enabled(spec: EffectPartitionSpec, application_state: object) -> bool:
    """Return whether one inventory entry is enabled for this process.

    A conditional partition is disabled until its capability has completed its
    own bootstrap.  This keeps an allocated but unusable runtime out of the
    recovery scheduler.
    """

    return (
        getattr(application_state, spec.state_attribute, None) is not None
        and (
            spec.conditional_capability is None
            or getattr(application_state, spec.conditional_capability, None) is not None
        )
    )


def enabled_effect_partition_specs(application_state: object) -> tuple[EffectPartitionSpec, ...]:
    """Resolve enabled production partitions from the single inventory."""

    return tuple(
        spec for spec in EFFECT_PARTITION_INVENTORY
        if effect_partition_enabled(spec, application_state)
    )


def register_enabled_effect_partitions(
    coordinator: "EffectRecoveryCoordinator", application_state: object, *, root_dir: Path,
) -> tuple[str, ...]:
    """Bind exactly the enabled inventory runtimes to Core recovery.

    Startup code must call this after all conditional capabilities are
    bootstrapped.  It deliberately fails closed when a required runtime is
    absent or an independently registered partition drifts from inventory.
    """

    # The runtime import stays local so the inventory remains usable by health
    # projections without importing Core execution composition at module load.
    from core.effect_log import EffectRecoveryCoordinator, EffectRuntime

    if not isinstance(coordinator, EffectRecoveryCoordinator):
        raise TypeError("effect recovery coordinator is invalid")
    enabled = enabled_effect_partition_specs(application_state)
    names = tuple(spec.name for spec in enabled)
    if "primary" not in names:
        raise RuntimeError("primary Effect partition is not enabled")
    for spec in enabled:
        runtime = getattr(application_state, spec.state_attribute, None)
        if not isinstance(runtime, EffectRuntime):
            raise RuntimeError(f"enabled Effect partition runtime is unavailable: {spec.name}")
        _assert_partition_runtime(spec, runtime, root_dir=Path(root_dir))
        existing = coordinator.runtime_for_partition(spec.name)
        if existing is None:
            coordinator.register_partition(spec.name, runtime)
        elif existing is not runtime:
            raise RuntimeError(f"Effect partition runtime drifted: {spec.name}")
    coordinator.configure_expected_partitions(names)
    coordinator.assert_expected_partitions()
    return names


def _assert_partition_runtime(
    spec: EffectPartitionSpec, runtime: "EffectRuntime", *, root_dir: Path,
) -> None:
    """Fail closed when runtime composition drifts from its inventory entry."""

    actual_database = Path(runtime.log.database).resolve(strict=False)
    expected_database = spec.database(Path(root_dir))
    runner = runtime.runner
    if actual_database != expected_database:
        raise RuntimeError(f"Effect partition database drifted: {spec.name}")
    if (
        not isinstance(runner.owner_id, str)
        or not runner.owner_id.startswith(spec.owner_prefix)
        or len(runner.owner_id) <= len(spec.owner_prefix)
    ):
        raise RuntimeError(f"Effect partition owner drifted: {spec.name}")
    if float(runner.lease_seconds) != float(spec.lease_seconds):
        raise RuntimeError(f"Effect partition lease drifted: {spec.name}")
    actual_heartbeat = runner.lease_heartbeat_seconds
    expected_heartbeat = spec.lease_heartbeat_seconds
    if (actual_heartbeat is None) != (expected_heartbeat is None):
        raise RuntimeError(f"Effect partition heartbeat drifted: {spec.name}")
    if (
        actual_heartbeat is not None
        and expected_heartbeat is not None
        and float(actual_heartbeat) != float(expected_heartbeat)
    ):
        raise RuntimeError(f"Effect partition heartbeat drifted: {spec.name}")
