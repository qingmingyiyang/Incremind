from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.app import create_app
from tests.architecture.route_introspection import registered_route_paths


ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROUTES = ROOT / "src/backend/api/routes/rebuild.py"
OVERVIEW_ROUTES = ROOT / "src/backend/api/routes/library_overview.py"
COMPANION_ROUTES = ROOT / "src/backend/api/routes/companion_memory_state.py"
ROUTE_INSTALL = ROOT / "src/backend/api/routes/__init__.py"

EXTRACTED_PATHS = {
    "/api/rebuild/library/overview",
    "/api/rebuild/library/activity-overview",
    "/api/rebuild/pet/mood",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            # from 父包 import 子模块同样形成依赖，别名不能隐藏真实路径。
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


def test_overview_and_companion_routes_have_one_production_owner(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    registered = [
        path for path in registered_route_paths(app)
        if path in EXTRACTED_PATHS
    ]

    assert set(registered) == EXTRACTED_PATHS
    assert len(registered) == len(EXTRACTED_PATHS)
    assert all(path not in LEGACY_ROUTES.read_text(encoding="utf-8") for path in EXTRACTED_PATHS)

    install_text = ROUTE_INSTALL.read_text(encoding="utf-8")
    assert "library_overview_router" in install_text
    assert "companion_memory_state_router" in install_text
    assert "app.include_router(library_overview_router)" in install_text
    assert "app.include_router(companion_memory_state_router)" in install_text


def test_overview_job_and_mood_runtime_are_platform_and_model_independent() -> None:
    for relative in (
        "src/backend/api/library_overview_runtime.py",
        "src/backend/shared/job_runtime.py",
        "src/backend/api/job_runtime.py",
        "src/core/companion_core/memory_mood.py",
    ):
        path = ROOT / relative
        imported_modules = _imported_modules(path)
        assert all(not module.startswith("fastapi") for module in imported_modules), f"{relative}: fastapi dependency"
        assert all("model" not in module for module in imported_modules), f"{relative}: model dependency"
        assert all("ai_kernel" not in module for module in imported_modules), f"{relative}: ai_kernel dependency"
        assert all("routes" not in module for module in imported_modules), f"{relative}: routes dependency"


@pytest.fixture
def isolated_runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 镜像固定的真实文件，仅替换仓库根，保证控制用例检验生产门禁实际选择的路径。
    for relative in (
        "src/backend/api/library_overview_runtime.py",
        "src/backend/shared/job_runtime.py",
        "src/backend/api/job_runtime.py",
        "src/core/companion_core/memory_mood.py",
    ):
        copied = tmp_path / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    return tmp_path


def test_independence_gate_accepts_existing_runtime_dependencies(isolated_runtime_root: Path) -> None:
    test_overview_job_and_mood_runtime_are_platform_and_model_independent()


@pytest.mark.parametrize(
    "relative",
    ("src/backend/shared/job_runtime.py", "src/backend/api/job_runtime.py"),
    ids=("shared-owner", "api-adapter"),
)
@pytest.mark.parametrize(
    ("statement", "forbidden"),
    (
        pytest.param("from fastapi import FastAPI", "fastapi", id="fastapi-from"),
        pytest.param("import fastapi as web", "fastapi", id="fastapi-import"),
        pytest.param("from backend.memory_app import model_config", "model", id="model-from"),
        pytest.param("import backend.memory_app.model_config as config", "model", id="model-import"),
        pytest.param("from core import ai_kernel", "ai_kernel", id="ai-kernel-from"),
        pytest.param("import core.ai_kernel as kernel", "ai_kernel", id="ai-kernel-import"),
        pytest.param("from backend.api import routes", "routes", id="routes-from"),
        pytest.param("import backend.api.routes as handlers", "routes", id="routes-import"),
    ),
)
def test_independence_gate_rejects_job_runtime_dependency_regressions(
    isolated_runtime_root: Path, relative: str, statement: str, forbidden: str,
) -> None:
    test_overview_job_and_mood_runtime_are_platform_and_model_independent()
    copied = isolated_runtime_root / relative
    copied.write_text(copied.read_text(encoding="utf-8") + f"\n{statement}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match=forbidden):
        test_overview_job_and_mood_runtime_are_platform_and_model_independent()


def test_companion_memory_state_projects_counts_without_library_content() -> None:
    companion_text = COMPANION_ROUTES.read_text(encoding="utf-8")
    string_literals = {
        node.value
        for node in ast.walk(ast.parse(companion_text))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "infer_companion_memory_mood" in companion_text
    assert {"title", "content", "source_path", "reference", "prompt"}.isdisjoint(string_literals)
    assert "prompt" not in companion_text.lower()


def test_rebuild_router_reuses_extracted_composition_without_local_implementations() -> None:
    legacy_text = (ROOT / "src/backend/api/routes/product/repositories.py").read_text(encoding="utf-8")
    overview_text = OVERVIEW_ROUTES.read_text(encoding="utf-8")

    assert "build_rebuild_job_repository as _job_repository" in legacy_text
    assert "build_library_overview_reader" in overview_text
    assert "def _job_repository(" not in legacy_text
    assert "def _library_overview_reader(" not in legacy_text
    assert "def _mood_from_activity(" not in legacy_text
