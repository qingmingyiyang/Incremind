"""Confirmed feedback uses real ASK execution and original source owners."""
import importlib
import importlib.util
import json
import re
import sqlite3
from datetime import datetime, timezone

import pytest

from backend.memory_app.v2.signal_reviews import install_signal_review_routes
from tests.memory_app.v2.test_workbench_ask import env, add_document, ask, publish


def prepare_confirmed(env, action='confirm'):
    document = add_document(env, summary='alpha beta gamma', body='Synthetic evidence',
        original='Synthetic original alpha beta gamma')[0]
    recognition, _ = publish(env, doc=document)
    install_signal_review_routes(env.http.app, records=env.records, service=env.service,
        documents=env.documents, runtime_root=env.root)
    turns = []
    for _ in range(2):
        response = ask(env, text='alpha beta gamma?', intent='ask')
        assert response.status_code == 200, response.text
        turns.append(response.json()['turn'])
    listed = env.http.get('/api/v2/library/signal-reviews', params={'project_id': 'alpha'})
    assert listed.status_code == 200, listed.text
    item = next(item for item in listed.json()['items'] if item['kind'] == 'reask')
    response = env.http.post('/api/v2/library/signal-reviews/decide', json={
        'project_id': 'alpha', 'items': [{'id': item['id'], 'action': action,
            'expected_revision': item['revision']}]})
    assert response.status_code == 200, response.text
    decision = env.records.list('v2_signal_decisions')[0]
    assert decision.payload['turn_ids'] == [turn['id'] for turn in turns]
    return env, document, recognition, turns, decision


@pytest.fixture
def confirmed(env):
    return prepare_confirmed(env)


def feedback_owner():
    name = 'backend.memory_app.v2.signal_review_feedback'
    assert importlib.util.find_spec(name) is not None, 'confirmed real ASK feedback owner is unavailable'
    return importlib.import_module(name)


def test_actual_confirmed_reask_builds_verified_answer_miss(confirmed):
    env, document, recognition, turns, decision = confirmed
    owner = feedback_owner()
    events = owner.review_corrections(env.records, 'alpha')
    assert len(events) == 1
    event = events[0]
    assert event['event_id'] == 'signal-decision:' + decision.object_id
    assert event['type'] == 'answer_miss' and event['turn_ids'] == [turn['id'] for turn in turns]
    assert 'alpha beta gamma?' in event['before'] and 'Synthetic answer' in event['before']
    assert 'alpha beta gamma?' in event['after'] and 'Synthetic answer' in event['after']
    assert event['_snapshots'] and event['_source_graph']['nodes']
    assert recognition.id in event['_source_recognition_ids'] and event['_source_experience_ids']
    assert document in event['_documents']
    owner.validate_review_corrections(env.records, events, project='alpha')
    rendered = owner.review_feedback(env.records, events, project='alpha')
    assert event['event_id'] in rendered and 'answer_miss' in rendered
    assert owner.review_key(events)


def replace(records, collection, row, payload):
    with records.begin() as tx:
        result = tx.put(collection, row.object_id, payload, expected_revision=row.revision)
        tx.commit()
    return result


@pytest.mark.parametrize('change', ['dismiss', 'unused', 'admin', 'extra_body'])
def test_nonqualified_decision_is_excluded(confirmed, change):
    env, _, _, _, decision = confirmed
    payload = dict(decision.payload)
    field, value = {'dismiss': ('action', 'dismiss'), 'unused': ('kind', 'unused'),
        'admin': ('by', 'admin'), 'extra_body': ('text', 'Synthetic forbidden body')}[change]
    payload[field] = value
    replace(env.records, 'v2_signal_decisions', decision, payload)
    assert feedback_owner().review_corrections(env.records, 'alpha') == []


