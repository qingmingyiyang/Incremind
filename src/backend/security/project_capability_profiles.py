from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
from threading import Lock

from backend.shared.filesystem import atomic_write_text
from backend.shared.interprocess_lock import interprocess_file_lock
from core.ai_tooling import (
    MCPServerSelectionBinding,
    ProjectCapabilityProfile,
    ToolSelectionBinding,
)
from core.ai_tooling.project_profile import MAX_MODEL_VISIBLE_TOOLS
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence


_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SCHEMA_VERSION = "1.3.0"
_FIELDS = frozenset({
    "schema_version", "profile_id", "project_id", "revision", "boundary_profile_id",
    "boundary_profile_revision",
    "enabled_sources", "enabled_skill_ids", "enabled_plugin_ids", "enabled_mcp_server_ids",
    "allowed_tool_ids", "denied_tool_ids", "preferred_model_tier", "memory_scope",
    "cross_project_grant_ids", "output_style_profile_id", "max_tools",
    "max_tool_descriptor_bytes",
    "tool_discovery_policy",
    "tool_selection_bindings",
    "mcp_server_selection_bindings",
})
_V12_FIELDS = _FIELDS - {"mcp_server_selection_bindings"}
_V11_FIELDS = _V12_FIELDS - {"tool_discovery_policy", "tool_selection_bindings"}
_LEGACY_FIELDS = _V11_FIELDS - {"boundary_profile_revision"}
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "cookies", "password", "secret",
    "token", "local_path", "windows_path",
})
_PATH_LOCKS: dict[Path, Lock] = {}
_PATH_LOCKS_GUARD = Lock()


class ProjectCapabilityProfileStoreError(ValueError):
    pass


