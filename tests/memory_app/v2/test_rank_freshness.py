"""明确的适用日期只改变召回排序，不改变认识事实。"""
from datetime import datetime

import pytest

from backend.memory_app.v2.policies import ACTIVE, get
from backend.memory_app.v2.policies.types import RankInput


def request(conditions, reference):
    return RankInput(3, .7, conditions=conditions, reference=reference)


def test_original_ranking_formula_is_preserved_when_freshness_becomes_default():
    original = get('rank', version='@1')
    assert original(RankInput(3, .7)) == (3 + 1) * .7
    assert ACTIVE['rank'] == '@2'
    assert get('rank') is get('rank', version='@2')


@pytest.mark.parametrize('conditions,reference,expired', [
    (['2025 年'], '2025-12-31T23:59:59.999999+08:00', False),
    (['2025 年'], '2026-01-01T00:00:00+08:00', True),
    (['2025 年'], '2025-12-31T15:59:59.999999+00:00', False),
    (['2025 年'], '2025-12-31T16:00:00+00:00', True),
    (['2026年2月'], '2026-02-28T23:59:59.999999+08:00', False),
    (['2026年2月'], '2026-03-01T00:00:00+08:00', True),
    (['2024年2月29日'], '2024-02-29T23:59:59.999999+08:00', False),
    (['2024年2月29日'], '2024-03-01T00:00:00+08:00', True),
    (['2026年十二月'], '2027-01-01T00:00:00+08:00', True),
    (['2025-12-31'], '2026-01-01T00:00:00+08:00', True),
    (['2025/12/31'], '2026-01-01T00:00:00+08:00', True),
    (['截至2025年12月31日'], '2026-01-01T00:00:00+08:00', True),
    (['2024年至2025年'], '2025-12-31T23:59:59+08:00', False),
    (['2024年至2025年'], '2026-01-01T00:00:00+08:00', True),
    (['2025年12月1日至2025年12月31日'], '2026-01-01T00:00:00+08:00', True),
    (['2025年', '周末'], '2026-01-01T00:00:00+08:00', True),
])
def test_registered_freshness_uses_the_complete_local_calendar_period(conditions, reference, expired):
    policy = get('rank', version='@2')
    given = request(conditions, reference)
    details = policy.freshness(given)
    original = get('rank', version='@1')(given)
    assert details['stale'] is expired
    assert details['expires_at'] is not None
    assert datetime.fromisoformat(details['expires_at']).tzinfo is not None
    assert policy(given) == original * (.5 if expired else 1)
    text = '山岛露营杯价格为80元。'
    rendered = policy.describe(text, given)
    if expired:
        assert '过时' in rendered
        assert rendered.endswith(text)
    else:
        assert rendered == text


@pytest.mark.parametrize('conditions', [
    [], ['夏季'], ['今年'], ['去年10月'], ['10月8日'],
    ['2024年以后'], ['自2024年起'], ['2024年至今'],
    ['每年2024年10月1日'], ['长期适用于2024年'],
    ['2024年或2026年'], ['2024年', '2026年'],
    ['2026年13月'], ['2026年2月30日'], ['2023年2月29日'],
    ['2026年至2024年'], ['材料出版于2024年'],
    ['2025年', '今年'], ['2025年', '每年10月'], ['2025年', '长期'],
])
def test_insufficient_or_open_date_evidence_keeps_the_original_score_and_text(conditions):
    policy = get('rank', version='@2')
    given = request(conditions, '2028-01-01T00:00:00+08:00')
    assert policy.freshness(given) == {'stale': False, 'expires_at': None}
    assert policy(given) == get('rank', version='@1')(given)
    assert policy.describe('原话保持', given) == '原话保持'


@pytest.mark.parametrize('reference', [None, 'not-a-clock', '2028-01-01T00:00:00'])
def test_missing_or_unqualified_reference_clock_does_not_invent_expiry(reference):
    policy = get('rank', version='@2')
    given = request(['2024年'], reference)
    assert policy.freshness(given) == {'stale': False, 'expires_at': None}
    assert policy(given) == get('rank', version='@1')(given)


def test_stale_prompt_is_in_the_same_original_token_budget():
    from backend.memory_app.v2.budget import ask_instruction, input_tokens, source_texts, user_text
    from backend.memory_app.v2.policies import override
    from backend.shared.llm.message_metadata import _estimate_input_tokens
    chosen = [{'id': 'expired', 'title': '过时：已发布认识', 'excerpt': '价格80元', 'stale': True}]
    with override(rank='@2'):
        instruction = ask_instruction(chosen)
        messages = [{'role': 'system', 'content': instruction},
                    {'role': 'user', 'content': user_text(source_texts(chosen), '价格多少？')}]
        assert '过时' in instruction
        assert input_tokens(chosen, '价格多少？') == _estimate_input_tokens(messages)
    with override(rank='@1'):
        assert '过时' not in ask_instruction(chosen)
