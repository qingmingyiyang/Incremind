import json
import sqlite3
import asyncio
import pytest

from tests.memory_app.v2.test_outcome_redos import scenario, completed, wait_product
from tests.memory_app.v2.test_workbench_do import env as do_env


BIRTH = '# 项目方案\n\n## 实施记录\n\n初稿段落。\n\n## 保留小节\n\n原有段落。'
CURRENT = BIRTH.replace('初稿段落。', '用户亲改的完整段落。')
UPDATED = '用户亲改的完整段落。\n\n新增资料支持的实施细节。\n\n'


def test_actual_explicit_continuation_freezes_current_body_and_applies_main_patch(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    old_document = values.documents.read(previous)
    saved = values.documents.save_user_edit(previous, markdown=CURRENT,
        expected_revision=old_document['revision'])
    before = values.records.read('documents', previous)
    history = values.records.read('document_markdown', previous + '~r2')
    main_wires = []
    respond = values.models.handler

    def external_response(messages, **options):
        context = json.loads(messages[-1]['content'])
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            # 仅提供外部模型补丁；选择、保护校验、应用和交付由真实原对象完成。
            main_wires.append(messages)
            return json.dumps({'type': 'complete', 'summary': '', 'patches': [
                {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': UPDATED}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'thread_id': original['thread_id'], 'intent': 'do',
        'text': '补充实施记录', 'continue_from': previous})
    assert response.status_code == 200, response.text
    result = wait_product(values, response.json())
    receipt = result['receipt']['do']
    assert receipt['state'] == 'done', receipt
    document = receipt['document_id']
    assert document != previous
    expected = CURRENT.replace('用户亲改的完整段落。\n\n', UPDATED)
    assert values.documents.markdown(document) == expected
    assert values.records.read('documents', previous) == before
    assert values.records.read('document_markdown', previous + '~r2') == history
    assert values.documents.markdown(previous, revision=1) == BIRTH
    assert values.documents.read(previous)['revision'] == saved['revision'] == 2
    assert receipt['continues'] == {'document_id': previous, 'version': 2}
    assert receipt['changes'] == [{'path': ['项目方案', '实施记录'], 'kind': 'updated'}]
    assert receipt['fallback_new'] is False
    lineage = values.records.read('v2_outcome_lineage', document)
    assert lineage.payload['root_id'] == previous
    assert lineage.payload['previous_id'] == previous and lineage.payload['version'] == 2
    execution = values.records.read('v2_task_executions', response.json()['turn']['id'])
    frozen = json.loads(execution.payload['request']['input']['text'])
    assert frozen['outcome_input']['current_markdown'] == CURRENT
    assert frozen['outcome_input']['birth_ai_markdown'] == BIRTH
    assert frozen['outcome_input']['document_revision'] == 2
    assert frozen['outcome_selection']['owner']['document_revision'] == 1
    assert frozen['outcome_input']['continuation_policy'] == '@2'
    assert any(json.loads(message['content']).get('previous_markdown') == CURRENT
               for wire in main_wires for message in wire if message['role'] == 'system'
               and message['content'].startswith('{'))
    assert len(main_wires) == 1


@pytest.mark.parametrize('malformed', [False, True])
def test_actual_invalid_patch_retries_once_then_delivers_independent_new_root(scenario, malformed):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    values.documents.save_user_edit(previous, markdown=CURRENT, expected_revision=1)
    before = values.records.read('documents', previous)
    respond, calls = values.models.handler, []

    def external_response(messages, **options):
        context = json.loads(messages[-1]['content'])
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            calls.append(messages)
            if len(calls) < 3:
                if malformed:
                    return json.dumps(['非法补丁对象'], ensure_ascii=False)
                return json.dumps({'type': 'complete', 'summary': '', 'patches': [
                    {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '覆盖用户段落。'}]}, ensure_ascii=False)
            return json.dumps({'type': 'complete', 'summary': '# 独立新稿\n\n真实回退正文。'}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'thread_id': original['thread_id'], 'intent': 'do',
        'text': '补充实施记录', 'continue_from': previous})
    assert response.status_code == 200, response.text
    receipt = wait_product(values, response.json())['receipt']['do']
    assert receipt['state'] == 'done', receipt
    assert len(calls) == 3
    assert receipt['fallback_new'] is True and receipt['continues'] is None
    assert receipt['changes'] == []
    document = receipt['document_id']
    assert values.documents.markdown(document) == '# 独立新稿\n\n真实回退正文。'
    assert values.records.read('documents', previous) == before
    assert values.documents.markdown(previous) == CURRENT
    lineage = values.records.read('v2_outcome_lineage', document)
    assert lineage.payload['root_id'] == document
    assert lineage.payload['previous_id'] is None and lineage.payload['version'] == 1
    assert values.client.get('/api/v2/library/outcomes/' + document + '/versions',
        params={'project_id': 'project-a'}).json()['items'][0]['version'] == 1


def test_actual_versions_returns_applied_patch_paths(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    respond = values.models.handler

    def external_response(messages, **options):
        context = json.loads(messages[-1]['content'])
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            return json.dumps({'type': 'complete', 'patches': [
                {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '新增实施证据。\n\n'}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'thread_id': original['thread_id'], 'intent': 'do',
        'text': '补充实施记录', 'continue_from': previous})
    assert response.status_code == 200, response.text
    receipt = wait_product(values, response.json())['receipt']['do']
    assert receipt['state'] == 'done', receipt
    items = values.client.get('/api/v2/library/outcomes/' + previous + '/versions',
        params={'project_id': 'project-a'}).json()['items']
    assert items[0]['changes'] == []
    assert items[1]['changes'] == receipt['changes'] == [{'path': ['项目方案', '实施记录'], 'kind': 'updated'}]


def test_actual_unknown_attempt_does_not_become_complete_logical_usage(scenario):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    respond, main_calls = values.models.handler, []

    def external_response(messages, **options):
        context = json.loads(messages[-1]['content'])
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            main_calls.append(messages)
            return json.dumps({'type': 'complete', 'patches': [
                {'kind': 'update', 'path': ['项目方案', '实施记录'],
                 'body': '新增正文。\n\n' if len(main_calls) == 2 else '# 非法标题'}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    transport = values.models._completion_fn

    def external_usage(**request):
        before = len(main_calls)
        result = transport(**request)
        if before == 0 and len(main_calls) == 1:
            if request.get('stream') is True:
                def without_usage():
                    # 只省略首个 Main 尝试的用量，保留原正文、终止帧和关闭边界。
                    try:
                        for chunk in result:
                            yield {**chunk, 'usage': {}} if 'usage' in chunk else chunk
                    finally:
                        close = getattr(result, 'close', None)
                        if callable(close):
                            close()
                return without_usage()
            result['usage'] = {}
        return result

    values.models._completion_fn = external_usage
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'thread_id': original['thread_id'], 'intent': 'do',
        'text': '补充实施记录', 'continue_from': previous})
    assert response.status_code == 200, response.text
    receipt = wait_product(values, response.json())['receipt']['do']
    assert receipt['state'] == 'done' and len(main_calls) == 2, receipt
    kernel = receipt['kernel_turn_id']
    with sqlite3.connect(values.state.ai_turn_store._path) as connection:
        payloads = [(kind, json.loads(raw)) for kind, raw in connection.execute(
            "SELECT kind,payload_json FROM ai_turn_payloads WHERE turn_id=? AND kind IN ('model-call-receipt','model-wire-attempt-receipt')", (kernel,))]
    wires = [data for kind, data in payloads if kind == 'model-wire-attempt-receipt']
    assert len(wires) == 2 and len({wire['attempt_id'] for wire in wires}) == 2
    assert {wire['attempt_number'] for wire in wires} == {1, 2}
    assert sum(wire['usage'] is None for wire in wires) == 1
    identity = wires[0]['model_request_id']
    logical, = [data for kind, data in payloads if kind == 'model-call-receipt' and data['model_request_id'] == identity]
    assert logical['usage'] is None and logical['usage_status'] == 'not_recorded'
    groups = kernel_call_groups(values.models.root, turn_id=kernel, project='project-a',
        remote_only=False, records=values.records)
    call, = [call for group in groups for call in group['calls'] if call['model_request_id'] == identity]
    assert call['usage'] == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
    assert call['usage_status'] == 'partial' and call['cost'] is None


def test_actual_outcome_picker_has_only_qualified_latest_outcomes(scenario):
    from core.document_engine.ports import DocumentDraft
    values = scenario
    _, delivered = completed(values, summary=BIRTH)
    identity = delivered['receipt']['do']['document_id']
    item = asyncio.run(values.state.workspace_domains.intake.add_text({
        'project_id': 'project-a', 'text': '普通独立原文'}))
    ordinary = values.documents.create(DocumentDraft(title='普通整理稿', document_type='note', markdown='普通正文',
        project_id='project-a', source_refs=({'source_id': item['id'], 'locator': 'workspace://' + item['id']},)))
    response = values.client.get('/api/v2/library/outcomes', params={'project_id': 'project-a'})
    assert response.status_code == 200, response.text
    assert response.json() == {'items': [{'document_id': identity, 'title': '准备一份成果', 'version': 1}]}
    assert ordinary['id'] not in {row['document_id'] for row in response.json()['items']}
    assert values.client.get('/api/v2/library/outcomes', params={'project_id': 'project-other'}).json() == {'items': []}


@pytest.mark.parametrize('automatic', [True, False])
def test_actual_auto_choice_is_frozen_and_explicit_null_creates_new(scenario, automatic):
    from backend.memory_app.v2.policies import override
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    respond = values.models.handler

    def external_response(messages, **options):
        context = json.loads(messages[-1]['content'])
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            if automatic:
                return json.dumps({'type': 'complete', 'patches': [
                    {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '自动续写正文。\n\n'}]}, ensure_ascii=False)
            return json.dumps({'type': 'complete', 'summary': '# 明确新写\n\n独立正文。'}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    body = {'project_id': 'project-a', 'thread_id': original['thread_id'], 'intent': 'do', 'text': '准备一份成果'}
    if not automatic:
        body['continue_from'] = None
    with override(continuation='@1'):
        response = values.client.post('/api/v2/workbench/turns', json=body)
    assert response.status_code == 200, response.text
    receipt = wait_product(values, response.json())['receipt']['do']
    assert receipt['state'] == 'done', receipt
    execution = values.records.read('v2_task_executions', response.json()['turn']['id'])
    frozen = json.loads(execution.payload['request']['input']['text'])
    assert frozen['outcome_input']['continuation_policy'] == '@1'
    lineage = values.records.read('v2_outcome_lineage', receipt['document_id'])
    if automatic:
        assert frozen['outcome_selection']['document_id'] == previous
        assert lineage.payload['previous_id'] == previous and lineage.payload['version'] == 2
        assert receipt['continues'] == {'document_id': previous, 'version': 2}
    else:
        assert frozen['outcome_input'] == {'continuation_policy': '@1', 'document_id': None}
        assert lineage.payload['root_id'] == receipt['document_id'] and lineage.payload['version'] == 1
        assert receipt.get('continues') is None


@pytest.mark.parametrize('target', [True, False])
def test_actual_redo_optional_choice_uses_original_division_and_redo_owner(scenario, target):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    sample = values.client.get('/api/v2/workbench/turns/' + original['turn']['id'] + '/division',
        params={'project_id': 'project-a'}).json()
    respond = values.models.handler

    def external_response(messages, **options):
        context = json.loads(messages[-1]['content'])
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            if target:
                return json.dumps({'type': 'complete', 'patches': [
                    {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '重做选择正文。\n\n'}]}, ensure_ascii=False)
            return json.dumps({'type': 'complete', 'summary': '# 重做新写\n\n独立正文。'}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    response = values.client.post('/api/v2/workbench/turns/' + original['turn']['id'] + '/redo', json={
        'project_id': 'project-a', 'expected_revision': sample['revision'],
        'continue_from': previous if target else None})
    assert response.status_code == 200, response.text
    receipt = wait_product(values, response.json())['receipt']['do']
    assert receipt['state'] == 'done', receipt
    lineage = values.records.read('v2_outcome_lineage', receipt['document_id'])
    assert lineage.payload['previous_id'] == (previous if target else None)
    assert lineage.payload['root_id'] == (previous if target else receipt['document_id'])
    event, = [row for row in values.records.list('v2_outcome_corrections') if row.payload['kind'] == 'outcome_redo']
    assert event.payload['turn_id'] == original['turn']['id']
    assert event.payload['new_turn_id'] == response.json()['turn']['id']
    assert event.payload['new_document_id'] == receipt['document_id'] and event.payload['after']
