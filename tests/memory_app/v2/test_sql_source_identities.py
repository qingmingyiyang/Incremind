"""Observe real selected domain reads and the original writer transaction."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from importlib import import_module
import inspect
import sqlite3
import sys
from threading import Event

import pytest

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.original_sources import source_store
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.transaction_records import TransactionRecords
from backend.recognition import RecognitionService, WorkScope
from backend.recognition.product_draft_dependencies import product_draft_source
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.record_lineage import FACTS, HEAD_COLLECTIONS, WITNESS_COLLECTIONS, verify_lineage
from tests.memory_app.v2.test_external_context import (
    ARCHIVE, DAY, DELIVERIES, TURN, document, encoded, env, material, prepare, request, settings, setup,
)
from tests.memory_app.v2.test_product_draft_dependencies import draft
from tests.recognition.test_artifact_dependencies import env as artifact_env, _root, _retain


KIND = 'external-context-sql-identities-v1'
ADDITIONAL = (
    'workspace_confirmation_operations', 'workspace_review_intents', 'v2_experience_origins',
    'recognition_tasks', 'recognition_context_packets', 'v2_task_draft_operations',
    'v2_task_executions', 'v2_turns', 'v2_external_input_dependencies',
    'v2_research_source_reads', 'v2_external_agent_deliveries',
)


def api():
    return import_module('backend.recognition.sql_source_identities')


def saved(env, identity=TURN):
    store = env.http.app.state.ai_turn_store
    archive_ref, archive = store.get_immutable_payload(identity, ARCHIVE)
    result = store.get_immutable_payload(identity, KIND)
    assert result is not None
    value = result[1]
    assert set(value) == {'schema_version', 'owner_id', 'turn_id', 'immutable_ref', 'entries'}
    assert value['schema_version'] == 1 and value['owner_id'] == 'local-user'
    assert value['turn_id'] == identity and value['immutable_ref'] == archive_ref
    assert set(value['entries']) == set(archive['mapping'])
    for number, entry in value['entries'].items():
        assert set(entry) == {'proof', 'sql_identities'}
        assert encoded(entry['proof']) == encoded(archive['mapping'][number])
        keys = [(row['collection'], row['object_id']) for row in entry['sql_identities']]
        assert keys == sorted(set(keys))
        for row in entry['sql_identities']:
            assert set(row) == {'collection', 'object_id', 'fact_id'}
            verify_lineage(env.records, row)
    return value, archive


def keys(entry):
    return {(row['collection'], row['object_id']) for row in entry['sql_identities']}


def recognition(env, selected, project='alpha'):
    experience = ensure_document_experience(env.documents, env.service, project, selected['id'])[0]
    candidate = env.service.propose(scope=WorkScope('local-user', project), content='完整认识正文',
        conditions=['保留适用条件'], source_experience_ids=[experience])
    value = env.service.publish(scope=WorkScope('local-user', project), candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer='local-user')
    return {'type': 'recognition', 'id': value.id, 'revision': value.revision,
        'project_id': project, 'layer': 'L3', 'windows': []}, experience


def test_real_layers_and_profile_capture_each_shared_root_closure(env):
    selected, original = document(env)
    insight, experience = recognition(env, selected)
    persona, persona_original = document(env, project='me')
    profile, persona_experience = recognition(env, persona, 'me')
    choices = [original, selected, {**selected, 'layer': 'L2'}, insight, profile]
    before = {kind: env.records.list(kind) for kind in ('workspace_items', 'documents', 'recognitions')}
    prepare(env, selections=choices)
    value, archive = saved(env)
    assert list(value['entries']) == ['M1', 'M2', 'M3', 'M4', 'P1']
    assert keys(value['entries']['M1']) == {('workspace_items', original['id'])}
    document_keys = {('documents', selected['id']), ('document_revisions', selected['id'] + '~r2'),
        ('document_markdown', selected['id'] + '~r2'), ('workspace_items', original['id'])}
    assert document_keys <= keys(value['entries']['M2'])
    assert keys(value['entries']['M2']) == keys(value['entries']['M3'])
    assert document_keys | {('recognitions', insight['id']), ('recognition_experiences', experience)} <= keys(value['entries']['M4'])
    assert {('recognitions', profile['id']), ('recognition_experiences', persona_experience),
        ('workspace_items', persona_original['id']), ('documents', persona['id'])} <= keys(value['entries']['P1'])
    assert ('workspace_items', original['id']) not in keys(value['entries']['P1'])
    assert all(env.records.list(kind) == rows for kind, rows in before.items())
    assert archive['schema_version'] == '1.0.0'


def remove_lineage(records, collection, identity):
    # A synthetic pre-feature database: remove only its test identity metadata,
    # never invoke a replacement owner or reinterpret today's UUID as birth.
    head = records.read(HEAD_COLLECTIONS[collection], identity)
    with sqlite3.connect(records.database_path) as connection:
        for kind in (HEAD_COLLECTIONS[collection], WITNESS_COLLECTIONS[collection]):
            connection.execute('DELETE FROM crp_structured_records WHERE collection=? AND object_id=?', (kind, identity))
        connection.execute('DELETE FROM crp_structured_records WHERE collection=? AND object_id=?',
            (FACTS, head.payload['fact_id']))


def test_budget_discarded_material_is_not_captured_or_legacy_anchored(env):
    selected, rejected = material(env), material(env)
    remove_lineage(env.records, 'workspace_items', rejected['id'])
    one = prepare(env, identity='turn-budget-one', selections=[selected])[3]
    store = env.http.app.state.ai_turn_store
    cost = store.get_immutable_payload(one['turn_id'], ARCHIVE)[1]['handoff']['tokens']
    prepare(env, selections=[selected, rejected], budget=cost)
    value, _ = saved(env)
    assert list(value['entries']) == ['M1']
    assert keys(value['entries']['M1']) == {('workspace_items', selected['id'])}
    assert env.records.read(HEAD_COLLECTIONS['workspace_items'], rejected['id']) is None


def test_legacy_material_gets_current_anchor_in_original_prepare_tx(env):
    selected = material(env)
    original = env.records.read('workspace_items', selected['id'])
    remove_lineage(env.records, 'workspace_items', selected['id'])
    prepare(env, selections=[selected])
    value, _ = saved(env)
    identity = value['entries']['M1']['sql_identities'][0]
    assert env.records.read(FACTS, identity['fact_id']).payload == {'schema_version': 1,
        'collection': 'workspace_items', 'object_id': selected['id'], 'origin': 'current_anchor',
        'observed_revision': original.revision}
    assert env.records.read('workspace_items', selected['id']) == original


def test_real_capture_abort_rolls_back_guard_and_all_anchors_before_turn_accept(env):
    selected = material(env)
    remove_lineage(env.records, 'workspace_items', selected['id'])
    api_instance, _, _ = setup(env)
    settings(env, allow_remote=True)
    before = env.records.list_all()
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_sql_capture BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection = 'record_lineage_first_workspace_items' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic capture abort'); END")
    with pytest.raises(ValueError) as failure:
        api_instance.prepare(TURN, request(), [selected], session_id='session-external',
            operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
    assert isinstance(failure.value.__context__, sqlite3.IntegrityError)
    assert 'synthetic capture abort' in str(failure.value.__context__)
    assert env.records.list_all() == before
    assert env.http.app.state.ai_turn_store.get_request(TURN) is None
    assert env.records.list(DELIVERIES) == ()


def test_json_material_collects_real_confirmation_alias_sql_dependencies(env):
    _, original = document(env)
    item = env.records.read('workspace_items', original['id'])
    identity = item.payload['source_id']
    store = source_store(env.records)
    body = store.read('sources', identity)
    assert body['identity_method'] == 'workspace_confirmation'
    selected = {'type': 'original_source', 'id': identity, 'revision': store.revision('sources', identity),
        'project_id': 'alpha', 'layer': 'L0', 'windows': []}
    prepare(env, selections=[selected])
    value, _ = saved(env)
    assert keys(value['entries']['M1']) == {('workspace_items', original['id']),
        ('workspace_confirmation_operations', body['confirmation_operation_id'])}


def test_pure_json_number_still_has_explicit_companion_with_empty_sql_closure(env):
    store = source_store(env.records)
    store.write('sources', 'pure-json', {'id': 'pure-json', 'project_id': 'alpha', 'title': '合成原件',
        'type': 'text', 'metadata': {'content_snapshot': '合成 JSON 正文'}}, expected_revision=0)
    selected = {'type': 'original_source', 'id': 'pure-json', 'revision': 1,
        'project_id': 'alpha', 'layer': 'L0', 'windows': []}
    prepare(env, selections=[selected])
    value, _ = saved(env)
    assert value['entries']['M1']['sql_identities'] == []


def test_immutable_companion_insert_abort_never_returns_a_usable_prepare(env):
    api_instance, _, _ = setup(env)
    settings(env, allow_remote=True)
    selected = material(env)
    with sqlite3.connect(source_store(env.records).root / 'ai-turns.sqlite3') as connection:
        connection.execute("CREATE TRIGGER reject_sql_companion BEFORE INSERT ON ai_turn_immutable_payloads "
            "WHEN NEW.kind = 'external-context-sql-identities-v1' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic immutable abort'); END")
    with pytest.raises(ValueError) as failure:
        api_instance.prepare(TURN, request(), [selected], session_id='session-external',
            operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
    assert isinstance(failure.value.__context__, sqlite3.IntegrityError)
    assert 'synthetic immutable abort' in str(failure.value.__context__)
    assert env.http.app.state.ai_turn_store.get_immutable_payload(TURN, ARCHIVE) is not None
    assert env.http.app.state.ai_turn_store.get_immutable_payload(TURN, KIND) is None
    assert env.records.list(DELIVERIES) == ()


@pytest.mark.parametrize('entry', ('invoke', 'execute', 'prepare_retry'))
def test_failed_companion_turn_cannot_be_consumed_or_reanchored_by_later_entry(env, entry):
    api_instance, runtime, runner = setup(env)
    settings(env, allow_remote=True)
    selected = material(env)
    store = env.http.app.state.ai_turn_store
    with sqlite3.connect(source_store(env.records).root / 'ai-turns.sqlite3') as connection:
        connection.execute("CREATE TRIGGER reject_later_sql_companion BEFORE INSERT ON ai_turn_immutable_payloads "
            "WHEN NEW.kind = 'external-context-sql-identities-v1' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic later immutable abort'); END")
    with pytest.raises(ValueError) as failure:
        api_instance.prepare(TURN, request(), [selected], session_id='session-external',
            operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
    assert isinstance(failure.value.__context__, sqlite3.IntegrityError)
    assert 'synthetic later immutable abort' in str(failure.value.__context__)
    frozen = store.get_request(TURN)
    assert frozen is not None and store.get_immutable_payload(TURN, ARCHIVE) is not None
    assert store.get_immutable_payload(TURN, KIND) is None
    with sqlite3.connect(source_store(env.records).root / 'ai-turns.sqlite3') as connection:
        connection.execute('DROP TRIGGER reject_later_sql_companion')
    before = (tuple(store.events_after(TURN)), env.records.list_all())
    with pytest.raises(ValueError):
        if entry == 'invoke':
            api_instance.invoke({'turn_id': TURN, 'arguments': frozen['capability_request']['arguments'],
                'privacy': frozen['privacy']})
        elif entry == 'execute':
            api_instance.execute(TURN, runtime=runtime, runner=runner)
        else:
            api_instance.prepare(TURN, request(), [selected], session_id='session-external',
                operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
    assert (tuple(store.events_after(TURN)), env.records.list_all()) == before
    assert store.get_immutable_payload(TURN, KIND) is None
    assert env.records.list(DELIVERIES) == env.records.list('v2_external_agent_reservations') == ()
    # The failed old Turn stays vetoed; a newly prepared Turn uses the intact
    # original lifecycle and has its own successfully persisted identities.
    _, _, _, new = prepare(env, identity='turn-after-companion-abort', selections=[selected])
    saved(env, new['turn_id'])
    assert api_instance.execute(new['turn_id'], runtime=runtime, runner=runner)['entries'][0]['id'] == 'M1'
    assert store.get_immutable_payload(TURN, KIND) is None
    assert env.model.calls == 0


def test_equal_sql_and_json_ids_bind_each_body_to_its_own_typed_proof(env):
    selected = material(env)
    store = source_store(env.records)
    store.write('sources', selected['id'], {'id': selected['id'], 'project_id': 'alpha',
        'title': '独立合成 JSON 原件', 'type': 'text',
        'metadata': {'content_snapshot': '独立 JSON 正文，身份与 SQL 原件分开'}}, expected_revision=0)
    same_json = {**selected, 'type': 'original_source'}
    prepare(env, selections=[selected, same_json])
    value, archive = saved(env)
    assert list(value['entries']) == ['M1', 'M2']
    for number, kind in (('M1', 'original_item'), ('M2', 'original_source')):
        proof = archive['mapping'][number]
        assert proof['material']['type'] == kind
        assert proof['snapshot']['roots'] == [{'type': kind, 'id': selected['id'], 'revision': 1}]
        assert value['entries'][number]['proof'] == proof
        entry = next(row for row in archive['handoff']['entries'] if row['id'] == number)
        assert set(entry) == {'id', 'object_id', 'layer', 'title', 'excerpt', 'sources', 'revision', 'conditions'}
        assert entry['sources'][0]['type'] == kind
    assert keys(value['entries']['M1']) == {('workspace_items', selected['id'])}
    assert keys(value['entries']['M2']) == set()
    assert archive['handoff']['entries'][0]['excerpt'] == env.records.read('workspace_items', selected['id']).payload['source_text']
    assert archive['handoff']['entries'][1]['excerpt'] == '独立 JSON 正文，身份与 SQL 原件分开'


@contextmanager
def paused_after_original_prepare_precheck(service, selected):
    # Pause an actual owner thread after its original get_request(None) check.
    # No owner, storage read/write, Kernel receipt or permission is replaced.
    original = inspect.unwrap(service.prepare)
    source, first_line = inspect.getsourcelines(original)
    gate_line = first_line + next(index for index, line in enumerate(source)
        if line.strip() == "versions = versions_for_turn('external.context')")
    arrived, release = Event(), Event()

    def trace(frame, event, _):
        if (event == 'line' and frame.f_code is original.__code__ and frame.f_lineno == gate_line):
            arrived.set()
            if not release.wait(timeout=12):
                raise TimeoutError('synthetic prepare trace deadline')
        return trace

    def prepare_actual():
        previous = sys.gettrace()
        sys.settrace(trace)
        try:
            return service.prepare(TURN, request(), [selected], session_id='session-external',
                operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
        finally:
            sys.settrace(previous)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(prepare_actual)
        try:
            assert arrived.wait(timeout=12), 'original prepare did not reach the trace gate'
            yield release, future
        finally:
            release.set()


def test_replayed_atomic_claim_cannot_fill_old_archive_with_recreated_sql_birth(env):
    service, _, _ = setup(env)
    settings(env, allow_remote=True)
    selected = material(env)
    store = env.http.app.state.ai_turn_store
    original = env.records.read('workspace_items', selected['id'])
    first_fact = env.records.read(HEAD_COLLECTIONS['workspace_items'], selected['id']).payload['fact_id']
    with paused_after_original_prepare_precheck(service, selected) as (release, future):
        assert store.get_request(TURN) is None
        with sqlite3.connect(source_store(env.records).root / 'ai-turns.sqlite3') as connection:
            connection.execute("CREATE TRIGGER reject_raced_companion BEFORE INSERT ON ai_turn_immutable_payloads "
                "WHEN NEW.kind = 'external-context-sql-identities-v1' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic raced companion abort'); END")
        with pytest.raises(ValueError) as aborted:
            service.prepare(TURN, request(), [selected], session_id='session-external',
                operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
        assert isinstance(aborted.value.__context__, sqlite3.IntegrityError)
        assert 'synthetic raced companion abort' in str(aborted.value.__context__)
        assert store.get_request(TURN) is not None and store.get_immutable_payload(TURN, ARCHIVE) is not None
        assert store.get_immutable_payload(TURN, KIND) is None
        with env.records.begin() as tx:
            deleted = tx.delete('workspace_items', selected['id'], expected_revision=original.revision)
            replacement = tx.put('workspace_items', selected['id'], original.payload, expected_revision=0)
            assert tx.commit() == (deleted, replacement)
        assert replacement.revision == original.revision == 1 and replacement.payload == original.payload
        fresh_fact = env.records.read(HEAD_COLLECTIONS['workspace_items'], selected['id']).payload['fact_id']
        assert fresh_fact != first_fact
        with sqlite3.connect(source_store(env.records).root / 'ai-turns.sqlite3') as connection:
            connection.execute('DROP TRIGGER reject_raced_companion')
        before = (tuple(store.events_after(TURN)), env.records.list_all())
        release.set()
        with pytest.raises(ValueError, match='^external_context_binding_invalid$'):
            future.result(timeout=12)
        assert store.get_immutable_payload(TURN, KIND) is None
        assert (tuple(store.events_after(TURN)), env.records.list_all()) == before
        assert env.records.list(DELIVERIES) == env.records.list('v2_external_agent_reservations') == ()
    assert env.model.calls == 0


def test_ordinary_concurrent_prepare_replays_an_intact_companion(env):
    service, _, _ = setup(env)
    settings(env, allow_remote=True)
    selected = material(env)
    store = env.http.app.state.ai_turn_store
    with paused_after_original_prepare_precheck(service, selected) as (release, future):
        assert store.get_request(TURN) is None
        frozen = service.prepare(TURN, request(), [selected], session_id='session-external',
            operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
        identities, _ = saved(env)
        before = (tuple(store.events_after(TURN)), env.records.list_all())
        release.set()
        assert future.result(timeout=12) == frozen
        assert saved(env)[0] == identities
        assert (tuple(store.events_after(TURN)), env.records.list_all()) == before
    assert env.records.list(DELIVERIES) == env.records.list('v2_external_agent_reservations') == ()
    assert env.model.calls == 0


def test_completed_turn_prepare_is_still_exactly_idempotent(env):
    selected = material(env)
    service, runtime, runner, frozen = prepare(env, selections=[selected])
    service.execute(TURN, runtime=runtime, runner=runner)
    store = env.http.app.state.ai_turn_store
    identities, _ = saved(env)
    before = (tuple(store.events_after(TURN)), env.records.list_all())
    assert service.prepare(TURN, request(), [selected], session_id='session-external',
        operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat()) == frozen
    assert saved(env)[0] == identities
    assert (tuple(store.events_after(TURN)), env.records.list_all()) == before
    assert env.model.calls == 0


def test_collector_nested_adapter_only_collects_point_reads(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('documents', 'selected', {'id': 'selected'}, expected_revision=0)
        tx.put('documents', 'unselected', {'id': 'unselected'}, expected_revision=0)
        tx.put('v2_external_agent_settings', 'settings', {}, expected_revision=0)
        tx.commit()
    with records.begin() as tx:
        collector = api().CollectingReader(tx)
        assert not isinstance(collector, TransactionRecords) and not hasattr(collector, '_transaction')
        nested = TransactionRecords(TransactionRecords(collector))
        assert len(nested.list('documents')) == 2
        assert collector.selected_keys() == ()
        assert nested.read('documents', 'missing') is None
        nested.read('v2_external_agent_settings', 'settings')
        nested.read('documents', 'selected')
        nested.read('documents', 'selected')
        assert collector.selected_keys() == (('documents', 'selected'),)
        proof = api().capture_identities(tx, collector)
        assert tx.commit() == ()
    assert len(proof) == 1
    verify_lineage(records, proof[0])


def test_actual_product_owner_selects_execution_without_collecting_list(draft):
    records, scope, identity = draft
    with records.begin() as tx:
        tx.put('v2_task_executions', 'unrelated', {'request': {'turn_id': 'other'}}, expected_revision=0)
        tx.commit()
    before = records.list_all()
    with records.begin() as tx:
        collector = api().CollectingReader(tx)
        bound = product_draft_source(collector, scope, identity, 1)
        assert bound.revisions['task_execution_id'] == 'product-test'
        assert ('v2_task_executions', 'product-test') in collector.selected_keys()
        assert ('v2_task_executions', 'unrelated') not in collector.selected_keys()
        assert set(collector.selected_keys()) == {('documents', identity), ('document_revisions', identity + '~r1'),
            ('document_markdown', identity + '~r1'), ('v2_task_draft_operations', 'deliver-turn-test'),
            ('v2_task_executions', 'product-test'), ('v2_turns', 'product-test')}
        identities = api().capture_identities(tx, collector)
        assert tx.commit() == ()
    assert records.list_all() == before
    for value in identities:
        verify_lineage(records, value)


def test_actual_retained_artifact_binds_birth_but_allows_current_document_edits(artifact_env):
    _, root = _root(artifact_env)
    artifact = _retain(artifact_env, 'selected', [root])
    records, scope = artifact_env.records, artifact_env.scope
    refs = [{'type': 'experience', 'id': artifact.id, 'revision': 1}]
    frozen = SourceEgressService(records).snapshot(scope, refs)
    with records.begin() as tx:
        collector = api().CollectingReader(tx)
        SourceEgressService(TransactionRecords(collector), _memo={}).validate_snapshot(scope, frozen)
        identities = api().capture_identities(tx, collector)
        assert tx.commit() == ()
    selected = {(row['collection'], row['object_id']) for row in identities}
    assert {('recognition_tasks', artifact.task_id), ('recognition_context_packets', artifact.packet_id),
        ('documents', artifact.document['id']), ('document_revisions', artifact.document['id'] + '~r1'),
        ('document_markdown', artifact.document['id'] + '~r1'), ('recognitions', root.id)} <= selected
    artifact_env.documents.save_user_edit(artifact.document['id'], markdown='合成后续修订', expected_revision=1)
    assert records.read('documents', artifact.document['id']).revision == 2
    SourceEgressService(records).validate_snapshot(scope, frozen)
    for value in identities:
        verify_lineage(records, value)
    assert ('document_revisions', artifact.document['id'] + '~r2') not in selected


def test_actual_copy_owner_collects_selected_origin_and_original_across_projects(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service = RecognitionService(records)
    original = service.stage_experience(scope=WorkScope('local-user', 'alpha'), content='合成原始证据',
        provenance={'kind': 'user_statement'})
    scope = WorkScope('local-user', 'beta')
    copied = service.stage_experience(scope=scope, content='合成原始证据', copy_from={
        'project_id': 'alpha', 'experience_id': original, 'revision': 1})
    frozen = SourceEgressService(records).snapshot(scope, [{'type': 'experience', 'id': copied, 'revision': 1}])
    with records.begin() as tx:
        collector = api().CollectingReader(tx)
        SourceEgressService(TransactionRecords(collector), _memo={}).validate_snapshot(scope, frozen)
        assert set(collector.selected_keys()) == {('recognition_experiences', copied),
            ('recognition_experiences', original), ('v2_experience_origins', copied)}
        identities = api().capture_identities(tx, collector)
        assert tx.commit() == ()
    for value in identities:
        verify_lineage(records, value)


def test_capture_rejects_a_reader_from_another_writer(tmp_path):
    first = SQLiteStructuredRecordStore(tmp_path / 'first.sqlite3')
    other = SQLiteStructuredRecordStore(tmp_path / 'other.sqlite3')
    with first.begin() as tx, other.begin() as another:
        with pytest.raises(ValueError):
            api().capture_identities(tx, api().CollectingReader(another))


@pytest.mark.parametrize('collection', ADDITIONAL)
def test_actual_authority_collection_mapping_rotates_at_insert_without_payload_change(tmp_path, collection):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        first = tx.put(collection, 'x' * 128, {'id': 'synthetic owner'}, expected_revision=0)
        collector = api().CollectingReader(tx)
        collector.read(collection, first.object_id)
        proof = api().capture_identities(tx, collector)[0]
        assert tx.commit() == (first,)
    verify_lineage(records, proof)
    with records.begin() as tx:
        deleted = tx.delete(collection, first.object_id, expected_revision=1)
        fresh = tx.put(collection, first.object_id, first.payload, expected_revision=0)
        assert tx.commit() == (deleted, fresh)
    assert fresh.revision == first.revision == 1 and fresh.payload == first.payload
    with pytest.raises(ValueError):
        verify_lineage(records, proof)


def minimal_companion():
    material = {'type': 'original_item', 'id': 'source', 'revision': 1, 'project_id': 'alpha'}
    proof = {'material': material, 'snapshot': {'schema_version': 1,
        'scope': {'user_id': 'local-user', 'project_id': 'alpha'}, 'privacy_revision': 0,
        'roots': [{'type': 'original_item', 'id': 'source', 'revision': 1}],
        'nodes': [{'type': 'original_item', 'id': 'source', 'source_revision': 1,
            'policy_revision': 0, 'effective_purposes': ['embedding', 'generation', 'rerank']}]}, 'usage_kind': None}
    value = {'schema_version': 1, 'owner_id': 'local-user', 'turn_id': 'turn-test', 'immutable_ref': 'ref',
        'entries': {'M1': {'proof': proof, 'sql_identities': [{'collection': 'workspace_items',
            'object_id': 'source', 'fact_id': 'a' * 32}]}}}
    return value, {'M1': deepcopy(proof)}


@pytest.mark.parametrize('fault', ('schema', 'schema_bool', 'unknown', 'missing_number', 'extra_number',
    'entry_extra', 'proof_extra', 'proof_missing_schema', 'proof_schema_bool', 'proof_unknown_schema',
    'proof_mismatch', 'identity_extra', 'identity_revision', 'identity_unknown_collection',
    'identity_invalid_id', 'identity_invalid_fact', 'duplicate_identity', 'identity_not_list'))
def test_companion_rejects_unknown_duplicate_and_mismatched_fields(fault):
    value, mapping = minimal_companion()
    entry = value['entries']['M1']
    if fault == 'schema': value['schema_version'] = 2
    elif fault == 'schema_bool': value['schema_version'] = True
    elif fault == 'unknown': value['extra'] = True
    elif fault == 'missing_number': value['entries'] = {}
    elif fault == 'extra_number': value['entries']['M2'] = deepcopy(entry)
    elif fault == 'entry_extra': entry['extra'] = True
    elif fault == 'proof_extra': entry['proof']['extra'] = True
    elif fault == 'proof_missing_schema': del entry['proof']['snapshot']['schema_version']
    elif fault == 'proof_schema_bool': entry['proof']['snapshot']['schema_version'] = True
    elif fault == 'proof_unknown_schema': entry['proof']['snapshot']['schema_version'] = 2
    elif fault == 'proof_mismatch': entry['proof']['material']['revision'] = 2
    elif fault == 'identity_extra': entry['sql_identities'][0]['extra'] = True
    elif fault == 'identity_revision': entry['sql_identities'][0]['revision'] = 1
    elif fault == 'identity_unknown_collection': entry['sql_identities'][0]['collection'] = 'settings'
    elif fault == 'identity_invalid_id': entry['sql_identities'][0]['object_id'] = '/' + 'x' * 128
    elif fault == 'identity_invalid_fact': entry['sql_identities'][0]['fact_id'] = True
    elif fault == 'duplicate_identity': entry['sql_identities'].append(deepcopy(entry['sql_identities'][0]))
    elif fault == 'identity_not_list': entry['sql_identities'] = {}
    with pytest.raises(ValueError):
        api().validate_companion(value, owner_id='local-user', turn_id='turn-test', immutable_ref='ref', mapping=mapping)


def test_companion_validation_preserves_exact_valid_payload():
    value, mapping = minimal_companion()
    original = encoded(value)
    assert api().validate_companion(value, owner_id='local-user', turn_id='turn-test', immutable_ref='ref', mapping=mapping) is None
    assert encoded(value) == original
