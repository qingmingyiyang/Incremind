"""B 纠正消费到用户取代旧方法；生产者完成与模型关系判断仅为合成输入。"""
from datetime import timedelta
import json
import re

from backend.memory_app.research_reads import product_read_sources
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.source_graph import SourceGraph
from backend.memory_app.v2.consolidation_events import authority_stores, consumer_outcomes
from backend.memory_app.v2.learning_events import events
from backend.memory_app.v2.outcome_corrections import _time
from backend.recognition import WorkScope
from tests.memory_app.v2.test_skill_exports import draft
from tests.memory_app.v2.test_skill_exports_full_factory import BASE, original_factory, post
from tests.memory_app.v2.test_skill_exports_mcp_factory import archive_sources, confirm_method
from tests.memory_app.v2.test_skill_exports_outcome_factory import (
    BIRTH, EDITED, PROJECT, SCOPE, save_edit, synthetic_published_output,
)


NEW_METHOD = '先核对来源与适用限制，再核实预算和近期愿望，保留纠正依据'
NEW_CONDITIONS = ['挑礼物时']


def checked_aux(actual, outcome):
    """核对原辅助 Turn、全部冻结来源和原模型尝试终态，不构造执行证明。"""
    turn, = [row for row in actual.records.list('v2_memory_turn_keys')
        if row.payload['request']['desired_outcome'] == outcome]
    request = actual.store.get_request(turn.object_id)
    assert request == turn.payload['request']
    assert request['desired_outcome'] == outcome and request['execution_policy']['purpose'] == 'aux'
    assert request['privacy']['material_refs'] and request['privacy']['source_snapshots']
    authority, graph = SourceEgressService(actual.records), SourceGraph()
    for snapshot in request['privacy']['source_snapshots']:
        scope = WorkScope(**snapshot['scope'])
        assert scope.user_id == 'local-user' and scope.project_id == PROJECT
        assert authority.snapshot(scope, snapshot['roots']) == snapshot
        authority.validate_snapshot(scope, snapshot)
        graph.snapshot(snapshot)
    completed = tuple(actual.store.events_after(turn.object_id))
    assert completed[-1]['type'] == 'turn.completed'
    assert len([event for event in completed if event['type'] == 'model.completed']) == 1
    terminals = [event for event in completed if event['type'] == 'model.attempt.terminal']
    assert len(terminals) == 1
    receipt = actual.store.get(terminals[0]['data']['receipt_ref'])
    assert receipt['status'] == 'succeeded'
    return turn, request, graph.result()


