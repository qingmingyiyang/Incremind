import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_verified_intake_and_answer_consumers_use_shared_runtime_resolver() -> None:
    routes = _read("src/backend/api/routes/product/workbench_compat.py")
    classifier_runtime = _read("src/backend/api/workbench_input_classifier_runtime.py")
    auto_intake_runtime = _read("src/backend/api/workbench_auto_intake_runtime.py")
    ai_composition = _read("src/backend/memory_app/kernel/ai_runtime.py")
    workbench = _read("src/backend/api/workbench_ai_runtime.py")
    assert "Legacy classifier provider enhancement is retired" in classifier_runtime
    assert 'resolve_model_gateway_runtime(\n        container,\n        INPUT_CLASSIFIER_MODEL_ROUTE' in ai_composition
    assert "orchestrator = self._orchestrator(None, project_id=project_id)" in auto_intake_runtime
    assert "classifier_runtime.enhance" not in auto_intake_runtime
    assert '@router.post("/api/rebuild/workbench/auto-intake")' not in routes
    assert 'resolve_model_gateway_runtime(\n        container,\n        "search.answer"' in ai_composition
    assert "ModelGatewayPort" in workbench
    assert 'capability="structured"' in workbench
    assert '_resolve_workbench_answer_provider' not in routes
    for forbidden in ("memory.candidate", "conversation.default", "task.lightweight"):
        assert f'.resolve(\n        "{forbidden}"' not in routes


def test_every_ai_turn_model_runtime_resolver_declares_a_tiered_capability() -> None:
    source = _read("src/backend/memory_app/kernel/ai_runtime.py")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_model_gateway_runtime"
    ]
    assert calls
    for call in calls:
        keyword = next(
            (item for item in call.keywords if item.arg == "tiered_capability"),
            None,
        )
        assert keyword is not None
        assert isinstance(keyword.value, ast.Constant)
        assert keyword.value.value in {"text", "structured", "vision"}


def test_runtime_authority_is_non_secret_bounded_and_emergency_disable_is_explicit() -> None:
    runtime = _read("src/core/product_core/model_route_runtime.py")
    assert "[-100:]" in runtime
    assert "CHRIPTMAS_MODEL_ROUTE_RUNTIME" in runtime
    assert "ProviderEgressPolicyStore" not in runtime
    assert "api_key_secret_name" not in runtime
    assert '"intake.classification"' in runtime
    assert '"memory.project_routing"' in runtime
    assert '"search.answer"' in runtime
    for route in ("companion.chat", "companion.event", "companion.ambient", "companion.diary", "companion.vision", "companion.voice"):
        assert f'"{route}"' in runtime


def test_runtime_activation_does_not_import_frontend_or_developer_studio() -> None:
    runtime = _read("src/core/product_core/model_route_runtime.py")
    assert "frontend" not in runtime
    assert "developer_studio" not in runtime
    assert "task_model_map" not in runtime
