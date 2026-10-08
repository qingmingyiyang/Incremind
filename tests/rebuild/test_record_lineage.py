"""Storage-owned incarnation facts share the original SQLite UOW."""
from __future__ import annotations

from importlib import import_module
import json
import sqlite3

import pytest

from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


FACTS = 'record_lineage_facts'
HEADS = {
    'workspace_items': 'record_lineage_workspace_items',
    'recognition_experiences': 'record_lineage_experiences',
    'recognitions': 'record_lineage_recognitions',
    'documents': 'record_lineage_documents',
    'document_revisions': 'record_lineage_document_revisions',
    'document_markdown': 'record_lineage_document_markdown',
}
WITNESSES = {
    'workspace_items': 'record_lineage_first_workspace_items',
    'recognition_experiences': 'record_lineage_first_experiences',
    'recognitions': 'record_lineage_first_recognitions',
    'documents': 'record_lineage_first_documents',
    'document_revisions': 'record_lineage_first_document_revisions',
    'document_markdown': 'record_lineage_first_document_markdown',
}


@pytest.fixture
def store(tmp_path):
    return SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')


def api():
    return import_module('core.storage_provider.record_lineage')


def create(store, collection='documents', identity='same', payload=None):
    payload = payload or {'id': identity, 'content': 'synthetic retained content'}
    with store.begin() as tx:
        row = tx.put(collection, identity, payload, expected_revision=0)
        committed = tx.commit()
    assert committed == (row,)
    return row


def legacy(store, collection='documents', identity='old', revision=4):
    # Simulate a database written before the creation hook existed. No owner
    # method is mocked, and the original stored payload is retained exactly.
    store.read(collection, identity)
    payload = {'id': identity, 'created_at': '2001-01-01', 'content': 'synthetic legacy content'}
    with sqlite3.connect(store.database_path) as connection:
        connection.execute('INSERT INTO crp_structured_records VALUES (?, ?, ?, ?)',
            (collection, identity, json.dumps(payload), revision))
    return store.read(collection, identity)


def capture(store, collection='documents', identity='same'):
    with store.begin() as tx:
        result = api().capture_lineage(tx, collection, identity)
        assert tx.commit() == ()
    return result


@pytest.mark.parametrize('collection', tuple(HEADS))
def test_actual_insert_update_delete_and_same_revision_recreation(store, collection):
    first = create(store, collection)
    # This assertion exposes the absent central INSERT hook without depending
    # on the new helper module or replacing the real record owner.
    facts = store.list(FACTS)
    assert len(facts) == 1
    proof = capture(store, collection)
    assert proof == {'collection': collection, 'object_id': 'same', 'fact_id': facts[0].object_id}
    assert facts[0].revision == 1
    assert facts[0].payload == {'schema_version': 1, 'collection': collection,
        'object_id': 'same', 'origin': 'created', 'observed_revision': 1}
    assert len(proof['fact_id']) == 32
    assert store.read(collection, 'same') == first
    with store.begin() as tx:
        changed = tx.put(collection, 'same', {**first.payload, 'content': 'synthetic edit'}, expected_revision=1)
        assert tx.commit() == (changed,)
    assert changed.revision == 2
    assert api().verify_lineage(store, proof) is None
    assert capture(store, collection) == proof
    with store.begin() as tx:
        deleted = tx.delete(collection, 'same', expected_revision=2)
        assert tx.commit() == (deleted,)
    assert store.read(HEADS[collection], 'same') is not None
    with pytest.raises(api().RecordLineageError):
        api().verify_lineage(store, proof)
    recreated = create(store, collection, payload=first.payload)
    fresh = capture(store, collection)
    assert recreated.revision == first.revision == 1
    assert recreated.payload == first.payload
    assert fresh['fact_id'] != proof['fact_id']
    assert api().verify_lineage(store, fresh) is None
    with pytest.raises(api().RecordLineageError):
        api().verify_lineage(store, proof)
    assert len(store.list(FACTS)) == 2
    assert store.read(FACTS, proof['fact_id']) == facts[0]


