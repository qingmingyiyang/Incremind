"""Selected auxiliary models are observed through real product Turns and wires."""
import json
import re
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.recognition import RecognitionService, RecognitionConflict, WorkScope
from backend.security.secrets import InMemorySecretStore
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_workbench_ask import assemble, publish, ask
from tests.memory_app.v2.test_route import env as route_env, TEXT


@pytest.fixture
def fast_env(tmp_path):
    import litellm  # Load the existing optional gateway before aux deadlines.
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    calls = []

    def wire(**request):
        calls.append(request)
        text = '\n'.join(message['content'] for message in request['messages'])
        if 'condensed_question' in text:
            output = {'condensed_question': 'alpha beta gamma?'}
        elif '"queries"' in text:
            output = {'queries': ['alpha beta gamma?']}
        elif 'Synthetic memory' in text:
            output = {'value': 'Synthetic result'}
        else:
            numbers = [int(n) for n in re.findall(r'^\[(\d+)\]', text, re.M)]
            output = {'answer': 'Synthetic answer', 'citations': numbers}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}

    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=wire)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'main',
        'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    documents, service = SQLiteDocumentRepository(records), RecognitionService(records)
    application, domains = assemble(tmp_path, records, documents, service, models)
    with TestClient(application) as http:
        yield SimpleNamespace(root=tmp_path, records=records, documents=documents, service=service,
            model=models, calls=calls, app=application, domains=domains, http=http)


def test_http_followup_fast_aux_preserves_primary_and_actual_receipts(fast_env):
    env = fast_env
    env.model.update_model_prices('generation', {'input_per_million': '2', 'output_per_million': '8',
        'cache_read_per_million': '0.04'}, expected_revision=0, expected_configuration_revision=1)
    choose(env.model, 'quick')
    recognition, _ = publish(env)
    first = ask(env, intent='ask')
    assert first.status_code == 200, first.text
    response = ask(env, intent='ask', text='它有哪些原则？', thread_id=first.json()['thread_id'])
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert receipt['trace'][0]['condensed_question'] == 'alpha beta gamma?'
    assert [row['id'] for row in receipt['citations']] == [recognition.id]
    assert [call['model'] for call in env.calls] == ['openai/main', 'openai/quick', 'openai/main']
    store = env.app.state.ai_turn_store
    turns = [row for row in env.records.list('v2_turns') if row.payload['intent'] == 'ask']
    target = next(row for row in turns if row.payload['user_text'] == '它有哪些原则？')
    receipts = [store.get(event['data']['receipt_ref']) for event in store.events_after(target.object_id)
        if event['type'] == 'model.completed']
    assert [(row['model_call_purpose'], row['model_id']) for row in receipts] == [('aux', 'quick'), ('primary', 'main')]
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    calls = next(group['calls'] for group in kernel_call_groups(env.root, turn_id=target.object_id,
        records=env.records) if group['turn_id'] == target.object_id)
    assert [(call['model_id'], call['egress']['model']) for call in calls] == [('quick', 'quick'), ('main', 'main')]
    prices = env.records.list('v2_model_wire_prices')
    assert len(prices) == 3
    assert [row.payload['rates'] for row in prices if row.payload['model_id'] == 'quick'] == [None]
    assert all(row.payload['rates']['input_per_million'] == '2' for row in prices if row.payload['model_id'] == 'main')


def test_real_route_aux_wire_and_replay_keep_selected_model(route_env):
    service, records, models, calls, output = route_env
    choose(models, 'quick')
    result = service.route(TEXT, project_id='alpha', request_key='fast')
    assert result.mode == 'model'
    assert calls[0]['model'] == 'openai/quick'
    event = next(event for event in service.store.events_after(result.turn_id) if event['type'] == 'model.completed')
    assert service.store.get(event['data']['receipt_ref'])['model_id'] == 'quick'
    choose(models, 'other', 1)
    assert service.route(TEXT, project_id='alpha', request_key='fast') == result
    assert len(calls) == 1


class MemoryOutput(BaseModel):
    value: str


