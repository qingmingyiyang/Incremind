"""Production composition for the governed PPT Master installer.

This module owns only fixed adapters.  It never accepts a repository URL,
archive location, executable, or command line from an HTTP request.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from backend.security.network_adapter import LoopbackHttpConnectProxy, SafeBinaryDownloadAdapter, SafeTextNetworkAdapter
from backend.security.network_egress_decision import (
    NetworkEgressDecisionError,
    NetworkEgressDecisionStore,
)
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.api.ppt_master_capability_runtime import ppt_master_fixed_capability_definition
from core.ai_tooling import ToolSelectionBinding, tool_contract_binding_identity
from core.application_skill.binding_registry import ApplicationSkillBindingRegistry
from core.effect_log.core import GateDecision, GateDecisionFact

if TYPE_CHECKING:
    from backend.api.ppt_master_installation_runtime import PptMasterInstallationRuntime


_GITHUB_SHA = re.compile(r"^[0-9a-f]{40}$")


def _installation_error(message: str) -> Exception:
    # Avoid importing the Effect runtime merely to exercise the pure profile
    # projection in focused tests.  Production invokes this only after Core is
    # available and receives the domain's stable error type.
    from backend.api.ppt_master_installation_runtime import PptMasterInstallationError
    return PptMasterInstallationError(message)


class _GitHubHeadResolver:
    def __init__(self, *, connect_proxy: LoopbackHttpConnectProxy | None = None) -> None:
        self._client = SafeTextNetworkAdapter(
            allowed_hosts=("api.github.com",), max_redirects=0, max_response_bytes=128 * 1024,
            connect_proxy=connect_proxy,
        )

    def __call__(self, repository: str) -> str:
        if repository != "hugohe3/ppt-master":
            raise _installation_error("repository is outside the installation allowlist")
        try:
            payload = json.loads(self._client.fetch_text("https://api.github.com/repos/hugohe3/ppt-master/commits/HEAD"))
            revision = payload.get("sha") if isinstance(payload, Mapping) else None
        except Exception as error:
            raise _installation_error("GitHub revision lookup is unavailable") from error
        if not isinstance(revision, str) or not _GITHUB_SHA.fullmatch(revision):
            raise _installation_error("GitHub revision lookup returned an invalid revision")
        return revision


class _ArchiveDownloader:
    def __init__(self, root: Path, *, connect_proxy: LoopbackHttpConnectProxy | None = None) -> None:
        self._root = root.resolve(strict=False)
        self._downloads = SafeBinaryDownloadAdapter(
            self._root, allowed_hosts=("github.com", "codeload.github.com"), max_redirects=1,
            connect_proxy=connect_proxy,
        )

    def download(self, url: str, destination: Path, *, max_bytes: int) -> Path:
        # The staging handler supplies an adapter-owned destination.  Convert
        # it to the adapter's relative namespace, never accept a caller path.
        relative = destination.resolve(strict=False).relative_to(self._root).as_posix()
        item = self._downloads.download(url, relative_path=relative, max_response_bytes=max_bytes)
        if item.path.resolve(strict=False) != destination.resolve(strict=False):
            raise ValueError("binary downloader destination drifted")
        return item.path


class _OperationScopedGitHubAcquisition:
    """Resolve the immutable egress fact from the Effect on every recovery."""

    def __init__(
        self, *, staging_root: Path, decisions: NetworkEgressDecisionStore,
        artifacts, extractor,
    ) -> None:
        self._staging_root = Path(staging_root).resolve(strict=False)
        self._decisions = decisions
        self._artifacts = artifacts
        self._extractor = extractor

    def stage(self, effect) -> str:
        from backend.api.ppt_master_installation_runtime import (
            _compatibility_acquisition_effect,
            _egress_revision_from_effect,
        )
        from core.plugin_host.github_acquisition import GitHubAcquisitionStagingHandler
        try:
            fact = self._decisions.get(_egress_revision_from_effect(effect))
        except NetworkEgressDecisionError as error:
            raise _installation_error("installation network egress authority is unavailable") from error
        bundle = effect.rev_set.get("bundle")
        if (
            fact.scope_ref != f"scope:project/{effect.root_id}"
            or not isinstance(bundle, str) or bundle != f"github-{fact.source_revision}"
        ):
            raise _installation_error("installation network egress authority drifted")
        compatibility_effect = _compatibility_acquisition_effect(effect, fact.source_revision)
        return GitHubAcquisitionStagingHandler(
            staging_root=self._staging_root,
            downloader=_ArchiveDownloader(
                self._staging_root, connect_proxy=fact.connect_proxy(),
            ),
            extractor=self._extractor,
            artifact_store=self._artifacts,
        ).stage(compatibility_effect)


class _PptMasterConfirmationGate:
    """Server-owned approval fact for an already exact UI confirmation.

    The route exposes only ``confirm: true`` after preview-token validation;
    policy, rule, scope and decision identity are host facts, never request
    fields.  EffectLog durably freezes the returned fact with the intent.
    """

    def __call__(self, *, project_id: str, operation_id: str, risks, policy_revision: str):
        from backend.api.ppt_master_installation_runtime import GateAuthorization
        if not isinstance(project_id, str) or not isinstance(operation_id, str) or not isinstance(policy_revision, str):
            raise _installation_error("trusted PPT Master Gate input is invalid")
        return GateAuthorization(
            decision_id=f"gate:ppt-master:{operation_id}",
            fact=GateDecisionFact(
                decision=GateDecision.ALLOW, rule_ref="rule:ppt-master-self-install/v2",
                scope_ref=f"scope:project/{project_id}", budget_after={},
                secret_scope="scope:local-ppt-master", policy_revision=policy_revision,
            ),
        )


class _ProfileProjector:
    def __init__(self, root: Path) -> None:
        self._store = ProjectCapabilityProfileStore(root)
        requested = Path(root).expanduser().absolute()
        _reject_profile_links(requested)
        self._steps_root = requested.resolve(strict=False) / ".rebuild-data" / "ppt-master-profile-steps"

    def activate(self, project_id: str, *, plugin_id: str, skill_id: str, operation_id: str) -> Mapping[str, object]:
        _require_project_id(project_id)
        subject = {"plugin_id": plugin_id, "skill_id": skill_id}
        existing = self._existing_step(project_id, operation_id, "activate", subject)
        if existing is not None:
            return self._resume_step(existing)
        snapshot = self._store.get(project_id)
        profile = snapshot.profile
        owned_plugin_source = "plugin" not in profile.enabled_sources
        owned_skill_id = skill_id not in profile.enabled_skill_ids
        owned_plugin_id = plugin_id not in profile.enabled_plugin_ids
        owned_tool_id = "presentation.pptx.fixed" not in profile.allowed_tool_ids
        binding = _ppt_master_tool_binding()
        existing_binding = next(
            (item for item in profile.tool_selection_bindings if item.stable_id == binding.stable_id),
            None,
        )
        if owned_tool_id and existing_binding is not None:
            raise _installation_error("unselected PPT Master tool already has a selection binding")
        if not owned_tool_id and existing_binding != binding:
            raise _installation_error("existing PPT Master tool selection binding drifted")
        before = _selection_state(profile)
        after = _selection_state(
            profile,
            enabled_sources=tuple(dict.fromkeys((*profile.enabled_sources, "plugin"))),
            enabled_skill_ids=tuple(dict.fromkeys((*profile.enabled_skill_ids, skill_id))),
            enabled_plugin_ids=tuple(dict.fromkeys((*profile.enabled_plugin_ids, plugin_id))),
            allowed_tool_ids=tuple(dict.fromkeys((*profile.allowed_tool_ids, "presentation.pptx.fixed"))),
            tool_selection_bindings=(
                (*profile.tool_selection_bindings, binding)
                if owned_tool_id else profile.tool_selection_bindings
            ),
        )
        projection = {
            "project_id": project_id, "status": "active",
            "owned_plugin_source": owned_plugin_source, "owned_skill_id": owned_skill_id,
            "owned_plugin_id": owned_plugin_id, "owned_tool_id": owned_tool_id,
        }
        return self._apply_step(project_id, operation_id, "activate", subject, before, after, projection)

    def deactivate(
        self, project_id: str, *, plugin_id: str, skill_id: str, operation_id: str,
        installation_projection: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        _require_project_id(project_id)
        projection = installation_projection if isinstance(installation_projection, Mapping) else {}
        subject = {"plugin_id": plugin_id, "skill_id": skill_id}
        existing = self._existing_step(project_id, operation_id, "deactivate", subject)
        if existing is not None:
            return self._resume_step(existing)
        snapshot = self._store.get(project_id)
        profile = snapshot.profile
        if not _valid_installation_projection(projection, project_id):
            raise _installation_error("installation profile ownership projection is invalid")
        remove_skill = projection["owned_skill_id"]
        remove_plugin = projection["owned_plugin_id"]
        remove_tool = projection["owned_tool_id"]
        remove_source = projection["owned_plugin_source"]
        enabled_plugins = tuple(item for item in profile.enabled_plugin_ids if not (remove_plugin and item == plugin_id))
        enabled_sources = tuple(item for item in profile.enabled_sources if not (remove_source and item == "plugin" and not enabled_plugins))
        before = _selection_state(profile)
        after = _selection_state(
            profile, enabled_sources=enabled_sources,
            enabled_skill_ids=tuple(item for item in profile.enabled_skill_ids if not (remove_skill and item == skill_id)),
            enabled_plugin_ids=enabled_plugins,
            allowed_tool_ids=tuple(item for item in profile.allowed_tool_ids if not (remove_tool and item == "presentation.pptx.fixed")),
            tool_selection_bindings=tuple(
                item for item in profile.tool_selection_bindings
                if not (remove_tool and item.stable_id == "presentation.pptx.fixed")
            ),
        )
        return self._apply_step(
            project_id, operation_id, "deactivate", subject, before, after,
            {"project_id": project_id, "status": "inactive"},
        )

    def _apply_step(
        self, project_id: str, operation_id: str, action: str, subject: Mapping[str, object], before: Mapping[str, object],
        after: Mapping[str, object], projection: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Persist a host-owned immutable step before its one permitted CAS."""
        _require_operation_id(operation_id)
        frozen = {
            "operation_id": operation_id, "project_id": project_id, "action": action, "subject": dict(subject),
            "before": dict(before), "after": dict(after), "projection": dict(projection),
        }
        path = self._step_path(operation_id)
        existing = _read_profile_step(path)
        if existing is None:
            _write_profile_step(path, frozen)
            existing = frozen
        if existing != frozen or not _valid_profile_step(existing):
            raise _installation_error("profile operation receipt drifted")
        return self._resume_step(existing)

    def _existing_step(self, project_id: str, operation_id: str, action: str, subject: Mapping[str, object]) -> Mapping[str, object] | None:
        _require_operation_id(operation_id)
        existing = _read_profile_step(self._step_path(operation_id))
        if existing is None:
            return None
        if (not _valid_profile_step(existing) or existing.get("project_id") != project_id
                or existing.get("action") != action or existing.get("subject") != dict(subject)):
            raise _installation_error("profile operation receipt drifted")
        return existing

    def _resume_step(self, existing: Mapping[str, object]) -> Mapping[str, object]:
        project_id = str(existing["project_id"])
        snapshot = self._store.get(project_id)
        current = _selection_state(snapshot.profile)
        if current == existing["after"]:
            return {**dict(existing["projection"]), "profile_revision": snapshot.store_revision}
        if current != existing["before"]:
            raise _installation_error("profile operation state conflicts with immutable receipt")
        updated = _update_selection(self._store, project_id, snapshot, existing["after"])
        return {**dict(existing["projection"]), "profile_revision": updated.store_revision}

    def _step_path(self, operation_id: str) -> Path:
        _reject_profile_links(self._steps_root)
        candidate = self._steps_root / f"{operation_id}.json"
        if candidate.parent != self._steps_root:
            raise _installation_error("profile operation path is invalid")
        return candidate


