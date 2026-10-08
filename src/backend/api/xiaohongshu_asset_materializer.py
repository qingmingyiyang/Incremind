"""Execution-time, governed Xiaohongshu asset staging.

Metadata resolution deliberately freezes no CDN locators.  This adapter fetches
the public note again, proves it has not drifted from that frozen manifest, and
only then keeps each reviewed locator in memory for a bounded binary transfer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit

from backend.api.xiaohongshu_platform_provider import (
    TextNetworkPort,
    XiaohongshuMetadataProviderError,
    _canonical_source,
    _extract_note,
    verify_xiaohongshu_frozen_manifest,
)
from backend.security import (
    DownloadedBinary,
    NetworkBoundaryError,
    NetworkEgressProfile,
    SafeBinaryDownloadAdapter,
    SafeTextNetworkAdapter,
    loopback_proxy_for_capability,
)
from core.job_runner import JobStepBlockedError
from core.job_runner.media_execution_receipt import media_job_uri_segment
from core.source_processing import SourceManifest


_DEFAULT_CDN_SUFFIXES = ("xhscdn.com",)
_EXPECTED_MEDIA_TYPES = {
    "image": frozenset({"image/jpeg", "image/png", "image/webp"}),
    "video": frozenset({"video/mp4"}),
}


class XiaohongshuAssetMaterializationError(ValueError):
    """Stable execution error that never includes page, locator or token data."""


class BinaryDownloadPort(Protocol):
    def download(
        self,
        url: str,
        *,
        relative_path: str,
        max_response_bytes: int,
        headers: Mapping[str, str] | None = None,
        control_check: Callable[[], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> DownloadedBinary: ...


class XiaohongshuControlledCredentialWireLeasePort(Protocol):
    """One OS-owned, one-request credential lease.

    The materializer deliberately cannot ask this object for a Cookie string.
    A future controlled network adapter owns that last-mile injection and may
    expose the value only while it constructs its pinned request.  Keeping the
    two request methods here also prevents a generic network port from being
    handed a reusable credential header by the media pipeline.
    """

    def fetch_text(
        self,
        network: TextNetworkPort,
        url: str,
        *,
        control_check: Callable[[], None],
    ) -> str: ...

    def download(
        self,
        network: BinaryDownloadPort,
        url: str,
        *,
        relative_path: str,
        max_response_bytes: int,
        headers: Mapping[str, str],
        control_check: Callable[[], None],
        timeout_seconds: float,
    ) -> DownloadedBinary: ...

    def generation_current(self) -> bool: ...


class XiaohongshuControlledCredentialRuntimePort(Protocol):
    """OS-owned controlled-credential runtime, intentionally not a model port.

    ``issue_wire_lease`` must hold the project's Boundary ``locked_snapshot``
    from its current-binding check through request initiation.  The lease must
    recheck authorization and Secret generation before its adapter creates the
    wire request.  Materializer calls this method once per page or binary wire;
    it never caches a lease across requests.
    """

    def issue_wire_lease(
        self,
        manifest: SourceManifest,
        *,
        project_id: str,
        operation: str,
    ) -> XiaohongshuControlledCredentialWireLeasePort: ...


PageNetworkFactory = Callable[[int, float, Callable[[], None]], TextNetworkPort]


@dataclass(frozen=True, slots=True)
class XiaohongshuStagedAsset:
    asset_id: str
    ordinal: int
    kind: str
    media_type: str | None
    staged_path: str | None
    byte_count: int


@dataclass(frozen=True, slots=True)
class XiaohongshuMaterializationOutcome:
    assets: tuple[XiaohongshuStagedAsset, ...]
    total_download_bytes: int


@dataclass(frozen=True, slots=True)
class XiaohongshuAssetMaterializer:
    page_network: TextNetworkPort | None
    binary_network: BinaryDownloadPort
    reviewed_host_suffixes: tuple[str, ...] = _DEFAULT_CDN_SUFFIXES
    clock: Callable[[], float] = monotonic
    page_network_factory: PageNetworkFactory | None = None
    controlled_credential_runtime: XiaohongshuControlledCredentialRuntimePort | None = None

    def __post_init__(self) -> None:
        suffixes = tuple(_normalize_suffix(item) for item in self.reviewed_host_suffixes)
        if not suffixes:
            raise ValueError("reviewed CDN host suffixes are required")
        if self.page_network is None and self.page_network_factory is None:
            raise ValueError("page network or page network factory is required")
        object.__setattr__(self, "reviewed_host_suffixes", suffixes)

    def materialize(
        self,
        manifest: SourceManifest,
        *,
        job_id: str,
        max_download_bytes: int,
        timeout_seconds: float,
        project_id: str | None = None,
        control_check: Callable[[], None] | None = None,
    ) -> XiaohongshuMaterializationOutcome:
        if max_download_bytes < 1 or timeout_seconds <= 0:
            raise XiaohongshuAssetMaterializationError("asset_download_budget_exhausted")
        if not isinstance(manifest, SourceManifest):
            raise XiaohongshuAssetMaterializationError("manifest_identity_invalid")
        started = self.clock()
        control_error: list[BaseException | None] = [None]
        staged_paths: list[Path] = []

        def checkpoint() -> None:
            try:
                self._checkpoint(control_check, started, timeout_seconds)
            except BaseException as error:
                control_error[0] = error
                raise

        try:
            checkpoint()
            _note_id, canonical_url = _canonical_source(manifest.input_identity)
            page_network = self.page_network
            if self.page_network_factory is not None:
                page_network = self.page_network_factory(
                    min(max_download_bytes, 1024 * 1024),
                    self._remaining_seconds(started, timeout_seconds),
                    checkpoint,
                )
            if page_network is None:  # pragma: no cover - constructor invariant
                raise XiaohongshuAssetMaterializationError("asset_metadata_unavailable")
            page_text = self._fetch_page(
                manifest, page_network, canonical_url, checkpoint, project_id
            )
            page_bytes = len(page_text.encode("utf-8", errors="strict"))
            if page_bytes > max_download_bytes:
                raise XiaohongshuAssetMaterializationError("asset_download_budget_exhausted")
            note = _extract_note(page_text, note_id=_note_id)
            checkpoint()
            _note_id, locators = verify_xiaohongshu_frozen_manifest(manifest, note)
            if len(locators) != len(manifest.assets):
                raise XiaohongshuAssetMaterializationError("manifest_drift")
            used_bytes = page_bytes
            results: list[XiaohongshuStagedAsset] = []
            for asset, locator in zip(manifest.assets, locators, strict=True):
                checkpoint()
                if asset.kind == "text":
                    results.append(XiaohongshuStagedAsset(
                        asset.asset_id, asset.ordinal, asset.kind, asset.media_type, None, 0
                    ))
                    continue
                if asset.kind not in _EXPECTED_MEDIA_TYPES or not isinstance(locator, str):
                    raise XiaohongshuAssetMaterializationError("manifest_drift")
                self._assert_reviewed_locator(locator)
                remaining_bytes = max_download_bytes - used_bytes
                if remaining_bytes < 1:
                    raise XiaohongshuAssetMaterializationError("asset_download_budget_exhausted")
                relative_path = self._relative_path(job_id, asset.asset_id, asset.ordinal, asset.kind)
                downloaded = self._download_asset(
                    manifest=manifest,
                    locator=locator,
                    relative_path=relative_path,
                    remaining_bytes=remaining_bytes,
                    canonical_url=canonical_url,
                    checkpoint=checkpoint,
                    timeout_seconds=self._remaining_seconds(started, timeout_seconds),
                    project_id=project_id,
                )
                staged_paths.append(downloaded.path)
                checkpoint()
                if downloaded.media_type not in _EXPECTED_MEDIA_TYPES[asset.kind]:
                    downloaded.path.unlink(missing_ok=True)
                    raise XiaohongshuAssetMaterializationError("asset_media_type_invalid")
                if downloaded.byte_count < 1 or downloaded.byte_count > remaining_bytes:
                    downloaded.path.unlink(missing_ok=True)
                    raise XiaohongshuAssetMaterializationError("asset_download_budget_exhausted")
                used_bytes += downloaded.byte_count
                results.append(XiaohongshuStagedAsset(
                    asset.asset_id, asset.ordinal, asset.kind, downloaded.media_type,
                    str(downloaded.path), downloaded.byte_count,
                ))
            checkpoint()
            return XiaohongshuMaterializationOutcome(tuple(results), used_bytes)
        except XiaohongshuAssetMaterializationError:
            _remove_staged(staged_paths)
            raise
        except XiaohongshuMetadataProviderError as error:
            _remove_staged(staged_paths)
            raise XiaohongshuAssetMaterializationError(_stable_provider_error(str(error))) from error
        except NetworkBoundaryError as error:
            _remove_staged(staged_paths)
            raise XiaohongshuAssetMaterializationError("asset_network_denied") from error
        except JobStepBlockedError:
            _remove_staged(staged_paths)
            raise
        except Exception as error:
            _remove_staged(staged_paths)
            if isinstance(control_error[0], JobStepBlockedError):
                raise control_error[0]
            raise XiaohongshuAssetMaterializationError("asset_materialization_interrupted") from error

    def _fetch_page(
        self,
        manifest: SourceManifest,
        page_network: TextNetworkPort,
        canonical_url: str,
        checkpoint: Callable[[], None],
        project_id: str | None,
    ) -> str:
        if manifest.credential_binding is None:
            return page_network.fetch_text(canonical_url)
        lease = self._issue_credential_lease(manifest, project_id=project_id, operation="page")
        try:
            page = lease.fetch_text(page_network, canonical_url, control_check=checkpoint)
        except XiaohongshuAssetMaterializationError:
            raise
        except Exception as error:
            # The request was delegated to the credential adapter.  Do not
            # manufacture a replacement request after an ambiguous failure.
            raise XiaohongshuAssetMaterializationError("controlled_credential_post_wire_unknown") from error
        self._require_credential_generation_current(lease)
        return page

    def _download_asset(
        self,
        *,
        manifest: SourceManifest,
        locator: str,
        relative_path: str,
        remaining_bytes: int,
        canonical_url: str,
        checkpoint: Callable[[], None],
        timeout_seconds: float,
        project_id: str | None,
    ) -> DownloadedBinary:
        headers = {"Referer": canonical_url, "Origin": "https://www.xiaohongshu.com"}
        if manifest.credential_binding is None:
            return self.binary_network.download(
                locator,
                relative_path=relative_path,
                max_response_bytes=remaining_bytes,
                headers=headers,
                control_check=checkpoint,
                timeout_seconds=timeout_seconds,
            )
        lease = self._issue_credential_lease(manifest, project_id=project_id, operation="binary")
        try:
            downloaded = lease.download(
                self.binary_network,
                locator,
                relative_path=relative_path,
                max_response_bytes=remaining_bytes,
                headers=headers,
                control_check=checkpoint,
                timeout_seconds=timeout_seconds,
            )
        except XiaohongshuAssetMaterializationError:
            raise
        except Exception as error:
            raise XiaohongshuAssetMaterializationError("controlled_credential_post_wire_unknown") from error
        try:
            self._require_credential_generation_current(lease)
        except XiaohongshuAssetMaterializationError:
            # The caller only records a staged path after this method returns.
            # A post-wire fence failure must therefore clean this just-written
            # artifact locally instead of relying on outer staging cleanup.
            downloaded.path.unlink(missing_ok=True)
            raise
        return downloaded

    def _issue_credential_lease(
        self, manifest: SourceManifest, *, project_id: str | None, operation: str
    ) -> XiaohongshuControlledCredentialWireLeasePort:
        runtime = self.controlled_credential_runtime
        if runtime is None:
            raise XiaohongshuAssetMaterializationError("controlled_credential_confirmed_none")
        if not isinstance(project_id, str) or not project_id.strip():
            raise XiaohongshuAssetMaterializationError("controlled_credential_confirmed_none")
        try:
            return runtime.issue_wire_lease(
                manifest, project_id=project_id.strip(), operation=operation
            )
        except XiaohongshuAssetMaterializationError:
            raise
        except Exception as error:
            # This is before the controlled adapter has been asked to make a
            # request, so it is a confirmed no-wire denial rather than an
            # ambiguous platform outcome.
            stable = str(error)
            if stable == "controlled_credential_pre_wire_drift":
                raise XiaohongshuAssetMaterializationError(stable) from error
            raise XiaohongshuAssetMaterializationError("controlled_credential_confirmed_none") from error

    @staticmethod
    def _require_credential_generation_current(
        lease: XiaohongshuControlledCredentialWireLeasePort,
    ) -> None:
        try:
            current = lease.generation_current()
        except Exception as error:
            raise XiaohongshuAssetMaterializationError("controlled_credential_post_wire_unknown") from error
        if current is not True:
            raise XiaohongshuAssetMaterializationError("controlled_credential_post_wire_unknown")

    def _assert_reviewed_locator(self, locator: str) -> None:
        try:
            parsed = urlsplit(locator)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            port = parsed.port
        except (ValueError, UnicodeError) as error:
            raise XiaohongshuAssetMaterializationError("asset_locator_denied") from error
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
            or not host
            or not any(host != suffix and host.endswith(f".{suffix}") for suffix in self.reviewed_host_suffixes)
        ):
            raise XiaohongshuAssetMaterializationError("asset_locator_denied")

    @staticmethod
    def _relative_path(job_id: str, asset_id: str, ordinal: int, kind: str) -> str:
        if not isinstance(asset_id, str) or not asset_id or any(char in asset_id for char in "\\/:"):
            raise XiaohongshuAssetMaterializationError("manifest_identity_invalid")
        extension = "jpg" if kind == "image" else "mp4"
        return f"{media_job_uri_segment(job_id)}/xiaohongshu/{ordinal:03d}-{asset_id}.{extension}"

    def _checkpoint(
        self, control_check: Callable[[], None] | None, started: float, timeout_seconds: float
    ) -> None:
        if self._remaining_seconds(started, timeout_seconds) <= 0:
            raise XiaohongshuAssetMaterializationError("asset_download_budget_exhausted")
        if control_check is not None:
            control_check()

    def _remaining_seconds(self, started: float, timeout_seconds: float) -> float:
        remaining = timeout_seconds - (self.clock() - started)
        if remaining <= 0:
            raise XiaohongshuAssetMaterializationError("asset_download_budget_exhausted")
        return remaining


def build_xiaohongshu_asset_materializer(
    staging_root: Path,
    *,
    reviewed_host_suffixes: Sequence[str] = _DEFAULT_CDN_SUFFIXES,
    network_profile: NetworkEgressProfile | None = None,
    controlled_credential_runtime: XiaohongshuControlledCredentialRuntimePort | None = None,
    binary_network: BinaryDownloadPort | None = None,
) -> XiaohongshuAssetMaterializer:
    """Production composition with separate public-page and binary boundaries."""

    suffixes = tuple(_normalize_suffix(item) for item in reviewed_host_suffixes)
    connect_proxy = (
        None
        if network_profile is None
        else loopback_proxy_for_capability(network_profile, "anonymous_public_media")
    )
    return XiaohongshuAssetMaterializer(
        page_network=None,
        binary_network=binary_network or SafeBinaryDownloadAdapter(
            staging_root,
            allowed_host_suffixes=suffixes,
            max_redirects=2,
            timeout_seconds=20.0,
            connect_proxy=connect_proxy,
        ),
        reviewed_host_suffixes=suffixes,
        controlled_credential_runtime=controlled_credential_runtime,
        page_network_factory=lambda max_bytes, timeout, control: SafeTextNetworkAdapter(
            allowed_hosts=("xiaohongshu.com", "www.xiaohongshu.com"),
            max_redirects=2,
            max_response_bytes=max_bytes,
            timeout_seconds=min(20.0, timeout),
            control_check=control,
            connect_proxy=connect_proxy,
        ),
    )


def _normalize_suffix(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "." not in value or "/" in value:
        raise ValueError("reviewed CDN host suffix is invalid")
    try:
        suffix = value.strip().encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ValueError("reviewed CDN host suffix is invalid") from error
    if not suffix or suffix.startswith("."):
        raise ValueError("reviewed CDN host suffix is invalid")
    return suffix


def _stable_provider_error(reason: str) -> str:
    if reason in {"manifest_drift", "asset_locator_unavailable", "metadata_identity_mismatch"}:
        return "manifest_drift"
    if reason in {"manifest_not_authorized", "manifest_not_xiaohongshu", "manifest_identity_invalid"}:
        return reason
    return "asset_metadata_unavailable"


def _remove_staged(paths: Sequence[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
