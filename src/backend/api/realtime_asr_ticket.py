from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Callable


REALTIME_ASR_TICKET_TTL_SECONDS = 15
_MAX_OUTSTANDING_TICKETS = 16
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


class RealtimeAsrTicketAuthority:
    """Issue single-use renderer tickets without exposing the desktop session secret."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        ttl_seconds: int = REALTIME_ASR_TICKET_TTL_SECONDS,
    ) -> None:
        if not callable(now) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 60:
            raise TypeError("realtime ASR ticket dependencies are invalid")
        self._now = now
        self._ttl_seconds = ttl_seconds
        self._tickets: dict[str, tuple[str, float]] = {}

    def issue(self, *, subject: str) -> str:
        clean_subject = _subject(subject)
        self._prune()
        while len(self._tickets) >= _MAX_OUTSTANDING_TICKETS:
            oldest = min(self._tickets, key=lambda key: self._tickets[key][1])
            self._tickets.pop(oldest, None)
        token = secrets.token_urlsafe(32)
        self._tickets[_digest(token)] = (clean_subject, self._now() + self._ttl_seconds)
        return token

    def consume(self, token: object, *, subject: str) -> bool:
        clean_subject = _subject(subject)
        return self.consume_subject(token) == clean_subject

    def consume_subject(self, token: object) -> str | None:
        """Consume once, retaining the original authority and expiry boundary."""
        self._prune()
        if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            return None
        record = self._tickets.pop(_digest(token), None)
        return record[0] if record is not None and record[1] >= self._now() else None

    def _prune(self) -> None:
        now = self._now()
        for digest, (_subject_value, expires_at) in tuple(self._tickets.items()):
            if expires_at < now:
                self._tickets.pop(digest, None)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _subject(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 191:
        raise ValueError("realtime ASR ticket subject is invalid")
    return value.strip()


__all__ = ("REALTIME_ASR_TICKET_TTL_SECONDS", "RealtimeAsrTicketAuthority")
