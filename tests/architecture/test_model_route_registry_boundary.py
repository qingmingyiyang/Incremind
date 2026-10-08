from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_product_consumers_do_not_import_registry_directly_after_c3_composition() -> None:
    consumers = [
        "src/core/product_core/workbench_input_classifier.py",
        "src/core/product_core/workbench_auto_intake.py",
        "src/core/product_core/source_structuring.py",
        "src/core/product_core/openai_compatible_four_layer_provider.py",
    ]
    for relative in consumers:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "ModelRouteRegistry" not in source, relative
        assert "model_route_registry" not in source, relative
    rebuild_route = (ROOT / "src/backend/api/routes/product/providers.py").read_text(encoding="utf-8")
    ai_composition = (ROOT / "src/backend/memory_app/kernel/ai_runtime.py").read_text(encoding="utf-8")
    assert "def _build_provider_for_rebuild_role" not in rebuild_route
    assert "resolve_model_gateway_runtime(" in ai_composition
    assert "ModelRouteRegistry" not in (
        ROOT / "src/backend/api/developer_studio_test_lab_ai_runtime.py"
    ).read_text(encoding="utf-8")


def test_c1_registry_has_no_secret_or_endpoint_fields() -> None:
    source = (ROOT / "src/core/product_core/model_route_registry.py").read_text(encoding="utf-8")
    record_block = source[source.index('return {\n            "route_key"'):source.index("    @staticmethod\n    def _normalize_route_key")]
    for forbidden in ('"api_key"', '"secret"', '"base_url"', '"api_path"', '"endpoint"'):
        assert forbidden not in record_block
    assert "def runtime_activation" in source
    assert 'status()["mode"] == "active"' in source
