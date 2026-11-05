from copy import deepcopy

import pytest

from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input, select_answer_input
from backend.memory_app.kernel.answer_continuations import encode
from backend.memory_app.kernel.answer_contract import answer_contract_scope, _BUDGET
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.answer_evidence import (
    answer_response_model, build_evidence_catalog, build_partition_messages, final_response_model,
    first_fact_partition, render_grounded_answer, reviewed_scope_answer,
)
from backend.memory_app.v2.policies import ACTIVE, get
from backend.memory_app.v2.workbench_receipts import _ask_receipt
from backend.memory_app.workspace_query import WorkspaceQuery
from backend.recognition import RecognitionService, WorkScope
from backend.security.secrets import InMemorySecretStore
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.document_engine import SQLiteDocumentRepository
from core.search_and_recall.evidence_windows import EvidenceWindow
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore
from tests.memory_app.v2.test_raw_answer_proof import frozen as raw_frozen


@pytest.fixture
def material():
    content = 'Session date: 2023-08-23\n[D1:1] Maya: I painted this self-portrait last week.\n'
    chosen = [{'id': 'painting', 'kind': 'source', 'layer': 'L0', 'title': 'Session 2023-08-23',
        'excerpt': content, 'coordinate_space': 'source_content_v1',
        'entry': {'id': 'painting', 'source_id': 'painting', 'content': content, 'revision': 1},
        'windows': (EvidenceWindow(0, len(content), content),)}]
    catalog = build_evidence_catalog(chosen, compose_version='@37')
    quote = {'unit_id': catalog['units'][1]['id'], 'quote': 'I painted this self-portrait last week.', 'role': 'support'}
    fact = {'kind': 'fact', 'text': 'Maya painted a self-portrait.', 'evidence': [quote], 'date_refs': []}
    first = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Requested painting.'}}
    final = {'statements': [{**deepcopy(fact), 'first_fact_uses': [
        {'fact_index': 1, 'action': 'adopt', 'reason': 'Same source and proposition.'}]},
        {**deepcopy(fact), 'text': '😀𠮷她在2023年8月23日画了这幅画。她在当天完成了作品。', 'first_fact_uses': []}],
        'unused': {'scope': 'all_unreferenced', 'reason': 'Other units not used.'}, 'uncertain': [], 'excluded_facts': []}
    return chosen, catalog, first, final


def publish(material, *, version='@37', maximum=7000):
    chosen, catalog, first, final = material
    value = reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What did Maya paint?', compose_version=version)
    metadata = {}
    answer = render_grounded_answer(value, catalog, first_fact_partition(first, catalog, compose_version=version),
        max_output_tokens=maximum, chosen=chosen, _on_publication=metadata.update,
        **get('compose', version=version)(None, operation='grounded_render', question='What did Maya paint?'))
    return answer, metadata


def test_marked_sentences_preserve_original_claims_and_unicode_offsets(material):
    before = deepcopy(material)
    answer, metadata = publish(material)
    assert answer.answer == ('Maya painted a self-portrait. [1]\n'
        '😀𠮷她在2023年8月23日画了这幅画。【未核实】她在当天完成了作品。【未核实】 [1]')
    assert answer.citations == [1] and metadata['unverified_claims'] == 1
    assert len(metadata['unverified_claim_spans']) == 2
    for span in metadata['unverified_claim_spans']:
        assert answer.answer[span['start']:span['end']] == '【未核实】'
        assert span['claim'] == 2 and span['reason_codes'] == ['grounded_calendar_provenance_invalid']
    assert material == before
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        publish(material, version='@34')


def test_no_grounded_positive_claim_publishes_only_unknown_with_read_citations(material):
    material[3]['statements'].pop(0)
    material[3]['statements'][0]['first_fact_uses'] = [{'fact_index': 1, 'action': 'correct', 'reason': 'Changed claim.'}]
    answer, metadata = publish(material)
    assert answer.answer == '没找到足够依据\n[1]' and answer.citations == [1]
    assert metadata == {'unverified_claims': 0, 'unverified_claim_spans': []}


