"""原 MCP 提议与用户确认消费链；仅模型 wire 返回合成关系建议。"""
import io
import json
import zipfile

from backend.recognition import WorkScope
from tests.memory_app.v2.test_skill_exports import draft
from tests.memory_app.v2.test_skill_exports_full_factory import (
    BASE, original_factory, post, source,
)


LIBRARY = '/api/v2/library'


def enable_mcp(actual):
    current = actual.client.get('/api/v2/settings').json()['external_agent']
    body = {key: value for key, value in current.items() if key != 'revision'}
    response = actual.client.patch('/api/v2/settings/external-agent', json={
        **body, 'allow_remote': True, 'expected_revision': current['revision']})
    assert response.status_code == 200, response.text
    assert response.json()['allow_remote'] is True


def propose_method(actual, text, *, client):
    value = post(actual, '/api/v2/external-agent/mcp/propose_insight', {
        'client': client, 'arguments': {'text': text, 'conditions': ['挑礼物时'],
            'project': 'alpha'}}).json()
    assert value['turn_id'] is None and value['result']['state'] == 'pending'
    result = value['result']
    candidate = actual.records.read('recognition_candidates', result['candidate_id'])
    assert candidate.revision == result['revision'] == 1
    assert candidate.payload['state'] == 'pending'
    assert candidate.payload['conditions'] == ['挑礼物时']
    assert candidate.payload['scope'] == {'user_id': 'local-user', 'project_id': 'alpha'}
    receipt = actual.records.read('v2_external_agent_intakes', result['receipt_id'])
    assert receipt.payload['tool'] == 'propose_insight' and receipt.payload['client'] == client
    assert receipt.payload['object_id'] == candidate.object_id
    assert receipt.payload['object_revision'] == candidate.revision
    assert not candidate.payload['source_recognition_ids']
    return candidate


def confirm_method(actual, candidate):
    return post(actual, f'{LIBRARY}/insights/{candidate.object_id}/confirm', {
        'project_id': 'alpha', 'expected_revision': candidate.revision}).json()


def archive_sources(actual, path, revision):
    response = post(actual, path + '/download', {'expected_revision': revision})
    assert response.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            'choose-gift/SKILL.md', 'choose-gift/references/methods.md'}
        return archive.read('choose-gift/references/methods.md').decode('utf-8')


def test_original_factory_mcp_method_proposal_requires_human_confirmation_before_skill_export(original_factory):
    actual = original_factory
    config = actual.models.public()['generation']
    actual.models.update('generation', {'enabled': False, 'expected_revision': config['revision']})
    old = source(actual)
    old_row = actual.records.read('recognitions', old.id)
    old_skill = post(actual, BASE, {'sources': [{'id': old.id, 'revision': old.revision}],
        'document': draft()}).json()
    old_path = BASE + '/' + old_skill['id']
    enable_mcp(actual)
    candidate = propose_method(actual, '先确认预算，再核对近期愿望和退换条件', client='codex')
    assert actual.records.read('recognitions', candidate.object_id) is None
    assert [row['id'] for row in actual.client.get(BASE + '/methods').json()['items']] == [old.id]
    rejected = post(actual, BASE, {'sources': [{'id': candidate.object_id,
        'revision': candidate.revision}], 'document': draft()}, 409)
    assert rejected.json()['detail'] == 'skill_source_unavailable'
    post(actual, f'{LIBRARY}/insights/{candidate.object_id}/confirm', {
        'project_id': 'alpha', 'expected_revision': candidate.revision + 1}, 409)
    assert actual.records.read('recognition_candidates', candidate.object_id) == candidate
    assert actual.records.read('recognitions', old.id) == old_row
    confirmed = confirm_method(actual, candidate)
    assert confirmed['kind'] == 'recognition' and confirmed['state'] == 'active'
    assert confirmed['conditions'] == ['挑礼物时']
    assert confirmed['id'] != old.id and confirmed['revision'] == 1
    assert actual.records.read('recognition_candidates', candidate.object_id).payload['state'] == 'published'
    qualified = actual.service.get_recognition(scope=WorkScope('local-user', 'alpha'),
        recognition_id=confirmed['id'])
    assert qualified.authorized and qualified.conditions == ('挑礼物时',)
    assert {row['id'] for row in actual.client.get(BASE + '/methods').json()['items']} == {
        old.id, confirmed['id']}
    # 独立新方法没有取代旧来源；用户确认不自动修改旧方法或旧导出。
    assert actual.records.read('recognitions', old.id) == old_row
    assert actual.client.get(old_path).json()['needs_update'] is False
    saved = post(actual, BASE, {'sources': [{'id': confirmed['id'],
        'revision': confirmed['revision']}], 'document': draft()}).json()
    assert saved['reviewed'] is False and saved['needs_update'] is False
    path = BASE + '/' + saved['id']
    post(actual, path + '/download', {'expected_revision': saved['revision']}, 409)
    reviewed = post(actual, path + '/review', {'expected_revision': saved['revision']}).json()
    assert confirmed['id'] in archive_sources(actual, path, reviewed['revision'])
    assert actual.wire['calls'] == 0 and actual.records.list('v2_memory_turn_keys') == ()
    assert actual.records.list('recognition_relations') == ()


