"""Production adapter for receipt-bound Bilibili local post-processing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import time

from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.effect_log import Effect, EffectLog
from core.job_runner.execution_admission import JobExecutionAdmissionAuthority
from core.job_runner.job_projection import JobProjectionBuilder
from core.job_runner.sqlite_admission import SQLiteJobAdmissionCommand
from core.product_core.bilibili_media_postprocess import (
    BilibiliPostprocessAdmissionFactory,
    exact_input,
)
from core.product_core.local_transcript_summary import CreateLocalTranscriptSummary
from core.product_core.transcript_ad_filter import CreateAdFilteredTranscript
from core.product_core.source_output_memory_candidate import CreateMemoryCandidateFromSourceOutput
from core.source_processing import SourceManifestArtifactRepository


@dataclass(slots=True)
class BilibiliReceiptPostprocessAdmission:
    database: Path
    object_store: object
    namespace_id: str
    _authority: JobExecutionAdmissionAuthority = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._authority = JobExecutionAdmissionAuthority(
            EffectLog(self.database), JobProjectionBuilder(),
        )

    def __call__(self, connection, effect: Effect, parent_job: Mapping[str, object], receipt: Mapping[str, object]) -> None:
        media = parent_job.get("media_hands")
        manifest = media.get("manifest") if isinstance(media, Mapping) else None
        permission = media.get("permission_snapshot") if isinstance(media, Mapping) else None
        if not isinstance(manifest, Mapping) or not isinstance(permission, Mapping):
            raise ValueError("Media Hands postprocess authority is unavailable")
        manifest_ref = _required(manifest, "ref")
        manifest_revision = _required(manifest, "revision")
        project_id = _required(permission, "project_id")
        artifact = SourceManifestArtifactRepository(
            self.object_store, namespace_id=self.namespace_id,
        ).resolve_source_ref(source_ref=manifest_ref, project_id=project_id)
        if (
            artifact.manifest.platform != "bilibili"
            or artifact.revision != manifest_revision
            or artifact.manifest.source_id != parent_job.get("source_id")
        ):
            return
        output = receipt.get("output")
        if not isinstance(output, Mapping) or output.get("kind") != "document":
            raise ValueError("Bilibili parent receipt Document is unavailable")
        parent_job_id = _required(parent_job, "id")
        child_job_id = f"bilibili-postprocess:{parent_job_id}"
        created_at = str(parent_job.get("created_at") or "1970-01-01T00:00:00Z")
        item = {
            "parent_job_id": parent_job_id,
            "parent_operation_id": effect.operation_id,
            "parent_receipt_ref": _required(receipt, "receipt_ref"),
            "source_id": _required(parent_job, "source_id"),
            "project_id": project_id,
            "manifest_ref": manifest_ref,
            "manifest_revision": manifest_revision,
            "document_id": _required(output, "object_id"),
            "document_revision": 1,
            "document_uri": _required(output, "uri"),
        }
        job = {
            "schema_version": "1.0.0",
            "id": child_job_id,
            "source_id": item["source_id"],
            "project_id": project_id,
            "job_type": "bilibili_media_postprocess",
            "idempotency_key": child_job_id,
            "execution_version": "effect-v2",
            "status": "pending",
            "attempt": 0,
            "max_attempts": 1,
            "lease": None,
            "progress": {"current": 0, "total": 1, "percent": 0, "message": None},
            "steps": [{"name": "local_summary_and_memory_candidate", "status": "pending"}],
            "error": None,
            "checkpoint": None,
            "staged_outputs": [],
            "published_outputs": [],
            "log_refs": [],
            "postprocess_input": item,
            "created_at": created_at,
            "updated_at": created_at,
        }
        admission = BilibiliPostprocessAdmissionFactory(
            admitted_at=int(time.time()),
        ).build(job_payload=job)
        self._authority.admit_in_connection(
            connection,
            payload=job,
            authorization=admission.authorization,
            intent=admission.intent,
        )


@dataclass(slots=True)
class BilibiliPostprocessDomain:
    runtime_root: Path
    object_store: object
    namespace_id: str
    documents: object

    def execute(self, effect: Effect, item: Mapping[str, object], checkpoint) -> Mapping[str, object]:
        self._materialize_source_projection(item)
        self._assert_input(item)
        checkpoint()
        markdown = self.documents.markdown(str(item["document_id"]), revision=1)
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("Bilibili transcript Document is empty")
        transcript_output_id = f"media-output-bilibili-transcript-{item['document_id']}"
        transcript_job_id = f"media-job-bilibili-transcript-{item['document_id']}"
        transcript_ref = f"crp://{self.namespace_id}/media-processing-outputs/{transcript_output_id}.json"
        created_at = _document_created_at(self.documents.read(str(item["document_id"])))
        transcript_job = {
            "schema_version": "1.0.0",
            "id": transcript_job_id,
            "source_id": item["source_id"],
            "source_type": "video",
            "status": "completed",
            "pipeline": "bilibili_document_transcript_projection",
            "input_refs": [item["document_uri"], item["manifest_ref"]],
            "output_refs": [transcript_ref],
            "error": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        transcript_output = {
            "schema_version": "1.0.0",
            "id": transcript_output_id,
            "job_id": transcript_job_id,
            "source_id": item["source_id"],
            "source_type": "video",
            "output_kind": "transcript",
            "status": "completed",
            "provider": "bilibili-document-transcript-projection",
            "title": "B站视频转写",
            "preview": _preview(markdown),
            "text": markdown,
            "segments": [],
            "metadata": {
                "document_id": item["document_id"],
                "document_revision": 1,
                "manifest_ref": item["manifest_ref"],
                "manifest_revision": item["manifest_revision"],
                "local_processing": True,
                "remote_processing": False,
                "execution_ref": f"facts:effect/{effect.operation_id}",
            },
            "memory_publication": "not_started",
            "created_at": created_at,
            "ref": transcript_ref,
        }
        _write_or_verify(self.object_store, "media_processing_jobs", transcript_job_id, transcript_job)
        _write_or_verify(self.object_store, "media_processing_outputs", transcript_output_id, transcript_output)
        content_transcript = CreateAdFilteredTranscript(
            self.object_store, namespace_id=self.namespace_id,
        ).execute(transcript_output_id=transcript_output_id)
        summary = CreateLocalTranscriptSummary(
            self.object_store, namespace_id=self.namespace_id,
        ).execute(transcript_output_id=content_transcript.output_id)
        candidate = CreateMemoryCandidateFromSourceOutput(
            self.object_store, namespace_id=self.namespace_id,
        ).execute_from_media_output(
            output_id=summary.output_id,
            project_id=str(item["project_id"]),
            target_layer="atom",
            candidate_type="answer_summary",
            execution_ref=f"facts:effect/{effect.operation_id}",
        )
        checkpoint()
        return {
            "transcript_output_id": transcript_output_id,
            "content_transcript_output_id": content_transcript.output_id,
            "summary_output_id": summary.output_id,
            "candidate_id": candidate.candidate_id,
            "candidate_status": candidate.candidate_status,
            "execution_ref": f"facts:effect/{effect.operation_id}",
        }

    def verify(self, effect: Effect, item: Mapping[str, object], output: Mapping[str, object]) -> None:
        self._assert_input(item)
        if output.get("execution_ref") != f"facts:effect/{effect.operation_id}":
            raise ValueError("Bilibili postprocess execution binding drifted")
        transcript = self.object_store.read("media_processing_outputs", str(output["transcript_output_id"]))
        content_transcript = self.object_store.read(
            "media_processing_outputs", str(output["content_transcript_output_id"])
        )
        summary = self.object_store.read("media_processing_outputs", str(output["summary_output_id"]))
        candidate = self.object_store.read("memory_candidates", str(output["candidate_id"]))
        review = candidate.get("review") if isinstance(candidate, Mapping) else None
        if (
            not isinstance(transcript, Mapping)
            or transcript.get("status") != "completed"
            or transcript.get("output_kind") != "transcript"
            or not isinstance(content_transcript, Mapping)
            or content_transcript.get("status") != "completed"
            or content_transcript.get("output_kind") != "content_transcript"
            or content_transcript.get("metadata", {}).get("original_transcript_output_id") != output["transcript_output_id"]
            or not isinstance(summary, Mapping)
            or summary.get("status") != "completed"
            or summary.get("output_kind") != "summary"
            or not isinstance(candidate, Mapping)
            or candidate.get("status") != "pending_review"
            or not isinstance(review, Mapping)
            or review.get("requires_user_confirmation") is not True
            or review.get("auto_promote_allowed") is not False
        ):
            raise ValueError("Bilibili postprocess output verification failed")

    def _assert_input(self, item: Mapping[str, object]) -> None:
        document = self.documents.read(str(item["document_id"]))
        if (
            not isinstance(document, Mapping)
            or document.get("revision") != item["document_revision"]
            or document.get("markdown_uri") != item["document_uri"]
            or document.get("project_id") != item["project_id"]
        ):
            raise ValueError("Bilibili postprocess Document drifted")
        source = self.object_store.read("sources", str(item["source_id"]))
        if not isinstance(source, Mapping):
            raise ValueError("Bilibili postprocess source is unavailable")

    def _materialize_source_projection(self, item: Mapping[str, object]) -> None:
        source_id = str(item["source_id"])
        if self.object_store.read("sources", source_id) is not None:
            return
        document = self.documents.read(str(item["document_id"]))
        if (
            not isinstance(document, Mapping)
            or document.get("revision") != item["document_revision"]
            or document.get("markdown_uri") != item["document_uri"]
            or document.get("project_id") != item["project_id"]
        ):
            raise ValueError("Bilibili postprocess Document drifted")
        created_at = _document_created_at(document)
        identity = "\n".join((
            str(item["manifest_ref"]),
            str(item["manifest_revision"]),
            str(item["document_uri"]),
        ))
        self.object_store.write(
            "sources",
            source_id,
            {
                "schema_version": "1.1.0",
                "id": source_id,
                "type": "video",
                "title": str(document.get("title") or "B站视频转写"),
                "project_id": str(item["project_id"]),
                "capture_mode": "verified_source_manifest",
                "storage_uri": f"crp://{self.namespace_id}/sources/{source_id}",
                "original_url": None,
                "content_hash": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "media_type": "video/x-bilibili",
                "size_bytes": 0,
                "parser_version": None,
                "processing_state": "ready",
                "created_at": created_at,
                "occurred_at": created_at,
                "recorded_at": created_at,
                "imported_from_legacy": False,
                "trust_status": "user_confirmed",
                "metadata": {
                    "manifest_ref": str(item["manifest_ref"]),
                    "manifest_revision": str(item["manifest_revision"]),
                    "document_id": str(item["document_id"]),
                    "document_revision": item["document_revision"],
                    "content_hash_basis": "verified_manifest_and_document",
                    "content_snapshot": None,
                    "memory_publication_state": "not_published",
                    "remote_fetch": "completed_by_media_hands",
                },
            },
            expected_revision=None,
        )


def readmit_bilibili_postprocess(
    *,
    database_path: Path,
    runtime_root: Path,
    object_store: object,
    namespace_id: str,
    previous_job: Mapping[str, object],
    command_id: str,
) -> Mapping[str, object]:
    """Create a new child Effect without mutating a failed post-process attempt."""

    if previous_job.get("job_type") != "bilibili_media_postprocess":
        raise ValueError("job is not a Bilibili postprocess")
    if previous_job.get("execution_version") != "effect-v2":
        raise ValueError("only Effect-v2 Bilibili postprocess Jobs can be rebuilt")
    if previous_job.get("status") not in {"failed", "cancelled", "waiting_user"}:
        raise ValueError("Bilibili postprocess is not rebuildable")
    if not isinstance(command_id, str) or not command_id.strip():
        raise ValueError("command_id must be non-empty")

    item = exact_input(previous_job.get("postprocess_input"))
    artifact = SourceManifestArtifactRepository(
        object_store, namespace_id=namespace_id,
    ).resolve_source_ref(
        source_ref=str(item["manifest_ref"]),
        project_id=str(item["project_id"]),
    )
    if (
        artifact.manifest.platform != "bilibili"
        or artifact.revision != item["manifest_revision"]
        or artifact.manifest.source_id != item["source_id"]
    ):
        raise ValueError("Bilibili postprocess manifest is no longer current")
    documents = AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=namespace_id,
        json_store=object_store,
    ).document_repository()
    document = documents.read(str(item["document_id"]))
    if (
        not isinstance(document, Mapping)
        or document.get("revision") != item["document_revision"]
        or document.get("markdown_uri") != item["document_uri"]
        or document.get("project_id") != item["project_id"]
    ):
        raise ValueError("Bilibili postprocess Document is no longer current")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    job_id = f"bilibili-postprocess-retry:{command_id}"
    job = {
        "schema_version": "1.0.0",
        "id": job_id,
        "source_id": item["source_id"],
        "project_id": item["project_id"],
        "job_type": "bilibili_media_postprocess",
        "idempotency_key": job_id,
        "execution_version": "effect-v2",
        "status": "pending",
        "attempt": 0,
        "max_attempts": 1,
        "lease": None,
        "progress": {"current": 0, "total": 1, "percent": 0, "message": None},
        "steps": [{"name": "local_summary_and_memory_candidate", "status": "pending"}],
        "error": None,
        "checkpoint": None,
        "staged_outputs": [],
        "published_outputs": [],
        "log_refs": [],
        "postprocess_input": item,
        "rebuilt_from_job_id": str(previous_job.get("id") or ""),
        "created_at": now,
        "updated_at": now,
    }
    admission = BilibiliPostprocessAdmissionFactory(
        admitted_at=int(time.time()),
    ).build(job_payload=job)
    admitted = SQLiteJobAdmissionCommand(database_path).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    )
    return dict(admitted.record.payload)


def build_bilibili_postprocess_domain(runtime_root: Path, object_store: object, namespace_id: str):
    documents = AggregateRepositoryFactory(
        runtime_root=runtime_root, namespace_id=namespace_id, json_store=object_store,
    ).document_repository()
    return BilibiliPostprocessDomain(runtime_root, object_store, namespace_id, documents)


def _write_or_verify(store, collection: str, object_id: str, expected: Mapping[str, object]) -> None:
    existing = store.read(collection, object_id)
    if existing is None:
        store.write(collection, object_id, dict(expected), expected_revision=None)
        return
    stable_keys = {
        "id", "job_id", "source_id", "source_type", "output_kind", "status",
        "provider", "title", "preview", "text", "segments", "pipeline",
        "input_refs", "output_refs", "created_at", "ref",
    }
    if any(existing.get(key) != expected.get(key) for key in stable_keys if key in expected):
        raise ValueError(f"existing {collection} projection drifted")


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be non-empty")
    return item


def _preview(markdown: str) -> str:
    return " ".join(markdown.replace("#", " ").split())[:600]


def _document_created_at(document: Mapping[str, object] | None) -> str:
    if isinstance(document, Mapping):
        value = document.get("created_at")
        if isinstance(value, str) and value:
            return value
    return "1970-01-01T00:00:00Z"
