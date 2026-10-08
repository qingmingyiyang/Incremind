from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/product/library_index.py"
QUERY_ROUTES = ROOT / "src/backend/api/routes/library_query.py"
QUERY_RUNTIME = ROOT / "src/backend/api/library_query_runtime.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"

LIBRARY_QUERY_PATHS = {
    "/api/rebuild/library/tag-facets",
    "/api/rebuild/library/sources/by-tag",
    "/api/rebuild/library/search",
    "/api/rebuild/library/related",
}


def test_library_query_routes_have_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path in LIBRARY_QUERY_PATHS
    ]

    assert set(registered) == LIBRARY_QUERY_PATHS
    assert len(registered) == len(LIBRARY_QUERY_PATHS)

    legacy_text = LEGACY_ROUTES.read_text(encoding="utf-8")
    assert all(path not in legacy_text for path in LIBRARY_QUERY_PATHS)

    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "library_query_router" in install_text
    assert "app.include_router(library_query_router)" in install_text


def test_library_query_runtime_is_platform_and_provider_independent() -> None:
    tree = ast.parse(QUERY_RUNTIME.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert all(not module.startswith("fastapi") for module in imported_modules)
    assert "backend.providers" not in imported_modules
    assert all("model_provider" not in module for module in imported_modules)
    assert all("companion" not in module for module in imported_modules)
    assert all("ai_kernel" not in module for module in imported_modules)
    assert all("routes" not in module for module in imported_modules)


def test_library_search_and_index_share_current_recall_projection_loader() -> None:
    runtime_text = QUERY_RUNTIME.read_text(encoding="utf-8")
    legacy_text = LEGACY_ROUTES.read_text(encoding="utf-8")

    assert runtime_text.count("def load_current_recall_entries(") == 1
    assert "current_entries = load_current_recall_entries(runtime_root, store)" in runtime_text
    assert "load_current_recall_entries as _current_recall_entries" in legacy_text
    assert "def _current_recall_entries(" not in legacy_text
    assert "build_recall_entries_from_authorities" not in legacy_text
