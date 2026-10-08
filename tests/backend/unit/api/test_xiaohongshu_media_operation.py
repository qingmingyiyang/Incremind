from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from backend.api.governed_local_ocr import GovernedLocalOcrOutcome
from backend.api.governed_local_asr import GovernedLocalAsrOutcome
from backend.api.governed_staged_video import GovernedStagedVideoDerivative, GovernedStagedVideoOutcome
from backend.api.xiaohongshu_asset_materializer import XiaohongshuMaterializationOutcome, XiaohongshuStagedAsset
from backend.api.xiaohongshu_asset_analysis_journal import XiaohongshuAssetAnalysisJournal
from backend.api.xiaohongshu_media_operation import XiaohongshuMediaOperationError, XiaohongshuMediaOperationProvider
from backend.api.xiaohongshu_staging_journal import XiaohongshuStagingJournal
from backend.api.xiaohongshu_video_derivative_journal import XiaohongshuVideoDerivativeJournal
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.media_hands import MediaOperationRequest, SourcePermissionSnapshot
from core.source_processing import SourceManifestArtifactRepository, SourceManifestCodec
from core.storage_provider import JsonObjectStore


class _Materializer:
    def __init__(self, root: Path) -> None:
        self.root, self.calls, self.fail = root, 0, False

    def materialize(self, manifest, *, job_id, max_download_bytes, timeout_seconds, project_id=None, control_check=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("transport lost")
        values = []
        for asset in manifest.assets:
            if control_check is not None:
                control_check()
            if asset.kind == "text":
                values.append(XiaohongshuStagedAsset(asset.asset_id, asset.ordinal, "text", "text/plain", None, 0))
                continue
            extension = "mp4" if asset.kind == "video" else "jpg"
            media_type = "video/mp4" if asset.kind == "video" else "image/jpeg"
            path = self.root / ".rebuild-data" / "media-hands" / "job" / f"{asset.ordinal}.{extension}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image" + bytes([asset.ordinal]))
            values.append(XiaohongshuStagedAsset(asset.asset_id, asset.ordinal, asset.kind, media_type, str(path), path.stat().st_size))
        return XiaohongshuMaterializationOutcome(tuple(values), sum(item.byte_count for item in values) + 7)


class _Ocr:
    provider_revision = "ocr-r1"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.remaining_wall_ms: list[int] = []

    def extract_text(self, path, *, media_type, remaining_wall_ms, remaining_media_cpu_ms, control_check=None):
        self.calls += 1
        self.remaining_wall_ms.append(remaining_wall_ms)
        if self.fail:
            raise AssertionError("OCR should have resumed from durable document")
        assert path.is_file() and media_type == "image/jpeg" and remaining_wall_ms > 0 and remaining_media_cpu_ms > 0
        if control_check is not None:
            control_check()
        return GovernedLocalOcrOutcome(f"文字 {self.calls}", "local", "ocr-r1", 1)


class _Control:
    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, object]] = {}
        self.inputs: dict[str, str] = {}
        self.unknown: list[str] = []

    def checkpoint(self):
        return None

    def begin_recipe_step(self, step_name, input_state_hash):
        old = self.inputs.setdefault(step_name, input_state_hash)
        assert old == input_state_hash
        return self.receipts.get(step_name)

    def complete_recipe_step(self, step_name, *, output_ref, output_state_hash, consumed):
        self.receipts[step_name] = {"output_ref": output_ref, "output_state_hash": output_state_hash, "consumed": dict(consumed)}

    def mark_recipe_step_unknown(self, step_name):
        self.unknown.append(step_name)


