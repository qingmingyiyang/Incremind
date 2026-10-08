from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort
from .four_layer_memory_candidate_import import ImportFourLayerMemoryCandidatesFromProviderOutput


BUILTIN_LOCAL_SUMMARY_COMMAND = "builtin:semantic-extractive-summary"
_SUMMARY_MODEL_FINGERPRINT_CACHE: dict[tuple[str, int, int], str] = {}


class TranscriptSummaryError(ValueError):
    """Raised when transcript summary generation cannot run safely."""


@dataclass(frozen=True, slots=True)
class TranscriptSummarySettings:
    status: str
    enabled: bool
    provider_name: str
    command: tuple[str, ...]
    timeout_seconds: float
    explicit_enable_required: bool
    remote_processing: bool
    memory_publication: str
    model_status: str
    summary_method: str


@dataclass(frozen=True, slots=True)
class TranscriptSummaryResult:
    status: str
    job_id: str
    output_id: str
    source_id: str
    transcript_output_id: str
    provider: str
    title: str | None
    chapter_count: int
    evidence_count: int
    output_preview: str | None
    creates_memory_candidate: bool
    publishes_memory: bool
    error: str | None
    candidate_ids: tuple[str, ...] = ()


class GetTranscriptSummarySettings:
    _COLLECTION = "transcript_summary_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> TranscriptSummarySettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        if record is None:
            return _settings_from_record(_default_settings_record())
        return _settings_from_record(record)


class SaveTranscriptSummarySettings:
    _COLLECTION = "transcript_summary_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort, *, now: str = "2026-07-02T04:05:00+08:00") -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        command: Sequence[str] = (),
        provider_name: str = "local-semantic-extractive-summary",
        timeout_seconds: float = 600.0,
        confirm_enable: bool = False,
    ) -> TranscriptSummarySettings:
        clean_provider = _required_text(provider_name, "provider_name")
        clean_command = _command_tuple(command)
        clean_timeout = _positive_float(timeout_seconds, "timeout_seconds")
        if enabled:
            if confirm_enable is not True:
                raise TranscriptSummaryError("enabling transcript summary provider requires confirm_enable=true")
            if not clean_command:
                clean_command = (BUILTIN_LOCAL_SUMMARY_COMMAND,)
            _validate_command_executable(clean_command)
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": bool(enabled),
            "provider_name": clean_provider,
            "command": list(clean_command),
            "timeout_seconds": clean_timeout,
            "remote_processing": False,
            "memory_publication": "not_started",
            "updated_at": self._now,
        }
        self._object_store.write(self._COLLECTION, self._SETTINGS_ID, record, expected_revision=None)
        return _settings_from_record(record)


