from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from backend.api.analyze_source_ai_runtime import AnalyzeSourceCapability
from backend.api.job_lifecycle_runtime import get_or_create_rebuild_job_lifecycle
from backend.api.job_execution_runtime import register_job_execution_handler
from backend.api.media_hands_runtime import (
    MediaHandsRuntimeUnavailable,
    configure_media_hands_runtime,
)
from backend.api.media_ingress_selection_authority import MediaIngressSelectionAuthority
from backend.api.governed_local_asr import GovernedLocalAsrOutcome
from backend.api.governed_local_ocr import GovernedLocalOcrOutcome
from backend.api.governed_staged_video import (
    GovernedStagedVideoDerivative,
    GovernedStagedVideoOutcome,
)
from backend.api.xiaohongshu_asset_materializer import (
    XiaohongshuMaterializationOutcome,
    XiaohongshuStagedAsset,
)
from backend.security import DownloadedBinary
from backend.api.ai_runtime import build_ai_runtime, get_or_build_ai_runtime
from backend.api.media_hands_composition import (
    compose_application_media_hands,
    compose_media_hands_lifecycle,
)
from backend.api.routes.product.job_lifecycle import (
    recover_rebuild_job_lifecycle,
    shutdown_rebuild_job_lifecycle,
)
from core.ai_kernel import InMemoryTurnPayloadStore
from core.effect_log import EffectRecoveryCoordinator, EffectState, build_effect_runtime
from core.job_runner import (
    InMemoryJobRepository,
    RoutedJobRepository,
    SQLiteJobRuntimeLifecycle,
    SQLiteJobStore,
)
from core.job_runner.media_execution_receipt import media_job_uri_segment
from core.media_hands import (
    MediaHandsPolicyAuthority,
    MediaOperationReceipt,
    SourcePermissionSnapshot,
    default_personal_workbench_policy_snapshot,
)
from core.media_hands.effect_contract import (
    INTENT_SCHEMA,
    RECEIPT_KIND,
    RECEIPT_SCHEMA,
    provider_revision_identity,
)
from core.source_processing import (
    ManifestPermission,
    SourceManifestArtifactRepository,
    SourceManifestCodec,
    SourcePermissionAuthority,
)
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[4]


def _publish_policy(tmp_path: Path, revision: int, *, enabled: bool) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = enabled
    snapshot["revision"] = f"personal-workbench-r{revision}"
    MediaHandsPolicyAuthority(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    ).publish(
        snapshot,
        expected_revision=revision - 1,
        command_id=f"media-runtime-policy-{revision:04d}",
        actor="local-user",
        created_at=f"2026-08-25T22:{revision:02d}:00Z",
    )


class _Provider:
    provider_id = "fixture-media-provider"
    provider_revision = "fixture-r1"
    supported_platforms = frozenset({"bilibili"})

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return MediaOperationReceipt(
            output={
                "kind": "document",
                "uri": f"crp://default/jobs/{request.job_id}/outputs/result",
                "object_id": "media-result-1",
                "published": True,
            },
            checkpoint={
                "resume_step": "execute_operation",
                "checkpoint_uri": f"crp://default/jobs/{request.job_id}/checkpoints/final",
                "state_hash": "sha256:" + "a" * 64,
                "updated_at": "2026-08-25T00:00:01Z",
            },
            consumed={key: 0 for key in request.budget},
            execution_receipt_ref=(
                f"crp://default/jobs/{media_job_uri_segment(request.job_id)}/receipts/execution"
            ),
        )


class _CanonicalOutputVerifier:
    def __init__(self) -> None:
        self.calls = []

    def assert_output_committed(self, *, output, request) -> None:
        self.calls.append((dict(output), request))


class _PlatformProvider:
    def provide(self, text, *, project_id):
        assert project_id == "project-1"
        value = json.loads(
            (ROOT / "core-contracts/rebuild/source-processing/fixtures/bilibili-video.json").read_text(
                encoding="utf-8"
            )
        )
        value["platform"] = "bilibili"
        value["metadata"] = {
            "bvid": "BV1xx411c7mD",
            "cid": 101,
            "title": "字幕视频",
            "duration_seconds": 5,
        }
        value["permission"] = {
            "decision": "unknown",
            "evidence_refs": [
                "crp://default/evidence/projects/project-1/bilibili-fixture-metadata-r1"
            ],
        }
        return SourceManifestCodec.decode(value)


class _XiaohongshuPlatformProvider:
    def provide(self, text, *, project_id):
        assert project_id == "project-1"
        return SourceManifestCodec.decode({
            "schema_version": "1.0.0",
            "source_id": "xhsrt",
            "source_ref": "crp://default/sources/xhsrt",
            "platform": "xiaohongshu",
            "input_identity": text,
            "resolver_revision": "xhsr1",
            "normalizer_revision": "xhsm1",
            "content_kind": "image_set",
            "body": None,
            "metadata": {"caption": "运行时图文"},
            "permission": {"decision": "unknown", "evidence_refs": ["crp://default/evidence/projects/project-1/xhs-runtime-r1"]},
            "provenance_refs": ["crp://default/evidence/projects/project-1/xhs-runtime-r1"],
            "assets": [{
                "asset_id": "xhs-image-1", "ordinal": 0, "kind": "image",
                "media_type": "image/jpeg", "role": "primary", "locator": None,
                "source_ref": "crp://default/sources/xhsrt/assets/xhs-image-1",
                "relations": [],
                "evidence_refs": ["crp://default/evidence/projects/project-1/xhs-image-1"],
            }],
        })


class _XiaohongshuMaterializer:
    def __init__(self, root: Path) -> None:
        self.root = root

    def materialize(self, manifest, *, job_id, max_download_bytes, timeout_seconds, project_id=None, control_check=None):
        if control_check is not None:
            control_check()
        path = self.root / ".rebuild-data" / "media-hands" / "runtime-xhs" / "0.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
        return XiaohongshuMaterializationOutcome((
            XiaohongshuStagedAsset("xhs-image-1", 0, "image", "image/jpeg", str(path), 5),
        ), 5)


class _XiaohongshuOcr:
    provider_revision = "runtime-ocr-r1"

    def assert_ready(self):
        return None

    def extract_text(self, path, *, media_type, remaining_wall_ms, remaining_media_cpu_ms, control_check=None):
        assert path.is_file() and media_type == "image/jpeg"
        assert remaining_wall_ms > 0 and remaining_media_cpu_ms > 0
        if control_check is not None:
            control_check()
        return GovernedLocalOcrOutcome("运行时 OCR", "runtime-ocr", self.provider_revision, 1)


class _XiaohongshuVideoPlatformProvider(_XiaohongshuPlatformProvider):
    def provide(self, text, *, project_id):
        manifest = super().provide(text, project_id=project_id)
        value = SourceManifestCodec.encode(manifest)
        value["content_kind"] = "video"
        value["assets"][0].update({"kind": "video", "media_type": "video/mp4"})
        return SourceManifestCodec.decode(value)


