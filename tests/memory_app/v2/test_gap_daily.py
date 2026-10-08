"""验证每日待补复查使用原本机服务，不调度模型。"""
import pytest
from backend.memory_app.workspace_query import WorkspaceQuery
from tests.memory_app.v2.test_workbench_ask import env, ask, publish
from tests.memory_app.v2.test_gaps import listed
from backend.memory_app.v2 import policies
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.signals import SignalService
from core.document_engine.ports import DocumentDraft
from core.document_engine.retrieval_index import COLLECTION as DOCUMENT_INDEX
from core.storage_provider import SQLiteUnitOfWorkConflict

@pytest.fixture(autouse=True)
def gap_policy():
    with policies.override(gap='@1'):
        yield

@pytest.mark.parametrize('private', [False, True])
def test_real_daily_new_material_resolves_without_model(env, private):
    env.model.allowed = False
    assert ask(env).json()['turn']['receipt']['ask']['no_match'] is True
    original = listed(env)
    assert len(original) == 1
    if private:
        set_private_project(env.records, 'alpha', True, expected_revision=0)
    calls = env.model.calls
    env.domains.query.source_store.write('sources', 'gap-material', {
        'id': 'gap-material', 'project_id': 'alpha', 'title': 'Synthetic material',
        'identity_method': 'legacy_import', 'metadata': {'content': 'alpha beta gamma'}},
        expected_revision=0)
    assert listed(env) == original
    jobs = env.http.app.state.memory_daily_jobs
    jobs.jobs['gaps']()
    assert listed(env) == []
    jobs.jobs['gaps']()
    assert listed(env) == [] and env.model.calls == calls
    assert env.records.list('v2_gap_dismissals') == ()

def test_real_daily_off_does_not_generate_projection(env):
    env.model.allowed = False
    assert ask(env).status_code == 200
    SignalService(env.records).set_enabled(False, expected_revision=0)
    env.http.app.state.memory_daily_jobs.jobs['gaps']()
    assert env.records.list('v2_gaps') == ()
    assert listed(env) == [] and env.model.calls == 0


def material(env, *, project='alpha', scene=None):
    source = 'daily-gap-source'
    env.domains.query.source_store.write('sources', source, {
        'id': source, 'project_id': project, 'title': 'Synthetic daily material',
        'identity_method': 'legacy_import', 'metadata': {'content': 'alpha beta gamma'}},
        expected_revision=0)
    doc = env.documents.create(DocumentDraft(title='Synthetic daily material', document_type='note',
        project_id=project, markdown='# Synthetic\n\n## 摘要\nalpha beta gamma\n\n## 正文\nalpha beta gamma',
        source_refs=({'source_id': source, 'locator': 'source://' + source},)))['id']
    if scene:
        assign_scene(env.records, 'document', doc, project, scene)
    return doc, source


def unanswered(env):
    env.model.allowed = False
    response = ask(env)
    assert response.status_code == 200, response.text
    assert response.json()['turn']['receipt']['ask']['no_match'] is True
    original = listed(env)
    assert len(original) == 1 and env.model.calls == 0
    return original


@pytest.mark.parametrize('damage', ['missing_document', 'malformed_document', 'missing_source'])
def test_daily_unknown_index_preserves_known_gap_without_repair(env, damage):
    from core.storage_provider.source_retrieval_index import COLLECTION as SOURCE_INDEX
    original = unanswered(env)
    doc, source = material(env)
    records = env.domains.query.retrieval_index.source_records if damage == 'missing_source' else env.records
    collection, identity = (SOURCE_INDEX, source) if damage == 'missing_source' else (DOCUMENT_INDEX, doc)
    row = records.read(collection, identity)
    with records.begin() as tx:
        if damage == 'malformed_document':
            tx.put(collection, identity, {**row.payload, 'entry': None}, expected_revision=row.revision)
        else:
            tx.delete(collection, identity, expected_revision=row.revision)
        tx.commit()
    damaged = records.read(collection, identity)
    before = env.records.read('v2_gaps', 'alpha')
    env.http.app.state.memory_daily_jobs.jobs['gaps']()
    assert env.records.read('v2_gaps', 'alpha') == before
    assert listed(env) == original and records.read(collection, identity) == damaged
    assert env.model.calls == 0 and env.records.list('v2_gap_dismissals') == ()


