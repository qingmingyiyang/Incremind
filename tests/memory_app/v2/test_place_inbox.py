"""Pending quick notes and project suggestions use real inbox writes."""
from backend.memory_app.v2.policies import override
from tests.memory_app.v2.test_workbench_placement import workbench
from tests.memory_app.v2.test_auto_confirm import runtime


def note(http, text):
    response = http.post('/api/v2/workbench/turns', json={
        'project_id': 'alpha', 'intent': 'inspiration', 'text': text})
    assert response.status_code == 200, response.text
    return response.json()['turn']['receipt']['inspiration']


def test_quick_note_gets_typed_hint_without_document_model_or_publication(workbench):
    runtime, app, http = workbench
    with override(place='@2'):
        receipt = note(http, '整理稿摘要 阅读方法')
    candidate = receipt['insight']
    assert candidate['kind'] == 'candidate' and candidate['state'] == 'pending'
    assert receipt['placement']['project_id'] == 'alpha' and receipt['placement']['scene'] == '阅读'
    hint = runtime.records.read('v2_place_hints_candidate', candidate['id'])
    assert hint.payload['source_project_id'] == 'inbox' and hint.payload['candidate_revision'] == candidate['revision']
    assert hint.payload['policy_version'] == '@2'
    assert runtime.documents.list() == () and runtime.model.calls == 0


def test_same_topic_threshold_is_three_and_existing_filing_owners_archive_group(workbench):
    runtime, app, http = workbench
    with override(place='@2'):
        notes = [note(http, '咖啡店铺 ' + name)['insight'] for name in ['北街', '山边']]
        response = http.get('/api/v2/library/inbox/project-suggestions')
        assert response.status_code == 200, response.text
        assert response.json()['items'] == []
        notes.append(note(http, '咖啡店铺 江边')['insight'])
        response = http.get('/api/v2/library/inbox/project-suggestions')
        assert response.status_code == 200, response.text
        groups = response.json()['items']
        assert len(groups) == 1 and groups[0]['count'] == 3
        assert set(groups[0]['ids']) == {row['id'] for row in notes}
        assert groups[0]['name'] in {'咖啡', '店铺', '咖啡店铺'}
        created = http.post('/api/v2/projects', json={'name': '周末咖啡'})
        assert created.status_code == 200
        destination = created.json()['id']
        for row in notes:
            response = http.post(f"/api/v2/library/inbox/insight/{row['id']}/file", json={
                'target_project_id': destination, 'expected_revision': row['revision']})
            assert response.status_code == 200, response.text
            assert response.json()['state'] == 'pending'
        assert len(runtime.records.list('v2_place_corrections')) == 3
        assert len(runtime.records.list('v2_place_examples')) == 3
        assert http.get('/api/v2/library/inbox/project-suggestions').json()['items'] == []
    assert runtime.model.calls == 0 and runtime.documents.list() == ()


def test_generic_inspiration_label_does_not_group_unrelated_notes(workbench):
    runtime, app, http = workbench
    with override(place='@2'):
        for text in ['灵感 小说人物', '灵感 渔具爸爸', '灵感 论文阅读']:
            note(http, text)
        response = http.get('/api/v2/library/inbox/project-suggestions')
        assert response.status_code == 200, response.text
        assert response.json()['items'] == []


def test_historical_place_one_does_not_add_quick_note_hints_or_groups(workbench):
    runtime, app, http = workbench
    with override(place='@1'):
        for text in ['咖啡店铺 北街', '咖啡店铺 山边', '咖啡店铺 江边']:
            assert 'placement' not in note(http, text)
        response = http.get('/api/v2/library/inbox/project-suggestions')
        assert response.status_code == 200, response.text
        assert response.json()['items'] == []
    assert runtime.records.list('v2_place_hints_candidate') == ()
