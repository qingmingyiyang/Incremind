"""提醒的日期、时段与限定词必须作为完整时间表达解释。"""

from datetime import timedelta, timezone

import pytest

from backend.memory_app.v2.policies.nudge import remind_v1


LOCAL = timezone(timedelta(hours=8))
REFERENCE = '2026-10-07T00:30:00+00:00'


@pytest.mark.parametrize('text,expected', [
    ('提醒我下个周五交申请', '2026-10-16T01:00:00+00:00'),
    ('提醒我下个星期五交申请', '2026-10-16T01:00:00+00:00'),
    ('提醒我下一个周五下午三点交申请', '2026-10-16T07:00:00+00:00'),
    ('提醒我下一个星期一早上复盘', '2026-10-12T01:00:00+00:00'),
    ('提醒我这个周五交申请', '2026-10-09T01:00:00+00:00'),
    ('提醒我明天早上前往会场', '2026-10-08T01:00:00+00:00'),
    ('提醒我明天早上后勤开会', '2026-10-08T01:00:00+00:00'),
])
def test_week_prefix_and_period_are_not_truncated(text, expected):
    assert remind_v1(text, REFERENCE, LOCAL) == {'at': expected, 'text': text}


@pytest.mark.parametrize('text', [
    '提醒我明天早上之前交申请',
    '提醒我明天早上之后交申请',
    '提醒我明天早上以前交申请',
    '提醒我明天早上以后交申请',
    '提醒我明天早上前交申请',
    '提醒我明天早上后交申请',
    '提醒我下周一早上之前交申请',
    '提醒我2027年1月1日早上之后交申请',
    '提醒我上个周五交申请',
    '提醒我下下个周五交申请',
    '提醒我再下个周五交申请',
    '提醒我两个周五交申请',
])
def test_unconsumed_temporal_qualifiers_fall_back(text):
    assert remind_v1(text, REFERENCE, LOCAL) is None


@pytest.mark.parametrize('at,expected', [
    ('2026-10-08T09:00:00+08:00', '2026-10-08T01:00:00+00:00'),
    ('2026-10-08T09:00:00Z', '2026-10-08T09:00:00+00:00'),
    ('2026-10-08T09:00:00', None),
    ('2026-10-07T00:30:00Z', None),
    ('2026-10-06T00:30:00Z', None),
    ('invalid', None),
    (None, None),
])
def test_changed_reminder_time_is_aware_future_and_normalized(at, expected):
    assert remind_v1(at, REFERENCE, LOCAL, operation='at') == expected
