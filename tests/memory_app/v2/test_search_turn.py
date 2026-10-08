from types import SimpleNamespace

import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.policies import ACTIVE, override
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionError
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore


class SearchWire:
    def __init__(self):
        self.calls = []
        self.after = lambda: None

    def __call__(self, **request):
        self.calls.append(request)
        self.after()
        return {'choices':[{'finish_reason':'stop', 'message':{
            'content':'当前开放，费用120元。', 'annotations':[{'url_citation':{
                'url':f'https://example.test/current/{index}', 'title':f'官方说明{index}',
                'start_index':0, 'end_index':13}} for index in range(3)]}}],
            'usage':{'prompt_tokens':8, 'completion_tokens':5}}


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    wire = SearchWire()
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=wire)
    return SimpleNamespace(root=tmp_path, records=records, models=models, wire=wire)


def enable(env):
    return env.models.update('search', {'base_url':'https://example.test/v1', 'model':'synthetic-search',
        'api_key':'synthetic-private-value', 'enabled':True, 'allow_remote':True, 'expected_revision':0})


def test_original_model_owner_exposes_search_as_off_without_touching_old_active_values(env):
    before = dict(ACTIVE)
    search = env.models.public()['search']
    assert search['revision'] == 0 and search['allow_remote'] is False and search['enabled'] is False
    assert search['configured'] is False and search['has_api_key'] is False
    assert ACTIVE == before and ACTIVE['rank'] == '@2' and ACTIVE['search'] == '@1'
    assert env.wire.calls == []


def test_search_off_creates_neither_a_wire_nor_an_auxiliary_turn(env):
    from backend.memory_app.v2.search import search_once
    with override(search='@1'):
        assert search_once(env.records, env.models, 'alpha', '最近开放吗', key='original-parent') is None
    assert env.wire.calls == [] and env.records.list('v2_memory_turn_keys') == ()
    assert not (env.root / 'ai-turns.sqlite3').exists()


def test_search_uses_one_real_aux_turn_and_cached_result_with_original_wire_receipts(env):
    from backend.memory_app.v2.search import search_once
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups, PURPOSES
    from backend.memory_app.v2.memory_turn import MemoryTurn
    enable(env)
    with override(search='@1'):
        result = search_once(env.records, env.models, 'alpha', '最近开放吗', key='original-parent')
        replay = search_once(env.records, env.models, 'alpha', '最近开放吗', key='original-parent')
    assert result['turn_id'] == replay['turn_id'] and result['results'] == replay['results']
    assert len(env.wire.calls) == 1 and len(result['results']) == 3
    assert env.wire.calls[0]['web_search_options'] == {'search_context_size':'medium'}
    assert 'memory-search@1' in env.wire.calls[0]['messages'][1]['content']
    store = MemoryTurn.store_for(env.records)
    request = store.get_request(result['turn_id'])
    assert request['desired_outcome'] == 'web.search' and request['execution_policy']['purpose'] == 'aux'
    assert request['policy_versions'] == {'search':'@1'}
    assert request['input']['text'] == '最近开放吗' and request['input']['refs'] == []
    assert request['privacy']['allow_remote'] is True
    assert request['privacy']['material_refs'] == request['privacy']['source_snapshots'] == []
    events = store.events_after(result['turn_id'])
    assert events[-1]['type'] == 'turn.completed'
    assert len([event for event in events if event['type'] == 'model.attempt.dispatched']) == 1
    assert result['usage'] == {'input_tokens':8, 'output_tokens':5, 'total_tokens':13}
    groups = kernel_call_groups(env.root, records=env.records)
    assert len(groups) == 1 and groups[0]['kind'] == 'web.search' and PURPOSES['web.search'] == '搜索'
    assert groups[0]['calls'][0]['cost'] is None
    assert 'synthetic-private-value' not in str(request) + str(events) + str(groups)
    result['validate_current']()


def test_private_project_does_not_create_search_turn_even_when_search_is_enabled(env):
    from backend.memory_app.v2.search import search_once
    enable(env)
    set_private_project(env.records, 'alpha', True, 0)
    with override(search='@1'):
        assert search_once(env.records, env.models, 'alpha', '最近开放吗', key='private-parent') is None
    assert env.wire.calls == [] and env.records.list('v2_memory_turn_keys') == ()


def test_privacy_revoked_by_the_provider_rejects_its_returned_search_evidence(env):
    from backend.memory_app.v2.search import search_once
    enable(env)
    env.wire.after = lambda:set_private_project(env.records, 'alpha', True, 0)
    with override(search='@1'), pytest.raises(RecognitionError):
        search_once(env.records, env.models, 'alpha', '最近开放吗', key='revoked-parent')
    assert len(env.wire.calls) == 1
    assert env.records.list('workspace_items') == ()


def test_same_aux_key_cannot_reinterpret_the_original_question_or_a_changed_configuration(env):
    from backend.memory_app.v2.search import search_once
    enable(env)
    with override(search='@1'):
        result = search_once(env.records, env.models, 'alpha', '最近开放吗', key='original-parent')
        with pytest.raises(RecognitionError):
            search_once(env.records, env.models, 'alpha', '最新费用多少', key='original-parent')
    env.models.update('search', {'model':'changed-model', 'expected_revision':1})
    with pytest.raises(RecognitionError):
        result['validate_current']()
    assert len(env.wire.calls) == 1 and env.records.list('workspace_items') == ()


def test_search_receipt_binds_its_own_settings_revision(env):
    from backend.memory_app.v2.search import search_once
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    enable(env)
    with override(search='@1'):
        result = search_once(env.records, env.models, 'alpha', '目前展览开放吗', key='search-settings-parent')
    groups = kernel_call_groups(env.root, turn_id=result['turn_id'], records=env.records)
    assert groups[0]['calls'][0]['egress']['settings_revision'] == {'search':1}


@pytest.mark.parametrize('change', ['purpose', 'version', 'steps', 'timeout', 'capability', 'context'])
def test_search_template_rejects_scope_and_budget_expansion_in_python_and_schema(change):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from core.ai_kernel import AIKernelContractError, validate_turn_request
    from tests.rebuild.test_product_turn_kinds import request
    value = request('web.search')
    if change == 'purpose':
        value['execution_policy']['purpose'] = 'primary'
    elif change == 'version':
        value['execution_policy']['template_version'] = 2
    elif change == 'steps':
        value['execution_policy']['budget']['max_steps'] = 2
    elif change == 'timeout':
        value['execution_policy']['budget']['planner_timeout_ms'] = 120001
    elif change == 'capability':
        value['capability_policy']['allowed'] = ['memory.recall']
    else:
        value['context_policy']['include_memory'] = True
    with pytest.raises(AIKernelContractError):
        validate_turn_request(value)
    schema = json.loads((Path(__file__).parents[3] / 'core-contracts/ai/turn-request.schema.json').read_text(encoding='utf-8'))
    assert not Draft202012Validator(schema).is_valid(value)
