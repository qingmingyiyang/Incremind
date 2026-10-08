from __future__ import annotations

import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def test_local_asr_provider_settings_get_returns_default(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/rebuild/settings/local-asr-provider")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["explicit_enable_required"] is True
    assert payload["memory_publication"] == "not_started"


def test_local_video_provider_settings_get_returns_default(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/rebuild/settings/local-video-provider")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["explicit_enable_required"] is True


def test_transcript_summary_provider_settings_get_returns_default(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/rebuild/settings/transcript-summary-provider")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["explicit_enable_required"] is True
    assert payload["memory_publication"] == "not_started"


def test_local_asr_provider_settings_put_enables_provider(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.put(
        "/api/rebuild/settings/local-asr-provider",
        json={
            "enabled": True,
            "confirm_enable": True,
            "command": [sys.executable, "--version"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["status"] in {"ready", "needs_configuration", "degraded"}


def test_local_video_provider_settings_put_enables_provider(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.put(
        "/api/rebuild/settings/local-video-provider",
        json={
            "enabled": True,
            "confirm_enable": True,
            "command": [sys.executable, "--version"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True


def test_transcript_summary_provider_settings_put_enables_provider(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.put(
        "/api/rebuild/settings/transcript-summary-provider",
        json={
            "enabled": True,
            "confirm_enable": True,
            "command": [sys.executable, "--version"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True


def test_video_source_intake_returns_captured_status(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/rebuild/workbench/video-source-intake",
        json={
            "title": "测试视频",
            "display_name": "test-video.mp4",
            "media_type": "video/mp4",
            "size_bytes": 1024,
            "video_reference": "platform-video-ref-test",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "captured"
    assert payload["source_id"]
    assert payload["asset_id"]


def test_video_source_intake_runs_auto_workflow(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/rebuild/workbench/video-source-intake",
        json={
            "title": "自动工作流测试视频",
            "display_name": "auto-workflow-video.mp4",
            "media_type": "video/mp4",
            "size_bytes": 2048,
            "video_reference": "platform-video-ref-auto-workflow",
            "project_id": "default",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "captured"
    assert "auto_workflow" in payload
    workflow = payload["auto_workflow"]
    assert workflow["source_id"] == payload["source_id"]
    assert workflow["workflow_id"]
    assert isinstance(workflow["steps"], list)
    assert workflow["memory_publication"] in {
        "not_started",
        "candidate_created",
        "published_with_rollback_ref",
    }


def test_audio_source_intake_returns_captured_status(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/rebuild/workbench/audio-source-intake",
        json={
            "title": "测试音频",
            "display_name": "test-audio.mp3",
            "media_type": "audio/mpeg",
            "size_bytes": 512,
            "audio_reference": "platform-audio-ref-test",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "captured"
    assert payload["source_id"]


def test_video_source_intake_auto_workflow_does_not_leak_secrets(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/rebuild/workbench/video-source-intake",
        json={
            "title": "安全边界测试视频",
            "display_name": "security-test.mp4",
            "media_type": "video/mp4",
            "size_bytes": 100,
            "video_reference": "platform-video-ref-security-test",
        },
    )

    payload = response.json()
    assert response.status_code == 201
    encoded = str(payload).lower()
    assert "sk-" not in encoded
    assert "cookie" not in encoded.replace("cookie_mode", "").replace("cookie_local_file", "")
    assert "authorization" not in encoded.replace("memory_publication_state", "").replace("auto_publication_status", "")


def test_provider_settings_response_has_no_secrets(tmp_path) -> None:
    client = _client(tmp_path)

    for endpoint in [
        "/api/rebuild/settings/local-asr-provider",
        "/api/rebuild/settings/local-video-provider",
        "/api/rebuild/settings/transcript-summary-provider",
    ]:
        response = client.get(endpoint)
        payload = response.json()
        encoded = str(payload).lower()
        assert "sk-" not in encoded
        assert "api_key" not in encoded
