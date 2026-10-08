from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.routes.cloud_asr_settings import router
from backend.api.tokenhub_asr_provider import (
    TokenHubAsrError,
    TokenHubAudioAssetTranscriber,
    TokenHubChunkedAudioAssetTranscriber,
    freeze_workbench_asr_binding,
    tokenhub_egress_manifest,
    workbench_local_transcriber_settings,
)
from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.security.secrets import InMemorySecretStore
from core.product_core.cloud_asr_provider_settings import (
    TOKENHUB_ASR_SECRET_REF,
    SaveCloudAsrProviderSettings,
)
from core.product_core.local_asr_provider_settings import SaveLocalAsrProviderSettings


_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"


def _completed_contract_response() -> dict[str, object]:
    fixture = json.loads((_FIXTURE_ROOT / "tokenhub_hy_asr_sync_completed.json").read_text("utf-8"))
    return fixture["response"]


def _client(tmp_path: Path, secrets: InMemorySecretStore) -> TestClient:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path, secret_store=secrets)
    app.include_router(router)
    return TestClient(app)


def test_long_retry_identity_keeps_asr_binding_addressable(tmp_path: Path) -> None:
    store, _ = build_rebuild_object_store(tmp_path)
    SaveLocalAsrProviderSettings(store).execute(
        enabled=True, confirm_enable=True, command=(sys.executable, "--version"),
    )
    options = {
        "runtime_root": tmp_path,
        "object_store": store,
        "job_id": "workbench-transform-retry-" + "r" * 100,
        "source_id": "source-video-" + "s" * 30,
        "project_id": "stage3-media-qa",
    }

    first = freeze_workbench_asr_binding(**options)
    replay = freeze_workbench_asr_binding(**options)

    assert len(first) <= 128
    assert replay == first
    assert store.read("workbench_asr_bindings", first)["source_id"] == options["source_id"]


def test_workspace_run_chunk_plan_uses_addressable_id_and_replays(tmp_path: Path) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    run_id = "workspace-run-" + "a" * 32
    source_id = f"source-{run_id}"
    audio_asset_id = f"audio-asset-{run_id}"
    binding_id = f"workbench-asr-{run_id}-{source_id}"
    transcriber = TokenHubChunkedAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id,
    )
    options = {
        "audio_asset_id": audio_asset_id,
        "source_id": source_id,
        "binding_id": binding_id,
        "execution_ref": f"facts:workspace-audio/{run_id}",
        "total_duration": 1037.8,
        "parent_evidence": {
            "parent_asset_revision": 1,
            "parent_duration_seconds": 1037.8,
            "parent_audio_path": str(tmp_path / "source.wav"),
            "parent_file_size_bytes": 33209730,
            "parent_file_mtime_ns": 1,
        },
    }

    plans = transcriber._freeze_chunk_plan(**options)
    record = store.list("cloud_asr_chunk_plans")[0]

    assert len(plans) == 19
    assert len(record["id"]) <= 128
    assert transcriber._freeze_chunk_plan(**options) == plans
    assert len(store.list("cloud_asr_chunk_plans")) == 1


def test_short_chunk_plan_keeps_existing_id(tmp_path: Path) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    transcriber = TokenHubChunkedAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id,
    )
    transcriber._freeze_chunk_plan(
        audio_asset_id="audio-short", source_id="source-short",
        binding_id="binding-short", execution_ref="facts:workspace-audio/short",
        total_duration=90.0,
        parent_evidence={
            "parent_asset_revision": 1,
            "parent_duration_seconds": 90.0,
            "parent_audio_path": str(tmp_path / "short.wav"),
            "parent_file_size_bytes": 3000000,
            "parent_file_mtime_ns": 1,
        },
    )
    assert store.list("cloud_asr_chunk_plans")[0]["id"] == (
        "tokenhub-asr-chunk-plan-binding-short-audio-short"
    )


