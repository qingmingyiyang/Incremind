from __future__ import annotations

from collections.abc import Mapping

from core.effect_log import EffectClass, EffectRunner, EffectWorkflowHandler
from core.product_core.audio_asset_transcriber import (
    AudioAssetTranscriptionResult,
    TranscribeGeneratedAudioAsset,
    serialize_audio_asset_transcription_result,
)
from core.product_core.source_output_memory_candidate import (
    CreateMemoryCandidateFromSourceOutput,
    SourceOutputMemoryCandidateResult,
    serialize_source_output_memory_candidate,
)
from core.product_core.transcript_summary_adapter import (
    GetTranscriptSummarySettings,
    SummarizeTranscriptOutput,
    TranscriptSummaryResult,
    serialize_transcript_summary_result,
)
from core.product_core.video_audio_extractor import (
    ExtractAudioTrackFromAuthorizedVideoSource,
    VideoAudioExtractionResult,
    serialize_video_audio_extraction_result,
)
from core.product_core.video_auto_decider import (
    VideoAutoFacts,
    decide_video_auto_workflow,
)
from core.product_core.video_auto_workflow import (
    VideoAutoWorkflowResult,
    serialize_video_auto_workflow_result,
    serialize_video_auto_workflow_step,
)
from core.product_core.ports import ObjectStorePort


class VideoAutoEffectRuntime:
    """Drive the pure Video decider; Core Effect owns every Handler transition."""

    def __init__(self, store: ObjectStorePort, *, namespace_id: str, effect_runner: EffectRunner, gate_decision_id: str) -> None:
        self._store = store
        self._namespace_id = namespace_id
        self._gate_decision_id = gate_decision_id
        self._effects = EffectWorkflowHandler(effect_runner, store, namespace_id=namespace_id)
        self._extractor = ExtractAudioTrackFromAuthorizedVideoSource(store, namespace_id=namespace_id)
        self._transcriber = TranscribeGeneratedAudioAsset(store, namespace_id=namespace_id)
        self._summarizer = SummarizeTranscriptOutput(store, namespace_id=namespace_id)
        self._candidate_creator = CreateMemoryCandidateFromSourceOutput(store, namespace_id=namespace_id)

    def execute(self, *, source_id: str, project_id: str | None) -> VideoAutoWorkflowResult:
        facts = VideoAutoFacts(
            source_id=source_id,
            project_id=project_id,
            summary_readiness=_summary_readiness(self._store),
        )
        while True:
            decision = decide_video_auto_workflow(facts)
            if decision.result is not None:
                _write_video_projection(self._store, self._namespace_id, decision.result)
                return decision.result
            step = str(decision.next_step)
            try:
                outcome = self._execute_step(step, facts)
            except Exception as error:  # Core Reaper owns retry/recovery.
                facts = _replace_facts(
                    facts, failed_step=step,
                    failure=str(error) or error.__class__.__name__,
                )
            else:
                facts = _replace_facts(facts, **{_fact_field(step): outcome})

    def extract_audio(self, source_id: str) -> VideoAudioExtractionResult:
        return self._run(
            "extract_audio", source_id,
            lambda: self._extractor.execute(source_id=source_id),
            serialize_video_audio_extraction_result,
            lambda value: VideoAudioExtractionResult(**dict(value)),
        )

    def transcribe_audio(
        self, source_id: str, audio_asset_id: str,
    ) -> AudioAssetTranscriptionResult:
        return self._run(
            "transcribe_audio", source_id,
            lambda: self._transcriber.execute(audio_asset_id=audio_asset_id),
            serialize_audio_asset_transcription_result,
            lambda value: AudioAssetTranscriptionResult(**dict(value)),
        )

    def summarize_transcript(
        self, source_id: str, transcript_output_id: str,
    ) -> TranscriptSummaryResult:
        return self._run(
            "summarize_transcript", source_id,
            lambda: self._summarizer.execute(
                transcript_output_id=transcript_output_id,
            ),
            serialize_transcript_summary_result,
            lambda value: TranscriptSummaryResult(
                **{
                    **dict(value),
                    "candidate_ids": tuple(value.get("candidate_ids", ())),
                },
            ),
        )

    def _execute_step(self, step: str, facts: VideoAutoFacts):
        source_id = facts.source_id
        if step == "extract_audio":
            return self.extract_audio(source_id)
        if step == "transcribe_audio":
            audio_asset_id = str(getattr(facts.audio, "audio_asset_id"))
            return self.transcribe_audio(source_id, audio_asset_id)
        if step == "summarize_transcript":
            transcript_id = str(getattr(facts.transcript, "output_id"))
            return self.summarize_transcript(source_id, transcript_id)
        if step == "create_memory_candidate":
            summary_id = str(getattr(facts.summary, "output_id"))
            project_id = facts.project_id or "default"
            return self._run(
                step, source_id,
                lambda: self._candidate_creator.execute_from_media_output(
                    output_id=summary_id,
                    project_id=project_id,
                    target_layer="atom",
                    candidate_type="answer_summary",
                ),
                serialize_source_output_memory_candidate,
                lambda value: SourceOutputMemoryCandidateResult(
                    **{
                        **dict(value),
                        "source_refs_display": tuple(value.get("source_refs_display", ())),
                        "blocked_operations": tuple(value.get("blocked_operations", ())),
                    },
                ),
            )
        raise ValueError(f"unsupported video workflow step: {step}")

    def _run(self, step, source_id, invoke, encode, decode):
        return self._effects.execute(
            operation_id=f"video-auto:{source_id}:{step}",
            session_id=f"video-auto:{source_id}",
            root_id=f"video-auto:{source_id}",
            step_key=step,
            kind=f"video_auto_{step}",
            intent_ref=(
                f"crp://{self._namespace_id}/workflow-intents/"
                f"video-auto/{source_id}/{step}"
            ),
            gate_decision_id=self._gate_decision_id,
            rev_set={"workflow_revision": "2", "handler_revision": f"{step}-v1"},
            payload={"source_id": source_id, "step": step},
            effect_class=EffectClass.IDEMPOTENT,
            invoke=invoke,
            encode=encode,
            decode=decode,
        )


