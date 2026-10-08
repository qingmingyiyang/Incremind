"""Real confirmed reasks enter the original immutable consolidation owner."""
import json
from datetime import datetime, timedelta, timezone

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.consolidation import Consolidation
from backend.memory_app.v2.learning_events import events
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.signal_reviews import install_signal_review_routes
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.kernel_receipts import requests, wire_receipts
from tests.memory_app.v2.test_workbench_ask import env, add_document, ask, publish


def test_confirmed_real_reask_is_frozen_consumed_once_and_stays_pending(env):
    document = add_document(env, summary='alpha beta gamma', body='Synthetic original evidence',
        original='Synthetic original alpha beta gamma')[0]
    published, _ = publish(env, doc=document)
    install_signal_review_routes(env.http.app, records=env.records, service=env.service,
        documents=env.documents, runtime_root=env.root)
    responses = [ask(env, text='alpha beta gamma?', intent='ask') for _ in range(2)]
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    turns = [response.json()['turn'] for response in responses]
    store = env.http.app.state.ai_turn_store
    immutable = []
    for turn in turns:
        request = store.get_request(turn['id'])
        saved = store.get_immutable_payload(turn['id'], 'product-answer-result-v2')
        wire_input = store.get_immutable_payload(turn['id'], 'answer-model-input-answer')
        assert request['scope']['project_id'] == 'alpha' and request['desired_outcome'] == 'project.answer'
        assert saved is not None and wire_input is not None
        immutable.append((request['input']['text'], saved[1]['receipt']['ask']['answer'][:300]))
    listed = env.http.get('/api/v2/library/signal-reviews', params={'project_id': 'alpha'})
    assert listed.status_code == 200, listed.text
    item = next(item for item in listed.json()['items'] if item['kind'] == 'reask')
    confirmed = env.http.post('/api/v2/library/signal-reviews/decide', json={
        'project_id': 'alpha', 'items': [{'id': item['id'], 'action': 'confirm',
            'expected_revision': item['revision']}]})
    assert confirmed.status_code == 200, confirmed.text
    decisions = env.records.list('v2_signal_decisions')
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.payload['turn_ids'] == [turn['id'] for turn in turns]
    assert set(decision.payload) == {'review_key', 'kind', 'action', 'turn_ids', 'object', 'at', 'by'}
    event_id = 'signal-decision:' + decision.object_id
    assert event_id in events(env.records)['alpha']

    calls = []
    def transport(**request):
        calls.append(request)
        text = '\n'.join(message['content'] for message in request['messages'])
        if '最多300字' in text:
            output = {'text': 'Synthetic overview'}
        else:
            output = {'text': 'Synthetic corrected method', 'conditions': ['Synthetic condition'],
                'event_ids': [event_id], 'kind': 'correction'}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
    models = ModelConfiguration(env.records, env.root, InMemorySecretStore(), completion_fn=transport)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'main',
        'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    choose(models, 'quick')
    job = Consolidation(env.records, env.service, env.documents, models,
        now=lambda: datetime.now(timezone.utc) + timedelta(days=1))
    with override(trigger='@2', consolidate='@2'):
        outcome = job.run('alpha')
    frozen = [request for request in requests(env.records) if request['desired_outcome'] == 'memory.consolidate']
    assert len(frozen) == 1, outcome
    request = frozen[0]
    assert event_id in request['input']['text'] and 'answer_miss' in request['input']['text']
    for question, answer in immutable:
        assert question in request['input']['text'] and answer in request['input']['text']
    assert request['privacy']['source_snapshots']
    assert any(snapshot['nodes'] for snapshot in request['privacy']['source_snapshots'])
    assert outcome['new_suggestions'] == 1
    consumed = [row for row in env.records.list('v2_consolidation_inputs')
        if event_id in row.payload.get('event_ids', [])]
    assert len(consumed) == 1 and consumed[0].payload['event_ids'] == [event_id]
    patterns = [row for row in env.records.list('v2_insight_patterns')
        if event_id in row.payload.get('event_ids', [])]
    assert len(patterns) == 1
    candidate = env.records.read('recognition_candidates', patterns[0].object_id)
    assert candidate.payload['state'] == 'pending' and candidate.payload['source_experience_ids']
    assert env.records.list('recognitions') == (env.records.read('recognitions', published.id),)
    before = (len(calls), wire_receipts(env.records), consumed, candidate)
    with override(trigger='@2', consolidate='@2'):
        assert job.run('alpha')['replayed'] is True
    assert len(calls) == before[0] and wire_receipts(env.records) == before[1]
    assert [row for row in env.records.list('v2_consolidation_inputs')
        if event_id in row.payload.get('event_ids', [])] == before[2]
    assert env.records.read('recognition_candidates', candidate.object_id) == before[3]
