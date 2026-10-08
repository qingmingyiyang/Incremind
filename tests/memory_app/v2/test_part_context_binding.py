"""使用真实 SQLite 事务验证多部分上下文的正式领域绑定合同。"""
from copy import deepcopy

import pytest

from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


def _request():
    request = freeze_turn_request('project.answer', turn_id='turn-binding-t13-2',
        session_id='session-binding-t13-2', operation_id='op-binding-t13-2',
        idempotency_key='binding-t13-2', project_id='project-a',
        created_at='2026-10-08T00:00:00Z', text='保留原输入。', situation='资金核验',
        refs=[{'kind':'source', 'object_id':'original-ref',
            'uri':'crp://default/workspace/original-ref'}],
        capabilities=['workbench.answer.execute'], template_version=2,
        privacy={'mode':'remote_allowed', 'allow_remote':True, 'pii':'possible',
            'consent_refs':['crp://default/model-settings/generation'], 'retention':'session'})
    request['context_policy']['include_memory'] = False
    return request


def _context():
    # 这里只验不透明 JSON 的绑定；来源权威由原八个真实调用方验证。
    return {'project_id':'project-a', 'parent':'parent-binding-t13-2',
        'originals':[{'identity':{'id':'original-ref', 'revision':2}, 'span':'预算十万元。'}],
        'answers':[{'id':'answer-ref', 'revision':3, 'question':'预算多少？',
            'answer':'十万元', 'authority':{'target':{'model':'synthetic'}, 'chosen':[]}}],
        'privacy_revision':4, 'text':'冻结依赖原文', 'target':{'model':'synthetic'},
        'refs':[{'kind':'session_event', 'object_id':'answer-ref',
            'uri':'crp://default/v2_turns/answer-ref'}]}


def _observe_transactions(records, monkeypatch):
    begin, transactions = records.begin, []

    def observed_begin():
        # 观察真实 Store/UoW，所有原方法均委托一次，不替换事务实现。
        tx = begin()
        seen = {'tx':tx, 'in_transaction':tx.connection.in_transaction,
            'puts':[], 'commits':0, 'rollbacks':0, 'sql_verbs':[]}
        tx.connection.set_trace_callback(lambda sql: seen['sql_verbs'].append(sql.split(None, 1)[0]))
        put, commit, rollback = tx.put, tx.commit, tx.rollback

        def observed_put(collection, identity, payload, *, expected_revision):
            seen['puts'].append((collection, identity, deepcopy(payload), expected_revision))
            return put(collection, identity, payload, expected_revision=expected_revision)

        def observed_commit():
            seen['commits'] += 1
            return commit()

        def observed_rollback():
            seen['rollbacks'] += 1
            return rollback()

        monkeypatch.setattr(tx, 'put', observed_put)
        monkeypatch.setattr(tx, 'commit', observed_commit)
        monkeypatch.setattr(tx, 'rollback', observed_rollback)
        transactions.append(seen)
        return tx

    monkeypatch.setattr(records, 'begin', observed_begin)
    return transactions


def _expected_refs(request, context, collection):
    return request['input']['refs'] + context['refs'] + [{
        'kind':'session_event', 'object_id':request['turn_id'],
        'uri':f"crp://default/{collection}/{request['turn_id']}"}]


def test_domain_binding_none_keeps_full_request_without_transaction(tmp_path, monkeypatch):
    from backend.memory_app.part_context_binding import bind_context

    records = SQLiteStructuredRecordStore(tmp_path / 'binding.sqlite3')
    transactions = _observe_transactions(records, monkeypatch)
    request = _request()
    before = deepcopy(request)
    assert bind_context(records, request, None) is None
    assert request == before
    assert transactions == []
    assert not records.database_path.exists()


def test_domain_binding_commits_exact_refs_payload_and_revision_zero(tmp_path, monkeypatch):
    from backend.memory_app.part_context_binding import COLLECTION, bind_context

    records = SQLiteStructuredRecordStore(tmp_path / 'binding.sqlite3')
    transactions = _observe_transactions(records, monkeypatch)
    request, context = _request(), _context()
    expected_request, before_context = deepcopy(request), deepcopy(context)
    refs = _expected_refs(request, context, COLLECTION)
    expected_request['input']['refs'] = refs
    payload = {'project_id':context['project_id'], 'input_refs':refs, 'context':context}
    assert bind_context(records, request, context) is None
    assert request == expected_request
    assert context == before_context
    assert len(transactions) == 1
    seen = transactions[0]
    assert seen['in_transaction'] is True
    assert seen['puts'] == [(COLLECTION, request['turn_id'], payload, 0)]
    assert seen['commits'] == 1 and seen['rollbacks'] == 0
    assert 'COMMIT' in seen['sql_verbs'] and 'ROLLBACK' not in seen['sql_verbs']
    assert seen['tx'].closed
    row = records.read(COLLECTION, request['turn_id'])
    assert row.collection == COLLECTION and row.object_id == request['turn_id']
    assert row.revision == 1 and row.payload == payload
    assert records.list_all() == (row,)


def test_v2_binding_is_the_same_domain_function_and_collection():
    from backend.memory_app import part_context_binding
    from backend.memory_app.v2 import part_context

    assert part_context_binding.COLLECTION == 'v2_part_contexts'
    assert part_context.COLLECTION == part_context_binding.COLLECTION
    assert part_context.bind_context is part_context_binding.bind_context


def test_domain_binding_duplicate_cas_rolls_back_original_sqlite_transaction(tmp_path, monkeypatch):
    from backend.memory_app.part_context_binding import COLLECTION, bind_context

    records = SQLiteStructuredRecordStore(tmp_path / 'binding.sqlite3')
    transactions = _observe_transactions(records, monkeypatch)
    bind_context(records, _request(), _context())
    before = records.list_all()
    duplicate, changed_context = _request(), _context()
    changed_context['text'] = '重复请求的新内容不得覆盖已存绑定'
    expected_duplicate = deepcopy(duplicate)
    refs = _expected_refs(duplicate, changed_context, COLLECTION)
    expected_duplicate['input']['refs'] = refs
    payload = {'project_id':changed_context['project_id'], 'input_refs':refs, 'context':changed_context}
    with pytest.raises(SQLiteUnitOfWorkConflict, match='expected revision 0, found 1'):
        bind_context(records, duplicate, changed_context)
    # 保留原先改请求再写 TX 的行为；CAS 失败只回滚数据库。
    assert duplicate == expected_duplicate
    assert len(transactions) == 2
    seen = transactions[1]
    assert seen['in_transaction'] is True
    assert seen['puts'] == [(COLLECTION, duplicate['turn_id'], payload, 0)]
    assert seen['commits'] == 0 and seen['rollbacks'] == 1
    assert 'ROLLBACK' in seen['sql_verbs'] and 'COMMIT' not in seen['sql_verbs']
    assert seen['tx'].closed
    assert records.list_all() == before
