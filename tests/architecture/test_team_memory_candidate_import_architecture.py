from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "team_memory_candidate_import.py"
)


def test_team_import_boundary_has_no_network_backend_or_publication_dependency() -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert not any(
        value.startswith(prefix)
        for value in imports
        for prefix in (
            "backend",
            "httpx",
            "requests",
            "urllib",
            "socket",
            "core.memory_core.publication",
            "core.product_core.progressive",
            "core.project_skill_core",
        )
    )


def test_team_import_contract_keeps_publication_and_recall_closed() -> None:
    source = MODULE.read_text(encoding="utf-8")

    assert '"publication_allowed": False' in source
    assert '"automatic_recall_allowed": False' in source
    assert '"memory_write_allowed": False' in source
    assert '"project_skill_write_allowed": False' in source
    assert '"remote_write_allowed": False' in source

