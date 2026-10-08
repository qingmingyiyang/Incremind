"""只替换外部模型传输，沿原 HTTP、领域和内核验证搜索。"""
from copy import deepcopy
import json
import re
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.policies import ACTIVE, override
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionService
from backend.security.secrets import InMemorySecretStore
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_workbench_ask import assemble, publish


class Wire:
    def __init__(self):
        self.calls = []
        self.search_after = lambda: None
        self.answer_after = lambda: None
        self.numbers = [2]
        self.search_usage = True
        self.rewrite_usage = True
        self.cache_usage = False

    def __call__(self, **request):
        self.calls.append(request)
        if 'web_search_options' in request:
            self.search_after()
            text = '当前春季展览开放，费用120元。'
            message = {'content':text, 'annotations':[{'url_citation':{
                'url':f'https://example.test/official/{i}', 'title':f'官网{i}',
                'start_index':0, 'end_index':len(text)}} for i in range(3)]}
        elif '问法' in request['messages'][0]['content']:
            message = {'content':json.dumps({'queries':[]})}
        else:
            self.answer_after()
            available = {int(n) for n in re.findall(r'^\[(\d+)\]', request['messages'][-1]['content'], re.MULTILINE)}
            message = {'content':json.dumps({'answer':'当前展览开放，费用120元。',
                'citations':[n for n in self.numbers if n in available]})}
        response = {'choices':[{'finish_reason':'stop', 'message':message}]}
        unknown = ('web_search_options' in request and not self.search_usage or
                   '问法' in request['messages'][0]['content'] and not self.rewrite_usage)
        if not unknown:
            response['usage'] = {'prompt_tokens':8, 'completion_tokens':5}
            if self.cache_usage:
                response['usage']['prompt_tokens_details'] = {'cached_tokens':0}
        return response


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    documents, service, wire = SQLiteDocumentRepository(records), RecognitionService(records), Wire()
    model = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=wire)
    model.update('generation', {'base_url':'https://example.test/v1', 'model':'answer',
        'api_key':'synthetic-generation-private', 'allow_remote':True, 'expected_revision':0})
    app, domains = assemble(tmp_path, records, documents, service, model)
    from types import SimpleNamespace
    with TestClient(app) as http:
        yield SimpleNamespace(root=tmp_path, records=records, documents=documents, service=service,
                              wire=wire, model=model, http=http, domains=domains)


def enabled(env):
    env.model.update('search', {'base_url':'https://example.test/v1', 'model':'search',
        'api_key':'synthetic-search-private', 'enabled':True, 'allow_remote':True, 'expected_revision':0})


def ask(env, text='最近春季展览开放吗', *, key=None):
    return env.http.post('/api/v2/workbench/turns', json={'intent':'ask', 'project_id':'alpha', 'text':text},
                         headers={'Idempotency-Key':key} if key else {})


def test_real_ask_cites_a_url_and_stores_only_that_result_with_durable_replay(env):
    enabled(env)
    with override(search='@1'):
        response = ask(env, key='search-original')
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved['turn']['receipt']['ask']
    assert receipt['no_match'] is False
    assert len(receipt['citations']) == 1
    citation = receipt['citations'][0]
    assert citation['url'] == 'https://example.test/official/1' and citation['n'] == 2
    originals = env.records.list('workspace_items')
    assert len(originals) == 1 and originals[0].object_id == citation['id']
    assert originals[0].payload['url'] == citation['url']
    assert originals[0].payload['source_text'] == citation['quote']
    assert originals[0].payload['status'] == 'staged' and '来自搜索' in originals[0].payload['title']
    assert env.records.list('recognitions') == () and env.records.list('documents') == ()
    assert len([call for call in env.wire.calls if 'web_search_options' in call]) == 1
    search = receipt['search']
    assert search['purpose'] == '搜索' and search['policy_version'] == '@1' and search['used'] == 1
    assert search['model_usage'] == {'input_tokens':8, 'output_tokens':5, 'total_tokens':13}
    assert search['model_cost'] is None
    store = env.http.app.state.ai_turn_store
    assert store.get_request(saved['turn']['id'])['policy_versions']['search'] == '@1'
    primary = store.get_immutable_payload(saved['turn']['id'], 'answer-model-input-answer')[1]
    assert '[2]' in primary['messages'][-1]['content'] and citation['url'] in primary['messages'][-1]['content']
    before_calls, before_originals = len(env.wire.calls), deepcopy(originals)
    for _ in range(2):
        assert env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha").json()['turns'][0] == saved['turn']
    assert ask(env, key='search-original').json() == saved
    assert env.records.list('workspace_items') == before_originals and len(env.wire.calls) == before_calls
    assert 'synthetic-search-private' not in str(receipt) + str(store.get_request(saved['turn']['id']))


