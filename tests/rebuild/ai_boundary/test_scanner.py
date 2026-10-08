from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.ai_boundary import EphemeralTokenVault, SensitiveTextScanner


NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("text", "data_class"),
    [
        ("key=sk-1234567890abcdefghij", "credential"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz.123", "credential"),
        ("api_key=abcdefghijklmnop", "credential"),
        ("身份证 11010519491231002X", "government_id"),
        ("银行卡 4111 1111 1111 1111", "payment"),
    ],
)
def test_hard_sensitive_content_is_blocked_without_outgoing_text(text: str, data_class: str) -> None:
    result = SensitiveTextScanner().sanitize_for_remote(
        text,
        vault=EphemeralTokenVault(),
        turn_id="turn-1",
        destination_id="provider-a",
        now=NOW,
    )
    assert result.outcome == "blocked"
    assert result.transformed_text is None
    assert result.summary.hard_blocked_classes == (data_class,)
    assert text not in repr(result.summary)


def test_soft_pii_is_replaced_by_opaque_turn_bound_tokens() -> None:
    text = "联系 alice@example.com 或 13800138000。"
    result = SensitiveTextScanner().sanitize_for_remote(
        text,
        vault=EphemeralTokenVault(),
        turn_id="turn-1",
        destination_id="provider-a",
        now=NOW,
    )
    assert result.outcome == "redacted"
    assert result.token_count == 2
    assert result.transformed_text is not None
    assert "alice@example.com" not in result.transformed_text
    assert "13800138000" not in result.transformed_text
    assert "[[CRP:EMAIL:" in result.transformed_text
    assert "[[CRP:PHONE:" in result.transformed_text
    assert result.summary.counts == (("email", 1), ("phone", 1))


def test_clean_text_is_preserved_without_vault_entry() -> None:
    text = "请把本次会议整理成行动项。"
    result = SensitiveTextScanner().sanitize_for_remote(
        text,
        vault=EphemeralTokenVault(),
        turn_id="turn-1",
        destination_id="provider-a",
        now=NOW,
    )
    assert result.outcome == "clean"
    assert result.transformed_text == text
    assert result.summary.counts == ()


def test_invalid_bank_card_candidate_is_not_classified_as_payment() -> None:
    findings = SensitiveTextScanner().scan("编号 1234 5678 9012 3456")
    assert all(finding.data_class != "payment" for finding in findings)


def test_summary_never_contains_matched_values() -> None:
    text = "alice@example.com 13800138000"
    findings = SensitiveTextScanner().scan(text)
    encoded = repr(findings)
    assert "alice@example.com" not in encoded
    assert "13800138000" not in encoded
