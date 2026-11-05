import asyncio
import os
from pathlib import Path
import shutil


def test_normal_server_lifespan_binds_background_owner_and_resets_resources(tmp_path):
    root = (tmp_path / 'server').resolve()
    assert all(path.name != 'runtime' for path in (root, *root.parents))
    (root / 'config').mkdir(parents=True)
    repository = Path(__file__).resolve().parents[3]
    shutil.copyfile(repository / 'config/settings.toml.example', root / 'config/settings.toml')
    previous = {name: os.environ.get(name) for name in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(root), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root
    try:
        _normal_server_lifetime(root)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _normal_server_lifetime(root):
    from backend.memory_app.app import create_server_app
    from backend.memory_app.v2.policies import override
    from backend.memory_app.workbench_identity_binding import execution_owner
    from backend.security.user_context import USER_ACCESS
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import RESOURCE_POOL
    from core.storage_provider.connection_scope import connection_scope

    first = create_server_app(layout=DeploymentLayout('server', root / 'users/local-user', root), port=8018)
    registry = first.state.device_registry
    registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='后台维护设备')
    second_root = root.parent / 'second-server'
    second = create_server_app(layout=DeploymentLayout('server', second_root / 'users/local-user', second_root), port=8019)
    first_context, second_context = first.state.server_context, second.state.server_context
    assert first_context.resources is not second_context.resources
    assert RESOURCE_POOL.get() is None

    async def scenario():
        async with first.router.lifespan_context(first):
            async with first_context.pool.lease('local-user') as child:
                child.state.workspace_domains.query.answer_turns._runtime()
                daily = child.state.memory_daily_jobs
                async with asyncio.timeout(10):
                    while daily._last_clock is None:
                        if daily.task.done():
                            await daily.task
                        await asyncio.sleep(.01)
                assert not daily.task.done() and daily._checkpoint_error is None
                assert not daily.maintenance_paused
                assert RESOURCE_POOL.get() is first_context.resources

                def actual_owner():
                    assert USER_ACCESS.get() is None
                    assert RESOURCE_POOL.get() is first_context.resources
                    owner = execution_owner(child, child.state.recognition_records, child.state.recognition_documents)
                    assert owner == {'owner_user_id': 'local-user',
                        'namespace_id': child.state.recognition_documents.namespace_id, 'origin_kind': 'personal'}
                    return owner

                ready = asyncio.Event()

                async def background():
                    await ready.wait()
                    return await first_context.jobs.submit('local-user', 'daily', actual_owner)

                task = asyncio.create_task(background())
                async with second.router.lifespan_context(second):
                    assert RESOURCE_POOL.get() is second_context.resources
                    assert await asyncio.to_thread(RESOURCE_POOL.get) is second_context.resources
                    ready.set()
                    assert (await task)['owner_user_id'] == 'local-user'
                    assert RESOURCE_POOL.get() is second_context.resources
                assert RESOURCE_POOL.get() is first_context.resources
                assert not second_context.pool.loaded_user_ids and not second_context.jobs._workers
                connection = child.state.ai_turn_store._connect()
                try:
                    assert connection.execute('SELECT count(*) FROM ai_turns').fetchone()[0] == 0
                finally:
                    connection.close()
        assert not first_context.pool.loaded_user_ids and not first_context.jobs._workers
        assert RESOURCE_POOL.get() is None

    with connection_scope(), override(trigger='@2'):
        asyncio.run(scenario())
    assert RESOURCE_POOL.get() is None
