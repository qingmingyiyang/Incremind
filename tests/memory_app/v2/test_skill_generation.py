import importlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.policies import get, version
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import WorkScope, RecognitionConflict
from core.ai_kernel import validate_turn_request
from core.ai_kernel.turn_kinds import freeze_turn_request
from tests.memory_app.governed_model_fixture import GovernedModel
from tests.memory_app.v2.test_skill_exports import env, method, draft


class Model(GovernedModel):
    def __init__(self):
        self.calls = 0
        self.allowed = True
        self.response = draft()
        self.after = lambda: None

    def public(self):
        result = super().public()
        result['generation']['allow_remote'] = self.allowed
        return result

    def complete(self, messages, *, max_tokens, validate_current=None, **kwargs):
        validate_current()
        self.calls += 1
        self.messages = messages
        self.after()
        return json.dumps(self.response, ensure_ascii=False), {}


def generate(env, model, source, **kwargs):
    module = importlib.import_module('backend.memory_app.v2.skill_generation')
    return module.generate_skill(model, env[2], 'alpha',
        sources=[{'id': source.id, 'revision': source.revision}], **kwargs)


def test_actual_aux_generation_freezes_sources_and_replays_without_wire(env):
    source, model = method(env), Model()
    result = generate(env, model, source)
    assert result['document'] == draft()
    assert result['sources'][0]['id'] == source.id
    store = MemoryTurn.store_for(env[0])
    request = store.get_request(result['turn_id'])
    assert request['desired_outcome'] == 'memory.skill_export'
    assert request['execution_policy']['purpose'] == 'aux'
    assert request['capability_policy']['allowed'] == []
    assert request['policy_versions'] == {
        'scope': version('scope'), 'skill_export': '@1', 'skill_author': '@1'}
    assert validate_turn_request(request) == request
    assert source.content in request['input']['text']
    assert source.content in model.messages[-1]['content']
    assert source.conditions[0] in model.messages[-1]['content']
    assert any(event['type'] == 'model.completed' for event in store.events_after(result['turn_id']))
    assert generate(env, model, source) == result and model.calls == 1
    assert env[0].list('v2_skill_exports') == ()
    assert len(env[0].list('recognitions')) == 1


def test_disabled_generation_creates_no_turn_or_wire_but_manual_remains(env):
    source, model = method(env), Model()
    model.allowed = False
    with pytest.raises(ValueError, match='^skill_generation_disabled$'):
        generate(env, model, source)
    assert model.calls == 0
    assert env[0].list('v2_memory_turn_keys') == ()
    assert not (env[0].database_path.parent / 'ai-turns.sqlite3').exists()
    assert env[2].create('alpha', sources=[{'id': source.id, 'revision': source.revision}],
        document=draft())['reviewed'] is False


@pytest.mark.parametrize('mutation', ['privacy', 'configuration', 'revision'])
def test_post_wire_source_and_configuration_change_rejects_document(env, mutation):
    source, model = method(env), Model()
    def change():
        if mutation == 'privacy':
            set_private_project(env[0], 'alpha', True, 0)
        elif mutation == 'configuration':
            model.allowed = False
        else:
            row = env[0].read('recognitions', source.id)
            with env[0].begin() as tx:
                tx.put('recognitions', source.id, dict(row.payload), expected_revision=row.revision)
                tx.commit()
    model.after = change
    with pytest.raises(ValueError):
        generate(env, model, source)
    assert model.calls == 1
    assert env[0].list('v2_skill_exports') == ()


@pytest.mark.parametrize('number', [0, 2, True])
def test_output_source_numbers_are_strict(env, number):
    source, model = method(env), Model()
    model.response['steps'][0]['sources'] = [number]
    with pytest.raises(ValueError):
        generate(env, model, source)
    assert model.calls >= 1
    assert env[0].list('v2_skill_exports') == ()


def test_wrong_scope_and_pending_sources_never_call_model(env):
    model = Model()
    for source in [method(env, project='beta'), method(env, publish=False)]:
        with pytest.raises(ValueError, match='skill_source_unavailable'):
            generate(env, model, source)
    assert model.calls == 0


