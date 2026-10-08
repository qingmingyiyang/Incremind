from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.external_extension_runtime_startup import (
    _ExternalExtensionIntakeExecutor,
    ExternalExtensionRuntimeStartupHandles,
    register_external_extension_runtime,
    register_external_extension_recovery,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.effect_log import (
    EFFECT_V2,
    EffectClass,
    EffectState,
    build_effect_runtime,
)
from core.external_extension_runtime.fact_store import (
    ExternalExtensionFactConflict,
    ExternalExtensionFactError,
    ExternalExtensionFactStore,
    artifact_ref,
    intent_ref,
    resolution_ref,
)
from core.external_extension_runtime.gate_authority import ExternalExtensionGateRequest
from core.external_extension_runtime.gate_authority import ExternalExtensionGateAuthority
from core.external_extension_runtime.artifact_evidence import ImmutableQuarantineArtifactStore
from core.external_extension_runtime.installation import ExternalExtensionInstallationStore
from core.external_extension_runtime.lifecycle_commands import ExternalExtensionLifecycleCommandService
from core.external_extension_runtime.terminal_receipts import (
    ExternalExtensionTerminalReceiptStore,
    ExternalExtensionTerminalReceiptVerifier,
)
from core.storage_provider import SQLiteStructuredRecordStore
from core.external_extension_runtime.outbound_fetch import FetchedBytes
from core.external_extensions import (
    ArtifactInventory,
    InstallIntent,
    ResolvedSource,
    SourceSpec,
    derive_review_plan,
    inspect_extension,
)
from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillPackageLoader,
)
from core.external_extension_runtime.skill_materializer import (
    ExternalExtensionSkillMaterializationError,
)
from core.external_extension_runtime.lifecycle import (
    ACQUIRE_EFFECT_KIND,
    RESOLVE_EFFECT_KIND,
    RESOLVE_INTENT_SCHEMA,
    RESOLVE_RECEIPT_KIND,
    RESOLVE_RECEIPT_SCHEMA,
)
from core.external_extension_runtime.terminal_receipts import (
    lifecycle_contract,
)


COMMIT = "a" * 40


@dataclass(frozen=True)
class _TestServices:
    """Test-only white-box components reopened from the same durable store."""

    handles: ExternalExtensionRuntimeStartupHandles
    facts: ExternalExtensionFactStore
    _intake: _ExternalExtensionIntakeExecutor
    installations: ExternalExtensionInstallationStore
    lifecycle_commands: ExternalExtensionLifecycleCommandService
    gate_authority: ExternalExtensionGateAuthority

    def resolve_install(self, *args, **kwargs): return self._intake.resolve_install(*args, **kwargs)
    def acquire_resolved(self, *args, **kwargs): return self._intake.acquire_resolved(*args, **kwargs)
    def active_packages(self, *args, **kwargs): return self.handles.active_packages(*args, **kwargs)
    def active_sources(self, *args, **kwargs): return self.handles.active_sources(*args, **kwargs)


class _Fetcher:
    def __init__(self, archive: bytes) -> None:
        self._archive = archive
        self.calls: list[str] = []

    def fetch(self, url: str, **_kwargs: object) -> FetchedBytes:
        self.calls.append(url)
        return FetchedBytes(url, self._archive, "application/zip", "93.184.216.34")


def _archive() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            f"extensions-{COMMIT}/SKILL.md",
            b"---\nname: demo\ndescription: quarantined fixture\n---\n",
        )
    return stream.getvalue()


def _install(intent_id: str) -> InstallIntent:
    immutable_id = f"install-{intent_id}-1001"
    return InstallIntent(
        intent_id=immutable_id,
        kind_hint="skill",
        source_spec=SourceSpec(
            request_id=immutable_id,
            kind="github_repository",
            locator="https://github.com/example/extensions",
            requested_ref=COMMIT,
        ),
        search_term=None,
        project_id="project-001",
        disposition="AUTO_WITH_NOTICE",
        reason_codes=("quarantine_preview_only",),
    )


def _registered_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = build_effect_runtime(tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="startup-test")
    fetcher = _Fetcher(_archive())
    monkeypatch.setattr(
        "backend.api.external_extension_runtime_startup.production_extension_acquisition_fetcher",
        lambda: fetcher,
    )
    handles = register_external_extension_runtime(tmp_path, runtime)
    return runtime, _whitebox_services(tmp_path, runtime, handles), fetcher


