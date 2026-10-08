from __future__ import annotations

import pytest
from types import SimpleNamespace

from core.media_hands import (
    MediaHandsPolicy,
    MediaOperationProfile,
    MediaResourceBudget,
    SourcePermissionSnapshot,
    build_media_hands_job,
    default_personal_workbench_policy_snapshot,
)


def _policy() -> MediaHandsPolicy:
    lanes = {lane: 1 for lane in ("download", "asr", "vision", "model", "media_cpu")}
    return MediaHandsPolicy(
        "policy-r1",
        lanes,
        lanes,
        {
            "analyze_source": MediaOperationProfile(
                ("download", "asr", "media_cpu"),
                MediaResourceBudget(10_000_000, 60_000, 3_600_000, 0, 0, 0, 120_000),
            ),
        },
    )


def _kwargs() -> dict[str, object]:
    return {
        "manifest": SimpleNamespace(
            source_id="source-media-test",
            source_ref="crp://sources/source-media-test",
            credential_binding=None,
        ),
        "operation": "analyze_source",
        "idempotency_key": "media-admission-test-001",
        "manifest_ref": "crp://jobs/source-manifests/source-media-test",
        "manifest_revision": "manifest-r7",
        "permission_snapshot": SourcePermissionSnapshot(
            project_id="project-media-test",
            manifest_ref="crp://jobs/source-manifests/source-media-test",
            manifest_revision="manifest-r7",
            grant_ref="crp://jobs/source-permissions/project-media-test/bili-1/r1",
            grant_revision="r1",
            revocation_generation=0,
        ),
        "created_at": "2026-08-25T00:00:00Z",
    }


def test_media_hands_builder_matches_official_schema() -> None:
    job = build_media_hands_job(policy=_policy(), **_kwargs())
    assert job["job_type"] == "media_hands"
    assert job["media_hands"]["admission_state"] == "queued"


def test_media_policy_snapshot_cannot_be_mutated_after_validation() -> None:
    policy = _policy()
    with pytest.raises(TypeError):
        policy.lane_max_queue["download"] = 2  # type: ignore[index]


def test_manifest_artifact_ref_cannot_alias_source_ref() -> None:
    values = _kwargs()
    values["manifest_ref"] = "crp://sources/source-media-test"
    with pytest.raises(ValueError, match="identity"):
        build_media_hands_job(policy=_policy(), **values)


def test_permission_snapshot_must_bind_the_admitted_manifest_revision() -> None:
    values = _kwargs()
    values["permission_snapshot"] = SourcePermissionSnapshot(
        project_id="project-media-test",
        manifest_ref="crp://jobs/source-manifests/other",
        manifest_revision="manifest-r7",
        grant_ref="crp://jobs/source-permissions/project-media-test/bili-1/r1",
        grant_revision="r1",
        revocation_generation=0,
    )
    with pytest.raises(ValueError, match="bind"):
        build_media_hands_job(policy=_policy(), **values)


def test_permission_snapshot_is_required_for_media_admission() -> None:
    values = _kwargs()
    values.pop("permission_snapshot")
    with pytest.raises(TypeError, match="permission_snapshot"):
        build_media_hands_job(policy=_policy(), **values)
