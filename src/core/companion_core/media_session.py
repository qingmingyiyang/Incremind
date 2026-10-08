from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets

from .errors import CompanionConflict, CompanionRepositoryError
from .model_routes import CompanionModelRouter, compose_companion_prompt
from .repository import CompanionRepository


MEDIA_PLAYBACK_STATES = frozenset({"playing", "paused", "stopped", "closed", "unknown"})
_OBSERVATION_ID = re.compile(r"^media:[a-f0-9][a-f0-9-]{7,95}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_COOLDOWN = timedelta(minutes=30)
_MAX_RECENT = 32


@dataclass(frozen=True, slots=True)
class CompanionMediaConfig:
    enabled: bool
    model_commentary_enabled: bool
    revision: int
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "model_commentary_enabled": self.model_commentary_enabled,
        }


class CompanionMediaSessionService:
    CONFIG_ID = "media_session"
    RUNTIME_ID = "media_session_runtime"

    def __init__(
        self,
        repository: CompanionRepository,
        *,
        model_router: CompanionModelRouter | None = None,
        character_prompt_loader: Callable[[], tuple[str, int]] | None = None,
        now: Callable[[], datetime] | None = None,
        key_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.model_router = model_router
        self.character_prompt_loader = character_prompt_loader or (lambda: ("温和陪伴用户。", 1))
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.key_factory = key_factory or (lambda: secrets.token_hex(32))

    def status(self) -> dict[str, object]:
        config = self.config()
        runtime = self._runtime(create=False)
        return {
            "config": config.as_dict(),
            "revision": config.revision,
            "updated_at": config.updated_at,
            "runtime": {
                "last_handled_at": runtime["payload"].get("last_handled_at") if runtime else None,
                "state": "enabled" if config.enabled else "disabled",
            },
        }

    def config(self) -> CompanionMediaConfig:
        self.repository.initialize()
        setting = self.repository.get_setting(self.CONFIG_ID)
        payload = _config(setting.payload if setting else None)
        return CompanionMediaConfig(
            enabled=payload["enabled"],
            model_commentary_enabled=payload["model_commentary_enabled"],
            revision=setting.revision if setting else 0,
            updated_at=setting.updated_at if setting else "",
        )

    def configure(
        self, *, enabled: object, model_commentary_enabled: object, expected_revision: object,
    ) -> CompanionMediaConfig:
        payload = _config({"enabled": enabled, "model_commentary_enabled": model_commentary_enabled})
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise CompanionRepositoryError("media settings revision is invalid")
        now = self._now()
        setting = self.repository.save_setting(
            setting_id=self.CONFIG_ID,
            payload=payload,
            expected_revision=expected_revision,
            updated_at=now.isoformat(),
        )
        if not payload["enabled"]:
            self._clear_current_runtime(now)
        return CompanionMediaConfig(
            enabled=payload["enabled"],
            model_commentary_enabled=payload["model_commentary_enabled"],
            revision=setting.revision,
            updated_at=setting.updated_at,
        )

    def observe(
        self,
        *,
        observation_id: object,
        title: object,
        artist: object,
        playback_status: object,
        quiet: object,
    ) -> dict[str, object]:
        if not isinstance(observation_id, str) or _OBSERVATION_ID.fullmatch(observation_id) is None:
            raise CompanionRepositoryError("media observation id is invalid")
        safe_title = _metadata(title, "title", required=False)
        safe_artist = _metadata(artist, "artist", required=False)
        if playback_status not in MEDIA_PLAYBACK_STATES or not isinstance(quiet, bool):
            raise CompanionRepositoryError("media observation is invalid")
        config = self.config()
        if not config.enabled:
            return _result("disabled", safe_title, safe_artist, str(playback_status), reason="disabled")
        if playback_status != "playing" or not safe_title:
            return _result(_projected_status(str(playback_status), safe_title), safe_title, safe_artist, str(playback_status), reason="not_playing")

        now = self._now()
        runtime = self._runtime(create=True)
        assert runtime is not None
        payload = runtime["payload"]
        fingerprint = _fingerprint(str(payload["key"]), safe_title, safe_artist)
        if payload.get("current_fingerprint") == fingerprint:
            return _result("playing", safe_title, safe_artist, "playing", reason="unchanged")
        recent = _recent(payload.get("recent"), now=now)
        duplicate = next((item for item in recent if item["fingerprint"] == fingerprint), None)
        reason = "quiet" if quiet else "cooldown" if duplicate else None
        commentary = None
        source = None
        if reason is None:
            commentary, source = self._commentary(
                observation_id=observation_id,
                title=safe_title,
                artist=safe_artist,
                model_enabled=config.model_commentary_enabled,
            )
            reason = "commented"
        next_recent = [item for item in recent if item["fingerprint"] != fingerprint]
        next_recent.insert(0, {"fingerprint": fingerprint, "handled_at": now.isoformat()})
        next_payload = {
            "key": payload["key"],
            "current_fingerprint": fingerprint,
            "last_handled_at": now.isoformat(),
            "recent": next_recent[:_MAX_RECENT],
        }
        self.repository.save_setting(
            setting_id=self.RUNTIME_ID,
            payload=next_payload,
            expected_revision=runtime["revision"],
            updated_at=now.isoformat(),
        )
        return _result(
            "playing", safe_title, safe_artist, "playing",
            reason=reason, commentary=commentary, commentary_source=source,
        )

    def _commentary(
        self, *, observation_id: str, title: str, artist: str, model_enabled: bool,
    ) -> tuple[str, str]:
        local = _local_commentary(title, artist)
        if not model_enabled or self.model_router is None:
            return local, "local"
        try:
            character, epoch = self.character_prompt_loader()
            prompt = compose_companion_prompt(
                route_key="companion.event",
                master_profile={},
                character_prompt=character,
                modifiers={"event_kind": "media_session", "reply_length": "one_or_two_sentences"},
                published_context=(),
                short_term_messages=(),
                user_payload={"event_type": "media_session_changed", "title": title, "artist": artist},
                context_epoch=epoch,
            )
            outcome = self.model_router.execute(
                route_key="companion.event", prompt=prompt, request_id=observation_id,
            )
        except (CompanionRepositoryError, RuntimeError, TypeError, ValueError):
            return local, "local"
        text = outcome.get("text") if outcome.get("source") == "provider" else None
        if not isinstance(text, str):
            return local, "local"
        safe = _metadata(text, "commentary", required=True, maximum=160)
        return safe, "model"

    def _runtime(self, *, create: bool) -> dict[str, object] | None:
        self.repository.initialize()
        setting = self.repository.get_setting(self.RUNTIME_ID)
        if setting is not None:
            payload = _runtime_payload(setting.payload)
            return {"payload": payload, "revision": setting.revision}
        if not create:
            return None
        now = self._now()
        key = self.key_factory()
        if not isinstance(key, str) or re.fullmatch(r"[a-f0-9]{64}", key) is None:
            raise CompanionRepositoryError("media runtime key is invalid")
        try:
            saved = self.repository.save_setting(
                setting_id=self.RUNTIME_ID,
                payload={"key": key, "current_fingerprint": None, "last_handled_at": None, "recent": []},
                expected_revision=0,
                updated_at=now.isoformat(),
            )
        except CompanionConflict:
            saved = self.repository.get_setting(self.RUNTIME_ID)
            if saved is None:
                raise
        return {"payload": _runtime_payload(saved.payload), "revision": saved.revision}

    def _clear_current_runtime(self, now: datetime) -> None:
        runtime = self._runtime(create=False)
        if runtime is None or runtime["payload"].get("current_fingerprint") is None:
            return
        payload = {**runtime["payload"], "current_fingerprint": None}
        self.repository.save_setting(
            setting_id=self.RUNTIME_ID, payload=payload,
            expected_revision=runtime["revision"], updated_at=now.isoformat(),
        )

    def _now(self) -> datetime:
        value = self.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise CompanionRepositoryError("media clock is invalid")
        return value.astimezone(timezone.utc)


def _config(value: object) -> dict[str, bool]:
    if value is None:
        return {"enabled": False, "model_commentary_enabled": False}
    if not isinstance(value, Mapping) or set(value) != {"enabled", "model_commentary_enabled"}:
        raise CompanionRepositoryError("media settings are invalid")
    if not isinstance(value["enabled"], bool) or not isinstance(value["model_commentary_enabled"], bool):
        raise CompanionRepositoryError("media settings are invalid")
    if value["model_commentary_enabled"] and not value["enabled"]:
        raise CompanionRepositoryError("model commentary requires media sensing")
    return {"enabled": value["enabled"], "model_commentary_enabled": value["model_commentary_enabled"]}


def _runtime_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"key", "current_fingerprint", "last_handled_at", "recent"}:
        raise CompanionRepositoryError("stored media runtime is invalid")
    key = value["key"]
    current = value["current_fingerprint"]
    last = value["last_handled_at"]
    if not isinstance(key, str) or re.fullmatch(r"[a-f0-9]{64}", key) is None:
        raise CompanionRepositoryError("stored media runtime is invalid")
    if current is not None and (not isinstance(current, str) or re.fullmatch(r"[a-f0-9]{64}", current) is None):
        raise CompanionRepositoryError("stored media runtime is invalid")
    if last is not None and not isinstance(last, str):
        raise CompanionRepositoryError("stored media runtime is invalid")
    recent = value["recent"]
    if not isinstance(recent, list) or len(recent) > _MAX_RECENT:
        raise CompanionRepositoryError("stored media runtime is invalid")
    for item in recent:
        if not isinstance(item, Mapping) or set(item) != {"fingerprint", "handled_at"}:
            raise CompanionRepositoryError("stored media runtime is invalid")
        if not isinstance(item["fingerprint"], str) or re.fullmatch(r"[a-f0-9]{64}", item["fingerprint"]) is None or not isinstance(item["handled_at"], str):
            raise CompanionRepositoryError("stored media runtime is invalid")
    return {"key": key, "current_fingerprint": current, "last_handled_at": last, "recent": [dict(item) for item in recent]}


