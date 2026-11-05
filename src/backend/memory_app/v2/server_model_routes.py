from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.security.user_context import USER_ACCESS
from .server_models import ServerModelError


def _response(status, value):
    return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})


async def _body(request, allowed, required):
    try:
        value = await request.json()
    except ValueError:
        raise ServerModelError('server_model_request_invalid') from None
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise ServerModelError('server_model_request_invalid')
    return value


def _actor():
    access = USER_ACCESS.get()
    if access is None:
        raise ServerModelError('server_model_unauthorized')
    return access.caller


def install_server_model_routes(application, *, models):
    router = APIRouter(prefix='/api/v2/server')
    required_fields = {'name', 'purpose', 'provider', 'base_url', 'model', 'allowed_models', 'enabled'}
    fields = required_fields | {'prices'}

    @router.get('/model-profiles')
    def list_profiles():
        return _response(200, {'items': models.list_profiles(_actor())})

    @router.post('/model-profiles')
    async def create_profile(request: Request):
        data = await _body(request, fields | {'api_key'}, required_fields)
        return _response(201, models.create_profile(_actor(), **data))

    @router.put('/model-profiles/{profile_id}')
    async def update_profile(profile_id: str, request: Request):
        data = await _body(request, fields | {'api_key', 'clear_api_key', 'expected_revision'}, {'expected_revision'})
        return _response(200, models.update_profile(_actor(), profile_id, **data))

    @router.get('/model-profiles/{profile_id}/metering-prices')
    def get_metering_prices(profile_id: str):
        return _response(200, models.metering_prices(_actor(), profile_id))

    @router.put('/model-profiles/{profile_id}/metering-prices')
    async def update_metering_prices(profile_id: str, request: Request):
        fields = {'models', 'expected_revision', 'expected_profile_revision'}
        data = await _body(request, fields, fields)
        return _response(200, models.update_metering_prices(_actor(), profile_id, **data))

    @router.get('/users/{user_id}/model-allocation')
    def get_allocation(user_id: str):
        return _response(200, models.allocation(_actor(), user_id))

    @router.put('/users/{user_id}/model-allocation')
    async def update_allocation(user_id: str, request: Request):
        required = {'mode', 'profiles', 'expected_revision', 'expected_user_revision'}
        data = await _body(request, required, required)
        return _response(200, models.update_allocation(_actor(), user_id, **data))

    async def model_error(_request, error):
        code = str(error)
        status = {'server_model_unauthorized': 401, 'server_model_forbidden': 403,
            'server_model_profile_not_found': 404, 'server_model_user_not_found': 404,
            'server_model_revision_conflict': 409, 'server_model_user_revision_conflict': 409,
            'server_model_user_deleting': 409, 'server_model_secret_unavailable': 503}.get(code, 400)
        return _response(status, {'detail': code})

    application.add_exception_handler(ServerModelError, model_error)
    application.include_router(router)