def test_fixed_collections_keep_equal_max_length_ids_distinct(store):
    identity = 'x' * 128
    proofs = []
    for collection in HEADS:
        create(store, collection, identity)
        proofs.append(capture(store, collection, identity))
    assert len({proof['fact_id'] for proof in proofs}) == len(HEADS)
    for collection, proof in zip(HEADS, proofs, strict=True):
        assert proof['object_id'] == identity
        assert store.read(HEADS[collection], identity).payload == {'schema_version': 1, 'fact_id': proof['fact_id']}
        assert api().verify_lineage(store, proof) is None


def test_legacy_anchor_records_today_observation_without_claiming_historical_birth(store):
    old = legacy(store)
    assert store.list(FACTS) == ()
    proof = capture(store, identity='old')
    assert store.read('documents', 'old') == old
    assert store.read(FACTS, proof['fact_id']).payload == {'schema_version': 1,
        'collection': 'documents', 'object_id': 'old', 'origin': 'current_anchor', 'observed_revision': 4}
    assert capture(store, identity='old') == proof
    assert len(store.list(FACTS)) == 1
    with store.begin() as tx:
        tx.delete('documents', 'old', expected_revision=4)
        replacement = tx.put('documents', 'old', old.payload, expected_revision=0)
        assert len(tx.commit()) == 2
    assert replacement.revision == 1
    fresh = capture(store, identity='old')
    assert store.read(FACTS, fresh['fact_id']).payload['origin'] == 'created'
    assert fresh != proof
    with pytest.raises(api().RecordLineageError):
        api().verify_lineage(store, proof)


@pytest.mark.parametrize('fault', ('head_extra', 'head_version_bool', 'bad_fact_id',
    'missing_fact', 'fact_version_bool', 'fact_collection', 'fact_object',
    'fact_origin', 'fact_origin_list', 'fact_observed_bool', 'fact_revision', 'orphan_fact'))
def test_damaged_existing_lineage_is_rejected_instead_of_reanchored(store, fault):
    create(store)
    proof = capture(store)
    with sqlite3.connect(store.database_path) as connection:
        if fault in {'missing_fact', 'orphan_fact'}:
            collection, identity = (FACTS, proof['fact_id']) if fault == 'missing_fact' else (HEADS['documents'], 'same')
            connection.execute('DELETE FROM crp_structured_records WHERE collection=? AND object_id=?', (collection, identity))
        else:
            head = fault.startswith('head_') or fault == 'bad_fact_id'
            collection, identity = (HEADS['documents'], 'same') if head else (FACTS, proof['fact_id'])
            row = store.read(collection, identity)
            value = dict(row.payload)
            if fault == 'head_extra': value['unexpected'] = 'extra'
            elif fault in {'head_version_bool', 'fact_version_bool'}: value['schema_version'] = True
            elif fault == 'bad_fact_id': value['fact_id'] = 'not-a-generation'
            elif fault == 'fact_collection': value['collection'] = 'recognitions'
            elif fault == 'fact_object': value['object_id'] = 'other'
            elif fault == 'fact_origin': value['origin'] = 'historical_birth'
            elif fault == 'fact_origin_list': value['origin'] = []
            elif fault == 'fact_observed_bool': value['observed_revision'] = True
            revision = 2 if fault == 'fact_revision' else row.revision
            connection.execute('UPDATE crp_structured_records SET payload_json=?,revision=? WHERE collection=? AND object_id=?',
                (json.dumps(value), revision, collection, identity))
    before = (store.list(FACTS), store.list(HEADS['documents']))
    with pytest.raises(api().RecordLineageError):
        capture(store)
    with pytest.raises(api().RecordLineageError):
        api().verify_lineage(store, proof)
    assert (store.list(FACTS), store.list(HEADS['documents'])) == before


def test_missing_record_and_unknown_collection_cannot_be_anchored(store):
    create(store, 'unrelated')
    assert store.list(FACTS) == ()
    for collection, identity in (('documents', 'missing'), ('unrelated', 'same')):
        with pytest.raises(api().RecordLineageError):
            capture(store, collection, identity)
        with pytest.raises(api().RecordLineageError):
            api().verify_lineage(store, {'collection': collection, 'object_id': identity, 'fact_id': 'a' * 32})
    assert store.list(FACTS) == ()


