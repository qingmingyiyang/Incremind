"""原读工具服务经真实注册、Boundary、内核与交付执行；不代签 HTTP/SDK/工厂。"""
from datetime import datetime, timezone
from importlib.abc import MetaPathFinder
import json
import sys

import pytest

from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver, ProjectAwareContextManifestResolver,
    TurnProjectProfileSnapshotAuthority,
)
from backend.api.ai_turn_runner import AITurnRunner
from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
from backend.memory_app.kernel.policy_runtime import ProductPolicyRuntime
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.original_sources import source_store
from backend.memory_app.research_sources import ReadRegistry
from backend.memory_app.source_egress import SourceEgressService, recognition_service
from backend.memory_app.v2.budget import text_tokens
from backend.memory_app.v2.external_agent_settings import external_agent_settings, replace_external_agent_settings
from backend.memory_app.v2.external_catalog import ExternalCatalog
from backend.memory_app.v2.external_context import ARCHIVE, DELIVERIES, ExternalContext, ExternalContextError
from backend.memory_app.v2.external_recall import ExternalRecall
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.workspace_query import WorkspaceQuery
from backend.recognition import WorkScope
from backend.security.ai_tool_execution_boundary import AIToolExecutionBoundary, TurnCapabilityBindingGuard
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.secrets import InMemorySecretStore
from core.ai_kernel import ScopedCapabilityRegistry, SQLiteAITurnStore
from core.ai_kernel.capability_manifest import manifest_from_payload
from core.ai_kernel.context_manifest import context_manifest_from_payload
from core.context_graph import capability_artifact
from core.context_graph.capability_artifact import CapabilityArtifactStore
from core.document_engine import SQLiteDocumentRepository
from core.effect_log import EffectState
from core.storage_provider import SQLiteStructuredRecordStore


NOW = datetime(2026, 10, 7, 4, tzinfo=timezone.utc)
CAPABILITY = 'external.context.execute'
RESERVATIONS = 'v2_external_agent_reservations'


