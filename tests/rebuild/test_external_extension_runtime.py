from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.effect_log import (
    EffectClass,
    EffectLog,
    EffectReaper,
    EffectRunner,
    EffectState,
    GateDecision,
    GateDecisionFact,
)
from core.external_extension_runtime import (
    ExternalExtensionAcquireHandler,
    ExternalExtensionFactConflict,
    ExternalExtensionFactStore,
    ExternalExtensionResolveHandler,
    ImmutableQuarantineArtifactStore,
    build_acquire_effect_intent,
    build_resolve_effect_intent,
)
from core.external_extension_runtime.fact_store import artifact_ref, resolution_ref
from core.external_extensions import (
    ArtifactInventory,
    ResolvedSource,
    parse_install_intent,
)
from core.storage_provider import SQLiteStructuredRecordStore


REVISION = "a" * 40
SECRET_CANARY = "SECRET_CANARY_EXTENSION_42"


class _Resolver:
    def __init__(self, *, trust_tier: str = "untrusted") -> None:
        self.calls = 0
        self.trust_tier = trust_tier

    def resolve(self, source, *, artifact_ref: str, operation_id: str) -> ResolvedSource:
        self.calls += 1
        resolved = ResolvedSource(
            source_kind=source.kind,
            canonical_locator=source.locator,
            immutable_revision=source.requested_ref or REVISION,
            artifact_ref=artifact_ref,
            trust_tier=self.trust_tier,
        )
        return resolved


class _Acquirer:
    def __init__(self, files: dict[str, bytes], *, expected_subpath: str | None = None) -> None:
        self.files = files
        self.calls = 0
        self.expected_subpath = expected_subpath
        self.observed_subpaths: list[str | None] = []

    def acquire(
        self,
        source: ResolvedSource,
        *,
        operation_id: str,
        subpath: str | None,
    ) -> ArtifactInventory:
        assert source.artifact_ref.endswith(source.artifact_ref.rsplit("/", 1)[-1])
        assert operation_id.startswith("eff2_")
        assert subpath == self.expected_subpath
        self.observed_subpaths.append(subpath)
        self.calls += 1
        return ArtifactInventory.capture(self.files)


def _facts(path: Path) -> ExternalExtensionFactStore:
    return ExternalExtensionFactStore(
        SQLiteStructuredRecordStore(path),
        ImmutableQuarantineArtifactStore(path.parent / f"{path.stem}-extension-quarantine"),
    )


def _test_only_frozen_identity_validator(_effect) -> None:
    """Unit fixtures construct legacy direct Effects without intake commands."""


def _gate_fact(step: str) -> GateDecisionFact:
    return GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref=f"crp://rules/external-extension-quarantine/{step}",
        scope_ref="crp://scopes/project-demo",
        budget_after={"network_bytes": 32 * 1024 * 1024},
        secret_scope="scope:secret/none",
        policy_revision="policy-1",
    )


def _intent():
    return parse_install_intent(
        "安装技能 https://github.com/example/fixture",
        intent_id="install-intent-1001",
        project_id="project:demo",
        requested_ref=REVISION,
    )


def _resolve_intent(intent):
    return build_resolve_effect_intent(
        intent,
        session_id="session-1",
        root_id="root-1",
        step_key="resolve-extension",
        gate_decision_id="gate:resolve:1",
        policy_revision="policy-1",
        boundary_revision="boundary-1",
        handler_revision="resolver-1",
    )


def _execute_resolution(
    database: Path,
    facts: ExternalExtensionFactStore,
    resolver: _Resolver,
    intent=None,
):
    intent = intent or _intent()
    facts.record_intent(intent, command_id="record-intent-1001")
    runner = EffectRunner(EffectLog(database), owner_id="resolver-worker", lease_seconds=10)
    effect = runner.execute_v2(
        _resolve_intent(intent),
        ExternalExtensionResolveHandler(
            facts, resolver,
            frozen_identity_validator=_test_only_frozen_identity_validator,
        ),
        gate_decision_id="gate:resolve:1",
        gate_fact=_gate_fact("resolve"),
        now=10,
    )
    return intent, runner, effect


