"""Verify outcome feedback against real product, history and source owners."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from backend.recognition import RecognitionConflict, WorkScope
from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2 import consolidation_events
from backend.memory_app.v2.outcome_corrections import _time
from tests.memory_app.v2.test_outcome_redos import (
    scenario, completed, redo, wait_product, blocked_new_main, OLD, EDITED, NEW,
)
from tests.memory_app.v2.test_workbench_do import env as do_env


COLLECTION = 'v2_outcome_corrections'
PROJECT = 'project-a'


def edit(env, turn):
    document = turn['receipt']['do']['document_id']
    response = env.client.patch('/api/recognition/documents/' + document,
        json={'project_id': PROJECT, 'expected_revision': 1, 'markdown': EDITED})
    assert response.status_code == 200, response.text
    event, = [row for row in env.records.list(COLLECTION) if row.payload['kind'] == 'outcome_edit']
    assert env.documents.markdown(document, revision=1) == OLD
    assert env.documents.markdown(document, revision=2) == EDITED
    assert event.payload['birth_revision'] == 1 and event.payload['to_revision'] == 2
    return event


def adjust(env, identity, *, revision=1):
    before = env.records.read('v2_task_divisions', identity)
    items = deepcopy(before.payload['items'])
    items[0]['goal'] = '用户保存的新目标'
    response = env.client.patch('/api/v2/workbench/turns/' + identity + '/division',
        json={'project_id': PROJECT, 'items': items, 'expected_revision': revision})
    assert response.status_code == 200, response.text
    return next(row for row in env.records.list(COLLECTION)
        if row.payload['kind'] == 'division_adjust' and row.payload['division_to_revision'] == revision + 1)


def mature(row, seconds=601):
    return (_time(row.payload['last_saved_at']) + timedelta(seconds=seconds)).isoformat()


def test_real_completed_roots_edit_division_redo_produce_verified_zero_ref_feedback(scenario):
    env = scenario
    old, old_turn = completed(env)
    edited = edit(env, old_turn)
    division = adjust(env, old['turn']['id'])
    env.summary = NEW
    response = redo(env, old, revision=2)
    assert response.status_code == 200, response.text
    new = response.json()
    new_turn = wait_product(env, new)
    assert new_turn['receipt']['do']['state'] == 'done'
    redone, = [row for row in env.records.list(COLLECTION) if row.payload['kind'] == 'outcome_redo']
    assert redone.payload['turn_id'] != old_turn['receipt']['do']['kernel_turn_id']
    assert len(env.models.calls) == 5
    before = env.records.list_all()
    events = consolidation_events.outcomes(env.records, PROJECT, now=mature(edited))
    assert {event['event_id'] for event in events} == {
        'outcome:' + row.object_id for row in (edited, division, redone)}
    assert {event['type'] for event in events} == {'outcome_edit', 'division_adjust', 'outcome_redo'}
    by_kind = {event['type']: event for event in events}
    assert by_kind['outcome_edit']['before'] == OLD[:600]
    assert by_kind['outcome_edit']['after'] == EDITED[:600]
    assert by_kind['outcome_redo']['before'] == EDITED[:300]
    assert by_kind['outcome_redo']['after'] == NEW[:300]
    assert json.loads(by_kind['division_adjust']['after']) == division.payload['after_goals']
    assert all(event['_refs'] == [] and event['_snapshots'] == [] for event in events)
    assert all('_snapshot' not in event for event in events)
    assert consolidation_events.outcomes(env.records, 'other-project', now=mature(edited)) == []
    assert 'outcome:' + edited.object_id not in {
        event['event_id'] for event in consolidation_events.outcomes(env.records, PROJECT, now=mature(edited, 600))}
    consolidation_events.validate_outcomes(env.records, events, project=PROJECT, now=mature(edited))
    feedback = json.loads(consolidation_events.verified_feedback(env.records, events, project=PROJECT, now=mature(edited)))
    assert feedback == {'outcomes': [{key: event[key] for key in (
        'event_id', 'type', 'turn_id', 'before', 'after', 'before_title', 'after_title', 'new_turn_id') if key in event}
        for event in events]}
    assert env.records.list_all() == before
    assert env.records.list('recognition_experiences') == ()


def test_actual_edit_qualification_rebuilds_history_and_revalidates_all_cas_owners(scenario):
    env = scenario
    old, turn = completed(env)
    fact = edit(env, turn)
    instant = mature(fact)
    event, = consolidation_events.outcomes(env.records, PROJECT, now=instant)
    from backend.memory_app.v2.learning_events import checkpoint, completed as reset_learning
    checkpoint(env.records, 0)
    reset_learning(env.records, PROJECT)
    assert len(consolidation_events.outcomes(env.records, PROJECT, now=instant)) == 1
    assert event['turn_id'] == old['turn']['id']
    assert event['turn_id'] != turn['receipt']['do']['kernel_turn_id']
    damaged = deepcopy(event)
    damaged['before'] = '替换历史正文'
    with pytest.raises(RecognitionConflict, match='consolidation_outcome_changed'):
        consolidation_events.verified_feedback(env.records, [damaged], project=PROJECT, now=instant)
    mutations = [
        (COLLECTION, fact.object_id, {'before': '并非历史改动'}),
        (COLLECTION, fact.object_id, {'net_change': False}),
        (COLLECTION, fact.object_id, {'birth_revision': 2}),
        (COLLECTION, fact.object_id, {'turn_id': turn['receipt']['do']['kernel_turn_id']}),
        (COLLECTION, fact.object_id, {'policy_version': None}),
        (COLLECTION, fact.object_id, {'policy_version': '@991234'}),
        (COLLECTION, fact.object_id, {'last_saved_at': '2026-10-06T00:00:00'}),
        ('v2_task_executions', old['turn']['id'], {'started': False}),
        ('v2_turns', old['turn']['id'], {'project_id': 'other-project'}),
        ('document_markdown', fact.payload['document_id'] + '~r2', {'markdown': '损坏的历史正文'}),
        ('documents', fact.payload['document_id'], {'status': 'archived'}),
    ]
    for collection, identity, changes in mutations:
        with env.records.begin() as tx:
            row = tx.read(collection, identity)
            tx.put(collection, identity, {**row.payload, **changes}, expected_revision=row.revision)
            reader = TransactionRecords(tx)
            assert consolidation_events.outcomes(reader, PROJECT, now=instant) == [], (collection, changes)
            with pytest.raises(RecognitionConflict, match='consolidation_outcome_changed'):
                consolidation_events.validate_outcomes(reader, [event], project=PROJECT, now=instant)
    for collection in ('documents', 'v2_turns', 'v2_task_executions'):
        identity = fact.payload['document_id'] if collection == 'documents' else old['turn']['id']
        with env.records.begin() as tx:
            row = tx.read(collection, identity)
            tx.put(collection, identity, row.payload, expected_revision=row.revision)
            with pytest.raises(RecognitionConflict, match='consolidation_outcome_changed'):
                consolidation_events.validate_outcomes(TransactionRecords(tx), [event], project=PROJECT, now=instant)
    with env.records.begin() as tx:
        tx.put('v2_document_recall', fact.payload['document_id'], {'project_id': PROJECT,
            'state': 'forgotten', 'by': 'user'}, expected_revision=0)
        assert consolidation_events.outcomes(TransactionRecords(tx), PROJECT, now=instant, local_only=True) == []
    assert consolidation_events.outcomes(env.records, PROJECT, now=mature(fact, -1)) == []
    consolidation_events.validate_outcomes(env.records, [event], project=PROJECT, now=instant)
    assert env.records.read(COLLECTION, fact.object_id) == fact


def test_consumption_requires_committed_qualified_id_and_private_is_own_project_only(scenario):
    env = scenario
    _, turn = completed(env)
    fact = edit(env, turn)
    instant = mature(fact)
    event, = consolidation_events.outcomes(env.records, PROJECT, now=instant)
    for index, (project, identity) in enumerate(((PROJECT, fact.object_id), ('other-project', event['event_id']))):
        with env.records.begin() as tx:
            tx.put('v2_consolidation_inputs', 'legacy-collision-' + str(index), {'project_id': project,
                'event_ids': [identity]}, expected_revision=0)
            tx.commit()
        assert len(consolidation_events.outcomes(env.records, PROJECT, now=instant)) == 1
    with env.records.begin() as tx:
        tx.put('v2_consolidation_inputs', 'rolled-back-consumption', {'project_id': PROJECT,
            'event_ids': [event['event_id']]}, expected_revision=0)
        assert consolidation_events.outcomes(TransactionRecords(tx), PROJECT, now=instant) == []
    assert len(consolidation_events.outcomes(env.records, PROJECT, now=instant)) == 1
    set_private_project(env.records, PROJECT, True, 0)
    assert consolidation_events.outcomes(env.records, PROJECT, now=instant) == []
    local, = consolidation_events.outcomes(env.records, PROJECT, now=instant, local_only=True)
    assert local['_refs'] == [] and local['_snapshots'] == []
    assert consolidation_events.outcomes(env.records, 'other-project', now=instant, local_only=True) == []
    with pytest.raises(RecognitionConflict, match='consolidation_outcome_changed'):
        consolidation_events.validate_outcomes(env.records, [event], project=PROJECT, now=instant)
    consolidation_events.validate_outcomes(env.records, [local], project=PROJECT, now=instant)
    with env.records.begin() as tx:
        tx.put('v2_consolidation_inputs', 'committed-consumption', {'project_id': PROJECT,
            'event_ids': [local['event_id']]}, expected_revision=0)
        tx.commit()
    assert consolidation_events.outcomes(env.records, PROJECT, now=instant, local_only=True) == []
    with pytest.raises(RecognitionConflict, match='consolidation_outcome_changed'):
        consolidation_events.verified_feedback(env.records, [local], project=PROJECT, now=instant)


def test_real_division_revision_chain_retains_prior_actions_without_fabricating_sources(scenario):
    env = scenario
    old, _ = completed(env)
    first = adjust(env, old['turn']['id'])
    second = adjust(env, old['turn']['id'], revision=2)
    events = consolidation_events.outcomes(env.records, PROJECT)
    assert {event['event_id'] for event in events} == {'outcome:' + row.object_id for row in (first, second)}
    assert second.payload['before_goals'] == second.payload['after_goals']
    assert all(event['_refs'] == [] and event['_snapshots'] == [] for event in events)
    for collection, identity, changes in (
            ('v2_task_divisions', old['turn']['id'], {'source_turn_id': 'turn-other'}),
            ('v2_task_divisions', old['turn']['id'], {'deleted': True}),
            ('v2_turns', old['turn']['id'], {'user_text': '并非实际冻结的用户要求'}),
            (COLLECTION, second.object_id, {'before_goals': ['并非前一次保存的目标']})):
        with env.records.begin() as tx:
            row = tx.read(collection, identity)
            tx.put(collection, identity, {**row.payload, **changes}, expected_revision=row.revision)
            assert consolidation_events.outcomes(TransactionRecords(tx), PROJECT) == []
            with pytest.raises(RecognitionConflict):
                consolidation_events.validate_outcomes(TransactionRecords(tx), events, project=PROJECT)
    assert env.records.read(COLLECTION, first.object_id) == first


def test_real_redo_pending_then_done_retains_captured_history_after_later_edits(scenario):
    env = scenario
    old, turn = completed(env)
    with blocked_new_main(env):
        response = redo(env, old)
        assert response.status_code == 200, response.text
        new = response.json()
        assert env.entered.wait(timeout=15)
        pending, = env.records.list(COLLECTION)
        assert 'after' not in pending.payload and 'completed_at' not in pending.payload
        assert consolidation_events.outcomes(env.records, PROJECT) == []
    new_turn = wait_product(env, new)
    assert new_turn['receipt']['do']['state'] == 'done'
    fact = env.records.read(COLLECTION, pending.object_id)
    assert fact.revision == 2 and fact.payload['after'] == NEW[:300]
    original, = consolidation_events.outcomes(env.records, PROJECT)
    for document in (turn['receipt']['do']['document_id'], new_turn['receipt']['do']['document_id']):
        saved = env.documents.save_user_edit(document, markdown='后来的用户改动',
            title='后来改名', expected_revision=1)
        assert saved['revision'] == 2
    retained, = consolidation_events.outcomes(env.records, PROJECT)
    assert retained['before'] == OLD[:300] and retained['after'] == NEW[:300]
    assert retained['before_title'] == fact.payload['before_title']
    assert retained['after_title'] == fact.payload['after_title']
    assert retained['_row'] == original['_row'] == fact
    with pytest.raises(RecognitionConflict):
        consolidation_events.validate_outcomes(env.records, [original], project=PROJECT)
    consolidation_events.validate_outcomes(env.records, [retained], project=PROJECT)
    for changes in ({'new_turn_id': old['turn']['id']}, {'new_birth_revision': 2},
            {'completed_at': None}, {'to_revision': 3}, {'after': ''}, {'policy_version': '@991234'}):
        with env.records.begin() as tx:
            tx.put(COLLECTION, fact.object_id, {**fact.payload, **changes}, expected_revision=fact.revision)
            assert consolidation_events.outcomes(TransactionRecords(tx), PROJECT) == [], changes
            with pytest.raises(RecognitionConflict):
                consolidation_events.validate_outcomes(TransactionRecords(tx), [retained], project=PROJECT)


def test_real_frozen_project_and_me_sources_keep_scope_privacy_and_retained_revision(scenario):
    from backend.memory_app.v2.policies import override
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition.product_draft_dependencies import product_draft_source
    from tests.memory_app.v2.test_profile import publish
    from tests.memory_app.v2.test_situation_methods import method
    env = scenario
    domain = SimpleNamespace(records=env.records, service=env.state.recognition_service)
    profile = publish(domain, '本人偏好用可核验的成果')
    project = method(domain, '先按目标完成成果', ['准备一份成果'], project=PROJECT)
    with override(retrieve='@2', compose='@2'):
        _, turn = completed(env)
    document = turn['receipt']['do']['document_id']
    bound = product_draft_source(env.records, WorkScope('local-user', PROJECT), document, 1)
    assert {root[0] for root in bound.roots} == {PROJECT, 'me'}
    fact = edit(env, turn)
    instant = mature(fact)
    event, = consolidation_events.outcomes(env.records, PROJECT, now=instant)
    assert {(ref['project_id'], ref['id']) for ref in event['_refs']} == {('me', profile.id), (PROJECT, project.id)}
    assert {snapshot['scope']['project_id'] for snapshot in event['_snapshots']} == {'me', PROJECT}
    assert all(snapshot['nodes'] for snapshot in event['_snapshots'])
    consolidation_events.validate_outcomes(env.records, [event], project=PROJECT, now=instant)
    authority = SourceEgressService(env.records)
    authority.set_policy(WorkScope('local-user', 'me'), 'recognition', profile.id, 1, 0, [])
    assert consolidation_events.outcomes(env.records, PROJECT, now=instant) == []
    local, = consolidation_events.outcomes(env.records, PROJECT, now=instant, local_only=True)
    assert local['_refs'] == event['_refs']
    assert any(node['effective_purposes'] == [] for snapshot in local['_snapshots'] for node in snapshot['nodes'])
    with pytest.raises(RecognitionConflict):
        consolidation_events.validate_outcomes(env.records, [event], project=PROJECT, now=instant)
    with env.records.begin() as tx:
        tx.put('recognition_recall_preferences', project.id,
            {'scope': {'user_id': 'local-user', 'project_id': PROJECT}, 'state': 'forgotten', 'by': 'user'}, expected_revision=0)
        assert consolidation_events.outcomes(TransactionRecords(tx), PROJECT, now=instant, local_only=True) == []
    recognition = env.records.read('recognitions', profile.id)
    env.state.recognition_service.revise(scope=WorkScope('local-user', 'me'), recognition_id=profile.id,
        expected_revision=recognition.revision, content='后来修订的画像')
    assert consolidation_events.outcomes(env.records, PROJECT, now=instant, local_only=True) == []
    with pytest.raises(RecognitionConflict):
        consolidation_events.validate_outcomes(env.records, [local], project=PROJECT, now=instant)


def test_verified_feedback_binds_target_project_mode_and_unique_actions(scenario):
    env = scenario
    _, turn = completed(env)
    fact = edit(env, turn)
    instant = mature(fact)
    public, = consolidation_events.outcomes(env.records, PROJECT, now=instant)
    local, = consolidation_events.outcomes(env.records, PROJECT, now=instant, local_only=True)
    for events, project in (([public], 'other-project'), ([public, local], PROJECT), ([public, public], PROJECT)):
        with pytest.raises(RecognitionConflict):
            consolidation_events.verified_feedback(env.records, events, project=project, now=instant)
    consolidation_events.validate_outcomes(env.records, [public], project=PROJECT, now=instant)
    assert json.loads(consolidation_events.verified_feedback(env.records, [local],
        project=PROJECT, now=instant))['outcomes'][0]['event_id'] == local['event_id']
    with pytest.raises(TypeError):
        consolidation_events.verified_feedback(env.records, [public], now=instant)


@pytest.mark.parametrize('unknown', ['request_json_array', 'receipt_none'])
def test_division_unknown_actual_owner_shapes_remain_unqualified(scenario, unknown):
    env = scenario
    old, _ = completed(env)
    adjust(env, old['turn']['id'])
    assert len(consolidation_events.outcomes(env.records, PROJECT)) == 1
    collection = 'v2_task_executions' if unknown == 'request_json_array' else 'v2_turns'
    with env.records.begin() as tx:
        row = tx.read(collection, old['turn']['id'])
        payload = deepcopy(dict(row.payload))
        if unknown == 'request_json_array':
            payload['request']['input']['text'] = '[]'
        else:
            payload['receipt']['do'] = None
        tx.put(collection, row.object_id, payload, expected_revision=row.revision)
        assert consolidation_events.outcomes(TransactionRecords(tx), PROJECT) == []
