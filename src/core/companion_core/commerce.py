from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .errors import CompanionConflict, CompanionIntegrityError, CompanionRepositoryError
from .repository import CompanionRepository, _state_snapshot_from_row
from .state import EconomyRules

_ID = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_KINDS = {"food", "outfit", "frame"}


@dataclass(frozen=True, slots=True)
class CatalogItem:
    item_id: str; kind: str; name: str; image: str; effect: dict[str, object]
    replies: tuple[str, ...]; animation_id: str


@dataclass(frozen=True, slots=True)
class ShopOffer:
    offer_id: str; item_id: str; price: int; available: bool


@dataclass(frozen=True, slots=True)
class CompanionCatalog:
    version: int; items: dict[str, CatalogItem]; offers: dict[str, ShopOffer]


def load_catalog(items_path: Path, shop_path: Path, *, image_root: Path | None = None) -> CompanionCatalog:
    items_doc = _read_json(items_path)
    shop_doc = _read_json(shop_path)
    if set(items_doc) != {"schema_version", "items"} or set(shop_doc) != {"schema_version", "offers"}:
        raise CompanionRepositoryError("companion catalog schema is invalid")
    version = items_doc["schema_version"]
    if version != shop_doc["schema_version"] or not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CompanionRepositoryError("companion catalog versions do not match")
    items: dict[str, CatalogItem] = {}
    for value in items_doc["items"] if isinstance(items_doc["items"], list) else ():
        expected = {"id", "kind", "name", "image", "effect", "reply_templates", "animation_id"}
        if not isinstance(value, dict) or set(value) != expected or not _valid_id(value["id"]) or value["kind"] not in _KINDS:
            raise CompanionRepositoryError("companion item is invalid")
        if value["id"] in items or not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 40:
            raise CompanionRepositoryError("companion item identity is invalid")
        image = value["image"]
        if not isinstance(image, str) or not re.fullmatch(r"items/[a-z0-9-]+\.svg", image):
            raise CompanionRepositoryError("companion item image is invalid")
        if image_root is not None and not (image_root / image).is_file():
            raise CompanionRepositoryError("companion item image is missing")
        replies = value["reply_templates"]
        if not isinstance(replies, list) or not replies or any(not isinstance(x, str) or not 1 <= len(x) <= 100 for x in replies):
            raise CompanionRepositoryError("companion item replies are invalid")
        if not isinstance(value["effect"], dict) or not _valid_id(value["animation_id"]):
            raise CompanionRepositoryError("companion item effect is invalid")
        if value["kind"] == "food" and (set(value["effect"]) != {"affinity", "mood"} or any(not isinstance(value["effect"][k], int) or not 0 <= value["effect"][k] <= 10 for k in ("affinity", "mood"))):
            raise CompanionRepositoryError("companion food effect is invalid")
        if value["kind"] == "outfit" and (set(value["effect"]) != {"pack_id", "outfit_id"} or not _valid_id(value["effect"].get("pack_id")) or not _valid_id(value["effect"].get("outfit_id"))):
            raise CompanionRepositoryError("companion outfit effect is invalid")
        if value["kind"] == "frame" and (set(value["effect"]) != {"frame_theme"} or value["effect"].get("frame_theme") not in {"night"}):
            raise CompanionRepositoryError("companion frame effect is invalid")
        items[value["id"]] = CatalogItem(value["id"], value["kind"], value["name"], image, dict(value["effect"]), tuple(replies), value["animation_id"])
    offers: dict[str, ShopOffer] = {}
    for value in shop_doc["offers"] if isinstance(shop_doc["offers"], list) else ():
        if not isinstance(value, dict) or set(value) != {"id", "item_id", "price", "available"} or not _valid_id(value["id"]) or value["item_id"] not in items:
            raise CompanionRepositoryError("companion shop offer is invalid")
        if value["id"] in offers or not isinstance(value["price"], int) or isinstance(value["price"], bool) or not 0 <= value["price"] <= 10000 or not isinstance(value["available"], bool):
            raise CompanionRepositoryError("companion shop offer values are invalid")
        offers[value["id"]] = ShopOffer(value["id"], value["item_id"], value["price"], value["available"])
    if not items or not offers:
        raise CompanionRepositoryError("companion catalog is empty")
    return CompanionCatalog(version, items, offers)


