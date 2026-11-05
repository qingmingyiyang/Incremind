from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..source_egress import recognition_service
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _json, _project
from ..recall_state import is_recall_excluded


OPERATIONS = 'v2_library_merge_operations'


class MergeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: StrictStr
    operation_id: StrictStr
    candidate_id: StrictStr
    candidate_revision: StrictInt = Field(ge=1)
    target_id: StrictStr
    target_revision: StrictInt = Field(ge=1)
    content: StrictStr = Field(min_length=1, max_length=20000)
    conditions: list[StrictStr] = Field(max_length=64)


def _identity(value):
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise RecognitionError('library_merge_operation_invalid') from None
    if str(parsed) != value:
        raise RecognitionError('library_merge_operation_invalid')
    return value


class LibraryMerges:
    def __init__(self, records):
        self.records = records

    def _service(self, tx):
        return recognition_service(TransactionRecords(tx), source_owner=self.records)

    def _read(self, tx, project, operation):
        row = tx.read(OPERATIONS, operation)
        if row is None or row.payload.get('project_id') != project or row.payload.get('user_id') != 'local-user':
            return None
        if (set(row.payload) != {'schema_version', 'operation_id', 'project_id', 'user_id', 'state', 'inputs', 'proposal_id'}
                or type(row.payload['schema_version']) is not int or row.payload['schema_version'] != 1
                or row.revision != 1 or row.payload['operation_id'] != operation
                or row.payload['state'] not in {'prepared', 'cancelled'}):
            raise RecognitionError('library_merge_operation_invalid')
        if row.payload['state'] == 'prepared':
            try:
                saved = MergeInput.model_validate(row.payload['inputs'])
            except ValidationError:
                raise RecognitionError('library_merge_operation_invalid') from None
            if (saved.operation_id != operation or saved.project_id != project
                    or row.payload['proposal_id'] != 'library-merge-' + UUID(operation).hex):
                raise RecognitionError('library_merge_operation_invalid')
        elif row.payload['inputs'] is not None or row.payload['proposal_id'] is not None:
            raise RecognitionError('library_merge_operation_invalid')
        return row

    def _result(self, tx, row):
        result = {'operation_id': row.object_id, 'project_id': row.payload['project_id'],
            'revision': row.revision, 'state': row.payload['state'], 'proposal': None}
        if row.payload['state'] == 'prepared':
            authority = RestructureProposalService(self._service(tx))
            scope = WorkScope('local-user', row.payload['project_id'])
            proposal = next((item for item in authority.list(scope=scope) if item['id'] == row.payload['proposal_id']), None)
            if proposal is None:
                raise RecognitionConflict('library_merge_proposal_unavailable')
            result['proposal'] = proposal
        return result

    def read(self, project, operation):
        project, operation = _project(project), _identity(operation)
        with self.records.begin_read() as tx:
            row = self._read(tx, project, operation)
            return self._result(tx, row) if row else None

    def prepare(self, data):
        project, operation = _project(data.project_id), _identity(data.operation_id)
        inputs = data.model_dump(mode='json')
        scope = WorkScope('local-user', project)
        with self.records.begin() as tx:
            previous = self._read(tx, project, operation)
            if previous:
                if previous.payload['state'] != 'prepared' or previous.payload['inputs'] != inputs:
                    raise RecognitionConflict('library_merge_operation_changed')
                return self._result(tx, previous)
            records = TransactionRecords(tx)
            service = self._service(tx)
            candidate = tx.read('recognition_candidates', data.candidate_id)
            target = tx.read('recognitions', data.target_id)
            expected_scope = {'user_id': scope.user_id, 'project_id': scope.project_id}
            if (candidate is None or candidate.payload.get('scope') != expected_scope
                    or candidate.payload.get('state') != 'pending' or candidate.revision != data.candidate_revision
                    or tx.read('v2_candidate_merges', data.candidate_id) or tx.read('v2_candidate_fade', data.candidate_id)
                    or target is None or target.payload.get('scope') != expected_scope
                    or target.payload.get('state') != 'active' or target.revision != data.target_revision
                    or is_recall_excluded(records, scope, data.target_id)):
                raise RecognitionConflict('library_merge_inputs_changed')
            qualified = service._qualified_recognition(tx, target)
            if not qualified.authorized or qualified.effective_state != 'active':
                raise RecognitionConflict('library_merge_inputs_changed')
            hint = tx.read('v2_candidate_hints', data.candidate_id)
            if (not hint or hint.payload.get('target_id') != data.target_id
                    or hint.payload.get('project_id') != project or hint.payload.get('relation') != 'duplicate_of'):
                raise RecognitionConflict('library_merge_target_changed')
            authority = RestructureProposalService(service)
            snapshot = authority.capture(scope=scope, recognition_ids=[data.candidate_id, data.target_id],
                expected_revisions={data.candidate_id: data.candidate_revision, data.target_id: data.target_revision},
                pending_output=True)
            proposal_id = 'library-merge-' + UUID(operation).hex
            authority.save_in_uow(tx, scope=scope, proposal_id=proposal_id, snapshot=snapshot, operation='merge',
                outputs=[{'content': data.content, 'conditions': data.conditions,
                    'source_experience_ids': [item['id'] for item in snapshot['experiences']],
                    'source_recognition_ids': [item['id'] for item in snapshot['recognitions']
                        if item['id'] not in snapshot['target_recognition_ids']]}],
                reason='用户预览合并待确认认识与已有认识', step_metadata={'source': 'library-review'})
            row = tx.put(OPERATIONS, operation, {'schema_version': 1, 'operation_id': operation,
                'project_id': project, 'user_id': 'local-user', 'state': 'prepared', 'inputs': inputs,
                'proposal_id': proposal_id}, expected_revision=0)
            result = self._result(tx, row)
            tx.commit()
            return result

    def cancel(self, project, operation):
        project, operation = _project(project), _identity(operation)
        with self.records.begin() as tx:
            row = self._read(tx, project, operation)
            if row:
                if row.payload['state'] != 'cancelled':
                    raise RecognitionConflict('library_merge_already_prepared')
                return self._result(tx, row)
            row = tx.put(OPERATIONS, operation, {'schema_version': 1, 'operation_id': operation,
                'project_id': project, 'user_id': 'local-user', 'state': 'cancelled', 'inputs': None,
                'proposal_id': None}, expected_revision=0)
            result = self._result(tx, row)
            tx.commit()
            return result


