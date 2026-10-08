from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "core" / "product_core" / "progressive_memory_retrieval.py"
ROUTES = ROOT / "src" / "backend" / "api" / "routes" / "product" / "project_brain.py"
PERFORMANCE_MODULE = ROOT / "src" / "core" / "product_core" / "memory_retrieval_performance.py"


def test_progressive_retrieval_planner_has_no_runtime_or_storage_dependency() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= {
        "__future__",
        "dataclasses",
        "hashlib",
        "re",
    }


def test_progressive_retrieval_planner_cannot_write_business_authority() -> None:
    source = MODULE.read_text(encoding="utf-8")

    for forbidden in (
        "memory_atoms",
        "memory_scenarios",
        "memory_series_memory",
        "project_skills",
        "memory_publications",
        "memory_transitions",
        "from backend.team_memory",
        "import team_memory",
        "httpx",
        "requests",
        "sqlite3",
        "open(",
        "write_text(",
        "write_bytes(",
    ):
        assert forbidden not in source


def test_plan_preview_route_does_not_receive_business_runtime_dependencies() -> None:
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "preview_progressive_memory_retrieval_plan"
    )

    assert [argument.arg for argument in route.args.args] == ["request"]
    called_names = {
        node.func.id
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "plan_progressive_memory_retrieval" in called_names
    assert not {
        "_memory_projection_runtime",
        "CreateMemoryProjectionRebuildJob",
        "run_memory_projection_rebuild_job",
    } & called_names


def test_memory_retrieval_performance_aggregator_has_no_storage_or_network_dependency() -> None:
    tree = ast.parse(PERFORMANCE_MODULE.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= {
        "__future__",
        "collections",
        "math",
        "re",
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not {"write", "write_text", "write_bytes", "request", "post"} & called_attributes


def test_memory_retrieval_performance_route_is_read_only() -> None:
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "memory_retrieval_performance"
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    called_names = {
        node.func.id
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "list" in called_attributes
    assert not {"write", "delete", "begin"} & called_attributes
    assert not {
        "CreateMemoryProjectionRebuildJob",
        "run_memory_projection_rebuild_job",
    } & called_names
