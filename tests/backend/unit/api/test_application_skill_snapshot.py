from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from backend.api.ai_runtime import build_ai_runtime
from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver,
    ProjectAwareContextManifestResolver,
    TurnProjectProfileSnapshotAuthority,
)
from backend.api.application_skill_snapshot import (
    TurnApplicationSkillSnapshotAuthority,
    TurnApplicationSkillSnapshotError,
    reviewed_external_application_skill_ids,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.workbench_ai_runtime import (
    WorkbenchQuestionPlanner,
    _LOCAL_ABSOLUTE_PATH,
    _application_skill_context,
    _validated_model_answer,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import (
    CapabilityManifestError,
    CapabilityDefinition,
    CapabilityManifest,
    ContextEntry,
    ContextManifest,
    InMemoryTurnPayloadStore,
    context_manifest_to_payload,
    manifest_from_payload,
    manifest_to_payload,
)
from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillResolver,
    ApplicationSkillSource,
    ObjectStoreApplicationSkillTraceRepository,
)
from core.model_gateway import ModelResult
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[4]
PROJECT_ID = "project-alpha"


class _RecordingCatalog(ApplicationSkillCatalog):
    def __init__(self) -> None:
        self.discover_calls = 0
        super().__init__()

    def discover(self, sources):  # type: ignore[no-untyped-def]
        self.discover_calls += 1
        return super().discover(sources)

    def discover_selected(self, sources, skill_ids):  # type: ignore[no-untyped-def]
        self.discover_calls += 1
        return super().discover_selected(sources, skill_ids)


def test_reviewed_external_skill_ids_reuse_immutable_package_gate(
    tmp_path: Path,
) -> None:
    source = _temporary_skill_source(tmp_path, "reviewed-external", "REVIEWED")
    package_root = source.root / "reviewed-external"
    catalog = ApplicationSkillCatalog()
    external = catalog.package_from_verified_content(
        {"SKILL.md": (package_root / "SKILL.md").read_bytes()},
        source_id="external-reviewed",
        source_kind="external",
        package_root=package_root,
    )

    assert reviewed_external_application_skill_ids((external,)) == (
        "reviewed-external",
    )
    with pytest.raises(TurnApplicationSkillSnapshotError):
        reviewed_external_application_skill_ids([external])
    with pytest.raises(TurnApplicationSkillSnapshotError):
        reviewed_external_application_skill_ids((replace(external, source_kind="user"),))


def test_empty_enabled_skills_freeze_an_empty_snapshot_without_catalog_scan(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "SELECTED-BODY")
    catalog = _RecordingCatalog()
    authority, payloads = _authority(tmp_path, catalog=catalog, source=source)

    snapshot = authority.acquire(
        _request(),
        project_id=PROJECT_ID,
        profile_id="project-capability-project-alpha",
        profile_revision=1,
        enabled_skill_ids=(),
    )

    assert snapshot is not None
    assert catalog.discover_calls == 0
    assert snapshot.payload["selected"] == []
    assert snapshot.payload["excluded"] == []
    assert payloads.get(snapshot.payload_ref) == snapshot.payload


def test_agent_child_skill_selection_requires_verified_binding(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "SELECTED-BODY")
    authority, _ = _authority(tmp_path, catalog=_RecordingCatalog(), source=source)

    with pytest.raises(TurnApplicationSkillSnapshotError, match="verified agent binding"):
        authority.acquire(
            _child_skill_request(("selected-review",)),
            project_id=PROJECT_ID,
            profile_id="project-capability-project-alpha",
            profile_revision=1,
            enabled_skill_ids=("selected-review", "other-review"),
        )


def test_agent_child_without_expert_skill_ids_keeps_skill_snapshot_disabled(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "SELECTED-BODY")
    authority, _ = _authority(tmp_path, catalog=_RecordingCatalog(), source=source)
    request = _child_skill_request(("selected-review",))
    request["expert_request"].pop("skill_ids")  # type: ignore[index]

    assert authority.acquire(
        request,
        project_id=PROJECT_ID,
        profile_id="project-capability-project-alpha",
        profile_revision=1,
        enabled_skill_ids=("selected-review",),
    ) is None


def test_agent_child_skill_selection_rejects_project_capability_overreach(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "SELECTED-BODY")
    authority, _ = _authority(
        tmp_path, catalog=_RecordingCatalog(), source=source,
        agent_binding_verifier=lambda request, binding: binding,
    )

    with pytest.raises(TurnApplicationSkillSnapshotError, match="project-enabled subset"):
        authority.acquire(
            _child_skill_request(("outside-review",)),
            project_id=PROJECT_ID,
            profile_id="project-capability-project-alpha",
            profile_revision=1,
            enabled_skill_ids=("selected-review", "other-review"),
        )


def test_agent_child_skill_selection_fails_closed_when_requested_skill_is_unselected(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "SELECTED-BODY")
    authority, _ = _authority(
        tmp_path, catalog=_RecordingCatalog(), source=source,
        agent_binding_verifier=lambda request, binding: binding,
    )

    with pytest.raises(TurnApplicationSkillSnapshotError, match="not fully selected"):
        authority.acquire(
            _child_skill_request(("missing-review",)),
            project_id=PROJECT_ID,
            profile_id="project-capability-project-alpha",
            profile_revision=1,
            enabled_skill_ids=("selected-review", "missing-review"),
        )


def test_agent_child_skill_selection_freezes_and_replays_verified_subset(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "SELECTED-BODY")
    catalog = _RecordingCatalog()
    calls: list[tuple[object, object]] = []

    def verify(request, binding):  # type: ignore[no-untyped-def]
        calls.append((request, binding))
        return binding

    authority, _ = _authority(
        tmp_path, catalog=catalog, source=source, agent_binding_verifier=verify,
    )
    _activate(tmp_path, catalog, source, "selected-review", consumer="turn.agent-child")
    request = _child_skill_request(("selected-review",))
    first = authority.acquire(
        request,
        project_id=PROJECT_ID,
        profile_id="project-capability-project-alpha",
        profile_revision=1,
        enabled_skill_ids=("selected-review",),
    )
    replay = authority.acquire(
        request,
        project_id=PROJECT_ID,
        profile_id="project-capability-project-alpha",
        profile_revision=1,
        enabled_skill_ids=("selected-review",),
    )

    assert first is not None and replay == first
    assert first.payload["consumer"] == "turn.agent-child"
    assert first.payload["task_kind"] == "agent.child.execute"
    assert [item["skill_id"] for item in first.payload["selected"]] == ["selected-review"]
    assert len(calls) == 2


def test_enabled_active_catalog_intersection_is_shared_by_both_manifests(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "SELECTED-INSTRUCTION")
    _temporary_skill_source(tmp_path, "unselected-review", "UNSELECTED-INSTRUCTION")
    catalog = _RecordingCatalog()
    authority, payloads = _authority(tmp_path, catalog=catalog, source=source)
    _activate(tmp_path, catalog, source, "selected-review")
    _activate(tmp_path, catalog, source, "unselected-review", consumer="document.generate")
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    boundary_store.update(PROJECT_ID, mode="guarded", remote_default="review", expected_revision=0)
    capability_store.update(
        PROJECT_ID,
        expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=1,
        enabled_skill_ids=("missing-review", "selected-review", "unselected-review"),
    )
    snapshots = TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    request = _request()
    discover_before = catalog.discover_calls
    capability = ProjectAwareCapabilityManifestResolver(
        snapshots, application_skills=authority,
    ).resolve(request, (_capability(),))
    context = ProjectAwareContextManifestResolver(
        snapshots, application_skills=authority,
    ).resolve(request, f"crp://session/{request['turn_id']}/capability-manifest/ref", capability)

    assert catalog.discover_calls == discover_before + 1
    assert capability.application_skill_snapshot_ref is not None
    assert capability.application_skill_snapshot_revision is not None
    snapshot = payloads.get(capability.application_skill_snapshot_ref)
    assert snapshot["selected"] and [item["skill_id"] for item in snapshot["selected"]] == ["selected-review"]
    assert snapshot["excluded"] == [
        {"skill_id": "missing-review", "reason": "missing_binding"},
        {"skill_id": "unselected-review", "reason": "inactive_binding"},
    ]
    snapshot_entry = next(item for item in context.entries if item.kind == "application_skill_snapshot")
    instruction_entry = next(item for item in context.entries if item.kind == "application_skill")
    assert snapshot_entry.payload_ref == capability.application_skill_snapshot_ref
    assert snapshot_entry.revision_identity == capability.application_skill_snapshot_revision
    assert instruction_entry.disclosure == "model"
    assert [item.payload_ref for item in context.entries if item.kind == "application_skill" and item.disclosure == "model"] == [instruction_entry.payload_ref]
    assert payloads.get(str(instruction_entry.payload_ref))["markdown"].endswith("SELECTED-INSTRUCTION\n")


def test_plugin_skill_requires_source_plugin_and_owner_selection(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "PLUGIN-INSTRUCTION")
    source = ApplicationSkillSource("method-plugin", source.root, "plugin")
    catalog = ApplicationSkillCatalog()
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    bindings = ApplicationSkillBindingRegistry(store, now="2026-08-24T10:00:00+00:00")
    payloads = InMemoryTurnPayloadStore()
    package = catalog.discover((source,)).get("selected-review")
    assert package is not None
    preview = bindings.preview_bind(
        package, project_id=PROJECT_ID, allowed_consumers=("turn.workbench-question",),
        priority=700, trigger_terms=("architecture",),
    )
    bindings.activate(
        package, project_id=PROJECT_ID, allowed_consumers=("turn.workbench-question",),
        priority=700, trigger_terms=("architecture",),
        expected_registry_revision=preview["registry_revision"], preview_token=preview["preview_token"],
        confirm=True, reason="Bind reviewed Plugin Skill",
    )
    authority = TurnApplicationSkillSnapshotAuthority(
        catalog=catalog, sources=(), plugin_sources=lambda ids: (source,) if "method-plugin" in ids else (),
        bindings=bindings,
        resolver=ApplicationSkillResolver(
            bindings, trace_store=ObjectStoreApplicationSkillTraceRepository(store),
            now="2026-08-24T10:00:00+00:00",
        ),
        payloads=payloads,
    )

    blocked = authority.acquire(
        _request(), project_id=PROJECT_ID, profile_id="profile-a", profile_revision=1,
        enabled_skill_ids=("selected-review",), enabled_sources=("plugin",), enabled_plugin_ids=(),
    )
    assert blocked is not None and blocked.payload["selected"] == []

    allowed_request = _request()
    allowed_request["turn_id"] = "turn-plugin-skill-12345678"
    allowed = authority.acquire(
        allowed_request, project_id=PROJECT_ID, profile_id="profile-a", profile_revision=1,
        enabled_skill_ids=("selected-review",), enabled_sources=("plugin",),
        enabled_plugin_ids=("method-plugin",),
    )
    assert allowed is not None
    assert allowed.payload["selected"][0]["source_kind"] == "plugin"
    instruction = payloads.get(allowed.payload["selected"][0]["instruction_payload_ref"])
    assert instruction["markdown"].endswith("PLUGIN-INSTRUCTION\n")


def test_external_skill_source_is_project_scoped_and_loaded_into_turn_snapshot(
    tmp_path: Path,
) -> None:
    generated = _temporary_skill_source(
        tmp_path, "selected-review", "EXTERNAL-INSTRUCTION",
    )
    source = ApplicationSkillSource("external-revision", generated.root, "external")
    catalog = ApplicationSkillCatalog()
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    bindings = ApplicationSkillBindingRegistry(
        store, now="2026-08-30T10:00:00+00:00",
    )
    package = catalog.discover((source,)).get("selected-review")
    assert package is not None
    preview = bindings.preview_bind(
        package,
        project_id=PROJECT_ID,
        allowed_consumers=("turn.workbench-question",),
        priority=500,
    )
    bindings.activate(
        package,
        project_id=PROJECT_ID,
        allowed_consumers=("turn.workbench-question",),
        priority=500,
        trigger_terms=(),
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason="Bind a reviewed external extension Skill.",
    )
    payloads = InMemoryTurnPayloadStore()
    authority = TurnApplicationSkillSnapshotAuthority(
        catalog=catalog,
        sources=(),
        external_sources=lambda project_id: (source,) if project_id == PROJECT_ID else (),
        bindings=bindings,
        resolver=ApplicationSkillResolver(
            bindings,
            trace_store=ObjectStoreApplicationSkillTraceRepository(store),
            now="2026-08-30T10:00:00+00:00",
        ),
        payloads=payloads,
    )

    snapshot = authority.acquire(
        _request(),
        project_id=PROJECT_ID,
        profile_id="profile-external",
        profile_revision=1,
        enabled_skill_ids=("selected-review",),
    )

    assert snapshot is not None
    assert snapshot.payload["selected"][0]["source_kind"] == "external"
    instruction = payloads.get(
        snapshot.payload["selected"][0]["instruction_payload_ref"]
    )
    assert instruction["markdown"].endswith("EXTERNAL-INSTRUCTION\n")


def test_production_ai_runtime_freezes_composed_external_skill_into_turn(
    tmp_path: Path,
) -> None:
    generated = _temporary_skill_source(
        tmp_path, "external-turn-skill", "PRODUCTION-EXTERNAL-INSTRUCTION",
    )
    catalog = ApplicationSkillCatalog()
    store, _settings = build_rebuild_object_store(tmp_path)
    bindings = ApplicationSkillBindingRegistry(
        store, now="2026-08-30T10:00:00+00:00",
    )
    package_root = generated.root / "external-turn-skill"
    package = catalog.package_from_verified_content(
        {"SKILL.md": (package_root / "SKILL.md").read_bytes()},
        source_id="external-active-revision",
        source_kind="external",
        package_root=package_root,
    )
    preview = bindings.preview_bind(
        package,
        project_id=PROJECT_ID,
        allowed_consumers=("turn.workbench-question",),
        priority=500,
    )
    bindings.activate(
        package,
        project_id=PROJECT_ID,
        allowed_consumers=("turn.workbench-question",),
        priority=500,
        trigger_terms=(),
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason="Bind a reviewed external extension Skill for a production Turn.",
    )
    ProjectBoundaryProfileStore(tmp_path).update(
        PROJECT_ID,
        mode="guarded",
        remote_default="review",
        expected_revision=0,
    )
    ProjectCapabilityProfileStore(tmp_path).update(
        PROJECT_ID,
        expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha",
        boundary_profile_revision=1,
        enabled_skill_ids=(),
    )
    package_reads: list[str] = []

    def active_packages(project_id: str):  # type: ignore[no-untyped-def]
        package_reads.append(project_id)
        return (package,) if project_id == PROJECT_ID else ()

    def forbidden_active_sources(_project_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError("production AI runtime must not reopen external Skill paths")

    application = SimpleNamespace(state=SimpleNamespace(
        external_extension_runtime=SimpleNamespace(
            active_packages=active_packages,
            active_sources=forbidden_active_sources,
        ),
    ))
    shutil.rmtree(package_root)
    runtime = build_ai_runtime(
        SimpleNamespace(root_dir=tmp_path), application=application,
    )
    request = _request()

    runtime.submit_turn(request)

    frozen = runtime._payloads.get_immutable_payload(
        request["turn_id"], "application-skill-snapshot-v1",
    )
    assert frozen is not None
    snapshot = frozen[1]
    assert package_reads == [PROJECT_ID]
    assert snapshot["selected"][0]["skill_id"] == "external-turn-skill"
    assert snapshot["selected"][0]["source_kind"] == "external"
    instruction = runtime._payloads.get(
        snapshot["selected"][0]["instruction_payload_ref"]
    )
    assert instruction["markdown"].endswith(
        "PRODUCTION-EXTERNAL-INSTRUCTION\n"
    )


def test_external_package_duplicate_with_filesystem_skill_is_fail_closed(
    tmp_path: Path,
) -> None:
    filesystem = _temporary_skill_source(
        tmp_path, "selected-review", "FILESYSTEM-INSTRUCTION",
    )
    external_root = tmp_path / "external-package" / "selected-review"
    external_root.mkdir(parents=True)
    (external_root / "SKILL.md").write_text(
        _skill_markdown("selected-review", "EXTERNAL-INSTRUCTION"), encoding="utf-8",
    )
    catalog = ApplicationSkillCatalog()
    external = catalog.package_from_verified_content(
        {"SKILL.md": (external_root / "SKILL.md").read_bytes()},
        source_id="external-duplicate",
        source_kind="external",
        package_root=external_root,
    )
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    bindings = ApplicationSkillBindingRegistry(
        store, now="2026-08-30T10:00:00+00:00",
    )
    preview = bindings.preview_bind(
        external,
        project_id=PROJECT_ID,
        allowed_consumers=("turn.workbench-question",),
        priority=500,
    )
    bindings.activate(
        external,
        project_id=PROJECT_ID,
        allowed_consumers=("turn.workbench-question",),
        priority=500,
        trigger_terms=(),
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason="Bind duplicate fixture for fail-closed catalog composition.",
    )
    payloads = InMemoryTurnPayloadStore()
    authority = TurnApplicationSkillSnapshotAuthority(
        catalog=catalog,
        sources=(filesystem,),
        external_packages=lambda project_id: (external,) if project_id == PROJECT_ID else (),
        bindings=bindings,
        resolver=ApplicationSkillResolver(
            bindings,
            trace_store=ObjectStoreApplicationSkillTraceRepository(store),
            now="2026-08-30T10:00:00+00:00",
        ),
        payloads=payloads,
    )

    snapshot = authority.acquire(
        _request(),
        project_id=PROJECT_ID,
        profile_id="profile-duplicate",
        profile_revision=1,
        enabled_skill_ids=("selected-review",),
    )

    assert snapshot is not None
    assert snapshot.payload["selected"] == []
    assert snapshot.payload["excluded"] == [
        {"skill_id": "selected-review", "reason": "missing_package"},
    ]


@pytest.mark.parametrize("kind", ("filesystem", "non-external", "non-package"))
def test_turn_external_package_callback_rejects_nonimmutable_values_before_path_access(
    tmp_path: Path,
    kind: str,
) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "EXTERNAL-INSTRUCTION")
    catalog = ApplicationSkillCatalog()
    package_root = source.root / "selected-review"
    verified = catalog.package_from_verified_content(
        {"SKILL.md": (package_root / "SKILL.md").read_bytes()},
        source_id="external-verified",
        source_kind="external",
        package_root=package_root,
    )
    if kind == "filesystem":
        candidate: object = catalog.inspect_package(package_root, source_id="external-path")
    elif kind == "non-external":
        candidate = replace(verified, source_kind="user")
    else:
        candidate = object()
    shutil.rmtree(source.root)
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    authority = TurnApplicationSkillSnapshotAuthority(
        catalog=catalog,
        sources=(),
        external_packages=lambda _: (candidate,),  # type: ignore[return-value]
        bindings=ApplicationSkillBindingRegistry(store),
        resolver=ApplicationSkillResolver(
            ApplicationSkillBindingRegistry(store),
            trace_store=ObjectStoreApplicationSkillTraceRepository(store),
        ),
        payloads=InMemoryTurnPayloadStore(),
    )

    with pytest.raises(TurnApplicationSkillSnapshotError, match="external Application Skill package"):
        authority.acquire(
            _request(),
            project_id=PROJECT_ID,
            profile_id="profile-invalid-external",
            profile_revision=1,
            enabled_skill_ids=("selected-review",),
        )


def test_reviewed_plugin_skill_cannot_fall_back_to_same_named_user_skill(tmp_path: Path) -> None:
    user_source = _temporary_skill_source(tmp_path, "selected-review", "USER-INSTRUCTION")
    catalog = ApplicationSkillCatalog()
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    bindings = ApplicationSkillBindingRegistry(store, now="2026-08-24T10:00:00+00:00")
    package = catalog.discover((user_source,)).get("selected-review")
    assert package is not None
    preview = bindings.preview_bind(
        package, project_id=PROJECT_ID, allowed_consumers=("turn.workbench-question",),
        priority=700, trigger_terms=("architecture",),
    )
    bindings.activate(
        package, project_id=PROJECT_ID, allowed_consumers=("turn.workbench-question",),
        priority=700, trigger_terms=("architecture",),
        expected_registry_revision=preview["registry_revision"], preview_token=preview["preview_token"],
        confirm=True, reason="Bind baseline Skill",
    )
    authority = TurnApplicationSkillSnapshotAuthority(
        catalog=catalog,
        sources=(user_source,),
        plugin_sources=lambda _ids: (),
        plugin_claimed_skill_ids=lambda _ids: ("selected-review",),
        bindings=bindings,
        resolver=ApplicationSkillResolver(
            bindings, trace_store=ObjectStoreApplicationSkillTraceRepository(store),
            now="2026-08-24T10:00:00+00:00",
        ),
        payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["turn_id"] = "turn-plugin-no-fallback-1234"
    snapshot = authority.acquire(
        request, project_id=PROJECT_ID, profile_id="profile-a", profile_revision=1,
        enabled_skill_ids=("selected-review",), enabled_sources=("plugin",),
        enabled_plugin_ids=("method-plugin",),
    )
    assert snapshot is not None
    assert snapshot.payload["selected"] == []
    assert snapshot.payload["excluded"] == [{"skill_id": "selected-review", "reason": "missing_package"}]

    no_source_request = _request()
    no_source_request["turn_id"] = "turn-plugin-source-disabled-1"
    no_source = authority.acquire(
        no_source_request, project_id=PROJECT_ID, profile_id="profile-a", profile_revision=2,
        enabled_skill_ids=("selected-review",), enabled_sources=("core",),
        enabled_plugin_ids=("method-plugin",),
    )
    assert no_source is not None
    assert no_source.payload["selected"] == []
    assert no_source.payload["excluded"] == [{"skill_id": "selected-review", "reason": "missing_package"}]


def test_existing_turn_reads_immutable_snapshot_without_rescan_or_package_drift(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "ORIGINAL-INSTRUCTION")
    catalog = _RecordingCatalog()
    authority, payloads = _authority(tmp_path, catalog=catalog, source=source)
    _activate(tmp_path, catalog, source, "selected-review")
    request = _request()
    first = authority.acquire(
        request,
        project_id=PROJECT_ID,
        profile_id="project-capability-project-alpha",
        profile_revision=1,
        enabled_skill_ids=("selected-review",),
    )
    assert first is not None
    (source.root / "selected-review" / "SKILL.md").write_text(
        _skill_markdown("selected-review", "CHANGED-INSTRUCTION"), encoding="utf-8"
    )

    replay = authority.acquire(
        request,
        project_id=PROJECT_ID,
        profile_id="project-capability-project-alpha",
        profile_revision=1,
        enabled_skill_ids=("selected-review",),
    )

    assert replay == first
    assert catalog.discover_calls == 2  # one setup discovery and one initial snapshot discovery
    selected = first.payload["selected"][0]
    assert payloads.get(selected["instruction_payload_ref"])["markdown"].endswith("ORIGINAL-INSTRUCTION\n")


def test_existing_snapshot_fails_closed_when_profile_revision_drifts(tmp_path: Path) -> None:
    source = _temporary_skill_source(tmp_path, "selected-review", "PROFILE-BOUND-INSTRUCTION")
    catalog = _RecordingCatalog()
    authority, _ = _authority(tmp_path, catalog=catalog, source=source)
    _activate(tmp_path, catalog, source, "selected-review")
    request = _request()
    authority.acquire(
        request,
        project_id=PROJECT_ID,
        profile_id="project-capability-project-alpha",
        profile_revision=1,
        enabled_skill_ids=("selected-review",),
    )

    with pytest.raises(TurnApplicationSkillSnapshotError, match="authority drifted"):
        authority.acquire(
            request,
            project_id=PROJECT_ID,
            profile_id="project-capability-project-alpha",
            profile_revision=2,
            enabled_skill_ids=("selected-review",),
        )


def test_capability_manifest_round_trips_optional_model_routing_binding() -> None:
    manifest = CapabilityManifest(
        manifest_id="capability-manifest-turn-routing", turn_id="turn-routing",
        resolver_id="test", profile_id="project-capability-project-alpha",
        profile_revision=1, capability_ids=("workbench.question.answer",),
        excluded_reason_counts=(), descriptor_bytes=0,
        boundary_profile_id="project-boundary-project-alpha", boundary_profile_revision=1,
        model_routing_snapshot_ref="crp://session/turn-routing/turn-model-routing-snapshot-v1/ref",
        model_routing_snapshot_revision="routing-revision-1",
    )

    payload = manifest_to_payload(manifest)

    assert manifest_from_payload(payload) == manifest
    assert "model_routing_snapshot_ref" in payload
    assert "application_skill_snapshot_ref" not in payload


def test_capability_manifest_rejects_partial_optional_model_routing_binding() -> None:
    manifest = CapabilityManifest(
        manifest_id="capability-manifest-turn-routing", turn_id="turn-routing",
        resolver_id="test", profile_id="project-capability-project-alpha",
        profile_revision=1, capability_ids=("workbench.question.answer",),
        excluded_reason_counts=(), descriptor_bytes=0,
        boundary_profile_id="project-boundary-project-alpha", boundary_profile_revision=1,
        model_routing_snapshot_ref="crp://session/turn-routing/turn-model-routing-snapshot-v1/ref",
        model_routing_snapshot_revision="routing-revision-1",
    )
    payload = manifest_to_payload(manifest)
    payload.pop("model_routing_snapshot_revision")

    with pytest.raises(CapabilityManifestError, match="shape is invalid"):
        manifest_from_payload(payload)


def test_workbench_planner_sends_only_model_selected_instruction_bodies(monkeypatch) -> None:
    payloads = InMemoryTurnPayloadStore()
    request = _request()
    request["privacy"] = {
        "mode": "remote_allowed", "allow_remote": True, "pii": "possible",
        "consent_refs": ["crp://default/consent/provider-egress-policy"], "retention": "local_durable",
    }
    selected_ref = payloads.put(request["turn_id"], "application-skill-instructions-selected", {
        "schema_version": "1.0.0", "skill_id": "selected-review", "skill_fingerprint": "fingerprint-selected", "markdown": "SELECTED-MODEL-BODY\n",
    })
    hidden_ref = payloads.put(request["turn_id"], "application-skill-instructions-hidden", {
        "schema_version": "1.0.0", "skill_id": "hidden-review", "skill_fingerprint": "fingerprint-hidden", "markdown": "HIDDEN-NONMODEL-BODY\n",
    })
    capability_ref = payloads.put(request["turn_id"], "capability-manifest", manifest_to_payload(CapabilityManifest(
        manifest_id=f"capability-manifest-{request['turn_id']}", turn_id=request["turn_id"],
        resolver_id="test", profile_id="project-capability-project-alpha", profile_revision=1,
        capability_ids=("workbench.question.answer",), excluded_reason_counts=(), descriptor_bytes=0,
        boundary_profile_id="project-boundary-project-alpha", boundary_profile_revision=1,
        application_skill_snapshot_ref=None, application_skill_snapshot_revision=None,
    )))
    snapshot_ref = payloads.put(request["turn_id"], "application-skill-snapshot-v1", {
        "schema_version": "1.0.0", "turn_id": request["turn_id"], "project_id": PROJECT_ID,
        "profile_id": "project-capability-project-alpha", "profile_revision": 1,
        "consumer": "turn.workbench-question", "task_kind": "workbench.question.answer",
        "task_fingerprint": "a" * 64, "snapshot_revision": "skill-resolution-test",
        "catalog_revision": "b" * 64, "binding_registry_revision": 1,
        "selected": [{"skill_id": "selected-review", "source_id": "temporary", "source_kind": "user",
                      "skill_fingerprint": "fingerprint-selected", "binding_id": "binding-selected",
                      "binding_revision": 1, "instruction_bytes": len("SELECTED-MODEL-BODY\n"),
                      "instruction_payload_ref": selected_ref}],
        "excluded": [], "selected_instruction_bytes": len("SELECTED-MODEL-BODY\n"),
        "context_budget_bytes": 4096,
    })
    capability_payload = payloads.get(capability_ref)
    capability_payload["application_skill_snapshot_ref"] = snapshot_ref
    capability_payload["application_skill_snapshot_revision"] = "skill-resolution-test"
    capability_ref = payloads.put(request["turn_id"], "capability-manifest", capability_payload)
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{request['turn_id']}", turn_id=request["turn_id"], resolver_id="test",
        project_id=PROJECT_ID, series_id=None, project_profile_id="project-capability-project-alpha",
        project_profile_revision=1, boundary_profile_id="project-boundary-project-alpha", boundary_profile_revision=1,
        capability_manifest_ref=capability_ref,
        entries=(
            ContextEntry("snapshot", "application_skill_snapshot", None, snapshot_ref, PROJECT_ID, "skill-resolution-test", None, (), "audit_only", "selected", 0),
            ContextEntry("selected", "application_skill", None, selected_ref, PROJECT_ID, "1", "fingerprint-selected", (), "model", "selected", len("SELECTED-MODEL-BODY\n")),
            ContextEntry("hidden", "application_skill", None, hidden_ref, PROJECT_ID, "1", "fingerprint-hidden", (), "audit_only", "excluded", 0),
        ),
        compactions=(), excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=len("SELECTED-MODEL-BODY\n"),
    )
    context_ref = payloads.put(request["turn_id"], "context-manifest", context_manifest_to_payload(manifest))
    presentation_ref = payloads.put(request["turn_id"], "turn-presentation", _presentation())
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        "backend.api.workbench_ai_runtime._model_routing_snapshot",
        lambda *_args, **_kwargs: (
            {"test_only": "frozen-routing-snapshot"},
            "crp://session/turn-test/turn-model-routing-snapshot-v1/test",
            "0" * 64,
        ),
    )

    class Gateway:
        def invoke(self, model_request):  # type: ignore[no-untyped-def]
            captured.append(json.loads(model_request.input))
            return ModelResult({"answer": "模型结论", "citations": ["crp://default/memory/atom-1"]}, "test", "model", {})

    result = WorkbenchQuestionPlanner(Gateway()).plan(
        request,
        (
            {"type": "context.resolved", "data": {"payload_ref": context_ref}},
            {"type": "tool.completed", "data": {"capability_id": "workbench.question.answer", "payload_ref": presentation_ref}},
        ),
        (), payloads,
    )

    assert result["type"] == "complete"
    assert "crp://default/memory/atom-1" in captured[0]["allowed_citations"]
    assert "Copy citations exactly from allowed_citations" in captured[0]["instruction"]
    assert captured[0]["application_skill_context"] == [{
        "skill_id": "selected-review", "skill_fingerprint": "fingerprint-selected", "instructions": "SELECTED-MODEL-BODY\n",
    }]
    assert "SELECTED-MODEL-BODY" in json.dumps(captured[0], ensure_ascii=False)
    assert "HIDDEN-NONMODEL-BODY" not in json.dumps(captured[0], ensure_ascii=False)


