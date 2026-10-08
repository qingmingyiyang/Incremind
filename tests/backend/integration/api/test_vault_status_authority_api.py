from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes.product.vault_status import _platform_app_data_dir
from core.storage_provider import RebuildStorageSettings


def _status(runtime_root: Path, user_data_root: Path, monkeypatch) -> dict[str, object]:
    runtime_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CHRIPTMAS_COMPANION_USER_DATA_ROOT", str(user_data_root))
    with TestClient(create_app(SimpleNamespace(root_dir=runtime_root))) as client:
        response = client.get("/api/rebuild/vault-status")
        assert response.status_code == 200, response.text
        assert str(user_data_root) not in response.text
        return response.json()


def test_formal_packaged_vault_uses_electron_user_data_authority(
    tmp_path: Path, monkeypatch
) -> None:
    user_data = tmp_path / "Chriptmas OS"
    runtime_root = user_data / "vault"

    body = _status(runtime_root, user_data, monkeypatch)

    assert body["storage_location_status"] == "formal_app_data_vault"
    assert body["migration_status"]["needs_migration"] is False
    assert body["migration_status"]["target_vault_masked"] == "…/.rebuild-data"
    assert body["backup_destination_masked"] == "…/snapshots"
    assert body["operational_plan"]["app_data_dir"]["status"] == "ready"
    layers = {item["layer_id"]: item for item in body["asset_layers"]}
    assert layers["L3"]["label"] == "系列记忆与项目能力"
    assert layers["L4"]["label"] == "稳定偏好与规则"
    assert layers["L4"]["count"] == 0
    settings = RebuildStorageSettings.from_mapping({}, repository_root=runtime_root)
    assert _platform_app_data_dir(settings) == user_data.resolve()


def test_legacy_packaged_root_reports_exact_formal_target(
    tmp_path: Path, monkeypatch
) -> None:
    user_data = tmp_path / "Chriptmas OS"

    body = _status(user_data, user_data, monkeypatch)

    assert body["storage_location_status"] == "project_root_development"
    assert body["migration_status"]["needs_migration"] is True
    assert body["migration_status"]["current_vault_masked"] == "…/.rebuild-data"
    assert body["migration_status"]["target_vault_masked"] == "…/.rebuild-data"
    assert body["backup_destination_masked"] == "…/snapshots"
    assert body["operational_plan"]["app_data_dir"]["status"] == "planned"


def test_vault_status_has_deterministic_fallback_without_electron_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CHRIPTMAS_COMPANION_USER_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    with TestClient(create_app(SimpleNamespace(root_dir=runtime_root))) as client:
        response = client.get("/api/rebuild/vault-status")

    assert response.status_code == 200
    body = response.json()
    assert body["migration_status"]["target_vault_masked"] == "…/.rebuild-data"
    settings = RebuildStorageSettings.from_mapping({}, repository_root=runtime_root)
    assert _platform_app_data_dir(settings) == (
        tmp_path / "Local" / "ChriptmasReplay"
    ).resolve()