def test_scope_owner_revision_and_missing_immutable_are_fail_closed(confirmed):
    from backend.recognition import RecognitionConflict, WorkScope
    env, _, recognition, turns, _ = confirmed
    owner = feedback_owner()
    events = owner.review_corrections(env.records, 'alpha')
    assert events and owner.review_corrections(env.records, 'beta') == []
    env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=recognition.id,
        expected_revision=recognition.revision, content='Synthetic changed current recognition')
    assert owner.review_corrections(env.records, 'alpha') == []
    with pytest.raises(RecognitionConflict, match='review_feedback_changed'):
        owner.validate_review_corrections(env.records, events, project='alpha')


def test_private_remote_defers_and_local_preserves_proof(confirmed):
    from backend.memory_app.v2.privacy import set_private_project
    env, _, _, _, _ = confirmed
    owner = feedback_owner()
    before = owner.review_corrections(env.records, 'alpha')
    set_private_project(env.records, 'alpha', True, expected_revision=0)
    assert owner.review_corrections(env.records, 'alpha', local_only=False) == []
    local = owner.review_corrections(env.records, 'alpha', local_only=True)
    assert len(local) == 1 and local[0]['_source_graph']['nodes']
    assert local[0]['_privacy_revision'] != before[0]['_privacy_revision']
    owner.validate_review_corrections(env.records, local, project='alpha')


def test_consumed_namespace_and_explicit_feedback_after_off_clear(confirmed):
    from backend.memory_app.v2.signals import SignalService
    env, _, _, _, decision = confirmed
    owner = feedback_owner()
    settings = SignalService(env.records)
    settings.set_enabled(False, expected_revision=0)
    settings.clear(expected_revision=1)
    events = owner.review_corrections(env.records, 'alpha')
    assert len(events) == 1  # Already confirmed explicit feedback is not an implicit event.
    with env.records.begin() as tx:
        tx.put('v2_consolidation_inputs', 'synthetic-consumption',
            {'project_id': 'alpha', 'documents': [], 'event_ids': [events[0]['event_id']]},
            expected_revision=0)
        tx.commit()
    assert owner.review_corrections(env.records, 'alpha') == []
    assert env.records.read('v2_signal_decisions', decision.object_id) == decision


def test_real_primary_long_answer_uses_policy_limit_and_exact_immutable(env):
    from fastapi.testclient import TestClient
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.v2.policies import get
    from backend.security.secrets import InMemorySecretStore
    from tests.memory_app.v2.test_workbench_ask import assemble
    document = add_document(env, summary='alpha beta gamma', body='Synthetic evidence',
        original='Synthetic original alpha beta gamma')[0]
    publish(env, doc=document)
    wires, answers = [], []
    def transport(**request):
        wires.append(request)
        text = '\n'.join(message['content'] for message in request['messages'])
        if 'condensed_question' in text:
            output = {'condensed_question': 'alpha beta gamma?'}
        elif '\"queries\"' in text:
            output = {'queries': ['alpha beta gamma?']}
        else:
            answer = ('Synthetic first ' if not answers else 'Synthetic second ') + '甲' * 330 + 'NEVER_RENDER_TAIL'
            answers.append(answer)
            output = {'answer': answer, 'citations': [int(n) for n in re.findall(r'^\[(\d+)\]', text, re.M)]}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
    models = ModelConfiguration(env.records, env.root, InMemorySecretStore(), completion_fn=transport)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'main',
        'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    app, domains = assemble(env.root, env.records, env.documents, env.service, models)
    install_signal_review_routes(app, records=env.records, service=env.service,
        documents=env.documents, runtime_root=env.root)
    with TestClient(app) as http:
        for _ in range(2):
            response = http.post('/api/v2/workbench/turns', json={
                'project_id': 'alpha', 'text': 'alpha beta gamma?', 'intent': 'ask'})
            assert response.status_code == 200, response.text
            identity = response.json()['turn']['id']
            frozen = app.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2')
            assert frozen[1]['receipt']['ask']['answer'] == answers[-1]
        listed = http.get('/api/v2/library/signal-reviews', params={'project_id': 'alpha'})
        assert listed.status_code == 200, listed.text
        item = next(item for item in listed.json()['items'] if item['kind'] == 'reask')
        response = http.post('/api/v2/library/signal-reviews/decide', json={'project_id': 'alpha',
            'items': [{'id': item['id'], 'action': 'confirm', 'expected_revision': item['revision']}]})
        assert response.status_code == 200, response.text
    before_wires = len(wires)
    events = feedback_owner().review_corrections(env.records, 'alpha')
    assert len(events) == 1 and len(answers) == 2
    limit = get('review').feedback_answer_chars
    assert limit == 300
    assert events[0]['before'] == 'alpha beta gamma?\n' + answers[0][:limit]
    assert events[0]['after'] == 'alpha beta gamma?\n' + answers[1][:limit]
    assert 'NEVER_RENDER_TAIL' not in feedback_owner().review_feedback(env.records, events, project='alpha')
    assert len(wires) == before_wires  # Qualification/rendering never makes a model request.