def test_new_workbench_asr_binding_requires_explicit_available_local_selection(tmp_path: Path) -> None:
    store, _ = build_rebuild_object_store(tmp_path)
    options = {
        "runtime_root": tmp_path, "object_store": store,
        "job_id": "job-local-1", "source_id": "source-video-1", "project_id": "default",
    }
    with pytest.raises(TokenHubAsrError, match="local ASR is disabled"):
        freeze_workbench_asr_binding(**options)
    assert store.list("workbench_asr_bindings") == ()

    SaveLocalAsrProviderSettings(store).execute(
        enabled=True, confirm_enable=True, command=("missing-local-asr-executable",),
    )
    with pytest.raises(TokenHubAsrError, match="selected local ASR is unavailable"):
        freeze_workbench_asr_binding(**options)
    assert store.list("workbench_asr_bindings") == ()


def test_explicit_ui_local_selection_precedes_old_audio_asset_record_and_is_frozen(tmp_path: Path) -> None:
    store, _ = build_rebuild_object_store(tmp_path)
    store.write("audio_asset_transcriber_settings", "default", {
        "id": "default", "enabled": False, "command": [],
    }, expected_revision=None)
    SaveLocalAsrProviderSettings(store).execute(
        enabled=True, confirm_enable=True, command=(sys.executable, "--version"),
    )
    binding_id = freeze_workbench_asr_binding(
        runtime_root=tmp_path, object_store=store, job_id="job-local-ui",
        source_id="source-video-ui", project_id="default",
    )
    binding = store.read("workbench_asr_bindings", binding_id)
    assert binding["provider"] == "local-faster-whisper"
    assert binding["local_settings"]["command"] == [sys.executable, "--version"]
    SaveLocalAsrProviderSettings(store).execute(enabled=False, command=())
    frozen = workbench_local_transcriber_settings(store, binding)
    assert frozen.enabled is True
    assert frozen.command == (sys.executable, "--version")
    with pytest.raises(TokenHubAsrError, match="local ASR is disabled"):
        freeze_workbench_asr_binding(
            runtime_root=tmp_path, object_store=store, job_id="job-local-ui-next",
            source_id="source-video-ui", project_id="default",
        )


def test_cloud_asr_settings_require_key_consent_and_explicit_enable(tmp_path: Path) -> None:
    secrets = InMemorySecretStore()
    client = _client(tmp_path, secrets)
    initial = client.get("/api/rebuild/settings/cloud-asr-provider")
    assert initial.status_code == 200
    assert initial.json()["status"] == "disabled"
    assert initial.json()["model"] == "hy-asr-3.0-preview"
    assert initial.json()["remote_processing"] is True
    assert initial.json()["long_audio_mode"] == "cloud_overlap_chunks"
    assert initial.json()["chunk_duration_seconds"] == 60
    assert initial.json()["chunk_overlap_seconds"] == 5

    rejected = client.put(
        "/api/rebuild/settings/cloud-asr-provider",
        json={"enabled": True, "confirm_enable": False},
    )
    assert rejected.status_code == 409
    enabled = client.put(
        "/api/rebuild/settings/cloud-asr-provider",
        json={"enabled": True, "confirm_enable": True},
    )
    assert enabled.json()["status"] == "needs_api_key"
    secrets.set(TOKENHUB_ASR_SECRET_REF, "test-tokenhub-secret")
    with_key = client.get("/api/rebuild/settings/cloud-asr-provider")
    assert with_key.json()["status"] == "needs_consent"
    manifest_id = with_key.json()["egress_manifest"]["manifest_id"]
    consented = client.post(
        "/api/rebuild/settings/cloud-asr-provider/egress-consent",
        json={"manifest_id": manifest_id, "confirm": True},
    )
    assert consented.json()["status"] == "ready"
    assert "test-tokenhub-secret" not in consented.text


