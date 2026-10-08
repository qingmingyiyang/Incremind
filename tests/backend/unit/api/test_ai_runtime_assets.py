from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.ai_runtime import RuntimeAssetRootError, resolve_runtime_asset_root


def _module_path(root: Path) -> Path:
    module = root / "backend" / "api" / "ai_runtime.py"
    module.parent.mkdir(parents=True)
    module.write_text("# synthetic module\n", encoding="utf-8")
    return module


def _assets(root: Path, executable: str = "python.exe") -> None:
    config = root / "config"
    runtime = root / "runtime"
    config.mkdir(parents=True)
    runtime.mkdir()
    (config / "codex-hooks.toml").write_text("[hooks]\nenabled = false\n", encoding="utf-8")
    (runtime / executable).write_text("synthetic runtime\n", encoding="utf-8")


def test_resolves_source_style_assets_from_module_ancestors(tmp_path: Path) -> None:
    _assets(tmp_path)
    resolved = resolve_runtime_asset_root(_module_path(tmp_path))

    assert resolved.root_dir == tmp_path.resolve()
    assert resolved.hook_config == tmp_path / "config" / "codex-hooks.toml"
    assert resolved.runtime_executable == tmp_path / "runtime" / "python.exe"


def test_resolves_packaged_sidecar_assets_without_fixed_parent_depth(tmp_path: Path) -> None:
    sidecar = tmp_path / "resources" / "sidecar"
    _assets(sidecar)
    resolved = resolve_runtime_asset_root(_module_path(sidecar))

    assert resolved.root_dir == sidecar.resolve()
    assert resolved.hook_config.parent == sidecar / "config"
    assert resolved.runtime_executable == sidecar / "runtime" / "python.exe"


def test_accepts_explicit_cross_platform_python_contract(tmp_path: Path) -> None:
    _assets(tmp_path, executable="python")

    assert resolve_runtime_asset_root(_module_path(tmp_path)).runtime_executable.name == "python"


def test_rejects_missing_or_ambiguous_asset_roots(tmp_path: Path) -> None:
    module = _module_path(tmp_path)
    with pytest.raises(RuntimeAssetRootError, match="unavailable"):
        resolve_runtime_asset_root(module)

    _assets(tmp_path)
    _assets(tmp_path / "backend")
    with pytest.raises(RuntimeAssetRootError, match="ambiguous"):
        resolve_runtime_asset_root(module)


def test_rejects_a_root_with_multiple_runtime_executables(tmp_path: Path) -> None:
    _assets(tmp_path)
    (tmp_path / "runtime" / "python").write_text("synthetic runtime\n", encoding="utf-8")

    with pytest.raises(RuntimeAssetRootError, match="unavailable"):
        resolve_runtime_asset_root(_module_path(tmp_path))


def test_web_source_uses_owned_venv_without_copying_sidecar_runtime(tmp_path: Path) -> None:
    module = _module_path(tmp_path / "src")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "codex-hooks.toml").write_text("[hooks]\nenabled = false\n")
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("synthetic venv executable")
    assert resolve_runtime_asset_root(module).runtime_executable == python
    # A copied/flattened sidecar may not inherit an unrelated repository venv.
    with pytest.raises(RuntimeAssetRootError, match="unavailable"):
        resolve_runtime_asset_root(_module_path(tmp_path))
