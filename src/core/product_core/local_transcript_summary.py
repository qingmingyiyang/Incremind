"""Always-available local transcript summary for automatic intake paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re


@dataclass(frozen=True, slots=True)
class LocalTranscriptSummaryResult:
    job_id: str
    output_id: str
    output_ref: str
    title: str


class CreateLocalTranscriptSummary:
    """Create a deterministic extractive summary without enabling a model provider."""

    def __init__(self, object_store: object, *, namespace_id: str) -> None:
        self._store = object_store
        self._namespace_id = namespace_id

    def execute(self, *, transcript_output_id: str) -> LocalTranscriptSummaryResult:
        transcript = self._store.read("media_processing_outputs", transcript_output_id)
        if (
            not isinstance(transcript, Mapping)
            or transcript.get("status") != "completed"
            or transcript.get("output_kind") not in {"transcript", "content_transcript"}
        ):
            raise ValueError("completed transcript output is required")
        source_id = _required(transcript, "source_id")
        source = self._store.read("sources", source_id)
        if not isinstance(source, Mapping):
            raise ValueError("transcript source is unavailable")
        body = transcript.get("text")
        if not isinstance(body, str):
            raise ValueError("text must be a string")
        clean = _plain_text(body)
        metadata = transcript.get("metadata") if isinstance(transcript.get("metadata"), Mapping) else {}
        ad_filter_version = str(metadata.get("ad_filter_version") or "").strip()
        no_non_ad_content = not clean and ad_filter_version and int(metadata.get("excluded_ad_count") or 0) > 0
        if not clean and not no_non_ad_content:
            raise ValueError("transcript text is empty")
        title = str(source.get("title") or transcript.get("title") or "视频资料").strip()
        preview = clean[:600] if clean else "未检测到可整理的非广告正文。"
        detail = clean[:2400] if clean else "原始转写仅包含已识别的明确广告片段，未生成内容结论。"
        markdown = (
            f"# {title}\n\n"
            "## 本地摘录摘要\n\n"
            f"{preview}\n\n"
            "## 关键原文\n\n"
            f"{detail}\n"
        )
        identity_suffix = ""
        if ad_filter_version:
            identity_suffix = "-" + ad_filter_version.replace("transcript-", "").replace("-", "_")
        job_id = f"media-job-local-summary{identity_suffix}-{source_id}"
        output_id = f"media-output-local-summary{identity_suffix}-{source_id}"
        output_ref = f"crp://{self._namespace_id}/media-processing-outputs/{output_id}.json"
        created_at = str(transcript.get("created_at") or _utc_now())
        job = {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "source_type": str(transcript.get("source_type") or "video"),
            "status": "completed",
            "pipeline": "local_extractive_transcript_summary",
            "input_refs": [_required(transcript, "ref")],
            "output_refs": [output_ref],
            "error": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        output = {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source_id,
            "source_type": str(transcript.get("source_type") or "video"),
            "output_kind": "summary",
            "status": "completed",
            "provider": "builtin-local-extractive-summary",
            "title": title,
            "preview": preview,
            "text": markdown,
            "markdown": markdown,
            "metadata": {
                "local_processing": True,
                "remote_processing": False,
                "transcript_output_id": transcript_output_id,
                "transcript_ref": _required(transcript, "ref"),
                "summary_method": "deterministic_extractive",
                "ad_filter_version": ad_filter_version or None,
                "content_transcript_output_id": transcript_output_id if ad_filter_version else None,
                "memory_publication": "not_started",
                "auto_memory_candidate": False,
            },
            "memory_publication": "not_started",
            "created_at": created_at,
            "ref": output_ref,
        }
        _write_or_verify(self._store, "media_processing_jobs", job_id, job)
        _write_or_verify(self._store, "media_processing_outputs", output_id, output)
        return LocalTranscriptSummaryResult(job_id, output_id, output_ref, title)


def _write_or_verify(store: object, collection: str, object_id: str, expected: Mapping[str, object]) -> None:
    existing = store.read(collection, object_id)
    if existing is None:
        store.write(collection, object_id, dict(expected), expected_revision=None)
        return
    stable_keys = {
        "id", "job_id", "source_id", "source_type", "output_kind", "status",
        "provider", "title", "preview", "text", "markdown", "pipeline",
        "input_refs", "output_refs", "created_at", "ref",
    }
    if any(existing.get(key) != expected.get(key) for key in stable_keys if key in expected):
        raise ValueError(f"existing {collection} output identity drifted")


def _plain_text(value: str) -> str:
    text = re.sub(r"[`#>*_\[\]()]", " ", value)
    return re.sub(r"\s+", " ", text).strip()


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be non-empty")
    return item


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
