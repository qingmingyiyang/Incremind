from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.recognition import RecognitionConflict
from backend.security.device_identity import server_identity, server_mode
from backend.security.user_context import USER_ACCESS, UserError
from .shared_projects import SharedProjectError


def install_shared_scope_routes(application, *, scopes):
    router = APIRouter(prefix='/api/v2/server/shared-projects')

    @router.get('/scopes')
    async def shared_scopes(request: Request):
        actor = server_identity(request) if server_mode(request) else None
        access = USER_ACCESS.get()
        if actor is None or access is None or access.caller != actor:
            raise HTTPException(401, 'shared_server_identity_required')
        try:
            value = await scopes.list(actor, access.target_user_id)
            status = 200
        except SharedProjectError as error:
            status, value = error.status, {'detail': str(error)}
        except RecognitionConflict:
            status, value = 409, {'detail': 'shared_scope_history_changed'}
        except UserError as error:
            status = 401 if str(error) == 'user_unauthorized' else 403
            value = {'detail': str(error)}
        return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})

    application.include_router(router)