def test_daily_source_document_scene_does_not_cover_sibling_scene(env):
    env.model.allowed = False
    created = env.http.post('/api/v2/projects', json={'name': 'alpha'}).json()
    project = created['id']
    assert env.http.patch('/api/v2/projects/' + project, json={'scenes': ['north', 'south'],
        'expected_revision': created['revision']}).status_code == 200
    for scene in ('north', 'south'):
        response = env.http.post('/api/v2/workbench/turns', json={'project_id': project,
            'text': '#alpha/' + scene + ' alpha beta gamma?', 'intent': 'ask'},
            headers={'Idempotency-Key': 'daily-' + scene})
        assert response.status_code == 200, response.text
        assert response.json()['turn']['receipt']['ask']['no_match'] is True
    original = listed(env, project)
    assert {item['scene'] for item in original} == {'north', 'south'}
    material(env, project=project, scene='north')
    env.http.app.state.memory_daily_jobs.jobs['gaps']()
    remaining = listed(env, project)
    assert remaining == [item for item in original if item['scene'] == 'south']
    assert env.model.calls == 0 and env.records.list('v2_gap_dismissals') == ()


@pytest.mark.parametrize('changed_owner', ['source', 'document'])
def test_daily_real_cas_after_coverage_rejects_stale_conclusion(env, monkeypatch, changed_owner):
    original = unanswered(env)
    doc, source = material(env)
    before = env.records.read('v2_gaps', 'alpha')
    collect = WorkspaceQuery.local_coverage
    observed = []
    def observe(query, *args, **kwargs):
        result = collect(query, *args, **kwargs)
        assert query.retrieval_index.read_only and result['status'] == 'known'
        assert result['coverage'] == 1.0 and callable(result['validate_current'])
        if not observed:
            if changed_owner == 'source':
                payload = dict(env.domains.query.source_store.read('sources', source))
                env.domains.query.source_store.write('sources', source, payload, expected_revision=1)
            else:
                env.documents.save_user_edit(doc, expected_revision=1,
                    markdown='# Synthetic\n\n## 摘要\nalpha beta gamma changed\n\n## 正文\nalpha beta gamma')
            observed.append(changed_owner)
        return result
    monkeypatch.setattr(WorkspaceQuery, 'local_coverage', observe)
    env.http.app.state.memory_daily_jobs.jobs['gaps']()
    assert observed == [changed_owner]
    assert env.records.read('v2_gaps', 'alpha') == before and listed(env) == original
    assert (env.domains.query.source_store.revision('sources', source) if changed_owner == 'source'
        else env.documents.read(doc)['revision']) == 2
    assert env.model.calls == 0 and env.records.list('v2_gap_dismissals') == ()


@pytest.mark.parametrize('action', ['off', 'clear'])
def test_daily_settings_change_after_real_validation_cannot_write_old_conclusion(env, monkeypatch, action):
    original = unanswered(env)
    material(env)
    before = env.records.read('v2_gaps', 'alpha')
    collect = WorkspaceQuery.local_coverage
    validated = []
    def observe(query, *args, **kwargs):
        result = collect(query, *args, **kwargs)
        assert query.retrieval_index.read_only and result['status'] == 'known' and result['coverage'] == 1.0
        validate = result['validate_current']
        def verify_then_change():
            validate()
            settings = SignalService(env.records)
            if action == 'off':
                settings.set_enabled(False, expected_revision=0)
            else:
                settings.clear(expected_revision=0)
            validated.append(action)
        return {**result, 'validate_current': verify_then_change}
    monkeypatch.setattr(WorkspaceQuery, 'local_coverage', observe)
    with pytest.raises(SQLiteUnitOfWorkConflict, match='gap_recheck_changed'):
        env.http.app.state.memory_daily_jobs.jobs['gaps']()
    assert validated == [action] and env.records.read('v2_gaps', 'alpha') == before
    assert before.payload['items'][0]['unresolved'] is True
    assert listed(env) == [] and env.model.calls == 0
    assert env.records.list('v2_gap_dismissals') == ()
    if action == 'off':
        SignalService(env.records).set_enabled(True, expected_revision=1)
        assert listed(env) == original
