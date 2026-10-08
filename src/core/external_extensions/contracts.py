from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class ExtensionContractError(ValueError):
    """Raised when external data attempts to cross the canonical contract."""


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+~-]{0,159}$")
_ARTIFACT_REF = re.compile(r"^crp://[A-Za-z0-9._~:/-]{1,240}$")
_SECRET_REF = re.compile(r"^[a-z][a-z0-9._-]{1,95}$")
_CONTRIBUTION_KINDS = frozenset(
    {
        "application_skill",
        "plugin_skill",
        "mcp_server_reference",
        "hook_binding",
        "declarative_tool",
        "plugin_hand",
        "ui_descriptor",
        "repository_artifact",
        "marketplace_catalog",
    }
)
_SOURCE_FORMATS = frozenset(
    {
        "openai_codex_plugin",
        "openai_agent_skill",
        "deepseek_harness_skill",
        "deepseek_harness_plugin",
        "codex_hook_config",
        "mcp_config",
        "codex_marketplace",
    }
)
_FILESYSTEM_SCOPES = frozenset({"workspace_read", "workspace_output"})
_HOOK_EVENTS = frozenset(
    {
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "SubagentStart",
        "SubagentStop",
        "Stop",
    }
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)), *(f"LPT{number}" for number in range(1, 10))}
)


