from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from core.companion_core import CompanionRepositoryError
from core.storage_provider import JsonObjectStore, RebuildStorageSettings


@dataclass(frozen=True, slots=True)
class CompanionRuntimeLayout:
    mode: str
    repository_root: Path
    resources_root: Path

    @property
    def companion_config_root(self) -> Path:
        if self.mode == "packaged":
            return self.resources_root / "companion-config"
        return self.repository_root / "config" / "companion"


def resolve_companion_runtime_layout(
    container: object,
    *,
    module_path: Path | None = None,
) -> CompanionRuntimeLayout:
    mode_value = getattr(container, "companion_mode", None) or os.environ.get("CHRIPTMAS_COMPANION_MODE") or "development"
    mode = str(mode_value).strip().lower()
    if mode not in {"development", "packaged"}:
        raise CompanionRepositoryError("companion runtime mode is invalid")

    repository_value = getattr(container, "companion_repository_root", None) or os.environ.get("CHRIPTMAS_COMPANION_REPOSITORY_ROOT")
    resources_value = getattr(container, "companion_resources_root", None) or os.environ.get("CHRIPTMAS_COMPANION_RESOURCES_ROOT")
    if mode == "packaged" and not resources_value:
        raise CompanionRepositoryError("companion packaged resources are unavailable")

    source_module = Path(__file__) if module_path is None else Path(module_path)
    repository_root = Path(repository_value).expanduser().resolve(strict=False) if repository_value else resolve_companion_config_root(source_module)
    resources_root = Path(resources_value).expanduser().resolve(strict=False) if resources_value else repository_root
    return CompanionRuntimeLayout(mode, repository_root, resources_root)


def build_companion_object_store(
    runtime_root: Path,
    *,
    module_path: Path | None = None,
) -> JsonObjectStore:
    source_module = Path(__file__) if module_path is None else Path(module_path)
    repository_root = resolve_companion_config_root(source_module)
    settings = RebuildStorageSettings.from_toml(
        repository_root / "config" / "rebuild.toml.example",
        repository_root=repository_root,
    )
    runtime = Path(runtime_root).expanduser().resolve(strict=False)
    return JsonObjectStore(
        runtime / ".rebuild-data",
        legacy_root=runtime / "library",
        namespace_id=settings.namespace_id,
    )


def resolve_companion_config_root(module_path: Path) -> Path:
    """Resolve source ``src/backend`` and packaged ``sidecar/backend`` layouts."""

    resolved = Path(module_path).expanduser().resolve(strict=False)
    if len(resolved.parents) < 3:
        raise FileNotFoundError("companion runtime module layout is invalid")
    for candidate in (resolved.parents[1], resolved.parents[2]):
        if (candidate / "config" / "rebuild.toml.example").is_file():
            return candidate
    raise FileNotFoundError("companion runtime configuration is unavailable")
