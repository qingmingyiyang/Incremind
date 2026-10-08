from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/rebuild.py"
AUDIO_ROUTES = ROOT / "src/backend/api/routes/workbench_audio_intake.py"
VIDEO_ROUTES = ROOT / "src/backend/api/routes/workbench_video_intake.py"
AUDIO_RUNTIME = ROOT / "src/backend/api/workbench_audio_intake_runtime.py"
AUDIO_EFFECT_RUNTIME = ROOT / "src/backend/api/audio_auto_effect_runtime.py"
AUDIO_FLOW = ROOT / "src/core/product_core/workbench_audio_intake_flow.py"
AUDIO_TRANSCRIBER = ROOT / "src/core/product_core/audio_asset_transcriber.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"

AUDIO_INTAKE_PATH = "/api/rebuild/workbench/audio-source-intake"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }


def test_audio_intake_route_has_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path == AUDIO_INTAKE_PATH
    ]

    assert registered == [AUDIO_INTAKE_PATH]
    assert AUDIO_INTAKE_PATH not in LEGACY_ROUTES.read_text(encoding="utf-8")
    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "workbench_audio_intake_router" in install_text
    assert "app.include_router(workbench_audio_intake_router)" in install_text


def test_audio_flow_and_runtime_are_platform_and_ai_independent() -> None:
    for path in (AUDIO_FLOW, AUDIO_RUNTIME):
        imported_modules = _imports(path)
        assert all(not module.startswith("fastapi") for module in imported_modules)
        assert all("ai_kernel" not in module for module in imported_modules)
        assert all("companion" not in module for module in imported_modules)
        assert all("desktop" not in module for module in imported_modules)
        assert all("routes" not in module for module in imported_modules)

    runtime_text = AUDIO_RUNTIME.read_text(encoding="utf-8")
    effect_runtime_text = AUDIO_EFFECT_RUNTIME.read_text(encoding="utf-8")
    assert "build_rebuild_job_repository" in runtime_text
    assert "AudioAutoEffectRuntime" in runtime_text
    assert "RunAudioAutoWorkflow" not in runtime_text
    assert "TranscribeGeneratedAudioAsset" in effect_runtime_text
    assert "decide_audio_auto_workflow" in effect_runtime_text
    assert "EffectWorkflowHandler" in effect_runtime_text
    assert "RunAudioAutoWorkflow" not in effect_runtime_text


def test_audio_router_has_no_workflow_branch_and_video_stays_outside() -> None:
    route_text = AUDIO_ROUTES.read_text(encoding="utf-8")
    legacy_text = LEGACY_ROUTES.read_text(encoding="utf-8")
    video_text = VIDEO_ROUTES.read_text(encoding="utf-8")

    assert "auto_workflow" not in route_text
    assert "response.status_code ==" not in route_text
    assert "video-source-intake" not in route_text
    assert '"/api/rebuild/workbench/video-source-intake"' in video_text
    assert '"/api/rebuild/workbench/video-source-intake"' not in legacy_text


def test_local_asr_subprocess_stdout_has_explicit_utf8_contract() -> None:
    transcriber_text = AUDIO_TRANSCRIBER.read_text(encoding="utf-8")

    assert 'child_env["PYTHONIOENCODING"] = "utf-8"' in transcriber_text
    assert 'child_env["PYTHONUTF8"] = "1"' in transcriber_text
    assert '.decode("utf-8", errors="replace")' in transcriber_text
