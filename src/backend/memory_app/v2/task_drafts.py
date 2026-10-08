"""Create-only task output, atomically recorded with its operation receipt."""
from core.document_engine.ports import DocumentDraft

COLLECTION = 'v2_task_draft_operations'


class TaskDrafts:
    def __init__(self, records, documents):
        self.records, self.documents = records, documents

    def get_operation(self, operation):
        """Read a durable successful create operation from its owned collection."""
        return self.records.read(COLLECTION, operation)

    def create(self, *, turn_id, project, operation, title, markdown):
        with self.records.begin() as tx:
            result = self.create_in_uow(tx, turn_id=turn_id, project=project,
                operation=operation, title=title, markdown=markdown)
            tx.commit()
            return result

    def create_in_uow(self, tx, *, turn_id, project, operation, title, markdown):
        """委托原文档写入者，在调用方原事务内保存草稿和操作回执。"""
        if any(not isinstance(value, str) or not value.strip()
               for value in (turn_id, project, operation, title, markdown)):
            raise ValueError('task draft fields are required')
        if len(title) > 160 or len(markdown) > 200_000:
            raise ValueError('task draft exceeds output limit')
        inputs = dict(turn_id=turn_id, project_id=project, title=title, markdown=markdown)
        current = tx.read(COLLECTION, operation)
        if current is not None:
            if current.payload['inputs'] != inputs:
                raise ValueError('task draft operation changed')
            return dict(current.payload['result'])
        document = self.documents.create_or_replay_generated_in_uow(DocumentDraft(
            title=title, document_type='agent-result-'+turn_id, markdown=markdown,
            project_id=project, source_refs=({'source_id':turn_id, 'locator':'task://'+turn_id},)), tx)
        result = {'document_id':document['id'], 'document_revision':document['revision'],
            'receipt_ref':f'crp://{self.documents.namespace_id}/{COLLECTION}/{operation}'}
        tx.put(COLLECTION, operation, {'inputs':inputs, 'result':result}, expected_revision=0)
        return result