_SELECTION_KEYS = (
    "enabled_sources", "enabled_skill_ids", "enabled_plugin_ids", "allowed_tool_ids",
    "tool_selection_bindings",
)


def _selection_state(profile, **changes) -> dict[str, object]:
    values = {
        "enabled_sources": profile.enabled_sources,
        "enabled_skill_ids": profile.enabled_skill_ids,
        "enabled_plugin_ids": profile.enabled_plugin_ids,
        "allowed_tool_ids": profile.allowed_tool_ids,
        "tool_selection_bindings": tuple(_binding_state(item) for item in profile.tool_selection_bindings),
    }
    values.update(changes)
    state = {key: list(values[key]) for key in _SELECTION_KEYS[:-1]}
    state["tool_selection_bindings"] = [
        _binding_state(item) if isinstance(item, ToolSelectionBinding) else dict(item)
        for item in values["tool_selection_bindings"]
    ]
    if not _valid_selection_state(state):
        raise _installation_error("profile selection state is invalid")
    return state


def _update_selection(store: ProjectCapabilityProfileStore, project_id: str, snapshot, selection: Mapping[str, object]):
    profile = snapshot.profile
    return store.update(
        project_id, expected_revision=snapshot.store_revision,
        boundary_profile_id=profile.boundary_profile_id,
        boundary_profile_revision=profile.boundary_profile_revision,
        enabled_sources=tuple(selection["enabled_sources"]),
        enabled_skill_ids=tuple(selection["enabled_skill_ids"]),
        enabled_plugin_ids=tuple(selection["enabled_plugin_ids"]),
        enabled_mcp_server_ids=profile.enabled_mcp_server_ids,
        allowed_tool_ids=tuple(selection["allowed_tool_ids"]),
        denied_tool_ids=profile.denied_tool_ids,
        preferred_model_tier=profile.preferred_model_tier, memory_scope=profile.memory_scope,
        cross_project_grant_ids=profile.cross_project_grant_ids,
        output_style_profile_id=profile.output_style_profile_id,
        max_tools=profile.max_tools, max_tool_descriptor_bytes=profile.max_tool_descriptor_bytes,
        tool_discovery_policy=profile.tool_discovery_policy,
        tool_selection_bindings=_selection_bindings(selection["tool_selection_bindings"]),
        mcp_server_selection_bindings=profile.mcp_server_selection_bindings,
    )


