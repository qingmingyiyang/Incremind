from __future__ import annotations

from datetime import datetime, timezone
import json
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread

import pytest

from backend.security.project_boundary_profiles import (
    ProjectBoundaryProfileConflict,
    ProjectBoundaryProfileError,
    ProjectBoundaryProfileStore,
)
from core.ai_boundary import BoundaryGrant


def _reentrant_profile_read_worker(root: str, ready: object, completed: object) -> None:
    """Run in a child process so a regression remains bounded, not hung."""
    store = ProjectBoundaryProfileStore(Path(root))
    with store.locked_snapshot("project-a"):
        ready.put("locked")
        snapshot = store.get("project-a")
        completed.put(snapshot.profile.revision)


def test_missing_profile_resolves_to_non_persisted_guarded_default(tmp_path) -> None:
    snapshot = ProjectBoundaryProfileStore(tmp_path).get("project-a")
    assert snapshot.profile.mode == "guarded"
    assert snapshot.profile.remote_default == "review"
    assert snapshot.store_revision == 0
    assert snapshot.persisted is False
    assert not (tmp_path / "library/projects/project-a/boundary/profile.json").exists()


def test_update_owns_revision_and_round_trips_grant(tmp_path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    grant = BoundaryGrant(
        grant_id="grant-1",
        subject_id="agent-main",
        project_id="project-a",
        target_id="document.write",
        actions=("write",),
        data_classes=("project_content",),
        destinations=("local",),
        expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        revision=1,
        redaction_required=False,
    )
    created = store.update(
        "project-a",
        mode="open",
        remote_default="allow",
        enabled_sources=("core", "user-skills"),
        denied_effects=("delete",),
        persistent_grants=(grant,),
        expected_revision=0,
    )
    loaded = ProjectBoundaryProfileStore(tmp_path).get("project-a")

    assert created.store_revision == 1
    assert loaded == created
    assert loaded.profile.persistent_grants == (grant,)


def test_compare_and_swap_rejects_stale_writer(tmp_path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    store.update("project-a", mode="guarded", remote_default="review", expected_revision=0)
    with pytest.raises(ProjectBoundaryProfileConflict, match="expected 0, current 1"):
        store.update("project-a", mode="open", remote_default="allow", expected_revision=0)


@pytest.mark.parametrize("project_id", ["../escape", "project/a", "project\\a", "", ".hidden"])
def test_project_identity_cannot_escape_profile_authority(tmp_path, project_id: str) -> None:
    with pytest.raises(ProjectBoundaryProfileError, match="identity is invalid"):
        ProjectBoundaryProfileStore(tmp_path).get(project_id)


def test_corrupt_profile_fails_closed(tmp_path) -> None:
    path = tmp_path / "library/projects/project-a/boundary/profile.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ProjectBoundaryProfileError, match="unreadable"):
        ProjectBoundaryProfileStore(tmp_path).get("project-a")


def test_profile_identity_drift_fails_closed(tmp_path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    store.update("project-a", mode="guarded", remote_default="review", expected_revision=0)
    path = tmp_path / "library/projects/project-a/boundary/profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["project_id"] = "project-b"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProjectBoundaryProfileError, match="identity drifted"):
        store.get("project-a")


def test_unknown_or_sensitive_fields_fail_closed(tmp_path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    store.update("project-a", mode="guarded", remote_default="review", expected_revision=0)
    path = tmp_path / "library/projects/project-a/boundary/profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["api_key"] = "must-not-be-accepted"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProjectBoundaryProfileError, match="fields are invalid"):
        store.get("project-a")


def test_invalid_mode_is_rejected_before_write(tmp_path) -> None:
    with pytest.raises(ValueError, match="mode is unsupported"):
        ProjectBoundaryProfileStore(tmp_path).update(
            "project-a",
            mode="unbounded",
            remote_default="allow",
            expected_revision=0,
        )
    assert not (tmp_path / "library/projects/project-a/boundary/profile.json").exists()


def test_get_reenters_locked_snapshot_without_releasing_profile_authority(tmp_path) -> None:
    """The Secret broker reads the revision while the dispatch fence is held."""
    store = ProjectBoundaryProfileStore(tmp_path)
    store.update("project-a", mode="guarded", remote_default="review", expected_revision=0)
    context = get_context("spawn")
    ready = context.Queue()
    completed = context.Queue()
    child = context.Process(
        target=_reentrant_profile_read_worker,
        args=(str(tmp_path), ready, completed),
    )
    child.start()
    try:
        assert ready.get(timeout=5) == "locked"
        assert completed.get(timeout=5) == 1
        child.join(timeout=5)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)


def test_other_thread_update_waits_for_locked_snapshot_to_exit(tmp_path) -> None:
    store = ProjectBoundaryProfileStore(tmp_path)
    store.update("project-a", mode="guarded", remote_default="review", expected_revision=0)
    entered = Event()
    release = Event()
    update_finished = Event()
    failure: list[BaseException] = []

    def hold_snapshot() -> None:
        with store.locked_snapshot("project-a"):
            entered.set()
            assert release.wait(timeout=5)

    def update_profile() -> None:
        try:
            store.update("project-a", mode="open", remote_default="allow", expected_revision=1)
        except BaseException as error:
            failure.append(error)
        finally:
            update_finished.set()

    holder = Thread(target=hold_snapshot)
    holder.start()
    assert entered.wait(timeout=5)
    updater = Thread(target=update_profile)
    updater.start()
    try:
        assert update_finished.wait(timeout=0.2) is False
    finally:
        release.set()
        holder.join(timeout=5)
        updater.join(timeout=5)
    assert not holder.is_alive()
    assert not updater.is_alive()
    assert failure == []
    assert store.get("project-a").profile.revision == 2
