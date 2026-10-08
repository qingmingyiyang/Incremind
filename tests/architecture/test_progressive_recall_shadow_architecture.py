from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "progressive_recall_shadow.py"
)
SCHEMA = (
    ROOT
    / "core-contracts"
    / "rebuild"
    / "progressive_recall_shadow_trace.schema.json"
)
VALIDATOR = ROOT / "tools" / "validate_rebuild_contracts.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            str(key)
            for key in value
        } | {
            nested
            for item in value.values()
            for nested in _keys(item)
        }
    if isinstance(value, list):
        return {
            nested
            for item in value
            for nested in _keys(item)
        }
    return set()


def test_shadow_router_keeps_read_only_runtime_boundaries() -> None:
    assert _imports(MODULE) <= {
        "__future__",
        "collections",
        "dataclasses",
        "hashlib",
        "math",
        "re",
        "core",
        "time",
        "unicodedata",
    }
    source = MODULE.read_text(encoding="utf-8").lower()

    for forbidden in (
        "from backend",
        "import backend",
        "src.frontend",
        "electron",
        "model_gateway",
        "model_provider",
        "provider_route",
        "api_key",
        "from core.team_memory",
        "import core.team_memory",
        "project_memory_recall",
        "workbench_direct_question",
    ):
        assert forbidden not in source


def test_shadow_trace_contract_forbids_query_and_evidence_body() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    schema_keys = _keys(schema)

    assert "query_fingerprint" in schema["required"]
    assert {"query", "snippet", "content", "source_refs"}.isdisjoint(
        schema_keys
    )
    assert schema["properties"]["safety"]["properties"][
        "prompt_unchanged"
    ] == {"const": True}
    assert schema["properties"]["comparison"]["properties"][
        "production_cutover_allowed"
    ] == {"const": False}


def test_shadow_contract_is_registered_in_global_validator() -> None:
    source = VALIDATOR.read_text(encoding="utf-8")

    assert "progressive_recall_shadow_trace.schema.json" in source
