from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.companion_core import (
    CompanionCommerceService, CompanionConflict, CompanionRepository,
    CompanionRepositoryError, CompanionStateReducer, load_catalog, load_economy_rules,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc)


def service(tmp_path: Path) -> tuple[CompanionCommerceService, CompanionStateReducer]:
    repository = CompanionRepository.at_data_root(tmp_path, now=lambda: NOW)
    rules_path = ROOT / "config" / "companion" / "economy-rules.json"
    catalog = load_catalog(ROOT / "config" / "companion" / "items.json", ROOT / "config" / "companion" / "shop.json", image_root=ROOT / "src" / "frontend" / "public" / "mascots")
    return CompanionCommerceService(repository, catalog=catalog, rules=load_economy_rules(rules_path), now=lambda: NOW), CompanionStateReducer(repository, rules_path=rules_path, now=lambda: NOW)


def test_purchase_and_feed_are_atomic_idempotent_and_persistent(tmp_path: Path) -> None:
    commerce, reducer = service(tmp_path)
    reducer.daily_check_in()
    bought = commerce.purchase("offer:cookie", "purchase:test")
    replay = commerce.purchase("offer:cookie", "purchase:test")
    assert (bought["coins_after"], bought["quantity_after"], replay["replayed"]) == (6, 1, True)
    fed = commerce.feed("food:cookie", "feed:test")
    assert (fed["quantity_after"], fed["coins_after"]) == (0, 6)
    assert commerce.snapshot()["coins"] == 6
    assert reducer.snapshot().affinity == 2
    assert reducer.repository.wallet_integrity().consistent is True
    with pytest.raises(CompanionConflict, match="unavailable"):
        commerce.feed("food:cookie", "feed:empty")


def test_purchase_rolls_back_when_balance_is_insufficient(tmp_path: Path) -> None:
    commerce, reducer = service(tmp_path)
    with pytest.raises(CompanionConflict, match="insufficient"):
        commerce.purchase("offer:red-scarf", "purchase:poor")
    assert commerce.snapshot()["items"][2]["quantity"] == 0
    assert reducer.repository.wallet_integrity().consistent is True


def test_catalog_rejects_missing_image(tmp_path: Path) -> None:
    with pytest.raises(CompanionRepositoryError, match="missing"):
        load_catalog(ROOT / "config" / "companion" / "items.json", ROOT / "config" / "companion" / "shop.json", image_root=tmp_path)
