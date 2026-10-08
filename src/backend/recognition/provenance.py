"""Stable provenance payloads for retained recognition experiences.

These references explain where a record came from.  They are descriptive
metadata only: they do not establish business confirmation, grant authority,
or turn an asserted outcome into a verified fact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime


_KINDS = {
    "legacy_unspecified": "unknown",
    "user_statement": "user_asserted",
    "model_generated_artifact": "unverified",
    "workspace_confirmed_document": "unverified",
}
_SOURCE_TYPES = {"task", "document", "context_packet", "turn", "experience", "recognition"}
_MAX_ACTOR_LENGTH = 128
_MAX_SOURCE_ID_LENGTH = 256


class ExperienceProvenanceError(ValueError):
    """An experience provenance payload does not meet the storage contract."""


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A descriptive pointer to a stored task, document, or recognition."""

    type: str
    id: str
    revision: int | None = None

    def __post_init__(self) -> None:
        if self.type not in _SOURCE_TYPES:
            raise ExperienceProvenanceError("source reference type is invalid")
        _bounded_text("source reference id", self.id, _MAX_SOURCE_ID_LENGTH)
        if self.revision is not None and (
            not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1
        ):
            raise ExperienceProvenanceError("source reference revision is invalid")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"type": self.type, "id": self.id}
        if self.revision is not None:
            payload["revision"] = self.revision
        return payload

    @classmethod
    def from_payload(cls, value: object) -> SourceRef:
        if not isinstance(value, Mapping) or set(value).difference({"type", "id", "revision"}):
            raise ExperienceProvenanceError("source reference is invalid")
        return cls(value.get("type"), value.get("id"), value.get("revision"))


@dataclass(frozen=True, slots=True)
class ExperienceProvenance:
    """A constrained, non-authoritative account of an experience's origin."""

    kind: str
    epistemic_status: str
    actor: str
    source_refs: tuple[SourceRef, ...]
    recorded_at: str
    occurred_at: str | None = None
    artifact_status: str | None = None
    outcome_status: str | None = None

    def __post_init__(self) -> None:
        expected_status = _KINDS.get(self.kind)
        if expected_status is None:
            raise ExperienceProvenanceError("experience provenance kind is invalid")
        if self.epistemic_status != expected_status:
            raise ExperienceProvenanceError("experience provenance epistemic status is invalid")
        _bounded_text("actor", self.actor, _MAX_ACTOR_LENGTH)
        _iso_time("recorded_at", self.recorded_at)
        if self.occurred_at is not None:
            _iso_time("occurred_at", self.occurred_at)
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ExperienceProvenanceError("source references contain duplicates")
        if self.kind == "model_generated_artifact":
            if self.artifact_status != "committed" or self.outcome_status != "unknown":
                raise ExperienceProvenanceError("model artifact provenance status is invalid")
        elif self.artifact_status is not None or self.outcome_status is not None:
            raise ExperienceProvenanceError("artifact and outcome status only apply to model artifacts")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "epistemic_status": self.epistemic_status,
            "actor": self.actor,
            "source_refs": [source.to_payload() for source in self.source_refs],
            "recorded_at": self.recorded_at,
        }
        if self.occurred_at is not None:
            payload["occurred_at"] = self.occurred_at
        if self.artifact_status is not None:
            payload["artifact_status"] = self.artifact_status
        if self.outcome_status is not None:
            payload["outcome_status"] = self.outcome_status
        return payload

    @classmethod
    def legacy(cls, *, recorded_at: str) -> ExperienceProvenance:
        return cls("legacy_unspecified", "unknown", "unknown", (), recorded_at)

    @classmethod
    def from_payload(cls, value: Mapping[str, object], *, recorded_at: str | None = None) -> ExperienceProvenance:
        if not isinstance(value, Mapping):
            raise ExperienceProvenanceError("experience provenance is invalid")
        allowed = {
            "kind", "actor", "source_refs", "occurred_at", "recorded_at", "epistemic_status",
            "artifact_status", "outcome_status",
        }
        if set(value).difference(allowed):
            raise ExperienceProvenanceError("experience provenance contains unsupported fields")
        kind = value.get("kind")
        if kind not in _KINDS:
            raise ExperienceProvenanceError("experience provenance kind is invalid")
        raw_sources = value.get("source_refs", ())
        if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes)):
            raise ExperienceProvenanceError("source references are invalid")
        actual_recorded_at = recorded_at if recorded_at is not None else value.get("recorded_at")
        if not isinstance(actual_recorded_at, str):
            raise ExperienceProvenanceError("recorded_at is required")
        default_actor = (
            "user" if kind == "user_statement"
            else "system" if kind == "model_generated_artifact"
            else "unknown"
        )
        actor = value.get("actor", default_actor)
        occurred_at = value.get("occurred_at")
        if occurred_at is not None and not isinstance(occurred_at, str):
            raise ExperienceProvenanceError("occurred_at is invalid")
        return cls(
            kind,
            _KINDS[kind],
            actor,
            tuple(SourceRef.from_payload(item) for item in raw_sources),
            actual_recorded_at,
            occurred_at,
            "committed" if kind == "model_generated_artifact" else None,
            "unknown" if kind == "model_generated_artifact" else None,
        )


def _bounded_text(label: str, value: object, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ExperienceProvenanceError(f"{label} is invalid")


def _iso_time(label: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ExperienceProvenanceError(f"{label} is invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperienceProvenanceError(f"{label} is invalid") from exc
