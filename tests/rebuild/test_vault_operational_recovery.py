from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.storage_provider import (
    VaultBackupRestoreError,
    VaultOperationalRecoveryConflict,
    VaultOperationalRecoveryError,
    adopt_prepared_vault,
    create_vault_backup,
    load_vault_recovery_operation,
    prepare_vault_recovery,
    recover_vault_operation,
    rollback_adopted_vault,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _child_env() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(_REPO_ROOT / "src")
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source_root if not current else source_root + os.pathsep + current
    return environment


def _vault(root: Path, name: str, value: str) -> Path:
    vault = root / name
    (vault / "library").mkdir(parents=True)
    (vault / "library" / "note.md").write_text(value, encoding="utf-8")
    (vault / ".rebuild-data").mkdir()
    (vault / ".rebuild-data" / "state.json").write_text(
        json.dumps({"value": value}), encoding="utf-8"
    )
    return vault


def _prepared(tmp_path: Path, operation_id: str = "recovery-001"):
    desired = _vault(tmp_path, "desired", "verified backup")
    active = _vault(tmp_path, "active", "current authority")
    snapshot = create_vault_backup(
        source_root=desired,
        backups_root=tmp_path / "backups",
        snapshot_id=f"snapshot-{operation_id}",
    )
    operations = tmp_path / "operations"
    operation = prepare_vault_recovery(
        snapshot_root=snapshot.snapshot_root,
        active_root=active,
        operations_root=operations,
        operation_id=operation_id,
    )
    return operation, operations, active, snapshot.snapshot_root


def test_offline_adoption_and_rollback_preserve_both_authorities(tmp_path: Path) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path)
    assert operation.state == "prepared"
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"

    adopted = adopt_prepared_vault(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )

    assert adopted.state == "adopted"
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "verified backup"
    assert (Path(adopted.rollback_root) / "library" / "note.md").read_text(encoding="utf-8") == "current authority"

    rolled_back = rollback_adopted_vault(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )

    assert rolled_back.state == "rolled_back"
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"
    assert (Path(rolled_back.displaced_root) / "library" / "note.md").read_text(encoding="utf-8") == "verified backup"


def test_adoption_requires_explicit_offline_confirmation(tmp_path: Path) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path)

    with pytest.raises(VaultOperationalRecoveryConflict, match="offline"):
        adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=False,
        )

    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"
    assert Path(operation.staging_root).is_dir()
    assert not Path(operation.rollback_root).exists()


def test_disk_full_equivalent_marks_prepare_failed_without_touching_active(tmp_path: Path) -> None:
    desired = _vault(tmp_path, "desired", "verified backup")
    active = _vault(tmp_path, "active", "current authority")
    snapshot = create_vault_backup(
        source_root=desired, backups_root=tmp_path / "backups", snapshot_id="snapshot-disk-full"
    )
    operations = tmp_path / "operations"

    def disk_full(_source: Path, _destination: Path) -> None:
        raise OSError(28, "No space left on device")

    with pytest.raises(VaultOperationalRecoveryError, match="did not complete"):
        prepare_vault_recovery(
            snapshot_root=snapshot.snapshot_root,
            active_root=active,
            operations_root=operations,
            operation_id="recovery-disk-full",
            copy_file=disk_full,
        )

    failed = load_vault_recovery_operation(
        operations_root=operations, operation_id="recovery-disk-full"
    )
    assert failed.state == "failed"
    assert failed.last_error == "VaultBackupRestoreError"
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


def test_corrupt_snapshot_is_rejected_before_operation_or_staging(tmp_path: Path) -> None:
    desired = _vault(tmp_path, "desired", "verified backup")
    active = _vault(tmp_path, "active", "current authority")
    snapshot = create_vault_backup(
        source_root=desired, backups_root=tmp_path / "backups", snapshot_id="snapshot-corrupt"
    )
    (snapshot.snapshot_root / "payload" / "library" / "note.md").write_text(
        "tampered", encoding="utf-8"
    )

    with pytest.raises(VaultBackupRestoreError, match="hash"):
        prepare_vault_recovery(
            snapshot_root=snapshot.snapshot_root,
            active_root=active,
            operations_root=tmp_path / "operations",
            operation_id="recovery-corrupt",
        )

    assert not (tmp_path / "operations" / "recovery-corrupt.json").exists()
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


