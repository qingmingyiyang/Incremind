"""A durable route selects its frozen entry before any fast-path decisions."""
import pytest

from backend.memory_app.v2.policies import ACTIVE, get, override, register
from backend.memory_app.v2.route import RouteService
from tests.memory_app.v2.test_route import env, TEXT

SEEN = []


@pytest.fixture(scope='module', autouse=True)
def versions():
    for selected in ('@9701', '@9702'):
        def route(entrypoint, *args, _selected=selected, **kwargs):
            SEEN.append(_selected)
            return get('route', version='@1')(entrypoint, *args, **kwargs)
        register('route', selected)(route)


def test_existing_route_pins_entry_before_cached_replay_after_active_and_override_change(env, monkeypatch):
    service, records, models, calls, output = env
    SEEN.clear()
    with override(route='@9701'):
        first = service.route(TEXT, project_id='alpha', request_key='frozen-route')
    assert first.mode == 'model'
    frozen = service.store.get_request(first.turn_id)
    assert frozen['policy_versions']['route'] == '@9701'
    monkeypatch.setitem(ACTIVE, 'route', '@9702')
    with override(route='@9702'):
        second = RouteService(records, models).route(TEXT, project_id='alpha', request_key='frozen-route')
    assert second == first
    assert SEEN == ['@9701', '@9701']
    assert len(calls) == 1
    assert service.store.get_request(first.turn_id) == frozen
    assert len([event for event in service.store.events_after(first.turn_id)
        if event['type'] == 'model.requested']) == 1


def test_legacy_index_without_map_selects_original_entry_before_runtime(env, monkeypatch):
    service, records, models, calls, output = env
    first = service.route(TEXT, project_id='alpha', request_key='legacy-route')
    old = records.list('v2_route_turn_keys')[0]
    payload = dict(old.payload)
    payload['request'] = {key: value for key, value in payload['request'].items() if key != 'policy_versions'}
    with records.begin() as tx:
        tx.put('v2_route_turn_keys', old.object_id, payload, expected_revision=old.revision)
        tx.commit()
    SEEN.clear()
    monkeypatch.setitem(ACTIVE, 'route', '@9702')
    with override(route='@9702'):
        second = RouteService(records, models).route(TEXT, project_id='alpha', request_key='legacy-route')
    assert second == first
    assert SEEN == []
    assert len(calls) == 1


def test_new_entry_and_frozen_trace_stay_consistent_if_active_changes_during_dispatch(env, monkeypatch):
    service, records, models, calls, output = env

    @register('route', '@9703')
    def change_active(entrypoint, *args, **kwargs):
        monkeypatch.setitem(ACTIVE, 'route', '@9702')
        return get('route', version='@1')(entrypoint, *args, **kwargs)

    monkeypatch.setitem(ACTIVE, 'route', '@9703')
    result = service.route(TEXT, project_id='alpha', request_key='active-changes')
    assert result.mode == 'model'
    assert ACTIVE['route'] == '@9702'
    assert service.store.get_request(result.turn_id)['policy_versions']['route'] == '@9703'
    assert len(calls) == 1


@pytest.mark.parametrize('field', ['identity', 'inputs'])
def test_mismatched_index_does_not_borrow_another_submissions_frozen_entry(env, field):
    service, records, models, calls, output = env
    with override(route='@9701'):
        first = service.route(TEXT, project_id='alpha', request_key='mismatched')
    assert first.mode == 'model'
    old = records.list('v2_route_turn_keys')[0]
    payload = dict(old.payload)
    payload[field] = {**payload[field], 'project_id': 'another-project'}
    with records.begin() as tx:
        tx.put('v2_route_turn_keys', old.object_id, payload, expected_revision=old.revision)
        tx.commit()
    SEEN.clear()
    with override(route='@9702'), pytest.raises(ValueError, match='route_identity_conflict'):
        RouteService(records, models).route(TEXT, project_id='alpha', request_key='mismatched')
    assert SEEN == ['@9702']
    assert len(calls) == 1