def test_tokenhub_transcriber_sends_only_bounded_audio_and_replays_local_receipt(
    tmp_path: Path,
) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "tokenhub-secret-value"})
    SaveCloudAsrProviderSettings(store, now="2026-09-04T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(
        manifest, manifest_id=manifest.manifest_id, confirm=True,
    )
    binding_id = freeze_workbench_asr_binding(
        runtime_root=tmp_path,
        object_store=store,
        job_id="workbench-job-1",
        source_id="source-video-1",
        project_id="default",
        secret_store=secrets,
    )
    audio = tmp_path / "short.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 4096)
    store.write("audio_asset_refs", "audio-1", {
        "schema_version": "1.0.0",
        "id": "audio-1",
        "source_id": "source-video-1",
        "audio_asset_ref": "crp-ref://default/assets/audio-1",
        "path": str(audio),
        "media_type": "audio/wav",
        "status": "available",
    }, expected_revision=None)
    calls = []

    def caller(url, headers, body, timeout):
        calls.append((url, dict(headers), json.loads(body), timeout))
        response = _completed_contract_response()
        response["output"]["subtitle_url"] = "https://not-persisted.invalid/subtitle.srt"
        return 200, response

    transcriber = TokenHubAudioAssetTranscriber(
        tmp_path,
        store,
        namespace_id=settings.namespace_id,
        secret_store=secrets,
        http_caller=caller,
    )
    first = transcriber.execute(
        audio_asset_id="audio-1",
        execution_ref="facts:effect/workbench-effect-1",
        binding_id=binding_id,
    )
    legacy_output = store.read("media_processing_outputs", first.output_id)
    legacy_output["metadata"].pop("binding_id")
    store.write("media_processing_outputs", first.output_id, legacy_output, expected_revision=None)
    replay = transcriber.execute(
        audio_asset_id="audio-1",
        execution_ref="facts:effect/workbench-effect-1",
        binding_id=binding_id,
    )
    dispatch = store.list("cloud_asr_dispatches")[0]
    dispatch.update({"status": "unknown", "output_id": None})
    store.write("cloud_asr_dispatches", dispatch["id"], dispatch, expected_revision=None)
    recovered = transcriber.execute(
        audio_asset_id="audio-1",
        execution_ref="facts:effect/workbench-effect-1",
        binding_id=binding_id,
    )

    assert first.status == replay.status == recovered.status == "completed"
    assert len(calls) == 1
    assert store.read("cloud_asr_dispatches", dispatch["id"])["status"] == "completed"
    assert calls[0][0].endswith("/v1/wand/asrproxy/sync_transcribe")
    assert calls[0][1]["Authorization"] == "Bearer tokenhub-secret-value"
    assert set(calls[0][2]) == {"model", "data", "voice_encode_format"}
    output = store.read("media_processing_outputs", first.output_id)
    assert output["metadata"]["remote_processing"] is True
    assert output["metadata"]["audio_path_stored_in_output"] is False
    assert "subtitle_url" not in json.dumps(output)
    assert "tokenhub-secret-value" not in json.dumps(store.list("cloud_asr_dispatches"))

    store.write("audio_asset_refs", "audio-2", {
        "schema_version": "1.0.0", "id": "audio-2",
        "source_id": "source-video-1",
        "audio_asset_ref": "crp-ref://default/assets/audio-2",
        "path": str(audio), "media_type": "audio/wav", "status": "available",
    }, expected_revision=None)

    def revoked():
        raise ValueError("target changed")

    guarded = TokenHubAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id,
        secret_store=secrets, http_caller=caller, validate_wire=revoked,
    )
    with pytest.raises(TokenHubAsrError, match="remote_processing_target_changed"):
        guarded.execute(audio_asset_id="audio-2", execution_ref="facts:effect/new-attempt",
                        binding_id=binding_id)
    assert len(calls) == 1


