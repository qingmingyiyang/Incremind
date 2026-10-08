from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.ppt_master_installation_runtime import (
    ImmutableArtifactStore,
    FinalInstallationReceiptStore,
    GateAuthorization,
    INSTALL_EFFECT_KIND,
    INSTALL_INTENT_SCHEMA,
    INSTALL_RECEIPT_KIND,
    INSTALL_RECEIPT_SCHEMA,
    PptMasterInstallationError,
    PptMasterWorkflowBoundaryError,
    PptMasterInstallationRuntime,
    ROLLBACK_EFFECT_KIND,
    ROLLBACK_INTENT_SCHEMA,
    ROLLBACK_RECEIPT_KIND,
    ROLLBACK_RECEIPT_SCHEMA,
    _rollback_revisions,
    _installation_revisions,
    _validate_runtime_self_manifest,
)
from core.effect_log import EffectClass, EffectIntent, EffectPurpose, EffectState, build_effect_runtime
from core.effect_log.core import GateDecision, GateDecisionFact
from backend.security.network_egress_decision import NetworkEgressDecisionStore


REVISION = "a" * 40


class Acquisition:
    def __init__(self, artifacts: ImmutableArtifactStore, source: Path, calls: list[str]) -> None:
        self.artifacts, self.source, self.calls = artifacts, source, calls

    def stage(self, effect):
        self.calls.append(f"effect:{effect.state.value}")
        return self.artifacts.store(effect.operation_id, {"revision": REVISION}, self.source)


class Intake:
    def __init__(self, calls: list[str], *, fail_review: bool = False) -> None:
        self.calls, self.fail_review = calls, fail_review

    def discover(self, source, *, command_id):
        self.calls.append("discover")
        return {"state_revision": 1}

    def install_disabled(self, *args, **kwargs):
        self.calls.append("install-disabled")
        return {"state_revision": 2}


class Activation:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls, self.fail = calls, fail

    def review(self, *args, **kwargs):
        self.calls.append("review")
        if self.fail:
            raise ValueError("review failed")
        return {"review_revision": 1}

    def activate(self, *args, **kwargs):
        self.calls.append("activate")
        return {"activation_revision": 1}

    def disable(self, *args, **kwargs):
        self.calls.append("skill-disable")
        return {"activation_revision": 2}


class Profiles:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def activate(self, project_id, **kwargs):
        self.calls.append("profile-activate")
        return {
            "project_id": project_id, "status": "active", "owned_plugin_source": True,
            "owned_skill_id": True, "owned_plugin_id": True, "owned_tool_id": True,
            "profile_revision": 1,
        }

    def deactivate(self, project_id, **kwargs):
        self.calls.append("profile-deactivate")
        return {"project_id": project_id, "status": "inactive", "profile_revision": 2}


class Bindings:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.active = False

    def preview_bind(self, _package, **_kwargs):
        self.calls.append("binding-preview")
        return {"registry_revision": 0, "preview_token": "binding-token"}

    def activate(self, _package, **_kwargs):
        self.calls.append("binding-activate")
        self.active = True
        return {"binding_revision": 1}

    def status(self):
        return {"registry_revision": 1, "bindings": ([{"project_id": "project-a", "skill_id": "ppt-master", "status": "active"}] if self.active else [])}

    def deactivate(self, **_kwargs):
        self.calls.append("binding-deactivate")
        self.active = False
        return {"status": "deactivated"}