class ProjectCapabilityProfileConflict(ProjectCapabilityProfileStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectCapabilityProfileSnapshot:
    profile: ProjectCapabilityProfile
    store_revision: int
    persisted: bool
    compatibility_diagnostics: tuple[str, ...] = ()


class ProjectCapabilityProfileStore:
    """CAS authority for a project's sparse capability configuration."""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir.resolve()

    def get(self, project_id: str) -> ProjectCapabilityProfileSnapshot:
        project = _project_id(project_id)
        path = self._path(project)
        # Atomic replacement makes a read coherent without creating lock state.
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path):
            if not path.exists():
                return ProjectCapabilityProfileSnapshot(_default_profile(project), 0, False)
            payload = _read_json(path)
            profile = _decode(payload, expected_project_id=project)
            return ProjectCapabilityProfileSnapshot(
                profile,
                profile.revision,
                True,
                _compatibility_diagnostics(payload),
            )

    def update(
        self,
        project_id: str,
        *,
        expected_revision: int,
        boundary_profile_id: str,
        boundary_profile_revision: int,
        enabled_sources: tuple[str, ...] = ("core",),
        enabled_skill_ids: tuple[str, ...] = (),
        enabled_plugin_ids: tuple[str, ...] = (),
        enabled_mcp_server_ids: tuple[str, ...] = (),
        allowed_tool_ids: tuple[str, ...] = (),
        denied_tool_ids: tuple[str, ...] = (),
        preferred_model_tier: str = "standard",
        memory_scope: str = "project_only",
        cross_project_grant_ids: tuple[str, ...] = (),
        output_style_profile_id: str | None = None,
        max_tools: int = MAX_MODEL_VISIBLE_TOOLS,
        max_tool_descriptor_bytes: int = 32 * 1024,
        tool_discovery_policy: str = "auto_discover",
        tool_selection_bindings: tuple[ToolSelectionBinding, ...] | None = None,
        mcp_server_selection_bindings: tuple[MCPServerSelectionBinding, ...] | None = None,
    ) -> ProjectCapabilityProfileSnapshot:
        project = _project_id(project_id)
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current_revision = 0
            current_tool_bindings: tuple[ToolSelectionBinding, ...] = ()
            current_mcp_bindings: tuple[MCPServerSelectionBinding, ...] = ()
            if path.exists():
                current = _decode(_read_json(path), expected_project_id=project)
                current_revision = current.revision
                current_tool_bindings = current.tool_selection_bindings
                current_mcp_bindings = current.mcp_server_selection_bindings
            if expected_revision != current_revision:
                raise ProjectCapabilityProfileConflict(
                    f"project capability profile revision conflict: expected {expected_revision}, current {current_revision}"
                )
            profile = ProjectCapabilityProfile(
                profile_id=f"project-capability-{project}",
                project_id=project,
                revision=current_revision + 1,
                boundary_profile_id=boundary_profile_id,
                boundary_profile_revision=boundary_profile_revision,
                enabled_sources=enabled_sources,
                enabled_skill_ids=enabled_skill_ids,
                enabled_plugin_ids=enabled_plugin_ids,
                enabled_mcp_server_ids=enabled_mcp_server_ids,
                allowed_tool_ids=allowed_tool_ids,
                denied_tool_ids=denied_tool_ids,
                preferred_model_tier=preferred_model_tier,  # type: ignore[arg-type]
                memory_scope=memory_scope,  # type: ignore[arg-type]
                cross_project_grant_ids=cross_project_grant_ids,
                output_style_profile_id=output_style_profile_id,
                max_tools=max_tools,
                max_tool_descriptor_bytes=max_tool_descriptor_bytes,
                tool_discovery_policy=tool_discovery_policy,  # type: ignore[arg-type]
                tool_selection_bindings=(
                    current_tool_bindings
                    if tool_selection_bindings is None
                    else tool_selection_bindings
                ),
                mcp_server_selection_bindings=(
                    current_mcp_bindings
                    if mcp_server_selection_bindings is None
                    else mcp_server_selection_bindings
                ),
            )
            payload = _encode(profile)
            _reject_sensitive(payload)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            return ProjectCapabilityProfileSnapshot(profile, profile.revision, True)

    def migrate_boundary_binding(
        self,
        project_id: str,
        *,
        expected_revision: int,
        boundary_profile_id: str,
        boundary_profile_revision: int,
    ) -> ProjectCapabilityProfileSnapshot:
        """Explicitly re-attest a legacy profile against a reviewed Boundary revision.

        Legacy files are never read as if they were bound to the current
        Boundary.  This migration is intentionally limited to adding the
        binding and advancing the profile revision; it cannot alter the old
        capability selection at the same time.
        """
        project = _project_id(project_id)
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            if not path.exists():
                raise ProjectCapabilityProfileStoreError(
                    "legacy project capability profile was not found"
                )
            legacy = _decode_legacy(_read_json(path), expected_project_id=project)
            if expected_revision != legacy.revision:
                raise ProjectCapabilityProfileConflict(
                    "project capability profile revision conflict: "
                    f"expected {expected_revision}, current {legacy.revision}"
                )
            profile = replace(
                legacy,
                revision=legacy.revision + 1,
                boundary_profile_id=boundary_profile_id,
                boundary_profile_revision=boundary_profile_revision,
            )
            payload = _encode(profile)
            _reject_sensitive(payload)
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            return ProjectCapabilityProfileSnapshot(profile, profile.revision, True)

    def rebind_boundary(
        self, project_id: str, *, expected_revision: int, boundary_profile_id: str,
        boundary_profile_revision: int,
    ) -> ProjectCapabilityProfileSnapshot:
        """Advance only the Boundary attestation, preserving every selection."""
        project = _project_id(project_id)
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current = _default_profile(project) if not path.exists() else _decode(_read_json(path), expected_project_id=project)
            current_revision = current.revision
            if expected_revision != current_revision:
                raise ProjectCapabilityProfileConflict(f"project capability profile revision conflict: expected {expected_revision}, current {current_revision}")
            profile = replace(current, revision=current.revision + 1, boundary_profile_id=boundary_profile_id, boundary_profile_revision=boundary_profile_revision)
            payload = _encode(profile)
            _reject_sensitive(payload)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            return ProjectCapabilityProfileSnapshot(profile, profile.revision, True)

    def rebind_mcp_server(
        self,
        project_id: str,
        *,
        expected_revision: int,
        server_id: str,
        protocol_profile: str,
        manifest_revision: int,
        endpoint_identity: str,
        credential_subject_id: str,
        transport_generation: int,
    ) -> ProjectCapabilityProfileSnapshot:
        """CAS-replace exactly one Project MCP identity attestation.

        Callers must derive these non-secret values from the approved-server
        authority.  This store deliberately accepts no endpoint, credential,
        command, or transport configuration material.
        """
        project = _project_id(project_id)
        expected_revision = _positive_int(expected_revision, "expected profile revision")
        binding = MCPServerSelectionBinding(
            server_id=server_id,
            protocol_profile=protocol_profile,
            manifest_revision=manifest_revision,
            endpoint_identity=endpoint_identity,
            credential_subject_id=credential_subject_id,
            transport_generation=transport_generation,
        )
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current = _default_profile(project) if not path.exists() else _decode(
                _read_json(path), expected_project_id=project,
            )
            if current.revision != expected_revision:
                raise ProjectCapabilityProfileConflict(
                    "project capability profile revision conflict: "
                    f"expected {expected_revision}, current {current.revision}"
                )
            bindings = tuple(
                item for item in current.mcp_server_selection_bindings
                if item.server_id != binding.server_id
            ) + (binding,)
            profile = replace(
                current,
                revision=current.revision + 1,
                mcp_server_selection_bindings=bindings,
            )
            payload = _encode(profile)
            _reject_sensitive(payload)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            return ProjectCapabilityProfileSnapshot(profile, profile.revision, True)

    def exclude_tool(
        self, project_id: str, *, tool_id: str, expected_revision: int,
    ) -> ProjectCapabilityProfileSnapshot:
        """Narrow only one Tool selection while preserving all other fields."""
        project = _project_id(project_id)
        expected_revision = _positive_int(expected_revision, "expected profile revision")
        if not isinstance(tool_id, str) or not tool_id.strip():
            raise ProjectCapabilityProfileStoreError("tool identity is invalid")
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current = _default_profile(project) if not path.exists() else _decode(
                _read_json(path), expected_project_id=project,
            )
            if current.revision != expected_revision:
                raise ProjectCapabilityProfileConflict(
                    "project capability profile revision conflict: "
                    f"expected {expected_revision}, current {current.revision}"
                )
            allowed = tuple(item for item in current.allowed_tool_ids if item != tool_id)
            denied = current.denied_tool_ids
            if tool_id not in denied:
                denied = (*denied, tool_id)
            profile = replace(
                current, revision=current.revision + 1,
                allowed_tool_ids=allowed,
                denied_tool_ids=denied,
            )
            payload = _encode(profile)
            _reject_sensitive(payload)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            return ProjectCapabilityProfileSnapshot(profile, profile.revision, True)

    def select_tool(
        self, project_id: str, *, tool_binding: ToolSelectionBinding,
        expected_revision: int, require_existing_exclusion: bool = False,
    ) -> ProjectCapabilityProfileSnapshot:
        """Select one reviewed Tool contract without changing container authority."""
        project = _project_id(project_id)
        expected_revision = _positive_int(expected_revision, "expected profile revision")
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current = _default_profile(project) if not path.exists() else _decode(
                _read_json(path), expected_project_id=project,
            )
            if current.revision != expected_revision:
                raise ProjectCapabilityProfileConflict(
                    "project capability profile revision conflict: "
                    f"expected {expected_revision}, current {current.revision}"
                )
            if require_existing_exclusion and tool_binding.stable_id not in current.denied_tool_ids:
                raise ProjectCapabilityProfileStoreError(
                    "tool exclusion is not present"
                )
            denied = tuple(
                item for item in current.denied_tool_ids
                if item != tool_binding.stable_id
            )
            allowed = current.allowed_tool_ids
            if (
                current.tool_discovery_policy != "auto_discover" or allowed
            ) and tool_binding.stable_id not in allowed:
                allowed = (*allowed, tool_binding.stable_id)
            bindings = tuple(
                item for item in current.tool_selection_bindings
                if item.stable_id != tool_binding.stable_id
            ) + (tool_binding,)
            profile = replace(
                current, revision=current.revision + 1,
                allowed_tool_ids=allowed, denied_tool_ids=denied,
                tool_selection_bindings=bindings,
            )
            payload = _encode(profile)
            _reject_sensitive(payload)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            return ProjectCapabilityProfileSnapshot(profile, profile.revision, True)

    def _path(self, project_id: str) -> Path:
        path = self._root_dir / "library" / "projects" / project_id / "ai" / "capability-profile.json"
        resolved = path.resolve()
        projects_root = (self._root_dir / "library" / "projects").resolve()
        if projects_root not in resolved.parents:
            raise ProjectCapabilityProfileStoreError("project capability profile path escaped authority root")
        return resolved


