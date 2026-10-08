"""Pre-accept authority canonicalization for Series AI Turns.

The public request may name a legacy Series, but it must never select its
Project.  Before a durable Turn is accepted this adapter resolves the one
authoritative Project membership and freezes only portable, revision-bound
facts into the immutable request.  Recovery reads that request; it does not
resolve current membership again.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.product_core.project_series_scope import (
    ProjectSeriesScopeResolver,
)


class SeriesTurnScopeAuthorityError(ValueError):
    """Fail-closed rejection before a Series Turn can be accepted."""


@dataclass(frozen=True, slots=True)
class FrozenAgentSeriesScope:
    """Revision-bound membership facts for a legacy Agent series turn."""

    project_id: str
    series_id: str
    object_id: str
    payload_revision: int
    storage_revision: int
    authority_identity: str
    authority_ref: str


class SeriesTurnScopeAuthority:
    """Canonicalize a Series Turn using a single Project scope resolver."""

    def __init__(self, resolver: ProjectSeriesScopeResolver) -> None:
        self._resolver = resolver

    def canonicalize(self, request: Mapping[str, object]) -> dict[str, object]:
        canonical = deepcopy(dict(request))
        scope = canonical.get("scope")
        if not isinstance(scope, Mapping):
            raise SeriesTurnScopeAuthorityError("series_turn_scope_invalid")
        scope_data = dict(scope)
        if scope_data.get("kind") != "series":
            return canonical
        if "authority" in scope_data:
            # This field is written only by this pre-accept adapter.  A client
            # supplied null is rejected too, so a request cannot smuggle a
            # seemingly harmless placeholder across an authority boundary.
            raise SeriesTurnScopeAuthorityError("series_turn_authority_client_supplied")
        series_id = scope_data.get("series_id")
        if not isinstance(series_id, str) or not series_id:
            raise SeriesTurnScopeAuthorityError("series_turn_series_id_invalid")
        snapshot = self._resolver.resolve(series_id)
        asserted_project_id = scope_data.get("project_id")
        if asserted_project_id is not None and asserted_project_id != snapshot.project_id:
            raise SeriesTurnScopeAuthorityError("series_turn_project_assertion_mismatch")
        canonical["scope"] = {
            "kind": "series",
            "project_id": snapshot.project_id,
            "series_id": snapshot.series_id,
            "authority": {
                "kind": "project_series_scope_v1",
                "object_id": snapshot.object_id,
                "payload_revision": snapshot.payload_revision,
                "storage_revision": snapshot.storage_revision,
                "authority_identity": snapshot.authority_identity,
                "authority_ref": snapshot.authority_ref,
            },
        }
        return canonical

    def freeze_agent_series_scope(
        self,
        *,
        series_id: str,
        asserted_project_id: str | None = None,
    ) -> FrozenAgentSeriesScope:
        """Resolve and freeze membership facts for a legacy Agent series turn."""
        if not isinstance(series_id, str) or not series_id:
            raise SeriesTurnScopeAuthorityError("series_turn_series_id_invalid")
        snapshot = self._resolver.resolve(series_id)
        if asserted_project_id is not None and asserted_project_id != snapshot.project_id:
            raise SeriesTurnScopeAuthorityError("series_turn_project_assertion_mismatch")
        return FrozenAgentSeriesScope(
            project_id=snapshot.project_id,
            series_id=snapshot.series_id,
            object_id=snapshot.object_id,
            payload_revision=snapshot.payload_revision,
            storage_revision=snapshot.storage_revision,
            authority_identity=snapshot.authority_identity,
            authority_ref=snapshot.authority_ref,
        )


def build_series_turn_scope_authority(runtime_root: Path) -> SeriesTurnScopeAuthority:
    """Build the resolver from the existing rebuild authority composition."""

    store, settings = build_rebuild_object_store(runtime_root)
    factory = AggregateRepositoryFactory(
        runtime_root=Path(runtime_root),
        namespace_id=settings.namespace_id,
        # SourceAssetRuntimeStore delegates JsonObjectStore operations for the
        # unrelated Memory collections consumed by the resolver.
        json_store=store,  # type: ignore[arg-type]
    )
    return SeriesTurnScopeAuthority(ProjectSeriesScopeResolver(factory, store))
