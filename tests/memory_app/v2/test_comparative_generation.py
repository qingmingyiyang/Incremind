import json

import pytest

from tests.memory_app.v2.test_insight_generation import env
from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.policies import ACTIVE, get, override, register
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import WorkScope
from backend.memory_app.v2.overviews import ScopeOverviews
from core.document_engine import DocumentDraft


def publish(env, identity, project='alpha', scene=None):
    scope = WorkScope('local-user', project)
    experience = env.service.stage_experience(scope=scope, content='原文证据 方法 ' + identity)
    candidate = env.service.propose(scope=scope, content='原文证据 方法 ' + identity,
        source_experience_ids=[experience])
    saved = env.service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer='local-user', recognition_id=identity)
    if scene:
        assign_scene(env.records, 'recognition', saved.id, project, scene)
    return saved


def projects(env):
    with env.records.begin() as tx:
        for identity, name in [('alpha', '当前项目'), ('beta', '送礼'), ('secret', '私密名称')]:
            tx.put('v2_projects', identity, {'name': name, 'scenes': [], 'builtin': None}, expected_revision=0)
        tx.commit()
    set_private_project(env.records, 'secret', True, 0)
    original = env.records.list('workspace_items')[0]
    document = env.documents.create(DocumentDraft(title='项目说明', document_type='note',
        markdown='## 摘要\n第一句。\n\n## 正文\n合成说明', project_id='beta',
        source_refs=({'source_id': 'beta-source', 'locator': 'workspace://beta-source'},)))
    with env.records.begin() as tx:
        tx.put('workspace_items', 'beta-source', {**original.payload, 'id': 'beta-source',
            'project_id': 'beta', 'document_id': document['id']}, expected_revision=0)
        tx.commit()
    signature, summaries = ScopeOverviews(env.records, env.documents, env.model)._snapshot('beta', None)
    assert summaries and summaries[0]['id'] == document['id']
    with env.records.begin() as tx:
        tx.put('v2_scope_overviews', 'scope-beta', {'text': '第一句。第二句不发。',
            'input_revision': signature, 'source_document_ids': [document['id']]}, expected_revision=0)
        tx.commit()


def response(target='old', destination='beta'):
    return {'insights': [{'kind': 'supplement', 'relation': 'supplement', 'text': '新增条件',
        'conditions': ['2026年送礼时'], 'target_id': target, 'scope_hint': destination}], 'supports': []}


def generate(env):
    with override(extract='@2'):
        return generate_insights(env.model, env.service, env.documents, 'alpha', env.doc)


def test_real_generation_freezes_five_qualified_neighbors_and_public_project_list(env):
    projects(env)
    for identity in ['old', 'other1', 'other2', 'other3', 'other4', 'other5']:
        publish(env, identity)
    publish(env, 'profile', 'me')
    publish(env, 'sibling', scene='兄弟')
    publish(env, 'foreign', 'beta')
    env.model.response = json.dumps(response(target='other1'))
    result = generate(env)
    assert len(result) == 1
    hint = env.records.read('v2_candidate_hints', result[0]['id']).payload
    assert hint['relation'] == 'supplement' and hint['scope_hint'] == 'beta'
    sent = json.loads(env.model.messages[1]['content'])
    assert len(sent['neighbors']) == 5
    assert not {'sibling', 'foreign'} & {row['id'] for row in sent['neighbors']}
    assert {row['id'] for row in sent['projects']} == {'alpha', 'beta'}
    assert next(row for row in sent['projects'] if row['id'] == 'beta')['overview'] == '第一句。'
    assert '私密名称' not in json.dumps(env.model.messages, ensure_ascii=False)
    request = env.records.list('v2_memory_turn_keys')[-1].payload['request']
    frozen = {m['id'] for m in request['privacy']['material_refs'] if m['type'] == 'recognition'}
    assert frozen == {row['id'] for row in sent['neighbors']}
    assert generate(env) == result and env.model.calls == 1
    assert env.records.list('recognitions') and all(r.payload['state'] == 'pending' for r in env.records.list('recognition_candidates') if r.object_id.startswith('candidate-v2-'))


def test_duplicate_only_proposes_evidence_support_without_new_candidate(env):
    old = publish(env, 'old')
    env.model.response = json.dumps({'insights': [], 'supports': [{'target_id': old.id, 'evidence': '另一份原文印证'}]})
    assert generate(env) == []
    proposals = env.records.list('v2_insight_evidence_support')
    assert len(proposals) == 1 and proposals[0].payload['state'] == 'pending'
    assert proposals[0].payload['target_id'] == old.id
    assert not any(r.object_id.startswith('candidate-v2-') for r in env.records.list('recognition_candidates'))
    assert env.records.list('recognition_relations') == ()
    assert generate(env) == [] and env.model.calls == 1
    assert env.records.list('v2_insight_evidence_support') == proposals


def test_neighbor_revision_change_discards_output_and_closed_egress_never_calls(env):
    old = publish(env, 'old')
    env.model.response = json.dumps(response(destination=None))
    env.model.after = lambda: env.service.revise(scope=env.scope, recognition_id=old.id,
        expected_revision=old.revision, content='修改的认识')
    assert generate(env) == []
    assert not any(r.object_id.startswith('candidate-v2-') for r in env.records.list('recognition_candidates'))
    env.model.after = lambda: None
    env.model.public = lambda: {'generation': {'base_url': 'https://example.com/v1', 'allow_remote': False}}
    before = env.model.calls
    assert generate(env) == [] and env.model.calls == before


def test_actual_neighbor_scope_version_is_bound_before_prepare_and_frozen(env, monkeypatch):
    publish(env, 'old')
    env.model.response = json.dumps(response(destination=None))
    policy = get('scope', version='@2')
    def chosen(value):
        monkeypatch.setitem(ACTIVE, 'scope', '@1')
        return policy(value)
    register('scope', '@9851')(chosen)
    monkeypatch.setitem(ACTIVE, 'scope', '@9851')
    result = generate(env)
    assert len(result) == 1
    request = env.records.list('v2_memory_turn_keys')[-1].payload['request']
    assert request['policy_versions']['scope'] == '@9851'
    assert [row['id'] for row in json.loads(env.model.messages[1]['content'])['neighbors']] == ['old']
    frozen = env.records.read('v2_extract_inputs', request['turn_id'])
    original_messages = json.dumps(env.model.messages, ensure_ascii=False, separators=(',', ':'))
    from backend.recognition import RecognitionService
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.document_engine import SQLiteDocumentRepository
    reopened = SQLiteStructuredRecordStore(env.records.database_path)
    with override(extract='@1', scope='@1'):
        replay = generate_insights(env.model, RecognitionService(reopened),
            SQLiteDocumentRepository(reopened), 'alpha', env.doc)
    assert replay == result and env.model.calls == 1
    assert reopened.read('v2_extract_inputs', request['turn_id']) == frozen
    assert json.dumps(env.model.messages, ensure_ascii=False, separators=(',', ':')) == original_messages
