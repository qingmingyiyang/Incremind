"""原 registrar/builder 经真实 HTTP、SDK 消费七工具；完整工厂认证另验。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
import pytest

from backend.api.mcp_runtime import shutdown_ai_mcp_runtime
from backend.api.plugin_hands_runtime import shutdown_plugin_hands_runtime
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.research_sources import ReadPlanner, ReadRegistry
from backend.memory_app.source_egress import SourceEgressService, recognition_service
from backend.memory_app.source_graph import SourceGraph
from backend.memory_app.turn_installation import install_recognition_turn_capability
from backend.memory_app.v2.external_agent_settings import (
    external_agent_settings, replace_external_agent_settings,
)
from backend.memory_app.v2.external_context import DELIVERIES, USES
from backend.memory_app.v2.budget import text_tokens
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.workspace_confirmation import COLLECTION
from backend.recognition import RecognitionConflict, WorkScope
from backend.security.secrets import InMemorySecretStore
from backend.security.turn_frozen_authorization import FROZEN_TOOL_AUTHORIZATION_KIND
from core.ai_kernel.capability_manifest import manifest_from_payload
from core.ai_kernel.context_manifest import context_manifest_from_payload
from core.ai_kernel.frozen_authorization_facts import frozen_authorization_facts_from_payload
from core.document_engine import SQLiteDocumentRepository
from core.effect_log import EffectState
from core.plugin_host.hands_artifact import PluginHandsArtifactService
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.record_lineage import FACTS, HEAD_COLLECTIONS, WITNESS_COLLECTIONS, verify_lineage
from tests.memory_app.v2.test_external_registered_context import (
    no_factory_or_artifact_capture,
)
from tests.memory_app.v2.test_mcp_sdk_backend import loopback, TOOLS
from tests.memory_app.v2.test_workbench_ask import assemble


ROOT = Path(__file__).resolve().parents[3]
SOURCE = '礼物预算先确认实际需求，再选择常用物品。'
QUERY = '礼物预算'
SCOPE = WorkScope('local-user', 'alpha')
PLUGIN_COLLECTIONS = ('plugin_raw_packages', 'plugin_package_states',
    'plugin_skill_activations', 'plugin_declarative_tool_activations',
    'plugin_hands_artifact_operations', 'plugin_hands_activations', 'plugin_hook_activations')


@pytest.fixture
def public_consumers(tmp_path, monkeypatch, no_factory_or_artifact_capture):
    # 未被测的文件 artifact 分支遇到即失败；不替换实际服务或 Runtime。
    artifact_hits = []

    def forbidden_artifact(*args, **kwargs):
        artifact_hits.append(True)
        raise AssertionError('合成空 plugin 目录不得进入文件 artifact 分支')

    for method in ('resume', 'resolve', '_ensure_exact_tree'):
        monkeypatch.setattr(PluginHandsArtifactService, method, forbidden_artifact)
    native_calls = []

    def completion(**request):
        assert request['messages'][-1]['content'] == SOURCE
        assert all(isinstance(message['content'], str) for message in request['messages'])
        native_calls.append(request['model'])
        draft = {'title': '礼物预算材料', 'summary': SOURCE, 'topics': [QUERY],
            'facts': [{'text': SOURCE, 'evidence': {'quote': SOURCE}}],
            'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
        return {'choices': [{'message': {'content': json.dumps(draft, ensure_ascii=False)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 30}}

    records = SQLiteStructuredRecordStore(tmp_path / '.rebuild-data/structured-records.sqlite3')
    documents, service = SQLiteDocumentRepository(records), recognition_service(records)
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=completion)
    models.update('generation', {'base_url': 'https://provider.invalid/v1', 'model': 'material-writer',
        'api_key': 'synthetic-local-only', 'allow_remote': True, 'expected_revision': 0})
    current = external_agent_settings(records)
    replace_external_agent_settings(records,
        {key: value for key, value in current.items() if key != 'revision'}
        | {'allow_remote': True, 'include_profile': False}, expected_revision=current['revision'])
    app, domains = assemble(tmp_path, records, documents, service, models)
    # 原 memory_app.app 的既有 state 接点使用同一实际 owner；不调用该工厂。
    app.state.source_privacy_registry_wrapper = lambda registry: ReadRegistry(registry, records)
    app.state.source_privacy_planner_wrapper = lambda planner, turns, agents: ReadPlanner(
        planner, records, turns, agents)
    app.state.recognition_records = records
    app.state.recognition_service = service
    app.state.recognition_models = models
    app.state.recognition_documents = documents
    app.state.recognition_document_namespace = documents.namespace_id
    app.state.recognition_mutation_lock = RLock()
    app.state.recognition_runtime_root = tmp_path
    app.state.recognition_turn_installer = install_recognition_turn_capability
    app.state.workspace_domains = domains
    actual = SimpleNamespace(root=tmp_path, app=app, records=records, documents=documents,
        service=service, models=models, domains=domains, native_calls=native_calls,
        context=app.state.external_context)
    try:
        # 原公共业务入口调用原 get_or_build；不混入预建的 RegisteredContext 内核。
        actual.runtime = domains.query.answer_turns._runtime()
        actual.turns, actual.runner = app.state.ai_turn_store, app.state.ai_turn_runner
        actual.execute = lambda turn: actual.context.execute(turn,
            runtime=actual.runtime, runner=actual.runner)
        assert actual.context.runtime is actual.runtime and actual.context.turns is actual.turns
        assert actual.runtime._registry is app.state.runtime_capability_admission._registry
        assert domains.query.records is records and domains.query.models is models
        assert domains.query.documents is documents and domains.query.service is service
        assert actual.context.guard.records is records and actual.context.documents is documents
        assert getattr(app.state, 'capability_package_catalog', None) is None
        jobs = SQLiteStructuredRecordStore(tmp_path / '.rebuild-data/jobs.sqlite3')
        assert all(jobs.list(collection) == () for collection in PLUGIN_COLLECTIONS)

        staged = asyncio.run(domains.intake.add_text({'project_id': 'alpha', 'text': SOURCE}))
        ready = asyncio.run(domains.intake.process(staged['id'], {'project_id': 'alpha'}))
        assert staged['status'] == 'staged' and ready['status'] == 'ready'
        assert ready['draft']['facts'][0]['evidence'] == {'start': 0, 'end': len(SOURCE), 'quote': SOURCE}
        assert len(native_calls) == 1
        checkpoint = records.list('workspace_organize_steps')
        assert len(checkpoint) == 1 and not checkpoint[0].payload['rejected']
        actual.organize_turn = checkpoint[0].payload['turn_id']
        events = actual.turns.events_after(actual.organize_turn)
        assert actual.turns.get_request(actual.organize_turn)['desired_outcome'] == 'memory.organize'
        assert events[-1]['type'] == 'turn.completed'
        dispatched = [event for event in events if event['type'] == 'model.attempt.dispatched']
        terminal = [event for event in events if event['type'] == 'model.attempt.terminal']
        assert len(dispatched) == len(terminal) == 1
        dispatch_ref = dispatched[0]['data']['payload_ref']
        dispatch = actual.turns.get(dispatch_ref)
        receipt_ref = terminal[0]['data']['receipt_ref']
        receipt = actual.turns.get(receipt_ref)
        assert receipt['status'] == 'succeeded' and receipt['turn_id'] == actual.organize_turn
        assert receipt['attempt_id'] == dispatch['attempt_id']
        assert terminal[0]['data']['evidence_refs'] == [dispatch_ref]
        effect = actual.turns.effect_runner.log.get(dispatch['attempt_id'])
        assert effect.state is EffectState.SETTLED_OK and effect.intent_ref == dispatch_ref
        assert effect.turn_id == actual.organize_turn and effect.result_ref == receipt_ref

        # 本地人确认仅生成测试素材；外部 SDK 没有确认能力。
        confirmed = asyncio.run(domains.review.confirm(staged['id'], {'project_id': 'alpha',
            'expected_revision': ready['revision']}))
        operations = records.list(COLLECTION)
        assert confirmed['status'] == 'confirmed' and len(operations) == 1
        assert operations[0].payload['state'] == 'committed'
        assert operations[0].payload['workspace_item_id'] == staged['id']
        source = domains.query.source_store.read('sources', confirmed['source_id'])
        assert source == operations[0].payload['source_payload']
        assert source['metadata']['content_snapshot'] == SOURCE and source['content_hash'] is None
        extracted = asyncio.run(domains.review.recognition(staged['id'], {'project_id': 'alpha'}))
        document = documents.read(extracted['document_id'])
        experience = records.read('recognition_experiences', extracted['experience_id'])
        assert experience.payload['provenance'] == {'kind': 'workspace_confirmed_document',
            'actor': 'local-user', 'source_refs': [{'type': 'document', 'id': document['id'],
                'revision': document['revision']}], 'epistemic_status': 'unverified',
            'recorded_at': experience.payload['created_at']}
        candidate = records.read('recognition_candidates', extracted['candidate_id'])
        assert candidate.payload['state'] == 'pending'
        amended = service.edit_candidate(scope=SCOPE, candidate_id=candidate.object_id,
            expected_revision=candidate.revision, content=candidate.payload['content'],
            conditions=[QUERY], editor='local-human-test-reviewer')
        actual.method = service.publish(scope=SCOPE, candidate_id=amended.id,
            expected_revision=amended.revision, reviewer='local-human-test-reviewer')
        actual.material_item = staged['id']
        actual.material_document = document['id']
        graph = SourceEgressService(records).snapshot(SCOPE,
            [{'type': 'recognition', 'id': actual.method.id, 'revision': actual.method.revision}])
        SourceEgressService(records).validate_snapshot(SCOPE, graph)
        assert any(node['type'] == 'original_item' and node['id'] == staged['id'] for node in graph['nodes'])
        yield actual
        assert all(jobs.list(collection) == () for collection in PLUGIN_COLLECTIONS)
    finally:
        if hasattr(actual, 'runner'):
            assert actual.runner.shutdown(timeout_seconds=5) == ()
        shutdown_ai_mcp_runtime(app)
        shutdown_plugin_hands_runtime(app)
        manager = getattr(app.state, 'plugin_tool_registration_manager', None)
        if manager is not None:
            manager.close()
        assert not artifact_hits


def sdk_environment(actual, endpoint):
    # 显式 env 使用此白名单；SDK 另合并默认系统白名单（含 APPDATA/USERPROFILE），不继承任意 API 或设备密钥。
    allowed = ('SystemRoot', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP')
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(PYTHONPATH=str(ROOT / 'src'), CHRIPTMAS_APP_ROOT=str(actual.root),
        CHRIPTMAS_MCP_BACKEND_URL=endpoint)
    assert set(environment) <= {*allowed, 'PYTHONPATH', 'CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_MCP_BACKEND_URL'}
    return environment


def assert_public_completed(actual, turn, frozen, delivered):
    # 原 builder 的 Hook 使用冻结 Boundary 资格；这里核原持久事实及其真实关联。
    turns, api = actual.turns, actual.context
    events = turns.events_after(turn)
    assert turns.get_request(turn) == frozen and events[-1]['type'] == 'turn.completed'
    assert not any(event['type'].startswith('model.') for event in events)
    kinds = ('tool.requested', 'tool.intent.recorded', 'tool.dispatch.claimed',
        'tool.outcome.recorded', 'tool.completed')
    matched = {}
    for kind in kinds:
        rows = [event for event in events if event['type'] == kind]
        assert len(rows) == 1
        matched[kind] = rows[0]
    assert [matched[kind]['sequence'] for kind in kinds] == sorted(matched[kind]['sequence'] for kind in kinds)
    assert matched['tool.requested']['data']['payload_ref'] is None
    invocation = matched['tool.intent.recorded']['correlation']['tool_call_id']
    intent_ref = matched['tool.intent.recorded']['data']['payload_ref']
    intent = turns.get(intent_ref)
    assert intent['arguments'] == frozen['capability_request']['arguments']
    assert intent['capability_id'] == 'external.context.execute' and intent['turn_id'] == turn
    assert matched['tool.dispatch.claimed']['data']['payload_ref'] == intent_ref
    effect = turns.effect_runner.log.get(invocation)
    outcome_ref = matched['tool.outcome.recorded']['data']['payload_ref']
    assert effect.state is EffectState.SETTLED_OK and effect.turn_id == turn
    assert effect.intent_ref == intent_ref and effect.result_ref == outcome_ref
    outcome = turns.get(outcome_ref)
    assert outcome['invocation_id'] == invocation and outcome['attempt'] == effect.attempt == 1
    immutable_ref, archive = api._archive(turn)
    assert archive['handoff'] == delivered and archive['request'] == frozen
    assert outcome['payload_ref'] == immutable_ref
    receipt = actual.records.read(DELIVERIES, turn)
    assert receipt.revision == 1 and receipt.payload['immutable_ref'] == immutable_ref
    assert receipt.payload['outcome_ref'] == outcome_ref and receipt.payload['owner_id'] == 'local-user'
    assert api._completed(turn) == (immutable_ref, archive, outcome_ref)
    contexts = [event for event in events if event['type'] == 'context.resolved']
    assert len(contexts) == 1
    context_ref = contexts[0]['data']['payload_ref']
    context = context_manifest_from_payload(turns.get(context_ref))
    manifest = manifest_from_payload(turns.get(context.capability_manifest_ref))
    assert manifest.resolver_id == 'project-profile-v1' and manifest.capability_ids == ('external.context.execute',)
    authorization_ref, payload = turns.get_immutable_payload(turn, FROZEN_TOOL_AUTHORIZATION_KIND)
    authorization = frozen_authorization_facts_from_payload(payload)
    assert authorization.turn_id == turn and authorization.project_id == frozen['scope']['project_id']
    assert authorization.context_manifest_ref == context_ref
    assert authorization.capability_manifest_ref == context.capability_manifest_ref
    assert authorization.capability_profile_id == context.project_profile_id == manifest.profile_id
    assert authorization.capability_profile_revision == context.project_profile_revision == manifest.profile_revision
    assert authorization.boundary_profile_id == context.boundary_profile_id == manifest.boundary_profile_id
    assert authorization.boundary_profile_revision == context.boundary_profile_revision == manifest.boundary_profile_revision
    assert intent['authorization_facts_ref'] == authorization_ref
    assert intent['authorization_facts_revision'] == authorization.revision
    assert len(authorization.capabilities) == 1
    admitted = authorization.capabilities[0]
    assert admitted.capability_id == intent['capability_id']
    assert admitted.contract_ref == 'crp://tool-contracts/external.context.execute'
    assert admitted.destination == intent['tool_contract']['destination'] == 'local'
    assert admitted.effect == intent['tool_contract']['effect'] == 'read'
    assert admitted.operation_semantics == intent['tool_contract']['operation_semantics'] == 'read_only'
    assert admitted.requires_approval is intent['requires_approval'] is False
    hooks = [(event, turns.get(event['data']['receipt_ref'])) for event in events if event['type'] == 'hook.invoked']
    hooks = [(event, receipt) for event, receipt in hooks if receipt['event'] == 'PreToolUse']
    assert len(hooks) == 1
    hook_event, hook = hooks[0]
    assert hook_event['correlation']['tool_call_id'] == invocation
    assert hook_event['sequence'] < matched['tool.requested']['sequence']
    assert hook['turn_id'] == turn and hook['project_id'] == authorization.project_id
    assert hook['normalized_outcome']['dispatch_blocked'] is False
    assert hook['normalized_outcome']['input_rewrite_applied'] is False and hook['handler_runs'] == []
    policy_ref, policy = turns.get_immutable_payload(turn, 'codex-hook-policy-snapshot')
    assert hook['policy_snapshot_ref'] == policy_ref
    assert hook['policy_snapshot_revision'] == policy['revision'] == 'builtin-empty-v1'
    assert policy['handlers'] == []
    assert actual.records.read('v2_external_agent_reservations', turn).revision == 1
    assert delivered['tokens'] == text_tokens(delivered['text']) <= delivered['budget']
    before = tuple(events)
    assert actual.execute(turn) == delivered and turns.events_after(turn) == before
    assert actual.records.read(DELIVERIES, turn).revision == 1


@pytest.mark.parametrize(('name', 'client'), [('Claude Code', 'claude'), ('Codex', 'codex')])
def test_original_registered_http_sdk_seven_tools(public_consumers, name, client):
    actual, outputs = public_consumers, {}
    secret = 'sk-' + 'A' * 24
    with loopback(actual.app) as endpoint:
        async def operation():
            parameters = StdioServerParameters(command=sys.executable,
                args=['-m', 'backend.memory_app.mcp'], env=sdk_environment(actual, endpoint), cwd=str(ROOT))
            with (actual.root / 'registered-sdk-stderr.log').open('w', encoding='utf-8') as errors:
                async with stdio_client(parameters, errlog=errors) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=150),
                        client_info=types.Implementation(name=name, version='integration-1')) as session:
                        initialized = await session.initialize()
                        assert initialized.serverInfo.name == 'chriptmas-memory'
                        listed = await session.list_tools()
                        assert [tool.name for tool in listed.tools] == TOOLS
                        arguments = {'projects': {}, 'recall': {'query': QUERY, 'project': 'alpha'},
                            'methods': {'situation': QUERY, 'project': 'alpha'},
                            'remember': {'text': 'SDK 合成待核对原件\n' + secret, 'project': 'alpha'}}
                        for index, tool in enumerate(listed.tools):
                            assert tool.annotations.readOnlyHint is (index < 4)
                            assert 'client' not in tool.inputSchema['properties']
                            Draft202012Validator.check_schema(tool.inputSchema)
                            Draft202012Validator.check_schema(tool.outputSchema)
                            if tool.name == 'read':
                                parent = outputs['recall']
                                entry = next(row for row in parent['result']['entries']
                                    if row['object_id'] == actual.method.id)
                                arguments['read'] = {'id': {'turn_id': parent['turn_id'], 'id': entry['id']},
                                    'window': {'start': 0, 'end': 8}}
                            elif tool.name == 'propose_insight':
                                child = outputs['read']
                                entry = next(row for row in child['result']['entries']
                                    if row['object_id'] == actual.material_item)
                                arguments[tool.name] = {'text': 'SDK 基于交付原件提出的合成认识',
                                    'conditions': [QUERY], 'project': 'alpha', 'evidence_ids': [
                                        {'turn_id': child['turn_id'], 'id': entry['id']}]}
                            elif tool.name == 'report_use':
                                child = outputs['read']
                                arguments[tool.name] = {'turn_id': child['turn_id'], 'ids': [
                                    row['id'] for row in child['result']['entries']
                                    if row['object_id'] == actual.material_item]}
                            result = await session.call_tool(tool.name, arguments[tool.name])
                            assert result.isError is False, (tool.name, result)
                            output = result.structuredContent
                            Draft202012Validator(tool.outputSchema).validate(output)
                            assert json.loads(result.content[0].text) == output
                            outputs[tool.name] = output
        asyncio.run(operation())

    assert set(outputs) == set(TOOLS)
    assert any(row['id'] == 'alpha' for row in outputs['projects']['result']['projects'])
    for tool in ('recall', 'methods'):
        entry = next(row for row in outputs[tool]['result']['entries'] if row['object_id'] == actual.method.id)
        assert entry['layer'] == 'L3' and SOURCE in entry['excerpt']
        proof, _ = actual.context.delivered_proof(outputs[tool]['turn_id'], entry['id'], client=client)
        SourceEgressService(actual.records).validate_snapshot(SCOPE, proof['snapshot'])
        assert any(node['type'] == 'original_item' and node['id'] == actual.material_item
            for node in proof['snapshot']['nodes'])
    parent, child = outputs['recall'], outputs['read']
    entry = next(row for row in child['result']['entries'] if row['object_id'] == actual.material_item)
    assert entry['layer'] == 'L0' and entry['excerpt'] == SOURCE[:8]
    assert child['turn_id'] != parent['turn_id']
    _, archive = actual.context._archive(child['turn_id'])
    parent_ref, _, parent_outcome = actual.context._completed(parent['turn_id'])
    assert archive['origin']['turn_id'] == parent['turn_id']
    assert archive['origin']['immutable_ref'] == parent_ref and archive['origin']['outcome_ref'] == parent_outcome
    assert archive['mapping'][entry['id']]['material']['type'] == 'original_item'
    for tool in ('projects', 'recall', 'methods', 'read'):
        turn, delivered = outputs[tool]['turn_id'], outputs[tool]['result']
        frozen = actual.turns.get_request(turn)
        assert frozen['capability_request']['arguments']['client'] == client
        assert_public_completed(actual, turn, frozen, delivered)
    remembered, proposed = outputs['remember'], outputs['propose_insight']
    assert remembered['turn_id'] is proposed['turn_id'] is None
    for tool in ('remember', 'propose_insight'):
        result = outputs[tool]['result']
        receipt = actual.records.read('v2_external_agent_intakes', result['receipt_id'])
        assert receipt.payload['client'] == result['client'] == client
        assert receipt.payload['project_id'] == result['project_id'] == 'alpha' and receipt.payload['tool'] == tool
    original = actual.records.read('workspace_items', remembered['result']['item_id'])
    assert original.payload['status'] == remembered['result']['state'] == 'staged'
    assert original.revision == remembered['result']['revision'] == 1
    assert remembered['result']['verified'] is False
    assert original.payload['source_text'] == 'SDK 合成待核对原件\n[REDACTED_SECRET]'
    assert original.payload['draft'] is None and original.payload['document_id'] is None
    assert secret not in json.dumps(outputs, ensure_ascii=False)
    candidate = actual.records.read('recognition_candidates', proposed['result']['candidate_id'])
    assert candidate.payload['state'] == proposed['result']['state'] == 'pending'
    assert candidate.revision == proposed['result']['revision'] == 1
    assert candidate.payload['conditions'] == [QUERY]
    experience = actual.records.read('recognition_experiences', candidate.payload['source_experience_ids'][0])
    assert experience.payload['provenance']['actor'] == client
    dependency = actual.records.read('v2_external_input_dependencies', experience.object_id)
    assert dependency.payload['client'] == client and dependency.payload['references'][0]['turn_id'] == child['turn_id']
    assert dependency.payload['references'][0]['id'] == entry['id']
    assert len(actual.records.list('recognitions')) == 1
    assert outputs['report_use'] == {'turn_id': child['turn_id'],
        'result': {'turn_id': child['turn_id'], 'ids': [entry['id']]}}
    citation = actual.records.read(USES, child['turn_id'])
    assert citation.revision == 1 and citation.payload['ids'] == [entry['id']]
    assert citation.payload['objects'][0]['id'] == actual.material_item
    assert len(actual.records.list(DELIVERIES)) == 4
    assert len(actual.records.list('v2_external_agent_reservations')) == 4
    assert len(actual.records.list('v2_external_agent_write_reservations')) == 2
    with sqlite3.connect((actual.root / '.rebuild-data/ai-turns.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        assert connection.execute('SELECT status,terminal_status FROM ai_model_attempt_reservations').fetchall() == [
            ('terminal', 'succeeded')]
        assert sum(json.loads(row[0])['type'] == 'model.attempt.dispatched' for row in
            connection.execute('SELECT event_json FROM ai_turn_events')) == 1
    assert len(actual.native_calls) == 1


@asynccontextmanager
async def registered_sdk_session(actual, endpoint, *, name='Codex', suffix='negative'):
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'backend.memory_app.mcp'], env=sdk_environment(actual, endpoint), cwd=str(ROOT))
    with (actual.root / f'registered-sdk-{suffix}-stderr.log').open('w', encoding='utf-8') as errors:
        async with stdio_client(parameters, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=150),
                client_info=types.Implementation(name=name, version='integration-1')) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == 'chriptmas-memory'
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == TOOLS
                yield session


async def sdk_success(session, tool, arguments):
    result = await session.call_tool(tool, arguments)
    assert result.isError is False, (tool, result)
    assert json.loads(result.content[0].text) == result.structuredContent
    return result.structuredContent


async def sdk_reject(session, tool, arguments, code):
    result = await session.call_tool(tool, arguments)
    assert result.isError is True and result.structuredContent is None, (tool, result)
    assert len(result.content) == 1 and result.content[0].type == 'text'
    assert result.content[0].text == code, (tool, result.content[0].text, code)


async def sdk_delivered_original(actual, session):
    parent = await sdk_success(session, 'recall', {'query': QUERY, 'project': 'alpha'})
    entry = next(row for row in parent['result']['entries'] if row['object_id'] == actual.method.id)
    child = await sdk_success(session, 'read', {'id': {'turn_id': parent['turn_id'], 'id': entry['id']}})
    original = next(row for row in child['result']['entries'] if row['object_id'] == actual.material_item)
    assert original['layer'] == 'L0' and original['excerpt'] == SOURCE
    for output in (parent, child):
        assert_public_completed(actual, output['turn_id'], actual.turns.get_request(output['turn_id']), output['result'])
    return child, original


def sdk_arguments(child, original):
    identity = {'turn_id': child['turn_id'], 'id': original['id']}
    return {'projects': {}, 'recall': {'query': QUERY, 'project': 'alpha'},
        'methods': {'situation': QUERY, 'project': 'alpha'}, 'read': {'id': identity},
        'remember': {'text': 'SDK 拒绝场景的合成原件', 'project': 'alpha'},
        'propose_insight': {'text': 'SDK 拒绝场景的合成认识', 'project': 'alpha', 'evidence_ids': [identity]},
        'report_use': {'turn_id': child['turn_id'], 'ids': [original['id']]}}


def configure_external(actual, **changes):
    current = external_agent_settings(actual.records)
    return replace_external_agent_settings(actual.records,
        {key: value for key, value in current.items() if key != 'revision'} | changes,
        expected_revision=current['revision'])


def consumer_facts(actual, *, omit_citations=False):
    # 读真实 SQLite 业务事实而非计算文件摘要；拒绝不应留下新资料、证明、额度或内核事实。
    with sqlite3.connect(actual.records.database_path.as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        rows = connection.execute('SELECT collection,object_id,payload_json,revision '
            'FROM crp_structured_records ORDER BY collection,object_id').fetchall()
        records = tuple(row for row in rows if not omit_citations or row[0] != USES)
    with sqlite3.connect((actual.root / '.rebuild-data/ai-turns.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        tables = ('ai_turns', 'ai_turn_events', 'ai_turn_payloads', 'ai_turn_immutable_payloads',
            'ai_model_attempt_reservations', 'effect', 'effect_receipt')
        kernel = tuple((table, tuple(connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()))
            for table in tables)
    return records, kernel


@pytest.mark.parametrize('control', ['off', 'client-disabled'])
def test_public_sdk_off_and_disabled_preserve_old_usage_without_new_delivery(public_consumers, control):
    actual = public_consumers
    with loopback(actual.app) as endpoint:
        async def operation():
            async with registered_sdk_session(actual, endpoint, suffix=control) as session:
                child, original = await sdk_delivered_original(actual, session)
                arguments = sdk_arguments(child, original)
                configure_external(actual, **({'allow_remote': False} if control == 'off'
                    else {'clients': {'claude': True, 'codex': False}}))
                before = consumer_facts(actual)
                guard_error = 'external_agent_disabled' if control == 'off' else 'external_agent_client_disabled'
                for tool in TOOLS[:-1]:
                    code = 'external_context_unavailable' if tool in ('recall', 'methods') else guard_error
                    await sdk_reject(session, tool, arguments[tool], code)
                    assert consumer_facts(actual) == before
                # 原 T16.1 的已交付使用回报在 OFF 后仍有效，不属于新内容外发或新配额调用。
                before_usage = consumer_facts(actual, omit_citations=True)
                used = await sdk_success(session, 'report_use', arguments['report_use'])
                assert used == {'turn_id': child['turn_id'], 'result': arguments['report_use']}
                citation = actual.records.read(USES, child['turn_id'])
                assert citation.revision == 1 and citation.payload['ids'] == [original['id']]
                assert citation.payload['objects'][0]['id'] == actual.material_item
                assert consumer_facts(actual, omit_citations=True) == before_usage
                settled = consumer_facts(actual)
                assert await sdk_success(session, 'report_use', arguments['report_use']) == used
                assert consumer_facts(actual) == settled
        asyncio.run(operation())
    assert len(actual.native_calls) == 1


def test_public_sdk_private_project_excludes_catalog_and_rejects_new_material(public_consumers):
    actual = public_consumers
    with loopback(actual.app) as endpoint:
        async def operation():
            async with registered_sdk_session(actual, endpoint, suffix='private-project') as session:
                child, original = await sdk_delivered_original(actual, session)
                arguments = sdk_arguments(child, original)
                set_private_project(actual.records, 'alpha', True, 0)
                catalog = await sdk_success(session, 'projects', {})
                assert 'alpha' not in {row['id'] for row in catalog['result']['projects']}
                assert SOURCE not in json.dumps(catalog, ensure_ascii=False)
                assert_public_completed(actual, catalog['turn_id'], actual.turns.get_request(catalog['turn_id']), catalog['result'])
                before = consumer_facts(actual)
                for tool in ('recall', 'methods', 'read', 'remember', 'propose_insight'):
                    code = 'external_context_unavailable' if tool in ('recall', 'methods') else 'external_agent_private'
                    await sdk_reject(session, tool, arguments[tool], code)
                    assert consumer_facts(actual) == before
                # 私密变更递增原来源快照 epoch，既有交付的回报仍须通过来源事实复验。
                await sdk_reject(session, 'report_use', arguments['report_use'], 'external_context_unavailable')
                assert consumer_facts(actual) == before
        asyncio.run(operation())
    assert len(actual.native_calls) == 1


@pytest.mark.parametrize('change', ['source-private', 'source-revision'])
def test_public_sdk_source_privacy_keeps_local_evidence_and_revision_drift_rejects_it(public_consumers, change):
    actual = public_consumers
    with loopback(actual.app) as endpoint:
        async def operation():
            async with registered_sdk_session(actual, endpoint, suffix=change) as session:
                child, original = await sdk_delivered_original(actual, session)
                arguments = sdk_arguments(child, original)
                prior = actual.records.read('workspace_items', actual.material_item)
                if change == 'source-private':
                    SourceEgressService(actual.records).set_policy(SCOPE, 'original_item', actual.material_item,
                        prior.revision, 0, [])
                    for tool in ('projects', 'recall', 'methods'):
                        output = await sdk_success(session, tool, arguments[tool])
                        assert SOURCE not in json.dumps(output, ensure_ascii=False)
                        if tool != 'projects':
                            assert output['result']['entries'] == []
                        assert_public_completed(actual, output['turn_id'], actual.turns.get_request(output['turn_id']), output['result'])
                else:
                    changed = actual.domains.items.update(actual.material_item, 'alpha', {'confirmed'},
                        source_text=SOURCE + '这是来源的新修订。')
                    assert changed['revision'] == prior.revision + 1
                before = consumer_facts(actual)
                await sdk_reject(session, 'read', arguments['read'], 'external_agent_binding_invalid')
                assert consumer_facts(actual) == before
                await sdk_reject(session, 'report_use', arguments['report_use'], 'external_context_unavailable')
                assert consumer_facts(actual) == before
                if change == 'source-revision':
                    await sdk_reject(session, 'propose_insight', arguments['propose_insight'], 'external_context_unavailable')
                    assert consumer_facts(actual) == before
                else:
                    # 原 SQL 资格保留本地证据；当前私密沿新经历继承，不能成为外发授权。
                    day = actual.records.list('v2_external_agent_reservations')[0].payload['day']
                    quota_collection = 'v2_external_agent_quota_' + day
                    quota = actual.records.read(quota_collection, 'local-user')
                    proposed = await sdk_success(session, 'propose_insight', arguments['propose_insight'])
                    result = proposed['result']
                    assert proposed['turn_id'] is None and result['state'] == 'pending'
                    candidate = actual.records.read('recognition_candidates', result['candidate_id'])
                    assert candidate.revision == result['revision'] == 1 and candidate.payload['state'] == 'pending'
                    assert candidate.payload['reviewed_at'] is None
                    experience = actual.records.read('recognition_experiences', candidate.payload['source_experience_ids'][0])
                    assert experience.revision == 1 and experience.payload['provenance']['actor'] == 'codex'
                    dependency = actual.records.read('v2_external_input_dependencies', experience.object_id)
                    assert dependency.revision == 1 and dependency.payload['client'] == 'codex'
                    assert dependency.payload['scope'] == {'user_id': 'local-user', 'project_id': 'alpha'}
                    reference = dependency.payload['references'][0]
                    assert len(dependency.payload['references']) == 1
                    assert (reference['turn_id'], reference['id']) == (child['turn_id'], original['id'])
                    receipt = actual.records.read('v2_external_agent_intakes', result['receipt_id'])
                    assert receipt.revision == 1 and receipt.payload['object_id'] == candidate.object_id
                    assert receipt.payload['object_revision'] == 1 and receipt.payload['tool'] == 'propose_insight'
                    assert receipt.payload['owner_id'] == 'local-user' and receipt.payload['client'] == 'codex'
                    reservation = actual.records.read('v2_external_agent_write_reservations', result['receipt_id'])
                    assert reservation.revision == 1 and reservation.payload['receipt_id'] == receipt.object_id
                    assert reservation.payload['day'] == day and reservation.payload['owner_id'] == 'local-user'
                    spent = actual.records.read(quota_collection, 'local-user')
                    assert spent.revision == quota.revision + 1
                    assert spent.payload == {**quota.payload, 'count': quota.payload['count'] + 1}
                    graph = SourceEgressService(actual.records).snapshot(SCOPE,
                        [{'type': 'experience', 'id': experience.object_id, 'revision': experience.revision}])
                    expanded = SourceGraph()
                    expanded.snapshot(graph)
                    private = next(node for node in expanded.result()['nodes']
                        if node['type'] == 'original_item' and node['id'] == actual.material_item)
                    assert private['policy_revision'] == 1 and private['effective_purposes'] == []
                    assert next(node for node in graph['nodes'] if node['type'] == 'experience'
                        and node['id'] == experience.object_id)['effective_purposes'] == []
                    with pytest.raises(RecognitionConflict):
                        SourceEgressService(actual.records).require(graph, 'generation')
                    after = consumer_facts(actual)
                    assert after[1] == before[1]
                    created = {('recognition_candidates', candidate.object_id),
                        ('recognition_experiences', experience.object_id),
                        ('v2_external_input_dependencies', dependency.object_id),
                        ('v2_external_agent_intakes', receipt.object_id),
                        ('v2_external_agent_write_reservations', reservation.object_id)}
                    # 原 SQLite INSERT 自动写原件身份事实，精确核本次两记录的六个 sidecar。
                    for collection, identity in (('recognition_experiences', experience.object_id),
                            ('v2_external_input_dependencies', dependency.object_id)):
                        head = actual.records.read(HEAD_COLLECTIONS[collection], identity)
                        witness = actual.records.read(WITNESS_COLLECTIONS[collection], identity)
                        assert head.revision == witness.revision == 1
                        assert witness.payload == {'schema_version': 1, 'first_fact_id': head.payload['fact_id']}
                        lineage = {'collection': collection, 'object_id': identity, 'fact_id': head.payload['fact_id']}
                        verify_lineage(actual.records, lineage)
                        fact = actual.records.read(FACTS, lineage['fact_id'])
                        assert fact.revision == 1 and fact.payload == {'schema_version': 1, 'collection': collection,
                            'object_id': identity, 'origin': 'created', 'observed_revision': 1}
                        if collection == 'recognition_experiences':
                            assert dependency.payload['ownexperience_identity'] == lineage
                        created.update({(HEAD_COLLECTIONS[collection], identity),
                            (WITNESS_COLLECTIONS[collection], identity), (FACTS, fact.object_id)})
                    old_rows = {(row[0], row[1]): row for row in before[0]}
                    new_rows = {(row[0], row[1]): row for row in after[0]}
                    assert set(new_rows) - set(old_rows) == created
                    for key, row in old_rows.items():
                        if key != (quota_collection, 'local-user'):
                            assert new_rows[key] == row
        asyncio.run(operation())
    assert len(actual.native_calls) == 1


def test_public_sdk_real_quota_exhaustion_settles_reads_and_rolls_back_writes(public_consumers):
    actual = public_consumers
    configure_external(actual, daily_limit=2)
    with loopback(actual.app) as endpoint:
        async def operation():
            async with registered_sdk_session(actual, endpoint, suffix='quota') as session:
                child, original = await sdk_delivered_original(actual, session)
                arguments = sdk_arguments(child, original)
                reservations = actual.records.list('v2_external_agent_reservations')
                assert len(reservations) == 2
                day = reservations[0].payload['day']
                quota_collection = 'v2_external_agent_quota_' + day
                quota = actual.records.read(quota_collection, 'local-user')
                assert quota.payload['count'] == 2
                deliveries = actual.records.list(DELIVERIES)
                for tool in ('projects', 'recall', 'methods', 'read'):
                    old_ids = {row[0] for row in consumer_facts(actual)[1][0][1]}
                    await sdk_reject(session, tool, arguments[tool], 'external_context_not_completed')
                    new_ids = {row[0] for row in consumer_facts(actual)[1][0][1]} - old_ids
                    assert len(new_ids) == 1
                    turn = new_ids.pop()
                    assert actual.turns.get_request(turn)['capability_request']['arguments']['tool'] == tool
                    events = actual.turns.events_after(turn)
                    assert events[-1]['type'] == 'turn.failed'
                    # 原失败 Effect 以 error_ref 结算；完整失败 outcome 另由原内核事实持久化。
                    kinds = ('tool.requested', 'tool.intent.recorded', 'tool.dispatch.claimed',
                        'tool.outcome.recorded', 'tool.failed')
                    matched = {}
                    for kind in kinds:
                        rows = [event for event in events if event['type'] == kind]
                        assert len(rows) == 1
                        matched[kind] = rows[0]
                    assert [matched[kind]['sequence'] for kind in kinds] == sorted(
                        matched[kind]['sequence'] for kind in kinds)
                    invocation = matched['tool.intent.recorded']['correlation']['tool_call_id']
                    assert all(matched[kind]['correlation']['tool_call_id'] == invocation for kind in kinds)
                    intent_ref = matched['tool.intent.recorded']['data']['payload_ref']
                    intent = actual.turns.get(intent_ref)
                    assert intent['capability_id'] == 'external.context.execute' and intent['turn_id'] == turn
                    assert matched['tool.dispatch.claimed']['data']['payload_ref'] == intent_ref
                    effect = actual.turns.effect_runner.log.get(invocation)
                    assert effect.state is EffectState.SETTLED_ERR and effect.turn_id == turn
                    assert effect.attempt == 1 and effect.intent_ref == intent_ref
                    assert effect.result_ref is None and effect.error_ref == 'ai.tool_failed'
                    outcome = actual.turns.get(matched['tool.outcome.recorded']['data']['payload_ref'])
                    assert outcome == {'schema_version': '1.0.0', 'invocation_id': invocation,
                        'turn_id': turn, 'capability_id': 'external.context.execute', 'attempt': 1,
                        'status': 'failed', 'effect_certainty': 'confirmed_none', 'payload_ref': None,
                        'receipt_ref': None, 'evidence_refs': [], 'error_code': 'ai.tool_failed', 'retryable': False}
                    assert all(matched[kind]['data']['error_code'] == 'ai.tool_failed'
                        for kind in ('tool.outcome.recorded', 'tool.failed'))
                    assert events[-1]['data']['error_code'] == 'ai.tool_failed'
                    assert not any(event['type'].startswith('model.') or event['type'] == 'tool.completed'
                        for event in events)
                    assert actual.records.read(DELIVERIES, turn) is None
                    with pytest.raises(ValueError, match='external_context_not_completed'):
                        actual.context.delivered_proof(turn, original['id'], client='codex')
                    assert actual.records.read(quota_collection, 'local-user') == quota
                    assert actual.records.list('v2_external_agent_reservations') == reservations
                    assert actual.records.list(DELIVERIES) == deliveries
                before = consumer_facts(actual)
                for tool in ('remember', 'propose_insight'):
                    await sdk_reject(session, tool, arguments[tool], 'external_agent_quota_exhausted')
                    assert consumer_facts(actual) == before
                before_usage = consumer_facts(actual, omit_citations=True)
                await sdk_success(session, 'report_use', arguments['report_use'])
                assert consumer_facts(actual, omit_citations=True) == before_usage
                assert actual.records.read(quota_collection, 'local-user') == quota
        asyncio.run(operation())
    assert len(actual.native_calls) == 1


def test_public_sdk_rejects_foreign_client_missing_number_scope_and_unfinished_parent(public_consumers):
    actual = public_consumers
    with loopback(actual.app) as endpoint:
        async def operation():
            async with registered_sdk_session(actual, endpoint, suffix='proof-codex') as session:
                child, original = await sdk_delivered_original(actual, session)
                arguments = sdk_arguments(child, original)
                before = consumer_facts(actual)
                mapping = actual.context._archive(child['turn_id'])[1]['mapping']
                # 原完整下钻可同时交出原件及原始来源；从全部实际编号之外构造无资格编号。
                unknown = 'M' + str(max(int(identity[1:]) for identity in mapping if identity.startswith('M')) + 1)
                assert unknown not in mapping
                for tool, invalid, code in (
                    ('read', {'id': {'turn_id': child['turn_id'], 'id': unknown}}, 'external_context_citations_invalid'),
                    ('report_use', {'turn_id': child['turn_id'], 'ids': [unknown]}, 'external_context_citations_invalid'),
                    ('propose_insight', {**arguments['propose_insight'], 'project': 'beta'}, 'external_context_unavailable')):
                    await sdk_reject(session, tool, invalid, code)
                    assert consumer_facts(actual) == before
                pending = 'turn-pending-' + uuid4().hex
                request = {'client': 'codex', 'tool': 'recall', 'query': QUERY,
                    'scope': {'user_id': 'local-user', 'project_id': 'alpha'}, 'budget': 3000}
                actual.context.prepare_recall(pending, request, session_id='session-' + pending,
                    operation_id='op-' + pending, idempotency_key=pending,
                    created_at=actual.context.now().isoformat())
                pending_entry = next(row for row in actual.context._archive(pending)[1]['handoff']['entries']
                    if row['object_id'] == actual.method.id)
                assert actual.turns.events_after(pending)[-1]['type'] == 'turn.accepted'
                assert actual.records.read(DELIVERIES, pending) is None
                before_pending = consumer_facts(actual)
                identity = {'turn_id': pending, 'id': pending_entry['id']}
                for tool, invalid, code in (
                    ('read', {'id': identity}, 'external_context_not_completed'),
                    ('report_use', {'turn_id': pending, 'ids': [pending_entry['id']]}, 'external_context_not_completed'),
                    ('propose_insight', {**arguments['propose_insight'], 'evidence_ids': [identity]}, 'external_context_unavailable')):
                    await sdk_reject(session, tool, invalid, code)
                    assert consumer_facts(actual) == before_pending
            async with registered_sdk_session(actual, endpoint, name='Claude Code', suffix='proof-claude') as session:
                before = consumer_facts(actual)
                for tool in ('read', 'propose_insight', 'report_use'):
                    code = 'external_context_unavailable' if tool == 'propose_insight' else 'external_context_citations_invalid'
                    await sdk_reject(session, tool, arguments[tool], code)
                    assert consumer_facts(actual) == before
        asyncio.run(operation())
    assert len(actual.native_calls) == 1


def test_public_sdk_closed_original_backend_returns_not_started_for_all_seven(public_consumers):
    actual = public_consumers
    with loopback(actual.app) as endpoint:
        async def prepare():
            async with registered_sdk_session(actual, endpoint, suffix='before-close') as session:
                return await sdk_delivered_original(actual, session)
        child, original = asyncio.run(prepare())
    # 原 server 和 socket 已真实关闭；SDK 仍连同一 numeric loopback，未替换 HTTP transport。
    before = consumer_facts(actual)

    async def unavailable():
        async with registered_sdk_session(actual, endpoint, suffix='closed-backend') as session:
            for tool, arguments in sdk_arguments(child, original).items():
                await sdk_reject(session, tool, arguments, '第二大脑未启动')
                assert consumer_facts(actual) == before

    asyncio.run(unavailable())
    assert len(actual.native_calls) == 1


def test_public_sdk_original_content_budgets_and_exact_schemas(public_consumers, monkeypatch):
    actual = public_consumers
    label = '长材料预算'
    # 模型引用的开头在原件中只出现一次，符合原整理 owner 的唯一引用要求。
    long_source = '唯一原件开头。' + (label + '的合成原件段落。') * 400

    def long_completion(**request):
        # 唯一替身仍是原 completion 接口；长原件由实际 Intake/Organize/Confirmation 生成。
        body = request['messages'][-1]['content']
        assert body == long_source
        actual.native_calls.append(request['model'])
        quote = body[:16]
        draft = {'title': label, 'summary': label + '合成摘要', 'topics': [label],
            'facts': [{'text': quote, 'evidence': {'quote': quote}}],
            'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
        return {'choices': [{'message': {'content': json.dumps(draft, ensure_ascii=False)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 30}}

    monkeypatch.setattr(actual.models, '_completion_fn', long_completion)
    prior_steps = {row.object_id for row in actual.records.list('workspace_organize_steps')}
    staged = asyncio.run(actual.domains.intake.add_text({'project_id': 'alpha', 'text': long_source}))
    ready = asyncio.run(actual.domains.intake.process(staged['id'], {'project_id': 'alpha'}))
    assert ready['status'] == 'ready' and ready['draft']['facts'][0]['evidence'] == {
        'start': 0, 'end': 16, 'quote': long_source[:16]}
    steps = [row for row in actual.records.list('workspace_organize_steps') if row.object_id not in prior_steps]
    assert len(steps) == 1 and not steps[0].payload['rejected']
    organize = steps[0].payload['turn_id']
    events = actual.turns.events_after(organize)
    assert events[-1]['type'] == 'turn.completed'
    dispatched = [event for event in events if event['type'] == 'model.attempt.dispatched']
    terminal = [event for event in events if event['type'] == 'model.attempt.terminal']
    assert len(dispatched) == len(terminal) == 1
    dispatch_ref, receipt_ref = dispatched[0]['data']['payload_ref'], terminal[0]['data']['receipt_ref']
    dispatch, receipt = actual.turns.get(dispatch_ref), actual.turns.get(receipt_ref)
    assert receipt['status'] == 'succeeded' and receipt['attempt_id'] == dispatch['attempt_id']
    effect = actual.turns.effect_runner.log.get(dispatch['attempt_id'])
    assert effect.state is EffectState.SETTLED_OK and effect.turn_id == organize
    assert effect.intent_ref == dispatch_ref and effect.result_ref == receipt_ref
    confirmed = asyncio.run(actual.domains.review.confirm(staged['id'], {'project_id': 'alpha',
        'expected_revision': ready['revision']}))
    operation = next(row for row in actual.records.list(COLLECTION) if row.payload['workspace_item_id'] == staged['id'])
    assert operation.payload['state'] == 'committed'
    source = actual.domains.query.source_store.read('sources', confirmed['source_id'])
    assert source == operation.payload['source_payload']
    assert source['metadata']['content_snapshot'] == long_source and source['content_hash'] is None
    extracted = asyncio.run(actual.domains.review.recognition(staged['id'], {'project_id': 'alpha'}))
    candidate = actual.records.read('recognition_candidates', extracted['candidate_id'])
    amended = actual.service.edit_candidate(scope=SCOPE, candidate_id=candidate.object_id,
        expected_revision=candidate.revision, content=candidate.payload['content'], conditions=[label],
        editor='local-human-test-reviewer')
    long_method = actual.service.publish(scope=SCOPE, candidate_id=amended.id,
        expected_revision=amended.revision, reviewer='local-human-test-reviewer')
    graph = SourceEgressService(actual.records).snapshot(SCOPE,
        [{'type': 'recognition', 'id': long_method.id, 'revision': long_method.revision}])
    SourceEgressService(actual.records).validate_snapshot(SCOPE, graph)
    assert any(node['type'] == 'original_item' and node['id'] == staged['id'] for node in graph['nodes'])
    huge = actual.service.propose(scope=SCOPE, content='长方法预算' + ('合成步骤。' * 1800),
        conditions=['长方法预算'], source_experience_ids=[extracted['experience_id']])
    huge_method = actual.service.publish(scope=SCOPE, candidate_id=huge.id,
        expected_revision=huge.revision, reviewer='local-human-test-reviewer')
    with loopback(actual.app) as endpoint:
        async def operation():
            # 合成人工项目仍走原公开项目 owner，不手填项目记录或目录快照。
            async with httpx.AsyncClient(base_url=endpoint, trust_env=False) as human:
                response = await human.post('/api/v2/projects', json={'name': '合成预算项目' * 800})
                assert response.status_code == 200
                large_project = response.json()
            async with registered_sdk_session(actual, endpoint, suffix='budgets') as session:
                catalog = await sdk_success(session, 'projects', {})
                assert large_project['id'] not in {row['id'] for row in catalog['result']['projects']}
                assert catalog['result']['budget'] == 3000
                assert_public_completed(actual, catalog['turn_id'], actual.turns.get_request(catalog['turn_id']), catalog['result'])
                methods = await sdk_success(session, 'methods', {'situation': '长方法预算', 'project': 'alpha'})
                assert huge_method.id not in {row['object_id'] for row in methods['result']['entries']}
                assert methods['result']['budget'] == 3000
                assert_public_completed(actual, methods['turn_id'], actual.turns.get_request(methods['turn_id']), methods['result'])
                tiny = await sdk_success(session, 'recall', {'query': QUERY, 'project': 'alpha', 'budget': 1})
                assert tiny['result']['budget'] == 1 and tiny['result']['tokens'] == 0
                assert tiny['result']['entries'] == tiny['result']['profile'] == [] and tiny['result']['text'] == ''
                assert_public_completed(actual, tiny['turn_id'], actual.turns.get_request(tiny['turn_id']), tiny['result'])
                parent = await sdk_success(session, 'recall', {'query': label, 'project': 'alpha', 'budget': 1024})
                entry = next(row for row in parent['result']['entries'] if row['object_id'] == long_method.id)
                assert_public_completed(actual, parent['turn_id'], actual.turns.get_request(parent['turn_id']), parent['result'])
                identity = {'turn_id': parent['turn_id'], 'id': entry['id']}
                child = await sdk_success(session, 'read', {'id': identity})
                assert child['result']['budget'] == parent['result']['budget'] == 1024
                assert child['result']['entries'] == [] and child['result']['text'] == ''
                assert_public_completed(actual, child['turn_id'], actual.turns.get_request(child['turn_id']), child['result'])
                window = await sdk_success(session, 'read', {'id': identity, 'window': {'start': 0, 'end': 8}})
                original = next(row for row in window['result']['entries'] if row['object_id'] == staged['id'])
                assert original['excerpt'] == long_source[:8] and window['result']['budget'] == 1024
                assert_public_completed(actual, window['turn_id'], actual.turns.get_request(window['turn_id']), window['result'])
                arguments = sdk_arguments(window, original)
                before = consumer_facts(actual)
                for tool in (name for name in TOOLS if name != 'recall'):
                    await sdk_reject(session, tool, {**arguments[tool], 'budget': 1}, 'external_agent_arguments_invalid')
                    assert consumer_facts(actual) == before
                for budget in (0, False, 12001):
                    await sdk_reject(session, 'recall', {'query': QUERY, 'project': 'alpha', 'budget': budget},
                        'external_agent_arguments_invalid')
                    assert consumer_facts(actual) == before
        asyncio.run(operation())
    assert len(actual.native_calls) == 2
