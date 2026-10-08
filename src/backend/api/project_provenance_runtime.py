"""System-owned project provenance projected from the World event stream."""

from __future__ import annotations

from collections.abc import Mapping

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from core.long_horizon_runtime import (
    ProjectProvenanceTrace,
    ProvenanceContractError,
    TraceLink,
    TraceSubject,
    ValidationFact,
    VersionBinding,
    project_provenance_events,
    provenance_event_identity,
)
from core.personal_world_model import WorldEventDraft, WorldEventKind


class ProjectProvenanceError(ValueError):
    """Raised when a World-backed provenance trace is incomplete or drifts."""


class ProjectProvenanceRuntime:
    """Write opaque provenance facts through the existing Personal World Model."""

    def __init__(self, *, world: PersonalWorldModelRuntime) -> None:
        self._world = world

    def record_subject(
        self, *, subject: TraceSubject, version: VersionBinding, recorded_at: str,
    ):
        project = subject.project_id
        if version.revision is None:
            raise ProjectProvenanceError("provenance subject revision is required")
        existing = self.project(project)
        prior = _find_subject(existing, subject, version.revision)
        if prior is not None and prior.version != version:
            raise ProjectProvenanceError("provenance subject version drifted")
        payload = {"subject": subject.to_payload(), "version": version.to_payload()}
        return self._world.append_event(_draft(
            kind=WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
            project_id=project, payload=payload, recorded_at=recorded_at,
        ))

    def record_link(self, *, link: TraceLink, recorded_at: str):
        projection = self.project(link.project_id)
        _require_subject(projection, link.source, link.source_version)
        _require_subject(projection, link.target, link.target_version)
        return self._world.append_event(_draft(
            kind=WorldEventKind.PROVENANCE_LINK_RECORDED,
            project_id=link.project_id, payload=link.to_payload(), recorded_at=recorded_at,
        ))

    def record_validation(self, *, validation: ValidationFact, recorded_at: str):
        projection = self.project(validation.project_id)
        _require_subject(projection, validation.subject, validation.subject_version)
        previous = next(
            (item for item in projection.validations if item.validation_id == validation.validation_id),
            None,
        )
        if previous is not None and previous != validation:
            raise ProjectProvenanceError("provenance validation id is already bound")
        return self._world.append_event(_draft(
            kind=WorldEventKind.PROVENANCE_VALIDATION_RECORDED,
            project_id=validation.project_id, payload=validation.to_payload(), recorded_at=recorded_at,
        ))

    def project(self, project_id: str) -> ProjectProvenanceTrace:
        try:
            return project_provenance_events(
                self._world.events(project_id),
                project_id=project_id,
            )
        except ProvenanceContractError as error:
            raise ProjectProvenanceError("project provenance stream is invalid") from error


def _draft(*, kind: WorldEventKind, project_id: str, payload: Mapping[str, object], recorded_at: str) -> WorldEventDraft:
    event_id, source_ref, source_revision = provenance_event_identity(
        kind.value, project_id, payload,
    )
    return WorldEventDraft(
        event_id=event_id, project_id=project_id, kind=kind, actor="system",
        source_ref=source_ref,
        source_revision=source_revision, occurred_at=recorded_at,
        recorded_at=recorded_at, payload=dict(payload),
    )


def _find_subject(
    projection: ProjectProvenanceTrace,
    subject: TraceSubject,
    revision: str | None,
):
    return next(
        (
            item
            for item in projection.subjects
            if item.subject == subject and item.version.revision == revision
        ),
        None,
    )


def _require_subject(
    projection: ProjectProvenanceTrace,
    subject: TraceSubject,
    version: VersionBinding,
) -> None:
    found = _find_subject(projection, subject, version.revision)
    if found is None or found.version != version:
        raise ProjectProvenanceError(
            "provenance endpoint version was not declared earlier"
        )
