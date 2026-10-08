from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.api.local_audio_chunking import split_local_audio_chunk
from backend.shared.server_resources import RESOURCE_POOL
from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.security.secret_egress import SecretEgressBroker
from backend.security.secrets import SecretStore, build_secret_store
from core.product_core.audio_asset_transcriber import AudioAssetTranscriptionResult
from core.product_core.audio_asset_transcriber import (
    BUILTIN_FASTER_WHISPER_COMMAND,
    GetAudioAssetTranscriberSettings,
)
from core.product_core.local_asr_provider_settings import GetLocalAsrProviderSettings
from core.product_core.cloud_asr_chunking import (
    CLOUD_ASR_CHUNK_DURATION_SECONDS,
    CLOUD_ASR_CHUNK_OVERLAP_SECONDS,
    CloudAsrChunkPlan,
    merge_cloud_asr_chunk_outputs,
    plan_cloud_asr_chunks,
)
from core.product_core.cloud_asr_provider_settings import (
    TOKENHUB_ASR_ENDPOINT,
    TOKENHUB_ASR_MAX_REQUEST_BYTES,
    TOKENHUB_ASR_MODEL,
    TOKENHUB_ASR_PROVIDER_ID,
    TOKENHUB_ASR_PROVIDER_NAME,
    TOKENHUB_ASR_SECRET_REF,
    GetCloudAsrProviderSettings,
)


_BOUNDARY_REVISION = "tokenhub-asr-boundary-v2"
_PROVIDER_REVISION = "tokenhub-hy-asr-sync-chunked-v2"
HttpCaller = Callable[[str, Mapping[str, str], bytes, float], tuple[int, object]]


class TokenHubAsrError(ValueError):
    pass


def tokenhub_egress_manifest(root_dir: Path):
    return ProviderEgressPolicyStore(root_dir).manifest(
        provider_id=TOKENHUB_ASR_PROVIDER_ID,
        endpoint=TOKENHUB_ASR_ENDPOINT,
        purposes=("video_processing",),
        payload_categories=("audio_data", "audio_chunks", "provider_metadata"),
        max_payload_bytes=TOKENHUB_ASR_MAX_REQUEST_BYTES,
    )


def freeze_workbench_asr_binding(
    *,
    runtime_root: Path,
    object_store: object,
    job_id: str,
    source_id: str,
    project_id: str,
    secret_store: SecretStore | None = None,
) -> str:
    settings = GetCloudAsrProviderSettings(object_store).execute()
    binding_id = f"workbench-asr-{job_id}-{source_id}"
    if len(binding_id) > 128:
        binding_id = f"workbench-asr-{uuid5(NAMESPACE_URL, f'{job_id}:{source_id}').hex}"
    if not settings.enabled:
        local_settings = workbench_local_transcriber_settings(object_store)
        record = {
            "schema_version": "1.0.0",
            "id": binding_id,
            "provider": "local-faster-whisper",
            "provider_revision": "workbench-local-faster-whisper-v1",
            "project_id": project_id,
            "source_id": source_id,
            "settings_revision": 0,
            "secret_generation": 0,
            "egress_manifest_id": "not-applicable",
            "remote_processing": False,
            "local_settings": {
                "provider_name": local_settings.provider_name,
                "command": list(local_settings.command),
                "model_profile": local_settings.model_profile,
                "model_name": local_settings.model_name,
                "timeout_seconds": local_settings.timeout_seconds,
            },
        }
    else:
        secrets = secret_store or build_secret_store(runtime_root)
        generation = secrets.get_generation(TOKENHUB_ASR_SECRET_REF)
        if generation < 1 or not secrets.has_secret(TOKENHUB_ASR_SECRET_REF):
            raise TokenHubAsrError("Hy-ASR is enabled but its API Key is unavailable")
        manifest = tokenhub_egress_manifest(runtime_root)
        if not ProviderEgressPolicyStore(runtime_root).is_consented(manifest):
            raise TokenHubAsrError("Hy-ASR audio egress consent is required")
        record = {
            "schema_version": "1.0.0",
            "id": binding_id,
            "provider": TOKENHUB_ASR_PROVIDER_ID,
            "provider_revision": _PROVIDER_REVISION,
            "project_id": project_id,
            "source_id": source_id,
            "settings_revision": settings.settings_revision,
            "secret_generation": generation,
            "egress_manifest_id": manifest.manifest_id,
            "remote_processing": True,
        }
    existing = object_store.read("workbench_asr_bindings", binding_id)
    if existing is None:
        object_store.write("workbench_asr_bindings", binding_id, record, expected_revision=None)
    elif existing != record:
        raise TokenHubAsrError("workbench ASR binding drifted")
    return binding_id


