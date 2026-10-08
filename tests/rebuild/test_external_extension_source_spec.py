from __future__ import annotations

import pytest

from core.external_extensions import SourceSpecError, parse_source_spec


def test_explicit_source_locators_are_classified_without_network_or_filesystem_access() -> None:
    github = parse_source_spec(
        "openai/skills",
        request_id="source-request-0001",
        requested_ref="v1.2.3",
        subpath="skills/.system",
    )
    assert github.kind == "github_repository"
    assert github.locator == "https://github.com/openai/skills"
    assert github.requested_ref == "v1.2.3"
    assert github.subpath == "skills/.system"

    registry = parse_source_spec(
        "npm:@scope/example@2.0.1",
        request_id="source-request-0002",
    )
    assert registry.kind == "registry_package"
    assert registry.locator == "npm:@scope/example@2.0.1"
    assert registry.requested_ref == "2.0.1"

    marketplace = parse_source_spec(
        "marketplace:example-plugin@openai",
        request_id="source-request-0003",
    )
    assert marketplace.kind == "marketplace_entry"
    assert marketplace.display_name == "example-plugin"

    mcp = parse_source_spec(
        "mcp+https://mcp.example.test/rpc",
        request_id="source-request-0004",
    )
    assert mcp.kind == "mcp_endpoint"
    assert mcp.locator == "https://mcp.example.test/rpc"


@pytest.mark.parametrize(
    "locator",
    [
        "https://user:secret@github.com/openai/skills",
        "https://github.com/openai/skills?token=secret",
        "file:///C:/unsafe",
        "git@github.com:openai/skills.git",
        "an ambiguous package name",
    ],
)
def test_source_parser_rejects_credentials_queries_and_ambiguous_inputs(locator: str) -> None:
    with pytest.raises(SourceSpecError):
        parse_source_spec(locator, request_id="source-request-0005")


@pytest.mark.parametrize("subpath", ["../escape", "/absolute", "safe/../escape", "folder\\child"])
def test_source_parser_rejects_unsafe_subpaths(subpath: str) -> None:
    with pytest.raises(SourceSpecError, match="subpath"):
        parse_source_spec(
            "openai/skills",
            request_id="source-request-0006",
            subpath=subpath,
        )


def test_local_selection_is_an_opaque_reference_not_a_host_path() -> None:
    selected = parse_source_spec(
        "crp://source-selections/user-choice-01",
        request_id="source-request-0007",
    )
    assert selected.kind == "local_selection"
    assert selected.locator == "crp://source-selections/user-choice-01"

    with pytest.raises(SourceSpecError):
        parse_source_spec("F:\\private\\skill", request_id="source-request-0008")


@pytest.mark.parametrize(
    "locator",
    [
        "crp://source-selections/../escape",
        "crp://source-selections/%2e%2e%2fescape",
        "crp://source-selections/too/long",
        "crp://source-selections/short",
    ],
)
def test_local_selection_rejects_traversal_encoding_and_nonopaque_shapes(locator: str) -> None:
    with pytest.raises(SourceSpecError, match="selection"):
        parse_source_spec(locator, request_id="source-request-0009")
