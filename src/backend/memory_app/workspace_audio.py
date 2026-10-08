"""Governed audio transcription, derivatives and retained evidence checkpoints."""

from __future__ import annotations

import os
import mimetypes
import wave
from collections.abc import Callable
from pathlib import Path
from fastapi import HTTPException
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.tokenhub_asr_provider import TokenHubChunkedAudioAssetTranscriber, freeze_workbench_asr_binding, workbench_local_transcriber_settings
from core.product_core.audio_asset_transcriber import TranscribeGeneratedAudioAsset
from core.product_core.cloud_asr_provider_settings import GetCloudAsrProviderSettings
from core.storage_provider import JsonObjectStore
from backend.security.user_context import json_attribution
from backend.shared.server_resources import RESOURCE_POOL
from backend.api.server_asr_runner import asset_runner
from core.product_core.audio_asset_transcriber import BUILTIN_FASTER_WHISPER_COMMAND



def _audio_original_identity(path: Path) -> dict[str, object]:
    original = path.resolve(strict=True)
    metadata = original.stat()
    return {
        "path": str(original), "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns, "ctime_ns": metadata.st_ctime_ns,
        "device": metadata.st_dev, "inode": metadata.st_ino,
    }


def _make_audio_transcription(
    item: dict, runtime_root: Path, run_id: str, output: dict,
    original_identity: dict[str, object], *, store_kind: str,
) -> dict:
    rebuild_store, storage = build_rebuild_object_store(runtime_root)
    source_id = "source-" + run_id
    asset_id = "audio-asset-" + run_id
    binding_id = f"workbench-asr-{run_id}-{source_id}"
    binding = rebuild_store.read("workbench_asr_bindings", binding_id)
    if not isinstance(binding, dict) or binding.get("project_id") != item["project_id"] or binding.get("source_id") != source_id:
        raise ValueError("audio_transcription_evidence_invalid")
    if (store_kind == "rebuild") != bool(binding.get("remote_processing")):
        raise ValueError("audio_transcription_evidence_invalid")
    output_id = output.get("id")
    output_ref = output.get("ref")
    metadata = output.get("metadata")
    text = output.get("text")
    if (not isinstance(output_id, str) or not output_id or not isinstance(output_ref, str) or not output_ref
            or output.get("source_id") != source_id or output.get("source_type") != "audio"
            or output.get("output_kind") != "transcript" or output.get("status") != "completed"
            or not isinstance(output.get("provider"), str) or not output["provider"]
            or not isinstance(text, str) or not text.strip()
            or not isinstance(metadata, dict) or metadata.get("audio_asset_id") != asset_id):
        raise ValueError("audio_transcription_evidence_invalid")
    return {
        "schema_version": 1, "project_id": item["project_id"], "item_id": item["id"],
        "run_id": run_id, "store_kind": store_kind, "namespace_id": storage.namespace_id,
        "binding_id": binding_id, "source_id": source_id, "audio_asset_id": asset_id,
        "output_id": output_id, "output_ref": output_ref, "provider": output["provider"],
        "original_identity": original_identity, "text": text.strip(),
    }


