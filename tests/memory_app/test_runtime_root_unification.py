from __future__ import annotations

from pathlib import Path
from shutil import copyfile

from fastapi import FastAPI
import pytest

from backend.api.runtime_root_config import (
    RUNTIME_MEDIA_ROOT_ENV, RUNTIME_MODEL_ROOT_ENV, RUNTIME_ROOT_REVISION_ENV,
    RUNTIME_ROOT_VERSION_ENV, RUNTIME_VAULT_ROOT_ENV, RuntimeRootConfigError,
    resolve_application_runtime_root,
)
from backend.memory_app.app import create_app


DESKTOP_KEYS = (
    RUNTIME_ROOT_VERSION_ENV, RUNTIME_ROOT_REVISION_ENV,
    RUNTIME_VAULT_ROOT_ENV, RUNTIME_MODEL_ROOT_ENV, RUNTIME_MEDIA_ROOT_ENV,
)


def test_implicit_and_explicit_roots_use_one_vault(tmp_path, monkeypatch):
    for key in (*DESKTOP_KEYS, "CHRIPTMAS_APP_ROOT"):
        monkeypatch.delenv(key, raising=False)
    implicit = tmp_path / "runtime"
    assert resolve_application_runtime_root(implicit) == implicit.resolve()
    assert implicit.is_dir()
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "config").mkdir()
    copyfile(Path(__file__).resolve().parents[2] / "config" / "settings.toml.example",
             explicit / "config" / "settings.toml")
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(explicit))
    assert resolve_application_runtime_root(implicit) == explicit.resolve()
    app = create_app(runtime_root=explicit)
    assert app.state.container.root_dir == explicit.resolve()
    assert app.state.recognition_records.database_path == explicit.resolve() / ".rebuild-data" / "structured-records.sqlite3"


def test_incomplete_desktop_root_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    monkeypatch.setenv(RUNTIME_ROOT_VERSION_ENV, "1")
    with pytest.raises(RuntimeRootConfigError, match="runtime_root_config_incomplete"):
        resolve_application_runtime_root(tmp_path)


def test_injected_legacy_root_mismatch_rejected(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    legacy = FastAPI()
    legacy.state.container = type("Container", (), {"root_dir": other})()
    with pytest.raises(RuntimeError, match="legacy_recognition_runtime_root_mismatch"):
        create_app(runtime_root=tmp_path, legacy_app=legacy)