def test_context_only_support_and_diagnosed_adoption_are_marked(material):
    material[2]['facts'][0]['text'] = 'Maya painted on 2023-08-23.'
    material[3]['statements'][0]['evidence'][0]['role'] = 'context'
    material[3]['statements'][1]['text'] = 'Maya described her self-portrait.'
    answer, metadata = publish(material)
    assert answer.answer.startswith('Maya painted a self-portrait.【未核实】')
    assert metadata['unverified_claims'] == 1
    assert metadata['unverified_claim_spans'][0]['reason_codes'] == [
        'first_fact_adoption_quote_missing', 'grounded_statement_without_support', 'invalid_first_fact_requires_correction']


@pytest.mark.parametrize('damage', ['quote', 'unit', 'ref', 'ref_sentence', 'position', 'coverage', 'reason', 'schema', 'coordinate', 'budget'])
def test_hard_boundaries_continue_to_reject(material, damage):
    chosen, catalog, first, final = material
    target = final['statements'][1]
    if damage == 'quote':
        target['evidence'][0]['quote'] = 'Not in the actual source.'
    elif damage == 'unit':
        target['evidence'][0]['unit_id'] = 999
    elif damage == 'ref':
        target['date_refs'] = [999]
    elif damage == 'ref_sentence':
        target['date_refs'] = [1]
        target['evidence'][0]['quote'] = 'this self-portrait'
    elif damage == 'position':
        target['first_fact_uses'] = [{'fact_index': 99, 'action': 'correct', 'reason': 'Invalid position.'}]
    elif damage == 'coverage':
        final['statements'][0]['first_fact_uses'] = []
    elif damage == 'reason':
        final['statements'][0]['first_fact_uses'][0]['reason'] = ''
    elif damage == 'schema':
        target['unverified'] = True
    elif damage == 'coordinate':
        catalog['units'][1]['span'][0] += 1
    with pytest.raises(ValueError):
        publish(material, maximum=1 if damage == 'budget' else 7000)


def test_valid_old_output_and_both_model_inputs_are_unchanged(material):
    active = deepcopy(ACTIVE)
    chosen, catalog, first, final = material
    final['statements'].pop()
    old, old_metadata = publish(material, version='@34')
    new, new_metadata = publish(material)
    assert new == old and old_metadata == {}
    assert new_metadata == {'unverified_claims': 0, 'unverified_claim_spans': []}
    assert ACTIVE == active
    assert answer_response_model('@37').model_json_schema() == answer_response_model('@34').model_json_schema()
    assert final_response_model('@37', 'What?').model_json_schema() == final_response_model('@34', 'What?').model_json_schema()
    for arguments in ({}, {'first_output': first}):
        assert build_partition_messages(chosen, 'What?', compose_version='@37', **arguments) == build_partition_messages(chosen, 'What?', compose_version='@34', **arguments)
    assert catalog == build_evidence_catalog(chosen, compose_version='@34')


def frozen_proof(material):
    chosen, _, first, final = material
    question = 'What did Maya paint?'
    request = {'turn_id': 'marked-turn', 'session_id': 'marked-session', 'idempotency_key': 'marked-key',
        'operation_id': 'answer-marked-turn', 'desired_outcome': 'project.answer',
        'scope': {'project_id': 'alpha'}, 'input': {'text': question},
        'policy_versions': {'compose': '@37', 'retrieve': '@9'}}
    plan = {'project_id': 'alpha', 'scope': WorkScope('local-user', 'alpha'), 'question': question,
        'policy_versions': request['policy_versions'], 'chosen': chosen, 'history': '', 'profile': {},
        'budget': 193000, 'target': {'revision': 1, 'execution_location': 'remote'},
        'answer_output_budget': {'window': 200000, 'reserve': 7000, 'target_revision': 1}}
    capsule = {'request': request, 'turn_id': request['turn_id'], 'project_id': 'alpha', 'question': question,
        'capsule': encode({'plan': plan, 'guards': {}, 'history': {'text': '', 'turns': []}, 'preview_lifetime': 60.0})}
    first_input = {'purpose': 'primary', 'messages': build_partition_messages(chosen, question, compose_version='@37'),
        'response_schema': answer_response_model('@37').model_json_schema()}
    final_input = {'purpose': 'primary', 'messages': build_partition_messages(chosen, question, compose_version='@37', first_output=first),
        'response_schema': final_response_model('@37', question).model_json_schema()}
    answer, metadata = publish(material)
    product = {'answer': answer.answer, 'sources': [{'number': value} for value in answer.citations], **metadata}
    return request, product, first_input, {'output': first}, final_input, {'output': final}, capsule


