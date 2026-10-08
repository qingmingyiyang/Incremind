"""Read-only integrity checks against real temporary repositories."""
import sqlite3
from contextlib import ExitStack, closing
from datetime import datetime

import pytest

from core.document_engine.ports import DocumentDraft
from tests.memory_app.v2.test_workbench_ask import env
from tests.memory_app.v2.test_settings import env as app_env


def item(env, *, path=None):
    return env.domains.items.create('alpha', 'file' if path else 'text', 'Original', 'body',
                                   **({'original_path': str(path)} if path else {}))


def document(env, source_id, *, archived=False):
    title = 'Draft ' + source_id + (' archived' if archived else '')
    doc = env.documents.create(DocumentDraft(title, 'note', '# ' + title,
        ({'source_id': source_id, 'locator': 'workspace://' + source_id},), 'alpha'))
    if archived:
        env.documents.archive(doc['id'], expected_revision=1)
    return doc


def check(env):
    response = env.http.get('/api/v2/settings/integrity')
    assert response.status_code == 200, response.text
    value = response.json()
    assert set(value) == {'ok', 'checked_at', 'problems'}
    assert datetime.fromisoformat(value['checked_at']).tzinfo is not None
    return value


def test_clean_runtime_is_ok_without_creating_files_or_mutating_records(env):
    path = env.root / 'original.txt'
    path.write_text('Original', encoding='utf-8')
    source = item(env, path=path)
    document(env, source['id'], archived=True)
    before = [(r.object_id, r.revision, r.payload) for r in env.records.list('workspace_items')]
    # Pin the existing WAL databases as a running server does. SQLite may
    # otherwise create its own ephemeral WAL/SHM files even for mode=ro.
    with ExitStack() as stack:
        for database_path in env.root.rglob('*.sqlite3'):
            connection = stack.enter_context(closing(sqlite3.connect(database_path)))
            connection.execute('SELECT * FROM sqlite_master').fetchall()
        files = {p.relative_to(env.root) for p in env.root.rglob('*') if p.is_file()}
        assert check(env)['problems'] == []
        assert check(env)['ok'] is True
        assert before == [(r.object_id, r.revision, r.payload) for r in env.records.list('workspace_items')]
        assert files == {p.relative_to(env.root) for p in env.root.rglob('*') if p.is_file()}
    assert path.read_text(encoding='utf-8') == 'Original'


@pytest.mark.parametrize('suffix', ['.sqlite3', '.sqlite', '.db'])
def test_corrupt_sqlite_is_counted_without_repair(env, suffix):
    path = env.root / 'nested' / ('broken' + suffix)
    path.parent.mkdir()
    path.write_bytes(b'not a database')
    assert check(env)['problems'] == [{'code': 'sqlite_corrupt', 'count': 1}]
    assert check(env)['ok'] is False
    assert path.read_bytes() == b'not a database'


