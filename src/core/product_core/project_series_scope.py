"""Resolve a Series into one frozen Project scope from Memory authority.

The legacy Series surface has no independent project-membership authority.  A
caller must therefore resolve it through the selected compound Memory
authority and retain this snapshot for the lifetime of a Turn.  This module
deliberately does not infer a project from a client hint, a session slug, or a
legacy default.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.storage_provider import JsonObjectStore, read_json_object_store_collection


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_TRUSTED_STATUSES = frozenset(("trusted", "user_confirmed"))
_COLLECTION = "memory_series_memory"


class ProjectSeriesScopeError(ValueError):
    """A stable, fail-closed error from Series to Project scope resolution."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProjectSeriesScopeSnapshot:
    """The authority facts a Turn must freeze before executing legacy work."""

    namespace_id: str
    project_id: str
    series_id: str
    object_id: str
    payload_revision: int
    storage_revision: int
    authority_identity: str
    authority_ref: str


class ProjectSeriesScopeResolver:
    """Resolve exactly one trusted, current L3 Series Memory mapping."""

    def __init__(
        self,
        factory: AggregateRepositoryFactory,
        object_store: JsonObjectStore,
    ) -> None:
        self._factory = factory
        self._object_store = object_store

    def resolve(self, series_id: str) -> ProjectSeriesScopeSnapshot:
        clean_series_id = _required_id(series_id, "series_id")
        try:
            authority = self._factory.memory_publication_authority_resolution()
        except AggregateRepositoryFactoryError as error:
            raise ProjectSeriesScopeError("authority_unavailable") from error
        except Exception as error:
            raise ProjectSeriesScopeError("authority_unavailable") from error

        try:
            if authority.records is None:
                candidates = tuple(
                    _Candidate(
                        object_id=record.object_id,
                        payload=record.payload,
                        storage_revision=None,
                    )
                    for record in read_json_object_store_collection(
                        self._object_store.root,
                        namespace_id=self._factory.namespace_id,
                        collection=_COLLECTION,
                    )
                    if record.payload.get("series_id") == clean_series_id
                )
            else:
                candidates = tuple(
                    _Candidate(
                        object_id=record.object_id,
                        payload=record.payload,
                        storage_revision=record.revision,
                    )
                    for record in authority.records.list(_COLLECTION)
                    if record.payload.get("series_id") == clean_series_id
                )
        except ProjectSeriesScopeError:
            raise
        except Exception as error:
            raise ProjectSeriesScopeError("authority_read_failed") from error

        if not candidates:
            raise ProjectSeriesScopeError("series_scope_not_found")
        if len(candidates) != 1:
            raise ProjectSeriesScopeError("series_scope_ambiguous")

        candidate = candidates[0]
        object_id = _payload_id(candidate.payload)
        if candidate.object_id != object_id:
            raise ProjectSeriesScopeError("series_scope_record_invalid")
        project_id = _single_project_id(candidate.payload)
        payload_revision = _positive_int(candidate.payload.get("revision"))
        try:
            storage_revision = (
                self._object_store.revision(_COLLECTION, object_id)
                if candidate.storage_revision is None
                else candidate.storage_revision
            )
        except Exception as error:
            raise ProjectSeriesScopeError("authority_read_failed") from error
        if storage_revision <= 0:
            raise ProjectSeriesScopeError("series_scope_record_invalid")
        if candidate.payload.get("stale") is not False:
            raise ProjectSeriesScopeError("series_scope_stale")
        if candidate.payload.get("trust_status") not in _TRUSTED_STATUSES:
            raise ProjectSeriesScopeError("series_scope_untrusted")

        namespace_id = _required_id(self._factory.namespace_id, "namespace_id")
        return ProjectSeriesScopeSnapshot(
            namespace_id=namespace_id,
            project_id=project_id,
            series_id=clean_series_id,
            object_id=object_id,
            payload_revision=payload_revision,
            storage_revision=storage_revision,
            authority_identity=authority.authority_identity,
            authority_ref=f"crp://{namespace_id}/memory/series/{object_id}",
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    object_id: str
    payload: Mapping[str, object]
    storage_revision: int | None


def _payload_id(payload: Mapping[str, object]) -> str:
    return _required_id(payload.get("id"), "id")


def _single_project_id(payload: Mapping[str, object]) -> str:
    project_ids = payload.get("project_ids")
    if not isinstance(project_ids, Sequence) or isinstance(project_ids, (str, bytes)):
        raise ProjectSeriesScopeError("series_scope_record_invalid")
    if len(project_ids) != 1:
        raise ProjectSeriesScopeError("project_scope_ambiguous")
    return _required_id(project_ids[0], "project_id")


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProjectSeriesScopeError("series_scope_record_invalid")
    return value


def _required_id(value: object, _field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ProjectSeriesScopeError("series_scope_record_invalid")
    return value