def workbench_local_transcriber_settings(object_store: object, binding: Mapping[str, object] | None = None):
    """Resolve an explicitly selected local ASR, preserving new Job settings."""
    settings = GetAudioAssetTranscriberSettings(object_store).execute()
    frozen = binding.get("local_settings") if isinstance(binding, Mapping) else None
    if isinstance(frozen, Mapping):
        return replace(
            settings,
            status="ready",
            enabled=True,
            provider_name=str(frozen["provider_name"]),
            command=tuple(frozen["command"]),
            model_profile=str(frozen["model_profile"]),
            model_name=str(frozen["model_name"]),
            timeout_seconds=float(frozen["timeout_seconds"]),
        )
    if binding is not None:
        # Bindings created before local settings were frozen retain their old behavior.
        return _legacy_workbench_local_settings(settings)
    if object_store.read("local_asr_provider_settings", "default") is not None:
        pool = RESOURCE_POOL.get()
        selected = GetLocalAsrProviderSettings(object_store,
            model_root=pool.model_path('faster-whisper') if pool is not None else None).execute()
        settings = replace(
            settings,
            status=selected.status,
            enabled=selected.enabled,
            provider_name=selected.provider_name,
            command=selected.command,
            model_profile=selected.model_profile,
            model_name=selected.model_name,
            timeout_seconds=selected.timeout_seconds,
        )
    if not settings.enabled:
        raise TokenHubAsrError("local ASR is disabled; enable a local model in settings or enable Hy-ASR")
    if settings.status != "ready" or not _local_command_available(settings.command, settings.model_name):
        raise TokenHubAsrError("selected local ASR is unavailable")
    return settings


def _local_command_available(command: tuple[str, ...], model_name: str) -> bool:
    if not command:
        return False
    if command == (BUILTIN_FASTER_WHISPER_COMMAND,):
        configured = os.environ.get("CHRIPTMAS_APP_ROOT", "").strip()
        app_root = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[3]
        model_dir = app_root.resolve(strict=False) / "data" / "models" / "faster-whisper" / model_name
        pool = RESOURCE_POOL.get()
        if pool is not None:
            model_dir = pool.model_path('faster-whisper', model_name)
        return all(path.is_file() and path.stat().st_size > 0 for path in (
            model_dir / "model.bin", model_dir / "config.json",
        ))
    executable = Path(command[0])
    return executable.is_file() if executable.is_absolute() else shutil.which(command[0]) is not None


def _legacy_workbench_local_settings(settings):
    if settings.enabled:
        return settings
    return replace(
        settings,
        status="ready",
        enabled=True,
        provider_name="workbench-local-faster-whisper",
        command=(BUILTIN_FASTER_WHISPER_COMMAND,),
        timeout_seconds=max(settings.timeout_seconds, 3600.0),
    )