@pytest.mark.parametrize("kill_stage", ["old_detached", "new_adopted"])
def test_process_kill_during_adoption_recovers_deterministically(
    tmp_path: Path, kill_stage: str
) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path, f"recovery-kill-{kill_stage}")
    script = """
import os, sys
from pathlib import Path
from core.storage_provider import adopt_prepared_vault

stage_to_kill = sys.argv[3]
def kill(stage):
    if stage == stage_to_kill:
        os._exit(77)
adopt_prepared_vault(
    operations_root=Path(sys.argv[1]),
    operation_id=sys.argv[2],
    application_offline=True,
    fault_hook=kill,
)
"""
    interrupted = subprocess.run(
        [sys.executable, "-c", script, str(operations), operation.operation_id, kill_stage],
        check=False,
        capture_output=True,
        timeout=20,
        env=_child_env(),
    )
    assert interrupted.returncode == 77

    recovered = recover_vault_operation(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )

    assert recovered.state == "adopted"
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "verified backup"
    assert (Path(recovered.rollback_root) / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


@pytest.mark.parametrize("kill_stage", ["adopted_displaced", "old_restored"])
def test_process_kill_during_rollback_recovers_deterministically(
    tmp_path: Path, kill_stage: str
) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path, f"rollback-kill-{kill_stage}")
    adopt_prepared_vault(
        operations_root=operations, operation_id=operation.operation_id, application_offline=True
    )
    script = """
import os, sys
from pathlib import Path
from core.storage_provider import rollback_adopted_vault

stage_to_kill = sys.argv[3]
def kill(stage):
    if stage == stage_to_kill:
        os._exit(78)
rollback_adopted_vault(
    operations_root=Path(sys.argv[1]),
    operation_id=sys.argv[2],
    application_offline=True,
    fault_hook=kill,
)
"""
    interrupted = subprocess.run(
        [sys.executable, "-c", script, str(operations), operation.operation_id, kill_stage],
        check=False,
        capture_output=True,
        timeout=20,
        env=_child_env(),
    )
    assert interrupted.returncode == 78

    recovered = recover_vault_operation(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )

    assert recovered.state == "rolled_back"
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"
    assert (Path(recovered.displaced_root) / "library" / "note.md").read_text(encoding="utf-8") == "verified backup"


def test_live_lease_blocks_concurrent_adoption(tmp_path: Path) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path)
    lock = active.parent / ".chriptmas-vault-recovery.lock"
    lock.write_text(
        json.dumps({"operation_id": operation.operation_id, "pid": os.getpid()}), encoding="utf-8"
    )

    with pytest.raises(VaultOperationalRecoveryConflict, match="holds the lease"):
        adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )

    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


def test_prepare_replay_is_bound_to_same_snapshot_and_active_root(tmp_path: Path) -> None:
    operation, operations, active, snapshot = _prepared(tmp_path)

    replayed = prepare_vault_recovery(
        snapshot_root=snapshot,
        active_root=active,
        operations_root=operations,
        operation_id=operation.operation_id,
    )

    assert replayed == operation
    other_active = _vault(tmp_path, "other-active", "other authority")
    with pytest.raises(VaultOperationalRecoveryConflict, match="replay drifted"):
        prepare_vault_recovery(
            snapshot_root=snapshot,
            active_root=other_active,
            operations_root=operations,
            operation_id=operation.operation_id,
        )


def test_active_authority_drift_fails_before_adoption(tmp_path: Path) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path)
    (active / "library" / "note.md").write_text("drifted authority", encoding="utf-8")

    with pytest.raises(VaultOperationalRecoveryConflict, match="active Vault fingerprint drifted"):
        adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )

    assert Path(operation.staging_root).is_dir()
    assert not Path(operation.rollback_root).exists()
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "drifted authority"


