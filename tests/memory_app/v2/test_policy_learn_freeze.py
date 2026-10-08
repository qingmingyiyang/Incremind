"""Real consolidation entry decisions match their generated child Turns."""
import pytest

from backend.memory_app.v2.policies import ACTIVE, get, override, register, version
from backend.memory_app.v2.policies.pipelines import interfaces_for_turn
from tests.memory_app.v2.kernel_receipts import requests
from tests.memory_app.v2.test_consolidation import env, documents, next_day_job, PatternModel


def test_real_pattern_freeze_keeps_entry_version_after_active_switch(env, monkeypatch):
    documents(env)
    original = get('consolidate', version='@1')
    seen = []
    def first(*args, **kwargs):
        seen.append(version('consolidate'))
        monkeypatch.setitem(ACTIVE, 'consolidate', '@9822')
        return original(*args, **kwargs)
    register('consolidate', '@9821')(first)
    register('consolidate', '@9822')(original)
    monkeypatch.setitem(ACTIVE, 'consolidate', '@9821')
    model = PatternModel()
    result = next_day_job(env, model).run()
    assert result['new_suggestions'] == 1 and model.calls == 2
    values = requests(env.records)
    assert 'memory.consolidate' in {value['desired_outcome'] for value in values}
    for value in values:
        assert value['policy_versions']['consolidate'] == '@9821'
        assert set(value['policy_versions']) == set(interfaces_for_turn(value['desired_outcome']))
    assert seen == ['@9821'] and version('consolidate') == '@9822'
    assert env.records.list('recognitions') == ()


def test_consolidation_entry_exception_restores_context(env):
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic consolidation failure')
    register('consolidate', '@9823')(fail)
    with override(consolidate='@9823'):
        with pytest.raises(RuntimeError, match='synthetic consolidation failure'):
            next_day_job(env, PatternModel()).run()
        assert version('consolidate') == '@9823'
