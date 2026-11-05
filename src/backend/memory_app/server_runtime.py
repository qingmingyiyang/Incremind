"""Explicit user roots and lifetimes; no per-request process environment changes."""
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import Context, ContextVar
from dataclasses import dataclass
import asyncio
import inspect
import time
import logging
import os
import re
from pathlib import Path
from dataclasses import field

from fastapi import FastAPI
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import Match

from backend.security.device_auth import ServerDeviceAuth, install_device_authentication
from backend.security.file_attribution import file_attribution
from backend.security.user_context import USER_ACCESS, UserError, authorize_user, user_context
from backend.shared.server_resources import SharedResources, resource_context
from .local_vectors import model_directory, unload
from .server_audit import AdminAudit
from .server_jobs import ServerJobScheduler, QuotaError, retain_until_finished
from .v2.devices import DeviceRegistry, install_device_routes
from .v2.server_users import ServerUsers, USERS

HOST_SETTINGS = frozenset('/api/rebuild/settings/' + suffix for suffix in (
    'local-ocr-provider', 'local-asr-provider', 'local-video-provider',
    'local-document-text-extractor', 'transcript-summary-provider'))
INTAKE_PATHS = frozenset({'/api/v2/workbench/files', '/api/v2/workbench/share',
    '/api/workspace/v1/items/text', '/api/workspace/v1/items/file', '/api/workspace/v1/items/audio'})
