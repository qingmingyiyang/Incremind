"""写法沿真实续写成果的出生经历闭包确认，不能替换成普通陈述来源。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.original_sources import source_store
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.style_context import confirmed_style
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.recognition.product_draft_dependencies import product_draft_source
from tests.memory_app.v2.test_outcome_continuation import BIRTH, CURRENT
from tests.memory_app.v2.test_outcome_redos import scenario, completed, wait_product
from tests.memory_app.v2.test_outcome_style_main import _capture
from tests.memory_app.v2.test_workbench_do import env as do_env


def style_candidate_from_continued_outcome(values, material_kind='experience', *, private=False):
    """复用真实出生与原件闭包，给确认和归类入口提供同一合法候选。"""
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    refs = None
    if material_kind == 'original_item':
        item = asyncio.run(values.state.workspace_domains.intake.add_text({
            'project_id':'project-a', 'text':'合成写法原件'}))
        refs = ({'source_id':item['id'], 'locator':'workspace://'+item['id']},)
    elif material_kind == 'original_source':
        source_store(values.records).write('sources', 'style-original', {
            'id':'style-original', 'project_id':'project-a', 'title':'合成写法原件',
            'type':'text', 'metadata':{'content_snapshot':'合成写法来源'}}, expected_revision=0)
        refs = ({'source_id':'style-original', 'locator':'source://style-original'},)
    values.documents.save_user_edit(previous, markdown=CURRENT, expected_revision=1,
        **({'source_refs':refs} if refs is not None else {}))
    ordinary = values.models.handler
    mains = _capture(values, patch=True)
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'thread_id':original['thread_id'], 'intent':'do',
        'text':'补充实施记录', 'continue_from':previous})
    assert response.status_code == 200, response.text
    receipt = wait_product(values, response.json())['receipt']['do']
    assert receipt['state'] == 'done' and receipt['continues']['document_id'] == previous
    assert len(mains) == 1
    document = receipt['document_id']
    scope = WorkScope('local-user', 'project-a')
    bound = product_draft_source(values.records, scope, document, 1)
    assert any(kind == 'experience' for _, kind, _, _ in bound.roots)
    assert any(kind == material_kind for _, kind, _, _ in bound.roots)
    if private:
        set_private_project(values.records, 'project-a', True, 0)
    service = values.state.recognition_service
    experience, revision = ensure_document_experience(values.documents, service,
        'project-a', document, retained_revision=1)
    assert revision == 1
    source = values.records.read('recognition_experiences', experience)
    assert source.payload['provenance']['kind'] == 'model_generated_artifact'
    assert {'type':'document', 'id':document, 'revision':1} in source.payload['provenance']['source_refs']
    assert {'type':'turn', 'id':receipt['kernel_turn_id']} in source.payload['provenance']['source_refs']
    candidate = service.propose(scope=scope, content='开头先列出结论。',
        source_experience_ids=[experience])
    return SimpleNamespace(service=service, scope=scope, candidate=candidate,
        previous=previous, ordinary=ordinary, experience=experience)


@pytest.mark.parametrize('material_kind', ['experience', 'original_item', 'original_source'])
@pytest.mark.parametrize('private', [False, True])
def test_writing_from_a_real_continued_outcome_retains_typed_birth_sources(scenario, material_kind, private):
    values = scenario
    source = style_candidate_from_continued_outcome(values, material_kind, private=private)
    service, scope, candidate = source.service, source.scope, source.candidate
    writing = service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer='local-user')
    assert writing.id in {entry['id'] for entry in service.retrieval_entries(scope=scope)}
    block = confirmed_style(values.records, service, 'project-a', version='@1', scope_version='@2')
    if private:
        assert block['selected'] == [] and block['count'] == 0 and block['text'] == ''
    else:
        assert block['selected'] == [{'id':writing.id, 'revision':writing.revision}]
        assert block['count'] == 1 and '开头先列出结论。' in block['text']
        values.models.handler = source.ordinary
        values.summary = BIRTH
        next_mains = _capture(values)
        with override(style='@1'):
            response = values.client.post('/api/v2/workbench/turns', json={
                'project_id':'project-a', 'intent':'do', 'text':'准备另一篇成果', 'continue_from':None})
        assert response.status_code == 200, response.text
        started = response.json()
        result = wait_product(values, started)['receipt']['do']
        assert result['state'] == 'done', result
        execution = values.records.read('v2_task_executions', started['turn']['id'])
        frozen = json.loads(execution.payload['request']['input']['text'])['style_input']
        assert frozen['text'] == block['text'] and frozen['selected'] == block['selected']
        assert len(next_mains) == 1 and any(message['role'] == 'system'
            and message['content'] == block['text'] for message in next_mains[0])
    assert values.documents.markdown(source.previous) == CURRENT


@pytest.mark.parametrize('change', ['revision', 'missing_capability'])
def test_typed_original_source_cannot_publish_after_authority_loss(scenario, change):
    values = scenario
    source = style_candidate_from_continued_outcome(values, 'original_source')
    candidate_before = values.records.read('recognition_candidates', source.candidate.id)
    recognized_before = values.records.list('recognitions')
    service = source.service
    if change == 'revision':
        store = source_store(values.records)
        body = store.read('sources', 'style-original')
        store.write('sources', 'style-original', {**body, 'title':'合成原件已修订'},
            expected_revision=store.revision('sources', 'style-original'))
    else:
        service = RecognitionService(values.records)
    with pytest.raises(RecognitionConflict):
        service.publish(scope=source.scope, candidate_id=source.candidate.id,
            expected_revision=source.candidate.revision, reviewer='local-user')
    assert values.records.read('recognition_candidates', source.candidate.id) == candidate_before
    assert values.records.list('recognitions') == recognized_before
