from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillPackageLoader,
    ApplicationSkillSource,
)
from core.effect_log import EffectState
from core.external_extension_runtime.fact_store import artifact_ref, intent_ref, resolution_ref
from core.external_extension_runtime.installation import (
    installation_revision_ref,
)
from core.external_extension_runtime.skill_materializer import (
    ExternalExtensionApplicationSkillMaterializer,
    ExternalExtensionSkillMaterializationError,
)
from core.external_extensions import (
    ArtifactInventory,
    ResolvedSource,
    derive_review_plan,
    inspect_extension,
    parse_install_intent,
)
from core.storage_provider import JsonObjectStore

# The lifecycle fixture already creates immutable intake, review, and installation
# facts.  Reusing it keeps this test focused on the materializer boundary.
from tests.rebuild.test_external_extension_installation import (
    _install_one,
    _record_intake,
    _services,
    _skill_body,
)


def _effect(operation_id: str) -> SimpleNamespace:
    return SimpleNamespace(operation_id=operation_id, recorded_at=1_700_000_000)


def _record_files_intake(
    services,
    *,
    suffix: str,
    files: dict[str, bytes],
) -> tuple[str, str | None, str]:
    """Freeze a multi-file external Skill artifact without changing shared fixtures."""
    install = parse_install_intent(
        "安装技能 https://github.com/example/materializer-fixture",
        intent_id=f"materializer-install-intent-{suffix}",
        project_id="project-001",
        requested_ref="a" * 40,
    )
    services.facts.record_intent(install, command_id=f"materializer-record-intent-{suffix}")
    operation_id = f"materializer-acquire-{suffix}"
    source = ResolvedSource(
        "github_repository",
        "https://github.com/example/materializer-fixture",
        "a" * 40,
        artifact_ref(install.intent_id, operation_id),
        trust_tier="untrusted",
    )
    services.facts.record_resolution_observation(
        operation_id=operation_id,
        intent_reference=intent_ref(install.intent_id),
        source=source,
    )
    services.facts.record_resolution(
        operation_id=operation_id,
        intent_reference=intent_ref(install.intent_id),
        source=source,
    )
    inventory = ArtifactInventory.capture(files)
    services.facts.commit_artifact(operation_id=operation_id, source=source, inventory=inventory)
    inspected = inspect_extension(inventory, source)
    review_plan = derive_review_plan(inspected.manifest)
    intake = services.facts.record_intake(
        operation_id=operation_id,
        resolution_reference=resolution_ref(operation_id),
        manifest=inspected.manifest,
        review_plan=review_plan,
    )
    confirmation_ref = None
    if review_plan.confirmation_ids:
        confirmation_ref = services.store.confirm_review(
            intake,
            confirmation_id=f"materializer-review-confirmation-{suffix}",
            confirmation_ids=review_plan.confirmation_ids,
            actor="local-operator",
            reason="Reviewed the exact materializer fixture artifact.",
        )
    return intake, confirmation_ref, inspected.manifest.extension_id


def _install_files(services, *, suffix: str, files: dict[str, bytes]):
    intake, confirmation_ref, extension_id = _record_files_intake(
        services, suffix=suffix, files=files,
    )
    services.store.install_disabled(
        intake,
        command_id=f"materializer-install-{suffix}",
        expected_state_revision=0,
        review_confirmation_ref=confirmation_ref,
    )
    return services.store.load_revision(
        installation_revision_ref("project-001", extension_id, 1)
    )


def _external_skill(name: str, instruction: str) -> bytes:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Materializer fixture for {name}\n"
        "trigger_boundary: explicit_or_semantic\n"
        "validation: deterministic_fixture\n"
        "maturity: stable\n"
        "---\n"
        f"{instruction}\n"
    ).encode()


