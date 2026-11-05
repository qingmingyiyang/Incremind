from fastapi import APIRouter, HTTPException, Request

from backend.recognition import RecognitionConflict
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.connection_scope import with_connection_scope

from ..workspace_contracts import _json
from .shared_projects import SharedProjectError


def install_outcome_freshness_routes(application, *, service):
    router = APIRouter(prefix='/api/v2/library/outcomes')

    def fail(error):
        if isinstance(error, SharedProjectError):
            return HTTPException(error.status, str(error))
        if isinstance(error, (RecognitionConflict, SQLiteUnitOfWorkConflict)):
            return HTTPException(409, str(error))
        return HTTPException(400, 'invalid_outcome_change_request')

    async def body_for(request):
        body = await _json(request)
        if (not isinstance(body, dict) or set(body) != {'project_id', 'expected_revision', 'observation'}
                or not isinstance(body['project_id'], str) or not body['project_id']
                or type(body['expected_revision']) is not int or body['expected_revision'] < 0
                or not isinstance(body['observation'], dict)):
            raise HTTPException(400, 'invalid_outcome_change_request')
        return body

    @router.get('/{document_id}/changes')
    @with_connection_scope
    def changes(document_id: str, project_id: str):
        try:
            return service.read_changes(project=project_id, document_id=document_id)
        except (ValueError, RecognitionConflict) as error:
            raise fail(error) from None

    @router.post('/{document_id}/ack')
    @with_connection_scope
    async def ack(document_id: str, request: Request):
        body = await body_for(request)
        try:
            with service.outcome_scope(project=body['project_id'], document_id=document_id), service.records.begin() as tx:
                service.ack_in_uow(tx, project=body['project_id'], document_id=document_id,
                    expected_revision=body['expected_revision'], observation=body['observation'])
                tx.commit()
            return service.read_changes(project=body['project_id'], document_id=document_id)
        except (ValueError, RecognitionConflict, SQLiteUnitOfWorkConflict) as error:
            raise fail(error) from None

    @router.post('/{document_id}/update')
    @with_connection_scope
    async def update(document_id: str, request: Request):
        body = await body_for(request)
        submission_key = request.headers.get('Idempotency-Key')
        if not isinstance(submission_key, str) or not submission_key:
            raise HTTPException(400, 'invalid_outcome_update_key')
        try:
            with service.outcome_scope(project=body['project_id'], document_id=document_id), service.records.begin() as tx:
                result = service.prepare_update_in_uow(tx, project=body['project_id'], document_id=document_id,
                    expected_revision=body['expected_revision'], observation=body['observation'], submission_key=submission_key)
                tx.commit()
            return result
        except (ValueError, RecognitionConflict, SQLiteUnitOfWorkConflict) as error:
            if isinstance(error, ValueError) and str(error) == 'invalid_outcome_update_key':
                raise HTTPException(400, 'invalid_outcome_update_key') from None
            raise fail(error) from None

    application.include_router(router)
