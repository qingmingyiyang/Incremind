"""当前默认调查协议的真实装配回归，模型运输返回合成 JSON。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


QUESTION = 'What is the andromeda calibration setting?'
SUMMARY = 'andromeda instrument overview.'
BODY = 'andromeda calibration body setting is 846 kelvin.'
MARKDOWN = '# Andromeda\n\n## 摘要\n' + SUMMARY + '\n\n## 正文\n' + BODY
PROJECT = 'alpha'


class _SyntheticTransport:
    def __init__(self):
        self.calls = []
        self.contexts = []
        self.invalid_quote_stage = None

    def __call__(self, **request):
        messages = request['messages']
        text = messages[-1]['content']
        if 'remaining_decisions' in text:
            context = json.loads(text)
            self.contexts.append(deepcopy(context))
            output = self._decision(context)
            stage = 'decision'
        else:
            assert '资料：\n' in text, messages
            payload, _ = json.JSONDecoder().raw_decode(text.split('资料：\n', 1)[1])
            catalog = payload['evidence_catalog']
            unit = next(row for row in catalog['units'] if BODY in row['text'])
            stage = 'review' if 'untrusted_first_fact_plan' in payload else 'first'
            quote = ('a quote absent from the actual source' if self.invalid_quote_stage == stage
                     else unit['text'])
            fact = {'kind': 'fact', 'text': BODY, 'evidence': [
                {'unit_id': unit['id'], 'quote': quote, 'role': 'support'}], 'date_refs': []}
            if stage == 'first':
                output = {'facts': [fact], 'unreviewed': {
                    'scope': 'all_unreferenced', 'reason': 'Other units do not answer the requested setting.'}}
            else:
                fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'adopt',
                                           'reason': 'The setting is quoted from the same source.'}]
                output = {'statements': [fact], 'unused': {
                    'scope': 'all_unreferenced', 'reason': 'Other units do not answer the requested setting.'},
                    'uncertain': [], 'excluded_facts': []}
        self.calls.append({'stage': stage, 'messages': deepcopy(messages), 'output': deepcopy(output)})
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(output)}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}}

    def _decision(self, context):
        # 动作依赖上轮真实返回的目录和正文，不伪造领域检索结果。
        decision = {'action': 'search', 'source': 'memory', 'query': '',
                    'depth': 'summary', 'ids': [], 'questions': []}
        if context['round'] == 1:
            assert context['materials'] == [] and context['steps'] == []
            decision['query'] = 'andromeda instrument'
        elif context['round'] == 2:
            assert SUMMARY in json.dumps(context['materials'])
            assert BODY not in json.dumps(context['materials'])
            selected = next(row for row in context['catalog'] if row['layer'] == 'L2')
            decision.update(action='read', depth='body', ids=[selected['id']])
        else:
            assert context['round'] == 3
            assert BODY in json.dumps(context['materials'])
            assert [row['decision']['action'] for row in context['steps']] == ['search', 'read']
            decision['action'] = 'answer'
        return decision


@pytest.fixture
def default_flow(monkeypatch):
    # 临时根不放在深层 checkout 内，避免 Windows 能力包 staging 路径超过系统上限。
    with TemporaryDirectory(prefix='r7-') as directory:
        root = Path(directory)
        (root / 'config').mkdir()
        code = Path(__file__).resolve().parents[3]
        shutil.copyfile(code / 'config/settings.toml.example', root / 'config/settings.toml')
        monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
        monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
        monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'true')
        monkeypatch.setenv('HF_HUB_OFFLINE', '1')
        monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
        connections = []

        def local_connections_only(original):
            def connect(sock, address):
                # Windows asyncio 的 socketpair 使用本机 TCP，自唤醒不经过模型或搜索。
                if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1', 'localhost'}:
                    return original(sock, address)
                connections.append(address)
                raise AssertionError('default_retrieve_network_forbidden')
            return connect

        monkeypatch.setattr(socket.socket, 'connect', local_connections_only(socket.socket.connect))
        monkeypatch.setattr(socket.socket, 'connect_ex', local_connections_only(socket.socket.connect_ex))
        import litellm  # noqa: F401
        from backend.memory_app.app import create_app
        from backend.memory_app.model_config import ModelConfiguration
        from backend.memory_app.storage_authority import resolve_recognition_document_store
        from backend.recognition import WorkScope
        from backend.security.secrets import InMemorySecretStore
        from backend.shared.deployment import DeploymentLayout
        from core.document_engine import DocumentDraft

        records, namespace = resolve_recognition_document_store(root)
        assert namespace == 'default'
        transport = _SyntheticTransport()
        models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=transport)
        models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'synthetic-flow',
            'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})

        def build():
            return create_app(runtime_root=root, model_configuration=models,
                              deployment_layout=DeploymentLayout('desktop', root))

        app = build()
        assert app.state.recognition_document_namespace == 'default'
        scope = WorkScope('local-user', PROJECT)
        service = app.state.recognition_service
        experience = service.stage_experience(scope=scope, content='Synthetic calibration observation.')
        recognition = service.propose(scope=scope,
            content='andromeda instruments require scheduled calibration.',
            source_experience_ids=[experience.id if hasattr(experience, 'id') else experience])
        service.publish(scope=scope, candidate_id=recognition.id, expected_revision=1, reviewer='local-user')
        original = 'andromeda original calibration setting is 963 kelvin.'
        app.state.workspace_domains.query.source_store.write('sources', 'andromeda-source', {
            'id': 'andromeda-source', 'title': 'Andromeda source', 'project_id': PROJECT,
            'metadata': {'content': original}}, expected_revision=0)
        document = app.state.recognition_documents.create(DocumentDraft(title='Andromeda calibration',
            document_type='legacy-material', markdown=MARKDOWN, source_refs=({
                'source_id': 'andromeda-source', 'locator': f'text:0:{len(original)}'},), project_id=PROJECT))
        with TestClient(app) as http:
            yield SimpleNamespace(root=root, app=app, http=http, records=records, model=models,
                transport=transport, build=build, document=document, recognition=recognition)
        assert connections == []


def _ask(flow, key):
    return flow.http.post('/api/v2/workbench/turns', headers={'Idempotency-Key': key},
        json={'project_id': PROJECT, 'text': QUESTION, 'intent': 'ask'})


def test_default_retrieve_searched_body_is_reviewed_and_durably_replayed(default_flow):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    from backend.memory_app.v2.answer_evidence import FocusedReferencedFactsOutput, FocusedReviewedScopeOutput
    from backend.memory_app.v2.policies import version

    flow = default_flow
    assert (version('retrieve'), version('compose')) == ('@7', '@30')
    response = _ask(flow, 'default-retrieve-success')
    assert response.status_code == 200, response.text
    saved = response.json()
    turn, receipt = saved['turn'], saved['turn']['receipt']['ask']
    assert turn['intent'] == 'ask' and turn['user_text'] == QUESTION
    assert receipt['no_match'] is False and BODY in receipt['answer']
    assert [(row['id'], row['layer']) for row in receipt['citations']] == [(flow.document['id'], 'note')]
    assert BODY in receipt['citations'][0]['quote']
    citation = receipt['citations'][0]
    assert citation['locator']['coordinate_space'] == 'document_markdown_v1'
    windows = citation['locator']['windows']
    assert windows and all(0 <= row['start'] < row['end'] <= len(MARKDOWN) for row in windows)
    assert citation['quote'] == '\n…\n'.join(MARKDOWN[row['start']:row['end']] for row in windows)
    investigation = receipt['trace'][0]['agentic']
    assert investigation['status'] == 'answer' and investigation['stop'] == 'answer'
    assert [step['decision']['action'] for step in investigation['steps']] == ['search', 'read']
    assert [call['stage'] for call in flow.transport.calls] == ['decision'] * 3 + ['first', 'review']
    assert any(row['layer'] == 'L3' for row in flow.transport.contexts[1]['materials'])

    store = flow.app.state.ai_turn_store
    frozen = store.get_request(turn['id'])
    assert frozen['policy_versions']['retrieve'] == '@7'
    assert frozen['policy_versions']['compose'] == '@30'
    assert frozen['scope']['project_id'] == PROJECT and frozen['privacy']['allow_remote'] is True
    for number in range(1, 4):
        actual = store.get_immutable_payload(turn['id'], f'answer-gap-input-rag-decision-{number}-v1')[1]
        assert actual['policy_versions'] == frozen['policy_versions']
    first = store.get_immutable_payload(turn['id'], 'answer-model-input-answer')[1]
    review = store.get_immutable_payload(turn['id'], 'answer-model-input-' + REVIEW_KEY)
    assert first['response_schema'] == FocusedReferencedFactsOutput.model_json_schema()
    assert review[1]['response_schema'] == FocusedReviewedScopeOutput.model_json_schema()
    assert read_answer_input(store, turn['id']) == review
    egress = flow.records.read('workspace_ask_receipts', receipt['egress_receipt_id'])
    assert egress.payload['status'] == 'completed'
    assert any(source['id'] == flow.document['id'] and source['kind'] == 'document'
               for source in egress.payload['sources'])
    groups = kernel_call_groups(flow.root, turn_id=turn['id'], project=PROJECT,
                                records=flow.records, include_answer_input=True)
    assert len(groups) == 1 and groups[0]['request'] == frozen
    assert len(groups[0]['calls']) == 5 and groups[0]['answer_input'] == review[1]
    assert all(call['status'] == 'completed' for call in groups[0]['calls'])
    assert receipt['model_usage']['total_tokens'] == 200

    counts = len(flow.transport.calls)
    before = {name: flow.records.list(name) for name in ('v2_turns', 'v2_threads', 'workspace_ask_receipts')}
    thread_path = f"/api/v2/workbench/threads/{saved['thread_id']}?project_id={PROJECT}"
    assert flow.http.get(thread_path).json()['turns'] == [turn]
    assert _ask(flow, 'default-retrieve-success').json() == saved
    assert flow.http.get(thread_path.replace('project_id=alpha', 'project_id=beta')).status_code == 404
    assert len(flow.transport.calls) == counts
    assert {name: flow.records.list(name) for name in before} == before
    with TestClient(flow.build()) as restarted:
        restored = restarted.get(thread_path)
        assert restored.status_code == 200 and restored.json()['turns'] == [turn]
        replay = restarted.post('/api/v2/workbench/turns', headers={
            'Idempotency-Key': 'default-retrieve-success'}, json={
                'project_id': PROJECT, 'text': QUESTION, 'intent': 'ask'})
        assert replay.status_code == 200 and replay.json() == saved
    assert len(flow.transport.calls) == counts


@pytest.mark.parametrize('stage', ['first', 'review'])
def test_default_retrieve_rejects_quotes_absent_from_real_evidence(default_flow, stage):
    flow = default_flow
    flow.transport.invalid_quote_stage = stage
    response = _ask(flow, 'default-retrieve-invalid-' + stage)
    assert response.status_code == 502 and response.json() == {'detail': 'answer_generation_failed'}
    assert any(call['stage'] == stage for call in flow.transport.calls)
    assert flow.records.list('v2_turns') == ()
    receipts = flow.records.list('workspace_ask_receipts')
    assert len(receipts) == 1 and receipts[0].payload['status'] == 'failed'
