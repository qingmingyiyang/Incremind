from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/rebuild.py"
VIDEO_ROUTES = ROOT / "src/backend/api/routes/workbench_video_intake.py"
VIDEO_RUNTIME = ROOT / "src/backend/api/workbench_video_intake_runtime.py"
VIDEO_EFFECT_RUNTIME = ROOT / "src/backend/api/video_auto_effect_runtime.py"
VIDEO_FLOW = ROOT / "src/core/product_core/workbench_video_intake_flow.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"

VIDEO_INTAKE_PATH = "/api/rebuild/workbench/video-source-intake"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }


def test_video_intake_route_has_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path == VIDEO_INTAKE_PATH
    ]

    assert registered == [VIDEO_INTAKE_PATH]
    assert VIDEO_INTAKE_PATH not in LEGACY_ROUTES.read_text(encoding="utf-8")
    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "workbench_video_intake_router" in install_text
    assert "app.include_router(workbench_video_intake_router)" in install_text


def test_video_flow_and_runtime_are_platform_and_ai_independent() -> None:
    for path in (VIDEO_FLOW, VIDEO_RUNTIME):
        imported_modules = _imports(path)
        assert all(not module.startswith("fastapi") for module in imported_modules)
        assert all("ai_kernel" not in module for module in imported_modules)
        assert all("companion" not in module for module in imported_modules)
        assert all("desktop" not in module for module in imported_modules)
        assert all("routes" not in module for module in imported_modules)

    runtime_text = VIDEO_RUNTIME.read_text(encoding="utf-8")
    effect_runtime_text = VIDEO_EFFECT_RUNTIME.read_text(encoding="utf-8")
    assert "build_rebuild_job_repository" in runtime_text
    assert "VideoAutoEffectRuntime" in runtime_text
    assert "RunVideoAutoWorkflow" not in runtime_text
    assert "decide_video_auto_workflow" in effect_runtime_text
    assert "EffectWorkflowHandler" in effect_runtime_text
    assert "ExtractAudioTrackFromAuthorizedVideoSource" in effect_runtime_text
    assert "TranscribeGeneratedAudioAsset" in effect_runtime_text
    assert "RunVideoAutoWorkflow" not in effect_runtime_text


def test_video_router_has_no_workflow_branch_and_audio_stays_outside() -> None:
    route_text = VIDEO_ROUTES.read_text(encoding="utf-8")

    assert "auto_workflow" not in route_text
    assert "response.status_code ==" not in route_text
    assert "audio-source-intake" not in route_text
