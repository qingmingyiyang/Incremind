from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver,
    ProjectAwareContextManifestResolver,
    ProjectProfileResolutionError,
    TurnProjectProfileSnapshotAuthority,
)
from backend.api.published_project_memory_snapshot import PublishedProjectMemorySnapshot
from backend.api.context_compaction import DeterministicLocalMemoryCompactor
from backend.api.runtime_self_manifest_runtime import build_runtime_self_manifest_for_app
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import (
    CapabilityDefinition, ContextEntry, ContextManifest, InMemoryTurnPayloadStore,
    context_manifest_from_payload, context_manifest_to_payload,
)
from core.ai_tooling import (
    MCPServerSelectionBinding,
    ToolConnectionIdentity,
    ToolDefinition,
    ToolRetryPolicy,
)


ROOT = Path(__file__).resolve().parents[4]


def test_project_profiles_filter_capabilities_and_stamp_both_manifest_revisions(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    boundary_store.update(
        "project-alpha", mode="sealed", remote_default="deny", expected_revision=0
    )
    capability_store.update(
        "project-alpha",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=1,
        denied_tool_ids=("local.write",),
    )
    snapshots = TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    request = _request()
    capabilities = (
        _capability("memory.recall", "read"),
        _capability("remote.answer", "external"),
        _capability("local.write", "write"),
    )
    request["capability_policy"] = {
        "allowed": [item.capability_id for item in capabilities],
        "denied": [],
        "require_approval": ["remote.answer", "local.write"],
    }
    manifest = ProjectAwareCapabilityManifestResolver(snapshots).resolve(request, capabilities)
    assert manifest.profile_id == "project-capability-project-alpha"
    assert manifest.boundary_profile_id == "project-boundary-project-alpha"
    assert manifest.boundary_profile_revision == 1
    assert manifest.capability_ids == ("memory.recall",)
    assert dict(manifest.excluded_reason_counts) == {
        "project_denied": 1,
        "sealed_destination": 1,
    }
    context = ProjectAwareContextManifestResolver(snapshots).resolve(
        request,
        f"crp://session/{request['turn_id']}/capability-manifest/ref",
        manifest,
    )
    assert context.project_profile_id == "project-capability-project-alpha"
    assert context.project_profile_revision == 1
    assert context.boundary_profile_id == "project-boundary-project-alpha"
    assert context.boundary_profile_revision == 1


def test_runtime_self_manifest_is_frozen_for_global_and_project_turns(tmp_path: Path) -> None:
    payloads = InMemoryTurnPayloadStore()
    manifest = _runtime_self_manifest(tmp_path)
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    resolver = ProjectAwareContextManifestResolver(
        TurnProjectProfileSnapshotAuthority(capability_store, boundary_store),
        runtime_self_manifest=manifest,
        payloads=payloads,
    )
    project_request = _request()
    capability = _capability("memory.recall", "read")
    project_capabilities = ProjectAwareCapabilityManifestResolver(
        TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    ).resolve(project_request, (capability,))
    project_context = resolver.resolve(
        project_request,
        f"crp://session/{project_request['turn_id']}/capabilities",
        project_capabilities,
    )
    project_entry = next(entry for entry in project_context.entries if entry.kind == "runtime_self_manifest")
    assert project_entry.source_project_id == "project-alpha"
    assert payloads.get(project_entry.payload_ref)["manifest_revision"] == manifest["manifest_revision"]

    global_request = dict(project_request)
    global_request["turn_id"] = "turn-runtime-global"
    global_request["scope"] = {"kind": "global"}
    global_capabilities = ProjectAwareCapabilityManifestResolver(
        TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    ).resolve(global_request, (capability,))
    global_context = resolver.resolve(
        global_request,
        f"crp://session/{global_request['turn_id']}/capabilities",
        global_capabilities,
    )
    global_entry = next(entry for entry in global_context.entries if entry.kind == "runtime_self_manifest")
    assert global_entry.source_project_id is None
    assert global_entry.disclosure == "model"


def test_runtime_self_manifest_degrades_to_tool_only_when_turn_budget_is_full(tmp_path: Path) -> None:
    payloads = InMemoryTurnPayloadStore()
    resolver = ProjectAwareContextManifestResolver(
        TurnProjectProfileSnapshotAuthority(
            ProjectCapabilityProfileStore(tmp_path), ProjectBoundaryProfileStore(tmp_path),
        ),
        runtime_self_manifest=_runtime_self_manifest(
            tmp_path,
            features=tuple(sorted((
                "isolated-python-artifact",
                *(f"diagnostic-feature-{index:03d}" for index in range(80)),
            ))),
        ),
        payloads=payloads,
    )
    request = _request()
    request["scope"] = {"kind": "global"}
    request["context_policy"] = {
        **request["context_policy"],
        "max_context_bytes": 1024,
    }
    capability = _capability("memory.recall", "read")
    capability_manifest = ProjectAwareCapabilityManifestResolver(
        TurnProjectProfileSnapshotAuthority(
            ProjectCapabilityProfileStore(tmp_path), ProjectBoundaryProfileStore(tmp_path),
        )
    ).resolve(request, (capability,))

    context = resolver.resolve(
        request,
        f"crp://session/{request['turn_id']}/capabilities",
        capability_manifest,
    )

    entry = next(entry for entry in context.entries if entry.kind == "runtime_self_manifest")
    assert entry.disclosure == "tool_only"
    assert entry.content_bytes == 0
    assert context.selected_context_bytes == 0


def test_exact_capability_request_narrows_project_manifest_with_task_tool_ids(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    request = _request()
    request["capability_policy"] = {
        "allowed": ["memory.recall", "document.draft"],
        "denied": [],
        "require_approval": [],
    }
    request["capability_request"] = {
        "mode": "execute_exact_v1",
        "capability_id": "memory.recall",
        "arguments": {"query": "fixture"},
    }

    manifest = ProjectAwareCapabilityManifestResolver(
        TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    ).resolve(
        request,
        (_capability("memory.recall", "read"), _capability("document.draft", "write")),
    )

    assert manifest.capability_ids == ("memory.recall",)
    assert dict(manifest.excluded_reason_counts) == {"task_not_selected": 1}


def test_capability_and_context_manifest_share_one_turn_snapshot(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    boundary_store.update(
        "project-alpha", mode="guarded", remote_default="review", expected_revision=0
    )
    snapshots = TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    request = _request()
    capability = _capability("memory.recall", "read")
    manifest = ProjectAwareCapabilityManifestResolver(snapshots).resolve(request, (capability,))
    boundary_store.update(
        "project-alpha", mode="sealed", remote_default="deny", expected_revision=1
    )
    context = ProjectAwareContextManifestResolver(snapshots).resolve(
        request,
        f"crp://session/{request['turn_id']}/capability-manifest/ref",
        manifest,
    )
    assert context.boundary_profile_revision == 1
    next_request = _request()
    next_request["turn_id"] = "turn-ffffffffffffffffffffffffffffffff"
    with pytest.raises(ProjectProfileResolutionError, match="revisions do not match"):
        ProjectAwareCapabilityManifestResolver(snapshots).resolve(next_request, (capability,))
    capability_store.update(
        "project-alpha",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=2,
    )
    next_manifest = ProjectAwareCapabilityManifestResolver(snapshots).resolve(next_request, (capability,))
    next_context = ProjectAwareContextManifestResolver(snapshots).resolve(
        next_request,
        f"crp://session/{next_request['turn_id']}/capability-manifest/ref",
        next_manifest,
    )
    assert next_context.boundary_profile_revision == 2


def test_context_rejects_capability_manifest_binding_drift(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    snapshots = TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    request = _request()
    manifest = ProjectAwareCapabilityManifestResolver(snapshots).resolve(
        request, (_capability("memory.recall", "read"),)
    )
    with pytest.raises(ProjectProfileResolutionError, match="Boundary binding drifted"):
        ProjectAwareContextManifestResolver(snapshots).resolve(
            request,
            f"crp://session/{request['turn_id']}/capability-manifest/ref",
            replace(manifest, boundary_profile_revision=2),
        )


def test_explicit_legacy_binding_migration_allows_a_new_turn(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    boundary_store.update(
        "project-alpha", mode="guarded", remote_default="review", expected_revision=0
    )
    boundary_store.update(
        "project-alpha", mode="sealed", remote_default="deny", expected_revision=1
    )
    capability_store.update(
        "project-alpha",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=1,
    )
    path = tmp_path / "library/projects/project-alpha/ai/capability-profile.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["schema_version"] = "1.0.0"
    legacy.pop("boundary_profile_revision")
    legacy.pop("tool_discovery_policy")
    legacy.pop("tool_selection_bindings")
    legacy.pop("mcp_server_selection_bindings")
    path.write_text(json.dumps(legacy), encoding="utf-8")
    capability_store.migrate_boundary_binding(
        "project-alpha",
        expected_revision=1,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=2,
    )

    manifest = ProjectAwareCapabilityManifestResolver(
        TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    ).resolve(_request(), (_capability("memory.recall", "read"),))

    assert manifest.boundary_profile_revision == 2


def test_profile_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    capability_store.update(
        "project-alpha",
        expected_revision=0,
        boundary_profile_id="wrong-boundary-profile",
        boundary_profile_revision=1,
    )
    snapshots = TurnProjectProfileSnapshotAuthority(
        capability_store, ProjectBoundaryProfileStore(tmp_path)
    )
    with pytest.raises(ProjectProfileResolutionError, match="do not match"):
        ProjectAwareCapabilityManifestResolver(snapshots).resolve(
            _request(), (_capability("memory.recall", "read"),)
        )


def test_global_turn_uses_v1_fallback_and_does_not_load_project_profile(tmp_path: Path) -> None:
    snapshots = TurnProjectProfileSnapshotAuthority(
        ProjectCapabilityProfileStore(tmp_path), ProjectBoundaryProfileStore(tmp_path)
    )
    request = _request()
    request["scope"] = {"kind": "global", "project_id": None, "series_id": None}
    manifest = ProjectAwareCapabilityManifestResolver(snapshots).resolve(
        request, (_capability("memory.recall", "read"),)
    )
    assert manifest.resolver_id == "v1-turn-policy"
    assert not (tmp_path / "library/projects").exists()


def test_native_mcp_capability_requires_exact_enabled_server(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    capability_store.update(
        "project-alpha",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=1,
        enabled_sources=("core", "mcp"),
    )
    capability = _mcp_capability()
    request = _request()
    request["capability_policy"] = {
        "allowed": [capability.capability_id], "denied": [], "require_approval": [],
    }
    resolver = ProjectAwareCapabilityManifestResolver(
        TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    )

    excluded = resolver.resolve(request, (capability,))

    assert excluded.capability_ids == ()
    assert dict(excluded.excluded_reason_counts) == {"mcp_disabled": 1}

    capability_store.update(
        "project-alpha",
        expected_revision=1,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=1,
        enabled_sources=("core", "mcp"),
        enabled_mcp_server_ids=("calendar-server",),
        mcp_server_selection_bindings=(_calendar_binding(),),
    )
    enabled_request = _request()
    enabled_request["turn_id"] = "turn-ffffffffffffffffffffffffffffffff"
    enabled_request["capability_policy"] = request["capability_policy"]
    enabled = ProjectAwareCapabilityManifestResolver(
        TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    ).resolve(enabled_request, (capability,))

    assert enabled.capability_ids == ("calendar.read",)


def test_context_manifest_injects_only_l0_l1_published_memory_and_keeps_recall_available(
    tmp_path: Path,
) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    snapshots = TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    request = _request()
    request["context_policy"] = {"max_context_bytes": 1024}
    capability = ProjectAwareCapabilityManifestResolver(snapshots).resolve(
        request, (_capability("memory.recall", "read"),)
    )
    skill_snapshot = SimpleNamespace(
        payload_ref=f"crp://session/{request['turn_id']}/application-skill-snapshot/ref",
        revision="application-skills-r7",
        payload={"selected": [{
            "instruction_bytes": 600,
            "instruction_payload_ref": f"crp://session/{request['turn_id']}/application-skill/item-a",
            "binding_revision": "binding-r3",
            "skill_fingerprint": "skill-fingerprint-a",
        }]},
    )
    published = _PublishedMemoryStub(request["turn_id"])
    context = ProjectAwareContextManifestResolver(
        snapshots,
        application_skills=_ApplicationSkillStub(skill_snapshot),
        published_memory=published,  # type: ignore[arg-type]
    ).resolve(
        request,
        f"crp://session/{request['turn_id']}/capability-manifest/ref",
        replace(
            capability,
            application_skill_snapshot_ref=skill_snapshot.payload_ref,
            application_skill_snapshot_revision=skill_snapshot.revision,
        ),
    )

    assert published.max_context_bytes == 424
    assert [(entry.kind, entry.source_ref, entry.payload_ref, entry.revision_identity, entry.content_bytes) for entry in context.entries[-3:]] == [
        (
            "memory_r0",
            "crp://memory/project-alpha/published-project-memory-snapshot",
            f"crp://session/{request['turn_id']}/published-project-memory-snapshot-v1/ref",
            "published-memory-r5",
            0,
        ),
        (
            "project_skill",
            "crp://skills/project-alpha/skill-alpha",
            f"crp://session/{request['turn_id']}/published-project-memory-item-v1-0/ref",
            "4",
            100,
        ),
        (
            "memory_r1",
            "crp://memory/project-alpha/atom-alpha",
            f"crp://session/{request['turn_id']}/published-project-memory-item-v1-1/ref",
            "9",
            324,
        ),
    ]
    assert context.selected_context_bytes == 1024
    assert context.entries[-1].provenance_refs == (context.entries[-3].payload_ref,)
    assert capability.capability_ids == ("memory.recall",)
    assert not any(
        entry.kind in {"memory_r2", "memory_r3"} and entry.disclosure == "model"
        for entry in context.entries
    )


def test_context_manifest_allows_application_skill_to_consume_the_full_budget(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    snapshots = TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    request = _request()
    request["context_policy"] = {"max_context_bytes": 1024}
    capability = ProjectAwareCapabilityManifestResolver(snapshots).resolve(
        request, (_capability("memory.recall", "read"),)
    )
    skill_snapshot = SimpleNamespace(
        payload_ref=f"crp://session/{request['turn_id']}/application-skill-snapshot/ref",
        revision="application-skills-full-budget",
        payload={"selected": [{
            "instruction_bytes": 1024,
            "instruction_payload_ref": f"crp://session/{request['turn_id']}/application-skill/item-a",
            "binding_revision": "binding-r3",
            "skill_fingerprint": "skill-fingerprint-a",
        }]},
    )
    published = _PublishedMemoryStub(request["turn_id"])

    context = ProjectAwareContextManifestResolver(
        snapshots,
        application_skills=_ApplicationSkillStub(skill_snapshot),
        published_memory=published,  # type: ignore[arg-type]
    ).resolve(
        request,
        f"crp://session/{request['turn_id']}/capability-manifest/ref",
        replace(
            capability,
            application_skill_snapshot_ref=skill_snapshot.payload_ref,
            application_skill_snapshot_revision=skill_snapshot.revision,
        ),
    )

    assert published.max_context_bytes == 0
    assert context.selected_context_bytes == 1024
    assert not any(entry.selection_reason == "published_project_memory_selected_within_remaining_budget" for entry in context.entries)


def test_local_memory_compactor_freezes_smaller_turn_summary_with_source_revisions() -> None:
    payloads = InMemoryTurnPayloadStore()
    turn_id = "turn-0123456789abcdef0123456789abcdef"
    capability_ref = payloads.put(turn_id, "capability", {"schema_version": "1.0.0"})
    entries = []
    for index in (1, 2):
        payload_ref = payloads.put(turn_id, f"memory-{index}", {
            "schema_version": "1.0.0", "kind": "memory_r1",
            "project_id": "project-alpha", "object_id": f"atom-{index}",
            "revision": str(index), "trust_status": "trusted",
            "markdown": (f"atom {index} evidence. " + "detail " * 160),
        })
        entries.append(ContextEntry(
            entry_id=f"memory-r1-{index}", kind="memory_r1",
            source_ref=f"crp://memory/project-alpha/atom-{index}", payload_ref=payload_ref,
            source_project_id="project-alpha", revision_identity=str(index),
            content_fingerprint=None, provenance_refs=(f"crp://sources/project-alpha/{index}",),
            disclosure="model", selection_reason="published_memory", content_bytes=1300,
        ))
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}", turn_id=turn_id, resolver_id="fixture",
        project_id="project-alpha", series_id=None, project_profile_id="profile", project_profile_revision=1,
        boundary_profile_id="boundary", boundary_profile_revision=1, capability_manifest_ref=capability_ref,
        entries=tuple(entries), compactions=(), excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=2600,
    )

    compacted = DeterministicLocalMemoryCompactor(payloads).compact(manifest)

    sources = compacted.entries[:2]
    summary = compacted.entries[-1]
    assert all(entry.disclosure == "audit_only" for entry in sources)
    assert summary.kind == "context_summary" and summary.disclosure == "model"
    assert compacted.selected_context_bytes == summary.content_bytes < manifest.selected_context_bytes
    frozen = payloads.get(summary.payload_ref)
    assert frozen["source_entry_ids"] == ["memory-r1-1", "memory-r1-2"]
    assert frozen["source_revisions"] == [
        {"entry_id": "memory-r1-1", "object_id": "atom-1", "revision": "1"},
        {"entry_id": "memory-r1-2", "object_id": "atom-2", "revision": "2"},
    ]
    assert frozen["provenance_refs"] == ["crp://sources/project-alpha/1", "crp://sources/project-alpha/2"]
    assert DeterministicLocalMemoryCompactor(payloads).compact(manifest) == compacted
    assert context_manifest_from_payload(context_manifest_to_payload(compacted)) == compacted

    with pytest.raises(Exception, match="source binding"):
        DeterministicLocalMemoryCompactor(payloads).compact(
            replace(manifest, turn_id="turn-other")
        )


def test_local_memory_compactor_skips_small_or_non_shrinking_inputs() -> None:
    payloads = InMemoryTurnPayloadStore()
    turn_id = "turn-0123456789abcdef0123456789abcdef"
    capability_ref = payloads.put(turn_id, "capability", {"schema_version": "1.0.0"})
    refs = [payloads.put(turn_id, f"small-{index}", {
        "schema_version": "1.0.0", "kind": "memory_r1", "project_id": "project-alpha",
        "object_id": f"small-{index}", "revision": "1", "trust_status": "trusted", "markdown": "detail " * 40,
    }) for index in (1, 2)]
    entries = tuple(ContextEntry(
        f"small-{index}", "memory_r1", f"crp://memory/project-alpha/small-{index}", refs[index - 1],
        "project-alpha", "1", None, (), "model", "published", 500,
    ) for index in (1, 2))
    small = ContextManifest(
        f"context-manifest-{turn_id}", turn_id, "fixture", "project-alpha", None, "profile", 1,
        "boundary", 1, capability_ref, entries, (), (), 4096, 1000,
    )
    assert DeterministicLocalMemoryCompactor(payloads).compact(small) == small

    class _Aggressive(DeterministicLocalMemoryCompactor):
        soft_threshold_bytes = 1

    non_shrinking = replace(
        small, entries=tuple(replace(entry, content_bytes=20) for entry in entries), selected_context_bytes=40,
    )
    assert _Aggressive(payloads).compact(non_shrinking) == non_shrinking


def test_local_memory_compactor_rejects_existing_summary_lineage_conflicts() -> None:
    payloads = InMemoryTurnPayloadStore()
    turn_id = "turn-0123456789abcdef0123456789abcdef"
    capability_ref = payloads.put(turn_id, "capability", {"schema_version": "1.0.0"})
    entries = []
    for index in (1, 2):
        payload_ref = payloads.put(turn_id, f"conflict-{index}", {
            "schema_version": "1.0.0", "kind": "memory_r1", "project_id": "project-alpha",
            "object_id": f"atom-{index}", "revision": "1", "trust_status": "trusted", "markdown": "evidence " * 200,
        })
        entries.append(ContextEntry(
            f"conflict-{index}", "memory_r1", f"crp://memory/project-alpha/{index}", payload_ref,
            "project-alpha", "1", None, (), "model", "published", 1300,
        ))
    conflict = ContextEntry(
        "context-entry-memory-r1-summary", "context_summary", "crp://memory/project-alpha/context-summary",
        payloads.put(turn_id, "existing-summary", {"summary": "old"}), "project-alpha", "old", None, (),
        "model", "old", 3,
    )
    manifest = ContextManifest(
        f"context-manifest-{turn_id}", turn_id, "fixture", "project-alpha", None, "profile", 1,
        "boundary", 1, capability_ref, (*entries, conflict), (), (), 4096, 2603,
    )
    with pytest.raises(Exception, match="lineage conflicts"):
        DeterministicLocalMemoryCompactor(payloads).compact(manifest)


def test_context_resolver_enables_memory_compaction_when_turn_payloads_are_composed(tmp_path: Path) -> None:
    payloads = InMemoryTurnPayloadStore()
    request = _request()
    request["context_policy"] = {"max_context_bytes": 8192}
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    snapshots = TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    capability = ProjectAwareCapabilityManifestResolver(snapshots).resolve(
        request, (_capability("memory.recall", "read"),)
    )
    selected = []
    for index in (1, 2):
        payload_ref = payloads.put(str(request["turn_id"]), f"atom-{index}", {
            "schema_version": "1.0.0", "kind": "memory_r1", "project_id": "project-alpha",
            "object_id": f"atom-{index}", "revision": str(index), "trust_status": "trusted",
            "markdown": ("verified detail " * 160),
        })
        selected.append({
            "manifest_kind": "memory_r1", "object_id": f"atom-{index}", "revision": index,
            "trust_status": "trusted", "payload_ref": payload_ref, "context_bytes": 2400,
        })

    class _Published:
        def acquire(self, *_args, **_kwargs):
            return PublishedProjectMemorySnapshot(
                payload_ref=f"crp://session/{request['turn_id']}/published-memory/ref",
                revision="published-r1", payload={"selected": selected},
            )

    context = ProjectAwareContextManifestResolver(
        snapshots, published_memory=_Published(), payloads=payloads,  # type: ignore[arg-type]
    ).resolve(
        request, f"crp://session/{request['turn_id']}/capability/ref", capability,
    )

    assert [entry.kind for entry in context.entries if entry.disclosure == "model"] == ["context_summary"]
    assert len(context.compactions) == 1


def test_memory_compaction_is_rebuilt_for_a_new_turn_and_source_revision() -> None:
    payloads = InMemoryTurnPayloadStore()

    def compact(turn_id: str, revision: str):
        capability_ref = payloads.put(turn_id, "capability", {"kind": "fixture"})
        entries = []
        for index in (1, 2):
            markdown = (f"verified revision {revision} detail {index} " * 80).strip()
            payload_ref = payloads.put(turn_id, f"atom-{index}", {
                "schema_version": "1.0.0",
                "kind": "memory_r1",
                "project_id": "project-alpha",
                "object_id": f"atom-{index}",
                "revision": revision,
                "trust_status": "trusted",
                "markdown": markdown,
            })
            entries.append(ContextEntry(
                entry_id=f"memory-{index}",
                kind="memory_r1",
                source_ref=f"crp://memory/project-alpha/{index}",
                payload_ref=payload_ref,
                source_project_id="project-alpha",
                revision_identity=revision,
                content_fingerprint=None,
                provenance_refs=(),
                disclosure="model",
                selection_reason="published",
                content_bytes=len(markdown.encode("utf-8")),
            ))
        manifest = ContextManifest(
            manifest_id=f"context-manifest-{turn_id}",
            turn_id=turn_id,
            resolver_id="fixture",
            project_id="project-alpha",
            series_id=None,
            project_profile_id="profile",
            project_profile_revision=1,
            boundary_profile_id="boundary",
            boundary_profile_revision=1,
            capability_manifest_ref=capability_ref,
            entries=tuple(entries),
            compactions=(),
            excluded_reason_counts=(),
            max_context_bytes=8192,
            selected_context_bytes=sum(item.content_bytes for item in entries),
        )
        compacted = DeterministicLocalMemoryCompactor(payloads).compact(manifest)
        output = next(item for item in compacted.entries if item.kind == "context_summary")
        return output, payloads.get(output.payload_ref)

    first_entry, first_payload = compact(
        "turn-11111111111111111111111111111111", "1",
    )
    second_entry, second_payload = compact(
        "turn-22222222222222222222222222222222", "2",
    )

    assert first_entry.payload_ref != second_entry.payload_ref
    assert first_payload["turn_id"] != second_payload["turn_id"]
    assert first_payload["source_revisions"][0]["revision"] == "1"
    assert second_payload["source_revisions"][0]["revision"] == "2"
    assert first_payload["summary"] != second_payload["summary"]


class _ApplicationSkillStub:
    def __init__(self, snapshot: SimpleNamespace) -> None:
        self._snapshot = snapshot

    def acquire(self, *_: object, **__: object) -> SimpleNamespace:
        return self._snapshot


class _PublishedMemoryStub:
    def __init__(self, turn_id: object) -> None:
        self._turn_id = str(turn_id)
        self.max_context_bytes: int | None = None

    def acquire(self, *_: object, max_context_bytes: int | None = None, **__: object) -> PublishedProjectMemorySnapshot:
        self.max_context_bytes = max_context_bytes
        payload_ref = f"crp://session/{self._turn_id}/published-project-memory-snapshot-v1/ref"
        if max_context_bytes == 0:
            return PublishedProjectMemorySnapshot(
                payload_ref=payload_ref,
                revision="published-memory-empty",
                payload={"selected": []},
            )
        return PublishedProjectMemorySnapshot(
            payload_ref=payload_ref,
            revision="published-memory-r5",
            payload={"selected": [
                {
                    "manifest_kind": "memory_r3", "object_id": "series-alpha", "revision": 2,
                    "trust_status": "trusted",
                    "payload_ref": f"crp://session/{self._turn_id}/published-project-memory-item-v1-3/ref",
                    "context_bytes": 96,
                },
                {
                    "manifest_kind": "memory_r2", "object_id": "scenario-alpha", "revision": 3,
                    "trust_status": "trusted",
                    "payload_ref": f"crp://session/{self._turn_id}/published-project-memory-item-v1-2/ref",
                    "context_bytes": 128,
                },
                {
                    "manifest_kind": "project_skill", "object_id": "skill-alpha", "revision": 4,
                    "trust_status": "trusted",
                    "payload_ref": f"crp://session/{self._turn_id}/published-project-memory-item-v1-0/ref",
                    "context_bytes": 100,
                },
                {
                    "manifest_kind": "memory_r1", "object_id": "atom-alpha", "revision": 9,
                    "trust_status": "trusted",
                    "payload_ref": f"crp://session/{self._turn_id}/published-project-memory-item-v1-1/ref",
                    "context_bytes": 324,
                },
            ]},
        )


def _capability(capability_id: str, mode: str) -> CapabilityDefinition:
    mutating = mode != "read"
    return CapabilityDefinition(
        capability_id,
        1,
        mode,
        mutating,
        "receipt_required" if mutating else "read_only",
        "crp://default/contracts/in.schema.json",
        "crp://default/contracts/out.schema.json",
    )


def _request() -> dict[str, object]:
    return json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )


def _runtime_self_manifest(
    tmp_path: Path,
    *,
    features: tuple[str, ...] = ("isolated-python-artifact",),
) -> dict[str, object]:
    resources = tmp_path / "runtime-self-manifest-resources"
    app_data = tmp_path / "runtime-self-manifest-data"
    resources.mkdir(exist_ok=True)
    app_data.mkdir(exist_ok=True)
    return build_runtime_self_manifest_for_app(
        tmp_path,
        packaged=True,
        resources_root=resources,
        app_data_root=app_data,
        runtime_source="bundled",
        features=features,
    )


def _mcp_capability() -> CapabilityDefinition:
    tool = ToolDefinition(
        "calendar.read", 1, "Read calendar", "Read events from MCP",
        "mcp", "calendar-server", "read", ("calendar_event",), "mcp",
        "crp://default/contracts/in.schema.json",
        "crp://default/contracts/out.schema.json",
        None, "read_only", "parallel", (), "never_retry",
        ToolRetryPolicy(1, 0, ()), None, None, "read_only", "remote",
        ("calendar-server",), ("calendar_event",), 10_000,
        ("calendar.read",), ("mcp_server_enabled",),
        connection_identity=ToolConnectionIdentity(
            "mcp", "calendar-server", "2025-11-25", 1,
            "calendar-local", "personal-calendar", 1, 1, 1,
        ),
    )
    return CapabilityDefinition(
        "calendar.read", 1, "read", False, "read_only",
        tool.input_schema_uri, tool.output_schema_uri, tool,
    )


def _calendar_binding() -> MCPServerSelectionBinding:
    return MCPServerSelectionBinding(
        "calendar-server", "legacy_2025_11_25", 1,
        "calendar-local", "personal-calendar", 1,
    )
