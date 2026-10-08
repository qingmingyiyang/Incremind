from __future__ import annotations

import base64
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.desktop_session import DESKTOP_SESSION_HEADER
from backend.companion_runtime_layout import build_companion_object_store
from backend.security.file_grant import DesktopFileGrant, sign_desktop_file_grant
from core.product_core.local_asr_provider_settings import SaveLocalAsrProviderSettings
from core.companion_core import voice_call as voice_module


SECRET = "v" * 43
INSTANCE = "instance-companion-voice"


def configure(monkeypatch, instance: str = INSTANCE) -> None:
    values = {
        "CHRIPTMAS_DESKTOP_SESSION_MODE": "desktop_production", "CHRIPTMAS_DESKTOP_SESSION_SECRET": SECRET,
        "CHRIPTMAS_DESKTOP_INSTANCE_ID": instance, "CHRIPTMAS_DESKTOP_NONCE": "n" * 43,
        "CHRIPTMAS_DESKTOP_PROTOCOL_VERSION": "desktop-loopback/1",
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN": "http://127.0.0.1:8317",
    }
    for key, value in values.items(): monkeypatch.setenv(key, value)


def webm() -> bytes:
    return b"\x1aE\xdf\xa3" + b"api-voice-canary" * 12


def headers(content: bytes, *, sha256: str | None = None, media_type: str = "audio/webm", instance: str = INSTANCE) -> dict[str, str]:
    grant = DesktopFileGrant(
        grant_id="file-grant-" + "a" * 43, session_instance_id=instance, display_name="voice.webm",
        media_type=media_type, source_kind="audio", size_bytes=len(content), sha256=sha256 or hashlib.sha256(content).hexdigest(),
        expires_at_ms=int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
    )
    return {
        DESKTOP_SESSION_HEADER: SECRET, "X-Chriptmas-File-Grant": grant.grant_id,
        "X-Chriptmas-File-Session": grant.session_instance_id,
        "X-Chriptmas-File-Name": base64.urlsafe_b64encode(grant.display_name.encode()).decode().rstrip("="),
        "X-Chriptmas-File-Media-Type": grant.media_type, "X-Chriptmas-File-Source-Kind": grant.source_kind,
        "X-Chriptmas-File-Size": str(grant.size_bytes), "X-Chriptmas-File-Sha256": grant.sha256,
        "X-Chriptmas-File-Expires": str(grant.expires_at_ms),
        "X-Chriptmas-File-Signature": sign_desktop_file_grant(grant, session_secret=SECRET),
        "Content-Type": "application/octet-stream",
    }


def enable_fake_asr(root, script) -> None:
    SaveLocalAsrProviderSettings(build_companion_object_store(root)).execute(
        enabled=True, confirm_enable=True, provider_name="local-fixture-asr",
        command=(sys.executable, str(script), "{audio_path}"), model_profile="small", model_name="small", timeout_seconds=10,
    )


def test_sidecar_startup_cleans_old_voice_audio_without_using_voice_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(voice_module.tempfile, "gettempdir", lambda: str(tmp_path / "system-temp"))
    old = tmp_path / "system-temp" / "chriptmas-companion-voice-call" / ("a" * 24); old.mkdir(parents=True)
    owned = old / ("voice-grant-" + "b" * 48 + ".webm"); owned.write_bytes(webm())
    unrelated = old / "keep.txt"; unrelated.write_text("keep", encoding="utf-8")
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path / "vault"))) as client:
        assert not hasattr(client.app.state, "companion_voice_grant_store")
    assert not owned.exists() and unrelated.exists()


