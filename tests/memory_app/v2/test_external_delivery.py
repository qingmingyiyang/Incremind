"""交付资格只沿真实 Kernel、交付事实和当前材料证明读取。"""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from backend.memory_app.v2.external_context import ExternalContext, ExternalContextError, DELIVERIES
from backend.memory_app.v2.privacy import set_private_project
from backend.shared.secret_detection import contains_secret
from core.ai_kernel import ScopedCapabilityRegistry
from tests.memory_app.v2.test_external_context import (env, prepare, TURN,
    formal_snapshot, settings)


def qualified(api):
    return api.qualified_delivery(TURN)


def completed_without_projection(env):
    api, runtime, runner, frozen = prepare(env)
    runner.accept_and_submit(frozen)
    receipt = runner.wait_for_terminal(TURN, timeout_seconds=125)
    assert receipt.status == 'completed'
    assert env.records.read(DELIVERIES,TURN) is None
    return api, runtime, runner, frozen


def test_real_qualified_delivery_is_detached_and_replay_has_zero_writes(env):
    api, runtime, runner, frozen = prepare(env)
    handoff = api.execute(TURN,runtime=runtime,runner=runner)
    before = formal_snapshot(env)
    result = qualified(api)
    delivery = env.records.read(DELIVERIES,TURN)
    assert set(result) == {'owner_id','turn_id','immutable_ref','outcome_ref',
        'client','project_id','policy_versions','privacy','mapping','refs','handoff'}
    assert result['owner_id'] == 'local-user' and result['turn_id'] == TURN
    assert result['immutable_ref'] == delivery.payload['immutable_ref']
    assert result['outcome_ref'] == delivery.payload['outcome_ref']
    assert result['client'] == 'codex' and result['project_id'] == 'alpha'
    assert result['policy_versions'] == frozen['policy_versions']
    assert result['privacy'] == frozen['privacy'] and result['refs'] == frozen['input']['refs']
    assert result['handoff'] == handoff
    assert set(result['mapping']) == {'M1'}
    untouched = deepcopy(result)
    result['handoff']['entries'][0]['excerpt'] = 'mutated'
    result['privacy']['material_refs'].clear()
    result['mapping'].clear()
    result['refs'].clear()
    assert qualified(api) == untouched
    assert formal_snapshot(env) == before


def test_completed_kernel_without_execute_delivery_is_rejected(env):
    api, *_ = completed_without_projection(env)
    before = formal_snapshot(env)
    with pytest.raises(ExternalContextError,match='^external_context_not_delivered$'):
        qualified(api)
    assert formal_snapshot(env) == before


def test_prepared_but_incomplete_kernel_is_rejected(env):
    api, *_ = prepare(env)
    before = formal_snapshot(env)
    with pytest.raises(ExternalContextError,match='^external_context_not_completed$'):
        qualified(api)
    assert formal_snapshot(env) == before


@pytest.mark.parametrize('field,value', [('owner_id','another-user'),('turn_id','another-turn'),
    ('immutable_ref','crp://default/forged'),('outcome_ref','crp://default/forged'),
    ('at','malformed'),('at',True),('extra','unknown')])
def test_false_first_revision_delivery_cannot_replace_real_completion_proof(env,field,value):
    api, *_ = completed_without_projection(env)
    ref, _archive, outcome = api._completed(TURN)
    payload = {'owner_id':'local-user','turn_id':TURN,'immutable_ref':ref,
        'outcome_ref':outcome,'at':datetime.now(timezone.utc).isoformat()}
    payload[field] = value
    with env.records.begin() as tx:
        tx.put(DELIVERIES,TURN,payload,expected_revision=0)
        tx.commit()
    before = formal_snapshot(env)
    with pytest.raises(ExternalContextError,match='^external_context_not_delivered$'):
        qualified(api)
    assert formal_snapshot(env) == before


def test_delivery_revision_two_is_not_a_fresh_first_delivery(env):
    api,runtime,runner,_ = prepare(env)
    api.execute(TURN,runtime=runtime,runner=runner)
    with env.records.begin() as tx:
        row = tx.read(DELIVERIES,TURN)
        tx.put(DELIVERIES,TURN,row.payload,expected_revision=row.revision)
        tx.commit()
    assert env.records.read(DELIVERIES,TURN).revision == 2
    with pytest.raises(ExternalContextError,match='^external_context_not_delivered$'):
        qualified(api)


@pytest.mark.parametrize('change', ['private','disabled','source_revision'])
def test_current_guard_revocation_invalidates_completed_delivery(env,change):
    api,runtime,runner,_ = prepare(env)
    api.execute(TURN,runtime=runtime,runner=runner)
    if change == 'private':
        set_private_project(env.records,'alpha',True,0)
    elif change == 'disabled':
        settings(env,allow_remote=False)
    else:
        selection = api._archive(TURN)[1]['selections'][0]
        with env.records.begin() as tx:
            row = tx.read('workspace_items',selection['id'])
            tx.put('workspace_items',selection['id'],{**row.payload,'source_text':'new synthetic text'},
                expected_revision=row.revision)
            tx.commit()
    before = formal_snapshot(env)
    with pytest.raises(ValueError):
        qualified(api)
    assert formal_snapshot(env) == before


def test_other_owner_cannot_adopt_existing_runtime_delivery(env):
    api,runtime,runner,_ = prepare(env)
    api.execute(TURN,runtime=runtime,runner=runner)
    other = ExternalContext(env.records,owner_id='another-user',documents=env.documents)
    other.install(ScopedCapabilityRegistry(),env.http.app.state.ai_turn_store)
    other.bind_runtime(runtime)
    before = formal_snapshot(env)
    with pytest.raises(ExternalContextError,match='^external_context_binding_invalid$'):
        qualified(other)
    assert formal_snapshot(env) == before


def test_returned_handoff_redacts_body_and_excludes_original_query(env):
    secret = 'sk-' + 'S' * 24
    source = env.domains.items.create('alpha','text','合成标题 '+secret,'合成正文 '+secret)
    selection = {'type':'original_item','id':source['id'],'project_id':'alpha',
        'revision':source['revision'],'layer':'L0','windows':[]}
    api,runtime,runner,frozen = prepare(env,selections=[selection],query='仅留在原归档的合成问题')
    original = api.execute(TURN,runtime=runtime,runner=runner)
    assert contains_secret(original['text'])
    before = formal_snapshot(env)
    result = qualified(api)
    assert not contains_secret(json.dumps(result,ensure_ascii=False))
    assert 'request' not in result and 'binding' not in result and 'query' not in result
    assert result['handoff']['entries'][0]['id'] == 'M1'
    assert result['handoff']['text'] == json.dumps({'entries':result['handoff']['entries'],
        'profile':result['handoff']['profile']},ensure_ascii=False,sort_keys=True,separators=(',',':'))
    assert result['handoff']['tokens'] == original['tokens']
    assert api._archive(TURN)[1]['request'] == frozen
    assert formal_snapshot(env) == before


def test_original_sensitive_query_failure_cannot_be_adopted_as_delivery(env):
    secret = 'sk-' + 'S' * 24
    api,runtime,runner,_ = prepare(env,query='本机问题 '+secret)
    with pytest.raises(ExternalContextError,match='^external_context_not_completed$'):
        api.execute(TURN,runtime=runtime,runner=runner)
    assert env.records.read(DELIVERIES,TURN) is None
    with pytest.raises(ExternalContextError,match='^external_context_not_completed$'):
        qualified(api)
