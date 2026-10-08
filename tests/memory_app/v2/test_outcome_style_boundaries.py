"""写法的原冻结输入、空块和实际发送前资格竞争。"""
import json

import pytest

from backend.memory_app.recall_preferences import set_preference
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2 import _LazyOrganization
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.task_do import TaskDo
from backend.recognition import WorkScope
from tests.memory_app.v2.test_outcome_continuation import BIRTH, CURRENT
from tests.memory_app.v2.test_outcome_redos import scenario, completed, wait_product
from tests.memory_app.v2.test_outcome_style_main import _styles, _start
from tests.memory_app.v2.test_workbench_do import env as do_env


def test_real_initial_freezes_the_same_style_and_full_input_bytes_twice(scenario):
    from backend.memory_app.v2.usage import UsageService
    values = scenario
    selected = _styles(values)
    organization = _LazyOrganization(values.client.app)
    organization.method_query = values.state.workspace_domains.query
    task = TaskDo(values.records, values.models, values.state.task_drafts,
        organization, organization.read, organization.topology)
    before = len(values.models.calls)
    with override(style='@1'):
        _, first = task.initial('same-style-first', 'project-a', '补充实施记录', '访谈')
        UsageService(values.records).record_usage('insight', selected[-1].id, 'me', 20)
        _, second = task.initial('same-style-second', 'project-a', '补充实施记录', '访谈')
    assert first['request']['input']['text'].encode() == second['request']['input']['text'].encode()
    assert first['request']['input']['refs'] == second['request']['input']['refs']
    assert first['request']['privacy'] == second['request']['privacy']
    one = values.records.read('v2_task_styles', first['request']['turn_id'])
    two = values.records.read('v2_task_styles', second['request']['turn_id'])
    assert one.payload['style'] == two.payload['style']
    assert len(values.models.calls) == before


def test_real_initial_freezes_previous_and_confirmed_style_bytes_twice(scenario):
    from backend.memory_app.v2.outcomes import select_outcome
    values = scenario
    _, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    values.documents.save_user_edit(previous, markdown=CURRENT, expected_revision=1)
    original = values.documents.read(previous)
    selected_styles = _styles(values)
    selection = select_outcome(values.records, project='project-a', scene='访谈', document_id=previous)
    organization = _LazyOrganization(values.client.app)
    organization.method_query = values.state.workspace_domains.query
    task = TaskDo(values.records, values.models, values.state.task_drafts,
        organization, organization.read, organization.topology)
    before = len(values.models.calls)
    # 两个独立请求走真实冻结路径，同时保留非空上一版和已确认写法。
    with override(style='@1'):
        _, first = task.initial('same-previous-style-first', 'project-a', '补充实施记录', '访谈',
            continuation=selection, continuation_version='@2')
        _, second = task.initial('same-previous-style-second', 'project-a', '补充实施记录', '访谈',
            continuation=selection, continuation_version='@2')
    requests = [first['request'], second['request']]
    assert requests[0]['turn_id'] != requests[1]['turn_id']
    frozen = [json.loads(request['input']['text']) for request in requests]
    for value in frozen:
        assert value['outcome_input']['document_id'] == previous
        assert value['outcome_input']['document_revision'] == 2
        assert value['outcome_input']['current_markdown'] == CURRENT
        assert value['outcome_input']['birth_ai_markdown'] == BIRTH
        assert value['outcome_selection'] == selection
        style = value['style_input']
        assert style['text'] and style['count'] == 4 and 0 < style['tokens'] <= 400
        assert {row['id'] for row in style['selected']} == {row.id for row in selected_styles}
    assert requests[0]['input']['text'].encode('utf-8') == requests[1]['input']['text'].encode('utf-8')
    assert requests[0]['input']['refs'] == requests[1]['input']['refs']
    assert requests[0]['privacy'] == requests[1]['privacy']
    bindings = [values.records.read('v2_task_styles', request['turn_id']) for request in requests]
    assert all(binding is not None and binding.revision == 1 for binding in bindings)
    assert bindings[0].payload['style'] == bindings[1].payload['style']
    assert values.documents.read(previous) == original
    assert values.documents.markdown(previous, revision=1) == BIRTH
    assert values.documents.markdown(previous, revision=2) == CURRENT
    assert len(values.models.calls) == before


