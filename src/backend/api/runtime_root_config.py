"""Legacy API adapter for the shared runtime-root configuration."""
from backend.shared.runtime_root_config import (
    RUNTIME_MEDIA_ROOT_ENV,
    RUNTIME_MODEL_ROOT_ENV,
    RUNTIME_ROOT_REVISION_ENV,
    RUNTIME_ROOT_VERSION_ENV,
    RUNTIME_VAULT_ROOT_ENV,
    RuntimeRootConfig,
    RuntimeRootConfigError,
    health_runtime_roots,
    load_runtime_root_config,
    observe_runtime_roots,
    resolve_application_runtime_root,
    runtime_root_environment_present,
    verify_runtime_roots,
)

__all__ = [
    "RUNTIME_MEDIA_ROOT_ENV",
    "RUNTIME_MODEL_ROOT_ENV",
    "RUNTIME_ROOT_REVISION_ENV",
    "RUNTIME_ROOT_VERSION_ENV",
    "RUNTIME_VAULT_ROOT_ENV",
    "RuntimeRootConfig",
    "RuntimeRootConfigError",
    "health_runtime_roots",
    "load_runtime_root_config",
    "observe_runtime_roots",
    "resolve_application_runtime_root",
    "runtime_root_environment_present",
    "verify_runtime_roots",
]