def _require_operation_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"eff2_[0-9a-f]{64}", value):
        raise _installation_error("profile operation id is invalid")


def _require_project_id(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise _installation_error("profile project id is invalid")


def _valid_selection_state(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != set(_SELECTION_KEYS):
        return False
    for key in _SELECTION_KEYS[:-1]:
        items = value[key]
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", item)
            for item in items
        ) or len(items) != len(set(items)):
            return False
    bindings = value["tool_selection_bindings"]
    if not isinstance(bindings, list):
        return False
    try:
        decoded = _selection_bindings(bindings)
    except Exception:
        return False
    return len(decoded) == len(bindings) and len({item.stable_id for item in decoded}) == len(decoded)


def _ppt_master_tool_binding() -> ToolSelectionBinding:
    definition = ppt_master_fixed_capability_definition()
    tool = definition.tool_definition
    if tool is None:
        raise _installation_error("PPT Master tool contract is unavailable")
    return ToolSelectionBinding(tool.tool_id, tool_contract_binding_identity(tool))


def _binding_state(binding: ToolSelectionBinding) -> dict[str, str]:
    return {"stable_id": binding.stable_id, "contract_identity": binding.contract_identity}


def _selection_bindings(value: object) -> tuple[ToolSelectionBinding, ...]:
    if not isinstance(value, (list, tuple)):
        raise _installation_error("tool selection binding state is invalid")
    result: list[ToolSelectionBinding] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"stable_id", "contract_identity"}:
            raise _installation_error("tool selection binding state is invalid")
        result.append(ToolSelectionBinding(str(item["stable_id"]), str(item["contract_identity"])))
    return tuple(result)


