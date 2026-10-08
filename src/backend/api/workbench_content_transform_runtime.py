"""Production composition for background local Workbench transformations."""

from __future__ import annotations

import logging
import time
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.document_engine import DocumentDraft
from core.effect_log import Effect, EffectHandlerAbandoned
from core.effect_log.runtime import EffectExecutionCancelled
from core.job_runner import SQLiteJobAdmissionCommand
from core.product_core.audio_asset_transcriber import (
    TranscribeGeneratedAudioAsset,
)
from core.product_core.local_document_text_extractor import (
    BuiltinDocumentTextExtractor,
    document_text_extractors_for_allowed_documents,
)
from core.product_core.source_output_memory_candidate import CreateMemoryCandidateFromSourceOutput
from core.product_core.source_content_read import ReadSourceTextContent
from core.product_core.source_structuring import StructureSourceContent
from core.product_core.local_transcript_summary import CreateLocalTranscriptSummary
from core.product_core.transcript_ad_filter import CreateAdFilteredTranscript
from core.product_core.video_audio_extractor import (
    BUILTIN_PYAV_PROBE,
    ExtractAudioTrackFromAuthorizedVideoSource,
    GetVideoAudioExtractorSettings,
)
from core.product_core.workbench_content_transform_admission import (
    WorkbenchContentTransformAdmissionFactory,
)
from backend.api.tokenhub_asr_provider import (
    TokenHubChunkedAudioAssetTranscriber,
    freeze_workbench_asr_binding,
    workbench_local_transcriber_settings,
)
from backend.security import SafeTextNetworkAdapter
from core.product_core.local_ocr_provider_settings import RunConfiguredLocalOcrProviderForSource
from core.product_core.media_processing_queue import CreateMediaProcessingQueueJob
from core.product_core.source_content_read import ReadLinkWebContent

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkbenchContentTransformDomain:
    runtime_root: Path
    object_store: object
    namespace_id: str
    documents: object

    def execute_item(
        self,
        effect: Effect,
        item: Mapping[str, object],
        checkpoint,
    ) -> Mapping[str, object]:
        self._assert_frozen_item(item)
        execution_ref = f"facts:effect/{effect.operation_id}"
        if item["pipeline"] == "document_extract":
            return self._execute_document(item, execution_ref=execution_ref, checkpoint=checkpoint)
        if item["pipeline"] == "local_video":
            return self._execute_video(item, execution_ref=execution_ref, checkpoint=checkpoint)
        if item["pipeline"] == "web_read":
            return self._execute_web(item, execution_ref=execution_ref, checkpoint=checkpoint)
        if item["pipeline"] == "image_ocr":
            return self._execute_image(item, execution_ref=execution_ref, checkpoint=checkpoint)
        if item["pipeline"] == "audio_transcript":
            return self._execute_audio(item, execution_ref=execution_ref, checkpoint=checkpoint)
        raise ValueError("unsupported workbench transform pipeline")

    def verify_output(
        self,
        effect: Effect,
        item: Mapping[str, object],
        output: Mapping[str, object],
    ) -> None:
        is_document_transform = item.get("pipeline") == "document_extract"
        try:
            self._assert_frozen_item(item)
            if output.get("execution_ref") != f"facts:effect/{effect.operation_id}":
                raise ValueError("workbench transform execution binding drifted")
            document = self.documents.read(str(output["document_id"]))
            produced_revision = output.get("document_revision")
            produced = (self.documents.revision(str(output["document_id"]), produced_revision)
                        if isinstance(produced_revision, int) else None)
            snapshot = produced.get("source_snapshot") if isinstance(produced, Mapping) else None
            source_refs = snapshot.get("source_refs") if isinstance(snapshot, Mapping) else None
            if (
                not isinstance(document, Mapping)
                or not isinstance(produced, Mapping)
                or document.get("revision", 0) < produced_revision
                or document.get("markdown_uri") != output.get("markdown_uri")
                or self.documents.markdown(str(output["document_id"]), revision=produced_revision) is None
                or not isinstance(source_refs, list)
                or not any(ref.get("source_id") == item["source_id"] for ref in source_refs if isinstance(ref, Mapping))
            ):
                raise ValueError("workbench transform Document read-back failed")
            candidate = self.object_store.read("memory_candidates", str(output["candidate_id"]))
            review = candidate.get("review") if isinstance(candidate, Mapping) else None
            if (
                not isinstance(candidate, Mapping)
                or candidate.get("status") != "pending_review"
                or not isinstance(review, Mapping)
                or review.get("requires_user_confirmation") is not True
                or review.get("auto_promote_allowed") is not False
            ):
                raise ValueError("workbench transform candidate read-back failed")
            summary_ref = str(output["summary_ref"])
            if item["pipeline"] in {"document_extract", "web_read"}:
                summary_id = summary_ref.rsplit("/", 1)[-1].removesuffix(".json")
                summary = self.object_store.read("source_structures", summary_id)
            else:
                summary_id = summary_ref.rsplit("/", 1)[-1].removesuffix(".json")
                summary = self.object_store.read("media_processing_outputs", summary_id)
            if not isinstance(summary, Mapping) or summary.get("source_id") != item["source_id"]:
                raise ValueError("workbench transform summary read-back failed")
        except Exception as error:
            if is_document_transform:
                _log_document_transform_failure("verify_output", error)
            raise

    def _execute_web(self, item, *, execution_ref: str, checkpoint) -> Mapping[str, object]:
        source_id = str(item["source_id"])
        checkpoint()
        read = ReadLinkWebContent(
            self.object_store,
            fetch_url=SafeTextNetworkAdapter().fetch_text,
            namespace_id=self.namespace_id,
        ).execute(source_id=source_id)
        if read.status != "completed" or not read.read_ref:
            raise ValueError(read.error or "web content read did not complete")
        content_read_id = read.read_ref.rsplit("/", 1)[-1].removesuffix(".json")
        structure = StructureSourceContent(self.object_store, namespace_id=self.namespace_id).execute(
            source_id=source_id, content_read_id=content_read_id,
        )
        record = self.object_store.read("source_content_reads", content_read_id)
        if not isinstance(record, Mapping):
            raise ValueError("web transform content read disappeared")
        candidate = CreateMemoryCandidateFromSourceOutput(
            self.object_store, namespace_id=self.namespace_id,
        ).execute_from_content_read(
            source_id=source_id, project_id=str(item["project_id"]),
            content_read_id=content_read_id, proposed_content=structure.summary,
            candidate_type="web_takeaway", execution_ref=execution_ref,
        )
        document = self.documents.create_or_replay_generated(DocumentDraft(
            title=str(item["source_title"]), document_type="web_page",
            markdown=_document_markdown(str(item["source_title"]), structure.summary, str(record.get("text") or "")),
            source_refs=({"source_id": source_id, "locator": read.read_ref, "quote": str(record.get("preview") or "")},),
            project_id=str(item["project_id"]),
        ))
        checkpoint()
        return _output(source_id=source_id, pipeline="web_read", document=document,
                       summary_ref=structure.structure_ref, candidate_id=candidate.candidate_id,
                       execution_ref=execution_ref)

    def _execute_image(self, item, *, execution_ref: str, checkpoint) -> Mapping[str, object]:
        source_id = str(item["source_id"])
        checkpoint()
        CreateMediaProcessingQueueJob(self.object_store, enabled_capabilities=("ocr",)).execute(source_id=source_id)
        result = RunConfiguredLocalOcrProviderForSource(
            self.object_store, namespace_id=self.namespace_id,
        ).execute(source_id=source_id)
        if result.status != "completed" or not result.output_refs:
            raise ValueError(result.error or "image OCR did not complete")
        output_id = str(result.output_refs[0]).rsplit("/", 1)[-1].removesuffix(".json")
        return self._output_from_media_text(item, output_id, "image_ocr", execution_ref, checkpoint)

    def _execute_audio(self, item, *, execution_ref: str, checkpoint) -> Mapping[str, object]:
        source_id = str(item["source_id"])
        authorization = self.object_store.read("authorized_file_refs", str(item["authorization_id"]))
        asset = next((record for record in self.object_store.list("audio_asset_refs")
                      if isinstance(record, Mapping) and record.get("source_id") == source_id), None)
        if not isinstance(authorization, Mapping) or not isinstance(asset, Mapping):
            raise ValueError("audio transform asset authorization is unavailable")
        asset_id = str(asset.get("id") or "")
        path = authorization.get("path")
        if not asset_id or not isinstance(path, str) or not Path(path).is_file():
            raise ValueError("audio transform authorized asset is unavailable")
        self.object_store.write("audio_asset_refs", asset_id, dict(asset) | {
            "path": path, "audio_asset_ref": str(item["original_asset_ref"]), "status": "available",
        }, expected_revision=None)
        checkpoint()
        binding = self.object_store.read("workbench_asr_bindings", str(item["asr_binding_id"]))
        if not isinstance(binding, Mapping):
            raise ValueError("workbench ASR binding is unavailable")
        if item["asr_provider"] == "tokenhub-asr":
            transcript = TokenHubChunkedAudioAssetTranscriber(
                self.runtime_root, self.object_store, namespace_id=self.namespace_id, checkpoint=checkpoint,
            ).execute(audio_asset_id=asset_id, execution_ref=execution_ref,
                      binding_id=str(item["asr_binding_id"]))
        else:
            transcript = TranscribeGeneratedAudioAsset(
                self.object_store, namespace_id=self.namespace_id,
                cancellation_requested=lambda: _checkpoint_requested(checkpoint),
                settings_override=workbench_local_transcriber_settings(self.object_store, binding),
            ).execute(audio_asset_id=asset_id)
        if transcript.status != "completed" or not transcript.output_id:
            raise ValueError(transcript.error or "audio transcription did not complete")
        return self._output_from_media_text(item, transcript.output_id, "audio_transcript", execution_ref, checkpoint)

    def _output_from_media_text(self, item, output_id: str, pipeline: str, execution_ref: str, checkpoint):
        source_id = str(item["source_id"])
        output = self.object_store.read("media_processing_outputs", output_id)
        if not isinstance(output, Mapping) or output.get("source_id") != source_id or not str(output.get("text") or "").strip():
            raise ValueError("media transform output read-back failed")
        candidate = CreateMemoryCandidateFromSourceOutput(self.object_store, namespace_id=self.namespace_id).execute_from_media_output(
            output_id=output_id, project_id=str(item["project_id"]), target_layer="atom",
            candidate_type="answer_summary", execution_ref=execution_ref,
        )
        document = self.documents.create_or_replay_generated(DocumentDraft(
            title=str(item["source_title"]), document_type="media_transcript",
            markdown=_document_markdown(str(item["source_title"]), str(output.get("preview") or ""), str(output.get("text") or "")),
            source_refs=({"source_id": source_id, "locator": str(output.get("ref") or "media:output"), "quote": str(output.get("preview") or "")},),
            project_id=str(item["project_id"]),
        ))
        checkpoint()
        return _output(source_id=source_id, pipeline=pipeline, document=document,
                       summary_ref=str(output.get("ref") or ""), candidate_id=candidate.candidate_id,
                       execution_ref=execution_ref)

    def _execute_document(self, item, *, execution_ref: str, checkpoint) -> Mapping[str, object]:
        source_id = str(item["source_id"])
        stage = "read"
        try:
            checkpoint()
            read_result = ReadSourceTextContent(
                self.object_store,
                namespace_id=self.namespace_id,
                document_extractors=document_text_extractors_for_allowed_documents(
                    BuiltinDocumentTextExtractor(provider_name="builtin-local-document-text"),
                ),
            ).execute(source_id=source_id)
            if read_result.status != "completed" or not read_result.read_ref:
                raise ValueError(read_result.error or "document extraction did not complete")
            content_read_id = read_result.read_ref.rsplit("/", 1)[-1].removesuffix(".json")
            stage = "structure"
            structure = StructureSourceContent(
                self.object_store, namespace_id=self.namespace_id,
            ).execute(source_id=source_id, content_read_id=content_read_id)
            stage = "read_evidence"
            read_record = self.object_store.read("source_content_reads", content_read_id)
            source = self.object_store.read("sources", source_id)
            if not isinstance(read_record, Mapping) or not isinstance(source, Mapping):
                raise ValueError("document transform source evidence disappeared")
            stage = "create_candidate"
            candidate = CreateMemoryCandidateFromSourceOutput(
                self.object_store, namespace_id=self.namespace_id,
            ).execute_from_content_read(
                source_id=source_id,
                project_id=str(item["project_id"]),
                content_read_id=content_read_id,
                candidate_type="document_takeaway",
                execution_ref=execution_ref,
            )
            stage = "build_markdown"
            markdown = _document_markdown(
                title=str(item["source_title"]),
                summary=structure.summary,
                body=str(read_record.get("text") or ""),
            )
            stage = "create_document"
            document = self.documents.create_or_replay_generated(DocumentDraft(
                title=str(item["source_title"]),
                document_type="source_document",
                markdown=markdown,
                source_refs=({
                    "source_id": source_id,
                    "locator": read_result.read_ref,
                    "quote": str(read_record.get("preview") or ""),
                },),
                project_id=str(item["project_id"]),
            ))
            stage = "final_checkpoint"
            checkpoint()
            return _output(
                source_id=source_id,
                pipeline="document_extract",
                document=document,
                summary_ref=structure.structure_ref,
                candidate_id=candidate.candidate_id,
                execution_ref=execution_ref,
            )
        except (EffectExecutionCancelled, EffectHandlerAbandoned):
            raise
        except Exception as error:
            _log_document_transform_failure(stage, error)
            raise

    def _execute_video(self, item, *, execution_ref: str, checkpoint) -> Mapping[str, object]:
        source_id = str(item["source_id"])
        checkpoint()
        audio = ExtractAudioTrackFromAuthorizedVideoSource(
            self.object_store,
            namespace_id=self.namespace_id,
            settings_override=_workbench_video_audio_settings(
                self.runtime_root, self.object_store,
            ),
        ).execute(source_id=source_id)
        if audio.status != "completed" or not audio.audio_asset_id:
            raise ValueError(audio.error or "video audio extraction did not complete")
        checkpoint()
        binding = self.object_store.read("workbench_asr_bindings", str(item["asr_binding_id"]))
        if not isinstance(binding, Mapping) or binding.get("provider") != item["asr_provider"]:
            raise ValueError("workbench ASR binding is unavailable")
        if item["asr_provider"] == "tokenhub-asr":
            transcript = TokenHubChunkedAudioAssetTranscriber(
                self.runtime_root,
                self.object_store,
                namespace_id=self.namespace_id,
                checkpoint=checkpoint,
            ).execute(
                audio_asset_id=audio.audio_asset_id,
                execution_ref=execution_ref,
                binding_id=str(item["asr_binding_id"]),
            )
        else:
            transcript = TranscribeGeneratedAudioAsset(
                self.object_store,
                namespace_id=self.namespace_id,
                cancellation_requested=lambda: _checkpoint_requested(checkpoint),
                settings_override=workbench_local_transcriber_settings(self.object_store, binding),
            ).execute(audio_asset_id=audio.audio_asset_id)
        if transcript.status == "cancelled":
            raise EffectHandlerAbandoned("workbench_content_transform.user_cancelled")
        if transcript.status != "completed" or not transcript.output_id:
            raise ValueError(transcript.error or "video transcription did not complete")
        content_transcript = CreateAdFilteredTranscript(
            self.object_store, namespace_id=self.namespace_id,
        ).execute(transcript_output_id=transcript.output_id)
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
            execution_ref=execution_ref,
        )
        source = self.object_store.read("sources", source_id)
        transcript_output = self.object_store.read("media_processing_outputs", transcript.output_id)
        content_transcript_output = self.object_store.read(
            "media_processing_outputs", content_transcript.output_id
        )
        summary_output = self.object_store.read("media_processing_outputs", summary.output_id)
        if not isinstance(source, Mapping) or not isinstance(transcript_output, Mapping) or not isinstance(content_transcript_output, Mapping) or not isinstance(summary_output, Mapping):
            raise ValueError("video transform output disappeared")
        document = self.documents.create_or_replay_generated(DocumentDraft(
            title=str(summary.title or item["source_title"]),
            document_type="media_transcript",
            markdown=_video_markdown(summary_output, transcript_output, content_transcript_output),
            source_refs=({
                "source_id": source_id,
                "locator": str(transcript_output.get("ref") or "media:transcript"),
                "quote": str(transcript_output.get("preview") or ""),
            },),
            project_id=str(item["project_id"]),
        ))
        checkpoint()
        return _output(
            source_id=source_id,
            pipeline="local_video",
            document=document,
            summary_ref=str(summary_output["ref"]),
            candidate_id=candidate.candidate_id,
            execution_ref=execution_ref,
        )

    def _assert_frozen_item(self, item: Mapping[str, object]) -> None:
        source_id = str(item["source_id"])
        if item["pipeline"] == "web_read":
            source = self.object_store.read("sources", source_id)
            if (
                not isinstance(source, Mapping)
                or self.object_store.revision("sources", source_id) != item["source_revision"]
                or source.get("original_url") != item["original_asset_ref"]
            ):
                raise ValueError("workbench transform frozen web source drifted")
            return
        authorization_id = str(item["authorization_id"])
        source = self.object_store.read("sources", source_id)
        authorization = self.object_store.read("authorized_file_refs", authorization_id)
        metadata = source.get("metadata") if isinstance(source, Mapping) else None
        authorization_facts = (
            [metadata.get(key) for key in (
                "file_authorization", "document_authorization", "video_authorization",
                "image_authorization", "audio_authorization",
            )]
            if isinstance(metadata, Mapping) else []
        )
        if (
            not isinstance(source, Mapping)
            or self.object_store.revision("sources", source_id) < item["source_revision"]
            or not isinstance(authorization, Mapping)
            or authorization.get("source_id") != source_id
            or (
                authorization.get("video_reference")
                if item["pipeline"] == "local_video"
                else authorization.get("image_reference")
                or authorization.get("audio_reference")
                or authorization.get("file_reference")
            ) != item["original_asset_ref"]
            or authorization.get("status") != "authorized"
            or self.object_store.revision("authorized_file_refs", authorization_id) != item["authorization_revision"]
            or not any(
                isinstance(fact, Mapping)
                and fact.get("authorization_id") == authorization_id
                and (
                    fact.get("video_reference")
                    if item["pipeline"] == "local_video"
                    else fact.get("image_reference")
                    or fact.get("audio_reference")
                    or fact.get("file_reference")
                ) == item["original_asset_ref"]
                for fact in authorization_facts
            )
        ):
            raise ValueError("workbench transform frozen source authorization drifted")
        if item["pipeline"] in {"local_video", "audio_transcript"}:
            binding = self.object_store.read("workbench_asr_bindings", str(item["asr_binding_id"]))
            if (
                not isinstance(binding, Mapping)
                or binding.get("source_id") != source_id
                or binding.get("project_id") != item["project_id"]
                or binding.get("provider") != item["asr_provider"]
            ):
                raise ValueError("workbench transform frozen ASR binding drifted")