@pytest.mark.parametrize('fault', ('extra', 'unknown_collection', 'long_id', 'bad_fact_id'))
def test_identity_is_strict_metadata(store, fault):
    create(store)
    proof = capture(store)
    invalid = dict(proof)
    if fault == 'extra': invalid['revision'] = 1
    elif fault == 'unknown_collection': invalid['collection'] = 'unrelated'
    elif fault == 'long_id': invalid['object_id'] = 'x' * 129
    else: invalid['fact_id'] = False
    with pytest.raises(api().RecordLineageError):
        api().verify_lineage(store, invalid)


def test_stale_cas_rolls_back_other_domain_and_internal_creations(store):
    original = create(store)
    proof = capture(store)
    with pytest.raises(SQLiteUnitOfWorkConflict):
        with store.begin() as tx:
            tx.put('recognitions', 'other', {'id': 'other'}, expected_revision=0)
            tx.put('documents', 'same', original.payload, expected_revision=0)
    assert store.read('recognitions', 'other') is None
    assert store.read(HEADS['recognitions'], 'other') is None
    assert len(store.list(FACTS)) == 1
    assert capture(store) == proof


def reject_collection(store, collection, operation='INSERT'):
    assert operation in {'INSERT', 'UPDATE'}
    # The collection is a fixed test-owned constant; domain inserts still use
    # the original UOW. This trigger exercises a real SQLite transaction abort.
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(f"CREATE TRIGGER reject_lineage BEFORE {operation} ON crp_structured_records "
            f"WHEN NEW.collection='{collection}' BEGIN SELECT RAISE(ABORT,'synthetic lineage abort'); END")


def test_real_fact_insert_abort_rolls_back_the_owner_and_siblings(store):
    store.read('documents', 'same')
    reject_collection(store, FACTS)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic lineage abort'):
        with store.begin() as tx:
            tx.put('unrelated', 'sibling', {'id': 'sibling'}, expected_revision=0)
            tx.put('documents', 'same', {'id': 'same'}, expected_revision=0)
    assert store.read('documents', 'same') is None
    assert store.read('unrelated', 'sibling') is None
    assert store.list(FACTS) == store.list(HEADS['documents']) == ()


def test_real_pointer_update_abort_rolls_back_delete_and_recreation(store):
    original = create(store)
    proof = capture(store)
    reject_collection(store, HEADS['documents'], 'UPDATE')
    with pytest.raises(sqlite3.IntegrityError, match='synthetic lineage abort'):
        with store.begin() as tx:
            tx.delete('documents', 'same', expected_revision=1)
            tx.put('documents', 'same', original.payload, expected_revision=0)
    assert store.read('documents', 'same') == original
    assert len(store.list(FACTS)) == 1
    assert capture(store) == proof


def test_real_anchor_pointer_abort_keeps_legacy_record_and_rolls_back_fact(store):
    original = legacy(store)
    reject_collection(store, HEADS['documents'])
    with pytest.raises(sqlite3.IntegrityError, match='synthetic lineage abort'):
        capture(store, identity='old')
    assert store.read('documents', 'old') == original
    assert store.list(FACTS) == store.list(HEADS['documents']) == ()


def test_lineage_adds_no_ddl_or_domain_payload_fields_and_commit_result_stays_original(store):
    store.read('documents', 'none')
    with sqlite3.connect(store.database_path) as connection:
        before = connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    with store.begin() as tx:
        rows = tuple(tx.put(collection, 'item', {'id': 'item', 'content': collection}, expected_revision=0)
            for collection in ('documents', 'workspace_items', 'unrelated'))
        assert tx.commit() == rows
    assert len(store.list(FACTS)) == 2
    for row in rows:
        assert store.read(row.collection, row.object_id) == row
        assert set(row.payload) == {'id', 'content'}
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall() == before


@pytest.mark.parametrize('collection', tuple(HEADS))
def test_first_witness_is_immutable_across_same_id_recreation(store, collection):
    original = create(store, collection, 'x' * 128)
    witness = store.read(WITNESSES[collection], original.object_id)
    assert witness is not None and witness.revision == 1
    first = capture(store, collection, original.object_id)
    assert witness.payload == {'schema_version': 1, 'first_fact_id': first['fact_id']}
    with store.begin() as tx:
        tx.delete(collection, original.object_id, expected_revision=1)
        replacement = tx.put(collection, original.object_id, original.payload, expected_revision=0)
        assert len(tx.commit()) == 2
    assert replacement.revision == 1
    fresh = capture(store, collection, original.object_id)
    assert fresh['fact_id'] != first['fact_id']
    assert store.read(WITNESSES[collection], original.object_id) == witness
    assert api().verify_lineage(store, fresh) is None


