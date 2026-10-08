"""Xiaohongshu image-set and video Media Hands operation.

The provider consumes an immutable, already granted SourceManifest.  Transient
CDN locators and local staging paths stay below this boundary: recipe receipts
contain only CRP references and state hashes, while the final output is the
existing Document authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time

from backend.api.governed_local_asr import GovernedLocalAsrOutcome
from backend.api.governed_local_ocr import GovernedLocalOcrOutcome
from backend.api.governed_staged_video import GovernedStagedVideoOutcome
from backend.api.xiaohongshu_asset_materializer import XiaohongshuAssetMaterializer, XiaohongshuMaterializationOutcome, XiaohongshuStagedAsset
from backend.api.xiaohongshu_asset_analysis_journal import XiaohongshuAssetAnalysisJournal, XiaohongshuAssetAnalysisRecord
from backend.api.xiaohongshu_staging_journal import XiaohongshuStagingJournal
from backend.api.xiaohongshu_video_derivative_journal import XiaohongshuVideoDerivativeJournal
from core.document_engine import DocumentDraft, DocumentRepositoryPort
from core.job_runner.media_execution_receipt import media_job_uri_segment
from core.media_hands import MediaOperationReceipt, MediaOperationRequest
from core.source_processing import SourceManifest, SourceManifestArtifactRepository


_BUDGET_FIELDS = frozenset({
    "max_download_bytes", "max_media_cpu_ms", "max_asr_audio_ms",
    "max_vision_frames", "max_model_input_tokens", "max_model_output_tokens", "max_wall_ms",
})


class XiaohongshuMediaOperationError(ValueError):
    """Stable, receipt-safe media operation failure."""


@dataclass(frozen=True, slots=True)
class XiaohongshuMediaOperationProvider:
    artifacts: SourceManifestArtifactRepository
    documents: DocumentRepositoryPort
    materializer: XiaohongshuAssetMaterializer
    journal: XiaohongshuStagingJournal
    ocr: object
    now: Callable[[], str]
    monotonic: Callable[[], float] = time.monotonic
    video_runner: object | None = None
    video_journal: XiaohongshuVideoDerivativeJournal | None = None
    local_asr: object | None = None
    analysis_journal: XiaohongshuAssetAnalysisJournal | None = None

    provider_id = "xiaohongshu-media-analysis"
    provider_revision = "xiaohongshu-media-analysis-r3"
    supported_platforms = frozenset({"xiaohongshu"})
    supports_durable_recipe_resume = True

    def execute(self, request: MediaOperationRequest) -> MediaOperationReceipt:
        """Dispatch only a frozen Xiaohongshu image or video manifest.

        The dispatch read is deliberately side-effect free.  Each concrete
        recipe repeats its binding proof immediately before staging so a
        permission/revision change cannot be hidden by the router.
        """
        self._validate_request(request)
        artifact = self.artifacts.resolve_source_ref(
            source_ref=request.manifest_ref, project_id=request.permission_snapshot.project_id,
        )
        manifest = artifact.manifest
        if (
            artifact.public_ref != request.manifest_ref
            or artifact.revision != request.manifest_revision
            or manifest.source_id != request.source_id
            or manifest.platform != "xiaohongshu"
            or manifest.permission.decision != "granted"
            or request.permission_snapshot.manifest_ref != request.manifest_ref
            or request.permission_snapshot.manifest_revision != request.manifest_revision
        ):
            raise XiaohongshuMediaOperationError("frozen_manifest_drift")
        if manifest.content_kind == "image_set":
            return self._execute_image(request)
        if manifest.content_kind == "video":
            return self._execute_video(request, manifest)
        if manifest.content_kind == "mixed":
            return self._execute_mixed(request, manifest)
        raise XiaohongshuMediaOperationError("unsupported_xiaohongshu_media")

    def _execute_image(self, request: MediaOperationRequest) -> MediaOperationReceipt:
        self._validate_request(request)
        artifact = self.artifacts.resolve_source_ref(
            source_ref=request.manifest_ref, project_id=request.permission_snapshot.project_id,
        )
        manifest = artifact.manifest
        if (
            artifact.public_ref != request.manifest_ref
            or artifact.revision != request.manifest_revision
            or manifest.source_id != request.source_id
            or manifest.platform != "xiaohongshu"
            or manifest.permission.decision != "granted"
            or request.permission_snapshot.manifest_ref != request.manifest_ref
            or request.permission_snapshot.manifest_revision != request.manifest_revision
        ):
            raise XiaohongshuMediaOperationError("frozen_manifest_drift")
        if manifest.content_kind != "image_set" or not manifest.assets or any(
            item.kind != "image" or item.ordinal != ordinal for ordinal, item in enumerate(manifest.assets)
        ):
            raise XiaohongshuMediaOperationError("unsupported_xiaohongshu_media")
        if request.budget["max_vision_frames"] < len(manifest.assets):
            raise XiaohongshuMediaOperationError("vision_budget_exhausted")

        started = self.monotonic()
        self._checkpoint(request)
        stage_input = _state_hash({
            "step": "stage_images", "manifest_ref": request.manifest_ref,
            "manifest_revision": request.manifest_revision,
            "assets": [{"asset_id": item.asset_id, "ordinal": item.ordinal} for item in manifest.assets],
        })
        recovered = self._begin(request, "stage_images", stage_input)
        recovered_stage_wall_ms = 0
        if recovered is not None:
            recovered_ref = recovered.get("output_ref")
            recovered_hash = recovered.get("output_state_hash")
            if not isinstance(recovered_ref, str) or not isinstance(recovered_hash, str):
                raise XiaohongshuMediaOperationError("stage_images_receipt_invalid")
            staged = self.journal.restore(
                job_id=request.job_id, manifest_ref=request.manifest_ref,
                manifest_revision=request.manifest_revision,
                receipt={"output_ref": recovered_ref, "state_hash": recovered_hash},
            )
            recovered_consumed = recovered.get("consumed")
            download_octets = (
                recovered_consumed.get("download_octets")
                if isinstance(recovered_consumed, Mapping) else None
            )
            recovered_stage_wall_ms = (
                recovered_consumed.get("wall_milliseconds")
                if isinstance(recovered_consumed, Mapping) else None
            )
            if (
                not isinstance(download_octets, int)
                or isinstance(download_octets, bool)
                or download_octets < sum(item.byte_count for item in staged)
                or download_octets > request.budget["max_download_bytes"]
                or not isinstance(recovered_stage_wall_ms, int)
                or isinstance(recovered_stage_wall_ms, bool)
                or recovered_stage_wall_ms < 0
                or recovered_stage_wall_ms > request.budget["max_wall_ms"]
            ):
                raise XiaohongshuMediaOperationError("stage_images_receipt_invalid")
        else:
            try:
                materialized = self.materializer.materialize(
                    manifest, job_id=request.job_id,
                    max_download_bytes=request.budget["max_download_bytes"],
                    timeout_seconds=self._remaining_wall(request, started) / 1000,
                    project_id=request.permission_snapshot.project_id,
                    control_check=(request.control.checkpoint if request.control is not None else None),
                )
                if not isinstance(materialized, XiaohongshuMaterializationOutcome):
                    raise XiaohongshuMediaOperationError("stage_images_output_invalid")
                staged = materialized.assets
                download_octets = materialized.total_download_bytes
                if (
                    not isinstance(download_octets, int)
                    or isinstance(download_octets, bool)
                    or download_octets < sum(item.byte_count for item in staged)
                    or download_octets > request.budget["max_download_bytes"]
                ):
                    raise XiaohongshuMediaOperationError("stage_images_consumption_invalid")
                checkpoint = self.journal.commit(
                    job_id=request.job_id, manifest_ref=request.manifest_ref,
                    manifest_revision=request.manifest_revision, assets=staged,
                )
                self._complete(request, "stage_images", checkpoint.output_ref, checkpoint.state_hash, {
                    "download_octets": download_octets,
                    "wall_milliseconds": self._elapsed(started),
                })
            except Exception:
                self._unknown(request, "stage_images")
                raise
        if len(staged) != len(manifest.assets) or any(
            item.asset_id != expected.asset_id or item.ordinal != expected.ordinal
            or item.kind != "image" or item.staged_path is None
            for item, expected in zip(staged, manifest.assets, strict=True)
        ):
            raise XiaohongshuMediaOperationError("staged_images_drifted")
        stage_checkpoint = self.journal.checkpoint(job_id=request.job_id)
        self._checkpoint(request)

        ocr_input = _state_hash({
            "step": "ocr_document", "staging_ref": stage_checkpoint.output_ref,
            "staging_state_hash": stage_checkpoint.state_hash,
            "manifest_ref": request.manifest_ref, "manifest_revision": request.manifest_revision,
            "assets": [{"asset_id": item.asset_id, "ordinal": item.ordinal} for item in staged],
            "ocr_provider_revision": _provider_revision(self.ocr),
        })
        recovered_ocr = self._begin(request, "ocr_document", ocr_input)
        recovered_ocr_wall_ms = 0
        media_cpu_ms = 0
        if recovered_ocr is not None:
            document = self._restore_document(recovered_ocr, request)
            recovered_consumed = recovered_ocr.get("consumed")
            recovered_vision_frames = (
                recovered_consumed.get("vision_frames")
                if isinstance(recovered_consumed, Mapping) else None
            )
            recovered_ocr_wall_ms = (
                recovered_consumed.get("wall_milliseconds")
                if isinstance(recovered_consumed, Mapping) else None
            )
            media_cpu_ms = (
                recovered_consumed.get("media_cpu_milliseconds")
                if isinstance(recovered_consumed, Mapping) else None
            )
            if (
                recovered_vision_frames != len(staged)
                or isinstance(recovered_vision_frames, bool)
                or not isinstance(media_cpu_ms, int)
                or isinstance(media_cpu_ms, bool)
                or media_cpu_ms < 0
                or media_cpu_ms > request.budget["max_media_cpu_ms"]
                or not isinstance(recovered_ocr_wall_ms, int)
                or isinstance(recovered_ocr_wall_ms, bool)
                or recovered_ocr_wall_ms < recovered_stage_wall_ms
                or recovered_ocr_wall_ms > request.budget["max_wall_ms"]
            ):
                raise XiaohongshuMediaOperationError("ocr_step_receipt_invalid")
        else:
            try:
                texts: list[str] = []
                for item in staged:
                    self._checkpoint(request)
                    remaining_cpu_ms = request.budget["max_media_cpu_ms"] - media_cpu_ms
                    if remaining_cpu_ms < 1:
                        raise XiaohongshuMediaOperationError("media_cpu_budget_exhausted")
                    outcome = self._ocr(
                        item,
                        remaining_wall_ms=self._remaining_wall(
                            request, started, prior_consumed_ms=recovered_stage_wall_ms,
                        ),
                        remaining_media_cpu_ms=remaining_cpu_ms,
                        request=request,
                    )
                    media_cpu_ms += outcome.wall_ms
                    texts.append(outcome.text)
                document = self._create_document(request=request, manifest=manifest, staged=staged, ocr_texts=tuple(texts))
                self._complete(request, "ocr_document", _required_str(document, "markdown_uri"),
                               _document_state_hash(document, request), {
                    "vision_frames": len(staged),
                    "media_cpu_milliseconds": media_cpu_ms,
                    "wall_milliseconds": recovered_stage_wall_ms + self._elapsed(started),
                })
            except Exception:
                self._unknown(request, "ocr_document")
                raise
        elapsed = (
            recovered_ocr_wall_ms
            if recovered_ocr is not None
            else recovered_stage_wall_ms + self._elapsed(started)
        )
        if elapsed > request.budget["max_wall_ms"]:
            raise XiaohongshuMediaOperationError("wall_budget_exhausted")
        consumed = {key: 0 for key in request.budget}
        consumed["max_download_bytes"] = download_octets
        consumed["max_media_cpu_ms"] = media_cpu_ms
        consumed["max_vision_frames"] = len(staged)
        consumed["max_wall_ms"] = elapsed
        if any(consumed[key] > request.budget[key] for key in consumed):
            raise XiaohongshuMediaOperationError("media_budget_exhausted")
        job_segment = media_job_uri_segment(request.job_id)
        doc_id = _required_str(document, "id")
        return MediaOperationReceipt(
            output={"kind": "document", "uri": _required_str(document, "markdown_uri"), "object_id": doc_id, "published": True},
            checkpoint={"resume_step": "execute_operation", "checkpoint_uri": f"crp://{self.artifacts.namespace_id}/jobs/{job_segment}/checkpoints/xiaohongshu-image-analysis-r1", "state_hash": _state_hash({"document_id": doc_id, "staging_state_hash": stage_checkpoint.state_hash, "consumed": consumed}), "updated_at": self.now()},
            consumed=consumed,
            execution_receipt_ref=f"crp://{self.artifacts.namespace_id}/jobs/{job_segment}/receipts/xiaohongshu-image-analysis-r1",
        )

    def _execute_video(self, request: MediaOperationRequest, manifest: SourceManifest) -> MediaOperationReceipt:
        """Run the fixed video recipe from one staged, frozen Manifest asset."""
        artifact = self.artifacts.resolve_source_ref(
            source_ref=request.manifest_ref,
            project_id=request.permission_snapshot.project_id,
        )
        manifest = artifact.manifest
        if (
            artifact.public_ref != request.manifest_ref
            or artifact.revision != request.manifest_revision
            or manifest.source_id != request.source_id
            or manifest.platform != "xiaohongshu"
            or manifest.permission.decision != "granted"
            or request.permission_snapshot.manifest_ref != request.manifest_ref
            or request.permission_snapshot.manifest_revision != request.manifest_revision
        ):
            raise XiaohongshuMediaOperationError("frozen_manifest_drift")
        if (
            len(manifest.assets) != 1
            or manifest.assets[0].kind != "video"
            or manifest.assets[0].ordinal != 0
            or manifest.assets[0].media_type != "video/mp4"
        ):
            raise XiaohongshuMediaOperationError("unsupported_xiaohongshu_media")
        if self.video_runner is None or self.video_journal is None or self.local_asr is None:
            raise XiaohongshuMediaOperationError("xiaohongshu_video_dependencies_unavailable")
        if request.budget["max_asr_audio_ms"] < 1 or request.budget["max_vision_frames"] < 1:
            raise XiaohongshuMediaOperationError("video_budget_exhausted")
        runner_max_frames = getattr(self.video_runner, "max_frames", None)
        if (
            not isinstance(runner_max_frames, int)
            or isinstance(runner_max_frames, bool)
            or runner_max_frames < 1
            or runner_max_frames > request.budget["max_vision_frames"]
        ):
            raise XiaohongshuMediaOperationError("vision_budget_exhausted")
        assert_ready = getattr(self.local_asr, "assert_ready", None)
        if not callable(assert_ready):
            raise XiaohongshuMediaOperationError("local_asr_unavailable")
        assert_ready()

        started = self.monotonic()
        staged, stage_checkpoint, download_octets, stage_wall_ms = self._stage_video_asset(request, manifest, started)
        self._checkpoint(request)
        source_asset = manifest.assets[0]
        if (
            staged.asset_id != source_asset.asset_id or staged.ordinal != 0
            or staged.kind != "video" or staged.media_type != "video/mp4"
            or not isinstance(staged.staged_path, str)
        ):
            raise XiaohongshuMediaOperationError("staged_video_drifted")

        derive_input = _state_hash({
            "step": "derive_video", "staging_ref": stage_checkpoint.output_ref,
            "staging_state_hash": stage_checkpoint.state_hash,
            "source_asset_id": source_asset.asset_id,
            "video_provider_revision": _provider_revision(self.video_runner),
        })
        recovered = self._begin(request, "derive_video", derive_input)
        if recovered is not None:
            derived = self._restore_derivatives(recovered, request, source_asset.asset_id)
            derivative_cpu_ms, derive_wall_ms = self._receipt_ints(
                recovered, "derive_video", ("media_cpu_milliseconds", "wall_milliseconds"),
                minimums=(0, stage_wall_ms),
            )
            if derived.wall_ms != derivative_cpu_ms:
                raise XiaohongshuMediaOperationError("derive_video_receipt_invalid")
        else:
            try:
                derive = getattr(self.video_runner, "derive", None)
                if not callable(derive):
                    raise XiaohongshuMediaOperationError("governed_video_unavailable")
                outcome = derive(
                    Path(staged.staged_path), job_id=request.job_id, media_type=staged.media_type,
                    remaining_wall_ms=self._remaining_consumed(
                        request.budget["max_wall_ms"], stage_wall_ms, "wall_budget_exhausted"
                    ),
                    remaining_media_cpu_ms=request.budget["max_media_cpu_ms"],
                    control_check=(request.control.checkpoint if request.control else None),
                )
                if not isinstance(outcome, GovernedStagedVideoOutcome) or outcome.wall_ms > request.budget["max_media_cpu_ms"]:
                    raise XiaohongshuMediaOperationError("video_derivative_output_invalid")
                checkpoint = self.video_journal.commit(
                    job_id=request.job_id, manifest_ref=request.manifest_ref,
                    manifest_revision=request.manifest_revision, source_id=request.source_id,
                    source_asset_id=source_asset.asset_id, outcome=outcome,
                )
                derived = outcome
                derivative_cpu_ms = outcome.wall_ms
                derive_wall_ms = stage_wall_ms + outcome.wall_ms
                self._complete(request, "derive_video", checkpoint.output_ref, checkpoint.state_hash, {
                    "media_cpu_milliseconds": derivative_cpu_ms,
                    "wall_milliseconds": derive_wall_ms,
                })
            except Exception:
                self._unknown(request, "derive_video")
                raise
        if not derived.frames or len(derived.frames) > request.budget["max_vision_frames"]:
            raise XiaohongshuMediaOperationError("vision_budget_exhausted")

        audio_ref = self.video_journal.checkpoint(job_id=request.job_id).output_ref + "/audio"
        probe_input = _state_hash({
            "step": "probe_audio", "derivative_ref": audio_ref,
            "derivative_state_hash": self.video_journal.checkpoint(job_id=request.job_id).state_hash,
            "max_asr_audio_ms": request.budget["max_asr_audio_ms"],
            "asr_provider_revision": _provider_revision(self.local_asr),
        })
        recovered_probe = self._begin(request, "probe_audio", probe_input)
        if recovered_probe is not None:
            duration_ms, probe_wall_ms = self._receipt_ints(
                recovered_probe, "probe_audio", ("audio_milliseconds", "wall_milliseconds"),
                minimums=(1, derive_wall_ms),
            )
            if duration_ms > request.budget["max_asr_audio_ms"] or recovered_probe.get("output_ref") != audio_ref or recovered_probe.get("output_state_hash") != _state_hash({"audio_ref": audio_ref, "duration_ms": duration_ms}):
                raise XiaohongshuMediaOperationError("probe_audio_receipt_invalid")
        else:
            try:
                probe = getattr(self.local_asr, "probe_duration", None)
                if not callable(probe):
                    raise XiaohongshuMediaOperationError("local_asr_unavailable")
                probe_started = self.monotonic()
                duration_ms = probe(
                    Path(derived.audio.staged_path), max_audio_ms=request.budget["max_asr_audio_ms"],
                    max_wall_ms=self._remaining_consumed(
                        request.budget["max_wall_ms"], derive_wall_ms, "wall_budget_exhausted"
                    ),
                    control_check=(request.control.checkpoint if request.control else None),
                )
                if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or not 1 <= duration_ms <= request.budget["max_asr_audio_ms"]:
                    raise XiaohongshuMediaOperationError("probe_audio_output_invalid")
                probe_wall_ms = derive_wall_ms + self._elapsed(probe_started)
                self._complete(request, "probe_audio", audio_ref, _state_hash({"audio_ref": audio_ref, "duration_ms": duration_ms}), {
                    "audio_milliseconds": duration_ms, "wall_milliseconds": probe_wall_ms,
                })
            except Exception:
                self._unknown(request, "probe_audio")
                raise

        analysis_input = _state_hash({
            "step": "analyze_video_document", "audio_ref": audio_ref, "duration_ms": duration_ms,
            "derivative_state_hash": self.video_journal.checkpoint(job_id=request.job_id).state_hash,
            "asr_provider_revision": _provider_revision(self.local_asr),
            "ocr_provider_revision": _provider_revision(self.ocr),
            "frames": [{"ordinal": item.ordinal, "media_type": item.media_type} for item in derived.frames],
        })
        recovered_analysis = self._begin(request, "analyze_video_document", analysis_input)
        if recovered_analysis is not None:
            document = self._restore_document(recovered_analysis, request)
            asr_audio_ms, vision_frames, media_cpu_ms, analysis_wall_ms = self._receipt_ints(
                recovered_analysis, "analyze_video_document",
                ("audio_milliseconds", "vision_frames", "media_cpu_milliseconds", "wall_milliseconds"),
                minimums=(duration_ms, len(derived.frames), derivative_cpu_ms, probe_wall_ms),
            )
            if asr_audio_ms != duration_ms or vision_frames != len(derived.frames):
                raise XiaohongshuMediaOperationError("analyze_video_receipt_invalid")
        else:
            try:
                transcribe = getattr(self.local_asr, "transcribe_known_duration", None)
                if not callable(transcribe):
                    raise XiaohongshuMediaOperationError("local_asr_unavailable")
                remaining_cpu = request.budget["max_media_cpu_ms"] - derivative_cpu_ms
                if remaining_cpu < 1:
                    raise XiaohongshuMediaOperationError("media_cpu_budget_exhausted")
                asr = transcribe(
                    Path(derived.audio.staged_path), title=_caption(manifest) or "小红书视频",
                    duration_ms=duration_ms,
                    max_wall_ms=min(
                        remaining_cpu,
                        self._remaining_consumed(
                            request.budget["max_wall_ms"], probe_wall_ms, "wall_budget_exhausted"
                        ),
                    ),
                    control_check=(request.control.checkpoint if request.control else None),
                )
                if not isinstance(asr, GovernedLocalAsrOutcome) or asr.audio_duration_ms != duration_ms:
                    raise XiaohongshuMediaOperationError("local_asr_output_invalid")
                media_cpu_ms = derivative_cpu_ms + asr.wall_ms
                ocr_wall_ms = 0
                texts: list[str] = []
                for frame in derived.frames:
                    self._checkpoint(request)
                    remaining_cpu = request.budget["max_media_cpu_ms"] - media_cpu_ms
                    if remaining_cpu < 1:
                        raise XiaohongshuMediaOperationError("media_cpu_budget_exhausted")
                    frame_asset = XiaohongshuStagedAsset(
                        source_asset.asset_id, frame.ordinal, "image", frame.media_type,
                        frame.staged_path, frame.byte_count,
                    )
                    ocr = self._ocr(
                        frame_asset,
                        remaining_wall_ms=self._remaining_consumed(
                            request.budget["max_wall_ms"],
                            probe_wall_ms + asr.wall_ms + ocr_wall_ms,
                            "wall_budget_exhausted",
                        ),
                        remaining_media_cpu_ms=remaining_cpu, request=request,
                    )
                    media_cpu_ms += ocr.wall_ms
                    ocr_wall_ms += ocr.wall_ms
                    texts.append(ocr.text)
                if media_cpu_ms > request.budget["max_media_cpu_ms"]:
                    raise XiaohongshuMediaOperationError("media_cpu_budget_exhausted")
                document = self._create_video_document(request, manifest, source_asset, asr, derived, tuple(texts))
                analysis_wall_ms = probe_wall_ms + asr.wall_ms + ocr_wall_ms
                asr_audio_ms, vision_frames = duration_ms, len(derived.frames)
                self._complete(request, "analyze_video_document", _required_str(document, "markdown_uri"), _document_state_hash(document, request), {
                    "audio_milliseconds": asr_audio_ms, "vision_frames": vision_frames,
                    "media_cpu_milliseconds": media_cpu_ms, "wall_milliseconds": analysis_wall_ms,
                })
            except Exception:
                self._unknown(request, "analyze_video_document")
                raise
        if analysis_wall_ms > request.budget["max_wall_ms"] or media_cpu_ms > request.budget["max_media_cpu_ms"]:
            raise XiaohongshuMediaOperationError("media_budget_exhausted")
        consumed = {key: 0 for key in request.budget}
        consumed.update({"max_download_bytes": download_octets, "max_media_cpu_ms": media_cpu_ms,
                         "max_asr_audio_ms": asr_audio_ms, "max_vision_frames": vision_frames,
                         "max_wall_ms": analysis_wall_ms})
        if any(consumed[key] > request.budget[key] for key in consumed):
            raise XiaohongshuMediaOperationError("media_budget_exhausted")
        job_segment = media_job_uri_segment(request.job_id)
        doc_id = _required_str(document, "id")
        return MediaOperationReceipt(
            output={"kind": "document", "uri": _required_str(document, "markdown_uri"), "object_id": doc_id, "published": True},
            checkpoint={"resume_step": "execute_operation", "checkpoint_uri": f"crp://{self.artifacts.namespace_id}/jobs/{job_segment}/checkpoints/xiaohongshu-media-analysis-r2", "state_hash": _state_hash({"document_id": doc_id, "derivative_state_hash": self.video_journal.checkpoint(job_id=request.job_id).state_hash, "consumed": consumed}), "updated_at": self.now()},
            consumed=consumed,
            execution_receipt_ref=f"crp://{self.artifacts.namespace_id}/jobs/{job_segment}/receipts/xiaohongshu-media-analysis-r2",
        )

    def _execute_mixed(self, request: MediaOperationRequest, manifest: SourceManifest) -> MediaOperationReceipt:
        """Process one frozen mixed manifest in ordinal order under one Media Job.

        The staging journal remains the sole binary recovery aid; the separate
        analysis journal carries only typed, path-free per-asset results.
        """
        # This is intentionally a second authority proof immediately before
        # the first process/network side effect, rather than trusting dispatch.
        self._assert_frozen_binding(request)
        self._validate_mixed_dependencies(request, manifest)
        started = self.monotonic()
        self._checkpoint(request)
        staged, stage_checkpoint, download_octets, stage_wall_ms = self._stage_mixed_assets(request, manifest, started)
        staged_by_id = {item.asset_id: item for item in staged}
        if len(staged_by_id) != len(manifest.assets) or any(
            item.asset_id != asset.asset_id or item.ordinal != asset.ordinal or item.kind != asset.kind
            for item, asset in zip(staged, manifest.assets, strict=True)
        ):
            raise XiaohongshuMediaOperationError("staged_mixed_drifted")

        totals = {"media_cpu_milliseconds": 0, "audio_milliseconds": 0, "vision_frames": 0, "wall_milliseconds": stage_wall_ms}
        records: list[tuple[object, XiaohongshuAssetAnalysisRecord]] = []
        for asset in manifest.assets:
            self._checkpoint(request)
            staged_asset = staged_by_id[asset.asset_id]
            if asset.kind == "image":
                record = self._mixed_image(request, asset, staged_asset, stage_checkpoint, totals)
            elif asset.kind == "video":
                record = self._mixed_video(request, asset, staged_asset, stage_checkpoint, totals)
            else:
                record = self._mixed_text(request, manifest, asset, stage_checkpoint)
            self._add_analysis_consumed(totals, record, request)
            records.append((asset, record))

        self._checkpoint(request)
        journal_states = [
            {"asset_id": asset.asset_id, "ordinal": asset.ordinal,
             "state_hash": self.analysis_journal.checkpoint(job_id=request.job_id, asset_id=asset.asset_id).state_hash}
            for asset, _record in records
        ]
        synth_input = _state_hash({
            "step": "synthesize-mixed-document", "manifest_ref": request.manifest_ref,
            "manifest_revision": request.manifest_revision, "staging_ref": stage_checkpoint.output_ref,
            "staging_state_hash": stage_checkpoint.state_hash, "analysis": journal_states,
        })
        recovered = self._begin(request, "synthesize-mixed-document", synth_input)
        if recovered is not None:
            document = self._restore_document(recovered, request)
            synth_wall = self._receipt_ints(recovered, "synthesize-mixed-document", ("wall_milliseconds",), minimums=(0,))[0]
        else:
            try:
                self._checkpoint(request)
                synth_started = self.monotonic()
                document = self._create_mixed_document(request, manifest, tuple(records))
                synth_wall = self._elapsed(synth_started)
                self._complete(request, "synthesize-mixed-document", _required_str(document, "markdown_uri"),
                               _document_state_hash(document, request), {"wall_milliseconds": synth_wall})
            except Exception:
                self._unknown(request, "synthesize-mixed-document")
                raise
        elapsed = totals["wall_milliseconds"] + synth_wall
        if elapsed > request.budget["max_wall_ms"]:
            raise XiaohongshuMediaOperationError("wall_budget_exhausted")
        consumed = {key: 0 for key in request.budget}
        consumed.update({
            "max_download_bytes": download_octets,
            "max_media_cpu_ms": totals["media_cpu_milliseconds"],
            "max_asr_audio_ms": totals["audio_milliseconds"],
            "max_vision_frames": totals["vision_frames"], "max_wall_ms": elapsed,
        })
        if any(consumed[key] > request.budget[key] for key in consumed):
            raise XiaohongshuMediaOperationError("media_budget_exhausted")
        job_segment = media_job_uri_segment(request.job_id)
        doc_id = _required_str(document, "id")
        return MediaOperationReceipt(
            output={"kind": "document", "uri": _required_str(document, "markdown_uri"), "object_id": doc_id, "published": True},
            checkpoint={"resume_step": "execute_operation", "checkpoint_uri": f"crp://{self.artifacts.namespace_id}/jobs/{job_segment}/checkpoints/xiaohongshu-mixed-analysis-r1", "state_hash": _state_hash({"document_id": doc_id, "staging_state_hash": stage_checkpoint.state_hash, "analysis": journal_states, "consumed": consumed}), "updated_at": self.now()},
            consumed=consumed,
            execution_receipt_ref=f"crp://{self.artifacts.namespace_id}/jobs/{job_segment}/receipts/xiaohongshu-mixed-analysis-r1",
        )

    def _validate_mixed_dependencies(self, request: MediaOperationRequest, manifest: SourceManifest) -> None:
        if self.analysis_journal is None or not manifest.assets or any(
            asset.ordinal != ordinal or asset.kind not in {"image", "video", "text"}
            for ordinal, asset in enumerate(manifest.assets)
        ):
            raise XiaohongshuMediaOperationError("unsupported_xiaohongshu_media")
        video_assets = tuple(asset for asset in manifest.assets if asset.kind == "video")
        image_assets = tuple(asset for asset in manifest.assets if asset.kind == "image")
        if image_assets and request.budget["max_vision_frames"] < len(image_assets):
            raise XiaohongshuMediaOperationError("vision_budget_exhausted")
        if video_assets:
            if self.video_runner is None or self.video_journal is None or self.local_asr is None:
                raise XiaohongshuMediaOperationError("xiaohongshu_video_dependencies_unavailable")
            ready = getattr(self.local_asr, "assert_ready", None)
            max_frames = getattr(self.video_runner, "max_frames", None)
            if not callable(ready) or not isinstance(max_frames, int) or isinstance(max_frames, bool) or max_frames < 1:
                raise XiaohongshuMediaOperationError("local_asr_unavailable")
            if request.budget["max_asr_audio_ms"] < 1 or request.budget["max_vision_frames"] < len(image_assets) + len(video_assets) * max_frames:
                raise XiaohongshuMediaOperationError("vision_budget_exhausted")
            ready()

    def _assert_frozen_binding(self, request: MediaOperationRequest) -> SourceManifest:
        artifact = self.artifacts.resolve_source_ref(source_ref=request.manifest_ref, project_id=request.permission_snapshot.project_id)
        manifest = artifact.manifest
        if (artifact.public_ref != request.manifest_ref or artifact.revision != request.manifest_revision
                or manifest.source_id != request.source_id or manifest.platform != "xiaohongshu"
                or manifest.permission.decision != "granted" or request.permission_snapshot.manifest_ref != request.manifest_ref
                or request.permission_snapshot.manifest_revision != request.manifest_revision):
            raise XiaohongshuMediaOperationError("frozen_manifest_drift")
        return manifest

    def _stage_mixed_assets(self, request: MediaOperationRequest, manifest: SourceManifest, started: float):
        stage_input = _state_hash({"step": "stage_assets", "manifest_ref": request.manifest_ref, "manifest_revision": request.manifest_revision, "assets": [{"asset_id": item.asset_id, "ordinal": item.ordinal, "kind": item.kind} for item in manifest.assets]})
        recovered = self._begin(request, "stage_assets", stage_input)
        if recovered is not None:
            if not isinstance(recovered.get("output_ref"), str) or not isinstance(recovered.get("output_state_hash"), str):
                raise XiaohongshuMediaOperationError("stage_assets_receipt_invalid")
            staged = self.journal.restore(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, receipt={"output_ref": recovered["output_ref"], "state_hash": recovered["output_state_hash"]})
            download_octets, wall_ms = self._receipt_ints(recovered, "stage_assets", ("download_octets", "wall_milliseconds"), minimums=(sum(item.byte_count for item in staged), 0))
        else:
            try:
                materialized = self.materializer.materialize(manifest, job_id=request.job_id, max_download_bytes=request.budget["max_download_bytes"], timeout_seconds=self._remaining_wall(request, started) / 1000, project_id=request.permission_snapshot.project_id, control_check=(request.control.checkpoint if request.control else None))
                if not isinstance(materialized, XiaohongshuMaterializationOutcome):
                    raise XiaohongshuMediaOperationError("stage_assets_output_invalid")
                staged, download_octets = materialized.assets, materialized.total_download_bytes
                if not isinstance(download_octets, int) or isinstance(download_octets, bool) or download_octets < sum(item.byte_count for item in staged) or download_octets > request.budget["max_download_bytes"]:
                    raise XiaohongshuMediaOperationError("stage_assets_consumption_invalid")
                checkpoint = self.journal.commit(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, assets=staged)
                wall_ms = self._elapsed(started)
                self._complete(request, "stage_assets", checkpoint.output_ref, checkpoint.state_hash, {"download_octets": download_octets, "wall_milliseconds": wall_ms})
            except Exception:
                self._unknown(request, "stage_assets")
                raise
        return staged, self.journal.checkpoint(job_id=request.job_id), download_octets, wall_ms

    def _mixed_image(self, request, asset, staged, stage_checkpoint, totals) -> XiaohongshuAssetAnalysisRecord:
        assert self.analysis_journal is not None
        if staged.kind != "image" or staged.staged_path is None:
            raise XiaohongshuMediaOperationError("staged_mixed_drifted")
        providers = {"ocr": _provider_revision(self.ocr)}
        name = f"ocr-image-{asset.ordinal}"
        input_hash = _state_hash({"step": name, "asset_id": asset.asset_id, "ordinal": asset.ordinal,
                                  "staging_ref": stage_checkpoint.output_ref, "staging_state_hash": stage_checkpoint.state_hash,
                                  "manifest_ref": request.manifest_ref, "manifest_revision": request.manifest_revision,
                                  "providers": providers})
        recovered = self._begin(request, name, input_hash)
        if recovered is not None:
            return self._restore_analysis(recovered, request, asset, providers, name)
        try:
            self._checkpoint(request)
            outcome = self._ocr(staged, remaining_wall_ms=self._remaining_consumed(request.budget["max_wall_ms"], totals["wall_milliseconds"], "wall_budget_exhausted"), remaining_media_cpu_ms=self._remaining_consumed(request.budget["max_media_cpu_ms"], totals["media_cpu_milliseconds"], "media_cpu_budget_exhausted"), request=request)
            consumed = {"media_cpu_milliseconds": outcome.wall_ms, "audio_milliseconds": 0, "vision_frames": 1, "wall_milliseconds": outcome.wall_ms}
            checkpoint = self.analysis_journal.commit(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, source_id=request.source_id, asset_id=asset.asset_id, ordinal=asset.ordinal, kind="image", provider_revisions=providers, result={"ocr_text": outcome.text}, consumed=consumed)
            self._complete(request, name, checkpoint.output_ref, checkpoint.state_hash, consumed)
            return self._restore_analysis({"output_ref": checkpoint.output_ref, "output_state_hash": checkpoint.state_hash}, request, asset, providers, name)
        except Exception:
            self._unknown(request, name)
            raise

    def _mixed_text(self, request, manifest, asset, stage_checkpoint) -> XiaohongshuAssetAnalysisRecord:
        assert self.analysis_journal is not None
        name = f"text-{asset.ordinal}"
        providers: dict[str, str] = {}
        input_hash = _state_hash({"step": name, "asset_id": asset.asset_id, "ordinal": asset.ordinal,
                                  "manifest_ref": request.manifest_ref, "manifest_revision": request.manifest_revision,
                                  "staging_ref": stage_checkpoint.output_ref, "staging_state_hash": stage_checkpoint.state_hash})
        recovered = self._begin(request, name, input_hash)
        if recovered is not None:
            return self._restore_analysis(recovered, request, asset, providers, name)
        try:
            self._checkpoint(request)
            body = _text_asset_body(manifest)
            consumed = {"media_cpu_milliseconds": 0, "audio_milliseconds": 0, "vision_frames": 0, "wall_milliseconds": 0}
            checkpoint = self.analysis_journal.commit(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, source_id=request.source_id, asset_id=asset.asset_id, ordinal=asset.ordinal, kind="text", provider_revisions=providers, result={"body": body}, consumed=consumed)
            self._complete(request, name, checkpoint.output_ref, checkpoint.state_hash, consumed)
            return self._restore_analysis({"output_ref": checkpoint.output_ref, "output_state_hash": checkpoint.state_hash}, request, asset, providers, name)
        except Exception:
            self._unknown(request, name)
            raise

    def _mixed_video(self, request, asset, staged, stage_checkpoint, totals) -> XiaohongshuAssetAnalysisRecord:
        assert self.analysis_journal is not None and self.video_journal is not None and self.video_runner is not None and self.local_asr is not None
        if staged.kind != "video" or staged.media_type != "video/mp4" or staged.staged_path is None:
            raise XiaohongshuMediaOperationError("staged_mixed_drifted")
        derive_name = f"derive-video-{asset.ordinal}"
        derive_input = _state_hash({"step": derive_name, "asset_id": asset.asset_id, "staging_ref": stage_checkpoint.output_ref, "staging_state_hash": stage_checkpoint.state_hash, "video_provider_revision": _provider_revision(self.video_runner)})
        recovered_derive = self._begin(request, derive_name, derive_input)
        if recovered_derive is not None:
            derived = self._restore_derivatives(recovered_derive, request, asset.asset_id)
            derive_cpu, derive_wall = self._receipt_ints(recovered_derive, derive_name, ("media_cpu_milliseconds", "wall_milliseconds"), minimums=(0, 0))
            if derive_cpu != derived.wall_ms:
                raise XiaohongshuMediaOperationError("derive_video_receipt_invalid")
        else:
            try:
                derive = getattr(self.video_runner, "derive", None)
                if not callable(derive):
                    raise XiaohongshuMediaOperationError("governed_video_unavailable")
                outcome = derive(Path(staged.staged_path), job_id=request.job_id, media_type=staged.media_type, remaining_wall_ms=self._remaining_consumed(request.budget["max_wall_ms"], totals["wall_milliseconds"], "wall_budget_exhausted"), remaining_media_cpu_ms=self._remaining_consumed(request.budget["max_media_cpu_ms"], totals["media_cpu_milliseconds"], "media_cpu_budget_exhausted"), asset_key=asset.asset_id, control_check=(request.control.checkpoint if request.control else None))
                if not isinstance(outcome, GovernedStagedVideoOutcome):
                    raise XiaohongshuMediaOperationError("video_derivative_output_invalid")
                checkpoint = self.video_journal.commit(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, source_id=request.source_id, source_asset_id=asset.asset_id, outcome=outcome)
                derived, derive_cpu, derive_wall = outcome, outcome.wall_ms, outcome.wall_ms
                self._complete(request, derive_name, checkpoint.output_ref, checkpoint.state_hash, {"media_cpu_milliseconds": derive_cpu, "wall_milliseconds": derive_wall})
            except Exception:
                self._unknown(request, derive_name)
                raise
        if not derived.frames or len(derived.frames) > request.budget["max_vision_frames"] - totals["vision_frames"]:
            raise XiaohongshuMediaOperationError("vision_budget_exhausted")
        derivative = self.video_journal.checkpoint(job_id=request.job_id, source_asset_id=asset.asset_id)
        audio_ref = derivative.output_ref + "/audio"
        probe_name = f"probe-audio-{asset.ordinal}"
        probe_input = _state_hash({"step": probe_name, "asset_id": asset.asset_id, "derivative_ref": audio_ref, "derivative_state_hash": derivative.state_hash, "asr_provider_revision": _provider_revision(self.local_asr), "max_asr_audio_ms": request.budget["max_asr_audio_ms"] - totals["audio_milliseconds"]})
        recovered_probe = self._begin(request, probe_name, probe_input)
        if recovered_probe is not None:
            duration_ms, probe_wall = self._receipt_ints(recovered_probe, probe_name, ("audio_milliseconds", "wall_milliseconds"), minimums=(1, 0))
            if duration_ms > request.budget["max_asr_audio_ms"] - totals["audio_milliseconds"] or recovered_probe.get("output_ref") != audio_ref or recovered_probe.get("output_state_hash") != _state_hash({"audio_ref": audio_ref, "duration_ms": duration_ms}):
                raise XiaohongshuMediaOperationError("probe_audio_receipt_invalid")
        else:
            try:
                probe = getattr(self.local_asr, "probe_duration", None)
                if not callable(probe):
                    raise XiaohongshuMediaOperationError("local_asr_unavailable")
                probe_started = self.monotonic()
                duration_ms = probe(Path(derived.audio.staged_path), max_audio_ms=request.budget["max_asr_audio_ms"] - totals["audio_milliseconds"], max_wall_ms=self._remaining_consumed(request.budget["max_wall_ms"], totals["wall_milliseconds"] + derive_wall, "wall_budget_exhausted"), control_check=(request.control.checkpoint if request.control else None))
                if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 1:
                    raise XiaohongshuMediaOperationError("probe_audio_output_invalid")
                probe_wall = self._elapsed(probe_started)
                self._complete(request, probe_name, audio_ref, _state_hash({"audio_ref": audio_ref, "duration_ms": duration_ms}), {"audio_milliseconds": duration_ms, "wall_milliseconds": probe_wall})
            except Exception:
                self._unknown(request, probe_name)
                raise
        providers = {"asr": _provider_revision(self.local_asr), "ocr": _provider_revision(self.ocr)}
        name = f"analyze-video-{asset.ordinal}"
        input_hash = _state_hash({"step": name, "asset_id": asset.asset_id, "derivative_ref": derivative.output_ref, "derivative_state_hash": derivative.state_hash, "duration_ms": duration_ms, "providers": providers})
        recovered = self._begin(request, name, input_hash)
        if recovered is not None:
            return self._restore_analysis(recovered, request, asset, providers, name)
        try:
            transcribe = getattr(self.local_asr, "transcribe_known_duration", None)
            if not callable(transcribe):
                raise XiaohongshuMediaOperationError("local_asr_unavailable")
            available_cpu = self._remaining_consumed(request.budget["max_media_cpu_ms"], totals["media_cpu_milliseconds"] + derive_cpu, "media_cpu_budget_exhausted")
            asr = transcribe(Path(derived.audio.staged_path), title=_caption_from_asset(asset) or "小红书视频", duration_ms=duration_ms, max_wall_ms=min(available_cpu, self._remaining_consumed(request.budget["max_wall_ms"], totals["wall_milliseconds"] + derive_wall + probe_wall, "wall_budget_exhausted")), control_check=(request.control.checkpoint if request.control else None))
            if not isinstance(asr, GovernedLocalAsrOutcome) or asr.audio_duration_ms != duration_ms:
                raise XiaohongshuMediaOperationError("local_asr_output_invalid")
            ocr_texts: list[str] = []
            cpu = derive_cpu + asr.wall_ms
            ocr_wall = 0
            for frame in derived.frames:
                self._checkpoint(request)
                frame_asset = XiaohongshuStagedAsset(asset.asset_id, frame.ordinal, "image", frame.media_type, frame.staged_path, frame.byte_count)
                outcome = self._ocr(frame_asset, remaining_wall_ms=self._remaining_consumed(request.budget["max_wall_ms"], totals["wall_milliseconds"] + derive_wall + probe_wall + asr.wall_ms + ocr_wall, "wall_budget_exhausted"), remaining_media_cpu_ms=self._remaining_consumed(request.budget["max_media_cpu_ms"], totals["media_cpu_milliseconds"] + cpu, "media_cpu_budget_exhausted"), request=request)
                cpu += outcome.wall_ms; ocr_wall += outcome.wall_ms; ocr_texts.append(outcome.text)
            consumed = {"media_cpu_milliseconds": cpu, "audio_milliseconds": duration_ms, "vision_frames": len(derived.frames), "wall_milliseconds": derive_wall + probe_wall + asr.wall_ms + ocr_wall}
            result = {"transcript_segments": _asr_segments(asr), "frame_ocr": [{"ordinal": index, "text": text} for index, text in enumerate(ocr_texts)]}
            checkpoint = self.analysis_journal.commit(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, source_id=request.source_id, asset_id=asset.asset_id, ordinal=asset.ordinal, kind="video", provider_revisions=providers, result=result, consumed=consumed)
            self._complete(request, name, checkpoint.output_ref, checkpoint.state_hash, consumed)
            return self._restore_analysis({"output_ref": checkpoint.output_ref, "output_state_hash": checkpoint.state_hash}, request, asset, providers, name)
        except Exception:
            self._unknown(request, name)
            raise

    def _restore_analysis(self, receipt, request, asset, providers, name) -> XiaohongshuAssetAnalysisRecord:
        assert self.analysis_journal is not None
        if not isinstance(receipt.get("output_ref"), str) or not isinstance(receipt.get("output_state_hash"), str):
            raise XiaohongshuMediaOperationError(f"{name}_receipt_invalid")
        return self.analysis_journal.restore(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, source_id=request.source_id, asset_id=asset.asset_id, ordinal=asset.ordinal, kind=asset.kind, provider_revisions=providers, receipt={"output_ref": receipt["output_ref"], "state_hash": receipt["output_state_hash"]})

    @staticmethod
    def _add_analysis_consumed(totals, record: XiaohongshuAssetAnalysisRecord, request) -> None:
        for key in totals:
            value = record.consumed[key]
            totals[key] += value
        if totals["media_cpu_milliseconds"] > request.budget["max_media_cpu_ms"] or totals["audio_milliseconds"] > request.budget["max_asr_audio_ms"] or totals["vision_frames"] > request.budget["max_vision_frames"]:
            raise XiaohongshuMediaOperationError("media_budget_exhausted")

    def _create_mixed_document(self, request, manifest: SourceManifest, records: tuple[tuple[object, XiaohongshuAssetAnalysisRecord], ...]) -> dict[str, object]:
        title = _caption(manifest) or "小红书混合内容"
        markdown = _mixed_markdown(request, manifest, records, title)
        return dict(self.documents.create_or_replay_generated(DocumentDraft(
            title=f"小红书混合内容分析：{title}", document_type="media_analysis", markdown=markdown,
            source_refs=({"source_id": request.source_id, "locator": request.manifest_ref, "quote": request.manifest_revision},),
            project_id=request.permission_snapshot.project_id,
        )))

    def _stage_video_asset(self, request: MediaOperationRequest, manifest: SourceManifest, started: float):
        stage_input = _state_hash({"step": "stage_assets", "manifest_ref": request.manifest_ref, "manifest_revision": request.manifest_revision, "assets": [{"asset_id": item.asset_id, "ordinal": item.ordinal, "kind": item.kind} for item in manifest.assets]})
        recovered = self._begin(request, "stage_assets", stage_input)
        if recovered is not None:
            if not isinstance(recovered.get("output_ref"), str) or not isinstance(recovered.get("output_state_hash"), str):
                raise XiaohongshuMediaOperationError("stage_assets_receipt_invalid")
            staged = self.journal.restore(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, receipt={"output_ref": recovered["output_ref"], "state_hash": recovered["output_state_hash"]})
            download_octets, wall_ms = self._receipt_ints(recovered, "stage_assets", ("download_octets", "wall_milliseconds"), minimums=(sum(item.byte_count for item in staged), 0))
        else:
            try:
                materialized = self.materializer.materialize(manifest, job_id=request.job_id, max_download_bytes=request.budget["max_download_bytes"], timeout_seconds=self._remaining_wall(request, started) / 1000, project_id=request.permission_snapshot.project_id, control_check=(request.control.checkpoint if request.control else None))
                if not isinstance(materialized, XiaohongshuMaterializationOutcome):
                    raise XiaohongshuMediaOperationError("stage_assets_output_invalid")
                staged, download_octets = materialized.assets, materialized.total_download_bytes
                if not isinstance(download_octets, int) or isinstance(download_octets, bool) or download_octets < sum(item.byte_count for item in staged) or download_octets > request.budget["max_download_bytes"]:
                    raise XiaohongshuMediaOperationError("stage_assets_consumption_invalid")
                checkpoint = self.journal.commit(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, assets=staged)
                wall_ms = self._elapsed(started)
                self._complete(request, "stage_assets", checkpoint.output_ref, checkpoint.state_hash, {"download_octets": download_octets, "wall_milliseconds": wall_ms})
            except Exception:
                self._unknown(request, "stage_assets")
                raise
        if len(staged) != 1:
            raise XiaohongshuMediaOperationError("staged_video_drifted")
        return staged[0], self.journal.checkpoint(job_id=request.job_id), download_octets, wall_ms

    def _restore_derivatives(self, receipt: Mapping[str, object], request: MediaOperationRequest, source_asset_id: str) -> GovernedStagedVideoOutcome:
        if not isinstance(receipt.get("output_ref"), str) or not isinstance(receipt.get("output_state_hash"), str) or self.video_journal is None:
            raise XiaohongshuMediaOperationError("derive_video_receipt_invalid")
        return self.video_journal.restore(job_id=request.job_id, manifest_ref=request.manifest_ref, manifest_revision=request.manifest_revision, source_id=request.source_id, source_asset_id=source_asset_id, receipt={"output_ref": receipt["output_ref"], "state_hash": receipt["output_state_hash"]})

    def _receipt_ints(self, receipt: Mapping[str, object], name: str, keys: tuple[str, ...], *, minimums: tuple[int, ...]) -> tuple[int, ...]:
        consumed = receipt.get("consumed")
        values = tuple(consumed.get(key) if isinstance(consumed, Mapping) else None for key in keys)
        if len(values) != len(minimums) or any(not isinstance(value, int) or isinstance(value, bool) or value < minimum for value, minimum in zip(values, minimums, strict=True)):
            raise XiaohongshuMediaOperationError(f"{name}_receipt_invalid")
        return values  # type: ignore[return-value]

    def _create_video_document(self, request: MediaOperationRequest, manifest: SourceManifest, source_asset, asr: GovernedLocalAsrOutcome, derived: GovernedStagedVideoOutcome, texts: tuple[str, ...]) -> dict[str, object]:
        if len(texts) != len(derived.frames):
            raise XiaohongshuMediaOperationError("video_ocr_result_order_invalid")
        title = _caption(manifest) or "小红书视频"
        evidence = ", ".join(f"`{item}`" for item in source_asset.evidence_refs)
        lines = [f"# {title}", "", "## 来源快照", f"- Manifest: `{request.manifest_ref}`", f"- Manifest revision: `{request.manifest_revision}`", f"- Asset: `{source_asset.asset_id}`", f"- Evidence: {evidence}", "", "## 转写"]
        for segment in asr.transcript.get("segments", ()):
            if not isinstance(segment, Mapping):
                raise XiaohongshuMediaOperationError("local_asr_output_invalid")
            lines.append(f"- [{float(segment.get('start_seconds')):.3f}–{float(segment.get('end_seconds')):.3f}] {_required_str(segment, 'text')}")
        lines.extend(("", "## 关键帧 OCR"))
        for frame, text in zip(derived.frames, texts, strict=True):
            lines.extend((f"### 关键帧 {frame.ordinal + 1}", f"- Frame ordinal: {frame.ordinal}", f"- Source asset: `{source_asset.asset_id}`", f"- Evidence: {evidence}", f"- OCR: {text}"))
        return dict(self.documents.create_or_replay_generated(DocumentDraft(title=f"小红书视频分析：{title}", document_type="media_analysis", markdown="\n".join(lines) + "\n", source_refs=({"source_id": request.source_id, "locator": request.manifest_ref, "quote": request.manifest_revision},), project_id=request.permission_snapshot.project_id)))

    def _create_document(self, *, request: MediaOperationRequest, manifest: SourceManifest, staged: tuple[XiaohongshuStagedAsset, ...], ocr_texts: tuple[str, ...]) -> dict[str, object]:
        title = _caption(manifest) or "小红书图片分析"
        markdown = _markdown(manifest, request, staged, ocr_texts, title)
        return dict(self.documents.create_or_replay_generated(DocumentDraft(
            title=f"小红书图文分析：{title}", document_type="media_analysis", markdown=markdown,
            source_refs=({"source_id": request.source_id, "locator": request.manifest_ref, "quote": request.manifest_revision},),
            project_id=request.permission_snapshot.project_id,
        )))

    def _restore_document(self, receipt: Mapping[str, object], request: MediaOperationRequest) -> dict[str, object]:
        output_ref = receipt.get("output_ref")
        if not isinstance(output_ref, str) or "/documents/" not in output_ref or not output_ref.endswith(".md"):
            raise XiaohongshuMediaOperationError("ocr_step_receipt_invalid")
        document = self.documents.read(output_ref.rsplit("/", 1)[-1][:-3])
        expected_refs = [{"source_id": request.source_id, "locator": request.manifest_ref, "quote": request.manifest_revision}]
        if not isinstance(document, Mapping) or document.get("revision") != 1 or document.get("type") != "media_analysis" or document.get("project_id") != request.permission_snapshot.project_id or document.get("source_refs") != expected_refs or document.get("markdown_uri") != output_ref or receipt.get("output_state_hash") != _document_state_hash(document, request):
            raise XiaohongshuMediaOperationError("ocr_step_document_drift")
        return dict(document)

    def _ocr(self, item: XiaohongshuStagedAsset, *, remaining_wall_ms: int, remaining_media_cpu_ms: int, request: MediaOperationRequest) -> GovernedLocalOcrOutcome:
        extract = getattr(self.ocr, "extract_text", None)
        if not callable(extract) or not isinstance(item.staged_path, str) or not isinstance(item.media_type, str):
            raise XiaohongshuMediaOperationError("local_ocr_unavailable")
        outcome = extract(
            Path(item.staged_path),
            media_type=item.media_type,
            remaining_wall_ms=remaining_wall_ms,
            remaining_media_cpu_ms=remaining_media_cpu_ms,
            control_check=(request.control.checkpoint if request.control else None),
        )
        if not isinstance(outcome, GovernedLocalOcrOutcome):
            raise XiaohongshuMediaOperationError("local_ocr_output_invalid")
        return outcome

    @staticmethod
    def _validate_request(request: MediaOperationRequest) -> None:
        if request.operation != "analyze_source":
            raise XiaohongshuMediaOperationError("unsupported_media_operation")
        if set(request.budget) != _BUDGET_FIELDS or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in request.budget.values()):
            raise XiaohongshuMediaOperationError("media_budget_invalid")
        if request.budget["max_download_bytes"] < 1 or request.budget["max_media_cpu_ms"] < 1 or request.budget["max_wall_ms"] < 1:
            raise XiaohongshuMediaOperationError("media_budget_exhausted")

    def _elapsed(self, started: float) -> int:
        return max(0, int((self.monotonic() - started) * 1000))

    def _remaining_wall(
        self,
        request: MediaOperationRequest,
        started: float,
        *,
        prior_consumed_ms: int = 0,
    ) -> int:
        value = request.budget["max_wall_ms"] - prior_consumed_ms - self._elapsed(started)
        if value < 1:
            raise XiaohongshuMediaOperationError("wall_budget_exhausted")
        return value

    @staticmethod
    def _remaining_consumed(limit: int, consumed: int, error_code: str) -> int:
        value = limit - consumed
        if value < 1:
            raise XiaohongshuMediaOperationError(error_code)
        return value

    @staticmethod
    def _checkpoint(request: MediaOperationRequest) -> None:
        if request.control is not None:
            request.control.checkpoint()

    @staticmethod
    def _begin(request: MediaOperationRequest, name: str, input_hash: str) -> Mapping[str, object] | None:
        return request.control.begin_recipe_step(name, input_hash) if request.control is not None else None

    @staticmethod
    def _complete(request: MediaOperationRequest, name: str, output_ref: str, output_hash: str, consumed: Mapping[str, int]) -> None:
        if request.control is not None:
            request.control.complete_recipe_step(name, output_ref=output_ref, output_state_hash=output_hash, consumed=consumed)

    @staticmethod
    def _unknown(request: MediaOperationRequest, name: str) -> None:
        if request.control is not None:
            request.control.mark_recipe_step_unknown(name)


def _caption(manifest: SourceManifest) -> str:
    if manifest.body is not None and isinstance(manifest.body.text, str) and manifest.body.text.strip():
        return manifest.body.text.strip()
    for key in ("caption", "title", "desc"):
        value = dict(manifest.metadata.entries).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _markdown(manifest: SourceManifest, request: MediaOperationRequest, staged: tuple[XiaohongshuStagedAsset, ...], texts: tuple[str, ...], title: str) -> str:
    if len(staged) != len(texts):
        raise XiaohongshuMediaOperationError("ocr_result_order_invalid")
    lines = [f"# {title}", "", "## 来源快照", f"- Manifest: `{request.manifest_ref}`", f"- Manifest revision: `{request.manifest_revision}`", f"- Caption: {title}", "", "## 图片 OCR"]
    assets = {asset.asset_id: asset for asset in manifest.assets}
    for staged_item, text in zip(staged, texts, strict=True):
        asset = assets.get(staged_item.asset_id)
        if asset is None or asset.ordinal != staged_item.ordinal or not isinstance(text, str):
            raise XiaohongshuMediaOperationError("ocr_result_order_invalid")
        evidence = ", ".join(f"`{item}`" for item in asset.evidence_refs)
        lines.extend((f"### 图片 {asset.ordinal + 1}", f"- Asset: `{asset.asset_id}`", f"- Ordinal: {asset.ordinal}", f"- Evidence: {evidence}", f"- OCR: {text}"))
    return "\n".join(lines) + "\n"


def _text_asset_body(manifest: SourceManifest) -> str:
    if manifest.body is not None and manifest.body.kind == "text" and isinstance(manifest.body.text, str):
        return manifest.body.text
    return _caption(manifest)


def _caption_from_asset(asset: object) -> str:
    # Asset metadata deliberately has no untrusted human text; role is the
    # only safe fallback for local ASR's display-only title.
    role = getattr(asset, "role", None)
    return role if isinstance(role, str) and role else ""


def _asr_segments(asr: GovernedLocalAsrOutcome) -> list[dict[str, object]]:
    rows = asr.transcript.get("segments")
    if not isinstance(rows, list):
        raise XiaohongshuMediaOperationError("local_asr_output_invalid")
    result: list[dict[str, object]] = []
    previous_end = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise XiaohongshuMediaOperationError("local_asr_output_invalid")
        try:
            start, end = round(float(row.get("start_seconds")) * 1000), round(float(row.get("end_seconds")) * 1000)
        except (TypeError, ValueError) as error:
            raise XiaohongshuMediaOperationError("local_asr_output_invalid") from error
        text = row.get("text")
        if start < previous_end or end <= start or not isinstance(text, str):
            raise XiaohongshuMediaOperationError("local_asr_output_invalid")
        result.append({"start_ms": start, "end_ms": end, "text": text})
        previous_end = end
    return result


def _mixed_markdown(request: MediaOperationRequest, manifest: SourceManifest, records: tuple[tuple[object, XiaohongshuAssetAnalysisRecord], ...], title: str) -> str:
    if len(records) != len(manifest.assets):
        raise XiaohongshuMediaOperationError("mixed_analysis_order_invalid")
    lines = [f"# {title}", "", "## 来源快照", f"- Manifest: `{request.manifest_ref}`", f"- Manifest revision: `{request.manifest_revision}`", "", "## 资产分析"]
    for expected, (asset, record) in zip(manifest.assets, records, strict=True):
        if asset is not expected or record.asset_id != expected.asset_id or record.ordinal != expected.ordinal or record.kind != expected.kind:
            raise XiaohongshuMediaOperationError("mixed_analysis_order_invalid")
        relations = ", ".join(f"{relation.relation}:{relation.target_asset_id}" for relation in expected.relations) or "none"
        evidence = ", ".join(f"`{ref}`" for ref in expected.evidence_refs) or "none"
        lines.extend((f"### 资产 {expected.ordinal + 1}", f"- Asset: `{expected.asset_id}`", f"- Kind: {expected.kind}", f"- Role: {expected.role}", f"- Ordinal: {expected.ordinal}", f"- Relations: {relations}", f"- Evidence: {evidence}"))
        if expected.kind == "image":
            text = record.result.get("ocr_text")
            if not isinstance(text, str):
                raise XiaohongshuMediaOperationError("mixed_analysis_result_invalid")
            lines.append(f"- OCR: {text}")
        elif expected.kind == "text":
            text = record.result.get("body")
            if not isinstance(text, str):
                raise XiaohongshuMediaOperationError("mixed_analysis_result_invalid")
            lines.append(f"- Text: {text}")
        else:
            segments, frames = record.result.get("transcript_segments"), record.result.get("frame_ocr")
            if not isinstance(segments, list) or not isinstance(frames, list):
                raise XiaohongshuMediaOperationError("mixed_analysis_result_invalid")
            lines.append("- Transcript:")
            for row in segments:
                if not isinstance(row, Mapping):
                    raise XiaohongshuMediaOperationError("mixed_analysis_result_invalid")
                lines.append(f"  - [{row.get('start_ms')}–{row.get('end_ms')}] {row.get('text')}")
            lines.append("- Frame OCR:")
            for row in frames:
                if not isinstance(row, Mapping):
                    raise XiaohongshuMediaOperationError("mixed_analysis_result_invalid")
                lines.append(f"  - Frame {row.get('ordinal')}: {row.get('text')}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _provider_revision(value: object) -> str:
    revision = getattr(value, "provider_revision", None)
    return revision if isinstance(revision, str) and revision else "governed-local-ocr"


def _document_state_hash(document: Mapping[str, object], request: MediaOperationRequest) -> str:
    return _state_hash({"document_id": _required_str(document, "id"), "markdown_uri": _required_str(document, "markdown_uri"), "revision": document.get("revision"), "project_id": document.get("project_id"), "type": document.get("type"), "source_refs": document.get("source_refs"), "request_project_id": request.permission_snapshot.project_id, "request_source_id": request.source_id, "manifest_ref": request.manifest_ref, "manifest_revision": request.manifest_revision})


def _state_hash(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _required_str(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise XiaohongshuMediaOperationError(f"{key}_invalid")
    return item
