"""你定的提醒只解析确定时间，并保留上游交来的原话。"""

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest


LOCAL = timezone(timedelta(hours=8))
REFERENCE = '2026-10-07T00:30:00+00:00'


@pytest.mark.parametrize('text,expected', [
    ('提醒我明天下午三点喝水', '2026-10-08T07:00:00+00:00'),
    ('提醒我周五交申请', '2026-10-09T01:00:00+00:00'),
    ('提醒我10月8日交申请', '2026-10-08T01:00:00+00:00'),
    ('提醒我三天后核对资料', '2026-10-10T01:00:00+00:00'),
    ('提醒我下周一早上复盘', '2026-10-12T01:00:00+00:00'),
    ('提醒我今天喝水', '2026-10-07T01:00:00+00:00'),
    ('提醒我后天喝水', '2026-10-09T01:00:00+00:00'),
    ('提醒我明天前往会场', '2026-10-08T01:00:00+00:00'),
    ('提醒我两天后喝水', '2026-10-09T01:00:00+00:00'),
    ('提醒我二十一天后复盘', '2026-10-28T01:00:00+00:00'),
    ('提醒我星期日核对资料', '2026-10-11T01:00:00+00:00'),
    ('提醒我本周六上午十点核对资料', '2026-10-10T02:00:00+00:00'),
    ('提醒我周六15:00核对资料', '2026-10-10T07:00:00+00:00'),
    ('提醒我星期六晚上八点核对资料', '2026-10-10T12:00:00+00:00'),
    ('提醒我下星期一早上复盘', '2026-10-12T01:00:00+00:00'),
    ('提醒我下周日晚上八点复盘', '2026-10-18T12:00:00+00:00'),
    ('提醒我十月八日交申请', '2026-10-08T01:00:00+00:00'),
    ('提醒我10月28号交申请', '2026-10-28T01:00:00+00:00'),
    ('提醒我2027年1月1日交申请', '2027-01-01T01:00:00+00:00'),
    ('提醒我2028-02-29交申请', '2028-02-29T01:00:00+00:00'),
    ('提醒我明天上午十一点半复盘', '2026-10-08T03:30:00+00:00'),
    ('提醒我明天中午十二点复盘', '2026-10-08T04:00:00+00:00'),
    ('提醒我明天中午一点复盘', '2026-10-08T05:00:00+00:00'),
    ('提醒我明天凌晨三点复盘', '2026-10-07T19:00:00+00:00'),
    ('提醒我明天早上七点复盘', '2026-10-07T23:00:00+00:00'),
    ('提醒我明天傍晚六点复盘', '2026-10-08T10:00:00+00:00'),
    ('提醒我明天23点复盘', '2026-10-08T15:00:00+00:00'),
    ('提醒我明天0点复盘', '2026-10-07T16:00:00+00:00'),
    ('提醒我明天15:30复盘', '2026-10-08T07:30:00+00:00'),
    ('提醒我明天09:05复盘', '2026-10-08T01:05:00+00:00'),
    ('提醒我明天下午三点二十分复盘', '2026-10-08T07:20:00+00:00'),
    ('提醒我明天下午15点复盘', '2026-10-08T07:00:00+00:00'),
    ('提醒我开会，明天下午三点', '2026-10-08T07:00:00+00:00'),
    ('提醒我  明天 下午三点   喝水\r\n', '2026-10-08T07:00:00+00:00'),
    ('提醒我10 月 8 日 上午十点 #生活/办事', '2026-10-08T02:00:00+00:00'),
])
def test_parse_explicit_reminder_preserves_original_text(text, expected):
    from backend.memory_app.v2.policies.nudge import remind_v1

    assert remind_v1(text, REFERENCE, LOCAL) == {'at': expected, 'text': text}


@pytest.mark.parametrize('text,reference,local_timezone,expected', [
    ('提醒我明天核对资料', '2026-10-07T20:30:00Z', LOCAL,
     '2026-10-09T01:00:00+00:00'),
    ('提醒我明天核对资料', '2026-10-07T02:30:00Z', timezone(timedelta(hours=-7)),
     '2026-10-07T16:00:00+00:00'),
    ('提醒我明天核对资料', '2026-10-07T00:30:00Z', timezone.utc,
     '2026-10-08T09:00:00+00:00'),
    ('提醒我明天核对资料', '2026-10-31T00:30:00Z', LOCAL,
     '2026-11-01T01:00:00+00:00'),
    ('提醒我明天核对资料', '2026-12-31T00:30:00Z', LOCAL,
     '2027-01-01T01:00:00+00:00'),
    ('提醒我明天核对资料', '2028-02-28T00:30:00Z', LOCAL,
     '2028-02-29T01:00:00+00:00'),
    ('提醒我明天核对资料', '2027-02-28T00:30:00Z', LOCAL,
     '2027-03-01T01:00:00+00:00'),
    ('提醒我周五核对资料', '2026-10-09T00:30:00Z', LOCAL,
     '2026-10-09T01:00:00+00:00'),
    ('提醒我周五核对资料', '2026-10-09T01:00:00Z', LOCAL,
     '2026-10-16T01:00:00+00:00'),
    ('提醒我周一核对资料', '2026-10-12T00:30:00Z', LOCAL,
     '2026-10-12T01:00:00+00:00'),
    ('提醒我下周一核对资料', '2026-10-12T00:30:00Z', LOCAL,
     '2026-10-19T01:00:00+00:00'),
    ('提醒我这周一核对资料', '2026-10-12T00:30:00Z', LOCAL,
     '2026-10-12T01:00:00+00:00'),
    ('提醒我明天09:00核对资料', '2026-03-07T13:30:00Z', ZoneInfo('America/New_York'),
     '2026-03-08T13:00:00+00:00'),
])
def test_calendar_uses_supplied_clock_and_timezone(text, reference, local_timezone, expected):
    from backend.memory_app.v2.policies.nudge import remind_v1

    assert remind_v1(text, reference, local_timezone) == {'at': expected, 'text': text}


