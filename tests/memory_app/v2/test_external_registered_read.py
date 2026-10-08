"""原 recall 交付编号到原 read 子轮次的真实 Source 链；不代签 HTTP/SDK。"""
from copy import deepcopy
import json

from fastapi import HTTPException
import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.external_agent_guard import ExternalAgentGuardError
from backend.memory_app.v2.external_context import ARCHIVE, DELIVERIES, ExternalContextError
from backend.memory_app.v2.mcp_memory import _read_selections
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import WorkScope
from tests.memory_app.v2.test_external_registered_context import (
    NOW, RESERVATIONS, no_factory_or_artifact_capture, registered,
)


def prepare_recall(actual, *, client='codex', budget=3000, suffix='parent'):
    turn = f'turn-recall-{client}-{suffix}'
    call = {'client': client, 'tool': 'recall', 'query': '公开原件正文',
        'scope': {'user_id': 'local-user', 'project_id': 'alpha'}, 'budget': budget}
    frozen = actual.context.prepare_recall(turn, call, session_id=f'session-{turn}',
        operation_id=f'op-{turn}', idempotency_key=turn, created_at=NOW.isoformat())
    return turn, frozen


def delivered_source(actual, *, client='codex'):
    turn, frozen = prepare_recall(actual, client=client)
    delivered = actual.execute(turn)
    actual.assert_completed(turn, frozen, delivered)
    # 编号来自原交付结果，不能预设 M1 代表指定原件。
    entry = next(row for row in delivered['entries'] if row['object_id'] == 'public-source')
    assert entry['layer'] == 'L0' and entry['excerpt'] == '公开原件正文'
    proof, original = actual.context.delivered_proof(turn, entry['id'], client=client)
    assert proof['material'] == {'type': 'original_source', 'id': 'public-source',
        'revision': 1, 'project_id': 'alpha'}
    SourceEgressService(actual.records).validate_snapshot(WorkScope('local-user', 'alpha'), proof['snapshot'])
    node = next(node for node in proof['snapshot']['nodes'] if node['type'] == 'original_source')
    assert node['incarnation'] == actual.sources.incarnation('sources', 'public-source')
    assert original == frozen['capability_request']['arguments']
    return turn, entry, proof, original


def parent_facts(actual, turn):
    return (deepcopy(actual.turns.get_request(turn)), deepcopy(actual.turns.events_after(turn)),
        deepcopy(actual.turns.get_immutable_payload(turn, ARCHIVE)), actual.records.read(DELIVERIES, turn))


def prepare_read(actual, parent, number, original, selections, *, suffix='child'):
    child = f'turn-read-{original["client"]}-{suffix}'
    frozen = actual.context.prepare(child, {**original, 'tool': 'read'}, selections,
        session_id=f'session-{child}', operation_id=f'op-{child}', idempotency_key=child,
        created_at=NOW.isoformat(), origin={'turn_id': parent, 'id': number})
    return child, frozen