class _VideoRunner:
    provider_revision = "fixed-video-r1"
    max_frames = 1

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0
        self.fail = False

    def derive(self, staged_video, *, job_id, media_type, remaining_wall_ms, remaining_media_cpu_ms, asset_key=None, control_check=None):
        self.calls += 1
        if self.fail:
            raise AssertionError("video derivative should have resumed")
        assert staged_video.is_file() and media_type == "video/mp4" and remaining_wall_ms > 0 and remaining_media_cpu_ms > 0
        if control_check is not None:
            control_check()
        root = self.root / ".rebuild-data" / "media-hands" / "job" / "xiaohongshu" / "derivatives" / (asset_key or "single")
        root.mkdir(parents=True, exist_ok=True)
        audio = root / "audio.wav"; audio.write_bytes(b"wav")
        frame = root / "frame-001.jpg"; frame.write_bytes(b"frame")
        return GovernedStagedVideoOutcome(
            GovernedStagedVideoDerivative("audio", 0, "audio/wav", str(audio), 3),
            (GovernedStagedVideoDerivative("frame", 0, "image/jpeg", str(frame), 5),), 3,
        )


class _Asr:
    provider_revision = "asr-r1"

    def __init__(self) -> None:
        self.probes = self.transcribes = 0
        self.fail = False
        self.max_walls: list[int] = []

    def assert_ready(self):
        return None

    def probe_duration(self, path, *, max_audio_ms, max_wall_ms=None, control_check=None):
        self.probes += 1
        assert path.is_file() and max_audio_ms >= 500
        if control_check is not None:
            control_check()
        return 500

    def transcribe_known_duration(self, path, *, title, duration_ms, max_wall_ms, control_check=None):
        self.transcribes += 1
        self.max_walls.append(max_wall_ms)
        if self.fail:
            raise AssertionError("ASR should have resumed")
        assert path.is_file() and duration_ms == 500 and max_wall_ms > 0
        if control_check is not None:
            control_check()
        transcript = {"title": title, "language": "zh", "source": "local_asr", "segments": [{"start_seconds": 0.0, "end_seconds": 0.5, "text": "视频文字"}]}
        return GovernedLocalAsrOutcome(transcript, (), 500, 4, "local", "asr-r1")


def _manifest():
    return SourceManifestCodec.decode({
        "schema_version": "1.0.0", "source_id": "xhs-source", "source_ref": "crp://default/sources/xhs-source", "platform": "xiaohongshu",
        "input_identity": "https://www.xiaohongshu.com/explore/note-1", "resolver_revision": "xhs-v1", "normalizer_revision": "xhs-manifest-v1",
        "content_kind": "image_set", "body": None, "metadata": {"caption": "测试图文"},
        "permission": {"decision": "granted", "evidence_refs": ["crp://default/evidence/projects/project-1/note-r1"]},
        "provenance_refs": ["crp://default/evidence/projects/project-1/note-r1"],
        "assets": [
            {"asset_id": "image-a", "ordinal": 0, "kind": "image", "media_type": "image/jpeg", "role": "primary", "locator": None, "source_ref": "crp://default/sources/xhs-source/assets/image-a", "relations": [], "evidence_refs": ["crp://default/evidence/projects/project-1/image-a"]},
            {"asset_id": "image-b", "ordinal": 1, "kind": "image", "media_type": "image/jpeg", "role": "gallery", "locator": None, "source_ref": "crp://default/sources/xhs-source/assets/image-b", "relations": [], "evidence_refs": ["crp://default/evidence/projects/project-1/image-b"]},
        ],
    })


def _parts(tmp_path: Path):
    store = JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="default")
    artifacts = SourceManifestArtifactRepository(store, namespace_id="default")
    artifact = artifacts.put(project_id="project-1", manifest_id="xhs-images-r1", manifest=_manifest())
    documents = AggregateRepositoryFactory(runtime_root=tmp_path, namespace_id="default", json_store=store).document_repository()
    materializer, ocr = _Materializer(tmp_path), _Ocr()
    provider = XiaohongshuMediaOperationProvider(artifacts, documents, materializer, XiaohongshuStagingJournal(tmp_path), ocr, now=lambda: "2026-08-26T00:00:00Z", monotonic=lambda: 0.0)
    request = MediaOperationRequest(
        job_id="media_hands:xhs-source:analyze_source", source_id="xhs-source", operation="analyze_source", manifest_ref=artifact.public_ref, manifest_revision=artifact.revision, checkpoint=None,
        budget={"max_download_bytes": 1000, "max_media_cpu_ms": 1000, "max_asr_audio_ms": 0, "max_vision_frames": 2, "max_model_input_tokens": 0, "max_model_output_tokens": 0, "max_wall_ms": 10000},
        permission_snapshot=SourcePermissionSnapshot("project-1", artifact.public_ref, artifact.revision, "crp://default/source-permissions/projects/project-1/grant", "r1", 0),
    )
    return provider, request, documents, materializer, ocr