def admit_workbench_content_transform(
    *,
    database_path: Path,
    runtime_root: Path,
    object_store: object,
    job_payload: Mapping[str, object],
    items: Sequence[object],
) -> Mapping[str, object]:
    transform_items: list[dict[str, object]] = []
    for item in items:
        workflow = str(getattr(item, "workflow", ""))
        status = str(getattr(item, "status", ""))
        if status == "failed" or workflow not in {
            "document_text_extraction", "video_auto_workflow", "image_ocr",
            "audio_auto_workflow", "link_auto_organization",
        }:
            continue
        source_id = str(getattr(item, "source_id"))
        source = object_store.read("sources", source_id)
        if not isinstance(source, Mapping):
            raise ValueError("workbench transform source is unavailable")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("workbench transform source metadata is unavailable")
        pipeline = {
            "document_text_extraction": "document_extract",
            "video_auto_workflow": "local_video",
            "image_ocr": "image_ocr",
            "audio_auto_workflow": "audio_transcript",
            "link_auto_organization": "web_read",
        }[workflow]
        if pipeline == "web_read":
            original_url = source.get("original_url")
            if not isinstance(original_url, str) or not original_url:
                raise ValueError("workbench web transform URL is unavailable")
            transform_items.append({
                "source_id": source_id,
                "pipeline": pipeline,
                "source_revision": object_store.revision("sources", source_id),
                "authorization_id": "not-applicable",
                "authorization_revision": 0,
                "original_asset_ref": original_url,
                "source_title": str(source.get("title") or "导入网页"),
                "project_id": str(source.get("project_id") or "default"),
                "asr_provider": "not-applicable",
                "asr_binding_id": "not-applicable",
            })
            continue
        authorization_key = "video_authorization" if pipeline == "local_video" else (
            "image_authorization" if pipeline == "image_ocr" and "image_authorization" in metadata else (
                "audio_authorization" if pipeline == "audio_transcript" and "audio_authorization" in metadata else (
                    "document_authorization" if "document_authorization" in metadata else "file_authorization"
                )
            )
        )
        authorization_fact = metadata.get(authorization_key)
        if not isinstance(authorization_fact, Mapping):
            raise ValueError("workbench transform authorization is unavailable")
        authorization_id = str(authorization_fact.get("authorization_id") or "")
        authorization = object_store.read("authorized_file_refs", authorization_id)
        original_asset_ref = str(
            metadata.get("original_asset_ref")
            or authorization_fact.get("file_reference")
            or authorization_fact.get("image_reference")
            or authorization_fact.get("audio_reference")
            or authorization_fact.get("video_reference")
            or ""
        )
        if not isinstance(authorization, Mapping) or not original_asset_ref:
            raise ValueError("workbench transform authorization evidence is unavailable")
        if pipeline in {"local_video", "audio_transcript"}:
            asr_binding_id = freeze_workbench_asr_binding(
                runtime_root=runtime_root,
                object_store=object_store,
                job_id=str(job_payload.get("id") or ""),
                source_id=source_id,
                project_id=str(source.get("project_id") or "default"),
            )
            asr_binding = object_store.read("workbench_asr_bindings", asr_binding_id)
            if not isinstance(asr_binding, Mapping):
                raise ValueError("workbench transform ASR binding was not persisted")
            asr_provider = str(asr_binding["provider"])
        else:
            asr_binding_id, asr_provider = "not-applicable", "not-applicable"
        transform_items.append({
            "source_id": source_id,
            "pipeline": pipeline,
            "source_revision": object_store.revision("sources", source_id),
            "authorization_id": authorization_id,
            "authorization_revision": object_store.revision("authorized_file_refs", authorization_id),
            "original_asset_ref": original_asset_ref,
            "source_title": str(source.get("title") or "导入资料"),
            "project_id": str(source.get("project_id") or "default"),
            "asr_provider": asr_provider,
            "asr_binding_id": asr_binding_id,
        })
    if not transform_items:
        return {}
    project_ids = {str(item["project_id"]) for item in transform_items}
    if len(project_ids) != 1:
        raise ValueError("workbench transform requires one Source project")
    job = dict(job_payload)
    job.update({
        "job_type": "workbench_content_transform",
        "execution_version": "effect-v2",
        "project_id": next(iter(project_ids)),
        "attempt": 0,
        "status": "pending",
        "transform_items": transform_items,
        "published_outputs": [],
    })
    admission = WorkbenchContentTransformAdmissionFactory(admitted_at=int(time.time())).build(job_payload=job)
    admitted = SQLiteJobAdmissionCommand(database_path).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    )
    return dict(admitted.record.payload)


