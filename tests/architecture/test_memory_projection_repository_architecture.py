from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "memory_projection_repository.py"
)
JOB_MODULE = (
    ROOT
    / "src"
    / "core"
    / "product_core"
    / "memory_projection_rebuild_job.py"
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_projection_repository_and_job_keep_runtime_boundaries() -> None:
    assert _imports(REPOSITORY_MODULE) <= {
        "__future__",
        "collections",
        "dataclasses",
        "hashlib",
        "json",
        "core",
        "typing",
    }
    assert _imports(JOB_MODULE) <= {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "hashlib",
        "core",
        "typing",
    }


def test_projection_repository_uses_only_fixed_derived_collections() -> None:
    source = REPOSITORY_MODULE.read_text(encoding="utf-8")

    for collection in (
        "memory_retrieval_projection_manifests",
        "memory_retrieval_projection_items",
        "memory_retrieval_projection_failures",
    ):
        assert collection in source
    for forbidden in (
        "memory_atoms",
        "memory_scenarios",
        "memory_series_memory",
        "project_skills",
        "memory_publications",
        "memory_transitions",
        "team_memory",
    ):
        assert forbidden not in source


def test_projection_repository_and_job_have_no_external_or_ui_dependency() -> None:
    source = (
        REPOSITORY_MODULE.read_text(encoding="utf-8")
        + JOB_MODULE.read_text(encoding="utf-8")
    )

    for forbidden in (
        "import httpx",
        "import requests",
        "import socket",
        "import subprocess",
        "src.frontend",
        "apps.desktop",
        "electron",
        "model_gateway",
        "model_provider",
        "provider_route",
        "api_key",
        "from backend",
        "import backend",
    ):
        assert forbidden not in source.lower()