def _video_manifest():
    return SourceManifestCodec.decode({
        "schema_version": "1.0.0", "source_id": "xhs-source", "source_ref": "crp://default/sources/xhs-source", "platform": "xiaohongshu",
        "input_identity": "https://www.xiaohongshu.com/explore/note-video", "resolver_revision": "xhs-v1", "normalizer_revision": "xhs-manifest-v1",
        "content_kind": "video", "body": None, "metadata": {"caption": "测试视频"},
        "permission": {"decision": "granted", "evidence_refs": ["crp://default/evidence/projects/project-1/note-r1"]},
        "provenance_refs": ["crp://default/evidence/projects/project-1/note-r1"],
        "assets": [{"asset_id": "video-a", "ordinal": 0, "kind": "video", "media_type": "video/mp4", "role": "primary", "locator": None, "source_ref": "crp://default/sources/xhs-source/assets/video-a", "relations": [], "evidence_refs": ["crp://default/evidence/projects/project-1/video-a"]}],
    })


def _video_parts(tmp_path: Path, *, with_dependencies=True):
    store = JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="default")
    artifacts = SourceManifestArtifactRepository(store, namespace_id="default")
    artifact = artifacts.put(project_id="project-1", manifest_id="xhs-video-r1", manifest=_video_manifest())
    documents = AggregateRepositoryFactory(runtime_root=tmp_path, namespace_id="default", json_store=store).document_repository()
    materializer, ocr, runner, asr = _Materializer(tmp_path), _Ocr(), _VideoRunner(tmp_path), _Asr()
    provider = XiaohongshuMediaOperationProvider(
        artifacts, documents, materializer, XiaohongshuStagingJournal(tmp_path), ocr,
        now=lambda: "2026-08-26T00:00:00Z", monotonic=lambda: 0.0,
        video_runner=runner if with_dependencies else None,
        video_journal=XiaohongshuVideoDerivativeJournal(tmp_path) if with_dependencies else None,
        local_asr=asr if with_dependencies else None,
    )
    request = MediaOperationRequest(
        job_id="media_hands:xhs-source:analyze_source", source_id="xhs-source", operation="analyze_source", manifest_ref=artifact.public_ref, manifest_revision=artifact.revision, checkpoint=None,
        budget={"max_download_bytes": 1000, "max_media_cpu_ms": 1000, "max_asr_audio_ms": 1000, "max_vision_frames": 1, "max_model_input_tokens": 0, "max_model_output_tokens": 0, "max_wall_ms": 10000},
        permission_snapshot=SourcePermissionSnapshot("project-1", artifact.public_ref, artifact.revision, "crp://default/source-permissions/projects/project-1/grant", "r1", 0),
    )
    return provider, request, documents, materializer, ocr, runner, asr


