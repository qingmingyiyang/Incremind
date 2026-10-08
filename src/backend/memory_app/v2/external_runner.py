"""将真实交付、冻结任务和宿主租约绑定到原 Kernel 原生执行能力。"""
from collections.abc import Mapping
from copy import deepcopy
import base64
import json
from pathlib import Path

from backend.shared.secret_detection import contains_secret
from core.ai_kernel import CapabilityDefinition, validate_turn_request
from core.ai_kernel.dispatcher import ToolExecutionContext, ToolProviderFailure
from core.ai_kernel.tool_invocation import intent_from_payload
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.ai_tooling import ToolDefinition, ToolRetryPolicy, tool_contract_identity

from .budget import text_tokens
from .external_adapters import build_launch_plan
from .external_workspace import (create_task_workspace, task_material_payloads, task_path_identity,
    validate_memory_mcp_config, validate_task_path, verify_task_material)


CAPABILITY = 'external.task.execute'
ARCHIVE = 'external-task-run-v1'
PREPARATIONS = 'v2_external_task_preparations'
_FIELDS = {'schema_version','owner_id','turn_id','request','delivery','launch','mcp_config',
    'host_permission_refs','memory_proof'}
_LEGACY_LAUNCH = {'executor','cli_version','preset','commands','cwd'}
_LAUNCH = _LEGACY_LAUNCH | {'material_directory','input_policy','stdin_text','materials'}


class ExternalRunnerError(ValueError):
    """错误码不包含资料、CLI 参数、凭据或本机路径。"""


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _reject():
    raise ExternalRunnerError('external_runner_binding_invalid')


def definition():
    """每用户 CAS 槽限制并发，不持全 dispatcher 的独占闸或跨用户锁。"""
    base = 'crp://default/contracts/external-task-'
    tool = ToolDefinition(CAPABILITY, 1, 'External task', 'Execute a frozen external task',
        'core', 'external-task-runner', 'external', ('project_content',), 'provider',
        base+'request.schema.json', base+'result.schema.json', base+'receipt.schema.json',
        'receipt_required', 'parallel', (), 'never_retry', ToolRetryPolicy(1, 0, ()),
        None, None, 'irreversible', 'remote', ('external_cli',), ('project_content',),
        1_230_000, (), ('external_execute',))
    return CapabilityDefinition(CAPABILITY, 1, 'external', False, 'receipt_required',
        tool.input_schema_uri, tool.output_schema_uri, tool)