def _valid_installation_projection(value: object, project_id: str) -> bool:
    required = {"project_id", "status", "owned_plugin_source", "owned_skill_id", "owned_plugin_id", "owned_tool_id"}
    permitted = required | {"profile_revision"}
    revision = value.get("profile_revision") if isinstance(value, Mapping) else None
    return (isinstance(value, Mapping) and set(value).issubset(permitted) and required.issubset(value)
            and (revision is None or isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0)
            and value.get("project_id") == project_id
            and value.get("status") == "active"
            and all(isinstance(value[key], bool) for key in required if key.startswith("owned_")))


def _reject_profile_links(root: Path) -> None:
    for item in (root, *root.parents):
        if item.exists() and item.is_symlink():
            raise _installation_error("profile operation path cannot cross a link")


def _valid_profile_step(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"operation_id", "project_id", "action", "subject", "before", "after", "projection"}:
        return False
    try:
        _require_operation_id(value["operation_id"])
    except Exception:
        return False
    try:
        _require_project_id(value["project_id"])
    except Exception:
        return False
    if value["action"] not in {"activate", "deactivate"}:
        return False
    if not isinstance(value["subject"], Mapping) or set(value["subject"]) != {"plugin_id", "skill_id"} or any(not isinstance(item, str) or not item for item in value["subject"].values()):
        return False
    for field in ("before", "after"):
        if not _valid_selection_state(value[field]):
            return False
    projection = value["projection"]
    required = ({"project_id", "status", "owned_plugin_source", "owned_skill_id", "owned_plugin_id", "owned_tool_id"}
                if value["action"] == "activate" else {"project_id", "status"})
    return (isinstance(projection, Mapping) and set(projection) == required and projection.get("project_id") == value["project_id"]
            and projection.get("status") == ("active" if value["action"] == "activate" else "inactive")
            and (value["action"] != "activate" or _valid_installation_projection(projection, str(value["project_id"]))))