class SummarizeTranscriptOutput:
    """Generate structured summary output from a completed transcript media output."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-02T04:10:00+08:00",
        runner: Callable[[Sequence[str], str, float], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._runner = runner or _run_command

    def execute(self, *, transcript_output_id: str) -> TranscriptSummaryResult:
        clean_output_id = _required_text(transcript_output_id, "transcript_output_id")
        settings = GetTranscriptSummarySettings(self._object_store).execute()
        if settings.enabled is not True:
            raise TranscriptSummaryError("transcript summary provider is disabled")
        if settings.status != "ready":
            raise TranscriptSummaryError("local semantic summary model is missing")
        transcript = self._object_store.read("media_processing_outputs", clean_output_id)
        if transcript is None:
            raise TranscriptSummaryError("transcript output not found")
        if transcript.get("status") != "completed" or transcript.get("output_kind") != "transcript":
            raise TranscriptSummaryError("transcript output must be completed transcript")
        source_id = _required_str(transcript, "source_id")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise TranscriptSummaryError("source not found")
        prompt_payload = _summary_prompt_payload(source=source, transcript=transcript)
        job_id = f"media-job-summary-{clean_output_id}"
        output_id = f"media-output-summary-{source_id}"
        input_identity = _input_identity(transcript, settings.command)
        existing = self._object_store.read("media_processing_outputs", output_id)
        if existing is not None:
            return self._replay_existing(
                existing,
                input_identity=input_identity,
                job_id=job_id,
                output_id=output_id,
                source_id=source_id,
                transcript_output_id=clean_output_id,
                provider=settings.provider_name,
            )
        self._write_job(job_id, source_id=source_id, transcript_output_id=clean_output_id, status="running", error=None)
        try:
            command = _resolved_command(settings.command)
            completed = self._runner(command, json.dumps(prompt_payload, ensure_ascii=False), settings.timeout_seconds)
            if completed.returncode != 0:
                raise TranscriptSummaryError(_command_error(completed, "transcript summary provider failed"))
            summary_data = _summary_data_from_stdout(completed.stdout)
            _validate_extractive_evidence(summary_data, transcript)
            markdown = _render_markdown(summary_data)
            preview = _summary_preview(summary_data, markdown)
            output_ref = f"crp://{self._namespace_id}/media-processing-outputs/{output_id}.json"
            self._object_store.write(
                "media_processing_outputs",
                output_id,
                {
                    "schema_version": "1.0.0",
                    "id": output_id,
                    "job_id": job_id,
                    "source_id": source_id,
                    "source_type": _required_str(transcript, "source_type"),
                    "output_kind": "summary",
                    "status": "completed",
                    "provider": settings.provider_name,
                    "title": _required_str(summary_data, "title"),
                    "preview": preview,
                    "text": markdown,
                    "markdown": markdown,
                    "summary_data": summary_data,
                    "metadata": {
                        "local_processing": True,
                        "remote_processing": False,
                        "transcript_output_id": clean_output_id,
                        "transcript_ref": _required_str(transcript, "ref"),
                        "structured_summary_schema": "old-replay-summary-payload-v1",
                        "memory_publication": "not_started",
                        "auto_memory_candidate": False,
                        "input_identity": input_identity,
                        "summary_method": summary_data.get("summary_method", "external_provider"),
                    },
                    "memory_publication": "not_started",
                    "created_at": self._now,
                    "ref": output_ref,
                },
                expected_revision=None,
            )
            candidate_ids: tuple[str, ...] = ()
            candidate_payload = summary_data.get("memory_candidate_payload")
            if isinstance(candidate_payload, Mapping):
                project_id = _source_project_id(source)
                imported = ImportFourLayerMemoryCandidatesFromProviderOutput(
                    self._object_store,
                    namespace_id=self._namespace_id,
                    now=self._now,
                ).execute_from_media_output(
                    output_id=output_id,
                    project_id=project_id,
                    provider_output=candidate_payload,
                    created_at=self._now,
                )
                candidate_ids = imported.candidate_ids
                stored_output = self._object_store.read("media_processing_outputs", output_id)
                if stored_output is None:
                    raise TranscriptSummaryError("summary output disappeared during candidate import")
                updated_output = dict(stored_output)
                updated_output["memory_publication"] = "candidates_created"
                updated_metadata = dict(updated_output.get("metadata") or {})
                updated_metadata["auto_memory_candidate"] = False
                updated_metadata["memory_candidate_ids"] = list(candidate_ids)
                updated_output["metadata"] = updated_metadata
                self._object_store.write("media_processing_outputs", output_id, updated_output, expected_revision=None)
            self._write_job(
                job_id,
                source_id=source_id,
                transcript_output_id=clean_output_id,
                status="completed",
                error=None,
                output_refs=(output_ref,),
                preview=preview,
            )
            self._mark_source(source, output_id=output_id, output_ref=output_ref, preview=preview)
            return TranscriptSummaryResult(
                status="completed",
                job_id=job_id,
                output_id=output_id,
                source_id=source_id,
                transcript_output_id=clean_output_id,
                provider=settings.provider_name,
                title=_required_str(summary_data, "title"),
                chapter_count=len(_list(summary_data.get("chapters"))),
                evidence_count=len(_list(summary_data.get("evidence"))),
                output_preview=preview,
                creates_memory_candidate=bool(candidate_ids),
                candidate_ids=candidate_ids,
                publishes_memory=False,
                error=None,
            )
        except Exception as error:  # noqa: BLE001 - provider failures must be traceable.
            reason = str(error) or error.__class__.__name__
            self._write_job(
                job_id,
                source_id=source_id,
                transcript_output_id=clean_output_id,
                status="failed",
                error=reason,
            )
            return TranscriptSummaryResult(
                status="failed",
                job_id=job_id,
                output_id=output_id,
                source_id=source_id,
                transcript_output_id=clean_output_id,
                provider=settings.provider_name,
                title=None,
                chapter_count=0,
                evidence_count=0,
                output_preview=None,
                creates_memory_candidate=False,
                candidate_ids=(),
                publishes_memory=False,
                error=reason,
            )

    def _replay_existing(
        self,
        output: Mapping[str, object],
        *,
        input_identity: Mapping[str, object],
        job_id: str,
        output_id: str,
        source_id: str,
        transcript_output_id: str,
        provider: str,
    ) -> TranscriptSummaryResult:
        metadata = output.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("input_identity") != input_identity:
            raise TranscriptSummaryError("transcript or local summary model changed after summary completion")
        if output.get("status") != "completed" or output.get("output_kind") != "summary":
            raise TranscriptSummaryError("existing summary output is not replayable")
        summary_data = output.get("summary_data")
        if not isinstance(summary_data, Mapping):
            raise TranscriptSummaryError("existing summary output is invalid")
        candidate_ids = tuple(
            item for item in metadata.get("memory_candidate_ids", [])
            if isinstance(item, str) and item
        )
        return TranscriptSummaryResult(
            status="completed",
            job_id=job_id,
            output_id=output_id,
            source_id=source_id,
            transcript_output_id=transcript_output_id,
            provider=_optional_str(output.get("provider")) or provider,
            title=_required_str(summary_data, "title"),
            chapter_count=len(_list(summary_data.get("chapters"))),
            evidence_count=len(_list(summary_data.get("evidence"))),
            output_preview=_optional_str(output.get("preview")),
            creates_memory_candidate=bool(candidate_ids),
            candidate_ids=candidate_ids,
            publishes_memory=False,
            error=None,
        )

    def _write_job(
        self,
        job_id: str,
        *,
        source_id: str,
        transcript_output_id: str,
        status: str,
        error: str | None,
        output_refs: Sequence[str] = (),
        preview: str | None = None,
    ) -> None:
        self._object_store.write(
            "media_processing_jobs",
            job_id,
            {
                "schema_version": "1.0.0",
                "id": job_id,
                "source_id": source_id,
                "source_type": "video",
                "required_capability": "transcript_summary",
                "projection_source": "effect_tree",
                "execution_state_owner": "core_effect_log",
                "status": status,
                "disabled_reason": None,
                "input_refs": [f"crp://{self._namespace_id}/media-processing-outputs/{transcript_output_id}.json"],
                "expected_output_refs": [
                    f"crp://{self._namespace_id}/media-processing/{source_id}/summary.json"
                ],
                "adapter_contract": {
                    "capability": "transcript_summary",
                    "provider": "local_command_summary",
                    "binary_content_read": False,
                    "remote_processing": False,
                    "memory_publication": "not_started",
                },
                "error": error,
                "activity_refs": [],
                "output_refs": list(output_refs),
                "output_preview": preview,
                "created_at": self._now,
                "updated_at": self._now,
            },
            expected_revision=None,
        )

    def _mark_source(self, source: Mapping[str, object], *, output_id: str, output_ref: str, preview: str) -> None:
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        extraction = dict(
            metadata.get("audio_track_extraction")
            if isinstance(metadata.get("audio_track_extraction"), Mapping)
            else {}
        )
        extraction.update(
            {
                "summary_state": "completed",
                "summary_output_id": output_id,
                "summary_output_ref": output_ref,
                "summary_preview": preview,
                "memory_publication": "not_started",
                "path_stored_in_source": False,
            }
        )
        metadata["audio_track_extraction"] = extraction
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", _required_str(source, "id"), updated, expected_revision=None)


def serialize_transcript_summary_settings(settings: TranscriptSummarySettings) -> dict[str, object]:
    return {
        "status": settings.status,
        "enabled": settings.enabled,
        "provider_name": settings.provider_name,
        "command": list(settings.command),
        "timeout_seconds": settings.timeout_seconds,
        "explicit_enable_required": settings.explicit_enable_required,
        "remote_processing": settings.remote_processing,
        "memory_publication": settings.memory_publication,
        "model_status": settings.model_status,
        "summary_method": settings.summary_method,
    }


def serialize_transcript_summary_result(result: TranscriptSummaryResult) -> dict[str, object]:
    return {
        "status": result.status,
        "job_id": result.job_id,
        "output_id": result.output_id,
        "source_id": result.source_id,
        "transcript_output_id": result.transcript_output_id,
        "provider": result.provider,
        "title": result.title,
        "chapter_count": result.chapter_count,
        "evidence_count": result.evidence_count,
        "output_preview": result.output_preview,
        "creates_memory_candidate": result.creates_memory_candidate,
        "candidate_ids": list(result.candidate_ids),
        "publishes_memory": result.publishes_memory,
        "error": result.error,
    }


def _settings_from_record(record: Mapping[str, object]) -> TranscriptSummarySettings:
    enabled = record.get("enabled") is True
    command = _command_tuple(record.get("command"))
    return TranscriptSummarySettings(
        status=("ready" if _model_status(command) == "ready" else "model_missing") if enabled else "disabled",
        enabled=enabled,
        provider_name=_optional_str(record.get("provider_name")) or "local-semantic-extractive-summary",
        command=command,
        timeout_seconds=_positive_float(record.get("timeout_seconds", 600.0), "timeout_seconds"),
        explicit_enable_required=True,
        remote_processing=False,
        memory_publication="not_started",
        model_status=_model_status(command),
        summary_method="semantic_extractive" if command == (BUILTIN_LOCAL_SUMMARY_COMMAND,) else "external_provider",
    )


def _default_settings_record() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "default",
        "enabled": False,
        "provider_name": "local-semantic-extractive-summary",
        "command": [BUILTIN_LOCAL_SUMMARY_COMMAND],
        "timeout_seconds": 600.0,
        "remote_processing": False,
        "memory_publication": "not_started",
    }


def _summary_prompt_payload(*, source: Mapping[str, object], transcript: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
    return {
        "title": _required_str(source, "title"),
        "source_id": _required_str(source, "id"),
        "video_reference": metadata.get("video_reference"),
        "duration_seconds": _duration_seconds(transcript),
        "language": transcript.get("language"),
        "text": _required_str(transcript, "text"),
        "segments": list(_list(transcript.get("segments"))),
        "required_summary_schema": "old-replay-summary-payload-v1",
        "safety": {
            "do_not_fabricate": True,
            "return_json_only": True,
            "do_not_publish_memory": True,
        },
    }


def _summary_data_from_stdout(stdout: str) -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TranscriptSummaryError("transcript summary provider must return summary JSON") from exc
    if not isinstance(payload, Mapping):
        raise TranscriptSummaryError("transcript summary JSON must be an object")
    summary = dict(payload)
    _validate_summary(summary)
    return summary


def _validate_summary(summary: Mapping[str, object]) -> None:
    _required_str(summary, "title")
    _required_str(summary, "core_problem")
    if not (_optional_str(summary.get("thirty_second_summary")) or _optional_str(summary.get("one_sentence_summary"))):
        raise TranscriptSummaryError("summary requires thirty_second_summary or one_sentence_summary")
    for key in (
        "chapters",
        "key_takeaways",
        "detailed_notes",
        "evidence",
        "people",
        "terms",
        "examples",
        "data_points",
        "viewpoints",
        "action_items",
        "relations",
        "open_questions",
    ):
        if key in summary and not isinstance(summary[key], Sequence):
            raise TranscriptSummaryError(f"summary {key} must be a list")
    if "visual_attention" in summary and not isinstance(summary["visual_attention"], Mapping):
        raise TranscriptSummaryError("summary visual_attention must be an object")
    for chapter in _list(summary.get("chapters")):
        if not isinstance(chapter, Mapping):
            raise TranscriptSummaryError("summary chapter must be an object")
        _required_str(chapter, "id")
        _required_str(chapter, "title")
        _required_str(chapter, "summary")
        _non_negative_float(chapter.get("start_seconds", 0.0), "chapter.start_seconds")
        _non_negative_float(chapter.get("end_seconds", 0.0), "chapter.end_seconds")
    for evidence in _list(summary.get("evidence")):
        if not isinstance(evidence, Mapping):
            raise TranscriptSummaryError("summary evidence must be an object")
        _required_str(evidence, "id")
        _required_str(evidence, "statement")
        _non_negative_float(evidence.get("start_seconds", 0.0), "evidence.start_seconds")
        _non_negative_float(evidence.get("end_seconds", 0.0), "evidence.end_seconds")


def _render_markdown(summary: Mapping[str, object]) -> str:
    title = _required_str(summary, "title")
    lines: list[str] = [f"# {title}", ""]
    content_type = _optional_str(summary.get("content_type"))
    if content_type:
        lines.extend([f"> 内容类型：{content_type}", ""])
    lines.extend(["## 30 秒摘要", _optional_str(summary.get("thirty_second_summary")) or _optional_str(summary.get("one_sentence_summary")) or "", ""])
    lines.extend(["## 一句话总结", _optional_str(summary.get("one_sentence_summary")) or "", ""])
    lines.extend(["## 核心问题", _required_str(summary, "core_problem"), ""])
    chapters = _list(summary.get("chapters"))
    if chapters:
        lines.extend(["## 章节摘要", ""])
        for chapter in chapters:
            if not isinstance(chapter, Mapping):
                continue
            lines.append(
                f"### {_required_str(chapter, 'title')} "
                f"({_format_timestamp(float(chapter.get('start_seconds', 0.0)))} - "
                f"{_format_timestamp(float(chapter.get('end_seconds', 0.0)))})"
            )
            lines.append(_required_str(chapter, "summary"))
            key_points = _list(chapter.get("key_points"))
            for point in key_points:
                lines.append(f"- {point}")
            lines.append("")
    _append_list_section(lines, "关键结论", summary.get("key_takeaways"))
    _append_list_section(lines, "详细结构化笔记", summary.get("detailed_notes"))
    evidence = _list(summary.get("evidence"))
    if evidence:
        lines.extend(["## 原文证据", ""])
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            lines.append(f"### {_required_str(item, 'id')}")
            lines.append(_required_str(item, "statement"))
            quote = _optional_str(item.get("quote"))
            if quote:
                lines.append(f"> {quote}")
            lines.append("")
    _append_list_section(lines, "仍待回答的问题", summary.get("open_questions"))
    return "\n".join(lines).strip() + "\n"


def _append_list_section(lines: list[str], title: str, value: object) -> None:
    items = _list(value)
    if not items:
        return
    lines.extend([f"## {title}", ""])
    for item in items:
        lines.append(f"- {item}")
    lines.append("")


def _summary_preview(summary: Mapping[str, object], markdown: str) -> str:
    preview = _optional_str(summary.get("thirty_second_summary")) or _optional_str(summary.get("one_sentence_summary"))
    return _preview(preview or markdown)


def _duration_seconds(transcript: Mapping[str, object]) -> float | None:
    segments = _list(transcript.get("segments"))
    if not segments:
        return None
    ends = [float(item.get("end_seconds", 0.0)) for item in segments if isinstance(item, Mapping)]
    return max(ends) if ends else None


def _command_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TranscriptSummaryError("transcript summary command must be a list")
    command = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(command) != len(value):
        raise TranscriptSummaryError("transcript summary command parts must be non-empty strings")
    return command


def _run_command(command: Sequence[str], stdin_text: str, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            input=stdin_text,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise TranscriptSummaryError("transcript summary provider executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise TranscriptSummaryError("transcript summary provider timed out") from exc


def _validate_command_executable(command: Sequence[str]) -> None:
    if tuple(command) == (BUILTIN_LOCAL_SUMMARY_COMMAND,):
        return
    executable = command[0]
    if Path(executable).is_absolute():
        if not Path(executable).exists() or not Path(executable).is_file():
            raise TranscriptSummaryError("transcript summary provider executable not found")
        return
    if shutil.which(executable) is None:
        raise TranscriptSummaryError("transcript summary provider executable not found")


def _resolved_command(command: Sequence[str]) -> tuple[str, ...]:
    if tuple(command) == (BUILTIN_LOCAL_SUMMARY_COMMAND,):
        return (
            sys.executable,
            "-m",
            "backend.video_summary.infrastructure.local_semantic_summary",
            "--stdin-json",
        )
    return tuple(command)


def _model_status(command: Sequence[str]) -> str:
    if tuple(command) != (BUILTIN_LOCAL_SUMMARY_COMMAND,):
        if not command:
            return "missing"
        executable = command[0]
        return "ready" if (Path(executable).is_file() if Path(executable).is_absolute() else shutil.which(executable) is not None) else "missing"
    return "ready" if _summary_model_files() is not None else "missing"


def _input_identity(transcript: Mapping[str, object], command: Sequence[str]) -> dict[str, object]:
    serialized = json.dumps(
        {
            "id": transcript.get("id"),
            "text": transcript.get("text"),
            "segments": transcript.get("segments"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity: dict[str, object] = {
        "transcript_sha256": hashlib.sha256(serialized).hexdigest(),
        "command": list(command),
    }
    if tuple(command) == (BUILTIN_LOCAL_SUMMARY_COMMAND,):
        identity["embedding_model"] = "BAAI/bge-small-zh-v1.5"
        identity["embedding_model_status"] = _model_status(command)
        files = _summary_model_files()
        if files is None:
            raise TranscriptSummaryError("local semantic summary model is missing")
        identity["embedding_model_sha256"] = _cached_sha256(files["model_optimized.onnx"])
        identity["embedding_config_sha256"] = _cached_sha256(files["config.json"])
    return identity


def _validate_extractive_evidence(summary: Mapping[str, object], transcript: Mapping[str, object]) -> None:
    if summary.get("summary_method") != "semantic_extractive_bge_v1":
        return
    transcript_text = _required_str(transcript, "text")
    for key in ("thirty_second_summary", "one_sentence_summary", "core_problem"):
        value = _optional_str(summary.get(key))
        if value and not all(part in transcript_text for part in value.split(" ") if part):
            raise TranscriptSummaryError(f"extractive summary {key} is not grounded in transcript")
    for key in ("key_takeaways", "detailed_notes", "keywords"):
        for value in _list(summary.get(key)):
            haystack = transcript_text.replace("\n", " ")
            grounded = (
                value.lower() in haystack.lower()
                if key == "keywords" and isinstance(value, str)
                else isinstance(value, str) and value in haystack
            )
            if isinstance(value, str) and value and not grounded:
                raise TranscriptSummaryError(f"extractive summary {key} is not grounded in transcript")
    for item in (*_list(summary.get("evidence")), *_list(summary.get("chapters"))):
        if not isinstance(item, Mapping):
            continue
        quote = _optional_str(item.get("quote")) or _optional_str(item.get("summary"))
        start = _non_negative_float(item.get("start_seconds", 0.0), "extractive evidence start")
        end = _non_negative_float(item.get("end_seconds", 0.0), "extractive evidence end")
        if end < start or (quote and not _quote_occurs_in_range(transcript, quote, start, end)):
            raise TranscriptSummaryError("extractive summary evidence is not grounded in transcript")


def _quote_occurs_in_range(transcript: Mapping[str, object], quote: str, start: float, end: float) -> bool:
    texts = [
        _optional_str(item.get("text")) or ""
        for item in _list(transcript.get("segments"))
        if isinstance(item, Mapping)
        and float(item.get("start_seconds", 0.0)) < end + 0.001
        and float(item.get("end_seconds", 0.0)) > start - 0.001
    ]
    return quote in " ".join(texts)


def _source_project_id(source: Mapping[str, object]) -> str:
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        project_id = _optional_str(metadata.get("project_id"))
        if project_id:
            return project_id
    return "default"


def _app_root() -> Path:
    configured = os.environ.get("CHRIPTMAS_APP_ROOT", "").strip()
    return Path(configured).expanduser().resolve(strict=False) if configured else Path(__file__).resolve().parents[3]


def _summary_model_files() -> dict[str, Path] | None:
    root = _app_root() / "data" / "models" / "fastembed"
    candidates = (
        root / "fast-bge-small-zh-v1.5",
        root / "bge-small-zh-v1.5",
        root / "models--BAAI--bge-small-zh-v1.5",
    )
    required = ("config.json", "model_optimized.onnx", "tokenizer.json", "tokenizer_config.json")
    for directory in candidates:
        if not directory.is_dir():
            continue
        resolved: dict[str, Path] = {}
        for name in required:
            path = next(
                (item for item in directory.rglob(name) if item.is_file() and item.stat().st_size > 0),
                None,
            )
            if path is None:
                break
            resolved[name] = path
        if len(resolved) == len(required):
            return resolved
    return None


def _cached_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    cached = _SUMMARY_MODEL_FINGERPRINT_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if len(_SUMMARY_MODEL_FINGERPRINT_CACHE) >= 8:
        _SUMMARY_MODEL_FINGERPRINT_CACHE.clear()
    _SUMMARY_MODEL_FINGERPRINT_CACHE[key] = value
    return value


def _command_error(completed: subprocess.CompletedProcess[str], fallback: str) -> str:
    detail = (completed.stderr or completed.stdout or fallback).strip()
    return " ".join(detail.split())[:240] or fallback


def _list(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(value)


def _preview(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}..."


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptSummaryError(f"{field_name} is required")
    return value.strip()


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise TranscriptSummaryError(f"{key} is required")
    return value


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_float(value: object, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
        raise TranscriptSummaryError(f"{field_name} must be positive")
    return float(value)


def _non_negative_float(value: object, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) < 0:
        raise TranscriptSummaryError(f"{field_name} must be non-negative")
    return float(value)