def _materializer(services, tmp_path: Path):
    registry_store = JsonObjectStore(tmp_path / "binding-store", legacy_root=tmp_path / "legacy")
    return (
        ExternalExtensionApplicationSkillMaterializer(
            services.facts,
            services.store,
            tmp_path / "managed",
            binding_store=registry_store,
        ),
        registry_store,
    )


def _activate_existing_binding(
    registry: ApplicationSkillBindingRegistry,
    package,
    *,
    project_id: str = "project-001",
) -> None:
    preview = registry.preview_bind(
        package,
        project_id=project_id,
        allowed_consumers=("answer.model-request",),
        priority=500,
        trigger_terms=("fixture",),
    )
    registry.activate(
        package,
        project_id=project_id,
        allowed_consumers=("answer.model-request",),
        priority=500,
        trigger_terms=("fixture",),
        expected_registry_revision=int(preview["registry_revision"]),
        preview_token=str(preview["preview_token"]),
        confirm=True,
        reason="Bind fixture source identity before external activation.",
    )


def _rewrite_binding_provenance(
    registry_store: JsonObjectStore,
    registry: ApplicationSkillBindingRegistry,
    *,
    source_id: str,
    source_kind: str,
) -> None:
    """Model an otherwise valid registry record from another provenance."""
    raw = registry_store.read(registry.collection, registry.registry_id)
    assert raw is not None

    def rewrite(binding: dict[str, object]) -> dict[str, object]:
        return {**binding, "source_id": source_id, "source_kind": source_kind}

    rewritten = {
        **raw,
        "bindings": [rewrite(dict(item)) for item in raw["bindings"]],
        "history": [
            {
                **dict(event),
                "before": [rewrite(dict(item)) for item in event["before"]],
                "after": [rewrite(dict(item)) for item in event["after"]],
            }
            for event in raw["history"]
        ],
    }
    registry_store.write(
        registry.collection,
        registry.registry_id,
        rewritten,
        expected_revision=registry_store.revision(registry.collection, registry.registry_id),
    )


def _source_package(tmp_path: Path, *, source_id: str, source_kind: str, skill_id: str):
    source = tmp_path / f"{source_kind}-skills"
    skill = source / skill_id
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(
        _external_skill(skill_id, f"Use {source_kind} fixture instructions.").replace(
            b"maturity: stable", b"maturity: verified",
        )
    )
    snapshot = ApplicationSkillCatalog().discover([ApplicationSkillSource(source_id, source, source_kind)])
    assert not snapshot.issues
    return snapshot.packages[0]


def test_codex_skill_materializes_without_executing_resources_and_binds(tmp_path: Path) -> None:
    services = _services(tmp_path)
    intake = _record_intake(services, suffix="materializer-1001", body=_skill_body("materializer"))
    _snapshot, revision = _install_one(services, intake)
    registry_store = JsonObjectStore(tmp_path / "binding-store", legacy_root=tmp_path / "legacy")
    materializer = ExternalExtensionApplicationSkillMaterializer(
        services.facts,
        services.store,
        tmp_path / "managed",
        binding_store=registry_store,
    )
    health = services.store.build_lifecycle_intent(revision.revision_ref, action="health", intent_id="health-materializer-1001")

    assert materializer.probe(health, _effect("effect-materializer-1001")).state is EffectState.PLANNED
    outcome = materializer.execute(health, _effect("effect-materializer-1001"))
    assert outcome.passed is True
    assert outcome.observed_checks == health.health_checks

    package = tmp_path / "managed" / "project-001" / "fixture-skill" / "revision-1" / "fixture-skill"
    assert ApplicationSkillPackageLoader().load_instructions(
        __import__("core.application_skill", fromlist=["ApplicationSkillCatalog"]).ApplicationSkillCatalog().inspect_package(package)
    ).markdown == "Use the fixture for materializer.\n"
    assert materializer.probe(health, _effect("effect-materializer-1001")).state is EffectState.SETTLED_OK

    activation = replace(health, action="activation", health_checks=())
    materializer.execute(activation, _effect("effect-materializer-1002"))
    status = ApplicationSkillBindingRegistry(registry_store).status()
    assert status["bindings"][0]["project_id"] == "project-001"
    assert status["bindings"][0]["status"] == "active"

    disable = replace(health, action="disable", health_checks=())
    materializer.execute(disable, _effect("effect-materializer-1003"))
    assert ApplicationSkillBindingRegistry(registry_store).status()["bindings"][0]["status"] == "inactive"


