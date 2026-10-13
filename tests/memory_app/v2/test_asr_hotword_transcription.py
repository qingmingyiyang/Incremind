"""真实编排与文件/缓存边界，隔离 Whisper 推理与付费 HTTP。"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.server_asr_runner import is_builtin
from backend.video_summary.infrastructure.faster_whisper_transcriber import FasterWhisperTranscriber
from backend.video_summary.infrastructure.local_asr_cli import read_hotwords_file
from core.product_core.audio_asset_transcriber import AudioAssetTranscriptionError, TranscribeGeneratedAudioAsset


def test_weighted_complete_words_are_clipped_to_real_token_budget_and_passed_to_model(tmp_path):
    calls = []
    class Model:
        max_length = 40
        hf_tokenizer = SimpleNamespace(encode=lambda text, **kwargs: SimpleNamespace(ids=list(text)))
        def transcribe(self, audio, **kwargs):
            calls.append(kwargs)
            return iter([SimpleNamespace(start=0, end=1, text="结果")]), SimpleNamespace(language="zh", duration=1)
    subject = FasterWhisperTranscriber.__new__(FasterWhisperTranscriber)
    subject._model, subject._language, subject._decode_options = Model(), "zh", {}
    subject.transcribe(tmp_path / "audio.wav", tmp_path / "output", hotwords=("甲", "乙" * 20, "丙"))
    assert calls[0]["hotwords"] == "甲, 丙"
    assert subject.hotword_receipt == {"applied_count": 2, "truncated_count": 1, "token_count": 5}


def test_hotword_file_is_restricted_to_same_user_root(tmp_path):
    root = tmp_path / "user-a"
    root.mkdir()
    inside = root / "words.json"
    inside.write_text(json.dumps({"revision": 3, "terms": [{"term": "专有名词", "weight": 4}]}), encoding="utf-8")
    assert read_hotwords_file(inside, root) == ("专有名词",)
    with pytest.raises(ValueError, match="local_asr_hotword_file_invalid"):
        read_hotwords_file(inside, tmp_path / "user-b")


def test_binding_pins_same_user_selection_and_rejects_changed_lexicon(tmp_path, monkeypatch):
    from backend.api import tokenhub_asr_provider as provider
    from backend.shared.asr_lexicon import user_asr_lexicon
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    store, _ = build_rebuild_object_store(tmp_path)
    local = SimpleNamespace(provider_name="local", command=("builtin:faster-whisper",), model_profile="small", model_name="small", timeout_seconds=30)
    monkeypatch.setattr(provider, "workbench_local_transcriber_settings", lambda *_: local)
    words = user_asr_lexicon(tmp_path)
    words.import_text(text="专业术语", expected_revision=0, preview=False)
    binding_id = provider.freeze_workbench_asr_binding(runtime_root=tmp_path, object_store=store, job_id="run", source_id="source", project_id="inbox")
    original = store.read("workbench_asr_bindings", binding_id)
    assert original["asr_lexicon"] == {"revision": 1, "terms": [{"term": "专业术语", "weight": 4}]}
    words.import_text(text="新增术语", expected_revision=1, preview=False)
    with pytest.raises(provider.TokenHubAsrError, match="binding drifted"):
        provider.freeze_workbench_asr_binding(runtime_root=tmp_path, object_store=store, job_id="run", source_id="source", project_id="inbox")
    assert store.read("workbench_asr_bindings", binding_id) == original


def test_asset_uses_private_word_file_records_counts_and_rejects_old_replay(tmp_path, monkeypatch):
    from core.storage_provider import JsonObjectStore
    from core.product_core.audio_asset_transcriber import SaveAudioAssetTranscriberSettings
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    store = JsonObjectStore(tmp_path / ".rebuild-data")
    model = tmp_path / "data/models/faster-whisper/large-v3-turbo"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    audio = tmp_path / "track.wav"
    audio.write_bytes(b"audio")
    store.write("sources", "source-a", {"id": "source-a", "type": "audio", "metadata": {}}, expected_revision=None)
    store.write("audio_asset_refs", "asset-a", {"id": "asset-a", "source_id": "source-a", "status": "available", "path": str(audio), "audio_asset_ref": "asset:ref"}, expected_revision=None)
    SaveAudioAssetTranscriberSettings(store).execute(enabled=True, command=("builtin:faster-whisper",), confirm_enable=True)
    snapshot = {"revision": 1, "terms": [{"term": "专业术语", "weight": 5}]}
    files = []
    def runner(command, timeout):
        assert is_builtin(command)
        words = Path(command[-1])
        assert command[-2] == "--hotwords-file" and json.loads(words.read_text(encoding="utf-8")) == snapshot
        files.append(words)
        return subprocess.CompletedProcess(command, 0, json.dumps({"language": "zh", "segments": [{"start_seconds": 0, "end_seconds": 1, "text": "专业术语"}],
                                                                  "hotwords": {"applied_count": 1, "truncated_count": 0, "token_count": 4}}), "")
    first = TranscribeGeneratedAudioAsset(store, runner=runner, lexicon_snapshot=snapshot).execute(audio_asset_id="asset-a")
    assert first.status == "completed"
    assert not files[0].exists()
    output = store.read("media_processing_outputs", first.output_id)
    assert output["metadata"]["asr_lexicon"] == {"revision": 1, "selected_count": 1, "hotwords_supported": True, "applied_count": 1, "truncated_count": 0, "token_count": 4}
    replay = TranscribeGeneratedAudioAsset(store, runner=runner, lexicon_snapshot=snapshot).execute(audio_asset_id="asset-a")
    assert replay.status == "completed" and len(files) == 1
    changed = {"revision": 2, "terms": snapshot["terms"]}
    with pytest.raises(AudioAssetTranscriptionError, match="changed after transcript completion"):
        TranscribeGeneratedAudioAsset(store, runner=runner, lexicon_snapshot=changed).execute(audio_asset_id="asset-a")


@pytest.mark.parametrize("tamper", [None, "original", "output"])
def test_changed_lexicon_expires_intact_checkpoint_but_never_accepts_damaged_evidence(tmp_path, tamper):
    from fastapi import HTTPException
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.memory_app.workspace_audio import _audio_original_identity, _make_audio_transcription, _validated_audio_transcription
    from backend.shared.asr_lexicon import user_asr_lexicon
    from core.product_core.audio_asset_transcriber import lexicon_input_identity
    from core.storage_provider import JsonObjectStore
    original = tmp_path / "original.wav"
    original.write_bytes(b"original")
    item = {"id": "item-a", "project_id": "inbox", "original_path": str(original), "source_text": "旧转写"}
    frozen = {"revision": 0, "terms": []}
    source_id, asset_id, binding_id = "source-run", "audio-asset-run", "workbench-asr-run-source-run"
    rebuild, settings = build_rebuild_object_store(tmp_path)
    rebuild.write("workbench_asr_bindings", binding_id, {"id": binding_id, "project_id": "inbox", "source_id": source_id,
                                                        "remote_processing": False, "asr_lexicon": frozen}, expected_revision=None)
    output = {"id": "output-a", "ref": "crp:output-a", "source_id": source_id, "source_type": "audio", "output_kind": "transcript",
              "status": "completed", "provider": "local", "text": "旧转写",
              "metadata": {"audio_asset_id": asset_id, "input_identity": {"asr_lexicon": lexicon_input_identity(frozen)}}}
    internal = JsonObjectStore(tmp_path / "workspace/asr-internal", namespace_id=settings.namespace_id)
    internal.write("media_processing_outputs", "output-a", output, expected_revision=None)
    internal.write("audio_asset_refs", asset_id, {"id": asset_id, "source_id": source_id}, expected_revision=None)
    item["audio_transcription"] = _make_audio_transcription(item, tmp_path, "run", output, _audio_original_identity(original), store_kind="workspace_asr_internal")
    assert _validated_audio_transcription(item, tmp_path) == item["audio_transcription"]
    user_asr_lexicon(tmp_path).import_text(text="新热词", expected_revision=0, preview=False)
    if tamper == "original":
        original.write_bytes(b"changed original")
    elif tamper == "output":
        output["metadata"]["input_identity"]["asr_lexicon"]["revision"] = 999
        internal.write("media_processing_outputs", "output-a", output, expected_revision=None)
    if tamper is None:
        assert _validated_audio_transcription(item, tmp_path) is None
        assert internal.read("media_processing_outputs", "output-a") == output
    else:
        with pytest.raises(HTTPException) as rejected:
            _validated_audio_transcription(item, tmp_path)
        assert rejected.value.status_code == 409 and rejected.value.detail == "audio_transcription_evidence_invalid"


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_existing_concrete_transform_callers_forward_identical_frozen_vocabulary(tmp_path, monkeypatch, kind):
    from backend.api import workbench_content_transform_runtime as runtime
    from core.storage_provider import JsonObjectStore
    store = JsonObjectStore(tmp_path / ".rebuild-data")
    snapshot = {"revision": 7, "terms": [{"term": "冻结术语", "weight": 4}]}
    store.write("workbench_asr_bindings", "binding-a", {"id": "binding-a", "provider": "local-faster-whisper", "asr_lexicon": snapshot}, expected_revision=None)
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"audio")
    store.write("authorized_file_refs", "auth-a", {"id": "auth-a", "path": str(audio)}, expected_revision=None)
    store.write("audio_asset_refs", "asset-a", {"id": "asset-a", "source_id": "source-a"}, expected_revision=None)
    received = {}
    class StopAfterAdmission(Exception):
        pass
    class TranscriberBoundary:
        def __init__(self, object_store, **kwargs):
            received.update(kwargs)
        def execute(self, **kwargs):
            received.update(kwargs)
            raise StopAfterAdmission()
    monkeypatch.setattr(runtime, "TranscribeGeneratedAudioAsset", TranscriberBoundary)
    monkeypatch.setattr(runtime, "workbench_local_transcriber_settings", lambda *_: "selected-settings")
    monkeypatch.setattr(runtime, "_workbench_video_audio_settings", lambda *_: None)
    monkeypatch.setattr(runtime, "ExtractAudioTrackFromAuthorizedVideoSource", lambda *_args, **_kwargs: SimpleNamespace(
        execute=lambda **_kw: SimpleNamespace(status="completed", audio_asset_id="asset-a")))
    domain = runtime.WorkbenchContentTransformDomain(tmp_path, store, "default", None)
    item = {"source_id": "source-a", "authorization_id": "auth-a", "original_asset_ref": "asset:ref",
            "asr_binding_id": "binding-a", "asr_provider": "local-faster-whisper"}
    with pytest.raises(StopAfterAdmission):
        getattr(domain, "_execute_" + kind)(item, execution_ref="facts:run", checkpoint=lambda: None)
    assert received["lexicon_snapshot"] == snapshot
    assert received["hotwords_root"] == tmp_path
    assert received["audio_asset_id"] == "asset-a"
