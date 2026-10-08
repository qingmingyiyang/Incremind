import pytest

from backend.memory_app.packet_egress import capture_packet_egress
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_origin_integrity import copy
from tests.memory_app.v2.test_candidate_destinations import seed, confirm_to
from tests.memory_app.v2.test_workbench_ask import env


def publish(service, scope, experience):
    candidate = service.propose(scope=scope, content='新方法', source_experience_ids=[experience])
    return service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer=scope.user_id)


def retain(records, service, scope, recognitions, identity):
    packet_payload = {'id': 'packet-' + identity, 'kind': 'context', 'project_id': scope.project_id,
        'query': '新方法', 'items': [{'id': row.id, 'revision': row.revision} for row in recognitions]}
    packet_payload['source_egress'] = capture_packet_egress(service, scope, packet_payload)
    packet_payload.update(state='consumed', task_id='task-' + identity)
    document = SQLiteDocumentRepository(records, namespace_id='recognition').create(DocumentDraft(
        title='整理稿 ' + identity, document_type='agent-result', markdown='依据新方法的整理稿 ' + identity, project_id=scope.project_id,
        source_refs=({'source_id': 'task-' + identity, 'locator': 'task://task-' + identity},)))
    with records.begin() as tx:
        packet = tx.put('recognition_context_packets', packet_payload['id'], packet_payload, expected_revision=0)
        task = tx.put('recognition_tasks', 'task-' + identity, {'id': 'task-' + identity,
            'project_id': scope.project_id, 'state': 'completed', 'document_id': document['id'],
            'context_packet_id': packet.object_id}, expected_revision=0)
        tx.commit()
    return service.stage_experience(scope=scope, content='整理稿 ' + identity,
        provenance={'kind': 'model_generated_artifact', 'actor': 'agent', 'source_refs': [
            {'type': 'task', 'id': task.object_id, 'revision': task.revision},
            {'type': 'document', 'id': document['id'], 'revision': document['revision']},
            {'type': 'context_packet', 'id': packet.object_id, 'revision': packet.revision}]})


@pytest.mark.parametrize('change', ['marker', 'privacy'])
def test_copied_insight_ask_and_retained_artifact_reuse_keep_original_authority(env, change):
    old, _, _, _ = seed(env)
    result = confirm_to(env, old)
    assert result.status_code == 200, result.text
    identity = result.json()['id']
    scope = WorkScope('local-user', 'beta')
    asked = env.http.post('/api/v2/workbench/turns', json={
        'project_id': 'beta', 'text': '新方法', 'intent': 'ask'})
    assert asked.status_code == 200, asked.text
    assert identity in [row['id'] for row in asked.json()['turn']['receipt']['ask']['citations']]
    recognition = env.service.get_recognition(scope=scope, recognition_id=identity)
    first = retain(env.records, env.service, scope, [recognition], 'first')
    second = retain(env.records, env.service, scope, [publish(env.service, scope, first)], 'second')
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(scope, [{'type': 'experience', 'id': second, 'revision': 1}])
    authority.require(snapshot, 'generation')
    copied = env.records.read('recognitions', identity).payload['source_experience_ids'][0]
    if change == 'marker':
        with env.records.begin() as tx:
            marker = tx.read('v2_experience_origins', copied)
            tx.put('v2_experience_origins', copied, dict(marker.payload), expected_revision=marker.revision)
            tx.commit()
        with pytest.raises(RecognitionConflict):
            authority.snapshot(scope, [{'type': 'experience', 'id': second, 'revision': 1}])
    else:
        from backend.memory_app.v2.privacy import set_private_project
        set_private_project(env.records, 'alpha', True, 0)
        assert env.service.get_recognition(scope=scope, recognition_id=identity).authorized
        with pytest.raises(RecognitionConflict):
            authority.require(authority.snapshot(scope, [{'type': 'experience', 'id': second, 'revision': 1}]), 'generation')
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, snapshot)


def test_origin_artifact_graph_uses_one_global_node_budget(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'budget.sqlite3')
    service = RecognitionService(records)
    alpha, beta, gamma = (WorkScope('user', name) for name in ('alpha', 'beta', 'gamma'))
    roots = [publish(service, alpha, service.stage_experience(scope=alpha, content='source ' + str(n)))
        for n in range(126)]
    first = retain(records, service, alpha, roots, 'first')
    copied = copy(service, first)
    second = retain(records, service, beta, [publish(service, beta, copied)], 'second')
    authority = SourceEgressService(records)
    authority.require(authority.snapshot(beta, [{'type': 'experience', 'id': second, 'revision': 1}]), 'generation')
    third = copy(service, second, source='beta', destination='gamma')
    with pytest.raises(RecognitionConflict, match='too large'):
        authority.snapshot(gamma, [{'type': 'experience', 'id': third, 'revision': 1}])
