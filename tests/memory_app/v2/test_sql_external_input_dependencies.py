"""Delivered SQL numbers retain their selected domain incarnations at writeback."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.document_recognition import extract_document_candidate
from backend.memory_app.legacy_intake_review import LegacyIntakeReview
from backend.memory_app.original_sources import source_store
from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_input_dependencies import COLLECTION, ExternalInputDependencyError, freeze_references, read_external_input_dependencies
from backend.recognition.external_turn_facts import IDENTITIES, delivery_facts
from backend.recognition.sql_source_identities import KIND
from core.storage_provider.record_lineage import capture_lineage
from core.job_runner.runtime import InMemoryJobRepository
from tests.memory_app.v2.test_external_context import env, document, material, prepare, settings
from tests.memory_app.v2.test_sql_source_identities import recognition
from tests.recognition.test_artifact_dependencies import _root, _retain, _publish


SCOPE = WorkScope('local-user', 'alpha')


def delivered(env, selections):
    turn = 'turn-' + uuid4().hex
    api, runtime, runner, _frozen = prepare(env, identity=turn, selections=selections)
    result = api.execute(turn, runtime=runtime, runner=runner)
    assert result['entries'] or result['profile']
    assert api.turns.events_after(turn)[-1]['type'] == 'turn.completed'
    return turn


def submit(env, turn, number='M1'):
    return env.http.post('/api/v2/external-agent/mcp/propose_insight', json={'client': 'codex',
        'arguments': {'text': 'Synthetic SQL-supported proposal', 'project': 'alpha',
            'evidence_ids': [{'turn_id': turn, 'id': value} for value in (number if isinstance(number, list) else [number])]}})


def pending(env, turn, number='M1'):
    response = submit(env, turn, number)
    assert response.status_code == 200, response.text
    row = env.records.read('recognition_candidates', response.json()['result']['candidate_id'])
    assert row.payload['state'] == 'pending'
    return row


def publish(env, candidate):
    return env.service.publish(scope=SCOPE, candidate_id=candidate.object_id,
        expected_revision=candidate.revision, reviewer='local-user')


def marker(env, candidate):
    return env.records.read(COLLECTION, candidate.payload['source_experience_ids'][0])


def assert_denied_without_writes(env, insight):
    before, calls = env.records.list_all(), env.model.calls
    assert not env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).snapshot(SCOPE,
            [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
    assert env.records.list_all() == before and env.model.calls == calls
    assert env.records.read('recognitions', insight.id).payload['state'] == 'active'


def legacy(env, project='alpha'):
    store = source_store(env.records)
    identity = 'legacy-sql-' + uuid4().hex
    payload = {'id': identity, 'project_id': project, 'title': 'Synthetic legacy evidence',
        'type': 'text', 'metadata': {'content_snapshot': 'Original synthetic legacy body'}}
    store.write('sources', identity, payload, expected_revision=0)
    job = 'job-' + identity
    with env.records.begin() as tx:
        tx.put('workspace_review_intents', 'review-' + identity, {'schema_version': '1.0.0',
            'id': 'review-' + identity, 'source_id': identity, 'project_id': project,
            'job_id': job, 'state': 'pending', 'source_revision': 1}, expected_revision=0)
        tx.commit()
    jobs = InMemoryJobRepository()
    jobs.save({'id': job, 'status': 'completed', 'source_id': identity})
    review = LegacyIntakeReview(env.root, env.records, env.documents, object_store=store, jobs=jobs)
    current = review.get(identity, project)
    current = review.save_draft(identity, project, 'Human reviewed synthetic legacy text',
        expected_revision=current['revision'], expected_document_basis=current['document_basis'])
    confirmed = review.confirm(identity, project, expected_revision=current['revision'],
        expected_document_basis=current['document_basis'], expected_markdown=current['draft_markdown'])
    extracted = extract_document_candidate(env.documents, env.service, project, confirmed['document_id'])
    insight = env.service.publish(scope=WorkScope('local-user', project), candidate_id=extracted['candidate_id'],
        expected_revision=1, reviewer='local-user')
    return insight, extracted['experience_id'], store, payload


def select(insight):
    return {'type': 'recognition', 'id': insight.id, 'revision': insight.revision,
        'project_id': 'alpha', 'layer': 'L3', 'windows': []}


def recreate(records, proof):
    old = records.read(proof['collection'], proof['object_id'])
    with records.begin() as tx:
        tx.delete(old.collection, old.object_id, expected_revision=old.revision)
        rebuilt = tx.put(old.collection, old.object_id, old.payload, expected_revision=0)
        while rebuilt.revision < old.revision:
            rebuilt = tx.put(old.collection, old.object_id, old.payload, expected_revision=rebuilt.revision)
        current = capture_lineage(tx, old.collection, old.object_id)
        tx.commit()
    assert rebuilt == old and current['fact_id'] != proof['fact_id']


def rewrite_marker(env, row, references):
    value = {**row.payload, 'references': references}
    with env.records.begin() as tx:
        tx.connection.execute('UPDATE crp_structured_records SET payload_json=? WHERE collection=? AND object_id=?',
            (json.dumps(value), COLLECTION, row.object_id))
        tx.commit()


@pytest.mark.parametrize('layer', ['L0', 'L1', 'L2', 'L3', 'P'])
def test_real_sql_layers_and_profile_reach_human_publication_and_original_egress(env, layer):
    number = 'M1'
    if layer == 'L0':
        selected = material(env)
    else:
        selected, _original = document(env, project='me' if layer == 'P' else 'alpha')
        if layer in {'L3', 'P'}:
            selected, _experience = recognition(env, selected, 'me' if layer == 'P' else 'alpha')
        elif layer == 'L2':
            selected = {**selected, 'layer': 'L2'}
        if layer == 'P': number = 'P1'
    turn = delivered(env, [selected])
    calls = env.model.calls
    candidate = pending(env, turn, number)
    value = marker(env, candidate)
    assert value.payload['schema_version'] == 2
    assert value.payload['references'][0]['identities_kind'] == KIND
    assert value.payload['references'][0]['id'] == number
    if layer not in {'L3', 'P'}:
        assert env.records.list('recognitions') == ()
    insight = publish(env, candidate)
    assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(SCOPE,
        [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
    authority.require(snapshot, 'generation')
    authority.validate_snapshot(SCOPE, snapshot)
    assert env.model.calls == calls


@pytest.mark.parametrize('collection', ['workspace_items', 'documents', 'document_revisions', 'document_markdown',
    'recognitions', 'recognition_experiences'])
def test_selected_sql_row_same_id_revision_and_full_payload_rebirth_rejects_consumers(env, collection):
    selected, _original = document(env)
    selected, _experience = recognition(env, selected)
    turn = delivered(env, [selected])
    insight = publish(env, pending(env, turn))
    identities = env.http.app.state.ai_turn_store.get_immutable_payload(turn, KIND)[1]['entries']['M1']['sql_identities']
    proof = next(row for row in identities if row['collection'] == collection)
    old = env.records.read(collection, proof['object_id'])
    with env.records.begin() as tx:
        tx.delete(collection, old.object_id, expected_revision=old.revision)
        rebuilt = tx.put(collection, old.object_id, old.payload, expected_revision=0)
        while rebuilt.revision < old.revision:
            rebuilt = tx.put(collection, old.object_id, old.payload, expected_revision=rebuilt.revision)
        current = capture_lineage(tx, collection, old.object_id)
        tx.commit()
    assert rebuilt == old and current['fact_id'] != proof['fact_id']
    assert_denied_without_writes(env, insight)


def test_current_privacy_and_external_client_revocation_do_not_revoke_local_sql_qualification(env):
    selected = material(env)
    turn = delivered(env, [selected])
    candidate = pending(env, turn)
    authority = SourceEgressService(env.records)
    authority.set_policy(SCOPE, 'original_item', selected['id'], 1, 0, [])
    settings(env, allow_remote=False, clients={'claude': True, 'codex': False})
    insight = publish(env, candidate)
    assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    snapshot = authority.snapshot(SCOPE,
        [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
    with pytest.raises(RecognitionConflict): authority.require(snapshot, 'generation')
    assert env.model.calls == 0


def test_missing_sql_companion_is_not_replaced_by_json_or_current_anchor(env):
    selected = material(env)
    turn = delivered(env, [selected])
    with sqlite3.connect(env.root / '.rebuild-data/ai-turns.sqlite3') as connection:
        connection.execute('DELETE FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?', (turn, KIND))
    before = env.records.list_all()
    assert submit(env, turn).status_code == 409
    assert env.records.list_all() == before and env.model.calls == 0


@pytest.mark.parametrize('fault', ['missing_kind', 'unknown_kind'])
def test_sql_frozen_reference_needs_a_known_explicit_kind(env, fault):
    turn = delivered(env, [material(env)])
    candidate = pending(env, turn)
    insight = publish(env, candidate)
    stored = marker(env, candidate)
    value = deepcopy(stored.payload)
    if fault == 'missing_kind': value['references'][0].pop('identities_kind')
    else: value['references'][0]['identities_kind'] = 'external-context-unknown-identities-v1'
    with env.records.begin() as tx:
        tx.connection.execute('UPDATE crp_structured_records SET payload_json=? WHERE collection=? AND object_id=?',
            (json.dumps(value), COLLECTION, stored.object_id))
        tx.commit()
    assert_denied_without_writes(env, insight)


def test_auxiliary_current_document_edit_keeps_chosen_historical_artifact_evidence(env):
    own = SimpleNamespace(records=env.records, service=env.service, documents=env.documents, scope=SCOPE)
    _experience, root = _root(own, 'sql-root')
    retained = _retain(own, 'sql-retained', [root])
    original = _publish(own, 'sql-retained-insight', experiences=[retained.id])
    selected = {'type': 'recognition', 'id': original.id, 'revision': original.revision,
        'project_id': 'alpha', 'layer': 'L3', 'windows': []}
    turn = delivered(env, [selected])
    insight = publish(env, pending(env, turn))
    env.documents.save_user_edit(retained.document['id'], markdown='Later synthetic edit', expected_revision=1)
    assert env.records.read('documents', retained.document['id']).revision == 2
    assert env.documents.markdown(retained.document['id'], revision=1) == 'historical model output sql-retained'
    assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(SCOPE, [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
    authority.require(snapshot, 'generation')
    authority.validate_snapshot(SCOPE, snapshot)


def test_nested_origin_json_requires_its_original_committed_index(env, monkeypatch):
    from core.storage_provider import runtime as storage_runtime
    from core.storage_provider.source_retrieval_index import index_store, namespace_projection, COLLECTION as INDEX
    _insight, original_id, store, payload = legacy(env, project='beta')
    original = env.records.read('recognition_experiences', original_id)
    copied = env.service.stage_experience(scope=SCOPE, content=original.payload['content'], copy_from={
        'project_id': 'beta', 'experience_id': original_id, 'revision': original.revision})
    own = SimpleNamespace(records=env.records, service=env.service, documents=env.documents, scope=SCOPE)
    root = _publish(own, 'nested-json-root', experiences=[copied])
    turn = delivered(env, [{'type': 'recognition', 'id': root.id, 'revision': root.revision,
        'project_id': 'alpha', 'layer': 'L3', 'windows': []}])
    candidate = pending(env, turn)
    meta = store._object_paths('sources', payload['id']).meta_path
    original_write = storage_runtime._write_json_atomic
    def interrupted(path, value):
        if path == meta: raise OSError('synthetic nested metadata interruption')
        return original_write(path, value)
    monkeypatch.setattr(storage_runtime, '_write_json_atomic', interrupted)
    changed = {**payload, 'metadata': {'content_snapshot': 'Uncommitted nested body'}}
    with pytest.raises(OSError, match='synthetic nested metadata interruption'):
        store.write('sources', payload['id'], changed, expected_revision=1)
    assert store.read('sources', payload['id']) == changed and store.revision('sources', payload['id']) == 1
    assert namespace_projection(index_store(store).read(INDEX, payload['id']), store.namespace_id)['state'] == 'invalid'
    before = env.records.list_all()
    with pytest.raises(RecognitionConflict): publish(env, candidate)
    assert env.records.list_all() == before and env.model.calls == 0


@pytest.mark.parametrize('rebirth', [False, True])
def test_actual_legacy_review_confirmation_is_selected_and_revalidated(env, rebirth):
    original, _experience, _store, payload = legacy(env)
    turn = delivered(env, [select(original)])
    identities = env.http.app.state.ai_turn_store.get_immutable_payload(turn, KIND)[1]['entries']['M1']['sql_identities']
    proof = next(row for row in identities if row['collection'] == 'workspace_review_intents')
    assert proof['object_id'] == 'review-' + payload['id']
    insight = publish(env, pending(env, turn))
    if rebirth:
        recreate(env.records, proof)
        assert_denied_without_writes(env, insight)
    else:
        assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
        authority = SourceEgressService(env.records)
        snapshot = authority.snapshot(SCOPE, [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
        authority.require(snapshot, 'generation')
        authority.validate_snapshot(SCOPE, snapshot)


@pytest.mark.parametrize('rebirth', [False, True])
def test_actual_retained_research_read_is_selected_and_revalidated(env, rebirth):
    from tests.memory_app.v2.test_original_privacy import original_source, completed_research
    from backend.memory_app.research_packets import capture_research_packet
    identity = original_source(env)
    research = completed_research(env, identity)
    bound = capture_research_packet(env.records, SCOPE, research['turn_id'], '比较证据后采用方案甲',
        authority=SourceEgressService(env.records))
    own = SimpleNamespace(records=env.records, service=env.service, documents=env.documents, scope=SCOPE)
    retained = _retain(own, 'actual-research', [])
    with env.records.begin() as tx:
        packet = tx.read('recognition_context_packets', retained.packet_id)
        saved = tx.put(packet.collection, packet.object_id, {**packet.payload, 'research_sources': bound,
            'messages': [{'role': 'user', 'content': '以下是专家团队的研究结论，仅供参考；与资料冲突时以资料为准：\n比较证据后采用方案甲'}]},
            expected_revision=packet.revision)
        experience = tx.read('recognition_experiences', retained.id)
        provenance = {**experience.payload['provenance'], 'source_refs': [
            {**ref, 'revision': saved.revision} if ref['type'] == 'context_packet' else ref
            for ref in experience.payload['provenance']['source_refs']]}
        tx.put(experience.collection, experience.object_id, {**experience.payload, 'provenance': provenance},
            expected_revision=experience.revision)
        tx.commit()
    original = _publish(own, 'actual-research-insight', experiences=[retained.id])
    turn = delivered(env, [select(original)])
    identities = env.http.app.state.ai_turn_store.get_immutable_payload(turn, KIND)[1]['entries']['M1']['sql_identities']
    proof = next(row for row in identities if row['collection'] == 'v2_research_source_reads')
    insight = publish(env, pending(env, turn))
    if rebirth:
        recreate(env.records, proof)
        assert_denied_without_writes(env, insight)
    else:
        assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
        authority = SourceEgressService(env.records)
        snapshot = authority.snapshot(SCOPE, [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
        authority.require(snapshot, 'generation')
        authority.validate_snapshot(SCOPE, snapshot)


def nested(env):
    original = material(env)
    first_turn = delivered(env, [original])
    first_candidate = pending(env, first_turn)
    first = publish(env, first_candidate)
    second_turn = delivered(env, [select(first), original])
    second_candidate = pending(env, second_turn, ['M1', 'M2'])
    second = publish(env, second_candidate)
    return original, first_candidate, first, second


def test_each_nested_number_replays_its_complete_shared_root_dag(env):
    _original, _first_candidate, first, second = nested(env)
    turn = delivered(env, [select(first), select(second)])
    entries = env.http.app.state.ai_turn_store.get_immutable_payload(turn, KIND)[1]['entries']
    assert set(entries) == {'M1', 'M2'}
    keys = [{(row['collection'], row['object_id']) for row in entry['sql_identities']} for entry in entries.values()]
    assert keys[0] < keys[1]
    candidate = pending(env, turn, ['M1', 'M2'])
    assert len(marker(env, candidate).payload['references']) == 2
    insight = publish(env, candidate)
    assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(SCOPE, [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
    authority.require(snapshot, 'generation')
    authority.validate_snapshot(SCOPE, snapshot)


@pytest.mark.parametrize('cycle', ['self', 'two_markers'])
def test_recursive_external_cycle_is_safe_and_next_normal_request_is_unpolluted(env, cycle):
    original, first_candidate, first, second = nested(env)
    turn = delivered(env, [select(first if cycle == 'self' else second)])
    with env.records.begin() as tx:
        refs = freeze_references(tx, SCOPE, 'codex', [{'turn_id': turn, 'id': 'M1'}],
            _sql_validator=env.service._external_input_validator)[0]
        assert tx.commit() == ()
    rewrite_marker(env, marker(env, first_candidate), refs)
    assert_denied_without_writes(env, first)
    # The original service returns an ineligible projection after catching its
    # domain conflict. A subsequent real read must still qualify normally.
    next_turn = delivered(env, [original])
    insight = publish(env, pending(env, next_turn))
    assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized


@pytest.mark.parametrize('fault', ['missing', 'extra', 'duplicate', 'foreign_fact', 'unknown_collection', 'foreign_proof'])
def test_sql_companion_must_match_the_exact_selected_domain_keys_and_original_archive(env, fault):
    turn = delivered(env, [material(env)])
    insight = publish(env, pending(env, turn))
    store = env.http.app.state.ai_turn_store
    _ref, value = store.get_immutable_payload(turn, KIND)
    value = deepcopy(value)
    entry = value['entries']['M1']
    if fault == 'missing': entry['sql_identities'].clear()
    elif fault == 'extra':
        selected = material(env)
        with env.records.begin() as tx:
            entry['sql_identities'].append(capture_lineage(tx, 'workspace_items', selected['id']))
            assert tx.commit() == ()
    elif fault == 'duplicate': entry['sql_identities'].append(deepcopy(entry['sql_identities'][0]))
    elif fault == 'foreign_fact': entry['sql_identities'][0]['fact_id'] = uuid4().hex
    elif fault == 'unknown_collection': entry['sql_identities'][0]['collection'] = 'unrelated'
    else: entry['proof']['material']['id'] = 'foreign-original'
    with sqlite3.connect(env.root / '.rebuild-data/ai-turns.sqlite3') as connection:
        connection.execute('UPDATE ai_turn_immutable_payloads SET payload_json=? WHERE turn_id=? AND kind=?',
            (json.dumps(value), turn, KIND))
    assert_denied_without_writes(env, insight)


@pytest.mark.parametrize('fault', [None, 'missing', 'bool'])
def test_explicit_old_json_marker2_kind_keeps_its_strict_historical_semantics(env, fault):
    from tests.memory_app.v2.test_mcp_evidence import delivered_original
    _api, turn, _store, _payload = delivered_original(env)
    candidate = pending(env, turn)
    stored = marker(env, candidate)
    with env.records.begin() as tx:
        ref, _facts, identities_ref, _identities, outcome, sequence = delivery_facts(tx, turn, 'M1', identities_kind=IDENTITIES)
        assert tx.commit() == ()
    references = [{'turn_id': turn, 'id': 'M1', 'immutable_ref': ref, 'identities_ref': identities_ref,
        'identities_kind': IDENTITIES, 'outcome_ref': outcome, 'completed_sequence': sequence}]
    rewrite_marker(env, stored, references)
    if fault is not None:
        with sqlite3.connect(env.root / '.rebuild-data/ai-turns.sqlite3') as connection:
            if fault == 'missing':
                connection.execute('DELETE FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?', (turn, IDENTITIES))
            else:
                value = _api.turns.get_immutable_payload(turn, IDENTITIES)[1]
                value['entries']['M1']['material']['revision'] = True
                connection.execute('UPDATE ai_turn_immutable_payloads SET payload_json=? WHERE turn_id=? AND kind=?',
                    (json.dumps(value), turn, IDENTITIES))
        before, calls = env.records.list_all(), env.model.calls
        with pytest.raises(RecognitionConflict): publish(env, candidate)
        assert env.records.list_all() == before and env.model.calls == calls
        assert marker(env, candidate).payload['ownexperience_identity'] == stored.payload['ownexperience_identity']
        return
    insight = publish(env, candidate)
    assert marker(env, candidate).payload['ownexperience_identity'] == stored.payload['ownexperience_identity']
    assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(SCOPE, [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
    authority.require(snapshot, 'generation')
    authority.validate_snapshot(SCOPE, snapshot)


def test_resolver_relocation_keeps_body_ast_and_v2_reexport():
    import ast
    import inspect
    import subprocess
    from backend.memory_app import original_sources
    from backend.memory_app.v2 import privacy
    source = subprocess.check_output(['git', 'show', '3c65e7dde5:src/backend/memory_app/v2/privacy.py'], text=True, encoding='utf-8')
    original = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == 'resolve_turn_material')
    relocation_import = original.body[2]
    assert isinstance(relocation_import, ast.ImportFrom) and relocation_import.level == 2
    relocation_import.level, relocation_import.module = 0, 'backend.memory_app.original_sources'
    current = ast.parse(inspect.getsource(original_sources.resolve_turn_material)).body[0]
    assert ast.dump(original, include_attributes=False) == ast.dump(current, include_attributes=False)
    assert privacy.resolve_turn_material is original_sources.resolve_turn_material


def test_actual_qualified_reader_connection_stays_query_only_during_sql_replay(env):
    turn = delivered(env, [material(env)])
    candidate = pending(env, turn)
    row = env.records.read('recognition_experiences', candidate.payload['source_experience_ids'][0])
    before = env.records.list_all()
    with env.service._evidence_reader() as reader:
        assert reader.connection.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError): reader.connection.execute('CREATE TABLE must_not_exist(id TEXT)')
        observed = []
        reader.connection.set_trace_callback(observed.append)
        assert read_external_input_dependencies(reader, SCOPE, row,
            _sql_validator=env.service._external_input_validator) is not None
        assert not any(sql.lstrip().split(' ', 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP'} for sql in observed)
    assert env.records.list_all() == before and env.model.calls == 0


def test_legacy_current_anchor_is_verified_but_missing_head_is_never_adopted(env):
    from tests.memory_app.v2.test_sql_source_identities import remove_lineage
    from core.storage_provider.record_lineage import FACTS, HEAD_COLLECTIONS
    selected = material(env)
    remove_lineage(env.records, 'workspace_items', selected['id'])
    turn = delivered(env, [selected])
    identity = env.http.app.state.ai_turn_store.get_immutable_payload(turn, KIND)[1]['entries']['M1']['sql_identities'][0]
    fact = env.records.read(FACTS, identity['fact_id'])
    assert fact.payload['origin'] == 'current_anchor' and fact.payload['observed_revision'] == 1
    insight = publish(env, pending(env, turn))
    assert env.service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    with env.records.begin() as tx:
        tx.connection.execute('DELETE FROM crp_structured_records WHERE collection=? AND object_id=?',
            (HEAD_COLLECTIONS['workspace_items'], selected['id']))
        tx.commit()
    assert_denied_without_writes(env, insight)
    assert env.records.read(HEAD_COLLECTIONS['workspace_items'], selected['id']) is None


def test_edit_of_the_actual_selected_document_invalidates_its_frozen_material_revision(env):
    selected, _original = document(env)
    turn = delivered(env, [selected])
    candidate = pending(env, turn)
    env.documents.save_user_edit(selected['id'], markdown='Selected body changed', expected_revision=selected['revision'])
    before = env.records.list_all()
    with pytest.raises(RecognitionConflict): publish(env, candidate)
    assert env.records.list_all() == before


def test_original_qualification_retains_unresolved_import_veto_outside_the_birth_set(env):
    selected, _original = document(env)
    selected, experience_id = recognition(env, selected)
    turn = delivered(env, [selected])
    candidate = pending(env, turn)
    companion = env.http.app.state.ai_turn_store.get_immutable_payload(turn, KIND)[1]
    assert all(row['collection'] != 'recognition_migration_imports'
        for row in companion['entries']['M1']['sql_identities'])
    # Persist a synthetic unresolved receipt with the actual original UOW.
    # The original qualification owner consumes this negative fact; it is
    # neither a positive source identity nor an egress permission owner.
    with env.records.begin() as tx:
        tx.put('recognition_migration_imports', 'synthetic-unresolved', {
            'scope': {'user_id': SCOPE.user_id, 'project_id': SCOPE.project_id},
            'mapping': {'experiences': {'portable-input': experience_id}},
            'receipt': {'issues': [{'location': 'provenance', 'code': 'external_provenance_reference',
                'record_id': 'portable-input'}]}}, expected_revision=0)
        tx.commit()
    assert not env.service.get_recognition(scope=SCOPE, recognition_id=selected['id']).authorized
    before, calls = env.records.list_all(), env.model.calls
    with pytest.raises(RecognitionConflict): publish(env, candidate)
    assert env.records.list_all() == before and env.model.calls == calls


def test_bare_domain_service_refuses_sql_capability_and_keeps_ordinary_statements(env):
    from backend.recognition import RecognitionService
    bare = RecognitionService(env.records)
    experience = bare.stage_experience(scope=SCOPE, content='Synthetic ordinary statement')
    ordinary = bare.propose(scope=SCOPE, content='Synthetic ordinary conclusion', source_experience_ids=[experience])
    ordinary = bare.publish(scope=SCOPE, candidate_id=ordinary.id, expected_revision=1, reviewer=SCOPE.user_id)
    assert bare.get_recognition(scope=SCOPE, recognition_id=ordinary.id).authorized
    turn = delivered(env, [material(env)])
    candidate = pending(env, turn)
    insight = publish(env, candidate)
    before, calls = env.records.list_all(), env.model.calls
    assert not bare.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    assert env.records.list_all() == before and env.model.calls == calls
    fresh = pending(env, turn)
    before = env.records.list_all()
    with pytest.raises(RecognitionConflict):
        bare.publish(scope=SCOPE, candidate_id=fresh.object_id, expected_revision=1, reviewer=SCOPE.user_id)
    assert env.records.list_all() == before and env.model.calls == calls


def test_fixed_source_factory_uses_the_real_service_reader_and_complete_chain(env):
    from backend.memory_app.source_egress import recognition_service, prepare_sources
    from backend.recognition import RecognitionService
    hook = prepare_sources
    service = recognition_service(env.records, cache_invalidation=hook)
    assert isinstance(service, RecognitionService) and service.records is env.records
    assert service.cache_invalidation is hook
    candidate = pending(env, delivered(env, [material(env)]))
    insight = service.publish(scope=SCOPE, candidate_id=candidate.object_id, expected_revision=1, reviewer=SCOPE.user_id)
    assert service.get_recognition(scope=SCOPE, recognition_id=insight.id).authorized
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(SCOPE, [{'type': 'recognition', 'id': insight.id, 'revision': insight.revision}])
    authority.require(snapshot, 'generation')
    authority.validate_snapshot(SCOPE, snapshot)
    assert env.model.calls == 0


@pytest.mark.parametrize('fault', ['returned_snapshot', 'mutated_input'])
def test_validator_cannot_return_a_different_archived_snapshot(env, fault):
    from backend.recognition import RecognitionService
    candidate = pending(env, delivered(env, [material(env)]))
    def malicious(reader, scope, entry, *, trail, budget):
        from backend.memory_app.source_egress import validate_external_number
        value = deepcopy(validate_external_number(reader, scope, entry, trail=trail, budget=budget))
        if fault == 'returned_snapshot':
            value['privacy_revision'] += 1
        else:
            entry['proof']['snapshot']['privacy_revision'] += 1
            value = entry['proof']['snapshot']
        return value
    service = RecognitionService(env.records, _external_input_validator=malicious)
    before, calls = env.records.list_all(), env.model.calls
    with pytest.raises(RecognitionConflict):
        service.publish(scope=SCOPE, candidate_id=candidate.object_id, expected_revision=1, reviewer=SCOPE.user_id)
    assert env.records.list_all() == before and env.model.calls == calls


def test_canonical_encoder_is_the_single_owner_with_the_original_exact_bytes():
    from backend.recognition.external_evidence_json import encoded
    from backend.recognition.external_turn_facts import encoded as facts_encoded
    from backend.recognition.sql_source_identities import encoded as sql_encoded
    assert encoded is facts_encoded is sql_encoded
    assert encoded({'z': [True, None, 1], 'a': '合成😀'}) == '{"a":"合成😀","z":[true,null,1]}'
    with pytest.raises(ValueError): encoded({'not_finite': float('nan')})
