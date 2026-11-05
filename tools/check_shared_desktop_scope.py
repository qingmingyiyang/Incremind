import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from uuid import uuid4

import httpx


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('d' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.shared.deployment import DeploymentLayout
    from core.storage_provider.connection_scope import connection_scope

    app = create_app(runtime_root=root, deployment_layout=DeploymentLayout('desktop', root))
    assert getattr(app.state, 'shared_query_factory', None) is None
    records = app.state.recognition_records
    before = tuple(records.list('v2_turns'))
    def native_counts():
        path = root / '.rebuild-data/ai-turns.sqlite3'
        if not path.exists():
            return 0, 0
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            return tuple(connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('ai_turns', 'ai_model_attempt_reservations'))
        finally:
            connection.close()
    native_before = native_counts()

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url='http://127.0.0.1:8013') as client:
                response = await client.post('/api/v2/workbench/turns', json={'project_id': 'inbox',
                    'scope_kind': 'shared', 'intent': 'ask', 'text': '共享资料在哪里'},
                    headers={'Idempotency-Key': 'desktop-shared-' + uuid4().hex})
                assert response.status_code == 409 and response.json()['detail'] == 'shared_scope_unavailable', response.text
                assert tuple(records.list('v2_turns')) == before
                assert native_counts() == native_before
        print('shared-scope actual desktop POST409 without shared factory; public/native turns and model attempts unchanged', flush=True)

    with connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
