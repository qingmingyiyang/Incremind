from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.ai_kernel.product_admission import ProductAdmissionItem, seal_admission
from core.ai_kernel.sqlite_store import SQLiteAITurnStore, TurnStateConflict


def admit(store, number, *, parent='public-parent'):
    request = json.loads((Path(__file__).parents[2] / 'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    request.update(turn_id=f'turn-steer-{number}', operation_id=f'operation-steer-{number}', idempotency_key=f'steer-{number}')
    scope = {'turn_id': request['turn_id'], 'project_id': request['scope']['project_id'],
        'owner_user_id': 'local-user', 'namespace_id': 'default', 'origin_kind': 'personal'}
    binding = {**scope, 'public_turn_id': parent, 'target_turn_id': f'public-{number}',
        'scene': None, 'thread_id': 'thread-one', 'part': number, 'part_turn_ids': ['public-1', 'public-2']}
    item = ProductAdmissionItem('workbench-steer-binding-v1',
        {'kind': 'session_event', 'object_id': request['turn_id'],
         'uri': store.immutable_payload_reference(request['turn_id'], 'workbench-steer-binding-v1')}, scope, binding)
    bundle = seal_admission(request, (item,), store)
    with store.admission_bundle(request, bundle):
        store.claim_turn(request)
    store.append(event(store, request, 'turn.accepted'), expected_sequence=0)
    now = datetime.now(timezone.utc)
    lease = store.try_acquire_run_lease(request['turn_id'], 'actual-sql-owner', now=now,
        stale_after=now + timedelta(minutes=5))
    return request, lease


def event(store, request, kind):
    return {'schema_version': '1.0.0', 'event_id': 'event-' + uuid4().hex, 'turn_id': request['turn_id'],
        'session_id': request['session_id'], 'sequence': len(store.events_after(request['turn_id'])) + 1,
        'type': kind, 'actor': 'user' if kind == 'turn.steer.received' else 'kernel',
        'correlation': {'step_id': None, 'tool_call_id': None, 'model_request_id': None, 'operation_id': request['operation_id']},
        'data': {'status': 'accepted' if kind == 'turn.accepted' else 'running', 'summary': '实际数据库接纳检查',
            'capability_id': None, 'payload_ref': None, 'receipt_ref': None, 'evidence_refs': [],
            'error_code': None, 'retryable': False}, 'occurred_at': datetime.now(timezone.utc).isoformat()}


def receive(store, request, lease, key, *, cursor=None):
    received = event(store, request, 'turn.steer.received')
    return store.accept_steer_bundle(received, expected_sequence=received['sequence'] - 1, run_lease=lease,
        submission_key=key, text='这次补充实际范围', snapshot={'answer_tail': ''}, visible_sequence=cursor,
        maximum_count=5, policy_version='@2')


def test_parts_share_quota_and_same_key_keeps_frozen_snapshot(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'native.sqlite3')
    first, one = admit(store, 1)
    second, two = admit(store, 2)
    original = receive(store, first, one, 'one', cursor=2)
    replay = receive(store, first, one, 'one', cursor=2)
    assert replay.immutable_payload_ref == original.immutable_payload_ref
    with pytest.raises(TurnStateConflict, match='identity conflict'):
        receive(store, first, one, 'one', cursor=3)
    for index in range(4):
        receive(store, second, two, f'two-{index}')
    with pytest.raises(TurnStateConflict, match='limit'):
        receive(store, first, one, 'six')
    assert len(store.workbench_steers('public-parent')) == 5


def test_input_event_and_owner_commit_together_then_decision_releases(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'native.sqlite3')
    request, lease = admit(store, 1)
    receive(store, request, lease, 'one')
    row = store.workbench_steers('public-parent')[0]
    payload = {'turn_id': request['turn_id'], 'project_id': request['scope']['project_id'],
        'steer_refs': [row['payload_ref']], 'run_generation': lease.generation, 'run_owner_id': lease.owner_id,
        'messages': [{'role': 'user', 'content': '补充'}]}
    wrong = deepcopy(payload)
    wrong['run_owner_id'] = 'another-owner'
    applied = event(store, request, 'turn.steer.applied')
    with pytest.raises(TurnStateConflict, match='evidence differs'):
        store.apply_steer_input(applied, expected_sequence=applied['sequence'] - 1, run_lease=lease,
            steer_ids=[row['payload']['id']], kind='turn-steer-input-one', payload=wrong)
    assert store.get_immutable_payload(request['turn_id'], 'turn-steer-input-one') is None
    accepted = store.apply_steer_input(applied, expected_sequence=applied['sequence'] - 1, run_lease=lease,
        steer_ids=[row['payload']['id']], kind='turn-steer-input-one', payload=payload)
    assert store.workbench_steers('public-parent')[0]['state'] == 'injected'
    with pytest.raises(TurnStateConflict, match='evidence is unavailable'):
        store.release_steer_input(request['turn_id'], run_lease=lease,
            input_ref=accepted.immutable_payload_ref, decision_ref='missing')
    decision = store.get_or_create_immutable_payload(request['turn_id'], 'decision-evidence',
        {'turn_id': request['turn_id'], 'steer_input_ref': accepted.immutable_payload_ref, 'decision': 'domain-decision-check'})
    store.release_steer_input(request['turn_id'], run_lease=lease,
        input_ref=accepted.immutable_payload_ref, decision_ref=decision)
    assert store.workbench_steers('public-parent')[0]['state'] == 'released'


def test_new_owner_retires_old_unconsumed_input_without_applying_it(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'native.sqlite3')
    request, lease = admit(store, 1)
    receive(store, request, lease, 'one')
    store.release_strict_run_lease(lease)
    now = datetime.now(timezone.utc)
    replacement = store.try_acquire_run_lease(request['turn_id'], 'replacement-owner', now=now,
        stale_after=now + timedelta(minutes=5))
    store.retire_unconsumed_steers(request['turn_id'], run_lease=replacement)
    row = store.workbench_steers('public-parent')[0]
    assert row['state'] == 'unapplied' and row['input_ref'] is None
    assert not any(value['type'] == 'turn.steer.applied' for value in store.events_after(request['turn_id']))


def test_adopted_input_without_actual_result_never_selects_original_answer(tmp_path):
    from backend.memory_app.answer_effective import recover_effective_answer
    from backend.memory_app.answer_proof import read_answer_input
    from backend.memory_app.v2.policies import get

    store = SQLiteAITurnStore(tmp_path / 'native.sqlite3')
    request, lease = admit(store, 1)
    receive(store, request, lease, 'one')
    row = store.workbench_steers('public-parent')[0]
    base = {'messages': [{'role': 'user', 'content': '原问题'}], 'purpose': 'primary', 'response_schema': {}}
    base_ref = store.get_or_create_immutable_payload(request['turn_id'], 'answer-base-input-answer', base)
    store.get_or_create_immutable_payload(request['turn_id'], 'answer-model-input-answer', base)
    payload = {**base, 'messages': get('steer', version='@2').answer_messages(base['messages'], [row['payload']]),
        'turn_id': request['turn_id'], 'project_id': request['scope']['project_id'], 'run_generation': lease.generation,
        'run_owner_id': lease.owner_id, 'steer_refs': [row['payload_ref']], 'origin_invocation': 'answer',
        'invocation': 'steer-unfinished', 'base_ref': base_ref, 'prior_inputs': [], 'prior_request_ids': [],
        'interruption_ref': 'unavailable'}
    applied = event(store, request, 'turn.steer.applied')
    accepted = store.apply_steer_input(applied, expected_sequence=applied['sequence'] - 1, run_lease=lease,
        steer_ids=[row['payload']['id']], kind='turn-steer-input-unfinished', payload=payload)
    with pytest.raises(ValueError, match='selector_unavailable'):
        read_answer_input(store, request['turn_id'])
    with pytest.raises(ValueError, match='terminal_result'):
        recover_effective_answer(store, request['turn_id'], 'answer')
    with pytest.raises(TurnStateConflict, match='proof is unavailable'):
        store.reserve_steer_model_grant(request['turn_id'], tool_invocation_id='unallocated', invocation_key='unallocated',
            run_lease=lease, proof={'input_ref': accepted.immutable_payload_ref, 'steer_ref': row['payload_ref'], 'interruption_ref': 'unavailable'})
    assert store.steer_model_grants(request['turn_id']) == ()
    assert store.get_immutable_payload(request['turn_id'], 'answer-effective-answer') is None
    assert not any(value['type'].startswith('model.') for value in store.events_after(request['turn_id']))


def test_assignment_policy_preserves_exact_native_goal_and_rejects_new_long_or_repeated_text(tmp_path):
    from backend.memory_app.v2.policies import get
    store = SQLiteAITurnStore(tmp_path / 'native.sqlite3')
    request, _ = admit(store, 1)
    task = '甲' * 2000
    originals = {}
    for identity in ('retained-one', 'retained-two'):
        value = {'assignment_id': identity, 'task': task, 'division': {'goal': task, 'deliverable': '文稿', 'depends_on': []}}
        reference = store.get_or_create_immutable_payload(request['turn_id'], 'agent.dispatch.task.' + identity + '.v1', value)
        originals[identity] = store.get(reference)
    policy = get('steer', version='@2')
    policy.validate_assignment_texts(list(originals.values()), originals)
    changed = {**originals['retained-one'], 'task': task + '改变'}
    with pytest.raises(ValueError, match='too_long'):
        policy.validate_assignment_texts([changed], originals)
    proposed = [{'assignment_id': identity, 'task': '新目标', 'division': {'goal': '新目标', 'deliverable': '新稿', 'depends_on': []}}
        for identity in ('new-one', 'new-two')]
    with pytest.raises(ValueError, match='repeated'):
        policy.validate_assignment_texts(proposed, originals)


def test_visible_snapshot_reads_only_current_reset_and_preserves_previous_frames(tmp_path):
    from core.storage_provider import SQLiteStructuredRecordStore
    from backend.memory_app.v2.policies import get
    from backend.memory_app.v2.steer import visible_snapshot
    from backend.memory_app.v2.turn_frames import FrameProducerOwner, TurnFrames, STREAMS, FRAMES
    store = SQLiteAITurnStore(tmp_path / 'native.sqlite3')
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    request, lease = admit(store, 1)
    binding = store.get_immutable_payload(request['turn_id'], 'workbench-steer-binding-v1')[1]
    frames = TurnFrames(records, turn_id=binding['target_turn_id'], project_id=binding['project_id'],
        recipe=get('retry', version='@1'), request=request, schedule=False,
        turn={'id': binding['target_turn_id'], 'thread_id': binding['thread_id'], 'intent': 'ask',
            'user_text': request['input']['text'], 'created_at': request['created_at']},
        producer_owner=lambda: FrameProducerOwner(turn_id=request['turn_id'], lease=lease, store=store))
    assert frames.start()
    frames.delta('旧' * 400)
    frames.reset_for_steer()
    frames.delta('新' * 400)
    head = records.read(STREAMS, binding['target_turn_id'])
    cursor = head.payload['events'][-1]['sequence']
    assert visible_snapshot(records, binding, request, lease, cursor, limit=300)['answer_tail'] == '新' * 300
    assert [item['text'] for item in records.read(FRAMES, binding['target_turn_id']).payload['frames']] == ['旧' * 400, '新' * 400]
    with pytest.raises(ValueError, match='cursor_unavailable'):
        visible_snapshot(records, binding, request, lease, 2, limit=300)
    assert not any(value['type'].startswith('model.') for value in store.events_after(request['turn_id']))


def test_draft_base_references_require_actual_native_admission(tmp_path):
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.ai_kernel.product_admission import bundle_to_payload
    from backend.recognition.product_draft_dependencies import _admitted_input_refs, ProductDraftDependencyError
    folder = tmp_path / '.rebuild-data'
    folder.mkdir()
    records = SQLiteStructuredRecordStore(folder / 'records.sqlite3')
    native = SQLiteAITurnStore(folder / 'ai-turns.sqlite3')
    request, _ = admit(native, 1)
    with records.begin() as tx:
        execution = tx.put('v2_task_executions', 'public-1',
            {'request': request, 'admission_bundle': bundle_to_payload(native.restore_admission(request))}, expected_revision=0)
        assert _admitted_input_refs(tx, request, execution) == native.admission_base_refs(request)
        altered = deepcopy(request)
        altered['input']['text'] += '改变实际请求'
        with pytest.raises(ProductDraftDependencyError, match='native admission changed'):
            _admitted_input_refs(tx, altered, execution)
        tx.commit()
    assert not any(value['type'].startswith('model.') for value in native.events_after(request['turn_id']))


def test_sql_snapshot_reads_received_steers_without_effective_selector(tmp_path):
    from core.ai_kernel.product_snapshot import read_product_turn_snapshot
    native = SQLiteAITurnStore(tmp_path / 'native.sqlite3')
    request, lease = admit(native, 1)
    receive(native, request, lease, 'received-before-dispatch')
    with native._connect() as connection:
        connection.execute('BEGIN')
        snapshot = read_product_turn_snapshot(connection, request['turn_id'])
    assert snapshot['events'] == list(native.events_after(request['turn_id']))
    assert len(snapshot['steers']) == 1
    assert snapshot['steers'][0]['payload_ref'] == native.workbench_steers('public-parent')[0]['payload_ref']
    assert snapshot['steers'][0]['state'] == 'queued'
    assert len(snapshot['attempts']) == 0


def test_sql_snapshot_legacy_absence_requires_no_native_steer_evidence(tmp_path):
    from core.ai_kernel.product_snapshot import read_product_turn_snapshot
    native = SQLiteAITurnStore(tmp_path / 'legacy.sqlite3')
    request = json.loads((Path(__file__).parents[2] / 'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    native.claim_turn(request)
    native.append(event(native, request, 'turn.accepted'), expected_sequence=0)
    with native._connect() as connection:
        connection.execute('DROP TABLE ai_turn_steer_model_grants')
        connection.execute('DROP TABLE ai_turn_steers')
        connection.execute('BEGIN')
        snapshot = read_product_turn_snapshot(connection, request['turn_id'])
    assert snapshot['events'] == list(native.events_after(request['turn_id']))
    assert snapshot['steers'] == [] and snapshot['grants'] == []
    assert 'workbench-steer-binding-v1' not in snapshot['immutable']
    native.get_or_create_immutable_payload(request['turn_id'], 'turn-steer-orphan', {'turn_id': request['turn_id']})
    with native._connect() as connection:
        connection.execute('BEGIN')
        with pytest.raises(ValueError, match='steer_schema_unavailable'):
            read_product_turn_snapshot(connection, request['turn_id'])


def test_sql_snapshot_new_binding_with_missing_tables_is_not_legacy(tmp_path):
    from core.ai_kernel.product_snapshot import read_product_turn_snapshot
    native = SQLiteAITurnStore(tmp_path / 'missing-tables.sqlite3')
    request, _ = admit(native, 1)
    with native._connect() as connection:
        connection.execute('DROP TABLE ai_turn_steer_model_grants')
        connection.execute('DROP TABLE ai_turn_steers')
        connection.execute('BEGIN')
        with pytest.raises(ValueError, match='steer_schema_unavailable'):
            read_product_turn_snapshot(connection, request['turn_id'])
