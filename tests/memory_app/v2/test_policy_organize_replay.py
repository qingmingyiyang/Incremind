"""Organization dispatch uses the saved step's trusted frozen version."""
import pytest

from backend.memory_app.v2.organize_turns import OrganizeTurns
from backend.memory_app.model_config import ModelConfigurationError
from backend.memory_app.v2.policies import ACTIVE, get, override, register
from tests.memory_app.test_workspace import client, Model

SEEN = []


@pytest.fixture(scope='module', autouse=True)
def versions():
    for selected in ('@9801', '@9802'):
        def organize(entrypoint, *args, _selected=selected, **kwargs):
            SEEN.append(_selected)
            return get('organize', version='@1')(entrypoint, *args, **kwargs)
        register('organize', selected)(organize)


def setup(tmp_path):
    http, records = client(tmp_path)
    item = http.post('/api/workspace/v1/items/text', json={'project_id': 'alpha', 'text': '原文证据'}).json()
    calls = []

    class Provider(Model):
        def complete(self, messages, **options):
            calls.append(messages)
            return super().complete(messages, **options)

    return records, calls, dict(root=tmp_path, records=records, models=Provider(), item_id=item['id'],
        project_id='alpha', source='原文证据', validate_current=lambda: None)


def test_real_saved_step_uses_frozen_entry_and_cached_output_after_version_switch(tmp_path, monkeypatch):
    records, calls, kwargs = setup(tmp_path)
    messages = [{'role': 'user', 'content': '原文证据'}]
    SEEN.clear()
    with override(organize='@9801'):
        first = OrganizeTurns(**kwargs).complete(messages, max_tokens=10, validate_current=lambda: None)
    step = records.list('workspace_organize_steps')[0]
    store = OrganizeTurns(**kwargs).store
    frozen = store.get_request(step.payload['turn_id'])
    assert frozen['policy_versions']['organize'] == '@9801'
    monkeypatch.setitem(ACTIVE, 'organize', '@9802')
    with override(organize='@9802'):
        second = OrganizeTurns(**kwargs).complete(messages, max_tokens=10, validate_current=lambda: None)
    assert second == first
    assert SEEN == ['@9801', '@9801']
    assert len(calls) == 1
    assert store.get_request(step.payload['turn_id']) == frozen


def test_new_organize_entry_and_trace_do_not_drift_when_active_changes_inside_entry(tmp_path, monkeypatch):
    records, calls, kwargs = setup(tmp_path)

    @register('organize', '@9803')
    def change(entrypoint, *args, **options):
        monkeypatch.setitem(ACTIVE, 'organize', '@9802')
        return get('organize', version='@1')(entrypoint, *args, **options)

    monkeypatch.setitem(ACTIVE, 'organize', '@9803')
    OrganizeTurns(**kwargs).complete([{'role': 'user', 'content': '原文证据'}], max_tokens=10, validate_current=lambda: None)
    step = records.list('workspace_organize_steps')[0]
    frozen = OrganizeTurns(**kwargs).store.get_request(step.payload['turn_id'])
    assert frozen['policy_versions']['organize'] == '@9803'
    assert ACTIVE['organize'] == '@9802'
    assert len(calls) == 1


def test_legacy_saved_step_without_map_uses_original_entry(tmp_path, monkeypatch):
    from backend.memory_app.v2 import organize_turns
    original_freeze = organize_turns.freeze_product_turn

    def historical_freeze(*args, **kwargs):
        return {key: value for key, value in original_freeze(*args, **kwargs).items() if key != 'policy_versions'}

    monkeypatch.setattr(organize_turns, 'freeze_product_turn', historical_freeze)
    records, calls, kwargs = setup(tmp_path)
    messages = [{'role': 'user', 'content': '原文证据'}]
    first = OrganizeTurns(**kwargs).complete(messages, max_tokens=10, validate_current=lambda: None)
    step = records.list('workspace_organize_steps')[0]
    store = OrganizeTurns(**kwargs).store
    frozen = store.get_request(step.payload['turn_id'])
    assert 'policy_versions' not in frozen
    SEEN.clear()
    monkeypatch.setitem(ACTIVE, 'organize', '@9802')
    with override(organize='@9802'):
        second = OrganizeTurns(**kwargs).complete(messages, max_tokens=10, validate_current=lambda: None)
    assert second == first
    assert SEEN == []
    assert len(calls) == 1
    assert store.get_request(step.payload['turn_id']) == frozen


@pytest.mark.parametrize('field', ['source', 'revision', 'inputs'])
def test_unmatched_saved_step_does_not_borrow_frozen_entry(tmp_path, field):
    records, calls, kwargs = setup(tmp_path)
    messages = [{'role': 'user', 'content': '原文证据'}]
    with override(organize='@9801'):
        first = OrganizeTurns(**kwargs).complete(messages, max_tokens=10, validate_current=lambda: None)
    if field == 'source':
        kwargs['source'] = '另一份原文'
    elif field == 'revision':
        row = records.read('workspace_items', kwargs['item_id'])
        with records.begin() as tx:
            tx.put('workspace_items', row.object_id, dict(row.payload), expected_revision=row.revision)
            tx.commit()
    else:
        messages = [{'role': 'user', 'content': '另一份输入'}]
    SEEN.clear()
    with override(organize='@9802'):
        if field == 'revision':
            # The old cache permits a state-only revision; do not change that behavior.
            assert OrganizeTurns(**kwargs).complete(messages, max_tokens=10,
                validate_current=lambda: None) == first
        else:
            with pytest.raises(ModelConfigurationError, match='remote_processing_target_changed'):
                OrganizeTurns(**kwargs).complete(messages, max_tokens=10, validate_current=lambda: None)
    assert SEEN == ['@9802']
    assert len(calls) == 1


def test_rejected_output_retry_is_a_new_turn_with_current_selection(tmp_path):
    records, calls, kwargs = setup(tmp_path)

    def reject(output):
        raise ValueError('synthetic schema rejection')

    messages = [{'role': 'user', 'content': '原文证据'}]
    with override(organize='@9801'):
        OrganizeTurns(**kwargs).complete(messages, max_tokens=10, validate_current=lambda: None,
            validate_output=reject)
    step = records.list('workspace_organize_steps')[0]
    old_turn_id = step.payload['turn_id']
    assert step.payload['rejected']
    SEEN.clear()
    with override(organize='@9802'):
        OrganizeTurns(**kwargs).complete(messages, max_tokens=10, validate_current=lambda: None)
    step = records.list('workspace_organize_steps')[0]
    store = OrganizeTurns(**kwargs).store
    assert step.payload['turn_id'] != old_turn_id
    assert store.get_request(step.payload['turn_id'])['policy_versions']['organize'] == '@9802'
    assert SEEN == ['@9802']
    assert len(calls) == 2
