import asyncio
import os
from pathlib import Path
import shutil

import pytest


def test_existing_user_quota_pauses_daily_and_preserves_cold_lifecycle(tmp_path):
    root = (tmp_path / 'server').resolve()
    assert all(path.name != 'runtime' for path in (root, *root.parents))
    (root / 'config').mkdir(parents=True)
    repository = Path(__file__).resolve().parents[3]
    shutil.copyfile(repository / 'config/settings.toml.example', root / 'config/settings.toml')
    previous = {name: os.environ.get(name) for name in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(root), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root
    try:
        _actual_quota_lifecycle(root)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _actual_quota_lifecycle(root):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.server_jobs import QuotaError
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.policies import override
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='维护管理')
    admin = devices.authenticate(pair['key'])
    users = ServerUsers(root, records=devices.records)
    account = users.create(admin, name='维护容量账号')
    pair = devices.exchange(devices.issue_pairing(user_id=account['user_id'], actor=admin.device_id)['code'], name='维护设备')
    actor = devices.authenticate(pair['key'])
    original_limit = account['storage_limit_mb']
    saved = {}

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root), server_context=context)

    def server():
        return create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
            child_factory=child, port=8016)

    def quota(registry, limit):
        current = registry.get(actor.user_id)
        with user_context(authorize_user(registry, admin, actor.user_id)):
            return registry.update(admin, actor.user_id, expected_revision=current['revision'], storage_limit_mb=limit)

    async def checkpoint_success(daily):
        async with asyncio.timeout(10):
            while daily._last_clock is None:
                if daily.task.done():
                    await daily.task
                await asyncio.sleep(.01)
        assert daily._checkpoint_error is None and not daily.maintenance_paused

    async def checkpoint_paused(daily):
        async with asyncio.timeout(10):
            while not daily.task.done():
                await asyncio.sleep(.01)
        error = daily.task.exception()
        assert isinstance(error, QuotaError) and str(error) == 'storage_quota_exceeded'
        assert daily._checkpoint_error is error and daily.maintenance_paused
        async with daily.maintenance_lease(wait=False) as admitted:
            assert admitted is False

    async def existing(server_app):
        async with server_app.router.lifespan_context(server_app):
            pool = server_app.state.server_user_pool
            registry = server_app.state.server_users
            async with pool.lease(actor.user_id) as application:
                access = authorize_user(registry, actor, actor.user_id)
                with user_context(access), pool.request_scope(actor.user_id, application, access):
                    application.state.workspace_domains.query.answer_turns._runtime()
                    daily = application.state.memory_daily_jobs
                    await checkpoint_success(daily)
                    item = await application.state.workspace_domains.intake.add_text(
                        {'project_id': 'default', 'text': '已有账号的原件在容量不足时仍须可读。'})
                    row = application.state.recognition_records.read('workspace_items', item['id'])
                    saved.update(identity=row.object_id, revision=row.revision, payload=dict(row.payload), app=application)
                    await daily.stop()
                    assert server_app.state.server_context.jobs.storage_bytes(actor.user_id) > 0
                    quota(registry, 0)
                    clock = daily._last_clock
                    await daily.start()
                    await checkpoint_paused(daily)
                    assert daily._last_clock == clock
                    token = object()
                    await daily.pause_maintenance(token)
                    daily.resume_maintenance(token)
                    assert daily.maintenance_paused
                    with pytest.raises(QuotaError, match='^storage_quota_exceeded$'):
                        await application.state.workspace_domains.intake.add_text(
                            {'project_id': 'default', 'text': '这次写入应被真实配额拒绝。'})
                    assert application.state.recognition_records.read('workspace_items', row.object_id) == row
            saved['pool'] = pool
        assert not saved['pool'].loaded_user_ids

    async def reopened(server_app):
        async with server_app.router.lifespan_context(server_app):
            pool = server_app.state.server_user_pool
            registry = server_app.state.server_users
            assert registry.get(actor.user_id)['storage_limit_mb'] == 0
            async with pool.lease(actor.user_id) as application:
                assert application is not saved['app']
                access = authorize_user(registry, actor, actor.user_id)
                with user_context(access), pool.request_scope(actor.user_id, application, access):
                    application.state.workspace_domains.query.answer_turns._runtime()
                    row = application.state.recognition_records.read('workspace_items', saved['identity'])
                    assert row.revision == saved['revision'] and dict(row.payload) == saved['payload']
                    daily = application.state.memory_daily_jobs
                    await checkpoint_paused(daily)
                    assert daily._last_clock is None
                    token = object()
                    await daily.pause_maintenance(token)
                    quota(registry, original_limit)
                    await daily.stop()
                    await daily.start()
                    assert daily.maintenance_paused and not daily._maintenance_open.is_set()
                    assert daily._checkpoint_error is None and daily._last_clock is None
                    daily.resume_maintenance(token)
                    await checkpoint_success(daily)
                    assert application.state.recognition_records.read('workspace_items', row.object_id) == row
                    assert application.state.ai_turn_store._connect().execute('SELECT count(*) FROM ai_turns').fetchone()[0] == 0
            saved['pool'] = pool
        assert not saved['pool'].loaded_user_ids

    try:
        application = server()
        with resource_context(application.state.server_context.resources), connection_scope(), override(trigger='@2'):
            asyncio.run(existing(application))
        application = server()
        with resource_context(application.state.server_context.resources), connection_scope(), override(trigger='@2'):
            asyncio.run(reopened(application))
    finally:
        quota(users, original_limit)
