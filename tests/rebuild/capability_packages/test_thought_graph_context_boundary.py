from __future__ import annotations

import ast
from pathlib import Path

from core.context_graph import CapabilityPackageLoader


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "src/core/capability_packages/thought_graph_context"


def test_manifest_declares_only_implemented_contribution_categories() -> None:
    path = PACKAGE / "manifest.json"
    manifest = CapabilityPackageLoader().load_manifest(path)
    assert manifest.capability_id == "thought_graph_context"
    assert manifest.capability_revision == "4.2.0"
    assert manifest.core_api == "2"
    assert manifest.display_name == "LineMap"
    assert manifest.contributions == (
        "context_compiler_extension", "evaluation_fixture", "exporter", "importer", "migration_adapter",
        "proposal_adapter", "read_only_preview", "renderer",
    )
    assert {item["id"] for item in manifest.context} >= {
        "context.import.thoughtdag", "context.import.markdown_graph",
        "context.export.thoughtdag", "context.export.markdown_graph",
        "context.proposal.handoff",
        "context.evaluate.model_suite_definition",
    }


def test_package_has_no_execution_secret_or_formal_writer_dependencies() -> None:
    forbidden_modules = {
        "core.effect_log", "core.job_runner", "core.model_gateway",
        "core.mcp_host", "core.memory_core", "core.document_engine",
        "core.project_skill_core", "backend",
    }
    violations = []
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                normalized = name.lstrip(".")
                if any(normalized == forbidden or normalized.startswith(forbidden + ".") for forbidden in forbidden_modules):
                    violations.append(f"{path.name}:{name}")
    assert violations == []


def test_package_source_contains_no_provider_secret_or_recovery_entrypoint() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in PACKAGE.glob("*.py"))
    forbidden_definitions = (
        "def recover(", "def retry(", "def resolve_secret(", "def call_provider(",
        "class effectrunner", "class workflowruntime", "class planner",
    )
    assert [item for item in forbidden_definitions if item in combined] == []
