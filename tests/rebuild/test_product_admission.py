from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from threading import Barrier
from contextvars import copy_context
import asyncio

import pytest

from core.ai_kernel.contracts import AIKernelContractError
from core.ai_kernel.product_admission import (
    MANIFEST_KIND, ProductAdmissionItem, seal_admission, bundle_from_payload, bundle_to_payload,
)
from core.ai_kernel.sqlite_store import SQLiteAITurnStore, TurnStateConflict


def request_for(number):
    request = json.loads((Path(__file__).parents[2] / 'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    request.update(turn_id=f'turn-admission-{number}',operation_id=f'operation-admission-{number}',
        idempotency_key=f'admission-{number}')
    return request


def item_for(request, *, payload=None):
    identity = request['turn_id']
    return ProductAdmissionItem('project-constraints-v1',
        {'kind':'session_event','object_id':identity,'uri':f'crp://default/project-constraints/{identity}'},
        {'turn_id':identity,'project_id':request['scope']['project_id'],'owner_user_id':'local-user',
         'namespace_id':'default','origin_kind':'personal'},
        {'turn_id':identity,'version':1,'constraints':[]} if payload is None else payload)


def test_manifest_requires_registered_bundle_and_all_capsules_commit_together(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    request = request_for(1)
    bundle = seal_admission(request,(item_for(request),),store)
    with pytest.raises(TurnStateConflict):
        store.claim_turn(request)
    assert store.get_request(request['turn_id']) is None
    with store.admission_bundle(request,bundle):
        assert store.claim_turn(request) == (request['turn_id'],True)
    manifest = store.get_immutable_payload(request['turn_id'],MANIFEST_KIND)
    item = store.get_immutable_payload(request['turn_id'],'project-constraints-v1')
    assert manifest[1]['request_identity'] == bundle.manifest['request_identity']
    assert [{key:value for key,value in row.items() if key != 'payload_ref'} for row in manifest[1]['items']] == bundle.manifest['items']
    assert item[0] == manifest[1]['items'][0]['payload_ref']
    assert item[1] == bundle.items[0].payload
    assert manifest[1]['items'][0]['ref']['uri'] != item[0]
    assert store.claim_turn(request) == (request['turn_id'],False)


def test_invalid_capsule_rolls_back_turn_and_all_immutable_rows(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    request = request_for(2)
    binding = ProductAdmissionItem('workbench-steer-binding-v1',
        {'kind':'session_event','object_id':request['turn_id'],
         'uri':store.immutable_payload_reference(request['turn_id'],'workbench-steer-binding-v1')},
        item_for(request).scope,{'turn_id':request['turn_id']})
    invalid = item_for(request,payload={'api_key':'must-not-persist'})
    bundle = seal_admission(request,(binding,invalid),store)
    with pytest.raises(AIKernelContractError):
        with store.admission_bundle(request,bundle):
            store.claim_turn(request)
    assert store.get_request(request['turn_id']) is None
    for kind in (MANIFEST_KIND,'workbench-steer-binding-v1','project-constraints-v1'):
        assert store.get_immutable_payload(request['turn_id'],kind) is None


def test_replay_rejects_changed_bundle_body_and_does_not_backfill_old_turn(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    request = request_for(3)
    bundle = seal_admission(request,(item_for(request),),store)
    restored = bundle_from_payload(bundle_to_payload(bundle))
    with store.admission_bundle(request,restored):
        store.claim_turn(request)
    changed = replace(bundle,items=(replace(bundle.items[0],payload={'version':2}),))
    with pytest.raises(TurnStateConflict):
        with store.admission_bundle(request,changed):
            store.claim_turn(request)
    original = request_for(4)
    store.claim_turn(original)
    sealed = deepcopy(original)
    later = seal_admission(sealed,(item_for(sealed),),store)
    with pytest.raises(TurnStateConflict):
        with store.admission_bundle(sealed,later):
            store.claim_turn(sealed)
    assert store.get_request(original['turn_id']) == original
    assert store.get_immutable_payload(original['turn_id'],MANIFEST_KIND) is None


def test_concurrent_bundles_remain_bound_to_their_actual_turns(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    barrier = Barrier(2)
    def accept(number):
        request = request_for(number)
        bundle = seal_admission(request,(item_for(request),),store)
        with store.admission_bundle(request,bundle):
            barrier.wait(timeout=5)
            store.claim_turn(request)
        return request,bundle
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept,(5,6)))
    for request,bundle in results:
        assert store.restore_admission(request) == bundle
        assert store.get_immutable_payload(request['turn_id'],'project-constraints-v1')[1]['turn_id'] == request['turn_id']


def test_unknown_and_duplicate_kinds_refs_and_nested_context_are_rejected(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    for items in ('unknown','duplicate_kind','duplicate_ref'):
        request = request_for(items)
        item = item_for(request)
        values = (replace(item,kind='unregistered-v1'),) if items == 'unknown' else (
            item,item if items == 'duplicate_kind' else replace(item,kind='workbench-steer-binding-v1'))
        with pytest.raises(ValueError):
            seal_admission(request,values,store)
        assert request['input']['refs'] == []
    request = request_for('nested')
    bundle = seal_admission(request,(item_for(request),),store)
    with store.admission_bundle(request,bundle):
        with pytest.raises(TurnStateConflict):
            with store.admission_bundle(request,bundle):
                pytest.fail('nested admission must not replace its owner')


def test_two_connections_same_key_different_capsules_accept_exactly_one(tmp_path):
    path = tmp_path / 'turns.sqlite3'
    stores = (SQLiteAITurnStore(path),SQLiteAITurnStore(path))
    barrier = Barrier(2)
    def accept(number):
        store = stores[number]
        request = request_for('same-key')
        bundle = seal_admission(request,(item_for(request,payload={'version':number}),),store)
        with store.admission_bundle(request,bundle):
            barrier.wait(timeout=5)
            try:
                return store.claim_turn(request)[1]
            except TurnStateConflict:
                return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(accept,(0,1)))
    assert values.count(True) == 1
    assert values.count('conflict') == 1
    winner = values.index(True)
    assert stores[0].get_immutable_payload('turn-admission-same-key','project-constraints-v1')[1] == {'version':winner}


def test_to_thread_context_propagation_and_expired_copied_context_rejection(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    request = request_for('thread')
    bundle = seal_admission(request,(item_for(request),),store)
    async def accept():
        with store.admission_bundle(request,bundle):
            stale = copy_context()
            assert await asyncio.to_thread(store.claim_turn,request) == (request['turn_id'],True)
        return stale
    stale = asyncio.run(accept())
    with pytest.raises(TurnStateConflict):
        stale.run(store.claim_turn,request)
    assert store.claim_turn(request) == (request['turn_id'],False)


def test_wrong_store_cannot_borrow_context_and_restart_restores_only_native_capsules(tmp_path):
    path = tmp_path / 'turns.sqlite3'
    store = SQLiteAITurnStore(path)
    other = SQLiteAITurnStore(tmp_path / 'other.sqlite3')
    request = request_for('restart')
    bundle = seal_admission(request,(item_for(request),),store)
    with store.admission_bundle(request,bundle):
        with pytest.raises(TurnStateConflict):
            other.claim_turn(request)
        store.claim_turn(request)
    assert other.get_request(request['turn_id']) is None
    reopened = SQLiteAITurnStore(path)
    restored = reopened.restore_admission(request)
    assert restored == bundle
    with reopened.admission_bundle(request,restored):
        assert reopened.claim_turn(request) == (request['turn_id'],False)


def test_final_input_and_manifest_reference_cannot_change_after_sealing(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    request = request_for('final-input')
    bundle = seal_admission(request,(item_for(request),),store)
    changed = deepcopy(request)
    changed['input']['text'] += '修改'
    with pytest.raises(ValueError):
        with store.admission_bundle(changed,bundle):
            pytest.fail('changed final input must be refused')
    duplicated = deepcopy(request)
    duplicated['input']['refs'].append(duplicated['input']['refs'][-1])
    corrupted = replace(bundle,manifest={**bundle.manifest,'request_identity':{
        **bundle.manifest['request_identity'],'input':duplicated['input']}})
    with pytest.raises(ValueError):
        with store.admission_bundle(duplicated,corrupted):
            pytest.fail('duplicate manifest reference must be refused')
    assert store.get_request(request['turn_id']) is None
