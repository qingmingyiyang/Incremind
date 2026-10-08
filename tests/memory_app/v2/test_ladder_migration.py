import asyncio
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.memory_app.workspace import install_workspace_routes
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


def tool():
    path = Path(__file__).parents[3] / 'tools' / 'migrate_to_ladder.py'
    spec = importlib.util.spec_from_file_location('ladder_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / '.rebuild-data' / 'structured-records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    service = RecognitionService(records)
    class Model:
        def complete(self, messages, **kwargs):
            return json.dumps({'title': 'Synthetic note', 'summary': 'Synthetic summary',
                'facts': [], 'topics': [], 'todos': [], 'uncertainties': [], 'people': [],
                'dates': [], 'suggestions': []}), {}
    domains = install_workspace_routes(FastAPI(), runtime_root=tmp_path, records=records,
        documents=documents, service=service, models=Model())
    item = asyncio.run(domains.intake.add_text({'project_id': 'alpha', 'text': 'PRIVATE BODY'}))
    done = asyncio.run(process_and_confirm(domains, item['id'], 'alpha'))
    item = records.read('workspace_items', item['id']).payload
    legacy = {'memory_atoms': [{'id': 'atom-one', 'revision': 1, 'content': 'PRIVATE ATOM',
        'source_id': item['source_id'], 'source_refs': [], 'lifecycle_status': 'active'}],
        'memory_scenarios': [{'id': 'scenario-one', 'project_id': 'alpha', 'title': 'Reading',
            'atom_ids': ['atom-one'], 'series_id': 'series-one'}]}
    return SimpleNamespace(root=tmp_path, records=records, documents=documents, service=service,
        legacy=legacy, item=item, doc=done['document_id'])


def run(env):
    return tool().migrate(env.records, env.documents, lambda c: env.legacy.get(c, ()))


def test_atom_is_pending_and_original_records_unchanged(env):
    before = env.documents.read(env.doc), dict(env.item)
    report = run(env)
    candidate = env.records.read('recognition_candidates', 'candidate-legacy-atom-one-r1')
    assert candidate.payload['state'] == 'pending'
    assert candidate.payload['source_experience_ids'] == [f'experience-legacy-{env.doc}-r1']
    assert env.records.list('recognitions') == ()
    assert (env.documents.read(env.doc), env.records.read('workspace_items', env.item['id']).payload) == before
    assert report['added']['candidates'] == 1
    assert 'PRIVATE' not in json.dumps(report)


def test_exact_user_publication_restores_active_and_second_run_adds_nothing(env):
    env.legacy['memory_publications'] = [{'id': 'publication-one', 'published_object_id': 'atom-one',
        'layer': 'atom', 'object_type': 'atom', 'published_revision': 1, 'status': 'published',
        'reviewer': 'user', 'review_ref': 'review-one'}]
    report = run(env)
    recognition = env.service.get_recognition(scope=WorkScope('local-user', 'alpha'),
        recognition_id='recognition-legacy-atom-one-r1')
    assert recognition.authorized and recognition.state == 'active'
    assert report['added']['recognitions'] == 1
    revisions = [(r.object_id, r.revision) for r in env.records.list('recognitions')]
    again = run(env)
    assert all(n == 0 for n in again['added'].values())
    assert revisions == [(r.object_id, r.revision) for r in env.records.list('recognitions')]


@pytest.mark.parametrize('change', [{'published_revision': 2}, {'reviewer': 'ai'}, {'review_ref': ''},
                                    {'layer': 'project_skill', 'object_type': 'project_skill'}])
def test_unproven_publication_never_publishes(env, change):
    env.legacy['memory_publications'] = [{**{'id': 'publication-one', 'published_object_id': 'atom-one',
        'layer': 'atom', 'object_type': 'atom', 'published_revision': 1, 'status': 'published',
        'reviewer': 'user', 'review_ref': 'review-one'}, **change}]
    run(env)
    assert env.records.list('recognitions') == ()


def test_ambiguous_project_and_composite_skill_are_reported_without_invention(env):
    env.legacy['memory_scenarios'].append({'id': 'scenario-two', 'project_id': 'beta',
        'title': 'Other', 'atom_ids': ['atom-one']})
    env.legacy['project_skills'] = [{'id': 'skill-alpha', 'project_id': 'alpha',
        'output_rules': [{'rule': 'PRIVATE RULE', 'origin': 'ai'}], 'style_preferences': ['private']}]
    report = run(env)
    assert env.records.list('recognition_candidates') == ()
    assert env.records.list('project_constraints') == ()
    assert {s['reason'] for s in report['skipped']} == {'atom_project_ambiguous', 'skill_projection_unspecified'}
    assert 'PRIVATE' not in json.dumps(report)


def test_scenario_and_named_series_register_scenes_but_overview_is_not_name(env):
    env.legacy['library_series'] = [{'id': 'series-one', 'name': 'Books', 'status': 'active'}]
    env.legacy['memory_series_memory'] = [{'id': 'series-memory-one', 'series_id': 'series-one',
        'project_ids': ['alpha'], 'overview': 'PRIVATE OVERVIEW'}]
    report = run(env)
    assert env.records.read('v2_projects', 'alpha').payload['scenes'] == ['Books', 'Reading']
    assert report['added']['scenes'] == 2
    env.legacy['library_series'] = []
    report = run(env)
    assert 'series_name_unavailable' in [s['reason'] for s in report['skipped']]


def test_unavailable_source_is_not_replaced_with_unqualified_experience(env):
    env.legacy['memory_atoms'][0]['source_id'] = 'source-missing'
    report = run(env)
    assert env.records.list('recognition_experiences') == ()
    assert report['skipped'][0]['reason'] == 'atom_document_unavailable'


def test_empty_dry_run_does_not_create_database_or_output_bodies(tmp_path):
    report = tool().run_migration(tmp_path, dry_run=True)
    assert report['mode'] == 'dry-run' and all(n == 0 for n in report['added'].values())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('state', ['confirmed', 'pending'])
def test_existing_source_review_only_marks_actually_confirmed_document(env, state):
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.memory_app.legacy_intake_review import LegacyIntakeReview
    from core.job_runner.runtime import InMemoryJobRepository
    source = env.item['source_id']
    intent = {'id': 'review-' + source, 'source_id': source, 'source_revision': 1,
        'project_id': 'alpha', 'state': 'pending', 'job_id': 'job-synthetic'}
    with env.records.begin() as tx:
        tx.put('workspace_review_intents', intent['id'], intent, expected_revision=0)
        tx.commit()
    store, _ = build_rebuild_object_store(env.root)
    jobs = InMemoryJobRepository()
    jobs.save({'id': 'job-synthetic', 'status': 'completed', 'source_id': source})
    reviews = LegacyIntakeReview(env.root, env.records, env.documents, object_store=store, jobs=jobs)
    if state == 'confirmed':
        projection = reviews.get(source, 'alpha')
        confirmed = reviews.confirm(source, 'alpha', expected_revision=projection['revision'],
            expected_document_basis=projection['document_basis'], expected_markdown=projection['draft_markdown'])
        assert confirmed['status'] == 'confirmed'
        intent = env.records.read('workspace_review_intents', intent['id']).payload
    env.legacy = {'workspace_review_intents': [intent]}
    before = env.documents.read(env.doc), env.records.read('workspace_review_intents', intent['id'])
    report = tool().migrate(env.records, env.documents, lambda c: env.legacy.get(c, ()), reviews=reviews)
    marker = env.records.read('v2_verifications', env.doc)
    assert (marker is not None) == (state == 'confirmed')
    assert report['added']['verified'] == (1 if state == 'confirmed' else 0)
    assert (env.documents.read(env.doc), env.records.read('workspace_review_intents', intent['id'])) == before


def test_cli_dry_run_and_two_commits_keep_legacy_json_and_do_not_log_bodies(tmp_path):
    from core.storage_provider import JsonObjectStore
    store = JsonObjectStore(tmp_path / '.rebuild-data', legacy_root=tmp_path / 'library', namespace_id='default')
    scenario = {'id': 'scenario-one', 'title': 'Reading', 'project_id': 'alpha', 'atom_ids': []}
    store.write('memory_scenarios', scenario['id'], scenario, expected_revision=0)
    store.write('project_skills', 'skill-alpha', {'id': 'skill-alpha', 'markdown': 'PRIVATE BODY'}, expected_revision=0)
    def cli(flag):
        result = subprocess.run([sys.executable, str(Path(__file__).parents[3] / 'tools/migrate_to_ladder.py'),
            '--app-root', str(tmp_path), flag], capture_output=True, text=True, check=True)
        assert 'PRIVATE' not in result.stdout + result.stderr
        return json.loads(result.stdout)
    files = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*') if p.is_file()}
    dry = cli('--dry-run')
    assert dry['added']['scenes'] == 1
    assert files == {str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*') if p.is_file()}
    first, second = cli('--commit'), cli('--commit')
    assert first['added']['scenes'] == 1 and all(n == 0 for n in second['added'].values())
    assert store.read('memory_scenarios', scenario['id']) == scenario
    assert store.revision('memory_scenarios', scenario['id']) == 1


def test_workspace_source_identity_is_reused_and_missing_identity_never_rewrites_item(env):
    from core.storage_provider import JsonObjectStore
    store = JsonObjectStore(env.root / '.rebuild-data', legacy_root=env.root / 'library', namespace_id='default')
    env.legacy = {'workspace_items': [env.item], 'sources': list(store.list('sources'))}
    before = env.records.read('workspace_items', env.item['id'])
    report = run(env)
    assert report['samples'] == [{'type': 'workspace_source', 'id': env.item['id'],
        'target_id': 'source-' + env.item['id'], 'state': 'retained'}]
    env.legacy['workspace_items'] = [{**env.item, 'source_id': None}]
    report = run(env)
    assert report['skipped'][0]['reason'] == 'workspace_source_backfill_required'
    assert env.records.read('workspace_items', env.item['id']) == before


@pytest.mark.parametrize('project,name', [('me', '我'), ('inbox', '收件箱')])
def test_builtin_scene_registration_preserves_existing_project_semantics(env, project, name):
    from backend.memory_app.v2.privacy import set_private_project
    set_private_project(env.records, project, True, 0)
    env.legacy = {'memory_scenarios': [{'id': 'scenario-builtin', 'project_id': project,
                                       'title': 'Reading', 'atom_ids': []}]}
    run(env)
    payload = env.records.read('v2_projects', project).payload
    assert payload == {'name': name, 'scenes': ['Reading'], 'builtin': project, 'private': True}


def test_dry_run_copies_real_long_media_path_and_sqlite_without_mutating_source(tmp_path):
    extended = Path('\\\\?\\' + str(tmp_path)) if os.name == 'nt' else tmp_path
    media = extended / 'library' / ('media-' + 'a' * 110) / ('nested-' + 'b' * 110) / 'asset.json'
    assert len(str(media)) > 260
    media.parent.mkdir(parents=True)
    media.write_text('{"body":"PRIVATE LONG MEDIA"}', encoding='utf-8')
    records = SQLiteStructuredRecordStore(tmp_path / '.rebuild-data' / 'structured-records.sqlite3')
    with records.begin() as tx:
        tx.put('probe', 'probe-one', {'body': 'PRIVATE DATABASE'}, expected_revision=0)
        tx.commit()
    before = records.read('probe', 'probe-one')
    long_database = SQLiteStructuredRecordStore(media.parent / 'archive.sqlite3')
    with long_database.begin() as tx:
        tx.put('probe', 'probe-long', {'body': 'PRIVATE LONG DATABASE'}, expected_revision=0)
        tx.commit()
    long_before = long_database.read('probe', 'probe-long')
    connection = sqlite3.connect(tool()._sqlite_read_uri(media.parent / 'archive.sqlite3'), uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            connection.execute('PRAGMA user_version = 99')
    finally:
        connection.close()
    report = tool().run_migration(tmp_path, dry_run=True)
    assert report['mode'] == 'dry-run'
    assert 'PRIVATE' not in json.dumps(report)
    assert records.read('probe', 'probe-one') == before
    assert long_database.read('probe', 'probe-long') == long_before
    assert media.read_text(encoding='utf-8') == '{"body":"PRIVATE LONG MEDIA"}'
    assert not (tmp_path / 'recognition.sqlite3').exists()
