from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.effect_log import EFFECT_V2
from core.job_runner import SQLiteJobAdmissionCommand, SQLiteMediaAdmissionConflict
from core.media_hands.effect_contract import EFFECT_KIND
from core.media_hands.job_admission import MediaHandsAdmissionCommand
from core.media_hands.provisioner import (
    MediaHandsPolicy,
    MediaHandsV2Provisioner,
    MediaOperationProfile,
    MediaResourceBudget,
    SourcePermissionSnapshot,
)
from core.source_processing import SourceManifestCodec


ROOT = Path(__file__).resolve().parents[3]


class _Handler:
    def __init__(self) -> None:
        self.provider_id = "fixture-media-provider"
        self.provider_revision = "fixture-r1"

    @property
    def provider_identity(self) -> tuple[str, str]:
        return self.provider_id, self.provider_revision


def _manifest():
    fixture = ROOT / "core-contracts/rebuild/source-processing/fixtures/bilibili-video.json"
    return SourceManifestCodec.decode(json.loads(fixture.read_text(encoding="utf-8")))


def _policy(*, limit: int = 2, revision: str = "policy-v2-r1") -> MediaHandsPolicy:
    lanes = {lane: limit for lane in ("download", "asr", "vision", "model", "media_cpu")}
    budget = MediaResourceBudget(10_000_000, 60_000, 3_600_000, 0, 0, 0, 120_000)
    profile = MediaOperationProfile(("download", "asr", "media_cpu"), budget)
    return MediaHandsPolicy(
        revision,
        lanes,
        lanes,
        {operation: profile for operation in ("analyze_source", "extract_audio_track")},
    )


def _permission(*, manifest_revision: str = "manifest-r7") -> SourcePermissionSnapshot:
    return SourcePermissionSnapshot(
        project_id="project-media-test",
        manifest_ref="crp://jobs/source-manifests/bilibili-video-r7",
        manifest_revision=manifest_revision,
        grant_ref="crp://jobs/source-permissions/project-media-test/bili-1/r1",
        grant_revision="grant-r1",
        revocation_generation=0,
    )


def _command(
    *, request_id: str = "media-v2-request-1", selection_revision: str = "media-ingress-r3",
) -> MediaHandsAdmissionCommand:
    return MediaHandsAdmissionCommand(
        request_id=request_id,
        project_id="project-media-test",
        selection_ref="crp://media-ingress/selections/bilibili-rebuild/r3",
        selection_revision=selection_revision,
        selection_mode="hands",
    )


def _kwargs(
    *, operation: str = "analyze_source", key: str = "media-v2-idempotency-0001",
    manifest_revision: str = "manifest-r7",
) -> dict[str, object]:
    return {
        "manifest": _manifest(),
        "manifest_ref": "crp://jobs/source-manifests/bilibili-video-r7",
        "manifest_revision": manifest_revision,
        "operation": operation,
        "idempotency_key": key,
        "created_at": "2026-08-30T00:00:00Z",
        "permission_snapshot": _permission(manifest_revision=manifest_revision),
    }


def _provisioner(
    database: Path,
    *,
    policy: MediaHandsPolicy | None = None,
    handler: _Handler | None = None,
    fence=None,
) -> MediaHandsV2Provisioner:
    return MediaHandsV2Provisioner(
        SQLiteJobAdmissionCommand(database),
        policy or _policy(),
        handler or _Handler(),
        admitted_at=lambda: 100,
        policy_admission_fence=fence,
    )


def test_v2_provision_is_atomic_replayable_and_never_creates_legacy_job_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    fence_calls: list[tuple[bool, str]] = []

    def fence(connection, revision: str) -> None:
        fence_calls.append((connection.in_transaction, revision))

    provisioner = _provisioner(database, fence=fence)
    first = provisioner.provision(command=_command(), **_kwargs())
    replay = provisioner.provision(command=_command(), **_kwargs())

    assert first.replayed is False and replay.replayed is True
    assert replay.record == first.record
    assert first.record.payload["execution_version"] == EFFECT_V2
    assert first.record.payload["media_hands"]["selection"] == {
        "ref": "crp://media-ingress/selections/bilibili-rebuild/r3",
        "revision": "media-ingress-r3",
        "mode": "hands",
    }
    assert not {
        "admission_state", "consumed", "concurrency",
    }.intersection(first.record.payload["media_hands"])
    assert fence_calls == [(True, "policy-v2-r1"), (True, "policy-v2-r1")]

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM effect WHERE kind=? AND contract_version=?",
            (EFFECT_KIND, EFFECT_V2),
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='job_store'"
        ).fetchone()[0] == 0


def test_v2_policy_fence_failure_rolls_back_gate_effect_fact_and_projection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"

    def reject(_connection, _revision: str) -> None:
        raise RuntimeError("policy changed")

    with pytest.raises(RuntimeError, match="policy changed"):
        _provisioner(database, fence=reject).provision(
            command=_command(), **_kwargs(),
        )

    with sqlite3.connect(database) as connection:
        for table in (
            "effect", "effect_gate_fact", "effect_intent_fact",
            "job_effect_fact", "job_effect_node", "job_projection",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("drift", ("selection", "provider", "manifest", "metadata"))
def test_v2_replay_drift_fails_closed_without_a_second_effect(
    tmp_path: Path, drift: str,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    handler = _Handler()
    provisioner = _provisioner(database, handler=handler)
    provisioner.provision(command=_command(), **_kwargs())

    command = _command()
    values = _kwargs()
    if drift == "selection":
        command = _command(selection_revision="media-ingress-r4")
    elif drift == "provider":
        handler.provider_revision = "fixture-r2"
    elif drift == "manifest":
        values = _kwargs(manifest_revision="manifest-r8")
    else:
        values = _kwargs(key="media-v2-idempotency-0002")

    with pytest.raises((ValueError, RuntimeError), match="drifted"):
        provisioner.provision(command=command, **values)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM job_effect_fact").fetchone()[0] == 1


def test_v2_capacity_counts_active_effects_not_job_status_or_lease(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    provisioner = _provisioner(database, policy=_policy(limit=1))
    provisioner.provision(command=_command(), **_kwargs())

    with pytest.raises(SQLiteMediaAdmissionConflict, match="queue is full"):
        provisioner.provision(
            command=_command(request_id="media-v2-request-2"),
            **_kwargs(
                operation="extract_audio_track",
                key="media-v2-idempotency-0002",
            ),
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 1
