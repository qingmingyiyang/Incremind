"""沿安全 smallbase 原装配复用 Q65 原负载，不执行 legacy FULL 工厂。"""
from copy import deepcopy
import json
import re
from threading import Lock
import time

from fastapi.testclient import TestClient

from backend.memory_app.kernel.answer_turns import answer_definition
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.recognition import RecognitionService
from backend.security.secrets import InMemorySecretStore
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.observability import Observation, observation_scope
from tests.memory_app.v2.test_workbench_ask import assemble


# 仅复用 Q65 的合成资料及外部 completion 替身，不导入其工厂或根目录配置。
OLD_TEXT = '海岛 展览旧记录。2024年旧资料载明，海岛展览门票为80元。此记录仅适用2024年。'
FRESH_TEXT = '2026年海岛交通记录。海岛环线公交每班20分钟，使用公共交通时应核对时刻。'
QUESTION = '今年 海岛 展览 开馆 预约 联系 电话'
OLD_INSIGHT = '海岛展览旧门票为80元'
SEARCH_TEXT = '今年海岛展览每日10点开馆，预约渠道为合成官网，联系电话为合成接待处。'
SELECTED_URL = 'https://example.test/official/1'


class Q65Wire:
    """只替换原外部传输，保持 Q65 三个网页和全部生成分支。"""
    def __init__(self):
        self.calls = []
        self.lock = Lock()

    def __call__(self, **request):
        messages = deepcopy(request['messages'])
        system, user = str(messages[0].get('content', '')), str(messages[-1].get('content', ''))
        annotations = None
        if 'web_search_options' in request:
            role, raw = 'search', SEARCH_TEXT
            annotations = [{'url_citation': {'url': f'https://example.test/official/{index}',
                'title': f'海岛展览官网{index}', 'start_index': 0, 'end_index': len(raw)}} for index in range(3)]
        elif 'title,summary,topics,facts,todos' in system:
            role, old = 'organize', user == OLD_TEXT
            assert old or user == FRESH_TEXT, 'qa_organization_source_unexpected'
            quote = '海岛展览门票为80元。' if old else '海岛环线公交每班20分钟，使用公共交通时应核对时刻。'
            raw = json.dumps({'title': '2024海岛展览旧记录' if old else '2026海岛交通记录',
                'summary': '海岛展览旧门票为80元，仅适用2024年。' if old else '海岛环线公交每班20分钟。',
                'topics': [], 'facts': [{'text': quote, 'evidence': {'quote': quote}}],
                'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, ensure_ascii=False)
        elif '正文候选origin=body' in system:
            role, context = 'extract', json.loads(user)
            text = '\n'.join(row['content'] for row in context['experiences'])
            output = {'insights': [], 'supports': []}
            if '2024海岛展览旧记录' in text:
                output['insights'] = [{'origin': 'body', 'kind': 'new_method', 'relation': 'new',
                    'text': OLD_INSIGHT, 'conditions': ['2024年'], 'target_id': None, 'scope_hint': None}]
            else:
                assert '2026海岛交通记录' in text, 'qa_extraction_source_unexpected'
            raw = json.dumps(output, ensure_ascii=False)
        elif 'condensed_question' in system:
            role, raw = 'condense', json.dumps({'condensed_question': QUESTION}, ensure_ascii=False)
        elif '问法' in system and 'queries' in system:
            role, raw = 'rewrite', json.dumps({'queries': []})
        elif '只根据用户提供的资料回答' in system:
            role = 'answer'
            blocks = [(int(match[1]), match[2]) for match in re.finditer(
                r'^\[(\d+)\]([^\n]*(?:\n(?!\[\d+\])[^\n]*)*)', user, re.MULTILINE)]
            stale = next((number for number, text in blocks if '过时' in text.split('\n', 1)[0]), None)
            searched = next((number for number, text in blocks if SELECTED_URL in text), None)
            assert stale is not None, 'qa_expired_insight_was_not_selected'
            answer, citations = f'旧门票记录只适用2024年，已经过时【{stale}】。', [stale]
            if searched is not None:
                answer += f'\n\n当前合成官网列明每日10点开馆，预约和联系信息以该网页为准【{searched}】。'
                citations.append(searched)
            else:
                answer += '\n\n本地资料未提供今年的开馆、预约和联系信息。'
            raw = json.dumps({'answer': answer, 'citations': citations}, ensure_ascii=False)
        else:
            raise ValueError('qa_completion_kind_unexpected')
        usage = {'prompt_tokens': 8, 'completion_tokens': 5, 'total_tokens': 13,
                 'prompt_tokens_details': {'cached_tokens': 0}}
        with self.lock:
            self.calls.append({'role': role, 'messages': messages, 'annotations': annotations})
        if request.get('stream'):
            assert annotations is None, 'qa_search_stream_unexpected'

            def chunks():
                for start in range(0, len(raw), 16):
                    yield {'choices': [{'delta': {'content': raw[start:start + 16]}, 'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': usage}

            return chunks()
        message = {'content': raw}
        if annotations is not None:
            message['annotations'] = annotations
        return {'choices': [{'finish_reason': 'stop', 'message': message}], 'usage': usage}


def test_q65_same_load_original_smallbase_http_kernel_sources_receipts_and_replay(tmp_path, record_property):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    documents, service, wire = SQLiteDocumentRepository(records), RecognitionService(records), Q65Wire()
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=wire)
    models.update('generation', {'base_url': 'https://example.test/v1', 'model': 'answer',
        'api_key': 'q15-9-synthetic-generation-only', 'allow_remote': True, 'expected_revision': 0})
    app, _ = assemble(tmp_path, records, documents, service, models)
    assert getattr(app.state, 'container', None) is None
    assert getattr(app.state, 'capability_package_catalog', None) is None
    assert getattr(app.state, 'capability_package_contributions', None) is None
    assert answer_definition().tool_definition.timeout_ms == 240_000
    with TestClient(app) as http:
        def call(method, path, body=None, *, key=None):
            response = http.request(method, path, json=body,
                headers={'Idempotency-Key': key} if key else None)
            assert response.status_code == 200, response.text
            return response.json()

        project = call('POST', '/api/v2/projects', {'name': '海岛展览'})['id']

        def remember(text, key):
            submitted = call('POST', '/api/v2/workbench/turns',
                {'intent': 'remember', 'project_id': project, 'text': text}, key=key)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                row = records.read('v2_turns', submitted['turn']['id'])
                assert row is not None
                state = row.payload['receipt']['remember']['state']
                if state in {'done', 'failed'}:
                    assert state == 'done', row.payload['receipt']['remember']
                    view = call('GET', f"/api/v2/workbench/threads/{submitted['thread_id']}?project_id={project}")
                    return next(turn for turn in view['turns'] if turn['id'] == row.object_id)
                time.sleep(.1)
            raise TimeoutError('qa_remember_deadline')

        old = remember(OLD_TEXT, 'seed-old')['receipt']['remember']['insights']
        assert len(old) == 1 and old[0]['conditions'] == ['2024年']
        confirmed = call('POST', f"/api/v2/library/insights/{old[0]['id']}/confirm",
            {'project_id': project, 'expected_revision': old[0]['revision']})
        original_insight = records.read('recognitions', confirmed['id'])
        assert original_insight.payload['conditions'] == ['2024年']
        assert remember(FRESH_TEXT, 'seed-current')['receipt']['remember']['insights'] == []
        models.update('search', {'base_url': 'https://example.test/v1', 'model': 'search',
            'api_key': 'q15-9-synthetic-search-only', 'enabled': False, 'allow_remote': False, 'expected_revision': 0})
        body = {'intent': 'ask', 'project_id': project, 'text': QUESTION}
        before = len(wire.calls)
        off_observation = Observation('focused-q65-off', None)
        with observation_scope(off_observation):
            off = call('POST', '/api/v2/workbench/turns', body, key='qa-search-off')
        record_property('off_observation', json.dumps(off_observation.snapshot(), sort_keys=True))
        off_timing = records.read('v2_turn_timings', off['turn']['id'])
        assert off_timing is not None and off_timing.payload['operation'] == 'ask'
        record_property('off_original_turn_timing', json.dumps(off_timing.payload, sort_keys=True))
        off_receipt = off['turn']['receipt']['ask']
        assert not any(row['role'] == 'search' for row in wire.calls[before:])
        assert any(row.get('stale') is True for row in off_receipt['citations'])
        assert off_receipt['trace'][-1]['stopped'] is False and 'search' not in off_receipt
        models.update('search', {'enabled': True, 'allow_remote': True, 'expected_revision': 1})
        originals_before, before = records.list('workspace_items'), len(wire.calls)
        on_observation = Observation('focused-q65-on', None)
        with observation_scope(on_observation):
            on = call('POST', '/api/v2/workbench/turns', body, key='qa-search-on')
        record_property('on_observation', json.dumps(on_observation.snapshot(), sort_keys=True))
        on_timing = records.read('v2_turn_timings', on['turn']['id'])
        assert on_timing is not None and on_timing.payload['operation'] == 'ask'
        record_property('on_original_turn_timing', json.dumps(on_timing.payload, sort_keys=True))
        receipt = on['turn']['receipt']['ask']
        searched = [row for row in wire.calls[before:] if row['role'] == 'search']
        assert len(searched) == 1 and len(searched[0]['annotations']) == 3
        urls = [row for row in receipt['citations'] if row.get('url')]
        assert len(urls) == 1 and urls[0]['url'] == SELECTED_URL
        assert any(row.get('stale') is True for row in receipt['citations'])
        search = receipt['search']
        assert search['purpose'] == '搜索' and search['policy_version'] == '@1'
        assert search['selected'] == 3 and search['used'] == 1
        assert search['model_usage'] == {'input_tokens': 8, 'output_tokens': 5, 'total_tokens': 13}
        assert search['model_cost'] is None and receipt['model_cost'] is None
        originals_after = records.list('workspace_items')
        added = [row for row in originals_after if row.object_id not in {old.object_id for old in originals_before}]
        assert len(added) == 1 and added[0].object_id == urls[0]['id']
        assert added[0].payload['url'] == SELECTED_URL
        assert added[0].payload['status'] == 'staged' and added[0].payload['origin'] == 'search'
        assert added[0].payload['source_text'] == urls[0]['quote']
        assert '来自搜索' in added[0].payload['title'] and all(row in originals_after for row in originals_before)
        assert records.read('recognitions', confirmed['id']) == original_insight
        store = app.state.ai_turn_store
        frozen = store.get_request(on['turn']['id'])
        assert frozen['policy_versions']['rank'] == '@2' and frozen['policy_versions']['search'] == '@1'
        events = store.events_after(on['turn']['id'])
        assert events[-1]['type'] == 'turn.completed'
        dispatched = [event for event in events if event['type'] == 'model.attempt.dispatched']
        calls = [store.get(event['data']['receipt_ref']) for event in events if event['type'] == 'model.completed']
        assert len(dispatched) == len(calls) == 2
        assert sorted(call['model_call_purpose'] for call in calls) == ['aux', 'primary']
        assert {call['model_request_id'] for call in calls} == {
            event['correlation']['model_request_id'] for event in dispatched}
        assert store.get_immutable_payload(on['turn']['id'], 'answer-model-input-multi-query')[1]['purpose'] == 'aux'
        auxiliary_store = MemoryTurn.store_for(records)
        auxiliary = auxiliary_store.get_request(search['turn_id'])
        assert auxiliary['desired_outcome'] == 'web.search' and auxiliary['scope']['project_id'] == project
        assert auxiliary['input']['text'] == QUESTION and auxiliary['policy_versions'] == {'search': '@1'}
        assert auxiliary['privacy']['material_refs'] == auxiliary['privacy']['source_snapshots'] == []
        auxiliary_events = auxiliary_store.events_after(search['turn_id'])
        assert auxiliary_events[-1]['type'] == 'turn.completed'
        assert len([event for event in auxiliary_events if event['type'] == 'model.attempt.dispatched']) == 1
        binding = records.read('v2_memory_turn_keys', search['turn_id'])
        assert binding.payload['identity'] == {'kind': 'web.search', 'project': project,
            'key': on['turn']['id'], 'purpose': 'search'}
        primary = store.get_immutable_payload(on['turn']['id'], 'answer-model-input-answer')[1]
        assert SELECTED_URL in primary['messages'][-1]['content']
        before = len(wire.calls)
        for _ in range(2):
            view = call('GET', f"/api/v2/workbench/threads/{on['thread_id']}?project_id={project}")
            assert view['turns'][0] == on['turn']
        assert call('POST', '/api/v2/workbench/turns', body, key='qa-search-on') == on
        assert len(wire.calls) == before and records.list('workspace_items') == originals_after
        assert records.read('recognitions', confirmed['id']) == original_insight
        settings = call('GET', '/api/v2/settings')['model']['search']
        assert settings['enabled'] is True and settings['allow_remote'] is True
        assert settings['pricing']['rates'] is None
