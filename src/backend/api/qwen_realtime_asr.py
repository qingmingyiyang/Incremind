from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backend.security.provider_egress import ProviderEgressManifest, ProviderEgressPolicyStore
from core.product_core.realtime_asr_provider_settings import (
    QWEN_REALTIME_ASR_MAX_AUDIO_BYTES,
    QWEN_REALTIME_ASR_MODEL,
    QWEN_REALTIME_ASR_PROVIDER_ID,
    RealtimeAsrProviderSettings,
)


QWEN_REALTIME_ASR_MAX_VOCABULARY_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class QwenRealtimeEvent:
    kind: Literal["started", "partial", "final", "finished", "failed", "ignored"]
    text: str = ""
    sentence_id: int = 0
    duration_seconds: int | None = None
    error_code: str = ""


def qwen_realtime_egress_manifest(
    root_dir: Path, *, endpoint: str
) -> ProviderEgressManifest:
    return ProviderEgressPolicyStore(root_dir).manifest(
        provider_id=QWEN_REALTIME_ASR_PROVIDER_ID,
        endpoint=endpoint,
        purposes=("realtime_transcription",),
        payload_categories=("microphone_pcm_audio", "session_vocabulary"),
        max_payload_bytes=QWEN_REALTIME_ASR_MAX_AUDIO_BYTES + QWEN_REALTIME_ASR_MAX_VOCABULARY_BYTES,
    )


def build_run_task(
    *,
    task_id: str,
    settings: RealtimeAsrProviderSettings,
    vocabulary: Mapping[str, int],
) -> dict[str, object]:
    parameters: dict[str, object] = {
        "format": "pcm",
        "sample_rate": settings.sample_rate,
    }
    if vocabulary:
        parameters["vocabulary"] = dict(vocabulary)
    return {
        "header": {
            "action": "run-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": QWEN_REALTIME_ASR_MODEL,
            "parameters": parameters,
            "input": {},
        },
    }


def build_finish_task(*, task_id: str) -> dict[str, object]:
    return {
        "header": {
            "action": "finish-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {"input": {}},
    }


def parse_qwen_event(value: object) -> QwenRealtimeEvent:
    if not isinstance(value, Mapping):
        return QwenRealtimeEvent("ignored")
    header = value.get("header")
    if not isinstance(header, Mapping):
        return QwenRealtimeEvent("ignored")
    event = header.get("event")
    if event == "task-started":
        return QwenRealtimeEvent("started")
    if event == "task-finished":
        return QwenRealtimeEvent("finished")
    if event == "task-failed":
        code = header.get("error_code")
        return QwenRealtimeEvent(
            "failed",
            error_code=str(code)[:64] if isinstance(code, str) else "provider_failed",
        )
    if event != "result-generated":
        return QwenRealtimeEvent("ignored")
    payload = value.get("payload")
    output = payload.get("output") if isinstance(payload, Mapping) else None
    sentence = output.get("sentence") if isinstance(output, Mapping) else None
    if not isinstance(sentence, Mapping) or sentence.get("heartbeat") is True:
        return QwenRealtimeEvent("ignored")
    text = sentence.get("text")
    if not isinstance(text, str) or len(text) > 32_768:
        return QwenRealtimeEvent("ignored")
    sentence_id = sentence.get("sentence_id")
    clean_sentence_id = sentence_id if isinstance(sentence_id, int) and not isinstance(sentence_id, bool) else 0
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    duration = usage.get("duration") if isinstance(usage, Mapping) else None
    clean_duration = duration if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0 else None
    return QwenRealtimeEvent(
        "final" if sentence.get("sentence_end") is True else "partial",
        text=text,
        sentence_id=clean_sentence_id,
        duration_seconds=clean_duration,
    )


__all__ = (
    "QwenRealtimeEvent",
    "build_finish_task",
    "build_run_task",
    "parse_qwen_event",
    "qwen_realtime_egress_manifest",
)
