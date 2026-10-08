"""每天的提醒只使用确定的候选、反馈与本机对话时间。"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.memory_app.v2.policies.nudge import NudgePolicy


LOCAL = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)


def test_new_policy_versions_are_registered_without_changing_active_defaults():
    from backend.memory_app.v2 import policies
    from backend.memory_app.v2.policies import nudge

    assert policies.get('nudge', version='@1') is nudge.v1
    assert policies.get('remind', version='@1') is nudge.remind_v1
    assert 'nudge' not in policies.ACTIVE and 'remind' not in policies.ACTIVE
    for interface in ('nudge', 'remind'):
        with pytest.raises(ValueError, match='^unknown_policy_interface$'):
            policies.get(interface)


def candidate(identity, *, kind='date', project='alpha', days=1):
    return {'id': identity, 'kind': kind, 'project_id': project, 'scene': '旅行',
            'at': (NOW + timedelta(days=days)).isoformat() if kind == 'date' else None,
            'city': '成都' if kind == 'place' else None, 'text': '核对预约',
            'evidence_ids': ['e-' + identity]}


def conversation(identity, hour, *, age=0, weekend=False, intent='remember'):
    day = datetime(2026, 10, 4 if weekend else 6, hour, tzinfo=LOCAL) - timedelta(days=age)
    return {'id': identity, 'created_at': day.isoformat(), 'intent': intent}


def test_date_and_place_candidates_share_the_same_local_selection():
    rows = [candidate('date'), candidate('place', kind='place')]
    assert NudgePolicy()(rows, now=NOW, local_timezone=LOCAL) == rows


@pytest.mark.parametrize('limit', range(6))
def test_daily_limit_is_applied_per_delivery_day(limit):
    rows = [candidate(str(i)) for i in range(6)] + [candidate('later', days=2)]
    result = NudgePolicy()(rows, now=NOW, local_timezone=LOCAL, limit=limit)
    assert [row['id'] for row in result] == ([str(i) for i in range(limit)] + ['later'] if limit else [])


@pytest.mark.parametrize('limit', [-1, 6, True, '2'])
def test_invalid_daily_limit_is_reported(limit):
    with pytest.raises(ValueError, match='^invalid_nudge_limit$'):
        NudgePolicy()([], now=NOW, local_timezone=LOCAL, limit=limit)


def test_default_daily_limit_is_two_and_future_window_is_fourteen_days():
    rows = [candidate(str(i)) for i in range(4)] + [candidate('far', days=15)]
    assert [row['id'] for row in NudgePolicy()(rows, now=NOW, local_timezone=LOCAL)] == ['0', '1']


def test_future_window_is_computed_in_the_supplied_local_calendar():
    now = datetime(2026, 10, 7, 20, tzinfo=timezone.utc)
    inside = {**candidate('inside'), 'at': '2026-10-21T15:59:59+00:00'}
    outside = {**candidate('outside'), 'at': '2026-10-21T16:00:00+00:00'}
    assert NudgePolicy()([inside, outside], now=now, local_timezone=LOCAL) == [inside]


@pytest.mark.parametrize('action', ['opened', 'ignored', 'closed', 'delivered'])
def test_same_event_is_not_repeated_within_seven_days(action):
    rows = [candidate('same'), candidate('new')]
    history = [{'id': 'same', 'kind': 'date', 'action': action,
                'at': (NOW - timedelta(days=6)).isoformat()}]
    assert [row['id'] for row in NudgePolicy()(rows, now=NOW, local_timezone=LOCAL,
                                             history=history)] == ['new']


def test_seven_day_boundary_allows_a_repeat_and_future_history_does_not_suppress():
    history = [{'id': 'same', 'kind': 'date', 'action': 'opened',
                'at': (NOW - timedelta(days=7)).isoformat()},
               {'id': 'other', 'kind': 'date', 'action': 'opened',
                'at': (NOW + timedelta(days=1)).isoformat()}]
    assert len(NudgePolicy()([candidate('same'), candidate('other')], now=NOW,
                            local_timezone=LOCAL, history=history)) == 2


def test_repeated_closes_reduce_priority_of_that_kind():
    rows = [candidate('date', days=0), candidate('place', kind='place')]
    history = [{'id': str(i), 'kind': 'date', 'action': 'closed',
                'at': (NOW - timedelta(days=i + 8)).isoformat()} for i in range(3)]
    assert [row['kind'] for row in NudgePolicy()(rows, now=NOW, local_timezone=LOCAL,
                                               history=history, limit=1)] == ['place']


def test_opening_a_kind_breaks_its_consecutive_close_penalty():
    rows = [candidate('date', days=0), candidate('place', kind='place')]
    history = [{'id': str(i), 'kind': 'date', 'action': 'closed',
                'at': (NOW - timedelta(days=i + 8)).isoformat()} for i in range(3)]
    history.append({'id': 'opened-date', 'kind': 'date', 'action': 'opened',
                    'at': (NOW - timedelta(days=7)).isoformat()})
    assert [row['kind'] for row in NudgePolicy()(rows, now=NOW, local_timezone=LOCAL,
                                               history=history, limit=1)] == ['date']


def test_private_candidates_are_omitted_before_delivery():
    row = {**candidate('private'), 'private': True}
    assert NudgePolicy()([row], now=NOW, local_timezone=LOCAL) == []


def test_explicit_user_reminders_bypass_limit_and_close_penalty_when_due():
    due = {**candidate('mine', kind='reminder'), 'at': NOW.isoformat(), 'text': '提醒我喝水'}
    later = {**due, 'id': 'later', 'at': (NOW + timedelta(seconds=1)).isoformat()}
    history = [{'id': 'mine', 'kind': 'reminder', 'action': 'closed', 'at': NOW.isoformat()}]
    assert NudgePolicy()([candidate('date'), due, later], now=NOW, local_timezone=LOCAL,
                         history=history, limit=0) == [due]


def test_workday_and_weekend_hours_are_estimated_separately():
    turns = [conversation('weekday-' + str(i), 17) for i in range(20)]
    turns += [conversation('weekend-' + str(i), 11, weekend=True) for i in range(20)]
    policy = NudgePolicy()
    assert policy.delivery_hour(turns, now=NOW, local_timezone=LOCAL, weekend=False) == 17
    assert policy.delivery_hour(turns, now=NOW, local_timezone=LOCAL, weekend=True) == 11


def test_twenty_rounds_are_required_for_the_requested_day_category():
    turns = [conversation(str(i), 17) for i in range(19)]
    turns += [conversation('weekend-' + str(i), 11, weekend=True) for i in range(20)]
    assert NudgePolicy().delivery_hour(turns, now=NOW, local_timezone=LOCAL, weekend=False) == 9


def test_disabled_recording_does_not_use_stored_conversation_hours():
    turns = [conversation(str(i), 17) for i in range(20)]
    assert NudgePolicy().delivery_hour(turns, now=NOW, local_timezone=LOCAL,
                                       weekend=False, recording_enabled=False) == 9


def test_hour_estimation_counts_only_recent_top_level_remember_and_ask_rounds():
    turns = [conversation('valid-' + str(i), 10, intent='ask') for i in range(20)]
    turns += [conversation('old-' + str(i), 17, age=70) for i in range(100)]
    turns += [conversation('do-' + str(i), 17, intent='do') for i in range(100)]
    turns += [{**conversation('part-' + str(i), 17), 'parent_turn_id': 'parent'} for i in range(100)]
    assert NudgePolicy().delivery_hour(turns, now=NOW, local_timezone=LOCAL, weekend=False) == 10


def test_hour_tie_uses_earlier_hour_and_duplicate_rounds_are_not_counted_twice():
    turns = [conversation('early-' + str(i), 10) for i in range(10)]
    turns += [conversation('late-' + str(i), 17) for i in range(10)]
    assert NudgePolicy().delivery_hour(turns + [turns[-1]] * 30, now=NOW,
                                       local_timezone=LOCAL, weekend=False) == 10


def test_invalid_conversation_timestamp_and_future_round_are_not_counted():
    turns = [conversation('valid-' + str(i), 10) for i in range(19)]
    turns += [{'id': 'invalid', 'created_at': 'invalid', 'intent': 'ask'},
              {'id': 'naive', 'created_at': '2026-10-06T10:00:00', 'intent': 'ask'},
              {'id': 'future', 'created_at': (NOW + timedelta(days=1)).isoformat(), 'intent': 'ask'}]
    assert NudgePolicy().delivery_hour(turns, now=NOW, local_timezone=LOCAL, weekend=False) == 9
