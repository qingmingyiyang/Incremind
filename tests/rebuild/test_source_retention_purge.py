from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

import core.product_core.source_retention_purge as source_purge_module
from core.product_core import (
    BuildRetentionDryRun,
    ExecuteSourceRetentionPurge,
    RetentionBackupEvidence,
    RetentionCandidate,
    RetentionRecord,
    RetentionReference,
    SourceRetentionPurgeError,
)
from core.storage_provider import JsonObjectStore
from core.effect_log import EffectReaper, EffectState, shared_effect_runner


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
FINGERPRINT = "f" * 64


def _runner(tmp_path):
    return shared_effect_runner(
        tmp_path / "source-purge-effects.sqlite3",
        owner_role="test-source-retention-purge",
        lease_seconds=300,
    )


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _fixture(tmp_path):
    store = _store(tmp_path)
    source_id = "source-retention-purge"
    store.write(
        "sources",
        source_id,
        {
            "id": source_id,
            "kind": "text",
            "title": "retention fixture",
            "library_lifecycle": {
                "status": "deleted",
                "deleted_at": "2026-07-01T00:00:00Z",
                "undo_expires_at": "2026-07-08T00:00:00Z",
            },
        },
        expected_revision=0,
    )
    store.write(
        "source_content_reads",
        "read-retention-purge",
        {
            "id": "read-retention-purge",
            "source_id": source_id,
            "status": "completed",
        },
        expected_revision=0,
    )
    candidate = RetentionCandidate(
        aggregate_type="source",
        object_id=source_id,
        authority="json_object_store",
        revision=1,
        lifecycle_status="deleted",
        lifecycle_at="2026-07-01T00:00:00Z",
        undo_expires_at="2026-07-08T00:00:00Z",
        observed_vault_fingerprint=FINGERPRINT,
        inventory_complete=True,
        owned_records=(
            RetentionRecord(
                "json_object_store",
                "sources",
                source_id,
                1,
            ),
            RetentionRecord(
                "json_object_store",
                "source_content_reads",
                "read-retention-purge",
                1,
            ),
        ),
        inbound_references=(),
    )
    backup = RetentionBackupEvidence(
        status="verified",
        snapshot_id="snapshot-retention-purge",
        snapshot_fingerprint=FINGERPRINT,
        active_fingerprint=FINGERPRINT,
        file_count=8,
    )
    report = BuildRetentionDryRun().execute(
        candidates=(candidate,),
        backup_evidence=backup,
        evaluated_at=NOW,
    )
    assert report.items[0].eligible
    return store, candidate, report


def test_source_purge_deletes_owned_records_source_last_and_is_idempotent(
    tmp_path,
) -> None:
    store, candidate, report = _fixture(tmp_path)
    deleted: list[tuple[str, int]] = []
    service = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: NOW,
        after_delete=lambda record, index: deleted.append((record.collection, index)),
    )

    first = service.execute(
        candidate=candidate,
        report=report,
        plan_id=report.plan_id,
        expected_source_revision=1,
        confirm=True,
    )
    replay = service.execute(
        candidate=candidate,
        report=report,
        plan_id=report.plan_id,
        expected_source_revision=1,
        confirm=True,
    )

    assert first.status == "completed"
    assert first.deleted_count == first.total_count == 2
    assert first.idempotent is False
    assert replay.idempotent is True
    assert deleted == [("source_content_reads", 0), ("sources", 1)]
    assert store.read_including_deleted("sources", candidate.object_id) is None
    assert store.read("source_content_reads", "read-retention-purge") is None
    receipt = store.read("source_retention_purge_receipts", first.operation_id)
    assert receipt is not None
    assert receipt["kind"] == "source_retention_purge_receipt"
    assert "title" not in receipt
    assert "content" not in receipt


