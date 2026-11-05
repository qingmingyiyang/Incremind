from functools import partial
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.relations import RelationProposalService
from backend.memory_app.restructure_api import install_restructure_routes
from backend.memory_app.source_egress import SourceEgressService, recognition_service
from backend.memory_app.v2.consolidation import Consolidation, evidence_guard, install_consolidation_routes
from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
from backend.recognition import WorkScope
from backend.recognition.restructuring import RestructureProposalService
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


TITLES = ('缓存按需加载与检索结果复用的实施记录和后续核对事项', '请求范围隔离记录')


def build_app(root):
    records = SQLiteStructuredRecordStore(root / '.rebuild-data' / 'structured-records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    service = recognition_service(records)
    confirmation = WorkspaceConfirmation(root, records, documents)
    scope = WorkScope('local-user', 'alpha')
    document_ids, experience_ids = [], []
    for index, title in enumerate(TITLES):
        identity = f'workspace-title-{index}'
        item = {'id': identity, 'project_id': 'alpha', 'input_kind': 'text', 'title': title,
            'source_text': title, 'status': 'ready', 'document_id': None,
            'created_at': '2026-10-25T00:00:00+00:00',
            'draft': {'title': title, 'facts': [], 'todos': []}}
        with records.begin() as tx:
            tx.put('workspace_items', identity, item, expected_revision=0)
            tx.commit()
        result = confirmation.confirm(identity, 'alpha', 1, lambda draft: '# ' + draft['title'])
        document_ids.append(result['document_id'])
        experience_ids.append(ensure_document_experience(documents, service, 'alpha', result['document_id'])[0])
    first = service.propose(scope=scope, content='按需加载可以减少重复请求', source_experience_ids=experience_ids)
    active = service.publish(scope=scope, candidate_id=first.id, expected_revision=first.revision, reviewer='local-user')
    second = service.propose(scope=scope, content='按需加载减少重复请求', source_experience_ids=experience_ids[:1])
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[active.id, second.id],
        expected_revisions={active.id: active.revision, second.id: second.revision}, pending_output=True)
    merge = authority.save(scope=scope, proposal_id='merge-titles', snapshot=snapshot, operation='merge',
        outputs=[{'content': '按需加载减少重复请求', 'conditions': [],
            'source_experience_ids': experience_ids, 'source_recognition_ids': []}], reason='合并相同认识')
    persona_scope = WorkScope('local-user', 'me')
    persona_experience = service.stage_experience(scope=persona_scope, content='倾向按需加载')
    persona = service.propose(scope=persona_scope, content='倾向按需加载', source_experience_ids=[persona_experience])
    persona = service.publish(scope=persona_scope, candidate_id=persona.id, expected_revision=persona.revision,
        reviewer='local-user')
    supports = RelationProposalService(records, allow_persona=True, evidence_guard=partial(evidence_guard, records))
    egress = SourceEgressService(records)
    source_snapshot = {'project_id': 'alpha', 'snapshot': egress.snapshot(scope,
        [{'type': 'experience', 'id': identity, 'revision': 1} for identity in experience_ids])}
    for target, target_scope in ((active, scope), (persona, persona_scope)):
        supports.propose_evidence(scope, target.id, experience_ids,
            [{'id': identity, 'revision': 1} for identity in document_ids], '两份整理稿提供支持',
            snapshots=[source_snapshot, {'project_id': target_scope.project_id,
                'snapshot': egress.snapshot(target_scope, [{'type': 'recognition', 'id': target.id, 'revision': target.revision}])}])
    application = FastAPI()
    router = APIRouter(prefix='/api/recognition')
    install_restructure_routes(router, records, mutate=None, read_body=None,
        scope_for=partial(WorkScope, 'local-user'), revision=None, text=None, service=service, models=None,
        documents=documents)
    application.include_router(router)
    install_consolidation_routes(application, records=records, consolidation=Consolidation(records, service, documents))
    return SimpleNamespace(app=application, records=records, documents=documents, service=service,
        merge=merge, insight=active.id, persona=persona.id, document_ids=document_ids)


@pytest.fixture
def env(tmp_path):
    return build_app(tmp_path)


def test_merge_titles_are_read_only_and_keep_frozen_sources(env):
    before = env.records.read('recognition_restructure_proposals', env.merge['id'])
    with TestClient(env.app) as client:
        response = client.get('/api/recognition/restructure-proposals', params={'project_id': 'alpha'})
    assert response.status_code == 200, response.text
    row = response.json()['items'][0]
    assert [(document['id'], document['title']) for document in row['documents']] == list(zip(env.document_ids, TITLES))
    assert row['snapshot'] == before.payload['snapshot']
    assert row['revision'] == before.revision
    assert env.records.read('recognition_restructure_proposals', env.merge['id']) == before


@pytest.mark.parametrize('project,field', [('alpha', 'insight'), ('me', 'persona')])
def test_support_titles_respect_the_original_source_project(env, project, field):
    before = env.records.list('v2_insight_evidence_support')
    with TestClient(env.app) as client:
        response = client.get(f'/api/v2/library/insights/{getattr(env, field)}/evidence-support',
            params={'project_id': project})
        foreign = client.get(f'/api/v2/library/insights/{getattr(env, field)}/evidence-support',
            params={'project_id': 'beta'})
    assert response.status_code == 200, response.text
    row = response.json()['items'][0]
    assert row['project_id'] == 'alpha' and row['current'] is True
    assert row['documents'] == [{'id': identity, 'revision': 1, 'title': title}
        for identity, title in zip(env.document_ids, TITLES)]
    assert foreign.json() == {'items': []}
    assert env.records.list('v2_insight_evidence_support') == before


def test_current_title_does_not_rewrite_frozen_document_revision(env):
    identity = env.document_ids[0]
    env.documents.save_user_edit(identity, expected_revision=1, title='更新后的标题', markdown='# 更新后的标题')
    with TestClient(env.app) as client:
        merge = client.get('/api/recognition/restructure-proposals', params={'project_id': 'alpha'}).json()['items'][0]
        support = client.get(f'/api/v2/library/insights/{env.insight}/evidence-support',
            params={'project_id': 'alpha'}).json()['items'][0]
    assert merge['documents'][0] == {'type': 'document', 'id': identity, 'revision': 1, 'title': '更新后的标题'}
    assert support['documents'][0] == {'id': identity, 'revision': 1, 'title': '更新后的标题'}
    assert merge['snapshot'] == env.merge['snapshot']


@pytest.mark.parametrize('state', ['missing', 'foreign'])
def test_unavailable_source_title_is_not_exposed(env, state):
    identity = env.document_ids[0]
    with env.records.begin() as tx:
        document = tx.read('documents', identity)
        if state == 'missing':
            tx.delete('documents', identity, expected_revision=document.revision)
        else:
            tx.put('documents', identity, {**document.payload, 'project_id': 'beta', 'title': '其他项目的标题'},
                expected_revision=document.revision)
        tx.commit()
    with TestClient(env.app) as client:
        merge = client.get('/api/recognition/restructure-proposals', params={'project_id': 'alpha'}).json()['items'][0]
        support = client.get(f'/api/v2/library/insights/{env.insight}/evidence-support',
            params={'project_id': 'alpha'}).json()['items'][0]
    assert merge['documents'][0]['title'] is None
    assert support['documents'][0]['title'] is None
    assert support['documents'][0]['id'] == identity
    assert merge['snapshot'] == env.merge['snapshot']
