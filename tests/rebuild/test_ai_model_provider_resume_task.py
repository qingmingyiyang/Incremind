"""原 Do 消费者沿真实组织、闭合胶囊和新尝试恢复，外部 HTTP 传输隔离。"""
from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path
import sqlite3
from time import monotonic
import traceback

import httpx
import pytest
from fastapi import HTTPException

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.task_do import TaskDo, TASK_EXECUTIONS
from backend.memory_app.v2.task_drafts import TaskDrafts
from backend.memory_app.v2.turn_execution import TurnExecutionService
from backend.security.secrets import InMemorySecretStore
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.observability import stage


class _FailureObservedPlanner:
    """记录固定安全诊断字段，规划与异常仍由原实现处理。"""
    def __init__(self, inner, failures):
        self.inner, self.failures = inner, failures

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def plan(self, *args, **kwargs):
        try:
            return self.inner.plan(*args, **kwargs)
        except Exception as error:
            self.failures.append({'type': type(error).__name__, 'frames': [
                (frame.filename.replace('\\', '/').split('/')[-1], frame.name, frame.lineno)
                for frame in traceback.extract_tb(error.__traceback__)[-6:]]})
            raise


def _create_product(records, models, *, turn_id, project, thread_id):
    from backend.memory_app.v2.workbench import persist_workbench_turn
    task = TaskDo(records, models, TaskDrafts(records, SQLiteDocumentRepository(records)),
                  None, None, None)
    initial, state = task.initial(turn_id, project, '写出同一份完整方案', None)
    now = datetime.now(timezone.utc).isoformat()
    response = {'thread_id': thread_id, 'turn': {'id': turn_id, 'thread_id': thread_id,
        'intent': 'do', 'user_text': '写出同一份完整方案', 'created_at': now, 'receipt': {'do': initial}}}
    executions = TurnExecutionService(records, task.advance, instance='resume-task-test')
    # 复用原 producer，并保留工作台创建事务的写入、完成和提交顺序。
    with stage('persist'), records.begin() as tx:
        saved = persist_workbench_turn(tx, turn_id=turn_id, project=project, thread_id=thread_id,
            cleaned='写出同一份完整方案', now=now, created_at=now, intent='do', receipt={'do': initial},
            item_id=None, item=None, instance='resume-task-test', run_id=None, title_prefix='',
            replace_turn=None, research_state=state)
        executions.complete(tx, None, response)
        tx.commit()
    return saved, state


