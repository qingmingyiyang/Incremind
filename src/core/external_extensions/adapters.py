from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from urllib.parse import urlsplit

from .contracts import (
    AdapterResult,
    CompatibilityIssue,
    ContributionCandidate,
    ExtensionContractError,
    ExtensionManifest,
    PermissionPlan,
    ResolvedSource,
)
from .detection import ArtifactInventory, Detection, StaticExtensionDetectorRegistry


class StaticExtensionAdapterRegistry:
    """Core-owned pure adapters. Installed packages cannot add adapters."""

    def __init__(self) -> None:
        self._adapters: Mapping[
            str, Callable[[ArtifactInventory, ResolvedSource, Detection], AdapterResult]
        ] = MappingProxyType({
            "openai_codex_plugin": _codex_plugin,
            "openai_agent_skill": _agent_skill,
            "deepseek_harness_skill": _agent_skill,
            "deepseek_harness_plugin": _dsh_plugin,
            "codex_hook_config": _hook_config,
            "mcp_config": _mcp_config,
            "codex_marketplace": _marketplace,
        })

    def normalize(
        self,
        detection: Detection,
        inventory: ArtifactInventory,
        source: ResolvedSource,
    ) -> AdapterResult:
        try:
            adapter = self._adapters[detection.source_format]
        except KeyError as error:
            raise ExtensionContractError("source format has no reviewed adapter") from error
        return adapter(inventory, source, detection)


def inspect_extension(
    inventory: ArtifactInventory,
    source: ResolvedSource,
    *,
    detectors: StaticExtensionDetectorRegistry | None = None,
    adapters: StaticExtensionAdapterRegistry | None = None,
) -> AdapterResult:
    detection = (detectors or StaticExtensionDetectorRegistry()).detect_exactly_one(inventory)
    return (adapters or StaticExtensionAdapterRegistry()).normalize(detection, inventory, source)


def _codex_plugin(
    inventory: ArtifactInventory, source: ResolvedSource, detection: Detection,
) -> AdapterResult:
    path = ".codex-plugin/plugin.json"
    payload = _json_object(inventory, path)
    identity = _manifest_id(payload.get("name"), fallback="codex-plugin")
    version = _optional_text(payload.get("version"))
    contributions: list[ContributionCandidate] = []
    for skill_path in _plugin_skill_paths(inventory):
        skill_id = PurePosixPath(skill_path).parent.name
        contributions.append(ContributionCandidate("plugin_skill", _manifest_id(skill_id), skill_path))
    top_groups = {PurePosixPath(item).parts[0] for item in inventory.paths}
    if ".mcp.json" in inventory.paths or "mcp" in top_groups:
        contributions.append(ContributionCandidate("mcp_server_reference", f"{identity}.mcp", path))
    if "hooks" in top_groups:
        contributions.append(ContributionCandidate("hook_binding", f"{identity}.hooks", path))
    if "tools" in top_groups:
        contributions.append(ContributionCandidate("declarative_tool", f"{identity}.tools", path))
    if "hands" in top_groups or "scripts" in top_groups:
        contributions.append(ContributionCandidate("plugin_hand", f"{identity}.hands", path))
    if "ui" in top_groups:
        contributions.append(ContributionCandidate("ui_descriptor", f"{identity}.ui", path))
    reasons = set()
    if any(item.kind in {"mcp_server_reference", "hook_binding", "declarative_tool", "plugin_hand"} for item in contributions):
        reasons.add("executable_or_external_contribution")
    if not source.is_immutable:
        reasons.add("mutable_source")
    unknown = sorted(set(payload) - {
        "name", "version", "description", "author", "homepage", "repository", "license", "keywords",
        "skills", "mcpServers", "interface",
    })
    issues = tuple(
        CompatibilityIssue("unsupported_manifest_field", f"{path}#{key}", "field requires a future reviewed adapter revision")
        for key in unknown
    )
    return AdapterResult(
        ExtensionManifest(
            extension_id=identity,
            version=version,
            source=source,
            source_format="openai_codex_plugin",
            adapter_id="openai-codex-plugin",
            adapter_revision="1",
            contributions=tuple(contributions),
            permission_plan=_permission_plan(
                source,
                reasons=reasons,
                requires_subprocess=any(item.kind in {"hook_binding", "plugin_hand", "declarative_tool"} for item in contributions),
            ),
            issues=issues,
        )
    )


