"""本机覆盖率使用原来源权限校验，测试只隔离外部传输。"""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import sqlite3

import pytest
from fastapi import FastAPI

from backend.memory_app.model_config import ModelConfiguration
from backend.security.secrets import InMemorySecretStore
from backend.memory_app.retrieval_models import ConfiguredTransport
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.policies import override
from backend.memory_app.recall_preferences import set_preference
from backend.memory_app.workspace import install_workspace_routes
from backend.memory_app.workspace_query import WorkspaceQuery
from backend.recognition import RecognitionService, RecognitionConflict, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.document_engine.retrieval_index import COLLECTION as INDEX
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_bytes(
        (Path(__file__).parents[3] / 'config/settings.toml.example').read_bytes())
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    documents, service = SQLiteDocumentRepository(records), RecognitionService(records)
    wires = []
    def completion(**request):
        wires.append(('generation', request))
        raise AssertionError('local coverage must not dispatch generation')
    def embedding(transport, *, endpoint, payload):
        transport._check_current()
        wires.append(('embedding', payload))
        return {'data': [{'index': i, 'embedding': [1.0, 0.0]}
                         for i, _ in enumerate(payload['input'])], 'usage': {'prompt_tokens': 5}}
    monkeypatch.setattr(ConfiguredTransport, 'post_json', embedding)
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=completion)
    for purpose in ('generation', 'embedding'):
        models.update(purpose, {'base_url': 'https://example.invalid/v1', 'model': 'synthetic-model',
            'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0,
            **({'enabled': True} if purpose == 'embedding' else {})})
    domains = install_workspace_routes(FastAPI(), runtime_root=tmp_path, records=records,
        documents=documents, service=service, models=models)
    domains.query.source_store.write('sources', 'fixture-other', {'id': 'fixture-other',
        'project_id': 'other', 'title': 'Synthetic unrelated source',
        'metadata': {'content': 'unrelated fixture material'}}, expected_revision=0)
    return SimpleNamespace(root=tmp_path, records=records, documents=documents, service=service,
                           models=models, query=domains.query, wires=wires)


def test_local_reader_owns_an_independent_readonly_index(env):
    original = env.query
    reader = original.local_reader()
    assert reader is not original
    assert reader.records is original.records and reader.documents is original.documents
    assert reader.source_store is original.source_store and reader.models is original.models
    assert reader.service is original.service
    assert reader.retrieval_index.source_records is original.retrieval_index.source_records
    assert reader.retrieval_index.read_only is True
    assert original.retrieval_index.read_only is False
    assert reader.ask_previews is not original.ask_previews
    assert env.wires == []


def document(env, text='alpha beta gamma', *, project='alpha'):
    identity = 'source-' + uuid4().hex
    env.query.source_store.write('sources', identity, {'id': identity, 'project_id': project,
        'title': 'Synthetic', 'identity_method': 'legacy_import', 'metadata': {'content': text}},
        expected_revision=0)
    result = env.documents.create(DocumentDraft(title=identity, document_type='note', project_id=project,
        markdown='# Synthetic\n\n## 摘要\n' + text + '\n\n## 正文\n' + text,
        source_refs=({'source_id': identity, 'locator': 'source://' + identity},)))
    return result['id'], identity


def readonly(env):
    return WorkspaceQuery(env.records, env.documents, env.query.source_store, env.models, env.service,
        read_only=True, source_index_records=env.query.retrieval_index.source_records)


