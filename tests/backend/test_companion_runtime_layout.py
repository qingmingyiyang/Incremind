from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.companion_runtime_layout import (
    build_companion_object_store,
    resolve_companion_config_root,
    resolve_companion_runtime_layout,
)
from core.companion_core import CompanionRepositoryError


def _config_root(tmp_path: Path, name: str, module_relative: Path) -> tuple[Path, Path]:
    root = tmp_path / name
    module = root / module_relative
    (root / "config").mkdir(parents=True)
    (root / "config" / "rebuild.toml.example").write_text(
        '[storage]\nnamespace_id = "layout-test"\n', encoding="utf-8",
    )
    return root, module


def test_config_root_supports_source_and_relocated_packaged_layout(tmp_path: Path) -> None:
    source_root, source_module = _config_root(tmp_path, "source", Path("src/backend/companion_runtime_layout.py"))
    packaged_root, packaged_module = _config_root(tmp_path / "resources", "sidecar", Path("backend/companion_runtime_layout.py"))

    assert resolve_companion_config_root(source_module) == source_root
    assert resolve_companion_config_root(packaged_module) == packaged_root
    assert resolve_companion_config_root(packaged_module) != tmp_path / "resources"


def test_config_root_fails_closed_without_bundled_config(tmp_path: Path) -> None:
    module = tmp_path / "resources" / "sidecar" / "backend" / "companion_runtime_layout.py"
    with pytest.raises(FileNotFoundError, match="configuration is unavailable"):
        resolve_companion_config_root(module)


def test_layout_uses_packaged_environment_and_exposes_one_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = tmp_path / "repository"
    resources_root = tmp_path / "resources"
    monkeypatch.setenv("CHRIPTMAS_COMPANION_MODE", "packaged")
    monkeypatch.setenv("CHRIPTMAS_COMPANION_REPOSITORY_ROOT", str(repository_root))
    monkeypatch.setenv("CHRIPTMAS_COMPANION_RESOURCES_ROOT", str(resources_root))

    layout = resolve_companion_runtime_layout(SimpleNamespace())

    assert layout.mode == "packaged"
    assert layout.repository_root == repository_root.resolve()
    assert layout.resources_root == resources_root.resolve()
    assert layout.companion_config_root == resources_root.resolve() / "companion-config"


def test_container_layout_overrides_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_COMPANION_MODE", "development")
    monkeypatch.setenv("CHRIPTMAS_COMPANION_REPOSITORY_ROOT", str(tmp_path / "env-repository"))
    monkeypatch.setenv("CHRIPTMAS_COMPANION_RESOURCES_ROOT", str(tmp_path / "env-resources"))
    container = SimpleNamespace(
        companion_mode="packaged",
        companion_repository_root=tmp_path / "container-repository",
        companion_resources_root=tmp_path / "container-resources",
    )

    layout = resolve_companion_runtime_layout(container)

    assert layout.mode == "packaged"
    assert layout.repository_root == (tmp_path / "container-repository").resolve()
    assert layout.resources_root == (tmp_path / "container-resources").resolve()


def test_layout_rejects_invalid_mode_and_missing_packaged_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_COMPANION_MODE", "invalid")
    with pytest.raises(CompanionRepositoryError, match="mode is invalid"):
        resolve_companion_runtime_layout(SimpleNamespace())

    monkeypatch.setenv("CHRIPTMAS_COMPANION_MODE", "packaged")
    monkeypatch.delenv("CHRIPTMAS_COMPANION_RESOURCES_ROOT", raising=False)
    with pytest.raises(CompanionRepositoryError, match="resources are unavailable"):
        resolve_companion_runtime_layout(SimpleNamespace())


def test_object_store_preserves_runtime_roots_and_configured_namespace(tmp_path: Path) -> None:
    repository_root, module = _config_root(tmp_path, "repository", Path("src/backend/companion_runtime_layout.py"))
    runtime_root = tmp_path / "vault"

    store = build_companion_object_store(runtime_root, module_path=module)

    assert repository_root.is_dir()
    assert store.root == (runtime_root / ".rebuild-data").resolve()
    assert store.legacy_root == (runtime_root / "library").resolve()
    assert store.namespace_id == "layout-test"
