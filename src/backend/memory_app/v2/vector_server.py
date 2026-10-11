"""独立私有向量服务：只加载已安装文字资产，和桌面共享唯一编码 owner。"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from ..local_vector_assets import installed_assets
from ..local_vectors import LocalVectorError, encode_request, load, model_directory, runtime_status, unload
from .embedding_settings import server_embedding_profile, vector_policy


def _secret(name, *, required=False):
    value, filename = os.environ.get(name, ''), os.environ.get(name + '_FILE', '')
    if value and filename:
        raise ValueError('vector_server_secret_configuration_invalid')
    if filename:
        try:
            with Path(filename).open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError('vector_server_secret_configuration_invalid')
            value = raw.decode('utf-8').strip()
        except (OSError, UnicodeError):
            raise ValueError('vector_server_secret_configuration_invalid') from None
    if (required and not value) or len(value) > 4096 or any(char in value for char in '\r\n'):
        raise ValueError('vector_server_secret_configuration_invalid')
    return value.encode('utf-8')


def _failure(error):
    code = str(error)
    status = (409 if code in {'embedding_runtime_revision_conflict', 'local_vector_device_requires_unload'} else
              429 if code == 'local_vector_queue_full' else
              413 if code == 'local_vector_input_too_large' else
              400 if code in {'invalid_local_vector_request', 'local_vector_device_invalid'} else 503)
    raise HTTPException(status, code) from None


def create_app():
    key = _secret('CHRIPTMAS_VECTOR_API_KEY', required=True)
    admin_key = _secret('CHRIPTMAS_VECTOR_ADMIN_KEY')
    if admin_key and hmac.compare_digest(key, admin_key):
        raise ValueError('vector_server_admin_key_must_differ')
    device = os.environ.get('CHRIPTMAS_VECTOR_DEVICE', 'cpu')
    autoload = os.environ.get('CHRIPTMAS_VECTOR_AUTOLOAD', '1')
    if device not in {'cpu', 'cuda', 'auto'} or autoload not in {'0', '1'}:
        raise ValueError('vector_server_configuration_invalid')
    root = Path(os.environ.get('CHRIPTMAS_VECTOR_ROOT', '/var/lib/incremind-vectors')).expanduser().absolute()
    directory = model_directory(root / 'data' / 'models')
    policy = vector_policy()
    profile = server_embedding_profile(policy['model'])
    startup_reason = None

    def begin_load(expected_revision=None):
        if not installed_assets(directory, model=policy['model']):
            raise LocalVectorError('local_vector_not_installed')
        return load(directory, policy=policy, device=device, expected_revision=expected_revision)

    @asynccontextmanager
    async def lifespan(application):
        nonlocal startup_reason
        if autoload == '1':
            try:
                # load 只提交后台加载，HTTP readiness 不等待权重初始化。
                begin_load()
            except LocalVectorError as error:
                startup_reason = str(error)
        try:
            yield
        finally:
            await asyncio.to_thread(unload, directory, wait=True)

    application = FastAPI(title='Incremind private vectors', lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(request, *, administrative=False):
        expected = admin_key if administrative else key
        supplied = request.headers.get('authorization', '')
        # 认证在读取正文之前；管理密钥不能由推理密钥冒充。
        if not expected or not supplied.startswith('Bearer ') or not hmac.compare_digest(
                supplied[7:].encode('utf-8'), expected):
            raise HTTPException(403 if administrative and not expected else 401, 'vector_server_unauthorized')

    async def read_body(request, *, limit):
        raw = bytearray()
        async for block in request.stream():
            if len(raw) + len(block) > limit:
                raise HTTPException(413, 'local_vector_input_too_large')
            raw.extend(block)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, 'invalid_local_vector_request') from None
        if not isinstance(result, dict):
            raise HTTPException(400, 'invalid_local_vector_request')
        return result

    @application.get('/health')
    async def health():
        current = runtime_status(directory)
        ready = current['runtime_status'] == 'loaded'
        return JSONResponse({'status': 'ready' if ready else current['runtime_status'],
                'reason_code': current['runtime_reason_code'] or startup_reason,
                'device': current['device'], 'runtime_revision': current['runtime_revision']},
                status_code=200 if ready else 503)

    @application.get('/v1/models')
    async def models(request: Request):
        authenticate(request)
        return {'object': 'list', 'data': [{'id': policy['model'], 'object': 'model', 'owned_by': 'self',
                                           'embedding_profile': profile}]}

    @application.post('/v1/embeddings')
    async def embeddings(request: Request):
        authenticate(request)
        body = await read_body(request, limit=policy['max_items'] * policy['max_input_chars'] * 4 + 65536)
        if body.get('embedding_profile') != profile:
            raise HTTPException(400, 'embedding_profile_mismatch')
        try:
            return await run_in_threadpool(encode_request, directory, body, policy=policy)
        except LocalVectorError as error:
            _failure(error)

    @application.get('/admin/runtime')
    async def runtime(request: Request):
        authenticate(request)
        return runtime_status(directory)

    async def change_runtime(request, action):
        nonlocal startup_reason
        authenticate(request, administrative=True)
        body = await read_body(request, limit=1024)
        revision = body.get('expected_revision')
        if set(body) != {'expected_revision'} or not isinstance(revision, str) or len(revision) > 100:
            raise HTTPException(400, 'embedding_runtime_invalid')
        try:
            result = (begin_load(revision) if action == 'load' else
                      unload(directory, expected_revision=revision))
        except LocalVectorError as error:
            _failure(error)
        startup_reason = None
        return result

    @application.post('/admin/load')
    async def admin_load(request: Request):
        return await change_runtime(request, 'load')

    @application.post('/admin/unload')
    async def admin_unload(request: Request):
        return await change_runtime(request, 'unload')

    return application


def main():
    parser = argparse.ArgumentParser(description='Incremind private vector service')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8002)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    import uvicorn
    # 单进程持有一个编码 owner；启动时就验证密钥与配置。
    uvicorn.run(create_app(), host=args.host, port=args.port, workers=1)


if __name__ == '__main__':
    main()
