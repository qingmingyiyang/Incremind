import os
from pathlib import Path
from threading import RLock
from uuid import uuid4

import pytest


@pytest.mark.parametrize('change', [None, 'markdown', 'reviewed_revision'])
def test_archive_confirmation_resumes_actual_frozen_operation(change):
    root = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (root, *root.parents))
    root = root / ('confirm-' + uuid4().hex[:8])
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation, COLLECTION, _source_payload, _refs
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.memory_app.workspace_generation import _draft
    from backend.memory_app.v2.shared_backup_import import SharedBackupImports
    from backend.memory_app.v2.shared_projects import SharedProjectError
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.storage_provider.source_retrieval_index import project_original
    from core.document_engine import SQLiteDocumentRepository

    records = SQLiteStructuredRecordStore(root / '.rebuild-data/structured-records.sqlite3')
    documents = SQLiteDocumentRepository(records, namespace_id='default')
    service = WorkspaceConfirmation(root, records, documents)
    markdown = '合成副本保留采购数量和金额。'
    draft = _draft({'title': '副本整理稿', 'summary': markdown, 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, markdown)
    item = WorkspaceItems(records, None, RLock()).create_upload({'id': 'workspace-import-' + uuid4().hex,
        'project_id': 'copy-project', 'input_kind': 'text', 'title': draft['title'], 'source_text': markdown,
        'status': 'ready', 'draft': draft, 'error': None, 'document_id': None, 'created_at': '2026-11-05T00:00:00+00:00'})
    item = records.read('workspace_items', item['id'])
    operation_id, source_id = 'confirm-' + item.object_id, 'source-' + item.object_id
    with records.begin() as tx:
        tx.put(COLLECTION, operation_id, {'id': operation_id, 'state': 'pending',
            'workspace_item_id': item.object_id, 'project_id': 'copy-project', 'source_id': source_id,
            'source_payload': _source_payload(item.payload, operation_id, source_id),
            'markdown': markdown + ('改变' if change == 'markdown' else ''), 'draft': draft,
            'reviewed_revision': item.revision + (1 if change == 'reviewed_revision' else 0),
            'source_refs': _refs(item.payload, source_id)}, expected_revision=0)
        current = tx.put('workspace_items', item.object_id, {**item.payload,
            'status': 'confirming', 'reviewed_revision': item.revision}, expected_revision=item.revision)
        project_original(tx, item.object_id, 'copy-project', current.revision, markdown)
        tx.commit()
    pending = records.read('workspace_items', item.object_id)
    assert pending.revision == 2 and pending.payload['reviewed_revision'] == 1
    if change is not None:
        with pytest.raises(SharedProjectError, match='target_confirmation_changed'):
            SharedBackupImports._confirmation_revision(records, pending, 'copy-project', draft, markdown)
        assert records.read(COLLECTION, operation_id).payload['state'] == 'pending'
        assert not documents.list()
        return
    reviewed = SharedBackupImports._confirmation_revision(records, pending, 'copy-project', draft, markdown)
    assert reviewed == 1
    saved = service.confirm(item.object_id, 'copy-project', reviewed, lambda _draft: markdown)
    assert saved['status'] == 'confirmed'
    assert documents.markdown(saved['document_id'], revision=1) == markdown
    assert records.read(COLLECTION, operation_id).payload['state'] == 'committed'
    assert service.confirm(item.object_id, 'copy-project', reviewed, lambda _draft: markdown) == saved
    assert len(documents.list()) == 1
