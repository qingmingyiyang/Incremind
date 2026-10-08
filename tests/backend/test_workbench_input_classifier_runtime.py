from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.routes.workbench_input_classifier import router
from backend.api.workbench_input_classifier_runtime import (
    build_workbench_input_classifier_runtime,
)


class _Settings:
    def get_provider_settings(self):
        return SimpleNamespace(
            llm_provider="openai",
            openai_base_url="http://127.0.0.1:8317",
            openai_model="fallback-model",
        )


def _container(root_dir: Path):
    return SimpleNamespace(
        root_dir=root_dir,
        settings_service=_Settings(),
        secret_store=SimpleNamespace(get=lambda _name: ""),
    )


def test_runtime_keeps_deterministic_response_and_recipe_trace_without_fastapi(tmp_path) -> None:
    container = _container(tmp_path)
    store, _settings = build_rebuild_object_store(tmp_path)

    response = build_workbench_input_classifier_runtime(container, store).execute(
        method="POST",
        path="/api/rebuild/workbench/input-classifier",
        body={"content": "下一步实现前置分类器"},
    )

    assert response.status_code == 200
    assert response.body["status"] == "classified"
    assert response.body["provider_boundary"] == "local_rule_classifier_no_remote_provider"
    assert response.body["processing_recipe_trace"]["consumer"] == "workbench.input-classifier"
    assert response.headers["Cache-Control"] == "no-store"


def test_route_is_independent_http_owner_and_preserves_invalid_body_contract(tmp_path) -> None:
    app = FastAPI()
    app.state.container = _container(tmp_path)
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post("/api/rebuild/workbench/input-classifier", json={"content": 7})

    assert response.status_code == 400
    assert response.json()["detail"] == "content, media_type and file_name must be strings"
    assert response.headers["cache-control"] == "no-store"


def test_runtime_and_route_do_not_depend_on_legacy_rebuild_route() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime = (root / "src/backend/api/workbench_input_classifier_runtime.py").read_text(encoding="utf-8")
    route = (root / "src/backend/api/routes/workbench_input_classifier.py").read_text(encoding="utf-8")

    assert "backend.api.routes.rebuild" not in runtime
    assert "backend.api.routes.rebuild" not in route
    assert "core.ai_kernel" not in runtime