def readmit_workbench_content_transform(
    *,
    database_path: Path,
    runtime_root: Path,
    object_store: object,
    previous_job: Mapping[str, object],
    command_id: str,
) -> Mapping[str, object]:
    """Create a fresh Effect-v2 Job from a terminal transform projection.

    The previous Job and its Effect subtree remain immutable.  Current source
    and authorization revisions are frozen into the new admission so a retry
    cannot silently reuse stale local-file authority.
    """

    if previous_job.get("job_type") != "workbench_content_transform":
        raise ValueError("job is not a workbench content transform")
    if previous_job.get("execution_version") != "effect-v2":
        raise ValueError("only Effect-v2 transforms can be rebuilt")
    if previous_job.get("status") not in {"failed", "cancelled", "waiting_user"}:
        raise ValueError("workbench transform is not rebuildable")
    if not isinstance(command_id, str) or not command_id.strip():
        raise ValueError("command_id must be non-empty")

    previous_items = previous_job.get("transform_items")
    if not isinstance(previous_items, Sequence) or isinstance(previous_items, (str, bytes)):
        raise ValueError("transform items are unavailable")
    refreshed_items: list[dict[str, object]] = []
    for raw_item in previous_items:
        if not isinstance(raw_item, Mapping):
            raise ValueError("transform item is invalid")
        item = dict(raw_item)
        source_id = str(item.get("source_id") or "")
        authorization_id = str(item.get("authorization_id") or "")
        source = object_store.read("sources", source_id)
        authorization = object_store.read("authorized_file_refs", authorization_id)
        metadata = source.get("metadata") if isinstance(source, Mapping) else None
        authorization_facts = (
            [metadata.get(key) for key in ("file_authorization", "document_authorization", "video_authorization")]
            if isinstance(metadata, Mapping) else []
        )
        if (
            not isinstance(source, Mapping)
            or not isinstance(authorization, Mapping)
            or authorization.get("status") != "authorized"
            or authorization.get("source_id") != source_id
            or (
                authorization.get("video_reference")
                if item.get("pipeline") == "local_video"
                else authorization.get("file_reference")
            ) != item.get("original_asset_ref")
            or not any(
                isinstance(fact, Mapping)
                and fact.get("authorization_id") == authorization_id
                and (
                    fact.get("video_reference")
                    if item.get("pipeline") == "local_video"
                    else fact.get("file_reference")
                ) == item.get("original_asset_ref")
                for fact in authorization_facts
            )
        ):
            raise ValueError("workbench transform source authorization is no longer current")
        item["source_revision"] = object_store.revision("sources", source_id)
        item["authorization_revision"] = object_store.revision(
            "authorized_file_refs", authorization_id,
        )
        if item.get("pipeline") in {"local_video", "audio_transcript"}:
            binding_id = freeze_workbench_asr_binding(
                runtime_root=runtime_root,
                object_store=object_store,
                job_id=f"workbench-transform-retry-{command_id}",
                source_id=source_id,
                project_id=str(item["project_id"]),
            )
            binding = object_store.read("workbench_asr_bindings", binding_id)
            if not isinstance(binding, Mapping):
                raise ValueError("workbench transform ASR binding was not persisted")
            item["asr_binding_id"] = binding_id
            item["asr_provider"] = str(binding["provider"])
        else:
            item["asr_binding_id"] = "not-applicable"
            item["asr_provider"] = "not-applicable"
        refreshed_items.append(item)
    if not refreshed_items:
        raise ValueError("transform items are unavailable")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    job_id = f"workbench-transform-retry-{command_id}"
    job = {
        "schema_version": "1.0.0",
        "id": job_id,
        "source_id": str(previous_job.get("source_id") or refreshed_items[0]["source_id"]),
        "job_type": "workbench_content_transform",
        "idempotency_key": job_id,
        "execution_version": "effect-v2",
        "project_id": str(previous_job.get("project_id") or refreshed_items[0]["project_id"]),
        "status": "pending",
        "attempt": 0,
        "max_attempts": 1,
        "lease": None,
        "progress": {"current": 0, "total": 1, "percent": 0, "message": None},
        "steps": [{
            "name": "transform_content",
            "status": "pending",
            "attempt": 0,
            "started_at": None,
            "completed_at": None,
            "progress": 0,
            "input_refs": [str(item["original_asset_ref"]) for item in refreshed_items],
            "staged_output_refs": [],
            "log_refs": [],
            "error": None,
        }],
        "error": None,
        "checkpoint": None,
        "staged_outputs": [],
        "published_outputs": [],
        "log_refs": [],
        "created_at": now,
        "updated_at": now,
        "transform_items": refreshed_items,
        "rebuilt_from_job_id": str(previous_job.get("id") or ""),
    }
    admission = WorkbenchContentTransformAdmissionFactory(
        admitted_at=int(time.time()),
    ).build(job_payload=job)
    admitted = SQLiteJobAdmissionCommand(database_path).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    )
    return dict(admitted.record.payload)