@pytest.mark.parametrize('active', [False, True])
def test_actual_empty_style_has_no_header_or_context_row_and_preserves_legacy_calls(scenario, active, monkeypatch):
    values = scenario
    set_private_project(values.records, 'me', True, 0)
    if not active:
        from backend.memory_app.v2 import policies
        monkeypatch.delitem(policies.ACTIVE, 'continuation')
        monkeypatch.delitem(policies.ACTIVE, 'style')
    with override(**({'style': '@1'} if active else {})):
        original, done = completed(values, summary=BIRTH)
    execution = values.records.read('v2_task_executions', original['turn']['id'])
    frozen = json.loads(execution.payload['request']['input']['text'])
    if active:
        assert frozen['style_input'] == {'version': '@1', 'text': '', 'count': 0, 'tokens': 0, 'selected': []}
    else:
        assert 'style_input' not in frozen
        assert values.records.read('v2_task_styles', execution.payload['request']['turn_id']) is None
    assert len(values.models.calls) == 3
    assert not any(message['content'].startswith('已确认的写法')
                   for wire in values.models.calls for message in wire)
    assert not {'style', 'previous'} & {part['key'] for part in done['receipt']['do']['context']['parts']}


@pytest.mark.parametrize('changed', ['source', 'forgotten', 'confirmation', 'scene', 'private', 'style_binding', 'previous'])
def test_actual_style_and_previous_cas_guards_share_one_wire_transaction(scenario, changed):
    values = scenario
    previous = thread = None
    if changed == 'previous':
        original, done = completed(values, summary=BIRTH)
        previous, thread = done['receipt']['do']['document_id'], original['thread_id']
        values.documents.save_user_edit(previous, markdown=CURRENT, expected_revision=1)
    selected = _styles(values)
    selected_row = selected[0]
    original_complete = values.models.complete_governed
    observed = []
    before = len(values.models.calls)

    def observe_then_complete(messages, **options):
        if not observed:
            observed.append(changed)
            scope = WorkScope('local-user', 'project-a')
            if changed == 'source':
                source = selected_row.source_experience_ids[0]
                SourceEgressService(values.records).set_policy(scope, 'experience', source,
                    values.records.read('recognition_experiences', source).revision, 0, [])
            elif changed == 'forgotten':
                set_preference(values.records, scope, selected_row.id,
                    recognition_revision=selected_row.revision, preference_revision=0, state='forgotten')
            elif changed == 'confirmation':
                values.state.recognition_service.revoke(scope=scope, recognition_id=selected_row.id,
                    expected_revision=selected_row.revision, reason='合成用户撤回确认')
            elif changed == 'scene':
                assign_scene(values.records, 'recognition', selected_row.id, 'project-a', '培训')
            elif changed == 'private':
                set_private_project(values.records, 'me', True, 0)
            elif changed == 'style_binding':
                row, = values.records.list('v2_task_styles')
                with values.records.begin() as tx:
                    tx.put('v2_task_styles', row.object_id, row.payload, expected_revision=row.revision)
                    tx.commit()
            else:
                values.documents.save_user_edit(previous, markdown=CURRENT + '\n\n发送前实际修改。', expected_revision=2)
        return original_complete(messages, **options)

    # 只观察并委托真实网关；竞争变化都经原临时事实所有者写入。
    values.models.complete_governed = observe_then_complete
    started = _start(values, previous=previous, thread=thread)
    result = wait_product(values, started)['receipt']['do']
    assert observed == [changed]
    assert len(values.models.calls) == before
    assert result['state'] == 'failed' and result['document_id'] is None
    execution = values.records.read('v2_task_executions', started['turn']['id'])
    kernel = execution.payload['request']['turn_id']
    assert values.records.read('v2_task_draft_operations', 'deliver-' + kernel) is None
    assert not any(row.payload['turn_id'] == started['turn']['id'] for row in values.records.list('v2_outcome_lineage'))
    assert values.state.ai_turn_store.get_immutable_payload(kernel, 'product-outcome-composition-v1') is None


def test_actual_completed_expert_cannot_deliver_after_style_is_forgotten_before_main_wire(scenario):
    from tests.memory_app.v2.test_outcome_continuation_boundaries import _is_main
    values = scenario
    selected = _styles(values)[0]
    original_complete = values.models.complete_governed
    before_main = []

    def observe_then_complete(messages, **options):
        if _is_main(messages) and not before_main:
            before_main.append(len(values.models.calls))
            set_preference(values.records, WorkScope('local-user', 'project-a'), selected.id,
                recognition_revision=selected.revision, preference_revision=0, state='forgotten')
        return original_complete(messages, **options)

    values.models.complete_governed = observe_then_complete
    started = _start(values)
    result = wait_product(values, started)['receipt']['do']
    assert before_main == [2] and len(values.models.calls) == 2
    assert any(row['state'] == 'done' for row in result['experts'])
    execution = values.records.read('v2_task_executions', started['turn']['id'])
    kernel = execution.payload['request']['turn_id']
    assert values.records.read('v2_task_draft_operations', 'deliver-' + kernel) is None
    assert result['state'] == 'failed' and result['document_id'] is None
    assert not {'style', 'previous'} & {part['key'] for part in result['context']['parts']}
    assert not any(event['type'] == 'model.attempt.dispatched'
                   for event in values.state.ai_turn_store.events_after(kernel))