def _whitebox_services(
    tmp_path: Path,
    runtime,
    handles: ExternalExtensionRuntimeStartupHandles,
) -> _TestServices:
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    facts = ExternalExtensionFactStore(
        records,
        ImmutableQuarantineArtifactStore(tmp_path / ".rebuild-data" / "external-extension-quarantine"),
    )
    gate_authority = ExternalExtensionGateAuthority(facts)
    receipts = ExternalExtensionTerminalReceiptStore(records)
    installations = ExternalExtensionInstallationStore(
        records, facts, ExternalExtensionTerminalReceiptVerifier(runtime.log, receipts),
    )
    lifecycle = ExternalExtensionLifecycleCommandService(
        records, installations, receipts, runtime, gate_authority=gate_authority,
    )
    return _TestServices(
        handles, facts, _ExternalExtensionIntakeExecutor(facts, runtime, gate_authority),
        installations, lifecycle, gate_authority,
    )


def _source_authorization(
    handles: ExternalExtensionRuntimeStartupHandles,
    install: InstallIntent,
) -> str:
    proposal_ref = handles.facts.record_proposal(
        install,
        proposal_id=f"source-proposal-{install.intent_id}",
    )
    return handles.facts.confirm_source_proposal(
        proposal_ref,
        confirmation_id=f"source-confirmation-{install.intent_id}",
        confirmation_ids=("approve_initial_network_source",),
        actor="local-operator",
        reason="Approved the exact fixture source for governed intake.",
    )


def _resolve(handles: ExternalExtensionRuntimeStartupHandles, *, intent_id: str, now: int):
    install = _install(intent_id)
    return handles.resolve_install(
        install,
        authorization_ref=_source_authorization(handles, install),
        now=now,
    )


def _acquire(handles: ExternalExtensionRuntimeStartupHandles, resolution_operation_id: str, *, intent_id: str, now: int):
    return handles.acquire_resolved(
        resolution_operation_id=resolution_operation_id,
        authorization_ref=_source_authorization(handles, _install(intent_id)),
        now=now,
    )


def _record_reviewed_skill(
    handles: ExternalExtensionRuntimeStartupHandles,
    *,
    suffix: str,
) -> tuple[object, object, str]:
    install = _install(suffix)
    proposal_ref = handles.facts.record_proposal(
        install, proposal_id=f"install-proposal-{suffix}",
    )
    authorization_ref = handles.facts.confirm_source_proposal(
        proposal_ref,
        confirmation_id=f"source-confirmation-{suffix}",
        confirmation_ids=("approve_initial_network_source",),
        actor="local-operator",
        reason="Approved the exact fixture source for lifecycle execution.",
    )
    operation_id = f"acquire-operation-{suffix}"
    source = ResolvedSource(
        "github_repository",
        install.source_spec.locator,
        COMMIT,
        artifact_ref(install.intent_id, operation_id),
        trust_tier="untrusted",
    )
    handles.facts.record_resolution_observation(
        operation_id=operation_id,
        intent_reference=intent_ref(install.intent_id),
        source=source,
    )
    handles.facts.record_resolution(
        operation_id=operation_id,
        intent_reference=intent_ref(install.intent_id),
        source=source,
    )
    inventory = ArtifactInventory.capture({
        "SKILL.md": (
            "---\n"
            "name: startup-skill\n"
            "description: startup lifecycle fixture\n"
            "trigger_boundary: explicit_or_semantic\n"
            "validation: deterministic_fixture\n"
            "maturity: stable\n"
            "---\n"
            "Use the startup lifecycle fixture.\n"
        ).encode(),
    })
    handles.facts.commit_artifact(
        operation_id=operation_id,
        source=source,
        inventory=inventory,
    )
    inspected = inspect_extension(inventory, source)
    review_plan = derive_review_plan(inspected.manifest)
    intake = handles.facts.record_intake(
        operation_id=operation_id,
        resolution_reference=resolution_ref(operation_id),
        manifest=inspected.manifest,
        review_plan=review_plan,
    )
    confirmation = None
    if review_plan.confirmation_ids:
        confirmation = handles.installations.confirm_review(
            intake,
            confirmation_id=f"review-confirmation-{suffix}",
            confirmation_ids=review_plan.confirmation_ids,
            actor="local-operator",
            reason="Reviewed the exact artifact and frozen review plan.",
        )
    snapshot = handles.installations.install_disabled(
        intake,
        command_id=f"install-disabled-{suffix}",
        expected_state_revision=0,
        review_confirmation_ref=confirmation,
    )
    return (
        snapshot,
        handles.installations.load_revision(snapshot.candidate_revision_ref),
        authorization_ref,
    )


