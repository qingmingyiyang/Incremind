from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/rebuild.py"
LIBRARY_ROUTES = ROOT / "src/backend/api/routes/library_lifecycle.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"
STORAGE_RUNTIME = ROOT / "src/backend/api/rebuild_storage_runtime.py"

LIBRARY_LIFECYCLE_PATHS = {
    "/api/rebuild/library/items/{item_id}",
    "/api/rebuild/library/items/{item_id}/undo-delete",
    "/api/rebuild/library/items/bulk-action",
    "/api/rebuild/library/sources/{source_id}/metadata",
    "/api/rebuild/library/sources/{source_id}/activity",
}


def test_library_lifecycle_routes_have_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path in LIBRARY_LIFECYCLE_PATHS
    ]

    assert set(registered) == LIBRARY_LIFECYCLE_PATHS
    assert len(registered) == len(LIBRARY_LIFECYCLE_PATHS)

    legacy_text = LEGACY_ROUTES.read_text(encoding="utf-8")
    assert all(path not in legacy_text for path in LIBRARY_LIFECYCLE_PATHS)

    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "library_lifecycle_router" in install_text
    assert "app.include_router(library_lifecycle_router)" in install_text


def test_library_lifecycle_router_only_adapts_http_to_domain_services() -> None:
    tree = ast.parse(LIBRARY_ROUTES.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "backend.providers" not in imported_modules
    assert all("model" not in module for module in imported_modules)
    assert all("companion" not in module for module in imported_modules)
    assert all("project_skill" not in module for module in imported_modules)
    assert "backend.api.routes.rebuild" not in imported_modules


def test_source_asset_store_composition_has_one_implementation() -> None:
    storage_text = STORAGE_RUNTIME.read_text(encoding="utf-8")
    legacy_text = (ROOT / "src/backend/api/routes/product/repositories.py").read_text(encoding="utf-8")

    assert storage_text.count("SourceAssetRuntimeStore(") == 1
    assert "build_rebuild_object_store(runtime_root" in legacy_text
    assert "SourceAssetRuntimeStore(" not in legacy_text
