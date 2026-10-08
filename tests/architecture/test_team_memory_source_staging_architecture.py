from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "team_memory_source_staging.py"
)


def test_team_source_staging_has_no_network_backend_or_authority_writer() -> None:
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
            "core.ingestion_core",
            "core.memory_core",
            "core.project_skill_core",
            "core.product_core.progressive",
        )
    )


def test_team_source_staging_explicitly_keeps_all_production_writes_closed() -> None:
    source = MODULE.read_text(encoding="utf-8")

    for boundary in (
        '"source_authority_written": False',
        '"memory_written": False',
        '"project_skill_written": False',
        '"projection_written": False',
        '"automatic_recall_enabled": False',
    ):
        assert boundary in source

