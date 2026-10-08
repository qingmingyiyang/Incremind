import json
from pathlib import Path
import pytest

from tests.memory_app.v2.test_workbench_ask import env, add_document, publish
from backend.memory_app.recall_preferences import set_preference
from backend.memory_app.v2.projects import assign_scene
from backend.recognition import WorkScope


def get(env, layer, **params):
    return env.http.get('/api/v2/library/' + layer, params={'project_id': 'alpha', **params})


def test_four_layers_counts_aliases_and_read_only(env):
    doc, item = add_document(env, summary='Alpha summary')
    recognition, experience = publish(env, doc=doc)
    pending = env.service.propose(scope=WorkScope('local-user', 'alpha'), content='Pending',
                                  source_experience_ids=[experience])
    before = [(c, r.object_id, r.revision, r.payload) for c in ('workspace_items', 'recognitions', 'documents')
              for r in env.records.list(c)]
    insights = get(env, 'insights')
    assert insights.status_code == 200, insights.text
    data = insights.json()
    assert len(data['items']) == 2
    assert data['counts'] == {'active': 1, 'pending': 1, 'stale': 0, 'forgotten': 0}
    assert {r['id'] for r in data['items']} == {recognition.id, pending.id}
    assert next(r for r in data['items'] if r['id'] == recognition.id)['document_ids'] == [doc]
    for layer in ('summaries', 'notes', 'sources'):
        response = get(env, layer)
        assert response.status_code == 200, response.text
        assert len(response.json()['items']) == 1
    assert get(env, 'sources').json()['items'][0]['id'] == item
    assert get(env, 'summaries').json()['items'][0]['source_type'] == 'text'
    assert before == [(c, r.object_id, r.revision, r.payload) for c in ('workspace_items', 'recognitions', 'documents')
                      for r in env.records.list(c)]


def test_forgotten_count_precedes_active_and_search_state(env):
    recognition, _ = publish(env, text='Alpha HELLO')
    set_preference(env.records, WorkScope('local-user', 'alpha'), recognition.id,
                   recognition_revision=1, preference_revision=0, state='forgotten')
    data = get(env, 'insights', q='hello', state='forgotten').json()
    assert len(data['items']) == 1 and data['items'][0]['state'] == 'forgotten'
    assert data['counts']['forgotten'] == 1 and data['counts']['active'] == 0
    assert get(env, 'insights', state='active').json()['items'] == []


def test_project_scene_and_casefold_filters(env):
    doc, item = add_document(env, summary='MIXEDcase', scene='reading')
    publish(env, doc=doc)
    add_document(env, project='beta', summary='MIXEDcase', scene='reading')
    add_document(env, scene='writing', summary='other')
    for layer in ('insights', 'summaries', 'notes', 'sources'):
        response = get(env, layer, scene='reading')
        assert response.status_code == 200, response.text
        assert len(response.json()['items']) == 1
        assert get(env, layer, scene='missing').json()['items'] == []
    assert len(get(env, 'summaries', q='mixedCASE').json()['items']) == 1
    assert len(get(env, 'notes', q='mixedCASE').json()['items']) == 1


@pytest.mark.parametrize('historical', [True, False], ids=['historical-place1', 'default-place2'])
def test_forgotten_note_remains_readable_and_restores_summary(env, historical):
    from contextlib import nullcontext
    from backend.memory_app.v2.policies import override, version
    with override(place='@1') if historical else nullcontext():
        if not historical:
            assert version('place') == '@2'
        _assert_forgotten_note_remains_readable_and_restores_summary(env, historical)