def test_author_policy_is_registered_and_preserves_conditions():
    importlib.import_module('backend.memory_app.v2.policies.skill_author')
    policy = get('skill_author', version='@1')
    source = {'number': 1, 'id': 'method-1', 'revision': 1,
        'text': 'Synthetic method', 'conditions': ['只在合成场景'], 'scene': None}
    messages = policy.prepare([source])
    assert messages[0]['role'] == 'system'
    assert '人工' in messages[0]['content']
    assert json.loads(messages[1]['content'])['sources'][0] == source
    assert policy.max_tokens > 0


@pytest.mark.parametrize('private', ['project', 'source'])
def test_private_inputs_refused_before_aux_constructor_even_for_local_model(env, private):
    source, model = method(env), Model()
    if private == 'project':
        set_private_project(env[0], 'alpha', True, 0)
    else:
        SourceEgressService(env[0]).set_policy(WorkScope('local-user', 'alpha'),
            'recognition', source.id, source.revision, 0, [])
    with pytest.raises((ValueError, RecognitionConflict), match='skill_generation_disabled|source egress'):
        generate(env, model, source)
    assert model.calls == 0
    assert not (env[0].database_path.parent / 'ai-turns.sqlite3').exists()


@pytest.mark.parametrize('key', ['', 'a' * 129, '含正文', 'a\nsecret'])
def test_generation_key_is_bounded_ascii(env, key):
    source, model = method(env), Model()
    with pytest.raises(ValueError, match='^invalid_skill_generation_key$'):
        generate(env, model, source, retry_token=key)
    assert model.calls == 0


def validator():
    schema = json.loads((Path(__file__).resolve().parents[3] /
        'core-contracts/ai/turn-request.schema.json').read_text(encoding='utf-8'))
    return Draft202012Validator(schema)


def test_original_memory_turn_id_has_existing_schema_boundary(env):
    source, model = method(env), Model()
    turn = MemoryTurn(env[0], model, kind='memory.overview', project='alpha',
        key='original-overview-schema-control', materials=[{'type': 'recognition',
            'id': source.id, 'revision': source.revision, 'project_id': 'alpha'}],
        validate=lambda: None)
    assert validate_turn_request(turn.request) == turn.request
    assert turn.turn_id.startswith('memory-')
    assert any(list(error.path) == ['turn_id'] for error in validator().iter_errors(turn.request))
    assert model.calls == 0


def test_skill_template_original_core_freeze_schema_valid():
    request = freeze_turn_request('memory.skill_export', turn_id='turn-' + 'a' * 32,
        session_id='synthetic-skill-session', operation_id='op-synthetic-skill-operation',
        idempotency_key='synthetic-skill-key', project_id='alpha',
        created_at='2026-10-06T00:00:00Z', text='Synthetic method',
        privacy={'mode': 'local_only', 'allow_remote': False, 'pii': 'possible',
            'consent_refs': [], 'retention': 'session'})
    request['policy_versions'] = {'scope': version('scope'),
        'skill_export': '@1', 'skill_author': '@1'}
    assert validate_turn_request(request) == request
    validator().validate(request)
    assert request['execution_policy']['purpose'] == 'aux'
    assert request['capability_policy']['allowed'] == []
    assert request['execution_policy']['budget'] == {'max_steps': 4, 'planner_timeout_ms': 120000}


def test_same_key_cannot_reuse_old_project_basis_after_legal_scene_assignment(env):
    source, model = method(env), Model()
    original = generate(env, model, source, scene='小王', retry_token='scene-basis-1')
    assert original['sources'][0]['scene'] is None and model.calls == 1
    assign_scene(env[0], 'recognition', source.id, 'alpha', '小王')
    assert env[0].read('recognitions', source.id).revision == source.revision
    with pytest.raises(RecognitionConflict, match='^skill_generation_basis_changed$'):
        generate(env, model, source, scene='小王', retry_token='scene-basis-1')
    assert model.calls == 1 and env[0].list('v2_skill_exports') == ()
    changed = generate(env, model, source, scene='小王', retry_token='scene-basis-2')
    assert changed['turn_id'] != original['turn_id'] and model.calls == 2
    assert changed['sources'][0]['scene'] == '小王'
    assert json.loads(model.messages[-1]['content'])['sources'][0]['scene'] == '小王'
