from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.memory_core import (
    ManualPublicationContractError,
    ObjectStoreMemoryStore,
    STAGING_PUBLICATION_CONTEXT_COLLECTION,
    build_manual_publication_record,
    manual_publication_context_id,
)
from .ports import ObjectStorePort


class MemoryPublicationError(ValueError):
    """Raised when staging memory cannot be formally published."""


@dataclass(frozen=True, slots=True)
class MemoryPublicationResult:
    status: str
    layer: str
    object_id: str
    published_object_id: str
    publication_id: str
    transition_id: str
    published_ref: str
    rollback_ref: str
    memory_publication_state: str


@dataclass(frozen=True, slots=True)
class MemoryRollbackResult:
    status: str
    layer: str
    object_id: str
    publication_id: str
    transition_id: str
    rollback_ref: str
    memory_publication_state: str


class PublishStagingMemoryToMemory:
    """Publish a staged Memory object into long-term Memory after explicit user confirmation."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        memory: ObjectStoreMemoryStore | None = None,
        namespace_id: str = "default",
        now: str = "2026-07-01T19:05:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._memory = memory or ObjectStoreMemoryStore(object_store)
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        object_id: str,
        layer: str,
        confirm: bool,
        reason: str,
        published_by: str = "user",
        authorized_system_publication: bool = False,
    ) -> MemoryPublicationResult:
        clean_object_id = _required_input(object_id, "object_id")
        clean_layer = _required_layer(layer)
        if confirm is not True:
            raise MemoryPublicationError("memory publication requires confirm=true")
        if published_by == "system" and authorized_system_publication is not True:
            raise MemoryPublicationError("system memory publication requires authorized_system_publication=true")
        if published_by not in {"user", "system"}:
            raise MemoryPublicationError("memory publication requires user confirmation")
        if published_by != "user":
            raise MemoryPublicationError("manual memory publication requires user publisher")
        _required_input(reason, "reason")
        staged = self._memory.staged(clean_layer, clean_object_id)
        if staged is None:
            raise MemoryPublicationError(f"staging {clean_layer} not found")
        external_series_update = _external_series_update(staged, layer=clean_layer, object_id=clean_object_id)
        if external_series_update is None and self._memory.get(clean_layer, clean_object_id) is not None:
            raise MemoryPublicationError(f"memory {clean_layer} already published")
        context = self._object_store.read(
            STAGING_PUBLICATION_CONTEXT_COLLECTION,
            manual_publication_context_id(clean_layer, clean_object_id),
        )
        if context is None:
            raise MemoryPublicationError("staging memory requires canonical manual publication context")
        published_object = dict(staged)
        published_object.pop("external_series_update", None)
        published_object["trust_status"] = "user_confirmed"
        published_object["updated_at"] = self._now
        if external_series_update is None:
            published_object_id = self._memory.publish(clean_layer, published_object)
        else:
            try:
                self._object_store.write(
                    "memory_series_memory",
                    clean_object_id,
                    published_object,
                    expected_revision=external_series_update["expected_object_revision"],
                )
            except ValueError as error:
                raise MemoryPublicationError("external Series current revision conflicted") from error
            if not self._object_store.delete("staging_series_memory", clean_object_id):
                raise MemoryPublicationError("external Series staging disappeared during publication")
            published_object_id = clean_object_id
        try:
            publication_record = build_manual_publication_record(
                context=context, namespace_id=self._namespace_id, layer=clean_layer,
                draft_id=clean_object_id, revision=_required_int(published_object, "revision"), published_at=self._now,
            )
        except ManualPublicationContractError as error:
            raise MemoryPublicationError(str(error)) from error
        publication_id = _required_str(publication_record, "publication_id")
        transition_id = _required_str(publication_record, "transition_ref").rsplit("/", 1)[-1].removesuffix(".json")
        published_ref = _required_str(publication_record, "published_ref")
        rollback_ref = _required_str(publication_record, "rollback_ref")
        source_refs = publication_record["source_refs"]
        transition = {
            "schema_version": "1.0.0",
            "id": transition_id,
            "object_type": clean_layer,
            "object_id": published_object_id,
            "transition_type": "confirm",
            "from_trust_status": _required_str(staged, "trust_status"),
            "to_trust_status": "user_confirmed",
            "from_revision": (
                external_series_update["base_series_revision"]
                if external_series_update is not None
                else 0
            ),
            "to_revision": _required_int(published_object, "revision"),
            "actor": published_by,
            "reason": _required_str(publication_record, "reason"),
            "evidence_refs": [
                {
                    "object_type": clean_layer,
                    "object_id": published_object_id,
                    "source_refs": [dict(ref) for ref in source_refs],
                }
            ],
            "created_at": self._now,
        }
        self._object_store.write("memory_transitions", transition_id, transition, expected_revision=None)
        self._object_store.write(
            "memory_publications",
            publication_id,
            publication_record,
            expected_revision=None,
        )
        self._object_store.delete(
            STAGING_PUBLICATION_CONTEXT_COLLECTION,
            manual_publication_context_id(clean_layer, clean_object_id),
        )
        return MemoryPublicationResult(
            status="published",
            layer=clean_layer,
            object_id=clean_object_id,
            published_object_id=published_object_id,
            publication_id=publication_id,
            transition_id=transition_id,
            published_ref=published_ref,
            rollback_ref=rollback_ref,
            memory_publication_state="published_with_rollback_ref",
        )


class PublishStagingAtomToMemory(PublishStagingMemoryToMemory):
    """Publish a staged Atom into long-term Memory after explicit user confirmation."""

    def execute(
        self,
        *,
        atom_id: str,
        confirm: bool,
        reason: str,
        published_by: str = "user",
        authorized_system_publication: bool = False,
    ) -> MemoryPublicationResult:
        return super().execute(
            object_id=atom_id,
            layer="atom",
            confirm=confirm,
            reason=reason,
            published_by=published_by,
            authorized_system_publication=authorized_system_publication,
        )


class RollbackPublishedMemory:
    """Rollback a published Memory object by publication id after user confirmation."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T20:05:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        publication_id: str,
        confirm: bool,
        reason: str,
        rolled_back_by: str = "user",
    ) -> MemoryRollbackResult:
        clean_publication_id = _required_input(publication_id, "publication_id")
        if confirm is not True:
            raise MemoryPublicationError("memory rollback requires confirm=true")
        if rolled_back_by != "user":
            raise MemoryPublicationError("memory rollback requires user confirmation")
        clean_reason = _required_input(reason, "reason")
        publication = self._object_store.read("memory_publications", clean_publication_id)
        if publication is None:
            raise MemoryPublicationError("memory publication not found")
        if publication.get("status") != "published":
            raise MemoryPublicationError("memory publication is not published")
        layer = _required_layer(_required_str(publication, "layer"))
        object_id = _required_str(publication, "published_object_id")
        published_object = self._object_store.read(_published_collection(layer), object_id)
        if published_object is None:
            raise MemoryPublicationError(f"published {layer} not found")
        source_refs = _source_refs(publication.get("source_refs")) or _source_refs(published_object.get("source_refs"))
        if not source_refs:
            raise MemoryPublicationError("memory rollback requires source refs")
        object_revision = _required_int(published_object, "revision")
        transition_id = f"transition-memory-rollback-{clean_publication_id}"
        rollback_ref = f"crp://{self._namespace_id}/memory-publications/{clean_publication_id}/rollback"
        transition = {
            "schema_version": "1.0.0",
            "id": transition_id,
            "object_type": layer,
            "object_id": object_id,
            "transition_type": "demote",
            "from_trust_status": _required_str(published_object, "trust_status"),
            "to_trust_status": "system_generated",
            "from_revision": object_revision,
            "to_revision": object_revision + 1,
            "actor": "user",
            "reason": clean_reason,
            "evidence_refs": [
                {
                    "object_type": layer,
                    "object_id": object_id,
                    "source_refs": [dict(ref) for ref in source_refs],
                }
            ],
            "created_at": self._now,
        }
        self._object_store.write("memory_transitions", transition_id, transition, expected_revision=None)
        removed = self._object_store.delete(_published_collection(layer), object_id)
        if not removed:
            raise MemoryPublicationError(f"published {layer} not found")
        updated_publication = dict(publication)
        updated_publication.update(
            {
                "status": "rolled_back",
                "rollback_reason": clean_reason,
                "rolled_back_by": "user",
                "rolled_back_at": self._now,
                "rollback_transition_ref": f"crp://{self._namespace_id}/memory-transitions/{transition_id}.json",
            }
        )
        self._object_store.write(
            "memory_publications",
            clean_publication_id,
            updated_publication,
            expected_revision=None,
        )
        return MemoryRollbackResult(
            status="rolled_back",
            layer=layer,
            object_id=object_id,
            publication_id=clean_publication_id,
            transition_id=transition_id,
            rollback_ref=rollback_ref,
            memory_publication_state="rolled_back_not_published",
        )


