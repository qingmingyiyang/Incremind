from __future__ import annotations

import time
import uuid
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.api.audio_auto_effect_runtime import AudioAutoEffectRuntime
from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.local_audio_chunking import split_local_audio_chunk
from backend.api.long_audio_effect_runtime import LongAudioEffectRuntime
from backend.api.workbench_content_transform_runtime import admit_workbench_content_transform
from backend.api.workbench_review_intent_runtime import ReviewIntentAdmission, ReviewIntentJobRepository
from backend.api.workbench_input_classifier_runtime import (
    WorkbenchInputClassifierContainerPort,
    build_workbench_input_classifier_runtime,
)
from backend.api.workbench_original_asset_runtime import build_original_asset_resolver
from backend.security import SafeTextNetworkAdapter
from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import RoutedJobRepository, SQLiteJobAdmissionCommand
from core.product_core.candidate_job_admission import CandidateJobAdmissionFactory
from core.product_core.candidate_memory_job import CandidateMemoryJobInput, build_candidate_memory_job
from core.product_core.developer_studio_config import GetDeveloperStudioConfig
from core.product_core.local_ocr_provider_settings import RunConfiguredLocalOcrProviderForSource
from core.product_core.media_processing_queue import CreateMediaProcessingQueueJob
from core.product_core.media_processing_queue import (
    MediaProcessingQueueResult,
    serialize_media_processing_queue_result,
)
from core.product_core.source_file_authorization import (
    AuthorizeLocalAudioFileForSource,
    AuthorizeLocalDocumentFileForSource,
    AuthorizeLocalImageFileForSource,
    AuthorizeLocalTextFileForSource,
    AuthorizeLocalVideoFileForSource,
    SourceFileAuthorizationResult,
    serialize_source_file_authorization_result,
)
from core.product_core.source_output_memory_candidate import (
    CreateMemoryCandidateFromSourceOutput,
    SourceOutputMemoryCandidateResult,
    serialize_source_output_memory_candidate,
)
from core.product_core.transcript_summary_adapter import (
    SummarizeTranscriptOutput,
    TranscriptSummaryResult,
    serialize_transcript_summary_result,
)
from core.product_core.workbench_auto_intake import (
    OrchestrateWorkbenchAutoIntake,
    ServeWorkbenchAutoIntakeEndpoint,
    WorkbenchAutoIntakeEndpointResponse,
)
from core.storage_provider import SQLiteStructuredRecordStore
from core.effect_log import EffectClass, EffectWorkflowHandler


class WorkbenchAutoIntakeContainerPort(WorkbenchInputClassifierContainerPort, Protocol):
    """Backend composition dependencies; deliberately independent of FastAPI."""


class ApplicationStatePort(Protocol):
    state: object


_URL_TEXT_NETWORK_ADAPTER = SafeTextNetworkAdapter()
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