def test_startup_registers_only_v2_queryable_intake_contracts_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_effect_runtime(tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="startup-test")

    monkeypatch.setattr(
        "core.external_extension_runtime.outbound_fetch._windows_timed_getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup must not perform outbound acquisition")
        ),
    )

    register_external_extension_runtime(tmp_path, runtime)


def test_startup_exposes_rederived_intake_boundary_not_raw_execution_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, handles, _fetcher = _registered_runtime(tmp_path, monkeypatch)

    assert isinstance(handles.handles, ExternalExtensionRuntimeStartupHandles)
    assert tuple(handles.handles.__dataclass_fields__) == (
        "install_workflow",
        "active_packages",
        "active_sources",
        "_register_recovery",
    )
    assert (tmp_path / ".rebuild-data" / "external-extension-quarantine").is_dir()
    assert not (tmp_path / ".rebuild-data" / "external-extension-skills").exists()
    assert tuple((tmp_path / ".rebuild-data").glob("*.sqlite3")) == (
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
    )
    assert runtime.handlers.kinds().count(RESOLVE_EFFECT_KIND) == 1
    assert runtime.handlers.kinds().count(ACQUIRE_EFFECT_KIND) == 1
    for action in ("health", "activation", "disable", "rollback"):
        contract = lifecycle_contract(action)
        assert runtime.handlers.kinds().count(contract.effect_kind) == 1
    assert handles.active_packages("project-001") == ()
    assert handles.active_sources("project-001") == ()

    resolved = _resolve(handles, intent_id="registration", now=1)
    resolve = runtime.handlers.resolve(resolved)
    assert (resolve.effect_class, resolve.contract_version) == (EffectClass.QUERYABLE, EFFECT_V2)
    assert (resolve.intent_schema_version, resolve.receipt_kind, resolve.receipt_schema_version) == (
        RESOLVE_INTENT_SCHEMA, RESOLVE_RECEIPT_KIND, RESOLVE_RECEIPT_SCHEMA,
    )
    assert runtime.runner.log is runtime.log
    assert runtime.reaper.log is runtime.log
    assert runtime.recoveries.kinds() == ()
    with pytest.raises(ValueError, match="effect handler already registered"):
        register_external_extension_runtime(tmp_path, runtime)


def test_startup_registers_terminal_projection_on_existing_core_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.effect_log import EffectRecoveryCoordinator

    runtime, handles, _fetcher = _registered_runtime(tmp_path, monkeypatch)
    coordinator = EffectRecoveryCoordinator(runtime)

    register_external_extension_recovery(
        coordinator, handles.handles, limit=7,
    )

    assert coordinator.backfill_kinds() == (
        "external-extension-confirmed-acquire-intents",
        "external-extension-lifecycle-intents",
        "external-extension-resolve-intents",
    )
    assert coordinator.coordination_kinds() == ("external-extension-terminal-projections",)
    with pytest.raises(ValueError, match="already registered"):
        register_external_extension_recovery(
            coordinator, handles.handles,
        )


def _record_resolution_without_acquire(
    handles: ExternalExtensionRuntimeStartupHandles,
    install: InstallIntent,
    *,
    operation_id: str,
) -> ResolvedSource:
    assert install.source_spec is not None
    source = ResolvedSource(
        "github_repository",
        install.source_spec.locator,
        COMMIT,
        artifact_ref(install.intent_id, operation_id),
        trust_tier="untrusted",
    )
    handles.facts.record_resolution_observation(
        operation_id=operation_id,
        intent_reference=intent_ref(install.intent_id),
        source=source,
    )
    handles.facts.record_resolution(
        operation_id=operation_id,
        intent_reference=intent_ref(install.intent_id),
        source=source,
    )
    return source


def test_confirmed_fixed_acquire_backfill_plans_only_then_core_dispatches_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.effect_log import EffectRecoveryCoordinator

    _runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)
    install = _install("confirmed-fixed-crash-window")
    _source_authorization(handles, install)
    operation_id = "resolution-confirmed-fixed-1001"
    _record_resolution_without_acquire(handles, install, operation_id=operation_id)

    # Simulate restart after the immutable source confirmation and resolution
    # became durable but before the caller created an Acquire Effect.
    restarted_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="confirmed-fixed-restart",
    )
    restarted = _whitebox_services(
        tmp_path, restarted_runtime,
        register_external_extension_runtime(tmp_path, restarted_runtime),
    )
    coordinator = EffectRecoveryCoordinator(restarted_runtime)
    register_external_extension_recovery(coordinator, restarted.handles, limit=10)

    assert restarted_runtime.log.planned_for_kinds(
        (ACQUIRE_EFFECT_KIND,), limit=10,
    ) == []
    assert fetcher.calls == []

    report = coordinator.recover_once(now=10)

    assert report.backfill_failed == ()
    acquire_commands = [
        record.payload
        for record in restarted.facts._records.list("external_extension_install_commands")
        if record.payload.get("operation") == "acquire_resolved_v2"
    ]
    assert len(acquire_commands) == 1
    acquired = restarted_runtime.log.get(str(acquire_commands[0]["operation_id"]))
    assert acquired.kind == ACQUIRE_EFFECT_KIND
    assert acquired.state is EffectState.SETTLED_OK
    assert fetcher.calls == [
        f"https://codeload.github.com/example/extensions/zip/{COMMIT}",
    ]


