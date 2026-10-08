import sqlite3

import pytest

from core.storage_provider.source_retrieval_index import ORIGINALS, project_original
from tests.memory_app.v2.test_cache_write_projection import seed, namespaces
from tests.memory_app.v2.test_workbench_ask import env as env, add_document


def test_confirmed_item_revision_evicts_only_its_document_namespace(env):
    doc, item = add_document(env, original='originalneedle')
    path = env.root / 'recognition-vectors.sqlite3'
    seed(path, 'alpha', 'document', doc, 2)
    other = seed(path, 'alpha', 'document', 'unrelated')
    previous = env.records.read('workspace_items', item)
    updated = env.domains.items.update(item, 'alpha', {'confirmed'}, title='Changed title')
    assert updated['revision'] == previous.revision + 1
    assert namespaces(path) == {other}
    candidates = env.domains.query.collect_candidates('alpha', 'originalneedle')['candidates']
    original = next(row for row in candidates if row['entry']['id'] == doc and row['layer'] == 'L0')
    assert original['entry']['item_revision'] == updated['revision']
    assert any('originalneedle' in window.text for window in original['windows'])


def test_neutral_original_projection_uses_explicit_old_and_new_document_bindings(env):
    doc, item = add_document(env)
    path = env.root / 'recognition-vectors.sqlite3'
    seed(path, 'alpha', 'document', doc)
    seed(path, 'beta', 'document', 'next-document')
    other = seed(path, 'alpha', 'document', 'unrelated')
    current = env.records.read('workspace_items', item)
    with env.records.begin() as tx:
        project_original(tx, item, 'beta', current.revision + 1, 'new original',
                         document_id='next-document', previous_document_id=doc)
        tx.commit()
    assert namespaces(path) == {other}


def test_original_cache_deletion_failure_rolls_back_item_and_projection(env):
    doc, item = add_document(env)
    path = env.root / 'recognition-vectors.sqlite3'
    seed(path, 'alpha', 'document', doc)
    before = env.records.read('workspace_items', item)
    before_index = env.records.read(ORIGINALS, item)
    lock = sqlite3.connect(path, timeout=0)
    try:
        lock.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):
            env.domains.items.update(item, 'alpha', {'confirmed'}, title='Changed title')
    finally:
        lock.rollback()
        lock.close()
    assert env.records.read('workspace_items', item) == before
    assert env.records.read(ORIGINALS, item) == before_index
