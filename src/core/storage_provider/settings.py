from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Literal, Mapping


class StorageConfigurationError(ValueError):
    """Raised when rebuild storage could overlap legacy user data."""


@dataclass(frozen=True, slots=True)
class RebuildStorageSettings:
    rebuild_root: Path
    legacy_root: Path
    namespace_id: str = "default"
    storage_version: int = 1
    app_root_uri: str = "platform-app-data://chriptmas-replay"
    root_uri: str = "crp://default/"
    reference_root_uri: str = "crp-ref://default/"
    legacy_access: Literal["disabled", "read_only"] = "disabled"
    backup_enabled: bool = True
    backup_retention_count: int = 5
    pre_restore_required: bool = True

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, object],
        *,
        repository_root: Path,
    ) -> "RebuildStorageSettings":
        storage = values.get("storage", {})
        if not isinstance(storage, Mapping):
            raise StorageConfigurationError("storage must be a mapping")
        rebuild_value = storage.get("root", ".rebuild-data")
        legacy_value = storage.get("legacy_root", "library")
        access = storage.get("legacy_access", "disabled")
        namespace_id = storage.get("namespace_id", "default")
        storage_version = storage.get("storage_version", 1)
        app_root_uri = storage.get("app_root_uri", "platform-app-data://chriptmas-replay")
        root_uri = storage.get("root_uri", f"crp://{namespace_id}/")
        reference_root_uri = storage.get("reference_root_uri", f"crp-ref://{namespace_id}/")
        backup = storage.get("backup", {})
        if not isinstance(rebuild_value, str) or not rebuild_value.strip():
            raise StorageConfigurationError("storage.root must be a non-empty string")
        if not isinstance(legacy_value, str) or not legacy_value.strip():
            raise StorageConfigurationError("storage.legacy_root must be a non-empty string")
        if access not in {"disabled", "read_only"}:
            raise StorageConfigurationError("storage.legacy_access must be disabled or read_only")
        if not isinstance(namespace_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", namespace_id):
            raise StorageConfigurationError("storage.namespace_id must be a platform-neutral namespace")
        if not isinstance(storage_version, int) or isinstance(storage_version, bool) or storage_version < 1:
            raise StorageConfigurationError("storage.storage_version must be a positive integer")
        if not isinstance(app_root_uri, str) or not re.fullmatch(r"platform-app-data://[a-z0-9][a-z0-9._-]*", app_root_uri):
            raise StorageConfigurationError("storage.app_root_uri must be platform-app-data://...")
        if root_uri != f"crp://{namespace_id}/":
            raise StorageConfigurationError("storage.root_uri must match storage.namespace_id")
        if reference_root_uri != f"crp-ref://{namespace_id}/":
            raise StorageConfigurationError("storage.reference_root_uri must match storage.namespace_id")
        if not isinstance(backup, Mapping):
            raise StorageConfigurationError("storage.backup must be a mapping")
        backup_enabled = backup.get("enabled", True)
        backup_retention_count = backup.get("retention_count", 5)
        pre_restore_required = backup.get("pre_restore_required", True)
        if not isinstance(backup_enabled, bool):
            raise StorageConfigurationError("storage.backup.enabled must be boolean")
        if not isinstance(backup_retention_count, int) or isinstance(backup_retention_count, bool):
            raise StorageConfigurationError("storage.backup.retention_count must be an integer")
        if not 1 <= backup_retention_count <= 100:
            raise StorageConfigurationError("storage.backup.retention_count must be between 1 and 100")
        if pre_restore_required is not True:
            raise StorageConfigurationError("storage.backup.pre_restore_required must be true")
        settings = cls(
            rebuild_root=_absolute(repository_root, rebuild_value),
            legacy_root=_absolute(repository_root, legacy_value),
            namespace_id=namespace_id,
            storage_version=storage_version,
            app_root_uri=app_root_uri,
            root_uri=root_uri,
            reference_root_uri=reference_root_uri,
            legacy_access=access,
            backup_enabled=backup_enabled,
            backup_retention_count=backup_retention_count,
            pre_restore_required=pre_restore_required,
        )
        settings.validate()
        return settings

    @classmethod
    def from_toml(
        cls,
        path: Path,
        *,
        repository_root: Path,
    ) -> "RebuildStorageSettings":
        return cls.from_mapping(
            tomllib.loads(path.read_text(encoding="utf-8")),
            repository_root=repository_root,
        )

    def validate(self) -> None:
        if self.rebuild_root == self.legacy_root:
            raise StorageConfigurationError("rebuild storage cannot equal legacy library")
        if _contains(self.legacy_root, self.rebuild_root):
            raise StorageConfigurationError("rebuild storage cannot be inside legacy library")
        if _contains(self.rebuild_root, self.legacy_root):
            raise StorageConfigurationError("legacy library cannot be inside rebuild storage")
        if self.root_uri != f"crp://{self.namespace_id}/":
            raise StorageConfigurationError("storage.root_uri must match storage.namespace_id")
        if self.reference_root_uri != f"crp-ref://{self.namespace_id}/":
            raise StorageConfigurationError("storage.reference_root_uri must match storage.namespace_id")
        if self.pre_restore_required is not True:
            raise StorageConfigurationError("storage.backup.pre_restore_required must be true")

    @property
    def isolated(self) -> bool:
        try:
            self.validate()
        except StorageConfigurationError:
            return False
        return True

    @property
    def backup_ready(self) -> bool:
        return self.backup_enabled and self.pre_restore_required and 1 <= self.backup_retention_count <= 100


def _absolute(repository_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repository_root / path
    return path.resolve(strict=False)


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True
