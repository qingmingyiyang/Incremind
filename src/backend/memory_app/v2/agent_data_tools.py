"""将白名单领域操作交给既有工具内核，不提供通用数据库写入。"""
from collections.abc import Mapping
from dataclasses import replace
import json
from uuid import uuid4
from jsonschema import Draft202012Validator

from core.ai_kernel import CapabilityDefinition, validate_turn_action
from core.ai_tooling.contracts import tool_from_legacy_capability
from core.application_skill.package_catalog import (
    ApplicationSkillCatalog, ApplicationSkillPackageLoader, ApplicationSkillSource,
)
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _now
from .policies import get


CAPABILITIES = ('asr.lexicon.read', 'asr.lexicon.preview', 'asr.lexicon.import')
PREVIEWS = 'v2_agent_lexicon_previews'
OPERATIONS = 'v2_agent_lexicon_operations'
AUTHORIZATIONS = 'v2_agent_lexicon_authorizations'
READS = 'v2_agent_lexicon_reads'


class AgentDataToolError(ValueError):
    """只暴露固定错误码，不包含词表正文、数据库路径或凭据。"""


def definitions():
    result = []
    for identity in CAPABILITIES:
        write = identity == CAPABILITIES[2]
        definition = CapabilityDefinition(identity, 1, 'write' if write else 'read', write,
            'receipt_required' if write else 'read_only',
            f'crp://default/contracts/ai/asr-lexicon-{identity.rsplit(".", 1)[-1]}-request.schema.json',
            'crp://default/contracts/ai/asr-lexicon-result.schema.json')
        tool = tool_from_legacy_capability(definition)
        result.append(replace(definition, tool_definition=replace(tool,
            description={'asr.lexicon.read': '读取当前用户热词表或内置操作方法',
                         'asr.lexicon.preview': '预览热词导入并核对用户明确授权',
                         'asr.lexicon.import': '保存已获用户明确授权的精确词表预览'}[identity])))
    return tuple(result)


class _Capability:
    def __init__(self, owner, identity):
        self.owner, self.identity = owner, identity

    def invoke(self, request):
        try:
            return self.owner.invoke(self.identity, request)
        except AgentDataToolError:
            raise
        except Exception as error:
            code = getattr(error, 'code', None)
            if code in {'invalid_asr_lexicon_import', 'asr_lexicon_revision_conflict',
                        'asr_lexicon_capacity_exceeded', 'asr_lexicon_import_too_large'}:
                raise AgentDataToolError(code) from None
            raise AgentDataToolError('agent_data_operation_failed') from None


