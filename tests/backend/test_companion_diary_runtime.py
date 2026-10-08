from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.companion_diary_runtime import load_diary_food_names


def _container(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        companion_mode="packaged",
        companion_repository_root=tmp_path / "repository",
        companion_resources_root=tmp_path / "resources",
    )


def _config_root(tmp_path: Path) -> Path:
    return tmp_path / "resources" / "companion-config"


def test_load_diary_food_names_projects_packaged_catalog(tmp_path: Path) -> None:
    config_root = _config_root(tmp_path)
    config_root.mkdir(parents=True)
    (config_root / "items.json").write_text(
        """
        {
          "items": [
            {"id": "food:apple", "kind": "food", "name": "苹果"},
            {"id": "food:milk-tea", "kind": "food", "name": "奶茶"},
            {"id": "focus:deep-work", "kind": "focus", "name": "深度工作"},
            {"id": "food:bad", "kind": "food", "name": "   "},
            {"kind": "food", "name": "缺少编号"}
          ]
        }
        """,
        encoding="utf-8",
    )

    assert load_diary_food_names(_container(tmp_path)) == {
        "food:apple": "苹果",
        "food:milk-tea": "奶茶",
    }


def test_load_diary_food_names_degrades_to_empty_when_catalog_missing(tmp_path: Path) -> None:
    assert load_diary_food_names(_container(tmp_path)) == {}


def test_load_diary_food_names_degrades_on_malformed_catalog(tmp_path: Path) -> None:
    config_root = _config_root(tmp_path)
    config_root.mkdir(parents=True)
    (config_root / "items.json").write_text('{"items": [', encoding="utf-8")
    assert load_diary_food_names(_container(tmp_path)) == {}


def test_load_diary_food_names_degrades_on_non_utf8_catalog(tmp_path: Path) -> None:
    config_root = _config_root(tmp_path)
    config_root.mkdir(parents=True)
    (config_root / "items.json").write_bytes(b'{"items": [{"id": "food:\xff\xfe"}]}')
    assert load_diary_food_names(_container(tmp_path)) == {}