class CompanionCommerceService:
    def __init__(self, repository: CompanionRepository, *, catalog: CompanionCatalog, rules: EconomyRules, now: Callable[[], datetime] | None = None) -> None:
        self.repository, self.catalog, self.rules = repository, catalog, rules
        self.now = now or (lambda: datetime.now(timezone.utc))

    def snapshot(self) -> dict[str, object]:
        self.repository.initialize()
        connection = self.repository._open_connection()
        try:
            quantities = {str(r["item_id"]): {"quantity": int(r["quantity"]), "revision": int(r["revision"])} for r in connection.execute("SELECT * FROM companion_inventory").fetchall()}
            state = _state_snapshot_from_row(connection.execute("SELECT * FROM companion_state WHERE id='current'").fetchone())
        finally:
            connection.close()
        return {"catalog_version": self.catalog.version, "coins": state.coins, "items": [{"id": i.item_id, "kind": i.kind, "name": i.name, "image": i.image, "effect": i.effect, "reply_templates": list(i.replies), "animation_id": i.animation_id, **quantities.get(i.item_id, {"quantity": 0, "revision": 0})} for i in self.catalog.items.values()], "offers": [{"id": o.offer_id, "item_id": o.item_id, "price": o.price, "available": o.available} for o in self.catalog.offers.values()]}

    def purchase(self, offer_id: object, idempotency_key: object) -> dict[str, object]:
        offer = self.catalog.offers.get(offer_id) if isinstance(offer_id, str) else None
        if offer is None or not offer.available or not _valid_id(idempotency_key):
            raise CompanionRepositoryError("shop purchase is invalid")
        return self._mutate("purchase", offer.item_id, idempotency_key, price=offer.price)

    def feed(self, item_id: object, idempotency_key: object) -> dict[str, object]:
        item = self.catalog.items.get(item_id) if isinstance(item_id, str) else None
        if item is None or item.kind != "food" or not _valid_id(idempotency_key):
            raise CompanionRepositoryError("feed request is invalid")
        return self._mutate("feed", item.item_id, idempotency_key, price=0)

    def _mutate(self, operation: str, item_id: str, key: str, *, price: int) -> dict[str, object]:
        self.repository.initialize(); now = self.now()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now): raise CompanionRepositoryError("commerce clock must use UTC")
        receipt_id = "commerce_" + hashlib.sha256((operation + "|" + key).encode()).hexdigest()[:32]
        with self.repository._transaction() as connection:
            prior = connection.execute("SELECT * FROM companion_commerce_receipts WHERE idempotency_key=?", (key,)).fetchone()
            if prior is not None:
                if prior["operation"] != operation or prior["item_id"] != item_id: raise CompanionConflict("commerce idempotency key was reused with different input")
                result = json.loads(prior["result_json"]); result["replayed"] = True; return result
            state_row = connection.execute("SELECT * FROM companion_state WHERE id='current'").fetchone(); state = _state_snapshot_from_row(state_row)
            inventory = connection.execute("SELECT * FROM companion_inventory WHERE item_id=?", (item_id,)).fetchone()
            quantity = int(inventory["quantity"]) if inventory else 0; revision = int(inventory["revision"]) if inventory else 0
            action_id = None
            if operation == "purchase":
                if state.coins < price: raise CompanionConflict("wallet balance is insufficient")
                if self.catalog.items[item_id].kind != "food" and quantity > 0: raise CompanionConflict("entitlement is already owned")
                quantity += 1; coins = state.coins - price
                connection.execute("UPDATE companion_state SET coins=?, revision=revision+1, updated_at=? WHERE id='current'", (coins, now.isoformat()))
                connection.execute("INSERT INTO companion_wallet_ledger(transaction_id,idempotency_key,reason,delta,balance_after,created_at) VALUES(?,?,?,?,?,?)", (f"wallet:{receipt_id}", f"commerce:{key}", f"购买{self.catalog.items[item_id].name}", -price, coins, now.isoformat()))
            else:
                if quantity < 1: raise CompanionConflict("inventory item is unavailable")
                quantity -= 1; item = self.catalog.items[item_id]; affinity = min(100, state.affinity + int(item.effect["affinity"])); mood_score = min(100, state.mood_score + int(item.effect["mood"])); level = max(i for i,t in enumerate(self.rules.affinity_thresholds) if affinity >= t); mood = "sad" if mood_score <= self.rules.sad_max else "happy" if mood_score >= self.rules.happy_min else "normal"; action_id = f"act_{receipt_id[9:]}"
                unlocks = []
                for threshold in self.rules.affinity_thresholds[1:]:
                    if state.affinity < threshold <= affinity:
                        cursor = connection.execute("INSERT OR IGNORE INTO companion_unlock_events(unlock_id,affinity_threshold,rule_version,created_at) VALUES(?,?,?,?)", (f"affinity:{threshold}", threshold, self.rules.version, now.isoformat()))
                        if cursor.rowcount == 1: unlocks.append(threshold)
                connection.execute("UPDATE companion_state SET affinity=?, affinity_level=?, mood_score=?, mood=?, revision=revision+1, updated_at=? WHERE id='current'", (affinity, level, mood_score, mood, now.isoformat()))
                connection.execute("INSERT INTO companion_state_actions(action_id,idempotency_key,command,subject_id,local_day,rule_version,affinity_before,affinity_after,affinity_level_after,mood_before,mood_after,mood_label_after,coins_before,coins_after,outfit_id,background_id,state_revision,unlocks_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (action_id, f"feed:{key}", "feed", item_id, now.astimezone().date().isoformat(), self.rules.version, state.affinity, affinity, level, state.mood_score, mood_score, mood, state.coins, state.coins, state.outfit_id, state.background_id, state.revision+1, json.dumps(unlocks), now.isoformat()))
            revision += 1
            connection.execute("INSERT INTO companion_inventory(item_id,quantity,revision,updated_at) VALUES(?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET quantity=excluded.quantity,revision=excluded.revision,updated_at=excluded.updated_at", (item_id, quantity, revision, now.isoformat()))
            result = {"receipt_id": receipt_id, "operation": operation, "item_id": item_id, "quantity_after": quantity, "inventory_revision": revision, "price": price, "coins_after": state.coins-price if operation == "purchase" else state.coins, "state_action_id": action_id, "replayed": False}
            connection.execute("INSERT INTO companion_commerce_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)", (receipt_id,key,operation,item_id,self.catalog.version,quantity,revision,price,action_id,json.dumps(result,separators=(",",":")),now.isoformat()))
            return result


def _valid_id(value: object) -> bool: return isinstance(value, str) and _ID.fullmatch(value) is not None
def _read_json(path: Path) -> dict[str, object]:
    try: raw = path.read_bytes()
    except OSError as exc: raise CompanionRepositoryError("companion catalog is unavailable") from exc
    if len(raw) > 128 * 1024: raise CompanionRepositoryError("companion catalog is too large")
    try: value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise CompanionRepositoryError("companion catalog is invalid") from exc
    if not isinstance(value, dict): raise CompanionRepositoryError("companion catalog is invalid")
    return value