def _default_profile(project_id: str) -> ProjectCapabilityProfile:
    return ProjectCapabilityProfile(
        profile_id=f"project-capability-{project_id}",
        project_id=project_id,
        revision=1,
        boundary_profile_id=f"project-boundary-{project_id}",
        boundary_profile_revision=1,
        enabled_sources=("core",),
        enabled_skill_ids=(),
        enabled_plugin_ids=(),
        enabled_mcp_server_ids=(),
        allowed_tool_ids=(),
        denied_tool_ids=(),
        preferred_model_tier="standard",
        memory_scope="project_only",
        cross_project_grant_ids=(),
        output_style_profile_id=None,
        tool_discovery_policy="auto_discover",
        tool_selection_bindings=(),
        mcp_server_selection_bindings=(),
    )


def _encode(profile: ProjectCapabilityProfile) -> dict[str, object]:
    payload = asdict(profile)
    payload["schema_version"] = _SCHEMA_VERSION
    for key, value in tuple(payload.items()):
        if isinstance(value, tuple):
            payload[key] = list(value)
    return {key: payload[key] for key in _FIELDS}


def _decode(payload: Mapping[str, object], *, expected_project_id: str) -> ProjectCapabilityProfile:
    if payload.get("schema_version") == "1.0.0" and "boundary_profile_revision" not in payload:
        # A legacy capability profile did not attest which Boundary revision
        # selected its tools.  It must be explicitly recreated against a
        # reviewed Boundary revision; binding it to today's profile could
        # silently widen a previously narrower configuration.
        raise ProjectCapabilityProfileStoreError(
            "legacy project capability profile requires explicit boundary revision migration"
        )
    fields = {str(key) for key in payload}
    if payload.get("schema_version") == "1.1.0" and fields == _V11_FIELDS:
        _reject_sensitive(payload)
        if payload.get("project_id") != expected_project_id:
            raise ProjectCapabilityProfileStoreError(
                "project capability profile identity drifted"
            )
        return _profile_from_payload(
            payload, expected_project_id=expected_project_id,
            tool_discovery_policy="auto_discover",
            tool_selection_bindings=(),
            mcp_server_selection_bindings=(),
        )
    if payload.get("schema_version") == "1.2.0" and fields == _V12_FIELDS:
        _reject_sensitive(payload)
        if payload.get("project_id") != expected_project_id:
            raise ProjectCapabilityProfileStoreError(
                "project capability profile identity drifted"
            )
        return _profile_from_payload(
            payload, expected_project_id=expected_project_id,
            mcp_server_selection_bindings=(),
        )
    if fields != _FIELDS:
        raise ProjectCapabilityProfileStoreError("project capability profile fields are invalid")
    _reject_sensitive(payload)
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ProjectCapabilityProfileStoreError("project capability profile schema is unsupported")
    if payload.get("project_id") != expected_project_id:
        raise ProjectCapabilityProfileStoreError("project capability profile identity drifted")
    return _profile_from_payload(payload, expected_project_id=expected_project_id)