def _text(value: object, label: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ExtensionContractError(f"{label} is invalid")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ExtensionContractError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label, maximum=128)
    if not _ID.fullmatch(text):
        raise ExtensionContractError(f"{label} is invalid")
    return text


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    source_kind: str
    canonical_locator: str
    immutable_revision: str | None
    artifact_ref: str
    acquisition_contract_revision: str = "1"
    trust_tier: str = "untrusted"

    def __post_init__(self) -> None:
        if self.source_kind not in {
            "local_selection",
            "github_repository",
            "git_repository",
            "https_archive",
            "registry_package",
            "marketplace_entry",
            "mcp_endpoint",
        }:
            raise ExtensionContractError("resolved source kind is invalid")
        _text(self.canonical_locator, "canonical source locator", maximum=512)
        if "://" in self.canonical_locator and not self.canonical_locator.startswith("crp://"):
            try:
                parsed = urlsplit(self.canonical_locator)
            except ValueError as error:
                raise ExtensionContractError("canonical source locator is invalid") from error
            if (
                parsed.scheme not in {"https", "git+https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ExtensionContractError("canonical source locator is invalid")
        if self.source_kind == "local_selection" and not self.canonical_locator.startswith(
            "crp://source-selections/"
        ):
            raise ExtensionContractError("local source must use an opaque selection reference")
        if self.source_kind == "github_repository":
            parsed = urlsplit(self.canonical_locator)
            if parsed.scheme != "https" or parsed.hostname != "github.com":
                raise ExtensionContractError("GitHub source locator is invalid")
        if self.source_kind == "mcp_endpoint":
            parsed = urlsplit(self.canonical_locator)
            if parsed.scheme != "https":
                raise ExtensionContractError("MCP endpoint source must use HTTPS")
        if self.immutable_revision is not None and not _REVISION.fullmatch(self.immutable_revision):
            raise ExtensionContractError("immutable source revision is invalid")
        if not _ARTIFACT_REF.fullmatch(self.artifact_ref):
            raise ExtensionContractError("artifact reference is invalid")
        if not _REVISION.fullmatch(self.acquisition_contract_revision):
            raise ExtensionContractError("acquisition contract revision is invalid")
        if self.trust_tier not in {"untrusted", "reviewed_source", "managed"}:
            raise ExtensionContractError("source trust tier is invalid")

    @property
    def is_immutable(self) -> bool:
        return self.immutable_revision is not None


@dataclass(frozen=True, slots=True)
class FrozenRuntimeContract:
    execution_state_owner: str = "core_effect_log"
    recovery_owner: str = "core_reaper"
    secret_access: str = "lease_reference_only"
    memory_write: str = "proposal_only"
    document_write: str = "draft_only"
    policy_predicates: str = "closed_core_set"

    def __post_init__(self) -> None:
        expected = (
            "core_effect_log",
            "core_reaper",
            "lease_reference_only",
            "proposal_only",
            "draft_only",
            "closed_core_set",
        )
        if (
            self.execution_state_owner,
            self.recovery_owner,
            self.secret_access,
            self.memory_write,
            self.document_write,
            self.policy_predicates,
        ) != expected:
            raise ExtensionContractError("external extensions cannot replace Core runtime ownership")


@dataclass(frozen=True, slots=True)
class ContributionCandidate:
    kind: str
    contribution_id: str
    source_path: str
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", tuple(tuple(item) for item in self.metadata))
        if self.kind not in _CONTRIBUTION_KINDS:
            raise ExtensionContractError("extension contribution kind is forbidden")
        _identifier(self.contribution_id, "contribution id")
        _text(self.source_path, "contribution source path")
        path_parts = self.source_path.split("/")
        if (
            self.source_path.startswith(("/", "\\"))
            or "\\" in self.source_path
            or ":" in self.source_path
            or any(
                part in {"", ".", ".."}
                or part.rstrip(" .") != part
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
                for part in path_parts
            )
        ):
            raise ExtensionContractError("contribution source path is unsafe")
        keys: set[str] = set()
        for key, value in self.metadata:
            normalized = _identifier(key, "contribution metadata key")
            _text(value, "contribution metadata value")
            if normalized in keys:
                raise ExtensionContractError("contribution metadata keys must be unique")
            keys.add(normalized)


@dataclass(frozen=True, slots=True)
class PermissionPlan:
    network_destinations: tuple[str, ...] = ()
    filesystem_scopes: tuple[str, ...] = ()
    secret_reference_names: tuple[str, ...] = ()
    requires_subprocess: bool = False
    requires_native_build: bool = False
    requires_oauth: bool = False
    hook_events: tuple[str, ...] = ()
    review_required: bool = False
    review_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "network_destinations",
            "filesystem_scopes",
            "secret_reference_names",
            "hook_events",
            "review_reasons",
        ):
            object.__setattr__(self, field, tuple(getattr(self, field)))
        if tuple(sorted(set(self.network_destinations))) != self.network_destinations:
            raise ExtensionContractError("network destinations must be sorted and unique")
        for destination in self.network_destinations:
            _text(destination, "network destination")
            if "://" in destination or "/" in destination or "@" in destination:
                raise ExtensionContractError("network destination must be a hostname only")
        if tuple(sorted(set(self.filesystem_scopes))) != self.filesystem_scopes or any(
            scope not in _FILESYSTEM_SCOPES for scope in self.filesystem_scopes
        ):
            raise ExtensionContractError("filesystem scopes are invalid")
        if tuple(sorted(set(self.secret_reference_names))) != self.secret_reference_names or any(
            not _SECRET_REF.fullmatch(name) for name in self.secret_reference_names
        ):
            raise ExtensionContractError("secret references are invalid")
        if tuple(sorted(set(self.hook_events))) != self.hook_events or any(
            event not in _HOOK_EVENTS for event in self.hook_events
        ):
            raise ExtensionContractError("hook events are invalid")
        if tuple(sorted(set(self.review_reasons))) != self.review_reasons:
            raise ExtensionContractError("review reasons must be sorted and unique")
        if self.review_reasons and not self.review_required:
            raise ExtensionContractError("review reasons require review")


@dataclass(frozen=True, slots=True)
class CompatibilityIssue:
    code: str
    path: str
    detail: str

    def __post_init__(self) -> None:
        _identifier(self.code, "compatibility issue code")
        _text(self.path, "compatibility issue path")
        _text(self.detail, "compatibility issue detail", maximum=512)


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    extension_id: str
    version: str | None
    source: ResolvedSource
    source_format: str
    adapter_id: str
    adapter_revision: str
    contributions: tuple[ContributionCandidate, ...]
    permission_plan: PermissionPlan
    issues: tuple[CompatibilityIssue, ...] = ()
    runtime_contract: FrozenRuntimeContract = FrozenRuntimeContract()
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "contributions", tuple(self.contributions))
        object.__setattr__(self, "issues", tuple(self.issues))
        _identifier(self.extension_id, "extension id")
        if self.version is not None:
            _text(self.version, "extension version", maximum=96)
        if self.source_format not in _SOURCE_FORMATS:
            raise ExtensionContractError("source format is invalid")
        _identifier(self.adapter_id, "adapter id")
        if not _REVISION.fullmatch(self.adapter_revision):
            raise ExtensionContractError("adapter revision is invalid")
        if self.schema_version != "1.0.0":
            raise ExtensionContractError("extension manifest schema is invalid")
        identities = [(item.kind, item.contribution_id) for item in self.contributions]
        if len(identities) != len(set(identities)):
            raise ExtensionContractError("extension contribution identities must be unique")

    @property
    def status(self) -> str:
        return "quarantined" if self.issues else "discovered"


@dataclass(frozen=True, slots=True)
class AdapterResult:
    manifest: ExtensionManifest

    @property
    def status(self) -> str:
        return self.manifest.status