def test_tokenhub_transcriber_stops_automatic_replay_after_unknown_outcome(tmp_path: Path) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "secret"})
    SaveCloudAsrProviderSettings(store, now="2026-09-04T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    binding_id = freeze_workbench_asr_binding(
        runtime_root=tmp_path, object_store=store, job_id="job-2",
        source_id="source-2", project_id="default", secret_store=secrets,
    )
    audio = tmp_path / "short.wav"
    audio.write_bytes(b"RIFFaudio")
    store.write("audio_asset_refs", "audio-2", {
        "id": "audio-2", "source_id": "source-2", "audio_asset_ref": "crp-ref://audio-2",
        "path": str(audio), "media_type": "audio/wav", "status": "available",
    }, expected_revision=None)
    calls = 0

    def uncertain(*_args):
        nonlocal calls
        calls += 1
        raise OSError("connection lost")

    transcriber = TokenHubAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id,
        secret_store=secrets, http_caller=uncertain,
    )
    for _ in range(2):
        with pytest.raises(TokenHubAsrError, match="replay|unknown"):
            transcriber.execute(
                audio_asset_id="audio-2",
                execution_ref="facts:effect/workbench-effect-2",
                binding_id=binding_id,
            )
    assert calls == 1


def test_tokenhub_transcriber_does_not_retry_a_rate_limited_dispatch(tmp_path: Path) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "secret"})
    SaveCloudAsrProviderSettings(store, now="2026-09-05T00:00:00Z").execute(enabled=True, confirm_enable=True)
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    binding_id = freeze_workbench_asr_binding(
        runtime_root=tmp_path, object_store=store, job_id="rate-limited", source_id="source-rate-limited",
        project_id="default", secret_store=secrets,
    )
    audio = tmp_path / "rate-limited.wav"
    audio.write_bytes(b"RIFFaudio")
    store.write("audio_asset_refs", "audio-rate-limited", {
        "id": "audio-rate-limited", "source_id": "source-rate-limited",
        "audio_asset_ref": "crp-ref://audio-rate-limited", "path": str(audio),
        "media_type": "audio/wav", "status": "available",
    }, expected_revision=None)
    calls = 0

    def rate_limited(*_args):
        nonlocal calls
        calls += 1
        return 429, {"error": "rate_limited"}

    transcriber = TokenHubAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id, secret_store=secrets, http_caller=rate_limited,
    )
    for _ in range(2):
        with pytest.raises(TokenHubAsrError, match="429|replayable"):
            transcriber.execute(
                audio_asset_id="audio-rate-limited", execution_ref="facts:effect/rate-limited", binding_id=binding_id,
            )
    assert calls == 1


