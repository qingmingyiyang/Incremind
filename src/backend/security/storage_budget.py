from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


MINIMUM_STREAM_RESERVE_BYTES = 512 * 1024 * 1024
STREAM_RESERVE_PERCENT = 5


class StreamStorageBudgetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StreamStorageBudget:
    incoming_bytes: int
    reserve_bytes: int
    required_free_bytes: int
    observed_free_bytes: int


def require_stream_storage_budget(
    root: Path,
    *,
    incoming_bytes: int,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> StreamStorageBudget:
    if not isinstance(incoming_bytes, int) or isinstance(incoming_bytes, bool) or incoming_bytes < 0:
        raise StreamStorageBudgetError("stream_storage_incoming_size_invalid")
    try:
        root.mkdir(parents=True, exist_ok=True)
        usage = disk_usage(root)
    except OSError as error:
        raise StreamStorageBudgetError("stream_storage_free_space_unavailable") from error
    observed = getattr(usage, "free", None)
    if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
        raise StreamStorageBudgetError("stream_storage_free_space_unavailable")
    reserve = max(
        MINIMUM_STREAM_RESERVE_BYTES,
        (incoming_bytes * STREAM_RESERVE_PERCENT + 99) // 100,
    )
    required = incoming_bytes + reserve
    budget = StreamStorageBudget(
        incoming_bytes=incoming_bytes,
        reserve_bytes=reserve,
        required_free_bytes=required,
        observed_free_bytes=observed,
    )
    if observed < required:
        raise StreamStorageBudgetError("stream_storage_budget_insufficient")
    return budget
