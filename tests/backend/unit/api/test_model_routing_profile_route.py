from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api.routes import ai as ai_routes
from backend.model_runtime import ModelRuntimeError


router = ai_routes.router


def _app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)
    return app


def _body() -> dict[str, object]:
    return {
        "expected_revision": 1,
        "rules_version": 2,
        "text_default_tier": "standard",
        "tier_routes": {
            "fast": "tier.fast",
            "standard": "tier.standard",
            "deep": "tier.deep",
            "vision": "tier.vision",
            "image_generation": None,
        },
        "confirm": True,
    }


def test_model_routing_profile_get_and_cas_update_are_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ai_routes, "validate_tiered_model_routing_profile", lambda *_args: None,
    )
    with TestClient(_app(tmp_path)) as client:
        initial = client.get("/api/ai/model-routing-profile")
        assert initial.status_code == 200
        assert initial.json()["persisted"] is False
        assert initial.json()["image_generation_available"] is False
        updated = client.put("/api/ai/model-routing-profile", json=_body())
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        assert updated.headers["cache-control"] == "no-store"
        stale = client.put("/api/ai/model-routing-profile", json=_body())
        assert stale.status_code == 409


def test_model_routing_profile_rejects_secret_extra_and_persists_dedicated_image_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ai_routes, "validate_tiered_model_routing_profile", lambda *_args: None,
    )
    with TestClient(_app(tmp_path)) as client:
        extra = _body() | {"api_key": "private"}
        assert client.put("/api/ai/model-routing-profile", json=extra).status_code == 400
        image = _body()
        image["tier_routes"] = dict(image["tier_routes"]) | {
            "image_generation": "tier.image",
        }
        saved = client.put("/api/ai/model-routing-profile", json=image)
        assert saved.status_code == 200
        assert saved.json()["tier_routes"]["image_generation"] == "tier.image"
        assert saved.json()["image_generation_available"] is False
        assert "private" not in saved.text


def test_model_routing_profile_rejects_unbound_runtime_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args):
        raise ModelRuntimeError("route is not active")

    monkeypatch.setattr(ai_routes, "validate_tiered_model_routing_profile", reject)
    with TestClient(_app(tmp_path)) as client:
        response = client.put("/api/ai/model-routing-profile", json=_body())
        assert response.status_code == 400
        assert "not active" in response.text
    assert not (
        tmp_path / "library/global/model-routes/tier-routing-profile.json"
    ).exists()
