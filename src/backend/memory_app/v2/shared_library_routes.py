import asyncio
from functools import partial

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.recognition import RecognitionConflict, RecognitionError
from backend.security.device_identity import server_identity, server_mode
from backend.security.user_context import USER_ACCESS, UserError, authorize_user, user_context
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..server_jobs import retain_until_finished
from .shared_projects import SharedProjectError
from .shared_library_files import SharedSourceResponse


_SharedOriginal = SharedSourceResponse


def install_shared_library_routes(application, *, library, context):
    router = APIRouter(prefix='/api/v2/server/shared-projects')

    def identity(request):
        actor = server_identity(request) if server_mode(request) else None
        access = USER_ACCESS.get()
        if actor is None or access is None or access.caller != actor:
            raise HTTPException(401, 'shared_server_identity_required')
        return actor

    async def read(operation, actor, project, **values):
        # 共享资料的读资格属于当前操作人，个人执行 owner 始终取其自己的应用。
        access = authorize_user(context.users, actor, actor.user_id)
        with user_context(access):
            async with context.pool.lease(actor.user_id) as child:
                return await retain_until_finished(asyncio.to_thread(
                    getattr(library, operation), child, actor, project, **values))

    async def execute(call):
        try:
            value = await call()
            if isinstance(value, tuple):
                return _SharedOriginal(*value)
            return JSONResponse(value, headers={'Cache-Control': 'no-store'})
        except SharedProjectError as error:
            status, code = error.status, str(error)
        except (RecognitionConflict, SQLiteUnitOfWorkConflict):
            status, code = 409, 'shared_library_source_changed'
        except RecognitionError:
            status, code = 400, 'shared_library_action_invalid'
        except UserError as error:
            status, code = (401 if str(error) == 'user_unauthorized' else 403), str(error)
        return JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})

    @router.get('/{project_id}/library/originals/{identity_id}')
    async def original(project_id: str, identity_id: str, request: Request):
        actor = identity(request)
        return await execute(partial(read, 'original', actor, project_id, identity=identity_id))

    @router.get('/{project_id}/library/sources/{identity_id}/text')
    async def full_source(project_id: str, identity_id: str, request: Request):
        actor = identity(request)
        return await execute(partial(read, 'full_source', actor, project_id, identity=identity_id))

    @router.get('/{project_id}/library/sources/{identity_id}/images')
    async def images(project_id: str, identity_id: str, request: Request):
        actor = identity(request)
        return await execute(partial(read, 'images', actor, project_id, identity=identity_id))

    @router.get('/{project_id}/library/sources/{identity_id}/images/{ordinal}')
    async def image(project_id: str, identity_id: str, ordinal: int, request: Request):
        actor = identity(request)
        return await execute(partial(read, 'image', actor, project_id, identity=identity_id, ordinal=ordinal))

    @router.get('/{project_id}/library/{layer}')
    async def listing(project_id: str, layer: str, request: Request, scene: str | None = None,
            q: str = '', state: str | None = None):
        actor = identity(request)
        return await execute(partial(read, 'list', actor, project_id,
            layer=layer, scene=scene, q=q, state=state))

    from .shared_insight_merge import install_shared_insight_merges
    install_shared_insight_merges(router, library=library, context=context, identity=identity, execute=execute)

    @router.get('/{project_id}/library/{layer}/{identity_id}')
    async def drill(project_id: str, layer: str, identity_id: str, request: Request,
            document_id: str | None = None, source_id: str | None = None):
        actor = identity(request)
        return await execute(partial(read, 'drill', actor, project_id,
            layer=layer, identity=identity_id, document_id=document_id, source_id=source_id))

    @router.post('/{project_id}/library/insights/{candidate_id}/review')
    async def review(project_id: str, candidate_id: str, request: Request):
        actor = identity(request)
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_library_request_invalid') from None
        if not isinstance(value, dict) or set(value) != {'action', 'expected_revision', 'authority'}:
            raise HTTPException(400, 'shared_library_request_fields_invalid')
        return await execute(partial(run_in_threadpool, library.review, actor, project_id, candidate_id, **value))

    from .shared_insight_maintenance import install_shared_insight_maintenance
    install_shared_insight_maintenance(router, library=library, context=context, identity=identity, execute=execute)
    application.include_router(router)