class _XiaohongshuMixedPlatformProvider(_XiaohongshuPlatformProvider):
    def provide(self, text, *, project_id):
        value = SourceManifestCodec.encode(super().provide(text, project_id=project_id))
        value["content_kind"] = "mixed"
        value["body"] = {"kind": "text", "text": "混合正文", "source_ref": None}
        shapes = (("image-a", "image", "image/jpeg"), ("video-a", "video", "video/mp4"), ("image-b", "image", "image/jpeg"), ("text-a", "text", "text/plain"))
        value["assets"] = []
        for ordinal, (asset_id, kind, media_type) in enumerate(shapes):
            relations = []
            if ordinal + 1 < len(shapes):
                relations.append({"relation": "next", "target_asset_id": shapes[ordinal + 1][0]})
            value["assets"].append({
                "asset_id": asset_id, "ordinal": ordinal, "kind": kind,
                "media_type": media_type, "role": "caption" if kind == "text" else "gallery",
                "locator": None, "source_ref": f"crp://default/sources/xhsrt/assets/{asset_id}",
                "relations": relations,
                "evidence_refs": [f"crp://default/evidence/projects/project-1/{asset_id}"],
            })
        return SourceManifestCodec.decode(value)


class _XiaohongshuVideoMaterializer:
    def __init__(self, root: Path) -> None:
        self.root = root

    def materialize(self, manifest, *, job_id, max_download_bytes, timeout_seconds, project_id=None, control_check=None):
        if control_check is not None:
            control_check()
        path = self.root / ".rebuild-data" / "media-hands" / "runtime-xhs-video" / "0.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return XiaohongshuMaterializationOutcome((
            XiaohongshuStagedAsset("xhs-image-1", 0, "video", "video/mp4", str(path), 5),
        ), 5)


class _XiaohongshuMixedMaterializer:
    def __init__(self, root: Path) -> None:
        self.root = root

    def materialize(self, manifest, *, job_id, max_download_bytes, timeout_seconds, project_id=None, control_check=None):
        values = []
        for asset in manifest.assets:
            if control_check is not None:
                control_check()
            if asset.kind == "text":
                values.append(XiaohongshuStagedAsset(asset.asset_id, asset.ordinal, "text", "text/plain", None, 0))
                continue
            suffix = "mp4" if asset.kind == "video" else "jpg"
            media_type = "video/mp4" if asset.kind == "video" else "image/jpeg"
            path = self.root / ".rebuild-data" / "media-hands" / "runtime-xhs-mixed" / f"{asset.ordinal}.{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(asset.kind.encode())
            values.append(XiaohongshuStagedAsset(asset.asset_id, asset.ordinal, asset.kind, media_type, str(path), path.stat().st_size))
        return XiaohongshuMaterializationOutcome(tuple(values), sum(item.byte_count for item in values))


class _XiaohongshuVideoRunner:
    provider_revision = "runtime-video-r1"
    max_frames = 1

    def __init__(self, root: Path) -> None:
        self.root = root

    def assert_ready(self):
        return None

    def derive(self, staged_video, *, job_id, media_type, remaining_wall_ms, remaining_media_cpu_ms, asset_key=None, control_check=None):
        if control_check is not None:
            control_check()
        base = self.root / ".rebuild-data" / "media-hands" / "runtime-xhs-video" / "derived" / (asset_key or "single")
        base.mkdir(parents=True, exist_ok=True)
        audio = base / "audio.wav"; audio.write_bytes(b"audio")
        frame = base / "frame-001.jpg"; frame.write_bytes(b"frame")
        return GovernedStagedVideoOutcome(
            GovernedStagedVideoDerivative("audio", 0, "audio/wav", str(audio), 5),
            (GovernedStagedVideoDerivative("frame", 0, "image/jpeg", str(frame), 5),),
            1,
        )


class _XiaohongshuAsr:
    provider_revision = "runtime-asr-r1"

    def assert_ready(self):
        return None

    def probe_duration(self, audio_path, *, max_audio_ms, max_wall_ms=None, control_check=None):
        return 500

    def transcribe_known_duration(self, audio_path, *, title, duration_ms, max_wall_ms, control_check=None):
        return GovernedLocalAsrOutcome(
            {"title": title, "language": "zh", "source": "local_asr", "segments": [{"start_seconds": 0.0, "end_seconds": 0.5, "text": "视频转写"}]},
            ({"text": "视频转写"},), duration_ms, 1, "runtime-asr", self.provider_revision,
        )


def _grant_snapshot(*, store, artifact, manifest, permission_id="media-fixture-permission"):
    authority = SourcePermissionAuthority(store, namespace_id="default")
    grant = authority.grant(
        project_id="project-1",
        permission_id=permission_id,
        source_id=manifest.source_id,
        platform=manifest.platform,
        source_manifest_ref=artifact.public_ref,
        source_manifest_revision=artifact.revision,
        metadata_evidence_ref=manifest.permission.evidence_refs[0],
        actor_id="test-user",
        command_id=f"grant-{permission_id}",
        created_at="2026-08-25T00:00:00Z",
        expected_revision=0,
    )
    return SourcePermissionSnapshot(
        project_id="project-1",
        manifest_ref=artifact.public_ref,
        manifest_revision=artifact.revision,
        grant_ref=grant.public_ref,
        grant_revision=f"r{grant.revision}",
        revocation_generation=grant.revocation_generation,
    )


def _parts(
    tmp_path,
    *,
    snapshot=None,
    provider=None,
    platform_providers=None,
    output_verifier=None,
    network_factory=None,
    binary_network=None,
    local_asr_runner=None,
    xiaohongshu_materializer=None,
    xiaohongshu_ocr_runner=None,
    xiaohongshu_video_runner=None,
    sqlite_store=None,
):
    root = tmp_path.resolve()
    store = JsonObjectStore(root / ".rebuild-data", namespace_id="default")
    repository = RoutedJobRepository(
        legacy=InMemoryJobRepository(),
        sqlite=sqlite_store or SQLiteJobStore(root / ".rebuild-data" / "jobs.sqlite3"),
        sqlite_job_types=frozenset(),
    )
    container = SimpleNamespace(root_dir=root)
    if snapshot is not None:
        container._media_hands_policy_snapshot_for_test = snapshot
    if provider is not None:
        container.media_operation_provider = provider
    if platform_providers is not None:
        container.platform_manifest_providers = platform_providers
    if output_verifier is not None:
        container.media_output_verifier = output_verifier
    if network_factory is not None:
        container.media_text_network_factory = network_factory
    if binary_network is not None:
        container.media_binary_network = binary_network
    if local_asr_runner is not None:
        container.media_local_asr_runner = local_asr_runner
    if xiaohongshu_materializer is not None:
        container.media_xiaohongshu_materializer = xiaohongshu_materializer
    if xiaohongshu_ocr_runner is not None:
        container.media_xiaohongshu_ocr_runner = xiaohongshu_ocr_runner
    if xiaohongshu_video_runner is not None:
        container.media_xiaohongshu_video_runner = xiaohongshu_video_runner
    application = SimpleNamespace(state=SimpleNamespace(container=container))
    _attach_core_job_runtime(application, root)
    return root, store, repository, application


