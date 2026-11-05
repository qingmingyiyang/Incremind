from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from core.ai_kernel.product_admission import ProductAdmissionItem, seal_admission, admission_identity, MANIFEST_KIND
from core.ai_kernel.product_snapshot import admission_base_refs, restore_admission_snapshot, read_admission_base_refs
from core.ai_kernel.sqlite_store import SQLiteAITurnStore


def accepted(tmp_path):
    path = tmp_path / 'native.sqlite3'
    store = SQLiteAITurnStore(path)
    request = json.loads((Path(__file__).parents[2] / 'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    identity = request['turn_id']
    base = [{'kind':'source','object_id':'untouched-ref','uri':'crp://default/workspace/untouched-ref'},
        {'kind':'session_event','object_id':identity,'uri':f'crp://default/v2_part_contexts/{identity}'}]
    request['input']['refs'] = deepcopy(base)
    item = ProductAdmissionItem('project-constraints-v1',
        {'kind':'session_event','object_id':identity,'uri':f'crp://default/project-constraints/{identity}'},
        {'turn_id':identity,'project_id':request['scope']['project_id'],'owner_user_id':'local-user',
         'namespace_id':'default','origin_kind':'personal'},{'turn_id':identity,'items':[]})
    bundle = seal_admission(request,(item,),store)
    with store.admission_bundle(request,bundle):
        store.claim_turn(request)
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as connection:
        immutable = {kind:(reference,json.loads(raw)) for kind,reference,raw in connection.execute(
            'SELECT kind,payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?',(identity,))}
    return path,store,request,immutable,base


def test_admitted_base_refs_preserve_every_original_ref_and_require_actual_mapping(tmp_path):
    path,store,request,immutable,base = accepted(tmp_path)
    assert admission_base_refs(request,immutable) == base
    assert request['input']['refs'][-1] != base[-1]
    assert admission_base_refs(request,immutable)[-1] == base[-1]
    assert restore_admission_snapshot(request,immutable) == store.restore_admission(request)
    before = path.stat().st_mtime_ns
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        assert read_admission_base_refs(connection,request) == base
        omitted = deepcopy(request)
        omitted['input']['refs'] = deepcopy(base)
        with pytest.raises(ValueError,match='request_changed'):
            read_admission_base_refs(connection,omitted)
    assert path.stat().st_mtime_ns == before
    damaged = deepcopy(immutable)
    item = damaged['project-constraints-v1']
    damaged['project-constraints-v1'] = ('not-the-admitted-ref',item[1])
    with pytest.raises(ValueError,match='item_changed'):
        admission_base_refs(request,damaged)


def test_snapshot_rejects_reordered_admission_and_keeps_legacy_refs_exact(tmp_path):
    _,_,request,immutable,base = accepted(tmp_path)
    changed = deepcopy(request)
    changed['input']['refs'][-2:] = list(reversed(changed['input']['refs'][-2:]))
    forged = deepcopy(immutable)
    manifest = forged[MANIFEST_KIND]
    forged[MANIFEST_KIND] = (manifest[0],{**manifest[1],'request_identity':admission_identity(changed)})
    with pytest.raises(ValueError,match='reference_order_changed'):
        restore_admission_snapshot(changed,forged)
    legacy = deepcopy(request)
    legacy['input']['refs'] = deepcopy(base)
    assert admission_base_refs(legacy,{}) == base
