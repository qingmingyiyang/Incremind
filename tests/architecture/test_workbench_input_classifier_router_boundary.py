from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/rebuild.py"
CLASSIFIER_ROUTES = ROOT / "src/backend/api/routes/workbench_input_classifier.py"
CLASSIFIER_RUNTIME = ROOT / "src/backend/api/workbench_input_classifier_runtime.py"
PROVIDER_RUNTIME = ROOT / "src/backend/four_layer_provider_runtime.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"

CLASSIFIER_PATH = "/api/rebuild/workbench/input-classifier"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }


def _function_text(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return ast.get_source_segment(source, node) or ""


def test_classifier_route_has_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path == CLASSIFIER_PATH
    ]

    assert registered == [CLASSIFIER_PATH]
    assert CLASSIFIER_PATH not in LEGACY_ROUTES.read_text(encoding="utf-8")
    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "workbench_input_classifier_router" in install_text
    assert "app.include_router(workbench_input_classifier_router)" in install_text


def test_classifier_runtime_is_platform_and_ai_independent() -> None:
    imported_modules = _imports(CLASSIFIER_RUNTIME)

    assert all(not module.startswith("fastapi") for module in imported_modules)
    assert all("ai_kernel" not in module for module in imported_modules)
    assert all("companion" not in module for module in imported_modules)
    assert all("desktop" not in module for module in imported_modules)
    assert all("routes" not in module for module in imported_modules)

    runtime_text = CLASSIFIER_RUNTIME.read_text(encoding="utf-8")
    assert "ProcessingRecipeRuntime" in runtime_text
    assert "Legacy classifier provider enhancement is retired" in runtime_text
    assert "ModelRouteRuntimeService" not in runtime_text
    assert "build_four_layer_json_provider" not in runtime_text


def test_classifier_router_has_no_provider_or_prompt_composition() -> None:
    route_text = CLASSIFIER_ROUTES.read_text(encoding="utf-8")

    assert "ProviderRegistry" not in route_text
    assert "ModelRouteRuntimeService" not in route_text
    assert "GetDeveloperStudioConfig" not in route_text
    assert "ProcessingRecipeRuntime" not in route_text
    assert "build_workbench_input_classifier_runtime" in route_text


def test_legacy_route_has_no_direct_provider_builder() -> None:
    legacy_text = LEGACY_ROUTES.read_text(encoding="utf-8")
    assert "build_four_layer_json_provider" not in legacy_text
    assert "_build_provider_from_record" not in legacy_text
