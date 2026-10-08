from __future__ import annotations

import pytest

from backend.api.context_revision_resolver import (
    ContextRevisionResolverError,
    ProjectContextRevisionResolver,
)
from core.context_graph import ContextCompiler, FrozenContextRevisions


_DEFAULT_SELECTED = object()


def _snapshot(*, selected: object = _DEFAULT_SELECTED) -> dict[str, object]:
    return {
        "boundary": {"profile_revision": 7},
        "selected": {
            "provider_revision": "provider-r4",
            "route_revision": 9,
            "execution_location": "remote",
        } if selected is _DEFAULT_SELECTED else selected,
    }


def test_projects_context_evaluate_authority_with_identity_only_input(monkeypatch):
    seen: dict[str, object] = {}

    def project(container, **kwargs):
        seen["container"] = container
        seen.update(kwargs)
        return _snapshot()

    monkeypatch.setattr(
        "backend.api.context_revision_resolver.project_turn_model_routing_snapshot", project,
    )
    resolver = ProjectContextRevisionResolver(
        object(), lambda capability_id: f"active-{capability_id}-r4",
    )

    result = resolver("project-a", "thought_graph_context", "binding-a", True)

    assert result == FrozenContextRevisions(
        "active-thought_graph_context-r4", "7", "provider-r4", "9",
        ContextCompiler.compiler_revision,
    )
    assert seen == {
        "container": seen["container"],
        "turn_id": "context-binding:binding-a",
        "project_id": "project-a",
        "required_capability": "structured",
        "modality": "text",
        "output_contract": "json_object",
        "egress_purpose": "search_answer",
        "egress_categories": ("instructions", "source_excerpt"),
        "privacy_scope": "remote_allowed",
        "retention_policy": "turn_only",
        "capability_ids": ("thought_graph_context",),
        "context_policy": {
            "include_project_skill": False, "include_memory": False,
            "include_session_history": False, "max_context_bytes": 262144,
        },
        "input_refs": ("crp://context-bindings/project-a/binding-a",),
    }


def test_allow_remote_false_projects_local_only(monkeypatch):
    seen: dict[str, object] = {}

    def project(_container, **kwargs):
        seen.update(kwargs)
        return _snapshot()

    monkeypatch.setattr(
        "backend.api.context_revision_resolver.project_turn_model_routing_snapshot", project,
    )
    result = ProjectContextRevisionResolver(object(), lambda _id: "cap-r1")(
        "project-a", "thought_graph_context", "binding-a", False,
    )

    assert seen["privacy_scope"] == "local_only"
    assert result.compiler_revision == ContextCompiler.compiler_revision


def test_resolve_returns_one_snapshot_location_and_can_require_remote(monkeypatch):
    calls = 0

    def project(_container, **_kwargs):
        nonlocal calls
        calls += 1
        return _snapshot()

    monkeypatch.setattr(
        "backend.api.context_revision_resolver.project_turn_model_routing_snapshot", project,
    )
    resolution = ProjectContextRevisionResolver(object(), lambda _id: "cap-r1").resolve(
        "project-a", "generic-capability", "binding-a", True,
    ).require_execution_location("remote")

    assert calls == 1
    assert resolution.execution_location == "remote"
    assert resolution.revisions == FrozenContextRevisions(
        "cap-r1", "7", "provider-r4", "9", ContextCompiler.compiler_revision,
    )


def test_local_loopback_cannot_satisfy_remote_requirement(monkeypatch):
    monkeypatch.setattr(
        "backend.api.context_revision_resolver.project_turn_model_routing_snapshot",
        lambda *_args, **_kwargs: _snapshot(selected={
            "provider_revision": "provider-r4",
            "route_revision": 9,
            "execution_location": "local_loopback",
        }),
    )
    resolution = ProjectContextRevisionResolver(object(), lambda _id: "cap-r1").resolve(
        "project-a", "generic-capability", "binding-a", True,
    )

    with pytest.raises(ContextRevisionResolverError, match="routing_execution_location_mismatch"):
        resolution.require_execution_location("remote")


def test_missing_execution_location_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "backend.api.context_revision_resolver.project_turn_model_routing_snapshot",
        lambda *_args, **_kwargs: _snapshot(selected={
            "provider_revision": "provider-r4", "route_revision": 9,
        }),
    )
    resolver = ProjectContextRevisionResolver(object(), lambda _id: "cap-r1")

    with pytest.raises(ContextRevisionResolverError, match="routing_execution_location_unavailable"):
        resolver.resolve("project-a", "generic-capability", "binding-a", True)


def test_selected_route_missing_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "backend.api.context_revision_resolver.project_turn_model_routing_snapshot",
        lambda *_args, **_kwargs: _snapshot(selected=None),
    )
    resolver = ProjectContextRevisionResolver(object(), lambda _id: "cap-r1")

    with pytest.raises(ContextRevisionResolverError, match="routing_selection_unavailable"):
        resolver("project-a", "thought_graph_context", "binding-a", True)


def test_incomplete_selected_or_boundary_revision_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "backend.api.context_revision_resolver.project_turn_model_routing_snapshot",
        lambda *_args, **_kwargs: {"boundary": {}, "selected": {
            "provider_revision": "p", "route_revision": "r", "execution_location": "remote",
        }},
    )
    resolver = ProjectContextRevisionResolver(object(), lambda _id: "cap-r1")

    with pytest.raises(ContextRevisionResolverError, match="routing_revision_incomplete"):
        resolver("project-a", "thought_graph_context", "binding-a", True)
