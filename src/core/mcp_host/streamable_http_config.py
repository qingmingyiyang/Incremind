from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from .host import MCPHostConnectionConfig


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SECRET_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_APPROVED_HTTP = object()
_FORBIDDEN_HEADERS = frozenset({
    "accept", "connection", "content-length", "content-type", "host",
    "mcp-protocol-version", "mcp-session-id", "last-event-id", "mcp-method", "mcp-name",
})
_SECRET_ONLY_HEADERS = frozenset({
    "authorization", "cookie", "proxy-authorization", "x-api-key", "api-key",
})


class MCPStreamableHTTPConfigError(ValueError):
    pass


class MCPStreamableHTTPSecretInjectorPort(Protocol):
    def headers_for_wire(
        self, *, url: str, secret_header_refs: Mapping[str, str], purpose: str,
    ) -> Mapping[str, str]: ...


class MCPStreamableHTTPApprovedManifestStorePort(Protocol):
    def get_approved_http(self, server_id: str) -> "MCPStreamableHTTPManifest | None": ...


@dataclass(frozen=True, slots=True)
class MCPStreamableHTTPManifest:
    server_id: str
    manifest_revision: int
    endpoint_identity: str
    credential_subject_id: str
    transport_generation: int
    approval_revision: int
    approval_status: str
    endpoint_url: str = field(repr=False)
    protocol_profile: str = "legacy_2025_11_25"
    headers: Mapping[str, str] | None = field(default=None, repr=False)
    secret_header_refs: Mapping[str, str] | None = field(default=None, repr=False)
    timeout_seconds: float = 20.0
    max_response_bytes: int = 4 * 1024 * 1024
    max_sse_events: int = 256

    def __post_init__(self) -> None:
        for value in (self.server_id, self.endpoint_identity, self.credential_subject_id):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise MCPStreamableHTTPConfigError("MCP HTTP manifest identity is invalid")
        for value in (self.manifest_revision, self.transport_generation, self.approval_revision):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise MCPStreamableHTTPConfigError("MCP HTTP manifest revision is invalid")
        if self.approval_status != "approved":
            raise MCPStreamableHTTPConfigError("MCP HTTP manifest is not approved")
        if self.protocol_profile not in {"legacy_2025_11_25", "stateless_2026_07_28"}:
            raise MCPStreamableHTTPConfigError("MCP HTTP protocol profile is invalid")
        object.__setattr__(self, "endpoint_url", _endpoint(self.endpoint_url))
        constants = _headers(self.headers, secret=False)
        secrets = _headers(self.secret_header_refs, secret=True)
        if {name.casefold() for name in constants} & {name.casefold() for name in secrets}:
            raise MCPStreamableHTTPConfigError("MCP HTTP header names conflict")
        object.__setattr__(self, "headers", MappingProxyType(constants))
        object.__setattr__(self, "secret_header_refs", MappingProxyType(secrets))
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(self.timeout_seconds, bool) or not 0 < self.timeout_seconds <= 120:
            raise MCPStreamableHTTPConfigError("MCP HTTP timeout is invalid")
        if not isinstance(self.max_response_bytes, int) or isinstance(self.max_response_bytes, bool) or not 1024 <= self.max_response_bytes <= 16 * 1024 * 1024:
            raise MCPStreamableHTTPConfigError("MCP HTTP response limit is invalid")
        if not isinstance(self.max_sse_events, int) or isinstance(self.max_sse_events, bool) or not 1 <= self.max_sse_events <= 4096:
            raise MCPStreamableHTTPConfigError("MCP HTTP SSE event limit is invalid")


@dataclass(frozen=True, slots=True, init=False)
class MCPStreamableHTTPConnectionConfig:
    manifest: MCPStreamableHTTPManifest

    def __init__(self, *, _approval: object, manifest: MCPStreamableHTTPManifest) -> None:
        if _approval is not _APPROVED_HTTP:
            raise MCPStreamableHTTPConfigError("MCP HTTP connection requires manifest authority")
        object.__setattr__(self, "manifest", manifest)

    def assert_matches(self, connection: MCPHostConnectionConfig) -> None:
        item = self.manifest
        if (
            item.server_id != connection.server_id
            or item.manifest_revision != connection.manifest_revision
            or item.endpoint_identity != connection.endpoint_identity
            or item.credential_subject_id != connection.credential_subject_id
            or item.transport_generation != connection.transport_generation
            or item.protocol_profile != connection.protocol_profile
        ):
            raise MCPStreamableHTTPConfigError("MCP HTTP identity does not match Host connection")

    def build_headers(self, injector: MCPStreamableHTTPSecretInjectorPort, *, purpose: str) -> dict[str, str]:
        values = dict(self.manifest.headers or {})
        try:
            values.update(dict(injector.headers_for_wire(
                url=self.manifest.endpoint_url,
                secret_header_refs=self.manifest.secret_header_refs or {},
                purpose=purpose,
            )))
        except Exception:
            raise MCPStreamableHTTPConfigError("MCP HTTP secret injection is unavailable") from None
        return values


class MCPStreamableHTTPAuthority:
    def __init__(self, store: MCPStreamableHTTPApprovedManifestStorePort) -> None:
        self._store = store

    def resolve(self, connection: MCPHostConnectionConfig) -> MCPStreamableHTTPConnectionConfig:
        try:
            manifest = self._store.get_approved_http(connection.server_id)
        except Exception:
            raise MCPStreamableHTTPConfigError("MCP HTTP authority is unavailable") from None
        if manifest is None:
            raise MCPStreamableHTTPConfigError("MCP HTTP manifest is unavailable")
        config = MCPStreamableHTTPConnectionConfig(_approval=_APPROVED_HTTP, manifest=manifest)
        config.assert_matches(connection)
        return config


def _endpoint(value: object) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\r\n\x00"):
        raise MCPStreamableHTTPConfigError("MCP HTTP endpoint is invalid")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise MCPStreamableHTTPConfigError("MCP HTTP endpoint must be an absolute HTTPS URL")
    try:
        port = parsed.port
    except ValueError:
        raise MCPStreamableHTTPConfigError("MCP HTTP endpoint port is invalid") from None
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _headers(value: object, *, secret: bool) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 16:
        raise MCPStreamableHTTPConfigError("MCP HTTP headers are invalid")
    result: dict[str, str] = {}
    normalized_names: set[str] = set()
    for name, item in value.items():
        normalized_name = name.casefold() if isinstance(name, str) else ""
        if (
            not isinstance(name, str) or not _HEADER_NAME.fullmatch(name)
            or name.casefold() in _FORBIDDEN_HEADERS
            or name.casefold().startswith("mcp-param-")
            or (not secret and name.casefold() in _SECRET_ONLY_HEADERS)
            or not isinstance(item, str) or not item
            or any(char in item for char in "\r\n\x00")
            or (secret and not _SECRET_REF.fullmatch(item))
            or normalized_name in normalized_names
        ):
            raise MCPStreamableHTTPConfigError("MCP HTTP headers are invalid")
        normalized_names.add(normalized_name)
        result[name] = item
    return result