def build_workbench_content_transform_domain(runtime_root: Path, object_store: object, namespace_id: str):
    documents = AggregateRepositoryFactory(
        runtime_root=Path(runtime_root), namespace_id=namespace_id, json_store=object_store,
    ).document_repository()
    return WorkbenchContentTransformDomain(Path(runtime_root), object_store, namespace_id, documents)


def _log_document_transform_failure(stage: str, error: Exception) -> None:
    """Record bounded local diagnostics without logging untrusted exception text."""

    LOGGER.warning(
        "workbench_document_transform_failed stage=%s exception_type=%s",
        stage,
        _bounded_exception_type(error),
    )


def _bounded_exception_type(error: Exception) -> str:
    name = type(error).__name__
    if not name.isascii() or not name.isidentifier():
        return "UnknownException"
    return name[:80]


def _output(*, source_id: str, pipeline: str, document, summary_ref: str, candidate_id: str, execution_ref: str):
    return {
        "source_id": source_id,
        "pipeline": pipeline,
        "document_id": str(document["id"]),
        "document_revision": int(document["revision"]),
        "markdown_uri": str(document["markdown_uri"]),
        "summary_ref": summary_ref,
        "candidate_id": candidate_id,
        "candidate_status": "pending_review",
        "execution_ref": execution_ref,
    }