def test_confirmed_floating_acquire_backfill_requires_exact_revision_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)
    fixed = _install("confirmed-floating-crash-window")
    assert fixed.source_spec is not None
    install = replace(
        fixed,
        source_spec=replace(fixed.source_spec, requested_ref=None),
    )
    proposal = handles.facts.record_proposal(
        install, proposal_id="proposal-confirmed-floating-1001",
    )
    handles.facts.confirm_source_proposal(
        proposal,
        confirmation_id="source-confirmed-floating-1001",
        confirmation_ids=("approve_initial_network_source",),
        actor="local-operator",
        reason="Approve source discovery only.",
    )
    operation_id = "resolution-confirmed-floating-1001"
    _record_resolution_without_acquire(handles, install, operation_id=operation_id)

    # The initial network confirmation alone is deliberately insufficient for
    # a floating revision.  It is pending approval, not terminal failure.
    assert handles._intake.backfill_confirmed_acquire_effects(limit=10) == 0
    assert runtime.log.planned_for_kinds((ACQUIRE_EFFECT_KIND,), limit=10) == []
    assert fetcher.calls == []
    assert handles.facts.confirmed_acquire_backfill_failure(operation_id) is None

    handles.facts.confirm_resolved_revision(
        proposal,
        resolution_ref(operation_id),
        confirmation_id="revision-confirmed-floating-1001",
        confirmation_ids=("approve_resolved_revision_download",),
        actor="local-operator",
        reason="Approve the exact pinned revision.",
    )
    assert handles._intake.backfill_confirmed_acquire_effects(limit=10) == 1
    effects = tuple(runtime.log.planned_for_kinds((ACQUIRE_EFFECT_KIND,), limit=10))
    assert len(effects) == 1
    assert effects[0].kind == ACQUIRE_EFFECT_KIND
    assert effects[0].state is EffectState.PLANNED
    assert fetcher.calls == []


def test_confirmed_acquire_backfill_is_idempotent_and_rejects_revision_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, handles, _fetcher = _registered_runtime(tmp_path, monkeypatch)
    install = _install("confirmed-idempotent")
    _source_authorization(handles, install)
    operation_id = "resolution-confirmed-idempotent-1001"
    _record_resolution_without_acquire(handles, install, operation_id=operation_id)

    assert handles._intake.backfill_confirmed_acquire_effects(limit=10) == 1
    assert handles._intake.backfill_confirmed_acquire_effects(limit=10) == 0

    drift_fixed = _install("confirmed-drift")
    assert drift_fixed.source_spec is not None
    floating = replace(
        drift_fixed,
        source_spec=replace(drift_fixed.source_spec, requested_ref=None),
    )
    proposal = handles.facts.record_proposal(
        floating, proposal_id="proposal-confirmed-drift-1001",
    )
    handles.facts.confirm_source_proposal(
        proposal,
        confirmation_id="source-confirmed-drift-1001",
        confirmation_ids=("approve_initial_network_source",),
        actor="local-operator",
        reason="Approve source discovery only.",
    )
    drift_operation = "resolution-confirmed-drift-1001"
    _record_resolution_without_acquire(handles, floating, operation_id=drift_operation)
    confirmation = handles.facts.confirm_resolved_revision(
        proposal,
        resolution_ref(drift_operation),
        confirmation_id="revision-confirmed-drift-1001",
        confirmation_ids=("approve_resolved_revision_download",),
        actor="local-operator",
        reason="Approve the exact pinned revision.",
    )
    confirmation_id = confirmation.rsplit("/", 1)[1]
    with handles.facts._records.begin() as uow:
        record = uow.read("external_extension_revision_confirmations", confirmation_id)
        assert record is not None
        payload = dict(record.payload)
        payload["immutable_revision"] = "b" * 40
        uow.put(
            "external_extension_revision_confirmations",
            confirmation_id,
            payload,
            expected_revision=record.revision,
        )
        uow.commit()

    with pytest.raises(ExternalExtensionFactConflict, match="rejected durable resolution"):
        handles._intake.backfill_confirmed_acquire_effects(limit=10)
    failure = handles.facts.confirmed_acquire_backfill_failure(drift_operation)
    assert failure == {
        "schema_version": "1.0.0",
        "kind": "confirmed_acquire",
        "reason_code": "integrity_conflict",
        "error_type": "ExternalExtensionFactConflict",
    }


