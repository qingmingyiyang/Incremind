"""Legacy API adapter for the shared frontend static mount."""
from backend.shared.static_assets import (
    FRONTEND_ASSET_MEDIA_TYPES,
    NoCacheStaticFiles,
    _resolve_dist_path,
    mount_frontend_dist,
)

__all__ = [
    "FRONTEND_ASSET_MEDIA_TYPES",
    "NoCacheStaticFiles",
    "_resolve_dist_path",
    "mount_frontend_dist",
]