@pytest.mark.parametrize("citation", ["atom-1", "crp://default/memory/unprovided", "source:content source-1"])
def test_workbench_model_still_rejects_unlisted_citation(citation) -> None:
    with pytest.raises(ValueError, match="citations are invalid"):
        _validated_model_answer({"answer": "受控答案", "citations": [citation]}, _presentation()["content"])


@pytest.mark.parametrize(
    "value",
    (
        r"Read C:\\Users\\owner\\private.txt",
        r"Use \\server\share\private.txt",
        "Read /home/owner/private.txt",
        "Read /etc/hosts",
        "Read /opt/acme/private.txt",
        "Read file:///Volumes/private/data.txt",
    ),
)
def test_remote_application_skill_context_detects_local_absolute_paths(value: str) -> None:
    assert _LOCAL_ABSOLUTE_PATH.search(value) is not None


def test_workbench_application_skill_egress_fails_closed_on_absolute_path() -> None:
    request = _request()
    payloads = InMemoryTurnPayloadStore()
    markdown = "Read /etc/hosts before answering.\n"
    instruction_ref = payloads.put(request["turn_id"], "application-skill-instructions", {
        "schema_version": "1.0.0", "skill_id": "selected-review",
        "skill_fingerprint": "fingerprint-selected", "markdown": markdown,
    })
    snapshot_ref = payloads.put(request["turn_id"], "application-skill-snapshot-v1", {
        "schema_version": "1.0.0", "turn_id": request["turn_id"], "project_id": PROJECT_ID,
        "profile_id": "project-capability-project-alpha", "profile_revision": 1,
        "consumer": "turn.workbench-question", "task_kind": "workbench.question.answer",
        "task_fingerprint": "a" * 64, "snapshot_revision": "skill-resolution-path",
        "catalog_revision": "b" * 64, "binding_registry_revision": 1,
        "selected": [{"skill_id": "selected-review", "source_id": "temporary", "source_kind": "user",
                      "skill_fingerprint": "fingerprint-selected", "binding_id": "binding-selected",
                      "binding_revision": 1, "instruction_bytes": len(markdown.encode("utf-8")),
                      "instruction_payload_ref": instruction_ref}],
        "excluded": [], "selected_instruction_bytes": len(markdown.encode("utf-8")),
        "context_budget_bytes": 4096,
    })
    capability_ref = payloads.put(request["turn_id"], "capability-manifest", manifest_to_payload(CapabilityManifest(
        f"capability-manifest-{request['turn_id']}", request["turn_id"], "test",
        "project-capability-project-alpha", 1, ("workbench.question.answer",), (), 0,
        "project-boundary-project-alpha", 1, snapshot_ref, "skill-resolution-path",
    )))
    context = ContextManifest(
        f"context-manifest-{request['turn_id']}", request["turn_id"], "test", PROJECT_ID, None,
        "project-capability-project-alpha", 1, "project-boundary-project-alpha", 1, capability_ref,
        (
            ContextEntry("snapshot", "application_skill_snapshot", None, snapshot_ref, PROJECT_ID, "skill-resolution-path", None, (), "audit_only", "selected", 0),
            ContextEntry("skill", "application_skill", None, instruction_ref, PROJECT_ID, "1", "fingerprint-selected", (), "model", "selected", len(markdown.encode("utf-8"))),
        ), (), (), 4096, len(markdown.encode("utf-8")),
    )
    context_ref = payloads.put(request["turn_id"], "context-manifest", context_manifest_to_payload(context))

    with pytest.raises(ValueError, match="local absolute path"):
        _application_skill_context(
            ({"type": "context.resolved", "data": {"payload_ref": context_ref}},),
            payloads,
            desired_outcome="workbench.question.answer",
        )


