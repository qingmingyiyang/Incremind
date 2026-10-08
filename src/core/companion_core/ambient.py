from __future__ import annotations

import hashlib
import json
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .errors import CompanionConflict, CompanionRepositoryError
from .repository import CompanionRepository, _state_snapshot_from_row
from .state import EconomyRules
from .model_routes import CompanionModelRouter, compose_companion_prompt


@dataclass(frozen=True, slots=True)
class AmbientOption:
    option_id: str
    label: str
    result: str
    affinity: int
    mood: int
    coins: int


@dataclass(frozen=True, slots=True)
class AmbientTemplate:
    template_id: str
    scene: str
    options: tuple[AmbientOption, AmbientOption]


_CATALOG = (
    AmbientTemplate("lost_coin", "散步时发现一枚没有主人的硬币，要怎么处理？", (
        AmbientOption("hand_in", "交给附近的工作人员", "我们把硬币交了出去，心里亮堂堂的。", 2, 2, 1),
        AmbientOption("keep", "先收好并留意失主", "先替失主保管吧，记得之后再问问。", 0, -1, 3),
    )),
    AmbientTemplate("rainy_cat", "窗外的小猫被雨困住了，我们要做点什么吗？", (
        AmbientOption("umbrella", "带伞去帮它", "小猫安全躲进屋檐下，还回头看了我们一眼。", 3, 3, -1),
        AmbientOption("shelter", "在门口搭个纸箱", "临时小窝很快派上了用场。", 2, 2, 0),
    )),
    AmbientTemplate("last_cookie", "盘子里只剩最后一块曲奇了。", (
        AmbientOption("share", "一人一半", "最后一块也能变成双份的好心情。", 3, 2, 0),
        AmbientOption("gift", "全部送给角色", "她小心收下曲奇，笑得格外满足。", 2, 3, 0),
    )),
)


def _option_for_result_ref(value: object) -> AmbientOption | None:
    if not isinstance(value, str) or value.count(":") != 1:
        return None
    template_id, option_id = value.split(":", 1)
    template = next((item for item in _CATALOG if item.template_id == template_id), None)
    return next((item for item in template.options if item.option_id == option_id), None) if template else None


def _provider_template_from_json(value: object) -> AmbientTemplate | None:
    if not isinstance(value, dict) or set(value) != {"scene", "options"}:
        return None
    scene, options = value["scene"], value["options"]
    if not isinstance(scene, str) or not 8 <= len(scene.strip()) <= 120 or any(ord(char) < 32 for char in scene):
        return None
    if not isinstance(options, list) or len(options) != 2:
        return None
    parsed: list[AmbientOption] = []
    labels: set[str] = set(); references: set[str] = set()
    for index, item in enumerate(options):
        if not isinstance(item, dict) or set(item) != {"label", "result_template_id"}:
            return None
        label, reference = item["label"], item["result_template_id"]
        local = _option_for_result_ref(reference)
        if not isinstance(label, str) or not 2 <= len(label.strip()) <= 48 or any(ord(char) < 32 for char in label) or local is None:
            return None
        label = label.strip()
        if label in labels or reference in references:
            return None
        labels.add(label); references.add(reference)
        parsed.append(AmbientOption(reference, label, local.result, local.affinity, local.mood, local.coins))
    return AmbientTemplate("provider", scene.strip(), (parsed[0], parsed[1]))