@pytest.mark.parametrize('text', [
    '明天下午三点提醒我喝水',
    '请提醒我明天下午三点喝水',
    '我想提醒我明天下午三点喝水',
    ' 提醒我明天下午三点喝水',
    '#生活 提醒我明天下午三点喝水',
    '提醒我喝水',
    '提醒我三点喝水',
    '提醒我明天下午喝水',
    '提醒我明天上午喝水',
    '提醒我明天晚上喝水',
    '提醒我明天三点喝水',
    '提醒我明天十二点喝水',
    '提醒我明天上午十五点喝水',
    '提醒我明天下午零点喝水',
    '提醒我明天晚上三点喝水',
    '提醒我明天晚上十二点喝水',
    '提醒我明天25点喝水',
    '提醒我明天下午三点六十分喝水',
    '提醒我明天15:99喝水',
    '提醒我明天24:00喝水',
    '提醒我明天三点一刻喝水',
    '提醒我明天三点一百分喝水',
    '提醒我明天下午三点左右喝水',
    '提醒我大概明天下午三点喝水',
    '提醒我明天下午三点或四点喝水',
    '提醒我明天下午三点-5分喝水',
    '提醒我明天下午三点前喝水',
    '提醒我明天约下午三点喝水',
    '提醒我约明天下午三点喝水',
    '提醒我明天9到10点喝水',
    '提醒我明天或后天喝水',
    '提醒我周五和周六喝水',
    '提醒我下下周一喝水',
    '提醒我每周五喝水',
    '提醒我每隔三天喝水',
    '提醒我每天上午九点喝水',
    '提醒我下个月喝水',
    '提醒我10月喝水',
    '提醒我10月32日喝水',
    '提醒我13月8日喝水',
    '提醒我2027年2月29日喝水',
    '提醒我2026-02-30喝水',
    '提醒我2028-10-080交稿',
    '提醒我2028-10-08T15:00Z交稿',
    '提醒我2028-10-08T15:00+00:00交稿',
    '提醒我26年10月8日喝水',
    '提醒我2026年10月6日喝水',
    '提醒我10月6日喝水',
    '提醒我本周一喝水',
    '提醒我昨天喝水',
    '提醒我负一天后喝水',
    '提醒我-1天后喝水',
    '提醒我一百天后喝水',
    '提醒我10月8日至10月10日出行',
    '提醒我10月8日前交申请',
    '提醒我2028年10月8日或者9日交稿',
    '提醒我2028-10-08~09交稿',
    '提醒我明天上午十点到十一点开会',
    '提醒我下周一之后复盘',
    '提醒我明天核对10月8日的排期',
    '',
    None,
])
def test_unknown_or_ambiguous_schedule_falls_back_without_guessing(text):
    from backend.memory_app.v2.policies.nudge import remind_v1

    assert remind_v1(text, REFERENCE, LOCAL) is None


@pytest.mark.parametrize('text,reference,local_timezone', [
    ('提醒我今天喝水', '2026-10-07T01:00:00Z', LOCAL),
    ('提醒我今天喝水', '2026-10-07T02:00:00Z', LOCAL),
    ('提醒我本周五喝水', '2026-10-09T02:00:00Z', LOCAL),
    ('提醒我10月8日喝水', '2026-10-08T02:00:00Z', LOCAL),
    ('提醒我明天喝水', '9999-12-31T00:30:00Z', LOCAL),
    ('提醒我明天02:30喝水', '2026-03-07T13:30:00Z', ZoneInfo('America/New_York')),
    ('提醒我明天01:30喝水', '2026-10-31T12:30:00Z', ZoneInfo('America/New_York')),
])
def test_past_overflow_and_dst_ambiguity_do_not_create_a_schedule(text, reference, local_timezone):
    from backend.memory_app.v2.policies.nudge import remind_v1

    assert remind_v1(text, reference, local_timezone) is None


@pytest.mark.parametrize('reference', [None, '', 'invalid', '2026-10-07T08:30:00'])
def test_invalid_internal_clock_is_reported(reference):
    from backend.memory_app.v2.policies.nudge import remind_v1

    with pytest.raises(ValueError, match='^invalid_reminder_reference$'):
        remind_v1('提醒我明天喝水', reference, LOCAL)


@pytest.mark.parametrize('local_timezone', [None, 'Asia/Shanghai', 8])
def test_invalid_internal_timezone_is_reported(local_timezone):
    from backend.memory_app.v2.policies.nudge import remind_v1

    with pytest.raises(ValueError, match='^invalid_reminder_timezone$'):
        remind_v1('提醒我明天喝水', REFERENCE, local_timezone)


def test_timezone_is_required_instead_of_reading_the_host_clock():
    from backend.memory_app.v2.policies.nudge import remind_v1

    with pytest.raises(TypeError):
        remind_v1('提醒我明天喝水', REFERENCE)
