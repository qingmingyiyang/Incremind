"""词表工具使用临时 SQLite，模型运输集成单独验证。"""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from backend.memory_app.v2.agent_data_tools import AgentDataTools, AgentDataToolError, CAPABILITIES, OPERATIONS
from backend.memory_app.v2.asr_lexicon import AsrLexiconService
from backend.memory_app.v2.policies import get
from core.ai_kernel.agent_profiles import AgentProfileRegistry, InMemoryAgentProfileStore
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider import SQLiteStructuredRecordStore


class Turns:
    def __init__(self):
        self.events, self.payloads = [], {}

    def events_after(self, identity):
        return tuple(self.events)

    def get(self, ref):
        return self.payloads[ref]


@pytest.fixture
def tools(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    user_text = '更新热词表：\nIncremind\n程虹'
    request = freeze_turn_request('project.task', turn_id='kernel-one', session_id='session-user-one',
        operation_id='op-one', idempotency_key='one', project_id='project-a', created_at='2026-10-13T00:00:00Z',
        text=json.dumps({'task': user_text}), privacy={'mode': 'local_only', 'allow_remote': False,
            'pii': 'none', 'consent_refs': [], 'retention': 'session'}, template_version=3)
    request['agent_binding'] = {'role': 'main', 'depth': 0}
    request['policy_versions'] = {'agent_data': '@1'}
    with records.begin() as tx:
        tx.put('v2_turns', 'user-one', {'intent': 'do', 'project_id': 'project-a', 'user_text': user_text,
            'created_at': '2026-10-13T00:00:00Z', 'thread_id': 'thread-one',
            'receipt': {'do': {'kernel_turn_id': 'kernel-one'}}}, expected_revision=0)
        tx.put('v2_task_executions', 'user-one', {'request': request}, expected_revision=0)
        tx.commit()
    owner = AgentDataTools(AsrLexiconService(records), turns=Turns(), request_loader=lambda identity: request,
        skill_root=Path(__file__).parents[3] / 'config' / 'application-skills')
    return owner, request


def invoke(owner, capability, args, operation='operation-one'):
    return owner.invoke(capability, {'turn_id': 'kernel-one', 'arguments': args, 'operation_id': operation})['result']


def approve(owner, args):
    owner.turns.events = [{'type': 'approval.required', 'event_id': 'approval-one', 'data': {'payload_ref': 'pending'}},
        {'type': 'approval.resolved', 'event_id': 'resolved-one', 'data': {'payload_ref': 'action'}}]
    owner.turns.payloads = {'pending': {'capability_id': CAPABILITIES[2], 'arguments': args},
        'action': {'type': 'approve', 'actor': 'user', 'target_event_id': 'approval-one'}}


def test_real_sqlite_preview_commit_receipt_and_replay(tools):
    owner, request = tools
    assert invoke(owner, CAPABILITIES[0], {'view': 'skill'})['name'] == 'asr-lexicon'
    assert invoke(owner, CAPABILITIES[0], {'view': 'terms'})['revision'] == 0
    preview = invoke(owner, CAPABILITIES[1], {'text': 'Incremind\n程虹', 'format': 'txt', 'expected_revision': 0})
    assert preview['authorized'] and preview['added_count'] == 2
    assert owner.lexicon.read()['terms'] == []
    args = {'preview_id': preview['preview_id']}
    approve(owner, args)
    result = invoke(owner, CAPABILITIES[2], args)
    assert result['verified'] is True and result['revision'] > 0
    assert {row['term'] for row in owner.lexicon.read()['terms']} == {'Incremind', '程虹'}
    assert invoke(owner, CAPABILITIES[2], args) == result
    assert len(owner.records.list(OPERATIONS)) == 1


@pytest.mark.parametrize('text,format', [('额外词', 'txt'), ('Incremind\n程虹', 'csv')])
def test_model_cannot_authorize_added_words_or_changed_format(tools, text, format):
    owner, request = tools
    if format == 'csv':
        assert not get('agent_data', version='@1').authorized_import(json.loads(request['input']['text'])['task'], text, format)
        return
    preview = invoke(owner, CAPABILITIES[1], {'text': text, 'format': format, 'expected_revision': 0})
    assert preview['authorized'] is False
    args = {'preview_id': preview['preview_id']}
    assert owner.constrain_decision(request, {'type': 'tool', 'capability_id': CAPABILITIES[2], 'arguments': args})['type'] == 'complete'
    approve(owner, args)
    with pytest.raises(AgentDataToolError, match='explicit_instruction_required'):
        invoke(owner, CAPABILITIES[2], args)
    assert not owner.records.list(OPERATIONS)


def test_user_change_and_stale_lexicon_refuse_write(tools):
    owner, request = tools
    preview = invoke(owner, CAPABILITIES[1], {'text': 'Incremind\n程虹', 'format': 'txt', 'expected_revision': 0})
    args = {'preview_id': preview['preview_id']}
    approve(owner, args)
    owner.lexicon.import_text(text='另一个词', expected_revision=0, preview=False)
    with pytest.raises(ValueError, match='revision_conflict'):
        invoke(owner, CAPABILITIES[2], args)
    assert len(owner.lexicon.read()['terms']) == 1
    assert not owner.records.list(OPERATIONS)
    with owner.records.begin() as tx:
        row = tx.read('v2_turns', 'user-one')
        tx.put('v2_turns', 'user-one', {**row.payload, 'user_text': '取消修改'}, expected_revision=row.revision)
        tx.commit()
    assert owner.can_import('kernel-one', args) is False


def test_fabricated_confirmation_and_missing_real_approval_refuse_write(tools):
    owner, _ = tools
    preview = invoke(owner, CAPABILITIES[1], {'text': 'Incremind\n程虹', 'format': 'txt', 'expected_revision': 0})
    with pytest.raises(AgentDataToolError, match='approval_unavailable'):
        invoke(owner, CAPABILITIES[2], {'preview_id': preview['preview_id']})
    assert owner.can_import('kernel-one', {'preview_id': preview['preview_id'], 'user_confirmed': True}) is False
    assert owner.lexicon.read()['terms'] == []


def test_recorded_user_instruction_is_consumed_once(tools):
    owner, request = tools
    first = invoke(owner, CAPABILITIES[1], {'text': 'Incremind\n程虹', 'format': 'txt', 'expected_revision': 0})
    approve(owner, {'preview_id': first['preview_id']})
    original = invoke(owner, CAPABILITIES[2], {'preview_id': first['preview_id']})
    changed = owner.lexicon.import_text(text='程虹\t5', expected_revision=original['revision'], preview=False)
    repeated = invoke(owner, CAPABILITIES[1], {'text': 'Incremind\n程虹', 'format': 'txt',
        'expected_revision': changed['revision']})
    assert repeated['authorized'] is False
    args = {'preview_id': repeated['preview_id']}
    assert not owner.can_import('kernel-one', args)
    approve(owner, args)
    with pytest.raises(AgentDataToolError, match='instruction_consumed'):
        invoke(owner, CAPABILITIES[2], args, operation='operation-two')
    assert invoke(owner, CAPABILITIES[2], {'preview_id': first['preview_id']}) == original
    assert next(row['weight'] for row in owner.lexicon.read()['terms'] if row['term'] == '程虹') == 5


def test_legacy_templates_do_not_gain_lexicon_tools_and_custom_profiles_do_not_upgrade():
    from core.ai_kernel.turn_templates import _template
    assert set(CAPABILITIES).isdisjoint(_template('project.task', 1)[0])
    assert _template('project.task', 2)[0] == ('external.task.execute',)
    assert set(CAPABILITIES) <= set(_template('project.task', 3)[0])
    defaults = AgentProfileRegistry()
    default = defaults.get('main.orchestrator')
    old = replace(default, capability_ids=tuple(row for row in default.capability_ids if row not in CAPABILITIES))
    store = InMemoryAgentProfileStore({'main.orchestrator': old})
    registry = AgentProfileRegistry(store)
    assert registry.upgrade_untouched_lexicon_tools()
    assert set(CAPABILITIES) <= set(registry.get('main.orchestrator').capability_ids)
    custom = replace(old, revision=old.revision + 1, instructions='用户自定义规则')
    registry = AgentProfileRegistry(InMemoryAgentProfileStore({'main.orchestrator': custom}))
    assert not registry.upgrade_untouched_lexicon_tools()
    assert registry.get('main.orchestrator') == custom


def test_published_turn_schema_accepts_new_template_and_rejects_legacy_expansion(tools):
    from copy import deepcopy
    from jsonschema import Draft202012Validator
    owner, request = tools
    value = deepcopy(request)
    value.pop('agent_binding')
    value['turn_id'] = 'turn-' + '1' * 32
    value['session_id'] = 'session-' + '2' * 32
    value['operation_id'] = 'op-' + '3' * 32
    value['idempotency_key'] = 'key-' + '4' * 32
    schema = json.loads((Path(__file__).parents[3] / 'core-contracts' / 'ai' / 'turn-request.schema.json').read_text(encoding='utf-8'))
    validator = Draft202012Validator(schema)
    from core.ai_kernel import validate_turn_request
    validate_turn_request(value)
    validator.validate(value)
    value['execution_policy']['template_version'] = 1
    assert not validator.is_valid(value)


def test_single_input_lexicon_command_routes_without_model_or_rewriting(tmp_path):
    from backend.memory_app.v2.route import RouteService
    service = RouteService(SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3'), object())
    text = '更新热词表：\nIncremind\n程虹'
    plan = service.route(text, project_id='project-a', request_key='route-one')
    assert len(plan.parts) == 1 and plan.parts[0].intent == 'do'
    assert plan.parts[0].span == text and plan.parts[0].instruction is None
    assert plan.reason == 'explicit_lexicon_instruction'
    manual = service.route(text, project_id='project-a', request_key='route-two', intent='remember')
    assert manual.parts[0].intent == 'remember'


def test_model_context_rejects_a_changed_lexicon_read(tools):
    owner, request = tools
    invoke(owner, CAPABILITIES[0], {'view': 'terms'})
    owner.validate_context(request)
    owner.lexicon.import_text(text='新词', expected_revision=0, preview=False)
    with pytest.raises(AgentDataToolError, match='read_revision_changed'):
        owner.validate_context(request)


def test_only_new_main_model_messages_project_machine_events(tools):
    from copy import deepcopy
    owner, request = tools
    events = [
        {'type': 'model.routed', 'sequence': 2, 'data': {'large_machine_trace': '机器' * 100}},
        {'type': 'context.resolved', 'sequence': 3, 'data': {'payload_ref': 'context-one'}},
        {'type': 'tool.completed', 'sequence': 8, 'data': {'capability_id': CAPABILITIES[0],
            'receipt_ref': 'read-receipt'}, 'resolved_payload': {'revision': 0}},
        {'type': 'approval.resolved', 'sequence': 11, 'data': {'payload_ref': 'approval-one'},
            'resolved_payload': {'actor': 'user', 'type': 'approve'}},
        {'type': 'tool.failed', 'sequence': 12, 'data': {'capability_id': CAPABILITIES[1], 'code': 'conflict'}},
        {'type': 'hook.continuation', 'sequence': 13, 'data': {'payload_ref': 'draft-continuation'}},
        {'type': 'approval.required', 'sequence': 14, 'data': {'payload_ref': 'pending-one'}},
        {'type': 'tool.cancelled', 'sequence': 15, 'data': {'capability_id': CAPABILITIES[2]}},
    ]
    original = deepcopy(events)
    context = {'decision_contract': {}, 'input': request['input'], 'context': [{'evidence': 'current'}], 'events': events}
    text = json.dumps(context, ensure_ascii=False)
    projected = json.loads(owner.model_input(request, text))
    assert projected == {**context, 'events': events[1:]}
    assert events == original
    for version, role in [(1, 'main'), (2, 'main'), (3, 'worker')]:
        other = deepcopy(request)
        other['execution_policy']['template_version'] = version
        other['agent_binding']['role'] = role
        assert owner.model_input(other, text) == text
    before = {**context, 'events': events[:2]}
    assert json.loads(owner.model_input(request, json.dumps(before))) == before