def _runtime(tmp_path: Path, *, fail_review: bool = False):
    source = tmp_path / "downloaded"
    source.mkdir()
    (source / "SKILL.md").write_text("upstream body", encoding="utf-8")
    calls: list[str] = []
    artifacts = ImmutableArtifactStore(tmp_path / "artifacts")
    final_receipts = FinalInstallationReceiptStore(tmp_path / "final-receipts")
    core = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    profiles = Profiles(calls)
    service = PptMasterInstallationRuntime(
        effect_runtime=core, resolver=lambda _repository: REVISION,
        runtime_self_manifest={"manifest_revision": "runtime-revision-1", "compatibility": {"ppt-master": {"status": "compatible"}}},
        gate=lambda **values: GateAuthorization(
            f"gate:ppt-master:test:{values['operation_id']}",
            GateDecisionFact(
                decision=GateDecision.ALLOW, rule_ref="rule:ppt-master-test",
                scope_ref="scope:ppt-master-test", budget_after={},
                secret_scope="scope:ppt-master-test", policy_revision=values["policy_revision"],
            ),
        ),
        acquisition=Acquisition(artifacts, source, calls), artifacts=artifacts,
        intake=Intake(calls), activation=Activation(calls, fail=fail_review),
        bindings=Bindings(calls), profiles=profiles, clock=lambda: 100,
        package_root=tmp_path / "thin-packages", final_receipts=final_receipts,
        egress_decisions=NetworkEgressDecisionStore(tmp_path / "egress-decisions"),
    )
    service._active_package = lambda: object()
    return service, calls, core, tmp_path


def _effect_id(core, kind: str) -> str:
    with core.log._connect() as connection:
        row = connection.execute(
            "SELECT operation_id FROM effect WHERE kind=? ORDER BY recorded_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_preview_is_deterministic_and_has_no_writes(tmp_path: Path) -> None:
    service, _calls, _core, root = _runtime(tmp_path)

    first = service.preview("project-a")
    second = service.preview("project-a")

    assert first == second
    assert first["repository_url"] == "https://github.com/hugohe3/ppt-master"
    assert first["revision"] == REVISION
    assert first["write_effect"] == "none"
    assert not (root / "artifacts").exists()
    assert not (root / "thin-packages").exists()


def test_runtime_manifest_requires_authoritative_manifest_revision() -> None:
    with pytest.raises(PptMasterInstallationError, match="compatible"):
        _validate_runtime_self_manifest({"revision": "legacy-only", "compatibility": {"ppt-master": {"status": "compatible"}}})
    assert _validate_runtime_self_manifest({
        "manifest_revision": "authoritative-v1", "compatibility": {"ppt-master": {"status": "compatible"}},
    })["revision"] == "authoritative-v1"


def test_confirm_requires_complete_gate_inputs_before_planning(tmp_path: Path) -> None:
    service, calls, core, _root = _runtime(tmp_path)
    preview = service.preview("project-a")

    with pytest.raises(PptMasterInstallationError, match="risks"):
        service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=())

    assert calls == []
    assert core.log.planned_for_kinds(("ppt_master_self_install",), limit=10) == []


@pytest.mark.parametrize("decision", [GateDecision.DENY, GateDecision.ASK])
def test_non_allow_gate_fails_closed_without_effect_facts(tmp_path: Path, decision: GateDecision) -> None:
    service, _calls, core, _root = _runtime(tmp_path)
    service._gate = lambda **values: GateAuthorization(
        "gate:ppt-master:denied",
        GateDecisionFact(decision=decision, rule_ref="rule:ppt-master-test", scope_ref="scope:ppt-master-test",
                         budget_after={}, secret_scope="scope:ppt-master-test", policy_revision=values["policy_revision"]),
    )
    preview = service.preview("project-a")
    with pytest.raises(PptMasterWorkflowBoundaryError) as captured:
        service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])
    projection = captured.value.projection
    assert projection["schema_version"] == "1.0.0"
    assert projection["action"] == ("need_user" if decision is GateDecision.ASK else "blocked")
    assert "gate_ask" in projection["reasons"] if decision is GateDecision.ASK else projection["reasons"] == ["gate_denied"]
    with core.log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM effect_intent_fact").fetchone()[0] == 0


def test_gate_policy_drift_fails_closed_before_inflight(tmp_path: Path) -> None:
    service, calls, core, _root = _runtime(tmp_path)
    service._gate = lambda **_values: GateAuthorization(
        "gate:ppt-master:drift",
        GateDecisionFact(decision=GateDecision.ALLOW, rule_ref="rule:ppt-master-test", scope_ref="scope:ppt-master-test",
                         budget_after={}, secret_scope="scope:ppt-master-test", policy_revision="other-policy"),
    )
    preview = service.preview("project-a")
    with pytest.raises(ValueError, match="policy revision drifted"):
        service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])
    assert calls == []
    assert core.log.planned_for_kinds(("ppt_master_self_install",), limit=10) == []


