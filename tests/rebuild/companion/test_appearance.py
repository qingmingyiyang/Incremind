from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.companion_core import (
    CompanionAppearanceService,
    CompanionConflict,
    CompanionRepository,
    CompanionRepositoryError,
    load_appearance_catalog,
    load_catalog,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)


def service(tmp_path: Path) -> CompanionAppearanceService:
    repository = CompanionRepository.at_data_root(tmp_path, now=lambda: NOW)
    commerce = load_catalog(
        ROOT / "config" / "companion" / "items.json",
        ROOT / "config" / "companion" / "shop.json",
        image_root=ROOT / "src" / "frontend" / "public" / "mascots",
    )
    appearance = load_appearance_catalog(
        ROOT / "config" / "companion" / "appearance.json",
        image_root=ROOT / "src" / "frontend" / "public" / "mascots",
    )
    return CompanionAppearanceService(repository, catalog=appearance, item_ids=set(commerce.items), now=lambda: NOW)


def test_equip_requires_entitlement_and_is_idempotent_and_persistent(tmp_path: Path) -> None:
    appearance = service(tmp_path)
    assert appearance.snapshot()["outfit_id"] == "default"
    with pytest.raises(CompanionConflict, match="not owned"):
        appearance.equip(slot="outfit", selection_id="red-scarf", idempotency_key="equip:missing")
    with appearance.repository._transaction() as connection:
        connection.execute("INSERT INTO companion_inventory VALUES(?,?,?,?)", ("outfit:red-scarf", 1, 1, NOW.isoformat()))
    first = appearance.equip(slot="outfit", selection_id="red-scarf", idempotency_key="equip:scarf")
    replay = appearance.equip(slot="outfit", selection_id="red-scarf", idempotency_key="equip:scarf")
    assert first["outfit_id"] == "red-scarf" and replay["replayed"] is True
    with pytest.raises(CompanionConflict, match="different input"):
        appearance.equip(slot="outfit", selection_id="default", idempotency_key="equip:scarf")
    restarted = service(tmp_path)
    assert restarted.snapshot()["outfit_id"] == "red-scarf"
    assert restarted.equip(slot="outfit", selection_id="default", idempotency_key="equip:off")["outfit_id"] == "default"


def test_affinity_reconciliation_grants_growth_story_voice_and_outfit_once(tmp_path: Path) -> None:
    appearance = service(tmp_path)
    appearance.repository.initialize()
    with appearance.repository._transaction() as connection:
        connection.execute("UPDATE companion_state SET affinity=50,affinity_level=2,revision=revision+1 WHERE id='current'")
    first = appearance.snapshot()
    second = appearance.snapshot()
    assert (first["growth_stage"], first["idle_variant"]) == ("partner", "smile")
    assert len(first["voice_lines"]) == 2
    assert [story["id"] for story in first["stories"]] == ["chapter:first-trust", "chapter:shared-routine"]
    assert next(item for item in first["outfits"] if item["id"] == "gold-star")["owned"] is True
    assert second == first
    result = appearance.equip(slot="outfit", selection_id="gold-star", idempotency_key="equip:growth")
    assert result["outfit_id"] == "gold-star"
    seen = appearance.mark_story_seen("chapter:first-trust")
    replay = appearance.mark_story_seen("chapter:first-trust")
    assert seen["replayed"] is False and replay["replayed"] is True


def test_locked_story_bad_catalog_and_unknown_fields_fail_closed(tmp_path: Path) -> None:
    appearance = service(tmp_path)
    with pytest.raises(CompanionConflict, match="locked"):
        appearance.mark_story_seen("chapter:first-trust")
    source = json.loads((ROOT / "config" / "companion" / "appearance.json").read_text(encoding="utf-8"))
    source["outfits"][0]["script"] = "run()"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(CompanionRepositoryError, match="option"):
        load_appearance_catalog(bad, image_root=ROOT / "src" / "frontend" / "public" / "mascots")
