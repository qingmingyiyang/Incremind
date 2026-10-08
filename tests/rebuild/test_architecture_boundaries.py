from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
REBUILD = ROOT / "src" / "core"
MODULES = {
    "companion_core",
    "product_core",
    "memory_core",
    "ingestion_core",
    "document_engine",
    "project_skill_core",
    "search_and_recall",
    "model_gateway",
    "job_runner",
    "storage_provider",
    "platform_adapter",
    "application_skill",
    "ai_boundary",
    "ai_tooling",
    "ai_kernel",
    "mcp_host",
}
BANNED_EXTERNAL_ROOTS = {
    "backend",
    "fastapi",
    "langgraph",
    "lancedb",
    "litellm",
    "llama_index",
    "openai",
    "react",
    "sqlalchemy",
    "tkinter",
}
ALLOWED_INTERNAL_DEPENDENCIES = {
    "companion_core": {"product_core"},
    "product_core": {
        "memory_core",
        "ingestion_core",
        "document_engine",
        "project_skill_core",
        "search_and_recall",
        "job_runner",
        "platform_adapter",
    },
    "memory_core": {"storage_provider"},
    "ingestion_core": {"model_gateway", "job_runner", "storage_provider"},
    "document_engine": {"storage_provider"},
    "project_skill_core": {"storage_provider"},
    "search_and_recall": {"model_gateway", "storage_provider"},
    "model_gateway": {"storage_provider"},
    "job_runner": {"model_gateway", "storage_provider"},
    "storage_provider": set(),
    "platform_adapter": set(),
    "application_skill": set(),
    "ai_boundary": set(),
    "ai_tooling": set(),
    "ai_kernel": {"ai_tooling", "model_gateway", "search_and_recall"},
    "mcp_host": {"ai_kernel", "ai_tooling"},
}


def _resolved_imports(source: str, owner: str) -> set[str]:
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0:
                imports.add(node.module)
            elif node.level == 1:
                imports.add(f"core.{owner}.{node.module}")
            else:
                imports.add(f"core.{node.module}")
    return imports


def _violations(owner: str, source: str) -> list[str]:
    violations: list[str] = []
    for imported in sorted(_resolved_imports(source, owner)):
        root = imported.split(".", 1)[0]
        if root in BANNED_EXTERNAL_ROOTS:
            violations.append(f"{owner} imports banned dependency {imported}")
        parts = imported.split(".")
        if len(parts) >= 2 and parts[0] == "core" and parts[1] in MODULES:
            target = parts[1]
            if target != owner and target not in ALLOWED_INTERNAL_DEPENDENCIES[owner]:
                violations.append(f"{owner} cannot depend on {target}")
    return violations


def test_rebuild_contains_only_declared_architecture_modules() -> None:
    actual = {path.name for path in REBUILD.iterdir() if path.is_dir() and not path.name.startswith("__")}

    assert actual == MODULES


@pytest.mark.parametrize("module", sorted(MODULES))
def test_each_architecture_module_is_importable(module: str) -> None:
    imported = importlib.import_module(f"core.{module}")

    assert imported.__doc__


def test_current_rebuild_sources_respect_dependency_direction() -> None:
    violations: list[str] = []
    for module in sorted(MODULES):
        for path in sorted((REBUILD / module).rglob("*.py")):
            violations.extend(f"{path.relative_to(ROOT)}: {item}" for item in _violations(module, path.read_text(encoding="utf-8")))

    assert violations == []


def test_boundary_gate_rejects_reverse_and_framework_dependencies() -> None:
    assert _violations("platform_adapter", "from fastapi import APIRouter") == [
        "platform_adapter imports banned dependency fastapi",
    ]
    assert _violations("platform_adapter", "from core.product_core import GetProductHealth") == [
        "platform_adapter cannot depend on product_core",
    ]
    assert _violations("platform_adapter", "from ..product_core import GetProductHealth") == [
        "platform_adapter cannot depend on product_core",
    ]


def test_existing_domains_cannot_reverse_depend_on_companion() -> None:
    for owner in sorted(MODULES - {"companion_core"}):
        assert _violations(owner, "from core.companion_core import CompanionRepository") == [
            f"{owner} cannot depend on companion_core",
        ]


def test_companion_may_only_depend_on_product_core_public_boundary() -> None:
    assert _violations("companion_core", "from core.product_core import GetProductHealth") == []
    assert _violations("companion_core", "from core.memory_core import MemoryReaderPort") == [
        "companion_core cannot depend on memory_core",
    ]
    assert _violations("companion_core", "from fastapi import APIRouter") == [
        "companion_core imports banned dependency fastapi",
    ]