class CompanionAmbientService:
    SETTING_ID = "ambient_runtime"

    def __init__(self, repository: CompanionRepository, *, rules: EconomyRules,
                 now: Callable[[], datetime] | None = None, choice: Callable[[tuple[AmbientTemplate, ...]], AmbientTemplate] | None = None,
                 model_router: CompanionModelRouter | None = None) -> None:
        self.repository = repository
        self.rules = rules
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.choice = choice or random.choice
        self.model_router = model_router

    def status(self) -> dict[str, object]:
        self.repository.initialize()
        setting = self.repository.get_setting(self.SETTING_ID)
        config = self._config(setting.payload if setting else None)
        connection = self.repository._open_connection()
        try:
            row = connection.execute("SELECT * FROM companion_random_events ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            connection.close()
        return {"revision": setting.revision if setting else 0, "config": config, "event": self._event(row) if row else None}

    def configure(self, *, enabled: object, interval_minutes: object, idle_enabled: object, idle_minutes: object, expected_revision: object) -> dict[str, object]:
        if not isinstance(enabled, bool) or not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool) or not 15 <= interval_minutes <= 1440:
            raise CompanionRepositoryError("ambient settings are invalid")
        if not isinstance(idle_enabled, bool) or not isinstance(idle_minutes, int) or isinstance(idle_minutes, bool) or not 5 <= idle_minutes <= 240:
            raise CompanionRepositoryError("ambient idle settings are invalid")
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise CompanionRepositoryError("ambient settings revision is invalid")
        now = self._now()
        setting = self.repository.save_setting(setting_id=self.SETTING_ID, expected_revision=expected_revision, payload={
            "enabled": enabled,
            "interval_minutes": interval_minutes,
            "idle_enabled": idle_enabled,
            "idle_minutes": idle_minutes,
            "next_at": (now + timedelta(minutes=interval_minutes)).isoformat() if enabled else None,
        }, updated_at=now.isoformat())
        return {"revision": setting.revision, "config": self._config(setting.payload)}

    def idle_message(self, *, idle_seconds: object, quiet: object = False, game: object = False, sleeping: object = False) -> dict[str, object]:
        if not isinstance(idle_seconds, int) or isinstance(idle_seconds, bool) or idle_seconds < 0 or idle_seconds > 31 * 24 * 3600:
            raise CompanionRepositoryError("ambient idle time is invalid")
        if not all(isinstance(value, bool) for value in (quiet, game, sleeping)):
            raise CompanionRepositoryError("ambient idle flags are invalid")
        status = self.status(); config = status["config"]
        if not config["idle_enabled"]:
            return {"message": None, "reason": "disabled", "active": False}
        threshold = int(config["idle_minutes"]) * 60
        if idle_seconds < threshold:
            return {"message": None, "reason": "active", "active": False}
        if quiet or game or sleeping:
            return {"message": None, "reason": "quiet" if quiet else "game" if game else "sleep", "active": True}
        hour = self._now().astimezone().hour
        connection = self.repository._open_connection()
        try: state = _state_snapshot_from_row(connection.execute("SELECT * FROM companion_state WHERE id='current'").fetchone())
        finally: connection.close()
        if 23 <= hour or hour < 7: text = "已经很晚了，还在忙吗？记得让眼睛休息一下。"
        elif state.mood == "sad": text = "我会安静陪着你。回来时，也可以和我说说话。"
        elif hour < 12: text = "忙了这么久，要不要伸个懒腰、喝口水？"
        else: text = "你还在忙吗？我在这里等你回来。"
        return {"message": text, "reason": "idle", "active": True}

    def offer(self, *, require_due: bool, quiet: object = False, game: object = False, sleeping: object = False) -> dict[str, object]:
        if not isinstance(require_due, bool) or not all(isinstance(value, bool) for value in (quiet, game, sleeping)):
            raise CompanionRepositoryError("ambient trigger flags are invalid")
        self.repository.initialize()
        now = self._now()
        # Do not create egress merely because the scheduler polled. The local
        # settings/deadline check happens before the optional Provider call;
        # the transaction below repeats it to preserve race safety.
        status = self.status()
        config_before = status["config"]
        pending = status.get("event")
        due = not require_due or (
            isinstance(config_before.get("next_at"), str)
            and now >= datetime.fromisoformat(str(config_before["next_at"]))
        )
        should_try_provider = not (
            quiet
            or game
            or sleeping
            or (pending and pending.get("state") == "offered")
            or not config_before["enabled"]
            or not due
        )
        provider_template = self._provider_template(now) if should_try_provider else None
        with self.repository._transaction() as connection:
            existing = connection.execute("SELECT * FROM companion_random_events WHERE state='offered' ORDER BY created_at DESC LIMIT 1").fetchone()
            if existing is not None:
                return {"event": self._event(existing), "suppressed": False, "replayed": True}
            setting = connection.execute("SELECT * FROM companion_settings WHERE id=?", (self.SETTING_ID,)).fetchone()
            config = self._config(json.loads(setting["payload_json"]) if setting else None)
            if quiet or game or sleeping or not config["enabled"]:
                return {"event": None, "suppressed": True, "reason": "quiet" if quiet else "game" if game else "sleep" if sleeping else "disabled", "replayed": False}
            if require_due and setting is None:
                config = {**config, "next_at": (now + timedelta(minutes=int(config["interval_minutes"]))).isoformat()}
                connection.execute("INSERT INTO companion_settings(id,revision,payload_json,updated_at) VALUES(?,?,?,?)", (self.SETTING_ID, 1, json.dumps(config, separators=(",", ":")), now.isoformat()))
                return {"event": None, "suppressed": True, "reason": "not_due", "replayed": False}
            if require_due and config["next_at"] and now < datetime.fromisoformat(str(config["next_at"])):
                return {"event": None, "suppressed": True, "reason": "not_due", "replayed": False}
            template = provider_template or self.choice(_CATALOG)
            event_id = "event_" + uuid.uuid4().hex
            options = [{"id": option.option_id, "label": option.label, "result_template_id": option.option_id if option.option_id.count(":") >= 1 else f"{template.template_id}:{option.option_id}"} for option in template.options]
            encoded = json.dumps({"scene": template.scene, "options": options}, ensure_ascii=False, separators=(",", ":"))
            stamp = now.isoformat()
            connection.execute("INSERT INTO companion_random_events(event_id,prompt_ref,options_json,selected_option,result_json,state,revision,created_at,updated_at) VALUES(?,?,?,NULL,NULL,'offered',1,?,?)", (event_id, template.template_id, encoded, stamp, stamp))
            interval = int(config["interval_minutes"])
            payload = {**config, "next_at": (now + timedelta(minutes=interval)).isoformat()}
            current_revision = int(setting["revision"]) if setting else 0
            connection.execute("INSERT INTO companion_settings(id,revision,payload_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload_json=excluded.payload_json,updated_at=excluded.updated_at", (self.SETTING_ID, current_revision + 1, json.dumps(payload, separators=(",", ":")), stamp))
            row = connection.execute("SELECT * FROM companion_random_events WHERE event_id=?", (event_id,)).fetchone()
        return {"event": self._event(row), "suppressed": False, "replayed": False}

    def choose(self, event_id: object, option_id: object, expected_revision: object) -> dict[str, object]:
        if not isinstance(event_id, str) or not event_id.startswith("event_") or not isinstance(option_id, str) or not isinstance(expected_revision, int):
            raise CompanionRepositoryError("random event choice is invalid")
        now = self._now()
        with self.repository._transaction() as connection:
            row = connection.execute("SELECT * FROM companion_random_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise CompanionRepositoryError("random event was not found")
            if row["state"] == "settled":
                if row["selected_option"] != option_id:
                    raise CompanionConflict("random event was already settled with another option")
                return {"event": self._event(row), "replayed": True}
            if row["state"] != "offered" or int(row["revision"]) != expected_revision:
                raise CompanionConflict("random event revision conflict")
            payload = json.loads(row["options_json"])
            selected = next((item for item in payload.get("options", []) if item.get("id") == option_id), None)
            option = _option_for_result_ref(selected.get("result_template_id")) if isinstance(selected, dict) else None
            if option is None:
                raise CompanionRepositoryError("random event option is invalid")
            state = _state_snapshot_from_row(connection.execute("SELECT * FROM companion_state WHERE id='current'").fetchone())
            positive_today = connection.execute("SELECT COUNT(*) FROM companion_state_actions WHERE command='random_event' AND local_day=? AND coins_after>coins_before", (now.astimezone().date().isoformat(),)).fetchone()[0]
            coin_delta = option.coins if option.coins <= 0 or positive_today < 4 else 0
            coin_delta = max(-state.coins, coin_delta)
            affinity = min(100, max(0, state.affinity + option.affinity)); mood_score = min(100, max(-100, state.mood_score + option.mood)); coins = state.coins + coin_delta
            level = max(index for index, threshold in enumerate(self.rules.affinity_thresholds) if affinity >= threshold)
            mood = "sad" if mood_score <= self.rules.sad_max else "happy" if mood_score >= self.rules.happy_min else "normal"
            revision = state.revision + 1; stamp = now.isoformat(); action_id = "act_" + hashlib.sha256((event_id + "|" + option_id).encode()).hexdigest()[:32]
            connection.execute("UPDATE companion_state SET affinity=?,affinity_level=?,mood_score=?,mood=?,coins=?,revision=?,updated_at=? WHERE id='current'", (affinity, level, mood_score, mood, coins, revision, stamp))
            connection.execute("INSERT INTO companion_state_actions(action_id,idempotency_key,command,subject_id,local_day,rule_version,affinity_before,affinity_after,affinity_level_after,mood_before,mood_after,mood_label_after,coins_before,coins_after,outfit_id,background_id,state_revision,unlocks_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (action_id, f"random:{event_id}", "random_event", option_id, now.astimezone().date().isoformat(), self.rules.version, state.affinity, affinity, level, state.mood_score, mood_score, mood, state.coins, coins, state.outfit_id, state.background_id, revision, "[]", stamp))
            if coin_delta:
                connection.execute("INSERT INTO companion_wallet_ledger(transaction_id,idempotency_key,reason,delta,balance_after,created_at) VALUES(?,?,?,?,?,?)", (f"wallet:{action_id}", f"random:{event_id}", "随机事件", coin_delta, coins, stamp))
            result = {"text": option.result, "changes": {"affinity": affinity-state.affinity, "mood": mood_score-state.mood_score, "coins": coin_delta}, "state_revision": revision, "reward_limited": option.coins > 0 and coin_delta == 0}
            connection.execute("UPDATE companion_random_events SET selected_option=?,result_json=?,state='settled',revision=revision+1,updated_at=? WHERE event_id=?", (option_id, json.dumps(result, ensure_ascii=False, separators=(",", ":")), stamp, event_id))
            saved = connection.execute("SELECT * FROM companion_random_events WHERE event_id=?", (event_id,)).fetchone()
        return {"event": self._event(saved), "replayed": False}

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise CompanionRepositoryError("ambient clock must use UTC")
        return value

    def _provider_template(self, now: datetime) -> AmbientTemplate | None:
        if self.model_router is None:
            return None
        try:
            state = self.repository.get_state_snapshot()
            allowed = [f"{template.template_id}:{option.option_id}" for template in _CATALOG for option in template.options]
            prompt = compose_companion_prompt(
                route_key="companion.ambient", master_profile={}, character_prompt="温和、简洁、低打扰。",
                modifiers={"mood": state.mood, "local_hour": now.astimezone().hour}, published_context=(), short_term_messages=(),
                user_payload={"allowed_result_template_ids": allowed}, context_epoch=1,
            )
            outcome = self.model_router.execute(route_key="companion.ambient", prompt=prompt, request_id=f"ambient:{uuid.uuid4().hex}")
            if outcome.get("source") != "provider":
                return None
            parsed = json.loads(str(outcome.get("text") or ""))
            return _provider_template_from_json(parsed)
        except Exception:
            return None

    @staticmethod
    def _config(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {"enabled": True, "interval_minutes": 60, "idle_enabled": True, "idle_minutes": 20, "next_at": None}
        enabled = value.get("enabled"); interval = value.get("interval_minutes"); next_at = value.get("next_at")
        idle_enabled = value.get("idle_enabled", True); idle_minutes = value.get("idle_minutes", 20)
        if not isinstance(enabled, bool) or not isinstance(interval, int) or isinstance(interval, bool) or not 15 <= interval <= 1440 or not isinstance(idle_enabled, bool) or not isinstance(idle_minutes, int) or isinstance(idle_minutes, bool) or not 5 <= idle_minutes <= 240 or (next_at is not None and not isinstance(next_at, str)):
            raise CompanionRepositoryError("stored ambient settings are invalid")
        return {"enabled": enabled, "interval_minutes": interval, "idle_enabled": idle_enabled, "idle_minutes": idle_minutes, "next_at": next_at}

    @staticmethod
    def _event(row: object) -> dict[str, object]:
        payload = json.loads(row["options_json"])
        return {"event_id": str(row["event_id"]), "scene": payload["scene"], "options": payload["options"], "selected_option": row["selected_option"], "result": json.loads(row["result_json"]) if row["result_json"] else None, "state": str(row["state"]), "revision": int(row["revision"]), "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}