def _mixed_manifest():
    return SourceManifestCodec.decode({
        "schema_version": "1.0.0", "source_id": "xhs-source", "source_ref": "crp://default/sources/xhs-source", "platform": "xiaohongshu",
        "input_identity": "https://www.xiaohongshu.com/explore/note-mixed", "resolver_revision": "xhs-v1", "normalizer_revision": "xhs-manifest-v1",
        "content_kind": "mixed", "body": {"kind": "text", "text": "冻结正文", "source_ref": None}, "metadata": {"caption": "测试混合内容"},
        "permission": {"decision": "granted", "evidence_refs": ["crp://default/evidence/projects/project-1/note-r1"]},
        "provenance_refs": ["crp://default/evidence/projects/project-1/note-r1"],
        "assets": [
            {"asset_id": "image-a", "ordinal": 0, "kind": "image", "media_type": "image/jpeg", "role": "cover", "locator": None, "source_ref": "crp://default/sources/xhs-source/assets/image-a", "relations": [{"relation": "precedes", "target_asset_id": "video-a"}], "evidence_refs": ["crp://default/evidence/projects/project-1/image-a"]},
            {"asset_id": "video-a", "ordinal": 1, "kind": "video", "media_type": "video/mp4", "role": "primary", "locator": None, "source_ref": "crp://default/sources/xhs-source/assets/video-a", "relations": [{"relation": "follows", "target_asset_id": "image-a"}], "evidence_refs": ["crp://default/evidence/projects/project-1/video-a"]},
            {"asset_id": "image-b", "ordinal": 2, "kind": "image", "media_type": "image/jpeg", "role": "gallery", "locator": None, "source_ref": "crp://default/sources/xhs-source/assets/image-b", "relations": [{"relation": "precedes", "target_asset_id": "text-a"}], "evidence_refs": ["crp://default/evidence/projects/project-1/image-b"]},
            {"asset_id": "text-a", "ordinal": 3, "kind": "text", "media_type": "text/plain", "role": "caption", "locator": None, "source_ref": "crp://default/sources/xhs-source/assets/text-a", "relations": [{"relation": "follows", "target_asset_id": "image-b"}], "evidence_refs": ["crp://default/evidence/projects/project-1/text-a"]},
        ],
    })


def _mixed_parts(tmp_path: Path, *, manifest=None):
    store = JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="default")
    artifacts = SourceManifestArtifactRepository(store, namespace_id="default")
    manifest = manifest or _mixed_manifest()
    artifact = artifacts.put(project_id="project-1", manifest_id="xhs-mixed-r1", manifest=manifest)
    documents = AggregateRepositoryFactory(runtime_root=tmp_path, namespace_id="default", json_store=store).document_repository()
    materializer, ocr, runner, asr = _Materializer(tmp_path), _Ocr(), _VideoRunner(tmp_path), _Asr()
    provider = XiaohongshuMediaOperationProvider(
        artifacts, documents, materializer, XiaohongshuStagingJournal(tmp_path), ocr,
        now=lambda: "2026-08-26T00:00:00Z", monotonic=lambda: 0.0, video_runner=runner,
        video_journal=XiaohongshuVideoDerivativeJournal(tmp_path), local_asr=asr,
        analysis_journal=XiaohongshuAssetAnalysisJournal(tmp_path),
    )
    request = MediaOperationRequest(
        job_id="media_hands:xhs-source:analyze_source", source_id="xhs-source", operation="analyze_source", manifest_ref=artifact.public_ref, manifest_revision=artifact.revision, checkpoint=None,
        budget={"max_download_bytes": 1000, "max_media_cpu_ms": 1000, "max_asr_audio_ms": 1000, "max_vision_frames": sum(1 if asset.kind == "image" else runner.max_frames if asset.kind == "video" else 0 for asset in manifest.assets), "max_model_input_tokens": 0, "max_model_output_tokens": 0, "max_wall_ms": 10000},
        permission_snapshot=SourcePermissionSnapshot("project-1", artifact.public_ref, artifact.revision, "crp://default/source-permissions/projects/project-1/grant", "r1", 0),
    )
    return provider, request, documents, materializer, ocr, runner, asr


def test_first_execution_writes_ordered_private_safe_media_analysis_document(tmp_path: Path):
    provider, request, documents, materializer, ocr = _parts(tmp_path)
    receipt = provider.execute(request)
    markdown = documents.markdown(str(receipt.output["object_id"]), revision=1)
    assert receipt.output["kind"] == "document" and receipt.consumed == {"max_download_bytes": 19, "max_media_cpu_ms": 2, "max_asr_audio_ms": 0, "max_vision_frames": 2, "max_model_input_tokens": 0, "max_model_output_tokens": 0, "max_wall_ms": 0}
    assert materializer.calls == 1 and ocr.calls == 2
    assert markdown is not None and "Asset: `image-a`" in markdown and markdown.index("image-a") < markdown.index("image-b")
    assert str(tmp_path) not in markdown and "https://" not in markdown and "文字 1" in markdown and "文字 2" in markdown


