from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from backend.security.user_context import USER_ACCESS
from .server_model_balance import ServerModelBalanceError


def _response(status, value):
    return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})


async def _body(request, fields):
    try:
        value = await request.json()
    except ValueError:
        raise ServerModelBalanceError('server_model_balance_request_invalid') from None
    if not isinstance(value, dict) or set(value) != fields:
        raise ServerModelBalanceError('server_model_balance_request_invalid')
    return value


def install_server_model_balance_routes(application, *, balances):
    router = APIRouter(prefix='/api/v2/server/users/{user_id}/model-balance')

    @router.get('')
    def read_balance(user_id: str):
        return _response(200, balances.read(USER_ACCESS.get().caller, user_id))

    @router.post('/adjust')
    async def adjust_balance(user_id: str, request: Request):
        data = await _body(request, {'amount', 'expected_revision', 'expected_user_revision', 'reason'})
        return _response(200, balances.adjust(USER_ACCESS.get().caller, user_id, **data))

    @router.get('/attempts')
    def list_attempts(user_id: str, state: str = 'pending', limit: int = Query(50, ge=1, le=200),
                      cursor: str | None = Query(None, max_length=1024)):
        return _response(200, balances.attempts(USER_ACCESS.get().caller, user_id,
            state=state, limit=limit, cursor=cursor))

    @router.post('/attempts/{attempt_id}/reconcile')
    async def reconcile_attempt(user_id: str, attempt_id: str, request: Request):
        data = await _body(request, {'amount', 'expected_revision', 'expected_user_revision', 'reason'})
        return _response(200, balances.reconcile(USER_ACCESS.get().caller, user_id, attempt_id, **data))

    application.include_router(router)

    async def error_response(_request, error):
        code = str(error)
        status = {'server_model_balance_unauthorized': 401, 'server_model_balance_forbidden': 403,
            'server_model_balance_user_not_found': 404, 'server_model_balance_attempt_not_found': 404,
            'server_model_balance_revision_conflict': 409, 'server_model_balance_user_revision_conflict': 409,
            'server_model_balance_reserved_conflict': 409, 'server_model_balance_settlement_conflict': 409,
            'server_model_balance_attempt_not_pending_review': 409,
            'server_model_balance_user_deleting': 409, 'server_model_balance_user_disabled': 403,
            'server_model_balance_insufficient': 402}.get(code, 400)
        return _response(status, {'detail': code})

    application.add_exception_handler(ServerModelBalanceError, error_response)