def _agent_skill(
    inventory: ArtifactInventory, source: ResolvedSource, detection: Detection,
) -> AdapterResult:
    skill_paths = detection.marker_paths
    if not skill_paths:
        raise ExtensionContractError("skill adapter did not receive a Skill artifact")
    contributions: list[ContributionCandidate] = []
    issues: list[CompatibilityIssue] = []
    identities: list[str] = []
    for path in skill_paths:
        metadata = _skill_frontmatter(inventory.read_text(path, maximum=128 * 1024), path)
        identity = _manifest_id(metadata.get("name"), fallback=PurePosixPath(path).parent.name or "skill")
        identities.append(identity)
        if not metadata.get("description"):
            issues.append(CompatibilityIssue("missing_description", path, "Skill description is required for safe selection"))
        contribution_metadata = _skill_selection_metadata(
            metadata,
            path=path,
        )
        if dict(contribution_metadata).get("model_invocable") == "false":
            issues.append(
                CompatibilityIssue(
                    "unsupported_invocation_policy",
                    path,
                    "model-disabled Skills require a reviewed human-invocation adapter",
                )
            )
        contributions.append(
            ContributionCandidate(
                "application_skill",
                identity,
                path,
                contribution_metadata,
            )
        )
    has_scripts = any("/scripts/" in f"/{path}" or path.startswith("scripts/") for path in inventory.paths)
    reasons = set()
    if has_scripts:
        reasons.add("script_resource")
    if not source.is_immutable:
        reasons.add("mutable_source")
    extension_id = identities[0] if len(identities) == 1 else "skill-collection"
    source_format = "deepseek_harness_skill" if any(path.startswith(".dsh/") for path in skill_paths) else "openai_agent_skill"
    return AdapterResult(
        ExtensionManifest(
            extension_id=extension_id,
            version=None,
            source=source,
            source_format=source_format,
            adapter_id="agent-skill",
            adapter_revision="1",
            contributions=tuple(contributions),
            permission_plan=_permission_plan(source, reasons=reasons, requires_subprocess=has_scripts),
            issues=tuple(issues),
        )
    )


def _dsh_plugin(
    inventory: ArtifactInventory, source: ResolvedSource, detection: Detection,
) -> AdapterResult:
    payload = _json_object(inventory, "package.json")
    identity = _manifest_id(payload.get("name"), fallback="dsh-plugin")
    dsh = payload.get("dsh")
    issues: list[CompatibilityIssue] = []
    if not isinstance(dsh, Mapping) or not isinstance(dsh.get("bundle"), Mapping):
        issues.append(CompatibilityIssue("invalid_dsh_bundle", "package.json#dsh", "DSH bundle declaration is required"))
    scripts = payload.get("scripts")
    requires_native = isinstance(scripts, Mapping) and any(
        key in scripts for key in ("install", "postinstall", "preinstall", "prepare")
    )
    reasons = {"dsh_runtime_plugin", "subprocess_runtime"}
    if requires_native:
        reasons.add("package_lifecycle_script")
    if not source.is_immutable:
        reasons.add("mutable_source")
    return AdapterResult(
        ExtensionManifest(
            extension_id=identity,
            version=_optional_text(payload.get("version")),
            source=source,
            source_format="deepseek_harness_plugin",
            adapter_id="deepseek-harness-plugin",
            adapter_revision="1",
            contributions=(ContributionCandidate("plugin_hand", f"{identity}.runtime", "package.json"),),
            permission_plan=_permission_plan(
                source,
                reasons=reasons,
                requires_subprocess=True,
                requires_native_build=requires_native,
            ),
            issues=tuple(issues),
        )
    )