def _authority(
    tmp_path: Path,
    *,
    catalog: ApplicationSkillCatalog,
    source: ApplicationSkillSource,
    agent_binding_verifier=None,  # type: ignore[no-untyped-def]
):
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    bindings = ApplicationSkillBindingRegistry(store, now="2026-08-24T10:00:00+00:00")
    payloads = InMemoryTurnPayloadStore()
    return TurnApplicationSkillSnapshotAuthority(
        catalog=catalog, sources=(source,), bindings=bindings,
        resolver=ApplicationSkillResolver(
            bindings, trace_store=ObjectStoreApplicationSkillTraceRepository(store), now="2026-08-24T10:00:00+00:00",
        ),
        payloads=payloads, agent_binding_verifier=agent_binding_verifier,
    ), payloads


def _activate(tmp_path: Path, catalog: ApplicationSkillCatalog, source: ApplicationSkillSource, skill_id: str, *, consumer: str = "turn.workbench-question") -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    bindings = ApplicationSkillBindingRegistry(store, now="2026-08-24T10:00:00+00:00")
    package = catalog.discover((source,)).get(skill_id)
    assert package is not None
    preview = bindings.preview_bind(package, project_id=PROJECT_ID, allowed_consumers=(consumer,), priority=700, trigger_terms=("architecture",))
    bindings.activate(
        package, project_id=PROJECT_ID, allowed_consumers=(consumer,), priority=700, trigger_terms=("architecture",),
        expected_registry_revision=preview["registry_revision"], preview_token=preview["preview_token"], confirm=True,
        reason="Bind a temporary test fixture skill.",
    )