class AgentDataTools:
    def __init__(self, lexicon, *, turns, request_loader, skill_root):
        self.lexicon, self.records, self.turns = lexicon, lexicon.records, turns
        self.request_loader = request_loader
        catalog = ApplicationSkillCatalog().discover((ApplicationSkillSource('bundled', skill_root, 'bundled'),))
        self.skill = catalog.get('asr-lexicon')
        if self.skill is None:
            raise AgentDataToolError('agent_data_skill_unavailable')
        self.instructions = ApplicationSkillPackageLoader().load_instructions(self.skill).markdown
        self.schemas = {}
        for name in ('read', 'preview', 'import', 'result'):
            filename = f'asr-lexicon-{name}' + ('' if name == 'result' else '-request') + '.schema.json'
            schema = json.loads((skill_root.parents[1] / 'core-contracts' / 'ai' / filename).read_text(encoding='utf-8'))
            Draft202012Validator.check_schema(schema)
            self.schemas[name] = Draft202012Validator(schema)

    def install(self, registry):
        for definition in definitions():
            registry.register(definition, _Capability(self, definition.capability_id))

    def catalog(self, request):
        if request.get('execution_policy', {}).get('template_version') != 3:
            return ''
        return self._policy(request).catalog(self.skill.description)

    def validate_context(self, request):
        """下一次模型外发前复验已读词表，不能继续使用已变化的设置。"""
        row = self.records.read(READS, request['turn_id'])
        if row is not None and self.lexicon.read()['revision'] != row.payload['revision']:
            raise AgentDataToolError('agent_data_read_revision_changed')

    @staticmethod
    def explicit_import(request):
        if request.get('execution_policy', {}).get('template_version') != 3:
            return False
        try:
            text = json.loads(request['input']['text'])['task']
        except (ValueError, KeyError, TypeError):
            return False
        return AgentDataTools._policy(request).parse_instruction(text) is not None

    def model_input(self, request, text):
        # 仅投影外发消息，派生步骤、序号和 Main 决策仍读取完整内核事件。
        if (request.get('execution_policy', {}).get('template_version') != 3
                or request.get('agent_binding', {}).get('role') != 'main'):
            return text
        context = json.loads(text)
        if 'decision_contract' not in context:
            return text
        context['events'] = self._policy(request).model_events(context['events'])
        return json.dumps(context, ensure_ascii=False, separators=(',', ':'))

    def verify_completion(self, request):
        """用户可见完成只认实际写入、真实内核回执和当前回读。"""
        turn_id = request['turn_id']
        _, user = self._binding(turn_id)
        authority = self.records.read(AUTHORIZATIONS, user['id'])
        if authority is None or authority.payload.get('turn_id') != turn_id:
            raise AgentDataToolError('asr_lexicon_update_not_completed')
        proof = authority.payload
        operation = self.records.read(OPERATIONS, proof.get('operation_id', ''))
        preview = self.records.read(PREVIEWS, proof.get('preview_id', ''))
        if (operation is None or preview is None or proof['user']['facts'] != user['facts']
                or proof['user']['id'] != user['id']
                or operation.payload.get('user', {}).get('id') != user['id']
                or operation.payload.get('user', {}).get('facts') != user['facts']
                or operation.payload.get('inputs') != {'turn_id': turn_id, 'preview_id': proof['preview_id']}
                or preview.payload.get('turn_id') != turn_id or preview.payload.get('authorized') is not True
                or preview.payload['user']['id'] != user['id']
                or preview.payload['user']['facts'] != user['facts']
                or not self._policy(request).authorized_import(user['facts']['user_text'],
                    preview.payload['inputs']['text'], preview.payload['inputs']['format'])):
            raise AgentDataToolError('asr_lexicon_update_not_completed')
        result = operation.payload['result']
        actual = self.lexicon.read()
        terms = {row['id']: row for row in actual['terms']}
        if (result.get('verified') is not True or actual['revision'] != result.get('revision')
                or any(terms.get(row['id']) != row for row in result['terms'])):
            raise AgentDataToolError('asr_lexicon_update_not_completed')
        completed = [event for event in self.turns.events_after(turn_id)
            if event.get('type') == 'tool.completed'
            and event.get('data', {}).get('capability_id') == CAPABILITIES[2]
            and event['data'].get('receipt_ref') == result.get('receipt_ref')]
        if len(completed) != 1 or self.turns.get(completed[0]['data']['payload_ref']) != result:
            raise AgentDataToolError('asr_lexicon_update_not_completed')
        return result

    def _enlisted(self, tx):
        return type(self.lexicon)(TransactionRecords(tx), legacy_store=self.lexicon.legacy)

    @staticmethod
    def _record_read(tx, turn_id, revision):
        prior = tx.read(READS, turn_id)
        tx.put(READS, turn_id, {'revision': revision}, expected_revision=prior.revision if prior else 0)

    @staticmethod
    def _policy(request):
        version = request.get('policy_versions', {}).get('agent_data')
        if version != '@1':
            raise AgentDataToolError('agent_data_policy_unavailable')
        return get('agent_data', version=version)

    def _binding(self, turn_id, reader=None):
        reader = reader or self.records
        frozen = self.request_loader(turn_id)
        binding = frozen.get('agent_binding', {})
        session = frozen.get('session_id', '')
        if (frozen.get('turn_id') != turn_id or frozen.get('desired_outcome') != 'project.task'
                or frozen.get('execution_policy', {}).get('template_version') != 3
                or binding.get('role') != 'main' or binding.get('depth') != 0
                or not session.startswith('session-')):
            raise AgentDataToolError('agent_data_user_binding_invalid')
        identity = session[len('session-'):]
        user, execution = reader.read('v2_turns', identity), reader.read('v2_task_executions', identity)
        try:
            text = json.loads(frozen['input']['text'])['task']
        except (ValueError, KeyError, TypeError):
            raise AgentDataToolError('agent_data_user_binding_invalid') from None
        if (user is None or execution is None or user.payload.get('intent') != 'do'
                or user.payload.get('project_id') != frozen['scope']['project_id']
                or user.payload.get('user_text') != text
                or user.payload.get('receipt', {}).get('do', {}).get('kernel_turn_id') != turn_id
                or any(execution.payload.get('request', {}).get(key) != frozen.get(key)
                       for key in ('input', 'scope', 'privacy', 'execution_policy', 'policy_versions'))):
            raise AgentDataToolError('agent_data_user_binding_invalid')
        facts = {key: user.payload.get(key) for key in ('user_text', 'created_at', 'thread_id', 'project_id', 'intent')}
        return frozen, {'id': identity, 'revision': user.revision, 'facts': facts}

    def _preview(self, turn_id, identity, reader=None):
        reader = reader or self.records
        frozen, user = self._binding(turn_id, reader)
        row = reader.read(PREVIEWS, identity) if isinstance(identity, str) else None
        if (row is None or row.payload['turn_id'] != turn_id
                or row.payload['user']['id'] != user['id']
                or row.payload['user']['facts'] != user['facts']
                or row.payload['user']['revision'] > user['revision']):
            raise AgentDataToolError('agent_data_preview_changed')
        if reader.read(AUTHORIZATIONS, user['id']) is not None:
            raise AgentDataToolError('agent_data_instruction_consumed')
        inputs = row.payload['inputs']
        if not self._policy(frozen).authorized_import(user['facts']['user_text'], inputs['text'], inputs['format']):
            raise AgentDataToolError('agent_data_explicit_instruction_required')
        return row, frozen, user

    def can_import(self, turn_id, arguments):
        if not isinstance(arguments, dict) or set(arguments) != {'preview_id'}:
            return False
        try:
            self._preview(turn_id, arguments['preview_id'])
            return True
        except (AgentDataToolError, KeyError, TypeError):
            return False

    def constrain_decision(self, request, decision):
        calls = [decision] if decision.get('type') == 'tool' else decision.get('calls', []) if decision.get('type') == 'tools' else []
        imports = [call for call in calls if call.get('capability_id') == CAPABILITIES[2]]
        if imports and (len(calls) != 1 or not self.can_import(request['turn_id'], imports[0].get('arguments'))):
            return {'type': 'complete', 'summary': '热词表尚未保存。请以“更新热词表：”开头，换行提供要导入的完整词表。'}
        return decision

    def invoke(self, identity, call):
        turn_id, args = call['turn_id'], call.get('arguments', {})
        schema = self.schemas.get(identity.rsplit('.', 1)[-1])
        if schema is None or not schema.is_valid(args):
            raise AgentDataToolError('agent_data_arguments_invalid')
        frozen, user = self._binding(turn_id)
        if identity not in frozen['capability_policy']['allowed'] or identity in frozen['capability_policy']['denied']:
            raise AgentDataToolError('agent_data_tool_not_allowed')
        if identity == CAPABILITIES[0]:
            if not isinstance(args, dict) or set(args) != {'view'} or args['view'] not in {'skill', 'terms'}:
                raise AgentDataToolError('agent_data_arguments_invalid')
            result = ({'name': self.skill.name, 'instructions': self.instructions}
                      if args['view'] == 'skill' else self.lexicon.read())
            if args['view'] == 'terms':
                with self.records.begin() as tx:
                    if self._enlisted(tx).read()['revision'] != result['revision']:
                        raise AgentDataToolError('agent_data_read_revision_changed')
                    self._record_read(tx, turn_id, result['revision'])
                    tx.commit()
        elif identity == CAPABILITIES[1]:
            if not isinstance(args, dict) or set(args) != {'text', 'format', 'expected_revision'}:
                raise AgentDataToolError('agent_data_arguments_invalid')
            result = self.lexicon.import_text(**args, preview=True)
            authorized = self._policy(frozen).authorized_import(user['facts']['user_text'], args['text'], args['format'])
            preview_id = 'lexicon-preview-' + uuid4().hex
            with self.records.begin() as tx:
                _, current_user = self._binding(turn_id, tx)
                if current_user['facts'] != user['facts']:
                    raise AgentDataToolError('agent_data_user_binding_invalid')
                authorized = authorized and tx.read(AUTHORIZATIONS, current_user['id']) is None
                if self._enlisted(tx).read()['revision'] != result['revision']:
                    raise AgentDataToolError('agent_data_read_revision_changed')
                self._record_read(tx, turn_id, result['revision'])
                tx.put(PREVIEWS, preview_id, {'turn_id': turn_id, 'user': current_user,
                    'inputs': dict(args), 'authorized': authorized}, expected_revision=0)
                tx.commit()
            result = {**result, 'preview_id': preview_id, 'authorized': authorized}
        elif identity == CAPABILITIES[2]:
            if not isinstance(args, dict) or set(args) != {'preview_id'}:
                raise AgentDataToolError('agent_data_arguments_invalid')
            operation = call['operation_id']
            with self.records.begin() as tx:
                previous = tx.read(OPERATIONS, operation)
                inputs = {'turn_id': turn_id, 'preview_id': args['preview_id']}
                if previous is not None:
                    if previous.payload['inputs'] != inputs:
                        raise AgentDataToolError('agent_data_operation_changed')
                    result = dict(previous.payload['result'])
                else:
                    preview, _, current_user = self._preview(turn_id, args['preview_id'], tx)
                    self._approved(turn_id, args)
                    enlisted = self._enlisted(tx)
                    result = enlisted.import_text(**preview.payload['inputs'], preview=False)
                    actual = enlisted.read()
                    actual_terms = {row['id']: row for row in actual['terms']}
                    if (actual['revision'] != result['revision']
                            or any(actual_terms.get(row['id']) != row for row in result['terms'])):
                        raise AgentDataToolError('agent_data_verification_failed')
                    result = {**result, 'verified': True,
                        'receipt_ref': f'crp://default/{OPERATIONS}/{operation}'}
                    tx.put(OPERATIONS, operation, {'inputs': inputs, 'user': current_user,
                        'result': result}, expected_revision=0)
                    tx.put(AUTHORIZATIONS, current_user['id'], {'turn_id': turn_id,
                        'operation_id': operation, 'preview_id': args['preview_id'], 'user': current_user},
                        expected_revision=0)
                    self._record_read(tx, turn_id, result['revision'])
                tx.commit()
        else:
            raise AgentDataToolError('agent_data_tool_not_allowed')
        output = {'summary': '热词表操作完成', 'receipt_ref': result.get('receipt_ref'),
                  'payload_ref': None, 'evidence_refs': [], 'result': result}
        self.schemas['result'].validate(output)
        return output

    def _approved(self, turn_id, args):
        # 准许事实来自原内核真实用户动作，不能由工具参数声称。
        for event in reversed(tuple(self.turns.events_after(turn_id))):
            if event.get('type') != 'approval.resolved':
                continue
            action = self.turns.get(event['data']['payload_ref'])
            if action.get('type') != 'approve' or action.get('actor') != 'user':
                break
            target = next((row for row in self.turns.events_after(turn_id)
                           if row['event_id'] == action['target_event_id']), None)
            decision = self.turns.get(target['data']['payload_ref']) if target else {}
            if decision.get('capability_id') == CAPABILITIES[2] and decision.get('arguments') == args:
                return
            break
        raise AgentDataToolError('agent_data_approval_unavailable')

    def approve_explicit_import(self, request, runner):
        events = tuple(self.turns.events_after(request['turn_id']))
        if not events or events[-1].get('type') != 'approval.required':
            return False
        target = events[-1]
        decision = self.turns.get(target['data']['payload_ref'])
        if decision.get('capability_id') != CAPABILITIES[2] or not self.can_import(request['turn_id'], decision.get('arguments')):
            return False
        action = validate_turn_action({'schema_version': '1.0.0',
            'action_id': 'action-' + uuid4().hex, 'turn_id': request['turn_id'], 'type': 'approve',
            'target_event_id': target['event_id'], 'reason': 'consume exact recorded lexicon instruction',
            'actor': 'user', 'expected_sequence': len(events),
            'idempotency_key': 'lexicon-approval-' + target['event_id'], 'created_at': _now()})
        runner.apply_action_and_wait(action)
        return True