def _validated_audio_transcription(item: dict, runtime_root: Path) -> dict:
    evidence = item.get("audio_transcription")
    if not isinstance(evidence, dict):
        raise HTTPException(409, "audio_transcription_evidence_invalid")
    try:
        original_identity = _audio_original_identity(Path(item["original_path"]))
        rebuild_store, storage = build_rebuild_object_store(runtime_root)
        if (evidence.get("schema_version") != 1
                or evidence.get("project_id") != item["project_id"]
                or evidence.get("item_id") != item["id"]
                or evidence.get("namespace_id") != storage.namespace_id
                or evidence.get("original_identity") != original_identity
                or evidence.get("text") != item.get("source_text")
                or evidence.get("store_kind") not in {"rebuild", "workspace_asr_internal"}):
            raise ValueError("invalid checkpoint")
        source_id = "source-" + evidence["run_id"]
        asset_id = "audio-asset-" + evidence["run_id"]
        binding_id = f"workbench-asr-{evidence['run_id']}-{source_id}"
        binding = rebuild_store.read("workbench_asr_bindings", binding_id)
        if (evidence.get("source_id") != source_id or evidence.get("audio_asset_id") != asset_id
                or evidence.get("binding_id") != binding_id
                or not isinstance(binding, dict) or binding.get("project_id") != item["project_id"]
                or binding.get("source_id") != source_id
                or bool(binding.get("remote_processing")) != (evidence["store_kind"] == "rebuild")):
            raise ValueError("invalid binding")
        output_store = (rebuild_store if evidence["store_kind"] == "rebuild" else JsonObjectStore(
            runtime_root / "workspace" / "asr-internal", namespace_id=storage.namespace_id,
            mutation_attribution=json_attribution(runtime_root, storage.namespace_id),
        ))
        output = output_store.read("media_processing_outputs", evidence["output_id"])
        asset = output_store.read("audio_asset_refs", asset_id)
        metadata = output.get("metadata") if isinstance(output, dict) else None
        if (not isinstance(output, dict) or not isinstance(asset, dict)
                or output.get("id") != evidence["output_id"]
                or output.get("ref") != evidence["output_ref"]
                or output.get("source_id") != source_id
                or output.get("source_type") != "audio"
                or output.get("output_kind") != "transcript"
                or output.get("status") != "completed"
                or output.get("provider") != evidence["provider"]
                or not isinstance(metadata, dict) or metadata.get("audio_asset_id") != asset_id
                or not isinstance(output.get("text"), str)
                or output["text"].strip() != evidence["text"]
                or asset.get("source_id") != source_id):
            raise ValueError("invalid output")
        return evidence
    except (KeyError, TypeError, ValueError, OSError):
        raise HTTPException(409, "audio_transcription_evidence_invalid") from None


def _remote_asr_target(runtime_root: Path) -> dict[str, object] | None:
    store, _ = build_rebuild_object_store(runtime_root)
    settings = GetCloudAsrProviderSettings(store).execute()
    if not settings.enabled:
        return None
    return {
        "endpoint": settings.endpoint,
        "model": settings.model,
        "revision": settings.settings_revision,
        "timeout_seconds": settings.timeout_seconds,
    }


def _transcribe(
    path: Path, runtime_root: Path, project_id: str, item_id: str, run_id: str,
    *, validate_remote: Callable[[], None] | None = None,
) -> str:
    """Transcribe a retained recording through the selected governed ASR."""
    output = _transcribe_output(
        path, runtime_root, project_id, item_id, run_id,
        validate_remote=validate_remote,
    )
    return str(output["text"]).strip()


