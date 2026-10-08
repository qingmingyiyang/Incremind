"""Dual-gated deterministic mixed-media providers for packaged Electron E2E.

The fixture is unreachable unless the Electron supervisor copies its freshly
generated desktop nonce into the fixture nonce.  It never installs a second
authority: production resolver, permission, Job, recipe, Document and output
verification remain in use.
"""

from __future__ import annotations

from pathlib import Path
import os

from backend.api.governed_local_asr import GovernedLocalAsrOutcome
from backend.api.governed_local_ocr import GovernedLocalOcrOutcome
from backend.api.source_resolution_evidence import SourceResolutionEvidenceRepository
from backend.api.governed_staged_video import (
    GovernedStagedVideoDerivative,
    GovernedStagedVideoOutcome,
)
from backend.api.xiaohongshu_asset_materializer import (
    XiaohongshuMaterializationOutcome,
    XiaohongshuStagedAsset,
)
from core.media_hands.policy_source import default_personal_workbench_policy_snapshot
from core.source_processing.codec import SourceManifestCodec


FIXTURE_NONCE_ENV = "CHRIPTMAS_E2E_MIXED_MEDIA_FIXTURE_NONCE"
DESKTOP_NONCE_ENV = "CHRIPTMAS_DESKTOP_NONCE"
FIXTURE_URL = "https://www.xiaohongshu.com/explore/e2e000000000000000000001"


def install_mixed_media_e2e_fixture(
    container: object,
    runtime_root: Path,
    *,
    object_store: object,
    namespace_id: str,
) -> bool:
    fixture_nonce = os.environ.get(FIXTURE_NONCE_ENV, "")
    desktop_nonce = os.environ.get(DESKTOP_NONCE_ENV, "")
    if not fixture_nonce or not desktop_nonce or fixture_nonce != desktop_nonce:
        return False
    policy = default_personal_workbench_policy_snapshot()
    policy["enabled"] = True
    policy["revision"] = "packaged-mixed-media-e2e-r1"
    values = {
        "_media_hands_policy_snapshot_for_test": policy,
        "platform_manifest_providers": {
            "bilibili": _RejectingBilibiliPlatformProvider(),
            "xiaohongshu": _MixedPlatformProvider(
                SourceResolutionEvidenceRepository(
                    object_store, namespace_id=namespace_id
                )
            ),
        },
        "media_xiaohongshu_materializer": _MixedMaterializer(runtime_root),
        "media_xiaohongshu_ocr_runner": _OcrRunner(),
        "media_xiaohongshu_video_runner": _VideoRunner(runtime_root),
        "media_local_asr_runner": _AsrRunner(),
    }
    for name, value in values.items():
        object.__setattr__(container, name, value)
    return True


class _RejectingBilibiliPlatformProvider:
    """Complete the frozen platform registry without supplying a Bilibili fixture."""

    def provide(self, text: str, *, project_id: str):
        raise ValueError("mixed_media_e2e_fixture_bilibili_disabled")


class _MixedPlatformProvider:
    def __init__(self, evidence: SourceResolutionEvidenceRepository) -> None:
        self._evidence = evidence

    def provide(self, text: str, *, project_id: str):
        if text != FIXTURE_URL or project_id != "default":
            raise ValueError("mixed_media_e2e_fixture_input_invalid")
        proof = self._evidence.put(
            project_id=project_id,
            evidence_id="xhs-e2e-mixed-metadata-r1",
            kind="xiaohongshu_metadata_resolution",
            payload={
                "source_id": "xhs-e2e-mixed",
                "input_identity": text,
                "resolver_revision": "xhs-e2e-r1",
                "normalizer_revision": "mixed-r1",
                "asset_kinds": ["image", "video", "image", "text"],
            },
        )
        evidence = proof.public_ref
        shapes = (
            ("image-a", "image", "image/jpeg", "gallery"),
            ("video-a", "video", "video/mp4", "gallery"),
            ("image-b", "image", "image/jpeg", "gallery"),
            ("text-a", "text", "text/plain", "caption"),
        )
        assets = []
        for ordinal, (asset_id, kind, media_type, role) in enumerate(shapes):
            relations = [] if ordinal + 1 == len(shapes) else [
                {"relation": "next", "target_asset_id": shapes[ordinal + 1][0]}
            ]
            assets.append({
                "asset_id": asset_id,
                "ordinal": ordinal,
                "kind": kind,
                "media_type": media_type,
                "role": role,
                "locator": None,
                "source_ref": f"crp://default/sources/xhs-e2e/assets/{asset_id}",
                "relations": relations,
                "evidence_refs": [f"{evidence}/{asset_id}"],
            })
        return SourceManifestCodec.decode({
            "schema_version": "1.0.0",
            "source_id": "xhs-e2e-mixed",
            "source_ref": "crp://default/sources/xhs-e2e-mixed",
            "platform": "xiaohongshu",
            "input_identity": text,
            "resolver_revision": "xhs-e2e-r1",
            "normalizer_revision": "mixed-r1",
            "content_kind": "mixed",
            "body": {"kind": "text", "text": "受控混合素材正文", "source_ref": None},
            "metadata": {"caption": "受控 mixed Electron E2E"},
            "permission": {"decision": "unknown", "evidence_refs": [evidence]},
            "provenance_refs": [evidence],
            "assets": assets,
        })