LOGGER = logging.getLogger(__name__)
_SERVER_JOB_SUBJECT = ContextVar('server_job_subject', default=None)
_SERVER_REQUEST_SUBJECT = ContextVar('server_request_subject', default=None)
_DOWNLOAD_ROUTE = re.compile(r'/api/(?:asr/faster-whisper/models|rag/models)/[A-Za-z0-9][A-Za-z0-9._~-]{0,127}/download\Z')
_CONTROL_USER_ROUTE = re.compile(r'/api/v2/server/users/([A-Za-z0-9][A-Za-z0-9._~-]{0,127})(?:/pair|/login|/model-allocation|/model-balance(?:/adjust|/attempts(?:/[A-Za-z0-9][A-Za-z0-9._~-]{0,127}/reconcile)?)?)?\Z')


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
    vector_device: str = 'cpu'
    vector_on_ready: object = None
    models: object = None
    model_balances: object = None
    restores: object = None
    native_mcp: object = None
    shared_projects: object = None
    shared_query_factory: object = None


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
                if (self.context.users.deletion_pending(target)
                        and not (scope.get('method') == 'DELETE'
                                 and scope['path'] == '/api/v2/server/users/' + target)):
                    raise UserError('user_deletion_in_progress')
                if scope['type'] == 'websocket' and self.context.users.get(target)['disabled_at'] is not None:
                    raise UserError('user_unauthorized')
                authorize_host_resource(self.context.users, access, scope.get('method'), scope['path'])
                if scope.get('method') == 'POST' and scope['path'] == '/api/v2/settings/subscriptions/login':
                    with self.context.models.records.begin_read() as tx:
                        shared = self.context.models._allocation(tx, target)['mode'] == 'shared'
                    if shared:
                        return await JSONResponse({'detail': 'server_model_configuration_readonly'}, status_code=403)(scope, receive, send)
                if scope.get('method') == 'POST' and scope['path'] in INTAKE_PATHS:
                    self.context.jobs.check_intake(target)
            with user_context(access), resource_context(self.context.resources):
                restore_control = scope['type'] == 'http' and re.fullmatch(
                    r'/api/rebuild/memory-restores/[a-z0-9][a-z0-9._-]{0,95}(?:/resume)?', scope['path'])
                if restore_control:
                    if access is None:
                        raise UserError('user_forbidden')
                    return await self._invoke(self.application, self.app, scope, receive, send, access)
                control = not internal and (scope['path'].startswith('/api/v2/server/')
                    or scope['path'] == '/api/v2/server/users' or scope['path'].startswith('/api/v2/devices'))
                if control:
                    targets = {target}
                    audit_access = access
                    route_user = _CONTROL_USER_ROUTE.fullmatch(scope['path'])
                    if (route_user is not None and self.context.users.can(access.caller, action='manage_users')
                            and self.context.users.records.read(USERS, route_user[1]) is not None):
                        targets.add(route_user[1])
                        # 中央账号操作按 URL 目标留痕，空间选择仍沿用原分发上下文。
                        audit_access = authorize_user(self.context.users, access.caller, route_user[1])
                    deleting = route_user[1] if route_user is not None and scope.get('method') == 'DELETE' else None
                    async with self.context.pool.control_lease(targets, deleting_user_id=deleting) as token:
                        state['server_control_token'] = token
                        return await self._invoke(self.application, self.app, scope, receive, send, audit_access)
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
                        with self.context.pool.request_scope(target, child, access):
                            return await self._invoke(child, child, scope, receive, send, access)
                finally:
                    self._outcome(load_intent, load_status)
        except UserError as error:
            code = str(error)
            status = {'user_unauthorized': 401, 'user_forbidden': 403, 'user_not_found': 404,
                'user_unavailable': 503, 'runtime_restore_in_progress': 503,
                'user_deletion_in_progress': 409, 'user_delete_busy': 409}.get(code, 400)
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
        scope.setdefault('state', {})['server_audit_intent'] = intent
        async def observed(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            elif message['type'] == 'websocket.accept':
                status = 101
            await send(message)
        with self.audit.request_scope(intent, access, application):
            try:
                await handler(scope, receive, observed)
            finally:
                # 只有此请求的删除事务实际清除了原 intent，才跳过对应末尾回执。
                removed = scope['state'].get('server_deleted_audit_intent')
                if not (status == 200 and intent is not None and removed == intent):
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
    vector_device = os.environ.get('CHRIPTMAS_VECTOR_DEVICE', 'cpu')
    if vector_device not in {'cpu', 'cuda', 'auto'}:
        raise ValueError('local_vector_device_invalid')
    registry = DeviceRegistry(layout.server_root / 'server')
    users = ServerUsers(layout.server_root, records=registry.records)
    from .v2.server_models import ServerModels
    from .v2.server_model_balance import ServerModelBalances
    models = ServerModels(users)
    model_balances = ServerModelBalances(users, models)
    model_balances.recover_interrupted()
    auth = ServerDeviceAuth(registry, port=port)
    jobs = ServerJobScheduler(users, concurrency=concurrency)
    resources = SharedResources(layout.server_root)
    # 在发布资源和创建用户池前，绑定原供应商配置留痕工厂。
    resources.provider_file_attribution_factory = file_attribution
    def asr_model_access(user_root):
        root = Path(user_root).resolve()
        access = USER_ACCESS.get()
        if access is not None:
            if users.root_for(access.target_user_id) != root:
                raise UserError('user_unauthorized')
            users.authorize_claim(access.caller, access.target_user_id)
            return models.access_for(access.target_user_id, root)
        subject = _SERVER_JOB_SUBJECT.get()
        if (subject is None or subject[0] is not pool or subject[3] != root
                or jobs.active_for(subject[2]) < 1
                or not pool.model_balance_job_current(subject[1], subject[2], root)):
            raise UserError('user_unauthorized')
        return models.access_for(subject[2], root)
    resources.server_model_access_for = asr_model_access
    def asr_wire_owner(user_root):
        from .model_config import ModelConfiguration
        root = Path(user_root).resolve()
        job, request = _SERVER_JOB_SUBJECT.get(), _SERVER_REQUEST_SUBJECT.get()
        if (job is not None and job[0] is pool and job[3] == root
                and jobs.active_for(job[2]) > 0 and pool.model_balance_job_current(job[1], job[2], root)):
            user_id = job[2]
        elif (request is not None and request[0] is pool and request[3] == root
                and pool.request_current(request[1], request[2], root, USER_ACCESS.get())):
            user_id = request[2]
        else:
            raise UserError('user_unauthorized')
        access = asr_model_access(root)
        child = pool._children.get(user_id)
        if child is None or child.active < 1 or child.retiring is not None:
            raise UserError('user_unauthorized')
        app = child.application
        models, records = app.state.recognition_models, app.state.recognition_records
        bound = getattr(models, 'server_model_access', None)
        if (not isinstance(models, ModelConfiguration) or records is not models.records
                or models.root != root or bound is None or bound._owner is not context.models
                or bound.user_id != user_id or bound.user_root != root or bound != access
                or app.state.server_context is not context or app.state.server_user_id != user_id
                or app.state.recognition_runtime_root != root or users.root_for(user_id) != root
                or users.get(user_id)['disabled_at'] is not None or users.deletion_pending(user_id)
                or models.server_model_balances is not context.model_balances
                or context.model_balances.models is not context.models or context.model_balances.users is not users
                or context.model_balances.records is not users.records
                or context.models.users is not users or context.models.records is not users.records):
            raise UserError('user_unauthorized')
        return records, models
    resources.server_asr_wire_owner_for = asr_wire_owner
    resources.server_model_wire_owner_for = asr_wire_owner
    context = ServerContext(layout, users, registry, auth, resources, jobs, vector_device=vector_device,
                            models=models, model_balances=model_balances)
    async def restore_aware_child(root, user):
        await asyncio.to_thread(context.restores.recover_before_open, user)
        child = child_factory(root, user, context)
        if inspect.isawaitable(child):
            child = await child
        child.state.runtime_restore_host = context.restores.for_user(user)
        if context.shared_query_factory is not None:
            from .v2.shared_origin_store import shared_origin_store_factory
            context.shared_query_factory.bind_personal_dispatcher(child)
            child.state.ai_turn_store_factory = shared_origin_store_factory(child, context.shared_projects, registry)
        return child
    pool = UserApplicationPool(users, factory=restore_aware_child, clock=clock,
                               pending_jobs=jobs.pending_for, model_balances=model_balances)
    context.pool = pool
    from .server_runtime_recovery import ServerRuntimeRestoreCoordinator
    context.restores = ServerRuntimeRestoreCoordinator(context)
    pool.restore_admission = context.restores.admitted
    pool.loaded_observer = context.restores.child_ready
    jobs.admission = lambda user: context.restores.admitted(user)
    context.restores.recover_existing()
    jobs.execution_lease = pool.job_lease
    model_balances.bind_job_authority(jobs, pool)
    audit = AdminAudit(users)

    @asynccontextmanager
    async def server_lifetime(application):
        from .server_inspirations import ServerInspirationCoordinator
        loop = asyncio.get_running_loop()
        accepting_vectors = True
        inspirations = ServerInspirationCoordinator(users, pool, jobs, resources)

        def broadcast():
            if accepting_vectors:
                with user_context(None), resource_context(resources):
                    pool.start_vector_indexes()

        def vector_ready():
            if accepting_vectors:
                try:
                    # 不继承发起加载的管理员上下文，自动任务仍归各用户自己的调度器。
                    loop.call_soon_threadsafe(broadcast, context=Context())
                except RuntimeError:
                    LOGGER.warning('server_vector_index_loop_unavailable')

        context.vector_on_ready = vector_ready

        async def maintain():
            while True:
                await asyncio.sleep(60)
                await pool.collect_idle()
                try:
                    await inspirations.tick()
                except Exception as error:
                    LOGGER.warning('automatic_inspiration_scan_unavailable exception_type=%s', type(error).__name__)
        maintenance = asyncio.create_task(maintain())
        try:
            async with context.native_mcp.native.lifespan():
                yield
        finally:
            accepting_vectors = False
            context.vector_on_ready = None
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            try:
                await inspirations.close()
            finally:
                try:
                    await context.restores.close()
                finally:
                    try:
                        await jobs.close()
                    finally:
                        try:
                            await pool.close()
                        finally:
                            # 用户应用只释放自己的任务，宿主最终统一释放共享权重。
                            await asyncio.to_thread(unload, model_directory(resources.model_root), wait=True)

    @asynccontextmanager
    async def lifetime(application):
        # 绑定覆盖后台任务创建和最终资源释放，离开生命周期后恢复原上下文。
        with resource_context(resources):
            async with server_lifetime(application):
                yield

    application = FastAPI(lifespan=lifetime)
    application.state.deployment = layout
    application.state.device_registry = registry
    application.state.server_users = users
    application.state.server_user_pool = pool
    application.state.server_context = context
    from .v2.native_mcp import NativeRuntime
    context.native_mcp = NativeRuntime(registry)
    application.state.server_related_owners = (context.native_mcp.related_owner,)
    context.restores.install(application)
    from .v2.server_control import install_server_routes
    from .v2.server_accounts import ServerAccounts, install_account_routes
    accounts = ServerAccounts(users, registry)
    install_server_routes(application, users=users, registry=registry, audit=audit, jobs=jobs, accounts=accounts)
    install_account_routes(application, accounts=accounts)
    from .v2.server_model_routes import install_server_model_routes
    install_server_model_routes(application, models=models)
    from .v2.server_model_balance_routes import install_server_model_balance_routes
    install_server_model_balance_routes(application, balances=model_balances)
    from .v2.shared_projects import SharedProjects
    from .v2.shared_runtime import SharedDataRoots
    from .v2.shared_routes import install_shared_project_routes
    context.shared_projects = SharedProjects(users, SharedDataRoots(layout.server_root))
    application.state.shared_projects = context.shared_projects
    from .v2.shared_storage import SharedStorage
    application.state.shared_storage = SharedStorage(context.shared_projects, jobs)
    from .v2.shared_query import SharedQueryFactory
    context.shared_query_factory = SharedQueryFactory(context.shared_projects, registry)
    application.state.shared_query_factory = context.shared_query_factory
    from .v2.shared_scopes import SharedScopes
    from .v2.shared_scope_routes import install_shared_scope_routes
    scopes = SharedScopes(context.shared_projects, server_context=context)
    application.state.shared_scopes = scopes
    install_shared_scope_routes(application, scopes=scopes)
    install_shared_project_routes(application, registry=context.shared_projects)
    from .v2.shared_extraction import SharedExtraction
    from .v2.shared_extraction_routes import install_shared_extraction_routes
    extraction = SharedExtraction(context.shared_projects, server_context=context)
    application.state.shared_extraction = extraction
    install_shared_extraction_routes(application, extraction=extraction)
    from .v2.shared_backup import SharedBackups
    from .v2.shared_backup_routes import install_shared_backup_routes
    application.state.shared_backups = SharedBackups(context.shared_projects, context)
    install_shared_backup_routes(application, owner=application.state.shared_backups)
    from .v2.shared_library import SharedLibrary
    from .v2.shared_library_routes import install_shared_library_routes
    library = SharedLibrary(context.shared_query_factory)
    application.state.shared_library = library
    install_shared_library_routes(application, library=library, context=context)
    from .v2.shared_intake import SharedIntake
    from .v2.shared_intake_routes import install_shared_intake_routes
    intake = SharedIntake(context.shared_query_factory, context)
    application.state.shared_intake = intake
    install_shared_intake_routes(application, intake=intake, context=context)
    from .v2.conversation_shares import ConversationShares
    from .v2.conversation_share_routes import install_conversation_share_routes
    shares = ConversationShares(context.shared_projects, context, library, intake)
    application.state.conversation_shares = shares
    application.state.server_related_owners += (shares,)
    install_conversation_share_routes(application, shares=shares)
    install_device_routes(application, registry=registry)
    from backend.api.routes.realtime_asr import consume_server_ticket
    application.state.server_realtime_ticket_consumer = consume_server_ticket
    application.add_middleware(_UserDispatcher, application=application, context=context, audit=audit)
    install_device_authentication(application, registry=registry, auth=auth)
    from .v2.external_proxy_identity import ProxyDeviceHeaders
    application.add_middleware(ProxyDeviceHeaders, authority=auth)
    from .v2.native_mcp_routes import NativeGateway
    application.add_middleware(NativeGateway, native=context.native_mcp.native, oauth=context.native_mcp.oauth)
    return application


@dataclass
class _Child:
    application: object
    lifespan: object
    touched: float
    active: int = 0
    retiring: object = None


class UserApplicationPool:
    def __init__(self, users, *, factory, clock=time.monotonic, idle_seconds=1800,pending_jobs=None,
                 model_balances=None):
        self.users, self.factory, self.clock = users, factory, clock
        self.idle_seconds = idle_seconds
        self.pending_jobs = pending_jobs
        self.model_balances = model_balances
        self._model_balance_jobs = {}
        self._request_leases = {}
        self.restore_admission = None
        self.loaded_observer = None
        self._children, self._locks = {}, {}
        self._controls, self._deleting = {}, {}
        self._closing, self._changed = False, asyncio.Event()

    @property
    def loaded_user_ids(self):
        return tuple(self._children)

    @contextmanager
    def request_scope(self, user_id, app, access):
        token, root = object(), self.users.root_for(user_id)
        self._request_leases[token] = (user_id, app, access)
        subject = _SERVER_REQUEST_SUBJECT.set((self, token, user_id, root))
        try:
            yield
        finally:
            _SERVER_REQUEST_SUBJECT.reset(subject)
            del self._request_leases[token]

    def request_current(self, token, user_id, root, access):
        child = self._children.get(user_id)
        return bool(not self._closing and access is not None and child is not None
            and child.active > 0 and child.retiring is None
            and self._request_leases.get(token) == (user_id, child.application, access)
            and access.target_user_id == user_id and self.users.root_for(user_id) == root
            and child.application.state.recognition_runtime_root == root)

    def _pending_work(self, app, *, detached_only=False, ignore_native=False):
        """Observe actual executors; durable Turn status is never completion."""
        tasks = [] if detached_only else list(getattr(app.state, 'workbench_tasks', ()))
        if not detached_only:
            # 索引从 create_task 起就保留应用；job lease 内不反向等待调用任务。
            tasks.extend(getattr(app.state, 'memory_embedding_index_tasks', ()))
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
        subscriptions = getattr(getattr(app.state, 'recognition_models', None), 'subscriptions', None)
        if subscriptions is not None and not detached_only and not self._closing:
            # 授权等待期间保留用户应用；关闭时由原 lifespan 取消，不等待用户操作。
            observed = observed or subscriptions.has_pending_login()
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
        host = getattr(app.state, 'external_execution_host', None)
        if host is not None and not detached_only and not ignore_native:
            # 原生租约和失败清理 owner 不能随着已终态 Turn 被当成空闲。
            observed = observed or bool(host._issued_leases) or any(not owner.closed for owner in host._bootstrap_owners)
        pending = tuple(dict.fromkeys(task for task in tasks if not task.done()))
        return pending, observed

    async def _settle_work(self, app, *, detached_only=False, ignore_native=False):
        while True:
            tasks, observed = self._pending_work(app, detached_only=detached_only, ignore_native=ignore_native)
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
        scheduler = getattr(self.pending_jobs, '__self__', None)
        accepted = isinstance(scheduler, ServerJobScheduler) and scheduler.active_for(user_id) > 0
        async with self.lease(user_id, existing_job=accepted) as app:
            token, root = object(), self.users.root_for(user_id)
            self._model_balance_jobs[token] = (user_id, root)
            subject = _SERVER_JOB_SUBJECT.set((self, token, user_id, root))
            try:
                scope = (self.model_balances.job_scope(user_id, root, lease_token=token)
                         if self.model_balances is not None else nullcontext())
                with scope:
                    try:
                        yield app
                    finally:
                        # 真实执行 Future 收口后才撤销后台任务的金额资格。
                        await retain_until_finished(self._settle_work(app, detached_only=True))
            finally:
                _SERVER_JOB_SUBJECT.reset(subject)
                del self._model_balance_jobs[token]

    def model_balance_job_current(self, token, user_id, user_root):
        root = Path(user_root).resolve()
        child = self._children.get(user_id)
        return bool(not self._closing and self._model_balance_jobs.get(token) == (user_id, root)
            and child is not None and child.active > 0 and child.retiring is None
            and getattr(child.application.state, 'server_user_id', None) == user_id
            and getattr(child.application.state, 'recognition_runtime_root', None) == root
            and self.users.root_for(user_id) == root)

    @asynccontextmanager
    async def lease(self, user_id, *, restore_token=None, existing_job=False):
        if self._closing:
            raise ValueError('server_runtime_closed')
        root = self.users.root_for(user_id)
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        while True:
            async with lock:
                if self._closing:
                    raise ValueError('server_runtime_closed')
                if self.restore_admission is not None and not existing_job and not self.restore_admission(user_id, token=restore_token):
                    raise UserError('runtime_restore_in_progress')
                if user_id in self._deleting:
                    raise UserError('user_delete_busy')
                if self.users.deletion_pending(user_id):
                    raise UserError('user_deletion_in_progress')
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
                            if self.loaded_observer is not None:
                                try:
                                    await self.loaded_observer(user_id, app)
                                except BaseException:
                                    await lifetime.__aexit__(None, None, None)
                                    from backend.shared.runtime_logging import close_runtime_logging
                                    close_runtime_logging(app)
                                    raise
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

    @asynccontextmanager
    async def control_lease(self, user_ids, *, deleting_user_id=None, restore_token=None):
        """中央请求保留到审计 outcome 结束，同一目标的删除不能抢先清理。"""
        identities = tuple(sorted(user_ids))
        token = object()
        # 准入检查、登记和删除独占标记都在事件循环内连续完成，不持锁等待其它用户。
        if self._closing:
            raise UserError('user_unavailable')
        for user_id in identities:
            self.users.get(user_id)
            if self.restore_admission is not None and not self.restore_admission(user_id, token=restore_token, control=True):
                raise UserError('runtime_restore_in_progress')
            if user_id in self._deleting:
                raise UserError('user_delete_busy')
            if self.users.deletion_pending(user_id) and deleting_user_id != user_id:
                raise UserError('user_deletion_in_progress')
        for user_id in identities:
            self._controls.setdefault(user_id, set()).add(token)
        try:
            yield token
        finally:
            # 集合只在事件循环内、无 await 的短区间增删；outcome 已完成后再归还。
            for user_id in identities:
                controls = self._controls[user_id]
                controls.remove(token)
                if not controls:
                    del self._controls[user_id]

    @asynccontextmanager
    async def deletion_lease(self, user_id, prepare, *, control_token=None):
        """在同一准入锁内封闭新租约，再关闭无忙碌任务的原应用。"""
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        ownership, claimed = object(), False
        try:
            async with lock:
                if (self._closing or user_id in self._deleting
                        or (self.pending_jobs is not None and self.pending_jobs(user_id))
                        or any(token is not control_token for token in self._controls.get(user_id, ()))):
                    raise UserError('user_delete_busy')
                child = self._children.get(user_id)
                if child is not None and (child.active or child.retiring is not None
                                         or any(self._pending_work(child.application))):
                    raise UserError('user_delete_busy')
                self._deleting[user_id] = ownership
                claimed = True
                identity = prepare()
                if inspect.isawaitable(identity):
                    identity = await identity
                if child is not None:
                    child.retiring = asyncio.get_running_loop().create_future()
            if child is not None:
                await self._retire(user_id, child)
            yield identity
        finally:
            if claimed:
                async with lock:
                    if self._deleting.get(user_id) is ownership:
                        del self._deleting[user_id]

    def start_vector_indexes(self):
        """只通知已存在的用户应用，不为加载权重创建或扫描其它空间。"""
        if self._closing:
            return
        for child in tuple(self._children.values()):
            state = child.application.state
            embeddings = getattr(state, 'memory_embedding_settings', None)
            models = getattr(state, 'recognition_models', None)
            start = getattr(state, 'start_vector_index', None)
            if child.retiring is not None or embeddings is None or embeddings.closed or models is None or start is None:
                continue
            try:
                if models.public()['embedding'].get('mode') == 'local':
                    start()
            except Exception as error:
                LOGGER.warning('server_vector_index_schedule_failed exception_type=%s', type(error).__name__)

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
            embeddings = getattr(child.application.state, 'memory_embedding_settings', None)
            if embeddings is not None:
                embeddings.begin_close()
            daily = getattr(child.application.state,'memory_daily_jobs',None)
            if daily is not None:
                await daily.stop()
            recovery = getattr(child.application.state, 'effect_recovery_service', None)
            if recovery is not None:
                # Core's bounded shutdown can leave a provider/recovery thread
                # alive. Stop its next pass, then observe real completion before
                # the lifespan closes clients or the root is recreated.
                recovery.shutdown(timeout_seconds=0)
            # 执行任务先结束，再让原 Host 收口残余租约；不能反向等 Host 自己关闭。
            await self._settle_work(child.application, ignore_native=True)
            host = getattr(child.application.state, 'external_execution_host', None)
            if host is not None:
                await retain_until_finished(asyncio.to_thread(host.close))
                if host._issued_leases or host._bootstrap_owners:
                    raise UserError('user_delete_incomplete')
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
        for child in tuple(self._children.values()):
            embeddings = getattr(child.application.state, 'memory_embedding_settings', None)
            if embeddings is not None:
                embeddings.begin_close()
        while any(child.active for child in self._children.values()):
            self._changed.clear()
            await self._changed.wait()
        failure = None
        for user_id in tuple(self._children):
            child = None
            try:
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
            except BaseException as error:
                # 保留第一个故障，但其它用户的安装线程仍须全部收口。
                if failure is None:
                    failure = error
                embeddings = getattr(child.application.state, 'memory_embedding_settings', None) if child else None
                if embeddings is not None:
                    try:
                        await retain_until_finished(asyncio.to_thread(embeddings.close))
                    except BaseException as close_error:
                        LOGGER.error('server_vector_install_close_failed exception_type=%s', type(close_error).__name__)
        if failure is not None:
            raise failure