def test_original_workbench_writer_preserves_create_transaction(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
    saved, state = _create_product(records, models, turn_id='product-do-one',
                                   project='project-alpha', thread_id='thread-do-one')
    assert saved == records.read('v2_turns', 'product-do-one')
    assert saved.payload['receipt']['do']['kernel_turn_id'] == state['request']['turn_id']
    assert state['request']['session_id'] == 'session-product-do-one'
    assert records.read(TASK_EXECUTIONS, 'product-do-one').payload == state
    assert records.read('v2_threads', 'thread-do-one').payload['project_id'] == 'project-alpha'
    assert state['started'] is False and state['owner'] is None


def test_original_workbench_writer_rejects_foreign_thread_before_product_write(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
    _create_product(records, models, turn_id='product-do-one', project='project-alpha', thread_id='thread-do-one')
    prior = records.read('v2_threads', 'thread-do-one')
    with pytest.raises(HTTPException) as failed:
        _create_product(records, models, turn_id='product-do-two', project='project-beta', thread_id='thread-do-one')
    assert failed.value.status_code == 404 and failed.value.detail == 'workbench_not_found'
    assert records.read('v2_threads', 'thread-do-one') == prior
    assert records.read('v2_turns', 'product-do-two') is None
    assert records.read(TASK_EXECUTIONS, 'product-do-two') is None


class _TaskOwner:
    partial = '第一段方案。\n\n'
    full = partial + '第二段完成。'
    product_id = 'product-native-do'
    project = 'project-alpha'
    thread_id = 'thread-native-do'

    def __init__(self, root, *, frames=True, enabled=True):
        from backend.api.agent_steward_proposal import AgentStewardProposalBuilder
        from backend.api.ai_profile_resolvers import (ProjectAwareCapabilityManifestResolver,
            ProjectAwareContextManifestResolver, TurnProjectProfileSnapshotAuthority)
        from backend.api.ai_turn_runner import AITurnRunner
        from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
        from backend.api.codex_hook_composition import build_codex_hook_host
        from backend.api.model_routing_snapshot_authority import TurnModelRoutingSnapshotAuthority
        from backend.memory_app.kernel.agent_runtime_composition import build_agent_runtime_composition
        from backend.memory_app.kernel.agent_organization_runtime import AgentOrganizationRuntime
        from backend.memory_app.kernel.policy_runtime import ProductPolicyRuntime
        from backend.memory_app.kernel.task_division_authority import frozen_division_capabilities
        from backend.memory_app.kernel.task_planner import ProductOutcomeComposition, ProductTaskPlanner
        from backend.memory_app.research_sources import ReadControl, ReadPlanner
        from backend.memory_app.v2 import TaskRuntimeProjection
        from backend.memory_app.v2.method_context import frozen_task_methods
        from backend.memory_app.v2.inspirations import validate_task_inputs
        from backend.memory_app.v2.outcome_patches import apply_patch, OutcomePatchError
        from backend.memory_app.v2.outcomes import validate_continuation, COMPOSITION
        from backend.memory_app.v2.policies import get
        from backend.memory_app.v2.profile import frozen_task_profile
        from backend.memory_app.v2.style_context import frozen_task_style
        from backend.memory_app.v2.provider_store_settings import ProviderStoreSettings
        from backend.memory_app.v2.turn_frames import TurnFrames
        from backend.memory_app.workspace_query import WorkspaceQuery
        from backend.recognition import RecognitionService
        from backend.security.ai_tool_execution_boundary import AIToolExecutionBoundary, TurnCapabilityBindingGuard
        from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
        from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
        from backend.security.turn_frozen_authorization import TurnFrozenAuthorizationAuthority
        from backend.shared.llm.model_capabilities import ModelCapabilities
        from backend.shared.llm.openai_responses import ResponsesCompletion
        from core.ai_kernel import ScopedCapabilityRegistry, SQLiteAITurnStore
        from core.storage_provider.runtime import JsonObjectStore
        from core.storage_provider.source_asset_runtime import SourceAssetRuntimeStore

        self.root = root
        self.records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
        self.store = SQLiteAITurnStore(root / '.rebuild-data' / 'ai-turns.sqlite3')
        self.registry = ScopedCapabilityRegistry()
        self.composition = build_agent_runtime_composition(runtime_root=root, session_store=self.store,
            registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(self.registry)))
        self.calls, self.main_calls, self.clients, self.main_responses = [], [], [], []
        self.pool_ready = False
        self.pool_failures = 0
        native = ResponsesCompletion(api_base='https://api.openai.com/v1',
                                     capabilities=ModelCapabilities(background_resume=True))
        self.models = ModelConfiguration(self.records, root, InMemorySecretStore(),
            completion_fn=native, model_http_client_factory=self.client)
        # 经原设置主人选择 API 模式及存储，不手写批准记录。
        self.models.update_generation_mode(mode='api', local_enabled=False,
            local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
        self.models.update('generation', {'base_url': native.api_base, 'model': 'synthetic',
            'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
        settings = ProviderStoreSettings(self.records, self.models)
        choice = settings.get()
        settings.update(enabled=enabled, expected_revision=choice['revision'],
            expected_generation_revision=choice['generation_revision'], expected_mode_revision=choice['mode_revision'])
        self.documents = SQLiteDocumentRepository(self.records)
        self.drafts = TaskDrafts(self.records, self.documents)
        self.service = RecognitionService(self.records)
        sources = SourceAssetRuntimeStore(json_store=JsonObjectStore(root / 'sources'),
            sqlite_records=self.records, library_root=root / 'library', authority_identity='resume-test-sqlite')
        self.query = WorkspaceQuery(self.records, self.documents, sources, self.models, self.service)
        capabilities, boundary = ProjectCapabilityProfileStore(root), ProjectBoundaryProfileStore(root)
        builder = AgentStewardProposalBuilder(profiles=self.composition.profiles,
            agent_store=self.composition.store, request_loader=self.composition.request_loader,
            capability_profiles=capabilities, capability_definition=self.registry.get)
        def validate_task_sources(records, request):
            # 原生产装配同时复验成果与写法，不删掉当前 Do 冻结的空写法块。
            validate_continuation(records, request)
            frozen_task_style(records, self.service, request)
        outcome = ProductOutcomeComposition(validate=validate_task_sources,
            policy=lambda version: get('continuation', version=version), patch=apply_patch,
            patch_error=OutcomePatchError, result_kind=COMPOSITION)
        self.planner = ProductTaskPlanner(models=self.models, store=self.store, composition=self.composition,
            guard=lambda request: validate_task_inputs(self.records, self.models, request, query=self.query),
            profile_reader=lambda request: {**frozen_task_profile(self.records, self.service, request),
                                           'methods': frozen_task_methods(self.records, self.query, request),
                                           'style': frozen_task_style(self.records, self.service, request)},
            builder=builder, fallback=None, drafts=self.drafts, records=self.records, runtime_root=root,
            read_control_type=ReadControl, frame_factory=TurnFrames if frames else None, outcome=outcome)
        profiles = TurnProjectProfileSnapshotAuthority(capabilities, boundary)
        # 本用例只有原产品 Main/Steward 路由，公共权威直接委托真实 Task routing。
        routing = TurnModelRoutingSnapshotAuthority(None, self.store,
            agent_binding_verifier=self.composition.coordinator.verify_agent_binding, task_routing=self.planner)
        execution_boundary = AIToolExecutionBoundary(boundary,
            binding_guard=TurnCapabilityBindingGuard(capabilities, boundary, events=self.store, payloads=self.store),
            division_capabilities=lambda request: frozen_division_capabilities(self.composition, request))
        # 沿原生产装配签发工具事实，Main 等待子任务时也保留同一授权边界。
        hook_host = build_codex_hook_host(Path(__file__).resolve().parents[2] / 'config' / 'codex-hooks.toml')
        assert hook_host is not None
        frozen_authorization = TurnFrozenAuthorizationAuthority(payloads=self.store,
            execution_boundary=execution_boundary,
            agent_capability_authorizer=self.composition.coordinator.authorize_agent_capability)
        self.planner_failures = []
        self.runtime = ProductPolicyRuntime(planner=_FailureObservedPlanner(
            ReadPlanner(self.planner, self.records, self.store, self.composition.store), self.planner_failures),
            registry=self.registry, events=self.store, payloads=self.store, state=self.store,
            manifest_resolver=ProjectAwareCapabilityManifestResolver(profiles, model_routing=routing),
            context_manifest_resolver=ProjectAwareContextManifestResolver(profiles, model_routing=routing, payloads=self.store),
            execution_boundary=execution_boundary, hook_host=hook_host, frozen_authorization=frozen_authorization,
            effect_runner=self.store.effect_runner, max_steps=self.composition.profiles.get('main.orchestrator').max_steps)
        self.runtime.task_continuations = self.planner.continuations
        self.runner = AITurnRunner(self.runtime)
        self.composition.bind_runtime(self.runtime)
        self.composition.bind_runner(self.runner)
        self.organization = AgentOrganizationRuntime(coordinator=self.composition.coordinator,
            dispatch_store=self.composition.dispatch_store, run_store=self.composition.store,
            request_loader=self.composition.request_loader)
        self.composition.bind_organization_runtime(self.organization)
        projection = TaskRuntimeProjection(runtime=self.runtime, turn_store=self.store,
            composition=self.composition, organization=self.organization, method_query=self.query)
        self.task = TaskDo(self.records, self.models, self.drafts, projection, projection.read, projection.topology,
            execution_getter=lambda: (self.runtime, self.runner))
        # 未指定人工分工时，真实 Steward 通过模型选择合法的 main_only plan。
        initial, state = self.task.initial(self.product_id, self.project, '写出同一份完整方案', None)
        self.identity = state['request']['turn_id']
        from backend.memory_app.v2.workbench import persist_workbench_turn
        now = datetime.now(timezone.utc).isoformat()
        response = {'thread_id': self.thread_id, 'turn': {'id': self.product_id, 'thread_id': self.thread_id,
            'intent': 'do', 'user_text': '写出同一份完整方案', 'created_at': now, 'receipt': {'do': initial}}}
        executions = TurnExecutionService(self.records, self.task.advance, instance='native-do-test')
        with stage('persist'), self.records.begin() as tx:
            persist_workbench_turn(tx, turn_id=self.product_id, project=self.project, thread_id=self.thread_id,
                cleaned='写出同一份完整方案', now=now, created_at=now, intent='do', receipt={'do': initial},
                item_id=None, item=None, instance='native-do-test', run_id=None, title_prefix='',
                replace_turn=None, research_state=state)
            executions.complete(tx, None, response)
            tx.commit()

    def client(self):
        if any(response.is_closed for response in self.main_responses) and not self.pool_ready:
            # 只隔离外部 HTTP pool；原 native 在 POST 已闭合后处理这个建立失败。
            self.pool_failures += 1
            raise httpx.ConnectError('synthetic provider pool unavailable')
        client = httpx.Client(transport=httpx.MockTransport(self.transport), trust_env=False, follow_redirects=False)
        self.clients.append(client)
        return client

    def transport(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, str(request.url), body))
        main = request.method == 'GET'
        for message in (body or {}).get('input', []):
            try:
                parsed = json.loads(message.get('content', ''))
            except (ValueError, TypeError):
                continue
            main = main or isinstance(parsed, dict) and 'decision_contract' in parsed
        identity = 'resp_native_product_do' if main else 'resp_native_product_steward'
        if main:
            self.main_calls.append(self.calls[-1])
        if not main:
            raw = json.dumps({'mode': 'main_only'})
            events = [{'type': 'response.created', 'sequence_number': 0,
                       'response': {'id': identity, 'status': 'in_progress'}},
                      {'type': 'response.output_text.delta', 'sequence_number': 1, 'delta': raw},
                      {'type': 'response.completed', 'sequence_number': 2,
                       'response': {'id': identity, 'status': 'completed',
                         'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': raw}]}],
                         'usage': {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}}}]
        elif not self.pool_ready:
            raw = json.dumps({'type': 'complete', 'summary': self.full}, ensure_ascii=False)
            raw = raw[:raw.index('第二段') + len('第二')]
            events = [{'type': 'response.created', 'sequence_number': 0,
                       'response': {'id': identity, 'status': 'in_progress'}},
                      {'type': 'response.output_text.delta', 'sequence_number': 1, 'delta': raw}]
        else:
            raw = json.dumps({'type': 'complete', 'summary': self.full}, ensure_ascii=False)
            events = ([{'type': 'response.created', 'sequence_number': 0,
                        'response': {'id': identity, 'status': 'in_progress'}},
                       {'type': 'response.output_text.delta', 'sequence_number': 1, 'delta': raw}]
                      if request.method == 'POST' else []) + [
                {'type': 'response.completed', 'sequence_number': 2,
                 'response': {'id': identity, 'status': 'completed',
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': raw}]}],
                    'usage': {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}}}]
        response = httpx.Response(200, headers={'content-type': 'text/event-stream'},
            stream=httpx.ByteStream(''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()))
        if main and request.method == 'POST':
            self.main_responses.append(response)
        return response

    async def interrupted(self):
        deadline = monotonic() + 20
        while monotonic() < deadline:
            await self.task.advance(self.product_id)
            receipt = self.runtime.receipt_for(self.identity)
            assert receipt.status != 'failed', {
                'events': [event['type'] for event in self.store.events_after(self.identity)][-12:],
                'planner_failures': self.planner_failures}
            if receipt.status == 'waiting_approval':
                await self.task.advance(self.product_id)
                product = self.records.read('v2_turns', self.product_id)
                assert product.payload['receipt']['do']['state'] == 'interrupted'
                binding = self.runtime.task_continuations.paused(self.identity, self.project)
                assert binding is not None and binding[1]['partial'] == self.partial
                return binding
            await asyncio.sleep(0.02)
        raise AssertionError('original Do did not persist a closed partial interruption')

    def close(self):
        assert self.runner.shutdown(timeout_seconds=2) == ()
        assert all(client.is_closed for client in self.clients)