def test_ppt_writers_never_use_legacy_plan_api() -> None:
    source = Path("src/backend/api/ppt_master_installation_runtime.py").read_text(encoding="utf-8")
    assert ".log.plan(" not in source


def test_confirm_plans_then_dispatches_once_and_creates_thin_package(tmp_path: Path) -> None:
    service, calls, core, root = _runtime(tmp_path)
    preview = service.preview("project-a")
    kwargs = dict(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])

    first = service.confirm(**kwargs)
    replay = service.confirm(**kwargs)

    assert first == replay
    assert calls == ["effect:INFLIGHT", "discover", "install-disabled", "review", "activate", "binding-preview", "binding-activate", "profile-activate"]
    assert core.log.get(str(first["operation_id"])).state is EffectState.SETTLED_OK
    receipt = (root / "final-receipts" / f"{first['operation_id']}.json").read_text(encoding="utf-8")
    assert '"binding_revision": 1' in receipt
    assert calls.index("binding-activate") < calls.index("profile-activate")
    skill = root / "thin-packages" / str(first["operation_id"]) / "skills" / "ppt-master" / "SKILL.md"
    assert skill.is_file()
    assert skill.stat().st_size <= 16 * 1024
    assert "presentation.pptx.fixed" in skill.read_text(encoding="utf-8")
    assert not (root / "thin-packages" / str(first["operation_id"]) / "scripts").exists()
    with core.log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect_intent_fact").fetchone()[0] == 1
        receipt_fact = connection.execute(
            "SELECT receipt_kind,receipt_schema_version FROM effect_receipt WHERE operation_id=?",
            (first["operation_id"],),
        ).fetchone()
    assert tuple(receipt_fact) == ("ppt-master-self-installation", "ppt-master-self-installation/v2")
    effect = core.log.get(first["operation_id"])
    assert effect.contract_version == "effect-v2"
    assert effect.operation_id.startswith("eff2_")
    assert effect.operation_id != preview["operation_id"]


def test_failed_review_never_projects_active_skill(tmp_path: Path) -> None:
    service, calls, core, _root = _runtime(tmp_path, fail_review=True)
    preview = service.preview("project-a")

    with pytest.raises(ValueError, match="review failed"):
        service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])

    assert "activate" not in calls
    assert "binding-activate" not in calls
    assert "profile-activate" not in calls
    with core.log._connect() as connection:
        operation_id = connection.execute("SELECT operation_id FROM effect WHERE kind=?", ("ppt_master_self_install",)).fetchone()[0]
    assert core.log.get(operation_id).state is EffectState.INFLIGHT
    assert service._probe(core.log.get(operation_id)) == (
        EffectState.PLANNED, f"facts:ppt-master-install-recovery:{operation_id}",
    )


def test_final_receipt_without_pointer_recovers_only_pointer(tmp_path: Path) -> None:
    service, calls, _core, root = _runtime(tmp_path)
    preview = service.preview("project-a")
    first = service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])
    pointer = root / "final-receipts" / "current-project-a.json"
    pointer.unlink()
    before = list(calls)

    result = service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])

    assert result["state"] == EffectState.SETTLED_OK.value
    assert result == first
    assert calls == before
    assert service.status("project-a")["state"] == "installed"


def test_tampered_final_receipt_fails_closed(tmp_path: Path) -> None:
    service, _calls, core, root = _runtime(tmp_path)
    preview = service.preview("project-a")
    installed = service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])
    path = root / "final-receipts" / f"{installed['operation_id']}.json"
    payload = path.read_text(encoding="utf-8").replace(REVISION, "b" * 40)
    path.write_text(payload, encoding="utf-8")
    assert service._probe(core.log.get(str(installed["operation_id"]))) == (
        EffectState.PLANNED,
        f"facts:ppt-master-install-recovery:{installed['operation_id']}",
    )


