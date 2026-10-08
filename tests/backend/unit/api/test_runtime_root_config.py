from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.desktop_session import (
    DESKTOP_ALLOWED_ORIGIN_ENV,
    DESKTOP_EXPIRES_ENV,
    DESKTOP_INSTANCE_ENV,
    DESKTOP_MODE_ENV,
    DESKTOP_NONCE_ENV,
    DESKTOP_PROTOCOL_ENV,
    DESKTOP_PROTOCOL_VERSION,
    DESKTOP_SECRET_ENV,
    DESKTOP_SESSION_HEADER,
)
from backend.api.runtime_root_config import (
    RUNTIME_MEDIA_ROOT_ENV,
    RUNTIME_MODEL_ROOT_ENV,
    RUNTIME_ROOT_REVISION_ENV,
    RUNTIME_ROOT_VERSION_ENV,
    RUNTIME_VAULT_ROOT_ENV,
    RuntimeRootConfigError,
    health_runtime_roots,
    load_runtime_root_config,
)
from backend.api.routes.health import _observe_runtime_roots


SECRET = "V7sQ2nL9kR4mX8cD1eF0gHjK3pT6wY5zB2vA9qN7rTu"


def _configure_desktop(monkeypatch, root) -> None:
    monkeypatch.setenv(DESKTOP_MODE_ENV, "desktop_production")
    monkeypatch.setenv(DESKTOP_SECRET_ENV, SECRET)
    monkeypatch.setenv(DESKTOP_NONCE_ENV, "A3v_Y8kN5mP2qR7sT4wX9zB6cD1eF0gHjK4lM8nQ2rS")
    monkeypatch.setenv(DESKTOP_INSTANCE_ENV, "deskinst_AQ9h6rwYqL3X5nP8cD2vK7")
    monkeypatch.setenv(DESKTOP_PROTOCOL_ENV, DESKTOP_PROTOCOL_VERSION)
    monkeypatch.setenv(DESKTOP_EXPIRES_ENV, (datetime.now(UTC) + timedelta(hours=1)).isoformat())
    monkeypatch.setenv(DESKTOP_ALLOWED_ORIGIN_ENV, "http://127.0.0.1:49231")
    monkeypatch.setenv(RUNTIME_ROOT_VERSION_ENV, "1")
    monkeypatch.setenv(RUNTIME_ROOT_REVISION_ENV, "pointer:123:456")
    monkeypatch.setenv(RUNTIME_VAULT_ROOT_ENV, str(root))
    monkeypatch.setenv(RUNTIME_MODEL_ROOT_ENV, str(root))
    monkeypatch.setenv(RUNTIME_MEDIA_ROOT_ENV, str(root))


def test_runtime_root_config_rejects_separate_roots_until_consumers_support_them(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch, tmp_path)
    monkeypatch.setenv(RUNTIME_MODEL_ROOT_ENV, str(tmp_path / "models"))
    (tmp_path / "models").mkdir()
    with pytest.raises(RuntimeRootConfigError, match="runtime_root_layout_unsupported"):
        load_runtime_root_config(tmp_path)


def test_runtime_root_config_canonicalizes_equivalent_symlink_spellings(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    alias = tmp_path / "vault-alias"
    try:
        alias.symlink_to(vault, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    _configure_desktop(monkeypatch, vault)
    monkeypatch.setenv(RUNTIME_MODEL_ROOT_ENV, str(alias))
    monkeypatch.setenv(RUNTIME_MEDIA_ROOT_ENV, str(vault / "."))
    config = load_runtime_root_config(vault)
    assert config.vault_root == config.model_root == config.media_root


def test_health_observation_tolerates_an_absent_non_desktop_test_root(tmp_path, monkeypatch) -> None:
    for key in (
        RUNTIME_ROOT_VERSION_ENV,
        RUNTIME_ROOT_REVISION_ENV,
        RUNTIME_VAULT_ROOT_ENV,
        RUNTIME_MODEL_ROOT_ENV,
        RUNTIME_MEDIA_ROOT_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    assert _observe_runtime_roots(tmp_path / "absent-root") is None


def test_desktop_root_observation_hides_paths_on_health_and_probes_on_authenticated_endpoint(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch, tmp_path)
    config = load_runtime_root_config(tmp_path)
    health = health_runtime_roots(config, container_root=tmp_path)
    assert health["revision"] == "pointer:123:456"
    assert str(tmp_path) not in str(health)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        denied = client.post("/api/desktop/runtime-roots/verify")
        verified = client.post(
            "/api/desktop/runtime-roots/verify",
            headers={DESKTOP_SESSION_HEADER: SECRET},
        )
        startup = client.get("/api/health", headers={DESKTOP_SESSION_HEADER: SECRET})

    assert denied.status_code == 403
    assert verified.status_code == 200
    assert verified.json()["roots"]["vault"]["path"] == str(tmp_path.resolve())
    assert verified.json()["probes"]["vault"] == {"read": True, "write": True, "cleanup": True}
    assert not list(tmp_path.glob(".chriptmas-runtime-root-probe-*"))
    assert startup.status_code == 200
    assert startup.json()["runtime_roots"]["revision"] == "pointer:123:456"
    assert str(tmp_path) not in startup.text
