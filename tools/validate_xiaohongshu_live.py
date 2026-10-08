from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
from typing import Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from backend.api.governed_local_ocr import GovernedLocalOcrError, GovernedLocalOcrRunner
from backend.api.xiaohongshu_asset_materializer import (
    XiaohongshuAssetMaterializationError,
    build_xiaohongshu_asset_materializer,
)
from backend.api.xiaohongshu_platform_provider import (
    XiaohongshuMetadataProviderError,
    build_xiaohongshu_anonymous_metadata_platform_provider,
)
from backend.security import NetworkEgressProfileError, NetworkEgressProfileStore
from rebuild.product_core.local_ocr_provider_settings import LocalOcrProviderSettings
from rebuild.source_processing import SourceManifestCodec
from rebuild.storage_provider import JsonObjectStore


_PUBLIC_CODES = frozenset({
    "invalid_source", "network_denied", "unsupported_source", "metadata_unavailable", "metadata_identity_mismatch",
    "manifest_drift", "asset_metadata_unavailable", "asset_network_denied", "asset_locator_denied",
    "asset_materialization_interrupted", "asset_download_budget_exhausted", "asset_media_type_invalid",
    "local_ocr_not_ready", "local_ocr_empty", "local_ocr_timed_out",
    "network_profile_invalid",
})


def run_live_gate(
    url: str,
    *,
    materialize: bool,
    ocr: bool,
    loopback_connect_address: str | None = None,
    loopback_connect_port: int | None = None,
    confirm_loopback_connect: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "gate": "xiaohongshu_live_image_set",
        "status": "environment_unverified",
        "metadata": "not_run",
        "materialization": "not_run",
        "ocr": "not_run",
        "cleanup_scope": "normal_exit",
    }
    with ExitStack() as stack:
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="chriptmas-xhs-live-")))
        objects = JsonObjectStore(temporary / "objects", namespace_id="default")
        profile_store = NetworkEgressProfileStore(temporary)
        network_profile = profile_store.get().profile
        if loopback_connect_address is not None or loopback_connect_port is not None:
            try:
                network_profile = profile_store.update(
                    mode="loopback_http_connect",
                    literal_address=loopback_connect_address,
                    port=loopback_connect_port,
                    confirm_enable=confirm_loopback_connect,
                    expected_revision=0,
                ).profile
            except NetworkEgressProfileError:
                result["status"] = "input_invalid"
                result["error_code"] = "network_profile_invalid"
                return result
        result["network_mode"] = network_profile.mode
        result["network_profile_revision"] = network_profile.revision
        try:
            manifest = build_xiaohongshu_anonymous_metadata_platform_provider(
                objects, namespace_id="default", network_profile=network_profile
            ).provide(url, project_id="xhs-live-gate")
        except XiaohongshuMetadataProviderError as error:
            result["status"] = metadata_failure_status(error)
            result["metadata"] = "blocked"
            result["error_code"] = public_error_code(error)
            return result
        result.update({
            "metadata": "resolved",
            "content_kind": manifest.content_kind,
            "asset_count": len(manifest.assets),
            "asset_kinds": [asset.kind for asset in manifest.assets],
        })
        if manifest.content_kind != "image_set":
            result["status"] = "sample_not_image_set"
            return result
        if not materialize:
            result["status"] = "metadata_passed"
            return result
        granted = _gate_only_grant_for_ephemeral_manifest(manifest)
        staging = temporary / "staging"
        try:
            outcome = build_xiaohongshu_asset_materializer(
                staging, network_profile=network_profile
            ).materialize(
                granted,
                job_id="media_hands:xhs-live-gate:analyze_source",
                max_download_bytes=32 * 1024 * 1024,
                timeout_seconds=60.0,
            )
        except XiaohongshuAssetMaterializationError as error:
            result["status"] = "environment_unverified" if str(error) in {"asset_metadata_unavailable", "asset_network_denied", "asset_materialization_interrupted"} else "platform_blocked"
            result["materialization"] = "blocked"
            result["error_code"] = public_error_code(error)
            return result
        result.update({
            "materialization": "completed",
            "downloaded_asset_count": len(outcome.assets),
            "downloaded_bytes": outcome.total_download_bytes,
        })
        if not ocr:
            result["status"] = "materialization_passed"
            return result
        settings = LocalOcrProviderSettings(
            status="ready", enabled=True, provider_name="builtin-windows-ocr",
            command=("builtin:windows-ocr",), diagnostic="ready",
            explicit_enable_required=True, remote_processing=False, memory_publication="not_started",
        )
        runner = GovernedLocalOcrRunner(staging, settings_reader=lambda: settings)
        recognized = 0
        try:
            runner.assert_ready()
            for asset in outcome.assets:
                if asset.kind != "image" or not asset.staged_path or not asset.media_type:
                    continue
                text = runner.extract_text(
                    Path(asset.staged_path), media_type=asset.media_type,
                    remaining_wall_ms=30_000, remaining_media_cpu_ms=30_000,
                ).text
                recognized += int(bool(text.strip()))
        except GovernedLocalOcrError as error:
            result["status"] = "product_blocked"
            result["ocr"] = "blocked"
            result["error_code"] = public_error_code(error)
            return result
        result.update({"status": "passed", "ocr": "completed", "recognized_asset_count": recognized})
        return result


def public_error_code(error: BaseException) -> str:
    code = str(error)
    return code if code in _PUBLIC_CODES else "redacted_provider_error"


def metadata_failure_status(error: BaseException) -> str:
    if str(error) == "invalid_source":
        return "input_invalid"
    if str(error) == "network_denied":
        return "environment_unverified"
    return "platform_blocked"


def _gate_only_grant_for_ephemeral_manifest(manifest):
    """Enable execution only inside this tool's disposable store.

    This is not a production SourcePermission receipt and must never be imported
    by runtime composition.
    """
    payload = SourceManifestCodec.encode(manifest)
    payload["permission"] = {
        "decision": "granted",
        "evidence_refs": list(manifest.permission.evidence_refs),
    }
    return SourceManifestCodec.decode(payload)


def gate_exit_code(result: Mapping[str, object]) -> int:
    return 0 if result.get("status") in {"passed", "metadata_passed", "materialization_passed"} else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Privacy-safe public Xiaohongshu image-set live Gate")
    parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--loopback-connect-address")
    parser.add_argument("--loopback-connect-port", type=int)
    parser.add_argument("--confirm-loopback-connect", action="store_true")
    args = parser.parse_args()
    if (args.loopback_connect_address is None) != (args.loopback_connect_port is None):
        parser.error("loopback CONNECT address and port must be supplied together")
    if args.confirm_loopback_connect and args.loopback_connect_address is None:
        parser.error("loopback CONNECT confirmation requires an endpoint")
    if args.loopback_connect_address is not None and not args.confirm_loopback_connect:
        parser.error("loopback CONNECT endpoint requires explicit confirmation")
    url = sys.stdin.readline().strip()
    result = run_live_gate(
        url,
        materialize=args.materialize or args.ocr,
        ocr=args.ocr,
        loopback_connect_address=args.loopback_connect_address,
        loopback_connect_port=args.loopback_connect_port,
        confirm_loopback_connect=args.confirm_loopback_connect,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return gate_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
