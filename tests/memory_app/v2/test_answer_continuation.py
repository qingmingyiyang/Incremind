"""Same-Turn continuation retains closed data and current source authority."""
import pytest
import json
import sqlite3

from fastapi.testclient import TestClient

from backend.recognition import WorkScope
from core.search_and_recall.evidence_windows import EvidenceWindow
from core.storage_provider import SQLiteStructuredRecord
from tests.memory_app.v2.test_partial_answer import env, interrupted_app
from tests.memory_app.v2.test_workbench_stream import events


def _partial(http):
    response = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
        headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'authority-partial'})
    assert events(response)[-1][0] == 'done', response.text
    return events(response)[-1][1]['turn']['id']


def test_paused_descriptor_references_lease_fenced_immutable_facts(env):
    app, _, _, calls, closed, _ = interrupted_app(env, continuation=True)
    with TestClient(app) as http:
        identity = _partial(http)
        row = env.records.read('v2_answer_continuations', identity)
        assert row.payload['state'] == 'paused'
        assert 'capsule' not in row.payload
        store = app.state.ai_turn_store
        plan_ref = row.payload['plan_ref']
        plan = store.get(plan_ref)
        assert plan['turn_id'] == identity and plan['project_id'] == 'alpha' and plan['question'] == 'alpha?'
        assert plan['request'] == store.get_request(identity)
        assert store.get_immutable_payload(identity, 'answer-continuation-plan-v1') == (plan_ref, plan)
        proof = store.get(row.payload['close_refs'][0])
        assert proof['lease'] == plan['lease']
        bundles = [event for event in store.events_after(identity) if event['type'] == 'tool.outcome.recorded']
        assert len(bundles) == 1
        assert set(bundles[0]['data']['evidence_refs']) == {plan_ref, row.payload['close_refs'][0]}
        assert bundles[0]['correlation']['tool_call_id'] == proof['invocation_id'] == row.payload['invocation_id']
        assert store.get(bundles[0]['data']['payload_ref'])['status'] == 'failed'
        with sqlite3.connect(env.root / '.rebuild-data/ai-turns.sqlite3') as database:
            assert database.execute('SELECT state FROM effect WHERE operation_id=?',
                (proof['attempts'][0]['attempt_id'],)).fetchone() == ('UNKNOWN',)
    assert len(calls) == 1 and closed == [True]


@pytest.mark.parametrize('change', ['missing_close', 'boolean_close', 'unknown_close', 'plan_ref',
                                   'missing_terminal', 'lease', 'inflight', 'bundle'])
def test_corrupt_or_unwitnessed_attempt_never_authorizes_continuation(env, change):
    app, _, _, calls, closed, _ = interrupted_app(env, continuation=True)
    with TestClient(app) as http:
        identity = _partial(http)
        row = env.records.read('v2_answer_continuations', identity)
        if change in {'missing_close', 'boolean_close', 'unknown_close', 'plan_ref'}:
            payload = dict(row.payload)
            if change == 'plan_ref':
                payload['plan_ref'] = payload['close_refs'][0]
            else:
                payload['close_refs'] = {'missing_close': [], 'boolean_close': [True],
                                        'unknown_close': ['crp://default/payloads/missing']}[change]
            with env.records.begin() as tx:
                tx.put(row.collection, identity, payload, expected_revision=row.revision)
                tx.commit()
        else:
            with sqlite3.connect(env.root / '.rebuild-data/ai-turns.sqlite3') as database:
                if change == 'missing_terminal':
                    database.execute("UPDATE ai_model_attempt_reservations SET status='committed',terminal_receipt_ref=NULL,terminal_status=NULL WHERE turn_id=?", (identity,))
                elif change == 'lease':
                    database.execute('UPDATE ai_model_attempt_reservations SET lease_generation=lease_generation+1 WHERE turn_id=?', (identity,))
                elif change == 'inflight':
                    database.execute("UPDATE effect SET state='INFLIGHT' WHERE operation_id IN (SELECT attempt_id FROM ai_model_attempt_reservations WHERE turn_id=?)", (identity,))
                else:
                    saved = database.execute("SELECT sequence,event_json FROM ai_turn_events WHERE turn_id=?", (identity,)).fetchall()
                    for sequence, raw in saved:
                        event = json.loads(raw)
                        if event['type'] == 'tool.outcome.recorded':
                            event['data']['evidence_refs'] = []
                            database.execute('UPDATE ai_turn_events SET event_json=? WHERE turn_id=? AND sequence=?',
                                (json.dumps(event), identity, sequence))
        response = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'corrupt-continue'})
        assert response.status_code == 409, response.text
        assert app.state.ai_turn_store.get_action('corrupt-continue') is None
    assert len(calls) == 1 and closed == [True]