@pytest.mark.parametrize(
    ("source_path", "skill_id"),
    (
        ("SKILL.md", "root-skill"),
        (".agents/skills/agents-skill/SKILL.md", "agents-skill"),
        ("skills/skills-skill/SKILL.md", "skills-skill"),
        (".dsh/skills/dsh-flat.md", "dsh-flat"),
        (".dsh/skills/dsh-directory/SKILL.md", "dsh-directory"),
    ),
)
def test_external_skill_layouts_materialize_load_and_bind_as_external(
    tmp_path: Path,
    source_path: str,
    skill_id: str,
) -> None:
    services = _services(tmp_path)
    files = {source_path: _external_skill(skill_id, f"Use {skill_id} instructions.")}
    sentinel = b"raise RuntimeError('external script must never execute')\n"
    if source_path.endswith("/SKILL.md"):
        prefix = source_path.removesuffix("SKILL.md")
        files[f"{prefix}scripts/sentinel.py"] = sentinel
    elif source_path == "SKILL.md":
        files["scripts/sentinel.py"] = sentinel
    revision = _install_files(services, suffix=skill_id, files=files)
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref,
        action="health",
        intent_id=f"health-layout-{skill_id}",
    )

    assert materializer.probe(health, _effect(f"effect-layout-{skill_id}-health")).state is EffectState.PLANNED
    assert materializer.execute(health, _effect(f"effect-layout-{skill_id}-health")).passed is True

    package_root = (
        tmp_path / "managed" / "project-001" / revision.extension_id / "revision-1" / skill_id
    )
    package = ApplicationSkillCatalog().discover_selected(
        [ApplicationSkillSource("external-layout", package_root.parent, "external")],
        [skill_id],
    ).packages[0]
    assert package.source_kind == "external"
    assert ApplicationSkillPackageLoader().load_instructions(package).markdown == (
        f"Use {skill_id} instructions.\n"
    )
    if source_path.endswith("/SKILL.md") or source_path == "SKILL.md":
        assert (package_root / "scripts" / "sentinel.py").read_bytes() == sentinel
        assert not (package_root / "scripts" / "executed.txt").exists()

    activation = replace(health, action="activation", health_checks=())
    materializer.execute(activation, _effect(f"effect-layout-{skill_id}-activation"))
    binding = ApplicationSkillBindingRegistry(registry_store).status()["bindings"]
    assert [(item["skill_id"], item["status"]) for item in binding] == [(skill_id, "active")]


@pytest.mark.parametrize("pre_materialize", (True, False))
def test_managed_skill_byte_tampering_is_unknown_and_execute_fails_closed(
    tmp_path: Path,
    pre_materialize: bool,
) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix=f"tamper-{'pre' if pre_materialize else 'post'}",
        files={"SKILL.md": _external_skill("tamper-skill", "Use pristine instructions.")},
    )
    materializer, _registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref,
        action="health",
        intent_id=f"health-tamper-{'pre' if pre_materialize else 'post'}",
    )
    managed_skill = (
        tmp_path / "managed" / "project-001" / revision.extension_id / "revision-1" / "tamper-skill" / "SKILL.md"
    )
    if pre_materialize:
        managed_skill.parent.mkdir(parents=True)
        managed_skill.write_bytes(b"preexisting unverified bytes\n")
    else:
        materializer.execute(health, _effect("effect-tamper-post-health"))
        managed_skill.write_bytes(b"post-health modified bytes\n")

    assert materializer.probe(health, _effect("effect-tamper-probe")).state is EffectState.UNKNOWN
    with pytest.raises(ExternalExtensionSkillMaterializationError):
        materializer.execute(health, _effect("effect-tamper-execute"))