def test_new_slot_uses_primary_key_reads_without_scanning_history_json(store):
    create(store)
    statements = []
    with store.begin() as tx:
        # Observe the real connection and owner, rather than replacing either.
        tx.connection.set_trace_callback(statements.append)
        row = tx.put('documents', 'fresh', {'id': 'fresh'}, expected_revision=0)
        proof = api().capture_lineage(tx, 'documents', 'fresh')
        assert tx.commit() == (row,)
    scans = [sql for sql in statements if 'json_extract' in sql.lower() and FACTS in sql]
    assert scans == []
    assert store.read(WITNESSES['documents'], 'fresh').payload['first_fact_id'] == proof['fact_id']


@pytest.mark.parametrize('fault', ('witness_missing', 'witness_version_bool', 'witness_extra',
    'witness_bad_fact', 'witness_revision', 'witness_foreign_fact', 'first_missing',
    'first_revision', 'first_version_bool', 'first_collection', 'first_object'))
def test_first_witness_and_original_fact_damage_block_current_identity(store, fault):
    original = create(store)
    first = capture(store)
    with store.begin() as tx:
        tx.delete('documents', 'same', expected_revision=1)
        tx.put('documents', 'same', original.payload, expected_revision=0)
        tx.commit()
    current = capture(store)
    if fault == 'witness_foreign_fact':
        create(store, 'recognitions', 'same')
        foreign = capture(store, 'recognitions')
    witness = store.read(WITNESSES['documents'], 'same')
    assert witness is not None
    with sqlite3.connect(store.database_path) as connection:
        if fault in {'witness_missing', 'first_missing'}:
            collection, identity = (WITNESSES['documents'], 'same') if fault == 'witness_missing' else (FACTS, first['fact_id'])
            connection.execute('DELETE FROM crp_structured_records WHERE collection=? AND object_id=?', (collection, identity))
        else:
            first_fact = fault.startswith('first_')
            collection, identity = (FACTS, first['fact_id']) if first_fact else (WITNESSES['documents'], 'same')
            row = store.read(collection, identity)
            value = dict(row.payload)
            if fault == 'witness_version_bool' or fault == 'first_version_bool': value['schema_version'] = True
            elif fault == 'witness_extra': value['unexpected'] = 'extra'
            elif fault == 'witness_bad_fact': value['first_fact_id'] = False
            elif fault == 'witness_foreign_fact': value['first_fact_id'] = foreign['fact_id']
            elif fault == 'first_collection': value['collection'] = 'recognitions'
            elif fault == 'first_object': value['object_id'] = 'other'
            revision = 2 if fault in {'witness_revision', 'first_revision'} else row.revision
            connection.execute('UPDATE crp_structured_records SET payload_json=?,revision=? WHERE collection=? AND object_id=?',
                (json.dumps(value), revision, collection, identity))
    before = (store.list(FACTS), store.list(HEADS['documents']), store.list(WITNESSES['documents']))
    with pytest.raises(api().RecordLineageError):
        capture(store)
    with pytest.raises(api().RecordLineageError):
        api().verify_lineage(store, current)
    assert (store.list(FACTS), store.list(HEADS['documents']), store.list(WITNESSES['documents'])) == before


def test_real_first_witness_insert_abort_rolls_back_owner_fact_and_sibling(store):
    store.read('documents', 'same')
    reject_collection(store, WITNESSES['documents'])
    with pytest.raises(sqlite3.IntegrityError, match='synthetic lineage abort'):
        with store.begin() as tx:
            tx.put('unrelated', 'sibling', {'id': 'sibling'}, expected_revision=0)
            tx.put('documents', 'same', {'id': 'same'}, expected_revision=0)
    assert store.read('documents', 'same') is None
    assert store.read('unrelated', 'sibling') is None
    assert store.list(FACTS) == store.list(HEADS['documents']) == store.list(WITNESSES['documents']) == ()
