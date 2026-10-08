from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import companion_runtime, companion_state_runtime
from backend.companion_runtime_layout import resolve_companion_config_root
from core.companion_core import CompanionRepository, CompanionRepositoryError


def _development_container(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(root_dir=tmp_path)


def _packaged_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    resources_root = tmp_path / "resources"
    config_root = resources_root / "companion-config"
    config_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CHRIPTMAS_COMPANION_MODE", "packaged")
    monkeypatch.setenv("CHRIPTMAS_COMPANION_REPOSITORY_ROOT", str(tmp_path / "repository"))
    monkeypatch.setenv("CHRIPTMAS_COMPANION_RESOURCES_ROOT", str(resources_root))
    return SimpleNamespace(root_dir=tmp_path / "vault")


def test_resolve_economy_rules_path_points_into_development_config_root(tmp_path: Path) -> None:
    rules_path = companion_state_runtime.resolve_economy_rules_path(_development_container(tmp_path))

    assert rules_path.name == "economy-rules.json"
    assert rules_path.exists()  # development layout falls back to the packaged source config


def test_resolve_economy_rules_path_honors_packaged_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = _packaged_container(tmp_path, monkeypatch)

    rules_path = companion_state_runtime.resolve_economy_rules_path(container)

    assert rules_path == tmp_path / "resources" / "companion-config" / "economy-rules.json"


def test_reducer_loads_rules_from_adapter_path_in_packaged_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _packaged_container(tmp_path, monkeypatch)
    source_config = resolve_companion_config_root(Path(companion_runtime.__file__)) / "config" / "companion"
    (tmp_path / "resources" / "companion-config" / "economy-rules.json").write_bytes(
        (source_config / "economy-rules.json").read_bytes()
    )

    repository = CompanionRepository.at_data_root(container.root_dir)
    reducer = companion_runtime._state_reducer(container=container, repository=repository)

    assert reducer.rules.version >= 1


def test_reducer_keeps_fail_fast_semantics_when_rules_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _packaged_container(tmp_path, monkeypatch)  # no economy-rules.json written
    repository = CompanionRepository.at_data_root(container.root_dir)

    with pytest.raises(CompanionRepositoryError):
        companion_runtime._state_reducer(container=container, repository=repository)


def test_composition_root_uses_the_external_state_runtime_adapter() -> None:
    assert companion_runtime.resolve_economy_rules_path.__module__ == "backend.companion_state_runtime"
