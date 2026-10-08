from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.companion_core import OpenMeteoWeatherProvider


def weather_response(code: int = 61) -> str:
    return json.dumps({"current": {"temperature_2m": 19.2, "precipitation": 0.2, "rain": 0.2, "showers": 0, "snowfall": 0, "weather_code": code, "is_day": 1}})


def settings(**patch):
    return {
        "enabled": True,
        "location_name": "测试城市",
        "latitude": 31.2304,
        "longitude": 121.4737,
        "noncommercial_acknowledged": True,
        "expected_revision": 0,
        **patch,
    }


def test_weather_api_default_configure_background_refresh_and_restart(tmp_path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    runtime = app.state.companion_weather_runtime
    calls = []
    runtime.service.provider = OpenMeteoWeatherProvider(lambda url: calls.append(url) or weather_response())
    client = TestClient(app)
    initial = client.get("/api/rebuild/companion/weather")
    assert initial.status_code == 200
    assert initial.json()["config"]["enabled"] is False
    assert initial.json()["weather"]["condition"] == "unknown"
    assert calls == []

    saved = client.put("/api/rebuild/companion/weather/settings", json=settings())
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert saved.json()["config"]["latitude"] == 31.2304
    # API save and explicit refresh only schedule work; neither waits on network.
    assert calls == []
    scheduled = client.post("/api/rebuild/companion/weather/refresh", json={})
    assert scheduled.status_code == 202 and scheduled.json()["status"] == "scheduled"
    assert calls == []
    refreshed = runtime.tick(manual=True)
    assert refreshed["status"] == "refreshed"
    assert len(calls) == 1 and "timezone=auto" in calls[0]
    current = client.get("/api/rebuild/companion/weather").json()
    assert current["weather"]["condition"] == "rain"
    assert current["weather"]["temperature_c"] == 19.2
    assert "api.open-meteo.com/v1/forecast?" not in json.dumps(current)

    restarted = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    persisted = restarted.get("/api/rebuild/companion/weather").json()
    assert persisted["config"]["location_name"] == "测试城市"
    assert persisted["weather"]["condition"] == "rain"


def test_weather_api_rejects_unknown_fields_bad_coordinates_terms_and_conflict(tmp_path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    client = TestClient(app)
    cases = [
        {**settings(), "provider_url": "http://127.0.0.1/private"},
        settings(latitude=91),
        settings(longitude=-181),
        settings(latitude="31.2"),
        settings(noncommercial_acknowledged=False),
    ]
    for payload in cases:
        result = client.put("/api/rebuild/companion/weather/settings", json=payload)
        assert result.status_code == 400
        assert result.json()["error"]["code"] == "invalid_weather_settings"
        assert "127.0.0.1" not in json.dumps(result.json())

    assert client.put("/api/rebuild/companion/weather/settings", json=settings()).status_code == 200
    conflict = client.put("/api/rebuild/companion/weather/settings", json=settings(location_name="旧 revision"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "weather_settings_conflict"
    assert client.post("/api/rebuild/companion/weather/refresh", json={"force": True}).status_code == 400


def test_weather_runtime_lifespan_starts_disabled_without_network(tmp_path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    calls = []
    app.state.companion_weather_runtime.service.provider = OpenMeteoWeatherProvider(lambda url: calls.append(url) or weather_response())
    with TestClient(app) as client:
        assert client.get("/api/rebuild/companion/weather").status_code == 200
    assert calls == []
    assert app.state.companion_weather_runtime._thread is None