@pytest.mark.parametrize('change', ['owner_and_receipt', 'windows'])
def test_mutable_egress_cannot_replace_original_sent_material(confirmed, change):
    from backend.recognition import WorkScope
    env, _, recognition, _, _ = confirmed
    if change == 'owner_and_receipt':
        changed = env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=recognition.id,
            expected_revision=recognition.revision, content='Synthetic never sent replacement statement')
    for row in env.records.list('workspace_ask_receipts'):
        sources = [dict(source) for source in row.payload['sources']]
        for source in sources:
            if source['kind'] == 'recognition' and source['id'] == recognition.id:
                if change == 'owner_and_receipt':
                    source['revision'] = changed.revision
                else:
                    source['windows'] = [{'start': 0, 'end': 1}]
        replace(env.records, 'workspace_ask_receipts', row, {**row.payload, 'sources': sources})
    assert feedback_owner().review_corrections(env.records, 'alpha') == []


@pytest.mark.parametrize('missing', ['immutable_input', 'completed_terminal'])
def test_actual_primary_without_parent_proof_is_unknown(confirmed, missing):
    from backend.memory_app.original_sources import source_store
    env, _, _, turns, _ = confirmed
    owner = feedback_owner()
    before = owner.review_corrections(env.records, 'alpha')
    assert before
    with sqlite3.connect(source_store(env.records).root / 'ai-turns.sqlite3') as connection:
        if missing == 'immutable_input':
            connection.execute('DELETE FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
                (turns[0]['id'], 'answer-model-input-answer'))
        else:
            connection.execute("DELETE FROM ai_turn_events WHERE turn_id=? AND json_extract(event_json,'$.type')='turn.completed'",
                (turns[0]['id'],))
    assert owner.review_corrections(env.records, 'alpha') == []


def test_actual_me_source_is_deferred_without_cross_scope_retention(env):
    publish(env, text='Synthetic alpha beta gamma profile', project='me')
    env, _, _, turns, _ = prepare_confirmed(env)
    result = env.http.app.state.ai_turn_store.get_immutable_payload(turns[0]['id'], 'product-answer-result-v2')[1]
    assert any(entry.get('persona') for entry in result['receipt']['ask']['context']['entries'])
    assert feedback_owner().review_corrections(env.records, 'alpha') == []


def test_actual_standalone_l0_without_existing_experience_is_deferred(env):
    query = env.domains.query
    query.source_store.write('sources', 'standalone', {'id': 'standalone', 'title': 'Synthetic source',
        'project_id': 'alpha', 'metadata': {'content': 'alpha beta gamma'}}, expected_revision=0)
    install_signal_review_routes(env.http.app, records=env.records, service=env.service,
        documents=env.documents, runtime_root=env.root)
    for _ in range(2):
        response = ask(env, intent='ask')
        assert response.status_code == 200, response.text
        identity = response.json()['turn']['id']
        immutable = env.http.app.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
        receipt = env.records.read('workspace_ask_receipts', immutable['receipt']['ask']['egress_receipt_id'])
        assert receipt.payload['sources'][0]['kind'] == 'source'
    assert not env.records.list('recognition_experiences')
    listed = env.http.get('/api/v2/library/signal-reviews', params={'project_id': 'alpha'})
    assert listed.status_code == 200, listed.text
    item = next(item for item in listed.json()['items'] if item['kind'] == 'reask')
    response = env.http.post('/api/v2/library/signal-reviews/decide', json={'project_id': 'alpha',
        'items': [{'id': item['id'], 'action': 'confirm', 'expected_revision': item['revision']}]})
    assert response.status_code == 200, response.text
    assert feedback_owner().review_corrections(env.records, 'alpha') == []
    assert not env.records.list('recognition_experiences')