class _MixedMaterializer:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def materialize(self, manifest, *, job_id, max_download_bytes, timeout_seconds, control_check=None):
        assets = []
        for asset in manifest.assets:
            if control_check is not None:
                control_check()
            if asset.kind == "text":
                assets.append(XiaohongshuStagedAsset(asset.asset_id, asset.ordinal, "text", "text/plain", None, 0))
                continue
            suffix = "mp4" if asset.kind == "video" else "jpg"
            media_type = "video/mp4" if asset.kind == "video" else "image/jpeg"
            target = self._root / ".rebuild-data" / "media-hands" / "e2e-mixed" / f"{asset.ordinal}.{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"fixture-{asset.kind}-{asset.ordinal}".encode("ascii"))
            assets.append(XiaohongshuStagedAsset(asset.asset_id, asset.ordinal, asset.kind, media_type, str(target), target.stat().st_size))
        total = sum(item.byte_count for item in assets)
        if total > max_download_bytes or timeout_seconds <= 0:
            raise ValueError("mixed_media_e2e_fixture_budget_exhausted")
        return XiaohongshuMaterializationOutcome(tuple(assets), total)


class _OcrRunner:
    provider_revision = "packaged-e2e-ocr-r1"

    def assert_ready(self):
        return None

    def extract_text(self, path, *, media_type, remaining_wall_ms, remaining_media_cpu_ms, control_check=None):
        if not Path(path).is_file() or media_type != "image/jpeg":
            raise ValueError("mixed_media_e2e_fixture_ocr_input_invalid")
        if control_check is not None:
            control_check()
        ordinal = Path(path).stem
        return GovernedLocalOcrOutcome(f"图片文字 {ordinal}", "packaged-e2e-ocr", self.provider_revision, 1)


class _VideoRunner:
    provider_revision = "packaged-e2e-video-r1"
    max_frames = 1

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def assert_ready(self):
        return None

    def derive(self, staged_video, *, job_id, media_type, remaining_wall_ms, remaining_media_cpu_ms, asset_key=None, control_check=None):
        if not Path(staged_video).is_file() or media_type != "video/mp4":
            raise ValueError("mixed_media_e2e_fixture_video_input_invalid")
        if control_check is not None:
            control_check()
        root = self._root / ".rebuild-data" / "media-hands" / "e2e-mixed" / "derived" / (asset_key or "single")
        root.mkdir(parents=True, exist_ok=True)
        audio = root / "audio.wav"
        frame = root / "frame-001.jpg"
        audio.write_bytes(b"fixture-audio")
        frame.write_bytes(b"fixture-frame")
        return GovernedStagedVideoOutcome(
            GovernedStagedVideoDerivative("audio", 0, "audio/wav", str(audio), audio.stat().st_size),
            (GovernedStagedVideoDerivative("frame", 0, "image/jpeg", str(frame), frame.stat().st_size),),
            1,
        )


class _AsrRunner:
    provider_revision = "packaged-e2e-asr-r1"

    def assert_ready(self):
        return None

    def probe_duration(self, audio_path, *, max_audio_ms, max_wall_ms=None, control_check=None):
        if not Path(audio_path).is_file() or max_audio_ms < 500:
            raise ValueError("mixed_media_e2e_fixture_asr_input_invalid")
        if control_check is not None:
            control_check()
        return 500

    def transcribe_known_duration(self, audio_path, *, title, duration_ms, max_wall_ms, control_check=None):
        if duration_ms != 500 or max_wall_ms <= 0:
            raise ValueError("mixed_media_e2e_fixture_asr_budget_invalid")
        if control_check is not None:
            control_check()
        transcript = {
            "title": title,
            "language": "zh",
            "source": "local_asr",
            "segments": [{"start_seconds": 0.0, "end_seconds": 0.5, "text": "视频转写文字"}],
        }
        return GovernedLocalAsrOutcome(transcript, ({"text": "视频转写文字"},), 500, 1, "packaged-e2e-asr", self.provider_revision)