def test_tampered_profile_projection_fails_closed(tmp_path: Path) -> None:
    service, _calls, core, root = _runtime(tmp_path)
    preview = service.preview("project-a")
    installed = service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])
    path = root / "final-receipts" / f"{installed['operation_id']}.json"
    payload = path.read_text(encoding="utf-8").replace('"status": "active"', '"status": "inactive"')
    path.write_text(payload, encoding="utf-8")
    assert service._probe(core.log.get(str(installed["operation_id"]))) == (
        EffectState.PLANNED,
        f"facts:ppt-master-install-recovery:{installed['operation_id']}",
    )


def test_install_reaper_replans_pre_side_effect_failure_for_same_v2_effect(tmp_path: Path) -> None:
    service, calls, core, _root = _runtime(tmp_path)
    preview = service.preview("project-a")
    original_stage = service._acquisition.stage
    attempts = 0

    def fail_before_staging(effect):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("transient acquisition failure")
        return original_stage(effect)

    service._acquisition.stage = fail_before_staging
    with pytest.raises(ConnectionError, match="transient acquisition failure"):
        service.confirm(
            project_id="project-a", preview_token=str(preview["preview_token"]),
            confirm=True, risk_acknowledgements=preview["risks"],
        )

    effect = core.log.get(_effect_id(core, INSTALL_EFFECT_KIND))
    assert effect.state is EffectState.INFLIGHT
    assert calls == []
    outcomes = core.recover_expired(now=200)
    assert [(item.operation_id, item.state, item.reason) for item in outcomes] == [
        (effect.operation_id, EffectState.PLANNED, "probe_resolved"),
    ]
    replanned = core.log.get(effect.operation_id)
    assert replanned.state is EffectState.PLANNED
    assert replanned.probe_ref == f"facts:ppt-master-install-recovery:{effect.operation_id}"

    retried = service.confirm(
        project_id="project-a", preview_token=str(preview["preview_token"]),
        confirm=True, risk_acknowledgements=preview["risks"],
    )

    assert retried["operation_id"] == effect.operation_id
    assert retried["state"] == EffectState.SETTLED_OK.value
    assert attempts == 2
    assert calls == [
        "effect:INFLIGHT", "discover", "install-disabled", "review", "activate",
        "binding-preview", "binding-activate", "profile-activate",
    ]


def test_rollback_reaper_replans_pre_side_effect_failure_for_same_v2_effect(tmp_path: Path) -> None:
    service, calls, core, _root = _runtime(tmp_path)
    preview = service.preview("project-a")
    service.confirm(
        project_id="project-a", preview_token=str(preview["preview_token"]),
        confirm=True, risk_acknowledgements=preview["risks"],
    )
    original_disable = service._activation.disable
    attempts = 0

    def fail_before_disable(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("transient rollback failure")
        return original_disable(*args, **kwargs)

    service._activation.disable = fail_before_disable
    with pytest.raises(ConnectionError, match="transient rollback failure"):
        service.rollback(project_id="project-a", confirm=True)

    effect = core.log.get(_effect_id(core, ROLLBACK_EFFECT_KIND))
    assert effect.state is EffectState.INFLIGHT
    before_recovery = list(calls)
    outcomes = core.recover_expired(now=200)
    assert [(item.operation_id, item.state, item.reason) for item in outcomes] == [
        (effect.operation_id, EffectState.PLANNED, "probe_resolved"),
    ]
    replanned = core.log.get(effect.operation_id)
    assert replanned.state is EffectState.PLANNED
    assert replanned.probe_ref == f"facts:ppt-master-rollback-recovery:{effect.operation_id}"
    assert calls == before_recovery

    retried = service.rollback(project_id="project-a", confirm=True)

    assert retried["operation_id"] == effect.operation_id
    assert retried["state"] == EffectState.SETTLED_OK.value
    assert attempts == 2
    assert calls[-3:] == ["skill-disable", "binding-deactivate", "profile-deactivate"]


def test_status_and_rollback_reject_tampered_active_authority(tmp_path: Path) -> None:
    service, calls, _core, root = _runtime(tmp_path)
    preview = service.preview("project-a")
    installed = service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])
    path = root / "final-receipts" / f"{installed['operation_id']}.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"binding_revision": 1', '"binding_revision": true'), encoding="utf-8")
    before = list(calls)
    with pytest.raises(PptMasterInstallationError, match="status authority drifted"):
        service.status("project-a")
    with pytest.raises(PptMasterInstallationError, match="status authority drifted"):
        service.rollback(project_id="project-a", confirm=True)
    assert calls == before