def test_live_wal_database_is_checked_and_not_checkpointed(env):
    path = env.root / 'live.db'
    with sqlite3.connect(path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE live (value TEXT)')
        writer.execute("INSERT INTO live VALUES ('retained')")
        writer.commit()
        wal = path.with_name(path.name + '-wal')
        before = (path.read_bytes(), wal.read_bytes())
        assert check(env)['ok'] is True
        assert before == (path.read_bytes(), wal.read_bytes())


def test_only_linked_file_originals_are_missing_and_duplicate_refs_count_once(env):
    source = item(env, path=env.root / 'private-missing.txt')
    document(env, source['id'])
    document(env, source['id'], archived=True)
    text = item(env)
    document(env, text['id'])
    value = check(env)
    assert value['problems'] == [{'code': 'source_file_missing', 'count': 1}]
    assert 'private-missing' not in str(value)


def test_orphans_include_unprocessed_originals_but_not_existing_drafts(env):
    item(env)
    source = item(env)
    with env.records.begin() as tx:
        row = tx.read('workspace_items', source['id'])
        tx.put('workspace_items', source['id'], {**row.payload, 'draft': {'title': 'Draft'}},
               expected_revision=row.revision)
        tx.commit()
    assert check(env)['problems'] == [{'code': 'source_without_document', 'count': 1}]


def test_legacy_sources_use_authorized_local_paths_and_count_unlinked_sources(env):
    store = env.domains.query.source_store
    store.write('sources', 'legacy-file', {'id': 'legacy-file', 'type': 'file',
        'project_id': 'alpha', 'metadata': {'file_reference': 'file-ref://original'}}, expected_revision=None)
    store.write('authorized_file_refs', 'authorized-file-legacy-file', {
        'id': 'authorized-file-legacy-file', 'source_id': 'legacy-file',
        'path': str(env.root / 'missing.pdf'), 'status': 'authorized'}, expected_revision=None)
    store.write('sources', 'orphan-text', {'id': 'orphan-text', 'type': 'text',
        'project_id': 'beta', 'metadata': {'content_snapshot': 'body'}}, expected_revision=None)
    document(env, 'legacy-file')
    assert check(env)['problems'] == [
        {'code': 'source_file_missing', 'count': 1},
        {'code': 'source_without_document', 'count': 1}]


def test_uploaded_legacy_asset_checks_vault_file_without_requiring_authorization(env):
    store = env.domains.query.source_store
    store.write('sources', 'uploaded', {'id': 'uploaded', 'type': 'file', 'project_id': 'alpha'}, expected_revision=None)
    store.write('source_asset_links', 'link-uploaded', {'id': 'link-uploaded', 'source_id': 'uploaded',
        'asset_id': 'asset-uploaded'}, expected_revision=None)
    store.write('workbench_original_assets', 'asset-uploaded', {'id': 'asset-uploaded',
        'vault_ref': 'assets/originals/uploaded.bin'}, expected_revision=None)
    document(env, 'uploaded')
    assert check(env)['problems'] == [{'code': 'source_file_missing', 'count': 1}]


def test_every_sqlite_connection_is_read_only(env, monkeypatch):
    import backend.memory_app.v2.settings as settings
    original = sqlite3.connect
    connections = []
    def connect(path, *args, **kwargs):
        connections.append((str(path), kwargs.get('uri')))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(settings.sqlite3, 'connect', connect)
    assert check(env)['ok'] is True
    assert connections and all('mode=ro' in path and uri for path, uri in connections)


def test_three_problem_counts_are_reported_together(env):
    (env.root / 'broken.sqlite3').write_bytes(b'broken')
    source = item(env, path=env.root / 'missing.txt')
    document(env, source['id'])
    item(env)
    assert check(env)['problems'] == [
        {'code': 'sqlite_corrupt', 'count': 1},
        {'code': 'source_file_missing', 'count': 1},
        {'code': 'source_without_document', 'count': 1}]


def test_sqlite_header_is_detected_without_a_standard_extension(env):
    path = env.root / 'renamed.data'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE saved (value TEXT)')
    # Retain the SQLite header but corrupt the btree header on the first page.
    content = bytearray(path.read_bytes())
    content[100] = 0
    path.write_bytes(content)
    assert check(env)['problems'] == [{'code': 'sqlite_corrupt', 'count': 1}]


def test_real_application_installs_integrity(app_env):
    app, client = app_env
    response = client.get('/api/v2/settings/integrity')
    assert response.status_code == 200, response.text
    assert response.json()['ok'] is True
    assert response.json()['problems'] == []


def test_temporarily_locked_database_is_not_reported_as_corrupt(env):
    with sqlite3.connect(env.root / 'locked.db') as writer:
        writer.execute('CREATE TABLE retained (value TEXT)')
        writer.commit()
        writer.execute('BEGIN EXCLUSIVE')
        response = env.http.get('/api/v2/settings/integrity')
        assert response.status_code == 503
        assert response.json() == {'detail': 'integrity_unavailable'}