@pytest.mark.parametrize('change', ['private', 'model', 'forgotten', 'revoked'])
def test_continue_revalidates_current_sources_and_model_before_action_or_wire(env, change):
    app, _, model, calls, closed, _ = interrupted_app(env, continuation=True)
    with TestClient(app) as http:
        identity = _partial(http)
        before = tuple(app.state.ai_turn_store.events_after(identity))
        recognition = env.service.retrieval_entries(scope=WorkScope('local-user', 'alpha'))[0]
        if change == 'private':
            from backend.memory_app.v2.privacy import set_private_project
            set_private_project(env.records, 'alpha', True, 0)
        elif change == 'model':
            model.update('generation', {'model': 'changed-model', 'expected_revision': 1})
        elif change == 'forgotten':
            response = http.post(f"/api/v2/library/insights/{recognition['id']}/forget",
                json={'project_id': 'alpha', 'forgotten': True})
            assert response.status_code == 200, response.text
        else:
            env.service.revoke(scope=WorkScope('local-user', 'alpha'), recognition_id=recognition['id'],
                expected_revision=recognition['revision'], reason='synthetic user revocation')
        result = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'changed-continue'})
        assert result.status_code == 409, result.text
        assert tuple(app.state.ai_turn_store.events_after(identity)) == before
        assert app.state.ai_turn_store.get_action('changed-continue') is None
        assert env.records.read('v2_answer_continuations', identity).payload['state'] == 'paused'
    assert len(calls) == 1 and closed == [True]


@pytest.mark.parametrize('action_type', ['approve', 'cancel'])
def test_internal_pause_grants_no_approval_and_cancel_prevents_resume(env, action_type):
    from core.ai_kernel import validate_turn_action
    from uuid import uuid4
    from backend.memory_app.workspace_contracts import _now
    app, _, _, calls, closed, _ = interrupted_app(env, continuation=True)
    with TestClient(app) as http:
        identity = _partial(http)
        runtime, store = app.state.ai_runtime, app.state.ai_turn_store
        before = tuple(store.events_after(identity))
        action = validate_turn_action({'schema_version': '1.0.0', 'action_id': 'action-' + uuid4().hex,
            'turn_id': identity, 'type': action_type,
            'target_event_id': before[-1]['event_id'] if action_type == 'approve' else None,
            'reason': 'synthetic explicit action',
            'actor': 'user', 'expected_sequence': len(before), 'idempotency_key': 'manual-' + action_type,
            'created_at': _now()})
        if action_type == 'approve':
            with pytest.raises(ValueError):
                runtime.apply_action(action)
            assert tuple(store.events_after(identity)) == before
            assert runtime.receipt_for(identity).status == 'waiting_approval'
        else:
            receipt = app.state.ai_turn_runner.apply_action_and_wait(action)
            assert receipt.status == 'cancelled'
            response = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
                headers={'Idempotency-Key': 'after-cancel'})
            assert response.status_code == 409, response.text
            assert store.get_action('after-cancel') is None
    assert len(calls) == 1 and closed == [True]


