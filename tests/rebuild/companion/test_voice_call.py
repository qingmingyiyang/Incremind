from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.companion_core import (
    CompanionConflict,
    CompanionRepositoryError,
    CompanionVoiceGrantStore,
    CompanionVoiceTranscriptionService,
)
from core.companion_core import voice_call as voice_module
from core.product_core.local_asr_provider import EphemeralLocalAsrResult, LocalAsrProviderError
from core.product_core.local_asr_provider_settings import LOCAL_ASR_MODEL_OPTIONS, LocalAsrProviderSettings


def webm() -> bytes:
    return b"\x1aE\xdf\xa3" + b"voice-canary" * 8


def settings(*, enabled: bool = True, status: str = "ready") -> LocalAsrProviderSettings:
    return LocalAsrProviderSettings(
        status=status, enabled=enabled, provider_name="local-test", command=("fake", "{audio_path}"),
        model_profile="small", model_name="small", model_options=LOCAL_ASR_MODEL_OPTIONS,
        diagnostic="ready" if status == "ready" else "model_missing", model_status="ready",
        timeout_seconds=10.0, explicit_enable_required=True, remote_processing=False, memory_publication="not_started",
    )


def issue(store: CompanionVoiceGrantStore, data: bytes | None = None):
    value = data or webm(); staging = store.create_staging(); staging.write_bytes(value)
    return store.commit_staging(path=staging, media_type="audio/webm", expected_size=len(value), expected_sha256=hashlib.sha256(value).hexdigest())


def test_voice_grant_stream_stage_is_single_use_and_deletes_audio(tmp_path: Path) -> None:
    store = CompanionVoiceGrantStore(session_id="session", root=tmp_path / "voice")
    grant = issue(store)
    assert set(grant.public()) == {"grant_id", "media_type", "byte_length"}
    taken = store.take(grant.grant_id)
    assert taken.path.is_file()
    taken.path.unlink()
    with pytest.raises(CompanionConflict, match="already consumed"):
        store.take(grant.grant_id)


def test_voice_grant_rejects_bad_magic_change_expiry_and_path_escape(tmp_path: Path) -> None:
    clock = [1.0]; store = CompanionVoiceGrantStore(session_id="session", root=tmp_path / "voice", now=lambda: clock[0])
    staging = store.create_staging(); staging.write_bytes(b"not-webm")
    with pytest.raises(CompanionRepositoryError, match="integrity"):
        store.commit_staging(path=staging, media_type="audio/webm", expected_size=8, expected_sha256=hashlib.sha256(b"not-webm").hexdigest())
    grant = issue(store); grant.path.write_bytes(webm() + b"changed")
    with pytest.raises(CompanionConflict, match="changed"):
        store.take(grant.grant_id)
    grant = issue(store); clock[0] += 121
    with pytest.raises(CompanionConflict, match="expired"):
        store.take(grant.grant_id)
    outside = tmp_path / ("voice-upload-" + "a" * 48 + ".part")
    with pytest.raises(CompanionRepositoryError):
        store.commit_staging(path=outside, media_type="audio/webm", expected_size=1, expected_sha256="0" * 64)


def test_startup_sweep_removes_only_owned_plain_voice_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(voice_module.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "chriptmas-companion-voice-call" / ("a" * 24); old.mkdir(parents=True)
    owned = old / ("voice-grant-" + "b" * 48 + ".webm"); owned.write_bytes(webm())
    part = old / ("voice-upload-" + "c" * 48 + ".part"); part.write_bytes(webm())
    unrelated = old / "keep.webm"; unrelated.write_bytes(webm())
    CompanionVoiceGrantStore(session_id="new")
    assert not owned.exists() and not part.exists() and unrelated.exists()


def test_transcription_consumes_audio_returns_bounded_metadata_and_never_persists(tmp_path: Path, monkeypatch) -> None:
    calls = []
    def fake(path, **kwargs):
        calls.append((path, kwargs)); return EphemeralLocalAsrResult("你好，桌面伙伴。", "zh", "local-test")
    monkeypatch.setattr(voice_module, "transcribe_ephemeral_local_audio", fake)
    store = CompanionVoiceGrantStore(session_id="session", root=tmp_path / "voice"); grant = issue(store)
    result = CompanionVoiceTranscriptionService(grant_store=store, settings_loader=settings).transcribe(request_id="voice:12345678", grant_id=grant.grant_id)
    assert result["text"] == "你好，桌面伙伴。" and result["provider"] == "local_asr"
    assert set(result["trace"]) == {"audio_bytes", "elapsed_ms", "remote_processing"}
    assert not calls[0][0].exists() and str(tmp_path) not in str(result) and "sha" not in str(result).lower()


def test_transcription_disabled_cancelled_and_invalid_output_delete_grant(tmp_path: Path, monkeypatch) -> None:
    store = CompanionVoiceGrantStore(session_id="session", root=tmp_path / "voice"); grant = issue(store)
    with pytest.raises(CompanionRepositoryError, match="asr_unavailable"):
        CompanionVoiceTranscriptionService(grant_store=store, settings_loader=lambda: settings(enabled=False, status="disabled")).transcribe(request_id="voice:12345678", grant_id=grant.grant_id)
    assert not grant.path.exists()
    grant = issue(store)
    with pytest.raises(CompanionConflict, match="cancelled"):
        CompanionVoiceTranscriptionService(grant_store=store, settings_loader=settings).transcribe(request_id="voice:12345678", grant_id=grant.grant_id, cancelled=lambda: True)
    monkeypatch.setattr(voice_module, "transcribe_ephemeral_local_audio", lambda *_a, **_k: EphemeralLocalAsrResult("", None, "local-test"))
    grant = issue(store)
    with pytest.raises(CompanionRepositoryError, match="invalid transcript"):
        CompanionVoiceTranscriptionService(grant_store=store, settings_loader=settings).transcribe(request_id="voice:12345678", grant_id=grant.grant_id)


def test_ephemeral_asr_failure_never_exposes_provider_diagnostics(tmp_path: Path, monkeypatch) -> None:
    canary = f"ASR_PATH_CANARY={tmp_path} MODEL=C:\\private\\model COMMAND=secret"
    def failed(*_args, **_kwargs):
        raise LocalAsrProviderError(canary)
    monkeypatch.setattr(voice_module, "transcribe_ephemeral_local_audio", failed)
    store = CompanionVoiceGrantStore(session_id="session", root=tmp_path / "voice"); grant = issue(store)
    with pytest.raises(CompanionRepositoryError) as captured:
        CompanionVoiceTranscriptionService(grant_store=store, settings_loader=settings).transcribe(request_id="voice:12345678", grant_id=grant.grant_id)
    assert str(captured.value) == "companion_voice_asr_failed"
    assert canary not in str(captured.value) and str(tmp_path) not in str(captured.value)
    assert not grant.path.exists()