@pytest.fixture(autouse=True)
def no_factory_or_artifact_capture(monkeypatch):
    factories = {'backend.memory_app.app', 'backend.api.app'}
    loaded = factories.intersection(sys.modules)
    hits = []

    def forbidden(*args, **kwargs):
        hits.append(True)
        raise AssertionError('原公共读工具服务测试不得进入 factory 或 ArtifactStore')

    class FactoryGate(MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname in factories:
                forbidden()

    monkeypatch.setattr(sys, 'meta_path', [FactoryGate(), *sys.meta_path])
    monkeypatch.setattr(CapabilityArtifactStore, '__init__', forbidden)
    monkeypatch.setattr(CapabilityArtifactStore, 'capture', forbidden)
    monkeypatch.setattr(CapabilityArtifactStore, 'verify', forbidden)
    monkeypatch.setattr(capability_artifact, '_read_checked', forbidden)
    for name in loaded:
        monkeypatch.setattr(sys.modules[name], 'create_app', forbidden)
    yield
    assert factories.intersection(sys.modules) <= loaded and not hits


class RegisteredContext:
    def __init__(self, root, *, completion_fn=None):
        # 此导入发生在门禁启用后；只构造原 planner，不调用 Runtime 工厂。
        from backend.memory_app.kernel.ai_runtime import RecallFirstLocalPlanner

        self.records = SQLiteStructuredRecordStore(root / '.rebuild-data/structured-records.sqlite3')
        self.documents = SQLiteDocumentRepository(self.records)
        self.sources = source_store(self.records)
        self.service = recognition_service(self.records)
        self.transport_calls = []

        def forbidden_transport(**request):
            self.transport_calls.append(True)
            raise AssertionError('外部读工具不得发起模型传输')

        self.models = ModelConfiguration(self.records, root, InMemorySecretStore(),
            completion_fn=forbidden_transport if completion_fn is None else completion_fn)
        self.query = WorkspaceQuery(self.records, self.documents, self.sources, self.models, self.service)
        self.context = ExternalContext(self.records, owner_id='local-user', documents=self.documents,
            now=lambda: NOW)
        self.context.bind_catalog(ExternalCatalog(self.query, owner_id='local-user'))
        self.context.bind_recall(ExternalRecall(self.query, owner_id='local-user'))
        self.turns = SQLiteAITurnStore(root / '.rebuild-data/ai-turns.sqlite3')
        self.dispatch = ScopedCapabilityRegistry()
        self.registration = ReadRegistry(ReviewedCoreCapabilityRegistry(
            RuntimeCapabilityAdmission(self.dispatch)), self.records)
        bind_runtime = self.context.install(self.registration, self.turns)
        capability_profiles = ProjectCapabilityProfileStore(root)
        boundary_profiles = ProjectBoundaryProfileStore(root)
        snapshots = TurnProjectProfileSnapshotAuthority(capability_profiles, boundary_profiles)
        self.boundary = AIToolExecutionBoundary(boundary_profiles,
            binding_guard=TurnCapabilityBindingGuard(capability_profiles, boundary_profiles,
                events=self.turns, payloads=self.turns))
        self.planner = RecallFirstLocalPlanner()
        self.runtime = ProductPolicyRuntime(planner=self.planner, registry=self.dispatch,
            events=self.turns, payloads=self.turns, state=self.turns,
            manifest_resolver=ProjectAwareCapabilityManifestResolver(snapshots),
            context_manifest_resolver=ProjectAwareContextManifestResolver(snapshots),
            execution_boundary=self.boundary, effect_runner=self.turns.effect_runner)
        bind_runtime(self.runtime)
        self.runner = AITurnRunner(self.runtime, max_workers=1, max_pending=2)
        self.configure(allow_remote=True, include_profile=False)
        self.add_source('public-source', 'alpha', '公开原件正文')
        self.add_source('private-source', 'private-project', '私密原件正文')
        set_private_project(self.records, 'private-project', True, 0)
        scope = WorkScope('local-user', 'alpha')
        experience = self.service.stage_experience(scope=scope, content='合成方法的真实原出处')
        pending = self.service.propose(scope=scope, content='先问近期爱好和实际预算',
            conditions=['挑礼物时'], source_experience_ids=[experience])
        self.method = self.service.publish(scope=scope, candidate_id=pending.id,
            expected_revision=1, reviewer='local-user')
        # 同一 owner/records/JSON/SQLite 实例贯穿原公共服务与原注册消费者。
        assert self.context.catalog.query is self.query and self.context.recall.query is self.query
        assert self.query.source_store is self.sources and self.context.guard.records is self.records
        assert self.context.turns is self.turns and self.context.runtime is self.runtime

    def configure(self, **changes):
        current = external_agent_settings(self.records)
        return replace_external_agent_settings(self.records,
            {key: value for key, value in current.items() if key != 'revision'} | changes,
            expected_revision=current['revision'])

    def add_source(self, identity, project, content):
        self.sources.write('sources', identity,
            {'id': identity, 'title': identity, 'project_id': project,
             'metadata': {'content_snapshot': content}}, expected_revision=0)

    def prepare(self, tool, *, client='codex', budget=3000, suffix='first'):
        turn = f'turn-{tool}-{client}-{suffix}'
        request = {'client': client, 'tool': tool,
            'query': '' if tool == 'projects' else '挑礼物',
            'scope': {'user_id': 'local-user', 'project_id': 'default' if tool == 'projects' else 'alpha'},
            'budget': budget}
        prepare = self.context.prepare_projects if tool == 'projects' else self.context.prepare_recall
        frozen = prepare(turn, request, session_id=f'session-{turn}', operation_id=f'op-{turn}',
            idempotency_key=turn, created_at=NOW.isoformat())
        return turn, frozen

    def execute(self, turn):
        return self.context.execute(turn, runtime=self.runtime, runner=self.runner)

    def assert_completed(self, turn, frozen, delivered):
        events = self.turns.events_after(turn)
        assert self.turns.get_request(turn) == frozen and events[-1]['type'] == 'turn.completed'
        assert not any(event['type'].startswith('model.') for event in events)
        kinds = ('tool.requested', 'tool.intent.recorded', 'tool.dispatch.claimed',
            'tool.outcome.recorded', 'tool.completed')
        matched = {}
        for kind in kinds:
            rows = [event for event in events if event['type'] == kind]
            assert len(rows) == 1
            matched[kind] = rows[0]
        assert [matched[kind]['sequence'] for kind in kinds] == sorted(matched[kind]['sequence'] for kind in kinds)
        boundary = self.turns.get(matched['tool.requested']['data']['payload_ref'])
        assert boundary['outcome'] == 'allow'
        invocation = matched['tool.intent.recorded']['correlation']['tool_call_id']
        intent_ref = matched['tool.intent.recorded']['data']['payload_ref']
        intent = self.turns.get(intent_ref)
        assert intent['arguments'] == frozen['capability_request']['arguments']
        assert intent['capability_id'] == CAPABILITY and intent['turn_id'] == turn
        assert matched['tool.dispatch.claimed']['data']['payload_ref'] == intent_ref
        effect = self.turns.effect_runner.log.get(invocation)
        outcome_ref = matched['tool.outcome.recorded']['data']['payload_ref']
        assert effect.state is EffectState.SETTLED_OK and effect.turn_id == turn
        assert effect.intent_ref == intent_ref and effect.result_ref == outcome_ref
        outcome = self.turns.get(outcome_ref)
        assert outcome['invocation_id'] == invocation and outcome['attempt'] == effect.attempt == 1
        immutable_ref, archive = self.context._archive(turn)
        assert archive['handoff'] == delivered and archive['request'] == frozen
        assert outcome['payload_ref'] == immutable_ref
        receipt = self.records.read(DELIVERIES, turn)
        assert receipt.revision == 1 and receipt.payload['immutable_ref'] == immutable_ref
        assert receipt.payload['outcome_ref'] == outcome_ref and receipt.payload['owner_id'] == 'local-user'
        assert self.context._completed(turn) == (immutable_ref, archive, outcome_ref)
        manifest_event = next(event for event in events if event['type'] == 'context.resolved')
        context_manifest = context_manifest_from_payload(self.turns.get(manifest_event['data']['payload_ref']))
        manifest = manifest_from_payload(self.turns.get(context_manifest.capability_manifest_ref))
        assert manifest.resolver_id == 'project-profile-v1' and manifest.capability_ids == (CAPABILITY,)
        assert self.records.read(RESERVATIONS, turn).revision == 1
        assert delivered['tokens'] == text_tokens(delivered['text']) <= delivered['budget']
        before = tuple(events)
        assert self.execute(turn) == delivered and self.turns.events_after(turn) == before
        assert self.records.read(DELIVERIES, turn).revision == 1


@pytest.fixture
def registered(tmp_path):
    actual = RegisteredContext(tmp_path)
    try:
        yield actual
    finally:
        assert actual.runner.shutdown(timeout_seconds=3) == ()
        assert actual.transport_calls == []


@pytest.mark.parametrize('tool', ['projects', 'methods'])
@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_registered_context_completes_real_delivery(registered, tool, client):
    turn, frozen = registered.prepare(tool, client=client)
    delivered = registered.execute(turn)
    registered.assert_completed(turn, frozen, delivered)
    assert 'private-project' not in json.dumps(delivered, ensure_ascii=False)
    assert '私密原件正文' not in delivered['text']
    if tool == 'projects':
        row = next(row for row in delivered['projects'] if row['id'] == 'alpha')
        assert row['overview'] == '资料1份 · 认识1条'
        assert delivered['version'] == 'handoff@2'
    else:
        row = next(row for row in delivered['entries'] if row['object_id'] == registered.method.id)
        assert row['layer'] == 'L3' and row['conditions'] == ['挑礼物时']
        proof, arguments = registered.context.delivered_proof(turn, row['id'], client=client)
        assert proof['material']['id'] == registered.method.id and arguments == frozen['capability_request']['arguments']
        SourceEgressService(registered.records).validate_snapshot(WorkScope('local-user', 'alpha'), proof['snapshot'])
    assert registered.records.list('v2_usage_document') == registered.records.list('v2_usage_insight') == ()


@pytest.mark.parametrize('tool', ['projects', 'methods'])
@pytest.mark.parametrize('blocked', ['off', 'client', 'invalid-budget'])
def test_registered_context_rejects_before_accept(registered, tool, blocked):
    if blocked == 'off':
        registered.configure(allow_remote=False)
    elif blocked == 'client':
        registered.configure(clients={'claude': True, 'codex': False})
    with pytest.raises(ValueError):
        registered.prepare(tool, budget=12001 if blocked == 'invalid-budget' else 3000)
    turn = f'turn-{tool}-codex-first'
    assert registered.turns.get_request(turn) is None and registered.turns.events_after(turn) == ()
    assert registered.records.list(RESERVATIONS) == registered.records.list(DELIVERIES) == ()


@pytest.mark.parametrize('tool', ['projects', 'methods'])
def test_registered_context_small_budget_omits_complete_rows(registered, tool):
    turn, frozen = registered.prepare(tool, budget=1)
    delivered = registered.execute(turn)
    registered.assert_completed(turn, frozen, delivered)
    assert delivered['projects' if tool == 'projects' else 'entries'] == []
    assert delivered['text'] == '' and delivered['tokens'] == 0


def test_registered_methods_private_project_cannot_accept(registered):
    set_private_project(registered.records, 'alpha', True, 0)
    with pytest.raises(ValueError):
        registered.prepare('methods')
    assert registered.turns.get_request('turn-methods-codex-first') is None
    assert registered.records.list(RESERVATIONS) == registered.records.list(DELIVERIES) == ()


@pytest.mark.parametrize('tool', ['projects', 'methods'])
def test_registered_context_daily_budget_fails_real_dispatch_without_delivery(registered, tool):
    registered.configure(daily_limit=1)
    first, frozen = registered.prepare(tool)
    registered.assert_completed(first, frozen, registered.execute(first))
    second, _ = registered.prepare(tool, suffix='second')
    with pytest.raises(ExternalContextError, match='external_context_not_completed'):
        registered.execute(second)
    events = registered.turns.events_after(second)
    assert events[-1]['type'] == 'turn.failed'
    intent = next(event for event in events if event['type'] == 'tool.intent.recorded')
    effect = registered.turns.effect_runner.log.get(intent['correlation']['tool_call_id'])
    assert effect.state is EffectState.SETTLED_ERR and effect.intent_ref == intent['data']['payload_ref']
    assert registered.records.read(DELIVERIES, second) is None and registered.records.read(RESERVATIONS, second) is None
    assert len(registered.records.list(RESERVATIONS)) == len(registered.records.list(DELIVERIES)) == 1
    assert registered.records.read('v2_external_agent_quota_20261007', 'local-user').payload['count'] == 1


@pytest.mark.parametrize('tool', ['projects', 'methods'])
def test_registered_context_source_change_before_dispatch_has_no_delivery(registered, tool):
    turn, _ = registered.prepare(tool)
    if tool == 'projects':
        registered.sources.write('sources', 'public-source',
            {'id': 'public-source', 'title': 'public-source', 'project_id': 'alpha',
             'metadata': {'content_snapshot': '真实原 owner 的新版原件'}}, expected_revision=1)
    else:
        SourceEgressService(registered.records).set_policy(WorkScope('local-user', 'alpha'),
            source_type='experience', source_id=registered.method.source_experience_ids[0],
            expected_source_revision=1, expected_policy_revision=0, allowed_purposes=[])
    with pytest.raises(ValueError):
        registered.execute(turn)
    events = registered.turns.events_after(turn)
    assert not any(event['type'].startswith('tool.') or event['type'].startswith('model.') for event in events)
    assert registered.records.read(DELIVERIES, turn) is None and registered.records.read(RESERVATIONS, turn) is None
