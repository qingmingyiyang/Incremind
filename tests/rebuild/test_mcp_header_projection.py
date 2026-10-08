from __future__ import annotations

import pytest

from core.mcp_host.header_projection import HeaderProjectionError, compile_header_projection, encode_header_value


def test_compiles_nested_property_paths_and_projects_only_present_primitive_values() -> None:
    plan = compile_header_projection({
        "type": "object",
        "properties": {
            "query": {"type": "string", "x-mcp-header": "Query"},
            "limit": {"type": "integer", "x-mcp-header": "Limit"},
            "enabled": {"type": "boolean", "x-mcp-header": "Enabled"},
            "nested": {"type": "object", "properties": {
                "label": {"type": "string", "x-mcp-header": "Label"},
            }},
        },
    })
    assert plan.project({"query": "alpha", "limit": 2, "enabled": False, "nested": {"label": "beta"}}) == {
        "Mcp-Param-Query": "alpha", "Mcp-Param-Limit": "2",
        "Mcp-Param-Enabled": "false", "Mcp-Param-Label": "beta",
    }
    assert plan.project({"query": None, "nested": {}}) == {}


@pytest.mark.parametrize("value", [" snow", "snow ", "a\nb", "a\tb", "中文", "=?base64?abc?="])
def test_encodes_unsafe_strings_with_exact_utf8_base64_sentinel(value: str) -> None:
    encoded = encode_header_value(value)
    assert encoded.startswith("=?base64?") and encoded.endswith("?=")
    assert "\r" not in encoded and "\n" not in encoded


@pytest.mark.parametrize("value", ["", "a b", "=?BASE64?abc?=", "=?base64?abc?"])
def test_preserves_allowed_internal_whitespace_and_nonmatching_sentinel_strings(value: str) -> None:
    assert encode_header_value(value) == value


@pytest.mark.parametrize("schema", [
    {"type": "object", "x-mcp-header": "Root"},
    {"type": "object", "properties": {"a": {"type": "number", "x-mcp-header": "A"}}},
    {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "bad space"}}},
    {"type": "object", "items": {"type": "string", "x-mcp-header": "Array"}},
    {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "Same"}, "b": {"type": "string", "x-mcp-header": "same"}}},
])
def test_rejects_unsupported_annotation_locations_types_and_casefold_collisions(schema) -> None:
    with pytest.raises(HeaderProjectionError):
        compile_header_projection(schema)


def test_accepts_unbounded_rfc_token_suffix() -> None:
    suffix = "A" * 65
    plan = compile_header_projection({"type": "object", "properties": {"value": {"type": "string", "x-mcp-header": suffix}}})
    assert plan.project({"value": "ok"}) == {f"Mcp-Param-{suffix}": "ok"}


@pytest.mark.parametrize("value", [True, 1 << 53, -(1 << 53)])
def test_rejects_invalid_integer_values(value: object) -> None:
    plan = compile_header_projection({"type": "object", "properties": {"value": {"type": "integer", "x-mcp-header": "Value"}}})
    with pytest.raises(HeaderProjectionError):
        plan.project({"value": value})
