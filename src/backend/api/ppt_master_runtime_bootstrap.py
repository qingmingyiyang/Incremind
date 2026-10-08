"""Startup composition for the managed PPT Master installer and capability.

The composition owns every filesystem location and execution dependency.  HTTP
callers can name only the current installation receipt; they never supply a
source tree, interpreter, command, or presentation input.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from backend.api.ppt_master_capability_runtime import FixedPptxGenerationCapability
from backend.api.effect_partition_inventory import PPT_MASTER_EFFECT_PARTITION
from backend.api.ppt_master_installation_composition import build_ppt_master_installation_runtime
from backend.api.ppt_master_installation_runtime import (
    FinalInstallationReceiptStore,
    ImmutableArtifactStore,
    PptMasterInstallationError,
)
from backend.api.runtime_self_manifest_runtime import build_runtime_self_manifest_for_app
from core.plugin_host.presentation_artifact import (
    HostArtifactRoot,
    PresentationArtifactManifest,
    PresentationArtifactService,
)
from core.effect_log import EffectRuntime, build_effect_runtime
from core.product_core.workflow_handler_governance import (
    PPT_MASTER_WORKFLOW_HANDLER_KINDS,
    validate_workflow_handler_governance,
)

_PROJECT_ID = "default"
_EFFECT_OPERATION = re.compile(r"eff2_[0-9a-f]{64}")
_FEATURES = (
    "isolated-python-artifact",
    "plugin-runtime",
    "presentation-effect-runtime",
)


def build_ppt_master_effect_runtime(root_dir: Path, owner_id: str) -> EffectRuntime:
    """Build the isolated durable writer for PPT Master effects.

    Conversion may legitimately run for five minutes.  Keeping this database
    and lease separate from the primary one prevents the one-second recovery
    service from reclaiming a still-running conversion as a duplicate write.
    """
    root = Path(root_dir).resolve(strict=False)
    return build_effect_runtime(
        PPT_MASTER_EFFECT_PARTITION.database(root),
        owner_id=owner_id,
        lease_seconds=PPT_MASTER_EFFECT_PARTITION.lease_seconds,
        lease_heartbeat_seconds=PPT_MASTER_EFFECT_PARTITION.lease_heartbeat_seconds,
    )


def register_ppt_master_effect_recovery_partition(coordinator: object, application: object) -> bool:
    """Expose the PPT partition only after its durable handlers exist."""
    state = getattr(application, "state", None)
    runtime = getattr(state, "ppt_master_effect_runtime", None)
    capability = getattr(state, "ppt_master_capability", None)
    if not isinstance(runtime, EffectRuntime) or capability is None:
        return False
    register = getattr(coordinator, "register_partition", None)
    if not callable(register):
        raise TypeError("effect recovery coordinator is unavailable")
    register(PPT_MASTER_EFFECT_PARTITION.name, runtime)
    return True


class _UnavailableInstallationRuntime:
    """Public projection used when the bundled interpreter is absent.

    It intentionally exposes no writer and makes every installer endpoint
    return the domain's stable unavailable error instead of breaking app boot.
    """
    @staticmethod
    def _unavailable(*_args, **_kwargs):
        raise PptMasterInstallationError("isolated PPT Master runtime is unavailable")

    status = _unavailable
    preview = _unavailable
    confirm = _unavailable
    rollback = _unavailable


def bootstrap_ppt_master_runtime(application: object, *, root_dir: Path, packaged: bool) -> None:
    """Attach governed PPT services before Effect recovery is allowed to run."""
    root = Path(root_dir).resolve(strict=False)
    data = root / ".rebuild-data"
    interpreter = root / "runtime" / "python.exe"
    interpreter_available = interpreter.is_file() and not interpreter.is_symlink()
    manifest = build_runtime_self_manifest_for_app(
        root,
        packaged=packaged,
        resources_root=root / "config",
        app_data_root=data,
        runtime_source="bundled" if interpreter_available else "system",
        # PPT Master needs this specific isolated interpreter.  Reporting a
        # generic process Python as usable would let installation pass then
        # fail only when the write Effect is dispatched.
        runtime_available=interpreter_available,
        features=_FEATURES,
    )
    application.state.runtime_self_manifest = manifest
    compatibility = manifest.get("compatibility") if isinstance(manifest, Mapping) else None
    ppt_compatibility = compatibility.get("ppt-master") if isinstance(compatibility, Mapping) else None
    manifest_compatible = (
        isinstance(ppt_compatibility, Mapping)
        and ppt_compatibility.get("status") == "compatible"
        and isinstance(manifest.get("manifest_revision"), str)
        and bool(str(manifest["manifest_revision"]).strip())
    )
    ready = interpreter_available and manifest_compatible
    effect_runtime = getattr(application.state, "ppt_master_effect_runtime", None)
    if effect_runtime is None:
        effect_runtime = application.state.effect_runtime
    if not ready:
        application.state.ppt_master_installation_runtime = _UnavailableInstallationRuntime()
        application.state.ppt_master_capability = None
        application.state.ppt_master_smoke = None
        return
    installation = build_ppt_master_installation_runtime(
        root_dir=root,
        effect_runtime=effect_runtime,
        runtime_self_manifest=manifest,
    )
    receipts = FinalInstallationReceiptStore(data / "ppt-master-installation-receipts")
    artifacts = ImmutableArtifactStore(data / "ppt-master-artifacts")
    jobs = data / "ppt-master-jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    if jobs.is_symlink() or not jobs.is_dir():
        raise PptMasterInstallationError("controlled PPT Master jobs root is unavailable")

    def resolve_artifact(project_id: str) -> tuple[PresentationArtifactService, PresentationArtifactManifest]:
        state = receipts.state(project_id)
        operation_id = state.get("installation_operation_id")
        if state.get("status") != "installed" or not isinstance(operation_id, str):
            raise PptMasterInstallationError("PPT Master is not installed for this project")
        receipt = receipts.read(operation_id)
        if not _current_receipt(project_id, operation_id, state, receipt):
            raise PptMasterInstallationError("PPT Master installation receipt is invalid")
        commit = str(receipt["commit"])
        if artifacts.probe(operation_id) != receipt.get("artifact_receipt"):
            raise PptMasterInstallationError("PPT Master installation artifact receipt is invalid")
        source = artifacts.source_tree(operation_id)
        if not ready:
            raise PptMasterInstallationError("isolated PPT Master runtime is unavailable")
        service = PresentationArtifactService(
            roots={"ppt-master-root": HostArtifactRoot(source, True)},
            jobs_root=jobs.resolve(strict=True),
            python_executable=interpreter.resolve(strict=True),
        )
        return service, PresentationArtifactManifest(
            artifact_id="ppt-master-managed",
            version="github-" + commit[:12],
            source_commit=commit,
            root_locator="ppt-master-root",
        )

    capability = FixedPptxGenerationCapability(
        effect_runtime=effect_runtime,
        jobs_root=jobs.resolve(strict=True),
        clock=lambda: __import__("time").time_ns() // 1_000_000_000,
        artifact_resolver=resolve_artifact,
    )
    validate_workflow_handler_governance(
        effect_runtime.handlers.kinds(),
        expected_kinds=PPT_MASTER_WORKFLOW_HANDLER_KINDS,
        allow_missing=True,
    )

    def smoke(installation_operation_id: str) -> bool:
        current = installation.status(_PROJECT_ID)
        if current.get("state") != "installed" or current.get("operation_id") != installation_operation_id:
            raise PptMasterInstallationError("installation receipt drifted")
        request_id = "ppt-master-smoke-" + hashlib.sha256(installation_operation_id.encode("utf-8")).hexdigest()[:32]
        result = capability.invoke({
            "turn_id": "ppt-master-smoke-turn",
            "scope": {"project_id": _PROJECT_ID},
            "authorization_facts_ref": "facts:ppt-master-smoke/" + request_id,
            "authorization_facts_revision": "ppt-master-smoke-v1",
            "arguments": {
                "schema_version": "1.0.0",
                "operation_id": request_id,
                "slides": [{"title": "PPT Master Smoke", "body": "Managed artifact verification"}],
            },
        })
        output = result.get("result") if isinstance(result, Mapping) else None
        return isinstance(output, Mapping) and output.get("slide_count") == 1

    application.state.ppt_master_installation_runtime = installation
    application.state.ppt_master_capability = capability
    application.state.ppt_master_smoke = smoke


def _current_receipt(project_id: str, operation_id: str, pointer: Mapping[str, object], receipt: Mapping[str, object] | None) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    commit = receipt.get("commit")
    generation, activation_revision = receipt.get("generation"), receipt.get("activation_revision")
    profile = receipt.get("profile_projection")
    return (
        set(receipt) == {"receipt", "operation_id", "project_id", "generation", "commit", "manifest_revision", "artifact_receipt", "plugin_id", "skill_id", "activation_revision", "binding_revision", "profile_projection"}
        and bool(_EFFECT_OPERATION.fullmatch(operation_id))
        and receipt.get("receipt") == f"receipt:ppt-master-installation:{operation_id}"
        and receipt.get("operation_id") == operation_id
        and receipt.get("project_id") == project_id
        and isinstance(commit, str)
        and len(commit) == 40
        and all(character in "0123456789abcdef" for character in commit)
        and isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0
        and generation == pointer.get("generation")
        and isinstance(activation_revision, int) and not isinstance(activation_revision, bool) and activation_revision >= 0
        and activation_revision == pointer.get("activation_revision")
        and isinstance(receipt.get("artifact_receipt"), str)
        and receipt.get("artifact_receipt") == f"ppt-master-artifact:{operation_id}"
        and isinstance(receipt.get("manifest_revision"), str) and bool(str(receipt.get("manifest_revision")).strip())
        and isinstance(receipt.get("binding_revision"), int) and not isinstance(receipt.get("binding_revision"), bool) and int(receipt["binding_revision"]) >= 0
        and _valid_active_profile_projection(profile, project_id)
        and receipt.get("plugin_id") == "ppt-master"
        and receipt.get("skill_id") == "ppt-master"
    )


def _valid_active_profile_projection(value: object, project_id: str) -> bool:
    """Freeze the ownership facts needed for a safe later rollback."""
    required = {
        "project_id", "status", "profile_revision", "owned_plugin_source",
        "owned_skill_id", "owned_plugin_id", "owned_tool_id",
    }
    return (
        isinstance(value, Mapping)
        and set(value) == required
        and value.get("project_id") == project_id
        and value.get("status") == "active"
        and isinstance(value.get("profile_revision"), int)
        and not isinstance(value.get("profile_revision"), bool)
        and int(value["profile_revision"]) >= 0
        and all(isinstance(value.get(key), bool) for key in required if key.startswith("owned_"))
    )