def test_original_factory_outcome_correction_human_supersession_updates_reviewed_skill(original_factory, monkeypatch):
    actual = original_factory
    config = actual.models.public()['generation']
    actual.models.update('generation', {'enabled': True, 'expected_revision': config['revision']})
    config = actual.models.public()['generation']
    assert config['configured'] is True and config['enabled'] is True and config['allow_remote'] is True
    assert config['base_url'] == 'https://example.invalid/v1' and config['model'] == 'synthetic-skill'
    # 完成 owner 事实沿用已核原帮助函数的合成前提；原请求、草稿与来源仍是真实对象。
    setup = synthetic_published_output(actual, known_birth=True)
    old = setup.parent
    old_row = actual.records.read('recognitions', old.id)
    old_validity = actual.records.read('v2_insight_validity', old.id)
    producer_rows = (actual.records.read('v2_turns', setup.product_id),
        actual.records.read('v2_task_executions', setup.product_id))
    saved = post(actual, BASE, {'sources': [{'id': old.id, 'revision': old.revision}],
        'document': draft()}).json()
    path = BASE + '/' + saved['id']
    reviewed = post(actual, path + '/review', {'expected_revision': saved['revision']}).json()
    assert old.id in archive_sources(actual, path, reviewed['revision'])
    current = actual.client.get(path).json()
    old_export = actual.records.read('v2_skill_exports', saved['id'])
    assert current['needs_update'] is False
    assert actual.wire['calls'] == 0 and actual.records.list('v2_memory_turn_keys') == ()

    turns, agents = authority_stores(actual.records)
    assert turns.get_request(setup.kernel_id) == actual.store.get_request(setup.kernel_id) == setup.request
    # 生产者未执行工具；原 readSources 所有者必须读到真实空集合，不能伪造非空读取完成。
    assert tuple(turns.events_after(setup.kernel_id)) == ()
    assert product_read_sources(actual.records, turns, agents, setup.request, PROJECT) == ((), ())
    initial = SourceEgressService(actual.records).snapshot(SCOPE,
        [{'type': 'recognition', 'id': old.id, 'revision': old.revision}])
    assert setup.request['privacy']['source_snapshots'] == [initial]
    assert setup.request['privacy']['material_refs'] == [
        {'type': 'recognition', 'id': old.id, 'revision': old.revision, 'project_id': PROJECT}]

    before = actual.records.read('documents', setup.output['document_id'])
    save_edit(actual, setup.output, 2, status=409)
    assert actual.records.read('documents', setup.output['document_id']) == before
    assert actual.records.list('v2_outcome_corrections') == ()
    save_edit(actual, setup.output, 1)
    fact, = actual.records.list('v2_outcome_corrections')
    assert fact.payload['kind'] == 'outcome_edit' and fact.payload['birth_revision'] == 1
    assert fact.payload['from_revision'] == 1 and fact.payload['to_revision'] == 2
    assert fact.payload['before'] == BIRTH.split('\n\n')[0] and fact.payload['after'] == EDITED.split('\n\n')[0]
    last = _time(fact.payload['last_saved_at'])
    assert last is not None and fact.payload['net_change'] is True
    boundary, mature = last + timedelta(seconds=600), last + timedelta(seconds=601)
    event_id = 'outcome:' + fact.object_id
    assert consumer_outcomes(actual.records, PROJECT, now=boundary.isoformat()) == []
    assert event_id not in events(actual.records, now=boundary.isoformat()).get(PROJECT, set())
    qualified, = consumer_outcomes(actual.records, PROJECT, now=mature.isoformat())
    assert qualified['event_id'] == event_id
    assert qualified['_roots'] == [{'id': setup.output['document_id'], 'revision': 1}]
    assert qualified['_refs'] == [{'type': 'recognition', 'id': old.id,
        'revision': old.revision, 'project_id': PROJECT}]

    stages = {'learning': 0, 'relation': 0}

    def completion(**request):
        actual.wire['calls'] += 1
        assert not request.get('stream')
        text = '\n'.join(message['content'] for message in request['messages'])
        if '"outcomes"' in text:
            stages['learning'] += 1
            assert fact.payload['before'] in text and fact.payload['after'] in text
            assert re.findall(r'"event_id":\s*"([^"]+)"', text) == [event_id]
            output = {'text': NEW_METHOD, 'conditions': NEW_CONDITIONS,
                'event_ids': [event_id], 'kind': 'correction'}
        else:
            stages['relation'] += 1
            context = json.loads(next(message['content'] for message in reversed(request['messages'])
                if message['role'] == 'user'))
            assert context['from']['text'] == NEW_METHOD
            assert context['from']['conditions'] == NEW_CONDITIONS
            assert old.id in {row['id'] for row in context['others']}
            # 关系判断也是合成响应；原服务只提建议，用户实际接受后才改变旧方法资格。
            output = {'suggestions': [{'other_id': old.id, 'kind': 'supersedes',
                'evidence': '保留预算和愿望核实，并根据成果纠正补充来源和适用限制。'}]}
        return {'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}

    # 只注入外层模型响应和原公开 now 墙钟；不替换消费、来源、Turn 或读证明所有者。
    monkeypatch.setattr(actual.models, '_completion_fn', completion)
    job = actual.app.state.memory_consolidation
    assert job.records is actual.records and job.service is actual.service and job.models is actual.models
    monkeypatch.setattr(job, 'now', lambda: mature)
    result = job.run(PROJECT)
    assert result['new_suggestions'] == 1 and result['failed_groups'] == 0
    pattern, = actual.records.list('v2_insight_patterns')
    assert pattern.payload['kind'] == 'correction' and pattern.payload['event_ids'] == [event_id]
    candidate = actual.records.read('recognition_candidates', pattern.object_id)
    assert candidate.payload['state'] == 'pending' and candidate.payload['content'] == NEW_METHOD
    assert candidate.payload['conditions'] == NEW_CONDITIONS
    assert candidate.object_id != old.id and actual.records.read('recognitions', candidate.object_id) is None
    assert stages == {'learning': 1, 'relation': 0} and actual.wire['calls'] == 1
    assert actual.records.read('recognitions', old.id) == old_row
    assert actual.records.read('v2_skill_exports', saved['id']) == old_export
    assert actual.client.get(path).json()['needs_update'] is False
    rejected = post(actual, BASE, {'sources': [{'id': candidate.object_id, 'revision': candidate.revision}],
        'document': draft()}, 409)
    assert rejected.json()['detail'] == 'skill_source_unavailable'
    learning_turn, learning_request, learning_graph = checked_aux(actual, 'memory.consolidate')
    assert any(node['kind'] == 'material' and node['type'] == 'recognition' and node['id'] == old.id
        and node['scope'] == {'user_id': 'local-user', 'project_id': PROJECT} for node in learning_graph['nodes'])
    consumed, = actual.records.list('v2_consolidation_inputs')
    assert consumed.payload['documents'] == [[setup.output['document_id'], 1]]
    assert consumed.payload['event_ids'] == [event_id]
    assert consumer_outcomes(actual.records, PROJECT, now=mature.isoformat()) == []
    assert job.run(PROJECT) == {'processed': 0, 'new_suggestions': 0, 'replayed': True}
    assert stages == {'learning': 1, 'relation': 0} and actual.wire['calls'] == 1
    post(actual, '/api/v2/library/insights/' + candidate.object_id + '/confirm',
        {'project_id': PROJECT, 'expected_revision': candidate.revision + 1}, 409)
    assert actual.records.read('recognition_candidates', candidate.object_id) == candidate

    confirmed = confirm_method(actual, candidate)
    assert confirmed['kind'] == 'recognition' and confirmed['state'] == 'active' and confirmed['revision'] == 1
    published = actual.records.read('recognition_candidates', candidate.object_id)
    assert published.payload['state'] == 'published' and published.revision == candidate.revision + 1
    assert published.payload['recognition_id'] == confirmed['id']
    assert confirmed['conditions'] == NEW_CONDITIONS
    links = actual.client.get('/api/v2/library/insights/' + confirmed['id'] + '/links',
        params={'project_id': PROJECT}).json()['links']
    suggestion, = [row for row in links if row['other_id'] == old.id
        and row['kind'] == 'supersedes' and row['state'] == 'suggested']
    proposal = actual.records.read('recognition_relation_proposals', suggestion['id'])
    assert proposal.payload['state'] == 'pending' and proposal.payload['from_id'] == confirmed['id']
    assert proposal.payload['to_id'] == old.id and actual.records.list('recognition_relations') == ()
    assert actual.records.read('recognitions', old.id) == old_row
    assert actual.records.read('v2_insight_validity', old.id) == old_validity
    assert actual.client.get(path).json()['needs_update'] is False
    assert actual.records.read('v2_skill_exports', saved['id']) == old_export
    link_turn, link_request, _ = checked_aux(actual, 'memory.link_suggest')
    assert {row['id'] for row in link_request['privacy']['material_refs']} == {old.id, confirmed['id']}
    assert stages == {'learning': 1, 'relation': 1} and actual.wire['calls'] == 2
    accept_path = '/api/v2/library/link-suggestions/' + proposal.object_id + '/accept'
    post(actual, accept_path, {'project_id': PROJECT, 'expected_revision': proposal.revision + 1}, 409)
    assert actual.records.read('recognition_relation_proposals', proposal.object_id) == proposal
    assert actual.client.get(path).json()['needs_update'] is False
    post(actual, accept_path, {'project_id': PROJECT, 'expected_revision': proposal.revision})
    assert actual.records.read('recognition_relation_proposals', proposal.object_id).payload['state'] == 'approved'
    edge, = actual.records.list('recognition_relations')
    assert edge.payload['relation'] == 'supersedes'
    assert edge.payload['from_id'] == confirmed['id'] and edge.payload['to_id'] == old.id
    validity = actual.records.read('v2_insight_validity', old.id)
    assert validity.payload['superseded_by'] == confirmed['id'] and validity.payload['valid_until']
    assert actual.records.read('recognitions', old.id) == old_row
    assert actual.client.get(path).json()['needs_update'] is True
    refused = post(actual, path + '/download', {'expected_revision': current['revision']}, 409)
    assert refused.json()['detail'] == 'skill_sources_changed'
    regenerated = post(actual, path + '/regenerate', {'expected_revision': current['revision'],
        'sources': [{'id': confirmed['id'], 'revision': confirmed['revision']}], 'document': draft()}).json()
    assert regenerated['needs_update'] is False and regenerated['reviewed'] is False
    post(actual, path + '/download', {'expected_revision': regenerated['revision']}, 409)
    reviewed = post(actual, path + '/review', {'expected_revision': regenerated['revision']}).json()
    references = archive_sources(actual, path, reviewed['revision'])
    assert confirmed['id'] in references and old.id not in references and NEW_METHOD in references
    assert '修订：' + str(confirmed['revision']) in references
    assert actual.records.list('v2_consolidation_inputs') == (consumed,)
    assert (actual.records.read('v2_turns', setup.product_id),
        actual.records.read('v2_task_executions', setup.product_id)) == producer_rows
    assert setup.documents.markdown(setup.output['document_id'], revision=1) == BIRTH
    assert setup.documents.markdown(setup.output['document_id'], revision=2) == EDITED
    assert actual.store.get_request(learning_turn.object_id) == learning_request
    assert actual.store.get_request(link_turn.object_id) == link_request
    assert tuple(turns.events_after(setup.kernel_id)) == ()
    assert product_read_sources(actual.records, turns, agents, setup.request, PROJECT) == ((), ())
    assert stages == {'learning': 1, 'relation': 1} and actual.wire['calls'] == 2