def test_off_search_and_enough_original_evidence_issue_zero_search_wires(env):
    with override(search='@1'):
        off = ask(env)
    assert off.status_code == 200 and off.json()['turn']['receipt']['ask']['no_match'] is True
    assert not any('web_search_options' in call for call in env.wire.calls)
    enabled(env)
    publish(env, text='最近 春季 展览 开放 当前 展览 费用120元')
    env.wire.numbers = [1]
    with override(search='@1'):
        response = ask(env, '最近 春季 展览 开放')
    assert response.status_code == 200, response.text
    assert response.json()['turn']['receipt']['ask']['no_match'] is False
    assert response.json()['turn']['receipt']['ask']['trace'][-1]['stopped'] is True
    assert not any('web_search_options' in call for call in env.wire.calls)


@pytest.mark.parametrize('when', ['search', 'answer'])
def test_revocation_blocks_search_evidence_publication_and_original_intake(env, when):
    enabled(env)
    callback = lambda:set_private_project(env.records, 'alpha', True, 0)
    if when == 'search':
        env.wire.search_after = callback
    else:
        env.wire.answer_after = callback
    with override(search='@1'):
        response = ask(env)
    assert response.status_code == 409, response.text
    assert env.records.list('workspace_items') == ()
    assert len([call for call in env.wire.calls if 'web_search_options' in call]) == 1
    assert env.records.list('v2_turns') == ()


def test_private_project_does_not_dispatch_the_enabled_search_service(env):
    enabled(env)
    set_private_project(env.records, 'alpha', True, 0)
    with override(search='@1'):
        response = ask(env)
    assert response.status_code == 409, response.text
    assert env.wire.calls == [] and env.records.list('workspace_items') == ()
    assert env.records.list('v2_memory_turn_keys') == ()


def test_original_settings_read_model_exposes_search_prices_and_off_default(env):
    response = env.http.get('/api/v2/settings')
    assert response.status_code == 200, response.text
    search = response.json()['model']['search']
    assert search['revision'] == 0 and search['enabled'] is False and search['allow_remote'] is False
    assert search['pricing']['revision'] == 0 and search['pricing']['rates'] is None
    assert env.wire.calls == []


def test_expired_confirmed_profile_can_trigger_search_without_sending_its_body(env, monkeypatch):
    from tests.memory_app.v2.test_rank_freshness_query import publish_period
    from backend.memory_app.v2.profile import confirmed_profile
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda:'2028-02-01T00:00:00+08:00')
    enabled(env)
    insight = publish_period(env, ['2025年'], project='me', content='本人私下偏好喜欢咖喱且忌辣')
    original = env.records.read('recognitions', insight.id)
    stable = confirmed_profile(env.records, env.service)
    with override(search='@1', rank='@2'):
        response = ask(env, '推荐吃什么')
    searches = [call for call in env.wire.calls if 'web_search_options' in call]
    assert len(searches) == 1
    assert response.status_code == 200, response.text
    assert '推荐吃什么' in searches[0]['messages'][-1]['content']
    assert insight.content not in str(searches[0]) and '忌辣' not in str(searches[0])
    primary = env.http.app.state.ai_turn_store.get_immutable_payload(
        response.json()['turn']['id'], 'answer-model-input-answer')[1]
    assert primary['messages'][0]['content'] == stable['text']
    assert env.records.read('recognitions', insight.id) == original
    assert confirmed_profile(env.records, env.service)['text'] == stable['text']
    persona = next(entry for entry in response.json()['turn']['receipt']['ask']['context']['entries'] if entry.get('persona'))
    assert persona['id'] == insight.id and persona['stale'] is True
    assert 'n' not in persona and 'score' not in persona


def test_profile_authority_revoked_during_search_prevents_the_primary_and_original_intake(env, monkeypatch):
    from tests.memory_app.v2.test_rank_freshness_query import publish_period
    monkeypatch.setattr('backend.memory_app.v2.insight_validity.now', lambda:'2028-02-01T00:00:00+08:00')
    enabled(env)
    publish_period(env, ['2025年'], project='me', content='本人私下偏好喜欢咖喱且忌辣')
    env.wire.search_after = lambda:set_private_project(env.records, 'me', True, 0)
    with override(search='@1', rank='@2'):
        response = ask(env, '推荐吃什么')
    assert response.status_code == 409, response.text
    assert len([call for call in env.wire.calls if 'web_search_options' in call]) == 1
    assert not any('只根据用户提供的资料回答' in call['messages'][0]['content'] for call in env.wire.calls)
    assert env.records.list('workspace_items') == () and env.records.list('v2_turns') == ()


