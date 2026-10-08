"""Leaf admission uses real domain objects, source closure and SQLite CAS."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
import sqlite3
import sys
from threading import Barrier, RLock
from types import SimpleNamespace

import pytest

from backend.memory_app.original_sources import source_store
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.v2.external_agent_settings import (
    external_agent_settings, replace_external_agent_settings,
)
from backend.memory_app.v2.privacy import external_egress_allowed, set_private_project
from backend.memory_app.workspace_items import WorkspaceItems
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import SQLiteStructuredRecordStore


OWNER = 'local-user'
DAY = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def request(**changes):
    return {'client': 'codex', 'tool': 'recall', 'query': '合成问题 😀',
            'scope': {'user_id': OWNER, 'project_id': 'alpha'}, 'budget': 3000, **changes}


def settings(records, **changes):
    current = external_agent_settings(records)
    return replace_external_agent_settings(records,
        {key: value for key, value in current.items() if key != 'revision'} | changes,
        expected_revision=current['revision'])


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    items = WorkspaceItems(records, None, RLock())
    item = items.create('alpha', 'text', '真实临时原件', '仅物料正文，不能存进绑定')
    material = {'type': 'original_item', 'id': item['id'], 'project_id': 'alpha', 'revision': 1}
    clock = SimpleNamespace(value=DAY)
    return SimpleNamespace(records=records, items=items, item=item, material=material,
                           clock=clock, root=tmp_path)


def guard(env, *, owner=OWNER, records=None):
    module = importlib.import_module('backend.memory_app.v2.external_agent_guard')
    return module.ExternalAgentGuard(records or env.records, owner_id=owner, now=lambda: env.clock.value)


def allowed(env, **changes):
    settings(env.records, allow_remote=True, **changes)
    return guard(env)


def bindings(env):
    return env.records.list('v2_external_agent_bindings')


def reservations(env):
    return env.records.list('v2_external_agent_reservations')


def quota(env, day=DAY):
    return env.records.read('v2_external_agent_quota_' + day.astimezone(timezone.utc).strftime('%Y%m%d'), OWNER)


def test_default_denial_creates_no_binding_or_quota(env):
    current = external_agent_settings(env.records)
    with pytest.raises(ValueError, match='^external_agent_disabled$'):
        guard(env).freeze('turn-default', request(), [env.material])
    assert current['daily_limit'] == 200 and current['include_profile'] is True
    assert external_agent_settings(env.records) == current
    assert bindings(env) == reservations(env) == () and quota(env) is None


def test_deterministic_body_free_freeze_and_detached_outputs(env):
    api = allowed(env)
    original = env.records.read('workspace_items', env.item['id'])
    first = api.freeze('turn-stable', request(), [env.material])
    row = bindings(env)[0]
    second = guard(env).freeze('turn-stable', request(), [env.material])
    assert encoded(first) == encoded(second) and bindings(env)[0] == row
    assert '仅物料正文' not in encoded(first).decode()
    assert first['material_refs'] == [env.material]
    assert first['request_json'].encode() == encoded(request())
    first['material_refs'][0]['revision'] = 999
    assert guard(env).validate('turn-stable', request())[0]['payload']['source_text'] == original.payload['source_text']
    assert env.records.read('workspace_items', env.item['id']) == original
    assert reservations(env) == () and quota(env) is None


@pytest.mark.parametrize('change', [
    {'client': 'unknown'}, {'tool': 'remember'}, {'tool': 'propose_insight'}, {'tool': 'report_use'},
    {'budget': True}, {'budget': 0}, {'budget': 12001}, {'query': 1}, {'unknown': 'field'},
    {'scope': {'user_id': 'other', 'project_id': 'alpha'}},
    {'scope': {'user_id': OWNER, 'project_id': 'alpha', 'scene': 'unfrozen'}},
])
def test_invalid_request_is_rejected_before_any_binding(env, change):
    api = allowed(env)
    with pytest.raises(ValueError, match='^external_agent_request_invalid$'):
        api.freeze('turn-invalid', request(**change), [env.material])
    assert bindings(env) == reservations(env) == () and quota(env) is None


@pytest.mark.parametrize('tool', ['projects', 'recall', 'methods', 'read'])
def test_each_planned_read_tool_uses_same_leaf_guard(env, tool):
    api = allowed(env)
    value = request(tool=tool)
    api.freeze('turn-' + tool, value, [env.material])
    assert api.validate('turn-' + tool, value)[0]['id'] == env.material['id']


def test_client_denial_and_requested_private_project_fail_closed(env):
    api = allowed(env, clients={'claude': True, 'codex': False})
    with pytest.raises(ValueError, match='^external_agent_client_disabled$'):
        api.freeze('turn-client', request(), [env.material])
    settings(env.records, clients={'claude': True, 'codex': True})
    set_private_project(env.records, 'alpha', True, 0)
    with pytest.raises(ValueError, match='^external_agent_private$'):
        api.freeze('turn-private', request(), [])
    assert bindings(env) == reservations(env) == () and quota(env) is None


def test_private_original_and_actual_document_l0_closure(env):
    api = allowed(env)
    documents = SQLiteDocumentRepository(env.records)
    document = documents.create(DocumentDraft(title='合成整理稿', document_type='note',
        markdown='整理正文', project_id='alpha',
        source_refs=({'source_id': env.item['id'], 'locator': 'workspace://' + env.item['id']},)))
    material = {'type': 'document', 'id': document['id'], 'project_id': 'alpha', 'revision': 1}
    saved = api.freeze('turn-document', request(), [material])
    assert saved['source_snapshots'][0]['roots'] == [
        {'type': 'original_item', 'id': env.item['id'], 'revision': 1}]
    SourceEgressService(env.records).set_policy(WorkScope(OWNER, 'alpha'),
        'original_item', env.item['id'], 1, 0, [])
    for action in (lambda: api.validate('turn-document', request()),
                   lambda: api.freeze('turn-now-private', request(), [material])):
        with pytest.raises(ValueError):
            action()
    assert reservations(env) == () and quota(env) is None


def test_profile_requires_setting_and_same_owner_real_domain_object(env):
    api = allowed(env)
    service = RecognitionService(env.records)
    identity = service.stage_experience(scope=WorkScope(OWNER, 'me'), content='合成画像事实')
    profile = {'type': 'experience', 'id': identity, 'project_id': 'me', 'revision': 1}
    api.freeze('turn-profile', request(), [env.material, profile])
    assert [row['project_id'] for row in api.validate('turn-profile', request())] == ['alpha', 'me']
    settings(env.records, include_profile=False)
    with pytest.raises(ValueError):
        api.validate('turn-profile', request())
    with pytest.raises(ValueError, match='^external_agent_profile_disabled$'):
        api.freeze('turn-no-profile', request(), [profile])
    assert len(bindings(env)) == 1 and reservations(env) == ()


def test_private_profile_and_foreign_owner_material_are_rejected(env):
    api = allowed(env)
    service = RecognitionService(env.records)
    foreign = service.stage_experience(scope=WorkScope('other', 'alpha'), content='他人临时事实')
    with pytest.raises(ValueError):
        api.freeze('turn-foreign', request(), [
            {'type': 'experience', 'id': foreign, 'project_id': 'alpha', 'revision': 1}])
    profile = service.stage_experience(scope=WorkScope(OWNER, 'me'), content='私密画像')
    set_private_project(env.records, 'me', True, 0)
    with pytest.raises(ValueError, match='^external_agent_private$'):
        api.freeze('turn-private-me', request(), [
            {'type': 'experience', 'id': profile, 'project_id': 'me', 'revision': 1}])
    assert bindings(env) == reservations(env) == ()


@pytest.mark.parametrize('change', ['settings', 'privacy_epoch', 'object', 'l0', 'deleted'])
def test_delivery_revalidation_rejects_authority_drift_without_quota(env, change):
    api = allowed(env)
    material = env.material
    if change == 'l0':
        documents = SQLiteDocumentRepository(env.records)
        document = documents.create(DocumentDraft(title='稿', document_type='note', markdown='完整稿',
            project_id='alpha', source_refs=({'source_id': env.item['id'],
                'locator': 'workspace://' + env.item['id']},)))
        material = {'type': 'document', 'id': document['id'], 'project_id': 'alpha', 'revision': 1}
    frozen = api.freeze('turn-drift', request(), [material])
    if change == 'settings':
        settings(env.records, allow_remote=False)
    elif change == 'privacy_epoch':
        set_private_project(env.records, 'unrelated', True, 0)
    else:
        with env.records.begin() as tx:
            row = tx.read('workspace_items', env.item['id'])
            if change == 'deleted':
                tx.delete('workspace_items', row.object_id, expected_revision=row.revision)
            else:
                tx.put('workspace_items', row.object_id, {**row.payload, 'source_text': '变更后的原文'},
                       expected_revision=row.revision)
            tx.commit()
    for action in (lambda: api.validate('turn-drift', request()),
                   lambda: api.reserve('turn-drift', request())):
        with pytest.raises(ValueError):
            action()
    assert bindings(env)[0].payload == frozen
    assert reservations(env) == () and quota(env) is None


def test_original_json_delete_recreate_with_same_revision_is_not_old_material(env):
    api = allowed(env)
    store = source_store(env.records)
    body = {'id': 'json-source', 'project_id': 'alpha', 'title': '原件', 'type': 'text',
            'metadata': {'content_snapshot': '原始正文'}}
    store.write('sources', 'json-source', body, expected_revision=0)
    material = {'type': 'original_source', 'id': 'json-source', 'project_id': 'alpha', 'revision': 1}
    saved = api.freeze('turn-json', request(), [material])
    store.delete('sources', 'json-source')
    store.write('sources', 'json-source', body, expected_revision=0)
    assert store.revision('sources', 'json-source') == 1
    with pytest.raises(ValueError):
        api.reserve('turn-json', request())
    assert bindings(env)[0].payload == saved and quota(env) is None


def test_same_turn_cannot_change_request_owner_or_materials(env):
    api = allowed(env)
    api.freeze('turn-bound', request(), [env.material])
    other = env.items.create('alpha', 'text', '其他原件', '不同正文')
    descriptor = {**env.material, 'id': other['id']}
    attempts = [lambda: api.freeze('turn-bound', request(query='变了'), [env.material]),
        lambda: api.freeze('turn-bound', request(), [descriptor]),
        lambda: api.reserve('turn-bound', request(query='变了')),
        lambda: guard(env, owner='other').reserve('turn-bound', request(
            scope={'user_id': 'other', 'project_id': 'alpha'}))]
    for action in attempts:
        with pytest.raises(ValueError):
            action()
    assert reservations(env) == () and quota(env) is None


@pytest.mark.parametrize('corruption', ['owner', 'request_json', 'snapshot', 'material'])
def test_corrupt_binding_is_rejected_without_creating_a_reservation(env, corruption):
    api = allowed(env)
    api.freeze('turn-corrupt', request(), [env.material])
    row = bindings(env)[0]
    damaged = deepcopy(dict(row.payload))
    if corruption == 'owner':
        damaged['owner_id'] = 'other'
    elif corruption == 'request_json':
        damaged['request_json'] = encoded(request(query='篡改')).decode()
    elif corruption == 'snapshot':
        damaged['source_snapshots'][0]['nodes'] = []
    else:
        damaged['material_refs'][0]['revision'] = True
    with env.records.begin() as tx:
        tx.put(row.collection, row.object_id, damaged, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(ValueError):
        api.reserve('turn-corrupt', request())
    assert reservations(env) == () and quota(env) is None


def test_default_two_hundred_limit_replay_and_restart(env):
    api = allowed(env)
    for n in range(201):
        api.freeze('turn-quota-' + str(n), request(), [])
    for n in range(200):
        api.reserve('turn-quota-' + str(n), request())
    assert quota(env).payload['count'] == 200 and len(reservations(env)) == 200
    api.reserve('turn-quota-0', request())
    restarted = guard(env, records=SQLiteStructuredRecordStore(env.records.database_path))
    restarted.reserve('turn-quota-0', request())
    with pytest.raises(ValueError, match='^external_agent_quota_exhausted$'):
        restarted.reserve('turn-quota-200', request())
    assert quota(env).payload['count'] == 200 and len(reservations(env)) == 200


def test_actual_two_instance_competition_never_exceeds_limit(env):
    api = allowed(env, daily_limit=1)
    for n in (1, 2):
        api.freeze('turn-race-' + str(n), request(), [env.material])
    barrier = Barrier(2)
    def run(n):
        other = guard(env, records=SQLiteStructuredRecordStore(env.records.database_path))
        barrier.wait(timeout=5)
        try:
            other.reserve('turn-race-' + str(n), request())
            return 'reserved'
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(run, (1, 2)))
    assert sorted(values) == ['external_agent_quota_exhausted', 'reserved']
    assert quota(env).payload['count'] == 1 and len(reservations(env)) == 1


def test_quota_and_turn_reservation_rollback_together_on_real_sqlite_abort(env):
    api = allowed(env)
    frozen = api.freeze('turn-rollback', request(), [env.material])
    with sqlite3.connect(env.records.database_path) as db:
        db.execute("CREATE TRIGGER reject_external_reservation BEFORE INSERT ON crp_structured_records "
                   "WHEN NEW.collection = 'v2_external_agent_reservations' "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic reservation failure'); END")
    with pytest.raises(ValueError):
        api.reserve('turn-rollback', request())
    assert quota(env) is None and reservations(env) == () and bindings(env)[0].payload == frozen
    with sqlite3.connect(env.records.database_path) as db:
        db.execute('DROP TRIGGER reject_external_reservation')
    api.reserve('turn-rollback', request())
    assert quota(env).payload['count'] == 1 and len(reservations(env)) == 1


def test_utc_day_rollover_does_not_recharge_old_turn_and_preserves_prior_day(env):
    api = allowed(env, daily_limit=1)
    api.freeze('turn-old-day', request(), [])
    first = api.reserve('turn-old-day', request())
    env.clock.value = DAY + timedelta(days=1)
    assert api.reserve('turn-old-day', request()) == first and quota(env, env.clock.value) is None
    api.freeze('turn-new-day', request(), [])
    api.reserve('turn-new-day', request())
    assert quota(env).payload['count'] == quota(env, env.clock.value).payload['count'] == 1
    env.clock.value = DAY
    api.freeze('turn-backward-day', request(), [])
    with pytest.raises(ValueError, match='^external_agent_quota_exhausted$'):
        api.reserve('turn-backward-day', request())


@pytest.mark.parametrize('value', [DAY.replace(tzinfo=None), '2026-10-05', None])
def test_invalid_clock_cannot_create_quota_or_reservation(env, value):
    api = allowed(env)
    api.freeze('turn-clock', request(), [])
    env.clock.value = value
    with pytest.raises(ValueError, match='^external_agent_clock_invalid$'):
        api.reserve('turn-clock', request())
    assert reservations(env) == () and quota(env) is None


def test_model_settings_and_old_internal_turn_bytes_are_unchanged(env):
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.v2.turn_requests import freeze_product_turn
    from backend.memory_app import privacy_policy, source_egress
    models = ModelConfiguration(env.records, env.root)
    before = models.public()
    assert before['generation']['allow_remote'] is False
    def internal():
        return freeze_product_turn('memory.organize', records=env.records, models=models,
            project_id='alpha', materials=[env.material], load_text=lambda row: row['payload']['source_text'],
            turn_id='turn-' + 'a' * 32, session_id='session-alpha', operation_id='op-external-leaf-check',
            idempotency_key='internal-byte-check', created_at='2026-10-05T00:00:00Z')
    original = encoded(internal())
    purposes = (privacy_policy._PURPOSES, source_egress._PURPOSES)
    api = allowed(env)
    api.freeze('turn-external-model-independent', request(), [env.material])
    api.reserve('turn-external-model-independent', request())
    assert encoded(internal()) == original and models.public() == before
    assert (privacy_policy._PURPOSES, source_egress._PURPOSES) == purposes


@pytest.mark.parametrize('field', [
    'settings_revision', 'privacy_revision', 'source_revision', 'root_revision', 'policy_revision',
])
def test_preserved_revision_boolean_cannot_masquerade_as_frozen_integer(env, field):
    api = allowed(env)
    binding = api.freeze('turn-bool-corrupt', request(), [env.material])
    damaged = deepcopy(binding)
    if field in {'settings_revision', 'privacy_revision'}:
        damaged[field] = bool(damaged[field])
    elif field == 'root_revision':
        damaged['source_snapshots'][0]['roots'][0]['revision'] = True
    else:
        node = damaged['source_snapshots'][0]['nodes'][0]
        node[field] = bool(node[field])
    with sqlite3.connect(env.records.database_path) as db:
        db.execute('UPDATE crp_structured_records SET payload_json=? '
            'WHERE collection=? AND object_id=? AND revision=1',
            (encoded(damaged).decode(), 'v2_external_agent_bindings', 'turn-bool-corrupt'))
    assert bindings(env)[0].revision == 1
    with pytest.raises(ValueError):
        api.reserve('turn-bool-corrupt', request())
    assert reservations(env) == () and quota(env) is None


def test_borrowed_outer_transaction_preserves_qualified_reads_and_whole_rollback(env):
    allowed(env)
    before = env.records.read('workspace_items', env.item['id'])
    with pytest.raises(RuntimeError, match='synthetic outer abort'):
        with env.records.begin() as tx:
            enlisted = guard(env, records=TransactionRecords(tx))
            saved = enlisted.freeze('turn-outer', request(), [env.material])
            assert saved['source_snapshots'][0]['nodes'][0]['source_revision'] == before.revision
            assert enlisted.validate('turn-outer', request())[0]['payload']['source_text'] == before.payload['source_text']
            enlisted.reserve('turn-outer', request())
            assert tx.read('v2_external_agent_quota_20261005', OWNER).payload['count'] == 1
            raise RuntimeError('synthetic outer abort')
    assert bindings(env) == reservations(env) == () and quota(env) is None
    assert env.records.read('workspace_items', env.item['id']) == before


def test_reserved_replay_revalidates_source_revocation_without_recounting(env):
    api = allowed(env)
    api.freeze('turn-reserved', request(), [env.material])
    saved = api.reserve('turn-reserved', request())
    SourceEgressService(env.records).set_policy(WorkScope(OWNER, 'alpha'),
        'original_item', env.item['id'], 1, 0, [])
    with pytest.raises(ValueError):
        guard(env).reserve('turn-reserved', request())
    assert reservations(env)[0].payload == saved and quota(env).payload['count'] == 1


def test_aware_offset_clock_counts_by_actual_utc_day(env):
    api = allowed(env)
    env.clock.value = datetime(2026, 10, 6, 0, 30, tzinfo=timezone(timedelta(hours=8)))
    api.freeze('turn-zone', request(), [])
    saved = api.reserve('turn-zone', request())
    assert saved['day'] == '20261005' and quota(env).payload['count'] == 1
    assert quota(env, DAY + timedelta(days=1)) is None


@pytest.mark.parametrize('project,client', [
    (None, 'codex'), (True, 'codex'), ('bad/path', 'codex'), ('alpha', 'unknown'),
])
def test_central_external_entry_rejects_invalid_scope_or_client(env, project, client):
    allowed(env)
    assert external_egress_allowed(env.records, project, client) is False
    assert bindings(env) == reservations(env) == () and quota(env) is None


@pytest.mark.parametrize('database,namespace', [('records.sqlite3', 'default'),
                                               ('recognition.sqlite3', 'recognition')])
def test_borrowed_json_closure_uses_actual_namespace_and_incarnation(env, database, namespace):
    records = SQLiteStructuredRecordStore(env.root / database)
    settings(records, allow_remote=True)
    store = source_store(records)
    assert store.namespace_id == namespace
    body = {'id': 'outer-json', 'project_id': 'alpha', 'title': 'JSON原件',
            'type': 'text', 'metadata': {'content_snapshot': '完整真实临时原件'}}
    store.write('sources', body['id'], body, expected_revision=0)
    descriptor = {'type': 'original_source', 'id': body['id'], 'project_id': 'alpha', 'revision': 1}
    incarnation = store.incarnation('sources', body['id'])
    with pytest.raises(RuntimeError, match='synthetic outer abort'):
        with records.begin() as tx:
            api = guard(env, records=TransactionRecords(tx))
            frozen = api.freeze('turn-outer-json', request(), [descriptor])
            assert frozen['source_snapshots'][0]['nodes'][0]['incarnation'] == incarnation
            assert api.validate('turn-outer-json', request())[0]['payload']['metadata'] == body['metadata']
            api.reserve('turn-outer-json', request())
            raise RuntimeError('synthetic outer abort')
    assert records.list('v2_external_agent_bindings') == records.list('v2_external_agent_reservations') == ()
    assert records.read('v2_external_agent_quota_20261005', OWNER) is None
    assert store.read('sources', body['id']) == body
    assert store.incarnation('sources', body['id']) == incarnation


def test_quota_owner_keys_are_independent_without_claiming_user_routing(env):
    api = allowed(env, daily_limit=1)
    api.freeze('turn-owner-one', request(), [])
    api.reserve('turn-owner-one', request())
    other = guard(env, owner='other')
    value = request(scope={'user_id': 'other', 'project_id': 'alpha'})
    other.freeze('turn-owner-two', value, [])
    other.reserve('turn-owner-two', value)
    assert quota(env).payload == {'owner_id': OWNER, 'day': '20261005', 'count': 1}
    assert env.records.read('v2_external_agent_quota_20261005', 'other').payload == {
        'owner_id': 'other', 'day': '20261005', 'count': 1}
    with pytest.raises(ValueError):
        other.reserve('turn-owner-one', value)
    assert len(reservations(env)) == 2


def test_current_rejects_actual_json_incarnation_change_between_resolve_and_snapshot(env):
    api = allowed(env)
    store = source_store(env.records)
    body_a = {'id': 'io-gap-source', 'project_id': 'alpha', 'title': 'A代原件',
              'type': 'text', 'metadata': {'content_snapshot': '完整A代正文'}}
    body_b = {**body_a, 'title': 'B代重建原件', 'metadata': {'content_snapshot': '完整B代正文'}}
    store.write('sources', body_a['id'], body_a, expected_revision=0)
    material = {'type': 'original_source', 'id': body_a['id'], 'project_id': 'alpha', 'revision': 1}
    incarnation_a = store.incarnation('sources', body_a['id'])
    with api._transaction() as tx:
        normal, resolved = api._current(tx, 'turn-io-same', request(), [material])
    assert resolved[0]['payload']['metadata'] == body_a['metadata']
    assert resolved[0]['payload']['_original_incarnation'] == incarnation_a
    assert normal['source_snapshots'][0]['nodes'][0]['incarnation'] == incarnation_a
    target = os.path.normcase(os.path.abspath(store._object_path('sources', body_a['id'])))
    state = {'active': True, 'reads': 0, 'changed': False, 'incarnation_b': None}
    def observe_open(event, args):
        if not state['active'] or event != 'open' or not isinstance(args[0], (str, os.PathLike)):
            return
        mode = args[1]
        if (not isinstance(mode, str) or 'r' not in mode or any(flag in mode for flag in 'wa+')
                or os.path.normcase(os.path.abspath(args[0])) != target):
            return
        state['reads'] += 1
        if state['reads'] == 2:
            # The actual resolver has already read A. Only the next exact
            # payload read, inside SourceEgress.snapshot, observes recreated B.
            # Disable first: mutation's own real IO cannot retrigger this hook.
            state['active'] = False
            assert store.delete('sources', body_a['id']) is True
            store.write('sources', body_a['id'], body_b, expected_revision=0)
            state['incarnation_b'] = store.incarnation('sources', body_a['id'])
            state['changed'] = True
    sys.addaudithook(observe_open)
    result, failure = None, None
    try:
        with api._transaction() as tx:
            try:
                result = api._current(tx, 'turn-io-gap', request(), [material])
            except ValueError as error:
                failure = str(error)
    finally:
        # Audit hooks are permanent observers; this one is permanently inert
        # after its single exact temporary-file event or any fixture failure.
        state['active'] = False
    assert state['reads'] == 2 and state['changed'] is True
    assert store.revision('sources', body_a['id']) == 1
    assert state['incarnation_b'] != incarnation_a
    assert store.read('sources', body_a['id']) == body_b
    if result is not None:
        frozen, returned = result
        # Preserve the actual pre-fix mismatch as evidence before the RED
        # assertion: old returned A is paired with a newly valid B snapshot.
        assert returned[0]['payload']['metadata'] == body_a['metadata']
        assert returned[0]['payload']['_original_incarnation'] == incarnation_a
        assert frozen['source_snapshots'][0]['nodes'][0]['incarnation'] == state['incarnation_b']
    assert failure == 'external_agent_material_changed'
    assert bindings(env) == reservations(env) == () and quota(env) is None