def test_future_stop_decision_contract_uses_real_completed_ask_without_after(confirmed):
    # T14.11 producer is not available: only its proposed decision shape is synthetic.
    env, _, _, turns, decision = confirmed
    payload = {**decision.payload, 'kind': 'stop', 'turn_ids': [turns[0]['id']],
        'review_key': json.dumps(['alpha', 'stop', [turns[0]['id']]], separators=(',', ':'))}
    replace(env.records, 'v2_signal_decisions', decision, payload)
    events = feedback_owner().review_corrections(env.records, 'alpha')
    assert len(events) == 1 and events[0]['type'] == 'answer_miss' and events[0]['after'] == ''
    assert events[0]['turn_ids'] == [turns[0]['id']]



def test_actual_http_dismiss_is_not_feedback(env):
    prepare_confirmed(env, action='dismiss')
    assert env.records.list('v2_signal_decisions')[0].payload['action'] == 'dismiss'
    assert feedback_owner().review_corrections(env.records, 'alpha') == []


def test_actual_http_reask_without_decision_is_not_feedback(env):
    document = add_document(env, summary='alpha beta gamma', body='Synthetic evidence',
        original='Synthetic original alpha beta gamma')[0]
    publish(env, doc=document)
    for _ in range(2):
        response = ask(env, intent='ask')
        assert response.status_code == 200, response.text
    assert not env.records.list('v2_signal_decisions')
    assert feedback_owner().review_corrections(env.records, 'alpha') == []


def test_actual_document_windows_retain_existing_original_experience(env):
    from backend.memory_app.document_recognition import ensure_document_experience
    document = add_document(env, summary='alpha beta gamma', body='Synthetic alpha beta gamma evidence',
        original='Synthetic original alpha beta gamma')[0]
    experience, _ = ensure_document_experience(env.documents, env.service, 'alpha', document)
    install_signal_review_routes(env.http.app, records=env.records, service=env.service,
        documents=env.documents, runtime_root=env.root)
    for _ in range(2):
        response = ask(env, intent='ask')
        assert response.status_code == 200, response.text
        saved = env.http.app.state.ai_turn_store.get_immutable_payload(response.json()['turn']['id'],
            'product-answer-result-v2')[1]
        assert any(candidate['layer'] in {'L1', 'L2'} for candidate in saved['chosen'])
    listed = env.http.get('/api/v2/library/signal-reviews', params={'project_id': 'alpha'})
    assert listed.status_code == 200, listed.text
    item = next(item for item in listed.json()['items'] if item['kind'] == 'reask')
    response = env.http.post('/api/v2/library/signal-reviews/decide', json={'project_id': 'alpha',
        'items': [{'id': item['id'], 'action': 'confirm', 'expected_revision': item['revision']}]})
    assert response.status_code == 200, response.text
    events = feedback_owner().review_corrections(env.records, 'alpha')
    assert len(events) == 1 and events[0]['_source_experience_ids'] == [experience]
    assert events[0]['_source_recognition_ids'] == []
    assert all(set(ref) == {'type', 'id', 'revision', 'project_id'} for ref in events[0]['_refs'])
    assert document in events[0]['_documents']