def test_authority_drift_and_unsupported_manifest_have_no_side_effects(tmp_path: Path):
    provider, request, documents, materializer, ocr = _parts(tmp_path)
    with pytest.raises(XiaohongshuMediaOperationError, match="frozen_manifest_drift"):
        provider.execute(replace(request, source_id="other"))
    with pytest.raises(XiaohongshuMediaOperationError, match="unsupported_media_operation"):
        provider.execute(replace(request, operation="extract_images"))
    assert materializer.calls == ocr.calls == 0 and documents.list() == ()


def test_durable_recipe_restore_reuses_staging_and_document_without_download_or_ocr(tmp_path: Path):
    provider, request, documents, materializer, ocr = _parts(tmp_path)
    control = _Control()
    first = provider.execute(replace(request, control=control))
    materializer.fail = True
    ocr.fail = True
    replay = provider.execute(replace(request, control=control))
    assert replay.output == first.output and materializer.calls == 1 and ocr.calls == 2 and len(documents.list()) == 1


def test_stage_only_recovery_deducts_historical_wall_before_ocr(tmp_path: Path):
    provider, request, _documents, materializer, ocr = _parts(tmp_path)
    control = _Control()
    provider.execute(replace(request, control=control))
    control.receipts.pop("ocr_document")
    control.receipts["stage_images"]["consumed"]["wall_milliseconds"] = 8_000
    ocr.calls = 0
    ocr.remaining_wall_ms.clear()

    replay = provider.execute(replace(request, control=control))

    assert materializer.calls == 1
    assert ocr.remaining_wall_ms == [2_000, 2_000]
    assert replay.consumed["max_wall_ms"] == 8_000


def test_staging_failure_marks_recipe_unknown(tmp_path: Path):
    provider, request, documents, materializer, _ocr = _parts(tmp_path)
    materializer.fail = True
    control = _Control()
    with pytest.raises(RuntimeError, match="transport lost"):
        provider.execute(replace(request, control=control))
    assert control.unknown == ["stage_images"] and documents.list() == ()


def test_vision_budget_is_checked_before_materialization(tmp_path: Path):
    provider, request, _documents, materializer, _ocr = _parts(tmp_path)
    with pytest.raises(XiaohongshuMediaOperationError, match="vision_budget_exhausted"):
        provider.execute(replace(request, budget={**request.budget, "max_vision_frames": 1}))
    assert materializer.calls == 0


def test_media_cpu_budget_is_checked_before_materialization(tmp_path: Path):
    provider, request, _documents, materializer, _ocr = _parts(tmp_path)
    with pytest.raises(XiaohongshuMediaOperationError, match="media_budget_exhausted"):
        provider.execute(replace(request, budget={**request.budget, "max_media_cpu_ms": 0}))
    assert materializer.calls == 0


def test_video_first_execution_writes_private_safe_document_with_asr_and_frame_ocr(tmp_path: Path):
    provider, request, documents, materializer, ocr, runner, asr = _video_parts(tmp_path)
    receipt = provider.execute(request)
    markdown = documents.markdown(str(receipt.output["object_id"]), revision=1)
    assert materializer.calls == runner.calls == asr.probes == asr.transcribes == ocr.calls == 1
    assert receipt.consumed == {"max_download_bytes": 13, "max_media_cpu_ms": 8, "max_asr_audio_ms": 500, "max_vision_frames": 1, "max_model_input_tokens": 0, "max_model_output_tokens": 0, "max_wall_ms": 8}
    assert markdown is not None and "视频文字" in markdown and "关键帧 1" in markdown and "Evidence: `crp://default/evidence/projects/project-1/video-a`" in markdown
    assert str(tmp_path) not in markdown and "https://" not in markdown and ".mp4" not in markdown


def test_video_missing_dependencies_fails_before_staging(tmp_path: Path):
    provider, request, _documents, materializer, _ocr, _runner, _asr = _video_parts(tmp_path, with_dependencies=False)
    with pytest.raises(XiaohongshuMediaOperationError, match="video_dependencies_unavailable"):
        provider.execute(request)
    assert materializer.calls == 0