def _attach_core_job_runtime(application, root: Path) -> None:
    runtime = build_effect_runtime(
        root / ".rebuild-data" / "jobs.sqlite3", owner_id="test-core-job-runtime"
    )
    application.state.effect_runtime = runtime
    register_job_execution_handler(application, root, runtime)


def _prepare_media_v2_runtime(tmp_path) -> SimpleNamespace:
    _publish_policy(tmp_path, 1, enabled=True)
    provider = _Provider()
    root, store, repository, application = _parts(
        tmp_path,
        provider=provider,
        platform_providers={"bilibili": _PlatformProvider()},
        output_verifier=_CanonicalOutputVerifier(),
    )
    database = root / ".rebuild-data" / "jobs.sqlite3"
    selection_authority = MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(database)
    )
    selection_authority.publish(
        "hands",
        expected_revision=0,
        command_id="media-v2-runtime-selection-r1",
        actor="local-user",
        created_at="2026-08-30T00:00:00Z",
    )
    runtime = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository,
    ).runtime
    assert runtime is not None and runtime.readiness().ready

    initial = runtime.resolver.resolve(
        {
            "input": {
                "kind": "text",
                "text": "https://www.bilibili.com/video/BV1xx411c7mD",
                "source_ref": None,
            },
            "intent": "organize",
            "output_profile": {"profile_id": "default", "revision": "1"},
            "resource_budget": {
                "max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60,
            },
        },
        {"project_id": "project-1"},
    )
    assert initial.manifest is not None
    assert initial.manifest_ref is not None and initial.manifest_revision is not None
    artifact = SimpleNamespace(
        public_ref=initial.manifest_ref,
        revision=initial.manifest_revision,
    )
    permission = _grant_snapshot(
        store=store,
        artifact=artifact,
        manifest=initial.manifest,
        permission_id="media-v2-runtime-permission",
    )
    return SimpleNamespace(
        root=root,
        store=store,
        database=database,
        provider=provider,
        application=application,
        runtime=runtime,
        manifest=initial.manifest,
        artifact=artifact,
        permission=permission,
        selection_authority=selection_authority,
    )


def _media_v2_operation_id(database: Path, job_id: str) -> str:
    with sqlite3.connect(database) as connection:
        return str(connection.execute(
            "SELECT effect_operation_id FROM job_effect_node WHERE job_id=?",
            (job_id,),
        ).fetchone()[0])


def _publish_hands_selection(database: Path, *, command_id: str) -> None:
    """Publish the only executable Media ingress selection for an Effect-v2 test."""
    MediaIngressSelectionAuthority(SQLiteStructuredRecordStore(database)).publish(
        "hands",
        expected_revision=0,
        command_id=command_id,
        actor="test-user",
        created_at="2026-08-30T00:00:00Z",
    )


def _dispatch_media_v2(*, runtime, application, database: Path, manifest, manifest_ref: str,
                       manifest_revision: str, permission_snapshot, idempotency_key: str):
    """Exercise the production admission -> Core recovery -> immutable receipt path."""
    admission = runtime.provision(
        manifest=manifest,
        manifest_ref=manifest_ref,
        manifest_revision=manifest_revision,
        operation="analyze_source",
        idempotency_key=idempotency_key,
        created_at="2026-08-30T00:00:01Z",
        permission_snapshot=permission_snapshot,
    )
    job_id = str(admission.record.payload["id"])
    operation_id = _media_v2_operation_id(database, job_id)
    planned = application.state.effect_runtime.log.get(operation_id)
    assert planned.occurred_at > 0
    # The production runtime persists Effect wall-clock seconds; its recovery
    # API consumes the same unit, so derive the test dispatch point from that
    # recorded fact instead of hard-coding an unrelated date.
    EffectRecoveryCoordinator(application.state.effect_runtime).recover_once(
        now=planned.occurred_at + 1,
    )
    effect = application.state.effect_runtime.log.get(operation_id)
    assert effect.state is EffectState.SETTLED_OK
    assert effect.root_id == job_id
    assert effect.rev_set["context_manifest"] == manifest_revision
    expected_provider = provider_revision_identity(*runtime.handler.provider_identity)
    assert effect.rev_set["provider"] == expected_provider
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT receipt_json FROM media_hands_effect_domain_receipt WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    assert row is not None
    receipt = json.loads(str(row[0]))
    assert receipt == {
        **receipt,
        "operation_id": operation_id,
        "job_id": job_id,
        "receipt_ref": f"receipt:media-hands/{operation_id}",
        "receipt_kind": RECEIPT_KIND,
        "receipt_schema_version": RECEIPT_SCHEMA,
        "intent_schema_version": INTENT_SCHEMA,
        "provider_revision_identity": expected_provider,
    }
    assert effect.result_ref == receipt["receipt_ref"]
    assert receipt["output"]["uri"].startswith("crp://default/documents/")
    return admission, operation_id, receipt


def test_media_v2_core_cancellation_fact_blocks_provider_after_reservation(tmp_path) -> None:
    """A v2 Media request only observes Core cancellation, never Job state."""

    prepared = _prepare_media_v2_runtime(tmp_path)
    admission = prepared.runtime.provision(
        manifest=prepared.manifest,
        manifest_ref=prepared.artifact.public_ref,
        manifest_revision=prepared.artifact.revision,
        operation="analyze_source",
        idempotency_key="media-v2-core-cancellation-fence-0001",
        created_at="2026-08-30T00:00:01Z",
        permission_snapshot=prepared.permission,
    )
    job_id = str(admission.record.payload["id"])
    operation_id = _media_v2_operation_id(prepared.database, job_id)
    planned = prepared.application.state.effect_runtime.log.get(operation_id)
    prepared.application.state.effect_runtime.log.request_cancellation(
        operation_id,
        request_ref="decision:media-hands/effect-v2/cancellation-fixture",
        now=planned.occurred_at + 1,
    )

    EffectRecoveryCoordinator(prepared.application.state.effect_runtime).recover_once(
        now=planned.occurred_at + 1,
    )

    effect = prepared.application.state.effect_runtime.log.get(operation_id)
    assert effect.state is EffectState.INFLIGHT
    assert prepared.provider.calls == 0
    with sqlite3.connect(prepared.database) as connection:
        reservation_count = connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_provider_reservation WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_domain_receipt WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
    assert (reservation_count, receipt_count) == (1, 0)