def test_streamed_voice_grant_transcribes_once_and_leaves_no_audio_or_hash(tmp_path, monkeypatch) -> None:
    configure(monkeypatch)
    script = tmp_path / "fake_asr.py"
    script.write_text("import json\nprint(json.dumps({'language':'zh','segments':[{'text':'你好，测试语音。'}]}, ensure_ascii=False))\n", encoding="utf-8")
    enable_fake_asr(tmp_path, script)
    content = webm(); digest = hashlib.sha256(content).hexdigest(); app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        uploaded = client.post("/api/rebuild/companion/voice/grants", headers=headers(content), content=content)
        assert uploaded.status_code == 201 and set(uploaded.json()["grant"]) == {"grant_id", "media_type", "byte_length"}
        grant_id = uploaded.json()["grant"]["grant_id"]; root = app.state.companion_voice_grant_store.root
        result = client.post("/api/rebuild/companion/voice/transcribe", headers={DESKTOP_SESSION_HEADER: SECRET}, json={"request_id":"voice:12345678", "grant_id":grant_id})
        replay = client.post("/api/rebuild/companion/voice/transcribe", headers={DESKTOP_SESSION_HEADER: SECRET}, json={"request_id":"voice:87654321", "grant_id":grant_id})
    assert result.status_code == 200 and result.json()["result"]["text"] == "你好，测试语音。"
    assert replay.status_code == 409 and list(root.iterdir()) == []
    serialized = json.dumps(result.json(), ensure_ascii=False)
    assert digest not in serialized and digest[:12] not in serialized and str(tmp_path) not in serialized
    databases = b"".join(path.read_bytes() for path in tmp_path.rglob("*.sqlite3"))
    assert digest.encode() not in databases and content not in databases


def test_voice_upload_rejects_hash_magic_type_and_disabled_asr(tmp_path, monkeypatch) -> None:
    configure(monkeypatch); content = webm()
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert client.post("/api/rebuild/companion/voice/grants", headers=headers(content, sha256="0" * 64), content=content).status_code == 400
        bad = b"not-webm"
        assert client.post("/api/rebuild/companion/voice/grants", headers=headers(bad), content=bad).status_code == 400
        assert client.post("/api/rebuild/companion/voice/grants", headers=headers(content, media_type="audio/mpeg"), content=content).status_code == 413
        grant_id = client.post("/api/rebuild/companion/voice/grants", headers=headers(content), content=content).json()["grant"]["grant_id"]
        disabled = client.post("/api/rebuild/companion/voice/transcribe", headers={DESKTOP_SESSION_HEADER: SECRET}, json={"request_id":"voice:12345678", "grant_id":grant_id})
    assert disabled.status_code == 503 and disabled.json()["error"]["code"] == "voice_transcription_unavailable"


def test_desktop_session_change_disposes_old_voice_grant(tmp_path, monkeypatch) -> None:
    configure(monkeypatch); content = webm(); app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        old = client.post("/api/rebuild/companion/voice/grants", headers=headers(content), content=content).json()["grant"]["grant_id"]
        old_root = app.state.companion_voice_grant_store.root
        second = "instance-companion-voice-b"; configure(monkeypatch, second)
        fresh = client.post("/api/rebuild/companion/voice/grants", headers=headers(content, instance=second), content=content)
        assert fresh.status_code == 201 and app.state.companion_voice_grant_store.session_id == second
        replay = client.post("/api/rebuild/companion/voice/transcribe", headers={DESKTOP_SESSION_HEADER: SECRET}, json={"request_id":"voice:12345678", "grant_id":old})
    assert replay.status_code == 409 and (not old_root.exists() or list(old_root.iterdir()) == [])


def test_asr_provider_failure_response_never_echoes_path_model_or_command(tmp_path, monkeypatch) -> None:
    configure(monkeypatch)
    canary = f"ASR_PATH_CANARY={tmp_path} MODEL=C:\\private\\model COMMAND=secret"
    script = tmp_path / "failed_asr.py"
    script.write_text(f"import sys\nsys.stderr.write({canary!r})\nraise SystemExit(7)\n", encoding="utf-8")
    enable_fake_asr(tmp_path, script)
    content = webm()
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        grant_id = client.post("/api/rebuild/companion/voice/grants", headers=headers(content), content=content).json()["grant"]["grant_id"]
        response = client.post("/api/rebuild/companion/voice/transcribe", headers={DESKTOP_SESSION_HEADER: SECRET}, json={"request_id":"voice:12345678", "grant_id":grant_id})
    serialized = json.dumps(response.json(), ensure_ascii=False)
    assert response.status_code == 503 and response.json()["error"]["code"] == "voice_transcription_unavailable"
    assert canary not in serialized and str(tmp_path) not in serialized and "private\\model" not in serialized and "COMMAND=secret" not in serialized
