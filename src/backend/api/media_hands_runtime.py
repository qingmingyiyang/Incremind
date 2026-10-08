from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from threading import Lock
from typing import Protocol

from backend.api.analyze_source_ai_runtime import (
    AnalyzeSourceReadiness,
    PlatformArtifactSourceManifestResolver,
    SourceManifestResolverPort,
)
from backend.api.bilibili_platform_provider import build_bilibili_view_api_platform_provider
from backend.api.xiaohongshu_platform_provider import (
    XiaohongshuControlledMetadataPlatformProvider,
    build_xiaohongshu_anonymous_metadata_platform_provider,
)
from backend.api.xiaohongshu_controlled_credential_runtime import (
    SafeControlledCookieTextNetworkAdapter,
    XiaohongshuControlledCredentialRuntime,
)
from backend.api.xhs_controlled_credential_e2e_fixture import (
    installed_controlled_binary_network,
    installed_controlled_metadata_network,
)
from backend.api.source_resolution_evidence import SourceResolutionEvidenceRepository
from backend.api.bilibili_subtitle_media_operation import (
    BilibiliSubtitleMediaOperationProvider,
    DocumentBackedCanonicalMediaOutputVerifier,
)
from backend.api.governed_local_asr import FfprobeAudioDurationProbe, GovernedLocalAsrRunner
from backend.api.governed_local_ocr import GovernedLocalOcrError, GovernedLocalOcrRunner
from backend.api.governed_staged_video import GovernedStagedVideoError, GovernedStagedVideoRunner
from backend.api.media_ingress_selection_authority import (
    MediaIngressSelectionAuthority,
    MediaIngressSelectionError,
)
from backend.api.xiaohongshu_asset_materializer import build_xiaohongshu_asset_materializer
from backend.api.xiaohongshu_asset_analysis_journal import XiaohongshuAssetAnalysisJournal
from backend.api.xiaohongshu_media_operation import XiaohongshuMediaOperationProvider
from backend.api.xiaohongshu_staging_journal import XiaohongshuStagingJournal
from backend.api.xiaohongshu_video_derivative_journal import XiaohongshuVideoDerivativeJournal
from backend.security import (
    NetworkEgressProfileError,
    NetworkEgressProfileStore,
    SafeBinaryDownloadAdapter,
    SafeTextNetworkAdapter,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.xiaohongshu_controlled_credentials import (
    XiaohongshuControlledCredentialAuthority,
)
from core.aggregate_repository_factory import AggregateRepositoryFactory, AggregateRepositoryFactoryError
from core.job_runner import RoutedJobRepository, SQLiteJobAdmissionCommand
from core.media_hands import (
    EFFECT_KIND,
    MediaHandsAdmission,
    MediaHandsAdmissionCommand,
    MediaHandsOperationHandler,
    MediaHandsPolicy,
    MediaHandsPolicyAuthority,
    MediaHandsPolicyAuthorityError,
    MediaHandsPolicySourceError,
    MediaHandsProvisioner,
    MediaHandsV2Provisioner,
    MediaOperationProviderRouter,
    SourcePermissionRevokedError,
    SourcePermissionSnapshot,
    load_media_hands_policy,
)
from core.storage_provider import SQLiteStructuredRecordStore
from core.source_processing import (
    PlatformResolver,
    SourceManifest,
    SourceManifestArtifactRepository,
    SourcePermissionAuthority,
)


class MediaHandsRuntimeUnavailable(RuntimeError):
    pass


class ApplicationPort(Protocol):
    state: object


_RUNTIME_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class LocalSourcePermissionChecker:
    authority: SourcePermissionAuthority

    def assert_active(self, snapshot: SourcePermissionSnapshot) -> None:
        marker = f"/source-permissions/projects/{snapshot.project_id}/"
        if marker not in snapshot.grant_ref:
            raise SourcePermissionRevokedError("Media source permission grant scope drifted.")
        suffix = snapshot.grant_ref.split(marker, 1)[1]
        parts = suffix.split("/")
        if len(parts) != 2 or parts[1] != snapshot.grant_revision:
            raise SourcePermissionRevokedError("Media source permission grant revision drifted.")
        current = self.authority.current(project_id=snapshot.project_id, permission_id=parts[0])
        if (
            current is None
            or current.state != "granted"
            or current.public_ref != snapshot.grant_ref
            or f"r{current.revision}" != snapshot.grant_revision
            or current.revocation_generation != snapshot.revocation_generation
        ):
            raise SourcePermissionRevokedError("Media source permission is revoked or superseded.")


@dataclass(frozen=True, slots=True)
class MediaHandsRuntimeResolution:
    runtime: "MediaHandsRuntime | None"
    reason: str


@dataclass(frozen=True, slots=True)
class MediaHandsRuntime:
    application: ApplicationPort
    runtime_root: Path
    namespace_id: str
    policy: MediaHandsPolicy
    policy_authority: MediaHandsPolicyAuthority | None
    provisioner: MediaHandsProvisioner
    v2_provisioner: MediaHandsV2Provisioner
    selection_authority: MediaIngressSelectionAuthority
    handler: MediaHandsOperationHandler
    resolver: SourceManifestResolverPort

    def readiness(self) -> AnalyzeSourceReadiness:
        effect_runtime = getattr(self.application.state, "effect_runtime", None)
        handlers = getattr(effect_runtime, "handlers", None)
        runner_ready = bool(
            handlers is not None
            and callable(getattr(handlers, "kinds", None))
            and EFFECT_KIND in handlers.kinds()
        )
        try:
            self.handler.provider_identity
        except Exception:
            provider_ready = False
        else:
            provider_ready = True
        return AnalyzeSourceReadiness(True, runner_ready, provider_ready)

    def provision(
        self,
        *,
        manifest: SourceManifest,
        manifest_ref: str,
        manifest_revision: str,
        operation: str,
        idempotency_key: str,
        created_at: str,
        permission_snapshot: SourcePermissionSnapshot,
    ) -> MediaHandsAdmission:
        if not self.readiness().ready:
            raise MediaHandsRuntimeUnavailable("media hands Effect runtime is not ready")
        try:
            with self.selection_authority.writer("hands") as selection:
                self.selection_authority.bind_request(
                    operation="admit",
                    request_id=idempotency_key,
                    project_id=permission_snapshot.project_id,
                    input_ref=manifest_ref,
                    selection=selection,
                )
                admission = self.v2_provisioner.provision(
                    manifest=manifest,
                    manifest_ref=manifest_ref,
                    manifest_revision=manifest_revision,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    created_at=created_at,
                    permission_snapshot=permission_snapshot,
                    command=MediaHandsAdmissionCommand(
                        request_id=idempotency_key,
                        project_id=permission_snapshot.project_id,
                        selection_ref=selection.public_ref,
                        selection_revision=f"media-ingress-r{selection.revision}",
                        selection_mode=selection.mode,
                    ),
                )
        except (
            MediaHandsPolicyAuthorityError,
            MediaHandsPolicySourceError,
            MediaIngressSelectionError,
        ) as exc:
            raise MediaHandsRuntimeUnavailable(
                "media hands admission authority changed before admission commit"
            ) from exc
        return admission


def configure_media_hands_runtime(
    application: ApplicationPort,
    *,
    runtime_root: Path,
    object_store: object,
    repository: RoutedJobRepository,
) -> MediaHandsRuntimeResolution:
    namespace_id = getattr(object_store, "namespace_id", None)
    if not isinstance(namespace_id, str) or not namespace_id:
        raise MediaHandsRuntimeUnavailable("media hands object store namespace is unavailable")
    resolved_root = Path(runtime_root).resolve(strict=False)
    job_path = repository.sqlite.database_path
    if job_path != (resolved_root / ".rebuild-data" / "jobs.sqlite3").resolve(strict=False):
        raise MediaHandsRuntimeUnavailable("media hands Job authority path drift")
    authority = (resolved_root, namespace_id, job_path)
    with _RUNTIME_LOCK:
        existing_authority = getattr(application.state, "media_hands_runtime_authority", None)
        existing = getattr(application.state, "media_hands_runtime_resolution", None)
        if isinstance(existing, MediaHandsRuntimeResolution):
            if existing_authority != authority:
                raise MediaHandsRuntimeUnavailable("media hands runtime authority drift")
            return existing

        container = getattr(application.state, "container", None)
        policy_authority: MediaHandsPolicyAuthority | None = None
        if hasattr(container, "_media_hands_policy_snapshot_for_test"):
            # Private dependency-injection seam used only by focused tests.
            snapshot = getattr(container, "_media_hands_policy_snapshot_for_test")
        else:
            try:
                policy_authority = MediaHandsPolicyAuthority(
                    SQLiteStructuredRecordStore(job_path)
                )
                snapshot = policy_authority.load_current_snapshot()
            except MediaHandsPolicyAuthorityError:
                snapshot = None
        if not isinstance(snapshot, Mapping):
            resolution = MediaHandsRuntimeResolution(None, "policy_invalid")
        else:
            try:
                policy = load_media_hands_policy(snapshot)
            except MediaHandsPolicySourceError as error:
                reason = "policy_disabled" if "disabled" in str(error) else "policy_invalid"
                resolution = MediaHandsRuntimeResolution(None, reason)
            else:
                provider = getattr(container, "media_operation_provider", None)
                output_verifier = getattr(container, "media_output_verifier", None)
                try:
                    network_profile = NetworkEgressProfileStore(resolved_root).get().profile
                except NetworkEgressProfileError:
                    resolution = MediaHandsRuntimeResolution(
                        None, "network_egress_profile_invalid"
                    )
                    setattr(application.state, "media_hands_runtime_authority", authority)
                    setattr(application.state, "media_hands_runtime_resolution", resolution)
                    return resolution
                default_documents = None
                if (
                    not hasattr(container, "media_operation_provider")
                    or not hasattr(container, "media_output_verifier")
                ):
                    try:
                        default_documents = AggregateRepositoryFactory(
                            runtime_root=resolved_root,
                            namespace_id=namespace_id,
                            json_store=object_store,
                        ).document_repository()
                    except AggregateRepositoryFactoryError:
                        default_documents = None
                if not hasattr(container, "media_operation_provider"):
                    network_factory = getattr(container, "media_text_network_factory", None)
                    if network_factory is None:
                        network_factory = lambda max_bytes, timeout, control: SafeTextNetworkAdapter(
                            allowed_hosts=("api.bilibili.com", "aisubtitle.hdslb.com"),
                            max_redirects=0,
                            max_response_bytes=max_bytes,
                            timeout_seconds=min(20.0, timeout),
                            control_check=control,
                        )
                    binary_network = getattr(container, "media_binary_network", None)
                    if binary_network is None:
                        binary_network = SafeBinaryDownloadAdapter(
                            resolved_root / ".rebuild-data" / "media-hands",
                            allowed_host_suffixes=("bilivideo.com",),
                        )
                    local_asr = getattr(container, "media_local_asr_runner", None)
                    if local_asr is None:
                        ffprobe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
                        local_asr = GovernedLocalAsrRunner(
                            resolved_root,
                            resolved_root / ".rebuild-data" / "media-hands",
                            str(getattr(container, "media_local_asr_model_name", "large-v3-turbo")),
                            FfprobeAudioDurationProbe(
                                Path(sys.executable).resolve().parent / "Library" / "bin" / ffprobe_name
                            ),
                        )
                    bilibili_provider = None if default_documents is None else BilibiliSubtitleMediaOperationProvider(
                        artifacts=SourceManifestArtifactRepository(
                            object_store, namespace_id=namespace_id
                        ),
                        documents=default_documents,
                        network_factory=network_factory,
                        now=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        binary_network=binary_network,
                        local_asr=local_asr,
                    )
                    provider = bilibili_provider
                    if bilibili_provider is not None:
                        artifacts = SourceManifestArtifactRepository(
                            object_store, namespace_id=namespace_id
                        )
                        controlled_runtime = None
                        secret_store = getattr(container, "secret_store", None)
                        if callable(getattr(secret_store, "get_snapshot", None)):
                            controlled_text_network = (
                                installed_controlled_metadata_network(container)
                                or SafeControlledCookieTextNetworkAdapter()
                            )
                            controlled_runtime = XiaohongshuControlledCredentialRuntime(
                                authority=XiaohongshuControlledCredentialAuthority(
                                    resolved_root, secret_store=secret_store
                                ),
                                boundary_profiles=ProjectBoundaryProfileStore(resolved_root),
                                controlled_text_network=controlled_text_network,
                            )
                        ocr = getattr(container, "media_xiaohongshu_ocr_runner", None)
                        if ocr is None:
                            ocr = GovernedLocalOcrRunner(
                                resolved_root / ".rebuild-data" / "media-hands",
                                object_store=object_store,
                            )
                        xhs_provider = None
                        try:
                            assert_ready = getattr(ocr, "assert_ready", None)
                            if not callable(assert_ready):
                                raise GovernedLocalOcrError("local_ocr_not_ready")
                            assert_ready()
                        except (GovernedLocalOcrError, ValueError):
                            pass
                        else:
                            xhs_materializer = getattr(
                                container, "media_xiaohongshu_materializer", None
                            )
                            if xhs_materializer is None:
                                xhs_materializer = build_xiaohongshu_asset_materializer(
                                    resolved_root / ".rebuild-data" / "media-hands",
                                    network_profile=network_profile,
                                    controlled_credential_runtime=controlled_runtime,
                                    binary_network=installed_controlled_binary_network(container),
                                )
                            xhs_provider = XiaohongshuMediaOperationProvider(
                                artifacts=artifacts,
                                documents=default_documents,
                                materializer=xhs_materializer,
                                journal=XiaohongshuStagingJournal(
                                    resolved_root, namespace_id=namespace_id
                                ),
                                ocr=ocr,
                                now=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                video_runner=None,
                                video_journal=None,
                                local_asr=None,
                                analysis_journal=XiaohongshuAssetAnalysisJournal(
                                    resolved_root, namespace_id=namespace_id
                                ),
                            )
                            video_runner = getattr(
                                container, "media_xiaohongshu_video_runner", None
                            )
                            if video_runner is None:
                                ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
                                video_runner = GovernedStagedVideoRunner(
                                    resolved_root / ".rebuild-data" / "media-hands",
                                    ffmpeg_executable=str(
                                        Path(sys.executable).resolve().parent / "Library" / "bin" / ffmpeg_name
                                    ),
                                )
                            try:
                                video_ready = getattr(video_runner, "assert_ready", None)
                                asr_ready = getattr(local_asr, "assert_ready", None)
                                if not callable(video_ready) or not callable(asr_ready):
                                    raise GovernedStagedVideoError("video_derivative_not_ready")
                                video_ready()
                                asr_ready()
                            except (GovernedStagedVideoError, ValueError):
                                pass
                            else:
                                xhs_provider = XiaohongshuMediaOperationProvider(
                                    artifacts=xhs_provider.artifacts,
                                    documents=xhs_provider.documents,
                                    materializer=xhs_provider.materializer,
                                    journal=xhs_provider.journal,
                                    ocr=xhs_provider.ocr,
                                    now=xhs_provider.now,
                                    video_runner=video_runner,
                                    video_journal=XiaohongshuVideoDerivativeJournal(
                                        resolved_root, namespace_id=namespace_id
                                    ),
                                    local_asr=local_asr,
                                    analysis_journal=xhs_provider.analysis_journal,
                                )
                        if xhs_provider is not None:
                            delegates = {
                                "bilibili": bilibili_provider,
                                "xiaohongshu": xhs_provider,
                            }

                            def frozen_manifest_platform(request):
                                artifact = artifacts.resolve_source_ref(
                                    source_ref=request.manifest_ref,
                                    project_id=request.permission_snapshot.project_id,
                                )
                                if (
                                    artifact.revision != request.manifest_revision
                                    or artifact.public_ref != request.manifest_ref
                                    or artifact.manifest.source_id != request.source_id
                                ):
                                    raise MediaHandsRuntimeUnavailable(
                                        "media provider frozen manifest drift"
                                    )
                                return artifact.manifest.platform

                            provider = MediaOperationProviderRouter(
                                delegates, frozen_manifest_platform
                            )
                if provider is None or not callable(getattr(provider, "execute", None)):
                    resolution = MediaHandsRuntimeResolution(None, "provider_unavailable")
                else:
                    permissions = SourcePermissionAuthority(object_store, namespace_id=namespace_id)
                    controlled_provider = None
                    controlled_runtime = locals().get("controlled_runtime")
                    if isinstance(controlled_runtime, XiaohongshuControlledCredentialRuntime):
                        controlled_network = controlled_runtime.controlled_text_network
                        controlled_provider = XiaohongshuControlledMetadataPlatformProvider(
                            runtime=controlled_runtime,
                            network=controlled_network,
                            evidence=SourceResolutionEvidenceRepository(
                                object_store, namespace_id=namespace_id
                            ),
                            namespace_id=namespace_id,
                        )
                    platform_providers = getattr(container, "platform_manifest_providers", None)
                    if platform_providers is None:
                        platform_providers = {
                            "bilibili": build_bilibili_view_api_platform_provider(
                                object_store, namespace_id=namespace_id
                            ),
                            "xiaohongshu": build_xiaohongshu_anonymous_metadata_platform_provider(
                                object_store,
                                namespace_id=namespace_id,
                                network_profile=network_profile,
                            ),
                        }
                    if not isinstance(platform_providers, Mapping) or not platform_providers:
                        resolution = MediaHandsRuntimeResolution(None, "platform_provider_unavailable")
                        setattr(application.state, "media_hands_runtime_authority", authority)
                        setattr(application.state, "media_hands_runtime_resolution", resolution)
                        return resolution
                    try:
                        platforms = PlatformResolver(dict(platform_providers))
                    except ValueError:
                        resolution = MediaHandsRuntimeResolution(None, "platform_provider_invalid")
                        setattr(application.state, "media_hands_runtime_authority", authority)
                        setattr(application.state, "media_hands_runtime_resolution", resolution)
                        return resolution
                    supported_platforms = getattr(provider, "supported_platforms", None)
                    if (
                        not isinstance(supported_platforms, frozenset)
                        or not supported_platforms
                        or any(
                            not isinstance(platform, str)
                            or platform not in platforms.registered_platforms
                            for platform in supported_platforms
                        )
                    ):
                        resolution = MediaHandsRuntimeResolution(
                            None, "provider_platform_contract_invalid"
                        )
                        setattr(application.state, "media_hands_runtime_authority", authority)
                        setattr(application.state, "media_hands_runtime_resolution", resolution)
                        return resolution
                    if not hasattr(container, "media_output_verifier"):
                        output_verifier = (
                            DocumentBackedCanonicalMediaOutputVerifier(
                                default_documents,
                                frozenset({"media_transcript", "media_analysis"}),
                            )
                            if default_documents is not None else None
                        )
                    if output_verifier is None or not callable(
                        getattr(output_verifier, "assert_output_committed", None)
                    ):
                        resolution = MediaHandsRuntimeResolution(
                            None, "output_verifier_unavailable"
                        )
                        setattr(application.state, "media_hands_runtime_authority", authority)
                        setattr(application.state, "media_hands_runtime_resolution", resolution)
                        return resolution
                    media_handler = MediaHandsOperationHandler(
                        provider,
                        LocalSourcePermissionChecker(permissions),
                        output_verifier,
                    )
                    runtime = MediaHandsRuntime(
                        application=application,
                        runtime_root=resolved_root,
                        namespace_id=namespace_id,
                        policy=policy,
                        policy_authority=policy_authority,
                        provisioner=MediaHandsProvisioner(
                            repository.sqlite,
                            policy,
                            policy_authority.assert_current_for_admission
                            if policy_authority is not None
                            else None,
                        ),
                        v2_provisioner=MediaHandsV2Provisioner(
                            SQLiteJobAdmissionCommand(job_path),
                            policy,
                            media_handler,
                            admitted_at=lambda: int(time.time()),
                            policy_admission_fence=(
                                policy_authority.assert_current_for_admission
                                if policy_authority is not None
                                else None
                            ),
                        ),
                        selection_authority=MediaIngressSelectionAuthority(
                            SQLiteStructuredRecordStore(job_path)
                        ),
                        handler=media_handler,
                        resolver=PlatformArtifactSourceManifestResolver(
                            artifacts=SourceManifestArtifactRepository(object_store, namespace_id=namespace_id),
                            platforms=platforms,
                            permissions=permissions,
                            executable_platforms=supported_platforms,
                            controlled_xiaohongshu=controlled_provider,
                        ),
                    )
                    resolution = MediaHandsRuntimeResolution(runtime, "ready")
                    setattr(application.state, "media_hands_handler", runtime.handler)
        setattr(application.state, "media_hands_runtime_authority", authority)
        setattr(application.state, "media_hands_runtime_resolution", resolution)
        return resolution


def current_media_hands_runtime(application: ApplicationPort) -> MediaHandsRuntime | None:
    resolution = getattr(application.state, "media_hands_runtime_resolution", None)
    return resolution.runtime if isinstance(resolution, MediaHandsRuntimeResolution) else None