def test_media_runtime_uses_core_v2_dispatch_without_job_lifecycle(
    monkeypatch, tmp_path,
) -> None:
    prepared = _prepare_media_v2_runtime(tmp_path)
    monkeypatch.setattr(
        SQLiteJobRuntimeLifecycle,
        "enqueue_durable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Media Effect-v2 entered legacy Job lifecycle")
        ),
    )

    admission = prepared.runtime.provision(
        manifest=prepared.manifest,
        manifest_ref=prepared.artifact.public_ref,
        manifest_revision=prepared.artifact.revision,
        operation="analyze_source",
        idempotency_key="media-v2-runtime-idempotency-0001",
        created_at="2026-08-30T00:00:01Z",
        permission_snapshot=prepared.permission,
    )
    job_id = str(admission.record.payload["id"])
    operation_id = _media_v2_operation_id(prepared.database, job_id)
    with sqlite3.connect(prepared.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_store").fetchone()[0] == 0
    assert prepared.application.state.effect_runtime.log.get(operation_id).state is EffectState.PLANNED

    EffectRecoveryCoordinator(prepared.application.state.effect_runtime).recover_once(
        now=int(time.time()) + 1,
    )

    settled = prepared.application.state.effect_runtime.log.get(operation_id)
    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == f"receipt:media-hands/{operation_id}"
    assert prepared.provider.calls == 1
    replay = prepared.runtime.provision(
        manifest=prepared.manifest,
        manifest_ref=prepared.artifact.public_ref,
        manifest_revision=prepared.artifact.revision,
        operation="analyze_source",
        idempotency_key="media-v2-runtime-idempotency-0001",
        created_at="2026-08-30T00:00:01Z",
        permission_snapshot=prepared.permission,
    )
    assert replay.replayed is True
    assert replay.record.payload["id"] == job_id
    assert replay.record.payload["execution_version"] == "effect-v2"
    assert _media_v2_operation_id(prepared.database, job_id) == operation_id
    assert prepared.provider.calls == 1
    with sqlite3.connect(prepared.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_domain_receipt"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 1


@pytest.mark.parametrize("drift", ("policy", "selection", "permission"))
def test_media_v2_live_authority_drift_never_invokes_provider(tmp_path, drift) -> None:
    prepared = _prepare_media_v2_runtime(tmp_path)
    admission = prepared.runtime.provision(
        manifest=prepared.manifest,
        manifest_ref=prepared.artifact.public_ref,
        manifest_revision=prepared.artifact.revision,
        operation="analyze_source",
        idempotency_key=f"media-v2-runtime-{drift}-drift-0001",
        created_at="2026-08-30T00:00:01Z",
        permission_snapshot=prepared.permission,
    )
    operation_id = _media_v2_operation_id(
        prepared.database, str(admission.record.payload["id"]),
    )
    if drift == "policy":
        _publish_policy(tmp_path, 2, enabled=False)
    elif drift == "selection":
        prepared.selection_authority.publish(
            "legacy",
            expected_revision=1,
            command_id="media-v2-runtime-selection-r2-legacy",
            actor="local-user",
            created_at="2026-08-30T00:00:02Z",
        )
    else:
        SourcePermissionAuthority(prepared.store, namespace_id="default").revoke(
            project_id="project-1",
            permission_id="media-v2-runtime-permission",
            actor_id="test-user",
            command_id="revoke-media-v2-runtime-permission",
            created_at="2026-08-30T00:00:02Z",
            expected_revision=1,
        )

    coordinator = EffectRecoveryCoordinator(prepared.application.state.effect_runtime)
    now = int(time.time()) + 1
    coordinator.recover_once(now=now)
    assert prepared.application.state.effect_runtime.log.get(operation_id).state is EffectState.INFLIGHT
    coordinator.recover_once(now=now + 31)

    recovered = prepared.application.state.effect_runtime.log.get(operation_id)
    assert recovered.state is EffectState.UNKNOWN
    expected_error_ref = (
        "error:media-hands-execution-authorization-invalid"
        if drift == "permission"
        else "error:media-hands-authority-revision-drift"
    )
    assert recovered.error_ref == expected_error_ref
    assert prepared.provider.calls == 0
    with sqlite3.connect(prepared.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_provider_reservation"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_domain_receipt"
        ).fetchone()[0] == 0


class _StepCommitCrash(BaseException):
    pass


def test_ai_first_keeps_media_backend_lazy_and_explicit_composer_reuses_lifecycle(
    monkeypatch, tmp_path,
) -> None:
    _publish_policy(tmp_path, 1, enabled=True)
    provider = _Provider()
    root, _store, _repository, application = _parts(
        tmp_path,
        provider=provider,
        platform_providers={"bilibili": _PlatformProvider()},
        output_verifier=_CanonicalOutputVerifier(),
    )
    container = application.state.container
    request = SimpleNamespace(app=application)

    ai = get_or_build_ai_runtime(request, container)
    composition = compose_application_media_hands(application, container)
    definition = next(
        item for item in ai.capability_registry_snapshot().definitions
        if item.capability_id == "analyze_source"
    )

    assert composition.resolution.reason == "ready"
    assert composition.resolution.runtime is not None
    assert composition.resolution.runtime.readiness().ready
    assert composition.expert_job_wait_bridge is not None
    assert callable(application.state.expert_media_job_wait_reconcile)
    assert "media_hands" not in composition.lifecycle.job_types
    assert definition.tool_definition is not None
    # The AI capability snapshot is frozen before an explicit media request;
    # composing Hands later must not hot-swap that Turn runtime.
    assert definition.tool_definition.available is False
    monkeypatch.setattr(
        "backend.api.media_hands_composition.compose_application_media_hands",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cached AI recomposed Hands")),
    )
    assert get_or_build_ai_runtime(request, container) is ai
    assert compose_application_media_hands(application, container).lifecycle is composition.lifecycle


def test_disabled_composer_does_not_hot_swap_ai_or_lifecycle_after_publish(
    tmp_path,
) -> None:
    root, _store, _repository, application = _parts(tmp_path)
    container = application.state.container
    request = SimpleNamespace(app=application)
    ai = get_or_build_ai_runtime(request, container)
    definition = next(
        item for item in ai.capability_registry_snapshot().definitions
        if item.capability_id == "analyze_source"
    )
    assert definition.tool_definition is not None
    assert definition.tool_definition.available is False
    assert getattr(application.state, "rebuild_job_lifecycle", None) is None
    assert getattr(application.state, "media_hands_runtime_resolution", None) is None

    _publish_policy(tmp_path, 1, enabled=True)

    assert get_or_build_ai_runtime(request, container) is ai
    assert getattr(application.state, "rebuild_job_lifecycle", None) is None
    assert getattr(application.state, "media_hands_runtime_resolution", None) is None


class _CrashAfterRecipeStepStore(SQLiteJobStore):
    def __init__(self, path: Path, step_name: str) -> None:
        super().__init__(path)
        self.step_name = step_name
        self.crashed = False

    def complete_media_recipe_step(self, job_id, **kwargs):
        result = super().complete_media_recipe_step(job_id, **kwargs)
        if not self.crashed and kwargs.get("step_name") == self.step_name:
            self.crashed = True
            raise _StepCommitCrash(f"crash after {self.step_name} receipt commit")
        return result


def test_disabled_policy_and_explicit_providerless_override_build_no_media_runtime_or_handler(tmp_path) -> None:
    root, store, repository, application = _parts(tmp_path)
    disabled = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    )
    assert disabled.runtime is None and disabled.reason == "policy_disabled"
    lifecycle = get_or_create_rebuild_job_lifecycle(
        application, repository, store, namespace_id="default"
    )
    assert "media_hands" not in lifecycle.job_types
    ai = build_ai_runtime(application.state.container, application=application)
    definition = next(
        item
        for item in ai.capability_registry_snapshot().definitions
        if item.capability_id == "analyze_source"
    )
    assert definition.tool_definition is not None and not definition.tool_definition.available

    enabled = default_personal_workbench_policy_snapshot()
    enabled["enabled"] = True
    root2, store2, repository2, application2 = _parts(
        tmp_path / "providerless", snapshot=enabled
    )
    application2.state.container.media_operation_provider = None
    providerless = configure_media_hands_runtime(
        application2, runtime_root=root2, object_store=store2, repository=repository2
    )
    assert providerless.runtime is None and providerless.reason == "provider_unavailable"
    assert repository2.sqlite.all() == ()

    root3, store3, repository3, application3 = _parts(
        tmp_path / "platform-providerless",
        snapshot=enabled,
        provider=_Provider(),
        platform_providers={},
    )
    platform_providerless = configure_media_hands_runtime(
        application3, runtime_root=root3, object_store=store3, repository=repository3
    )
    assert platform_providerless.runtime is None
    assert platform_providerless.reason == "platform_provider_unavailable"

    root4, store4, repository4, application4 = _parts(
        tmp_path / "output-verifierless",
        snapshot=enabled,
        provider=_Provider(),
        platform_providers={"bilibili": _PlatformProvider()},
    )
    application4.state.container.media_output_verifier = None
    verifierless = configure_media_hands_runtime(
        application4, runtime_root=root4, object_store=store4, repository=repository4
    )
    assert verifierless.runtime is None
    assert verifierless.reason == "output_verifier_unavailable"


def test_enabled_policy_builds_core_subtitle_provider_and_document_verifier_without_overrides(tmp_path) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    root, store, repository, application = _parts(tmp_path, snapshot=snapshot)

    resolution = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    )

    assert resolution.reason == "ready"
    assert resolution.runtime is not None
    assert resolution.runtime.handler.provider_identity == (
        "bilibili-official-subtitle", "bilibili-official-subtitle-r1"
    )