def _assert_forgotten_note_remains_readable_and_restores_summary(env, historical):
    doc, item = add_document(env, summary='Retained summary', scene='reading')
    archived = env.documents.archive(doc, expected_revision=2)
    assert get(env, 'summaries').json()['items'] == []
    note = get(env, 'notes', scene='reading', q='retained').json()['items'][0]
    assert note['document_id'] == doc and note['revision'] == archived['revision']
    assert note['verified'] is True
    if historical:
        assert set(note) == {'document_id', 'title', 'created_at', 'verified', 'revision', 'recall_state'}
    else:
        assert set(note) == {'document_id', 'title', 'created_at', 'verified', 'revision', 'recall_state',
                             'scene', 'assignment_revision'}
        assert note['scene'] == 'reading'
        assert note['assignment_revision'] == env.records.read('v2_scene_assignments_document', doc).revision
    assert note['recall_state'] == 'normal'
    assert get(env, 'notes', scene='other').json()['items'] == []
    assert get(env, 'notes', project_id='beta').json()['items'] == []
    assert [row['id'] for row in get(env, 'sources', scene='reading').json()['items']] == [item]
    detail = get(env, 'drill', **{'from': 'note', 'id': doc}).json()
    assert detail['note']['markdown'] == env.documents.markdown(doc)
    assert detail['note']['revision'] == archived['revision'] and detail['summary'] is None
    assert get(env, 'drill', **{'from': 'summary', 'id': doc}).status_code == 404
    assert get(env, 'drill', **{'from': 'source', 'id': item}).status_code == 200
    assert env.domains.query.query_entries('alpha') == []
    restored = env.documents.restore(doc, expected_revision=archived['revision'])
    assert get(env, 'summaries').json()['items'][0]['summary'] == 'Retained summary'
    assert get(env, 'notes').json()['items'][0]['revision'] == restored['revision']
    assert get(env, 'notes').json()['items'][0]['verified'] is True


def test_forgotten_legacy_source_preserves_scope_and_review_visibility(env):
    doc, _ = add_document(env)
    env.documents.archive(doc, expected_revision=2)
    store = env.domains.query.source_store
    for identity, project in [('legacy-source', 'alpha'), ('foreign-source', 'beta')]:
        store.write('sources', identity, {'id': identity, 'project_id': project, 'title': identity,
            'type': 'text', 'metadata': {'content_snapshot': 'Original'}}, expected_revision=0)
    with env.records.begin() as tx:
        row = tx.read('documents', doc)
        tx.put('documents', doc, {**row.payload, 'source_refs': [
            {'source_id': identity, 'locator': 'source://' + identity}
            for identity in ('legacy-source', 'foreign-source')]}, expected_revision=row.revision)
        tx.commit()
    identities = {row['id'] for row in get(env, 'sources').json()['items']}
    assert 'legacy-source' in identities and 'foreign-source' not in identities
    assert env.domains.query.query_entries('alpha') == []
    with env.records.begin() as tx:
        tx.put('workspace_review_intents', 'pending-review', {'source_id': 'legacy-source',
            'project_id': 'alpha', 'state': 'pending'}, expected_revision=0)
        tx.commit()
    assert get(env, 'sources').json()['items'] == []
    assert get(env, 'notes').json()['items'] == []


def test_pending_source_hidden_and_standalone_json_source_visible(env):
    store = env.domains.query.source_store
    payload = {'id': 'legacy-source', 'project_id': 'alpha', 'title': 'Legacy', 'type': 'text',
               'metadata': {'content_snapshot': 'Original'}, 'created_at': '2026-01-01'}
    store.write('sources', 'legacy-source', payload, expected_revision=0)
    assert get(env, 'sources').json()['items'][0]['id'] == 'legacy-source'
    with env.records.begin() as tx:
        tx.put('workspace_review_intents', 'review-one', {'source_id': 'legacy-source',
                 'project_id': 'alpha', 'state': 'pending'}, expected_revision=0)
        tx.commit()
    assert get(env, 'sources').json()['items'] == []


