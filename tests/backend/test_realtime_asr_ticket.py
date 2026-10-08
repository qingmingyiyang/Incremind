from __future__ import annotations

from backend.api.realtime_asr_ticket import RealtimeAsrTicketAuthority


def test_ticket_is_subject_bound_single_use_and_expires() -> None:
    clock = [100.0]
    authority = RealtimeAsrTicketAuthority(now=lambda: clock[0], ttl_seconds=15)

    first = authority.issue(subject="desktop-a")
    assert authority.consume(first, subject="desktop-b") is False
    assert authority.consume(first, subject="desktop-a") is False

    second = authority.issue(subject="desktop-a")
    assert authority.consume(second, subject="desktop-a") is True
    assert authority.consume(second, subject="desktop-a") is False

    expired = authority.issue(subject="desktop-a")
    clock[0] += 16
    assert authority.consume(expired, subject="desktop-a") is False