def test_sqlite_frozen_proof_reread_and_mark_tampering(material, tmp_path):
    parts = frozen_proof(material)
    request, product, first_input, first_output, final_input, final_output, capsule = parts
    assert select_answer_input(*parts) is final_input
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    assert store.claim_turn(request) == ('marked-turn', True)
    for kind, value in [('answer-model-input-answer', first_input), ('answer-model-result-answer', first_output),
            ('answer-model-input-' + REVIEW_KEY, final_input), ('answer-model-result-' + REVIEW_KEY, final_output),
            ('answer-continuation-plan-v1', capsule), ('product-answer-result-v2', product)]:
        store.get_or_create_immutable_payload('marked-turn', kind, value)
    reopened = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    assert read_answer_input(reopened, 'marked-turn') == reopened.get_immutable_payload('marked-turn', 'answer-model-input-' + REVIEW_KEY)
    for key in ('answer', 'unverified_claims', 'unverified_claim_spans'):
        damaged = deepcopy(product)
        damaged[key] = '' if key == 'answer' else False if key == 'unverified_claims' else []
        assert select_answer_input(request, damaged, first_input, first_output, final_input, final_output, capsule) is None
    for key in ('start', 'end', 'claim'):
        damaged = deepcopy(product)
        damaged['unverified_claim_spans'][0][key] = float(damaged['unverified_claim_spans'][0][key])
        assert select_answer_input(request, damaged, first_input, first_output, final_input, final_output, capsule) is None
    altered_schema = deepcopy(final_input)
    altered_schema['response_schema']['title'] = 'UnboundSchema'
    assert select_answer_input(request, product, first_input, first_output, altered_schema, final_output, capsule) is None
    changed_request = deepcopy(request)
    changed_request['policy_versions']['compose'] = '@36'
    assert select_answer_input(changed_request, product, first_input, first_output, final_input, final_output, capsule) is None


def test_product_and_egress_receipts_keep_counts(material, tmp_path):
    answer, metadata = publish(material)
    chosen = material[0]
    plan = {'trace': []}
    result = {'answer': answer.answer, 'sources': [{'number': 1, 'title': chosen[0]['title'],
        'excerpt': chosen[0]['excerpt'], 'coordinate_space': 'source_content_v1', 'windows': []}], **metadata}
    receipt = _ask_receipt(plan, chosen, result, 'preview')
    assert receipt['unverified_claims'] == 1 and receipt['unverified_claim_spans'] == metadata['unverified_claim_spans']
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('workspace_ask_receipts', 'preview', {'status': 'running'}, expected_revision=0)
        tx.commit()
    query = WorkspaceQuery(records, SQLiteDocumentRepository(records), JsonObjectStore(tmp_path / 'sources'),
        ModelConfiguration(records, tmp_path, InMemorySecretStore()), RecognitionService(records))
    query.mark_ask_receipt('preview', 'completed', usage={'total_tokens': 10}, unverified_claims=1)
    saved = records.read('workspace_ask_receipts', 'preview')
    assert saved.payload['unverified_claims'] == 1 and saved.payload['usage'] == {'total_tokens': 10}