def test_reaper_settles_expired_v2_install_from_probe_without_repeating_writer(tmp_path: Path) -> None:
    service, calls, core, _root = _runtime(tmp_path)
    egress = service._egress_decisions.decide(
        project_id="project-a", source_revision=REVISION,
        manifest_revision="runtime-revision-1", generation=0,
    )
    revisions = _installation_revisions(REVISION, "runtime-revision-1", 0, egress)
    intent = EffectIntent(
        session_id="recovery-session", root_id="project-a", step_key="ppt-master:recovery",
        kind=INSTALL_EFFECT_KIND, effect_class=EffectClass.QUERYABLE, purpose=EffectPurpose.PRIMARY,
        intent_ref="crp://ppt-master-install-intents/recovery", gate_decision_id="gate:ppt-master:recovery",
        rev_set=revisions,
        payload={"installation_ref": "crp://ppt-master-install-intents/recovery", "source_revision": REVISION,
                 "manifest_revision": "runtime-revision-1", "generation_id": "0", "mode": "governed-install",
                 "egress_decision_ref": egress.decision_ref},
        idem_key="ppt-master-install:recovery", contract_version="effect-v2",
        intent_schema_version=INSTALL_INTENT_SCHEMA, expected_receipt_kind=INSTALL_RECEIPT_KIND,
        expected_receipt_schema_version=INSTALL_RECEIPT_SCHEMA,
    )
    planned, _ = core.log.plan_v2(intent, gate_decision_id=intent.gate_decision_id, gate_fact=GateDecisionFact(
        decision=GateDecision.ALLOW, rule_ref="rule:ppt-master-test", scope_ref="scope:ppt-master-test",
        budget_after={}, secret_scope="scope:ppt-master-test", policy_revision=revisions["policy"],
    ), now=100)
    service._final_receipts.write(planned.operation_id, {
        "receipt": f"receipt:ppt-master-installation:{planned.operation_id}", "operation_id": planned.operation_id,
        "project_id": "project-a", "generation": 0, "commit": REVISION,
        "manifest_revision": "runtime-revision-1", "artifact_receipt": f"ppt-master-artifact:{planned.operation_id}",
        "plugin_id": "ppt-master", "skill_id": "ppt-master", "activation_revision": 1,
        "binding_revision": 1,
        "profile_projection": {"project_id": "project-a", "status": "active", "owned_plugin_source": True,
                               "owned_skill_id": True, "owned_plugin_id": True, "owned_tool_id": True,
                               "profile_revision": 1},
    })
    service._final_receipts.set_installed("project-a", planned.operation_id, generation=0, activation_revision=1)
    core.log.transition(planned.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                        now=101, lease_owner="crashed", lease_expires_at=102)
    outcomes = core.recover_expired(now=200)
    assert [(item.operation_id, item.state, item.reason) for item in outcomes] == [
        (planned.operation_id, EffectState.SETTLED_OK, "probe_resolved"),
    ]
    assert core.log.get(planned.operation_id).state is EffectState.SETTLED_OK
    assert calls == []


def test_pending_thin_and_receipt_pointer_promote(tmp_path: Path) -> None:
    service, _calls, _core, root = _runtime(tmp_path)
    operation_id = "pptmaster-pending"
    thin = service._write_thin_package(operation_id, REVISION)
    thin.replace(thin.parent / f".{operation_id}.pending")
    assert service._write_thin_package(operation_id, REVISION).is_dir()

    receipts = service._final_receipts
    payload = {"receipt": "ppt-master-installation:pending", "operation_id": "pending", "project_id": "project-a", "generation": 0, "activation_revision": 1}
    receipts.write("pending", payload)
    receipt_path = root / "final-receipts" / "pending.json"
    receipt_path.replace(root / "final-receipts" / ".pending.pending")
    assert receipts.write("pending", payload) == "ppt-master-installation:pending"
    receipts.set_installed("project-a", "pending", generation=0, activation_revision=1)
    pointer = root / "final-receipts" / "current-project-a.json"
    pointer.replace(root / "final-receipts" / ".current-project-a.pending")
    receipts.set_installed("project-a", "pending", generation=0, activation_revision=1)
    assert receipts.state("project-a")["status"] == "installed"


