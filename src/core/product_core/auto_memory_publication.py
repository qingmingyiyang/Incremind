from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.memory_core import ObjectStoreMemoryCandidateRepository
from .ports import ObjectStorePort


_QUARANTINE_REASON = "automatic memory publication is quarantined pending a low-risk Atom policy"


class AutoMemoryPublicationError(ValueError):
    """Raised when local auto-publication policy is invalid."""


@dataclass(frozen=True, slots=True)
class AutoMemoryPublicationSettings:
    status: str
    enabled: bool
    allowed_layers: tuple[str, ...]
    reviewer: str
    publisher: str
    explicit_enable_required: bool
    rollback_required: bool
    quarantined: bool
    quarantine_reason: str | None


@dataclass(frozen=True, slots=True)
class AutoMemoryPublicationResult:
    status: str
    candidate_id: str
    target_layer: str | None
    reviewed_object_id: str | None
    publication_id: str | None
    published_object_id: str | None
    published_ref: str | None
    rollback_ref: str | None
    memory_publication_state: str
    skipped_reason: str | None


class GetAutoMemoryPublicationSettings:
    _COLLECTION = "auto_memory_publication_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> AutoMemoryPublicationSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        if record is None:
            return _settings_from_record(_default_settings_record())
        return _settings_from_record(record)


class SaveAutoMemoryPublicationSettings:
    _COLLECTION = "auto_memory_publication_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort, *, now: str = "2026-07-02T10:30:00+08:00") -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        confirm_enable: bool = False,
        allowed_layers: Sequence[str] = ("atom", "scenario", "series_memory", "project_skill"),
    ) -> AutoMemoryPublicationSettings:
        clean_layers = _allowed_layers(allowed_layers)
        if enabled and confirm_enable is not True:
            raise AutoMemoryPublicationError("enabling auto memory publication requires confirm_enable=true")
        if enabled and clean_layers != ("atom",):
            raise AutoMemoryPublicationError(
                "automatic memory publication configuration is limited to atom while quarantined"
            )
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": bool(enabled),
            "allowed_layers": list(clean_layers),
            "reviewer": "system",
            "publisher": "system",
            "rollback_required": True,
            "quarantined": bool(enabled),
            "quarantine_reason": _QUARANTINE_REASON if enabled else None,
            "updated_at": self._now,
        }
        self._object_store.write(self._COLLECTION, self._SETTINGS_ID, record, expected_revision=None)
        return _settings_from_record(record)


class AutoPublishMemoryCandidate:
    """Promote and publish a candidate only when local auto-publication is explicitly enabled."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        candidates: ObjectStoreMemoryCandidateRepository | None = None,
        namespace_id: str = "default",
        now: str = "2026-07-02T10:35:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._candidates = candidates or ObjectStoreMemoryCandidateRepository(object_store)
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, candidate_id: str, reason: str | None = None) -> AutoMemoryPublicationResult:
        clean_candidate_id = _required_input(candidate_id, "candidate_id")
        settings = GetAutoMemoryPublicationSettings(self._object_store).execute()
        candidate = self._candidates.get(clean_candidate_id)
        if candidate is None:
            raise AutoMemoryPublicationError("memory candidate not found")
        target_layer = _required_layer(candidate.get("target_layer"))
        if settings.enabled is not True:
            return self._skipped(clean_candidate_id, target_layer, "auto memory publication is disabled")
        _ = reason
        return self._skipped(clean_candidate_id, target_layer, _QUARANTINE_REASON)

    def _skipped(self, candidate_id: str, target_layer: str, reason: str) -> AutoMemoryPublicationResult:
        return AutoMemoryPublicationResult(
            status="skipped",
            candidate_id=candidate_id,
            target_layer=target_layer,
            reviewed_object_id=None,
            publication_id=None,
            published_object_id=None,
            published_ref=None,
            rollback_ref=None,
            memory_publication_state="not_published",
            skipped_reason=reason,
        )

def serialize_auto_memory_publication_settings(settings: AutoMemoryPublicationSettings) -> dict[str, object]:
    return {
        "status": settings.status,
        "enabled": settings.enabled,
        "allowed_layers": list(settings.allowed_layers),
        "reviewer": settings.reviewer,
        "publisher": settings.publisher,
        "explicit_enable_required": settings.explicit_enable_required,
        "rollback_required": settings.rollback_required,
        "quarantined": settings.quarantined,
        "quarantine_reason": settings.quarantine_reason,
    }


def serialize_auto_memory_publication_result(result: AutoMemoryPublicationResult) -> dict[str, object]:
    return {
        "status": result.status,
        "candidate_id": result.candidate_id,
        "target_layer": result.target_layer,
        "reviewed_object_id": result.reviewed_object_id,
        "publication_id": result.publication_id,
        "published_object_id": result.published_object_id,
        "published_ref": result.published_ref,
        "rollback_ref": result.rollback_ref,
        "memory_publication_state": result.memory_publication_state,
        "skipped_reason": result.skipped_reason,
    }


def _settings_from_record(record: Mapping[str, object]) -> AutoMemoryPublicationSettings:
    enabled = record.get("enabled") is True
    return AutoMemoryPublicationSettings(
        status="quarantined" if enabled else "disabled",
        enabled=enabled,
        allowed_layers=_allowed_layers(record.get("allowed_layers")),
        reviewer="system",
        publisher="system",
        explicit_enable_required=True,
        rollback_required=True,
        quarantined=enabled,
        quarantine_reason=_QUARANTINE_REASON if enabled else None,
    )


def _default_settings_record() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "default",
        "enabled": False,
        "allowed_layers": ["atom", "scenario", "series_memory", "project_skill"],
        "reviewer": "system",
        "publisher": "system",
        "rollback_required": True,
        "quarantined": False,
        "quarantine_reason": None,
    }


def _allowed_layers(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AutoMemoryPublicationError("allowed_layers must be a list")
    layers: list[str] = []
    for item in value:
        layer = _required_layer(item)
        if layer not in layers:
            layers.append(layer)
    if not layers:
        raise AutoMemoryPublicationError("allowed_layers is required")
    return tuple(layers)


def _required_layer(value: object) -> str:
    if value not in {"atom", "scenario", "series_memory", "project_skill"}:
        raise AutoMemoryPublicationError("target layer is not supported")
    return str(value)


def _required_input(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutoMemoryPublicationError(f"{field_name} is required")
    return value.strip()
