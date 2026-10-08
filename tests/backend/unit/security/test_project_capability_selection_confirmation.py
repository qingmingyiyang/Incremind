from __future__ import annotations

import multiprocessing
from pathlib import Path
import sqlite3

import pytest

from backend.security.project_capability_selection_confirmation import (
    ProjectCapabilitySelectionConfirmationError,
    ProjectCapabilitySelectionConfirmationStore,
)


_BINDING = {
    "project_id": "alpha",
    "action": "select",
    "target_stable_id": "memory.recall",
    "command_id": "select-1",
    "expected_boundary_revision": 1,
    "expected_capability_revision": 2,
    "expected_registry_generation": 3,
    "contract_version": 4,
}


def _consume(root: str, token: str, ready, start, results) -> None:
    ready.put(True)
    start.wait(10)
    try:
        ProjectCapabilitySelectionConfirmationStore(Path(root)).consume_exact(
            token=token, **_BINDING,
        )
        results.put("consumed")
    except Exception as error:
        results.put((type(error).__name__, str(error)))


def test_issue_stores_only_a_verifier_and_consume_is_single_use(tmp_path: Path) -> None:
    store = ProjectCapabilitySelectionConfirmationStore(tmp_path)
    token = store.issue(**_BINDING)

    with sqlite3.connect(
        tmp_path / ".rebuild-data" / "capability-selection-confirmations.sqlite3"
    ) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(capability_selection_confirmations)")}
        row = conn.execute("SELECT token_verifier FROM capability_selection_confirmations").fetchone()
    assert "token" not in columns
    assert token not in row[0] and len(row[0]) == 64

    consumed = store.consume_exact(token=token, **_BINDING)
    assert consumed.project_id == "alpha"
    assert consumed.contract_version == 4
    with pytest.raises(ProjectCapabilitySelectionConfirmationError) as error:
        store.consume_exact(token=token, **_BINDING)
    assert str(token) not in str(error.value)


def test_consume_requires_every_bound_value(tmp_path: Path) -> None:
    store = ProjectCapabilitySelectionConfirmationStore(tmp_path)
    token = store.issue(**_BINDING)
    with pytest.raises(ProjectCapabilitySelectionConfirmationError):
        store.consume_exact(token=token, **(_BINDING | {"contract_version": 5}))
    assert store.consume_exact(token=token, **_BINDING).command_id == "select-1"


def test_confirmation_expires_after_five_minutes(tmp_path: Path) -> None:
    clock = [1000.0]
    store = ProjectCapabilitySelectionConfirmationStore(tmp_path, clock=lambda: clock[0])
    token = store.issue(**_BINDING)
    clock[0] += 300
    with pytest.raises(ProjectCapabilitySelectionConfirmationError):
        store.consume_exact(token=token, **_BINDING)


def test_cross_process_consume_allows_exactly_one_winner(tmp_path: Path) -> None:
    token = ProjectCapabilitySelectionConfirmationStore(tmp_path).issue(**_BINDING)
    context = multiprocessing.get_context("spawn")
    ready, start, results = context.Queue(), context.Event(), context.Queue()
    processes = [
        context.Process(target=_consume, args=(str(tmp_path), token, ready, start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=15) is True
    assert ready.get(timeout=15) is True
    start.set()
    output = [results.get(timeout=20), results.get(timeout=20)]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert output.count("consumed") == 1
    rejected = next(item for item in output if item != "consumed")
    assert rejected[0] == "ProjectCapabilitySelectionConfirmationError"
    assert token not in rejected[1]
