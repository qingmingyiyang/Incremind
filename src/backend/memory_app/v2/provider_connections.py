from fastapi import APIRouter, HTTPException, Request

from core.storage_provider import SQLiteUnitOfWorkConflict
from ..model_config import ModelConfigurationError
from ..workspace_contracts import _json


class ProviderConnections:
    def __init__(self, registry, models):
        if (getattr(registry, '_connection_owner', None) is not models
                or registry.records is not models.records or registry.secrets is not models.secrets):
            raise ValueError('provider_connection_owner_invalid')
        self.registry, self.models = registry, models

    def list(self):
        return {'items': self.registry.connections()}

    def save(self, identity, body):
        if set(body) != {'name', 'base_url', 'models', 'api_key', 'expected_revision', 'operation_id'}:
            raise ValueError('provider_connection_invalid')
        return self.registry.save_connection(identity,
            {key: body[key] for key in ('name', 'base_url', 'models')},
            api_key=body['api_key'], expected_revision=body['expected_revision'], operation_id=body['operation_id'])

    def disconnect(self, identity, body):
        if set(body) != {'expected_revision', 'operation_id'}:
            raise ValueError('provider_connection_invalid')
        return self.registry.disconnect_connection(identity, **body)

    def select(self, identity, body):
        if set(body) != {'purpose', 'model', 'expected_revision', 'expected_configuration_revision', 'expected_mode_revision', 'operation_id'}:
            raise ValueError('provider_connection_invalid')
        return self.models.select_provider_connection(identity, **body)


def install_provider_connection_routes(application, *, registry, models):
    domain = ProviderConnections(registry, models)
    router = APIRouter(prefix='/api/v2/settings/provider-connections')

    @router.get('')
    async def connection_list():
        return domain.list()

    @router.get('/operations/{operation_id}')
    async def connection_operation(operation_id: str):
        try:
            return registry.connection_operation(operation_id)
        except ValueError:
            raise HTTPException(400, 'provider_connection_invalid') from None

    @router.post('/operations/{operation_id}/cancel')
    async def connection_operation_cancel(operation_id: str, request: Request):
        if await _json(request) != {}:
            raise HTTPException(400, 'provider_connection_invalid')
        try:
            return registry.connection_operation(operation_id, cancel=True)
        except ValueError:
            raise HTTPException(400, 'provider_connection_invalid') from None

    async def write(request, action):
        body = await _json(request)
        try:
            return action(body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'provider_connection_revision_conflict') from None
        except (ValueError, ModelConfigurationError) as error:
            code = str(error)
            permitted = code.startswith(('provider_connection_', 'server_model_', 'select_api_'))
            raise HTTPException(403 if code == 'server_model_configuration_readonly' else
                409 if code.endswith(('_changed', '_conflict')) else 400,
                code if permitted else 'provider_connection_invalid') from None

    @router.put('/{identity}')
    async def connection_save(identity: str, request: Request):
        return await write(request, lambda body: domain.save(identity, body))

    @router.post('/{identity}/disconnect')
    async def connection_disconnect(identity: str, request: Request):
        return await write(request, lambda body: domain.disconnect(identity, body))

    @router.post('/{identity}/selection')
    async def connection_select(identity: str, request: Request):
        return await write(request, lambda body: domain.select(identity, body))

    application.include_router(router)