def test_video_recipe_recovery_restores_derivative_probe_and_document_without_processes(tmp_path: Path):
    provider, request, documents, materializer, ocr, runner, asr = _video_parts(tmp_path)
    control = _Control()
    first = provider.execute(replace(request, control=control))
    materializer.fail = runner.fail = asr.fail = ocr.fail = True
    replay = provider.execute(replace(request, control=control))
    assert replay.output == first.output and len(documents.list()) == 1
    assert materializer.calls == runner.calls == asr.probes == asr.transcribes == ocr.calls == 1


def test_video_derivative_receipt_drift_fails_closed_without_asr(tmp_path: Path):
    provider, request, _documents, _materializer, _ocr, _runner, asr = _video_parts(tmp_path)
    control = _Control()
    provider.execute(replace(request, control=control))
    control.receipts.pop("analyze_video_document")
    control.receipts["derive_video"]["output_state_hash"] = "sha256:" + "0" * 64
    with pytest.raises(Exception):
        provider.execute(replace(request, control=control))
    assert asr.transcribes == 1


def test_video_budget_is_checked_before_staging_and_derivative_failure_is_unknown(tmp_path: Path):
    provider, request, _documents, materializer, _ocr, runner, _asr = _video_parts(tmp_path)
    with pytest.raises(XiaohongshuMediaOperationError, match="video_budget_exhausted"):
        provider.execute(replace(request, budget={**request.budget, "max_asr_audio_ms": 0}))
    assert materializer.calls == 0
    control = _Control()
    runner.fail = True
    with pytest.raises(AssertionError, match="should have resumed"):
        provider.execute(replace(request, control=control))
    assert control.unknown == ["derive_video"]


def test_video_rechecks_frozen_manifest_immediately_before_staging(tmp_path: Path):
    provider, request, _documents, materializer, _ocr, _runner, _asr = _video_parts(tmp_path)
    original = provider.artifacts

    class DriftingArtifacts:
        namespace_id = original.namespace_id
        calls = 0
        def resolve_source_ref(self, *, source_ref, project_id):
            self.calls += 1
            artifact = original.resolve_source_ref(source_ref=source_ref, project_id=project_id)
            if self.calls == 1:
                return artifact
            return type("Drift", (), {
                "public_ref": artifact.public_ref,
                "revision": "drifted-r2",
                "manifest": artifact.manifest,
            })()

    with pytest.raises(XiaohongshuMediaOperationError, match="frozen_manifest_drift"):
        replace(provider, artifacts=DriftingArtifacts()).execute(request)
    assert materializer.calls == 0


def test_video_frame_budget_is_admitted_before_download_or_derivation(tmp_path: Path):
    provider, request, _documents, materializer, _ocr, runner, _asr = _video_parts(tmp_path)
    runner.max_frames = 2
    with pytest.raises(XiaohongshuMediaOperationError, match="vision_budget_exhausted"):
        provider.execute(request)
    assert materializer.calls == runner.calls == 0


def test_video_recovery_cross_checks_derivative_cpu_and_asr_uses_remaining_cpu(tmp_path: Path):
    provider, request, _documents, _materializer, _ocr, _runner, asr = _video_parts(tmp_path)
    control = _Control()
    provider.execute(replace(request, control=control))
    control.receipts["derive_video"]["consumed"]["media_cpu_milliseconds"] = 2
    with pytest.raises(XiaohongshuMediaOperationError, match="derive_video_receipt_invalid"):
        provider.execute(replace(request, control=control))

    provider2, request2, _documents2, _materializer2, _ocr2, _runner2, asr2 = _video_parts(tmp_path / "cpu")
    with pytest.raises(XiaohongshuMediaOperationError, match="media_cpu_budget_exhausted"):
        provider2.execute(replace(request2, budget={**request2.budget, "max_media_cpu_ms": 5}))
    assert asr2.max_walls == [2]