def test_commands_execute_registered_runtime_and_settle_immutable_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)

    resolved = _resolve(handles, intent_id="success", now=1)
    acquired = _acquire(handles, resolved.operation_id, intent_id="success", now=2)

    assert resolved.state is EffectState.SETTLED_OK
    assert resolved.result_ref
    assert acquired.state is EffectState.SETTLED_OK
    assert acquired.result_ref
    assert fetcher.calls == [f"https://codeload.github.com/example/extensions/zip/{COMMIT}"]


@pytest.mark.parametrize(
    "field,value",
    (
        ("gate_decision_id", "gate:external-extension/forged"),
        ("purpose", "aux"),
        ("turn_id", "forged-turn"),
        ("parent_id", "forged-parent"),
        ("rev_set", {
            "policy": "forged-policy", "boundary": "forged-boundary",
            "capability": "not_applicable", "context_manifest": "not_applicable",
            "provider": "not_applicable", "model_route": "not_applicable",
            "bundle": "not_applicable", "handler": "forged-handler",
            "secret": "not_applicable", "budget": "not_applicable",
            "workflow": "not_applicable",
        }),
        ("identity_algorithm", "forged-identity"),
        ("revision_schema_version", "forged-schema"),
    ),
)
def test_registered_intake_handlers_reject_frozen_identity_tampering_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    """Both normal Handler and recovery probe stop before resolver/acquirer work."""

    runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)
    resolved = _resolve(handles, intent_id=f"frozen-resolve-{field}", now=1)
    acquired = _acquire(
        handles,
        resolved.operation_id,
        intent_id=f"frozen-resolve-{field}",
        now=2,
    )
    resolve_registration = runtime.handlers.resolve(resolved)
    acquire_registration = runtime.handlers.resolve(acquired)
    assert resolve_registration.probe is not None
    assert acquire_registration.probe is not None
    calls_before = list(fetcher.calls)

    for registration, effect in (
        (resolve_registration, resolved),
        (acquire_registration, acquired),
    ):
        tampered = replace(effect, **{field: value})
        with pytest.raises(ExternalExtensionFactConflict, match="identity|invalid|drifted"):
            registration.handler(tampered)
        with pytest.raises(ExternalExtensionFactConflict, match="identity|invalid|drifted"):
            registration.probe(tampered)

    # Production dispatch above exercised the real registered Runner path.
    # The post-tamper Handler and probe calls must not make another fetch or
    # emit another observation/evidence/receipt.
    assert fetcher.calls == calls_before
    assert handles.facts.resolution_receipt(resolved.operation_id) == resolved.result_ref
    assert handles.facts.intake_receipt(acquired.operation_id) == acquired.result_ref


def test_registered_handler_rejects_command_record_shifted_away_from_semantic_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)
    resolved = _resolve(handles, intent_id="shifted-command", now=1)
    command_id = handles._intake._derived(
        "resolve-command", _install("shifted-command").intent_id,
    )
    shifted_id = "external-extension-resolve-command-" + "f" * 32
    with handles.facts._records.begin() as uow:
        command = uow.read("external_extension_install_commands", command_id)
        assert command is not None
        uow.delete(
            "external_extension_install_commands",
            command_id,
            expected_revision=command.revision,
        )
        uow.put(
            "external_extension_install_commands",
            shifted_id,
            command.payload,
            expected_revision=0,
        )
        uow.commit()

    registration = runtime.handlers.resolve(resolved)
    assert registration.probe is not None
    calls_before = list(fetcher.calls)
    with pytest.raises(ExternalExtensionFactConflict, match="semantic identity"):
        registration.handler(resolved)
    with pytest.raises(ExternalExtensionFactConflict, match="semantic identity"):
        registration.probe(resolved)
    assert fetcher.calls == calls_before