def _hook_config(
    inventory: ArtifactInventory, source: ResolvedSource, detection: Detection,
) -> AdapterResult:
    path = next(path for path in (".codex/hooks.json", "hooks/hooks.json", "hooks.json") if inventory.contains(path))
    payload = _json_object(inventory, path)
    hooks = payload.get("hooks")
    if not isinstance(hooks, Mapping):
        raise ExtensionContractError("Codex Hook config requires a hooks object")
    events = tuple(sorted(str(event) for event in hooks))
    reasons = {"hook_execution"}
    if not source.is_immutable:
        reasons.add("mutable_source")
    issues = tuple(
        CompatibilityIssue("unsupported_hook_event", f"{path}#{event}", "Hook event is outside the reviewed Codex event set")
        for event in events
        if event not in _CODEX_HOOK_EVENTS
    )
    identity = "codex-hooks"
    return AdapterResult(
        ExtensionManifest(
            extension_id=identity,
            version=None,
            source=source,
            source_format="codex_hook_config",
            adapter_id="codex-hooks",
            adapter_revision="1",
            contributions=(ContributionCandidate("hook_binding", f"{identity}.binding", path),),
            permission_plan=_permission_plan(
                source,
                reasons=reasons,
                requires_subprocess=True,
                hook_events=tuple(sorted(event for event in events if event in _CODEX_HOOK_EVENTS)),
            ),
            issues=issues,
        )
    )


def _mcp_config(
    inventory: ArtifactInventory, source: ResolvedSource, detection: Detection,
) -> AdapterResult:
    path = next(path for path in (".mcp.json", "mcp.json", ".codex/config.toml") if inventory.contains(path))
    if path.endswith(".toml"):
        try:
            payload = tomllib.loads(inventory.read_text(path, maximum=256 * 1024))
        except tomllib.TOMLDecodeError as error:
            raise ExtensionContractError("MCP TOML is invalid") from error
        servers = payload.get("mcp_servers")
    else:
        payload = _json_object(inventory, path)
        servers = payload.get("mcpServers", payload.get("mcp_servers"))
    if not isinstance(servers, Mapping) or not servers:
        raise ExtensionContractError("MCP config requires a non-empty server mapping")
    contributions: list[ContributionCandidate] = []
    destinations: set[str] = set()
    requires_subprocess = False
    requires_oauth = False
    reasons = {"mcp_connection"}
    issues: list[CompatibilityIssue] = []
    for raw_name, raw_config in sorted(servers.items(), key=lambda item: str(item[0])):
        name = _manifest_id(raw_name, fallback="mcp-server")
        if not isinstance(raw_config, Mapping):
            raise ExtensionContractError("MCP server config must be an object")
        transport = "http" if isinstance(raw_config.get("url"), str) else "stdio"
        if transport == "http":
            parsed = urlsplit(str(raw_config["url"]))
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ExtensionContractError("MCP HTTP endpoint is invalid")
            destinations.add(parsed.hostname.lower())
            if parsed.scheme != "https":
                issues.append(
                    CompatibilityIssue(
                        "insecure_mcp_endpoint",
                        f"{path}#{name}",
                        "MCP HTTP endpoints must use TLS before approval",
                    )
                )
        else:
            requires_subprocess = True
            reasons.add("stdio_command")
        if "env" in raw_config:
            issues.append(
                CompatibilityIssue(
                    "literal_environment_value", f"{path}#{name}",
                    "MCP environment values must be replaced by reviewed references",
                )
            )
        if any(key in raw_config for key in ("http_headers", "headers")):
            issues.append(
                CompatibilityIssue(
                    "literal_header_value", f"{path}#{name}",
                    "MCP header values must be replaced by reviewed references",
                )
            )
        if any(key in raw_config for key in ("env_vars", "env_http_headers", "bearer_token_env_var")):
            reasons.add("credential_reference_declaration")
        if any(key in raw_config for key in ("env", "http_headers", "headers")):
            reasons.add("environment_or_credential_declaration")
        if any(key in raw_config for key in ("auth", "oauth", "oauth2", "oauth_config")):
            reasons.add("oauth_authorization")
            requires_oauth = True
        contributions.append(
            ContributionCandidate(
                "mcp_server_reference",
                name,
                path,
                (("transport", transport),),
            )
        )
    if not source.is_immutable:
        reasons.add("mutable_source")
    return AdapterResult(
        ExtensionManifest(
            extension_id="mcp-config",
            version=None,
            source=source,
            source_format="mcp_config",
            adapter_id="mcp-config",
            adapter_revision="1",
            contributions=tuple(contributions),
            permission_plan=_permission_plan(
                source,
                reasons=reasons,
                network_destinations=tuple(sorted(destinations)),
                requires_subprocess=requires_subprocess,
                requires_oauth=requires_oauth,
            ),
            issues=tuple(issues),
        )
    )