def test_tokenhub_chunked_transcriber_sends_overlapping_chunks_and_merges_once(
    tmp_path: Path,
) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "secret"})
    SaveCloudAsrProviderSettings(store, now="2026-09-05T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    binding_id = freeze_workbench_asr_binding(
        runtime_root=tmp_path, object_store=store, job_id="job-long",
        source_id="source-long", project_id="default", secret_store=secrets,
    )
    parent = tmp_path / "long.wav"
    parent.write_bytes(b"RIFF" + b"x" * (2 * 1024 * 1024))
    store.write("audio_asset_refs", "audio-long", {
        "id": "audio-long", "source_id": "source-long",
        "audio_asset_ref": "crp-ref://default/assets/audio-long",
        "path": str(parent), "media_type": "audio/wav", "status": "available",
        "duration_seconds": 90.0, "sample_rate_hz": 16000, "channels": 1,
    }, expected_revision=None)
    split_ranges = []

    def splitter(_source, output, start, end):
        split_ranges.append((start, end))
        Path(output).write_bytes(b"RIFF" + b"c" * 1024)
        return output

    calls = []

    def caller(url, headers, body, timeout):
        index = len(calls)
        calls.append((url, headers, json.loads(body), timeout))
        if index == 0:
            sentences = [
                {"begin_ms": 0, "end_ms": 56000, "text": "第一句。"},
                {"begin_ms": 56000, "end_ms": 60000, "text": "衔接内容。"},
            ]
            text = "第一句。衔接内容。"
            duration = 60000
        else:
            sentences = [
                {"begin_ms": 0, "end_ms": 5000, "text": "衔接内容。"},
                {"begin_ms": 5000, "end_ms": 35000, "text": "第二句。"},
            ]
            text = "衔接内容。第二句。"
            duration = 35000
        return 200, {
            "status": "completed",
            "output": {"source": "zh", "duration_ms": duration, "text": text, "sentences": sentences},
            "usage": {"total_token": 10},
        }

    transcriber = TokenHubChunkedAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id,
        secret_store=secrets, http_caller=caller, chunk_splitter=splitter,
    )
    first = transcriber.execute(
        audio_asset_id="audio-long", execution_ref="facts:effect/long-1", binding_id=binding_id,
    )
    frozen_plan = store.list("cloud_asr_chunk_plans")[0]
    assert frozen_plan["binding_id"] == binding_id
    assert frozen_plan["execution_ref"] == "facts:effect/long-1"
    replay = transcriber.execute(
        audio_asset_id="audio-long", execution_ref="facts:effect/long-1", binding_id=binding_id,
    )

    assert first.output_id == replay.output_id == "media-output-transcript-tokenhub-source-long"
    assert split_ranges == [(0.0, 60.0), (55.0, 90.0)]
    assert len(calls) == 2
    assert all(len(call[2]["data"]) < 2 * 1024 * 1024 for call in calls)
    output = store.read("media_processing_outputs", first.output_id)
    assert output["text"] == "第一句。衔接内容。第二句。"
    assert output["metadata"]["chunk_count"] == 2
    assert output["metadata"]["chunk_overlap_seconds"] == 5.0
    assert len(output["metadata"]["dispatch_refs"]) == 2
    assert len(store.list("cloud_asr_dispatches")) == 2
    assert len(store.list("cloud_asr_chunk_plans")) == 1
    assert frozen_plan["parent_asset_revision"] == 1
    assert frozen_plan["parent_duration_seconds"] == 90.0
    assert frozen_plan["parent_file_size_bytes"] == parent.stat().st_size
    assert frozen_plan["parent_file_mtime_ns"] == parent.stat().st_mtime_ns

    invalid_plan = json.loads(json.dumps(frozen_plan))
    invalid_plan["plans"][1]["start_seconds"] = -1.0
    store.write("cloud_asr_chunk_plans", frozen_plan["id"], invalid_plan, expected_revision=None)
    with pytest.raises(TokenHubAsrError, match="chunk plan is invalid"):
        transcriber.execute(
            audio_asset_id="audio-long", execution_ref="facts:effect/long-1", binding_id=binding_id,
        )
    store.write("cloud_asr_chunk_plans", frozen_plan["id"], frozen_plan, expected_revision=None)

    parent_record = store.read("audio_asset_refs", "audio-long")
    parent_record["duration_seconds"] = 130.0
    store.write("audio_asset_refs", "audio-long", parent_record, expected_revision=None)
    with pytest.raises(TokenHubAsrError, match="chunk plan binding drifted"):
        transcriber.execute(
            audio_asset_id="audio-long", execution_ref="facts:effect/long-1", binding_id=binding_id,
        )
    assert len(calls) == 2


def test_tokenhub_chunked_transcriber_stops_on_an_unknown_chunk_without_replay(tmp_path: Path) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "secret"})
    SaveCloudAsrProviderSettings(store, now="2026-09-05T00:00:00Z").execute(enabled=True, confirm_enable=True)
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    binding_id = freeze_workbench_asr_binding(
        runtime_root=tmp_path, object_store=store, job_id="chunk-unknown", source_id="source-chunk-unknown",
        project_id="default", secret_store=secrets,
    )
    parent = tmp_path / "unknown-long.wav"
    parent.write_bytes(b"RIFF" + b"x" * (2 * 1024 * 1024))
    store.write("audio_asset_refs", "audio-chunk-unknown", {
        "id": "audio-chunk-unknown", "source_id": "source-chunk-unknown",
        "audio_asset_ref": "crp-ref://audio-chunk-unknown", "path": str(parent),
        "media_type": "audio/wav", "status": "available", "duration_seconds": 90.0,
    }, expected_revision=None)
    calls = 0

    def splitter(_source, output, _start, _end):
        Path(output).write_bytes(b"RIFFchunk")
        return output

    def caller(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 200, _completed_contract_response()
        raise OSError("connection lost after send")

    transcriber = TokenHubChunkedAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id, secret_store=secrets,
        http_caller=caller, chunk_splitter=splitter,
    )
    for _ in range(2):
        with pytest.raises(TokenHubAsrError, match="unknown|replayable"):
            transcriber.execute(
                audio_asset_id="audio-chunk-unknown", execution_ref="facts:effect/chunk-unknown", binding_id=binding_id,
            )
    assert calls == 2
    assert len(store.list("cloud_asr_chunk_plans")) == 1


