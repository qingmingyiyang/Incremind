import json

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from backend.security.device_identity import server_identity
from backend.security.user_context import USER_ACCESS
from core.storage_provider import SQLiteUnitOfWorkConflict

from .android_attestation import AttestationError
from .android_mobile import MobileError


async def _body(request, fields):
    parts, size = [], 0
    async for part in request.stream():
        size += len(part)
        if size > 170_000:
            raise HTTPException(413, 'mobile_request_too_large')
        parts.append(part)
    try:
        value = json.loads(b''.join(parts))
    except (ValueError, RecursionError):
        raise HTTPException(400, 'mobile_request_invalid') from None
    if not isinstance(value, dict) or set(value) != fields:
        raise HTTPException(400, 'mobile_request_invalid')
    return value


def _response(operation, *args, **kwargs):
    headers = {'Cache-Control': 'no-store'}
    try:
        return JSONResponse(operation(*args, **kwargs), headers=headers)
    except (MobileError, AttestationError, SQLiteUnitOfWorkConflict) as error:
        detail = str(error) if isinstance(error, (MobileError, AttestationError)) else 'mobile_invite_unavailable'
        status = {'mobile_invite_unavailable': 409, 'mobile_device_unauthorized': 401, 'mobile_attestation_revoked': 403,
            'mobile_ca_expiring': 503, 'mobile_revocations_unavailable': 503}.get(detail, 400)
        return JSONResponse({'detail': detail}, status_code=status, headers=headers)


def install_android_mobile_routes(application, mobile):
    from starlette.concurrency import run_in_threadpool

    def require_https(request):
        if request.url.scheme != 'https':
            raise HTTPException(400, 'mobile_https_required')

    @application.post('/api/v2/server/users/{user_id}/mobile-invites')
    async def invite(user_id: str, request: Request):
        require_https(request)
        await _body(request, set())
        return await run_in_threadpool(_response, mobile.issue_invite, USER_ACCESS.get().caller, user_id)

    @application.post('/api/v2/mobile/challenge')
    async def challenge(request: Request):
        require_https(request)
        data = await _body(request, {'code'})
        return await run_in_threadpool(_response, mobile.challenge, data['code'])

    @application.post('/api/v2/mobile/activate')
    async def activate(request: Request):
        require_https(request)
        data = await _body(request, {'code', 'challenge_id', 'device_name', 'attestation_chain'})
        return await run_in_threadpool(_response, mobile.activate, **data)

    @application.post('/api/v2/mobile/renew')
    async def renew(request: Request):
        require_https(request)
        await _body(request, set())
        actor = server_identity(request)
        if actor is None or request.scope.get('state', {}).get('android_mobile_identity') is not actor:
            raise HTTPException(401, 'mobile_device_unauthorized')
        return await run_in_threadpool(_response, mobile.renew, actor)
