from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import secrets
from threading import Lock


_DATA_CLASS = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_TOKEN = re.compile(r"\[\[CRP:([A-Z][A-Z0-9_]{0,31}):([A-Za-z0-9_-]{20,})\]\]")


class TokenVaultError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _VaultEntry:
    token: str
    value: str
    data_class: str
    turn_id: str
    destination_id: str
    expires_at: datetime


class EphemeralTokenVault:
    """Process-memory-only, destination-bound placeholder authority."""

    def __init__(self, *, max_entries: int = 4096) -> None:
        if max_entries < 1:
            raise TokenVaultError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: dict[str, _VaultEntry] = {}
        self._lock = Lock()

    def tokenize(
        self,
        value: str,
        *,
        data_class: str,
        turn_id: str,
        destination_id: str,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> str:
        normalized_class = str(data_class).strip().upper()
        if not _DATA_CLASS.fullmatch(normalized_class):
            raise TokenVaultError("token data class is invalid")
        if not _text(value) or not _text(turn_id) or not _text(destination_id):
            raise TokenVaultError("token binding values must be non-empty")
        if ttl <= timedelta(0) or ttl > timedelta(hours=1):
            raise TokenVaultError("token ttl must be within one hour")
        current = _aware_now(now)
        opaque = secrets.token_urlsafe(24)
        token = f"[[CRP:{normalized_class}:{opaque}]]"
        entry = _VaultEntry(
            token=token,
            value=value,
            data_class=normalized_class,
            turn_id=turn_id,
            destination_id=destination_id,
            expires_at=current + ttl,
        )
        with self._lock:
            self._purge_locked(current)
            if len(self._entries) >= self._max_entries:
                raise TokenVaultError("token vault capacity exceeded")
            self._entries[token] = entry
        return token

    def rehydrate_exact(
        self,
        text: str,
        *,
        turn_id: str,
        destination_id: str,
        trusted_projection: bool,
        now: datetime | None = None,
    ) -> str:
        if not trusted_projection:
            raise TokenVaultError("token rehydration requires a trusted local projection")
        current = _aware_now(now)
        tokens = [match.group(0) for match in _TOKEN.finditer(text)]
        if len(tokens) != len(set(tokens)):
            raise TokenVaultError("token replay in one projection is forbidden")
        if not tokens:
            return text
        with self._lock:
            self._purge_locked(current)
            entries: list[_VaultEntry] = []
            for token in tokens:
                entry = self._entries.get(token)
                if entry is None:
                    raise TokenVaultError("token is unknown or expired")
                if entry.turn_id != turn_id or entry.destination_id != destination_id:
                    raise TokenVaultError("token binding mismatch")
                entries.append(entry)
            output = text
            for entry in entries:
                output = output.replace(entry.token, entry.value, 1)
            for entry in entries:
                self._entries.pop(entry.token, None)
            return output

    def purge_expired(self, *, now: datetime | None = None) -> int:
        current = _aware_now(now)
        with self._lock:
            return self._purge_locked(current)

    def _purge_locked(self, now: datetime) -> int:
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in expired:
            self._entries.pop(token, None)
        return len(expired)


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise TokenVaultError("token time must be timezone-aware")
    return current


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
