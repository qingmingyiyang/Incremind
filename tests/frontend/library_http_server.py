import asyncio
import json
import os
from pathlib import Path
from shutil import copyfile
import sys

import uvicorn


root = Path(sys.argv[1]).resolve()
os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
(root / 'config').mkdir()
copyfile(Path(__file__).resolve().parents[2] / 'config/settings.toml.example', root / 'config/settings.toml')

from backend.memory_app.app import create_app


app = create_app(runtime_root=root)
store = app.state.workspace_domains.query.source_store
for identity, title, body in [('alpha-source', 'Alpha original', 'alpha beta 搜索'),
                              ('other-source', 'Other original', 'different body')]:
    store.write('sources', identity, {'id': identity, 'project_id': 'alpha', 'title': title,
        'type': 'text', 'metadata': {'content_snapshot': body}}, expected_revision=0)


@app.middleware('http')
async def record_request(request, call_next):
    # 记录真实请求路径，正文和凭据不进入测试日志。
    with (root / 'requests.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'path': request.url.path, 'query': dict(request.query_params)}) + '\n')
    if request.url.path.endswith('/sources') and request.query_params.get('q') == 'slow':
        await asyncio.sleep(0.5)
    return await call_next(request)


uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[2]), lifespan='off', log_level='error')
