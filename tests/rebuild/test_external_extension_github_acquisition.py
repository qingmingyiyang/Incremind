from __future__ import annotations

import io
import json
import zipfile

import pytest

from core.external_extension_runtime.github_acquisition import (
    GitHubAcquisitionError,
    GitHubArtifactAcquirer,
    GitHubSourceResolver,
)
from core.external_extension_runtime.outbound_fetch import FetchedBytes, OutboundFetchError
from core.external_extensions import ResolvedSource, SourceSpec
from core.external_extensions.source_spec import SourceSpecError


COMMIT = "a" * 40
ARTIFACT_REF = "crp://extension-artifacts/install-intent-1001/resolve-operation-1001"


class _Fetcher:
    def __init__(self, responses: dict[str, FetchedBytes | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def fetch(self, url: str, **kwargs: object) -> FetchedBytes:
        self.calls.append({"url": url, **kwargs})
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _response(url: str, body: bytes, content_type: str) -> FetchedBytes:
    return FetchedBytes(url, body, content_type, "93.184.216.34")


def _source(*, ref: str | None = None, locator: str = "https://github.com/example/fixture") -> SourceSpec:
    return SourceSpec("source-request-1001", "github_repository", locator, requested_ref=ref)


def _resolved(*, revision: str = COMMIT, locator: str = "https://github.com/example/fixture") -> ResolvedSource:
    return ResolvedSource("github_repository", locator, revision, ARTIFACT_REF)


def _zip(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return stream.getvalue()


def test_full_lowercase_commit_is_pinned_without_a_network_request() -> None:
    fetcher = _Fetcher({})
    resolved = GitHubSourceResolver(fetcher).resolve(
        _source(ref=COMMIT), artifact_ref=ARTIFACT_REF, operation_id="resolve-operation-1001"
    )
    assert resolved.immutable_revision == COMMIT
    assert resolved.trust_tier == "untrusted"
    assert fetcher.calls == []


@pytest.mark.parametrize(("ref", "suffix"), (("main", "main"), (None, "HEAD"), ("release/v1", "release%2Fv1")))
def test_branch_tag_or_head_is_resolved_through_only_the_github_api(ref: str | None, suffix: str) -> None:
    url = f"https://api.github.com/repos/example/fixture/commits/{suffix}"
    fetcher = _Fetcher({url: _response(url, json.dumps({"sha": COMMIT}).encode(), "application/vnd.github+json")})
    resolved = GitHubSourceResolver(fetcher).resolve(
        _source(ref=ref), artifact_ref=ARTIFACT_REF, operation_id="resolve-operation-1001"
    )
    assert resolved.immutable_revision == COMMIT
    assert fetcher.calls == [{
        "url": url,
        "allowed_hosts": frozenset({"api.github.com"}),
        "max_bytes": 256 * 1024,
        "accepted_content_types": frozenset({"application/json", "application/vnd.github+json", "application/vnd.github.v3+json"}),
    }]


@pytest.mark.parametrize("payload", (b"not-json", b'{"sha":"A"}', b'{"sha":"A"' * 41 + b'}'))
def test_resolution_rejects_api_errors_and_commit_drift(payload: bytes) -> None:
    url = "https://api.github.com/repos/example/fixture/commits/main"
    fetcher = _Fetcher({url: _response(url, payload, "application/json")})
    with pytest.raises(GitHubAcquisitionError, match="commit response"):
        GitHubSourceResolver(fetcher).resolve(
            _source(ref="main"), artifact_ref=ARTIFACT_REF, operation_id="resolve-operation-1001"
        )
    failing = _Fetcher({url: OutboundFetchError("unexpected status")})
    with pytest.raises(GitHubAcquisitionError, match="resolution failed"):
        GitHubSourceResolver(failing).resolve(
            _source(ref="main"), artifact_ref=ARTIFACT_REF, operation_id="resolve-operation-1001"
        )


@pytest.mark.parametrize(
    "locator",
    (
        "https://github.com/example/fixture/extra",
        "https://github.com/example/fixture.git",
        "https://github.com/example/fixture/",
        "https://github.com/example/%2e%2e/fixture",
        "https://github.com/example/fixture?x=y",
        "https://evil.example/example/fixture",
    ),
)
def test_resolution_rejects_noncanonical_or_injected_repository_locator(locator: str) -> None:
    fetcher = _Fetcher({})
    with pytest.raises((SourceSpecError, GitHubAcquisitionError)):
        GitHubSourceResolver(fetcher).resolve(
            _source(ref=COMMIT, locator=locator), artifact_ref=ARTIFACT_REF, operation_id="resolve-operation-1001"
        )
    assert fetcher.calls == []


@pytest.mark.parametrize("ref", ("../main", "main/../evil", "main//evil", "main.lock", "bad?query"))
def test_resolution_rejects_ref_injection_before_network(ref: str) -> None:
    fetcher = _Fetcher({})
    with pytest.raises((SourceSpecError, GitHubAcquisitionError)):
        GitHubSourceResolver(fetcher).resolve(
            _source(ref=ref), artifact_ref=ARTIFACT_REF, operation_id="resolve-operation-1001"
        )
    assert fetcher.calls == []


def test_archive_is_pinned_to_codeload_zip_host_content_type_and_limit() -> None:
    url = f"https://codeload.github.com/example/fixture/zip/{COMMIT}"
    payload = _zip({f"fixture-{COMMIT}/SKILL.md": b"---\nname: demo\ndescription: demo\n---\n"})
    fetcher = _Fetcher({url: _response(url, payload, "application/zip")})
    inventory = GitHubArtifactAcquirer(fetcher).acquire(
        _resolved(), operation_id="acquire-operation-1001", subpath=None
    )
    assert inventory.paths == ("SKILL.md",)
    assert fetcher.calls == [{
        "url": url,
        "allowed_hosts": frozenset({"codeload.github.com"}),
        "max_bytes": 32 * 1024 * 1024,
        "accepted_content_types": frozenset({"application/zip", "application/x-zip-compressed"}),
    }]


def test_archive_rejects_a_codeload_root_that_does_not_match_repository_and_commit() -> None:
    url = f"https://codeload.github.com/example/fixture/zip/{COMMIT}"
    payload = _zip({"other-repository-main/SKILL.md": b"not this source"})
    fetcher = _Fetcher({url: _response(url, payload, "application/zip")})
    with pytest.raises(GitHubAcquisitionError, match="archive acquisition failed"):
        GitHubArtifactAcquirer(fetcher).acquire(
            _resolved(), operation_id="acquire-operation-1001", subpath=None
        )


@pytest.mark.parametrize("subpath, expected", (("skills/codex", "SKILL.md"), (".dsh/skills/deepseek", "SKILL.md")))
def test_archive_passes_codex_and_dsh_skill_subpaths_to_safe_zip_intake(subpath: str, expected: str) -> None:
    url = f"https://codeload.github.com/example/fixture/zip/{COMMIT}"
    payload = _zip({
        f"fixture-{COMMIT}/skills/codex/SKILL.md": b"codex",
        f"fixture-{COMMIT}/.dsh/skills/deepseek/SKILL.md": b"dsh",
        f"fixture-{COMMIT}/README.md": b"readme",
    })
    fetcher = _Fetcher({url: _response(url, payload, "application/x-zip-compressed")})
    inventory = GitHubArtifactAcquirer(fetcher).acquire(
        _resolved(), operation_id="acquire-operation-1001", subpath=subpath
    )
    assert inventory.paths == (expected,)


@pytest.mark.parametrize("revision", ("A" * 40, "a" * 39, None))
def test_archive_refuses_mutable_or_noncanonical_revision_without_network(revision: str | None) -> None:
    fetcher = _Fetcher({})
    with pytest.raises(GitHubAcquisitionError, match="pinned"):
        GitHubArtifactAcquirer(fetcher).acquire(
            _resolved(revision=revision), operation_id="acquire-operation-1001", subpath=None
        )
    assert fetcher.calls == []