def test_enabled_local_ocr_adds_xiaohongshu_to_default_provider_router(tmp_path) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    root, store, repository, application = _parts(tmp_path, snapshot=snapshot)
    store.write(
        "local_ocr_provider_settings",
        "default",
        {
            "schema_version": "1.0.0",
            "id": "default",
            "enabled": True,
            "provider_name": "test-local-ocr",
            "command": [sys.executable, "{image_path}"],
            "explicit_enable_confirmed": True,
            "remote_processing": False,
            "memory_publication": "not_started",
            "updated_at": "2026-08-26T00:00:00Z",
        },
        expected_revision=0,
    )

    resolution = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    )

    assert resolution.runtime is not None
    assert resolution.runtime.handler.provider_identity[0] == "media-operation-provider-router"
    assert resolution.runtime.handler._provider.supported_platforms == frozenset(
        {"bilibili", "xiaohongshu"}
    )


def test_default_runtime_completes_xiaohongshu_image_set_through_one_job_and_document_authority(tmp_path) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    root, store, repository, application = _parts(
        tmp_path,
        snapshot=snapshot,
        platform_providers={
            "bilibili": _PlatformProvider(),
            "xiaohongshu": _XiaohongshuPlatformProvider(),
        },
        xiaohongshu_materializer=_XiaohongshuMaterializer(tmp_path),
        xiaohongshu_ocr_runner=_XiaohongshuOcr(),
    )
    runtime = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    ).runtime
    assert runtime is not None
    _publish_hands_selection(
        root / ".rebuild-data" / "jobs.sqlite3",
        command_id="xhs-runtime-image-effect-v2-selection",
    )
    arguments = {
        "input": {"kind": "text", "text": "https://www.xiaohongshu.com/explore/runtime-note", "source_ref": None},
        "intent": "organize",
        "output_profile": {"profile_id": "default", "revision": "1"},
        "resource_budget": {"max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60},
    }
    scope = {"project_id": "project-1"}
    initial = runtime.resolver.resolve(arguments, scope)
    assert initial.manifest is not None and initial.manifest_ref is not None
    _grant_snapshot(
        store=store,
        artifact=SimpleNamespace(public_ref=initial.manifest_ref, revision=initial.manifest_revision),
        manifest=initial.manifest,
        permission_id="xhs-runtime-permission",
    )
    granted = runtime.resolver.resolve(arguments, scope)
    assert granted.manifest is not None and granted.permission_snapshot is not None
    admission, operation_id, receipt = _dispatch_media_v2(
        runtime=runtime,
        application=application,
        database=root / ".rebuild-data" / "jobs.sqlite3",
        manifest=granted.manifest,
        manifest_ref=granted.manifest_ref,
        manifest_revision=granted.manifest_revision,
        idempotency_key="xhs-runtime-image-set-0001",
        permission_snapshot=granted.permission_snapshot,
    )
    assert operation_id.startswith("eff2_")
    output = receipt["output"]
    assert output["kind"] == "document" and "/documents/" in output["uri"]
    assert receipt["consumed"]["max_vision_frames"] == 1
    assert receipt["checkpoint"]["resume_step"] == "execute_operation"
    assert admission.record.payload["execution_version"] == "effect-v2"


def test_default_runtime_completes_xiaohongshu_video_through_one_job_and_document_authority(tmp_path) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    root, store, repository, application = _parts(
        tmp_path,
        snapshot=snapshot,
        platform_providers={
            "bilibili": _PlatformProvider(),
            "xiaohongshu": _XiaohongshuVideoPlatformProvider(),
        },
        local_asr_runner=_XiaohongshuAsr(),
        xiaohongshu_materializer=_XiaohongshuVideoMaterializer(tmp_path),
        xiaohongshu_ocr_runner=_XiaohongshuOcr(),
        xiaohongshu_video_runner=_XiaohongshuVideoRunner(tmp_path),
    )
    runtime = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    ).runtime
    assert runtime is not None
    _publish_hands_selection(
        root / ".rebuild-data" / "jobs.sqlite3",
        command_id="xhs-runtime-video-effect-v2-selection",
    )
    arguments = {
        "input": {"kind": "text", "text": "https://www.xiaohongshu.com/explore/runtime-video", "source_ref": None},
        "intent": "organize", "output_profile": {"profile_id": "default", "revision": "1"},
        "resource_budget": {"max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60},
    }
    scope = {"project_id": "project-1"}
    initial = runtime.resolver.resolve(arguments, scope)
    assert initial.manifest is not None and initial.manifest_ref is not None
    _grant_snapshot(
        store=store,
        artifact=SimpleNamespace(public_ref=initial.manifest_ref, revision=initial.manifest_revision),
        manifest=initial.manifest,
        permission_id="xhs-runtime-video-permission",
    )
    granted = runtime.resolver.resolve(arguments, scope)
    assert granted.manifest is not None and granted.permission_snapshot is not None
    admission, _operation_id, receipt = _dispatch_media_v2(
        runtime=runtime, application=application,
        database=root / ".rebuild-data" / "jobs.sqlite3",
        manifest=granted.manifest, manifest_ref=granted.manifest_ref,
        manifest_revision=granted.manifest_revision,
        idempotency_key="xhs-runtime-video-0001",
        permission_snapshot=granted.permission_snapshot,
    )
    output = receipt["output"]
    assert output["kind"] == "document" and "/documents/" in output["uri"]
    assert receipt["consumed"]["max_asr_audio_ms"] == 500
    assert receipt["consumed"]["max_vision_frames"] == 1
    assert admission.record.payload["execution_version"] == "effect-v2"


def test_default_runtime_completes_xiaohongshu_mixed_in_manifest_order_with_one_document(tmp_path) -> None:
    snapshot = default_personal_workbench_policy_snapshot(); snapshot["enabled"] = True
    root, store, repository, application = _parts(
        tmp_path, snapshot=snapshot,
        platform_providers={"bilibili": _PlatformProvider(), "xiaohongshu": _XiaohongshuMixedPlatformProvider()},
        local_asr_runner=_XiaohongshuAsr(),
        xiaohongshu_materializer=_XiaohongshuMixedMaterializer(tmp_path),
        xiaohongshu_ocr_runner=_XiaohongshuOcr(),
        xiaohongshu_video_runner=_XiaohongshuVideoRunner(tmp_path),
    )
    runtime = configure_media_hands_runtime(application, runtime_root=root, object_store=store, repository=repository).runtime
    assert runtime is not None
    _publish_hands_selection(
        root / ".rebuild-data" / "jobs.sqlite3",
        command_id="xhs-runtime-mixed-effect-v2-selection",
    )
    arguments = {
        "input": {"kind": "text", "text": "https://www.xiaohongshu.com/explore/runtime-mixed", "source_ref": None},
        "intent": "organize", "output_profile": {"profile_id": "default", "revision": "1"},
        "resource_budget": {"max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60},
    }
    scope = {"project_id": "project-1"}
    initial = runtime.resolver.resolve(arguments, scope)
    assert initial.manifest is not None and initial.manifest_ref is not None
    _grant_snapshot(store=store, artifact=SimpleNamespace(public_ref=initial.manifest_ref, revision=initial.manifest_revision), manifest=initial.manifest, permission_id="xhs-runtime-mixed-permission")
    granted = runtime.resolver.resolve(arguments, scope)
    assert granted.manifest is not None and granted.permission_snapshot is not None
    admission, _operation_id, receipt = _dispatch_media_v2(
        runtime=runtime, application=application,
        database=root / ".rebuild-data" / "jobs.sqlite3",
        manifest=granted.manifest, manifest_ref=granted.manifest_ref, manifest_revision=granted.manifest_revision,
        idempotency_key="xhs-runtime-mixed-0001", permission_snapshot=granted.permission_snapshot,
    )
    output = receipt["output"]
    assert output["kind"] == "document" and "/documents/" in output["uri"]
    assert receipt["consumed"]["max_asr_audio_ms"] == 500
    assert receipt["consumed"]["max_vision_frames"] == 3
    assert admission.record.payload["execution_version"] == "effect-v2"


def test_enabled_ai_capability_uses_core_effect_and_freezes_policy_revision(tmp_path) -> None:
    prepared = _prepare_media_v2_runtime(tmp_path)
    assert getattr(prepared.application.state, "rebuild_job_lifecycle", None) is None
    ai = build_ai_runtime(
        prepared.application.state.container,
        application=prepared.application,
    )
    definition = next(
        item
        for item in ai.capability_registry_snapshot().definitions
        if item.capability_id == "analyze_source"
    )
    assert definition.tool_definition is not None and definition.tool_definition.available

    capability = AnalyzeSourceCapability(
        resolver=prepared.runtime.resolver,
        provisioner=prepared.runtime,
        payloads=InMemoryTurnPayloadStore(),
        readiness=prepared.runtime.readiness,
        created_at=lambda: "2026-08-25T00:00:00Z",
        namespace_id="default",
    )
    request = {
        "turn_id": "turn-media-1",
        "tool_call_id": "tool-media-1",
        "operation_id": "operation-media-1",
        "idempotency_key": "media-runtime-idempotency-0001",
        "scope": {"project_id": "project-1"},
        "arguments": {
            "input": {
                "kind": "text",
                "text": "https://www.bilibili.com/video/BV1xx411c7mD",
                "source_ref": None,
            },
            "intent": "organize",
            "output_profile": {"profile_id": "default", "revision": "1"},
            "resource_budget": {"max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60},
        },
    }
    result = capability.invoke(request)
    assert result["result"]["status"] == "admitted"
    assert result["result"]["manifest_ref"].startswith(
        "crp://default/source-manifests/projects/project-1/"
    )
    job_id = str(result["result"]["job_id"])
    operation_id = _media_v2_operation_id(prepared.database, job_id)
    assert prepared.application.state.effect_runtime.log.get(operation_id).state is EffectState.PLANNED
    EffectRecoveryCoordinator(prepared.application.state.effect_runtime).recover_once(
        now=int(time.time()) + 1,
    )
    stored = SQLiteJobStore(prepared.database).read(job_id)
    assert stored is not None and stored.payload["status"] == "completed"
    assert stored.payload["media_hands"]["policy"]["revision"] == "personal-workbench-r1"
    assert prepared.provider.calls == 1
    assert prepared.runtime.readiness().ready
    assert getattr(prepared.application.state, "rebuild_job_lifecycle", None) is None


def test_derived_manifest_source_ref_rechecks_grant_and_observes_revoke(tmp_path) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    root, store, repository, application = _parts(
        tmp_path, snapshot=snapshot, provider=_Provider(),
        platform_providers={"bilibili": _PlatformProvider()},
        output_verifier=_CanonicalOutputVerifier(),
    )
    runtime = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    ).runtime
    assert runtime is not None
    arguments = {
        "input": {"kind": "text", "text": "https://www.bilibili.com/video/BV1xx411c7mD", "source_ref": None},
        "intent": "organize", "output_profile": {"profile_id": "default", "revision": "1"},
        "resource_budget": {"max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60},
    }
    scope = {"project_id": "project-1"}
    initial = runtime.resolver.resolve(arguments, scope)
    assert initial.manifest is not None and initial.manifest_ref is not None
    _grant_snapshot(
        store=store,
        artifact=SimpleNamespace(public_ref=initial.manifest_ref, revision=initial.manifest_revision),
        manifest=initial.manifest,
    )
    granted = runtime.resolver.resolve(arguments, scope)
    assert granted.manifest is not None and granted.manifest.permission.decision == "granted"
    assert granted.permission_snapshot is not None and granted.manifest_ref is not None
    by_ref = runtime.resolver.resolve(
        {**arguments, "input": {"kind": "source_ref", "text": None, "source_ref": granted.manifest_ref}},
        scope,
    )
    assert by_ref.permission_snapshot == granted.permission_snapshot
    SourcePermissionAuthority(store, namespace_id="default").revoke(
        project_id="project-1", permission_id="media-fixture-permission",
        actor_id="test-user", command_id="revoke-media-fixture-permission",
        created_at="2026-08-25T00:01:00Z", expected_revision=1,
    )
    revoked = runtime.resolver.resolve(
        {**arguments, "input": {"kind": "source_ref", "text": None, "source_ref": granted.manifest_ref}},
        scope,
    )
    assert revoked.manifest is not None and revoked.manifest.permission.decision == "denied"
    assert revoked.permission_snapshot is None


def test_revoked_grant_blocks_core_effect_before_provider_effect(tmp_path) -> None:
    """A durable admission snapshot cannot outlive its source-processing grant."""

    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    provider = _Provider()
    root, store, repository, application = _parts(
        tmp_path,
        snapshot=snapshot,
        provider=provider,
        platform_providers={"bilibili": _PlatformProvider()},
        output_verifier=_CanonicalOutputVerifier(),
    )
    resolution = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    )
    assert resolution.runtime is not None
    runtime = resolution.runtime
    _publish_hands_selection(
        root / ".rebuild-data" / "jobs.sqlite3",
        command_id="revoked-grant-effect-v2-selection",
    )
    arguments = {
        "input": {
            "kind": "text",
            "text": "https://www.bilibili.com/video/BV1xx411c7mD",
            "source_ref": None,
        },
        "intent": "organize",
        "output_profile": {"profile_id": "default", "revision": "1"},
        "resource_budget": {"max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60},
    }
    scope = {"project_id": "project-1"}
    initial = runtime.resolver.resolve(arguments, scope)
    assert initial.manifest is not None
    assert initial.manifest_ref is not None and initial.manifest_revision is not None
    _grant_snapshot(
        store=store,
        artifact=SimpleNamespace(
            public_ref=initial.manifest_ref,
            revision=initial.manifest_revision,
        ),
        manifest=initial.manifest,
        permission_id="revoked-before-worker-permission",
    )
    admitted_manifest = runtime.resolver.resolve(arguments, scope)
    assert admitted_manifest.manifest is not None
    assert admitted_manifest.manifest_ref is not None
    assert admitted_manifest.manifest_revision is not None
    assert admitted_manifest.permission_snapshot is not None
    assert admitted_manifest.manifest.permission.decision == "granted"
    admission = runtime.provision(
        manifest=admitted_manifest.manifest,
        manifest_ref=admitted_manifest.manifest_ref,
        manifest_revision=admitted_manifest.manifest_revision,
        operation="analyze_source",
        idempotency_key="revoked-before-worker-idempotency-0001",
        created_at="2026-08-25T00:00:00Z",
        permission_snapshot=admitted_manifest.permission_snapshot,
    )
    SourcePermissionAuthority(store, namespace_id="default").revoke(
        project_id="project-1",
        permission_id="revoked-before-worker-permission",
        actor_id="test-user",
        command_id="revoke-before-worker-provider-effect",
        created_at="2026-08-25T00:01:00Z",
        expected_revision=1,
    )

    operation_id = _media_v2_operation_id(
        root / ".rebuild-data" / "jobs.sqlite3",
        str(admission.record.payload["id"]),
    )
    coordinator = EffectRecoveryCoordinator(application.state.effect_runtime)
    now = int(time.time()) + 1
    coordinator.recover_once(now=now)
    assert application.state.effect_runtime.log.get(operation_id).state is EffectState.INFLIGHT
    coordinator.recover_once(now=now + 31)
    recovered = application.state.effect_runtime.log.get(operation_id)
    assert recovered.state is EffectState.UNKNOWN
    assert recovered.error_ref == "error:media-hands-execution-authorization-invalid"
    assert provider.calls == 0
    with sqlite3.connect(root / ".rebuild-data" / "jobs.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_provider_reservation"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_domain_receipt"
        ).fetchone()[0] == 0


def test_enabled_runtime_builds_real_core_platform_providers_when_not_overridden(tmp_path) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    root, store, repository, application = _parts(
        tmp_path, snapshot=snapshot, provider=_Provider(), output_verifier=_CanonicalOutputVerifier()
    )
    resolution = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    )
    assert resolution.runtime is not None
    assert resolution.runtime.resolver.platforms.registered_platforms == (
        "bilibili",
        "xiaohongshu",
    )
    assert resolution.runtime.resolver.executable_platforms == frozenset({"bilibili"})


def test_media_provider_without_explicit_platform_contract_fails_closed(tmp_path) -> None:
    class ProviderWithoutPlatforms:
        def execute(self, request):
            raise AssertionError(f"invalid provider executed: {request}")

    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    root, store, repository, application = _parts(
        tmp_path,
        snapshot=snapshot,
        provider=ProviderWithoutPlatforms(),
        output_verifier=_CanonicalOutputVerifier(),
    )
    resolution = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    )
    assert resolution.runtime is None
    assert resolution.reason == "provider_platform_contract_invalid"


def test_default_production_provider_completes_core_effect_with_injected_bounded_transport(tmp_path) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    catalog = "https://api.bilibili.com/x/player/v2?bvid=BV1xx411c7mD&cid=101"
    subtitle_url = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/zh.json"
    responses = {
        catalog: json.dumps({"code": 0, "data": {"subtitle": {"subtitles": [
            {"lan": "zh-Hans", "subtitle_url": subtitle_url}
        ]}}}),
        subtitle_url: json.dumps({"body": [
            {"from": 0, "to": 1.0, "content": "生产组合字幕"}
        ]}),
    }
    network_limits = []

    def network_factory(max_bytes, timeout, control):
        network_limits.append((max_bytes, timeout, control is not None))

        class Network:
            def fetch_text(self, url):
                if control is not None:
                    control()
                return responses[url]

        return Network()

    root, store, repository, application = _parts(
        tmp_path,
        snapshot=snapshot,
        platform_providers={"bilibili": _PlatformProvider()},
        network_factory=network_factory,
    )
    runtime = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    ).runtime
    assert runtime is not None
    _publish_hands_selection(
        root / ".rebuild-data" / "jobs.sqlite3",
        command_id="bounded-transport-effect-v2-selection",
    )
    arguments = {
        "input": {"kind": "text", "text": "https://www.bilibili.com/video/BV1xx411c7mD", "source_ref": None},
        "intent": "organize",
        "output_profile": {"profile_id": "default", "revision": "1"},
        "resource_budget": {"max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60},
    }
    scope = {"project_id": "project-1"}
    initial = runtime.resolver.resolve(arguments, scope)
    assert initial.manifest is not None and initial.manifest_ref and initial.manifest_revision
    _grant_snapshot(
        store=store,
        artifact=SimpleNamespace(public_ref=initial.manifest_ref, revision=initial.manifest_revision),
        manifest=initial.manifest,
        permission_id="production-default-provider",
    )
    granted = runtime.resolver.resolve(arguments, scope)
    assert granted.manifest is not None and granted.permission_snapshot is not None
    admission, _operation_id, receipt = _dispatch_media_v2(
        runtime=runtime, application=application,
        database=root / ".rebuild-data" / "jobs.sqlite3",
        manifest=granted.manifest,
        manifest_ref=str(granted.manifest_ref),
        manifest_revision=str(granted.manifest_revision),
        idempotency_key="production-default-provider-e2e-0001",
        permission_snapshot=granted.permission_snapshot,
    )
    assert admission.record.payload["execution_version"] == "effect-v2"
    assert receipt["output"]["kind"] == "document"
    assert receipt["consumed"]["max_download_bytes"] > 0
    assert receipt["consumed"]["max_asr_audio_ms"] == 0
    assert len(network_limits) == 2
    assert all(value[0] > 0 and value[1] > 0 for value in network_limits)
    assert all(value[2] for value in network_limits)


def test_default_production_provider_runs_governed_asr_fallback_through_core_effect(
    tmp_path,
) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    catalog = "https://api.bilibili.com/x/player/v2?bvid=BV1xx411c7mD&cid=101"
    playurl = "https://api.bilibili.com/x/player/playurl?bvid=BV1xx411c7mD&cid=101&fnval=16&fnver=0&fourk=0"
    responses = {
        catalog: json.dumps({"code": 0, "data": {"subtitle": {"subtitles": []}}}),
        playurl: json.dumps({"code": 0, "data": {"dash": {"audio": [{
            "baseUrl": "https://cdn.bilivideo.com/audio.m4s", "bandwidth": 64000
        }]}}}),
    }

    def network_factory(max_bytes, timeout, control):
        class Network:
            def fetch_text(self, url):
                if control is not None:
                    control()
                return responses[url]
        return Network()

    class Binary:
        def download(self, url, **kwargs):
            path = tmp_path / ".rebuild-data" / "media-hands" / kwargs["relative_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return DownloadedBinary(path, 5, "audio/mp4")

        def staged_path(self, relative_path):
            return tmp_path / ".rebuild-data" / "media-hands" / relative_path

    class Asr:
        provider_revision = "local-faster-whisper-cli-r1"

        def assert_ready(self):
            return None

        def probe_duration(self, path, **kwargs):
            return 2500

        def transcribe_known_duration(self, path, **kwargs):
            transcript = {
                "title": "字幕视频", "language": "zh", "duration_seconds": 2.5,
                "source": "local_asr", "segments": [
                    {"start_seconds": 0.0, "end_seconds": 2.5, "text": "本地转写"}
                ],
            }
            return GovernedLocalAsrOutcome(
                transcript, ({"chunk_id": "local-1", "start": 0.0, "end": 2.5, "text": "本地转写"},),
                2500, 10, "local-faster-whisper", "local-faster-whisper-cli-r1",
            )

    root, store, repository, application = _parts(
        tmp_path, snapshot=snapshot,
        platform_providers={"bilibili": _PlatformProvider()},
        network_factory=network_factory, binary_network=Binary(), local_asr_runner=Asr(),
    )
    runtime = configure_media_hands_runtime(
        application, runtime_root=root, object_store=store, repository=repository
    ).runtime
    assert runtime is not None
    _publish_hands_selection(
        root / ".rebuild-data" / "jobs.sqlite3",
        command_id="asr-fallback-effect-v2-selection",
    )
    arguments = {
        "input": {"kind": "text", "text": "https://www.bilibili.com/video/BV1xx411c7mD", "source_ref": None},
        "intent": "organize", "output_profile": {"profile_id": "default", "revision": "1"},
        "resource_budget": {"max_assets": 8, "max_bytes": 1_000_000, "max_seconds": 60},
    }
    scope = {"project_id": "project-1"}
    initial = runtime.resolver.resolve(arguments, scope)
    assert initial.manifest is not None and initial.manifest_ref and initial.manifest_revision
    _grant_snapshot(
        store=store,
        artifact=SimpleNamespace(public_ref=initial.manifest_ref, revision=initial.manifest_revision),
        manifest=initial.manifest, permission_id="production-asr-fallback",
    )
    granted = runtime.resolver.resolve(arguments, scope)
    admission, _operation_id, receipt = _dispatch_media_v2(
        runtime=runtime, application=application,
        database=root / ".rebuild-data" / "jobs.sqlite3",
        manifest=granted.manifest, manifest_ref=str(granted.manifest_ref),
        manifest_revision=str(granted.manifest_revision),
        idempotency_key="production-asr-fallback-e2e-0001",
        permission_snapshot=granted.permission_snapshot,
    )
    assert admission.record.payload["execution_version"] == "effect-v2"
    assert receipt["output"]["kind"] == "document"
    assert receipt["consumed"]["max_asr_audio_ms"] == 2500
    assert receipt["checkpoint"]["resume_step"] == "execute_operation"
    assert receipt["execution_receipt_ref"].startswith(
        "crp://default/jobs/"
    )
    assert receipt["execution_receipt_ref"].endswith(
        "/receipts/bilibili-official-subtitle-r1"
    )
    assert receipt["output"]["uri"].startswith("crp://default/documents/")
    assert set(receipt["consumed"]) == {
        "max_download_bytes", "max_asr_audio_ms", "max_vision_frames",
        "max_media_cpu_ms", "max_wall_ms", "max_model_input_tokens",
        "max_model_output_tokens",
    }







def test_durable_policy_authority_enables_new_app_and_disabled_revision_fails_closed(
    tmp_path,
) -> None:
    provider = _Provider()
    _publish_policy(tmp_path, 1, enabled=True)
    root, store, repository, first_app = _parts(
        tmp_path,
        provider=provider,
        platform_providers={"bilibili": _PlatformProvider()},
        output_verifier=_CanonicalOutputVerifier(),
    )
    first = configure_media_hands_runtime(
        first_app, runtime_root=root, object_store=store, repository=repository
    )
    assert first.reason == "ready"
    assert first.runtime is not None
    assert first.runtime.policy.revision == "personal-workbench-r1"
    _, restarted_store, restarted_repository, restarted_app = _parts(
        tmp_path,
        provider=_Provider(),
        platform_providers={"bilibili": _PlatformProvider()},
        output_verifier=_CanonicalOutputVerifier(),
    )
    restarted = configure_media_hands_runtime(
        restarted_app,
        runtime_root=root,
        object_store=restarted_store,
        repository=restarted_repository,
    )
    assert restarted.reason == "ready"
    assert restarted.runtime is not None
    assert restarted.runtime.policy.revision == "personal-workbench-r1"

    _publish_policy(tmp_path, 2, enabled=False)
    manifest = SourceManifestCodec.decode(
        json.loads(
            (ROOT / "core-contracts/rebuild/source-processing/fixtures/bilibili-video.json").read_text(
                encoding="utf-8"
            )
        )
    )
    with pytest.raises(MediaHandsRuntimeUnavailable, match="changed before admission"):
        first.runtime.provision(
            manifest=manifest,
            manifest_ref="crp://default/source-manifests/live-disable-r1",
            manifest_revision="manifest-r1",
            operation="analyze_source",
            idempotency_key="live-disable-policy-idempotency-0001",
            created_at="2026-08-25T22:40:00Z",
            permission_snapshot=SourcePermissionSnapshot(
                project_id="project-1",
                manifest_ref="crp://default/source-manifests/live-disable-r1",
                manifest_revision="manifest-r1",
                grant_ref="crp://default/source-permissions/projects/project-1/live-disable/r1",
                grant_revision="r1",
                revocation_generation=0,
            ),
        )
    _, disabled_store, disabled_repository, disabled_app = _parts(
        tmp_path,
        provider=_Provider(),
        platform_providers={"bilibili": _PlatformProvider()},
        output_verifier=_CanonicalOutputVerifier(),
    )
    disabled = configure_media_hands_runtime(
        disabled_app,
        runtime_root=root,
        object_store=disabled_store,
        repository=disabled_repository,
    )
    assert disabled.runtime is None
    assert disabled.reason == "policy_disabled"