def _marketplace(
    inventory: ArtifactInventory, source: ResolvedSource, detection: Detection,
) -> AdapterResult:
    path = ".agents/plugins/marketplace.json"
    payload = _json_object(inventory, path)
    identity = _manifest_id(payload.get("name"), fallback="codex-marketplace")
    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        raise ExtensionContractError("Codex marketplace requires a plugins array")
    contributions = tuple(
        ContributionCandidate(
            "repository_artifact",
            _manifest_id(item.get("name"), fallback=f"plugin-{index}"),
            path,
        )
        for index, item in enumerate(plugins)
        if isinstance(item, Mapping)
    )
    reasons = set()
    if not source.is_immutable:
        reasons.add("mutable_source")
    return AdapterResult(
        ExtensionManifest(
            extension_id=identity,
            version=None,
            source=source,
            source_format="codex_marketplace",
            adapter_id="codex-marketplace",
            adapter_revision="1",
            contributions=contributions or (ContributionCandidate("marketplace_catalog", f"{identity}.catalog", path),),
            permission_plan=_permission_plan(source, reasons=reasons),
        )
    )


def _permission_plan(
    source: ResolvedSource,
    *,
    reasons: set[str],
    network_destinations: tuple[str, ...] = (),
    requires_subprocess: bool = False,
    requires_native_build: bool = False,
    requires_oauth: bool = False,
    hook_events: tuple[str, ...] = (),
) -> PermissionPlan:
    normalized = set(reasons)
    if not source.is_immutable:
        normalized.add("mutable_source")
    return PermissionPlan(
        network_destinations=tuple(sorted(set(network_destinations))),
        requires_subprocess=requires_subprocess,
        requires_native_build=requires_native_build,
        requires_oauth=requires_oauth,
        hook_events=tuple(sorted(set(hook_events))),
        review_required=bool(normalized),
        review_reasons=tuple(sorted(normalized)),
    )