def _decode_legacy(payload: Mapping[str, object], *, expected_project_id: str) -> ProjectCapabilityProfile:
    if {str(key) for key in payload} != _LEGACY_FIELDS or payload.get("schema_version") != "1.0.0":
        raise ProjectCapabilityProfileStoreError("project capability profile is not a migratable legacy schema")
    _reject_sensitive(payload)
    if payload.get("project_id") != expected_project_id:
        raise ProjectCapabilityProfileStoreError("project capability profile identity drifted")
    return _profile_from_payload(
        payload,
        expected_project_id=expected_project_id,
        # This temporary value is only used to validate legacy capability
        # fields.  It is never persisted or returned by get().
        boundary_profile_revision=1,
        tool_discovery_policy="auto_discover",
        tool_selection_bindings=(),
        mcp_server_selection_bindings=(),
    )


def _profile_from_payload(
    payload: Mapping[str, object],
    *,
    expected_project_id: str,
    boundary_profile_revision: int | None = None,
    tool_discovery_policy: str | None = None,
    tool_selection_bindings: tuple[ToolSelectionBinding, ...] | None = None,
    mcp_server_selection_bindings: tuple[MCPServerSelectionBinding, ...] | None = None,
) -> ProjectCapabilityProfile:
    try:
        return ProjectCapabilityProfile(
            profile_id=_text(payload.get("profile_id"), "profile id"),
            project_id=expected_project_id,
            revision=_positive_int(payload.get("revision"), "profile revision"),
            boundary_profile_id=_text(payload.get("boundary_profile_id"), "boundary profile id"),
            boundary_profile_revision=(
                boundary_profile_revision if boundary_profile_revision is not None
                else _positive_int(payload.get("boundary_profile_revision"), "boundary profile revision")
            ),
            enabled_sources=_text_tuple(payload.get("enabled_sources"), "enabled sources"),
            enabled_skill_ids=_text_tuple(payload.get("enabled_skill_ids"), "enabled skills"),
            enabled_plugin_ids=_text_tuple(payload.get("enabled_plugin_ids"), "enabled plugins"),
            enabled_mcp_server_ids=_text_tuple(payload.get("enabled_mcp_server_ids"), "enabled MCP servers"),
            allowed_tool_ids=_text_tuple(payload.get("allowed_tool_ids"), "allowed tools"),
            denied_tool_ids=_text_tuple(payload.get("denied_tool_ids"), "denied tools"),
            preferred_model_tier=_text(payload.get("preferred_model_tier"), "preferred model tier"),  # type: ignore[arg-type]
            memory_scope=_text(payload.get("memory_scope"), "memory scope"),  # type: ignore[arg-type]
            cross_project_grant_ids=_text_tuple(payload.get("cross_project_grant_ids"), "cross-project grants"),
            output_style_profile_id=(
                None if payload.get("output_style_profile_id") is None
                else _text(payload.get("output_style_profile_id"), "output style profile id")
            ),
            max_tools=_decoded_max_tools(payload.get("max_tools")),
            max_tool_descriptor_bytes=_positive_int(
                payload.get("max_tool_descriptor_bytes"), "maximum tool descriptor bytes"
            ),
            tool_discovery_policy=(
                tool_discovery_policy
                if tool_discovery_policy is not None
                else _text(payload.get("tool_discovery_policy"), "tool discovery policy")
            ),  # type: ignore[arg-type]
            tool_selection_bindings=(
                tool_selection_bindings
                if tool_selection_bindings is not None
                else _selection_bindings(payload.get("tool_selection_bindings"))
            ),
            mcp_server_selection_bindings=(
                mcp_server_selection_bindings
                if mcp_server_selection_bindings is not None
                else _mcp_selection_bindings(payload.get("mcp_server_selection_bindings"))
            ),
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, ProjectCapabilityProfileStoreError):
            raise
        raise ProjectCapabilityProfileStoreError("project capability profile is invalid") from error


