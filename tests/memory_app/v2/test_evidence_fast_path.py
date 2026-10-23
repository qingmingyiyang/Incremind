"""Owner and lifecycle controls; native generation is stubbed, never a wire call."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.memory_app.model_config import ModelConfigurationError
from backend.memory_app.structured_generation import AskOutput
from backend.memory_app.v2 import evidence_fast_path as fast
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.search_and_recall.evidence_windows import EvidenceWindow
from core.storage_provider import SQLiteStructuredRecordStore


BODY = 'I polished the copper lens. It was then sealed for transport.'
PUBLISHED = '2026-03-12T00:00:00+00:00'
CLOCK = '2026-10-23T00:00:00+00:00'


def source(*, layer='L0', identity='source-one'):
    # Partial L1/L2 windows are legitimate source-owner evidence, unlike raw@11.
    text = BODY if layer == 'L0' else BODY[:26]
    return {'kind': 'source', 'id': identity, 'layer': layer,
        'scope': WorkScope('local-user', 'alpha'),
        'entry': {'id': identity, 'kind': 'source', 'content': BODY, 'revision': 1},
        'coordinate_space': 'source_content_v1', 'snapshot': {'owner': 'synthetic-owner'},
        'title': 'Lens record', 'href': '', 'match_in': 'content', 'excerpt': text,
        'windows': (EvidenceWindow(0, len(text), text),)}


@pytest.fixture
def lifecycle(monkeypatch):
    rows = [source()]
    plan = {'project_id': 'alpha', 'scope': rows[0]['scope'], 'question': 'What happened to the copper lens?',
        'chosen': rows, 'policy_versions': {'retrieve': '@12', 'compose': '@36'},
        'history': '', 'profile': {}, 'part_context': None, 'budget': 50000,
        'target': {'revision': 1, 'execution_location': 'remote'},
        'answer_output_budget': {'window': 64000, 'reserve': 7000, 'target_revision': 1}}
    facts, calls, abandoned = {}, [], []
    store = SimpleNamespace(get_request=lambda _: {'turn_id': 'unit-turn'},
        get_immutable_payload=lambda _, key: facts.get(key))
    query = SimpleNamespace(records=SimpleNamespace(list=lambda _: [], read=lambda *_: None),
        models=SimpleNamespace(generation_budget_limits=lambda **kw: {'window': 64000, 'reserve': kw['max_tokens']}),
        _validate_ask_sources=lambda _: None, validate_answer_request=lambda *_: None)
    def validate(plan):
        query._validate_ask_sources(plan)
        fast.validate_evidence_plan(query, plan)
    query.validate_ask_plan = validate
    state = SimpleNamespace(query=query, plan=plan, collected={'candidates': rows, 'excluded_sources': []},
        route={}, calls=calls, facts=facts, abandoned=abandoned, after=lambda: None,
        error=None, reject=False, observation={'receipt_ids': ['wire-one'],
            'observations': [{'input_tokens': 17, 'output_tokens': 9}], 'complete': True})
    def generate(models, prepared, **kwargs):
        kwargs['validate_current']()
        calls.append({'messages': deepcopy(prepared), **kwargs})
        state.after()
        if state.error is not None:
            raise state.error
        unit = json.loads(prepared[-1]['content'])['evidence_catalog']['units'][0]
        payload = {'requirements': [{'text': 'What happened to the lens?',
            'status': 'missing' if state.reject else 'supported', 'statement_indices': [1]}],
            'statements': [{'kind': 'fact', 'text': 'The lens was polished.',
                'evidence': [{'unit_id': unit['id'], 'quote': unit['text'], 'role': 'support'}], 'date_refs': []}],
            'issues': []}
        output = kwargs['response_model'].model_validate(payload)
        meta = {'usage': {'input_tokens': 17, 'output_tokens': 9}}
        facts['answer-model-input-' + fast.INVOCATION] = ('input-ref', {'purpose': 'primary',
            'messages': deepcopy(prepared), 'response_schema': kwargs['response_model'].model_json_schema()})
        facts['answer-model-result-' + fast.INVOCATION] = ('result-ref',
            {'output': output.model_dump(), 'metadata': meta})
        return output, meta
    monkeypatch.setattr(fast, 'generate_answer', generate)
    monkeypatch.setattr(fast, 'answer_observation', lambda _: deepcopy(state.observation))
    monkeypatch.setattr(fast, 'abandon_answer_model', abandoned.append)
    monkeypatch.setattr(fast, 'now', lambda: CLOCK)
    token = fast.ACTIVE_ANSWER.set(({'turn_id': 'unit-turn'}, store, query, {}))
    try:
        yield state
    finally:
        fast.ACTIVE_ANSWER.reset(token)


def run(state):
    return asyncio.run(fast.attempt(state.query, state.plan, state.collected, state.route))


@pytest.mark.parametrize('layer', ['L0', 'L1', 'L2'])
def test_normal_owner_windows_do_not_require_raw_capture_or_whole_sources(lifecycle, layer):
    lifecycle.plan['chosen'] = [source(layer=layer)]
    basis = fast.source_basis(lifecycle.query, lifecycle.plan)
    assert basis[0]['layer'] == layer
    assert basis[0]['qualification'] == {'owner': 'workspace_query_sources'}
    assert json.loads(json.dumps(basis)) == basis


def test_one_accepted_proposal_is_revalidated_and_published_without_generation(lifecycle):
    result = run(lifecycle)
    assert result['accepted'] and result['diagnostics'] == []
    assert len(lifecycle.calls) == 1
    assert lifecycle.calls[0]['invocation_key'] == fast.INVOCATION
    assert lifecycle.calls[0]['max_tokens'] == 4096
    assert lifecycle.route['reason'] == 'evidence_answer_accepted'
    assert lifecycle.route['generation_physical_attempts'] == 1
    output, sent, meta = fast.frozen_result(lifecycle.query, lifecycle.plan)
    assert isinstance(output, AskOutput) and output.citations == [1]
    assert sent == lifecycle.calls[0]['messages'] and meta['usage'] == result['usage']
    assert len(lifecycle.calls) == 1


def test_rejection_keeps_real_usage_and_only_structural_hints(lifecycle):
    lifecycle.reject = True
    result = run(lifecycle)
    assert not result['accepted'] and len(lifecycle.calls) == 1
    assert 'evidence_answer_proof' not in lifecycle.plan
    assert result['usage'] == {'input_tokens': 17, 'output_tokens': 9}
    assert result['receipt_ids'] == ['wire-one']
    assert result['diagnostics'] == [{'kind': 'requirement', 'requirement_indices': [1], 'status': 'missing'}]
    assert 'The lens was polished' not in json.dumps(result['diagnostics'])


def test_failed_transport_retains_all_attempts_and_unknown_usage(lifecycle):
    lifecycle.error = ModelConfigurationError('synthetic_transport_failure')
    lifecycle.observation = {'receipt_ids': ['failed-one', 'failed-two', 'inflight-dispatch'],
        'observations': [{'input_tokens': 5}, {}], 'complete': False}
    result = run(lifecycle)
    assert not result['accepted'] and len(lifecycle.calls) == 1
    assert result['usage'] == {'input_tokens': 5} and not result['usage_complete']
    assert result['receipt_ids'] == lifecycle.observation['receipt_ids']
    assert lifecycle.route['generation_physical_attempts'] == 3
    assert lifecycle.abandoned == [fast.INVOCATION]


@pytest.mark.parametrize('change', ['content', 'authorization'])
def test_source_change_aborts_instead_of_falling_back(lifecycle, change):
    def changed():
        if change == 'content':
            lifecycle.plan['chosen'][0]['entry']['content'] += ' Changed.'
        else:
            def deny(_):
                raise RecognitionError('synthetic_source_revoked')
            lifecycle.query._validate_ask_sources = deny
    lifecycle.after = changed
    with pytest.raises(RecognitionError):
        run(lifecycle)
    assert len(lifecycle.calls) == 1 and 'evidence_answer_proof' not in lifecycle.plan


@pytest.mark.parametrize('risk', ['stale', 'refutes', 'incomplete', 'expired'])
def test_known_unselected_risk_prevents_dispatch(lifecycle, risk):
    omitted = source(identity='not-selected')
    if risk == 'stale':
        omitted['stale'] = True
    elif risk == 'refutes':
        omitted['link_kind'] = 'refutes'
    elif risk == 'expired':
        omitted['validity'] = {'valid_from': PUBLISHED, 'valid_until': '2026-04-01T00:00:00+00:00'}
    else:
        lifecycle.collected['excluded_sources'] = [{'reason': 'recognition_source_evidence_incomplete'}]
    lifecycle.collected['candidates'] = [*lifecycle.collected['candidates'], omitted]
    result = run(lifecycle)
    assert not result['accepted'] and not lifecycle.calls
    assert result['diagnostics'] and result['usage_complete']


def test_input_budget_prevents_dispatch(lifecycle):
    lifecycle.plan['budget'] = 1
    assert not run(lifecycle)['accepted'] and lifecycle.calls == []


@pytest.mark.parametrize('current_request', [True, False])
def test_failure_trace_uses_only_current_input_and_model_request(lifecycle, current_request):
    lifecycle.error = ModelConfigurationError('model_request_failed: check endpoint, model and credentials')
    fast.ACTIVE_ANSWER.get()[3][fast.INVOCATION] = SimpleNamespace(model_request_id='current-request')
    lifecycle.facts['answer-model-input-' + fast.INVOCATION] = ('current-input', {})
    lifecycle.facts['answer-model-failure-' + fast.INVOCATION] = ('failure-ref', {
        'version': 'answer-model-failure-v1', 'turn_id': 'unit-turn', 'invocation': fast.INVOCATION,
        'model_request_id': 'current-request' if current_request else 'previous-request',
        'input_ref': 'current-input', 'category': 'model', 'code': 'model_request_failed',
        'completed_wire': None, 'usage_complete': False})
    assert not run(lifecycle)['accepted']
    assert lifecycle.route['reason'] == 'evidence_generation_failed'
    assert ('generation_failure' in lifecycle.route) is current_request


def test_unavailable_failure_diagnostic_does_not_prevent_fallback(lifecycle):
    lifecycle.error = ModelConfigurationError('model_request_failed: check endpoint, model and credentials')
    def unavailable(*_):
        raise OSError('synthetic unavailable diagnostic')
    fast.ACTIVE_ANSWER.get()[1].get_immutable_payload = unavailable
    assert not run(lifecycle)['accepted']
    assert lifecycle.route['reason'] == 'evidence_generation_failed'
    assert 'generation_failure' not in lifecycle.route


@pytest.mark.parametrize('limits', [None, {'window': 64000, 'reserve': -1}, {'window': 64000, 'reserve': 64000}])
def test_unavailable_current_gateway_budget_prevents_dispatch(lifecycle, limits):
    lifecycle.query.models.generation_budget_limits = lambda **_: limits
    assert not run(lifecycle)['accepted'] and lifecycle.calls == []


def test_bad_present_marker_fails_before_reading_sources(lifecycle):
    lifecycle.plan['evidence_answer_proof'] = None
    def forbidden(_):
        raise AssertionError('a malformed marker must not reach the owner')
    lifecycle.query._validate_ask_sources = forbidden
    with pytest.raises(RecognitionConflict):
        fast.validate_evidence_plan(lifecycle.query, lifecycle.plan)
    assert not lifecycle.calls


def test_frozen_result_rejects_changed_schema(lifecycle):
    assert run(lifecycle)['accepted']
    lifecycle.facts['answer-model-input-' + fast.INVOCATION][1]['response_schema'] = {}
    with pytest.raises(RecognitionConflict):
        fast.frozen_result(lifecycle.query, lifecycle.plan)
    assert len(lifecycle.calls) == 1


@pytest.fixture
def published(tmp_path, monkeypatch):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service = RecognitionService(records)
    scope = WorkScope('local-user', 'alpha')
    with patch('backend.recognition.service._now', return_value=PUBLISHED):
        original = service.stage_experience(scope=scope, content=BODY)
        candidate = service.propose(scope=scope, content=BODY, source_experience_ids=[original])
        current = service.publish(scope=scope, candidate_id=candidate.id,
            expected_revision=candidate.revision, reviewer='local-user')
    recognition = service.get_recognition(scope=scope, recognition_id=current.id)
    row = {'kind': 'recognition', 'layer': 'L3', 'id': current.id, 'scope': scope,
        'entry': recognition.retrieval_projection(),
        'snapshot': {}, 'excerpt': BODY, 'windows': [EvidenceWindow(0, len(BODY), BODY)]}
    query = SimpleNamespace(records=records, service=service, _validate_ask_sources=lambda _: None)
    monkeypatch.setattr(fast, 'now', lambda: CLOCK)
    return SimpleNamespace(query=query, row=row, records=records, scope=scope)


def test_l3_basis_comes_from_real_published_confirmation_and_provenance(published):
    basis = fast.source_basis(published.query, {'project_id': 'alpha', 'chosen': [published.row]})
    qualification = basis[0]['qualification']
    assert qualification['confirmation']['valid_from'] == PUBLISHED
    assert qualification['validity']['valid_from'] == PUBLISHED
    assert qualification['source_evidence']
    assert 'coordinate_space' not in published.row
    assert basis[0]['coordinate_space'] == 'recognition_content_v1'


def test_l3_without_confirmation_is_not_promoted_from_its_projection(published, monkeypatch):
    monkeypatch.setattr(fast, 'from_confirmation', lambda *_: None)
    with pytest.raises(ValueError, match='not_current'):
        fast.source_basis(published.query, {'project_id': 'alpha', 'chosen': [published.row]})


@pytest.mark.parametrize('relation', ['refutes', 'supersedes'])
def test_registered_unselected_conflict_or_supersession_cannot_hide_in_budget(published, relation):
    # A stale sidecar must not mask a registered supersedes edge.
    with published.records.begin() as tx:
        marker = tx.read('v2_insight_validity', published.row['id'])
        tx.put('v2_insight_validity', published.row['id'],
            {'valid_from': PUBLISHED, 'valid_until': None, 'superseded_by': None},
            expected_revision=marker.revision if marker else 0)
        tx.put('recognition_relations', 'registered-edge', {'scope': {'user_id': 'local-user', 'project_id': 'alpha'},
            'relation': relation, 'from_id': 'other-real-record', 'to_id': published.row['id'],
            'created_at': '2026-04-01T00:00:00+00:00'}, expected_revision=0)
        tx.commit()
    plan = {'project_id': 'alpha', 'chosen': [source()], 'evidence_retrieved_recognitions': [
        {'id': published.row['id'], 'scope': {'user_id': 'local-user', 'project_id': 'alpha'}}]}
    with pytest.raises(ValueError, match='registered_conflict|not_current'):
        fast.source_basis(published.query, plan)
