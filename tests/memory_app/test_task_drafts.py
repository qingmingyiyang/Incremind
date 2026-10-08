import pytest

from backend.memory_app.v2.task_drafts import TaskDrafts
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import SQLiteStructuredRecordStore


def test_only_delivered_task_draft_is_visible_to_document_reader(tmp_path):
    from backend.shared.document_visibility import recognition_document_visible, LegacyDocumentVisibility
    from backend.recognition import WorkScope
    records = SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    output = TaskDrafts(records, documents).create(turn_id='turn-kernel', project='alpha',
        operation='deliver-turn-kernel', title='交付稿', markdown='正文')
    identity = output['document_id']
    scope = WorkScope('local-user', 'alpha')
    assert not recognition_document_visible(records, scope, identity)
    assert not LegacyDocumentVisibility.from_repository(documents).allows(documents.read(identity))
    with records.begin() as tx:
        tx.put('v2_turns', 'turn-product', {'project_id':'alpha', 'receipt':{'do':{
            'state':'done','kernel_turn_id':'turn-kernel','document_id':identity}}}, expected_revision=0)
        tx.commit()
    assert recognition_document_visible(records, scope, identity)
    assert LegacyDocumentVisibility.from_repository(documents).allows(documents.read(identity))
    assert not recognition_document_visible(records, WorkScope('local-user', 'other'), identity)


def test_task_draft_creates_once_without_changing_existing_material(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    original = documents.create(DocumentDraft('原稿', 'note', '保留原文',
        ({'source_id':'original', 'locator':'task://original'},), 'alpha'))
    before = {name:records.list(name) for name in ('documents','document_revisions','document_markdown')}
    service = TaskDrafts(records, documents)
    output = service.create(turn_id='turn-task-one', project='alpha',
        operation='operation-draft-one', title='部分成果', markdown='新草稿')
    assert output == service.create(turn_id='turn-task-one', project='alpha',
        operation='operation-draft-one', title='部分成果', markdown='新草稿')
    assert output['document_id'] != original['id']
    assert len(documents.list()) == 2
    for name, rows in before.items():
        for row in rows:
            assert records.read(name, row.object_id) == row
    redone = service.create(turn_id='turn-task-redone', project='alpha',
        operation='operation-draft-redone', title='部分成果', markdown='新草稿')
    assert redone['document_id'] != output['document_id']
    with pytest.raises(ValueError):
        service.create(turn_id='turn-task-one', project='alpha',
            operation='operation-draft-one', title='部分成果', markdown='覆盖原输出')


@pytest.mark.parametrize('field', ['document_id', 'source_id', 'delete', 'expected_revision'])
def test_task_capability_rejects_existing_object_mutations(tmp_path, field):
    from backend.memory_app.kernel.task_draft_capability import TaskDraftCapability
    records = SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    frozen = {'turn_id':'turn-task', 'desired_outcome':'project.task',
              'scope':{'project_id':'alpha'}}
    capability = TaskDraftCapability(legacy=None, drafts=TaskDrafts(records, documents),
        request_loader=lambda identity: frozen,
        division_capabilities=lambda request: ('document.draft.propose',))
    with pytest.raises(ValueError, match='only accepts new'):
        capability.invoke({'turn_id':'turn-task', 'operation_id':'operation-task',
            'arguments':{'title':'新稿', 'markdown':'新正文', field:'existing'}})
    assert not documents.list()


def test_task_capability_requires_frozen_assignment_before_writing(tmp_path):
    from backend.memory_app.kernel.task_draft_capability import TaskDraftCapability
    records = SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    capability = TaskDraftCapability(legacy=None, drafts=TaskDrafts(records, documents),
        request_loader=lambda identity: {'turn_id':identity, 'desired_outcome':'project.task'},
        division_capabilities=lambda request: ())
    with pytest.raises(ValueError, match='outside frozen division'):
        capability.invoke({'turn_id':'turn-task', 'operation_id':'operation-task',
            'arguments':{'title':'新稿', 'markdown':'新正文'}})
    assert not documents.list()
