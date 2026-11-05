import os
import sys
import json
from pathlib import Path

import uvicorn

root = Path(sys.argv[1]).resolve()
os.environ['CHRIPTMAS_APP_ROOT'] = str(root)

from tests.memory_app.v2.test_library_unfinished import build_app, prepare_ready, seed_original

app = build_app(root)
records = app.state.recognition_records
prepare_ready(records)
seed_original(records, identity='processing-original', turn_id='processing-turn', state='processing',
    instance=app.state.workbench_instance)
item = records.read('workspace_items', 'processing-original')
with records.begin() as tx:
    tx.put('workspace_items', item.object_id, {**item.payload, 'status': 'processing', 'title': '处理中原件'},
        expected_revision=item.revision)
    tx.commit()


@app.middleware('http')
async def record_retry(request, call_next):
    if request.url.path.endswith('/retry'):
        with (root / 'retries.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'path': request.url.path}) + '\n')
    return await call_next(request)


uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[2]), lifespan='off', log_level='error')