def test_source_purge_resumes_after_crash_between_owned_records(tmp_path) -> None:
    store, candidate, report = _fixture(tmp_path)
    clock = [NOW]
    crashed = False

    def fail_once(_record, index):
        nonlocal crashed
        if index == 0 and not crashed:
            crashed = True
            raise RuntimeError("simulated process crash")

    service = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
        after_delete=fail_once,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        service.execute(
            candidate=candidate,
            report=report,
            plan_id=report.plan_id,
            expected_source_revision=1,
            confirm=True,
        )

    assert store.read("source_content_reads", "read-retention-purge") is None
    assert store.read_including_deleted("sources", candidate.object_id) is not None
    clock[0] = datetime.fromtimestamp(NOW.timestamp() + 301, tz=UTC)
    recovered = EffectReaper(service._runner.log).recover_expired(
        now=int(clock[0].timestamp()),
        probes={("source_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
        verifiers={("source_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
    )
    assert recovered[0].state is EffectState.PLANNED
    completed = service.execute(
        candidate=candidate,
        report=report,
        plan_id=report.plan_id,
        expected_source_revision=1,
        confirm=True,
    )

    assert completed.status == "completed"
    assert store.read_including_deleted("sources", candidate.object_id) is None


def test_source_purge_restarted_handler_resumes_from_immutable_intent(tmp_path) -> None:
    store, candidate, report = _fixture(tmp_path)
    clock = [NOW]
    crashed = False

    def fail_once(_record, index):
        nonlocal crashed
        if index == 0 and not crashed:
            crashed = True
            raise RuntimeError("simulated process crash")

    first = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
        after_delete=fail_once,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.execute(
            candidate=candidate, report=report, plan_id=report.plan_id,
            expected_source_revision=1, confirm=True,
        )
    operation_id = store.list("source_retention_purge_intents")[0]["id"]
    clock[0] = datetime.fromtimestamp(NOW.timestamp() + 301, tz=UTC)
    EffectReaper(first._runner.log).recover_expired(
        now=int(clock[0].timestamp()),
        probes={("source_retention_purge", "effect-v2"): lambda effect: first.verify_effect(effect.operation_id)},
        verifiers={("source_retention_purge", "effect-v2"): lambda effect: first.verify_effect(effect.operation_id)},
    )
    restarted_runner = shared_effect_runner(
        tmp_path / "source-purge-effects.sqlite3",
        owner_role="restarted-source-retention-purge",
        lease_seconds=300,
    )
    restarted = ExecuteSourceRetentionPurge(
        store,
        effect_runner=restarted_runner,
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
    )
    outcome = restarted_runner.execute_planned(
        operation_id, restarted.handle_effect, now=int(clock[0].timestamp()),
    )
    assert outcome.state is EffectState.SETTLED_OK
    assert store.read_including_deleted("sources", candidate.object_id) is None


def test_source_purge_receipt_before_settle_waits_for_core_reaper(
    tmp_path, monkeypatch,
) -> None:
    store, candidate, report = _fixture(tmp_path)
    clock = [NOW]
    service = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: clock[0],
    )

    def crash_before_settle(*_args, **_kwargs):
        raise BaseException("simulated process exit before Effect settle")

    original_settle = service._runner.settle_ok
    monkeypatch.setattr(service._runner, "settle_ok", crash_before_settle)
    with pytest.raises(BaseException, match="before Effect settle"):
        service.execute(
            candidate=candidate, report=report, plan_id=report.plan_id,
            expected_source_revision=1, confirm=True,
        )
    operation_id = store.list("source_retention_purge_receipts")[0]["id"]
    assert store.read("source_retention_purge_receipts", operation_id) is not None
    assert service._runner.log.get(operation_id).state is EffectState.INFLIGHT

    monkeypatch.setattr(service._runner, "settle_ok", original_settle)
    with pytest.raises(SourceRetentionPurgeError, match="not settled"):
        service.execute(
            candidate=candidate, report=report, plan_id=report.plan_id,
            expected_source_revision=1, confirm=True,
        )
    clock[0] = datetime.fromtimestamp(NOW.timestamp() + 301, tz=UTC)
    recovered = EffectReaper(service._runner.log).recover_expired(
        now=int(clock[0].timestamp()),
        probes={("source_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
        verifiers={("source_retention_purge", "effect-v2"): lambda effect: service.verify_effect(effect.operation_id)},
    )
    assert recovered[0].state is EffectState.SETTLED_OK
    replay = service.execute(
        candidate=candidate, report=report, plan_id=report.plan_id,
        expected_source_revision=1, confirm=True,
    )
    assert replay.idempotent is True


def test_source_purge_quarantines_owned_audio_before_deleting_authority(
    tmp_path,
) -> None:
    store, candidate, _report = _fixture(tmp_path)
    media_root = tmp_path / "generated-audio"
    audio_path = media_root / candidate.object_id / "track.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"RIFF-owned")
    store.write(
        "audio_asset_refs",
        "audio-retention-purge",
        {
            "id": "audio-retention-purge",
            "source_id": candidate.object_id,
            "path": str(audio_path),
            "path_scope": "local_generated_audio_track",
            "size_bytes": audio_path.stat().st_size,
        },
        expected_revision=0,
    )
    candidate = replace(
        candidate,
        owned_records=(
            *candidate.owned_records,
            RetentionRecord(
                "json_object_store",
                "audio_asset_refs",
                "audio-retention-purge",
                1,
            ),
        ),
    )
    report = BuildRetentionDryRun().execute(
        candidates=(candidate,),
        backup_evidence=RetentionBackupEvidence(
            "verified", "snapshot-media", FINGERPRINT, FINGERPRINT, 9
        ),
        evaluated_at=NOW,
    )

    result = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: FINGERPRINT,
        clock=lambda: NOW,
        owned_media_roots=(media_root,),
    ).execute(
        candidate=candidate,
        report=report,
        plan_id=report.plan_id,
        expected_source_revision=1,
        confirm=True,
    )

    assert result.status == "completed"
    assert not audio_path.exists()
    assert not list(audio_path.parent.glob(".crp-source-purge-*.quarantine"))
    assert store.read("audio_asset_refs", "audio-retention-purge") is None


def test_source_purge_rejects_audio_outside_configured_root(tmp_path) -> None:
    store, candidate, _report = _fixture(tmp_path)
    audio_path = tmp_path / "outside.wav"
    audio_path.write_bytes(b"outside")
    store.write(
        "audio_asset_refs",
        "audio-outside",
        {
            "id": "audio-outside",
            "source_id": candidate.object_id,
            "path": str(audio_path),
            "path_scope": "local_generated_audio_track",
            "size_bytes": audio_path.stat().st_size,
        },
        expected_revision=0,
    )
    candidate = replace(
        candidate,
        owned_records=(
            *candidate.owned_records,
            RetentionRecord(
                "json_object_store",
                "audio_asset_refs",
                "audio-outside",
                1,
            ),
        ),
    )
    service = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: FINGERPRINT,
        owned_media_roots=(tmp_path / "configured",),
    )

    with pytest.raises(SourceRetentionPurgeError, match="root is not configured"):
        service.preflight_owned_records(candidate)

    assert audio_path.read_bytes() == b"outside"
    assert store.read("audio_asset_refs", "audio-outside") is not None


def test_source_purge_rejects_audio_through_directory_reparse(
    tmp_path,
    monkeypatch,
) -> None:
    store, candidate, _report = _fixture(tmp_path)
    media_root = tmp_path / "generated-audio"
    linked_directory = media_root / "linked"
    linked_directory.mkdir(parents=True)
    audio_path = linked_directory / "track.wav"
    audio_path.write_bytes(b"linked-directory")
    original_is_reparse = source_purge_module._is_reparse
    monkeypatch.setattr(
        source_purge_module,
        "_is_reparse",
        lambda path: (
            path == linked_directory
            or original_is_reparse(path)
        ),
    )
    store.write(
        "audio_asset_refs",
        "audio-linked-directory",
        {
            "id": "audio-linked-directory",
            "source_id": candidate.object_id,
            "path": str(audio_path),
            "path_scope": "local_generated_audio_track",
            "size_bytes": len(b"linked-directory"),
        },
        expected_revision=0,
    )
    candidate = replace(
        candidate,
        owned_records=(
            *candidate.owned_records,
            RetentionRecord(
                "json_object_store",
                "audio_asset_refs",
                "audio-linked-directory",
                1,
            ),
        ),
    )
    service = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: FINGERPRINT,
        owned_media_roots=(media_root,),
    )

    with pytest.raises(SourceRetentionPurgeError, match="reparse directory"):
        service.preflight_owned_records(candidate)

    assert audio_path.read_bytes() == b"linked-directory"
    assert store.read("audio_asset_refs", "audio-linked-directory") is not None


def test_source_purge_rejects_reparse_directory_before_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    store, candidate, _report = _fixture(tmp_path)
    media_root = tmp_path / "generated-audio"
    reparse_directory = media_root / "reparse"
    reparse_directory.mkdir(parents=True)
    audio_path = reparse_directory / "track.wav"
    audio_path.write_bytes(b"reparse-directory")
    store.write(
        "audio_asset_refs",
        "audio-reparse-directory",
        {
            "id": "audio-reparse-directory",
            "source_id": candidate.object_id,
            "path": str(audio_path),
            "path_scope": "local_generated_audio_track",
            "size_bytes": audio_path.stat().st_size,
        },
        expected_revision=0,
    )
    candidate = replace(
        candidate,
        owned_records=(
            *candidate.owned_records,
            RetentionRecord(
                "json_object_store",
                "audio_asset_refs",
                "audio-reparse-directory",
                1,
            ),
        ),
    )
    monkeypatch.setattr(
        source_purge_module,
        "_is_reparse",
        lambda path: path == reparse_directory,
    )
    service = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: FINGERPRINT,
        owned_media_roots=(media_root,),
    )

    with pytest.raises(SourceRetentionPurgeError, match="reparse directory"):
        service.preflight_owned_records(candidate)

    assert audio_path.read_bytes() == b"reparse-directory"
    assert store.read("audio_asset_refs", "audio-reparse-directory") is not None


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("unconfirmed", "explicit confirmation"),
        ("wrong_plan", "plan identity mismatch"),
        ("stale_revision", "revision mismatch"),
        ("vault_drift", "active Vault drifted"),
        ("inbound_reference", "inbound references"),
        ("incomplete_inventory", "reference catalog is incomplete"),
        ("inventory_drift", "dry-run inventory drifted"),
    ],
)
def test_source_purge_fails_closed_before_any_delete(
    tmp_path,
    mutation: str,
    error: str,
) -> None:
    store, candidate, report = _fixture(tmp_path)
    confirm = mutation != "unconfirmed"
    plan_id = "wrong-plan" if mutation == "wrong_plan" else report.plan_id
    revision = 2 if mutation == "stale_revision" else 1
    fingerprint = "d" * 64 if mutation == "vault_drift" else FINGERPRINT
    if mutation == "inbound_reference":
        candidate = replace(
            candidate,
            inbound_references=(
                RetentionReference(
                    "json_object_store",
                    "memory_candidates",
                    "candidate-1",
                    "$.source_refs[0].source_id",
                ),
            ),
        )
    if mutation == "incomplete_inventory":
        candidate = replace(candidate, inventory_complete=False)
    if mutation == "inventory_drift":
        candidate = replace(
            candidate,
            owned_records=(
                *candidate.owned_records,
                RetentionRecord(
                    "json_object_store",
                    "jobs",
                    "unplanned-job",
                    1,
                ),
            ),
        )
    service = ExecuteSourceRetentionPurge(
        store,
        effect_runner=_runner(tmp_path),
        active_fingerprint=lambda: fingerprint,
        clock=lambda: NOW,
    )

    with pytest.raises(SourceRetentionPurgeError, match=error):
        service.execute(
            candidate=candidate,
            report=report,
            plan_id=plan_id,
            expected_source_revision=revision,
            confirm=confirm,
        )

    assert store.read_including_deleted("sources", "source-retention-purge") is not None
    assert store.read("source_content_reads", "read-retention-purge") is not None
    assert store.list("source_retention_purge_intents") == ()
    assert store.list("source_retention_purge_receipts") == ()


def test_source_purge_rejects_owned_record_revision_drift_before_receipt(
    tmp_path,
) -> None:
    store, candidate, report = _fixture(tmp_path)
    current = store.read("source_content_reads", "read-retention-purge")
    assert current is not None
    store.write(
        "source_content_reads",
        "read-retention-purge",
        current,
        expected_revision=1,
    )

    with pytest.raises(SourceRetentionPurgeError, match="owned record revision drifted"):
        ExecuteSourceRetentionPurge(
            store,
            effect_runner=_runner(tmp_path),
            active_fingerprint=lambda: FINGERPRINT,
            clock=lambda: NOW,
        ).execute(
            candidate=candidate,
            report=report,
            plan_id=report.plan_id,
            expected_source_revision=1,
            confirm=True,
        )

    assert store.read_including_deleted("sources", candidate.object_id) is not None
    assert store.list("source_retention_purge_intents") == ()
    assert store.list("source_retention_purge_receipts") == ()
