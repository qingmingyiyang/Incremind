from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/rebuild.py"
INTAKE_ROUTES = ROOT / "src/backend/api/routes/workbench_source_intake.py"
AUDIO_INTAKE_ROUTES = ROOT / "src/backend/api/routes/workbench_audio_intake.py"
VIDEO_INTAKE_ROUTES = ROOT / "src/backend/api/routes/workbench_video_intake.py"
INTAKE_RUNTIME = ROOT / "src/backend/api/workbench_source_intake_runtime.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"

BASIC_INTAKE_PATHS = {
    "/api/rebuild/workbench/text-source-intake",
    "/api/rebuild/workbench/link-source-intake",
    "/api/rebuild/workbench/bookmark-collection-intake",
    "/api/rebuild/workbench/file-source-intake",
    "/api/rebuild/workbench/image-source-intake",
}


def test_basic_source_intake_routes_have_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path in BASIC_INTAKE_PATHS
    ]

    assert set(registered) == BASIC_INTAKE_PATHS
    assert len(registered) == len(BASIC_INTAKE_PATHS)
    assert all(path not in LEGACY_ROUTES.read_text(encoding="utf-8") for path in BASIC_INTAKE_PATHS)

    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "workbench_source_intake_router" in install_text
    assert "app.include_router(workbench_source_intake_router)" in install_text


def test_source_intake_runtime_uses_shared_authorities_without_platform_dependencies() -> None:
    tree = ast.parse(INTAKE_RUNTIME.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    runtime_text = INTAKE_RUNTIME.read_text(encoding="utf-8")

    assert "build_rebuild_job_repository" in runtime_text
    assert runtime_text.count("ObjectStoreSourceRegistrar(") == 1
    assert all(not module.startswith("fastapi") for module in imported_modules)
    assert all("model" not in module for module in imported_modules)
    assert all("ai_kernel" not in module for module in imported_modules)
    assert all("companion" not in module for module in imported_modules)
    assert all("desktop" not in module for module in imported_modules)
    assert all("routes" not in module for module in imported_modules)


def test_audio_and_video_workflows_remain_outside_basic_intake_router() -> None:
    intake_text = INTAKE_ROUTES.read_text(encoding="utf-8")
    audio_intake_text = AUDIO_INTAKE_ROUTES.read_text(encoding="utf-8")
    video_intake_text = VIDEO_INTAKE_ROUTES.read_text(encoding="utf-8")
    legacy_text = LEGACY_ROUTES.read_text(encoding="utf-8")

    assert "audio-source-intake" not in intake_text
    assert "video-source-intake" not in intake_text
    assert '"/api/rebuild/workbench/audio-source-intake"' in audio_intake_text
    assert '"/api/rebuild/workbench/audio-source-intake"' not in legacy_text
    assert '"/api/rebuild/workbench/video-source-intake"' in video_intake_text
    assert '"/api/rebuild/workbench/video-source-intake"' not in legacy_text
    assert "RunAudioAutoWorkflow" not in intake_text
    assert "RunVideoAutoWorkflow" not in intake_text


def test_basic_intake_http_adapter_does_not_read_files_or_call_models() -> None:
    route_text = INTAKE_ROUTES.read_text(encoding="utf-8")

    assert "Path(" not in route_text
    assert ".open(" not in route_text
    assert "Provider" not in route_text
    assert "Model" not in route_text
    assert "execute" in route_text