class RollbackPublishedAtomMemory(RollbackPublishedMemory):
    """Backward-compatible Atom rollback entrypoint."""


def serialize_memory_publication_result(result: MemoryPublicationResult) -> dict[str, object]:
    return {
        "status": result.status,
        "layer": result.layer,
        "object_id": result.object_id,
        "published_object_id": result.published_object_id,
        "publication_id": result.publication_id,
        "transition_id": result.transition_id,
        "published_ref": result.published_ref,
        "rollback_ref": result.rollback_ref,
        "memory_publication_state": result.memory_publication_state,
    }


def serialize_memory_rollback_result(result: MemoryRollbackResult) -> dict[str, object]:
    return {
        "status": result.status,
        "layer": result.layer,
        "object_id": result.object_id,
        "publication_id": result.publication_id,
        "transition_id": result.transition_id,
        "rollback_ref": result.rollback_ref,
        "memory_publication_state": result.memory_publication_state,
    }


def _source_refs(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("source_id")
        locator = item.get("locator")
        if isinstance(source_id, str) and source_id and isinstance(locator, str) and locator:
            refs.append(dict(item))
    return tuple(refs)


def _required_input(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise MemoryPublicationError(f"{field_name} is required")
    clean = value.strip()
    if not clean:
        raise MemoryPublicationError(f"{field_name} is required")
    return clean


def _required_layer(value: str) -> str:
    if value not in {"atom", "scenario", "series_memory", "project_skill"}:
        raise MemoryPublicationError("memory publication layer is not supported")
    return value


def _layer_slug(layer: str) -> str:
    return layer.replace("_", "-")


def _ref_layer(layer: str) -> str:
    return {
        "atom": "atom",
        "scenario": "scenario",
        "series_memory": "series",
        "project_skill": "project-skill",
    }[layer]


def _published_collection(layer: str) -> str:
    return {
        "atom": "memory_atoms",
        "scenario": "memory_scenarios",
        "series_memory": "memory_series_memory",
        "project_skill": "project_skills",
    }[layer]


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryPublicationError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MemoryPublicationError(f"{key} must be an integer")
    return value


def _external_series_update(
    staged: Mapping[str, object],
    *,
    layer: str,
    object_id: str,
) -> dict[str, int] | None:
    value = staged.get("external_series_update")
    if value is None:
        return None
    if layer != "series_memory" or not isinstance(value, Mapping):
        raise MemoryPublicationError("external Series staging metadata is invalid")
    candidate_id = value.get("candidate_id")
    expected_object_revision = value.get("expected_object_revision")
    base_series_revision = value.get("base_series_revision")
    payload_sha256 = value.get("payload_sha256")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or not isinstance(payload_sha256, str)
        or not payload_sha256
        or not isinstance(expected_object_revision, int)
        or isinstance(expected_object_revision, bool)
        or expected_object_revision < 1
        or not isinstance(base_series_revision, int)
        or isinstance(base_series_revision, bool)
        or base_series_revision < 1
        or _required_int(staged, "revision") != base_series_revision + 1
        or staged.get("id") != object_id
    ):
        raise MemoryPublicationError("external Series staging metadata is invalid")
    return {
        "expected_object_revision": expected_object_revision,
        "base_series_revision": base_series_revision,
    }