def test_mixed_execution_stages_once_and_synthesizes_ordered_asset_evidence(tmp_path: Path):
    provider, request, documents, materializer, ocr, runner, asr = _mixed_parts(tmp_path)
    receipt = provider.execute(request)
    markdown = documents.markdown(str(receipt.output["object_id"]), revision=1)
    assert materializer.calls == runner.calls == asr.probes == asr.transcribes == 1
    assert ocr.calls == 3 and len(documents.list()) == 1
    assert receipt.consumed == {"max_download_bytes": 25, "max_media_cpu_ms": 10, "max_asr_audio_ms": 500, "max_vision_frames": 3, "max_model_input_tokens": 0, "max_model_output_tokens": 0, "max_wall_ms": 10}
    assert markdown is not None
    assert markdown.index("image-a") < markdown.index("video-a") < markdown.index("image-b") < markdown.index("text-a")
    assert "Relations: precedes:video-a" in markdown and "Relations: follows:image-a" in markdown
    assert "冻结正文" in markdown and "视频文字" in markdown and "文字 3" in markdown
    assert str(tmp_path) not in markdown and "https://" not in markdown and ".mp4" not in markdown


def test_mixed_full_recovery_uses_asset_journals_without_process_or_second_document(tmp_path: Path):
    provider, request, documents, materializer, ocr, runner, asr = _mixed_parts(tmp_path)
    control = _Control()
    first = provider.execute(replace(request, control=control))
    materializer.fail = runner.fail = asr.fail = ocr.fail = True
    replay = provider.execute(replace(request, control=control))
    assert replay.output == first.output and len(documents.list()) == 1
    assert materializer.calls == runner.calls == asr.probes == asr.transcribes == 1 and ocr.calls == 3


def test_mixed_partial_recovery_reuses_completed_assets_and_marks_only_failed_step_unknown(tmp_path: Path):
    provider, request, _documents, materializer, ocr, runner, asr = _mixed_parts(tmp_path)
    control = _Control()
    provider.execute(replace(request, control=control))
    control.receipts.pop("analyze-video-1")
    asr.fail = True
    with pytest.raises(AssertionError, match="ASR should have resumed"):
        provider.execute(replace(request, control=control))
    assert control.unknown == ["analyze-video-1"]
    assert materializer.calls == runner.calls == asr.probes == 1 and ocr.calls == 3


def test_mixed_budget_and_manifest_drift_fail_before_download(tmp_path: Path):
    provider, request, _documents, materializer, _ocr, _runner, _asr = _mixed_parts(tmp_path)
    with pytest.raises(XiaohongshuMediaOperationError, match="vision_budget_exhausted"):
        provider.execute(replace(request, budget={**request.budget, "max_vision_frames": 2}))
    assert materializer.calls == 0
    original = provider.artifacts
    class DriftingArtifacts:
        namespace_id = original.namespace_id
        calls = 0
        def resolve_source_ref(self, *, source_ref, project_id):
            self.calls += 1
            artifact = original.resolve_source_ref(source_ref=source_ref, project_id=project_id)
            if self.calls == 1:
                return artifact
            return type("Drift", (), {"public_ref": artifact.public_ref, "revision": "drifted-r2", "manifest": artifact.manifest})()
    with pytest.raises(XiaohongshuMediaOperationError, match="frozen_manifest_drift"):
        replace(provider, artifacts=DriftingArtifacts()).execute(request)
    assert materializer.calls == 0


def test_mixed_supports_multiple_asset_scoped_videos(tmp_path: Path):
    payload = SourceManifestCodec.encode(_mixed_manifest())
    payload["assets"].append({"asset_id": "video-b", "ordinal": 4, "kind": "video", "media_type": "video/mp4", "role": "gallery", "locator": None, "source_ref": "crp://default/sources/xhs-source/assets/video-b", "relations": [], "evidence_refs": ["crp://default/evidence/projects/project-1/video-b"]})
    provider, request, documents, _materializer, _ocr, runner, asr = _mixed_parts(tmp_path, manifest=SourceManifestCodec.decode(payload))
    receipt = provider.execute(request)
    markdown = documents.markdown(str(receipt.output["object_id"]), revision=1)
    assert runner.calls == asr.probes == asr.transcribes == 2
    assert markdown is not None and markdown.index("video-a") < markdown.index("video-b")