def test_multi_skill_binding_and_deactivation_are_atomic_batches(tmp_path: Path) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="batch",
        files={
            "SKILL.md": _external_skill("alpha-skill", "Use alpha instructions."),
            "skills/beta-skill/SKILL.md": _external_skill("beta-skill", "Use beta instructions."),
        },
    )
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id="health-batch",
    )
    materializer.execute(health, _effect("effect-batch-health"))
    activation = replace(health, action="activation", health_checks=())
    materializer.execute(activation, _effect("effect-batch-activation"))
    registry = ApplicationSkillBindingRegistry(registry_store)
    active = registry.status()
    # The registry records one event per Skill, but commits the full request batch
    # with one storage revision so a reader cannot observe a partially active set.
    assert active["registry_revision"] == 2
    assert registry_store.revision(registry.collection, registry.registry_id) == 1
    assert [(item["skill_id"], item["status"]) for item in active["bindings"]] == [
        ("alpha-skill", "active"),
        ("beta-skill", "active"),
    ]

    disable = replace(health, action="disable", health_checks=())
    materializer.execute(disable, _effect("effect-batch-disable"))
    inactive = registry.status()
    assert inactive["registry_revision"] == 4
    assert registry_store.revision(registry.collection, registry.registry_id) == 2
    assert [(item["skill_id"], item["status"]) for item in inactive["bindings"]] == [
        ("alpha-skill", "inactive"),
        ("beta-skill", "inactive"),
    ]


def test_uninstall_probe_marks_partial_binding_deactivation_unknown(tmp_path: Path) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="uninstall-partial",
        files={
            "SKILL.md": _external_skill("alpha-skill", "Use alpha instructions."),
            "skills/beta-skill/SKILL.md": _external_skill(
                "beta-skill", "Use beta instructions."
            ),
        },
    )
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id="health-uninstall-partial",
    )
    materializer.execute(health, _effect("effect-uninstall-partial-health"))
    materializer.execute(
        replace(health, action="activation", health_checks=()),
        _effect("effect-uninstall-partial-activation"),
    )
    registry = ApplicationSkillBindingRegistry(registry_store)
    status = registry.status()
    registry.deactivate(
        project_id="project-001",
        skill_id="alpha-skill",
        expected_registry_revision=int(status["registry_revision"]),
        confirm=True,
        reason="Inject a partial uninstall binding state.",
    )
    operation_id = "effect-uninstall-partial-probe"
    with services.records.begin() as uow:
        services.store.plan_uninstall_in_uow(
            revision,
            effect_operation_id=operation_id,
            command_id="command-uninstall-partial-probe",
            expected_state_revision=1,
            uow=uow,
        )
        uow.commit()
    uninstall = replace(health, action="uninstall", health_checks=())

    outcome = materializer.probe(uninstall, _effect(operation_id))

    assert outcome.state is EffectState.UNKNOWN
    assert outcome.evidence_ref == "error:external-extension-uninstall-binding-ambiguous"


def test_multi_skill_activation_drift_leaves_no_partial_binding_state(tmp_path: Path) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="batch-drift",
        files={
            "SKILL.md": _external_skill("alpha-skill", "Use alpha instructions."),
            "skills/beta-skill/SKILL.md": _external_skill("beta-skill", "Use beta instructions."),
        },
    )
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id="health-batch-drift",
    )
    materializer.execute(health, _effect("effect-batch-drift-health"))
    beta_skill = (
        tmp_path / "managed" / "project-001" / revision.extension_id / "revision-1" / "beta-skill" / "SKILL.md"
    )
    beta_skill.write_bytes(b"tampered beta bytes\n")

    with pytest.raises(ExternalExtensionSkillMaterializationError):
        materializer.execute(
            replace(health, action="activation", health_checks=()),
            _effect("effect-batch-drift-activation"),
        )
    registry = ApplicationSkillBindingRegistry(registry_store).status()
    assert registry["registry_revision"] == 0
    assert registry["bindings"] == []


