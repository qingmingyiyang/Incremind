from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "src" / "backend"

# Every remaining constructor is an explicit migration or retirement target.
# model_runtime.py is the only approved long-term production composition point.
LEGACY_DIRECT_CONSTRUCTORS: set[str] = set()
# 5138b34e3: settings-only fixed diagnostic composition; runtime behavior is
# covered by test_runtime_security_behavior, including real policy revocation.
APPROVED_COMPOSITION = {
    "src/backend/model_runtime.py",
    "src/backend/shared/llm/connection_diagnostic.py",
}
FOUR_LAYER_DIRECT_CONSTRUCTORS: set[str] = set()


def test_direct_model_gateway_constructors_match_frozen_cutover_inventory() -> None:
    found: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LiteLLMCompletionGateway"
            for node in ast.walk(tree)
        ):
            found.add(path.relative_to(ROOT).as_posix())

    assert found == LEGACY_DIRECT_CONSTRUCTORS | APPROVED_COMPOSITION


def test_four_layer_provider_constructors_match_frozen_cutover_inventory() -> None:
    found: set[str] = set()
    for root in (ROOT / "src/backend", ROOT / "src/core"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "OpenAICompatibleFourLayerJsonProvider"
                for node in ast.walk(tree)
            ):
                found.add(path.relative_to(ROOT).as_posix())

    assert found == FOUR_LAYER_DIRECT_CONSTRUCTORS


def test_executable_work_scripts_cannot_construct_legacy_model_provider() -> None:
    found: set[str] = set()
    for path in (ROOT / "work/scripts").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {
                "OpenAICompatibleFourLayerJsonProvider",
                "LiteLLMCompletionGateway",
                "build_four_layer_json_provider",
                "build_deepseek_four_layer_provider",
            }
            for node in ast.walk(tree)
        ):
            found.add(path.relative_to(ROOT).as_posix())

    assert found == set()


def test_non_tiered_runtime_resolution_is_fail_closed() -> None:
    source = (BACKEND / "model_runtime.py").read_text(encoding="utf-8")
    function = source[source.index("def resolve_model_gateway_runtime("):source.index("def _resolve_tiered_model_gateway(")]

    assert "if tiered_capability is not None:" in function
    assert "LiteLLMCompletionGateway(" not in function
    assert 'ModelGatewayResolution(None, "unconfigured", "openai-compatible", False, False)' in function


def test_legacy_video_runtime_cannot_construct_model_transport() -> None:
    source = (BACKEND / "video_summary/infrastructure/video_summary_runtime.py").read_text(encoding="utf-8")
    function = source[source.index("def build_litellm_completion_gateway("):source.index("def build_video_summary_runtime(")]

    assert "LiteLLMCompletionGateway(" not in function
    assert "Legacy video model entry is retired" in function
    assert "analyze_source through Media Hands" in function


def test_legacy_video_http_write_entry_fails_before_service_or_side_effects() -> None:
    source = (BACKEND / "api/routes/intake.py").read_text(encoding="utf-8")
    function = source[source.index("async def start_import("):source.index("@router.get(\"/tasks\"")]

    assert "统一 analyze_source 使用 Media Hands" in function
    for forbidden in ("_service(", ".start(", "create_task", "resolve("):
        assert forbidden not in function


def test_legacy_series_routes_cannot_construct_or_invoke_model_transport() -> None:
    source = (BACKEND / "api/routes/series.py").read_text(encoding="utf-8")

    assert "LiteLLMCompletionGateway" not in source
    assert "_merge_gateway" not in source
    assert "旧版系列 AI 合并入口已停用" in source
    assert "gateway=None" in source