def test_resolve_backfill_excludes_terminal_effect_and_rejects_tampered_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)
    resolved = _resolve(handles, intent_id="backfill-terminal", now=1)
    command_id = handles._intake._derived(
        "resolve-command", _install("backfill-terminal").intent_id,
    )
    record = handles.facts._records.read(
        "external_extension_install_commands", command_id,
    )
    assert record is not None
    calls_before = list(fetcher.calls)
    assert handles._intake._backfill_resolve_record(record) is False
    assert fetcher.calls == calls_before

    with handles.facts._records.begin() as uow:
        current = uow.read("external_extension_install_commands", command_id)
        assert current is not None
        payload = dict(current.payload)
        payload["operation_id"] = "forged-effect-operation-1001"
        uow.put(
            "external_extension_install_commands",
            command_id,
            payload,
            expected_revision=current.revision,
        )
        uow.commit()
    tampered = handles.facts._records.read("external_extension_install_commands", command_id)
    assert tampered is not None
    with pytest.raises(ExternalExtensionFactError, match="Effect identity drifted"):
        handles._intake._backfill_resolve_record(tampered)


def test_atomic_intake_plan_rejects_a_different_effect_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, handles, _fetcher = _registered_runtime(tmp_path, monkeypatch)
    install = _install("different-effect-database")
    effect = SimpleNamespace(operation_id="resolve-effect-different-database-1001")
    other_runtime = build_effect_runtime(
        tmp_path / "other.sqlite3", owner_id="other-effect-log",
    )
    with pytest.raises(ExternalExtensionFactConflict, match="atomic plan contract drifted"):
        handles.facts._record_intent_and_plan_effect(
            install,
            command_id="resolve-command-different-database-1001",
            command_payload={
                "schema_version": "2.0.0",
                "operation": "resolve_install_v2",
                "intent_id": install.intent_id,
                "project_id": "project-001",
                "authorization_ref": "confirmation-different-database-1001",
                "gate_decision_id": "gate-different-database-1001",
                "operation_id": effect.operation_id,
            },
            effect_log=other_runtime.log,
            effect_intent=effect,
            gate_decision_id="gate-different-database-1001",
            gate_fact=object(),
            now=1,
        )


def test_intake_boundary_rejects_unconfirmed_or_invalid_sources_before_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)
    auto = _install("scope")
    cases = (
        (
            replace(
                auto,
                disposition="ASK",
                reason_codes=("source_resolution_required",),
            ),
            "not authorized",
        ),
        (
            replace(
                auto,
                source_spec=SourceSpec(
                    request_id=auto.intent_id,
                    kind="https_archive",
                    locator="https://example.com/extensions.zip",
                    requested_ref=COMMIT,
                ),
            ),
            "GitHub repository",
        ),
    )

    for index, (install, message) in enumerate(cases):
        with pytest.raises(ValueError, match=message):
            handles.resolve_install(
                install,
                authorization_ref="confirmation:forged",
                now=index + 1,
            )

    with pytest.raises(ExternalExtensionFactError, match="install intent is missing"):
        handles.facts.load_intent(intent_ref(auto.intent_id))
    with pytest.raises(ExternalExtensionFactError):
        handles.resolve_install(
            auto,
            authorization_ref="confirmation:forged",
            now=3,
        )
    with pytest.raises(TypeError, match="unexpected keyword argument 'gate_fact'"):
        handles.resolve_install(
            auto,
            authorization_ref="confirmation:forged",
            now=3,
            gate_fact=object(),
        )
    foreign_authorization = _source_authorization(handles, _install("foreign"))
    with pytest.raises(ValueError, match="exact source confirmation"):
        handles.resolve_install(
            auto,
            authorization_ref=foreign_authorization,
            now=4,
        )
    assert fetcher.calls == []


def test_core_reaper_recovers_receipt_and_artifact_crash_windows_from_registered_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, handles, fetcher = _registered_runtime(tmp_path, monkeypatch)
    original_settle = runtime.runner.settle_ok

    def crash_after_receipt(*_args: object, **_kwargs: object):
        raise RuntimeError("simulated crash after durable handler output")

    monkeypatch.setattr(runtime.runner, "settle_ok", crash_after_receipt)
    with pytest.raises(RuntimeError, match="durable handler output"):
        _resolve(handles, intent_id="receipt-window", now=1)
    monkeypatch.setattr(runtime.runner, "settle_ok", original_settle)
    recovered = runtime.recover_expired(now=100)
    assert any(outcome.state is EffectState.SETTLED_OK for outcome in recovered)

    resolved = _resolve(handles, intent_id="artifact-window", now=200)
    original_record_intake = ExternalExtensionFactStore.record_intake

    def crash_after_artifact(self: ExternalExtensionFactStore, **_kwargs: object) -> str:
        raise RuntimeError("simulated crash after artifact evidence")

    monkeypatch.setattr(ExternalExtensionFactStore, "record_intake", crash_after_artifact)
    with pytest.raises(RuntimeError, match="artifact evidence"):
        _acquire(handles, resolved.operation_id, intent_id="artifact-window", now=201)
    monkeypatch.setattr(ExternalExtensionFactStore, "record_intake", original_record_intake)

    recovered = runtime.recover_expired(now=400)
    assert any(outcome.state is EffectState.SETTLED_OK for outcome in recovered)
    assert fetcher.calls == [f"https://codeload.github.com/example/extensions/zip/{COMMIT}"]


