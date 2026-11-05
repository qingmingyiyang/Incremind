import asyncio
from functools import partial

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.recognition import RecognitionConflict, RecognitionError
from backend.security.device_identity import server_identity, server_mode
from backend.security.user_context import USER_ACCESS, UserError
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..server_jobs import QuotaError, retain_until_finished
from .shared_extraction import PersonalDestination
from .shared_projects import SharedProjectError, revision


def install_shared_extraction_routes(application, *, extraction):
    router = APIRouter(prefix='/api/v2/server/shared-projects')
    context = extraction.server_context

    def identity(request):
        actor = server_identity(request) if server_mode(request) else None
        access = USER_ACCESS.get()
        if actor is None or access is None or access.caller != actor:
            raise HTTPException(401, 'shared_server_identity_required')
        return actor, access.target_user_id

    async def body(request, fields):
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_extraction_request_invalid') from None
        if not isinstance(value, dict) or set(value) != set(fields):
            raise HTTPException(400, 'shared_extraction_request_fields_invalid')
        return value

    async def read(operation, actor, owner, *args):
        async with context.pool.lease(owner) as child:
            destination = PersonalDestination.from_application(extraction.registry, actor, child,
                target_user_id=owner)
            # 客户端断开时，实际读取仍持有个人应用，直到线程释放 SQLite 连接。
            return await retain_until_finished(asyncio.to_thread(
                getattr(extraction, operation), actor, destination, *args))

    async def execute(call):
        try:
            value = await call()
            status = 202 if value.get('state') == 'pending' else 200
        except SharedProjectError as error:
            status, value = error.status, {'detail': str(error)}
        except (RecognitionConflict, SQLiteUnitOfWorkConflict):
            status, value = 409, {'detail': 'shared_extraction_source_changed'}
        except RecognitionError:
            status, value = 400, {'detail': 'shared_extraction_material_invalid'}
        except UserError as error:
            status = 401 if str(error) == 'user_unauthorized' else 403
            value = {'detail': str(error)}
        except QuotaError as error:
            status, value = 429, {'detail': str(error)}
        except (OSError, TimeoutError):
            status, value = 503, {'detail': 'shared_extraction_incomplete'}
        return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})

    @router.post('/{project_id}/extractions')
    async def preview(project_id: str, request: Request):
        actor, owner = identity(request)
        value = await body(request, {'target_project_id', 'recognition_ids'})
        keys = request.headers.getlist('idempotency-key')
        if len(keys) > 1:
            raise HTTPException(400, 'shared_extraction_request_key_invalid')
        return await execute(partial(extraction.run_admitted, 'preview', actor, owner,
            project_id, value['target_project_id'], recognition_ids=value['recognition_ids'],
            request_key=keys[0] if keys else None))

    @router.get('/extraction-commands/{command_id}')
    async def command(command_id: str, request: Request):
        actor, owner = identity(request)
        return await execute(partial(read, 'preview_command', actor, owner, command_id))

    @router.get('/extractions/{preview_id}')
    async def view(preview_id: str, request: Request):
        actor, owner = identity(request)
        return await execute(partial(read, 'view_preview', actor, owner, preview_id))

    @router.post('/extractions/{preview_id}/adopt')
    async def adopt(preview_id: str, request: Request):
        actor, owner = identity(request)
        value = await body(request, {'expected_revision', 'confirmed_ids', 'batch_size'})
        try:
            revision(value['expected_revision'])
        except SharedProjectError as error:
            raise HTTPException(error.status, str(error)) from None
        return await execute(partial(extraction.run_admitted, 'adopt', actor, owner,
            preview_id, **value))

    @router.get('/extractions/{preview_id}/progress')
    async def progress(preview_id: str, request: Request):
        actor, owner = identity(request)
        return await execute(partial(read, 'progress', actor, owner, preview_id))

    @router.post('/extractions/{preview_id}/reconcile')
    async def reconcile(preview_id: str, request: Request):
        actor, owner = identity(request)
        await body(request, set())
        return await execute(partial(extraction.run_admitted, 'reconcile', actor, owner, preview_id))

    @router.get('/extracted-insights/{copy_id}')
    async def retained(copy_id: str, request: Request):
        actor, owner = identity(request)
        return await execute(partial(read, 'retained_copy', actor, owner, copy_id))

    application.include_router(router)