def assert_child_not_accepted(actual, child):
    assert actual.turns.get_request(child) is None and actual.turns.events_after(child) == ()
    assert actual.turns.get_immutable_payload(child, ARCHIVE) is None
    assert actual.records.read('v2_external_agent_bindings', child) is None
    assert actual.records.read(RESERVATIONS, child) is None and actual.records.read(DELIVERIES, child) is None


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('window', [None, {'start': 2, 'end': 5}])
def test_recall_number_drills_into_new_registered_read_turn(registered, client, window):
    parent, entry, proof, original = delivered_source(registered, client=client)
    before = parent_facts(registered, parent)
    selections = _read_selections(proof, window)
    child, frozen = prepare_read(registered, parent, entry['id'], original, selections)
    delivered = registered.execute(child)
    registered.assert_completed(child, frozen, delivered)
    assert child != parent and frozen['capability_request']['arguments'] == {**original, 'tool': 'read'}
    row = next(row for row in delivered['entries'] if row['object_id'] == 'public-source')
    assert row['layer'] == 'L0' and row['excerpt'] == ('公开原件正文' if window is None else '原件正')
    child_ref, archive = registered.context._archive(child)
    parent_ref, _, parent_outcome = registered.context._completed(parent)
    assert archive['schema_version'] == '2.0.0'
    assert archive['origin'] == {'turn_id': parent, 'id': entry['id'],
        'immutable_ref': parent_ref, 'outcome_ref': parent_outcome}
    assert archive['selections'] == selections and registered.records.read(DELIVERIES, child).payload['immutable_ref'] == child_ref
    child_proof, child_arguments = registered.context.delivered_proof(child, row['id'], client=client)
    assert child_proof['material'] == proof['material'] and child_arguments['scope'] == original['scope']
    parent_nodes = {(node['type'], node['id']): node for node in proof['snapshot']['nodes']}
    for node in child_proof['snapshot']['nodes']:
        assert node == parent_nodes[node['type'], node['id']]
    assert parent_facts(registered, parent) == before
    assert len(registered.records.list(RESERVATIONS)) == len(registered.records.list(DELIVERIES)) == 2
    assert registered.records.list('v2_usage_document') == registered.records.list('v2_usage_insight') == ()


@pytest.mark.parametrize('invalid', ['client', 'number', 'turn'])
def test_delivered_source_number_requires_exact_parent_and_client(registered, invalid):
    parent, entry, _, _ = delivered_source(registered)
    before = parent_facts(registered, parent)
    turn = 'turn-never-accepted' if invalid == 'turn' else parent
    number = 'M999' if invalid == 'number' else entry['id']
    client = 'claude' if invalid == 'client' else 'codex'
    expected = 'external_context_binding_invalid' if invalid == 'turn' else 'external_context_citations_invalid'
    with pytest.raises(ExternalContextError, match=expected):
        registered.context.delivered_proof(turn, number, client=client)
    assert parent_facts(registered, parent) == before
    assert_child_not_accepted(registered, 'turn-read-codex-child')
    assert len(registered.records.list(DELIVERIES)) == 1


@pytest.mark.parametrize('invalid', ['scope', 'closure'])
def test_read_origin_cannot_extend_the_delivered_scope_or_closure(registered, invalid):
    parent, entry, proof, original = delivered_source(registered)
    before = parent_facts(registered, parent)
    selections = _read_selections(proof, None)
    if invalid == 'scope':
        original = {**original, 'scope': {**original['scope'], 'project_id': 'foreign-project'}}
    else:
        registered.add_source('outside-parent', 'alpha', '真实原 owner 的闭包外原件')
        selections = [{**selections[0], 'id': 'outside-parent'}]
    with pytest.raises((ExternalContextError, ExternalAgentGuardError)):
        prepare_read(registered, parent, entry['id'], original, selections)
    assert_child_not_accepted(registered, 'turn-read-codex-child')
    assert parent_facts(registered, parent) == before and len(registered.records.list(DELIVERIES)) == 1


def test_read_origin_cannot_use_an_accepted_but_uncompleted_parent(registered):
    parent, frozen = prepare_recall(registered)
    assert registered.turns.events_after(parent)[-1]['type'] == 'turn.accepted'
    _, archive = registered.context._archive(parent)
    assert registered.records.read(DELIVERIES, parent) is None
    number = next(number for number, proof in archive['mapping'].items()
        if proof['material']['id'] == 'public-source')
    with pytest.raises(ExternalContextError, match='external_context_not_completed'):
        registered.context.delivered_proof(parent, number, client='codex')
    with pytest.raises(ExternalContextError, match='external_context_not_completed'):
        prepare_read(registered, parent, number, frozen['capability_request']['arguments'], archive['selections'])
    assert_child_not_accepted(registered, 'turn-read-codex-child')
    assert registered.records.list(RESERVATIONS) == registered.records.list(DELIVERIES) == ()


