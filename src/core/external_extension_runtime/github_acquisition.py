"""Read-only, revision-pinned GitHub acquisition for extension quarantine.

This module is an adapter over :class:`GovernedOutboundFetcher`.  It neither
persists state nor executes archive contents: the Effect handler owns retry
and recovery, and ``secure_archive`` only constructs an in-memory inventory.
"""

from __future__ import annotations

import json
import re
from typing import Final
from urllib.parse import quote, urlsplit

from core.external_extensions import ArtifactInventory, ResolvedSource, SourceSpec

from .outbound_fetch import GovernedOutboundFetcher, OutboundFetchError
from .secure_archive import SecureArchiveError, zip_bytes_to_inventory


class GitHubAcquisitionError(ValueError):
    """Raised when a GitHub source cannot become immutable artifact evidence."""


_GITHUB_HOST: Final = "github.com"
_API_HOST: Final = "api.github.com"
_CODELOAD_HOST: Final = "codeload.github.com"
_MAX_BYTES: Final = 32 * 1024 * 1024
_COMMIT: Final = re.compile(r"^[0-9a-f]{40}$")
_OWNER: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPOSITORY: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REF: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,159}$")
_API_TYPES: Final = frozenset({
    "application/json",
    "application/vnd.github+json",
    "application/vnd.github.v3+json",
})
_ZIP_TYPES: Final = frozenset({"application/zip", "application/x-zip-compressed"})


class GitHubSourceResolver:
    """Resolve a canonical GitHub repository ref to a full immutable commit."""

    def __init__(self, fetcher: GovernedOutboundFetcher) -> None:
        if not hasattr(fetcher, "fetch"):
            raise TypeError("fetcher must provide governed fetch")
        self._fetcher = fetcher

    def resolve(
        self,
        source: SourceSpec,
        *,
        artifact_ref: str,
        operation_id: str,
    ) -> ResolvedSource:
        owner, repository = _source_repository(source)
        _operation_id(operation_id)
        requested = source.requested_ref
        if requested is not None and _COMMIT.fullmatch(requested):
            commit = requested
        else:
            ref = _safe_ref(requested) if requested is not None else "HEAD"
            endpoint = (
                f"https://{_API_HOST}/repos/{owner}/{repository}/commits/"
                f"{quote(ref, safe='')}"
            )
            try:
                response = self._fetcher.fetch(
                    endpoint,
                    allowed_hosts=frozenset({_API_HOST}),
                    max_bytes=256 * 1024,
                    accepted_content_types=_API_TYPES,
                )
            except (OutboundFetchError, ValueError) as error:
                raise GitHubAcquisitionError("GitHub commit resolution failed") from error
            commit = _commit_from_response(response.body)
        return ResolvedSource(
            source_kind="github_repository",
            canonical_locator=f"https://{_GITHUB_HOST}/{owner}/{repository}",
            immutable_revision=commit,
            artifact_ref=artifact_ref,
            # A remote resolver must never self-attest reviewed or managed trust.
            trust_tier="untrusted",
        )


class GitHubArtifactAcquirer:
    """Fetch one pinned GitHub archive and convert it to safe in-memory files."""

    def __init__(self, fetcher: GovernedOutboundFetcher) -> None:
        if not hasattr(fetcher, "fetch"):
            raise TypeError("fetcher must provide governed fetch")
        self._fetcher = fetcher

    def acquire(
        self,
        source: ResolvedSource,
        *,
        operation_id: str,
        subpath: str | None,
    ) -> ArtifactInventory:
        owner, repository = _resolved_repository(source)
        _operation_id(operation_id)
        commit = source.immutable_revision
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise GitHubAcquisitionError("GitHub artifact source must be pinned to a lowercase full commit")
        endpoint = f"https://{_CODELOAD_HOST}/{owner}/{repository}/zip/{commit}"
        try:
            response = self._fetcher.fetch(
                endpoint,
                allowed_hosts=frozenset({_CODELOAD_HOST}),
                max_bytes=_MAX_BYTES,
                accepted_content_types=_ZIP_TYPES,
            )
            return zip_bytes_to_inventory(
                response.body,
                subpath=subpath,
                expected_github_root=f"{repository}-{commit}",
            )
        except (OutboundFetchError, SecureArchiveError, ValueError) as error:
            raise GitHubAcquisitionError("GitHub archive acquisition failed") from error


def _source_repository(source: SourceSpec) -> tuple[str, str]:
    if not isinstance(source, SourceSpec) or source.kind != "github_repository":
        raise GitHubAcquisitionError("source must be a GitHub repository")
    return _repository_from_locator(source.locator)


def _resolved_repository(source: ResolvedSource) -> tuple[str, str]:
    if not isinstance(source, ResolvedSource) or source.source_kind != "github_repository":
        raise GitHubAcquisitionError("resolved source must be a GitHub repository")
    return _repository_from_locator(source.canonical_locator)


def _repository_from_locator(locator: object) -> tuple[str, str]:
    if not isinstance(locator, str):
        raise GitHubAcquisitionError("GitHub repository locator is invalid")
    try:
        parsed = urlsplit(locator)
    except ValueError as error:
        raise GitHubAcquisitionError("GitHub repository locator is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.netloc != _GITHUB_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GitHubAcquisitionError("GitHub repository locator is not canonical")
    parts = parsed.path.split("/")
    if (
        len(parts) != 3
        or parts[0]
        or not _OWNER.fullmatch(parts[1])
        or not _REPOSITORY.fullmatch(parts[2])
        or parts[2].lower().endswith(".git")
    ):
        raise GitHubAcquisitionError("GitHub repository owner or name is invalid")
    # Reject normalizations such as trailing slash, .git and percent escapes.
    owner, repository = parts[1], parts[2]
    if locator != f"https://{_GITHUB_HOST}/{owner}/{repository}":
        raise GitHubAcquisitionError("GitHub repository locator is not canonical")
    return owner, repository


def _safe_ref(value: object) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise GitHubAcquisitionError("GitHub ref is invalid")
    if value in {".", ".."} or value.startswith("/") or value.endswith("/") or "//" in value:
        raise GitHubAcquisitionError("GitHub ref is invalid")
    if any(part in {".", ".."} or part.endswith(".") or part.endswith(".lock") for part in value.split("/")):
        raise GitHubAcquisitionError("GitHub ref is invalid")
    return value


def _commit_from_response(payload: object) -> str:
    if not isinstance(payload, bytes):
        raise GitHubAcquisitionError("GitHub commit response is invalid")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubAcquisitionError("GitHub commit response is invalid") from error
    if not isinstance(value, dict) or set(value) != {"sha"} and "sha" not in value:
        raise GitHubAcquisitionError("GitHub commit response is invalid")
    sha = value["sha"]
    if not isinstance(sha, str) or not _COMMIT.fullmatch(sha):
        raise GitHubAcquisitionError("GitHub commit response did not contain a full lowercase commit")
    return sha


def _operation_id(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~:-]{7,159}", value):
        raise GitHubAcquisitionError("operation id is invalid")
