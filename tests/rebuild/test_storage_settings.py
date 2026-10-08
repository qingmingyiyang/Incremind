from __future__ import annotations

from pathlib import Path

import pytest

from core.storage_provider import RebuildStorageSettings, StorageConfigurationError


def test_default_storage_is_isolated_and_legacy_access_is_disabled(tmp_path: Path) -> None:
    settings = RebuildStorageSettings.from_mapping({}, repository_root=tmp_path)

    assert settings.rebuild_root == (tmp_path / ".rebuild-data").resolve()
    assert settings.legacy_root == (tmp_path / "library").resolve()
    assert settings.namespace_id == "default"
    assert settings.storage_version == 1
    assert settings.app_root_uri == "platform-app-data://chriptmas-replay"
    assert settings.root_uri == "crp://default/"
    assert settings.reference_root_uri == "crp-ref://default/"
    assert settings.legacy_access == "disabled"
    assert settings.backup_ready is True
    assert settings.isolated is True
    assert not settings.rebuild_root.exists()
    assert not settings.legacy_root.exists()


@pytest.mark.parametrize(
    ("rebuild_root", "legacy_root"),
    [
        ("library", "library"),
        ("library/rebuild", "library"),
        (".rebuild-data", ".rebuild-data/legacy"),
    ],
)
def test_overlapping_storage_roots_are_rejected(
    tmp_path: Path,
    rebuild_root: str,
    legacy_root: str,
) -> None:
    with pytest.raises(StorageConfigurationError):
        RebuildStorageSettings.from_mapping(
            {
                "storage": {
                    "root": rebuild_root,
                    "legacy_root": legacy_root,
                    "legacy_access": "disabled",
                }
            },
            repository_root=tmp_path,
        )


def test_legacy_storage_cannot_be_configured_for_write(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigurationError, match="disabled or read_only"):
        RebuildStorageSettings.from_mapping(
            {"storage": {"legacy_access": "read_write"}},
            repository_root=tmp_path,
        )


def test_namespace_uri_must_match_namespace_id(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigurationError, match="root_uri"):
        RebuildStorageSettings.from_mapping(
            {
                "storage": {
                    "namespace_id": "default",
                    "root_uri": "crp://other/",
                }
            },
            repository_root=tmp_path,
        )


def test_reference_uri_must_match_namespace_id(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigurationError, match="reference_root_uri"):
        RebuildStorageSettings.from_mapping(
            {
                "storage": {
                    "namespace_id": "default",
                    "reference_root_uri": "crp-ref://other/",
                }
            },
            repository_root=tmp_path,
        )


def test_app_root_uri_must_be_platform_neutral(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigurationError, match="app_root_uri"):
        RebuildStorageSettings.from_mapping(
            {
                "storage": {
                    "app_root_uri": "C:\\Users\\example\\AppData\\Roaming\\ChriptmasReplay",
                }
            },
            repository_root=tmp_path,
        )


def test_pre_restore_backup_is_required(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigurationError, match="pre_restore_required"):
        RebuildStorageSettings.from_mapping(
            {
                "storage": {
                    "backup": {
                        "enabled": True,
                        "retention_count": 5,
                        "pre_restore_required": False,
                    }
                }
            },
            repository_root=tmp_path,
        )


def test_config_example_expresses_namespace_without_creating_storage() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    rebuild_root = repository_root / ".rebuild-data"
    existed_before = rebuild_root.exists()

    settings = RebuildStorageSettings.from_toml(
        repository_root / "config" / "rebuild.toml.example",
        repository_root=repository_root,
    )

    assert settings.namespace_id == "default"
    assert settings.root_uri == "crp://default/"
    assert settings.reference_root_uri == "crp-ref://default/"
    assert settings.legacy_access == "disabled"
    assert settings.backup_ready is True
    assert rebuild_root.exists() is existed_before