def test_lifecycle_handles_share_persistent_installations_bindings_and_materialized_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, handles, _fetcher = _registered_runtime(tmp_path, monkeypatch)
    installed, revision, authorization_ref = _record_reviewed_skill(
        handles, suffix="lifecycle-1001",
    )

    health = handles.lifecycle_commands.execute(
        revision.revision_ref, "health", installed.state_revision,
        authorization_ref=authorization_ref,
    )
    assert health.completed
    assert health.snapshot is not None
    assert health.snapshot.state_revision > installed.state_revision
    activated = handles.lifecycle_commands.execute(
        revision.revision_ref, "activation", health.snapshot.state_revision,
        authorization_ref=authorization_ref,
    )
    assert activated.completed
    packages = handles.active_packages("project-001")
    assert len(packages) == 1
    assert ApplicationSkillPackageLoader().load_instructions(packages[0]).markdown == (
        "Use the startup lifecycle fixture.\n"
    )
    sources = handles.active_sources("project-001")
    assert len(sources) == 1
    package = ApplicationSkillCatalog().inspect_package(sources[0].root / "startup-skill")
    assert ApplicationSkillPackageLoader().load_instructions(package).markdown == (
        "Use the startup lifecycle fixture.\n"
    )

    restarted_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="startup-restarted",
    )
    restarted = _whitebox_services(
        tmp_path, restarted_runtime,
        register_external_extension_runtime(tmp_path, restarted_runtime),
    )
    assert restarted.installations.load_revision(revision.revision_ref) == revision
    assert restarted.active_packages("project-001") == packages
    assert restarted.active_sources("project-001") == sources
    assert restarted_runtime.runner.log is restarted_runtime.log
    assert restarted_runtime.reaper.log is restarted_runtime.log
    assert restarted_runtime.handlers.kinds() == runtime.handlers.kinds()


def test_restarted_active_packages_fail_closed_on_binding_provenance_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, handles, _fetcher = _registered_runtime(tmp_path, monkeypatch)
    installed, revision, authorization_ref = _record_reviewed_skill(
        handles, suffix="restart-provenance-drift",
    )
    health = handles.lifecycle_commands.execute(
        revision.revision_ref,
        "health",
        installed.state_revision,
        authorization_ref=authorization_ref,
    )
    assert health.snapshot is not None
    activated = handles.lifecycle_commands.execute(
        revision.revision_ref,
        "activation",
        health.snapshot.state_revision,
        authorization_ref=authorization_ref,
    )
    assert activated.completed

    binding_store, _settings = build_rebuild_object_store(tmp_path)
    registry = ApplicationSkillBindingRegistry(binding_store)
    raw = binding_store.read(registry.collection, registry.registry_id)
    assert raw is not None
    drifted_source_id = "external-another-reviewed-revision"
    for binding in raw["bindings"]:
        binding["source_id"] = drifted_source_id
    for event in raw["history"]:
        for field in ("before", "after"):
            for binding in event[field]:
                binding["source_id"] = drifted_source_id
    binding_store.write(
        registry.collection,
        registry.registry_id,
        raw,
        expected_revision=binding_store.revision(
            registry.collection, registry.registry_id,
        ),
    )

    restarted_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="restart-provenance-drift",
    )
    restarted = _whitebox_services(
        tmp_path, restarted_runtime,
        register_external_extension_runtime(tmp_path, restarted_runtime),
    )

    with pytest.raises(
        ExternalExtensionSkillMaterializationError,
        match="provenance drifted",
    ):
        restarted.active_packages("project-001")