@pytest.mark.asyncio
async def test_original_do_consumer_resumes_primary_main_by_get_without_duplicate_body(tmp_path):
    owner = _TaskOwner(tmp_path)
    try:
        row, capsule = await owner.interrupted()
        assert len(capsule['attempts']) == 1
        before = tuple(owner.store.events_after(owner.identity))
        tools = [event for event in before if event['type'].startswith('tool.')]
        assert tools and any(event['type'] == 'tool.completed' for event in tools)
        original_terminal = owner.store.get(capsule['attempts'][0]['terminal_ref'])
        original_lease = dict(capsule['lease'])
        assert original_terminal['status'] == 'failed_transport' and owner.pool_failures == 1
        assert [method for method, _, _ in owner.calls] == ['POST', 'POST']
        assert [method for method, _, _ in owner.main_calls] == ['POST']
        plans = owner.composition.dispatch_store.list_plans_for_main(project_id=owner.project,
            main_run_id=owner.composition.store.get_run_by_turn_id(owner.identity, project_id=owner.project)[0].run_id)
        assert len(plans) == 1 and plans[0].mode == 'main_only' and plans[0].status == 'completed'
        stewards = [run for run in owner.composition.store.list_runs(project_id=owner.project)
                    if run.profile_id == 'steward.scheduler']
        assert len(stewards) == 1 and stewards[0].status == 'completed'
        assert plans[0].steward_run_id == stewards[0].run_id
        owner.pool_ready = True
        await owner.task.continue_on_request(owner.product_id, owner.project, 'native-do-resume-key')
        assert [method for method, _, _ in owner.calls] == ['POST', 'POST', 'GET']
        assert [method for method, _, _ in owner.main_calls] == ['POST', 'GET']
        assert 'starting_after=1' in owner.calls[-1][1] and '/responses/resp_native_product_do' in owner.calls[-1][1]
        product = owner.records.read('v2_turns', owner.product_id).payload['receipt']['do']
        assert product['state'] == 'done'
        operation = owner.drafts.get_operation('deliver-' + owner.identity)
        assert operation.payload['inputs']['markdown'] == owner.full
        assert operation.payload['result']['document_id'] == product['document_id']
        after = tuple(owner.store.events_after(owner.identity))
        assert [event for event in after if event['type'].startswith('tool.')] == tools
        assert owner.store.get(capsule['attempts'][0]['terminal_ref']) == original_terminal
        terminal_events = [event for event in after if event['type'] == 'model.attempt.terminal']
        resumed_terminal = owner.store.get(terminal_events[-1]['data']['receipt_ref'])
        assert resumed_terminal['lease']['generation'] > original_lease['generation']
        with sqlite3.connect(owner.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
            assert connection.execute('SELECT state FROM effect WHERE operation_id=?',
                (capsule['attempts'][0]['attempt_id'],)).fetchone()[0] == 'UNKNOWN'
        frames = owner.records.read('v2_turn_frames', owner.product_id).payload
        suffix = ''.join(frame['text'] for frame in frames['frames'] if frame['sequence'] >= frames['text_from'])
        assert frames['text_prefix'] == owner.partial and suffix == owner.full[len(owner.partial):]
        assert owner.runtime.receipt_for(owner.identity).status == 'completed'
        assert owner.store.get_action('native-do-resume-key') is not None
    finally:
        owner.close()