def _document_markdown(*, title: str, summary: str, body: str) -> str:
    return f"# {title}\n\n## 摘要\n\n{summary}\n\n## 原文\n\n{body.strip()}\n"


def _video_markdown(
    summary: Mapping[str, object],
    transcript: Mapping[str, object],
    content_transcript: Mapping[str, object],
) -> str:
    summary_text = str(summary.get("text") or summary.get("markdown") or "").strip()
    transcript_text = str(transcript.get("text") or "").strip()
    metadata = content_transcript.get("metadata") if isinstance(content_transcript.get("metadata"), Mapping) else {}
    excluded = int(metadata.get("excluded_ad_count") or 0)
    uncertain = int(metadata.get("uncertain_ad_count") or 0)
    note = (
        f"> 整理说明：摘要已排除 {excluded} 个明确广告片段；"
        f"{uncertain} 个不确定片段按保守规则保留。完整转写保持原样。"
    )
    return f"# 视频转写与摘要\n\n{note}\n\n{summary_text}\n\n## 完整转写\n\n{transcript_text}\n"


def _checkpoint_requested(checkpoint) -> bool:
    checkpoint()
    return False


def _workbench_video_audio_settings(runtime_root: Path, object_store: object):
    settings = GetVideoAudioExtractorSettings(object_store).execute()
    if settings.enabled:
        return settings
    return replace(
        settings,
        status="ready",
        enabled=True,
        provider_name="workbench-local-ffmpeg-audio-extractor",
        ffmpeg_path=str(_bundled_ffmpeg()),
        ffprobe_path=BUILTIN_PYAV_PROBE,
        output_root=str(
            Path(runtime_root).resolve(strict=False)
            / ".rebuild-data"
            / "workbench-content-transform"
            / "audio"
        ),
    )


def _bundled_ffmpeg() -> Path:
    executable_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidates = [
        Path(sys.executable).resolve(strict=False).parent
        / "Library"
        / "bin"
        / executable_name,
    ]
    candidates.extend(
        parent / "runtime" / "Library" / "bin" / executable_name
        for parent in Path(__file__).resolve(strict=False).parents
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve(strict=False)
    resolved = shutil.which(executable_name)
    if resolved:
        return Path(resolved).resolve(strict=False)
    raise RuntimeError("bundled FFmpeg is unavailable for Workbench video transformation")
