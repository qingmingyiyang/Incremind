"""Create-only task output, atomically recorded with its operation receipt."""
from core.document_engine.ports import DocumentDraft

COLLECTION = 'v2_task_draft_operations'


class TaskDrafts:
    def __init__(self, records, documents):
        self.records, self.documents = records, documents

    def get_operation(self, operation):
        """Read a durable successful create operation from its owned collection."""
        return self.records.read(COLLECTION, operation)

    def create(self, *, turn_id, project, operation, title, markdown, source_request=None):
        with self.records.begin() as tx:
            result = self.create_in_uow(tx, turn_id=turn_id, project=project,
                operation=operation, title=title, markdown=markdown, source_request=source_request)
            tx.commit()
            return result

    def create_in_uow(self, tx, *, turn_id, project, operation, title, markdown, generated_files=None, source_request=None):
        """委托原文档写入者，在调用方原事务内保存草稿和操作回执。"""
        if any(not isinstance(value, str) or not value.strip()
               for value in (turn_id, project, operation, title, markdown)):
            raise ValueError('task draft fields are required')
        if len(title) > 160 or len(markdown) > 200_000:
            raise ValueError('task draft exceeds output limit')
        inputs = dict(turn_id=turn_id, project_id=project, title=title, markdown=markdown)
        if generated_files is not None:
            from ..workspace_intake import GeneratedTaskFiles
            if (type(generated_files) is not GeneratedTaskFiles or generated_files.proof.records is not self.records
                    or generated_files.proof.request['turn_id'] != turn_id
                    or generated_files.proof.request['scope']['project_id'] != project):
                raise ValueError('generated_file_authority_invalid')
            generated_files.validate(tx)
        current = tx.read(COLLECTION, operation)
        if current is not None:
            if current.payload['inputs'] != inputs:
                raise ValueError('task draft operation changed')
            if generated_files is not None:
                generated_files.enlist(tx, operation, current.payload['result'])
            self._scope(tx, current.payload['result']['document_id'], project, source_request)
            return dict(current.payload['result'])
        document = self.documents.create_or_replay_generated_in_uow(DocumentDraft(
            title=title, document_type='agent-result-'+turn_id, markdown=markdown,
            project_id=project, source_refs=({'source_id':turn_id, 'locator':'task://'+turn_id},)), tx)
        result = {'document_id':document['id'], 'document_revision':document['revision'],
            'receipt_ref':f'crp://{self.documents.namespace_id}/{COLLECTION}/{operation}'}
        tx.put(COLLECTION, operation, {'inputs':inputs, 'result':result}, expected_revision=0)
        self._scope(tx, document['id'], project, source_request)
        if generated_files is not None:
            generated_files.enlist(tx, operation, result)
        return result

    def _scope(self, tx, identity, project, source_request):
        from .material_logical_scope import bind_material_scope_in_uow
        return bind_material_scope_in_uow(tx, self.records, material_type='document', identity=identity,
            project_id=project, namespace_id=self.documents.namespace_id, source_request=source_request)

    def attach_generated_in_uow(self, tx, *, generated_files, result, project):
        from core.document_engine import SQLiteDocumentRepository
        from core.storage_provider.record_lineage import capture_lineage
        from ..document_recognition import ensure_document_experience
        from ..source_egress import recognition_service
        from ..transaction_records import TransactionRecords
        from ..workspace_contracts import _now
        generated_files.validate_publication(tx, result['document_id'])
        generated_files.check_managed_files()
        enlisted = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=self.documents.namespace_id, now=_now())
        experience_id, _ = ensure_document_experience(enlisted,
            recognition_service(TransactionRecords(tx), source_owner=self.records), project,
            result['document_id'], retained_revision=result['document_revision'])
        parent = tx.read('recognition_experiences', experience_id)
        births = generated_files.save_in_uow(tx, result['document_id'], parent)
        tx.put('v2_external_task_attachments', result['document_id'], {'project_id': project,
            'document_id': result['document_id'], 'birth_revision': result['document_revision'],
            'turn_id': generated_files.proof.request['turn_id'], 'product_id': generated_files.proof.product_id,
            'owner_id': generated_files.proof.value['binding']['owner_id'],
            'product_owner': generated_files.proof.owner, 'operation': generated_files._publication[1],
            'immutable_ref': generated_files.inputs['immutable_ref'], 'files': generated_files.attachments,
            'source_births': births, 'parent_experience': {'id': parent.object_id, 'revision': parent.revision,
                'lineage': capture_lineage(tx, 'recognition_experiences', parent.object_id)}}, expected_revision=0)

    def create_restored_in_uow(self, tx, *, project, operation, title, markdown):
        if (not isinstance(operation, str) or not operation.startswith('restore-')
                or any(not isinstance(value, str) or not value.strip() for value in (project, title, markdown))):
            raise ValueError('restored draft fields are required')
        return self.documents.create_or_replay_generated_in_uow(DocumentDraft(
            title=title, document_type='agent-result-' + operation, markdown=markdown,
            project_id=project, source_refs=({'source_id': operation, 'locator': 'outcome://' + operation},)), tx)

    def create_answer_in_uow(self, tx, *, project, turn_id, title, markdown):
        if any(not isinstance(value, str) or not value.strip() for value in (project, turn_id, title, markdown)):
            raise ValueError('saved answer draft fields are required')
        document = self.documents.create_or_replay_generated_in_uow(DocumentDraft(
            title=title, document_type='agent-result-answer-' + turn_id, markdown=markdown,
            project_id=project, source_refs=({'source_id': turn_id, 'locator': 'conversation://' + turn_id},)), tx)
        self._scope(tx, document['id'], project, None)
        return document
