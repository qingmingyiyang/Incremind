"""Actual confirmation and ContextBinding owners retain admin attribution."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.storage_provider import SQLiteStructuredRecordStore
from core.document_engine import SQLiteDocumentRepository
from backend.security.audited_records import AuditedRecordStore
from backend.security.user_context import UserAccess, user_context
from backend.security.device_identity import DeviceIdentity
from backend.shared.server_resources import SharedResources, resource_context


def test_real_http_confirmation_marks_original_and_document_without_payload_edits(tmp_path):
    from backend.memory_app.workspace import install_workspace_routes
    from backend.recognition import RecognitionService
    from tests.memory_app.test_workspace import Model
    from tests.memory_app.test_workspace_confirmation import _ready_item
    root=tmp_path/'users/user-b'
    records=AuditedRecordStore(SQLiteStructuredRecordStore(root/'.rebuild-data/structured-records.sqlite3'),namespace='default',user_id='user-b')
    documents=SQLiteDocumentRepository(records,namespace_id='default')
    access=UserAccess(DeviceIdentity('device-admin','local-user',1),'user-b','admin')
    with resource_context(SharedResources(tmp_path)):
        app=FastAPI()
        domains=install_workspace_routes(app,runtime_root=root,records=records,documents=documents,models=Model(),service=RecognitionService(records))
        item=_ready_item('workspace-one')
        item['draft']['summary']='人工核对摘要'
        item['draft'].update(topics=[],uncertainties=[],people=[],dates=[],suggestions=[])
        with records.begin() as tx:
            tx.put('workspace_items',item['id'],item,expected_revision=0);tx.commit()
        with user_context(access),TestClient(app) as client:
            result=client.post('/api/workspace/v1/items/workspace-one/confirm',json={'project_id':'alpha','expected_revision':1})
            assert result.status_code==200
        value=result.json();store=domains.confirmations.source_store
        assert store.attribution('sources',value['source_id'],store.revision('sources',value['source_id']))['by']=='admin'
        doc=documents.read(value['document_id'])
        assert records.writer_for('documents',value['document_id'],doc['revision'])['by']=='admin'
        assert 'by' not in doc and 'by' not in store.read('sources',value['source_id'])


def test_context_binding_formal_owner_has_same_root_and_exact_metadata(tmp_path):
    from backend.api.context_binding_runtime import ContextBindingRegistry
    from tests.rebuild.context_graph.test_context_binding_turn_contract import _binding
    root=tmp_path/'users/user-b'
    with resource_context(SharedResources(tmp_path)):
        registry=ContextBindingRegistry(root)
        with user_context(UserAccess(DeviceIdentity('device-admin','local-user',1),'user-b','admin')):
            registry.create(binding_id='binding-one',project_id='default',capability_id='context-one',
                capability_revision='2.5.0',binding=_binding(),expected_revision=0)
        assert registry._store.attribution('context_bindings','binding-one',1)['by']=='admin'


def test_actual_external_invalidation_dispatch_binds_json_and_sqlite_writes(tmp_path):
    from core.storage_provider import JsonObjectStore
    from core.storage_provider.external_agent_publication_change import publication_outbox_collection
    from backend.api.external_agent_publication_change_startup import dispatch_memory_invalidation_changes
    from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
    root=tmp_path/'users/user-b'
    store=JsonObjectStore(root/'.rebuild-data',legacy_root=root/'library')
    store.write('memory_invalidations','invalid-one',{'id':'invalid-one','external_event':{
        'publication_identity':'publication-one','project_id':'alpha','change_type':'memory.invalidated',
        'object_ref':'crp://memory/alpha/memory-one','object_revision':'r2',
        'occurred_at':'2026-10-05T00:00:00Z'}},expected_revision=0)
    access=UserAccess(DeviceIdentity('device-admin','local-user',1),'user-b','admin')
    with resource_context(SharedResources(tmp_path)),user_context(access):
        result=dispatch_memory_invalidation_changes(root)
    assert result.enqueued==1 and result.failed==0
    assert store.attribution('memory_invalidations','invalid-one',2)['by']=='admin'
    records=AuditedRecordStore(SQLiteStructuredRecordStore(root/'.rebuild-data'/STRUCTURED_DATABASE_NAME),namespace='default',user_id='user-b')
    rows=records.list(publication_outbox_collection('alpha'))
    assert len(rows)==1
    assert records.writer_for(rows[0].collection,rows[0].object_id,rows[0].revision)['by']=='admin'


def test_real_kernel_approval_thread_keeps_admin_writes_then_clears_identity_for_owner_lease(tmp_path):
    from threading import Event
    from time import monotonic
    from backend.api.ai_turn_runner import AITurnRunner
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.api.source_document_ai_runtime import DOCUMENT_DRAFT_PROPOSE_CAPABILITY
    from backend.security.user_context import USER_ACCESS
    from backend.shared.server_resources import RESOURCE_POOL
    from core.ai_kernel import CapabilityDefinition, SQLiteAITurnStore, ScopedCapabilityRegistry, SynchronousAIRuntime
    from core.product_core.source_template_document import ApprovedSourceDocumentDraftWriter, prepare_source_document_ai_evidence
    from core.storage_provider.connection_scope import connection_scope
    from tests.backend.integration.api.test_source_document_ai_turn_runtime import _approval, _write_source
    from tests.backend.unit.api.test_ai_turn_runner import _request

    root = tmp_path / 'users/user-b'
    resources = SharedResources(tmp_path)
    admin = UserAccess(DeviceIdentity('device-admin', 'local-user', 1), 'user-b', 'admin')
    owner = UserAccess(DeviceIdentity('device-owner', 'user-b', 1), 'user-b', None)
    flows = ('draft-admin', 'draft-owner')
    planning_entered = {flow: Event() for flow in flows}
    planning_release = {flow: Event() for flow in flows}
    writing_entered = {flow: Event() for flow in flows}
    writing_release = {flow: Event() for flow in flows}
    arguments, written, observed, bindings = {}, {}, [], {}

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            if any(event['type'] == 'tool.completed' for event in events):
                return {'type': 'complete', 'summary': '真实原整理稿写入完成'}
            values = arguments[request['turn_id']]
            flow = values['generated']['title']
            planning_entered[flow].set()
            assert planning_release[flow].wait(3)
            return {'type': 'tool', 'capability_id': DOCUMENT_DRAFT_PROPOSE_CAPABILITY,
                'arguments': values}

    class DraftAdapter:
        def invoke(self, request):
            values = request['arguments']
            flow = values['generated']['title']
            writing_entered[flow].set()
            assert writing_release[flow].wait(3)
            observed.append((flow, USER_ACCESS.get(), RESOURCE_POOL.get()))
            # 仅适配原能力协议，事实写入由原批准写入者和 SQLite/JSON 所有者完成。
            result = writer.execute(evidence=values['evidence'], generated=values['generated'],
                provider_id='synthetic-model-boundary', model_name='synthetic-model')
            written[flow] = result
            return {'summary': '原写入者已完成', 'receipt_ref': result.receipt_ref,
                'evidence_refs': []}

    def waiting_for_approval(runtime, runner, turn_id):
        deadline = monotonic() + 3
        while True:
            receipt = runtime.receipt_for(turn_id)
            if receipt.status == 'waiting_approval' and turn_id not in runner.active_turn_ids:
                return receipt
            assert receipt.status not in {'failed', 'cancelled'}, receipt
            assert monotonic() < deadline, receipt
            Event().wait(0.01)

    with resource_context(resources):
        store, settings = build_rebuild_object_store(root)
        _write_source(store)
        records = AuditedRecordStore(SQLiteStructuredRecordStore(root / '.rebuild-data/documents.sqlite3'),
            namespace=settings.namespace_id, user_id='user-b')
        documents = SQLiteDocumentRepository(records, namespace_id=settings.namespace_id)
        writer = ApprovedSourceDocumentDraftWriter(object_store=store, documents=documents,
            namespace_id=settings.namespace_id)
        registry = ScopedCapabilityRegistry()
        registry.register(CapabilityDefinition(DOCUMENT_DRAFT_PROPOSE_CAPABILITY, 1, 'write', True,
            'receipt_required', 'crp://default/contracts/in.schema.json',
            'crp://default/contracts/out.schema.json'), DraftAdapter())
        session = SQLiteAITurnStore(root / '.rebuild-data/ai-turns.sqlite3')
        runtime = SynchronousAIRuntime(planner=Planner(), registry=registry,
            events=session, payloads=session, state=session)
        # Runner 在管理员请求之前创建，不能把一次访问者身份固定到工厂。
        runner = AITurnRunner(runtime, max_workers=1)
        try:
            for index, (flow, access) in enumerate(zip(flows, (admin, owner))):
                request = _request()
                request.update(turn_id='turn-' + str(index + 1) * 32,
                    operation_id='op-' + flow, idempotency_key='kernel-' + flow)
                request['capability_policy'] = {'allowed': [DOCUMENT_DRAFT_PROPOSE_CAPABILITY],
                    'denied': [], 'require_approval': [DOCUMENT_DRAFT_PROPOSE_CAPABILITY]}
                arguments[request['turn_id']] = {
                    'evidence': prepare_source_document_ai_evidence(source=store.read('sources', 'source-1'),
                        source_revision=store.revision('sources', 'source-1'), template_type='answer_manual'),
                    'generated': {'title': flow, 'markdown': '## 结论\n\n合成资料的整理稿。'}}
                with connection_scope(), user_context(access):
                    accepted = runner.accept_and_submit(request)
                    assert accepted.status == 'accepted'
                    assert planning_entered[flow].wait(3)
                assert USER_ACCESS.get() is None
                planning_release[flow].set()
                waiting = waiting_for_approval(runtime, runner, accepted.turn_id)
                assert len(documents.list()) == index and len(store.list('source_template_outputs')) == index
                events = tuple(runtime.events_after(waiting.turn_id))
                approval = _approval(waiting, events[-1], suffix=flow)
                approval['action_id'] = 'action-' + str(index + 1) * 32
                with connection_scope(), user_context(access):
                    action_receipt = runner.accept_action_and_submit(approval, acceptance_timeout_seconds=3)
                    assert action_receipt.status == 'running'
                    assert writing_entered[flow].wait(3)
                assert USER_ACCESS.get() is None
                writing_release[flow].set()
                terminal = runner.wait_for_terminal(waiting.turn_id, timeout_seconds=3)
                assert terminal is not None and terminal.status == 'completed'
                result = written[flow]
                document = records.read('documents', result.document.document_id)
                output_id = result.receipt_ref.rsplit('/', 1)[-1].removesuffix('.json')
                bindings[flow] = (records.writer_for('documents', document.object_id, document.revision),
                    store.attribution('source_template_outputs', output_id, 1),
                    store.attribution('sources', 'source-1', index + 2))
                assert 'by' not in document.payload and 'by' not in store.read('source_template_outputs', output_id)

            admin_sql, admin_json, admin_source = bindings[flows[0]]
            assert admin_sql is not None and admin_json is not None and admin_source is not None, bindings
            assert admin_sql['by'] == admin_json['by'] == admin_source['by'] == 'admin'
            assert admin_sql['actor_user_id'] == admin_json['actor_user_id'] == admin.caller.user_id
            assert admin_sql['actor_device_id'] == admin_json['device_id'] == admin.caller.device_id
            assert admin_sql['target_user_id'] == admin_json['target_user_id'] == 'user-b'
            assert admin_sql['namespace'] == admin_json['namespace_id'] == settings.namespace_id
            assert bindings[flows[1]] == (None, None, None)
            assert observed == [(flows[0], admin, resources), (flows[1], owner, resources)]
            assert len(documents.list()) == len(store.list('source_template_outputs')) == 2
            assert not store.list('memory_candidates')
        finally:
            for gate in (*planning_release.values(), *writing_release.values()):
                gate.set()
            assert runner.shutdown(timeout_seconds=3) == ()
