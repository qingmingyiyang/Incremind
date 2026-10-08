from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/rebuild.py"
OVERVIEW_ROUTES = ROOT / "src/backend/api/routes/library_overview.py"
COMPANION_ROUTES = ROOT / "src/backend/api/routes/companion_memory_state.py"
OVERVIEW_RUNTIME = ROOT / "src/backend/api/library_overview_runtime.py"
JOB_RUNTIME = ROOT / "src/backend/api/job_runtime.py"
MOOD_DOMAIN = ROOT / "src/core/companion_core/memory_mood.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"

EXTRACTED_PATHS = {
    "/api/rebuild/library/overview",
    "/api/rebuild/library/activity-overview",
    "/api/rebuild/pet/mood",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }


def test_overview_and_companion_routes_have_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path in EXTRACTED_PATHS
    ]

    assert set(registered) == EXTRACTED_PATHS
    assert len(registered) == len(EXTRACTED_PATHS)
    assert all(path not in LEGACY_ROUTES.read_text(encoding="utf-8") for path in EXTRACTED_PATHS)

    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "library_overview_router" in install_text
    assert "companion_memory_state_router" in install_text
    assert "app.include_router(library_overview_router)" in install_text
    assert "app.include_router(companion_memory_state_router)" in install_text


def test_overview_job_and_mood_runtime_are_platform_and_model_independent() -> None:
    for path in (OVERVIEW_RUNTIME, JOB_RUNTIME, MOOD_DOMAIN):
        imported_modules = _imported_modules(path)
        assert all(not module.startswith("fastapi") for module in imported_modules)
        assert all("model" not in module for module in imported_modules)
        assert all("ai_kernel" not in module for module in imported_modules)
        assert all("routes" not in module for module in imported_modules)


def test_companion_memory_state_projects_counts_without_library_content() -> None:
    companion_text = COMPANION_ROUTES.read_text(encoding="utf-8")
    string_literals = {
        node.value
        for node in ast.walk(ast.parse(companion_text))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "infer_companion_memory_mood" in companion_text
    assert {"title", "content", "source_path", "reference", "prompt"}.isdisjoint(string_literals)
    assert "prompt" not in companion_text.lower()


def test_rebuild_router_reuses_extracted_composition_without_local_implementations() -> None:
    legacy_text = (ROOT / "src/backend/api/routes/product/repositories.py").read_text(encoding="utf-8")
    overview_text = OVERVIEW_ROUTES.read_text(encoding="utf-8")

    assert "build_rebuild_job_repository as _job_repository" in legacy_text
    assert "build_library_overview_reader" in overview_text
    assert "def _job_repository(" not in legacy_text
    assert "def _library_overview_reader(" not in legacy_text
    assert "def _mood_from_activity(" not in legacy_text