def test_rollback_only_changes_projection(tmp_path: Path) -> None:
    service, calls, core, _root = _runtime(tmp_path)

    preview = service.preview("project-a")
    service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])
    service._resolver = lambda _repository: "b" * 40
    assert service.status("project-a")["state"] == "installed"
    result = service.rollback(project_id="project-a", confirm=True)
    assert result["state"] == EffectState.SETTLED_OK.value
    assert core.log.get(str(result["operation_id"])).state is EffectState.SETTLED_OK
    assert calls[-3:] == ["skill-disable", "binding-deactivate", "profile-deactivate"]
    assert (tmp_path / "final-receipts" / f"{result['operation_id']}.json").is_file()
    assert service.status("project-a")["state"] == "rolled_back"

    reinstall_preview = service.preview("project-a")
    assert reinstall_preview["operation_id"] != preview["operation_id"]
    service.confirm(project_id="project-a", preview_token=str(reinstall_preview["preview_token"]), confirm=True, risk_acknowledgements=reinstall_preview["risks"])
    assert service.status("project-a")["state"] == "installed"
    assert calls.count("binding-activate") == 2
    assert calls.count("profile-activate") == 2


def test_rollback_receipt_without_pointer_recovers_only_pointer(tmp_path: Path) -> None:
    service, calls, core, _root = _runtime(tmp_path)
    preview = service.preview("project-a")
    installed = service.confirm(project_id="project-a", preview_token=str(preview["preview_token"]), confirm=True, risk_acknowledgements=preview["risks"])
    installation_receipt = service._final_receipts.read(installed["operation_id"])
    revisions = _rollback_revisions(installation_receipt, generation=0)
    pending, _ = core.log.plan_v2(EffectIntent(
        session_id="local-self-install", root_id="project-a", step_key=f"rollback-{installed['operation_id']}",
        kind=ROLLBACK_EFFECT_KIND, effect_class=EffectClass.QUERYABLE, purpose=EffectPurpose.PRIMARY,
        intent_ref=f"crp://ppt-master-rollback-intents/{installed['operation_id']}",
        gate_decision_id=f"gate:ppt-master:test:rollback-{installed['operation_id']}", rev_set=revisions,
        payload={"installation_ref": f"receipt:ppt-master-installation:{installed['operation_id']}",
                 "installation_operation_id": installed["operation_id"], "generation_id": "0", "mode": "governed-rollback"},
        idem_key=f"ppt-master-rollback:{installed['operation_id']}", contract_version="effect-v2",
        intent_schema_version=ROLLBACK_INTENT_SCHEMA, expected_receipt_kind=ROLLBACK_RECEIPT_KIND,
        expected_receipt_schema_version=ROLLBACK_RECEIPT_SCHEMA,
    ), gate_decision_id=f"gate:ppt-master:test:rollback-{installed['operation_id']}", gate_fact=GateDecisionFact(
        decision=GateDecision.ALLOW, rule_ref="rule:ppt-master-test", scope_ref="scope:ppt-master-test",
        budget_after={}, secret_scope="scope:ppt-master-test", policy_revision=revisions["policy"],
    ), now=100)
    rollback_operation = pending.operation_id
    service._final_receipts.write(rollback_operation, {
        "receipt": f"receipt:ppt-master-rollback:{rollback_operation}", "operation_id": rollback_operation,
        "project_id": "project-a", "installation_operation_id": installed["operation_id"],
        "installation_receipt": installed["receipt"], "next_generation": 1,
        "disabled_activation_revision": 2,
        "profile_projection": {"project_id": "project-a", "status": "inactive", "profile_revision": 2},
    })
    before = list(calls)

    result = service.rollback(project_id="project-a", confirm=True)

    assert result["state"] == EffectState.SETTLED_OK.value
    assert calls == before
    assert service.status("project-a")["state"] == "rolled_back"