def test_restored_sufficient_bookshelf_evidence_does_not_search(env):
    from tests.memory_app.v2.test_bookshelf import forgotten
    enabled(env)
    insight, _ = publish(env, text='最近 春季 展览 开放 当前 展览 费用120元')
    forgotten(env, insight)
    env.wire.numbers = [1]
    with override(search='@1'):
        response = ask(env, '最近 春季 展览 开放')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert receipt['citations'][0]['id'] == insight.id and receipt['citations'][0]['bookshelf'] is True
    assert not any('web_search_options' in call for call in env.wire.calls)
    assert 'search' not in receipt and env.records.list('workspace_items') == ()


@pytest.mark.parametrize('unknown', ['search', 'rewrite'])
def test_unknown_search_or_rewrite_usage_remains_partial_in_the_frozen_and_public_receipts(env, unknown):
    enabled(env)
    if unknown == 'search':
        env.wire.search_usage = False
    else:
        env.wire.rewrite_usage = False
    with override(search='@1'):
        response = ask(env)
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved['turn']['receipt']['ask']
    frozen = env.http.app.state.ai_turn_store.get_immutable_payload(saved['turn']['id'], 'product-answer-result-v2')[1]['receipt']['ask']
    assert frozen['model_usage']['observed_only'] is True
    assert receipt['model_usage']['observed_only'] is True
    if unknown == 'search':
        assert receipt['search']['model_usage'] == {}
    else:
        assert receipt['search']['model_usage'] == {'input_tokens':8, 'output_tokens':5, 'total_tokens':13}
        assert receipt['trace'][0]['rewrite_receipt_ids']
    assert receipt['search']['model_cost'] is None


def test_public_question_usage_includes_the_linked_actual_search_attempt(env):
    enabled(env)
    with override(search='@1'):
        response = ask(env)
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert receipt['model_usage'] == {'input_tokens':8 * len(env.wire.calls),
        'output_tokens':5 * len(env.wire.calls), 'total_tokens':13 * len(env.wire.calls)}
    saved = response.json()
    from backend.memory_app.v2.search import search_once
    with override(search='@1'):
        unrelated = search_once(env.records, env.model, 'alpha', '目前其他展览开放吗', key='unrelated-parent')
    assert unrelated['turn_id'] != receipt['search']['turn_id']
    before_calls = len(env.wire.calls)
    for _ in range(2):
        replay = env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha")
        assert replay.json()['turns'][0] == saved['turn']
    assert len(env.wire.calls) == before_calls


def test_search_and_parent_costs_use_the_frozen_original_price_ledger(env):
    enabled(env)
    env.wire.cache_usage = True
    rates = {'input_per_million':'2', 'output_per_million':'3', 'cache_read_per_million':'0.1'}
    for purpose in ('generation', 'search'):
        env.model.update_model_prices(purpose, rates, expected_revision=0, expected_configuration_revision=1)
    with override(search='@1'):
        response = ask(env, key='priced-search')
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved['turn']['receipt']['ask']
    assert receipt['search']['model_cost'] == {'currency':'CNY', 'amount':'0.000031'}
    assert receipt['model_cost'] == {'currency':'CNY', 'amount':'0.000093'}
    env.model.update_model_prices('search', {**rates, 'input_per_million':'99'},
                                  expected_revision=1, expected_configuration_revision=1)
    before_calls = len(env.wire.calls)
    assert ask(env, key='priced-search').json() == saved
    assert env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha").json()['turns'][0] == saved['turn']
    assert len(env.wire.calls) == before_calls


def test_second_cited_original_sql_failure_rolls_back_the_batch_and_failed_replay_sends_no_wire(env):
    enabled(env)
    env.wire.numbers = [1, 2]
    owners = {collection:env.records.list(collection) for collection in
              ('v2_original_sections', 'source_retrieval_index', 'original_text_retrieval_index')}
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("""CREATE TRIGGER search_second_original_failure BEFORE INSERT ON crp_structured_records
            WHEN NEW.collection='workspace_items' AND
                 (SELECT count(*) FROM crp_structured_records WHERE collection='workspace_items')=1
            BEGIN SELECT RAISE(ABORT,'synthetic_search_second_original'); END""")
    with override(search='@1'), pytest.raises(sqlite3.IntegrityError, match='synthetic_search_second_original'):
        ask(env, key='search-sql-failure')
    assert len([call for call in env.wire.calls if 'web_search_options' in call]) == 1
    failed = env.records.read('v2_turn_requests', 'search-sql-failure')
    assert failed.payload['state'] == 'failed' and failed.payload['error'] == 'answer_generation_failed'
    assert env.records.list('v2_turns') == ()
    calls, originals = len(env.wire.calls), env.records.list('workspace_items')
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute('DROP TRIGGER search_second_original_failure')
    with override(search='@1'):
        replay = ask(env, key='search-sql-failure')
    assert replay.status_code == 502 and replay.json()['detail'] == 'answer_generation_failed'
    assert len(env.wire.calls) == calls and env.records.list('workspace_items') == originals
    assert originals == ()
    assert {collection:env.records.list(collection) for collection in owners} == owners