def test_resolution_and_intake_run_as_v2_queryable_effects_with_immutable_receipts(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    resolver = _Resolver()
    install, _resolve_runner, resolved_effect = _execute_resolution(database, facts, resolver)

    assert resolved_effect.state is EffectState.SETTLED_OK
    assert resolver.calls == 1
    install_id, source = facts.load_resolution(resolution_ref(resolved_effect.operation_id))
    assert install_id == install.intent_id
    assert source.immutable_revision == REVISION
    assert resolved_effect.result_ref == resolution_ref(resolved_effect.operation_id)
    assert facts.load_resolution(resolved_effect.result_ref) == (install.intent_id, source)

    acquisition = build_acquire_effect_intent(
        resolution_operation_id=resolved_effect.operation_id,
        facts=facts,
        session_id="session-1",
        root_id="root-1",
        step_key="acquire-extension",
        gate_decision_id="gate:acquire:1",
        policy_revision="policy-1",
        boundary_revision="boundary-1",
        handler_revision="acquirer-1",
    )
    acquirer = _Acquirer(
        {
            "SKILL.md": b"---\nname: fixture-skill\ndescription: Safe fixture.\n---\nReturn fixture.\n",
            "README.md": SECRET_CANARY.encode("ascii"),
        },
    )
    runner = EffectRunner(EffectLog(database), owner_id="acquirer-worker", lease_seconds=10)
    acquired_effect = runner.execute_v2(
        acquisition,
        ExternalExtensionAcquireHandler(
            facts, acquirer,
            frozen_identity_validator=_test_only_frozen_identity_validator,
        ),
        gate_decision_id="gate:acquire:1",
        gate_fact=_gate_fact("acquire"),
        now=20,
    )

    assert acquired_effect.state is EffectState.SETTLED_OK
    assert acquirer.calls == 1
    snapshot = facts.intake_snapshot(acquired_effect.operation_id)
    assert snapshot is not None
    assert acquired_effect.result_ref == facts.intake_receipt(acquired_effect.operation_id)
    assert facts.load_intake(acquired_effect.result_ref) == snapshot
    assert snapshot["artifact_file_count"] == 2
    assert snapshot["artifact_total_bytes"] > 0
    assert SECRET_CANARY not in repr(snapshot)
    assert str(tmp_path) not in repr(snapshot)
    assert snapshot["result"]["projection"] == "installed_disabled"
    assert snapshot["result"]["review_plan"]["disposition"] == "ASK"


def test_acquisition_passes_frozen_source_subpath_to_read_only_fetcher(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    install = parse_install_intent(
        "安装技能 https://github.com/example/fixture",
        intent_id="install-intent-subpath-1001",
        requested_ref=REVISION,
        subpath="skills/demo",
    )
    install, _resolve_runner, resolved = _execute_resolution(
        database,
        facts,
        _Resolver(),
        install,
    )
    acquisition = build_acquire_effect_intent(
        resolution_operation_id=resolved.operation_id,
        facts=facts,
        session_id="session-1",
        root_id="root-1",
        step_key="acquire-extension-subpath",
        gate_decision_id="gate:acquire:subpath",
        policy_revision="policy-1",
        boundary_revision="boundary-1",
        handler_revision="acquirer-1",
    )
    acquirer = _Acquirer(
        {"SKILL.md": b"---\nname: subpath-skill\ndescription: Safe fixture.\n---\n"},
        expected_subpath="skills/demo",
    )
    acquired = EffectRunner(
        EffectLog(database),
        owner_id="acquirer-worker",
        lease_seconds=10,
    ).execute_v2(
        acquisition,
        ExternalExtensionAcquireHandler(
            facts, acquirer,
            frozen_identity_validator=_test_only_frozen_identity_validator,
        ),
        gate_decision_id="gate:acquire:subpath",
        gate_fact=GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="crp://rules/external-extension-quarantine/subpath",
            scope_ref="crp://scopes/project-demo",
            budget_after={"network_bytes": 32 * 1024 * 1024},
            secret_scope="scope:secret/none",
            policy_revision="policy-1",
        ),
        now=20,
    )

    assert acquired.state is EffectState.SETTLED_OK
    assert acquirer.observed_subpaths == ["skills/demo"]
    assert facts.intake_snapshot(acquired.operation_id)["result"]["projection"] == "installed_disabled"


def test_receipt_fact_recovers_crash_before_effect_settlement_without_reacquisition(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    install, _resolve_runner, resolved_effect = _execute_resolution(database, facts, _Resolver())
    _install_id, source = facts.load_resolution(resolution_ref(resolved_effect.operation_id))
    intent = build_acquire_effect_intent(
        resolution_operation_id=resolved_effect.operation_id,
        facts=facts,
        session_id="session-1",
        root_id="root-1",
        step_key="acquire-extension",
        gate_decision_id="gate:acquire:1",
        policy_revision="policy-1",
        boundary_revision="boundary-1",
        handler_revision="acquirer-1",
    )
    log = EffectLog(database)
    runner = EffectRunner(log, owner_id="crashing-worker", lease_seconds=2)
    planned, _ = log.plan_v2(
        intent,
        gate_decision_id="gate:acquire:1",
        gate_fact=_gate_fact("acquire"),
        now=20,
    )
    inflight, claimed = runner.claim_planned(planned.operation_id, now=20)
    assert claimed and inflight.state is EffectState.INFLIGHT
    acquirer = _Acquirer({"README.md": b"unknown artifact"})
    handler = ExternalExtensionAcquireHandler(
        facts, acquirer,
        frozen_identity_validator=_test_only_frozen_identity_validator,
    )

    receipt = handler(inflight)
    assert receipt.receipt_ref == facts.intake_receipt(inflight.operation_id)
    assert log.get(inflight.operation_id).state is EffectState.INFLIGHT
    assert facts.intake_snapshot(inflight.operation_id)["result"] == {
        "projection": "quarantined",
        "quarantine_code": "unknown_format",
    }

    # Model worker A disappearing after the immutable intake receipt and an
    # independent worker B reopening both Core Effect and extension facts.
    restarted_facts = _facts(database)
    restarted_handler = ExternalExtensionAcquireHandler(
        restarted_facts, acquirer,
        frozen_identity_validator=_test_only_frozen_identity_validator,
    )
    outcomes = EffectReaper(EffectLog(database)).recover_expired(
        now=23,
        probes={(intent.kind, intent.contract_version): restarted_handler.probe},
    )
    assert outcomes[0].state is EffectState.SETTLED_OK
    assert log.get(inflight.operation_id).result_ref == receipt.receipt_ref
    assert restarted_facts.intake_receipt(inflight.operation_id) == receipt.receipt_ref
    assert acquirer.calls == 1


def test_quarantine_evidence_recovers_crash_before_intake_fact_without_refetch(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    _install, _resolve_runner, resolved_effect = _execute_resolution(database, facts, _Resolver())
    _install_id, source = facts.load_resolution(resolution_ref(resolved_effect.operation_id))
    intent = build_acquire_effect_intent(
        resolution_operation_id=resolved_effect.operation_id,
        facts=facts,
        session_id="session-1",
        root_id="root-1",
        step_key="acquire-extension",
        gate_decision_id="gate:acquire:1",
        policy_revision="policy-1",
        boundary_revision="boundary-1",
        handler_revision="acquirer-1",
    )
    log = EffectLog(database)
    runner = EffectRunner(log, owner_id="crashing-worker", lease_seconds=2)
    planned, _ = log.plan_v2(
        intent,
        gate_decision_id="gate:acquire:1",
        gate_fact=_gate_fact("acquire"),
        now=20,
    )
    inflight, claimed = runner.claim_planned(planned.operation_id, now=20)
    assert claimed and inflight.state is EffectState.INFLIGHT
    acquirer = _Acquirer(
        {"SKILL.md": b"---\nname: recovered-skill\ndescription: Safe fixture.\n---\nReturn fixture.\n"},
    )
    evidence = facts.commit_artifact(
        operation_id=inflight.operation_id,
        source=source,
        inventory=acquirer.acquire(source, operation_id=inflight.operation_id, subpath=None),
    )
    assert facts.intake_receipt(inflight.operation_id) is None

    restarted_facts = _facts(database)
    handler = ExternalExtensionAcquireHandler(
        restarted_facts, acquirer,
        frozen_identity_validator=_test_only_frozen_identity_validator,
    )
    outcomes = EffectReaper(EffectLog(database)).recover_expired(
        now=23,
        probes={(intent.kind, intent.contract_version): handler.probe},
    )

    assert outcomes[0].state is EffectState.SETTLED_OK
    recovered = log.get(inflight.operation_id)
    assert recovered.result_ref == restarted_facts.intake_receipt(inflight.operation_id)
    snapshot = restarted_facts.intake_snapshot(inflight.operation_id)
    assert snapshot["artifact_receipt_ref"] == evidence.artifact_receipt_ref
    assert snapshot["artifact_content_sha256"] == evidence.content_sha256
    assert snapshot["result"]["projection"] == "installed_disabled"
    assert acquirer.calls == 1


def test_durable_resolution_observation_recovers_before_fact_without_second_resolution(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    install = _intent()
    facts.record_intent(install, command_id="record-intent-1001")
    intent = _resolve_intent(install)
    log = EffectLog(database)
    runner = EffectRunner(log, owner_id="crashing-resolver", lease_seconds=2)
    planned, _ = log.plan_v2(
        intent,
        gate_decision_id="gate:resolve:1",
        gate_fact=_gate_fact("resolve"),
        now=10,
    )
    inflight, claimed = runner.claim_planned(planned.operation_id, now=10)
    assert claimed and inflight.state is EffectState.INFLIGHT
    resolver = _Resolver()
    observed = resolver.resolve(
        install.source_spec,
        artifact_ref=artifact_ref(install.intent_id, inflight.operation_id),
        operation_id=inflight.operation_id,
    )
    facts.record_resolution_observation(
        operation_id=inflight.operation_id,
        intent_reference=inflight.intent_ref,
        source=observed,
    )
    restarted_facts = _facts(database)
    cold_resolver = _Resolver()

    outcomes = EffectReaper(log).recover_expired(
        now=13,
        probes={
            (intent.kind, intent.contract_version): ExternalExtensionResolveHandler(
                restarted_facts, cold_resolver,
                frozen_identity_validator=_test_only_frozen_identity_validator,
            ).probe,
        },
    )

    assert outcomes[0].state is EffectState.SETTLED_OK
    recovered = log.get(inflight.operation_id)
    assert recovered.result_ref == resolution_ref(inflight.operation_id)
    assert restarted_facts.load_resolution(recovered.result_ref)[1].immutable_revision == REVISION
    assert resolver.calls == 1
    assert cold_resolver.calls == 0


def test_intent_command_replay_is_idempotent_and_drift_fails_closed(tmp_path: Path) -> None:
    facts = _facts(tmp_path / "runtime.sqlite3")
    original = _intent()
    reference = facts.record_intent(original, command_id="record-intent-1001")
    assert facts.record_intent(original, command_id="record-intent-1001") == reference

    drifted = parse_install_intent(
        "安装技能 https://github.com/example/other",
        intent_id=original.intent_id,
        requested_ref=REVISION,
    )
    with pytest.raises(ExternalExtensionFactConflict, match="drifted"):
        facts.record_intent(drifted, command_id="record-intent-1001")


def test_resolution_replay_rejects_source_identity_drift(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    install, _runner, effect = _execute_resolution(database, facts, _Resolver())
    _install_id, source = facts.load_resolution(resolution_ref(effect.operation_id))
    drifted = ResolvedSource(
        source_kind=source.source_kind,
        canonical_locator=source.canonical_locator,
        immutable_revision="b" * 40,
        artifact_ref=source.artifact_ref,
    )
    with pytest.raises(
        ExternalExtensionFactConflict,
        match="fixed source revision|durable observation",
    ):
        facts.record_resolution(
            operation_id=effect.operation_id,
            intent_reference=f"crp://external-extension-install-intents/{install.intent_id}",
            source=drifted,
        )


def test_resolution_rejects_cross_operation_artifact_reference(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    install = _intent()
    facts.record_intent(install, command_id="record-intent-1001")
    source = ResolvedSource(
        source_kind=install.source_spec.kind,
        canonical_locator=install.source_spec.locator,
        immutable_revision=REVISION,
        artifact_ref="crp://extension-artifacts/another-install/another-operation",
    )
    with pytest.raises(ExternalExtensionFactConflict, match="artifact reference drifted"):
        facts.record_resolution(
            operation_id="resolution-operation-1001",
            intent_reference=f"crp://external-extension-install-intents/{install.intent_id}",
            source=source,
        )


def test_resolver_cannot_self_attest_reviewed_source_trust(tmp_path: Path) -> None:
    facts = _facts(tmp_path / "runtime.sqlite3")
    install = _intent()
    facts.record_intent(install, command_id="record-intent-1001")
    source = ResolvedSource(
        source_kind=install.source_spec.kind,
        canonical_locator=install.source_spec.locator,
        immutable_revision=REVISION,
        artifact_ref=artifact_ref(install.intent_id, "resolution-operation-1001"),
        trust_tier="reviewed_source",
    )
    with pytest.raises(ExternalExtensionFactConflict, match="cannot be self-attested"):
        facts.record_resolution_observation(
            operation_id="resolution-operation-1001",
            intent_reference=f"crp://external-extension-install-intents/{install.intent_id}",
            source=source,
        )


def test_handlers_reject_effect_identity_drift_from_durable_facts(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    resolver = _Resolver()
    install, _runner, resolved = _execute_resolution(database, facts, resolver)
    resolve_handler = ExternalExtensionResolveHandler(
        facts, resolver,
        frozen_identity_validator=_test_only_frozen_identity_validator,
    )
    with pytest.raises(ExternalExtensionFactConflict, match="Effect identity drifted"):
        resolve_handler.probe(replace(resolved, intent_digest="0" * 64))

    acquisition = build_acquire_effect_intent(
        resolution_operation_id=resolved.operation_id,
        facts=facts,
        session_id="session-1",
        root_id="root-1",
        step_key="acquire-extension",
        gate_decision_id="gate:acquire:1",
        policy_revision="policy-1",
        boundary_revision="boundary-1",
        handler_revision="acquirer-1",
    )
    planned, _ = EffectLog(database).plan_v2(
        acquisition,
        gate_decision_id="gate:acquire:1",
        gate_fact=_gate_fact("acquire"),
        now=20,
    )
    acquire_handler = ExternalExtensionAcquireHandler(
        facts, _Acquirer({"README.md": b"fixture"}),
        frozen_identity_validator=_test_only_frozen_identity_validator,
    )
    with pytest.raises(ExternalExtensionFactConflict, match="Effect identity drifted"):
        acquire_handler.probe(replace(planned, intent_digest="1" * 64))


def test_acquirer_cannot_inject_unbound_in_memory_artifact_evidence(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    facts = _facts(database)
    _install, _runner, resolved = _execute_resolution(database, facts, _Resolver())
    _install_id, source = facts.load_resolution(resolution_ref(resolved.operation_id))
    acquisition = build_acquire_effect_intent(
        resolution_operation_id=resolved.operation_id,
        facts=facts,
        session_id="session-1",
        root_id="root-1",
        step_key="acquire-extension",
        gate_decision_id="gate:acquire:1",
        policy_revision="policy-1",
        boundary_revision="boundary-1",
        handler_revision="acquirer-1",
    )
    log = EffectLog(database)
    planned, _ = log.plan_v2(
        acquisition,
        gate_decision_id="gate:acquire:1",
        gate_fact=_gate_fact("acquire"),
        now=20,
    )
    inflight, claimed = EffectRunner(log, owner_id="worker", lease_seconds=10).claim_planned(
        planned.operation_id,
        now=20,
    )
    assert claimed
    competing, competing_claimed = EffectRunner(
        log,
        owner_id="competing-worker",
        lease_seconds=10,
    ).claim_planned(planned.operation_id, now=20)
    assert not competing_claimed
    assert competing.lease_owner == "worker"
    forged = ImmutableQuarantineArtifactStore(tmp_path / "attacker-store").commit(
        inflight.operation_id,
        source,
        ArtifactInventory.capture({"SKILL.md": b"forged"}),
    )

    class _ForgedEvidenceAcquirer:
        def acquire(
            self,
            source: ResolvedSource,
            *,
            operation_id: str,
            subpath: str | None,
        ):
            return forged

    with pytest.raises(ExternalExtensionFactConflict, match="invalid inventory"):
        ExternalExtensionAcquireHandler(
            facts, _ForgedEvidenceAcquirer(),
            frozen_identity_validator=_test_only_frozen_identity_validator,
        )(inflight)
    assert facts.intake_receipt(inflight.operation_id) is None
    assert facts.artifact_evidence(operation_id=inflight.operation_id, source=source) is None


def test_effect_builders_freeze_queryable_v2_contract_and_reject_mutable_acquisition(tmp_path: Path) -> None:
    install = _intent()
    resolution = _resolve_intent(install)
    assert resolution.effect_class is EffectClass.QUERYABLE
    assert resolution.contract_version == "effect-v2"
    assert resolution.payload == {
        "install_id": install.intent_id,
        "descriptor_ref": f"crp://external-extension-install-intents/{install.intent_id}",
        "requested_revision": REVISION,
    }
    assert install.source_spec.locator not in repr(resolution.payload)
    assert "subpath" not in resolution.payload
    assert tuple(sorted(resolution.rev_set)) == (
        "boundary", "budget", "bundle", "capability", "context_manifest", "handler",
        "model_route", "policy", "provider", "secret", "workflow",
    )

    immutable = ResolvedSource(
        source_kind="github_repository",
        canonical_locator="https://github.com/example/fixture",
        immutable_revision=REVISION,
        artifact_ref=artifact_ref(install.intent_id, "resolution-operation-1001"),
    )
    facts = _facts(tmp_path / "runtime.sqlite3")
    facts.record_intent(install, command_id="record-intent-1001")
    facts.record_resolution_observation(
        operation_id="resolution-operation-1001",
        intent_reference=f"crp://external-extension-install-intents/{install.intent_id}",
        source=immutable,
    )
    facts.record_resolution(
        operation_id="resolution-operation-1001",
        intent_reference=f"crp://external-extension-install-intents/{install.intent_id}",
        source=immutable,
    )
    acquisition = build_acquire_effect_intent(
        resolution_operation_id="resolution-operation-1001",
        facts=facts,
        session_id="session-1",
        root_id="root-1",
        step_key="acquire",
        gate_decision_id="gate:1",
        policy_revision="policy-1",
        boundary_revision="boundary-1",
        handler_revision="handler-1",
    )
    assert acquisition.payload == {
        "install_id": "install-intent-1001",
        "resolution_ref": resolution_ref("resolution-operation-1001"),
        "source_revision": REVISION,
        "artifact_ref": immutable.artifact_ref,
    }
    assert immutable.canonical_locator not in repr(acquisition.payload)

    mutable = ResolvedSource(
        source_kind="github_repository",
        canonical_locator="https://github.com/example/fixture",
        immutable_revision=None,
        artifact_ref="crp://extension-artifacts/fixture/mutable",
    )
    with pytest.raises(ValueError, match="immutable revision"):
        facts.record_resolution(
            operation_id="resolution-operation-2002",
            intent_reference=f"crp://external-extension-install-intents/{install.intent_id}",
            source=mutable,
        )
