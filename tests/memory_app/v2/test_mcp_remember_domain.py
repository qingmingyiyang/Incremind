"""只核原 remember 文本写入 adapter，不代签 HTTP、握手或外层 check_write。"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import sys
from threading import RLock

import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.processing_lease import ProcessingLease
from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.v2.external_agent_guard import ExternalAgentGuard, ExternalAgentGuardError
from backend.memory_app.v2.external_agent_settings import external_agent_settings, replace_external_agent_settings
from backend.memory_app.v2.mcp_intake import INTAKES, _ClientItems
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.workspace_intake import WorkspaceIntake
from backend.memory_app.workspace_items import WorkspaceItems
from backend.security.secrets import InMemorySecretStore
from core.context_graph.capability_artifact import CapabilityArtifactStore
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.record_lineage import FACTS, HEAD_COLLECTIONS, WITNESS_COLLECTIONS
from core.storage_provider.source_retrieval_index import ORIGINALS, ORIGINAL_VERSION


NOW = datetime(2026, 10, 7, 1, tzinfo=timezone.utc)
QUOTA = 'v2_external_agent_quota_20261007'
WRITES = 'v2_external_agent_write_reservations'
SCENES = 'v2_scene_assignments_item'


@pytest.fixture(autouse=True)
def no_factory_or_artifact_capture(monkeypatch):
    factories = {'backend.memory_app.app', 'backend.api.app'}
    loaded = factories.intersection(sys.modules)
    hits = []

    def forbidden(*args, **kwargs):
        hits.append(True)
        raise AssertionError('原文本写入 adapter 不得进入 factory 或 ArtifactStore')

    monkeypatch.setattr(CapabilityArtifactStore, '__init__', forbidden)
    monkeypatch.setattr(CapabilityArtifactStore, 'capture', forbidden)
    for name in loaded:
        monkeypatch.setattr(sys.modules[name], 'create_app', forbidden)
    yield
    assert factories.intersection(sys.modules) <= loaded and not hits


class RememberDomain:
    def __init__(self, root, monkeypatch):
        self.records = SQLiteStructuredRecordStore(root / '.rebuild-data/structured-records.sqlite3')
        self.configure(allow_remote=True)
        self.transport_calls = []

        def forbidden_transport(**request):
            self.transport_calls.append(True)
            raise AssertionError('文本原件投入不得调用模型传输')

        self.models = ModelConfiguration(self.records, root, InMemorySecretStore(),
            completion_fn=forbidden_transport)
        self.items = WorkspaceItems(self.records,
            ProcessingLease(self.records, 'workspace_items', 'synthetic-remember-instance'), RLock())
        self.intake = WorkspaceIntake(root, self.items, self.models)
        self.guard = ExternalAgentGuard(self.records, owner_id='local-user', now=lambda: NOW)
        self.transactions, self.statements = [], []
        original_begin = self.records.begin

        @contextmanager
        def observed_begin():
            with original_begin() as tx:
                self.transactions.append(tx)

                def trace(statement):
                    if statement.strip().upper() in {'COMMIT', 'ROLLBACK'}:
                        self.statements.append(statement.strip().upper())

                # 只观测原连接的实际提交；不截获或替换任何领域写入。
                tx.connection.set_trace_callback(trace)
                yield tx

        monkeypatch.setattr(self.records, 'begin', observed_begin)

    def configure(self, **changes):
        current = external_agent_settings(self.records)
        return replace_external_agent_settings(self.records,
            {key: value for key, value in current.items() if key != 'revision'} | changes,
            expected_revision=current['revision'])

    def remember(self, text, *, client='codex', project='alpha', scene='reading'):
        self.transactions.clear()
        self.statements.clear()
        adapter = _ClientItems(self.items, records=self.records,
            reserve_write=self.guard.reserve_write, owner_id='local-user', client=client, scene=scene)
        actual = self.intake.with_items(adapter, self.models)
        assert isinstance(actual, WorkspaceIntake) and actual.models is self.models
        return asyncio.run(actual.add_text({'text': text, 'project_id': project}))


@pytest.fixture
def domain(tmp_path, monkeypatch):
    actual = RememberDomain(tmp_path, monkeypatch)
    yield actual
    assert actual.transport_calls == []


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_original_text_adapter_redacts_and_commits_client_receipt_and_sidecars_once(domain, client):
    text = 'Synthetic original\nsk-' + 'A' * 24
    result = domain.remember(text, client=client)
    assert result['project_id'] == 'alpha' and result['client'] == client
    assert result['state'] == 'staged' and result['verified'] is False
    assert result['revision'] == 1
    row = domain.records.read('workspace_items', result['item_id'])
    assert row.revision == 1 and row.payload['status'] == 'staged'
    assert row.payload['source_text'] == 'Synthetic original\n[REDACTED_SECRET]'
    assert row.payload['title'] == 'Synthetic original'
    assert row.payload['draft'] is None and row.payload['document_id'] is None
    assert 'client' not in row.payload and 'source_client' not in row.payload
    index = domain.records.read(ORIGINALS, row.object_id)
    assert index.revision == 1 and index.payload['original_revision'] == 1
    assert index.payload['id'] == row.object_id and index.payload['project_id'] == 'alpha'
    assert index.payload['projection_version'] == ORIGINAL_VERSION
    assert index.payload['length'] == len(row.payload['source_text'])
    assert [chunk['text'] for chunk in index.payload['chunks']] == [row.payload['source_text']]
    receipt = domain.records.read(INTAKES, result['receipt_id'])
    assert receipt.revision == 1
    assert receipt.payload['owner_id'] == 'local-user' and receipt.payload['client'] == client
    assert receipt.payload['tool'] == 'remember' and receipt.payload['project_id'] == 'alpha'
    assert receipt.payload['object_id'] == row.object_id and receipt.payload['object_revision'] == row.revision
    assert receipt.payload['scene'] == 'reading' and receipt.payload['created_at']
    scene = domain.records.read(SCENES, row.object_id)
    assert scene.revision == 1 and scene.payload == {'project_id': 'alpha', 'scene': 'reading'}
    reservation = domain.records.read(WRITES, result['receipt_id'])
    assert reservation.revision == 1 and reservation.payload['day'] == '20261007'
    assert reservation.payload['receipt_id'] == result['receipt_id']
    assert json.loads(reservation.payload['request_json']) == {'client': client, 'tool': 'remember',
        'scope': {'user_id': 'local-user', 'project_id': 'alpha'}}
    assert domain.records.read(QUOTA, 'local-user').payload['count'] == 1
    assert len(domain.transactions) == 1 and domain.statements == ['COMMIT']
    assert domain.records.list('documents') == domain.records.list('recognition_candidates') == ()
    assert domain.records.list('recognitions') == domain.records.list('v2_external_agent_deliveries') == ()


def test_original_text_adapter_borrows_owner_records_and_commits_all_facts_once(domain, monkeypatch):
    collections = ('workspace_items', ORIGINALS, SCENES, INTAKES, WRITES, QUOTA,
        'v2_original_sections', HEAD_COLLECTIONS['workspace_items'],
        WITNESS_COLLECTIONS['workspace_items'], FACTS)
    original_with_records = domain.items.with_records
    calls, snapshots, snapshot_errors = [], [], []

    def observe_with_records(enlisted):
        assert isinstance(enlisted, TransactionRecords)
        assert len(domain.transactions) == 1
        outer = domain.transactions[0]
        assert enlisted._transaction is outer
        borrowed = original_with_records(enlisted)
        calls.append((enlisted, borrowed))
        assert type(borrowed) is WorkspaceItems and borrowed is not domain.items
        assert borrowed.records is enlisted
        assert borrowed.processing_lease is domain.items.processing_lease
        assert borrowed.lock is domain.items.lock
        connection = outer.connection

        def trace(statement):
            operation = statement.strip().upper()
            if operation in {'COMMIT', 'ROLLBACK'}:
                domain.statements.append(operation)
            if operation != 'COMMIT':
                return
            # COMMIT 执行前只读观察原连接，独立连接应仍看不到这些未提交事实。
            try:
                rows = {collection: outer.list(collection) for collection in collections}
                detached = sqlite3.connect(domain.records.database_path.as_uri() + '?mode=ro', uri=True)
                try:
                    detached.execute('PRAGMA query_only=ON')
                    visible = {collection: detached.execute(
                        'SELECT count(*) FROM crp_structured_records WHERE collection=?',
                        (collection,)).fetchone()[0] for collection in collections}
                finally:
                    detached.close()
                snapshots.append((connection.in_transaction, rows, visible))
            except Exception as error:
                # SQLite 的 trace callback 会吞掉异常，留到调用完成后作严格断言。
                snapshot_errors.append(type(error).__name__)

        connection.set_trace_callback(trace)
        return borrowed

    # 只观察原 with_records 并委托，领域创建、额度及持久化 writer 均保持真实。
    monkeypatch.setattr(domain.items, 'with_records', observe_with_records)
    result = domain.remember('Synthetic borrowed original\nsk-' + 'C' * 24)
    assert len(calls) == 1
    assert calls[0][0]._transaction is domain.transactions[0]
    assert calls[0][1].records is calls[0][0]
    assert snapshot_errors == [] and len(snapshots) == 1
    in_transaction, rows, visible = snapshots[0]
    assert in_transaction is True and visible == dict.fromkeys(collections, 0)
    assert all(len(rows[collection]) == 1 for collection in collections)
    assert all(domain.records.list(collection) == rows[collection] for collection in collections)
    assert len(domain.transactions) == 1 and domain.statements == ['COMMIT']
    item = rows['workspace_items'][0]
    assert item.object_id == result['item_id'] and item.revision == result['revision'] == 1
    assert item.payload['source_text'] == 'Synthetic borrowed original\n[REDACTED_SECRET]'
    assert item.payload['title'] == 'Synthetic borrowed original'
    assert item.payload['status'] == result['state'] == 'staged' and result['verified'] is False
    assert item.payload['draft'] is None and item.payload['document_id'] is None
    assert 'client' not in item.payload and 'source_client' not in item.payload
    index = rows[ORIGINALS][0]
    assert index.object_id == item.object_id and index.revision == 1
    assert index.payload['original_revision'] == 1 and index.payload['project_id'] == 'alpha'
    assert [chunk['text'] for chunk in index.payload['chunks']] == [item.payload['source_text']]
    scene = rows[SCENES][0]
    assert scene.object_id == item.object_id and scene.revision == 1
    assert scene.payload == {'project_id': 'alpha', 'scene': 'reading'}
    receipt = rows[INTAKES][0]
    assert receipt.object_id == result['receipt_id'] and receipt.revision == 1
    assert receipt.payload['owner_id'] == 'local-user' and receipt.payload['client'] == 'codex'
    assert receipt.payload['tool'] == 'remember' and receipt.payload['scene'] == 'reading'
    assert receipt.payload['object_id'] == item.object_id and receipt.payload['object_revision'] == 1
    reservation = rows[WRITES][0]
    assert reservation.object_id == result['receipt_id'] and reservation.revision == 1
    assert reservation.payload['day'] == '20261007' and reservation.payload['receipt_id'] == result['receipt_id']
    assert json.loads(reservation.payload['request_json']) == {'client': 'codex', 'tool': 'remember',
        'scope': {'user_id': 'local-user', 'project_id': 'alpha'}}
    quota = rows[QUOTA][0]
    assert quota.object_id == 'local-user' and quota.revision == 1 and quota.payload['count'] == 1
    section = rows['v2_original_sections'][0]
    assert section.object_id == item.object_id and section.revision == 1
    assert section.payload['item_id'] == item.object_id and section.payload['project_id'] == 'alpha'
    assert section.payload['state'] == 'unbound' and section.payload['owner_birth']
    head, witness, fact = (rows[collection][0] for collection in (
        HEAD_COLLECTIONS['workspace_items'], WITNESS_COLLECTIONS['workspace_items'], FACTS))
    assert head.object_id == witness.object_id == item.object_id
    assert head.revision == witness.revision == fact.revision == 1
    assert head.payload['fact_id'] == witness.payload['first_fact_id'] == fact.object_id
    assert fact.payload == {'schema_version': 1, 'collection': 'workspace_items',
        'object_id': item.object_id, 'origin': 'created', 'observed_revision': 1}
    assert domain.records.list('documents') == domain.records.list('recognition_candidates') == ()
    assert domain.records.list('recognitions') == domain.records.list('v2_external_agent_deliveries') == ()


@pytest.mark.parametrize('restriction,error', [('off', 'external_agent_disabled'),
    ('client', 'external_agent_client_disabled'), ('private', 'external_agent_private'),
    ('daily_budget', 'external_agent_quota_exhausted')])
def test_original_inner_guard_rejects_without_new_domain_or_quota_facts(domain, restriction, error):
    if restriction == 'off':
        domain.configure(allow_remote=False)
    elif restriction == 'client':
        domain.configure(clients={'claude': True, 'codex': False})
    elif restriction == 'private':
        set_private_project(domain.records, 'alpha', True, 0)
    else:
        domain.configure(daily_limit=1)
        domain.remember('First synthetic original', client='claude')
    before = domain.records.list_all()
    with pytest.raises(ExternalAgentGuardError, match=error):
        domain.remember('Rejected synthetic original')
    assert domain.records.list_all() == before
    assert len(domain.transactions) == 1 and domain.statements == ['ROLLBACK']


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('prior_write', [False, True])
def test_receipt_trigger_failure_rolls_back_original_index_scene_lineage_and_shared_quota(domain, client, prior_write):
    if prior_write:
        domain.remember('Existing synthetic original', client=client)
    with domain.records.begin() as tx:
        tx.connection.execute("CREATE TRIGGER reject_synthetic_receipt BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_external_agent_intakes' BEGIN SELECT RAISE(ABORT,'synthetic_receipt_rejected'); END")
        tx.commit()
    before = domain.records.list_all()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic_receipt_rejected'):
        domain.remember('Must roll back synthetic original', client=client)
    assert domain.records.list_all() == before
    expected = int(prior_write)
    for collection in ('workspace_items', ORIGINALS, SCENES, INTAKES, WRITES):
        assert len(domain.records.list(collection)) == expected
    quota = domain.records.read(QUOTA, 'local-user')
    assert (quota.payload['count'] if quota else 0) == expected
    assert domain.records.list('documents') == domain.records.list('recognition_candidates') == ()
    assert len(domain.transactions) == 1 and domain.statements == ['ROLLBACK']
