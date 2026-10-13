"""平台登录态路由仅编排既有用户空间和登录态领域服务。"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.storage_provider import SQLiteUnitOfWorkConflict
from ..platform_sessions import PlatformSessions, PlatformSessionError, MAX_COOKIE_BYTES
from ..platform_login import PlatformLogin
from .devices import _request_owner, DeviceError


def install_platform_session_routes(application, *, records, models):
    sessions = PlatformSessions(records, models.secrets)
    login = PlatformLogin(sessions)
    router = APIRouter(prefix='/api/v2/settings/platform-sessions')
    application.state.platform_sessions = sessions

    @application.on_event('shutdown')
    async def close_platform_login():
        await run_in_threadpool(login.close)

    def statuses():
        result = []
        for platform in ('bilibili', 'xiaohongshu'):
            row, capability = sessions.status(platform), login.capability(platform)
            result.append({**row, 'qr_available': capability['available'], 'qr_unavailable_reason': capability['reason']})
        return {'items': result}

    def owner(request):
        try:
            return _request_owner(request)
        except DeviceError:
            raise HTTPException(403, 'platform_session_forbidden') from None

    async def run(operation, *args):
        try:
            value = await run_in_threadpool(operation, *args)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'platform_session_revision_conflict') from None
        except PlatformSessionError as error:
            raise HTTPException(409 if str(error) == 'platform_session_revision_conflict' else 400, str(error)) from None
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    async def body(request, fields):
        # 读取有界原始正文，校验错误不回显 FastAPI 输入，Cookie 不进入错误响应。
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_COOKIE_BYTES + 4096:
                raise HTTPException(413, 'platform_cookie_size_invalid')
        import json
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            raise HTTPException(400, 'platform_session_request_invalid') from None
        if not isinstance(value, dict) or set(value) != fields:
            raise HTTPException(400, 'platform_session_request_invalid')
        return value

    @router.get('')
    async def status(request: Request):
        owner(request)
        return await run(statuses)

    @router.put('/{platform}')
    async def replace(platform: str, request: Request):
        owner(request)
        data = await body(request, {'content', 'expected_revision'})
        return await run(sessions.replace, platform, data['content'], data['expected_revision'])

    @router.delete('/{platform}')
    async def delete(platform: str, request: Request):
        owner(request)
        data = await body(request, {'expected_revision'})
        return await run(sessions.delete, platform, data['expected_revision'])

    @router.post('/{platform}/login')
    async def start(platform: str, request: Request):
        identity = owner(request)
        await body(request, set())
        return await run(login.start, platform, identity)

    @router.post('/login/{login_id}/poll')
    async def poll(login_id: str, request: Request):
        identity = owner(request)
        await body(request, set())
        return await run(login.poll, login_id, identity)

    @router.delete('/login/{login_id}')
    async def cancel(login_id: str, request: Request):
        identity = owner(request)
        await body(request, set())
        return await run(login.cancel, login_id, identity)

    application.include_router(router)