@pytest.mark.parametrize("source_kind", ("bundled", "user", "plugin"))
def test_external_activation_rejects_active_skill_from_another_source_without_registry_write(
    tmp_path: Path, source_kind: str,
) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix=f"identity-{source_kind}",
        files={"SKILL.md": _external_skill("shared-skill", "Use reviewed external instructions.")},
    )
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id=f"health-identity-{source_kind}",
    )
    materializer.execute(health, _effect(f"effect-identity-{source_kind}-health"))
    registry = ApplicationSkillBindingRegistry(registry_store)
    _activate_existing_binding(
        registry,
        _source_package(tmp_path, source_id=f"{source_kind}-fixture", source_kind=source_kind, skill_id="shared-skill"),
    )
    before = registry.status()
    store_revision = registry_store.revision(registry.collection, registry.registry_id)

    with pytest.raises(ExternalExtensionSkillMaterializationError, match="another source"):
        materializer.execute(
            replace(health, action="activation", health_checks=()),
            _effect(f"effect-identity-{source_kind}-activation"),
        )

    assert registry.status() == before
    assert registry_store.revision(registry.collection, registry.registry_id) == store_revision


def test_external_activation_collision_rejects_whole_multi_skill_batch_without_registry_write(tmp_path: Path) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="identity-batch",
        files={
            "SKILL.md": _external_skill("batch-alpha", "Use alpha external instructions."),
            "skills/batch-beta/SKILL.md": _external_skill("batch-beta", "Use beta external instructions."),
        },
    )
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id="health-identity-batch",
    )
    materializer.execute(health, _effect("effect-identity-batch-health"))
    registry = ApplicationSkillBindingRegistry(registry_store)
    _activate_existing_binding(
        registry,
        _source_package(tmp_path, source_id="bundled-fixture", source_kind="bundled", skill_id="batch-beta"),
    )
    before = registry.status()
    store_revision = registry_store.revision(registry.collection, registry.registry_id)

    with pytest.raises(ExternalExtensionSkillMaterializationError, match="another source"):
        materializer.execute(
            replace(health, action="activation", health_checks=()),
            _effect("effect-identity-batch-activation"),
        )

    assert registry.status() == before
    assert registry_store.revision(registry.collection, registry.registry_id) == store_revision


def test_external_activation_rejects_legacy_unknown_binding_provenance(tmp_path: Path) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="identity-legacy",
        files={"SKILL.md": _external_skill("legacy-skill", "Use reviewed external instructions.")},
    )
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id="health-identity-legacy",
    )
    materializer.execute(health, _effect("effect-identity-legacy-health"))
    registry = ApplicationSkillBindingRegistry(registry_store)
    _activate_existing_binding(
        registry,
        _source_package(tmp_path, source_id="user-fixture", source_kind="user", skill_id="legacy-skill"),
    )
    raw = registry_store.read(registry.collection, registry.registry_id)
    assert raw is not None

    def legacy(binding: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in binding.items() if key not in {"source_id", "source_kind"}}

    legacy_record = {
        **raw,
        "bindings": [legacy(dict(item)) for item in raw["bindings"]],
        "history": [
            {
                **dict(event),
                "before": [legacy(dict(item)) for item in event["before"]],
                "after": [legacy(dict(item)) for item in event["after"]],
            }
            for event in raw["history"]
        ],
    }
    registry_store.write(
        registry.collection, registry.registry_id, legacy_record,
        expected_revision=registry_store.revision(registry.collection, registry.registry_id),
    )
    before = ApplicationSkillBindingRegistry(registry_store).status()
    store_revision = registry_store.revision(registry.collection, registry.registry_id)

    with pytest.raises(ExternalExtensionSkillMaterializationError, match="another source"):
        materializer.execute(
            replace(health, action="activation", health_checks=()),
            _effect("effect-identity-legacy-activation"),
        )

    assert ApplicationSkillBindingRegistry(registry_store).status() == before
    assert registry_store.revision(registry.collection, registry.registry_id) == store_revision


