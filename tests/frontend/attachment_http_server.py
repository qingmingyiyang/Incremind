import asyncio
import json
import os
from pathlib import Path
import sys

import uvicorn

root = Path(sys.argv[1]).resolve()
os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
from tests.memory_app.v2.test_library_unfinished import build_app

app = build_app(root)


@app.post('/test/server/stop')
async def stop_server():
    asyncio.get_running_loop().call_later(.05, os._exit, 0)
    return {'stopping': True}


@app.middleware('http')
async def observe_requests(request, call_next):
    if request.method == 'POST' and request.url.path in {'/api/v2/workbench/turns', '/api/v2/workbench/files'}:
        body = await request.json() if request.url.path.endswith('/turns') else None
        with (root / 'requests.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'path': request.url.path, 'body': body}) + '\n')
        if body is not None and (root / 'disconnect-next-turn').exists():
            # 测试进程在真实请求到达后终止服务器，验证断连重发。
            await asyncio.Event().wait()
    response = await call_next(request)
    if request.method == 'POST':
        with (root / 'statuses.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'path': request.url.path, 'status': response.status_code,
                'content_type': request.headers.get('content-type')}) + '\n')
        if response.status_code >= 400:
            original = response.body_iterator
            async def capture_error():
                chunks = []
                async for chunk in original:
                    chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
                    yield chunk
                (root / 'response-error.json').write_bytes(b''.join(chunks))
            response.body_iterator = capture_error()
    return response


uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[2]), lifespan='off', log_level='error')
