"""A damaged recorded policy must not borrow the current default on save."""
import pytest

from tests.memory_app.v2.test_outcome_corrections import edit_env, _save, _event


@pytest.mark.parametrize('policy', ['missing', None, False, '@999999'])
def test_edit_with_unknown_recorded_policy_preserves_fact_and_original_save(edit_env, policy):
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    event = _event(edit_env)
    payload = dict(event.payload)
    if policy == 'missing':
        del payload['policy_version']
    else:
        payload['policy_version'] = policy
    with edit_env[1].begin() as tx:
        damaged = tx.put('v2_outcome_corrections', event.object_id, payload,
                         expected_revision=event.revision)
        tx.commit()
    edit_env[4].advance(60)
    response = _save(edit_env, '最终做法\n\n保留的段落', 2)
    assert response.status_code == 200
    assert edit_env[2].read(edit_env[3])['revision'] == 3
    assert edit_env[2].markdown(edit_env[3], revision=3) == '最终做法\n\n保留的段落'
    assert edit_env[1].list('v2_outcome_corrections') == (damaged,)