def _temporary_skill_source(tmp_path: Path, skill_id: str, marker: str) -> ApplicationSkillSource:
    root = tmp_path / "temporary-test-skills"
    package = root / skill_id
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(_skill_markdown(skill_id, marker), encoding="utf-8")
    return ApplicationSkillSource("temporary", root, "user")


def _skill_markdown(skill_id: str, marker: str) -> str:
    return f"---\nname: {skill_id}\ndescription: Temporary architecture review fixture.\n---\n\n# {marker}\n"


def _capability() -> CapabilityDefinition:
    return CapabilityDefinition("workbench.question.answer", 1, "read", False, "read_only", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json")


def _request() -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request["desired_outcome"] = "workbench.question.answer"
    request["input"]["text"] = "Please perform an architecture review."
    request["capability_policy"] = {"allowed": ["workbench.question.answer"], "denied": [], "require_approval": []}
    return request


def _child_skill_request(skill_ids: tuple[str, ...]) -> dict[str, object]:
    request = _request()
    request["desired_outcome"] = "agent.child.execute"
    request["expert_request"] = {
        "expert_id": "video-research-expert",
        "task_intents": ["research"],
        "budget": "research-2k",
        "skill_ids": list(skill_ids),
    }
    request["agent_binding"] = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "child-run-1",
        "role": "subagent",
        "profile_id": "subagent.explorer",
        "profile_revision": 1,
        "model_tier": "fast",
        "parent_run_id": "main-run-1",
        "link_id": "child-link-1",
        "reservation_id": "reservation-1",
        "spawn_operation_id": "spawn-1",
        "depth": 1,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://session/child-turn/agent-budget/ref",
    }
    return request


def _presentation() -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "kind": "workbench.question.answer", "content": {
            "schema_version": "1.0.0", "status": "answered", "question": "What is the architecture result?",
            "answer": {"status": "evidence_found", "text": "Local evidence"}, "answer_preview": "Local evidence",
            "evidence_items": [{"target_ref": "crp://default/memory/atom-1", "snippet": "Evidence"}], "source_links": [],
            "provider_call_performed": False, "provider_status": "fallback_not_configured", "provider_route": "",
            "privacy": {"mode": "local_only", "source_path_exposed": False, "provider_call_performed": False},
            "project_route": {"status": "explicit", "selected_project_id": PROJECT_ID, "ai_assist": None},
        },
    }