def test_invalid_joined_citations_never_publish_answer_or_record_usage_and_history(env):
    from backend.memory_app.v2.followup import read_history
    app, domains, _, calls, closed, _ = interrupted_app(env, continuation=True, invalid_citations=True)
    with TestClient(app) as http:
        identity = _partial(http)
        turn = env.records.read('v2_turns', identity)
        before = {name: env.records.list(name) for name in ('v2_usage_insight', 'v2_usage_document')}
        response = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'invalid-cite-continue'})
        assert response.status_code == 502, response.text
        assert app.state.ai_runtime.receipt_for(identity).status == 'failed'
        assert app.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2') is None
        assert env.records.read('v2_turns', identity) == turn
        assert {name: env.records.list(name) for name in before} == before
        assert read_history(env.records, 'alpha', turn.payload['thread_id'], '下一问', query=domains.query)['turns'] == []
    assert len(calls) == 2 and closed == [True, True]


def test_true_new_application_lifespan_and_get_never_auto_continue(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.recognition import RecognitionService
    from core.document_engine import SQLiteDocumentRepository
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_bytes((Path(__file__).parents[3] / 'config/settings.toml.example').read_bytes())
    records, namespace = resolve_recognition_document_store(tmp_path)
    assert namespace == 'default'
    state = SimpleNamespace(root=tmp_path, records=records, documents=SQLiteDocumentRepository(records),
        service=RecognitionService(records))
    first, _, model, calls, closed, _ = interrupted_app(state, continuation=True)
    with TestClient(first) as http:
        identity = _partial(http)
        turn = records.read('v2_turns', identity)
    first.state.ai_turn_runner.shutdown()
    from backend.memory_app.app import create_app
    restarted = create_app(runtime_root=tmp_path, model_configuration=model)
    with TestClient(restarted) as http:
        response = http.get(f"/api/v2/workbench/threads/{turn.payload['thread_id']}?project_id=alpha")
        assert response.status_code == 200, response.text
        assert response.json()['turns'][0]['id'] == identity
        assert response.json()['turns'][0]['receipt']['ask']['partial'] == turn.payload['receipt']['ask']['partial']
        assert len(calls) == 1
    assert len(calls) == 1 and closed == [True]


def test_continuation_codec_round_trips_only_known_frozen_fact_types():
    from backend.memory_app.kernel.answer_continuations import encode, decode
    row = SQLiteStructuredRecord('v2_document_recall', 'doc-one', {'state': 'faded'}, 2)
    value = {'scope': WorkScope('local-user', 'alpha'), 'chosen': [{'windows': (EvidenceWindow(1, 3, '正文'),)}],
             'preferences': {('v2_document_recall', 'doc-one'): row}, 'model': {'revision': 2}, 'nothing': None}
    assert decode(encode(value)) == value
    with pytest.raises(ValueError):
        encode(lambda: 'permission')
    with pytest.raises(ValueError):
        encode(float('nan'))


@pytest.mark.parametrize('value', [
    {'tag': 'class', 'module': 'arbitrary', 'name': 'Authority'},
    {'tag': 'scope', 'values': ['local-user', 'alpha', 'unexpected']},
    {'tag': 'window', 'values': [1, 0, 'forged']},
    {'tag': 'record', 'values': ['v2_document_recall', 'doc-one', {}, True]},
])
def test_continuation_codec_rejects_unknown_or_corrupt_constructor_data(value):
    from backend.memory_app.kernel.answer_continuations import decode
    with pytest.raises(ValueError):
        decode(value)


def test_actual_closed_provider_issues_a_non_json_witness_from_governed_body_interruption(tmp_path, monkeypatch):
    from backend.memory_app.model_config import ModelConfiguration
    from backend.shared.llm.model_transport import ModelInterrupted
    from tests.memory_app.test_governed_retry import governed_runtime
    caught = []
    original = ModelConfiguration.complete_governed
    def observe(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except ModelInterrupted as error:
            caught.append(error)
            raise
    monkeypatch.setattr(ModelConfiguration, 'complete_governed', observe)
    state = governed_runtime(tmp_path, streaming=True, eof_phase='body')
    assert state['receipt'].status == 'failed'
    assert len(state['calls']) == len(state['attempts']) == 1
    assert all(stream.closed for stream in state['streams'])
    assert len(caught) == 1
    assert caught[0].close_witness is not None
    with pytest.raises(ValueError):
        from backend.memory_app.kernel.answer_continuations import encode
        encode(caught[0].close_witness)