def _json_object(inventory: ArtifactInventory, path: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ExtensionContractError(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(inventory.read_text(path, maximum=512 * 1024), object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as error:
        raise ExtensionContractError(f"invalid JSON extension descriptor: {path}") from error
    if not isinstance(value, dict):
        raise ExtensionContractError(f"extension descriptor must be an object: {path}")
    return value


def _plugin_skill_paths(inventory: ArtifactInventory) -> tuple[str, ...]:
    return tuple(
        path
        for path in inventory.paths
        if re.fullmatch(r"skills/[a-z0-9][a-z0-9-]{0,62}/SKILL\.md", path)
    )


def _skill_frontmatter(text: str, path: str) -> dict[str, object]:
    """Read the bounded scalar YAML subset used by Agent and DSH Skill headers.

    This deliberately does not attempt general YAML: aliases, collections, block
    values, tags, and multi-level mappings stay rejected until a reviewed parser
    can preserve their semantics.
    """
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise ExtensionContractError(f"Skill frontmatter is required: {path}")
    closing = re.search(r"^---(?:\n|$)", text[4:], flags=re.MULTILINE)
    if closing is None or closing.start() + 4 > 4096:
        raise ExtensionContractError(f"Skill frontmatter is invalid: {path}")
    metadata: dict[str, object] = {}
    lines = text[4:4 + closing.start()].splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace():
            raise ExtensionContractError(f"Skill frontmatter entry is invalid: {path}")
        key, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key.strip()):
            raise ExtensionContractError(f"Skill frontmatter entry is invalid: {path}")
        if key.strip() in metadata:
            raise ExtensionContractError(f"Skill frontmatter key is duplicated: {path}")
        field = key.strip()
        raw_value = value.strip()
        if field == "metadata":
            if raw_value:
                raise ExtensionContractError(f"Skill metadata must be a mapping: {path}")
            nested, index = _skill_metadata_mapping(lines, index, path)
            metadata[field] = nested
            continue
        metadata[field] = _skill_scalar(raw_value, path)
    if "name" not in metadata:
        raise ExtensionContractError(f"Skill name is required: {path}")
    return metadata


def _skill_metadata_mapping(lines: list[str], index: int, path: str) -> tuple[dict[str, str], int]:
    metadata: dict[str, str] = {}
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if not line.startswith("  ") or line.startswith("   "):
            break
        key, separator, value = line[2:].partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key.strip()):
            raise ExtensionContractError(f"Skill metadata entry is invalid: {path}")
        field = key.strip()
        if field in metadata or not value.strip():
            raise ExtensionContractError(f"Skill metadata entry is invalid: {path}")
        metadata[field] = _skill_scalar(value.strip(), path)
        index += 1
    return metadata, index


def _skill_scalar(value: str, path: str) -> str:
    if not value or value[0] in "[{|>&*!" or value.startswith("-"):
        raise ExtensionContractError(f"Skill frontmatter value is unsupported: {path}")
    if value[0] in "\"'":
        if len(value) < 2 or value[-1] != value[0]:
            raise ExtensionContractError(f"Skill frontmatter value is invalid: {path}")
        if value[0] == "\"":
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as error:
                raise ExtensionContractError(f"Skill frontmatter value is invalid: {path}") from error
            if not isinstance(parsed, str):
                raise ExtensionContractError(f"Skill frontmatter value is invalid: {path}")
            return parsed
        return value[1:-1].replace("''", "'")
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise ExtensionContractError(f"Skill frontmatter value is invalid: {path}")
    return value


def _skill_selection_metadata(
    frontmatter: Mapping[str, object], *, path: str,
) -> tuple[tuple[str, str], ...]:
    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        raise ExtensionContractError(f"Skill description is invalid: {path}")
    result: list[tuple[str, str]] = [("description", description)] if description else []
    # `.agents/skills` is a shared source consumed by both ecosystems.  Honor
    # the stricter invocation policy fields regardless of which discovery root
    # supplied the package so a shared Skill cannot gain a broader surface.
    _reject_legacy_invocation_keys(frontmatter, path)
    when_to_use = frontmatter.get("whenToUse")
    if when_to_use is not None:
        if not isinstance(when_to_use, str):
            raise ExtensionContractError(f"Skill whenToUse is invalid: {path}")
        result.append(("when_to_use", when_to_use))
    disable_model = _skill_boolean(frontmatter, "disable-model-invocation", path, default=False)
    user_invocable = _skill_boolean(frontmatter, "user-invocable", path, default=True)
    result.extend((
        ("model_invocable", str(not disable_model).lower()),
        ("user_invocable", str(user_invocable).lower()),
    ))
    return tuple(result)


def _reject_legacy_invocation_keys(frontmatter: Mapping[str, object], path: str) -> None:
    rejected = {
        "disableModelInvocation", "disable_model_invocation", "modelInvocable", "model-invocable",
        "userInvocable", "user_invocable",
    }
    found = sorted(rejected.intersection(frontmatter))
    if found:
        raise ExtensionContractError(f"Skill invocation key is unsupported: {path}#{found[0]}")


def _skill_boolean(frontmatter: Mapping[str, object], key: str, path: str, *, default: bool) -> bool:
    raw = frontmatter.get(key)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ExtensionContractError(f"Skill {key} must be a boolean: {path}")
    normalized = raw.lower()
    values = {"true": True, "yes": True, "on": True, "1": True, "false": False, "no": False, "off": False, "0": False}
    try:
        return values[normalized]
    except KeyError as error:
        raise ExtensionContractError(f"Skill {key} must be a boolean: {path}") from error


def _manifest_id(value: object, *, fallback: str = "extension") -> str:
    raw = value if isinstance(value, str) and value else fallback
    normalized = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-._")
    if len(normalized) < 2:
        normalized = f"ext-{normalized or 'unknown'}"
    if len(normalized) > 127:
        normalized = normalized[:127].rstrip("-._")
    return normalized


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 96 or any(char in value for char in ("\x00", "\r", "\n")):
        raise ExtensionContractError("extension version is invalid")
    return value


_CODEX_HOOK_EVENTS = frozenset(
    {
        "PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact",
        "SessionStart", "SessionEnd", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop",
    }
)
