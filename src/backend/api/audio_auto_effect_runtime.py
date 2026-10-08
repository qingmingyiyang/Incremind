from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.effect_log import (
    EffectClass,
    EffectHandlerAbandoned,
    EffectRunner,
    EffectWorkflowHandler,
)
from core.product_core.audio_asset_transcriber import (
    AudioAssetTranscriptionResult,
    GetAudioAssetTranscriberSettings,
    TranscribeGeneratedAudioAsset,
    serialize_audio_asset_transcription_result,
)
from core.product_core.audio_auto_decider import decide_audio_auto_workflow
from core.product_core.audio_auto_workflow import (
    AudioAutoWorkflowResult,
    serialize_audio_auto_workflow_result,
    serialize_audio_auto_workflow_step,
)
from core.product_core.ports import ObjectStorePort


class AudioAutoEffectRuntime:
    """Execute Audio Handler facts and materialize a rebuildable read projection."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str,
        effect_runner: EffectRunner,
        gate_decision_id: str,
    ) -> None:
        self._store = object_store
        self._namespace_id = namespace_id
        self._gate_decision_id = gate_decision_id
        self._effect_runner = effect_runner
        self._transcriber: TranscribeGeneratedAudioAsset | None = None
        self._effects = EffectWorkflowHandler(
            effect_runner, object_store, namespace_id=namespace_id,
        )

    def transcribe(
        self, source_id: str, **kwargs: object,
    ) -> AudioAssetTranscriptionResult:
        audio_asset_id = str(kwargs.get("audio_asset_id") or "missing")
        operation_id = f"audio-auto:{source_id}:{audio_asset_id}:transcribe"

        def invoke() -> AudioAssetTranscriptionResult:
            transcriber = self._transcriber or TranscribeGeneratedAudioAsset(
                self._store,
                namespace_id=self._namespace_id,
                cancellation_requested=lambda: (
                    self._effect_runner.log.cancellation_requested(operation_id)
                ),
            )
            result = transcriber.execute(**kwargs)
            if result.status == "cancelled":
                raise EffectHandlerAbandoned("workflow.user_cancelled")
            return result

        return self._effects.execute(
            operation_id=operation_id,
            session_id=f"audio-auto:{source_id}",
            root_id=f"audio-auto:{source_id}",
            step_key=f"transcribe_audio:{audio_asset_id}",
            kind="audio_auto_transcribe",
            intent_ref=(
                f"crp://{self._namespace_id}/workflow-intents/"
                f"audio-auto/{source_id}/transcribe"
            ),
            gate_decision_id=self._gate_decision_id,
            rev_set={
                "workflow_revision": "2",
                "handler_revision": "audio-transcriber-v1",
            },
            payload={
                "audio_asset_id": audio_asset_id,
                "source_id": source_id,
            },
            effect_class=EffectClass.IDEMPOTENT,
            invoke=invoke,
            encode=serialize_audio_asset_transcription_result,
            decode=lambda value: AudioAssetTranscriptionResult(**dict(value)),
        )

    def execute(
        self,
        *,
        source_id: str,
        project_id: str | None,
        audio_asset_id: str | None,
    ) -> AudioAutoWorkflowResult:
        readiness = _audio_readiness(self._store, audio_asset_id)
        transcript = None
        failure = None
        if readiness["next_step"] == "ready_to_transcribe" and audio_asset_id is not None:
            try:
                transcript = self.transcribe(source_id, audio_asset_id=audio_asset_id)
            except Exception as error:  # Effect remains recoverable by Core Reaper.
                failure = str(error) or error.__class__.__name__
        result = decide_audio_auto_workflow(
            source_id=source_id,
            project_id=project_id,
            audio_asset_id=audio_asset_id,
            readiness=readiness,
            transcript=transcript,
            failure=failure,
        )
        _write_audio_projection(self._store, self._namespace_id, result)
        return result


def _audio_readiness(
    object_store: ObjectStorePort, audio_asset_id: str | None,
) -> dict[str, object]:
    settings = GetAudioAssetTranscriberSettings(object_store).execute()
    common = {
        "transcriber_status": settings.status,
        "transcriber_model_profile": settings.model_profile,
        "transcriber_model_name": settings.model_name,
    }
    if audio_asset_id is None:
        return common | {
            "audio_asset_status": "missing",
            "readiness_reason": "audio asset id is missing",
            "next_step": "save_or_authorize_audio_asset",
        }
    asset = object_store.read("audio_asset_refs", audio_asset_id)
    if asset is None:
        return common | {
            "audio_asset_status": "missing",
            "readiness_reason": "audio asset reference is missing",
            "next_step": "save_or_authorize_audio_asset",
        }
    status = str(asset.get("status") or "unknown")
    path = asset.get("path")
    if status != "available" or not isinstance(path, str) or not Path(path).is_file():
        return common | {
            "audio_asset_status": status if status != "available" else "path_unavailable",
            "readiness_reason": str(
                asset.get("availability_reason") or "audio asset is not available"
            ),
            "next_step": "save_or_authorize_audio_asset",
        }
    if settings.enabled is not True:
        return common | {
            "audio_asset_status": "available",
            "readiness_reason": "audio asset transcriber is disabled",
            "next_step": "enable_local_asr_provider",
        }
    return common | {
        "audio_asset_status": "available",
        "readiness_reason": None,
        "next_step": "ready_to_transcribe",
    }


def _write_audio_projection(
    object_store: ObjectStorePort,
    namespace_id: str,
    result: AudioAutoWorkflowResult,
) -> None:
    payload = serialize_audio_auto_workflow_result(result)
    object_store.write(
        "audio_auto_workflows",
        result.workflow_id,
        payload | {"projection_source": "effect_tree"},
        expected_revision=None,
    )
    source = object_store.read("sources", result.source_id)
    if not isinstance(source, Mapping):
        return
    metadata = dict(
        source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
    )
    metadata["audio_auto_workflow"] = {
        **payload,
        "workflow_ref": (
            f"crp://{namespace_id}/audio-auto-workflows/{result.workflow_id}.json"
        ),
        "steps": [serialize_audio_auto_workflow_step(step) for step in result.steps],
        "projection_source": "effect_tree",
    }
    object_store.write(
        "sources",
        result.source_id,
        dict(source) | {"metadata": metadata},
        expected_revision=None,
    )
