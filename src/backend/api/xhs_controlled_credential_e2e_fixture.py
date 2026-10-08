"""Dual-nonce deterministic transports for the packaged XHS credential Gates.

The fixture supplies pinned metadata and binary transports plus a readiness-only
OCR sentinel.  It deliberately does not create credentials, grants, manifests,
Jobs, OCR output, or a replacement platform provider: the production controlled
runtime continues to own each wire lease and the production provider continues
to own the Manifest.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
from time import monotonic, sleep

from backend.api.governed_local_ocr import GovernedLocalOcrError
from backend.api.xiaohongshu_controlled_credential_runtime import (
    SafeControlledCookieTextNetworkAdapter,
)
from backend.security.network_adapter import (
    BoundedHttpResponse,
    NetworkBoundaryError,
    PinnedBinaryDownloadRequest,
    PinnedHttpRequest,
    SafeBinaryDownloadAdapter,
    StreamedBinaryDownloadResponse,
)
from core.media_hands.policy_source import default_personal_workbench_policy_snapshot


FIXTURE_NONCE_ENV = "CHRIPTMAS_E2E_XHS_CREDENTIAL_FIXTURE_NONCE"
REAL_OCR_NONCE_ENV = "CHRIPTMAS_E2E_XHS_CREDENTIAL_REAL_OCR_NONCE"
DESKTOP_NONCE_ENV = "CHRIPTMAS_DESKTOP_NONCE"
FIXTURE_URL = "https://www.xiaohongshu.com/explore/e2e000000000000000000002"
FIXTURE_NOTE_ID = "e2e000000000000000000002"
FIXTURE_PUBLIC_IP = "93.184.216.34"
# This is test data only.  The fixture never writes it to a store, log, or
# response body; the packaged Gate grants this exact value through SecretStore.
FIXTURE_COOKIE_CANARY = "xhs-e2e-controlled-cookie-canary-r1"
FIXTURE_BINARY_ARRIVED_RELATIVE_PATH = (
    ".rebuild-data/xhs-controlled-credential-e2e/binary-arrived.json"
)
FIXTURE_BINARY_RELEASE_RELATIVE_PATH = (
    ".rebuild-data/xhs-controlled-credential-e2e/binary-release"
)
FIXTURE_BINARY_TRACE_RELATIVE_PATH = (
    ".rebuild-data/xhs-controlled-credential-e2e/binary-trace.json"
)
_FIXTURE_BINARY_WAIT_SECONDS = 15.0


@dataclass(slots=True)
class _FixtureRequestCounter:
    count: int = 0
    binary_count: int = 0


class _ReadinessOnlyOcrRunner:
    """Register the image provider without fabricating post-download OCR."""

    provider_revision = "xhs-controlled-binary-fence-no-ocr-v1"

    def assert_ready(self) -> None:
        return None

    def extract_text(self, *args: object, **kwargs: object) -> object:
        raise GovernedLocalOcrError("xhs_controlled_binary_fixture_ocr_forbidden")


class _FixtureSafeBinaryDownloadAdapter(SafeBinaryDownloadAdapter):
    def __init__(self, *args: object, trace: Path, **kwargs: object) -> None:
        self._fixture_trace = trace
        super().__init__(*args, **kwargs)

    def _download_controlled(self, *args: object, **kwargs: object):
        self._fixture_trace.parent.mkdir(parents=True, exist_ok=True)
        self._fixture_trace.write_text(
            json.dumps({"schema_version": "1.0.0", "phase": "binary_adapter_entered"}),
            encoding="utf-8",
        )
        return super()._download_controlled(*args, **kwargs)


def install_xhs_controlled_credential_e2e_fixture(container: object) -> bool:
    """Install the deterministic adapter only for a supervisor-owned nonce.

    The Electron supervisor owns ``CHRIPTMAS_DESKTOP_NONCE``.  Requiring the
    exact second nonce makes the fixture unavailable to ordinary sidecars and
    prevents a generic test-mode toggle from widening controlled credentials.
    """

    fixture_nonce = os.environ.get(FIXTURE_NONCE_ENV, "")
    desktop_nonce = os.environ.get(DESKTOP_NONCE_ENV, "")
    if not fixture_nonce or not desktop_nonce or fixture_nonce != desktop_nonce:
        return False
    policy = default_personal_workbench_policy_snapshot()
    policy["enabled"] = True
    policy["revision"] = "packaged-xhs-controlled-credential-e2e-r1"
    counter = _FixtureRequestCounter()
    arrived, release = fixture_binary_marker_paths(container)
    trace = Path(getattr(container, "root_dir")).resolve(strict=False) / Path(
        FIXTURE_BINARY_TRACE_RELATIVE_PATH
    )
    # The supervisor-owned dual nonce is the only path permitted to clear an
    # old inter-process marker.  A restarted Gate must observe a fresh binary
    # arrival rather than accidentally releasing a prior attempt.
    arrived.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    trace.unlink(missing_ok=True)
    object.__setattr__(container, "_media_hands_policy_snapshot_for_test", policy)
    object.__setattr__(
        container,
        "_e2e_xhs_controlled_metadata_network",
        SafeControlledCookieTextNetworkAdapter(
            resolver=_fixture_resolver,
            transport=_fixture_transport(counter, trace),
        ),
    )
    object.__setattr__(
        container,
        "_e2e_xhs_controlled_binary_network",
        _FixtureSafeBinaryDownloadAdapter(
            _fixture_binary_staging_root(container),
            trace=trace,
            resolver=_fixture_binary_resolver(trace),
            transport=_fixture_binary_transport(counter, arrived, release, trace),
            allowed_host_suffixes=("xhscdn.com",),
            max_redirects=0,
            timeout_seconds=_FIXTURE_BINARY_WAIT_SECONDS,
        ),
    )
    if os.environ.get(REAL_OCR_NONCE_ENV, "") != desktop_nonce:
        object.__setattr__(
            container, "media_xiaohongshu_ocr_runner", _ReadinessOnlyOcrRunner()
        )
    # Count only, never request headers or a Cookie-bearing request object.
    object.__setattr__(container, "_e2e_xhs_controlled_credential_counter", counter)
    return True


def controlled_credential_fixture_transport_call_count(container: object) -> int:
    """Return the non-secret in-memory transport count for the packaged Gate."""

    counter = getattr(container, "_e2e_xhs_controlled_credential_counter", None)
    return counter.count if isinstance(counter, _FixtureRequestCounter) else 0


def controlled_credential_fixture_binary_transport_call_count(container: object) -> int:
    """Return the binary transport count without retaining request metadata."""

    counter = getattr(container, "_e2e_xhs_controlled_credential_counter", None)
    return counter.binary_count if isinstance(counter, _FixtureRequestCounter) else 0


def installed_controlled_metadata_network(
    container: object,
) -> SafeControlledCookieTextNetworkAdapter | None:
    """Return the fixture adapter only while the dual nonce remains valid."""

    fixture_nonce = os.environ.get(FIXTURE_NONCE_ENV, "")
    desktop_nonce = os.environ.get(DESKTOP_NONCE_ENV, "")
    candidate = getattr(container, "_e2e_xhs_controlled_metadata_network", None)
    if (
        fixture_nonce
        and fixture_nonce == desktop_nonce
        and isinstance(candidate, SafeControlledCookieTextNetworkAdapter)
    ):
        return candidate
    return None


def installed_controlled_binary_network(container: object) -> SafeBinaryDownloadAdapter | None:
    """Return the fixture binary adapter only while the dual nonce remains valid."""

    fixture_nonce = os.environ.get(FIXTURE_NONCE_ENV, "")
    desktop_nonce = os.environ.get(DESKTOP_NONCE_ENV, "")
    candidate = getattr(container, "_e2e_xhs_controlled_binary_network", None)
    if (
        fixture_nonce
        and fixture_nonce == desktop_nonce
        and isinstance(candidate, SafeBinaryDownloadAdapter)
    ):
        return candidate
    return None


def fixture_binary_marker_paths(container: object) -> tuple[Path, Path]:
    """Return the non-secret binary Gate coordination markers under Vault root."""

    root = Path(getattr(container, "root_dir")).resolve(strict=False)
    arrived = root / Path(FIXTURE_BINARY_ARRIVED_RELATIVE_PATH)
    release = root / Path(FIXTURE_BINARY_RELEASE_RELATIVE_PATH)
    return arrived, release


def _fixture_binary_staging_root(container: object) -> Path:
    return Path(getattr(container, "root_dir")).resolve(strict=False) / ".rebuild-data" / "media-hands"


def _fixture_resolver(host: str, port: int) -> tuple[str, ...]:
    if host != "www.xiaohongshu.com" or port != 443:
        raise NetworkBoundaryError("xhs_controlled_credential_fixture_target_invalid")
    return (FIXTURE_PUBLIC_IP,)


def _fixture_transport(counter: _FixtureRequestCounter, trace: Path):
    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        if (
            request.scheme != "https"
            or request.host != "www.xiaohongshu.com"
            or request.port != 443
            or request.target != f"/explore/{FIXTURE_NOTE_ID}"
            or request.addresses != (FIXTURE_PUBLIC_IP,)
            or request.headers.get("Cookie") != FIXTURE_COOKIE_CANARY
        ):
            raise NetworkBoundaryError("xhs_controlled_credential_fixture_request_denied")
        counter.count += 1
        _write_fixture_trace(trace, "metadata_wire_completed", counter)
        return BoundedHttpResponse(
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            _fixture_html().encode("utf-8"),
        )

    return transport


def _fixture_binary_resolver(trace: Path):
    def resolve(host: str, port: int) -> tuple[str, ...]:
        if host != "img.xhscdn.com" or port != 443:
            raise NetworkBoundaryError("xhs_controlled_credential_fixture_binary_target_invalid")
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text(
            json.dumps({"schema_version": "1.0.0", "phase": "binary_dns_validated"}),
            encoding="utf-8",
        )
        return (FIXTURE_PUBLIC_IP,)

    return resolve


def _fixture_binary_transport(
    counter: _FixtureRequestCounter, arrived: Path, release: Path, trace: Path
):
    def transport(request: PinnedBinaryDownloadRequest) -> StreamedBinaryDownloadResponse:
        target_basename = PurePosixPath(request.target.split("?", 1)[0]).name
        if (
            request.scheme != "https"
            or request.host != "img.xhscdn.com"
            or request.port != 443
            or request.addresses != (FIXTURE_PUBLIC_IP,)
            or target_basename not in {"asset-1.jpg", "asset-2.jpg"}
            or request.headers.get("Cookie") != FIXTURE_COOKIE_CANARY
        ):
            raise NetworkBoundaryError("xhs_controlled_credential_fixture_binary_request_denied")
        counter.binary_count += 1
        _write_fixture_trace(trace, "binary_wire_entered", counter)
        if counter.binary_count == 1:
            arrived.parent.mkdir(parents=True, exist_ok=True)
            arrived.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "count": counter.binary_count,
                        "target_basename": target_basename,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            deadline = monotonic() + min(_FIXTURE_BINARY_WAIT_SECONDS, request.timeout_seconds)
            while not release.is_file():
                if request.control_check is not None:
                    request.control_check()
                if monotonic() >= deadline:
                    raise NetworkBoundaryError("xhs_controlled_credential_fixture_binary_release_timeout")
                sleep(0.025)
        if request.control_check is not None:
            request.control_check()
        request.destination_part.parent.mkdir(parents=True, exist_ok=True)
        fixture_jpeg = _fixture_jpeg()
        request.destination_part.write_bytes(fixture_jpeg)
        return StreamedBinaryDownloadResponse(
            200, {"Content-Type": "image/jpeg"}, len(fixture_jpeg)
        )

    return transport


def _write_fixture_trace(trace: Path, phase: str, counter: _FixtureRequestCounter) -> None:
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "phase": phase,
                "metadata_count": counter.count,
                "binary_count": counter.binary_count,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@lru_cache(maxsize=1)
def _fixture_jpeg() -> bytes:
    """Generate a non-user OCR image without persisting a second source asset."""

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1400, 280), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=72)
    draw.text((50, 80), "PROJECT MEMORY 2026", fill="black", font=font)
    output = BytesIO()
    image.save(output, format="JPEG", quality=95, optimize=True)
    return output.getvalue()


def _fixture_html() -> str:
    """A production-parser-compatible public image-set page without a Cookie."""

    payload = {
        "note": {
            "noteDetailMap": {
                FIXTURE_NOTE_ID: {
                    "note": {
                        "noteId": FIXTURE_NOTE_ID,
                        "type": "image",
                        "title": "受控凭据 Gate 图片素材",
                        "desc": "确定性受控文本网络 fixture",
                        "time": 1_725_638_400_000,
                        "imageList": [
                            {"urlDefault": "https://img.xhscdn.com/xhs-e2e/asset-1.jpg"},
                            {"urlDefault": "https://img.xhscdn.com/xhs-e2e/asset-2.jpg"},
                        ],
                    }
                }
            }
        }
    }
    return (
        "<!doctype html><html><body>"
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )
