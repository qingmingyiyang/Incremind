"""Insight prompt and post-runtime parsing use one durable version."""
import pytest

from backend.memory_app.v2.policies import ACTIVE, get, override, register
from backend.memory_app.v2.policies.types import ModelPolicy, ExtractOutput
from tests.memory_app.v2.test_insight_generation import env, generate

SEEN = []


@pytest.fixture(scope='module', autouse=True)
def versions():
    original = get('extract', version='@1')
    for selected in ('@9601', '@9602'):
        def prepare(value, _selected=selected):
            SEEN.append(('prepare', _selected))
            return original.prepare(value)

        def decide(value, _selected=selected):
            SEEN.append(('decide', _selected))
            result = original.decide(value)
            suffix = '旧版' if _selected == '@9601' else '新版'
            return ExtractOutput(tuple((text + suffix, conditions) for text, conditions in result.rows),
                result.errors, result.valid)
        register('extract', selected)(ModelPolicy(prepare, decide))


def prepare_frozen(env):
    from backend.memory_app.document_recognition import ensure_document_experience
    from backend.memory_app.generation_sources import generation_source_guard
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.memory_turn import MemoryTurn
    experience, revision = ensure_document_experience(env.documents, env.service, 'alpha', env.doc)
    value = env.service.read_candidate_experiences(scope=env.scope, experience_ids=[experience])[0]
    validate = generation_source_guard(SourceEgressService(env.records), env.model, env.scope,
        [{'type': 'experience', 'id': experience, 'revision': value.revision}])
    return MemoryTurn(env.records, env.model, kind='memory.propose_insights', project='alpha',
        key=f'candidate-v2-{env.doc}-r{revision}-',
        materials=[{'type': 'experience', 'id': experience, 'revision': value.revision, 'project_id': 'alpha'}],
        validate=validate)


def test_real_insight_replay_pins_prepare_and_decide_outside_runtime(env, monkeypatch):
    SEEN.clear()
    with override(extract='@9601'):
        turn = prepare_frozen(env)
    assert turn.request['policy_versions']['extract'] == '@9601'
    monkeypatch.setitem(ACTIVE, 'extract', '@9602')
    result = generate(env)
    assert [row['text'] for row in result] == ['短认识一旧版', '短认识二旧版']
    assert SEEN == [('prepare', '@9601'), ('decide', '@9601')]
    assert turn.store.get_request(turn.turn_id)['policy_versions']['extract'] == '@9601'
    assert len(env.records.list('recognitions')) == 0
    assert generate(env) == result
    assert env.model.calls == 1


def test_historical_insight_turn_without_map_uses_original_parser(env, monkeypatch):
    SEEN.clear()
    turn = prepare_frozen(env)
    row = env.records.read('v2_memory_turn_keys', turn.turn_id)
    payload = dict(row.payload)
    payload['request'] = {key: value for key, value in turn.request.items() if key != 'policy_versions'}
    with env.records.begin() as tx:
        tx.put('v2_memory_turn_keys', turn.turn_id, payload, expected_revision=row.revision)
        tx.commit()
    monkeypatch.setitem(ACTIVE, 'extract', '@9602')
    result = generate(env)
    assert [row['text'] for row in result] == ['短认识一', '短认识二']
    assert SEEN == []
    assert 'policy_versions' not in turn.store.get_request(turn.turn_id)