def test_original_factory_human_confirmed_mcp_supersession_marks_linked_skill_for_update(original_factory, monkeypatch):
    actual = original_factory
    old = source(actual)
    old_row = actual.records.read('recognitions', old.id)
    old_validity = actual.records.read('v2_insight_validity', old.id)
    saved = post(actual, BASE, {'sources': [{'id': old.id, 'revision': old.revision}],
        'document': draft()}).json()
    path = BASE + '/' + saved['id']
    reviewed = post(actual, path + '/review', {'expected_revision': saved['revision']}).json()
    assert old.id in archive_sources(actual, path, reviewed['revision'])

    def completion(**request):
        actual.wire['calls'] += 1
        actual.wire['messages'] = request['messages']
        assert not request.get('stream')
        context = json.loads(next(message['content'] for message in reversed(request['messages'])
            if message['role'] == 'user'))
        assert old.id in {row['id'] for row in context['others']}
        output = {'suggestions': [{'other_id': old.id, 'kind': 'supersedes',
            'evidence': '用户提出的新做法保留预算与愿望核实，并补充退换条件。'}]}
        return {'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}

    # 只替换外层提供方返回，原 ModelConfiguration、网关和辅助 Turn 保持原对象。
    monkeypatch.setattr(actual.models, '_completion_fn', completion)
    config = actual.models.public()['generation']
    actual.models.update('generation', {'enabled': True, 'expected_revision': config['revision']})
    config = actual.models.public()['generation']
    assert config['configured'] is True and config['enabled'] is True and config['allow_remote'] is True
    assert config['base_url'] == 'https://example.invalid/v1' and config['model'] == 'synthetic-skill'
    enable_mcp(actual)
    candidate = propose_method(actual, '先问清预算和最近愿望，再确认可退换条件', client='claude')
    assert actual.records.read('recognitions', old.id) == old_row
    assert actual.client.get(path).json()['needs_update'] is False
    assert actual.records.list('recognition_relations') == () and actual.wire['calls'] == 0
    confirmed = confirm_method(actual, candidate)
    assert confirmed['state'] == 'active' and confirmed['conditions'] == ['挑礼物时']
    links = actual.client.get(f"{LIBRARY}/insights/{confirmed['id']}/links",
        params={'project_id': 'alpha'}).json()['links']
    suggestions = [row for row in links if row['other_id'] == old.id
        and row['kind'] == 'supersedes' and row['state'] == 'suggested']
    assert len(suggestions) == 1 and actual.wire['calls'] == 1
    proposal = actual.records.read('recognition_relation_proposals', suggestions[0]['id'])
    assert proposal.payload['state'] == 'pending'
    assert proposal.payload['from_id'] == confirmed['id'] and proposal.payload['to_id'] == old.id
    assert actual.records.list('recognition_relations') == ()
    assert actual.records.read('recognitions', old.id) == old_row
    assert actual.records.read('v2_insight_validity', old.id) == old_validity
    assert actual.client.get(path).json()['needs_update'] is False
    current = actual.client.get(path).json()
    accept_path = f'{LIBRARY}/link-suggestions/{proposal.object_id}/accept'
    post(actual, accept_path, {'project_id': 'alpha', 'expected_revision': proposal.revision + 1}, 409)
    assert actual.records.read('recognition_relation_proposals', proposal.object_id) == proposal
    post(actual, accept_path, {'project_id': 'alpha', 'expected_revision': proposal.revision})
    assert actual.records.read('recognition_relation_proposals', proposal.object_id).payload['state'] == 'approved'
    edge, = actual.records.list('recognition_relations')
    assert edge.payload['relation'] == 'supersedes'
    assert edge.payload['from_id'] == confirmed['id'] and edge.payload['to_id'] == old.id
    validity = actual.records.read('v2_insight_validity', old.id)
    assert validity.payload['superseded_by'] == confirmed['id'] and validity.payload['valid_until']
    assert actual.records.read('recognitions', old.id) == old_row
    assert actual.client.get(path).json()['needs_update'] is True
    rejected = post(actual, path + '/download', {'expected_revision': current['revision']}, 409)
    assert rejected.json()['detail'] == 'skill_sources_changed'
    regenerated = post(actual, path + '/regenerate', {'expected_revision': current['revision'],
        'sources': [{'id': confirmed['id'], 'revision': confirmed['revision']}], 'document': draft()}).json()
    assert regenerated['needs_update'] is False and regenerated['reviewed'] is False
    post(actual, path + '/download', {'expected_revision': regenerated['revision']}, 409)
    reviewed = post(actual, path + '/review', {'expected_revision': regenerated['revision']}).json()
    references = archive_sources(actual, path, reviewed['revision'])
    assert confirmed['id'] in references and old.id not in references
    turn, = [row for row in actual.records.list('v2_memory_turn_keys')
        if row.payload['request']['desired_outcome'] == 'memory.link_suggest']
    request = actual.store.get_request(turn.object_id)
    assert request == turn.payload['request'] and request['execution_policy']['purpose'] == 'aux'
    assert {row['id'] for row in request['privacy']['material_refs']} == {old.id, confirmed['id']}
    events = tuple(actual.store.events_after(turn.object_id))
    assert events[-1]['type'] == 'turn.completed'
    assert len([event for event in events if event['type'] == 'model.completed']) == 1
    terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
    assert len(terminals) == 1
    assert actual.store.get(terminals[0]['data']['receipt_ref'])['status'] == 'succeeded'
    assert actual.wire['calls'] == 1