def test_same_external_revision_activation_replay_preserves_registry_history(tmp_path: Path) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="identity-replay",
        files={"SKILL.md": _external_skill("replay-skill", "Use replay external instructions.")},
    )
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id="health-identity-replay",
    )
    materializer.execute(health, _effect("effect-identity-replay-health"))
    activation = replace(health, action="activation", health_checks=())
    materializer.execute(activation, _effect("effect-identity-replay-first"))
    registry = ApplicationSkillBindingRegistry(registry_store)
    before = registry.status()
    store_revision = registry_store.revision(registry.collection, registry.registry_id)

    materializer.execute(activation, _effect("effect-identity-replay-second"))

    assert registry.status() == before
    assert registry_store.revision(registry.collection, registry.registry_id) == store_revision


def test_different_external_provenance_with_same_fingerprint_and_policy_never_settles_disables_or_exposes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="provenance-drift",
        files={"SKILL.md": _external_skill("provenance-skill", "Use provenance instructions.")},
    )
    materializer, registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id="health-provenance-drift",
    )
    activation = replace(health, action="activation", health_checks=())
    disable = replace(health, action="disable", health_checks=())
    materializer.execute(health, _effect("effect-provenance-health"))
    materializer.execute(activation, _effect("effect-provenance-activation"))
    registry = ApplicationSkillBindingRegistry(registry_store)
    before = registry.status()
    original = before["bindings"][0]
    _rewrite_binding_provenance(
        registry_store,
        registry,
        source_id="external-other-reviewed-revision",
        source_kind="external",
    )
    drifted = registry.status()
    assert drifted["bindings"][0]["skill_fingerprint"] == original["skill_fingerprint"]
    assert drifted["bindings"][0]["allowed_consumers"] == original["allowed_consumers"]
    assert drifted["bindings"][0]["priority"] == original["priority"]
    assert drifted["bindings"][0]["trigger_terms"] == original["trigger_terms"]

    assert materializer.probe(activation, _effect("effect-provenance-probe-activation")).state is EffectState.UNKNOWN
    assert materializer.probe(disable, _effect("effect-provenance-probe-disable")).state is EffectState.UNKNOWN
    store_revision = registry_store.revision(registry.collection, registry.registry_id)
    with pytest.raises(ExternalExtensionSkillMaterializationError, match="provenance drifted"):
        materializer.execute(disable, _effect("effect-provenance-disable"))
    assert registry.status() == drifted
    assert registry_store.revision(registry.collection, registry.registry_id) == store_revision

    monkeypatch.setattr(
        services.store,
        "active_revisions",
        lambda *, root_id: (revision,) if root_id == "project-001" else (),
    )
    assert materializer.active_sources("project-001") == ()
    with pytest.raises(ExternalExtensionSkillMaterializationError, match="provenance drifted"):
        materializer.active_packages("project-001")


def test_external_source_id_stays_within_binding_contract_at_high_revision() -> None:
    source_id = ExternalExtensionApplicationSkillMaterializer._source_id(
        "project-001",
        "x" * 128,
        10**19,
    )

    assert len(source_id) <= 64
    assert source_id.startswith(f"external-{'x' * 17}-{10**19}-")

    extreme_revision = 10**100
    fallback_source_id = ExternalExtensionApplicationSkillMaterializer._source_id(
        "project-001",
        "x" * 128,
        extreme_revision,
    )
    assert len(fallback_source_id) <= 64
    assert fallback_source_id.startswith("external-")
    assert str(extreme_revision) not in fallback_source_id


