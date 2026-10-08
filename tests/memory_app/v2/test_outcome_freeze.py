"""Exercise the product freezer seam with persisted synthetic user feedback.

These rows prove caller verification and freezing, not a completed TaskDo or a
connected consolidation consumer. The original material/privacy owners remain real.
"""
import json
from copy import deepcopy

import pytest

from backend.memory_app.kernel.turn_requests import freeze_product_turn as assemble_product_turn
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.privacy import freeze_turn_materials, privacy_revision, set_private_project
from backend.memory_app.v2.turn_requests import freeze_product_turn, validate_frozen_inputs
from backend.recognition import RecognitionConflict
from core.ai_kernel import validate_turn_request
from tests.memory_app.v2.test_turn_requests import materials


INSTRUCTION = '从以下认识和使用记录归纳待确认的规律。'


@pytest.fixture
def feedback_facts(materials):
    records, models, descriptors = materials
    with records.begin() as tx:
        division = tx.put('v2_task_divisions', 'synthetic-turn', {
            'project_id': 'alpha', 'turn_id': 'synthetic-turn', 'goals': ['调整后的目标']},
            expected_revision=0)
        event = tx.put('v2_outcome_corrections', 'synthetic-feedback', {
            'kind': 'division_adjust', 'project_id': 'alpha', 'turn_id': 'synthetic-turn',
            'division_revision': division.revision, 'before': ['原来的目标'],
            'after': ['调整后的目标']}, expected_revision=0)
        tx.commit()
    return records, models, descriptors, event, division


def _verified_loader(facts, calls):
    records, _, _, event, division = facts
    privacy = privacy_revision(records)

    def load():
        calls.append('feedback')
        current_event = records.read(event.collection, event.object_id)
        current_division = records.read(division.collection, division.object_id)
        if (current_event != event or current_division != division
                or event.payload['project_id'] != 'alpha'
                or event.payload['turn_id'] != division.object_id
                or event.payload['division_revision'] != division.revision
                or event.payload['after'] != division.payload['goals']
                or privacy_revision(records) != privacy):
            raise RecognitionConflict('synthetic_feedback_binding_changed')
        return json.dumps({'feedback': [{
            'event_id': event.object_id, 'event_revision': event.revision,
            'turn_id': division.object_id, 'division_revision': division.revision,
            'before': event.payload['before'], 'after': event.payload['after']} ]}, ensure_ascii=False)
    return load


def _freeze(facts, calls, *, material_count=0, kind='memory.consolidate', entry='product', **options):
    records, models, descriptors, _, _ = facts

    def load_text(item):
        calls.append('material:' + item['id'])
        return item['payload']['source_text']

    values = dict(records=records, models=models, project_id='alpha',
        materials=descriptors if material_count else (), load_text=load_text,
        turn_id='turn-' + 'b' * 32, session_id='session-feedback',
        operation_id='operation-feedback', idempotency_key='feedback-freeze',
        created_at='2026-10-06T08:00:00+00:00', **options)
    if entry == 'kernel':
        def freeze_materials(records, models, project, descriptors, **values):
            return freeze_turn_materials(records, models, project, descriptors,
                authority=SourceEgressService(records), **values)
        return assemble_product_turn(kind, **values, freeze_materials=freeze_materials,
            validate_request=validate_frozen_inputs, instruction=INSTRUCTION)
    return freeze_product_turn(kind, **values)


@pytest.mark.parametrize('material_count', [0, 1])
def test_verified_feedback_is_appended_once_after_original_material_loading(feedback_facts, material_count):
    calls = []
    load = _verified_loader(feedback_facts, calls)
    expected = {'feedback': [{'event_id': 'synthetic-feedback', 'event_revision': 1,
        'turn_id': 'synthetic-turn', 'division_revision': 1,
        'before': ['原来的目标'], 'after': ['调整后的目标']}]}
    request = _freeze(feedback_facts, calls, material_count=material_count, load_verified_feedback=load)
    assert calls == (['material:public-item'] if material_count else []) + ['feedback']
    pieces = [INSTRUCTION] + (['public-item-body'] if material_count else [])
    pieces.append(json.dumps(expected, ensure_ascii=False))
    assert request['input']['text'] == '\n\n'.join(pieces)
    assert request['input']['text'].count('synthetic-feedback') == 1
    assert len(request['input']['refs']) == material_count
    assert len(request['privacy']['material_refs']) == material_count
    assert len(request['privacy']['source_snapshots']) == material_count
    assert request['privacy']['allow_remote'] is True
    assert 'private-item-body' not in json.dumps(request, ensure_ascii=False)
    assert validate_turn_request(request) == request
    validate_frozen_inputs(feedback_facts[0], feedback_facts[1], request)


