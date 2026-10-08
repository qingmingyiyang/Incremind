"""Serializable multipart bindings reuse the original navigation guards."""
import json
import pytest
from backend.recognition import RecognitionError
from backend.memory_app.v2.part_context import _json_value
from tests.memory_app.v2.test_workbench_ask import env, add_document, publish


@pytest.mark.parametrize('change', ['move', 'add', 'remove'])
def test_navigation_binding_survives_serialization_and_rejects_scope_changes(env, change):
    from tests.memory_app.v2.test_overviews import OverviewModel
    from backend.memory_app.v2.overviews import ScopeOverviews, navigation_candidates, validate_navigation_binding
    from backend.memory_app.v2.projects import assign_scene
    doc, _ = add_document(env, summary='桥梁预算')
    ScopeOverviews(env.records, env.documents, OverviewModel()).update('alpha')
    _, guard = navigation_candidates(env.domains.query, 'alpha', '最近在忙什么？')
    assert guard is not None
    frozen = json.loads(json.dumps(_json_value(guard.frozen_binding)))
    validate_navigation_binding(env.domains.query, frozen)
    if change == 'move':
        assign_scene(env.records, 'document', doc, 'alpha', '另一场景')
    elif change == 'add':
        add_document(env, summary='设备验收')
    else:
        with env.records.begin() as tx:
            row = tx.read('documents', doc)
            tx.put('documents', doc, {**row.payload, 'status':'archived'}, expected_revision=row.revision)
            tx.commit()
    with pytest.raises(RecognitionError):
        validate_navigation_binding(env.domains.query, frozen)


@pytest.mark.parametrize('change', ['preference', 'private'])
def test_bookshelf_binding_preserves_records_and_original_snapshots(env, change):
    from tests.memory_app.v2.test_bookshelf import forgotten
    from backend.memory_app.v2.bookshelf import consult_bookshelf, validate_bookshelf_binding
    from backend.memory_app.v2.privacy import set_private_project
    insight, _ = publish(env)
    forgotten(env, insight)
    plan = env.domains.query.prepare_ask('alpha', 'alpha beta gamma?')
    consult_bookshelf(env.domains.query, 'alpha', 'alpha beta gamma?', plan)
    assert plan['bookshelf']['hits'] == 1
    frozen = json.loads(json.dumps(_json_value(plan['bookshelf_guard'].frozen_binding)))
    validate_bookshelf_binding(env.domains.query, frozen)
    if change == 'private':
        set_private_project(env.records, 'alpha', True, 0)
    else:
        with env.records.begin() as tx:
            row = tx.read('recognition_recall_preferences', insight.id)
            tx.put(row.collection, row.object_id, {**row.payload, 'by':'user'}, expected_revision=row.revision)
            tx.commit()
    with pytest.raises(RecognitionError):
        validate_bookshelf_binding(env.domains.query, frozen)


def test_bookshelf_binding_accepts_only_its_own_recorded_revival(env):
    from tests.memory_app.v2.test_bookshelf import forgotten
    from backend.memory_app.v2.bookshelf import consult_bookshelf, validate_bookshelf_binding, relearn_cited
    from backend.memory_app.v2.usage import record_answer_usage
    insight, _ = publish(env)
    forgotten(env, insight)
    query = env.domains.query
    plan = query.prepare_ask('alpha', 'alpha beta gamma?')
    consult_bookshelf(query, 'alpha', 'alpha beta gamma?', plan)
    assert plan['bookshelf']['hits'] == 1
    frozen = json.loads(json.dumps(_json_value(plan['bookshelf_guard'].frozen_binding)))
    citations = [{'n':1, 'id':insight.id}]
    record_answer_usage(env.records, plan['chosen'], citations, 'alpha')
    relearn_cited(env.records, plan['chosen'], citations, turn_id='answer-revival')
    validate_bookshelf_binding(query, frozen, turn_id='answer-revival')
    with pytest.raises(RecognitionError):
        validate_bookshelf_binding(query, frozen, turn_id='another-answer')
    with env.records.begin() as tx:
        row = tx.read('recognition_recall_preferences', insight.id)
        tx.put(row.collection, row.object_id, {**row.payload, 'by':'user'}, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionError):
        validate_bookshelf_binding(query, frozen, turn_id='answer-revival')
