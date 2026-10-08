from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit


class SourceSpecError(ValueError):
    """Raised when a user-provided source locator is ambiguous or unsafe."""


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_PACKAGE = re.compile(r"^(npm|pypi):([A-Za-z0-9@._/-]{1,180})(?:@|==)([A-Za-z0-9._+~-]{1,96})$")
_MARKETPLACE = re.compile(r"^marketplace:([a-z0-9][a-z0-9._-]{1,63})@([a-z0-9][a-z0-9._-]{1,63})$")
_GITHUB_SHORTHAND = re.compile(r"^([A-Za-z0-9_.-]{1,80})/([A-Za-z0-9_.-]{1,100})$")


@dataclass(frozen=True, slots=True)
class SourceSpec:
    request_id: str
    kind: str
    locator: str
    requested_ref: str | None = None
    subpath: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not _REQUEST_ID.fullmatch(self.request_id):
            raise SourceSpecError("source request id is invalid")
        if self.kind not in {
            "local_selection",
            "github_repository",
            "git_repository",
            "https_archive",
            "registry_package",
            "marketplace_entry",
            "mcp_endpoint",
        }:
            raise SourceSpecError("source kind is invalid")
        _safe_text(self.locator, "source locator", maximum=512)
        if self.requested_ref is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+~-]{0,159}", self.requested_ref):
                raise SourceSpecError("requested source revision is invalid")
        if self.subpath is not None:
            _safe_relative_path(self.subpath)
        if self.display_name is not None:
            _safe_text(self.display_name, "source display name", maximum=160)


def parse_source_spec(
    raw: str,
    *,
    request_id: str,
    requested_ref: str | None = None,
    subpath: str | None = None,
) -> SourceSpec:
    """Classify one explicit locator without network access or filesystem I/O."""

    value = _safe_text(raw.strip() if isinstance(raw, str) else raw, "source locator", maximum=512)
    package = _PACKAGE.fullmatch(value)
    if package:
        ecosystem, name, version = package.groups()
        return SourceSpec(
            request_id=request_id,
            kind="registry_package",
            locator=f"{ecosystem}:{name}@{version}",
            requested_ref=version,
            subpath=subpath,
            display_name=name,
        )
    marketplace = _MARKETPLACE.fullmatch(value)
    if marketplace:
        plugin, catalog = marketplace.groups()
        return SourceSpec(
            request_id=request_id,
            kind="marketplace_entry",
            locator=f"marketplace:{plugin}@{catalog}",
            requested_ref=requested_ref,
            subpath=subpath,
            display_name=plugin,
        )
    if value.startswith("crp://source-selections/"):
        parsed_selection = urlsplit(value)
        selection_id = parsed_selection.path.removeprefix("/")
        if (
            parsed_selection.scheme != "crp"
            or parsed_selection.hostname != "source-selections"
            or parsed_selection.query
            or parsed_selection.fragment
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{7,127}", selection_id)
        ):
            raise SourceSpecError("local source selection reference is invalid")
        return SourceSpec(
            request_id=request_id,
            kind="local_selection",
            locator=value,
            requested_ref=requested_ref,
            subpath=subpath,
        )
    shorthand = _GITHUB_SHORTHAND.fullmatch(value)
    if shorthand:
        owner, repository = shorthand.groups()
        return SourceSpec(
            request_id=request_id,
            kind="github_repository",
            locator=f"https://github.com/{owner}/{repository.removesuffix('.git')}",
            requested_ref=requested_ref,
            subpath=subpath,
            display_name=repository.removesuffix(".git"),
        )
    return _url_spec(
        value,
        request_id=request_id,
        requested_ref=requested_ref,
        subpath=subpath,
    )


def _url_spec(value: str, *, request_id: str, requested_ref: str | None, subpath: str | None) -> SourceSpec:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise SourceSpecError("source URL is invalid") from error
    if parsed.scheme not in {"https", "git+https", "mcp+https"} or not parsed.hostname:
        raise SourceSpecError("source locator is unsupported or ambiguous")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SourceSpecError("source URL cannot contain credentials, query, or fragment")
    if port not in {None, 443}:
        raise SourceSpecError("source URL port is not allowed")
    scheme = "https" if parsed.scheme in {"https", "mcp+https"} else "git+https"
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        raise SourceSpecError("source URL path is required")
    canonical = urlunsplit((scheme, parsed.hostname.lower(), path, "", ""))
    if parsed.scheme == "mcp+https":
        kind = "mcp_endpoint"
    elif parsed.hostname.lower() == "github.com" and len(PurePosixPath(path).parts) == 3:
        kind = "github_repository"
        canonical = canonical.removesuffix(".git")
    elif parsed.scheme == "git+https" or path.endswith(".git"):
        kind = "git_repository"
    elif path.lower().endswith((".zip", ".tar.gz", ".tgz")):
        kind = "https_archive"
    else:
        raise SourceSpecError("HTTPS source must identify a repository, archive, or MCP endpoint")
    return SourceSpec(
        request_id=request_id,
        kind=kind,
        locator=canonical,
        requested_ref=requested_ref,
        subpath=subpath,
        display_name=PurePosixPath(path).name.removesuffix(".git"),
    )


def _safe_relative_path(value: str) -> None:
    _safe_text(value, "source subpath")
    if "\\" in value:
        raise SourceSpecError("source subpath is unsafe")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} or part.rstrip(" .") != part for part in path.parts):
        raise SourceSpecError("source subpath is unsafe")


def _safe_text(value: object, label: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SourceSpecError(f"{label} is invalid")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise SourceSpecError(f"{label} is invalid")
    return value