def test_real_memory_turn_uses_fast_route_and_retains_source_authority(fast_env):
    env = fast_env
    choose(env.model, 'quick')
    scope = WorkScope('local-user', 'alpha')
    source = env.service.stage_experience(scope=scope, content='Synthetic memory')
    memory = MemoryTurn(env.records, env.model, kind='memory.overview', project='alpha', key='fast-memory',
        materials=[{'type': 'experience', 'id': source, 'project_id': 'alpha', 'revision': 1}], validate=lambda: None)
    result, metadata = memory.generate([{'role': 'user', 'content': 'Synthetic memory'}],
        response_model=MemoryOutput, max_tokens=200)
    assert result.value == 'Synthetic result'
    assert metadata['model'] == 'quick'
    assert env.calls[0]['model'] == 'openai/quick'
    event = next(event for event in memory.store.events_after(memory.turn_id) if event['type'] == 'model.completed')
    assert memory.store.get(event['data']['receipt_ref'])['model_id'] == 'quick'


def test_two_real_http_questions_keep_complete_profile_and_system_prefix(fast_env):
    env = fast_env
    profile, _ = publish(env, text='My preferred tools are notebooks.', project='me')
    recognition, _ = publish(env)
    responses = [ask(env, intent='ask', text=question) for question in
                 ('alpha beta gamma?', 'alpha beta gamma 怎样应用？')]
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()['turn']['receipt']['ask']['layers']['persona'] == 1 for response in responses)
    assert [call['model'] for call in env.calls] == ['openai/main', 'openai/main']
    def prefix(messages):
        result = []
        for message in messages:
            before, marker, _ = message['content'].partition('资料：')
            result.append({**message, 'content': before})
            if marker:
                break
        else:
            raise AssertionError('actual wire has no material boundary')
        return json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    first, second = (prefix(call['messages']) for call in env.calls)
    assert first == second
    assert 'My preferred tools are notebooks.'.encode() in first
    assert '只根据用户提供的资料回答'.encode() in first
    assert env.calls[0]['messages'] != env.calls[1]['messages']
    assert all(response.json()['turn']['receipt']['ask']['citations'][0]['id'] == recognition.id for response in responses)


def test_organize_aux_uses_fast_model_and_completed_checkpoint_replays(fast_env):
    from backend.memory_app.v2.organize_turns import OrganizeTurns
    env = fast_env
    choose(env.model, 'quick')
    item = asyncio.run(env.domains.intake.add_text({'project_id': 'alpha', 'text': 'Synthetic organization'}))
    kwargs = dict(root=env.root, records=env.records, models=env.model, item_id=item['id'],
        project_id='alpha', source='Synthetic organization', validate_current=lambda: None)
    call = dict(max_tokens=100, validate_current=lambda: None)
    messages = [{'role': 'user', 'content': 'Synthetic organization'}]
    first = OrganizeTurns(**kwargs).complete(messages, **call)
    assert first[1]['model'] == 'quick' and env.calls[0]['model'] == 'openai/quick'
    choose(env.model, 'other', 1)
    assert OrganizeTurns(**kwargs).complete(messages, **call) == first
    assert len(env.calls) == 1


def test_memory_frozen_default_does_not_adopt_later_fast_setting(fast_env):
    env = fast_env
    scope = WorkScope('local-user', 'alpha')
    source = env.service.stage_experience(scope=scope, content='Synthetic memory')
    memory = MemoryTurn(env.records, env.model, kind='memory.overview', project='alpha', key='default-memory',
        materials=[{'type': 'experience', 'id': source, 'project_id': 'alpha', 'revision': 1}], validate=lambda: None)
    choose(env.model, 'quick')
    _, metadata = memory.generate([{'role': 'user', 'content': 'Synthetic memory'}],
        response_model=MemoryOutput, max_tokens=200)
    assert metadata['model'] == 'main'
    assert [call['model'] for call in env.calls] == ['openai/main']


def test_changed_staged_binding_is_not_reinterpreted_as_a_new_choice(fast_env):
    from backend.memory_app.kernel.aux_routing import CHOICES
    env = fast_env
    source = env.service.stage_experience(scope=WorkScope('local-user', 'alpha'), content='Synthetic memory')
    memory = MemoryTurn(env.records, env.model, kind='memory.overview', project='alpha', key='changed-binding',
        materials=[{'type': 'experience', 'id': source, 'project_id': 'alpha', 'revision': 1}], validate=lambda: None)
    original = env.records.read(CHOICES, memory.turn_id)
    with env.records.begin() as tx:
        tx.put(CHOICES, memory.turn_id, original.payload, expected_revision=original.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match='memory_turn_result_unavailable'):
        memory.generate([{'role': 'user', 'content': 'Synthetic memory'}], response_model=MemoryOutput, max_tokens=200)
    assert env.calls == []
