"""Explicit user roots and lifetimes; no per-request process environment changes."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import inspect
import time
import logging
import re
from dataclasses import field

from fastapi import FastAPI
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import Match

from backend.security.device_auth import ServerDeviceAuth, install_device_authentication
from backend.security.file_attribution import file_attribution
from backend.security.user_context import USER_ACCESS, UserError, authorize_user, user_context
from backend.shared.server_resources import SharedResources, resource_context
from .server_audit import AdminAudit
from .server_jobs import ServerJobScheduler, QuotaError, retain_until_finished
from .v2.devices import DeviceRegistry, install_device_routes
from .v2.server_users import ServerUsers

HOST_SETTINGS = frozenset('/api/rebuild/settings/' + suffix for suffix in (
    'local-ocr-provider', 'local-asr-provider', 'local-video-provider',
    'local-document-text-extractor', 'transcript-summary-provider'))
INTAKE_PATHS = frozenset({'/api/v2/workbench/files',
    '/api/workspace/v1/items/text', '/api/workspace/v1/items/file', '/api/workspace/v1/items/audio'})
LOGGER = logging.getLogger(__name__)
_DOWNLOAD_ROUTE = re.compile(r'/api/(?:asr/faster-whisper/models|rag/models)/[A-Za-z0-9][A-Za-z0-9._~-]{0,127}/download\Z')


def authorize_host_resource(users, access, method, path):
    if ((method == 'PUT' and path in HOST_SETTINGS)
            or (method == 'POST' and _DOWNLOAD_ROUTE.fullmatch(path))):
        users.authorize_claim(access.caller, access.target_user_id, action='manage_resources')


@dataclass
class ServerContext:
    layout: object
    users: object
    registry: object
    auth: object
    resources: object
    jobs: object
    pool: object = field(default=None)


def _route_path(application, scope):
    for route in application.router.routes:
        effective = getattr(route, 'effective_route_contexts', None)
        candidates = effective() if callable(effective) else (route,)
        for candidate in candidates:
            match, _ = candidate.matches(scope)
            if match == Match.FULL:
                return getattr(candidate, 'path', '/unmatched')
    return '/unmatched'


class _UserDispatcher:
    def __init__(self, app, *, application, context, audit):
        self.app, self.application, self.context, self.audit = app, application, context, audit

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        state = scope.setdefault('state', {})
        if state.get('server_auth_public'):
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        internal = state.get('server_internal_model') is True
        try:
            if internal:
                target = state['server_internal_target']
                user = self.context.users.get(target)
                if user['disabled_at'] is not None:
                    raise UserError('user_unauthorized')
                access = None
            else:
                targets = connection.headers.getlist('x-chriptmas-target-user')
                if len(targets) > 1:
                    raise UserError('user_request_invalid')
                target = state.get('server_ticket_target', targets[0] if targets else None)
                access = authorize_user(self.context.users, state.get('device_identity'), target)
                target = access.target_user_id
                state['user_access'] = access
                if scope['type'] == 'websocket' and self.context.users.get(target)['disabled_at'] is not None:
                    raise UserError('user_unauthorized')
                authorize_host_resource(self.context.users, access, scope.get('method'), scope['path'])
                if scope.get('method') == 'POST' and scope['path'] == '/api/v2/settings/subscriptions/login':
                    return await JSONResponse({'detail': 'subscription_login_unavailable_server'}, status_code=409)(scope, receive, send)
                if scope.get('method') == 'POST' and scope['path'] in INTAKE_PATHS:
                    self.context.jobs.check_intake(target)
            with user_context(access), resource_context(self.context.resources):
                control = not internal and (scope['path'].startswith('/api/v2/server/')
                    or scope['path'] == '/api/v2/server/users' or scope['path'].startswith('/api/v2/devices'))
                if control:
                    return await self._invoke(self.application, self.app, scope, receive, send, access)
                load_intent = None
                if access is not None and access.by == 'admin' and target not in self.context.pool.loaded_user_ids:
                    try:
                        load_intent = self.audit.intent(access, method='LOAD', path='/user-space')
                    except Exception:
                        return await self._audit_unavailable(scope, receive, send)
                load_status = 500
                try:
                    async with self.context.pool.lease(target) as child:
                        load_status = 200
                        state['server_dispatch_authority'] = self.context.auth
                        return await self._invoke(child, child, scope, receive, send, access)
                finally:
                    self._outcome(load_intent, load_status)
        except UserError as error:
            code = str(error)
            status = {'user_unauthorized': 401, 'user_forbidden': 403, 'user_not_found': 404, 'user_unavailable':503}.get(code, 400)
        except QuotaError as error:
            code, status = str(error), 429
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 4401 if status == 401 else 4403})
        return await JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})(scope, receive, send)

    async def _invoke(self, application, handler, scope, receive, send, access):
        try:
            intent = self.audit.intent(access, method=scope.get('method', 'WEBSOCKET'),
                path=_route_path(application, scope)) if access is not None else None
        except Exception:
            return await self._audit_unavailable(scope, receive, send)
        status = 500
        async def observed(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            elif message['type'] == 'websocket.accept':
                status = 101
            await send(message)
        try:
            await handler(scope, receive, observed)
        finally:
            self._outcome(intent, status)

    async def _audit_unavailable(self, scope, receive, send):
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 4403})
        return await JSONResponse({'detail': 'admin_audit_unavailable'}, status_code=503)(scope, receive, send)

    def _outcome(self, intent, status):
        try:
            self.audit.outcome(intent, status=status)
        except Exception:
            # A second transaction cannot undo the durable preceding intent.
            LOGGER.error('admin_audit_outcome_unavailable')


def create_server_application(layout, *, child_factory, port=8001, concurrency=1, clock=time.monotonic):
    registry = DeviceRegistry(layout.server_root / 'server')
    users = ServerUsers(layout.server_root, records=registry.records)
    auth = ServerDeviceAuth(registry, port=port)
    jobs = ServerJobScheduler(users, concurrency=concurrency)
    resources = SharedResources(layout.server_root)
    # 在发布资源和创建用户池前，绑定原供应商配置留痕工厂。
    resources.provider_file_attribution_factory = file_attribution
    context = ServerContext(layout, users, registry, auth, resources, jobs)
    pool = UserApplicationPool(users, factory=lambda root, user: child_factory(root, user, context), clock=clock,pending_jobs=jobs.pending_for)
    context.pool = pool
    jobs.execution_lease = pool.job_lease
    audit = AdminAudit(users)

    @asynccontextmanager
    async def lifetime(application):
        async def maintain():
            while True:
                await asyncio.sleep(60)
                await pool.collect_idle()
        maintenance = asyncio.create_task(maintain())
        try:
            yield
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            await jobs.close()
            await pool.close()

    application = FastAPI(lifespan=lifetime)
    application.state.deployment = layout
    application.state.device_registry = registry
    application.state.server_users = users
    application.state.server_user_pool = pool
    application.state.server_context = context
    from .v2.server_control import install_server_routes
    install_server_routes(application, users=users, registry=registry, audit=audit, jobs=jobs)
    install_device_routes(application, registry=registry)
    from backend.api.routes.realtime_asr import consume_server_ticket
    application.state.server_realtime_ticket_consumer = consume_server_ticket
    application.add_middleware(_UserDispatcher, application=application, context=context, audit=audit)
    install_device_authentication(application, registry=registry, auth=auth)
    return application


@dataclass
class _Child:
    application: object
    lifespan: object
    touched: float
    active: int = 0
    retiring: object = None


class UserApplicationPool:
    def __init__(self, users, *, factory, clock=time.monotonic, idle_seconds=1800,pending_jobs=None):
        self.users, self.factory, self.clock = users, factory, clock
        self.idle_seconds = idle_seconds
        self.pending_jobs = pending_jobs
        self._children, self._locks = {}, {}
        self._closing, self._changed = False, asyncio.Event()

    @property
    def loaded_user_ids(self):
        return tuple(self._children)

    def _pending_work(self, app, *, detached_only=False):
        """Observe actual executors; durable Turn status is never completion."""
        tasks = [] if detached_only else list(getattr(app.state, 'workbench_tasks', ()))
        turns = getattr(app.state, 'workbench_turn_execution', None)
        if turns is not None and not detached_only:
            tasks.extend(turns.tasks.values())
        runner = getattr(app.state, 'ai_turn_runner', None)
        if runner is not None:
            with runner._lock:
                tasks.extend(value[3] for value in runner._active.values())
                tasks.extend(value[1] for value in runner._active_actions.values())
        observed = False
        if self.pending_jobs is not None and not detached_only:
            observed = bool(self.pending_jobs(app.state.server_user_id))
        answers = getattr(app.state, 'product_answer_turns', None)
        if answers is not None and not detached_only:
            with answers.lock:
                observed = observed or bool(answers.active)
        container = getattr(app.state, 'container', None)
        if container is not None and not detached_only:
            observed = observed or container.rag_model_manager.has_active_download()
            # The download thread removes its global membership in finally.
            # Only the requesting child's progress owner pins that child.
            from backend.api.routes.settings import _ASR_DOWNLOAD_LOCK, _ACTIVE_ASR_DOWNLOADS
            with _ASR_DOWNLOAD_LOCK:
                downloads = tuple(_ACTIVE_ASR_DOWNLOADS)
            observed = observed or any(container.model_download_progress_tracker.get_snapshot(key).status == 'running'
                for key in downloads)
        recovery = getattr(app.state, 'effect_recovery_service', None)
        if recovery is not None and not detached_only:
            observed = observed or (recovery._stop.is_set() and recovery._thread.is_alive())
        pending = tuple(dict.fromkeys(task for task in tasks if not task.done()))
        return pending, observed

    async def _settle_work(self, app, *, detached_only=False):
        while True:
            tasks, observed = self._pending_work(app, detached_only=detached_only)
            if not tasks and not observed:
                return
            if tasks:
                waiters = [task if asyncio.isfuture(task) else asyncio.wrap_future(task) for task in tasks]
                await asyncio.gather(*waiters, return_exceptions=True)
            else:
                # Actual download/answer owners expose busy state without a
                # Future. Wait outside the creation/unload lock.
                await asyncio.sleep(.01)

    @asynccontextmanager
    async def job_lease(self, user_id):
        async with self.lease(user_id) as app:
            try:
                yield app
            finally:
                # Registered HTTP/background callers may themselves await this
                # scheduler. Only independent executor Futures can outlive its
                # retained callback; waiting for caller Tasks would deadlock.
                await retain_until_finished(self._settle_work(app, detached_only=True))

    @asynccontextmanager
    async def lease(self, user_id):
        if self._closing:
            raise ValueError('server_runtime_closed')
        root = self.users.root_for(user_id)
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        while True:
            async with lock:
                if self._closing:
                    raise ValueError('server_runtime_closed')
                child = self._children.get(user_id)
                retiring = child.retiring if child is not None else None
                if retiring is None:
                    if child is None:
                        # Automatic startup work has no visiting admin actor.
                        with user_context(None):
                            app = self.factory(root, user_id)
                            if inspect.isawaitable(app):
                                app = await app
                            if getattr(app.state,'server_user_id',user_id) != user_id:
                                raise ValueError('server_user_mismatch')
                            app.state.server_user_id=user_id
                            lifetime = app.router.lifespan_context(app)
                            await lifetime.__aenter__()
                        child = _Child(app, lifetime, self.clock())
                        self._children[user_id] = child
                    child.active += 1
                    break
            await asyncio.shield(retiring)
        try:
            yield child.application
        finally:
            child.active -= 1
            child.touched = self.clock()
            self._changed.set()

    async def collect_idle(self):
        unloaded = []
        for user_id in tuple(self._children):
            async with self._locks[user_id]:
                child = self._children.get(user_id)
                if (child is None or child.retiring is not None or child.active or any(self._pending_work(child.application))
                        or self.clock() - child.touched < self.idle_seconds):
                    continue
                child.retiring = asyncio.get_running_loop().create_future()
            await retain_until_finished(self._retire(user_id, child))
            unloaded.append(user_id)
        return unloaded

    async def _retire(self, user_id, child):
        try:
            daily = getattr(child.application.state,'memory_daily_jobs',None)
            if daily is not None:
                await daily.stop()
            recovery = getattr(child.application.state, 'effect_recovery_service', None)
            if recovery is not None:
                # Core's bounded shutdown can leave a provider/recovery thread
                # alive. Stop its next pass, then observe real completion before
                # the lifespan closes clients or the root is recreated.
                recovery.shutdown(timeout_seconds=0)
            await self._settle_work(child.application)
            await child.lifespan.__aexit__(None, None, None)
        except BaseException:
            async with self._locks[user_id]:
                child.retiring.set_exception(UserError('user_unavailable'))
                child.retiring.exception()
                self._changed.set()
            raise
        else:
            async with self._locks[user_id]:
                if self._children.get(user_id) is child:
                    del self._children[user_id]
                child.retiring.set_result(None)
                self._changed.set()

    async def close(self):
        self._closing = True
        while any(child.active for child in self._children.values()):
            self._changed.clear()
            await self._changed.wait()
        for user_id in tuple(self._children):
            async with self._locks[user_id]:
                child = self._children.get(user_id)
                if child is None:
                    continue
                retiring = child.retiring
                if retiring is None:
                    child.retiring = asyncio.get_running_loop().create_future()
            if retiring is not None:
                await retain_until_finished(retiring)
            else:
                await retain_until_finished(self._retire(user_id, child))
