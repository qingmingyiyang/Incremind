"""Validated runtime-root configuration supplied by the Electron sidecar owner.

The desktop process resolves the user-data pointer before launching Python.  This
module is the single backend reader for that private launch contract.  It does
not treat the environment as proof that a root is usable: callers must request
an observation or an explicit controlled probe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


RUNTIME_ROOT_VERSION_ENV = "CHRIPTMAS_RUNTIME_ROOT_VERSION"
RUNTIME_ROOT_REVISION_ENV = "CHRIPTMAS_RUNTIME_ROOT_REVISION"
RUNTIME_VAULT_ROOT_ENV = "CHRIPTMAS_RUNTIME_VAULT_ROOT"
RUNTIME_MODEL_ROOT_ENV = "CHRIPTMAS_RUNTIME_MODEL_ROOT"
RUNTIME_MEDIA_ROOT_ENV = "CHRIPTMAS_RUNTIME_MEDIA_ROOT"
_ROOT_ENV_KEYS = (
    RUNTIME_ROOT_VERSION_ENV,
    RUNTIME_ROOT_REVISION_ENV,
    RUNTIME_VAULT_ROOT_ENV,
    RUNTIME_MODEL_ROOT_ENV,
    RUNTIME_MEDIA_ROOT_ENV,
)
_SUPPORTED_VERSION = "1"


class RuntimeRootConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeRootConfig:
    version: int
    revision: str
    vault_root: Path
    model_root: Path
    media_root: Path


def load_runtime_root_config(default_root: Path) -> RuntimeRootConfig:
    """Load the private desktop contract or use a single-root non-desktop mode."""
    configured = {key: os.environ.get(key) for key in _ROOT_ENV_KEYS}
    present = [key for key, value in configured.items() if value is not None]
    if not present:
        root = _absolute_directory(default_root)
        return RuntimeRootConfig(1, "implicit:process-root", root, root, root)
    if len(present) != len(_ROOT_ENV_KEYS):
        raise RuntimeRootConfigError("runtime_root_config_incomplete")
    if configured[RUNTIME_ROOT_VERSION_ENV] != _SUPPORTED_VERSION:
        raise RuntimeRootConfigError("runtime_root_config_version_unsupported")
    revision = configured[RUNTIME_ROOT_REVISION_ENV]
    if not isinstance(revision, str) or not revision or len(revision) > 256 or "\x00" in revision:
        raise RuntimeRootConfigError("runtime_root_config_revision_invalid")
    vault = _absolute_directory(configured[RUNTIME_VAULT_ROOT_ENV])
    model = _absolute_directory(configured[RUNTIME_MODEL_ROOT_ENV])
    media = _absolute_directory(configured[RUNTIME_MEDIA_ROOT_ENV])
    # The current backend has one broad root consumer graph.  Allowing a UI to
    # migrate a narrower model/media root would claim an activation that its
    # production consumers cannot honor.
    if model != vault or media != vault:
        raise RuntimeRootConfigError("runtime_root_layout_unsupported")
    return RuntimeRootConfig(1, revision, vault, model, media)


def runtime_root_environment_present() -> bool:
    return any(os.environ.get(key) is not None for key in _ROOT_ENV_KEYS)


def resolve_application_runtime_root(default_root: Path) -> Path:
    """Resolve the one vault root shared by the old API and recognition API."""
    if os.environ.get('CHRIPTMAS_DEPLOY', 'desktop') != 'desktop':
        from backend.shared.deployment import resolve_deployment
        layout = resolve_deployment(default_root)
        (layout.server_root / 'server').mkdir(parents=True, exist_ok=True)
        layout.user_root.mkdir(parents=True, exist_ok=True)
        return load_runtime_root_config(layout.user_root).vault_root
    if runtime_root_environment_present():
        return load_runtime_root_config(default_root).vault_root
    configured = os.environ.get("CHRIPTMAS_APP_ROOT")
    if configured:
        return load_runtime_root_config(Path(configured)).vault_root
    root = Path(default_root).expanduser().resolve(strict=False)
    if not root.is_absolute():
        raise RuntimeRootConfigError("runtime_root_path_invalid")
    root.mkdir(parents=True, exist_ok=True)
    return load_runtime_root_config(root).vault_root


def observe_runtime_roots(config: RuntimeRootConfig, *, container_root: Path) -> dict[str, object]:
    """Read each resolved root and return facts derived from filesystem access."""
    if not _same_path(_absolute_directory(container_root), config.vault_root):
        raise RuntimeRootConfigError("runtime_root_container_mismatch")
    roots = {
        "vault": _read_observation(config.vault_root),
        "model": _read_observation(config.model_root),
        "media": _read_observation(config.media_root),
    }
    return {
        "schema_version": "runtime-roots-v1",
        "version": config.version,
        "revision": config.revision,
        "roots": roots,
    }


def health_runtime_roots(config: RuntimeRootConfig, *, container_root: Path) -> dict[str, object]:
    """Return startup observation facts without exposing local filesystem paths."""
    observed = observe_runtime_roots(config, container_root=container_root)
    return {
        "schema_version": observed["schema_version"],
        "version": observed["version"],
        "revision": observed["revision"],
        "roots": {role: {"readable": True} for role in observed["roots"]},
    }


def verify_runtime_roots(config: RuntimeRootConfig, *, container_root: Path) -> dict[str, object]:
    """Perform an authenticated, bounded read/write/delete probe for each root."""
    observed = observe_runtime_roots(config, container_root=container_root)
    probes = {role: _write_probe(Path(item["path"])) for role, item in observed["roots"].items()}
    return {**observed, "probes": probes}


def _absolute_directory(value: Path | str | None) -> Path:
    if value is None:
        raise RuntimeRootConfigError("runtime_root_path_invalid")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise RuntimeRootConfigError("runtime_root_path_invalid")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_dir():
        raise RuntimeRootConfigError("runtime_root_unavailable")
    return resolved


def _read_observation(root: Path) -> dict[str, object]:
    try:
        with os.scandir(root) as entries:
            next(entries, None)
    except OSError as error:
        raise RuntimeRootConfigError("runtime_root_unreadable") from error
    return {"path": str(root), "readable": True}


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right)
    )


def _write_probe(root: Path) -> dict[str, bool]:
    probe = root / f".chriptmas-runtime-root-probe-{uuid4().hex}"
    try:
        with probe.open("xb") as handle:
            handle.write(b"runtime-root-probe-v1")
            handle.flush()
            os.fsync(handle.fileno())
        with probe.open("rb") as handle:
            if handle.read() != b"runtime-root-probe-v1":
                raise RuntimeRootConfigError("runtime_root_probe_readback_failed")
    except OSError as error:
        raise RuntimeRootConfigError("runtime_root_unwritable") from error
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeRootConfigError("runtime_root_probe_cleanup_failed") from error
    return {"read": True, "write": True, "cleanup": True}