def _decoded_max_tools(value: object) -> int:
    """Read historical 13-64 profiles without allowing them to widen a Turn."""
    maximum = _positive_int(value, "maximum tools")
    if MAX_MODEL_VISIBLE_TOOLS < maximum <= 64:
        return MAX_MODEL_VISIBLE_TOOLS
    return maximum


def _compatibility_diagnostics(payload: Mapping[str, object]) -> tuple[str, ...]:
    value = payload.get("max_tools")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MAX_MODEL_VISIBLE_TOOLS < value <= 64
    ):
        return ("max_tools_clamped_to_12",)
    return ()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectCapabilityProfileStoreError("project capability profile is unreadable") from error
    if not isinstance(value, dict):
        raise ProjectCapabilityProfileStoreError("project capability profile must be an object")
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProjectCapabilityProfileStoreError(f"{label} must be a string array")
    return tuple(value)


def _selection_bindings(value: object) -> tuple[ToolSelectionBinding, ...]:
    if not isinstance(value, list):
        raise ProjectCapabilityProfileStoreError(
            "tool selection bindings must be an array"
        )
    bindings: list[ToolSelectionBinding] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "stable_id", "contract_identity",
        }:
            raise ProjectCapabilityProfileStoreError(
                "tool selection binding is invalid"
            )
        bindings.append(ToolSelectionBinding(
            stable_id=_text(item.get("stable_id"), "selection stable id"),
            contract_identity=_text(
                item.get("contract_identity"), "selection contract identity",
            ),
        ))
    return tuple(bindings)


