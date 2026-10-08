from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import CompanionRepositoryError
from .models import CompanionStateActionResult, CompanionStateProjection, CompanionStateSnapshot, CompanionWalletEntry
from .repository import CompanionRepository


@dataclass(frozen=True, slots=True)
class StateRule:
    affinity: int
    mood: int
    coins: int
    cooldown_seconds: int
    daily_limit: int
    reason: str


@dataclass(frozen=True, slots=True)
class EconomyRules:
    version: int
    affinity_thresholds: tuple[int, ...]
    sad_max: int
    happy_min: int
    commands: dict[str, StateRule]


class CompanionStateReducer:
    DAILY_MOOD_STEP = 2
    DAILY_MOOD_MAX_DAYS = 30

    def __init__(
        self, repository: CompanionRepository, *, rules_path: Path,
        now: Callable[[], datetime] | None = None, local_day: Callable[[datetime], str] | None = None,
    ) -> None:
        self.repository = repository
        self.rules = load_economy_rules(rules_path)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.local_day = local_day or (lambda value: value.astimezone().date().isoformat())

    def snapshot(self) -> CompanionStateSnapshot:
        self.repository.initialize()
        self.reconcile_daily_mood()
        return self.repository.get_state_snapshot()

    def reconcile_daily_mood(self) -> CompanionStateActionResult:
        now = self.now()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise CompanionRepositoryError("state reducer clock must use UTC")
        local_day = self.local_day(now)
        return self.repository.reconcile_daily_mood(
            local_day=local_day,
            rule_version=self.rules.version,
            step=self.DAILY_MOOD_STEP,
            max_days=self.DAILY_MOOD_MAX_DAYS,
            sad_max=self.rules.sad_max,
            happy_min=self.rules.happy_min,
            created_at=now.isoformat(),
        )

    def wallet(self, *, limit: int = 20) -> tuple[CompanionWalletEntry, ...]:
        self.repository.initialize()
        return self.repository.list_wallet_entries(limit=limit)

    def daily_check_in_claimed(self) -> bool:
        now = self.now()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise CompanionRepositoryError("state reducer clock must use UTC")
        return self.repository.has_state_action(command="daily_check_in", local_day=self.local_day(now))

    def project(self, *, transient_modifier: int = 0, expires_at: datetime | None = None) -> CompanionStateProjection:
        if not isinstance(transient_modifier, int) or isinstance(transient_modifier, bool) or not -20 <= transient_modifier <= 20:
            raise CompanionRepositoryError("transient mood modifier is invalid")
        now = self.now()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise CompanionRepositoryError("state reducer clock must use UTC")
        if expires_at is not None and (expires_at.tzinfo is None or expires_at.utcoffset() != timezone.utc.utcoffset(expires_at)):
            raise CompanionRepositoryError("transient mood expiry must use UTC")
        active_modifier = transient_modifier if expires_at is not None and expires_at > now else 0
        snapshot = self.snapshot()
        score = min(100, max(-100, snapshot.mood_score + active_modifier))
        mood = "sad" if score <= self.rules.sad_max else "happy" if score >= self.rules.happy_min else "normal"
        return CompanionStateProjection(
            snapshot=snapshot, effective_mood_score=score, effective_mood=mood,
            transient_modifier=active_modifier,
            transient_expires_at=expires_at.isoformat() if active_modifier else None,
        )

    def apply(self, *, command: object, idempotency_key: object, subject_id: object = None) -> CompanionStateActionResult:
        if not isinstance(command, str) or command not in self.rules.commands:
            raise CompanionRepositoryError("state action command is not allowed")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise CompanionRepositoryError("state action idempotency key is invalid")
        if subject_id is not None and (not isinstance(subject_id, str) or not subject_id):
            raise CompanionRepositoryError("state action subject is invalid")
        now = self.now()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise CompanionRepositoryError("state reducer clock must use UTC")
        return self._apply_at(command=command, idempotency_key=idempotency_key, subject_id=subject_id, now=now)

    def daily_check_in(self) -> CompanionStateActionResult:
        now = self.now()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise CompanionRepositoryError("state reducer clock must use UTC")
        local_day = self.local_day(now)
        return self._apply_at(
            command="daily_check_in", idempotency_key=f"daily_check_in:{local_day}", subject_id=None, now=now
        )

    def apply_chat_affect(self, *, signal: object, request_id: object) -> CompanionStateActionResult | None:
        if not isinstance(signal, str) or signal not in {"positive", "neutral", "negative"}:
            raise CompanionRepositoryError("chat affect signal is invalid")
        if not isinstance(request_id, str) or not request_id:
            raise CompanionRepositoryError("chat affect request id is invalid")
        if signal == "neutral":
            return None
        now = self.now()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise CompanionRepositoryError("state reducer clock must use UTC")
        command = f"chat_affect_{signal}"
        digest = hashlib.sha256(f"{command}|{request_id}".encode("utf-8")).hexdigest()[:32]
        return self.repository.apply_state_action(
            action_id=f"act_{digest}", idempotency_key=f"affect:{request_id}", command=command,
            subject_id=None, local_day=self.local_day(now), rule_version=self.rules.version,
            affinity_delta=0, mood_delta=1 if signal == "positive" else -1, coin_delta=0,
            daily_limit=8, cooldown_seconds=0, affinity_thresholds=self.rules.affinity_thresholds,
            sad_max=self.rules.sad_max, happy_min=self.rules.happy_min, wallet_reason="对话情绪反馈",
            created_at=now.isoformat(),
        )

    def _apply_at(self, *, command: str, idempotency_key: str, subject_id: str | None, now: datetime) -> CompanionStateActionResult:
        rule = self.rules.commands[command]
        digest = hashlib.sha256(f"{command}|{idempotency_key}".encode("utf-8")).hexdigest()[:32]
        self.repository.initialize()
        return self.repository.apply_state_action(
            action_id=f"act_{digest}", idempotency_key=idempotency_key, command=command, subject_id=subject_id,
            local_day=self.local_day(now), rule_version=self.rules.version,
            affinity_delta=rule.affinity, mood_delta=rule.mood, coin_delta=rule.coins,
            daily_limit=rule.daily_limit, cooldown_seconds=rule.cooldown_seconds,
            affinity_thresholds=self.rules.affinity_thresholds, sad_max=self.rules.sad_max,
            happy_min=self.rules.happy_min, wallet_reason=rule.reason, created_at=now.isoformat(),
        )


