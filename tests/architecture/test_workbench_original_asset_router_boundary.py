from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/rebuild.py"
ASSET_ROUTES = ROOT / "src/backend/api/routes/workbench_original_asset.py"
ASSET_RUNTIME = ROOT / "src/backend/api/workbench_original_asset_runtime.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"

ASSET_PATHS = {
    "/api/rebuild/workbench/original-asset",
    "/api/rebuild/workbench/original-asset-stream",
    "/api/rebuild/library/sources/{source_id}/original-asset",
    "/api/rebuild/desktop/original-assets/{asset_id}/resolve",
    "/api/rebuild/workbench/original-assets",
}


def test_original_asset_routes_have_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path in ASSET_PATHS
    ]

    assert set(registered) == ASSET_PATHS
    assert len(registered) == len(ASSET_PATHS)
    assert all(path not in LEGACY_ROUTES.read_text(encoding="utf-8") for path in ASSET_PATHS)

    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "workbench_original_asset_router" in install_text
    assert "app.include_router(workbench_original_asset_router)" in install_text


def test_original_asset_runtime_is_platform_and_security_adapter_independent() -> None:
    tree = ast.parse(ASSET_RUNTIME.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert all(not module.startswith("fastapi") for module in imported_modules)
    assert all("desktop_session" not in module for module in imported_modules)
    assert all("security" not in module for module in imported_modules)
    assert all("model" not in module for module in imported_modules)
    assert all("ai_kernel" not in module for module in imported_modules)
    assert all("companion" not in module for module in imported_modules)
    assert all("routes" not in module for module in imported_modules)


def test_stream_and_desktop_resolve_security_remain_in_http_adapter() -> None:
    route_text = ASSET_ROUTES.read_text(encoding="utf-8")

    for token in (
        "verify_desktop_file_grant",
        "require_stream_storage_budget",
        "os.fsync",
        "desktop_session",
        "hmac.compare_digest",
        "staged.unlink(missing_ok=True)",
    ):
        assert token in route_text