def _mcp_selection_bindings(value: object) -> tuple[MCPServerSelectionBinding, ...]:
    if not isinstance(value, list):
        raise ProjectCapabilityProfileStoreError(
            "MCP server selection bindings must be an array"
        )
    bindings: list[MCPServerSelectionBinding] = []
    fields = {
        "server_id", "protocol_profile", "manifest_revision",
        "endpoint_identity", "credential_subject_id", "transport_generation",
    }
    for item in value:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise ProjectCapabilityProfileStoreError(
                "MCP server selection binding is invalid"
            )
        bindings.append(MCPServerSelectionBinding(
            server_id=_text(item.get("server_id"), "MCP server id"),
            protocol_profile=_text(item.get("protocol_profile"), "MCP protocol profile"),
            manifest_revision=_positive_int(item.get("manifest_revision"), "MCP manifest revision"),
            endpoint_identity=_text(item.get("endpoint_identity"), "MCP endpoint identity"),
            credential_subject_id=_text(item.get("credential_subject_id"), "MCP credential subject id"),
            transport_generation=_positive_int(item.get("transport_generation"), "MCP transport generation"),
        ))
    return tuple(bindings)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectCapabilityProfileStoreError(f"{label} must be non-empty")
    return value.strip()


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProjectCapabilityProfileStoreError(f"{label} must be positive")
    return value




def _project_id(value: str) -> str:
    project_id = str(value).strip()
    if not _PROJECT_ID.fullmatch(project_id):
        raise ProjectCapabilityProfileStoreError("project identity is invalid")
    return project_id


def _path_lock(path: Path) -> Lock:
    resolved = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(resolved, Lock())


def _reject_sensitive(value: object, *, path: str = "profile") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_secret"):
                raise ProjectCapabilityProfileStoreError(f"sensitive material is forbidden at {path}.{key}")
            _reject_sensitive(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive(nested, path=f"{path}[{index}]")