def test_drill_preserves_markdown_and_reverse_grown_without_fabricated_window(env):
    doc, item = add_document(env, summary='Actual summary', original='Actual original')
    recognition, _ = publish(env, doc=doc)
    response = get(env, 'drill', **{'from': 'insight', 'id': recognition.id})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['insight']['id'] == recognition.id
    assert data['summary']['text'] == 'Actual summary'
    assert data['note']['markdown'] == env.documents.markdown(doc)
    assert data['note']['facts'] == []
    assert data['source']['id'] == item and data['source']['window'] is None
    reverse = get(env, 'drill', **{'from': 'source', 'id': item})
    assert reverse.status_code == 200, reverse.text
    assert [r['id'] for r in reverse.json()['grown']] == [recognition.id]
    assert get(env, 'drill', project_id='beta', **{'from': 'source', 'id': item}).status_code == 404


def test_migrated_candidate_visible(env):
    import importlib.util
    from pathlib import Path
    doc, item = add_document(env)
    source = env.records.read('workspace_items', item).payload['source_id']
    legacy = {'memory_atoms': [{'id': 'old-atom', 'revision': 1, 'content': 'Migrated alpha',
                'source_id': source, 'lifecycle_status': 'active'}],
              'memory_scenarios': [{'id': 'old-scene', 'project_id': 'alpha', 'title': 'reading',
                                    'atom_ids': ['old-atom']}]}
    spec = importlib.util.spec_from_file_location('migration_for_library', Path(__file__).parents[3] / 'tools/migrate_to_ladder.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.migrate(env.records, env.documents, lambda c: legacy.get(c, ()))
    data = get(env, 'insights', scene='reading').json()
    assert len(data['items']) == 1
    assert data['items'][0]['text'] == 'Migrated alpha'
    assert data['items'][0]['document_ids'] == [doc]


def test_drill_uses_real_fact_window_and_current_todos(env):
    import asyncio
    from backend.memory_app.v2.auto_confirm import process_and_confirm
    original = '前' * 110 + 'Original unique quote' + '后' * 110
    class Model:
        def complete(self, messages, **kwargs):
            return json.dumps({'title': 'Evidence note', 'summary': 'Summary',
                'facts': [{'text': 'Grounded fact', 'evidence': {'quote': 'Original unique quote'}}],
                'topics': [], 'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}), {}
    previous = env.domains.intake.models
    env.domains.intake.models = Model()
    try:
        item = asyncio.run(env.domains.intake.add_text({'project_id': 'alpha', 'text': original}))
        done = asyncio.run(process_and_confirm(env.domains, item['id'], 'alpha'))
    finally:
        env.domains.intake.models = previous
    doc = done['document_id']
    markdown = env.documents.markdown(doc) + '\n## 待办\n- Current todo\n'
    env.documents.save_user_edit(doc, expected_revision=1, markdown=markdown)
    data = get(env, 'drill', **{'from': 'note', 'id': doc}).json()
    assert data['note']['todos'] == ['Current todo']
    assert data['note']['facts'] == [{'text': 'Grounded fact', 'evidence': {
        'start': 110, 'end': 131, 'quote': 'Original unique quote'}}]
    assert data['source']['window'] == {'pre': '前' * 80, 'quote': 'Original unique quote', 'post': '后' * 80}
    env.documents.save_user_edit(doc, expected_revision=2, markdown=markdown.replace('Grounded fact', 'Edited fact'))
    edited = get(env, 'drill', **{'from': 'note', 'id': doc}).json()
    assert edited['note']['facts'] == []
    assert edited['source']['id'] == item['id'] and edited['source']['window'] is None
    assert 'Edited fact' in edited['note']['markdown']
    env.documents.save_user_edit(doc, expected_revision=3, markdown=markdown)
    env.domains.query.source_store.write('sources', 'second-source', {
        'id': 'second-source', 'project_id': 'alpha', 'title': 'Second source', 'type': 'text',
        'metadata': {'content_snapshot': 'Different original'}}, expected_revision=0)
    # Represent an existing multi-source document, then verify the GET is read-only.
    with env.records.begin() as tx:
        row = tx.read('documents', doc)
        tx.put('documents', doc, {**row.payload, 'source_refs': [*row.payload['source_refs'],
            {'source_id': 'second-source', 'locator': 'source://second-source'}]}, expected_revision=row.revision)
        tx.commit()
    before = env.records.read('documents', doc)
    multiple = get(env, 'drill', **{'from': 'source', 'id': 'second-source'}).json()
    assert multiple['source']['id'] == 'second-source' and multiple['source']['window'] is None
    assert multiple['note']['markdown'] == markdown
    assert env.records.read('documents', doc) == before


def test_json_source_id_collision_does_not_search_other_project_text(env):
    doc, item = add_document(env, project='beta', original='PRIVATE OTHER PROJECT')
    env.domains.query.source_store.write('sources', item, {
        'id': item, 'project_id': 'alpha', 'title': 'Public alpha', 'type': 'text',
        'metadata': {'content_snapshot': 'PUBLIC ORIGINAL'}}, expected_revision=0)
    assert len(get(env, 'sources', q='public original').json()['items']) == 1
    assert get(env, 'sources', q='PRIVATE OTHER PROJECT').json()['items'] == []


def text(env, identity, project='alpha'):
    return env.http.get(f'/api/v2/library/sources/{identity}/text', params={'project_id': project})


def legacy_source(env, identity='standalone', project='alpha', **metadata):
    env.domains.query.source_store.write('sources', identity, {
        'id': identity, 'project_id': project, 'title': identity, 'type': 'text',
        'original_url': 'https://example.invalid/original', 'metadata': metadata}, expected_revision=0)


def test_full_text_workspace_preserves_complete_text_and_coordinates(env):
    original = '\n'.join(['完整原文'] * 1200)
    doc, item = add_document(env, original=original)
    response = text(env, item)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['text'] == original
    assert result['coordinate_space'] == 'workspace_source_text_v1'
    assert result['document_ids'] == [doc]
    assert result['download_url'] is None and result['url'] is None
    assert text(env, item, project='beta').status_code == 404
    assert text(env, 'missing').status_code == 404
    env.documents.archive(doc, expected_revision=2)
    assert text(env, item).json()['text'] == original


def test_full_text_standalone_uses_snapshot_then_content_and_scope(env):
    legacy_source(env, content_snapshot='Exact snapshot\n', content='Older content')
    result = text(env, 'standalone')
    assert result.status_code == 200, result.text
    assert result.json()['text'] == 'Exact snapshot\n'
    assert result.json()['coordinate_space'] == 'source_content_v1'
    assert result.json()['document_ids'] == []
    assert result.json()['url'] == 'https://example.invalid/original'
    assert text(env, 'standalone', 'beta').status_code == 404
    legacy_source(env, 'content-only', content='Fallback original')
    assert text(env, 'content-only').json()['text'] == 'Fallback original'
    with env.records.begin() as tx:
        tx.put('workspace_review_intents', 'pending-r1', {'source_id': 'standalone',
            'project_id': 'alpha', 'state': 'pending'}, expected_revision=0)
        tx.commit()
    assert text(env, 'standalone').status_code == 404


def test_drill_multiple_documents_requires_valid_choice(env):
    from backend.memory_app.document_recognition import ensure_document_experience
    first, _ = add_document(env)
    second, _ = add_document(env)
    scope = WorkScope('local-user', 'alpha')
    experiences = [ensure_document_experience(env.documents, env.service, 'alpha', doc)[0]
                   for doc in (first, second)]
    candidate = env.service.propose(scope=scope, content='Two documents', source_experience_ids=experiences)
    recognition = env.service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=1, reviewer='local-user')
    params = {'from': 'insight', 'id': recognition.id}
    response = get(env, 'drill', **params)
    result = response.json()
    assert result['summary'] is None and result['note'] is None and result['source'] is None
    assert {r['document_id'] for r in result['documents']} == {first, second}
    assert result['documents'] == sorted(result['documents'], key=lambda r: (r['title'], r['document_id']))
    selected = get(env, 'drill', **params, document_id=second)
    assert selected.status_code == 200, selected.text
    assert selected.json()['note']['document_id'] == second
    assert get(env, 'drill', **params, document_id='missing').status_code == 404


def test_drill_multiple_sources_and_standalone_without_evidence(env):
    doc, item = add_document(env)
    legacy_source(env, 'second-r1', content_snapshot='Second original')
    with env.records.begin() as tx:
        row = tx.read('documents', doc)
        tx.put('documents', doc, {**row.payload, 'source_refs': [*row.payload['source_refs'],
            {'source_id': 'second-r1', 'locator': 'source://second-r1'}]}, expected_revision=row.revision)
        tx.commit()
    params = {'from': 'note', 'id': doc}
    result = get(env, 'drill', **params).json()
    assert {r['id'] for r in result['sources']} == {item, 'second-r1'}
    assert result['source'] is None
    for identity in (item, 'second-r1'):
        selected = get(env, 'drill', **params, source_id=identity)
        assert selected.status_code == 200, selected.text
        assert selected.json()['source']['id'] == identity
        assert selected.json()['source']['window'] is None
    assert get(env, 'drill', **params, source_id='missing').status_code == 404
    fixed = get(env, 'drill', **{'from': 'source', 'id': 'second-r1'}, source_id=item)
    assert fixed.status_code == 404
    legacy_source(env, 'unlinked-r1', content_snapshot='Unlinked original')
    standalone = get(env, 'drill', **{'from': 'source', 'id': 'unlinked-r1'}).json()
    assert standalone['source']['id'] == 'unlinked-r1'
    assert standalone['source']['window'] is None


@pytest.fixture
def question_payload(env, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(env.root))
    (env.root / 'config').mkdir(exist_ok=True)
    (env.root / 'config/settings.toml').write_bytes(
        (Path(__file__).parents[3] / 'config/settings.toml.example').read_bytes())
    from backend.memory_app.app import _question_payload
    return _question_payload


def test_question_projection_reads_persisted_time_and_evidence_count(env, question_payload):
    first, _ = publish(env, text='First')
    second, _ = publish(env, text='Second')
    question = env.service.upsert_question(scope=WorkScope('local-user', 'alpha'), question_id='r1-question',
        question='Why?', content='Because', recognition_ids=[first.id, second.id],
        source_revisions={first.id: first.revision, second.id: second.revision}, expected_revision=0)
    stored = env.records.read('recognition_questions', question.id)
    assert stored is not None
    result = question_payload(question)
    assert result['updated_at'] == stored.payload['updated_at']
    assert result['evidence_count'] == 2


def test_full_text_coordinates_match_real_question_candidates(env):
    doc, item = add_document(env, original='coordinate needle original')
    legacy_source(env, 'coordinate-source', content_snapshot='standalone coordinate needle')
    candidates = env.domains.query.collect_candidates('alpha', 'coordinate needle')['candidates']
    for identity in (item, 'coordinate-source'):
        candidate = next(c for c in candidates if c['layer'] == 'L0' and c['id'] == identity)
        original = text(env, identity).json()
        assert original['coordinate_space'] == candidate['coordinate_space']
        for window in candidate['windows']:
            assert original['text'][window.start:window.end] == window.text


def test_question_projection_handles_legacy_missing_update_time(env, question_payload):
    recognition, _ = publish(env)
    scope = WorkScope('local-user', 'alpha')
    question = env.service.upsert_question(scope=scope, question_id='legacy-question',
        question='Why?', content='Because', recognition_ids=[recognition.id],
        source_revisions={recognition.id: recognition.revision}, expected_revision=0)
    with env.records.begin() as tx:
        row = tx.read('recognition_questions', question.id)
        payload = dict(row.payload)
        payload.pop('updated_at')
        tx.put('recognition_questions', question.id, payload, expected_revision=row.revision)
        tx.commit()
    result = question_payload(env.service.list_questions(scope=scope)[0])
    assert result['updated_at'] == '' and result['evidence_count'] == 1
