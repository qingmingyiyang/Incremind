import asyncio
from contextvars import copy_context
import os
from pathlib import Path
from shutil import copyfile
import sqlite3
from threading import Event

from fastapi.testclient import TestClient
import pytest


def test_actual_server_checkpoint_uses_current_job_owner_and_closes_normally(tmp_path_factory):
    root = tmp_path_factory.mktemp('s').resolve(strict=True)
    assert root.is_absolute()
    config = root / 'config/settings.toml'
    config.parent.mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', config)
    previous_root, previous_mode = os.environ.get('CHRIPTMAS_APP_ROOT'), os.environ.get('CHRIPTMAS_DEPLOY')
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(root), 'server'
    try:
        from backend.memory_app.serve import create_application
        from backend.memory_app.workbench_identity_binding import execution_owner
        from backend.security.user_context import USER_ACCESS, user_context
        from backend.shared.server_resources import resource_context
        from core.document_engine.sqlite_runtime import SQLiteDocumentRepository

        application = create_application(port=8765)
        context = application.state.server_context
        registry = application.state.device_registry
        pair = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'],
            name='真实后台归属检查')
        with TestClient(application, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 1234)) as client:
            client.headers['Authorization'] = 'Bearer ' + pair['key']
            del pair
            response = client.get('/api/v2/settings')
            assert response.status_code == 200
            child = context.pool._children['local-user'].application
            records, documents = child.state.recognition_records, child.state.recognition_documents
            daily = child.state.memory_daily_jobs
            assert daily.steer_learning is child.state.product_steer_learning

            async def checkpoint_completed():
                async def wait():
                    while daily._last_clock is None:
                        if daily.task.done():
                            daily.task.result()
                            raise AssertionError('server_checkpoint_did_not_run')
                        await asyncio.sleep(.01)
                await asyncio.wait_for(wait(), timeout=60)
                assert not daily.task.done()

            client.portal.call(checkpoint_completed)
            assert daily._last_clock is not None

            async def current_job():
                assert USER_ACCESS.get() is None
                owner = execution_owner(child, records, documents)
                assert owner == {'owner_user_id': 'local-user', 'namespace_id': documents.namespace_id,
                    'origin_kind': 'personal'}
                wrong = SQLiteDocumentRepository(records, namespace_id='wrong-owner')
                with pytest.raises(ValueError, match='workbench_identity_owner_changed'):
                    execution_owner(child, records, wrong)
                return copy_context()

            async def submit():
                with resource_context(context.resources), user_context(None):
                    return await context.jobs.submit('local-user', 'daily', current_job)

            expired = client.portal.call(submit)
            with resource_context(context.resources), user_context(None):
                with pytest.raises(ValueError, match='workbench_identity_background_owner_unavailable'):
                    execution_owner(child, records, documents)
            with pytest.raises(ValueError, match='user_unauthorized'):
                expired.run(execution_owner, child, records, documents)
            assert context.jobs.active_for('local-user') == 0
            assert not daily.task.done()
            path = Path(child.state.ai_turn_store._path).resolve()
            assert path.is_relative_to(root)
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
                count = connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json, '$.type') = ?",
                    ('model.attempt.dispatched',)).fetchone()[0]
                assert count == 0

            async def close_during_checkpoint():
                entered, release, settled = Event(), Event(), Event()

                def running_job():
                    try:
                        execution_owner(child, records, documents)
                        entered.set()
                        if not release.wait(10):
                            raise TimeoutError('server_job_release_not_received')
                    finally:
                        settled.set()

                with resource_context(context.resources), user_context(None):
                    running = asyncio.create_task(context.jobs.submit('local-user', 'daily', running_job))
                    assert await asyncio.to_thread(entered.wait, 8)
                    closing = asyncio.create_task(context.jobs.close())
                    await asyncio.sleep(0)
                    assert context.jobs._closed and not closing.done()
                    try:
                        with pytest.raises(asyncio.CancelledError):
                            await daily._checkpoint()
                    finally:
                        release.set()
                        await closing
                        results = await asyncio.gather(running, return_exceptions=True)
                    assert settled.is_set()
                    assert isinstance(results[0], asyncio.CancelledError)
                    assert context.jobs.active_for('local-user') == 0

            client.portal.call(close_during_checkpoint)
        assert daily.task.done() and daily.task.cancelled()
        assert context.pool.loaded_user_ids == ()
        assert not context.jobs._workers
    finally:
        if previous_root is None:
            os.environ.pop('CHRIPTMAS_APP_ROOT', None)
        else:
            os.environ['CHRIPTMAS_APP_ROOT'] = previous_root
        if previous_mode is None:
            os.environ.pop('CHRIPTMAS_DEPLOY', None)
        else:
            os.environ['CHRIPTMAS_DEPLOY'] = previous_mode
