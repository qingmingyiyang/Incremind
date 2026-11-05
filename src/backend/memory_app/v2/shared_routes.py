from functools import partial

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.security.device_identity import server_identity, server_mode
from backend.security.user_context import UserError
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..server_jobs import QuotaError
from .shared_projects import SharedProjectError


def install_shared_project_routes(application, *, registry):
    router = APIRouter(prefix='/api/v2/server/shared-projects')

    def actor(request):
        identity = server_identity(request) if server_mode(request) else None
        if identity is None:
            raise HTTPException(401, 'shared_server_identity_required')
        return identity

    async def body(request, fields):
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_request_invalid') from None
        if not isinstance(value, dict) or set(value) != set(fields):
            raise HTTPException(400, 'shared_request_fields_invalid')
        return value

    async def execute(call, *, operation_id=None, pending_status=202):
        try:
            value = await run_in_threadpool(call)
            pending = isinstance(value, dict) and value.get('state') == 'pending'
            return JSONResponse(value, status_code=pending_status if pending else 200, headers={'Cache-Control': 'no-store'})
        except SharedProjectError as error:
            return JSONResponse({'detail': str(error), 'operation_id': error.operation_id or operation_id},
                                status_code=error.status, headers={'Cache-Control': 'no-store'})
        except SQLiteUnitOfWorkConflict:
            return JSONResponse({'detail': 'shared_revision_conflict', 'operation_id': operation_id}, status_code=409)
        except QuotaError as error:
            return JSONResponse({'detail': str(error), 'operation_id': operation_id}, status_code=429,
                                headers={'Cache-Control': 'no-store'})
        except UserError as error:
            code = str(error)
            status = 401 if code == 'user_unauthorized' else 403
            return JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})
        except (OSError, TimeoutError):
            return JSONResponse({'detail': 'shared_operation_incomplete', 'operation_id': operation_id},
                                status_code=503, headers={'Cache-Control': 'no-store'})

    @router.get('')
    async def projects(request: Request):
        return await execute(partial(registry.list, actor(request)))

    @router.post('')
    async def create(request: Request):
        value = await body(request, {'project_name', 'operation_id'})
        return await execute(partial(registry.create, actor(request), **value), operation_id=value['operation_id'])

    @router.get('/users')
    async def users(request: Request):
        return await execute(partial(registry.directory, actor(request)))

    @router.get('/invitations')
    async def invitations(request: Request):
        return await execute(partial(registry.invitations, actor(request)))

    @router.post('/invitations/{invitation_id}/accept')
    async def accept(invitation_id: str, request: Request):
        value = await body(request, {'expected_revision', 'operation_id'})
        return await execute(partial(registry.accept, actor(request), invitation_id, **value),
                             operation_id=value['operation_id'])

    @router.post('/operations/{operation_id}/resume')
    async def resume(operation_id: str, request: Request):
        await body(request, set())
        return await execute(partial(registry.finish, actor(request), operation_id), operation_id=operation_id)

    @router.get('/operations/{operation_id}')
    async def operation(operation_id: str, request: Request):
        return await execute(partial(registry.operation, actor(request), operation_id), operation_id=operation_id, pending_status=200)

    @router.post('/operations/{operation_id}/cancel-unreceived')
    async def cancel_unreceived(operation_id: str, request: Request):
        value = await body(request, {'action', 'project_id', 'inputs'})
        return await execute(partial(registry.cancel_unreceived, actor(request), operation_id, **value), operation_id=operation_id)

    @router.get('/{project_id}')
    async def detail(project_id: str, request: Request):
        return await execute(partial(registry.detail, actor(request), project_id))

    @router.get('/{project_id}/operations')
    async def pending(project_id: str, request: Request):
        return await execute(partial(registry.pending, actor(request), project_id))

    @router.get('/{project_id}/activity')
    async def activity(project_id: str, request: Request, actor_user_id: str | None = None):
        return await execute(partial(registry.activity, actor(request), project_id, actor_user_id=actor_user_id))

    @router.post('/{project_id}/invitations')
    async def invite(project_id: str, request: Request):
        value = await body(request, {'user_id', 'role', 'authority', 'operation_id'})
        return await execute(partial(registry.invite, actor(request), project_id, **value),
                             operation_id=value['operation_id'])

    @router.post('/{project_id}/members/{user_id}')
    async def change(project_id: str, user_id: str, request: Request):
        value = await body(request, {'action', 'role', 'expected_revision', 'authority', 'operation_id'})
        return await execute(partial(registry.change_member, actor(request), project_id, user_id=user_id, **value),
                             operation_id=value['operation_id'])

    @router.post('/{project_id}/delete')
    async def delete(project_id: str, request: Request):
        value = await body(request, {'confirmation', 'authority', 'operation_id', 'skip_backup'})
        return await execute(partial(registry.delete, actor(request), project_id, **value),
                             operation_id=value['operation_id'])

    application.include_router(router)