def _transcribe_output(
    path: Path, runtime_root: Path, project_id: str, item_id: str, run_id: str,
    *, source_type: str = "audio", max_duration_seconds: float | None = None,
    expected_provider: str | None = None,
    validate_remote: Callable[[], None] | None = None,
) -> dict:
    if max_duration_seconds is not None:
        duration = _media_duration_seconds(path)
        if duration is None or duration <= 0 or duration > max_duration_seconds:
            raise ValueError("audio_transcription_failed")
    store, storage = build_rebuild_object_store(runtime_root)
    source_id = "source-" + run_id
    asset_id = "audio-asset-" + run_id
    if validate_remote is not None:
        validate_remote()
    try:
        binding_id = freeze_workbench_asr_binding(
            runtime_root=runtime_root, object_store=store, job_id=run_id,
            source_id=source_id, project_id=project_id,
        )
    except ValueError as error:
        raise ValueError("asr_unavailable") from error
    binding = store.read("workbench_asr_bindings", binding_id)
    if not isinstance(binding, dict):
        raise ValueError("asr_unavailable")
    if expected_provider is not None and binding.get("provider") != expected_provider:
        raise ValueError("asr_unavailable")
    if binding["provider"] == "tokenhub-asr":
        audio_path, duration_seconds = _cloud_audio_derivative(path, runtime_root, run_id)
        media_type = "audio/wav"
        asset_store = store
    else:
        audio_path, duration_seconds = path, None
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        asset_store = JsonObjectStore(
            runtime_root / "workspace" / "asr-internal", namespace_id=storage.namespace_id,
            mutation_attribution=json_attribution(runtime_root, storage.namespace_id),
        )
        asset_store.write("sources", source_id, {
            "schema_version": "1.0.0", "id": source_id, "type": source_type,
            "title": path.name, "project_id": project_id,
            "metadata": {"workspace_item_id": item_id},
        }, expected_revision=None)
    if validate_remote is not None:
        validate_remote()
    asset_store.write("audio_asset_refs", asset_id, {
        "schema_version": "1.0.0", "id": asset_id, "source_id": source_id,
        "audio_asset_ref": f"crp-ref://{storage.namespace_id}/assets/{asset_id}",
        "path": str(audio_path.resolve(strict=False)), "media_type": media_type,
        "source_type": source_type, "status": "available", "duration_seconds": duration_seconds,
    }, expected_revision=None)
    if binding["provider"] == "tokenhub-asr":
        result = TokenHubChunkedAudioAssetTranscriber(
            runtime_root, store, namespace_id=storage.namespace_id,
            chunk_splitter=_split_workspace_wav_chunk,
            validate_wire=validate_remote,
        ).execute(
            audio_asset_id=asset_id,
            execution_ref=f"facts:workspace-audio/{run_id}", binding_id=binding_id,
        )
    else:
        settings = workbench_local_transcriber_settings(store, binding)
        pool = RESOURCE_POOL.get()
        result = TranscribeGeneratedAudioAsset(
            asset_store, namespace_id=storage.namespace_id,
            settings_override=settings,
            runner=asset_runner(runtime_root) if settings.command == (BUILTIN_FASTER_WHISPER_COMMAND,) else None,
            model_root=pool.model_path('faster-whisper') if pool is not None else None,
        ).execute(audio_asset_id=asset_id)
    output = asset_store.read("media_processing_outputs", result.output_id)
    if result.status != "completed" or not isinstance(output, dict):
        raise ValueError("audio_transcription_failed")
    text = output.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("audio_transcription_failed")
    return output


def _media_duration_seconds(path: Path) -> float | None:
    import av

    with av.open(str(path)) as source:
        if source.duration is not None:
            return source.duration / 1_000_000
        audio = next((stream for stream in source.streams if stream.type == "audio"), None)
        if audio is not None and audio.duration is not None and audio.time_base is not None:
            return float(audio.duration * audio.time_base)
    return None


def _cloud_asr_selected(runtime_root: Path) -> bool:
    store, _ = build_rebuild_object_store(runtime_root)
    return GetCloudAsrProviderSettings(store).execute().enabled


def _cloud_audio_derivative(path: Path, runtime_root: Path, run_id: str) -> tuple[Path, float]:
    import av

    output_dir = runtime_root / "workspace" / "transcripts"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{run_id}.wav"
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        with av.open(str(path)) as source, wave.open(str(partial), "wb") as target:
            streams = [stream for stream in source.streams if stream.type == "audio"]
            if not streams:
                raise ValueError("audio_transcription_failed")
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16000)
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in source.decode(streams[0]):
                for converted in resampler.resample(frame):
                    target.writeframes(converted.to_ndarray().tobytes())
            for converted in resampler.resample(None):
                target.writeframes(converted.to_ndarray().tobytes())
        with wave.open(str(partial), "rb") as converted_audio:
            if converted_audio.getnframes() == 0:
                raise ValueError("audio_transcription_failed")
        os.replace(partial, output)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    with wave.open(str(output), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
    return output, duration


def _split_workspace_wav_chunk(source_path: str, output_path: str, start: float, end: float) -> str:
    source = Path(source_path)
    output = Path(output_path)
    partial = output.with_name(f".{output.name}.partial")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial.unlink(missing_ok=True)
    try:
        with wave.open(str(source), "rb") as input_audio:
            rate = input_audio.getframerate()
            if rate != 16000 or input_audio.getnchannels() != 1 or input_audio.getsampwidth() != 2:
                raise ValueError("audio_transcription_failed")
            first = max(0, int(start * rate))
            count = max(0, int(end * rate) - first)
            if count == 0:
                raise ValueError("audio_transcription_failed")
            input_audio.setpos(first)
            with wave.open(str(partial), "wb") as chunk:
                chunk.setnchannels(1)
                chunk.setsampwidth(2)
                chunk.setframerate(rate)
                chunk.writeframes(input_audio.readframes(count))
        os.replace(partial, output)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return str(output)
