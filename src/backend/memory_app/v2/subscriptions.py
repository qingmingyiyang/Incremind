"""Subscription HTTP delivery; OAuth and model execution remain domain services."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool

from core.storage_provider import SQLiteUnitOfWorkConflict
from backend.security.device_identity import server_mode
from backend.security.user_context import USER_ACCESS, UserError, authorize_user
from ..chatgpt_subscription import SubscriptionError
from ..model_config import ModelConfigurationError, _GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID
from .devices import _request_owner, DeviceError


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt


class Selection(Revision):
    model: StrictStr | None
    allow_remote: StrictBool | None = None
    expected_generation_revision: StrictInt | None = None


def _login_owner(request, models):
    try:
        owner = _request_owner(request)
    except DeviceError:
        raise HTTPException(403, 'subscription_identity_unavailable') from None
    state = request.app.state
    access = getattr(models, 'server_model_access', None)
    binding = access.public_selection('generation')['server_model_binding'] if access is not None else None
    def revisions():
        # OAuth 自身持锁时只读修订，避免反向获取模型配置锁。
        with models.records.begin_read() as reader:
            generation = reader.read('recognition_model_config', 'generation')
            mode = reader.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
            return (generation.revision if generation else 0, mode.revision if mode else 0)
    initial_revisions = revisions()
    context = getattr(state, 'server_context', None)
    claim = USER_ACCESS.get()
    identity = request.scope.get('state', {}).get('device_identity')
    dispatched = request.scope.get('state', {}).get('server_dispatch_authority')
    request_access = request.scope.get('state', {}).get('user_access')
    remote = server_mode(request)

    def validate():
        try:
            if remote:
                if (context is None or state.server_context is not context or claim is None
                        or dispatched is not context.auth or request_access != claim
                        or identity != claim.caller or state.server_user_id != owner[0]
                        or context.users.root_for(owner[0]) != state.recognition_runtime_root
                        or context.users.get(owner[0])['disabled_at'] is not None
                        or context.users.deletion_pending(owner[0])
                        or not context.registry.is_current(identity)
                        or authorize_user(context.users, identity, owner[0]) != claim):
                    raise SubscriptionError('subscription_identity_unavailable', 403)
                if access is None or binding is None:
                    raise SubscriptionError('subscription_configuration_changed', 409)
                try:
                    selected = access.validate_binding(binding)
                except ValueError:
                    raise SubscriptionError('subscription_configuration_changed', 409) from None
                if selected['mode'] != 'independent':
                    raise SubscriptionError('subscription_configuration_changed', 409)
            if revisions() != initial_revisions:
                raise SubscriptionError('subscription_configuration_changed', 409)
        except (UserError, ValueError) as error:
            if isinstance(error, SubscriptionError):
                raise
            raise SubscriptionError('subscription_identity_unavailable', 403) from None
    validate()
    return owner, validate


def install_subscription_routes(application, *, models):
    subscriptions = getattr(models, "subscriptions", None)
    if subscriptions is None:
        return
    router = APIRouter(prefix="/api/v2/settings/subscriptions")

    def writable():
        access = getattr(models, 'server_model_access', None)
        if access is not None and access.public_selection('generation')['mode'] == 'shared':
            raise HTTPException(403, 'server_model_configuration_readonly')

    def call(fn, **kwargs):
        try:
            return fn(**kwargs)
        except SubscriptionError as error:
            raise HTTPException(error.status, error.code) from None
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, "subscription_revision_conflict") from None
        except ModelConfigurationError as error:
            code = str(error)
            raise HTTPException(403 if code == 'server_model_configuration_readonly' else
                409 if 'conflict' in code or 'changed' in code else 400, code) from None

    def owner(request):
        try:
            return _request_owner(request)
        except DeviceError:
            raise HTTPException(403, 'subscription_identity_unavailable') from None

    @router.get("")
    def status(request: Request):
        generation = models.public()["generation"]
        return JSONResponse({**subscriptions.status(owner=owner(request)), "selection": models.subscription_selection(),
                "allow_remote": generation["allow_remote"], "generation_revision": generation["revision"],
                "selection_ready": generation["configured"] if generation.get("subscription_binding") else False},
                headers={'Cache-Control': 'no-store'})

    @router.post("/login")
    def login(body: Revision, request: Request):
        writable()
        identity, validate = _login_owner(request, models)
        return call(subscriptions.login, expected_revision=body.expected_revision,
                    owner=identity, validate_owner=validate)

    @router.get("/login/{attempt_id}")
    def progress(attempt_id: str, request: Request):
        return call(subscriptions.attempt, attempt_id=attempt_id, owner=owner(request))

    @router.delete("/login/{attempt_id}")
    def cancel(attempt_id: str, request: Request):
        writable()
        return call(subscriptions.cancel, attempt_id=attempt_id, owner=owner(request))

    @router.post('/login/{attempt_id}/complete')
    async def complete(attempt_id: str, request: Request):
        identity = owner(request)
        writable()
        # 按实际流量计数，不信任 Content-Length；固定错误不回显含授权码的正文。
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 20000:
                raise HTTPException(413, 'subscription_callback_too_large')
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {'callback_url'} or not isinstance(body['callback_url'], str):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, 'subscription_callback_invalid') from None
        result = await run_in_threadpool(call, subscriptions.complete, attempt_id=attempt_id,
                                        callback_url=body['callback_url'], owner=identity)
        return JSONResponse(result, status_code=202 if result['state'] == 'pending' else 200,
                            headers={'Cache-Control': 'no-store'})

    @router.post("/logout")
    def logout(body: Revision):
        writable()
        return call(subscriptions.logout, expected_revision=body.expected_revision)

    @router.get("/models")
    def catalog():
        return {"items": call(subscriptions.models)}

    @router.patch("/selection")
    def select(body: Selection):
        writable()
        return call(models.select_subscription, **body.model_dump())

    application.include_router(router)
    application.router.on_shutdown.append(models.close)
