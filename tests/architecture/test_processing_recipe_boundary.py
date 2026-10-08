from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "src/core/product_core/processing_recipe.py"


def test_processing_recipe_contract_has_no_tool_network_file_or_project_skill_dependency() -> None:
    tree = ast.parse(CORE.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = {
        "subprocess", "requests", "urllib", "socket", "httpx", "importlib",
        "backend", "core.project_skill_core", "core.storage_provider",
    }

    assert not (imported | imported_from) & forbidden


def test_processing_recipe_has_only_the_two_bounded_workbench_consumers() -> None:
    classifier_runtime_path = ROOT / "src/backend/api/workbench_input_classifier_runtime.py"
    classifier_route_path = ROOT / "src/backend/api/routes/workbench_input_classifier.py"
    auto_intake_runtime_path = ROOT / "src/backend/api/workbench_auto_intake_runtime.py"
    auto_intake_route_path = ROOT / "src/backend/api/routes/workbench_auto_intake.py"
    offenders: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        if path in {
            CORE,
            ROOT / "src/backend/api/routes/product/processing_recipes.py",
            ROOT / "src/backend/api/routes/product/developer_test_lab.py",
            ROOT / "src/backend/api/routes/product/route_order.py",
            classifier_runtime_path,
            classifier_route_path,
            auto_intake_runtime_path,
            auto_intake_route_path,
        } or path.name in {"__init__.py", "_exports.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "ProcessingRecipeRegistry" in text or "processing_recipe" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []

    classifier_runtime = classifier_runtime_path.read_text(encoding="utf-8")
    auto_intake_runtime = auto_intake_runtime_path.read_text(encoding="utf-8")
    assert "ProcessingRecipeRuntime(self._recipe_registry()).preflight(" in classifier_runtime
    assert 'trigger: str = "workbench.input-classifier"' in classifier_runtime
    assert 'trigger="workbench.auto-intake"' in auto_intake_runtime
    assert "classifier_runtime.recipe_preflight(" in auto_intake_runtime


def test_recipe_runtime_flag_is_fail_closed_and_only_none_side_effect_is_allowed() -> None:
    text = CORE.read_text(encoding="utf-8")

    assert 'RECIPE_SIDE_EFFECT_CLASSES = {"none"}' in text
    # The persisted D3 marker remains false for backward-compatible reads; the
    # public runtime capability is computed and does not rewrite Registry data.
    assert '"production_feature_enabled": False' in text
    assert "PROCESSING_RECIPE_RUNTIME_ENABLED = True" in text
    assert '"runtime_effect": "active_empty_guard_preflight"' in text
    assert '"side_effects": "none"' in text
    assert '"legacy_enabled_is_production_active": False' in text