class TokenHubAudioAssetTranscriber:
    def __init__(
        self,
        runtime_root: Path,
        object_store: object,
        *,
        namespace_id: str,
        secret_store: SecretStore | None = None,
        http_caller: HttpCaller | None = None,
        validate_wire: Callable[[], None] | None = None,
        now: str = "2026-09-04T00:00:00Z",
    ) -> None:
        self._runtime_root = runtime_root
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._secrets = secret_store or build_secret_store(runtime_root)
        self._http = http_caller or _http_call
        self._validate_wire = validate_wire
        self._now = now

    def _check_wire_consent(self) -> None:
        if self._validate_wire is None:
            return
        try:
            self._validate_wire()
        except Exception:
            raise TokenHubAsrError("remote_processing_target_changed") from None

    def execute(
        self,
        *,
        audio_asset_id: str,
        execution_ref: str,
        binding_id: str,
    ) -> AudioAssetTranscriptionResult:
        binding = self._object_store.read("workbench_asr_bindings", binding_id)
        if not isinstance(binding, Mapping) or binding.get("provider") != TOKENHUB_ASR_PROVIDER_ID:
            raise TokenHubAsrError("Hy-ASR binding is unavailable")
        settings = GetCloudAsrProviderSettings(self._object_store).execute()
        if (
            not settings.enabled
            or settings.settings_revision != binding.get("settings_revision")
            or self._secrets.get_generation(TOKENHUB_ASR_SECRET_REF) != binding.get("secret_generation")
        ):
            raise TokenHubAsrError("Hy-ASR settings or credential changed after admission")
        asset = self._object_store.read("audio_asset_refs", audio_asset_id)
        if (
            not isinstance(asset, Mapping)
            or asset.get("status") != "available"
            or asset.get("media_type") != "audio/wav"
        ):
            raise TokenHubAsrError("audio asset is unavailable")
        source_id = _required(asset, "source_id")
        if source_id != binding.get("source_id"):
            raise TokenHubAsrError("Hy-ASR audio source drifted")
        audio_path = Path(_required(asset, "path")).resolve(strict=False)
        if not audio_path.is_file():
            raise TokenHubAsrError("audio asset file is unavailable")
        audio_size = audio_path.stat().st_size
        if not audio_size or audio_size > settings.max_audio_bytes:
            raise TokenHubAsrError(
                "Hy-ASR short-audio limit exceeded; use local faster-whisper for this file"
            )
        audio = audio_path.read_bytes()
        request_body = json.dumps(
            {
                "model": TOKENHUB_ASR_MODEL,
                "data": base64.b64encode(audio).decode("ascii"),
                "voice_encode_format": "wav",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request_body) > TOKENHUB_ASR_MAX_REQUEST_BYTES:
            raise TokenHubAsrError("Hy-ASR request payload budget exceeded")
        dispatch_id = "tokenhub-asr-" + hashlib.sha256(
            f"{execution_ref}\0{binding_id}\0{audio_asset_id}".encode("utf-8")
        ).hexdigest()
        output_id = _tokenhub_output_id(
            source_id=source_id,
            audio_asset_id=audio_asset_id,
            asset=asset,
            dispatch_id=dispatch_id,
        )
        existing = self._object_store.read("cloud_asr_dispatches", dispatch_id)
        if isinstance(existing, Mapping):
            if not self._is_dispatch_for_attempt(
                existing,
                dispatch_id=dispatch_id,
                audio_asset_id=audio_asset_id,
                source_id=source_id,
                binding_id=binding_id,
                execution_ref=execution_ref,
            ):
                raise TokenHubAsrError("previous Hy-ASR dispatch outcome is not safely replayable")
            if existing.get("status") == "completed":
                return self._replay(
                    existing,
                    audio_asset_id=audio_asset_id,
                    source_id=source_id,
                    binding_id=binding_id,
                    execution_ref=execution_ref,
                    output_id=output_id,
                )
            recovered_output_id = output_id
            recovered = self._object_store.read("media_processing_outputs", recovered_output_id)
            if self._is_completed_output_for_dispatch(
                recovered,
                dispatch_id=dispatch_id,
                output_id=recovered_output_id,
                audio_asset_id=audio_asset_id,
                source_id=source_id,
                binding_id=binding_id,
                execution_ref=execution_ref,
                accept_legacy_binding=True,
            ):
                promoted = dict(existing)
                promoted.update({"status": "completed", "output_id": recovered_output_id})
                self._object_store.write(
                    "cloud_asr_dispatches", dispatch_id, promoted, expected_revision=None,
                )
                return self._result(recovered, audio_asset_id=audio_asset_id)
            raise TokenHubAsrError("previous Hy-ASR dispatch outcome is not safely replayable")

        self._check_wire_consent()
        manifest = tokenhub_egress_manifest(self._runtime_root)
        if manifest.manifest_id != binding.get("egress_manifest_id"):
            raise TokenHubAsrError("Hy-ASR egress manifest changed after admission")
        policy = ProviderEgressPolicyStore(self._runtime_root)
        egress_lease = policy.authorize(
            manifest,
            purpose="video_processing",
            payload_categories=("audio_data", "audio_chunks", "provider_metadata"),
            payload_bytes=len(request_body),
        )
        broker = SecretEgressBroker(
            self._secrets,
            boundary_revision_reader=lambda _project: _BOUNDARY_REVISION,
        )
        secret_lease = broker.grant(
            project_id=str(binding["project_id"]),
            secret_ref=TOKENHUB_ASR_SECRET_REF,
            purpose="video_processing",
            allowed_hosts=("tokenhub.tencentmaas.com",),
            boundary_revision=_BOUNDARY_REVISION,
            ttl_seconds=300,
        )
        dispatch = {
            "schema_version": "1.0.0",
            "id": dispatch_id,
            "status": "sending",
            "provider": TOKENHUB_ASR_PROVIDER_ID,
            "provider_revision": _PROVIDER_REVISION,
            "model": TOKENHUB_ASR_MODEL,
            "source_id": source_id,
            "audio_asset_id": audio_asset_id,
            "binding_id": binding_id,
            "execution_ref": execution_ref,
            "request_bytes": len(request_body),
            "secret_generation": secret_lease.secret_revision,
            "egress_manifest_id": manifest.manifest_id,
            "created_at": self._now,
            "output_id": None,
        }
        self._object_store.write("cloud_asr_dispatches", dispatch_id, dispatch, expected_revision=None)
        try:
            headers = {
                "Content-Type": "application/json",
                **broker.inject_header(
                    secret_lease,
                    project_id=str(binding["project_id"]),
                    purpose="video_processing",
                    boundary_revision=_BOUNDARY_REVISION,
                    url=TOKENHUB_ASR_ENDPOINT,
                    header_name="Authorization",
                    prefix="Bearer ",
                ),
            }
            self._check_wire_consent()
            status, response = self._http(
                TOKENHUB_ASR_ENDPOINT, headers, request_body, settings.timeout_seconds,
            )
            if status < 200 or status >= 300:
                dispatch["status"] = "failed"
                self._object_store.write("cloud_asr_dispatches", dispatch_id, dispatch, expected_revision=None)
                egress_lease.finish("failed", error_code=f"http_{status}")
                raise TokenHubAsrError(f"Hy-ASR request failed with status {status}")
            result = _parse_response(response)
            output_ref = f"crp://{self._namespace_id}/media-processing-outputs/{output_id}.json"
            output = {
                "schema_version": "1.0.0",
                "id": output_id,
                "job_id": f"media-job-transcript-tokenhub-{audio_asset_id}",
                "source_id": source_id,
                "source_type": "audio" if asset.get("source_type") == "audio" else "video",
                "output_kind": "transcript",
                "status": "completed",
                "provider": TOKENHUB_ASR_PROVIDER_NAME,
                "language": result["language"],
                "segment_count": len(result["segments"]),
                "char_count": len(result["text"]),
                "byte_count": len(result["text"].encode("utf-8")),
                "preview": _preview(result["text"]),
                "text": result["text"],
                "segments": result["segments"],
                "metadata": {
                    "local_processing": False,
                    "remote_processing": True,
                    "audio_asset_id": audio_asset_id,
                    "audio_asset_ref": _required(asset, "audio_asset_ref"),
                    "audio_path_stored_in_output": False,
                    "model_name": TOKENHUB_ASR_MODEL,
                    "duration_ms": result["duration_ms"],
                    "usage_total_token": result["usage_total_token"],
                    "dispatch_ref": f"facts:cloud-asr-dispatch/{dispatch_id}",
                    "binding_id": binding_id,
                    "execution_ref": execution_ref,
                    "is_chunk": asset.get("is_chunk") is True,
                    "parent_audio_asset_id": asset.get("parent_audio_asset_id"),
                    "chunk_index": asset.get("chunk_index"),
                    "chunk_start_seconds": asset.get("chunk_start_seconds"),
                    "chunk_end_seconds": asset.get("chunk_end_seconds"),
                    "chunk_overlap_before_seconds": asset.get("chunk_overlap_before_seconds"),
                    "memory_publication": "not_started",
                },
                "memory_publication": "not_started",
                "created_at": self._now,
                "ref": output_ref,
            }
            prior = self._object_store.read("media_processing_outputs", output_id)
            if prior is None:
                self._object_store.write("media_processing_outputs", output_id, output, expected_revision=None)
            elif prior != output:
                raise TokenHubAsrError("existing Hy-ASR transcript drifted")
            dispatch.update({"status": "completed", "output_id": output_id})
            self._object_store.write("cloud_asr_dispatches", dispatch_id, dispatch, expected_revision=None)
            egress_lease.finish("completed")
            return self._result(output, audio_asset_id=audio_asset_id)
        except TokenHubAsrError as error:
            if dispatch.get("status") == "sending":
                dispatch["status"] = "failed"
                self._object_store.write(
                    "cloud_asr_dispatches", dispatch_id, dispatch, expected_revision=None,
                )
                error_code = (
                    "consent_or_target_changed"
                    if str(error) == "remote_processing_target_changed"
                    else "provider_response_invalid"
                )
                egress_lease.finish("failed", error_code=error_code)
            raise
        except Exception as error:
            dispatch["status"] = "unknown"
            self._object_store.write("cloud_asr_dispatches", dispatch_id, dispatch, expected_revision=None)
            egress_lease.finish("unknown", error_code="provider_outcome_unknown")
            raise TokenHubAsrError("Hy-ASR provider outcome is unknown; automatic replay was stopped") from error
        finally:
            broker.revoke(secret_lease.lease_id)

    def _replay(
        self,
        dispatch: Mapping[str, object],
        *,
        audio_asset_id: str,
        source_id: str,
        binding_id: str,
        execution_ref: str,
        output_id: str,
    ):
        if (
            not self._is_dispatch_for_attempt(
                dispatch,
                dispatch_id=str(dispatch.get("id") or ""),
                audio_asset_id=audio_asset_id,
                source_id=source_id,
                binding_id=binding_id,
                execution_ref=execution_ref,
            )
            or dispatch.get("output_id") != output_id
        ):
            raise TokenHubAsrError("completed Hy-ASR dispatch receipt is unavailable")
        output = self._object_store.read("media_processing_outputs", output_id)
        if not self._is_completed_output_for_dispatch(
            output,
            dispatch_id=str(dispatch.get("id") or ""),
            output_id=output_id,
            audio_asset_id=audio_asset_id,
            source_id=source_id,
            binding_id=binding_id,
            execution_ref=execution_ref,
            accept_legacy_binding=True,
        ):
            raise TokenHubAsrError("completed Hy-ASR dispatch output is unavailable")
        return self._result(output, audio_asset_id=audio_asset_id)

    @staticmethod
    def _is_dispatch_for_attempt(
        dispatch: Mapping[str, object],
        *,
        dispatch_id: str,
        audio_asset_id: str,
        source_id: str,
        binding_id: str,
        execution_ref: str,
    ) -> bool:
        return (
            dispatch.get("id") == dispatch_id
            and dispatch.get("provider") == TOKENHUB_ASR_PROVIDER_ID
            and dispatch.get("audio_asset_id") == audio_asset_id
            and dispatch.get("source_id") == source_id
            and dispatch.get("binding_id") == binding_id
            and dispatch.get("execution_ref") == execution_ref
        )

    @staticmethod
    def _is_completed_output_for_dispatch(
        output: object,
        *,
        dispatch_id: str,
        output_id: str,
        audio_asset_id: str,
        source_id: str,
        binding_id: str,
        execution_ref: str,
        accept_legacy_binding: bool,
    ) -> bool:
        if not isinstance(output, Mapping):
            return False
        metadata = output.get("metadata")
        if (
            not isinstance(metadata, Mapping)
            or output.get("id") != output_id
            or output.get("status") != "completed"
            or output.get("source_id") != source_id
            or output.get("provider") != TOKENHUB_ASR_PROVIDER_NAME
            or metadata.get("audio_asset_id") != audio_asset_id
            or metadata.get("execution_ref") != execution_ref
            or metadata.get("dispatch_ref") != f"facts:cloud-asr-dispatch/{dispatch_id}"
        ):
            return False
        output_binding_id = metadata.get("binding_id")
        if output_binding_id != binding_id and not (accept_legacy_binding and output_binding_id is None):
            return False
        try:
            _required(output, "text")
        except TokenHubAsrError:
            return False
        return True

    @staticmethod
    def _result(output: Mapping[str, object], *, audio_asset_id: str) -> AudioAssetTranscriptionResult:
        text = _required(output, "text")
        return AudioAssetTranscriptionResult(
            status="completed",
            job_id=str(output["job_id"]),
            output_id=str(output["id"]),
            source_id=str(output["source_id"]),
            audio_asset_id=audio_asset_id,
            provider=str(output["provider"]),
            language=str(output.get("language") or "") or None,
            segment_count=int(output.get("segment_count", 0)),
            char_count=len(text),
            output_preview=str(output.get("preview") or ""),
            starts_summary=False,
            creates_memory_candidate=False,
            publishes_memory=False,
            error=None,
        )


class TokenHubChunkedAudioAssetTranscriber:
    """Route one extracted WAV through bounded, overlapping TokenHub requests."""

    def __init__(
        self,
        runtime_root: Path,
        object_store: object,
        *,
        namespace_id: str,
        secret_store: SecretStore | None = None,
        http_caller: HttpCaller | None = None,
        chunk_splitter: Callable[[str, str, float, float], str] | None = None,
        checkpoint: Callable[[], object] | None = None,
        validate_wire: Callable[[], None] | None = None,
        now: str = "2026-09-05T00:00:00Z",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._split = chunk_splitter or split_local_audio_chunk
        self._checkpoint = checkpoint or (lambda: None)
        self._single = TokenHubAudioAssetTranscriber(
            runtime_root,
            object_store,
            namespace_id=namespace_id,
            secret_store=secret_store,
            http_caller=http_caller,
            validate_wire=validate_wire,
            now=now,
        )
        self._now = now

    def execute(
        self,
        *,
        audio_asset_id: str,
        execution_ref: str,
        binding_id: str,
    ) -> AudioAssetTranscriptionResult:
        asset = self._object_store.read("audio_asset_refs", audio_asset_id)
        if not isinstance(asset, Mapping) or asset.get("status") != "available":
            raise TokenHubAsrError("audio asset is unavailable")
        source_id = _required(asset, "source_id")
        audio_path = Path(_required(asset, "path")).resolve(strict=False)
        if not audio_path.is_file():
            raise TokenHubAsrError("audio asset file is unavailable")
        settings = GetCloudAsrProviderSettings(self._object_store).execute()
        if 0 < audio_path.stat().st_size <= settings.max_audio_bytes:
            return self._single.execute(
                audio_asset_id=audio_asset_id,
                execution_ref=execution_ref,
                binding_id=binding_id,
            )
        raw_duration = asset.get("duration_seconds")
        try:
            if isinstance(raw_duration, bool):
                raise TypeError("boolean duration is invalid")
            total_duration = float(raw_duration or 0.0)
        except (TypeError, ValueError) as error:
            raise TokenHubAsrError("long audio duration is unavailable") from error
        parent_evidence = self._parent_audio_evidence(
            asset=asset,
            audio_asset_id=audio_asset_id,
            audio_path=audio_path,
            total_duration=total_duration,
        )
        plans = self._freeze_chunk_plan(
            audio_asset_id=audio_asset_id,
            source_id=source_id,
            binding_id=binding_id,
            execution_ref=execution_ref,
            total_duration=total_duration,
            parent_evidence=parent_evidence,
        )
        chunk_outputs: list[tuple[CloudAsrChunkPlan, Mapping[str, object]]] = []
        output_ids: list[str] = []
        dispatch_refs: list[str] = []
        for plan in plans:
            self._checkpoint()
            chunk_id = f"{audio_asset_id}-tokenhub-chunk-{plan.index}"
            chunk_path = audio_path.with_name(
                f"{audio_path.stem}.tokenhub-{plan.index:04d}.wav"
            ).resolve(strict=False)
            chunk_ref = f"crp-ref://{self._namespace_id}/assets/{chunk_id}"
            chunk_asset = {
                "schema_version": "1.0.0",
                "id": chunk_id,
                "source_id": source_id,
                "source_type": "audio" if asset.get("source_type") == "audio" else "video",
                "audio_asset_ref": chunk_ref,
                "path": str(chunk_path),
                "media_type": "audio/wav",
                "status": "available",
                "sample_rate_hz": 16000,
                "channels": 1,
                "duration_seconds": round(plan.end_seconds - plan.start_seconds, 6),
                "parent_audio_asset_id": audio_asset_id,
                "chunk_index": plan.index,
                "chunk_start_seconds": plan.start_seconds,
                "chunk_end_seconds": plan.end_seconds,
                "chunk_overlap_before_seconds": plan.overlap_before_seconds,
                "is_chunk": True,
                "chunk_strategy": "tokenhub-overlap-v1",
            }
            prior_asset = self._object_store.read("audio_asset_refs", chunk_id)
            if prior_asset is not None and prior_asset != chunk_asset:
                raise TokenHubAsrError("Hy-ASR audio chunk evidence drifted")
            needs_materialization = (
                not chunk_path.is_file()
                or chunk_path.stat().st_size <= 0
                or chunk_path.stat().st_size > settings.max_audio_bytes
            )
            if needs_materialization:
                produced_path = Path(self._split(
                    str(audio_path),
                    str(chunk_path),
                    plan.start_seconds,
                    plan.end_seconds,
                )).resolve(strict=False)
                if produced_path != chunk_path:
                    raise TokenHubAsrError("Hy-ASR audio chunk path drifted")
            if prior_asset is None:
                self._object_store.write(
                    "audio_asset_refs", chunk_id, chunk_asset, expected_revision=None,
                )
            if (
                not chunk_path.is_file()
                or chunk_path.stat().st_size <= 0
                or chunk_path.stat().st_size > settings.max_audio_bytes
            ):
                raise TokenHubAsrError("Hy-ASR audio chunk exceeds the per-request limit")
            transcript = self._single.execute(
                audio_asset_id=chunk_id,
                execution_ref=execution_ref,
                binding_id=binding_id,
            )
            output = self._object_store.read("media_processing_outputs", transcript.output_id)
            if not isinstance(output, Mapping):
                raise TokenHubAsrError("Hy-ASR chunk transcript is unavailable")
            chunk_outputs.append((plan, output))
            output_ids.append(transcript.output_id)
            metadata = output.get("metadata")
            if isinstance(metadata, Mapping) and isinstance(metadata.get("dispatch_ref"), str):
                dispatch_refs.append(str(metadata["dispatch_ref"]))
        merged = merge_cloud_asr_chunk_outputs(chunk_outputs)
        output_id = f"media-output-transcript-tokenhub-{source_id}"
        output_ref = f"crp://{self._namespace_id}/media-processing-outputs/{output_id}.json"
        text = str(merged["text"])
        segments = list(merged["segments"])
        output = {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": f"media-job-transcript-tokenhub-{audio_asset_id}",
            "source_id": source_id,
            "source_type": "audio" if asset.get("source_type") == "audio" else "video",
            "output_kind": "transcript",
            "status": "completed",
            "provider": TOKENHUB_ASR_PROVIDER_NAME,
            "language": merged["language"],
            "segment_count": len(segments),
            "char_count": len(text),
            "byte_count": len(text.encode("utf-8")),
            "preview": _preview(text),
            "text": text,
            "segments": segments,
            "metadata": {
                "local_processing": False,
                "remote_processing": True,
                "audio_asset_id": audio_asset_id,
                "audio_asset_ref": _required(asset, "audio_asset_ref"),
                "audio_path_stored_in_output": False,
                "model_name": TOKENHUB_ASR_MODEL,
                "duration_ms": merged["duration_ms"],
                "usage_total_token": merged["usage_total_token"],
                "execution_ref": execution_ref,
                "chunked": True,
                "chunk_count": len(plans),
                "chunk_duration_seconds": CLOUD_ASR_CHUNK_DURATION_SECONDS,
                "chunk_overlap_seconds": CLOUD_ASR_CHUNK_OVERLAP_SECONDS,
                "chunk_output_ids": output_ids,
                "dispatch_refs": dispatch_refs,
                "memory_publication": "not_started",
            },
            "memory_publication": "not_started",
            "created_at": self._now,
            "ref": output_ref,
        }
        prior_output = self._object_store.read("media_processing_outputs", output_id)
        if prior_output is None:
            self._object_store.write(
                "media_processing_outputs", output_id, output, expected_revision=None,
            )
        elif prior_output != output:
            raise TokenHubAsrError("existing chunked Hy-ASR transcript drifted")
        self._checkpoint()
        return TokenHubAudioAssetTranscriber._result(output, audio_asset_id=audio_asset_id)

    def _freeze_chunk_plan(
        self,
        *,
        audio_asset_id: str,
        source_id: str,
        binding_id: str,
        execution_ref: str,
        total_duration: float,
        parent_evidence: Mapping[str, object],
    ) -> tuple[CloudAsrChunkPlan, ...]:
        legacy_plan_id = f"tokenhub-asr-chunk-plan-{binding_id}-{audio_asset_id}"
        # Repository object IDs are limited to 128 characters. Keep existing
        # short IDs readable while giving workspace run IDs a stable short key.
        plan_id = (
            legacy_plan_id if len(legacy_plan_id) <= 128 else
            "tokenhub-asr-chunk-plan-" + hashlib.sha256(
                f"{binding_id}\0{audio_asset_id}".encode("utf-8")
            ).hexdigest()
        )
        record = {
            "schema_version": "1.0.0",
            "id": plan_id,
            "provider": TOKENHUB_ASR_PROVIDER_ID,
            "strategy": "tokenhub-overlap-v1",
            "audio_asset_id": audio_asset_id,
            "source_id": source_id,
            "binding_id": binding_id,
            "execution_ref": execution_ref,
            **parent_evidence,
            "plans": [],
            "created_at": self._now,
        }
        existing = self._object_store.read("cloud_asr_chunk_plans", plan_id)
        if existing is None:
            planned = plan_cloud_asr_chunks(total_duration)
            if not planned:
                raise TokenHubAsrError("long audio duration is unavailable")
            record["plans"] = [
                {
                    "index": plan.index,
                    "start_seconds": plan.start_seconds,
                    "end_seconds": plan.end_seconds,
                    "overlap_before_seconds": plan.overlap_before_seconds,
                }
                for plan in planned
            ]
            self._object_store.write("cloud_asr_chunk_plans", plan_id, record, expected_revision=None)
            return planned
        if not isinstance(existing, Mapping) or any(
            existing.get(key) != record[key]
            for key in (
                "id", "provider", "strategy", "audio_asset_id", "source_id", "binding_id", "execution_ref",
                "parent_asset_revision", "parent_duration_seconds", "parent_audio_path",
                "parent_file_size_bytes", "parent_file_mtime_ns",
            )
        ):
            raise TokenHubAsrError("Hy-ASR chunk plan binding drifted")
        raw_plans = existing.get("plans")
        if not isinstance(raw_plans, list) or not raw_plans:
            raise TokenHubAsrError("Hy-ASR chunk plan is unavailable")
        try:
            plans = tuple(_cloud_asr_chunk_plan_from_record(item) for item in raw_plans)
        except (KeyError, TypeError, ValueError) as error:
            raise TokenHubAsrError("Hy-ASR chunk plan is invalid") from error
        if plans != plan_cloud_asr_chunks(total_duration):
            raise TokenHubAsrError("Hy-ASR chunk plan is invalid")
        return plans

    def _parent_audio_evidence(
        self,
        *,
        asset: Mapping[str, object],
        audio_asset_id: str,
        audio_path: Path,
        total_duration: float,
    ) -> dict[str, object]:
        if not math.isfinite(total_duration) or total_duration <= 0:
            raise TokenHubAsrError("long audio duration is unavailable")
        stat = audio_path.stat()
        return {
            "parent_asset_revision": self._object_store.revision("audio_asset_refs", audio_asset_id),
            "parent_duration_seconds": total_duration,
            "parent_audio_path": str(audio_path),
            "parent_file_size_bytes": stat.st_size,
            "parent_file_mtime_ns": stat.st_mtime_ns,
        }


def _parse_response(response: object) -> dict[str, object]:
    if not isinstance(response, Mapping) or response.get("status") != "completed" or not isinstance(response.get("output"), Mapping):
        raise TokenHubAsrError("Hy-ASR response is incomplete")
    output = response["output"]
    text = _required(output, "text")
    raw_sentences = output.get("sentences")
    if not isinstance(raw_sentences, list):
        raise TokenHubAsrError("Hy-ASR sentences are unavailable")
    segments = []
    for sentence in raw_sentences:
        if not isinstance(sentence, Mapping):
            raise TokenHubAsrError("Hy-ASR sentence is invalid")
        begin, end = sentence.get("begin_ms"), sentence.get("end_ms")
        if (
            isinstance(begin, bool)
            or isinstance(end, bool)
            or not isinstance(begin, int)
            or not isinstance(end, int)
            or begin < 0
            or end < begin
        ):
            raise TokenHubAsrError("Hy-ASR sentence timestamp is invalid")
        segments.append({
            "start_seconds": begin / 1000,
            "end_seconds": end / 1000,
            "text": _required(sentence, "text"),
        })
    usage = response.get("usage")
    duration = output.get("duration_ms")
    return {
        "text": text,
        "language": str(output.get("source") or "") or None,
        "duration_ms": duration if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0 else None,
        "segments": segments,
        "usage_total_token": usage.get("total_token") if isinstance(usage, Mapping) else None,
    }


def _http_call(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> tuple[int, object]:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - endpoint is fixed above.
            payload = json.loads(response.read().decode("utf-8"))
            return int(response.status), payload
    except HTTPError as error:
        return int(error.code), {}
    except (URLError, OSError, TimeoutError) as error:
        raise OSError("Hy-ASR network request did not complete") from error


def _tokenhub_output_id(
    *,
    source_id: str,
    audio_asset_id: str,
    asset: Mapping[str, object],
    dispatch_id: str,
) -> str:
    if asset.get("is_chunk") is True:
        return f"media-output-transcript-tokenhub-{audio_asset_id}-{dispatch_id[-12:]}"
    return f"media-output-transcript-tokenhub-{source_id}"


def _cloud_asr_chunk_plan_from_record(value: object) -> CloudAsrChunkPlan:
    if not isinstance(value, Mapping):
        raise ValueError("chunk plan is invalid")
    index = value.get("index")
    start = value.get("start_seconds")
    end = value.get("end_seconds")
    overlap = value.get("overlap_before_seconds")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in (start, end, overlap))
    ):
        raise ValueError("chunk plan is invalid")
    start_float, end_float, overlap_float = float(start), float(end), float(overlap)
    if not all(math.isfinite(item) for item in (start_float, end_float, overlap_float)):
        raise ValueError("chunk plan is invalid")
    return CloudAsrChunkPlan(index, start_float, end_float, overlap_float)


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise TokenHubAsrError(f"{key} is required")
    return item


def _preview(value: str) -> str:
    return " ".join(value.split())[:600]
