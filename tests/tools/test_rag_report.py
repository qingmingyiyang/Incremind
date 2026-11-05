"""Synthetic report records isolate attribution; service/proof integration is separate."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import BaseModel


def report_module():
    path = Path(__file__).resolve().parents[2] / 'tools/rag_report.py'
    spec = importlib.util.spec_from_file_location('rag_report_test', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def payload(*identities, evidence=False):
    catalog = {'units': [{'id': index, 'text': f'[{identity}] Synthetic source statement.'}
                         for index, identity in enumerate(identities, 1)]}
    data = {'evidence_catalog': catalog}
    text = json.dumps(data)
    if not evidence:
        text = '资料：\n' + text + '\n\n问题：Synthetic question?'
    return {'purpose': 'primary', 'messages': [{'role': 'user', 'content': text}],
            'response_schema': {'title': 'SyntheticReportOutput', 'type': 'object'}}


def native_refs(model_request):
    return ['routing-snapshot-' + model_request, 'wire-attempt-' + model_request, 'prompt-cache-' + model_request]


def case(tmp_path, frozen, messages, *, retrieve='@12', status=200, unknown_first=False):
    directory = tmp_path / 'synthetic-conversation'
    directory.mkdir()
    question = directory / 'question-1.json'
    request = {'turn_id': 'synthetic-turn', 'desired_outcome': 'project.answer',
               'policy_versions': {'retrieve': retrieve, 'compose': '@36'}}
    attempts, http, wires, events = [], [], [], []
    for number, sent in enumerate(messages, 1):
        model_request = 'model-1' if unknown_first else f'model-{number}'
        usage = None if unknown_first and number == 1 else {
            'input_tokens': 20, 'output_tokens': 20, 'total_tokens': 40}
        attempts.append({'attempt_id': f'attempt-{number}', 'model_request_id': model_request,
            'status': 'terminal', 'terminal_status': 'failed_transport' if usage is None else 'succeeded',
            'dispatch_payload': {'provider_id': 'synthetic-remote'}, 'terminal_receipt': {
                'model_id': 'synthetic-deepseek', 'attempt_id': f'attempt-{number}',
                'model_request_id': model_request, 'usage': usage}})
        events.append({'event_json': json.dumps({'type': 'model.attempt.dispatched',
            'correlation': {'model_request_id': model_request}})})
        if usage is not None:
            events.append({'event_json': json.dumps({'type': 'model.completed',
                'correlation': {'model_request_id': model_request},
                'data': {'receipt_ref': 'model-call-receipt-' + model_request,
                         'evidence_refs': native_refs(model_request), 'model_call_purpose': 'primary'}})})
        completion = {'number': number, 'label': 'synthetic-conversation:1', 'turn_id': 'synthetic-turn'}
        http.append({'completion': completion})
        wires.append(completion)
        (tmp_path / f'messages-{number}.json').write_text(json.dumps(sent), encoding='utf8')
    events.append({'event_json': json.dumps({'type': 'turn.failed' if status >= 400 else 'turn.completed'})})
    row = {'conversation_id': 'synthetic-conversation', 'qa_index': 1, 'http_status': status,
           'seconds': 3.0, 'physical_http': len(attempts), 'source': {'revision': 'synthetic'},
           'body': {'turn': {'receipt': {'ask': {'answer': 'Synthetic final answer.' if status == 200 else None}}}},
           'native': {'turn': [{'turn_id': 'synthetic-turn', 'request_json': json.dumps(request)}],
               'events': events, 'attempts': attempts,
               'immutable': [{'kind': key, 'payload': value} for key, value in frozen.items()]}}
    question.write_text(json.dumps(row), encoding='utf8')
    return question, http, wires


@pytest.fixture
def selectors(monkeypatch):
    from backend.memory_app import answer_proof

    observed = []
    monkeypatch.setattr(answer_proof, 'has_evidence_answer_proof',
                        lambda request, capsule: bool(capsule and capsule.get('accepted')), raising=False)

    def accepted(request, product, sent, received, capsule):
        observed.append((request, product, sent, received, capsule))
        return sent if capsule.get('valid', True) else None

    monkeypatch.setattr(answer_proof, 'select_evidence_answer_input', accepted, raising=False)
    monkeypatch.setattr(answer_proof, 'select_answer_input',
                        lambda request, product, first, result, review, final, capsule=None: review)
    return observed


@pytest.mark.parametrize('retrieve', ['@12', '@13'])
def test_accepted_single_generation_uses_actual_frozen_catalog_and_keeps_unknown_retry_cost(tmp_path, selectors, retrieve):
    report = report_module()
    single = payload('D1:1', 'D1:2', evidence=True)
    frozen = {report.EVIDENCE_INPUT: single, report.EVIDENCE_RESULT: {'output': {},
                  'metadata': {'kernel_receipt_refs': native_refs('model-1')}},
              report.REVIEW_INPUT: payload('D1:99'),
              'answer-continuation-plan-v1': {'accepted': True}, 'product-answer-result-v2': {'answer': 'synthetic'}}
    from backend.shared.llm.message_metadata import _build_json_mode_messages
    actual = _build_json_mode_messages(messages=single['messages'], validation_error=None)
    path, http, wires = case(tmp_path, frozen, [actual, actual], unknown_first=True, retrieve=retrieve)
    reader = report.SourceReader()
    row = report.heldout_question(reader, path, http, wires, '@36')
    assert row['selected'] == {'D1:1', 'D1:2'} and row['input_state'] == 'recorded'
    assert row['status'] == 'answered' and len(row['attempts']) == 2
    assert selectors and selectors[0][2] == single
    usage = report.token_totals([attempt['terminal_receipt']['usage'] or {} for attempt in row['attempts']])
    assert usage['unknown_calls'] == 1 and usage['complete'] is None
    assert usage['recorded'] == {'input_tokens': 20, 'output_tokens': 20, 'total_tokens': 40}
    reader.verify_unchanged()


def test_fallback_uses_only_final_review_instead_of_higher_recall_rejected_proposal(tmp_path, selectors):
    report = report_module()
    rejected, first, review = payload('D1:1', 'D1:2', 'D1:3', evidence=True), payload('D1:8'), payload('D1:4')
    frozen = {report.EVIDENCE_INPUT: rejected, report.EVIDENCE_RESULT: {'output': {}},
              'answer-model-input-answer': first, report.REVIEW_INPUT: review,
              'answer-continuation-plan-v1': {'accepted': False}, 'product-answer-result-v2': {'answer': 'synthetic'}}
    path, http, wires = case(tmp_path, frozen, [rejected['messages'], first['messages'], review['messages']])
    row = report.heldout_question(report.SourceReader(), path, http, wires, '@36')
    assert row['selected'] == {'D1:4'} and row['input_state'] == 'recorded'
    assert len(row['attempts']) == 3 and selectors == []
    assert report.token_totals([item['terminal_receipt']['usage'] for item in row['attempts']])['complete']['total_tokens'] == 120


def test_rejected_proposal_without_review_has_no_final_input_but_retains_cost(tmp_path, selectors):
    report = report_module()
    rejected = payload('D1:1', evidence=True)
    frozen = {report.EVIDENCE_INPUT: rejected, 'answer-continuation-plan-v1': {'accepted': False}}
    path, http, wires = case(tmp_path, frozen, [rejected['messages']], status=502)
    row = report.heldout_question(report.SourceReader(), path, http, wires, '@36')
    assert row['input_state'] == 'not_reached' and row['selected'] == set()
    assert row['status'] == 'failed' and len(row['attempts']) == 1


def test_bad_accepted_proof_cannot_fall_through_to_an_old_review(tmp_path, selectors):
    report = report_module()
    single, review = payload('D1:1', evidence=True), payload('D1:99')
    frozen = {report.EVIDENCE_INPUT: single, report.REVIEW_INPUT: review,
              'answer-continuation-plan-v1': {'accepted': True, 'valid': False}}
    path, http, wires = case(tmp_path, frozen, [single['messages'], review['messages']])
    row = report.heldout_question(report.SourceReader(), path, http, wires, '@36')
    assert row['input_state'] == 'missing' and row['selected'] == set()


@pytest.mark.parametrize('changed', ['http', 'native_receipt'])
def test_single_generation_requires_both_actual_wire_and_native_completion(tmp_path, selectors, changed):
    report = report_module()
    single = payload('D1:1', evidence=True)
    frozen = {report.EVIDENCE_INPUT: single, report.EVIDENCE_RESULT: {'output': {},
                  'metadata': {'kernel_receipt_refs': ['wrong-receipt'] if changed == 'native_receipt'
                               else native_refs('model-1')}},
              'answer-continuation-plan-v1': {'accepted': True}}
    actual = payload('D1:2', evidence=True)['messages'] if changed == 'http' else single['messages']
    path, http, wires = case(tmp_path, frozen, [actual])
    if changed == 'native_receipt':
        with pytest.raises(ValueError, match='单轮冻结结果缺少唯一原生完成回执'):
            report.heldout_question(report.SourceReader(), path, http, wires, '@36')
    else:
        row = report.heldout_question(report.SourceReader(), path, http, wires, '@36')
        assert row['input_state'] == 'missing' and row['selected'] == set()
        assert len(row['attempts']) == 1


def test_legacy_report_keeps_last_dispatched_answer_input_boundary(tmp_path):
    report = report_module()
    first, review = payload('D1:1', 'D1:2'), payload('D1:3')
    frozen = {'answer-model-input-answer': first, report.REVIEW_INPUT: review}
    path, http, wires = case(tmp_path, frozen, [first['messages'], review['messages']], retrieve='@9', status=502)
    row = report.heldout_question(report.SourceReader(), path, http, wires, '@36')
    assert row['input_state'] == 'recorded' and row['selected'] == {'D1:3'}
    assert row['status'] == 'failed'


def test_prompt_fallback_matches_the_actual_single_generation_schema():
    report = report_module()

    class SyntheticReportOutput(BaseModel):
        value: str

    frozen = payload('D1:1', evidence=True)
    frozen['response_schema'] = SyntheticReportOutput.model_json_schema()
    from backend.shared.llm.message_metadata import _build_prompt_fallback_messages
    actual = _build_prompt_fallback_messages(messages=frozen['messages'], response_model=SyntheticReportOutput,
                                              validation_error='synthetic_validation_error')
    assert report.input_messages_match(actual, frozen, '@36', response_model=SyntheticReportOutput)
    tampered = deepcopy(actual)
    tampered[1]['content'] = tampered[1]['content'].replace('D1:1', 'D1:2')
    assert report.input_messages_match(tampered, frozen, '@36', response_model=SyntheticReportOutput) is False
