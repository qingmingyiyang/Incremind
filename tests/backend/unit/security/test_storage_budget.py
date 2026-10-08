from __future__ import annotations

from collections import namedtuple

import pytest

from backend.security import (
    MINIMUM_STREAM_RESERVE_BYTES,
    StreamStorageBudgetError,
    require_stream_storage_budget,
)


DiskUsage = namedtuple("DiskUsage", "total used free")


def test_stream_budget_requires_input_plus_reserve_before_writing(tmp_path) -> None:
    incoming = 10 * 1024 * 1024
    required = incoming + MINIMUM_STREAM_RESERVE_BYTES

    budget = require_stream_storage_budget(
        tmp_path / "incoming",
        incoming_bytes=incoming,
        disk_usage=lambda _path: DiskUsage(required, 0, required),
    )

    assert budget.required_free_bytes == required
    assert budget.observed_free_bytes == required


def test_stream_budget_uses_five_percent_for_very_large_input(tmp_path) -> None:
    incoming = 16 * 1024 * 1024 * 1024
    expected_reserve = (incoming * 5 + 99) // 100

    budget = require_stream_storage_budget(
        tmp_path,
        incoming_bytes=incoming,
        disk_usage=lambda _path: DiskUsage(incoming * 2, 0, incoming * 2),
    )

    assert budget.reserve_bytes == expected_reserve


def test_stream_budget_fails_closed_before_partial_creation(tmp_path) -> None:
    incoming_root = tmp_path / "incoming"

    with pytest.raises(StreamStorageBudgetError, match="budget_insufficient"):
        require_stream_storage_budget(
            incoming_root,
            incoming_bytes=1024,
            disk_usage=lambda _path: DiskUsage(1024, 1024, 0),
        )

    assert incoming_root.is_dir()
    assert list(incoming_root.iterdir()) == []


def test_stream_budget_fails_closed_when_free_space_cannot_be_observed(tmp_path) -> None:
    def unavailable(_path):
        raise OSError("disk probe failed")

    with pytest.raises(StreamStorageBudgetError, match="free_space_unavailable"):
        require_stream_storage_budget(
            tmp_path / "incoming",
            incoming_bytes=1024,
            disk_usage=unavailable,
        )


@pytest.mark.parametrize("incoming", [-1, True, 1.5])
def test_stream_budget_rejects_invalid_declared_size(tmp_path, incoming) -> None:
    with pytest.raises(StreamStorageBudgetError, match="incoming_size_invalid"):
        require_stream_storage_budget(tmp_path, incoming_bytes=incoming)
