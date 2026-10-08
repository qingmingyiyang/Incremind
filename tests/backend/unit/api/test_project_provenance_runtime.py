from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import (
    ProjectProvenanceError,
    ProjectProvenanceRuntime,
)
from core.long_horizon_runtime import (
    TraceLink,
    TraceSubject,
    ValidationFact,
    VersionBinding,
    provenance_event_identity,
)
from core.storage_provider import SQLiteStructuredRecordStore
from core.personal_world_model import (
    PersonalWorldModelError,
    SQLiteWorldEventRepository,
    WorldEventDraft,
    WorldEventKind,
)


PROJECT = "project-alpha"
NOW = "2026-09-03T08:00:00Z"


def _world(tmp_path: Path) -> PersonalWorldModelRuntime:
    return PersonalWorldModelRuntime(
        repository=SQLiteWorldEventRepository(
            SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "world.sqlite3")
        ),
        now=lambda: datetime(2026, 9, 3, tzinfo=timezone.utc),
    )


def _runtime(tmp_path: Path) -> ProjectProvenanceRuntime:
    return ProjectProvenanceRuntime(world=_world(tmp_path))


def _subject(kind: str, subject_id: str, revision: str) -> tuple[TraceSubject, VersionBinding]:
    subject = TraceSubject(PROJECT, kind, subject_id)
    return subject, VersionBinding(
        f"crp://provenance/{PROJECT}/{kind}/{subject_id}", revision, f"fingerprint-{revision}",
    )


def test_records_multiple_subject_revisions_and_replays_after_restart(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    subject, r1 = _subject("artifact", "artifact-alpha", "r1")
    _, r2 = _subject("artifact", "artifact-alpha", "r2")
    first = runtime.record_subject(subject=subject, version=r1, recorded_at=NOW)
    replay = runtime.record_subject(subject=subject, version=r1, recorded_at=NOW)
    runtime.record_subject(subject=subject, version=r2, recorded_at="2026-09-03T08:00:01Z")

    assert first.replayed is False and replay.replayed is True
    projection = _runtime(tmp_path).project(PROJECT)
    assert [item.version.revision for item in projection.subjects] == ["r1", "r2"]
    assert projection.current_subjects[0].version.revision == "r2"
    assert projection.summary_payload() == {
        "kind": "project_provenance_summary.v1",
        "counts": {"subjects": 2, "links": 0, "validations": 0},
        "subject_kinds": [{"kind": "artifact", "count": 1}],
        "relations": [], "validation_statuses": [],
    }


def test_links_and_validations_require_earlier_exact_project_scoped_versions(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source, source_v = _subject("task", "task-alpha", "r1")
    target, target_v = _subject("artifact", "artifact-alpha", "r1")
    link = TraceLink(PROJECT, source, source_v, target, target_v, "produced_by")
    validation = ValidationFact(PROJECT, "validation-alpha", target, target_v, "verified", "receipt", "v1", ("crp://receipts/project-alpha/a",))

    with pytest.raises(ProjectProvenanceError, match="declared earlier"):
        runtime.record_link(link=link, recorded_at=NOW)
    runtime.record_subject(subject=source, version=source_v, recorded_at=NOW)
    runtime.record_subject(subject=target, version=target_v, recorded_at="2026-09-03T08:00:01Z")
    assert runtime.record_link(link=link, recorded_at="2026-09-03T08:00:02Z").replayed is False
    assert runtime.record_validation(validation=validation, recorded_at="2026-09-03T08:00:03Z").replayed is False
    assert runtime.record_link(link=link, recorded_at="2026-09-03T08:00:02Z").replayed is True
    assert runtime.record_validation(validation=validation, recorded_at="2026-09-03T08:00:03Z").replayed is True

    changed = ValidationFact(PROJECT, "validation-alpha", target, target_v, "rejected", "receipt", "v1", ("crp://receipts/project-alpha/a",))
    with pytest.raises(ProjectProvenanceError, match="already bound"):
        runtime.record_validation(validation=changed, recorded_at="2026-09-03T08:00:04Z")


def test_rejects_cross_project_and_drifted_or_future_events(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    subject, version = _subject("data", "data-alpha", "r1")
    runtime.record_subject(subject=subject, version=version, recorded_at=NOW)
    other = TraceSubject("project-beta", "artifact", "artifact-beta")
    other_v = VersionBinding("crp://provenance/project-beta/artifact/artifact-beta", "r1", None)
    with pytest.raises(ValueError, match="cross-project"):
        TraceLink(PROJECT, subject, version, other, other_v, "uses")
    different = VersionBinding(version.authority_ref, "r1", "fingerprint-drift")
    with pytest.raises(ProjectProvenanceError, match="version drifted"):
        runtime.record_subject(subject=subject, version=different, recorded_at="2026-09-03T08:00:01Z")


def test_world_append_rejects_missing_provenance_causality_and_non_system_actor(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    source, source_v = _subject("task", "task-alpha", "r1")
    target, target_v = _subject("artifact", "artifact-alpha", "r1")
    link = TraceLink(PROJECT, source, source_v, target, target_v, "produced_by")
    payload = link.to_payload()
    event_id, source_ref, source_revision = provenance_event_identity(
        WorldEventKind.PROVENANCE_LINK_RECORDED.value,
        PROJECT,
        payload,
    )
    missing_endpoints = WorldEventDraft(
        event_id=event_id,
        project_id=PROJECT,
        kind=WorldEventKind.PROVENANCE_LINK_RECORDED,
        actor="system",
        source_ref=source_ref,
        source_revision=source_revision,
        occurred_at=NOW,
        recorded_at=NOW,
        payload=payload,
    )
    with pytest.raises(PersonalWorldModelError, match="provenance stream"):
        world.append_event(missing_endpoints)

    subject_payload = {"subject": source.to_payload(), "version": source_v.to_payload()}
    subject_id, subject_ref, subject_revision = provenance_event_identity(
        WorldEventKind.PROVENANCE_SUBJECT_RECORDED.value,
        PROJECT,
        subject_payload,
    )
    wrong_actor = WorldEventDraft(
        event_id=subject_id,
        project_id=PROJECT,
        kind=WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
        actor="external",
        source_ref=subject_ref,
        source_revision=subject_revision,
        occurred_at=NOW,
        recorded_at=NOW,
        payload=subject_payload,
    )
    with pytest.raises(PersonalWorldModelError, match="provenance stream"):
        world.append_event(wrong_actor)


def test_world_append_rejects_noncanonical_provenance_identity(tmp_path: Path) -> None:
    world = _world(tmp_path)
    subject, version = _subject("data", "data-alpha", "r1")
    payload = {"subject": subject.to_payload(), "version": version.to_payload()}
    drifted = WorldEventDraft(
        event_id="provenance-subject-drifted",
        project_id=PROJECT,
        kind=WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
        actor="system",
        source_ref="crp://world-provenance/project-alpha/provenance-subject-drifted",
        source_revision="provenance-v1",
        occurred_at=NOW,
        recorded_at=NOW,
        payload=payload,
    )

    with pytest.raises(PersonalWorldModelError, match="provenance stream"):
        world.append_event(drifted)