def test_same_content_revision_and_mutable_egress_drift_is_deferred(confirmed):
    from backend.recognition import WorkScope
    env, _, recognition, _, _ = confirmed
    from backend.memory_app.context_adapter import format_recognition_content
    scope = WorkScope('local-user', 'alpha')
    before_text = format_recognition_content(next(entry for entry in env.service.retrieval_entries(scope=scope)
        if entry['id'] == recognition.id))
    intermediate = env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=recognition.id,
        expected_revision=recognition.revision, content='Synthetic intermediate revision')
    changed = env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=recognition.id,
        expected_revision=intermediate.revision, content=recognition.content,
        conditions=recognition.conditions)
    assert changed.revision == recognition.revision + 2
    assert format_recognition_content(next(entry for entry in env.service.retrieval_entries(scope=scope)
        if entry['id'] == recognition.id)) == before_text
    for row in env.records.list('workspace_ask_receipts'):
        sources = [dict(source) for source in row.payload['sources']]
        for source in sources:
            if source['kind'] == 'recognition' and source['id'] == recognition.id:
                source['revision'] = changed.revision
        replace(env.records, 'workspace_ask_receipts', row, {**row.payload, 'sources': sources})
    assert feedback_owner().review_corrections(env.records, 'alpha') == []


@pytest.mark.parametrize('change', ['private', 'history_cas'])
def test_real_thread_history_source_closure_is_retained_or_deferred(env, change):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import WorkScope
    document_a = add_document(env, summary='alpha beta gamma', body='Synthetic source A')[0]
    recognition_a, _ = publish(env, doc=document_a)
    first = ask(env, text='alpha beta gamma?', intent='ask')
    assert first.status_code == 200, first.text
    saved = first.json()
    document_b = add_document(env, summary='omega sigma tau', body='Synthetic source B',
        original='omega sigma tau')[0]
    recognition_b, _ = publish(env, text='omega sigma tau', doc=document_b)
    install_signal_review_routes(env.http.app, records=env.records, service=env.service,
        documents=env.documents, runtime_root=env.root)
    turns = []
    for _ in range(2):
        response = ask(env, text='omega sigma tau?', thread_id=saved['thread_id'], intent='ask')
        assert response.status_code == 200, response.text
        turn = response.json()['turn']
        entries = turn['receipt']['ask']['context']['entries']
        assert [(entry['layer'], entry['id']) for entry in entries] == [('insight', recognition_b.id)]
        assert saved['turn']['id'] in [item['id'] for item in turn['receipt']['ask']['trace'][0]['history_turn_ids']]
        turns.append(turn)
    listed = env.http.get('/api/v2/library/signal-reviews', params={'project_id': 'alpha'})
    assert listed.status_code == 200, listed.text
    item = next(item for item in listed.json()['items'] if item['kind'] == 'reask')
    response = env.http.post('/api/v2/library/signal-reviews/decide', json={
        'project_id': 'alpha', 'items': [{'id': item['id'], 'action': 'confirm',
            'expected_revision': item['revision']}]})
    assert response.status_code == 200, response.text
    decision = env.records.list('v2_signal_decisions')[0]
    assert decision.payload['turn_ids'] == [turn['id'] for turn in turns]
    owner = feedback_owner()
    before = owner.review_corrections(env.records, 'alpha')
    assert len(before) == 1
    assert {recognition_a.id, recognition_b.id} <= set(before[0]['_source_recognition_ids'])
    assert set(before[0]['_ask_proofs']) == {saved['turn']['id'], *(turn['id'] for turn in turns)}
    assert ('v2_turns', saved['turn']['id']) in before[0]['_owners']
    owner.validate_review_corrections(env.records, before, project='alpha')
    if change == 'private':
        SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'),
            'recognition', recognition_a.id, recognition_a.revision, 0, [])
    else:
        row = env.records.read('v2_turns', saved['turn']['id'])
        replace(env.records, 'v2_turns', row, {**row.payload, 'updated_at': '2099-01-01T00:00:00+00:00'})
    calls = env.model.calls
    assert feedback_owner().review_corrections(env.records, 'alpha') == []
    assert env.model.calls == calls
    assert env.records.list('v2_consolidation_inputs') == ()
    from backend.recognition import RecognitionConflict
    with pytest.raises(RecognitionConflict, match='review_feedback_changed'):
        owner.validate_review_corrections(env.records, before, project='alpha')
