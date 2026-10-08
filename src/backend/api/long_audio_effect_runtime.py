from __future__ import annotations

from collections.abc import Callable, Mapping

from backend.api.audio_auto_effect_runtime import AudioAutoEffectRuntime
from core.effect_log import EffectClass, EffectRunner, EffectWorkflowHandler
from core.product_core.long_audio_chunker import LongAudioChunkedWorkflowResult
from core.product_core.long_audio_decider import (
    LongAudioChunkFact,
    decide_long_audio_result,
    plan_long_audio_chunks,
)
from core.product_core.ports import ObjectStorePort


class LongAudioEffectRuntime:
    """Execute deterministic chunk Effects without a private retry state machine."""

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        namespace_id: str,
        effect_runner: EffectRunner,
        audio_runtime: AudioAutoEffectRuntime,
        split_runner: Callable[[str, str, float, float], str],
        chunk_duration_seconds: float = 900.0,
    ) -> None:
        self._store = store
        self._namespace_id = namespace_id
        self._effects = EffectWorkflowHandler(
            effect_runner, store, namespace_id=namespace_id,
        )
        self._audio_runtime = audio_runtime
        self._split_runner = split_runner
        self._chunk_duration = chunk_duration_seconds

    def execute(
        self, *, source_id: str, project_id: str | None, audio_asset_id: str,
    ) -> LongAudioChunkedWorkflowResult:
        asset = self._store.read("audio_asset_refs", audio_asset_id)
        if not isinstance(asset, Mapping) or asset.get("status") != "available":
            return _blocked(source_id, project_id, audio_asset_id, "audio asset is unavailable")
        duration = float(asset.get("duration_seconds") or 0.0)
        plans = plan_long_audio_chunks(
            duration, chunk_duration_seconds=self._chunk_duration,
        )
        source_path = str(asset.get("path") or "")
        facts: list[LongAudioChunkFact] = []
        for plan in plans:
            chunk_asset_id = f"{audio_asset_id}-chunk-{plan.index}"
            try:
                chunk_path = self._split(source_id, audio_asset_id, source_path, plan)
                self._write_chunk_asset(
                    source_id, audio_asset_id, chunk_asset_id, chunk_path, asset, plan,
                )
                transcript = self._audio_runtime.transcribe(
                    source_id, audio_asset_id=chunk_asset_id,
                )
                output_id = (
                    transcript.output_id if transcript.status == "completed" else None
                )
                error = transcript.error
            except Exception as exc:  # Core Reaper owns retry and UNKNOWN.
                output_id = None
                error = str(exc) or exc.__class__.__name__
            facts.append(LongAudioChunkFact(plan, output_id, error))
        merged_id, preview = self._merge(source_id, tuple(facts))
        result = decide_long_audio_result(
            source_id=source_id,
            project_id=project_id,
            audio_asset_id=audio_asset_id,
            total_duration_seconds=duration,
            facts=tuple(facts),
            merged_output_id=merged_id,
            merged_preview=preview,
        )
        self._write_projection(result)
        return result

    def _split(self, source_id, audio_asset_id, source_path, plan) -> str:
        output_path = f"{source_path}.chunk-{plan.index}.wav"
        return self._effects.execute(
            operation_id=f"long-audio:{source_id}:split:{plan.index}",
            session_id=f"long-audio:{source_id}",
            root_id=f"long-audio:{source_id}",
            step_key=f"split:{plan.index}",
            kind="long_audio_split",
            intent_ref=(
                f"crp://{self._namespace_id}/workflow-intents/long-audio/"
                f"{source_id}/split/{plan.index}"
            ),
            gate_decision_id="workbench-auto-intake:v1",
            rev_set={"workflow_revision": "2", "handler_revision": "ffmpeg-split-v1"},
            payload={
                "source_id": source_id,
                "audio_asset_id": audio_asset_id,
                "chunk_index": plan.index,
                "start_seconds": plan.start_seconds,
                "end_seconds": plan.end_seconds,
            },
            effect_class=EffectClass.IDEMPOTENT,
            invoke=lambda: self._split_runner(
                source_path, output_path, plan.start_seconds, plan.end_seconds,
            ),
            encode=lambda value: {"path": value},
            decode=lambda value: str(value["path"]),
        )

    def _write_chunk_asset(self, source_id, parent_id, chunk_id, path, parent, plan):
        self._store.write("audio_asset_refs", chunk_id, {
            "schema_version": "1.0.0", "id": chunk_id, "source_id": source_id,
            "parent_audio_asset_id": parent_id, "chunk_index": plan.index,
            "chunk_start_seconds": plan.start_seconds,
            "chunk_end_seconds": plan.end_seconds,
            "duration_seconds": plan.end_seconds - plan.start_seconds,
            "path": path, "media_type": parent.get("media_type", "audio/wav"),
            "sample_rate_hz": parent.get("sample_rate_hz", 16000),
            "channels": parent.get("channels", 1), "status": "available",
            "is_chunk": True, "projection_source": "effect_tree",
        }, expected_revision=None)

    def _merge(self, source_id: str, facts: tuple[LongAudioChunkFact, ...]):
        parts: list[str] = []
        for fact in facts:
            if fact.transcript_output_id is None:
                continue
            output = self._store.read("media_processing_outputs", fact.transcript_output_id)
            text = output.get("text") if isinstance(output, Mapping) else None
            if isinstance(text, str) and text:
                parts.append(f"[{fact.plan.index}] {text}")
        if not parts:
            return None, None
        merged = "\n\n".join(parts)
        output_id = f"media-output-transcript-merged-{source_id}"
        self._store.write("media_processing_outputs", output_id, {
            "schema_version": "1.0.0", "id": output_id,
            "job_id": f"media-job-merge-{source_id}", "source_id": source_id,
            "source_type": "audio", "output_kind": "transcript",
            "status": "completed", "provider": "local-whisper-chunked",
            "language": "zh", "segment_count": 0, "char_count": len(merged),
            "preview": merged[:200], "text": merged, "segments": [],
            "metadata": {"is_merged_transcript": True, "projection_source": "effect_tree"},
            "memory_publication": "not_started",
            "ref": f"crp://{self._namespace_id}/media-processing-outputs/{output_id}.json",
        }, expected_revision=None)
        return output_id, merged[:200]

    def _write_projection(self, result: LongAudioChunkedWorkflowResult) -> None:
        for chunk in result.chunks:
            chunk_id = f"audio-chunk-{result.source_id}-{chunk.chunk_index}"
            self._store.write("audio_chunks", chunk_id, {
                "schema_version": "2.0.0", "id": chunk_id,
                "source_id": result.source_id,
                "parent_audio_asset_id": result.audio_asset_id,
                "chunk_audio_asset_id": f"{result.audio_asset_id}-chunk-{chunk.chunk_index}",
                "chunk_index": chunk.chunk_index,
                "start_seconds": chunk.start_seconds, "end_seconds": chunk.end_seconds,
                "duration_seconds": chunk.duration_seconds, "status": chunk.status,
                "transcript_output_id": chunk.transcript_output_id, "error": chunk.error,
                "retry_count": chunk.retry_count, "projection_source": "effect_tree",
            }, expected_revision=None)


def _blocked(source_id, project_id, audio_asset_id, reason):
    return LongAudioChunkedWorkflowResult(
        status="blocked", workflow_id=f"long-audio-chunked-workflow-{source_id}",
        source_id=source_id, project_id=project_id or "default",
        audio_asset_id=audio_asset_id, is_long_audio=False,
        total_duration_seconds=0.0, completed_duration_seconds=0.0,
        chunk_count=0, completed_chunk_count=0, failed_chunk_count=0,
        progress=0.0, chunks=(), merged_transcript_output_id=None,
        merged_transcript_preview=None, memory_publication="not_started",
        blocked_operations=("long_audio_chunked_transcription",),
        readiness_reason=reason, next_step="save_or_authorize_audio_asset",
        error=reason, is_partial_result=False,
    )