def facts(env):
    result = []
    for path in sorted(env.root.rglob('*.sqlite3')):
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            result.append((str(path.relative_to(env.root)), tuple((table, tuple(connection.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid'))) for table in tables)))
    bodies = tuple((str(path.relative_to(env.root)), path.read_bytes()) for path in sorted(
        (env.root / '.rebuild-data').rglob('*.json')))
    return result, bodies


def test_private_material_coverage_is_known_and_has_no_model_or_fact_writes(env):
    set_private_project(env.records, 'alpha', True, 0)
    local = readonly(env)
    empty = local.local_coverage('alpha', 'alpha beta gamma?')
    assert empty['status'] == 'known' and empty['coverage'] == 0 and empty['stopped'] is False
    doc, source = document(env)
    before = facts(env)
    result = local.local_coverage('alpha', 'alpha beta gamma?')
    assert result['status'] == 'known'
    assert result['coverage'] == 1.0 and result['stopped'] is True
    assert {row['entry']['id'] for row in result['chosen']} == {doc}
    assert result['chosen'][0]['snapshot']['roots'] == [
        {'type': 'original_source', 'id': source, 'revision': 1}]
    assert not result['chosen'][0]['snapshot']['nodes'][0]['effective_purposes']
    result['validate_current']()
    assert env.wires == [] and facts(env) == before
    assert local.retrieval_index.worker is None and local.retrieval_index.pending == set()
    assert env.query.retrieval_index.read_only is False
    assert not (env.root / 'recognition-vectors.sqlite3').exists()
    assert env.records.list('v2_profile_blocks') == ()


def test_local_coverage_requires_a_separate_readonly_index(env):
    document(env)
    with pytest.raises(ValueError, match='readonly'):
        env.query.local_coverage('alpha', 'alpha')
    assert env.wires == []


@pytest.mark.parametrize('broken_project, expected', [('alpha', 'unknown'), ('other', 'known')])
def test_missing_projection_is_unknown_only_for_its_real_project(env, broken_project, expected):
    document(env)
    missing, _ = document(env, project=broken_project)
    row = env.records.read(INDEX, missing)
    with env.records.begin() as tx:
        tx.delete(INDEX, missing, expected_revision=row.revision)
        tx.commit()
    local = readonly(env)
    before = facts(env)
    result = local.local_coverage('alpha', 'alpha beta gamma?')
    assert result['status'] == expected
    assert result['coverage'] is None if expected == 'unknown' else result['coverage'] == 1.0
    assert ('document', missing) in local.retrieval_index.unavailable
    assert local.retrieval_index.worker is None and env.wires == []
    assert facts(env) == before and env.records.read(INDEX, missing) is None


def test_explicit_default_flag_preserves_real_default_candidates_and_embedding(env):
    document(env)
    before = env.query.collect_candidates('alpha', 'alpha beta gamma?')
    assert any(kind == 'embedding' for kind, _ in env.wires)
    after = env.query.collect_candidates('alpha', 'alpha beta gamma?', local_only=False)
    assert after == before


@pytest.mark.parametrize('damage', ['stale', 'bad_window', 'malformed'])
def test_untrusted_document_projection_is_unknown_without_repair(env, damage):
    doc, _ = document(env)
    row = env.records.read(INDEX, doc)
    payload = {**row.payload}
    if damage == 'stale':
        payload['document_revision'] -= 1
    elif damage == 'malformed':
        payload['entry'] = None
    else:
        payload['spans'] = [{**span, 'chunks': [{**chunk, 'text': chunk['text'] + ' alpha corruption'}
            for chunk in span['chunks']]} for span in payload['spans']]
    with env.records.begin() as tx:
        tx.put(INDEX, doc, payload, expected_revision=row.revision)
        tx.commit()
    local, before = readonly(env), facts(env)
    result = local.local_coverage('alpha', 'alpha beta gamma?')
    assert result['status'] == 'unknown' and result['coverage'] is None and result['chosen'] == []
    assert local.retrieval_index.worker is None and env.wires == []
    assert facts(env) == before and env.records.read(INDEX, doc).payload == payload


def test_live_source_without_its_namespace_projection_is_unknown(env):
    from core.storage_provider.source_retrieval_index import COLLECTION
    _, source = document(env)
    records = env.query.retrieval_index.source_records
    row = records.read(COLLECTION, source)
    with records.begin() as tx:
        tx.delete(COLLECTION, source, expected_revision=row.revision)
        tx.commit()
    local, before = readonly(env), facts(env)
    result = local.local_coverage('alpha', 'alpha beta gamma?')
    assert result['status'] == 'unknown' and ('source', source) in local.retrieval_index.unavailable
    assert env.wires == [] and facts(env) == before and records.read(COLLECTION, source) is None


def test_cold_source_index_stays_absent_and_returns_unknown(env):
    document(env)
    local = readonly(env)
    path = local.retrieval_index.source_records.database_path
    for suffix in ('-wal', '-shm', ''):
        Path(str(path) + suffix).unlink(missing_ok=True)
    assert not path.exists()
    before = facts(env)
    result = local.local_coverage('alpha', 'alpha beta gamma?')
    assert result['status'] == 'unknown' and result['coverage'] is None
    assert not path.exists() and facts(env) == before
    assert env.wires == [] and local.retrieval_index.worker is None


@pytest.mark.parametrize('damage', ['empty_database', 'missing_trigger'])
def test_unready_source_index_schema_is_unknown_and_is_not_initialized(env, damage):
    document(env)
    local = readonly(env)
    path = local.retrieval_index.source_records.database_path
    if damage == 'empty_database':
        for suffix in ('-wal', '-shm', ''):
            Path(str(path) + suffix).unlink(missing_ok=True)
        with sqlite3.connect(path):
            pass
    else:
        with sqlite3.connect(path) as connection:
            connection.execute('DROP TRIGGER crp_generation_after_insert')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        before_schema = tuple(connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name'))
    before = facts(env)
    result = local.local_coverage('alpha', 'alpha beta gamma?')
    assert result['status'] == 'unknown' and result['coverage'] is None
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        assert tuple(connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')) == before_schema
    assert env.wires == [] and facts(env) == before and local.retrieval_index.worker is None


@pytest.mark.parametrize('changed_owner', ['document', 'source'])
def test_real_cas_during_hydration_returns_unknown(env, monkeypatch, changed_owner):
    doc, source = document(env)
    local = readonly(env)
    original = local.retrieval_index.hydrate
    changed = []
    def observe(project, entry):
        result = original(project, entry)
        if entry['id'] == doc and not changed:
            if changed_owner == 'document':
                env.documents.save_user_edit(doc, expected_revision=1,
                    markdown='# Synthetic\n\n## 摘要\nnew alpha beta gamma')
            else:
                payload = dict(env.query.source_store.read('sources', source))
                env.query.source_store.write('sources', source, payload, expected_revision=1)
            changed.append(changed_owner)
        return result
    monkeypatch.setattr(local.retrieval_index, 'hydrate', observe)
    result = local.local_coverage('alpha', 'alpha beta gamma?')
    assert changed == [changed_owner]
    assert result['status'] == 'unknown' and result['coverage'] is None
    assert env.wires == [] and local.retrieval_index.worker is None
    assert (env.documents.read(doc)['revision'] if changed_owner == 'document'
            else env.query.source_store.revision('sources', source)) == 2


@pytest.mark.parametrize('changed_owner', ['source', 'scene'])
def test_returned_guard_rejects_real_source_or_scene_cas(env, changed_owner):
    doc, source = document(env)
    assign_scene(env.records, 'document', doc, 'alpha', 'reading')
    local = readonly(env)
    with override(scope='@2'):
        result = local.local_coverage('alpha', 'alpha beta gamma?', scene='reading')
        assert result['status'] == 'known' and result['coverage'] == 1.0
        result['validate_current']()
        if changed_owner == 'source':
            payload = dict(env.query.source_store.read('sources', source))
            env.query.source_store.write('sources', source, payload, expected_revision=1)
        else:
            assign_scene(env.records, 'document', doc, 'alpha', 'writing')
        before = facts(env)
        with pytest.raises(RecognitionConflict):
            result['validate_current']()
        assert env.wires == [] and facts(env) == before


def published(env, text, project='alpha'):
    scope = WorkScope('local-user', project)
    source = env.service.stage_experience(scope=scope, content='Synthetic explicit user evidence')
    candidate = env.service.propose(scope=scope, content=text, source_experience_ids=[source])
    return env.service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')


def test_private_recognition_and_method_path_use_no_model_and_keep_detail_stop_rule(env):
    insight = published(env, 'alpha 原文')
    set_private_project(env.records, 'alpha', True, 0)
    local, before = readonly(env), facts(env)
    result = local.local_coverage('alpha', 'alpha 原文?')
    assert result['status'] == 'known' and result['coverage'] == 1.0
    assert result['stopped'] is False and [row['id'] for row in result['chosen']] == [insight.id]
    assert {row['layer'] for row in result['chosen']} == {'L3'}
    result['validate_current']()
    assert env.wires == [] and facts(env) == before


def test_scene_forget_archive_and_pending_keep_original_qualification(env):
    doc, _ = document(env)
    assign_scene(env.records, 'document', doc, 'alpha', 'writing')
    forgotten = published(env, 'alpha beta gamma')
    set_preference(env.records, WorkScope('local-user', 'alpha'), forgotten.id,
        recognition_revision=1, preference_revision=0, state='forgotten')
    parent = env.service.stage_experience(scope=WorkScope('local-user', 'alpha'), content='Synthetic pending evidence')
    pending = env.service.propose(scope=WorkScope('local-user', 'alpha'), content='alpha beta gamma',
        source_experience_ids=[parent])
    published(env, 'alpha beta gamma profile', project='me')
    local = readonly(env)
    before = facts(env)
    with override(scope='@2'):
        result = local.local_coverage('alpha', 'alpha beta gamma?', scene='reading')
    assert result['status'] == 'known' and result['coverage'] == 0 and result['chosen'] == []
    result['validate_current']()
    assert env.wires == [] and facts(env) == before
    env.documents.archive(doc, expected_revision=1)
    before = facts(env)
    result = readonly(env).local_coverage('alpha', 'alpha beta gamma?')
    assert result['status'] == 'known' and result['coverage'] == 0 and result['chosen'] == []
    assert env.records.read('recognition_candidates', pending.id).payload['state'] == 'pending'
    assert env.records.list('v2_profile_blocks') == () and env.wires == [] and facts(env) == before


def test_temporal_selection_uses_original_validity_and_ignores_resampled_reference_clock(env):
    from tests.memory_app.v2.test_insight_validity import publish, supersede
    old = publish(env, '春港展会在北厅办小型展')
    new = publish(env, '春港展会改在南厅办大型展', '2026-06-12T00:00:00+00:00')
    supersede(env, old, new)
    set_private_project(env.records, 'alpha', True, 0)
    local, before = readonly(env), facts(env)
    with override(retrieve='@3', compose='@3'):
        result = local.local_coverage('alpha', '春港展会在2026年三月时怎么想的？')
    assert result['status'] == 'known'
    assert {row['id'] for row in result['chosen']} == {old.id}
    assert result['chosen'][0]['temporal'] is True
    result['validate_current']()
    assert env.wires == [] and facts(env) == before


def test_private_condition_method_uses_original_supplement_without_embedding(env):
    from tests.memory_app.v2.test_situation_methods import method
    selected = method(env, '先问清预算和对方近期愿望', ['挑礼物时'])
    set_private_project(env.records, 'alpha', True, 0)
    local, before = readonly(env), facts(env)
    with override(retrieve='@2', compose='@2'):
        result = local.local_coverage('alpha', '给小王送什么生日礼物？')
    assert result['status'] == 'known' and result['coverage'] == 0 and result['stopped'] is False
    assert [row['id'] for row in result['chosen']] == [selected.id]
    assert result['chosen'][0]['supplemented'] is True
    result['validate_current']()
    assert env.wires == [] and facts(env) == before and local.retrieval_index.worker is None


@pytest.mark.parametrize('restored_owner', ['document', 'source'])
def test_same_readonly_owner_sees_real_projection_restoration(env, restored_owner):
    from core.storage_provider.source_retrieval_index import COLLECTION as SOURCE_INDEX
    doc, source = document(env)
    records = env.records if restored_owner == 'document' else env.query.retrieval_index.source_records
    collection, identity = (INDEX, doc) if restored_owner == 'document' else (SOURCE_INDEX, source)
    row = records.read(collection, identity)
    with records.begin() as tx:
        tx.delete(collection, identity, expected_revision=row.revision)
        tx.commit()
    local = readonly(env)
    before = facts(env)
    assert local.local_coverage('alpha', 'alpha beta gamma?')['status'] == 'unknown'
    assert facts(env) == before and env.wires == []
    if restored_owner == 'document':
        env.documents.save_user_edit(doc, expected_revision=1,
            markdown='# Synthetic\n\n## 摘要\nalpha beta gamma\n\n## 正文\nalpha beta gamma')
    else:
        payload = dict(env.query.source_store.read('sources', source))
        env.query.source_store.write('sources', source, payload, expected_revision=1)
    before = facts(env)
    result = local.local_coverage('alpha', 'alpha beta gamma?')
    assert result['status'] == 'known' and result['coverage'] == 1.0 and result['stopped'] is True
    result['validate_current']()
    assert env.wires == [] and facts(env) == before and local.retrieval_index.worker is None


def test_nested_and_concurrent_local_inventory_reset_without_sharing_missing_rows(env):
    from concurrent.futures import ThreadPoolExecutor
    doc, _ = document(env)
    row = env.records.read(INDEX, doc)
    with env.records.begin() as tx:
        tx.delete(INDEX, doc, expected_revision=row.revision)
        tx.commit()
    local = readonly(env)
    index = local.retrieval_index
    original_unavailable = set(index.unavailable)
    assert ('document', doc) in original_unavailable
    with index.readonly_inventory():
        assert ('document', doc) in index.unavailable_for('alpha')
        env.documents.save_user_edit(doc, expected_revision=1,
            markdown='# Synthetic\n\n## 摘要\nalpha beta gamma\n\n## 正文\nalpha beta gamma')
        before = facts(env)
        def check_fresh():
            result = local.local_coverage('alpha', 'alpha beta gamma?')
            assert result['status'] == 'known' and result['coverage'] == 1.0
            result['validate_current']()
            return result['stopped']
        with ThreadPoolExecutor(max_workers=1) as executor:
            concurrent = executor.submit(check_fresh)
            with pytest.raises(RuntimeError, match='synthetic caller interruption'):
                with index.readonly_inventory():
                    assert index.unavailable_for('alpha') == ()
                    raise RuntimeError('synthetic caller interruption')
            assert ('document', doc) in index.unavailable_for('alpha')
            assert check_fresh() is True and concurrent.result(timeout=30) is True
            assert ('document', doc) in index.unavailable_for('alpha')
        assert facts(env) == before and env.wires == []
    assert index.unavailable == original_unavailable
    assert ('document', doc) in index.unavailable_for('alpha')
    assert index.worker is None and index.pending == set()
