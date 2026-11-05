import sqlite3
from functools import partial
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.recognition import RecognitionConflict
from backend.security.device_identity import server_identity, server_mode
from backend.security.user_context import USER_ACCESS, UserError
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.record_lineage import RecordLineageError

from ..server_jobs import QuotaError
from .shared_projects import SharedProjectError


def install_conversation_share_routes(application, *, shares):
    router = APIRouter(prefix='/api/v2/server/conversation-shares')

    def identity(request):
        actor = server_identity(request) if server_mode(request) else None
        access = USER_ACCESS.get()
        if actor is None or access is None or access.caller != actor:
            raise HTTPException(401, 'conversation_server_identity_required')
        return access

    def command(request):
        keys = request.headers.getlist('idempotency-key')
        if len(keys) != 1:
            raise HTTPException(400, 'conversation_command_invalid')
        return keys[0]

    async def body(request, fields):
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, 'conversation_share_request_invalid') from None
        if not isinstance(value, dict) or set(value) != set(fields):
            raise HTTPException(400, 'conversation_share_request_invalid')
        return value

    async def execute(operation):
        try:
            value = await operation()
            status = 200
        except (SharedProjectError, HTTPException) as error:
            status = error.status if isinstance(error, SharedProjectError) else error.status_code
            value = {'detail': str(error) if isinstance(error, SharedProjectError) else error.detail}
        except UserError as error:
            status, value = (401 if str(error) == 'user_unauthorized' else 403), {'detail': str(error)}
        except (RecognitionConflict, SQLiteUnitOfWorkConflict, RecordLineageError):
            status, value = 409, {'detail': 'conversation_share_source_changed'}
        except QuotaError as error:
            status, value = 429, {'detail': str(error)}
        except (OSError, TimeoutError, sqlite3.Error):
            status, value = 503, {'detail': 'conversation_share_requires_check'}
        return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})

    @router.get('')
    async def list_shares(request: Request, side: Literal['inbox', 'sent'] = 'inbox', turn_id: str | None = None):
        return await execute(partial(run_in_threadpool, shares.list, identity(request), side=side, turn_id=turn_id))

    @router.get('/preview/{turn_id}')
    async def preview(turn_id: str, project_id: str, scope_kind: Literal['personal', 'shared'], request: Request):
        return await execute(partial(shares.preview, identity(request), turn_id, project_id, scope_kind))

    @router.get('/commands/{key}')
    async def command_status(key: str, request: Request):
        return await execute(partial(shares.command, identity(request), key))

    @router.post('')
    async def send(request: Request):
        access, key = identity(request), command(request)
        values = await body(request, ('turn_id', 'project_id', 'scope_kind', 'source_revision', 'to_user_id', 'include_citations'))
        return await execute(partial(shares.send, access, key, values))

    @router.get('/projects/{project_id}')
    async def project_target(project_id: str, request: Request):
        return await execute(partial(shares.project_target, identity(request), project_id))

    @router.post('/projects/{project_id}')
    async def share_project(project_id: str, request: Request):
        access, key = identity(request), command(request)
        values = await body(request, ('turn_id', 'project_id', 'scope_kind', 'source_revision',
            'include_citations', 'namespace', 'authority'))
        return await execute(partial(shares.share_project, access, project_id, key, values))

    @router.get('/projects/{project_id}/commands/{key}')
    async def project_command(project_id: str, key: str, namespace: str, request: Request):
        return await execute(partial(shares.project_command, identity(request), project_id, key, namespace))

    @router.get('/{share_id}')
    async def detail(share_id: str, request: Request):
        return await execute(partial(run_in_threadpool, shares.detail, identity(request), share_id))

    @router.post('/{share_id}/file')
    async def file(share_id: str, request: Request):
        access, key = identity(request), command(request)
        values = await body(request, ('project_id', 'expected_revision'))
        return await execute(partial(shares.archive, access, share_id, key,
            project=values['project_id'], expected_revision=values['expected_revision']))

    @router.post('/{share_id}/revoke')
    async def revoke(share_id: str, request: Request):
        access, key = identity(request), command(request)
        values = await body(request, ('expected_revision',))
        return await execute(partial(shares.revoke, access, share_id, key, **values))

    @router.get('/{share_id}/citations/{ordinal}')
    async def citation(share_id: str, ordinal: int, request: Request):
        return await execute(partial(shares.drill, identity(request), share_id, ordinal))

    application.include_router(router)
