from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRILLDOWN = ROOT / "src/core/product_core/progressive_recall_drilldown.py"
READER = ROOT / "src/core/product_core/progressive_recall_authority_reader.py"
CONTEXT_SCHEMA = (
    ROOT / "core-contracts/rebuild/progressive_recall_context_bundle.schema.json"
)
TRACE_SCHEMA = (
    ROOT / "core-contracts/rebuild/progressive_recall_drilldown_trace.schema.json"
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def _property_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for child in value.values():
            names.update(_property_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_property_names(child))
    return names


def test_drilldown_has_no_backend_ui_electron_provider_or_team_memory_dependency() -> None:
    imports = _imports(DRILLDOWN) | _imports(READER)
    forbidden = (
        "backend",
        "frontend",
        "electron",
        "model_provider",
        "provider_runtime",
        "team_memory",
        "api.routes",
    )
    assert not {
        name for name in imports if any(token in name for token in forbidden)
    }


def test_trace_schema_cannot_store_content_query_locator_path_or_urls() -> None:
    schema = json.loads(TRACE_SCHEMA.read_text(encoding="utf-8"))
    names = _property_names(schema)
    assert names.isdisjoint(
        {
            "content",
            "query",
            "locator",
            "path",
            "url",
            "source_refs",
            "quote",
            "snippet",
        }
    )


def test_context_schema_is_ephemeral_and_not_provider_ready() -> None:
    schema = json.loads(CONTEXT_SCHEMA.read_text(encoding="utf-8"))
    safety = schema["properties"]["safety"]["properties"]
    assert safety["ephemeral"]["const"] is True
    assert safety["persistence_allowed"]["const"] is False
    assert safety["logging_allowed"]["const"] is False
    assert safety["provider_egress_allowed"]["const"] is False
    assert safety["business_writes_allowed"]["const"] is False


def test_production_answer_paths_do_not_import_phase_e() -> None:
    production_paths = (
        ROOT / "src/core/product_core/project_memory_recall.py",
        ROOT / "src/core/product_core/workbench_direct_question.py",
        ROOT / "src/core/product_core/answer_model_request.py",
    )
    for path in production_paths:
        assert "progressive_recall_drilldown" not in path.read_text(encoding="utf-8")


def test_contract_validator_registers_both_phase_e_schemas() -> None:
    validator = (ROOT / "tools/validate_rebuild_contracts.py").read_text(
        encoding="utf-8"
    )
    assert "progressive_recall_context_bundle.schema.json" in validator
    assert "progressive_recall_drilldown_trace.schema.json" in validator