@pytest.mark.parametrize("response", [
    {"status": "completed", "output": {"text": "ok", "sentences": [{"begin_ms": True, "end_ms": 1, "text": "ok"}]}},
    {"status": "completed", "output": {"text": "", "sentences": []}},
    ["not", "a", "response"],
])
def test_tokenhub_transcriber_marks_invalid_completed_contract_as_failed(
    tmp_path: Path, response: object,
) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "secret"})
    SaveCloudAsrProviderSettings(store, now="2026-09-05T00:00:00Z").execute(enabled=True, confirm_enable=True)
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    binding_id = freeze_workbench_asr_binding(
        runtime_root=tmp_path, object_store=store, job_id="invalid-contract", source_id="source-invalid",
        project_id="default", secret_store=secrets,
    )
    audio = tmp_path / "invalid.wav"
    audio.write_bytes(b"RIFFaudio")
    store.write("audio_asset_refs", "audio-invalid", {
        "id": "audio-invalid", "source_id": "source-invalid", "audio_asset_ref": "crp-ref://audio-invalid",
        "path": str(audio), "media_type": "audio/wav", "status": "available",
    }, expected_revision=None)
    transcriber = TokenHubAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id, secret_store=secrets,
        http_caller=lambda *_args: (200, response),
    )

    with pytest.raises(TokenHubAsrError, match="response|timestamp|text"):
        transcriber.execute(
            audio_asset_id="audio-invalid", execution_ref="facts:effect/invalid-contract", binding_id=binding_id,
        )
    dispatch = store.list("cloud_asr_dispatches")[0]
    assert dispatch["status"] == "failed"


def test_tokenhub_completed_dispatch_replay_requires_matching_output_receipt(tmp_path: Path) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "secret"})
    SaveCloudAsrProviderSettings(store, now="2026-09-05T00:00:00Z").execute(enabled=True, confirm_enable=True)
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    binding_id = freeze_workbench_asr_binding(
        runtime_root=tmp_path, object_store=store, job_id="replay", source_id="source-replay",
        project_id="default", secret_store=secrets,
    )
    audio = tmp_path / "replay.wav"
    audio.write_bytes(b"RIFFaudio")
    store.write("audio_asset_refs", "audio-replay", {
        "id": "audio-replay", "source_id": "source-replay", "audio_asset_ref": "crp-ref://audio-replay",
        "path": str(audio), "media_type": "audio/wav", "status": "available",
    }, expected_revision=None)
    calls = 0

    def caller(*_args):
        nonlocal calls
        calls += 1
        return 200, _completed_contract_response()

    transcriber = TokenHubAudioAssetTranscriber(
        tmp_path, store, namespace_id=settings.namespace_id, secret_store=secrets, http_caller=caller,
    )
    result = transcriber.execute(
        audio_asset_id="audio-replay", execution_ref="facts:effect/replay", binding_id=binding_id,
    )
    output = store.read("media_processing_outputs", result.output_id)
    output["metadata"]["binding_id"] = "unrelated-binding"
    store.write("media_processing_outputs", result.output_id, output, expected_revision=None)

    with pytest.raises(TokenHubAsrError, match="unavailable"):
        transcriber.execute(
            audio_asset_id="audio-replay", execution_ref="facts:effect/replay", binding_id=binding_id,
        )
    assert calls == 1