def test_materialized_skill_symlink_is_rejected_or_skipped_without_privilege(tmp_path: Path) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="symlink",
        files={"SKILL.md": _external_skill("symlink-skill", "Use symlink instructions.")},
    )
    materializer, _registry_store = _materializer(services, tmp_path)
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id="health-symlink",
    )
    materializer.execute(health, _effect("effect-symlink-health"))
    managed_skill = (
        tmp_path / "managed" / "project-001" / revision.extension_id / "revision-1" / "symlink-skill" / "SKILL.md"
    )
    target = tmp_path / "outside-skill.md"
    target.write_bytes(_external_skill("symlink-skill", "Outside instructions."))
    managed_skill.unlink()
    try:
        os.symlink(target, managed_skill)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    assert materializer.probe(health, _effect("effect-symlink-probe")).state is EffectState.UNKNOWN
    with pytest.raises(ExternalExtensionSkillMaterializationError):
        materializer.execute(health, _effect("effect-symlink-execute"))


def _activate_for_active_packages(services, materializer, revision, *, suffix: str) -> None:
    health = services.store.build_lifecycle_intent(
        revision.revision_ref, action="health", intent_id=f"active-packages-health-{suffix}",
    )
    materializer.execute(health, _effect(f"active-packages-health-effect-{suffix}"))
    materializer.execute(
        replace(health, action="activation", health_checks=()),
        _effect(f"active-packages-activation-effect-{suffix}"),
    )


def test_active_packages_freezes_verified_bytes_after_exact_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="active-frozen",
        files={"SKILL.md": _external_skill("active-frozen", "Use frozen active instructions.")},
    )
    materializer, _registry_store = _materializer(services, tmp_path)
    _activate_for_active_packages(services, materializer, revision, suffix="frozen")
    monkeypatch.setattr(
        services.store, "active_revisions", lambda *, root_id: (revision,) if root_id == "project-001" else (),
    )
    # A path-backed catalog would raise here.  The materializer must use its
    # handle/exact-read map and package_from_verified_content instead.
    monkeypatch.setattr(
        ApplicationSkillCatalog,
        "discover_selected",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("path catalog reopen")),
    )

    packages = materializer.active_packages("project-001")
    skill_file = (
        tmp_path / "managed" / "project-001" / revision.extension_id / "revision-1"
        / "active-frozen" / "SKILL.md"
    )
    skill_file.unlink()

    assert [(package.skill_id, package.source_kind) for package in packages] == [
        ("active-frozen", "external"),
    ]
    assert packages[0].verified_content is not None
    assert ApplicationSkillPackageLoader().load_instructions(packages[0]).markdown == (
        "Use frozen active instructions.\n"
    )


def test_active_packages_returns_all_skills_and_detects_later_disk_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(tmp_path)
    revision = _install_files(
        services,
        suffix="active-multiple",
        files={
            "SKILL.md": _external_skill("active-alpha", "Use alpha active instructions."),
            "skills/active-beta/SKILL.md": _external_skill("active-beta", "Use beta active instructions."),
        },
    )
    materializer, _registry_store = _materializer(services, tmp_path)
    _activate_for_active_packages(services, materializer, revision, suffix="multiple")
    monkeypatch.setattr(
        services.store, "active_revisions", lambda *, root_id: (revision,) if root_id == "project-001" else (),
    )

    packages = materializer.active_packages("project-001")

    assert [package.skill_id for package in packages] == ["active-alpha", "active-beta"]
    assert len({package.source_id for package in packages}) == 1
    beta = (
        tmp_path / "managed" / "project-001" / revision.extension_id / "revision-1"
        / "active-beta" / "SKILL.md"
    )
    beta.write_bytes(b"tampered after active package snapshot\n")

    with pytest.raises(ExternalExtensionSkillMaterializationError, match="materialized external Skill"):
        materializer.active_packages("project-001")
