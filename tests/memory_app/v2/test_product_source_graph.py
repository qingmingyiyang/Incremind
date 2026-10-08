"""Only real owner records authorize the flat product source graph."""
from copy import deepcopy

import pytest

from backend.memory_app.original_sources import source_store
from backend.memory_app.source_egress import SourceEgressService, _frozen_packet_authority
from backend.recognition import RecognitionConflict
from core.ai_kernel import SQLiteAITurnStore
from tests.memory_app.v2.test_product_draft_dependencies import draft, product_experience


@pytest.fixture
def graph_draft(draft):
    records, scope, _ = draft
    row = records.read('v2_task_executions', 'product-test')
    request = {**row.payload['request'], 'session_id': 'session-test',
        'operation_id': 'op-test', 'idempotency_key': 'key-test'}
    with records.begin() as tx:
        tx.put('v2_task_executions', row.object_id, {**row.payload, 'request': request},
            expected_revision=row.revision)
        tx.commit()
    SQLiteAITurnStore(source_store(records).root / 'ai-turns.sqlite3').claim_turn(request)
    return draft


def test_product_graph_is_flat_and_keeps_scoped_cas_metadata(graph_draft):
    authority, scope, ref = product_experience(graph_draft)
    snapshot = authority.snapshot(scope, [ref])
    dependency = snapshot['nodes'][0]['dependency_revisions']
    assert 'source_snapshots' not in dependency and 'current_source_snapshots' not in dependency
    graph = dependency['current_source_graph']
    assert set(graph) == {'roots', 'nodes'}
    assert graph['roots'] and all(type(index) is int for index in graph['roots'])
    assert all(node['kind'] == 'material' and node['scope']['project_id'] == scope.project_id
        for node in graph['nodes'])
    assert all(not {'source_snapshot', 'source_snapshots', 'current_source_graph'}.intersection(
        node.get('dependency_revisions', {})) for node in graph['nodes'])
    authority.validate_snapshot(scope, snapshot)
    authority.require(snapshot, 'generation')


@pytest.mark.parametrize('corruption', ['extra', 'duplicate', 'root_bool', 'root_negative',
    'root_outside', 'root_duplicate', 'root_order', 'node_order', 'revision_conflict',
    'revision_bool', 'revision_float', 'revision_overflow', 'id_long', 'id_unicode',
    'scope_forged', 'node_budget', 'nested_graph'])
def test_frozen_flat_graph_rejects_corruption_without_writes(graph_draft, corruption):
    records, scope, _ = graph_draft
    authority, scope, ref = product_experience(graph_draft)
    snapshot = authority.snapshot(scope, [ref])
    bad = deepcopy(snapshot)
    graph = bad['nodes'][0]['dependency_revisions']['current_source_graph']
    if corruption == 'extra':
        graph['edges'] = [[0, 0]]
    elif corruption == 'duplicate':
        graph['nodes'].append(deepcopy(graph['nodes'][0]))
    elif corruption == 'root_bool':
        graph['roots'][0] = True
    elif corruption == 'root_negative':
        graph['roots'][0] = -1
    elif corruption == 'root_outside':
        graph['roots'][0] = len(graph['nodes'])
    elif corruption == 'root_duplicate':
        graph['roots'].append(graph['roots'][0])
    elif corruption == 'root_order':
        graph['roots'] = [1, 0]
    elif corruption == 'node_order':
        graph['nodes'].reverse()
    elif corruption == 'revision_conflict':
        node = deepcopy(graph['nodes'][0])
        node['source_revision'] += 1
        graph['nodes'].append(node)
    elif corruption == 'revision_float':
        graph['nodes'][0]['source_revision'] = 1.0
    elif corruption == 'revision_overflow':
        graph['nodes'][0]['source_revision'] = 9223372036854775808
    elif corruption == 'id_long':
        graph['nodes'][0]['id'] = 'a' * 129
    elif corruption == 'id_unicode':
        graph['nodes'][0]['id'] = '原件'
    elif corruption == 'scope_forged':
        graph['nodes'][0]['scope']['user_id'] = 'another-user'
    elif corruption == 'node_budget':
        node = deepcopy(graph['nodes'][0])
        graph['nodes'] = [{**node, 'id': f'node-{index:03d}'} for index in range(257)]
    elif corruption == 'nested_graph':
        graph['nodes'][0]['dependency_revisions'] = {'current_source_graph': deepcopy(graph)}
    else:
        graph['nodes'][0]['source_revision'] = True
    before = records.list_all()
    with pytest.raises(RecognitionConflict):
        _frozen_packet_authority(scope, {'source_egress': bad},
            [('experience', ref['id'], ref['revision'])])
    assert records.list_all() == before


def test_same_uow_memo_rejects_a_cached_descendant_in_the_current_trail(graph_draft):
    from backend.memory_app.transaction_records import TransactionRecords
    records, scope, _ = graph_draft
    authority, scope, ref = product_experience(graph_draft)
    with records.begin() as tx:
        enlisted = SourceEgressService(TransactionRecords(tx))
        memo = {}
        first = enlisted._snapshot(scope, [ref], (), set(), memo)
        graph = first['nodes'][0]['dependency_revisions']['current_source_graph']
        descendant = next(node for node in graph['nodes'] if node['kind'] == 'material')
        key = ('material', descendant['scope']['user_id'], descendant['scope']['project_id'],
            descendant['type'], descendant['id'])
        with pytest.raises(RecognitionConflict, match='cyclic'):
            enlisted._snapshot(scope, [ref], (key,), set(), memo)


def test_top_level_snapshots_never_reuse_permission_from_a_prior_transaction(graph_draft):
    records, scope, _ = graph_draft
    authority, scope, ref = product_experience(graph_draft)
    first = authority.snapshot(scope, [ref])
    ancestor = first['nodes'][0]['dependency_revisions']['current_source_graph']['nodes'][0]
    authority.set_policy(scope, ancestor['type'], ancestor['id'], ancestor['source_revision'], 0, [])
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, first)
    with pytest.raises(RecognitionConflict):
        authority.require(authority.snapshot(scope, [ref]), 'generation')