def test_new_policy_keeps_real_answer_handle_limits_and_frozen_version():
    from backend.memory_app.v2.policies import override
    from backend.memory_app.v2.policies.pipelines import versions_for_turn
    with override(compose='@37'):
        assert versions_for_turn('project.answer')['compose'] == '@37'
    for retrieve, expected in [('@3', 4), ('@9', 9)]:
        request = {'desired_outcome': 'project.answer', 'policy_versions': {'compose': '@37', 'retrieve': retrieve}}
        with answer_contract_scope(request):
            assert _BUDGET.get() == expected


def test_multi_sentence_marker_preserves_original_identical_text(material):
    material[3]['statements'][1]['text'] = '😀𠮷原文【未核实】。她在2023年8月23日画画。她在2023年8月23日画画。'
    answer, metadata = publish(material)
    assert '原文【未核实】。【未核实】' in answer.answer
    assert answer.answer.count('她在2023年8月23日画画。【未核实】') == 2
    assert metadata['unverified_claims'] == 1 and len(metadata['unverified_claim_spans']) == 3
    assert len({item['start'] for item in metadata['unverified_claim_spans']}) == 3


def test_raw_literal_contract_and_input_keep_34_eligibility_under_37():
    from tests.memory_app.v2.test_raw_answer import CASES, make_plan
    from backend.memory_app.v2.raw_answer import messages, preflight
    for question, content, *_ in CASES:
        plan = make_plan(question, content)
        old = preflight(plan)
        assert old is not None
        new_plan = deepcopy(plan)
        new_plan['policy_versions']['compose'] = '@37'
        assert preflight(new_plan) == old
        assert messages(new_plan, old) == messages(plan, old)
    for history in ('Earlier context.',):
        plan['history'] = history
        new_plan['history'] = history
        assert preflight(plan) is None and preflight(new_plan) is None


def test_confirmed_literal_memory_keeps_34_eligibility_under_37(tmp_path):
    from backend.memory_app.v2.initial_answer import assess
    from backend.memory_app.v2.policies import override
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service = RecognitionService(records)
    scope = WorkScope('local-user', 'alpha')
    body = 'Orion 的重试上限为 3 次。'
    experience = service.stage_experience(scope=scope, content=body)
    candidate = service.propose(scope=scope, content=body, source_experience_ids=[experience])
    service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    query = WorkspaceQuery(records, SQLiteDocumentRepository(records), JsonObjectStore(tmp_path / 'sources'),
        ModelConfiguration(records, tmp_path, InMemorySecretStore()), service)
    with override(compose='@34', retrieve='@1'):
        collected = query.collect_candidates('alpha', 'Orion 的重试上限是多少？')
    plan = {'project_id': 'alpha', 'scope': scope, 'question': 'Orion 的重试上限是多少？',
        'policy_versions': {'compose': '@34'}, 'chosen': collected['candidates']}
    old = assess(query, plan)
    assert old['status'] == 'sufficient', old
    plan['policy_versions']['compose'] = '@37'
    assert assess(query, plan, reference=old['proof']['reference']) == old


def test_raw_frozen_proof_uses_37_identity_with_unchanged_literal_wire(raw_frozen):
    from backend.memory_app.answer_proof import has_raw_answer_proof, select_raw_answer_input
    from backend.memory_app.kernel.answer_continuations import decode
    request = raw_frozen['request']
    request['policy_versions']['compose'] = '@37'
    capsule = raw_frozen['capsule']
    capsule['request'] = deepcopy(request)
    facts = decode(capsule['capsule'])
    facts['plan']['policy_versions']['compose'] = '@37'
    capsule['capsule'] = encode(facts)
    product = raw_frozen['product']
    product['receipt']['ask'].update(unverified_claims=0, unverified_claim_spans=[])
    assert has_raw_answer_proof(request, capsule)
    assert select_raw_answer_input(request, product, raw_frozen['raw_input'], raw_frozen['raw_result'], capsule) is raw_frozen['raw_input']
