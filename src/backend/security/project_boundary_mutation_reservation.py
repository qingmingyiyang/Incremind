"""Cross-process reservation for all project Boundary mutation families."""
from __future__ import annotations

from contextlib import closing, contextmanager
from collections.abc import Iterator
from pathlib import Path
import re
import sqlite3

from backend.shared.interprocess_lock import interprocess_file_lock


class ProjectBoundaryMutationReservationConflict(ValueError):
    pass


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ProjectBoundaryMutationReservation:
    """One active command per project, independent of its command family.

    Active rows intentionally survive process death.  A replay of the exact
    command reacquires its reservation; terminal commands explicitly release
    it after their durable receipt transition.
    """

    def __init__(self, root_dir: Path) -> None:
        self._path = Path(root_dir) / ".rebuild-data" / "project-boundary-mutations.sqlite3"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with interprocess_file_lock(self._path):
                with closing(sqlite3.connect(self._path, timeout=5.0)) as conn:
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute("""CREATE TABLE IF NOT EXISTS project_boundary_mutation_reservations (
                        project_id TEXT PRIMARY KEY, command_id TEXT NOT NULL,
                        command_kind TEXT NOT NULL, semantic TEXT NOT NULL
                    )""")
                    conn.commit()
        except (sqlite3.OperationalError, TimeoutError) as error:
            raise ProjectBoundaryMutationReservationConflict(
                "Boundary mutation reservation is busy"
            ) from error

    @contextmanager
    def execution(self, project_id: str) -> Iterator[None]:
        """Serialize exact replays as well as competing command families."""
        project_id = _identity(project_id, "project")
        execution_path = self._path.with_name(
            f".{self._path.stem}.{project_id}.execution",
        )
        try:
            with interprocess_file_lock(execution_path):
                yield
        except TimeoutError as error:
            raise ProjectBoundaryMutationReservationConflict(
                "Boundary mutation execution is busy"
            ) from error

    def reserve(self, *, project_id: str, command_id: str, command_kind: str, semantic: str) -> None:
        project_id = _identity(project_id, "project")
        command_id = _identity(command_id, "command")
        command_kind = _identity(command_kind, "command kind")
        if not isinstance(semantic, str) or not semantic:
            raise ProjectBoundaryMutationReservationConflict(
                "Boundary mutation semantic is invalid"
            )
        try:
            with interprocess_file_lock(self._path):
                with closing(sqlite3.connect(self._path, timeout=5.0)) as conn:
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute("SELECT command_id, command_kind, semantic FROM project_boundary_mutation_reservations WHERE project_id=?", (project_id,)).fetchone()
                    if row is None:
                        conn.execute("INSERT INTO project_boundary_mutation_reservations VALUES(?,?,?,?)", (project_id, command_id, command_kind, semantic))
                    elif tuple(row) != (command_id, command_kind, semantic):
                        raise ProjectBoundaryMutationReservationConflict("another Boundary mutation command is active for this project")
                    conn.commit()
        except (sqlite3.OperationalError, TimeoutError) as error:
            raise ProjectBoundaryMutationReservationConflict("Boundary mutation reservation is busy") from error

    def release(self, *, project_id: str, command_id: str, command_kind: str) -> None:
        project_id = _identity(project_id, "project")
        command_id = _identity(command_id, "command")
        command_kind = _identity(command_kind, "command kind")
        try:
            with interprocess_file_lock(self._path):
                with closing(sqlite3.connect(self._path, timeout=5.0)) as conn:
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute("DELETE FROM project_boundary_mutation_reservations WHERE project_id=? AND command_id=? AND command_kind=?", (project_id, command_id, command_kind))
                    conn.commit()
        except (sqlite3.OperationalError, TimeoutError) as error:
            raise ProjectBoundaryMutationReservationConflict("Boundary mutation reservation is busy") from error


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ProjectBoundaryMutationReservationConflict(
            f"Boundary mutation {label} identity is invalid"
        )
    return value
