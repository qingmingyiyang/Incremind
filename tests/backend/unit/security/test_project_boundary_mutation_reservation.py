from pathlib import Path
from contextlib import contextmanager

import pytest

from backend.security.project_boundary_mutation_reservation import (
    ProjectBoundaryMutationReservation,
    ProjectBoundaryMutationReservationConflict,
)


def test_project_reservation_replays_same_command_and_blocks_other_kind(tmp_path: Path) -> None:
    reservation = ProjectBoundaryMutationReservation(tmp_path)
    reservation.reserve(project_id="alpha", command_id="mode-1", command_kind="mode", semantic="open|1|1")
    reservation.reserve(project_id="alpha", command_id="mode-1", command_kind="mode", semantic="open|1|1")
    with pytest.raises(ProjectBoundaryMutationReservationConflict):
        reservation.reserve(project_id="alpha", command_id="grant-1", command_kind="grant-create", semantic="target")


def test_terminal_release_allows_next_command_but_crash_active_stays_reserved(tmp_path: Path) -> None:
    reservation = ProjectBoundaryMutationReservation(tmp_path)
    reservation.reserve(project_id="alpha", command_id="mode-1", command_kind="mode", semantic="open|1|1")
    with pytest.raises(ProjectBoundaryMutationReservationConflict):
        ProjectBoundaryMutationReservation(tmp_path).reserve(project_id="alpha", command_id="mode-2", command_kind="mode", semantic="sealed|1|1")
    reservation.release(project_id="alpha", command_id="mode-1", command_kind="mode")
    reservation.reserve(project_id="alpha", command_id="grant-1", command_kind="grant-create", semantic="target")


def test_file_lock_timeout_is_a_controlled_reservation_conflict(tmp_path: Path, monkeypatch) -> None:
    @contextmanager
    def busy(_path):
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr(
        "backend.security.project_boundary_mutation_reservation.interprocess_file_lock",
        busy,
    )
    with pytest.raises(ProjectBoundaryMutationReservationConflict, match="busy"):
        ProjectBoundaryMutationReservation(tmp_path)
