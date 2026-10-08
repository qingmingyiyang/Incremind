from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_migration_only_handles_openai_compatible_text_route_keys() -> None:
    source = _read("src/core/product_core/model_route_migration.py")
    specs = source[source.index("ROUTE_SPECS"):source.index("RETAINED_SPECIAL_KEYS")]
    for use_key in ("lightweight", "intakeMain", "default", "memory", "search"):
        assert f'"{use_key}"' in specs
    for special in ("embed", "vision", "asr"):
        assert f'"{special}"' not in specs
    assert 'RETAINED_SPECIAL_KEYS: tuple[str, ...] = ("embed", "vision", "asr")' in source


def test_migration_api_does_not_delete_legacy_sources_or_activate_runtime() -> None:
    route = _read("src/backend/api/routes/product/model_routes.py")
    migration_block = route[
        route.index('@router.post("/api/rebuild/model-routes/migration/preview")'):
        route.index('@router.get("/api/model-route-runtime")')
    ]
    assert "SaveDeveloperStudioConfig" not in migration_block
    assert "localStorage" not in migration_block
    assert "ModelRouteRuntimeService" not in migration_block
    frontend = _read("src/frontend/src/features/rebuild/ModelRouteMigrationPanel.jsx")
    assert "removeItem" not in frontend
    assert "生产调用尚未切换" in frontend
    assert "此前没有控制生产模型" in frontend


def test_production_consumers_still_do_not_import_model_route_registry() -> None:
    for relative in (
        "src/core/product_core/workbench_input_classifier.py",
        "src/core/product_core/workbench_auto_intake.py",
        "src/core/product_core/source_structuring.py",
        "src/core/product_core/openai_compatible_four_layer_provider.py",
    ):
        source = _read(relative)
        assert "model_route_registry" not in source, relative
        assert "ModelRouteRegistry" not in source, relative