def test_snapshot_drift_after_prepare_fails_before_adoption(tmp_path: Path) -> None:
    operation, operations, active, snapshot = _prepared(tmp_path)
    (snapshot / "payload" / "library" / "note.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(VaultBackupRestoreError, match="hash"):
        adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )

    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"
    assert not Path(operation.rollback_root).exists()


def test_staging_drift_after_prepare_fails_before_adoption(tmp_path: Path) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path)
    (Path(operation.staging_root) / "library" / "note.md").write_text(
        "tampered staging", encoding="utf-8"
    )

    with pytest.raises(VaultBackupRestoreError, match="fingerprint"):
        adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )

    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"
    assert not Path(operation.rollback_root).exists()


def test_prepare_rejects_preexisting_recovery_sibling(tmp_path: Path) -> None:
    desired = _vault(tmp_path, "desired", "verified backup")
    active = _vault(tmp_path, "active", "current authority")
    snapshot = create_vault_backup(
        source_root=desired,
        backups_root=tmp_path / "backups",
        snapshot_id="snapshot-sibling",
    )
    sibling = active.parent / ".active.restore-recovery-sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(VaultOperationalRecoveryConflict, match="sibling"):
        prepare_vault_recovery(
            snapshot_root=snapshot.snapshot_root,
            active_root=active,
            operations_root=tmp_path / "operations",
            operation_id="recovery-sibling",
        )

    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


def test_preparing_operation_recovers_valid_staging_without_copy_replay(tmp_path: Path) -> None:
    operation, operations, _active, _snapshot = _prepared(tmp_path)
    operation_path = operations / f"{operation.operation_id}.json"
    payload = json.loads(operation_path.read_text(encoding="utf-8"))
    payload.update({"state": "preparing", "intent": "restore_staging"})
    operation_path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = recover_vault_operation(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )

    assert recovered.state == "prepared"
    assert Path(recovered.staging_root).is_dir()


def test_preparing_operation_with_partial_staging_becomes_diagnostic_failure(
    tmp_path: Path,
) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path)
    (Path(operation.staging_root) / "library" / "note.md").write_text(
        "partial", encoding="utf-8"
    )
    operation_path = operations / f"{operation.operation_id}.json"
    payload = json.loads(operation_path.read_text(encoding="utf-8"))
    payload.update({"state": "preparing", "intent": "restore_staging"})
    operation_path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = recover_vault_operation(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )

    assert recovered.state == "failed"
    assert recovered.last_error == "VaultBackupRestoreError"
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


def test_operation_path_tampering_is_rejected_without_filesystem_mutation(tmp_path: Path) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path)
    operation_path = operations / f"{operation.operation_id}.json"
    payload = json.loads(operation_path.read_text(encoding="utf-8"))
    payload["rollback_root"] = str(tmp_path / "unrelated")
    operation_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VaultOperationalRecoveryError, match="paths are invalid"):
        adopt_prepared_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )

    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


def test_repeated_adoption_and_rollback_are_idempotent(tmp_path: Path) -> None:
    operation, operations, active, snapshot = _prepared(tmp_path)
    first_adoption = adopt_prepared_vault(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )
    second_adoption = adopt_prepared_vault(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )
    assert second_adoption == first_adoption
    assert prepare_vault_recovery(
        snapshot_root=snapshot,
        active_root=active,
        operations_root=operations,
        operation_id=operation.operation_id,
    ) == first_adoption

    first_rollback = rollback_adopted_vault(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )
    second_rollback = rollback_adopted_vault(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )

    assert second_rollback == first_rollback
    assert prepare_vault_recovery(
        snapshot_root=snapshot,
        active_root=active,
        operations_root=operations,
        operation_id=operation.operation_id,
    ) == first_rollback
    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


