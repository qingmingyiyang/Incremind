from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.companion_core import CompanionSystemSensorService


def test_sensor_api_settings_sampling_and_restart_persistence(tmp_path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    client = TestClient(app)
    initial = client.get("/api/rebuild/companion/sensors")
    assert initial.status_code == 200 and initial.json()["revision"] == 0
    saved = client.put("/api/rebuild/companion/sensors/settings", json={
        "enabled": True,
        "network_enabled": True,
        "health_origin": "https://example.com/",
        "game_enabled": True,
        "game_processes": ["steam.exe"],
        "game_behavior": "quiet",
        "expected_revision": 0,
    })
    assert saved.status_code == 200 and saved.json()["config"]["health_origin"] == "https://example.com"
    fake = CompanionSystemSensorService(
        app.state.companion_system_sensor_service.repository,
        cpu_percent=lambda: 25,
        virtual_memory=lambda: SimpleNamespace(percent=40, available=8 * 1024**3),
        process_names=lambda: ("steam.exe", "private.exe"),
    )
    app.state.companion_system_sensor_service = fake
    sample = client.post("/api/rebuild/companion/sensors/sample", json={"network_state": "normal", "latency_ms": 42})
    assert sample.status_code == 200
    assert sample.json()["sample"]["game_active"] is True
    assert "steam" not in str(sample.json()["sample"]).lower() and "private" not in str(sample.json()["sample"]).lower()
    restarted = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    assert restarted.get("/api/rebuild/companion/sensors").json()["config"]["game_processes"] == ["steam.exe"]


def test_sensor_api_rejects_renderer_process_injection_and_bad_network_values(tmp_path) -> None:
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    injected = client.post("/api/rebuild/companion/sensors/sample", json={"network_state": "normal", "latency_ms": 1, "processes": ["fake.exe"]})
    invalid = client.post("/api/rebuild/companion/sensors/sample", json={"network_state": "normal", "latency_ms": -1})
    bad_origin = client.put("/api/rebuild/companion/sensors/settings", json={
        "enabled": True, "network_enabled": True, "health_origin": "http://example.com/path",
        "game_enabled": True, "game_processes": [], "game_behavior": "hide", "expected_revision": 0,
    })
    assert injected.status_code == 400
    assert invalid.status_code == 400
    assert bad_origin.status_code == 400