@pytest.mark.parametrize('change', ['off', 'private', 'revision', 'incarnation'])
def test_read_rechecks_parent_qualification_after_proof_lookup(registered, change):
    parent, entry, proof, original = delivered_source(registered)
    before = parent_facts(registered, parent)
    selections = _read_selections(proof, None)
    old_incarnation = registered.sources.incarnation('sources', 'public-source')
    if change == 'off':
        registered.configure(allow_remote=False)
    elif change == 'private':
        set_private_project(registered.records, 'alpha', True, 0)
    else:
        source = registered.sources.read('sources', 'public-source')
        if change == 'revision':
            source = {**source, 'metadata': {**source['metadata'], 'content_snapshot': '实际原件的新修订'}}
            registered.sources.write('sources', 'public-source', source, expected_revision=1)
        else:
            registered.sources.delete('sources', 'public-source')
            registered.sources.write('sources', 'public-source', source, expected_revision=0)
            assert registered.sources.revision('sources', 'public-source') == 1
            assert registered.sources.incarnation('sources', 'public-source') != old_incarnation
    expected = {'off': 'external_agent_disabled', 'private': 'external_agent_private',
        'revision': 'external_agent_binding_invalid', 'incarnation': 'external_agent_binding_invalid'}[change]
    with pytest.raises(ExternalAgentGuardError, match=expected):
        registered.context.delivered_proof(parent, entry['id'], client='codex')
    # 复用之前真实 proof 构造请求；原 prepare 自己重验父资格，不信任旧读取。
    with pytest.raises(ExternalAgentGuardError, match=expected):
        prepare_read(registered, parent, entry['id'], original, selections)
    assert_child_not_accepted(registered, 'turn-read-codex-child')
    assert parent_facts(registered, parent) == before and len(registered.records.list(DELIVERIES)) == 1


@pytest.mark.parametrize('window', [{'start': False, 'end': 2}, {'start': 0, 'end': 7}])
def test_read_window_keeps_original_integer_and_source_bounds(registered, window):
    parent, entry, proof, original = delivered_source(registered)
    before = parent_facts(registered, parent)
    if type(window['start']) is bool:
        with pytest.raises(HTTPException) as caught:
            _read_selections(proof, window)
        assert caught.value.status_code == 400 and caught.value.detail == 'external_agent_window_invalid'
    else:
        selections = _read_selections(proof, window)
        with pytest.raises(ExternalContextError, match='external_context_selection_invalid'):
            prepare_read(registered, parent, entry['id'], original, selections)
    assert_child_not_accepted(registered, 'turn-read-codex-child')
    assert parent_facts(registered, parent) == before and len(registered.records.list(DELIVERIES)) == 1


def test_recall_empty_budget_does_not_invent_a_delivered_number(registered):
    turn, frozen = prepare_recall(registered, budget=1)
    delivered = registered.execute(turn)
    registered.assert_completed(turn, frozen, delivered)
    assert delivered['entries'] == [] and delivered['text'] == '' and delivered['tokens'] == 0
    assert registered.context._archive(turn)[1]['mapping'] == {}
    with pytest.raises(ExternalContextError, match='external_context_citations_invalid'):
        registered.context.delivered_proof(turn, 'M1', client='codex')
    assert_child_not_accepted(registered, 'turn-read-codex-child')


def test_l3_delivery_without_original_roots_refuses_read_selection(registered):
    parent, frozen = registered.prepare('methods')
    delivered = registered.execute(parent)
    registered.assert_completed(parent, frozen, delivered)
    entry = next(row for row in delivered['entries'] if row['object_id'] == registered.method.id)
    proof, _ = registered.context.delivered_proof(parent, entry['id'], client='codex')
    assert entry['layer'] == 'L3'
    assert not any(node['type'] in {'original_item', 'original_source'} for node in proof['snapshot']['nodes'])
    with pytest.raises(ExternalContextError, match='external_context_material_changed'):
        _read_selections(proof, None)
    assert_child_not_accepted(registered, 'turn-read-codex-child')
    assert len(registered.records.list(DELIVERIES)) == 1
