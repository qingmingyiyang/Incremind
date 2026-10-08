from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "team_memory_source_authority_saga.py"
)


def test_team_source_authority_saga_has_no_network_memory_or_skill_publication() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
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
            "socket",
            "core.memory_core",
            "core.project_skill_core",
            "core.product_core.progressive",
        )
    )


def test_completed_source_receipt_keeps_downstream_production_writes_closed() -> None:
    source = MODULE.read_text(encoding="utf-8")

    assert '"source_created": True' in source
    assert '"source_authority_written": True' in source
    assert "memory_created" not in source
    assert "project_skill_created" not in source
    assert "publication_created" not in source