def test_restarted_core_coordinator_projects_terminal_lifecycle_effect_without_command_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.effect_log import EffectRecoveryCoordinator

    runtime, handles, _fetcher = _registered_runtime(tmp_path, monkeypatch)
    installed, revision, authorization_ref = _record_reviewed_skill(
        handles, suffix="restart-terminal-projection",
    )
    original_projection = handles.lifecycle_commands._record_projection
    monkeypatch.setattr(
        handles.lifecycle_commands,
        "_record_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("crash after terminal Effect before projection"),
        ),
    )
    with pytest.raises(RuntimeError, match="crash after terminal Effect"):
        handles.lifecycle_commands.execute(
            revision.revision_ref,
            "health",
            installed.state_revision,
            authorization_ref=authorization_ref,
        )
    monkeypatch.setattr(
        handles.lifecycle_commands, "_record_projection", original_projection,
    )

    restarted_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="restart-terminal",
    )
    restarted = _whitebox_services(
        tmp_path, restarted_runtime,
        register_external_extension_runtime(tmp_path, restarted_runtime),
    )
    coordinator = EffectRecoveryCoordinator(restarted_runtime)
    register_external_extension_recovery(
        coordinator, restarted.handles,
    )

    report = coordinator.recover_once(now=100)
    assert report.coordination_completed == ("external-extension-terminal-projections",)
    assert restarted.installations.load(
        "startup-skill", root_id="project-001",
    ).candidate_status == "health_verified_disabled"
    assert not [
        record for record in restarted.lifecycle_commands._records.list_all()
        if record.collection == "external_extension_lifecycle_reservations"
    ]


def test_restarted_core_coordinator_backfills_dispatches_and_projects_legacy_intent_only_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.effect_log import EffectRecoveryCoordinator
    from core.external_extension_runtime.lifecycle_commands import (
        _COMMANDS,
        _INTENTS,
        _RESERVATIONS,
        _derived,
        _intent_payload,
        _reservation_id,
    )

    runtime, handles, _fetcher = _registered_runtime(tmp_path, monkeypatch)
    installed, revision, authorization_ref = _record_reviewed_skill(
        handles, suffix="restart-intent-only",
    )
    semantic = handles.lifecycle_commands._semantic(
        revision.revision_ref, "health", installed.state_revision,
    )
    authorization = handles.gate_authority.authorize(ExternalExtensionGateRequest(
        phase="lifecycle_health",
        project_id="project-001",
        subject_ref=revision.intake_ref,
        authorization_ref=authorization_ref,
        policy_revision="external-extension-lifecycle-policy-v1",
    ))
    command_id = _derived("command", semantic)
    intent = handles.installations.build_lifecycle_intent(
        revision.revision_ref,
        action="health",
        intent_id=_derived("intent", semantic),
    )
    operation_id = handles.lifecycle_commands._effect_intent(
        intent, semantic, authorization.decision_id,
    ).operation_id
    reservation_id = _reservation_id(semantic)
    records = handles.lifecycle_commands._records
    with records.begin() as uow:
        uow.put(
            _INTENTS,
            intent.intent_id,
            _intent_payload(intent),
            expected_revision=0,
        )
        uow.put(
            _RESERVATIONS,
            reservation_id,
            {
                "schema_version": "1.0.0",
                "reservation_id": reservation_id,
                "command_id": command_id,
                "root_id": semantic["root_id"],
                "extension_id": semantic["extension_id"],
                "expected_state_revision": semantic["expected_state_revision"],
                "effect_operation_id": operation_id,
            },
            expected_revision=0,
        )
        uow.put(
            _COMMANDS,
            command_id,
            {
                "schema_version": "1.0.0",
                "semantic": semantic,
                "intent_ref": intent.intent_ref,
                "operation_id": operation_id,
                "gate_decision_id": authorization.decision_id,
                "authorization_ref": authorization.authorization_ref,
                "snapshot": None,
            },
            expected_revision=0,
        )
        uow.commit()

    restarted_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="restart-intent-only",
    )
    restarted = _whitebox_services(
        tmp_path, restarted_runtime,
        register_external_extension_runtime(tmp_path, restarted_runtime),
    )
    coordinator = EffectRecoveryCoordinator(restarted_runtime)
    register_external_extension_recovery(
        coordinator, restarted.handles,
    )

    report = coordinator.recover_once(now=100)
    assert report.backfill_failed == ()
    assert report.backfill_completed == (
        "external-extension-confirmed-acquire-intents",
        "external-extension-resolve-intents",
        "external-extension-lifecycle-intents",
    )
    assert report.coordination_completed == ("external-extension-terminal-projections",)
    effect = restarted_runtime.log.get(operation_id)
    assert effect.state is EffectState.SETTLED_OK
    assert effect.result_ref is not None
    assert restarted_runtime.handlers.kinds().count(
        lifecycle_contract("health").effect_kind,
    ) == 1
    assert restarted.lifecycle_commands._receipts.load_receipt(operation_id).effect_operation_id == operation_id
    assert restarted.installations.load(
        "startup-skill", root_id="project-001",
    ).candidate_status == "health_verified_disabled"
    assert restarted.lifecycle_commands._records.read(_RESERVATIONS, reservation_id) is None