def test_failed_prepare_cannot_be_adopted(tmp_path: Path) -> None:
    desired = _vault(tmp_path, "desired", "verified backup")
    active = _vault(tmp_path, "active", "current authority")
    snapshot = create_vault_backup(
        source_root=desired,
        backups_root=tmp_path / "backups",
        snapshot_id="snapshot-failed-adopt",
    )
    operations = tmp_path / "operations"

    def fail_copy(_source: Path, _destination: Path) -> None:
        raise OSError("injected failure")

    with pytest.raises(VaultOperationalRecoveryError):
        prepare_vault_recovery(
            snapshot_root=snapshot.snapshot_root,
            active_root=active,
            operations_root=operations,
            operation_id="recovery-failed-adopt",
            copy_file=fail_copy,
        )

    with pytest.raises(VaultOperationalRecoveryConflict, match="cannot adopt from failed"):
        adopt_prepared_vault(
            operations_root=operations,
            operation_id="recovery-failed-adopt",
            application_offline=True,
        )

    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "current authority"


def test_rollback_authority_drift_fails_before_displacing_active(tmp_path: Path) -> None:
    operation, operations, active, _snapshot = _prepared(tmp_path)
    adopted = adopt_prepared_vault(
        operations_root=operations,
        operation_id=operation.operation_id,
        application_offline=True,
    )
    (Path(adopted.rollback_root) / "library" / "note.md").write_text(
        "drifted rollback", encoding="utf-8"
    )

    with pytest.raises(VaultOperationalRecoveryConflict, match="rollback Vault fingerprint drifted"):
        rollback_adopted_vault(
            operations_root=operations,
            operation_id=operation.operation_id,
            application_offline=True,
        )

    assert (active / "library" / "note.md").read_text(encoding="utf-8") == "verified backup"
    assert not Path(adopted.displaced_root).exists()

@pytest.mark.parametrize('legacy', [False, True])
def test_restore_operation_keeps_explicit_fingerprint_algorithm(tmp_path, legacy):
    import sqlite3
    from contextlib import closing
    from core.storage_provider.vault_backup_restore import fingerprint_vault_restore_source
    desired = _vault(tmp_path, 'desired-logical', 'backup')
    active = _vault(tmp_path, 'active-logical', 'current')
    database = active / 'records.sqlite3'
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript('CREATE TABLE notes(body); INSERT INTO notes VALUES ("same"); CREATE INDEX note_body ON notes(body);')
    snapshot = create_vault_backup(source_root=desired, backups_root=tmp_path/'snapshots', snapshot_id='logical')
    operations = tmp_path/'operations'
    extra = {} if legacy else {'expected_source_fingerprint': fingerprint_vault_restore_source(active)}
    operation = prepare_vault_recovery(snapshot_root=snapshot.snapshot_root, active_root=active, operations_root=operations, operation_id='logical-restore', **extra)
    if legacy:
        path = operations / 'logical-restore.json'
        payload = json.loads(path.read_text())
        payload.pop('original_fingerprint_kind')
        path.write_text(json.dumps(payload))
        assert load_vault_recovery_operation(operations_root=operations, operation_id='logical-restore').original_fingerprint_kind == 'physical-v1'
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript('DROP INDEX note_body; CREATE INDEX note_body ON notes(body);')
    if legacy:
        with pytest.raises(VaultOperationalRecoveryConflict, match='fingerprint drifted'):
            adopt_prepared_vault(operations_root=operations, operation_id=operation.operation_id, application_offline=True)
    else:
        adopted = adopt_prepared_vault(operations_root=operations, operation_id=operation.operation_id, application_offline=True)
        assert adopted.state == 'adopted'
        assert recover_vault_operation(operations_root=operations, operation_id=operation.operation_id, application_offline=True).state == 'adopted'
        assert rollback_adopted_vault(operations_root=operations, operation_id=operation.operation_id, application_offline=True).state == 'rolled_back'