def companion_chat_state_context(projection: CompanionStateProjection) -> Mapping[str, object]:
    mood = projection.effective_mood
    if mood == "sad":
        reply_style = "brief"
    elif mood == "happy":
        reply_style = "warm"
    elif mood == "normal":
        reply_style = "balanced"
    else:
        raise CompanionRepositoryError("companion chat mood is invalid")
    return {"mood": mood, "reply_style": reply_style}


def load_economy_rules(path: Path) -> EconomyRules:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CompanionRepositoryError("economy rules are unavailable") from exc
    if len(raw) > 64 * 1024:
        raise CompanionRepositoryError("economy rules are too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionRepositoryError("economy rules are invalid") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "affinity_thresholds", "mood", "commands"}:
        raise CompanionRepositoryError("economy rules schema is invalid")
    version = value["schema_version"]
    thresholds = value["affinity_thresholds"]
    mood = value["mood"]
    commands = value["commands"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CompanionRepositoryError("economy rule version is invalid")
    if thresholds != [0, 25, 50, 75, 100]:
        raise CompanionRepositoryError("affinity thresholds are invalid")
    if not isinstance(mood, dict) or set(mood) != {"sad_max", "happy_min"} or mood["sad_max"] >= mood["happy_min"]:
        raise CompanionRepositoryError("mood thresholds are invalid")
    if not isinstance(commands, dict) or not commands or len(commands) > 32:
        raise CompanionRepositoryError("economy commands are invalid")
    parsed: dict[str, StateRule] = {}
    for command, rule in commands.items():
        if not isinstance(command, str) or not command or not isinstance(rule, dict) or set(rule) != {
            "affinity", "mood", "coins", "cooldown_seconds", "daily_limit", "reason"
        }:
            raise CompanionRepositoryError("economy command rule is invalid")
        numbers = [rule["affinity"], rule["mood"], rule["coins"], rule["cooldown_seconds"], rule["daily_limit"]]
        if any(not isinstance(item, int) or isinstance(item, bool) for item in numbers):
            raise CompanionRepositoryError("economy command values are invalid")
        if not 0 <= rule["affinity"] <= 10 or not -10 <= rule["mood"] <= 10 or not -100 <= rule["coins"] <= 100:
            raise CompanionRepositoryError("economy command deltas are out of bounds")
        if not 0 <= rule["cooldown_seconds"] <= 86400 or not 1 <= rule["daily_limit"] <= 100:
            raise CompanionRepositoryError("economy command limits are invalid")
        if not isinstance(rule["reason"], str) or not 1 <= len(rule["reason"]) <= 80:
            raise CompanionRepositoryError("economy command reason is invalid")
        parsed[command] = StateRule(*numbers[:3], cooldown_seconds=numbers[3], daily_limit=numbers[4], reason=rule["reason"])
    return EconomyRules(version, tuple(thresholds), mood["sad_max"], mood["happy_min"], parsed)
