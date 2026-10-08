"""Real local admission and user corrections, without a model placement call."""
import pytest

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.projects import assign_scene, scene_of
from tests.memory_app.v2.test_auto_confirm import runtime, item, confirm


def projects(runtime, *, suggested='alpha'):
    service = RecognitionService(runtime.records)
    with runtime.records.begin() as tx:
        for identity, scene in [('alpha', '阅读'), ('beta', '简历'), ('me', '画像')]:
            tx.put('v2_projects', identity, {'name': '整理稿' if identity == suggested else identity,
                'scenes': [scene], 'private': False, 'builtin': 'me' if identity == 'me' else None},
                expected_revision=0)
        tx.commit()
    own = WorkScope('local-user', suggested)
    experience = service.stage_experience(scope=own, content='摘要 阅读方法',
        provenance={'kind': 'user_statement', 'actor': 'local-user'})
    candidate = service.propose(scope=own, content='摘要 阅读方法', source_experience_ids=[experience])
    recognition = service.publish(scope=own, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer='local-user')
    assign_scene(runtime.records, 'recognition', recognition.id, suggested,
        '阅读' if suggested == 'alpha' else '简历')
    return service


def admitted(runtime, *, suggested='alpha'):
    service = projects(runtime, suggested=suggested)
    row = confirm(runtime, item(runtime)['id'])
    return service, row['document_id']


def placement(runtime, service):
    from backend.memory_app.v2.placement import PlacementSuggestions
    from backend.memory_app.v2.overviews import ScopeOverviews
    return PlacementSuggestions(runtime.records, runtime.documents, service,
        ScopeOverviews(runtime.records, runtime.documents, runtime.model))


def test_historical_place_one_has_no_new_hint_or_scene(runtime):
    service, document = admitted(runtime)
    before = runtime.records.list_all()
    assert placement(runtime, service).admit(document, 'alpha', policy_version='@1') is None
    assert runtime.records.list_all() == before
    assert runtime.model.calls == 1


def test_local_hint_assigns_only_missing_scene_and_reentry_is_idempotent(runtime):
    service, document = admitted(runtime)
    original_doc = runtime.records.read('documents', document)
    original_item = runtime.records.list('workspace_items')
    suggestions = placement(runtime, service)
    with override(place='@2'):
        hint = suggestions.admit(document, 'alpha')
        assert hint['project_id'] == 'alpha' and hint['scene'] == '阅读' and hint['score'] > 0
        assert scene_of(runtime.records, 'document', document) == {'project_id': 'alpha', 'scene': '阅读'}
        before = runtime.records.list_all()
        assert suggestions.admit(document, 'alpha') == hint
        assert runtime.records.list_all() == before
    assert runtime.records.read('documents', document) == original_doc
    assert runtime.records.list('workspace_items') == original_item
    assert runtime.model.calls == 1


def test_other_project_hint_does_not_move_or_assign_scene(runtime):
    service, document = admitted(runtime, suggested='beta')
    with override(place='@2'):
        hint = placement(runtime, service).admit(document, 'alpha')
    assert hint['project_id'] == 'beta' and hint['scene'] == '简历'
    assert scene_of(runtime.records, 'document', document) is None
    assert len(runtime.documents.list()) == 1
    assert runtime.records.read('v2_document_recall', document) is None
    assert runtime.records.list('v2_document_filings') == ()
    assert runtime.model.calls == 1


def test_tagged_material_has_no_placement_side_effect(runtime):
    service, document = admitted(runtime)
    before = runtime.records.list_all()
    with override(place='@2'):
        assert placement(runtime, service).admit(document, 'alpha', tagged=True) is None
    assert runtime.records.list_all() == before


def test_user_scene_kept_and_correction_learns_same_project_example_once(runtime):
    service, document = admitted(runtime)
    assign_scene(runtime.records, 'document', document, 'alpha', '手选')
    with runtime.records.begin() as tx:
        row = tx.read('v2_projects', 'alpha')
        tx.put('v2_projects', 'alpha', {**row.payload, 'scenes': ['阅读', '手选']},
            expected_revision=row.revision)
        tx.commit()
    suggestions = placement(runtime, service)
    with override(place='@2'):
        suggestions.admit(document, 'alpha')
        assert scene_of(runtime.records, 'document', document)['scene'] == '手选'
        assignment = runtime.records.read('v2_scene_assignments_document', document)
        result = suggestions.correct_scene(document, 'alpha', '手选',
            expected_revision=1, assignment_revision=assignment.revision)
        assert result['scene'] == '手选'
        assert len(runtime.records.list('v2_place_examples')) == 1
        assert len(runtime.records.list('v2_place_corrections')) == 1
        before = runtime.records.list_all()
        assert suggestions.correct_scene(document, 'alpha', '手选',
            expected_revision=1, assignment_revision=assignment.revision) == result
        assert runtime.records.list_all() == before
        assert suggestions.suggest('整理稿', '摘要', 'alpha').scene == '手选'
        assert suggestions.suggest('整理稿', '摘要', 'beta').scene == '阅读'


def test_stale_scene_correction_has_no_writes(runtime):
    service, document = admitted(runtime)
    before = runtime.records.list_all()
    with pytest.raises(RecognitionConflict):
        placement(runtime, service).correct_scene(document, 'alpha', '阅读',
            expected_revision=2, assignment_revision=0)
    assert runtime.records.list_all() == before