def _recent(value: object, *, now: datetime) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        try:
            handled = datetime.fromisoformat(str(item["handled_at"])).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if now - handled < _COOLDOWN and handled <= now + timedelta(seconds=5):
            result.append({"fingerprint": str(item["fingerprint"]), "handled_at": handled.isoformat()})
    return result


def _metadata(value: object, field: str, *, required: bool, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise CompanionRepositoryError(f"media {field} is invalid")
    normalized = " ".join(value.strip().split())
    if _CONTROL.search(value) or len(normalized) > maximum or (required and not normalized):
        raise CompanionRepositoryError(f"media {field} is invalid")
    return normalized


def _fingerprint(key: str, title: str, artist: str) -> str:
    normalized = f"{title.casefold()}\0{artist.casefold()}".encode("utf-8")
    return hmac.new(bytes.fromhex(key), normalized, hashlib.sha256).hexdigest()


def _local_commentary(title: str, artist: str) -> str:
    return f"《{title}》正在播放，我陪你一起听。" if not artist else f"现在是 {artist} 的《{title}》，我陪你一起听。"


def _projected_status(playback: str, title: str) -> str:
    if not title or playback in {"closed", "stopped", "unknown"}:
        return "empty"
    return "paused" if playback == "paused" else "empty"


def _result(
    status: str, title: str, artist: str, playback: str, *, reason: str,
    commentary: str | None = None, commentary_source: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "title": title,
        "artist": artist,
        "playback_status": playback,
        "commentary": commentary,
        "commentary_source": commentary_source,
        "reason": reason,
    }
