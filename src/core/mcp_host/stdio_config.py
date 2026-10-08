from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .host import MCPHostConnectionConfig


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SECRET_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FORBIDDEN_EXECUTABLES = frozenset({"cmd.exe", "powershell.exe", "pwsh.exe"})
_FORBIDDEN_EXECUTABLE_SUFFIXES = frozenset({".bat", ".cmd", ".ps1"})
_SAFE_CONSTANT_ENVIRONMENT = {
    "MCP_LOG_LEVEL": frozenset({"error", "warning", "info"}),
    "NO_COLOR": frozenset({"1"}),
    "PYTHONUNBUFFERED": frozenset({"1"}),
}
_APPROVED_LAUNCH = object()


class MCPStdioConfigError(ValueError):
    """Safe configuration failure; never includes process or secret details."""


class MCPStdioApprovedLaunchManifestStorePort(Protocol):
    """Security authority that returns only previously approved launch records."""

    def get_approved(self, server_id: str) -> MCPStdioLaunchManifest | None: ...


@dataclass(frozen=True, slots=True)
class MCPStdioLaunchManifest:
    """Reviewed non-secret authority record for one exact stdio launch."""

    server_id: str
    manifest_revision: int
    endpoint_identity: str
    credential_subject_id: str
    transport_generation: int
    approval_revision: int
    approval_status: str
    executable: str = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    protocol_profile: str = "legacy_2025_11_25"
    cwd: str | None = field(default=None, repr=False)
    environment: Mapping[str, str] | None = field(default=None, repr=False)
    secret_env_refs: Mapping[str, str] | None = field(default=None, repr=False)
    max_argv_items: int = 64
    max_argv_bytes: int = 24 * 1024

    def __post_init__(self) -> None:
        for value in (self.server_id, self.endpoint_identity, self.credential_subject_id):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise MCPStdioConfigError("MCP stdio manifest identity is invalid")
        for value in (self.manifest_revision, self.transport_generation, self.approval_revision):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise MCPStdioConfigError("MCP stdio manifest revision is invalid")
        if self.approval_status != "approved":
            raise MCPStdioConfigError("MCP stdio launch manifest is not approved")
        if self.protocol_profile not in {"legacy_2025_11_25", "stateless_2026_07_28"}:
            raise MCPStdioConfigError("MCP stdio protocol profile is invalid")
        object.__setattr__(self, "executable", _absolute_file(self.executable, "executable"))
        object.__setattr__(self, "argv", _arguments(self.argv, self.max_argv_items, self.max_argv_bytes))
        object.__setattr__(self, "cwd", _absolute_directory(self.cwd) if self.cwd is not None else None)
        environment = _constant_environment(self.environment)
        secret_refs = _secret_environment(self.secret_env_refs)
        _reject_environment_collisions(environment, secret_refs)
        object.__setattr__(self, "environment", MappingProxyType(environment))
        object.__setattr__(self, "secret_env_refs", MappingProxyType(secret_refs))


@dataclass(frozen=True, slots=True, init=False)
class MCPStdioConnectionConfig:
    """Authority-resolved launch bound to an exact MCP Host connection."""

    server_id: str
    manifest_revision: int
    endpoint_identity: str
    credential_subject_id: str
    transport_generation: int
    approval_revision: int
    protocol_profile: str
    executable: str = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    cwd: str | None = field(default=None, repr=False)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    secret_env_refs: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __init__(self, *, _approval: object, manifest: MCPStdioLaunchManifest) -> None:
        if _approval is not _APPROVED_LAUNCH:
            raise MCPStdioConfigError("MCP stdio launch requires manifest authority")
        for name in (
            "server_id", "manifest_revision", "endpoint_identity", "credential_subject_id",
            "transport_generation", "approval_revision", "executable", "argv", "cwd",
            "environment", "secret_env_refs", "protocol_profile",
        ):
            object.__setattr__(self, name, getattr(manifest, name))

    def assert_matches(self, connection: MCPHostConnectionConfig) -> None:
        if not isinstance(connection, MCPHostConnectionConfig) or (
            self.server_id != connection.server_id
            or self.manifest_revision != connection.manifest_revision
            or self.endpoint_identity != connection.endpoint_identity
            or self.credential_subject_id != connection.credential_subject_id
            or self.transport_generation != connection.transport_generation
            or self.protocol_profile != connection.protocol_profile
        ):
            raise MCPStdioConfigError("MCP stdio launch identity does not match Host connection")

    def build_environment(self) -> dict[str, str]:
        """Build a non-secret child environment; legacy secret refs fail closed."""
        if self.secret_env_refs:
            raise MCPStdioConfigError("MCP stdio secret environment is prohibited")
        environment = _minimal_environment()
        environment.update(self.environment)
        return environment


