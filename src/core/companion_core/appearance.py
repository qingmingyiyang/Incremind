from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .errors import CompanionConflict, CompanionRepositoryError
from .repository import CompanionRepository, _state_snapshot_from_row

_ID = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_VISUALS = {"happy", "sleepy", "surprised"}
_STAGES = {"friend", "partner", "confidant", "bonded"}
_IDLES = {"warm", "smile", "bright", "radiant"}


@dataclass(frozen=True, slots=True)
class AppearanceOption:
    option_id: str
    item_id: str
    label: str
    overlay: str | None = None
    theme: str | None = None


@dataclass(frozen=True, slots=True)
class AppearanceUnlock:
    threshold: int
    growth_stage: str
    idle_variant: str
    voice_line: str
    story_chapter_id: str
    grant_item_id: str | None


@dataclass(frozen=True, slots=True)
class StoryChapter:
    chapter_id: str
    title: str
    text: str
    visual_state: str


@dataclass(frozen=True, slots=True)
class CompanionAppearanceCatalog:
    version: int
    pack_id: str
    outfits: dict[str, AppearanceOption]
    backgrounds: dict[str, AppearanceOption]
    unlocks: tuple[AppearanceUnlock, ...]
    stories: dict[str, StoryChapter]


def load_appearance_catalog(path: Path, *, image_root: Path | None = None) -> CompanionAppearanceCatalog:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CompanionRepositoryError("companion appearance catalog is unavailable") from exc
    if len(raw) > 128 * 1024:
        raise CompanionRepositoryError("companion appearance catalog is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionRepositoryError("companion appearance catalog is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "pack_id", "outfits", "backgrounds", "unlocks", "stories"}:
        raise CompanionRepositoryError("companion appearance schema is invalid")
    version, pack_id = value["schema_version"], value["pack_id"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1 or not _valid_id(pack_id):
        raise CompanionRepositoryError("companion appearance identity is invalid")
    outfits = _options(value["outfits"], kind="outfit", image_root=image_root)
    backgrounds = _options(value["backgrounds"], kind="background", image_root=image_root)
    stories: dict[str, StoryChapter] = {}
    for item in value["stories"] if isinstance(value["stories"], list) else ():
        if not isinstance(item, dict) or set(item) != {"id", "title", "text", "visual_state"}:
            raise CompanionRepositoryError("companion story is invalid")
        chapter_id = item["id"]
        if not _valid_id(chapter_id) or chapter_id in stories or not _bounded_text(item["title"], 40) or not _bounded_text(item["text"], 240) or item["visual_state"] not in _VISUALS:
            raise CompanionRepositoryError("companion story values are invalid")
        stories[chapter_id] = StoryChapter(chapter_id, item["title"].strip(), item["text"].strip(), item["visual_state"])
    unlocks: list[AppearanceUnlock] = []
    expected = 25
    for item in value["unlocks"] if isinstance(value["unlocks"], list) else ():
        allowed = {"threshold", "growth_stage", "idle_variant", "voice_line", "story_chapter_id", "grant_item_id"}
        if not isinstance(item, dict) or not set(item).issubset(allowed) or set(item) < allowed - {"grant_item_id"}:
            raise CompanionRepositoryError("companion appearance unlock is invalid")
        grant = item.get("grant_item_id")
        if item["threshold"] != expected or item["growth_stage"] not in _STAGES or item["idle_variant"] not in _IDLES or not _bounded_text(item["voice_line"], 100) or item["story_chapter_id"] not in stories or (grant is not None and not _valid_id(grant)):
            raise CompanionRepositoryError("companion appearance unlock values are invalid")
        unlocks.append(AppearanceUnlock(item["threshold"], item["growth_stage"], item["idle_variant"], item["voice_line"].strip(), item["story_chapter_id"], grant))
        expected += 25
    if expected != 125 or not outfits or not backgrounds or not stories:
        raise CompanionRepositoryError("companion appearance catalog is incomplete")
    return CompanionAppearanceCatalog(version, pack_id, outfits, backgrounds, tuple(unlocks), stories)


class CompanionAppearanceService:
    def __init__(self, repository: CompanionRepository, *, catalog: CompanionAppearanceCatalog, item_ids: set[str], now: Callable[[], datetime] | None = None) -> None:
        self.repository, self.catalog, self.item_ids = repository, catalog, set(item_ids)
        self.now = now or (lambda: datetime.now(timezone.utc))
        referenced = {option.item_id for option in (*catalog.outfits.values(), *catalog.backgrounds.values())}
        referenced.update(unlock.grant_item_id for unlock in catalog.unlocks if unlock.grant_item_id)
        if not referenced.issubset(self.item_ids):
            raise CompanionRepositoryError("companion appearance references unknown items")

    def snapshot(self) -> dict[str, object]:
        now = self._now()
        self.repository.initialize()
        with self.repository._transaction() as connection:
            self._reconcile(connection, now.isoformat())
            state = _state_snapshot_from_row(connection.execute("SELECT * FROM companion_state WHERE id='current'").fetchone())
            owned = {str(row["item_id"]) for row in connection.execute("SELECT item_id FROM companion_inventory WHERE quantity > 0")}
            progress = {str(row["chapter_id"]): row for row in connection.execute("SELECT * FROM companion_story_progress")}
        unlocks = [item for item in self.catalog.unlocks if item.threshold <= state.affinity]
        current = unlocks[-1] if unlocks else None
        return {
            "catalog_version": self.catalog.version,
            "pack_id": self.catalog.pack_id,
            "outfit_id": state.outfit_id,
            "background_id": state.background_id,
            "growth_stage": current.growth_stage if current else "new",
            "idle_variant": current.idle_variant if current else "default",
            "voice_lines": [item.voice_line for item in unlocks],
            "outfits": [self._project_option(item, item.item_id in owned) for item in self.catalog.outfits.values()],
            "backgrounds": [self._project_option(item, item.item_id in owned) for item in self.catalog.backgrounds.values()],
            "stories": [{"id": story.chapter_id, "title": story.title, "text": story.text, "visual_state": story.visual_state, "seen": progress[story.chapter_id]["seen_at"] is not None} for story in self.catalog.stories.values() if story.chapter_id in progress],
            "state_revision": state.revision,
        }

    def equip(self, *, slot: object, selection_id: object, idempotency_key: object) -> dict[str, object]:
        if slot not in {"outfit", "background"} or not isinstance(selection_id, str) or not _valid_id(selection_id) or not isinstance(idempotency_key, str) or not _valid_id(idempotency_key):
            raise CompanionRepositoryError("appearance request is invalid")
        options = self.catalog.outfits if slot == "outfit" else self.catalog.backgrounds
        option = None if selection_id == "default" else options.get(selection_id)
        if selection_id != "default" and option is None:
            raise CompanionRepositoryError("appearance selection is invalid")
        now = self._now(); self.repository.initialize()
        receipt_id = "appearance_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
        with self.repository._transaction() as connection:
            self._reconcile(connection, now.isoformat())
            prior = connection.execute("SELECT * FROM companion_appearance_receipts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["slot"] != slot or prior["selection_id"] != selection_id:
                    raise CompanionConflict("appearance idempotency key was reused with different input")
                result = json.loads(prior["result_json"]); result["replayed"] = True; return result
            if option is not None:
                owned = connection.execute("SELECT quantity FROM companion_inventory WHERE item_id=?", (option.item_id,)).fetchone()
                if owned is None or int(owned["quantity"]) < 1:
                    raise CompanionConflict("appearance entitlement is not owned")
            column = "outfit_id" if slot == "outfit" else "background_id"
            connection.execute(f"UPDATE companion_state SET {column}=?, revision=revision+1, updated_at=? WHERE id='current'", (selection_id, now.isoformat()))
            state = _state_snapshot_from_row(connection.execute("SELECT * FROM companion_state WHERE id='current'").fetchone())
            result = {"receipt_id": receipt_id, "slot": slot, "selection_id": selection_id, "outfit_id": state.outfit_id, "background_id": state.background_id, "state_revision": state.revision, "replayed": False}
            connection.execute("INSERT INTO companion_appearance_receipts VALUES(?,?,?,?,?,?,?)", (receipt_id, idempotency_key, slot, selection_id, state.revision, json.dumps(result, separators=(",", ":")), now.isoformat()))
            return result

    def mark_story_seen(self, chapter_id: object) -> dict[str, object]:
        if not isinstance(chapter_id, str) or chapter_id not in self.catalog.stories:
            raise CompanionRepositoryError("story chapter is invalid")
        now = self._now(); self.repository.initialize()
        with self.repository._transaction() as connection:
            self._reconcile(connection, now.isoformat())
            row = connection.execute("SELECT * FROM companion_story_progress WHERE chapter_id=?", (chapter_id,)).fetchone()
            if row is None:
                raise CompanionConflict("story chapter is locked")
            if row["seen_at"] is None:
                connection.execute("UPDATE companion_story_progress SET seen_at=?,revision=revision+1 WHERE chapter_id=?", (now.isoformat(), chapter_id))
            updated = connection.execute("SELECT * FROM companion_story_progress WHERE chapter_id=?", (chapter_id,)).fetchone()
        return {"chapter_id": chapter_id, "seen": True, "revision": int(updated["revision"]), "replayed": row["seen_at"] is not None}

    def _reconcile(self, connection, stamp: str) -> None:
        state = connection.execute("SELECT affinity FROM companion_state WHERE id='current'").fetchone()
        affinity = int(state["affinity"])
        for unlock in self.catalog.unlocks:
            if unlock.threshold > affinity: break
            connection.execute("INSERT OR IGNORE INTO companion_unlock_events(unlock_id,affinity_threshold,rule_version,created_at) VALUES(?,?,?,?)", (f"affinity:{unlock.threshold}", unlock.threshold, self.catalog.version, stamp))
            connection.execute("INSERT OR IGNORE INTO companion_story_progress(chapter_id,affinity_threshold,unlocked_at,seen_at,revision) VALUES(?,?,?,NULL,1)", (unlock.story_chapter_id, unlock.threshold, stamp))
            if unlock.grant_item_id:
                connection.execute("INSERT INTO companion_inventory(item_id,quantity,revision,updated_at) VALUES(?,1,1,?) ON CONFLICT(item_id) DO UPDATE SET quantity=MAX(quantity,1),revision=CASE WHEN quantity < 1 THEN revision+1 ELSE revision END,updated_at=CASE WHEN quantity < 1 THEN excluded.updated_at ELSE updated_at END", (unlock.grant_item_id, stamp))

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise CompanionRepositoryError("appearance clock must use UTC")
        return value

    @staticmethod
    def _project_option(item: AppearanceOption, owned: bool) -> dict[str, object]:
        return {"id": item.option_id, "item_id": item.item_id, "label": item.label, "owned": owned, **({"overlay": item.overlay} if item.overlay else {}), **({"theme": item.theme} if item.theme else {})}


def _options(value: object, *, kind: str, image_root: Path | None) -> dict[str, AppearanceOption]:
    output: dict[str, AppearanceOption] = {}
    expected = {"id", "item_id", "label", "overlay" if kind == "outfit" else "theme"}
    for item in value if isinstance(value, list) else ():
        if not isinstance(item, dict) or set(item) != expected or not _valid_id(item.get("id")) or not _valid_id(item.get("item_id")) or not _bounded_text(item.get("label"), 40):
            raise CompanionRepositoryError("companion appearance option is invalid")
        option_id = item["id"]
        if option_id in output:
            raise CompanionRepositoryError("companion appearance option is duplicated")
        if kind == "outfit":
            resource = item["overlay"]
            if not isinstance(resource, str) or re.fullmatch(r"items/[a-z0-9-]+\.svg", resource) is None or image_root is not None and not (image_root / resource).is_file():
                raise CompanionRepositoryError("companion outfit resource is invalid")
            output[option_id] = AppearanceOption(option_id, item["item_id"], item["label"].strip(), overlay=resource)
        else:
            if item["theme"] not in {"night"}:
                raise CompanionRepositoryError("companion background theme is invalid")
            output[option_id] = AppearanceOption(option_id, item["item_id"], item["label"].strip(), theme=item["theme"])
    return output


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _bounded_text(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and not any(ord(character) < 32 and character not in "\n\t" for character in value)
