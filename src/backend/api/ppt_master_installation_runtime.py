"""Governed self-install orchestration for the reviewed PPT Master Skill.

The module is deliberately a composition seam: preview is read-only, while the
only mutating path is an Effect handler registered as QUERYABLE.  It never
executes acquired content and never accepts a caller supplied URL or path.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from core.effect_log import Effect, EffectClass, EffectHandlerRegistration, EffectIntent, EffectPurpose, EffectState
from core.effect_log.core import (
    EFFECT_V2, NOT_APPLICABLE, EffectReceipt, GateDecision, GateDecisionFact,
    V2_REVISION_KEYS,
)
from core.plugin_host.github_acquisition import (
    GitHubAcquisitionPreview, GitHubAcquisitionStagingHandler, RevisionResolver,
    preview_github_source,
)
from core.plugin_host.package_intake import PluginPackageIntake
from core.plugin_host.skill_activation import PluginSkillActivation
from core.application_skill.package_catalog import ApplicationSkillCatalog
from core.application_skill.binding_registry import ApplicationSkillBindingRegistry
from core.product_core.workflow_decision_evidence import (
    WorkflowGateOutcome,
    decide_workflow_transition,
)
from core.product_core.workflow_progression import WorkflowDecisionBoundary
from backend.security.network_egress_decision import (
    NetworkEgressDecisionError,
    NetworkEgressDecisionFact,
    NetworkEgressDecisionStore,
)


PPT_MASTER_URL = "https://github.com/hugohe3/ppt-master"
PPT_MASTER_PLUGIN_ID = "ppt-master"
PPT_MASTER_SKILL_ID = "ppt-master"
INSTALL_EFFECT_KIND = "ppt_master_self_install"
ROLLBACK_EFFECT_KIND = "ppt_master_self_install_rollback"
INSTALL_INTENT_SCHEMA = "ppt-master-self-install/v2"
INSTALL_RECEIPT_KIND = "ppt-master-self-installation"
INSTALL_RECEIPT_SCHEMA = "ppt-master-self-installation/v2"
ROLLBACK_INTENT_SCHEMA = "ppt-master-self-install-rollback/v2"
ROLLBACK_RECEIPT_KIND = "ppt-master-self-install-rollback"
ROLLBACK_RECEIPT_SCHEMA = "ppt-master-self-install-rollback/v2"
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class PptMasterInstallationError(ValueError):
    pass


class PptMasterWorkflowBoundaryError(PptMasterInstallationError):
    def __init__(self, message: str, projection: Mapping[str, object]) -> None:
        super().__init__(message)
        self.projection = dict(projection)


@dataclass(frozen=True, slots=True)
class GateAuthorization:
    """Trusted-host Gate output; callers never supply these facts."""

    decision_id: str
    fact: GateDecisionFact


class ConfirmationGate(Protocol):
    def __call__(self, *, project_id: str, operation_id: str, risks: Sequence[str], policy_revision: str) -> GateAuthorization: ...


class ProfileProjector(Protocol):
    def activate(self, project_id: str, *, plugin_id: str, skill_id: str, operation_id: str) -> Mapping[str, object]: ...

    def deactivate(self, project_id: str, *, plugin_id: str, skill_id: str, operation_id: str, installation_projection: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class InstallationPreview:
    project_id: str
    operation_id: str
    revision: str
    manifest_revision: str
    preview_token: str
    risks: tuple[str, ...]
    write_effect: str = "none"

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id, "operation_id": self.operation_id,
            "repository_url": PPT_MASTER_URL, "revision": self.revision,
            "runtime_self_manifest_revision": self.manifest_revision,
            "preview_token": self.preview_token, "risks": list(self.risks),
            "write_effect": self.write_effect,
        }


class ImmutableArtifactStore:
    """Host-owned artifact authority; only the governed staging handler writes it."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve(strict=False)

    def probe(self, operation_id: str) -> str | None:
        receipt = self._root / operation_id / "receipt.json"
        if not receipt.is_file():
            return None
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        value = payload.get("receipt")
        return value if isinstance(value, str) and value else None

    def store(self, operation_id: str, descriptor: Mapping[str, object], tree: Path) -> str:
        existing = self.probe(operation_id)
        if existing is not None:
            return existing
        destination = self._root / operation_id
        if destination.exists():
            raise PptMasterInstallationError("artifact installation identity conflicts")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._root / f".{operation_id}.pending"
        if temporary.exists():
            receipt_path = temporary / "receipt.json"
            try:
                pending = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = None
            if isinstance(pending, Mapping) and pending.get("descriptor") == dict(descriptor) and isinstance(pending.get("receipt"), str) and (temporary / "source").is_dir():
                temporary.replace(destination)
                return str(pending["receipt"])
            shutil.rmtree(temporary)
        shutil.copytree(tree, temporary / "source")
        receipt = f"ppt-master-artifact:{operation_id}"
        (temporary / "receipt.json").write_text(
            json.dumps({"receipt": receipt, "descriptor": dict(descriptor)}, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(destination)
        return receipt

    def source_tree(self, operation_id: str) -> Path:
        tree = self._root / operation_id / "source"
        if not tree.is_dir() or tree.is_symlink():
            raise PptMasterInstallationError("immutable acquired artifact is unavailable")
        return tree


class FinalInstallationReceiptStore:
    """Immutable success receipt. Artifact presence is never installation success."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve(strict=False)

    def read(self, operation_id: str) -> Mapping[str, object] | None:
        path = self._root / f"{operation_id}.json"
        if not path.is_file() or path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None

    def write(self, operation_id: str, payload: Mapping[str, object]) -> str:
        existing = self.read(operation_id)
        frozen = dict(payload)
        if existing is not None:
            if existing != frozen:
                raise PptMasterInstallationError("installation receipt drifted")
            return str(existing["receipt"])
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / f"{operation_id}.json"
        temporary = self._root / f".{operation_id}.pending"
        if target.exists():
            raise PptMasterInstallationError("installation receipt target conflicts")
        if temporary.exists():
            try:
                pending = json.loads(temporary.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = None
            if pending == frozen:
                temporary.replace(target)
                return str(frozen["receipt"])
            temporary.unlink()
        temporary.write_text(json.dumps(frozen, sort_keys=True), encoding="utf-8")
        temporary.replace(target)
        return str(frozen["receipt"])

    def set_installed(self, project_id: str, operation_id: str, *, generation: int, activation_revision: int) -> None:
        receipt = self.read(operation_id)
        if receipt is None or receipt.get("project_id") != project_id or receipt.get("generation") != generation:
            raise PptMasterInstallationError("current installation pointer lacks a matching final receipt")
        self._write_pointer(project_id, {"project_id": project_id, "status": "installed", "generation": generation, "activation_revision": activation_revision, "installation_operation_id": operation_id})

    def mark_rolled_back(self, project_id: str, *, installation_operation_id: str, rollback_operation_id: str, generation: int, activation_revision: int) -> None:
        self._write_pointer(project_id, {"project_id": project_id, "status": "rolled_back", "generation": generation, "activation_revision": activation_revision, "installation_operation_id": installation_operation_id, "rollback_operation_id": rollback_operation_id})

    def state(self, project_id: str) -> Mapping[str, object]:
        path = self._root / f"current-{project_id}.json"
        if not path.is_file() or path.is_symlink():
            return {"project_id": project_id, "status": "not_installed", "generation": 0}
        pointer = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(pointer, Mapping) or pointer.get("project_id") != project_id or pointer.get("status") not in {"installed", "rolled_back"} or not isinstance(pointer.get("generation"), int) or isinstance(pointer.get("generation"), bool) or int(pointer["generation"]) < 0:
            raise PptMasterInstallationError("project installation pointer is invalid")
        return dict(pointer)

    def installation_receipt(self, project_id: str) -> Mapping[str, object] | None:
        pointer = self.state(project_id)
        operation_id = pointer.get("installation_operation_id")
        receipt = self.read(operation_id) if isinstance(operation_id, str) else None
        return receipt if receipt is not None and receipt.get("project_id") == project_id else None

    def _write_pointer(self, project_id: str, payload: Mapping[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / f"current-{project_id}.json"
        temporary = self._root / f".current-{project_id}.pending"
        frozen = dict(payload)
        if temporary.exists():
            try:
                pending = json.loads(temporary.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = None
            if pending == frozen:
                temporary.replace(target)
                return
            temporary.unlink()
        temporary.write_text(json.dumps(frozen, sort_keys=True), encoding="utf-8")
        temporary.replace(target)


class PptMasterInstallationRuntime:
    def __init__(
        self, *, effect_runtime, resolver: RevisionResolver, runtime_self_manifest: Mapping[str, object],
        gate: ConfirmationGate, acquisition: GitHubAcquisitionStagingHandler,
        artifacts: ImmutableArtifactStore, intake: PluginPackageIntake,
        activation: PluginSkillActivation, bindings: ApplicationSkillBindingRegistry,
        profiles: ProfileProjector, clock: Callable[[], int], package_root: Path,
        final_receipts: FinalInstallationReceiptStore,
        egress_decisions: NetworkEgressDecisionStore,
    ) -> None:
        self._effects = effect_runtime
        self._resolver, self._manifest, self._gate = resolver, _validate_runtime_self_manifest(runtime_self_manifest), gate
        self._acquisition, self._artifacts = acquisition, artifacts
        self._intake, self._activation, self._bindings, self._profiles = intake, activation, bindings, profiles
        self._clock, self._package_root = clock, Path(package_root).resolve(strict=False)
        self._final_receipts = final_receipts
        self._egress_decisions = egress_decisions
        self._effects.handlers.register(EffectHandlerRegistration(
            kind=INSTALL_EFFECT_KIND, effect_class=EffectClass.QUERYABLE,
            handler=self._handle, probe=self._probe, contract_version=EFFECT_V2,
            intent_schema_version=INSTALL_INTENT_SCHEMA,
            receipt_kind=INSTALL_RECEIPT_KIND, receipt_schema_version=INSTALL_RECEIPT_SCHEMA,
        ))
        self._effects.handlers.register(EffectHandlerRegistration(
            kind=ROLLBACK_EFFECT_KIND, effect_class=EffectClass.QUERYABLE,
            handler=self._handle_rollback, probe=self._probe_rollback, contract_version=EFFECT_V2,
            intent_schema_version=ROLLBACK_INTENT_SCHEMA,
            receipt_kind=ROLLBACK_RECEIPT_KIND, receipt_schema_version=ROLLBACK_RECEIPT_SCHEMA,
        ))

    def preview(self, project_id: str) -> dict[str, object]:
        project = _project(project_id)
        source = preview_github_source(PPT_MASTER_URL, resolver=self._resolver)
        preview = self._preview(project, source)
        return preview.as_dict()

    def confirm(
        self, *, project_id: str, preview_token: str, confirm: bool,
        risk_acknowledgements: Sequence[str], session_id: str = "local-self-install",
        network_egress: object = None, confirm_loopback_egress: bool = False,
    ) -> dict[str, object]:
        if confirm is not True:
            raise PptMasterInstallationError("installation requires explicit confirmation")
        project = _project(project_id)
        source = preview_github_source(PPT_MASTER_URL, resolver=self._resolver)
        preview = self._preview(project, source)
        if preview.preview_token != preview_token:
            raise PptMasterInstallationError("installation preview drifted")
        if set(risk_acknowledgements) != set(preview.risks):
            raise PptMasterInstallationError("all installation risks must be acknowledged")
        generation = int(self._final_receipts.state(project)["generation"])
        try:
            egress = self._egress_decisions.decide(
                project_id=project, source_revision=source.revision,
                manifest_revision=str(self._manifest["revision"]), generation=generation,
                network_egress=network_egress, confirm_loopback=confirm_loopback_egress,
            )
        except NetworkEgressDecisionError as error:
            raise PptMasterInstallationError(str(error)) from error
        revisions = _installation_revisions(
            source.revision, str(self._manifest["revision"]), generation, egress,
        )
        gated_operation = f"{preview.operation_id}.egress.{_egress_digest(egress.decision_revision)}"
        authorization = _require_allowed_gate(self._gate(
            project_id=project, operation_id=gated_operation, risks=preview.risks,
            policy_revision=revisions["policy"],
        ))
        intent = EffectIntent(
            session_id=session_id, root_id=project, step_key=f"ppt-master:{preview.operation_id}",
            kind=INSTALL_EFFECT_KIND, effect_class=EffectClass.QUERYABLE,
            purpose=EffectPurpose.PRIMARY,
            intent_ref=f"crp://ppt-master-install-intents/{preview.operation_id}",
            gate_decision_id=authorization.decision_id, rev_set=revisions,
            payload={
                "installation_ref": f"crp://ppt-master-install-intents/{preview.operation_id}",
                "source_revision": source.revision, "manifest_revision": str(self._manifest["revision"]),
                "generation_id": str(generation), "mode": "governed-install",
                "egress_decision_ref": egress.decision_ref,
            }, idem_key=f"ppt-master-install:{preview.operation_id}:{_egress_digest(egress.decision_revision)}",
            contract_version=EFFECT_V2,
            intent_schema_version=INSTALL_INTENT_SCHEMA, expected_receipt_kind=INSTALL_RECEIPT_KIND,
            expected_receipt_schema_version=INSTALL_RECEIPT_SCHEMA,
        )
        planned, _ = self._effects.log.plan_v2(
            intent, gate_decision_id=authorization.decision_id, gate_fact=authorization.fact,
            now=self._clock(),
        )
        settled = self._effects.dispatch_operation(planned.operation_id, now=self._clock())
        if settled.state is EffectState.SETTLED_OK:
            receipt = self._final_receipts.read(settled.operation_id)
            if _valid_installation_receipt(settled, receipt):
                self._final_receipts.set_installed(
                    project, settled.operation_id,
                    generation=_installation_authority(settled)[2],
                    activation_revision=_receipt_int(receipt, "activation_revision"),
                )
        return {"operation_id": settled.operation_id, "state": settled.state.value, "receipt": settled.result_ref}

    def status(self, project_id: str) -> dict[str, object]:
        project = _project(project_id)
        pointer, receipt = self._validated_status(project)
        return {"project_id": project, "operation_id": pointer.get("installation_operation_id"), "state": pointer["status"], "generation": pointer["generation"], "receipt": receipt.get("receipt") if receipt else None}

    def rollback(self, *, project_id: str, confirm: bool, session_id: str = "local-self-install") -> Mapping[str, object]:
        if confirm is not True:
            raise PptMasterInstallationError("rollback requires explicit confirmation")
        project = _project(project_id)
        pointer, receipt = self._validated_status(project)
        if pointer["status"] != "installed" or receipt is None:
            raise PptMasterInstallationError("final installation receipt is required for rollback")
        operation_id = f"rollback-{receipt['operation_id']}"
        revisions = _rollback_revisions(receipt, generation=int(pointer["generation"]))
        authorization = _require_allowed_gate(self._gate(
            project_id=project, operation_id=operation_id, risks=("project-skill-deactivation",),
            policy_revision=revisions["policy"],
        ))
        intent = EffectIntent(
            session_id=session_id, root_id=project, step_key=operation_id,
            kind=ROLLBACK_EFFECT_KIND, effect_class=EffectClass.QUERYABLE,
            purpose=EffectPurpose.PRIMARY, intent_ref=f"crp://ppt-master-rollback-intents/{receipt['operation_id']}",
            gate_decision_id=authorization.decision_id, rev_set=revisions,
            payload={"installation_ref": f"receipt:ppt-master-installation:{receipt['operation_id']}",
                     "installation_operation_id": str(receipt["operation_id"]),
                     "generation_id": str(pointer["generation"]), "mode": "governed-rollback"},
            idem_key=f"ppt-master-rollback:{receipt['operation_id']}", contract_version=EFFECT_V2,
            intent_schema_version=ROLLBACK_INTENT_SCHEMA, expected_receipt_kind=ROLLBACK_RECEIPT_KIND,
            expected_receipt_schema_version=ROLLBACK_RECEIPT_SCHEMA,
        )
        planned, _ = self._effects.log.plan_v2(
            intent, gate_decision_id=authorization.decision_id, gate_fact=authorization.fact,
            now=self._clock(),
        )
        settled = self._effects.dispatch_operation(planned.operation_id, now=self._clock())
        if settled.state is EffectState.SETTLED_OK:
            settled_receipt = self._final_receipts.read(settled.operation_id)
            if _valid_rollback_receipt(settled, settled_receipt):
                installation_operation_id, _generation = _rollback_authority(settled)
                self._final_receipts.mark_rolled_back(
                    project, installation_operation_id=installation_operation_id,
                    rollback_operation_id=settled.operation_id,
                    generation=_receipt_int(settled_receipt, "next_generation"),
                    activation_revision=_receipt_int(settled_receipt, "disabled_activation_revision"),
                )
        return {"operation_id": settled.operation_id, "state": settled.state.value, "receipt": settled.result_ref}

    def _preview(self, project: str, source: GitHubAcquisitionPreview) -> InstallationPreview:
        if not _REVISION.fullmatch(source.revision):
            raise PptMasterInstallationError("GitHub revision is not immutable")
        generation = int(self._final_receipts.state(project)["generation"])
        operation = _identity(project, source.revision, f"{self._manifest['revision']}:{generation}")
        risks = ("network-download", "third-party-content", "project-skill-activation")
        token = _token(project, operation, source.revision, f"{self._manifest['revision']}:{generation}", risks)
        return InstallationPreview(project, operation, source.revision, str(self._manifest["revision"]), token, risks)

    def _validated_status(self, project: str) -> tuple[Mapping[str, object], Mapping[str, object] | None]:
        """Read UI/rollback state only when pointer, receipt and Core agree."""
        pointer = self._final_receipts.state(project)
        status = pointer["status"]
        if status == "not_installed":
            return pointer, None
        installation_operation_id = pointer.get("installation_operation_id")
        if not isinstance(installation_operation_id, str):
            raise PptMasterInstallationError("installation pointer is invalid")
        installation = self._effect(installation_operation_id)
        receipt = self._final_receipts.read(installation_operation_id)
        if installation.state is not EffectState.SETTLED_OK or not _valid_installation_receipt(installation, receipt):
            raise PptMasterInstallationError("installation status authority drifted")
        if status == "installed":
            if not _pointer_matches_installation(pointer, installation, receipt):
                raise PptMasterInstallationError("installation pointer authority drifted")
            return pointer, receipt
        rollback_operation_id = pointer.get("rollback_operation_id")
        if not isinstance(rollback_operation_id, str):
            raise PptMasterInstallationError("rollback pointer is invalid")
        rollback = self._effect(rollback_operation_id)
        rollback_receipt = self._final_receipts.read(rollback_operation_id)
        if (rollback.state is not EffectState.SETTLED_OK
                or not _valid_rollback_receipt(rollback, rollback_receipt)
                or not _pointer_matches_rollback(pointer, rollback, rollback_receipt)):
            raise PptMasterInstallationError("rollback status authority drifted")
        return pointer, receipt

    def _effect(self, operation_id: str) -> Effect:
        try:
            return self._effects.log.get(operation_id)
        except KeyError as error:
            raise PptMasterInstallationError("installation Effect authority is unavailable") from error

    def _handle(self, effect: Effect) -> EffectReceipt:
        commit, manifest_revision, generation = _installation_authority(effect)
        self._egress_decision(effect, commit, manifest_revision, generation)
        pointer = self._final_receipts.state(effect.root_id)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation != pointer["generation"]:
            raise PptMasterInstallationError("installation generation drifted")
        existing = self._final_receipts.read(effect.operation_id)
        if existing is not None:
            if not _valid_installation_receipt(effect, existing):
                raise PptMasterInstallationError("installation final receipt drifted")
            self._final_receipts.set_installed(effect.root_id, effect.operation_id, generation=generation, activation_revision=_receipt_int(existing, "activation_revision"))
            return EffectReceipt(str(existing["receipt"]), INSTALL_RECEIPT_KIND, INSTALL_RECEIPT_SCHEMA, INSTALL_INTENT_SCHEMA)
        if pointer["status"] == "installed":
            raise PptMasterInstallationError("installation generation is already active")
        artifact_receipt = self._acquisition.stage(effect)
        self._artifacts.source_tree(effect.operation_id)
        package = self._write_thin_package(effect.operation_id, commit)
        discovered = self._intake.discover(str(package), command_id=f"{effect.operation_id}.discover")
        state = discovered.get("state") if isinstance(discovered, Mapping) else None
        already_installed_disabled = (
            discovered.get("status") == "installed_disabled"
            or (isinstance(state, Mapping) and state.get("status") == "installed_disabled")
        )
        installed = discovered if already_installed_disabled else self._intake.install_disabled(PPT_MASTER_PLUGIN_ID, expected_state_revision=int(discovered["state_revision"]), command_id=f"{effect.operation_id}.install", confirm=True)
        prior_installation = pointer.get("installation_operation_id")
        review_operation = (
            prior_installation
            if already_installed_disabled and pointer.get("status") == "rolled_back" and isinstance(prior_installation, str)
            else effect.operation_id
        )
        reviewed = self._activation.review(PPT_MASTER_PLUGIN_ID, skill_ids=(PPT_MASTER_SKILL_ID,), expected_state_revision=int(installed["state_revision"]), command_id=f"{review_operation}.review", confirm=True, reason="governed PPT Master self-install")
        activated = self._activation.activate(PPT_MASTER_PLUGIN_ID, expected_review_revision=int(reviewed["review_revision"]), expected_activation_revision=int(pointer.get("activation_revision", 0)), command_id=f"{effect.operation_id}.activate", confirm=True)
        package = self._active_package()
        binding_preview = self._bindings.preview_bind(package, project_id=effect.root_id, allowed_consumers=("turn.workbench-question", "document.generate"), priority=500, trigger_terms=("pptx", "presentation"))
        binding = self._bindings.activate(package, project_id=effect.root_id, allowed_consumers=("turn.workbench-question", "document.generate"), priority=500, trigger_terms=("pptx", "presentation"), expected_registry_revision=int(binding_preview["registry_revision"]), preview_token=str(binding_preview["preview_token"]), confirm=True, reason="governed PPT Master self-install")
        profile = self._profiles.activate(effect.root_id, plugin_id=PPT_MASTER_PLUGIN_ID, skill_id=PPT_MASTER_SKILL_ID, operation_id=effect.operation_id)
        receipt = _installation_receipt_ref(effect.operation_id)
        self._final_receipts.write(effect.operation_id, {"receipt": receipt, "operation_id": effect.operation_id, "project_id": effect.root_id, "generation": generation, "commit": commit, "manifest_revision": manifest_revision, "artifact_receipt": artifact_receipt, "plugin_id": PPT_MASTER_PLUGIN_ID, "skill_id": PPT_MASTER_SKILL_ID, "activation_revision": activated["activation_revision"], "binding_revision": _binding_revision(binding, effect.root_id), "profile_projection": dict(profile)})
        self._final_receipts.set_installed(effect.root_id, effect.operation_id, generation=generation, activation_revision=int(activated["activation_revision"]))
        return EffectReceipt(receipt, INSTALL_RECEIPT_KIND, INSTALL_RECEIPT_SCHEMA, INSTALL_INTENT_SCHEMA)

    def _probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            commit, manifest_revision, generation = _installation_authority(effect)
            self._egress_decision(effect, commit, manifest_revision, generation)
        except PptMasterInstallationError:
            return EffectState.UNKNOWN, f"error:ppt-master-egress-decision-missing:{effect.operation_id}"
        receipt = self._final_receipts.read(effect.operation_id)
        pointer = self._final_receipts.state(effect.root_id)
        if (_valid_installation_receipt(effect, receipt)
                and _pointer_matches_installation(pointer, effect, receipt)):
            return EffectState.SETTLED_OK, str(receipt["receipt"])
        return EffectState.PLANNED, _install_recovery_evidence_ref(effect.operation_id)

    def _egress_decision(
        self, effect: Effect, commit: str, manifest_revision: str, generation: int,
    ) -> NetworkEgressDecisionFact:
        revision = _egress_revision_from_effect(effect)
        try:
            fact = self._egress_decisions.get(revision)
        except NetworkEgressDecisionError as error:
            raise PptMasterInstallationError("installation network egress authority is unavailable") from error
        if (
            fact.scope_ref != f"scope:project/{effect.root_id}"
            or fact.source_revision != commit
            or fact.manifest_revision != manifest_revision
            or fact.generation != generation
        ):
            raise PptMasterInstallationError("installation network egress authority drifted")
        return fact

    def _handle_rollback(self, effect: Effect) -> EffectReceipt:
        installation_operation_id, generation = _rollback_authority(effect)
        receipt = self._final_receipts.read(installation_operation_id)
        pointer = self._final_receipts.state(effect.root_id)
        existing = self._final_receipts.read(effect.operation_id)
        if existing is not None:
            if not _valid_rollback_receipt(effect, existing):
                raise PptMasterInstallationError("rollback final receipt drifted")
            self._final_receipts.mark_rolled_back(effect.root_id, installation_operation_id=str(existing["installation_operation_id"]), rollback_operation_id=effect.operation_id, generation=_receipt_int(existing, "next_generation"), activation_revision=_receipt_int(existing, "disabled_activation_revision"))
            return EffectReceipt(str(existing["receipt"]), ROLLBACK_RECEIPT_KIND, ROLLBACK_RECEIPT_SCHEMA, ROLLBACK_INTENT_SCHEMA)
        if receipt is None or receipt.get("project_id") != effect.root_id or pointer["status"] != "installed" or generation != pointer["generation"]:
            raise PptMasterInstallationError("rollback installation receipt drifted")
        registry = self._bindings.status()
        disabled = self._activation.disable(PPT_MASTER_PLUGIN_ID, expected_activation_revision=int(receipt["activation_revision"]), command_id=f"{effect.operation_id}.disable", confirm=True, reason="governed PPT Master rollback")
        binding = next((item for item in registry["bindings"] if item["project_id"] == effect.root_id and item["skill_id"] == PPT_MASTER_SKILL_ID), None)
        if binding is not None and binding["status"] == "active":
            self._bindings.deactivate(project_id=effect.root_id, skill_id=PPT_MASTER_SKILL_ID, expected_registry_revision=int(registry["registry_revision"]), confirm=True, reason="governed PPT Master rollback")
        projection = receipt.get("profile_projection")
        if not isinstance(projection, Mapping):
            raise PptMasterInstallationError("installation profile projection is invalid")
        profile = self._profiles.deactivate(effect.root_id, plugin_id=PPT_MASTER_PLUGIN_ID, skill_id=PPT_MASTER_SKILL_ID, operation_id=effect.operation_id, installation_projection=projection)
        rollback_receipt = _rollback_receipt_ref(effect.operation_id)
        next_generation = int(generation) + 1
        disabled_activation_revision = int(disabled["activation_revision"])
        self._final_receipts.write(effect.operation_id, {"receipt": rollback_receipt, "operation_id": effect.operation_id, "project_id": effect.root_id, "installation_operation_id": receipt["operation_id"], "installation_receipt": receipt["receipt"], "next_generation": next_generation, "disabled_activation_revision": disabled_activation_revision, "profile_projection": dict(profile)})
        self._final_receipts.mark_rolled_back(effect.root_id, installation_operation_id=str(receipt["operation_id"]), rollback_operation_id=effect.operation_id, generation=next_generation, activation_revision=disabled_activation_revision)
        return EffectReceipt(rollback_receipt, ROLLBACK_RECEIPT_KIND, ROLLBACK_RECEIPT_SCHEMA, ROLLBACK_INTENT_SCHEMA)

    def _probe_rollback(self, effect: Effect) -> tuple[EffectState, str | None]:
        receipt = self._final_receipts.read(effect.operation_id)
        pointer = self._final_receipts.state(effect.root_id)
        if (_valid_rollback_receipt(effect, receipt)
                and _pointer_matches_rollback(pointer, effect, receipt)):
            return EffectState.SETTLED_OK, str(receipt["receipt"])
        return EffectState.PLANNED, _rollback_recovery_evidence_ref(effect.operation_id)

    def _active_package(self):
        sources = self._activation.active_sources((PPT_MASTER_PLUGIN_ID,))
        package = ApplicationSkillCatalog().discover_selected(sources, (PPT_MASTER_SKILL_ID,)).get(PPT_MASTER_SKILL_ID)
        if package is None or package.source_kind != "plugin":
            raise PptMasterInstallationError("activated PPT Master Skill is unavailable for binding")
        return package

    def _write_thin_package(self, operation_id: str, commit: str) -> Path:
        root = self._package_root / operation_id
        if root.exists():
            if _thin_package_matches(root, commit):
                return root
            raise PptMasterInstallationError("thin plugin package is incomplete or drifted")
        temporary = self._package_root / f".{operation_id}.pending"
        if temporary.exists():
            if _thin_package_matches(temporary, commit):
                temporary.replace(root)
                return root
            shutil.rmtree(temporary)
        skill = temporary / "skills" / PPT_MASTER_SKILL_ID / "SKILL.md"
        skill.parent.mkdir(parents=True)
        manifest = temporary / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": PPT_MASTER_PLUGIN_ID, "version": "1.0.0", "description": "Host adapter for upstream hugohe3/ppt-master; fixed presentation capability only", "author": "Chriptmas OS host adapter", "repository": PPT_MASTER_URL, "license": "MIT", "keywords": ["ppt-master", "host-adapter"]}, sort_keys=True), encoding="utf-8")
        body = f"---\nname: ppt-master\ndescription: Fixed host-owned presentation capability\ntrigger_boundary: presentation\nvalidation: host-governed\nmaturity: verified\n---\n\nCopyright (c) 2025-2026 Hugo He. Upstream MIT project: `https://github.com/hugohe3/ppt-master` at commit `{commit}`. This thin host adapter does not copy upstream code. The host executes only the fixed reviewed entrypoint from the immutable managed artifact, using `presentation.pptx.fixed`.\n"
        if len(body.encode("utf-8")) > 16 * 1024:
            raise PptMasterInstallationError("thin Skill body exceeds its fixed limit")
        skill.write_text(body, encoding="utf-8")
        temporary.replace(root)
        return root


def _installation_revisions(
    commit: str, manifest_revision: str, generation: int,
    egress: NetworkEgressDecisionFact,
) -> Mapping[str, str]:
    if not _REVISION.fullmatch(commit) or not manifest_revision or generation < 0:
        raise PptMasterInstallationError("installation authority facts are invalid")
    digest = _egress_digest(egress.decision_revision)
    return _v2_revisions(
        policy=f"ppt-master-install-policy/v2.egress.{digest}",
        boundary=f"ppt-master-host-boundary/v1.egress.{digest}",
        capability="presentation.pptx.fixed/v1", context_manifest=manifest_revision,
        provider="github.com/hugohe3/ppt-master", bundle=f"github-{commit}",
        handler="ppt-master-install-handler/v2", workflow=f"ppt-master-install:g{generation}",
    )


def _rollback_revisions(receipt: Mapping[str, object], *, generation: int) -> Mapping[str, str]:
    operation = receipt.get("operation_id")
    commit = receipt.get("commit")
    manifest = receipt.get("manifest_revision")
    if (not isinstance(operation, str) or not re.fullmatch(r"eff2_[0-9a-f]{64}", operation)
            or not isinstance(commit, str) or not _REVISION.fullmatch(commit)
            or not isinstance(manifest, str) or not manifest.strip() or generation < 0):
        raise PptMasterInstallationError("rollback authority facts are invalid")
    return _v2_revisions(
        policy="ppt-master-rollback-policy/v2", boundary="ppt-master-host-boundary/v1",
        capability="presentation.pptx.fixed/v1", context_manifest=manifest,
        provider="not_applicable", bundle=f"rollback-{operation}",
        handler="ppt-master-rollback-handler/v2", workflow=f"ppt-master-rollback:g{generation}",
    )


def _egress_digest(decision_revision: str) -> str:
    match = re.fullmatch(r"network-egress-decision-v1\.([0-9a-f]{64})", decision_revision)
    if match is None:
        raise PptMasterInstallationError("network egress decision revision is invalid")
    return match.group(1)


def _egress_revision_from_effect(effect: Effect) -> str:
    policy = effect.rev_set.get("policy")
    boundary = effect.rev_set.get("boundary")
    policy_match = re.fullmatch(r"ppt-master-install-policy/v2\.egress\.([0-9a-f]{64})", policy or "")
    boundary_match = re.fullmatch(r"ppt-master-host-boundary/v1\.egress\.([0-9a-f]{64})", boundary or "")
    if policy_match is None or boundary_match is None or policy_match.group(1) != boundary_match.group(1):
        raise PptMasterInstallationError("installation network egress revision drifted")
    return f"network-egress-decision-v1.{policy_match.group(1)}"


def _v2_revisions(**values: str) -> Mapping[str, str]:
    result = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    result.update(values)
    if tuple(sorted(result)) != tuple(sorted(V2_REVISION_KEYS)):
        raise AssertionError("PPT Master v2 authority set is incomplete")
    return result


def _require_allowed_gate(value: object) -> GateAuthorization:
    if not isinstance(value, GateAuthorization) or not isinstance(value.decision_id, str) or not value.decision_id.strip():
        raise PptMasterInstallationError("trusted confirmation Gate did not return a durable decision fact")
    if value.fact.decision in {GateDecision.ASK, GateDecision.DENY}:
        transition = decide_workflow_transition(
            effect_states=(),
            boundaries=(WorkflowDecisionBoundary.PERMISSION_EXPANSION,),
            gate_outcome=(
                WorkflowGateOutcome.ASK
                if value.fact.decision is GateDecision.ASK
                else WorkflowGateOutcome.DENY
            ),
            intent_fingerprint=value.fact.decision_digest,
        )
        raise PptMasterWorkflowBoundaryError(
            "installation confirmation requires a governed workflow decision",
            transition.to_projection(),
        )
    if value.fact.decision is not GateDecision.ALLOW:
        raise PptMasterInstallationError("installation confirmation Gate decision is unsupported")
    return value


def _installation_authority(effect: Effect) -> tuple[str, str, int]:
    _require_effect_contract(effect, INSTALL_EFFECT_KIND, INSTALL_INTENT_SCHEMA, INSTALL_RECEIPT_KIND, INSTALL_RECEIPT_SCHEMA)
    revisions = effect.rev_set
    commit = revisions.get("bundle")
    manifest = revisions.get("context_manifest")
    workflow = revisions.get("workflow")
    if not isinstance(commit, str) or not commit.startswith("github-") or not _REVISION.fullmatch(commit[7:]) or not isinstance(manifest, str) or not isinstance(workflow, str):
        raise PptMasterInstallationError("installation Effect authority facts drifted")
    match = re.fullmatch(r"ppt-master-install:g([0-9]+)", workflow)
    if match is None:
        raise PptMasterInstallationError("installation generation authority drifted")
    return commit[7:], manifest, int(match.group(1))


def _rollback_authority(effect: Effect) -> tuple[str, int]:
    _require_effect_contract(effect, ROLLBACK_EFFECT_KIND, ROLLBACK_INTENT_SCHEMA, ROLLBACK_RECEIPT_KIND, ROLLBACK_RECEIPT_SCHEMA)
    bundle, workflow = effect.rev_set.get("bundle"), effect.rev_set.get("workflow")
    if not isinstance(bundle, str) or not bundle.startswith("rollback-") or not isinstance(workflow, str):
        raise PptMasterInstallationError("rollback Effect authority facts drifted")
    match = re.fullmatch(r"ppt-master-rollback:g([0-9]+)", workflow)
    if match is None:
        raise PptMasterInstallationError("rollback generation authority drifted")
    return bundle[9:], int(match.group(1))


def _require_effect_contract(effect: Effect, kind: str, intent_schema: str, receipt_kind: str, receipt_schema: str) -> None:
    if (effect.kind != kind or effect.effect_class is not EffectClass.QUERYABLE or effect.contract_version != EFFECT_V2
            or effect.intent_schema_version != intent_schema or effect.expected_receipt_kind != receipt_kind
            or effect.expected_receipt_schema_version != receipt_schema or tuple(sorted(effect.rev_set)) != tuple(sorted(V2_REVISION_KEYS))):
        raise PptMasterInstallationError("PPT Master Effect contract drifted")


def _compatibility_acquisition_effect(effect: Effect, commit: str) -> Effect:
    return replace(
        effect, kind="plugin_github_acquisition_stage", contract_version="legacy-v1",
        rev_set={"github_source_id": "ppt-master", "github_repository": "hugohe3/ppt-master", "github_revision": commit, "acquisition_contract_revision": "1"},
    )


def _project(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise PptMasterInstallationError("project id is invalid")
    return value


def _installation_receipt_ref(operation_id: str) -> str:
    return f"receipt:ppt-master-installation:{operation_id}"


def _rollback_receipt_ref(operation_id: str) -> str:
    return f"receipt:ppt-master-rollback:{operation_id}"


def _install_recovery_evidence_ref(operation_id: str) -> str:
    """Stable internal evidence for a receipt/pointer absence probe."""
    return f"facts:ppt-master-install-recovery:{operation_id}"


def _rollback_recovery_evidence_ref(operation_id: str) -> str:
    """Stable internal evidence for a rollback receipt/pointer absence probe."""
    return f"facts:ppt-master-rollback-recovery:{operation_id}"


def _identity(project: str, revision: str, manifest_revision: str) -> str:
    return "pptmaster-" + hashlib.sha256(f"{project}\0{revision}\0{manifest_revision}".encode()).hexdigest()[:40]


def _token(project: str, operation: str, revision: str, manifest_revision: str, risks: Sequence[str]) -> str:
    payload = json.dumps([project, operation, revision, manifest_revision, list(risks)], separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_runtime_self_manifest(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PptMasterInstallationError("runtime self manifest must be a mapping")
    revision = value.get("manifest_revision")
    compatibility = value.get("compatibility")
    entry = compatibility.get("ppt-master") if isinstance(compatibility, Mapping) else None
    if not isinstance(revision, str) or not revision.strip() or not isinstance(entry, Mapping) or entry.get("status") != "compatible":
        raise PptMasterInstallationError("runtime self manifest does not declare compatible PPT Master support")
    return {"revision": revision, "compatibility": {"ppt-master": {"status": "compatible"}}}


def _binding_revision(result: Mapping[str, object], project_id: str) -> int:
    nested = result.get("registry") if isinstance(result, Mapping) else None
    for registry in (nested, result):
        bindings = registry.get("bindings") if isinstance(registry, Mapping) else None
        if isinstance(bindings, Sequence):
            for binding in bindings:
                if isinstance(binding, Mapping) and binding.get("project_id") == project_id and binding.get("skill_id") == PPT_MASTER_SKILL_ID:
                    value = binding.get("binding_revision")
                    if isinstance(value, int) and not isinstance(value, bool):
                        return value
    value = result.get("binding_revision") if isinstance(result, Mapping) else None
    if isinstance(value, int):
        return value
    raise PptMasterInstallationError("Application Skill binding receipt is invalid")


def _receipt_int(receipt: Mapping[str, object], field: str) -> int:
    value = receipt.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PptMasterInstallationError("final receipt recovery field is invalid")
    return value


def _valid_installation_receipt(effect: Effect, receipt: Mapping[str, object] | None) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    try:
        return (
            set(receipt) == {
                "receipt", "operation_id", "project_id", "generation", "commit", "manifest_revision",
                "artifact_receipt", "plugin_id", "skill_id", "activation_revision", "binding_revision",
                "profile_projection",
            }
            and receipt.get("receipt") == _installation_receipt_ref(effect.operation_id)
            and receipt.get("operation_id") == effect.operation_id
            and receipt.get("project_id") == effect.root_id
            and receipt.get("commit") == _installation_authority(effect)[0]
            and receipt.get("manifest_revision") == _installation_authority(effect)[1]
            and receipt.get("artifact_receipt") == f"ppt-master-artifact:{effect.operation_id}"
            and receipt.get("plugin_id") == PPT_MASTER_PLUGIN_ID
            and receipt.get("skill_id") == PPT_MASTER_SKILL_ID
            and receipt.get("generation") == _installation_authority(effect)[2]
            and _receipt_int(receipt, "activation_revision") >= 0
            and _receipt_int(receipt, "binding_revision") >= 0
            and _valid_active_profile_projection(receipt.get("profile_projection"), effect.root_id)
        )
    except PptMasterInstallationError:
        return False


def _valid_rollback_receipt(effect: Effect, receipt: Mapping[str, object] | None) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    try:
        installation_operation_id, generation = _rollback_authority(effect)
        return (
            set(receipt) == {
                "receipt", "operation_id", "project_id", "installation_operation_id", "installation_receipt",
                "next_generation", "disabled_activation_revision", "profile_projection",
            }
            and receipt.get("receipt") == _rollback_receipt_ref(effect.operation_id)
            and receipt.get("operation_id") == effect.operation_id
            and receipt.get("project_id") == effect.root_id
            and receipt.get("installation_operation_id") == installation_operation_id
            and receipt.get("installation_receipt") == _installation_receipt_ref(installation_operation_id)
            and _receipt_int(receipt, "next_generation") == generation + 1
            and _receipt_int(receipt, "disabled_activation_revision") >= 0
            and _valid_inactive_profile_projection(receipt.get("profile_projection"), effect.root_id)
        )
    except PptMasterInstallationError:
        return False


def _valid_active_profile_projection(value: object, project_id: str) -> bool:
    required = {
        "project_id", "status", "owned_plugin_source", "owned_skill_id", "owned_plugin_id",
        "owned_tool_id", "profile_revision",
    }
    return (
        isinstance(value, Mapping) and set(value) == required and value.get("project_id") == project_id
        and value.get("status") == "active"
        and isinstance(value.get("profile_revision"), int) and not isinstance(value.get("profile_revision"), bool)
        and int(value["profile_revision"]) >= 0
        and all(isinstance(value.get(key), bool) for key in required if key.startswith("owned_"))
    )


def _valid_inactive_profile_projection(value: object, project_id: str) -> bool:
    return (
        isinstance(value, Mapping) and set(value) == {"project_id", "status", "profile_revision"}
        and value.get("project_id") == project_id and value.get("status") == "inactive"
        and isinstance(value.get("profile_revision"), int) and not isinstance(value.get("profile_revision"), bool)
        and int(value["profile_revision"]) >= 0
    )


def _pointer_matches_installation(
    pointer: Mapping[str, object], effect: Effect, receipt: Mapping[str, object] | None,
) -> bool:
    if not isinstance(pointer, Mapping) or not _valid_installation_receipt(effect, receipt):
        return False
    return (
        set(pointer) == {"project_id", "status", "generation", "activation_revision", "installation_operation_id"}
        and pointer.get("project_id") == effect.root_id
        and pointer.get("status") == "installed"
        and pointer.get("installation_operation_id") == effect.operation_id
        and pointer.get("generation") == receipt.get("generation")
        and pointer.get("activation_revision") == receipt.get("activation_revision")
        and _receipt_int(pointer, "generation") >= 0
        and _receipt_int(pointer, "activation_revision") >= 0
    )


def _pointer_matches_rollback(
    pointer: Mapping[str, object], effect: Effect, receipt: Mapping[str, object] | None,
) -> bool:
    if not isinstance(pointer, Mapping) or not _valid_rollback_receipt(effect, receipt):
        return False
    return (
        set(pointer) == {
            "project_id", "status", "generation", "activation_revision", "installation_operation_id",
            "rollback_operation_id",
        }
        and pointer.get("project_id") == effect.root_id
        and pointer.get("status") == "rolled_back"
        and pointer.get("rollback_operation_id") == effect.operation_id
        and pointer.get("installation_operation_id") == receipt.get("installation_operation_id")
        and pointer.get("generation") == receipt.get("next_generation")
        and pointer.get("activation_revision") == receipt.get("disabled_activation_revision")
        and _receipt_int(pointer, "generation") >= 0
        and _receipt_int(pointer, "activation_revision") >= 0
    )


def _thin_package_matches(root: Path, commit: str) -> bool:
    manifest = root / ".codex-plugin" / "plugin.json"
    skill = root / "skills" / PPT_MASTER_SKILL_ID / "SKILL.md"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        body = skill.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        return False
    files = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()))
    return (
        isinstance(payload, dict)
        and payload.get("name") == PPT_MASTER_PLUGIN_ID
        and payload.get("license") == "MIT"
        and f"commit `{commit}`" in body
        and "Copyright (c) 2025-2026 Hugo He" in body
        and "Upstream MIT project" in body
        and len(body.encode("utf-8")) <= 16 * 1024
        and files == (".codex-plugin/plugin.json", "skills/ppt-master/SKILL.md")
    )