class MCPStdioLaunchAuthority:
    """Resolves only approved manifests against the Host identity snapshot."""

    def __init__(self, store: MCPStdioApprovedLaunchManifestStorePort) -> None:
        if not callable(getattr(store, "get_approved", None)):
            raise MCPStdioConfigError("MCP stdio launch authority is invalid")
        self._store = store

    def resolve(self, connection: MCPHostConnectionConfig) -> MCPStdioConnectionConfig:
        if not isinstance(connection, MCPHostConnectionConfig):
            raise MCPStdioConfigError("MCP stdio Host connection is invalid")
        try:
            manifest = self._store.get_approved(connection.server_id)
        except Exception:
            raise MCPStdioConfigError("MCP stdio launch authority is unavailable") from None
        if manifest is None:
            raise MCPStdioConfigError("MCP stdio launch manifest is unavailable")
        if not isinstance(manifest, MCPStdioLaunchManifest) or manifest.approval_status != "approved":
            raise MCPStdioConfigError("MCP stdio launch authority returned an invalid record")
        config = MCPStdioConnectionConfig(_approval=_APPROVED_LAUNCH, manifest=manifest)
        config.assert_matches(connection)
        return config


def _absolute_file(value: object, label: str) -> str:
    if not isinstance(value, str) or not _safe_text(value):
        raise MCPStdioConfigError(f"MCP {label} is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise MCPStdioConfigError(f"MCP {label} must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise MCPStdioConfigError(f"MCP {label} is unavailable") from None
    if not resolved.is_file():
        raise MCPStdioConfigError(f"MCP {label} must be a file")
    if resolved.name.casefold() in _FORBIDDEN_EXECUTABLES or resolved.suffix.casefold() in _FORBIDDEN_EXECUTABLE_SUFFIXES:
        raise MCPStdioConfigError(f"MCP {label} is not an approved direct executable")
    return str(resolved)


def _absolute_directory(value: object) -> str:
    if not isinstance(value, str) or not _safe_text(value):
        raise MCPStdioConfigError("MCP cwd is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise MCPStdioConfigError("MCP cwd must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise MCPStdioConfigError("MCP cwd is unavailable") from None
    if not resolved.is_dir():
        raise MCPStdioConfigError("MCP cwd must be a directory")
    return str(resolved)


def _arguments(value: object, max_items: object, max_bytes: object) -> tuple[str, ...]:
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 128:
        raise MCPStdioConfigError("MCP argv item limit is invalid")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 256 <= max_bytes <= 65_536:
        raise MCPStdioConfigError("MCP argv byte limit is invalid")
    if not isinstance(value, tuple) or len(value) > max_items:
        raise MCPStdioConfigError("MCP argv is invalid")
    if any(not isinstance(item, str) or not item or not _safe_text(item) for item in value):
        raise MCPStdioConfigError("MCP argv is invalid")
    if sum(len(item.encode("utf-8")) for item in value) > max_bytes:
        raise MCPStdioConfigError("MCP argv exceeds the byte limit")
    return tuple(value)


def _constant_environment(value: object) -> dict[str, str]:
    raw = _environment_mapping(value, "constant environment")
    for name, item in raw.items():
        if name not in _SAFE_CONSTANT_ENVIRONMENT or item not in _SAFE_CONSTANT_ENVIRONMENT[name]:
            raise MCPStdioConfigError("MCP constant environment is not allowlisted")
    return raw


def _secret_environment(value: object) -> dict[str, str]:
    raw = _environment_mapping(value, "secret environment reference")
    if any(not _SECRET_REF.fullmatch(item) for item in raw.values()):
        raise MCPStdioConfigError("MCP secret environment reference is invalid")
    return raw


def _environment_mapping(value: object, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 64:
        raise MCPStdioConfigError(f"MCP {label} is invalid")
    normalized: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise MCPStdioConfigError(f"MCP {label} name is invalid")
        if not isinstance(item, str) or not item or not _safe_text(item):
            raise MCPStdioConfigError(f"MCP {label} value is invalid")
        normalized[name] = item
    if sum(len(name.encode("utf-8")) + len(item.encode("utf-8")) for name, item in normalized.items()) > 16 * 1024:
        raise MCPStdioConfigError(f"MCP {label} exceeds the byte limit")
    return normalized


def _reject_environment_collisions(environment: Mapping[str, str], secret_refs: Mapping[str, str]) -> None:
    names: dict[str, str] = {}
    for name in (*environment, *secret_refs):
        key = name.casefold() if os.name == "nt" else name
        if key in names:
            raise MCPStdioConfigError("MCP environment names conflict")
        names[key] = name


def _minimal_environment() -> dict[str, str]:
    environment = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE", "TMP", "TEMP"):
        value = os.environ.get(key)
        if isinstance(value, str) and value and _safe_text(value):
            environment[key] = value
    return environment


def _safe_text(value: str) -> bool:
    if "\x00" in value or "\r" in value or "\n" in value:
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    return True