def install_library_merge_routes(application, *, records):
    router = APIRouter(prefix='/api/v2/library/insight-merges')
    owner = LibraryMerges(records)

    def invoke(action):
        try:
            return action()
        except (RecognitionConflict, SQLiteUnitOfWorkConflict):
            raise HTTPException(409, 'library_merge_changed') from None
        except RecognitionError:
            raise HTTPException(400, 'library_merge_invalid') from None

    @router.post('/prepare')
    async def prepare(request: Request):
        try:
            data = MergeInput.model_validate(await _json(request))
        except ValidationError:
            raise HTTPException(400, 'library_merge_invalid') from None
        return invoke(lambda: owner.prepare(data))

    @router.get('/{operation_id}')
    def read(operation_id: str, project_id: str):
        result = invoke(lambda: owner.read(project_id, operation_id))
        if result is None:
            raise HTTPException(404, 'library_merge_not_found')
        return result

    @router.post('/{operation_id}/cancel')
    async def cancel(operation_id: str, request: Request):
        data = await _json(request)
        if (not isinstance(data, dict) or set(data) != {'project_id', 'expected_revision'} or type(data['expected_revision']) is not int
                or data['expected_revision'] != 0):
            raise HTTPException(400, 'library_merge_invalid')
        return invoke(lambda: owner.cancel(data['project_id'], operation_id))

    application.include_router(router)