def _read_profile_step(path: Path) -> Mapping[str, object] | None:
    if path.is_symlink():
        raise _installation_error("profile operation receipt cannot be a link")
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _installation_error("profile operation receipt is unreadable") from error
    return payload if isinstance(payload, Mapping) else None


def _write_profile_step(path: Path, payload: Mapping[str, object]) -> None:
    if not _valid_profile_step(payload):
        raise _installation_error("profile operation receipt is invalid")
    _reject_profile_links(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.stem}.pending"
    if temporary.is_symlink():
        raise _installation_error("profile operation pending receipt cannot be a link")
    pending = _read_profile_step(temporary)
    if pending is not None:
        if pending != payload:
            raise _installation_error("profile operation pending receipt drifted")
        temporary.replace(path)
        return
    temporary.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def build_ppt_master_installation_runtime(*, root_dir: Path, effect_runtime, runtime_self_manifest: Mapping[str, object]) -> "PptMasterInstallationRuntime":
    """Compose the fixed installer once application startup has built Effects."""
    from backend.api.plugin_runtime import build_plugin_package_intake, build_plugin_skill_activation
    from backend.api.ppt_master_installation_runtime import (
        FinalInstallationReceiptStore, ImmutableArtifactStore, PptMasterInstallationRuntime,
    )
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from core.plugin_host.github_acquisition import GitHubAcquisitionStagingHandler, SafeZipExtractor
    root = Path(root_dir).resolve(strict=False)
    data = root / ".rebuild-data"
    # The adapter and staging handler share this exact root, making the binary
    # destination check above a containment assertion rather than a copy.
    acquisition_staging = data / "ppt-master-acquisition-staging"
    egress_decisions = NetworkEgressDecisionStore(data / "ppt-master-egress-decisions")
    artifacts = ImmutableArtifactStore(data / "ppt-master-artifacts")
    objects, _settings = build_rebuild_object_store(root)
    return PptMasterInstallationRuntime(
        effect_runtime=effect_runtime,
        resolver=_GitHubHeadResolver(), runtime_self_manifest=runtime_self_manifest,
        gate=_PptMasterConfirmationGate(),
        acquisition=_OperationScopedGitHubAcquisition(
            staging_root=acquisition_staging, decisions=egress_decisions,
            extractor=SafeZipExtractor(), artifacts=artifacts,
        ),
        artifacts=artifacts,
        intake=build_plugin_package_intake(root), activation=build_plugin_skill_activation(root),
        bindings=ApplicationSkillBindingRegistry(objects), profiles=_ProfileProjector(root),
        clock=lambda: int(datetime.now(timezone.utc).timestamp()),
        package_root=data / "plugin-package-inbox",
        final_receipts=FinalInstallationReceiptStore(data / "ppt-master-installation-receipts"),
        egress_decisions=egress_decisions,
    )
