"""Compile reviewed MCP schema annotations into bounded HTTP header values.

The remote catalog is deliberately absent from this module.  A projection plan
is derived once from the locally reviewed input schema and later reads only
values at those frozen property paths.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType


_HEADER_SUFFIX = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_SENTINEL_PREFIX = "=?base64?"
_SENTINEL_SUFFIX = "?="


class HeaderProjectionError(ValueError):
    """A reviewed annotation cannot safely become an HTTP parameter header."""


@dataclass(frozen=True, slots=True)
class HeaderProjectionEntry:
    path: tuple[str, ...]
    header_name: str
    value_type: str


@dataclass(frozen=True, slots=True)
class HeaderProjectionPlan:
    """An immutable, authority-derived list of parameter header projections."""

    entries: tuple[HeaderProjectionEntry, ...] = ()

    def project(self, arguments: Mapping[str, object]) -> Mapping[str, str]:
        if not isinstance(arguments, Mapping):
            raise HeaderProjectionError("MCP parameter header arguments are invalid")
        headers: dict[str, str] = {}
        for entry in self.entries:
            value, found = _value_at(arguments, entry.path)
            if not found or value is None:
                continue
            headers[entry.header_name] = _encode_typed_value(value, entry.value_type)
        return MappingProxyType(headers)


EMPTY_HEADER_PROJECTION = HeaderProjectionPlan()


def compile_header_projection(schema: Mapping[str, object]) -> HeaderProjectionPlan:
    """Compile every allowed ``x-mcp-header`` annotation in a reviewed schema.

    An annotation is valid only on a schema node reached exclusively through a
    root ``properties`` chain.  Discovering one anywhere else invalidates the
    whole policy: JSON Schema composition and array traversal are intentionally
    not an authority path for model-supplied wire values.
    """
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise HeaderProjectionError("MCP parameter header schema is invalid")
    entries: list[HeaderProjectionEntry] = []
    _visit(schema, (), True, entries)
    seen: set[str] = set()
    for entry in entries:
        key = entry.header_name.casefold()
        if key in seen:
            raise HeaderProjectionError("MCP parameter header names conflict")
        seen.add(key)
    return HeaderProjectionPlan(tuple(entries))


def encode_header_value(value: str) -> str:
    """Return a single-line HTTP field value, preserving only safe ASCII."""
    if not isinstance(value, str):
        raise HeaderProjectionError("MCP parameter header string is invalid")
    if _requires_base64(value):
        encoded = base64.b64encode(value.encode("utf-8", "strict")).decode("ascii")
        return f"{_SENTINEL_PREFIX}{encoded}{_SENTINEL_SUFFIX}"
    return value


def _visit(
    value: object,
    path: tuple[str, ...],
    property_path: bool,
    entries: list[HeaderProjectionEntry],
) -> None:
    if isinstance(value, Mapping):
        if "x-mcp-header" in value:
            annotation = value["x-mcp-header"]
            if not property_path or not path:
                raise HeaderProjectionError("MCP parameter header annotation location is invalid")
            if not isinstance(annotation, str) or not _HEADER_SUFFIX.fullmatch(annotation):
                raise HeaderProjectionError("MCP parameter header name is invalid")
            value_type = value.get("type")
            if value_type not in {"string", "integer", "boolean"}:
                raise HeaderProjectionError("MCP parameter header type is invalid")
            entries.append(HeaderProjectionEntry(path, f"Mcp-Param-{annotation}", value_type))
        for key, child in value.items():
            if key == "x-mcp-header":
                continue
            if key == "properties" and isinstance(child, Mapping):
                for name, property_schema in child.items():
                    if not isinstance(name, str) or not name:
                        raise HeaderProjectionError("MCP parameter header property is invalid")
                    _visit(property_schema, (*path, name), property_path, entries)
            else:
                _visit(child, path, False, entries)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            _visit(child, path, False, entries)


def _value_at(arguments: Mapping[str, object], path: tuple[str, ...]) -> tuple[object, bool]:
    current: object = arguments
    for name in path:
        if not isinstance(current, Mapping) or name not in current:
            return None, False
        current = current[name]
    return current, True


def _encode_typed_value(value: object, value_type: str) -> str:
    if value_type == "string":
        return encode_header_value(value) if isinstance(value, str) else _invalid_value()
    if value_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool) or abs(value) > _MAX_SAFE_INTEGER:
            return _invalid_value()
        return str(value)
    if value_type == "boolean":
        if not isinstance(value, bool):
            return _invalid_value()
        return "true" if value else "false"
    return _invalid_value()


def _invalid_value() -> str:
    raise HeaderProjectionError("MCP parameter header value is invalid")


def _requires_base64(value: str) -> bool:
    return (
        (bool(value) and (value[0] == " " or value[-1] == " "))
        or (value.startswith(_SENTINEL_PREFIX) and value.endswith(_SENTINEL_SUFFIX))
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
    )
