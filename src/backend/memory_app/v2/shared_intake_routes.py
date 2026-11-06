import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.recognition import RecognitionConflict, RecognitionError
from backend.security.device_identity import server_identity, server_mode
from backend.security.user_context import USER_ACCESS, UserError
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.record_lineage import RecordLineageError
from ..server_jobs import QuotaError, retain_until_finished
from .shared_projects import SharedProjectError
from .shared_document_filing import SharedDocumentFiling


def install_shared_intake_routes(application, *, intake, context):
    router = APIRouter(prefix='/api/v2/server/shared-projects')
    filing = SharedDocumentFiling(intake)

    async def execute(request, operation, project, key, body=None, *, document=None):
        actor = server_identity(request) if server_mode(request) else None
        access = USER_ACCESS.get()
        if actor is None or access is None or access.caller != actor:
            raise HTTPException(401, 'shared_server_identity_required')
        try:
            async with context.pool.lease(access.target_user_id) as child:
                if operation == 'file':
                    value = await filing.move(child, actor, project, document, key, body)
                elif operation == 'file_command':
                    value = await retain_until_finished(asyncio.to_thread(filing.command, child, actor, project, document, key))
                elif operation == 'file_target':
                    value = await retain_until_finished(asyncio.to_thread(filing.target, child, actor, project))
                elif operation in {'text', 'inspiration'}:
                    value = await getattr(intake, operation)(child, actor, project, body, key)
                elif operation == 'process':
                    value = await intake.process(child, actor, project, key, body)
                elif operation in {'draft', 'confirm', 'recognition'}:
                    from .shared_intake_review import review_shared_intake
                    value = await review_shared_intake(intake, child, actor, project, key, body, operation=operation)
                else:
                    value = await retain_until_finished(asyncio.to_thread(getattr(intake, operation), child, actor, project, key))
            return JSONResponse(value, headers={'Cache-Control': 'no-store'})
        except SharedProjectError as error:
            status, code = error.status, str(error)
        except (RecognitionConflict, SQLiteUnitOfWorkConflict, RecordLineageError):
            status, code = 409, 'shared_filing_source_changed' if document else 'shared_intake_source_changed'
        except RecognitionError:
            if document is None:
                raise
            status, code = 409, 'shared_filing_source_changed'
        except QuotaError as error:
            status, code = 429, str(error)
        except UserError as error:
            status, code = 403, str(error)
        return JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})

    @router.post('/{project_id}/filings/{document_id}')
    async def file_document(project_id: str, document_id: str, request: Request):
        key = request.headers.get('Idempotency-Key')
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_filing_fields_invalid') from None
        return await execute(request, 'file', project_id, key, body, document=document_id)

    @router.get('/{project_id}/filing-target')
    async def filing_target(project_id: str, request: Request):
        return await execute(request, 'file_target', project_id, None)

    @router.get('/{project_id}/filings/{document_id}/commands/{key}')
    async def filing_command(project_id: str, document_id: str, key: str, request: Request):
        return await execute(request, 'file_command', project_id, key, document=document_id)

    @router.post('/{project_id}/intake/text')
    async def text(project_id: str, request: Request):
        key = request.headers.get('Idempotency-Key')
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_intake_request_invalid') from None
        return await execute(request, 'text', project_id, key, body)

    @router.post('/{project_id}/intake/inspiration')
    async def inspiration(project_id: str, request: Request):
        key = request.headers.get('Idempotency-Key')
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_intake_request_invalid') from None
        return await execute(request, 'inspiration', project_id, key, body)

    @router.get('/{project_id}/intake/commands/{key}')
    async def command(project_id: str, key: str, request: Request):
        return await execute(request, 'command', project_id, key)

    @router.get('/{project_id}/intake/items/{identity}')
    async def item(project_id: str, identity: str, request: Request):
        return await execute(request, 'item', project_id, identity)

    @router.post('/{project_id}/intake/items/{identity}/process')
    async def process(project_id: str, identity: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_intake_request_invalid') from None
        return await execute(request, 'process', project_id, identity, body)

    @router.put('/{project_id}/intake/items/{identity}/draft')
    async def draft(project_id: str, identity: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_intake_request_invalid') from None
        return await execute(request, 'draft', project_id, identity, body)

    @router.post('/{project_id}/intake/items/{identity}/confirm')
    async def confirm(project_id: str, identity: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_intake_request_invalid') from None
        return await execute(request, 'confirm', project_id, identity, body)

    @router.post('/{project_id}/intake/items/{identity}/recognition')
    async def recognition(project_id: str, identity: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_intake_request_invalid') from None
        return await execute(request, 'recognition', project_id, identity, body)

    application.include_router(router)