def _replace_facts(facts: VideoAutoFacts, **changes) -> VideoAutoFacts:
    values = {
        "source_id": facts.source_id,
        "project_id": facts.project_id,
        "summary_readiness": facts.summary_readiness,
        "audio": facts.audio,
        "transcript": facts.transcript,
        "summary": facts.summary,
        "candidate": facts.candidate,
        "failed_step": facts.failed_step,
        "failure": facts.failure,
    }
    values.update(changes)
    return VideoAutoFacts(**values)


def _fact_field(step: str) -> str:
    return {
        "extract_audio": "audio",
        "transcribe_audio": "transcript",
        "summarize_transcript": "summary",
        "create_memory_candidate": "candidate",
    }[step]


def _summary_readiness(store: ObjectStorePort) -> dict[str, object]:
    settings = GetTranscriptSummarySettings(store).execute()
    if settings.enabled is not True:
        return {"status": "disabled", "provider_name": settings.provider_name, "reason": "transcript summary provider is disabled", "next_step": "enable_transcript_summary_provider"}
    if not settings.command:
        return {"status": "misconfigured", "provider_name": settings.provider_name, "reason": "transcript summary provider command is missing", "next_step": "configure_transcript_summary_command"}
    return {"status": "ready", "provider_name": settings.provider_name, "reason": None, "next_step": None}


def _write_video_projection(store: ObjectStorePort, namespace_id: str, result: VideoAutoWorkflowResult) -> None:
    payload = serialize_video_auto_workflow_result(result)
    store.write("video_auto_workflows", result.workflow_id, payload | {"projection_source": "effect_tree"}, expected_revision=None)
    source = store.read("sources", result.source_id)
    if not isinstance(source, Mapping):
        return
    metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
    metadata["video_auto_workflow"] = {
        **payload,
        "workflow_ref": f"crp://{namespace_id}/video-auto-workflows/{result.workflow_id}.json",
        "steps": [serialize_video_auto_workflow_step(step) for step in result.steps],
        "projection_source": "effect_tree",
    }
    store.write("sources", result.source_id, dict(source) | {"metadata": metadata}, expected_revision=None)