@pytest.mark.parametrize('entry', ['product', 'kernel'])
def test_default_and_explicit_none_keep_original_request_bytes(feedback_facts, entry):
    first_calls, second_calls = [], []
    first = _freeze(feedback_facts, first_calls, material_count=1, entry=entry)
    second = _freeze(feedback_facts, second_calls, material_count=1, entry=entry, load_verified_feedback=None)
    assert first_calls == second_calls == ['material:public-item']
    assert first['input']['text'] == INSTRUCTION + '\n\npublic-item-body'
    assert json.dumps(first, ensure_ascii=False).encode('utf-8') == json.dumps(second, ensure_ascii=False).encode('utf-8')


def test_memory_text_still_refuses_before_feedback_callback(feedback_facts):
    calls = []
    with pytest.raises(RecognitionConflict, match='auxiliary instructions'):
        _freeze(feedback_facts, calls, text='unfiltered user material',
            load_verified_feedback=_verified_loader(feedback_facts, calls))
    assert calls == []


@pytest.mark.parametrize('returned', [None, {'feedback': []}, 17])
def test_non_string_feedback_return_is_rejected(feedback_facts, returned):
    calls = []

    def load():
        calls.append('feedback')
        return returned

    with pytest.raises(RecognitionConflict, match='feedback'):
        _freeze(feedback_facts, calls, load_verified_feedback=load)
    assert calls == ['feedback']


@pytest.mark.parametrize('kind', ['memory.organize', 'memory.propose_insights', 'project.task', 'workbench.route'])
def test_feedback_hook_is_only_available_for_consolidation(feedback_facts, kind):
    calls = []
    with pytest.raises(RecognitionConflict, match='consolidat'):
        _freeze(feedback_facts, calls, kind=kind, load_verified_feedback=_verified_loader(feedback_facts, calls))
    assert calls == []


@pytest.mark.parametrize('which', ['event', 'division'])
def test_domain_callback_rejects_real_row_cas_drift(feedback_facts, which):
    records, _, _, event, division = feedback_facts
    calls = []
    load = _verified_loader(feedback_facts, calls)
    row = event if which == 'event' else division
    payload = deepcopy(row.payload)
    payload['after' if which == 'event' else 'goals'] = ['后来保存的目标']
    with records.begin() as tx:
        tx.put(row.collection, row.object_id, payload, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match='binding_changed'):
        _freeze(feedback_facts, calls, load_verified_feedback=load)
    assert calls == ['feedback']


def test_unknown_feedback_fact_is_not_frozen(feedback_facts):
    records, _, _, event, _ = feedback_facts
    calls = []
    load = _verified_loader(feedback_facts, calls)
    with records.begin() as tx:
        tx.delete(event.collection, event.object_id, expected_revision=event.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match='binding_changed'):
        _freeze(feedback_facts, calls, load_verified_feedback=load)
    assert calls == ['feedback']


def test_stale_material_is_rejected_before_feedback_callback(feedback_facts):
    records = feedback_facts[0]
    calls = []
    row = records.read('workspace_items', 'public-item')
    with records.begin() as tx:
        tx.put(row.collection, row.object_id, {**row.payload, 'source_text': 'changed original'},
            expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        _freeze(feedback_facts, calls, material_count=1, load_verified_feedback=_verified_loader(feedback_facts, calls))
    assert calls == []


def test_original_privacy_guard_rechecks_after_feedback_loading(feedback_facts):
    records = feedback_facts[0]
    calls = []
    verified = _verified_loader(feedback_facts, calls)

    def load():
        result = verified()
        set_private_project(records, 'alpha', True, 0)
        return result

    with pytest.raises(RecognitionConflict, match='privacy revision'):
        _freeze(feedback_facts, calls, load_verified_feedback=load)
    assert calls == ['feedback']
