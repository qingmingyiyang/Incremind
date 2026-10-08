"""Pure source-manifest contracts; intentionally detached from runtime intake."""

from .adapters import BilibiliAdapter, XiaohongshuAdapter
from .artifact_repository import SourceManifestArtifact, SourceManifestArtifactError, SourceManifestArtifactRepository, SourceManifestIdentityConflict
from .codec import SourceManifestCodec, SourceManifestCodecError
from .models import AssetRelation, ControlledCredentialBinding, FrozenJsonArray, FrozenJsonObject, ManifestBody, ManifestPermission, SourceAsset, SourceManifest
from .platform_resolver import PlatformProbeResult, PlatformResolution, PlatformResolver, ProviderPort
from .router import MediaRouter, MediaRoutingOutcome, PipelineDeclaration
from .source_permission import SourcePermissionAuthority, SourcePermissionConflict, SourcePermissionError, SourcePermissionRevision

__all__ = [
    "AssetRelation",
    "BilibiliAdapter",
    "ControlledCredentialBinding",
    "FrozenJsonArray",
    "FrozenJsonObject",
    "ManifestBody",
    "ManifestPermission",
    "MediaRouter",
    "MediaRoutingOutcome",
    "PipelineDeclaration",
    "PlatformProbeResult",
    "PlatformResolution",
    "PlatformResolver",
    "ProviderPort",
    "SourceAsset",
    "SourceManifestArtifact",
    "SourceManifestArtifactError",
    "SourceManifestArtifactRepository",
    "SourceManifest",
    "SourceManifestCodec",
    "SourceManifestCodecError",
    "SourceManifestIdentityConflict",
    "SourcePermissionAuthority",
    "SourcePermissionConflict",
    "SourcePermissionError",
    "SourcePermissionRevision",
    "XiaohongshuAdapter",
]