@dataclass(frozen=True, slots=True)
class WorkbenchAutoIntakeRuntime:
    """Compose the auto-intake application service without reimplementing its flow."""

    runtime_root: Path
    object_store: object
    namespace_id: str
    container: WorkbenchAutoIntakeContainerPort
    application: ApplicationStatePort

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
    ) -> WorkbenchAutoIntakeEndpointResponse:
        project_id = body.get("project_id", "default") if isinstance(body, Mapping) else "default"
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            return WorkbenchAutoIntakeEndpointResponse(
                status_code=400, body={"detail": "invalid project_id"},
                headers={"Cache-Control": "no-store"},
            )
        classifier_runtime = build_workbench_input_classifier_runtime(
            self.container,
            self.object_store,
        )
        recipe_trace = classifier_runtime.recipe_preflight(
            body,
            trigger="workbench.auto-intake",
        )
        classifier_prompt = classifier_runtime.active_prompt()
        # Auto Intake owns deterministic Source/Job orchestration, not model
        # approval. Provider enhancement is obtained through the scoped AI
        # Turn endpoint before this domain workflow is submitted.
        orchestrator = self._orchestrator(None, project_id=project_id)
        response = ServeWorkbenchAutoIntakeEndpoint().execute(
            method=method,
            path=path,
            body=body,
            orchestrate=lambda **kwargs: orchestrator.execute(
                **kwargs,
                classifier_prompt=classifier_prompt,
            ),
        )
        return _with_review_ids(_with_recipe_trace(response, recipe_trace), self.runtime_root)

    def _orchestrator(self, enhance_classification: Callable[..., object], *, project_id: str = "default") -> OrchestrateWorkbenchAutoIntake:
        store = self.object_store
        audio_workflow = AudioAutoEffectRuntime(
            store,
            namespace_id=self.namespace_id,
            effect_runner=self.application.state.effect_runtime.runner,
            gate_decision_id="workbench-auto-intake:v1",
        )
        long_audio_workflow = LongAudioEffectRuntime(
            store,
            namespace_id=self.namespace_id,
            effect_runner=self.application.state.effect_runtime.runner,
            audio_runtime=audio_workflow,
            split_runner=split_local_audio_chunk,
        )
        transcript_summarizer = SummarizeTranscriptOutput(store, namespace_id=self.namespace_id)
        memory_candidate_creator = CreateMemoryCandidateFromSourceOutput(store, namespace_id=self.namespace_id)
        effects = EffectWorkflowHandler(
            self.application.state.effect_runtime.runner,
            store,
            namespace_id=self.namespace_id,
        )
        routed_jobs = build_rebuild_job_repository(self.runtime_root, store)
        review_admission = ReviewIntentAdmission(
            SQLiteStructuredRecordStore(self.runtime_root / ".rebuild-data" / "structured-records.sqlite3"),
            store,
        )

        def admit_review_intents(job_id: str, source_id: str, items: Sequence[object]) -> None:
            review_admission.admit(
                job_id,
                [source_id, *(str(item.source_id) for item in items)],
                existing_job_ok=True,
            )

        def produce_candidate_jobs(parent_job_id: str, items: Sequence[object], now: str):
            return self._produce_candidate_jobs(routed_jobs, parent_job_id, items, now)

        def prepare_managed_file(source_id: str, asset_ref: str):
            return run_effect(
                "prepare_file", source_id,
                lambda: self._prepare_managed_file(source_id, asset_ref),
                serialize_source_file_authorization_result,
                lambda value: SourceFileAuthorizationResult(**dict(value)),
            )

        def prepare_managed_video(source_id: str, asset_ref: str):
            return run_effect(
                "prepare_video", source_id,
                lambda: self._prepare_managed_video(source_id, asset_ref),
                serialize_source_file_authorization_result,
                lambda value: SourceFileAuthorizationResult(**dict(value)),
            )

        def run_effect(step: str, source_id: str, invoke, encode, decode):
            return effects.execute(
                operation_id=f"workbench-auto:{source_id}:{step}",
                session_id=f"workbench-auto:{source_id}",
                root_id=f"workbench-auto:{source_id}",
                step_key=step,
                kind=f"workbench_auto_{step}",
                intent_ref=(
                    f"crp://{self.namespace_id}/workflow-intents/"
                    f"workbench-auto/{source_id}/{step}"
                ),
                gate_decision_id="workbench-auto-intake:v1",
                rev_set={"workflow_revision": "1", "handler_revision": f"{step}-v1"},
                payload={"source_id": source_id, "step": step},
                effect_class=EffectClass.IDEMPOTENT,
                invoke=invoke,
                encode=encode,
                decode=decode,
            )

        def fetch_url(url: str) -> str:
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
            return effects.execute(
                operation_id=f"workbench-auto-url:{identity}:fetch",
                session_id=f"workbench-auto-url:{identity}",
                root_id=f"workbench-auto-url:{identity}",
                step_key="fetch_url",
                kind="workbench_auto_fetch_url",
                intent_ref=(
                    f"crp://{self.namespace_id}/workflow-intents/"
                    f"workbench-auto-url/{identity}/fetch"
                ),
                gate_decision_id="workbench-auto-intake:v1",
                rev_set={"workflow_revision": "2", "handler_revision": "safe-text-fetch-v1"},
                payload={"url_identity": identity},
                effect_class=EffectClass.QUERYABLE,
                invoke=lambda: _URL_TEXT_NETWORK_ADAPTER.fetch_text(url),
                encode=lambda value: {"text": value},
                decode=lambda value: str(value["text"]),
            )

        def run_image_ocr(source_id: str) -> MediaProcessingQueueResult:
            return run_effect(
                "image_ocr", source_id,
                lambda: self._run_image_ocr(source_id),
                serialize_media_processing_queue_result,
                lambda value: MediaProcessingQueueResult(
                    **{
                        **dict(value),
                        "input_refs": tuple(value.get("input_refs", ())),
                        "expected_output_refs": tuple(value.get("expected_output_refs", ())),
                        "activity_refs": tuple(value.get("activity_refs", ())),
                        "output_refs": tuple(value.get("output_refs", ())),
                    }
                ),
            )

        def transcribe(**kwargs: object):
            source_id = str(kwargs.pop("source_id", "unknown"))
            return audio_workflow.transcribe(source_id, **kwargs)

        def summarize(**kwargs: object) -> TranscriptSummaryResult:
            source_id = str(kwargs.pop("source_id", "unknown"))
            return run_effect(
                "summarize_transcript", source_id, lambda: transcript_summarizer.execute(**kwargs),
                serialize_transcript_summary_result,
                lambda value: TranscriptSummaryResult(
                    **{**dict(value), "candidate_ids": tuple(value.get("candidate_ids", ()))},
                ),
            )

        def create_candidate(**kwargs: object) -> SourceOutputMemoryCandidateResult:
            source_id = str(kwargs.pop("source_id", "unknown"))
            return run_effect(
                "create_memory_candidate", source_id,
                lambda: memory_candidate_creator.execute_from_media_output(**kwargs),
                serialize_source_output_memory_candidate,
                lambda value: SourceOutputMemoryCandidateResult(
                    **{
                        **dict(value),
                        "source_refs_display": tuple(value.get("source_refs_display", ())),
                        "blocked_operations": tuple(value.get("blocked_operations", ())),
                    },
                ),
            )

        def run_audio_workflow(source_id: str, audio_asset_id: str | None):
            return audio_workflow.execute(
                source_id=source_id,
                project_id=project_id,
                audio_asset_id=audio_asset_id,
            )

        def run_long_audio_workflow(source_id: str, audio_asset_id: str | None):
            if audio_asset_id is None:
                raise ValueError("audio asset id is required for long audio")
            return long_audio_workflow.execute(
                source_id=source_id,
                project_id=project_id,
                audio_asset_id=audio_asset_id,
            )

        def admit_content_transform(job: Mapping[str, object], items: Sequence[object]):
            return admit_workbench_content_transform(
                database_path=routed_jobs.sqlite.database_path,
                runtime_root=self.runtime_root,
                object_store=store,
                job_payload=job,
                items=items,
            )

        return OrchestrateWorkbenchAutoIntake(
            object_store=store,
            source_registrar=ObjectStoreSourceRegistrar(store, namespace_id=self.namespace_id, project_id=project_id),
            job_repository=ReviewIntentJobRepository(routed_jobs, review_admission),
            fetch_url=fetch_url,
            namespace_id=self.namespace_id,
            project_id=project_id,
            enhance_classification=enhance_classification,
            run_document_text_extractor=None,
            run_image_ocr=run_image_ocr,
            run_audio_auto_workflow=run_audio_workflow,
            run_video_auto_workflow=None,
            prepare_file_source=prepare_managed_file,
            prepare_video_source=prepare_managed_video,
            run_long_audio_chunked_workflow=run_long_audio_workflow,
            audio_summarize_transcript=summarize,
            audio_create_memory_candidate=create_candidate,
            task_model_map=GetDeveloperStudioConfig(store).execute().task_model_map,
            produce_candidate_jobs=produce_candidate_jobs,
            admit_content_transform=admit_content_transform,
            admit_review_intents=admit_review_intents,
        )

    def _run_image_ocr(self, source_id: str):
        CreateMediaProcessingQueueJob(self.object_store, enabled_capabilities=("ocr",)).execute(
            source_id=source_id
        )
        return RunConfiguredLocalOcrProviderForSource(
            self.object_store,
            namespace_id=self.namespace_id,
        ).execute(source_id=source_id)

    def _prepare_managed_file(self, source_id: str, asset_ref: str):
        resolved = build_original_asset_resolver(self.runtime_root, self.object_store).for_source(source_id)
        asset = (
            self.object_store.read("workbench_original_assets", resolved.asset_id)
            if resolved is not None
            else None
        )
        if resolved is None or asset is None or asset.get("asset_ref") != asset_ref:
            raise ValueError("linked managed original asset does not match the upload")
        if resolved.status != "available" or resolved.path is None:
            raise ValueError(f"managed original asset is unavailable: {resolved.reason}")
        return self._authorize_source_file(source_id=source_id, file_path=str(resolved.path))

    def _prepare_managed_video(self, source_id: str, asset_ref: str):
        asset = next(
            (
                item
                for item in self.object_store.list("workbench_original_assets")
                if item.get("asset_ref") == asset_ref
            ),
            None,
        )
        if asset is None or asset.get("status") != "stored":
            raise ValueError("stored original video asset not found")
        vault_ref = asset.get("vault_ref")
        relative = Path(vault_ref) if isinstance(vault_ref, str) else Path()
        if (
            not vault_ref
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("stored original video vault reference is invalid")
        library_root = (self.runtime_root / "library").resolve(strict=False)
        stored_path = (library_root / relative).resolve(strict=True)
        if library_root not in stored_path.parents or not stored_path.is_file():
            raise ValueError("stored original video escapes the active Vault")
        resolved = build_original_asset_resolver(
            self.runtime_root,
            self.object_store,
        ).for_source(source_id)
        if resolved is None or resolved.path != stored_path:
            raise ValueError("linked managed original asset does not match the upload")
        if resolved.status != "available":
            raise ValueError(f"managed original asset is unavailable: {resolved.reason}")
        return AuthorizeLocalVideoFileForSource(
            self.object_store,
            namespace_id=self.namespace_id,
        ).execute(source_id=source_id, file_path=str(stored_path))

    def _authorize_source_file(self, *, source_id: str, file_path: str):
        source = self.object_store.read("sources", source_id)
        source_type = str((source or {}).get("type") or "").strip().lower()
        media_type = str((source or {}).get("media_type") or "").strip().lower()
        if source_type == "image":
            authorizer = AuthorizeLocalImageFileForSource
        elif source_type == "audio":
            authorizer = AuthorizeLocalAudioFileForSource
        elif source_type == "video":
            authorizer = AuthorizeLocalVideoFileForSource
        elif media_type in {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }:
            authorizer = AuthorizeLocalDocumentFileForSource
        else:
            authorizer = AuthorizeLocalTextFileForSource
        return authorizer(self.object_store, namespace_id=self.namespace_id).execute(
            source_id=source_id,
            file_path=file_path,
        )

    def _produce_candidate_jobs(
        self,
        repository: RoutedJobRepository,
        parent_job_id: str,
        items: Sequence[object],
        now: str,
    ) -> tuple[Mapping[str, object], ...]:
        if "extract_memory_candidate" not in repository.sqlite_job_types:
            return ()
        admission_command = SQLiteJobAdmissionCommand(repository.sqlite.database_path)
        read_object = getattr(self.object_store, "read", None)
        read_revision = getattr(self.object_store, "revision", None)
        if not callable(read_object) or not callable(read_revision):
            raise RuntimeError("candidate v2 admission requires durable object evidence")
        produced: list[Mapping[str, object]] = []
        for item in items:
            auto_organization = getattr(item, "auto_organization", {})
            content_read_id = (
                auto_organization.get("content_read_id")
                if isinstance(auto_organization, Mapping)
                else None
            )
            if (
                getattr(item, "content_read_status", None) != "completed"
                or not isinstance(content_read_id, str)
                or not content_read_id
            ):
                continue
            source_id = str(getattr(item, "source_id"))
            source = read_object("sources", source_id)
            project_id = source.get("project_id") if isinstance(source, Mapping) else None
            if not isinstance(project_id, str) or not project_id:
                raise RuntimeError("candidate v2 admission requires Source project ownership")
            evidence = read_object("source_content_reads", content_read_id)
            evidence_created_at = (
                evidence.get("created_at") if isinstance(evidence, Mapping) else None
            )
            if (
                not isinstance(evidence_created_at, str)
                or not evidence_created_at
                or evidence_created_at != evidence_created_at.strip()
            ):
                raise RuntimeError("candidate v2 admission requires immutable evidence time")
            candidate = dict(
                build_candidate_memory_job(
                    CandidateMemoryJobInput(
                        parent_job_id=parent_job_id,
                        source_id=source_id,
                        project_id=project_id,
                        evidence_kind="source_content_read",
                        evidence_id=content_read_id,
                    ),
                    now=evidence_created_at,
                )
            )
            evidence_id = str(candidate["evidence_id"])
            source_revision = read_revision("sources", source_id)
            evidence_revision = read_revision("source_content_reads", evidence_id)
            admitted = CandidateJobAdmissionFactory(
                admitted_at=int(time.time()),
            ).build(
                job_payload=candidate,
                source_record=source,
                source_revision=source_revision,
                source_content_read_record=evidence,
                source_content_read_revision=evidence_revision,
            )

            def revalidate_evidence(
                _connection,
                _replay: bool,
                *,
                expected_source=source,
                expected_evidence=evidence,
                expected_source_revision=source_revision,
                expected_evidence_revision=evidence_revision,
                expected_source_id=source_id,
                expected_evidence_id=evidence_id,
            ) -> None:
                if (
                    read_object("sources", expected_source_id) != expected_source
                    or read_object("source_content_reads", expected_evidence_id) != expected_evidence
                    or read_revision("sources", expected_source_id) != expected_source_revision
                    or read_revision("source_content_reads", expected_evidence_id) != expected_evidence_revision
                ):
                    raise RuntimeError("candidate admission evidence drifted before commit")

            admission_command.admit(
                payload=candidate,
                authorization=admitted.authorization,
                intent=admitted.intent,
                preflight=revalidate_evidence,
            )
            produced.append(candidate)
        return tuple(produced)


def build_workbench_auto_intake_runtime(
    container: WorkbenchAutoIntakeContainerPort,
    object_store: object,
    *,
    namespace_id: str,
    application: ApplicationStatePort,
) -> WorkbenchAutoIntakeRuntime:
    return WorkbenchAutoIntakeRuntime(
        runtime_root=container.root_dir,
        object_store=object_store,
        namespace_id=namespace_id,
        container=container,
        application=application,
    )


def _with_recipe_trace(
    response: WorkbenchAutoIntakeEndpointResponse,
    trace: Mapping[str, object] | None,
) -> WorkbenchAutoIntakeEndpointResponse:
    body = dict(response.body)
    if trace is not None:
        body["processing_recipe_trace"] = dict(trace)
    return WorkbenchAutoIntakeEndpointResponse(
        status_code=response.status_code,
        body=body,
        headers=response.headers,
    )


def _with_review_ids(
    response: WorkbenchAutoIntakeEndpointResponse,
    runtime_root: Path,
) -> WorkbenchAutoIntakeEndpointResponse:
    if response.body.get("status") != "accepted":
        return response
    items = response.body.get("items")
    if not isinstance(items, list) or not items:
        return response
    records = SQLiteStructuredRecordStore(runtime_root / ".rebuild-data" / "structured-records.sqlite3")
    enriched: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("source_id"), str):
            raise ValueError("accepted intake item lacks Source identity")
        row = records.read("workspace_review_intents", f"review-{item['source_id']}")
        if row is None or row.payload.get("source_id") != item["source_id"]:
            raise ValueError("accepted intake item lacks durable review intent")
        enriched.append({**item, "review_item_id": row.object_id,
                         "project_id": row.payload["project_id"]})
    body = {**response.body, "items": enriched, "review_item_id": enriched[0]["review_item_id"],
            "project_id": enriched[0]["project_id"]}
    return WorkbenchAutoIntakeEndpointResponse(
        status_code=response.status_code, body=body, headers=response.headers,
    )
