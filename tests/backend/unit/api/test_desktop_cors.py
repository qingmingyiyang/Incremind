from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from backend.api.app import create_app


def test_web_origins_are_exact_loopback_endpoints(tmp_path):
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    cors = next(row for row in app.user_middleware if row.cls is CORSMiddleware)
    assert set(cors.kwargs["allow_origins"]) == {
        "http://127.0.0.1:8001", "http://localhost:8001",
        "http://127.0.0.1:4173", "http://localhost:4173",
    }


def test_loopback_origin_preflight_allows_native_worker_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_WORKER_SECRET", "test-worker-secret")
    monkeypatch.setenv("CHRIPTMAS_WORKER_INSTANCE_ID", "b1a9195e6ca34f308f741cdfcf8d6a02")
    monkeypatch.setenv("CHRIPTMAS_WORKER_PORT", "8001")
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        response = client.options(
            "/api/health",
            headers={
                "Origin": "http://127.0.0.1:4173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type,x-worker-secret",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert "GET" in response.headers["access-control-allow-methods"]
    assert "x-worker-secret" in response.headers["access-control-allow-headers"].lower()


def test_unknown_origin_is_not_granted_cors_access(tmp_path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        response = client.options(
            "/api/health",
            headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
        )

    assert "access-control-allow-origin" not in response.headers