class ExternalRunner:
    def __init__(self, records, *, owner_id, context, host, turns, frozen_authorization=None):
        if (owner_id != context.owner_id or owner_id != host.owner_id or context.turns is not turns
                or context.records is not records or host.records is not records):
            _reject()
        self.records, self.owner_id = records, owner_id
        self.context, self.host, self.turns = context, host, turns
        self.frozen_authorization = frozen_authorization
        self.runtime = None
        self.application = None
        self.authority = None
        self.turn_binding_guard = None
        self.execution_boundary = None

    def configure(self, *, runtime, frozen_authorization, boundary_profiles,
            turn_binding_guard, execution_boundary, application):
        """装配原权威对象；不复制授权事实、存储或 Kernel。"""
        from .external_authority import ExternalExecutionAuthority
        if self.runtime is not None or self.context.runtime is not runtime:
            _reject()
        self.runtime, self.application = runtime, application
        self.frozen_authorization = frozen_authorization
        self.turn_binding_guard = turn_binding_guard
        self.execution_boundary = execution_boundary
        self.authority = ExternalExecutionAuthority(runner=self, boundary_profiles=boundary_profiles,
            turn_binding_guard=turn_binding_guard, execution_boundary=execution_boundary)

    def authorize(self, request, capability, boundary_request):
        """缺原冻结授权路径时拒绝，避免 dispatcher 持 profile 锁运行 CLI。"""
        if self.authority is None or self.frozen_authorization is None:
            return None
        try:
            saved = self.turns.get_immutable_payload(request['turn_id'], ARCHIVE)
            if saved is None:
                return None
            self._revalidate_memory(saved[1]['memory_proof'], saved[1]['launch']['executor'],
                config=saved[1]['mcp_config'])
        except Exception:
            return None
        return self.authority(request, capability, boundary_request)

    def _require_memory(self, config, executor):
        from .external_memory_admission import MemoryAdmissionError, require_memory_service
        from .external_host import ExternalHostError
        try:
            return require_memory_service(self.application, config=config,
                client={'codex':'codex', 'claude-code':'claude'}.get(executor),
                environment=self.host._memory_environment())
        except (MemoryAdmissionError, ExternalHostError):
            raise ExternalRunnerError('external_runner_memory_unavailable') from None

    def _check_memory_configuration(self, config, executor):
        from .external_memory_admission import MemoryAdmissionError, check_memory_configuration
        from .external_host import ExternalHostError
        try:
            check_memory_configuration(self.application, config=config,
                client={'codex':'codex', 'claude-code':'claude'}.get(executor),
                environment=self.host._memory_environment())
        except (MemoryAdmissionError, ExternalHostError):
            raise ExternalRunnerError('external_runner_memory_unavailable') from None

    def _claim_preparation(self, turn_id):
        # 原事务只占位一次；释放写锁后才探测 SDK。失败占位保留，重试使用新的 Turn。
        with self.records.begin() as tx:
            if tx.read(PREPARATIONS, turn_id) is not None:
                _reject()
            tx.put(PREPARATIONS, turn_id, {'owner_id':self.owner_id, 'turn_id':turn_id}, expected_revision=0)
            tx.commit()

    def _revalidate_memory(self, proof, executor, *, config):
        from .external_memory_admission import MemoryAdmissionError, revalidate_memory_service
        try:
            self._composition_current()
            self._check_memory_configuration(config, executor)
            revalidate_memory_service(self.application, proof,
                client={'codex':'codex', 'claude-code':'claude'}.get(executor))
        except MemoryAdmissionError:
            raise ExternalRunnerError('external_runner_memory_unavailable') from None

    def _composition_current(self):
        state = getattr(self.application, 'state', None)
        if (state is None or getattr(state, 'external_runner', None) is not self
                or getattr(state, 'external_execution_host', None) is not self.host
                or getattr(state, 'external_context', None) is not self.context
                or getattr(state, 'ai_turn_store', None) is not self.turns
                or getattr(state, 'ai_runtime', None) is not self.runtime):
            _reject()

    def _prepared_replay(self, turn_id, *, delivery_turn_id, executor, cli_version, task, mcp_config,
            session_id, operation_id, idempotency_key, created_at, preset, commands, host_permission_refs,
            folder, attachments):
        """已有任务只读原绑定，不生成新的目录交付，也不修补无绑定的历史任务。"""
        original = self.turns.get_request(turn_id)
        if original is None:
            return None
        saved = self.turns.get_immutable_payload(turn_id, ARCHIVE)
        events = self.turns.events_after(turn_id)
        if (saved is None or not isinstance(saved[1], dict) or set(saved[1]) != _FIELDS
                or any(event['type'] in {'turn.failed', 'turn.cancelled'} for event in events)):
            _reject()
        ref, archive = saved
        request = validate_turn_request(original)
        launch = archive['launch']
        if (archive['owner_id'] != self.owner_id or archive['turn_id'] != turn_id
                or archive['schema_version'] != '1.0.0'
                or _encoded(archive['request']) != _encoded(request)
                or request['desired_outcome'] != 'project.task'
                or request['execution_policy']['template_version'] != 2
                or request['capability_request']['arguments'] != {'binding_ref':ref}
                or (session_id, operation_id, idempotency_key, created_at, task)
                    != tuple(request[key] for key in ('session_id', 'operation_id', 'idempotency_key', 'created_at'))
                        + (request['input']['text'],)
                or archive['delivery']['turn_id'] != delivery_turn_id
                or (launch['executor'], launch['cli_version'], launch['preset'], launch['commands'])
                    != (executor, cli_version, preset, commands)
                or _encoded(archive['mcp_config']) != _encoded(mcp_config)
                or _encoded(archive['host_permission_refs']) != _encoded(host_permission_refs)):
            _reject()
        self._revalidate_memory(archive['memory_proof'], executor, config=archive['mcp_config'])
        delivery, _ = self._delivery(request, delivery_turn_id)
        if _encoded(delivery) != _encoded(archive['delivery']):
            _reject()
        plan = self._archive_plan(archive)
        if (preset == 'folder' and folder != plan.cwd) or (preset != 'folder' and folder is not None):
            _reject()
        if set(launch) == _LEGACY_LAUNCH and attachments:
            _reject()
        self.host._permission_check(request, plan,
            {'folder':None, 'commands':None} if host_permission_refs is None else host_permission_refs,
            self.records)
        saved_materials = self._archive_materials(archive)
        self._material_snapshot(plan, delivery, archive['mcp_config'], {} if attachments is None else attachments,
            saved=saved_materials, legacy=saved_materials is None)
        return deepcopy(request)

    def prepare(self, turn_id, *, delivery_turn_id, executor, cli_version, task, mcp_config,
            session_id, operation_id, idempotency_key, created_at,
            preset='workspace', commands='disabled', host_permission_refs=None, folder=None, attachments=None):
        """先核验记忆入口，再由原 Kernel 接受任务并冻结宿主交付绑定。"""
        newly_accepted = False
        try:
            self._composition_current()
            if self.runtime is None or self.frozen_authorization is None:
                _reject()
            replay = self._prepared_replay(turn_id, delivery_turn_id=delivery_turn_id, executor=executor,
                cli_version=cli_version, task=task, mcp_config=mcp_config, session_id=session_id,
                operation_id=operation_id, idempotency_key=idempotency_key, created_at=created_at,
                preset=preset, commands=commands, host_permission_refs=host_permission_refs,
                folder=folder, attachments=attachments)
            if replay is not None:
                return replay
            self._check_memory_configuration(mcp_config, executor)
            client = {'codex':'codex', 'claude-code':'claude'}.get(executor)
            delivery = self.context.qualified_delivery(delivery_turn_id)
            if delivery['owner_id'] != self.owner_id or delivery['client'] != client:
                _reject()
            # 先复用原 writer 的输入校验，不在 SDK、Turn 或文件写入后才发现坏附件。
            payloads = task_material_payloads(task=task, handoff=delivery['handoff'], mcp_config=mcp_config,
                attachments=attachments, memory_endpoint=self.host.memory_endpoint)
            attachments = {name:content for name, content in payloads.items()
                if name not in {'TASK.md', 'CONTEXT.md', 'memory-mcp.json'}}
            body = {'task':task, 'handoff':delivery['handoff'],
                'attachments':{name:content.decode('utf-8', errors='replace') for name, content in attachments.items()}}
            texts = [task, delivery['handoff']['text'], *body['attachments'], *body['attachments'].values()]
            if any(not self.host.material_text_is_safe(text) for text in texts):
                _reject()
            sanitized = self.execution_boundary.sanitize_candidate_arguments(
                definition(), body, turn_id=turn_id)
            if sanitized is None or _encoded(sanitized) != _encoded(body):
                _reject()
            frozen = freeze_turn_request('project.task', template_version=2, turn_id=turn_id,
                session_id=session_id, operation_id=operation_id, idempotency_key=idempotency_key,
                project_id=delivery['project_id'], created_at=created_at, text=task,
                privacy=delivery['privacy'], refs=delivery['refs'],
                capability_request={'mode':'execute_exact_v1', 'capability_id':CAPABILITY,
                    'arguments':{'binding_ref':'crp://session/' + turn_id + '/' + ARCHIVE}})
            frozen['policy_versions'] = delivery['policy_versions']
            validate_turn_request(frozen)
            registration = self.host.registrations[executor]
            if preset == 'folder':
                permission = self.host.permissions.folder_reference(folder)
                if (permission is None or not isinstance(host_permission_refs, Mapping)
                        or host_permission_refs.get('folder') != permission):
                    _reject()
                material_root, cwd = folder, folder
            else:
                if folder is not None:
                    _reject()
                material_root = self.host.deployment.user_root
                cwd = material_root / 'agent_workspaces' / turn_id
            material_directory = material_root / 'agent_workspaces' / turn_id
            validate_task_path(material_directory)
            if material_directory.exists():
                _reject()
            plan = build_launch_plan(executor, cli_version=cli_version, executable=registration.executable,
                cwd=cwd, task=task, mcp_config=mcp_config, preset=preset, commands=commands,
                memory_endpoint=self.host.memory_endpoint,
                material_directory=material_directory if preset == 'folder' else None)
            # 核许可不需要外部进程；目录仍将在版本后和真正启动前复验。
            self.host._permission_check(frozen, plan,
                {'folder':None, 'commands':None} if host_permission_refs is None else host_permission_refs,
                self.records)
            root_identity = task_path_identity(material_root, directory=True)

            def destination_current():
                if task_path_identity(material_root, directory=True) != root_identity:
                    _reject()
                self.host._permission_check(frozen, plan,
                    {'folder':None, 'commands':None} if host_permission_refs is None else host_permission_refs,
                    self.records)

            self._claim_preparation(turn_id)
            memory_proof = self._require_memory(mcp_config, executor)
            destination_current()
            accepted = self.runtime.accept_turn(frozen)
            newly_accepted = not accepted.replayed
            if accepted.turn_id != turn_id or accepted.status in {'failed', 'cancelled'}:
                _reject()
            destination_current()
            create_task_workspace(material_root, turn_id, task=task, handoff=delivery['handoff'],
                mcp_config=mcp_config, attachments=attachments, memory_endpoint=self.host.memory_endpoint,
                expected_root_identity=root_identity)
            self.bind(frozen, delivery_turn_id=delivery_turn_id, plan=plan, mcp_config=mcp_config,
                host_permission_refs=host_permission_refs, memory_proof=memory_proof, attachments=attachments)
            return deepcopy(frozen)
        except ExternalRunnerError:
            if newly_accepted:
                self.runtime.fail_accepted_turn(turn_id)
            raise
        except Exception:
            if newly_accepted:
                self.runtime.fail_accepted_turn(turn_id)
            raise ExternalRunnerError('external_runner_not_prepared') from None

    def _delivery(self, request, delivery_turn_id):
        delivery = self.context.qualified_delivery(delivery_turn_id)
        client = {'codex':'codex','claude-code':'claude'}
        if (delivery['owner_id'] != self.owner_id
                or delivery['project_id'] != request['scope']['project_id']
                or _encoded(delivery['privacy']) != _encoded(request['privacy'])
                or delivery['refs'] != request['input']['refs']
                or delivery['policy_versions'] != request['policy_versions']):
            _reject()
        return delivery, client

    def _archive_plan(self, archive):
        launch = archive['launch']
        legacy = set(launch) == _LEGACY_LAUNCH
        if ((not legacy and (set(launch) != _LAUNCH or not isinstance(launch['materials'], Mapping)))
                or (legacy and (launch['preset'] != 'workspace'
                    or Path(launch['cwd']) != self.host.deployment.user_root / 'agent_workspaces' / archive['turn_id']))):
            _reject()
        material_directory = launch.get('material_directory')
        plan = build_launch_plan(launch['executor'], cli_version=launch['cli_version'],
            executable=self.host.registrations[launch['executor']].executable, cwd=Path(launch['cwd']),
            task=archive['request']['input']['text'], mcp_config=archive['mcp_config'],
            preset=launch['preset'], commands=launch['commands'], memory_endpoint=self.host.memory_endpoint,
            material_directory=Path(material_directory) if material_directory is not None else None)
        if not legacy and (launch['input_policy'] != plan.input_policy or launch['stdin_text'] != plan.stdin_text):
            _reject()
        return plan

    def _archive_materials(self, archive):
        # 原 workspace 归档只核原三份控制材料，不推断或补写历史附件身份。
        self._archive_plan(archive)
        return None if set(archive['launch']) == _LEGACY_LAUNCH else archive['launch']['materials']

    def _material_snapshot(self, plan, delivery, config, attachments=None, *, saved=None, legacy=False):
        path = plan.material_directory or plan.cwd
        if legacy:
            if saved is not None or attachments:
                _reject()
            expected = task_material_payloads(task=plan.input_text, handoff=delivery['handoff'], mcp_config=config,
                memory_endpoint=self.host.memory_endpoint)
            for name in ('TASK.md', 'CONTEXT.md'):
                verify_task_material(path / name, expected[name])
            mcp_path = path / 'memory-mcp.json'
            validate_task_path(mcp_path)
            if (validate_memory_mcp_config(json.loads(mcp_path.read_text(encoding='utf-8')),
                    memory_endpoint=self.host.memory_endpoint) != expected['memory-mcp.json']):
                _reject()
            # 原五字段档案只证明原三份材料语义，不产生或补写文件身份与附件证明。
            return {'path':str(path), 'files':{name:{'content':base64.b64encode(content).decode('ascii')}
                for name, content in expected.items()}}
        if saved is not None:
            if (not isinstance(saved, Mapping) or set(saved) != {'path','directory_identity','files'}
                    or saved['path'] != str(path) or not isinstance(saved['files'], Mapping)):
                _reject()
            decoded = {}
            for name, value in saved['files'].items():
                if not isinstance(value, Mapping) or set(value) != {'content','identity'}:
                    _reject()
                decoded[name] = base64.b64decode(value['content'], validate=True)
            frozen_attachments = {name:content for name, content in decoded.items()
                if name not in {'TASK.md','CONTEXT.md','memory-mcp.json'}}
            if attachments is not None and dict(attachments) != frozen_attachments:
                _reject()
            # 启动接点只读冻结附件；prepare 回放另行传入明确的调用方附件集合。
            attachments = frozen_attachments
        expected = task_material_payloads(task=plan.input_text, handoff=delivery['handoff'], mcp_config=config,
            attachments=attachments, memory_endpoint=self.host.memory_endpoint)
        if saved is not None and decoded != expected:
            _reject()
        directory_identity = task_path_identity(path, directory=True)
        if saved is not None and (directory_identity != saved['directory_identity'] or set(expected) != set(saved['files'])):
            _reject()
        files = {}
        for name, content in expected.items():
            file = path / name
            identity = verify_task_material(file, content)
            if saved is not None and identity != saved['files'][name]['identity']:
                _reject()
            files[name] = {'content':base64.b64encode(content).decode('ascii'), 'identity':identity}
        if task_path_identity(path, directory=True) != directory_identity:
            _reject()
        return {'path':str(path), 'directory_identity':directory_identity, 'files':files}

    def _materials(self, lease, delivery, config, *, materials=None, attachments=None, legacy=False):
        plan = lease.plan
        if (lease.redact(plan.input_text) != plan.input_text
                or lease.redact(delivery['handoff']['text']) != delivery['handoff']['text']
                or text_tokens(delivery['handoff']['text']) > delivery['handoff']['budget']):
            _reject()
        snapshot = self._material_snapshot(plan, delivery, config, attachments, saved=materials, legacy=legacy)
        for name, value in snapshot['files'].items():
            text = base64.b64decode(value['content'], validate=True).decode('utf-8', errors='replace')
            if lease.redact(text) != text or contains_secret(name):
                _reject()
        lease.validate()
        return snapshot

    def bind(self, frozen, *, delivery_turn_id, plan, mcp_config, host_permission_refs=None, memory_proof=None,
            attachments=None):
        """接受后冻结绑定；不启动任务，不把调用方的请求当成实际 Turn。"""
        try:
            if self.application is not None:
                if memory_proof is None:
                    memory_proof = self._require_memory(mcp_config, plan.executor)
                self._revalidate_memory(memory_proof, plan.executor, config=mcp_config)
            request = validate_turn_request(frozen)
            turn_id = request['turn_id']
            if (request['desired_outcome'] != 'project.task'
                    or type(request['execution_policy']['template_version']) is not int
                    or request['execution_policy']['template_version'] != 2
                    or _encoded(self.turns.get_request(turn_id)) != _encoded(request)
                    or plan.input_text != request['input']['text']
                    or contains_secret(request['input']['text'])):
                _reject()
            delivery, clients = self._delivery(request, delivery_turn_id)
            if delivery['client'] != clients.get(plan.executor):
                _reject()
            lease = self.host.prepare(request, plan, mcp_config=mcp_config,
                host_permission_refs=host_permission_refs)
            try:
                materials = self._materials(lease, delivery, mcp_config, attachments=attachments)
                current_delivery, _ = self._delivery(request, delivery_turn_id)
                if _encoded(current_delivery) != _encoded(delivery):
                    _reject()
                archive = {'schema_version':'1.0.0','owner_id':self.owner_id,'turn_id':turn_id,
                    'request':request,'delivery':delivery,'mcp_config':mcp_config,
                    'host_permission_refs':host_permission_refs,
                    'memory_proof':memory_proof,
                    'launch':{'executor':plan.executor,'cli_version':plan.cli_version,
                        'preset':plan.preset,'commands':plan.requested_commands,'cwd':str(plan.cwd),
                        'material_directory':str(plan.material_directory) if plan.material_directory is not None else None,
                        'input_policy':plan.input_policy, 'stdin_text':plan.stdin_text, 'materials':materials}}
                ref = self.turns.get_or_create_immutable_payload(turn_id, ARCHIVE, deepcopy(archive))
                if ref != request['capability_request']['arguments']['binding_ref']:
                    _reject()
                return ref
            finally:
                lease.close()
        except ExternalRunnerError:
            raise
        except Exception:
            raise ExternalRunnerError('external_runner_binding_invalid') from None

    def _invocation(self, value):
        if not isinstance(value, Mapping) or self.frozen_authorization is None:
            _reject()
        turn_id = value.get('turn_id')
        saved = self.turns.get_immutable_payload(turn_id, ARCHIVE)
        if saved is None or not isinstance(saved[1], dict) or set(saved[1]) != _FIELDS:
            _reject()
        ref, archive = saved
        request = validate_turn_request(archive['request'])
        context = value.get('execution_context')
        if (archive['schema_version'] != '1.0.0' or archive['owner_id'] != self.owner_id
                or archive['turn_id'] != turn_id or request['turn_id'] != turn_id
                or _encoded(self.turns.get_request(turn_id)) != _encoded(request)
                or value.get('capability_id') != CAPABILITY
                or type(value.get('capability_version')) is not int or value['capability_version'] != 1
                or value.get('arguments') != {'binding_ref':ref}
                or value.get('privacy') != request['privacy'] or value.get('scope') != request['scope']
                or not isinstance(context, ToolExecutionContext)):
            _reject()
        # 原持久 intent 与 facts 证明所走的是短冻结授权路径，不持长期 profile 锁。
        intent = intent_from_payload(self.turns.get(value['intent_ref']))
        if (intent.turn_id != turn_id or intent.invocation_id != context.invocation_id
                or intent.invocation_id != value.get('tool_call_id') or context.attempt != value.get('attempt')
                or context.attempt != 1 or context.timeout_ms != intent.timeout_ms
                or value.get('timeout_ms') != intent.timeout_ms
                or value.get('operation_id') != intent.operation_id
                or intent.operation_id != request['operation_id']
                or value.get('idempotency_key') != intent.idempotency_key
                or value.get('tool_contract') != intent.tool_contract
                or value.get('resource_locks') != list(intent.resource_locks)
                or intent.capability_id != CAPABILITY or intent.arguments != value['arguments']
                or intent.tool_contract != tool_contract_identity(definition().tool_definition)
                or intent.authorization_facts_ref is None or intent.authorization_facts_revision is None
                or intent.authorization_facts_ref != value.get('authorization_facts_ref')
                or intent.authorization_facts_revision != value.get('authorization_facts_revision')):
            _reject()
        self.frozen_authorization.load(turn_id=turn_id, facts_ref=intent.authorization_facts_ref,
            facts_revision=intent.authorization_facts_revision)
        if not self.frozen_authorization.prepare_intent(intent):
            _reject()
        delivery, clients = self._delivery(request, archive['delivery']['turn_id'])
        if (delivery['client'] != clients.get(archive['launch']['executor'])
                or _encoded(delivery) != _encoded(archive['delivery'])):
            _reject()
        return request, archive, context

    def invoke(self, value):
        execution_entered = False
        try:
            request, archive, context = self._invocation(value)
            if self.application is not None:
                self._revalidate_memory(archive['memory_proof'], archive['launch']['executor'],
                    config=archive['mcp_config'])
            if self.turn_binding_guard is not None:
                self.turn_binding_guard.ensure_current(request)
            launch = archive['launch']
            plan = self._archive_plan(archive)
            lease = self.host.prepare(request, plan, mcp_config=archive['mcp_config'],
                host_permission_refs=archive['host_permission_refs'])
            try:
                material_snapshot = self._archive_materials(archive)
                self._materials(lease, archive['delivery'], archive['mcp_config'], materials=material_snapshot,
                    legacy=material_snapshot is None)
                # 延后导入既有生命周期编排，保持本模块与原 composition 的依赖方向。
                from .external_execution import execute_external

                def before_launch():
                    if self.application is not None:
                        self._revalidate_memory(archive['memory_proof'], launch['executor'],
                            config=archive['mcp_config'])
                    if self.turn_binding_guard is not None:
                        self.turn_binding_guard.ensure_current(request)
                    if _encoded(self.turns.get_request(request['turn_id'])) != _encoded(request):
                        _reject()
                    material_snapshot = self._archive_materials(archive)
                    self._materials(lease, archive['delivery'], archive['mcp_config'], materials=material_snapshot,
                        legacy=material_snapshot is None)
                    delivery, _ = self._delivery(request, archive['delivery']['turn_id'])
                    if _encoded(delivery) != _encoded(archive['delivery']):
                        _reject()

                execution_entered = True
                return execute_external(lease, context, records=self.records, turns=self.turns,
                    turn_id=request['turn_id'], owner_id=self.owner_id, before_launch=before_launch)
            finally:
                lease.close()
        except ToolProviderFailure:
            raise
        except Exception:
            # 生命周期若意外失去证据，不能假称从未产生外部效果。
            raise ToolProviderFailure('external_runner_not_admitted',
                effect_certainty='unknown' if execution_entered else 'confirmed_none') from None