@pytest.mark.parametrize('when', ['before_prepare', 'after_prepare'])
def test_logical_restore_rejects_real_change_after_confirmation(tmp_path, when):
    import sqlite3
    from contextlib import closing
    from core.storage_provider.vault_backup_restore import fingerprint_vault_restore_source
    active = _vault(tmp_path, 'active-record', 'current')
    database = active / 'records.sqlite3'
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript('CREATE TABLE notes(body); INSERT INTO notes VALUES ("before");')
    snapshot = create_vault_backup(source_root=active, backups_root=tmp_path/'snapshots', snapshot_id='records')
    expected = fingerprint_vault_restore_source(active)
    def change():
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('UPDATE notes SET body="after"')
            connection.commit()
    def prepare():
        return prepare_vault_recovery(snapshot_root=snapshot.snapshot_root, active_root=active, operations_root=tmp_path/'operations', operation_id='record-restore', expected_source_fingerprint=expected)
    if when == 'before_prepare':
        change()
        with pytest.raises(VaultOperationalRecoveryConflict, match='after restore confirmation'):
            prepare()
        assert not (tmp_path/'operations').exists()
    else:
        operation = prepare()
        change()
        with pytest.raises(VaultOperationalRecoveryConflict, match='fingerprint drifted'):
            adopt_prepared_vault(operations_root=tmp_path/'operations', operation_id=operation.operation_id, application_offline=True)


def test_recovery_operation_rejects_unknown_fingerprint_algorithm(tmp_path):
    operation, operations, _active, _snapshot = _prepared(tmp_path)
    path = operations / (operation.operation_id + '.json')
    payload = json.loads(path.read_text())
    payload['original_fingerprint_kind'] = 'unknown'
    path.write_text(json.dumps(payload))
    with pytest.raises(VaultOperationalRecoveryError, match='fingerprint kind'):
        load_vault_recovery_operation(operations_root=operations, operation_id=operation.operation_id)

@pytest.mark.parametrize('stage', ['old_detached', 'new_adopted', 'adopted_displaced', 'old_restored'])
def test_logical_restore_recovers_interrupted_transitions(tmp_path, stage):
    from core.storage_provider.vault_backup_restore import fingerprint_vault_restore_source
    desired = _vault(tmp_path, 'desired-interrupt', 'backup')
    active = _vault(tmp_path, 'active-interrupt', 'current')
    snapshot = create_vault_backup(source_root=desired, backups_root=tmp_path/'snapshots', snapshot_id='interrupt')
    operations = tmp_path/'operations'
    operation = prepare_vault_recovery(snapshot_root=snapshot.snapshot_root, active_root=active, operations_root=operations, operation_id='interrupt', expected_source_fingerprint=fingerprint_vault_restore_source(active))
    def interrupt(actual):
        if actual == stage:
            raise RuntimeError('simulated interruption')
    rollback = stage in {'adopted_displaced', 'old_restored'}
    if rollback:
        adopt_prepared_vault(operations_root=operations, operation_id=operation.operation_id, application_offline=True)
    action = rollback_adopted_vault if rollback else adopt_prepared_vault
    with pytest.raises(RuntimeError, match='simulated interruption'):
        action(operations_root=operations, operation_id=operation.operation_id, application_offline=True, fault_hook=interrupt)
    expected_state = 'rolled_back' if rollback else 'adopted'
    for _ in range(2):
        assert recover_vault_operation(operations_root=operations, operation_id=operation.operation_id, application_offline=True).state == expected_state
    assert (active/'library'/'note.md').read_text() == ('current' if rollback else 'backup')

def test_logical_prepare_replay_follows_original_source_across_states(tmp_path):
    from core.storage_provider.vault_backup_restore import fingerprint_vault_restore_source
    desired = _vault(tmp_path, 'desired-replay', 'backup')
    active = _vault(tmp_path, 'active-replay', 'current')
    snapshot = create_vault_backup(source_root=desired, backups_root=tmp_path/'snapshots', snapshot_id='replay')
    operations = tmp_path/'operations'
    expected = fingerprint_vault_restore_source(active)
    def prepare(fingerprint=expected):
        return prepare_vault_recovery(snapshot_root=snapshot.snapshot_root, active_root=active, operations_root=operations, operation_id='replay', expected_source_fingerprint=fingerprint)
    assert prepare().state == 'prepared'
    assert prepare().state == 'prepared'
    adopt_prepared_vault(operations_root=operations, operation_id='replay', application_offline=True)
    assert prepare().state == 'adopted'
    with pytest.raises(VaultOperationalRecoveryConflict):
        prepare('0'*64)
    rollback_adopted_vault(operations_root=operations, operation_id='replay', application_offline=True)
    assert prepare().state == 'rolled_back'
