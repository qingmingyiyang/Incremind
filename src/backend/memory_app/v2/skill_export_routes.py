"""方法草稿、显式审阅和用户下载的 v2 门面。"""
import os
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from backend.recognition import RecognitionConflict, RecognitionError
from backend.security.audited_records import AuditedRecordStore
from backend.security.user_context import USER_ACCESS, UserAccess
from backend.shared.deployment import DeploymentLayout
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..workspace_contracts import _json, _project
from .skill_exports import SkillExports
from .server_users import ServerUsers
from .privacy import egress_allowed


def _desktop(request: Request):
    # 服务器只能下载 ZIP，选本机目录仍限定电脑本机。
    layout = getattr(request.app.state, 'deployment', None)
    if layout is not None and layout.mode != 'desktop':
        raise HTTPException(503, 'skill_user_domain_unavailable')


def _user_domain(request: Request, *, records, service, models):
    state = request.app.state
    layout = getattr(state, 'deployment', None)
    context = getattr(state, 'server_context', None)
    access = USER_ACCESS.get()
    if layout is None or getattr(layout, 'mode', None) == 'desktop':
        # 保留独立桌面路由夹具；已有任何服务器绑定时，缺部署元数据不得降级。
        if (context is not None or access is not None
                or getattr(state, 'server_user_id', None) is not None
                or getattr(state, 'server_users', None) is not None
                or request.scope.get('state', {}).get('server_dispatch_authority') is not None):
            raise HTTPException(503, 'skill_user_domain_unavailable')
        return
    users = getattr(state, 'server_users', None)
    if (not isinstance(layout, DeploymentLayout) or layout.mode != 'server'
            or not isinstance(users, ServerUsers) or not isinstance(access, UserAccess)
            or not isinstance(records, AuditedRecordStore) or context is None):
        raise HTTPException(503, 'skill_user_domain_unavailable')
    try:
        target = state.server_user_id
        authority = request.scope.get('state', {})
        if (context.users is not users or context.auth is None
                or authority.get('server_dispatch_authority') is not context.auth
                or authority.get('user_access') is not access
                or authority.get('device_identity') is not access.caller
                or access.target_user_id != target or records.user_id != target
                or context.registry is not state.device_registry
                or context.registry.records is not users.records
                or context.pool.users is not users
                or state.recognition_records is not records or service.records is not records
                or state.recognition_service is not service
                or state.recognition_models is not models or models.records is not records):
            raise ValueError('skill_user_domain_unavailable')
        # 权限与物理根均取原用户所有者的当前事实，不把 local-user 改为另一领域。
        if users.authorize_claim(access.caller, target) != (target, access.by):
            raise ValueError('skill_user_domain_unavailable')
        user_root = users.root_for(target)
        if (users.get(target)['disabled_at'] is not None
                or Path(layout.server_root).resolve() != users.server_root
                or Path(context.layout.server_root).resolve() != users.server_root
                or Path(layout.user_root).resolve() != user_root
                or Path(state.recognition_runtime_root).resolve() != user_root
                or Path(state.container.root_dir).resolve() != user_root
                or Path(models.root).resolve() != user_root
                or not Path(records.database_path).resolve().is_relative_to(user_root)):
            raise ValueError('skill_user_domain_unavailable')
    except (AttributeError, TypeError, ValueError, OSError):
        raise HTTPException(503, 'skill_user_domain_unavailable') from None


def _call(entry, *args, **kwargs):
    try:
        return entry(*args, **kwargs)
    except (RecognitionConflict, SQLiteUnitOfWorkConflict):
        raise HTTPException(409, 'skill_sources_changed') from None
    except (ValueError, RecognitionError) as error:
        code = str(error)
        if code == 'skill_export_unavailable':
            status = 404
        elif code == 'skill_generation_disabled':
            status = 403
        elif code in {'skill_revision_conflict', 'skill_sources_changed', 'skill_review_required',
                'skill_source_unavailable', 'skill_source_private',
                'skill_folder_confirmation_required', 'skill_folder_confirmation_conflict', 'skill_folder_exists',
                'skill_generation_origin_invalid'}:
            status = 409
        elif code in {'invalid_skill_document', 'invalid_skill_scene', 'invalid_skill_generation_key'}:
            status = 400
        else:
            raise HTTPException(400, 'invalid_skill_request') from None
        raise HTTPException(status, code) from None


def _fields(body, required, optional=()):
    if set(body).difference(set(required) | set(optional)) or not set(required).issubset(body):
        raise HTTPException(400, 'invalid_skill_fields')


def install_skill_export_routes(application, *, records, service, models):
    exports = SkillExports(records, service, owner_id='local-user')
    def user_domain(request: Request):
        _user_domain(request, records=records, service=service, models=models)

    router = APIRouter(prefix='/api/v2/projects/{project_id}/skill-exports', dependencies=[Depends(user_domain)])

    @router.get('')
    def list_exports(project_id: str, request: Request):
        project = _project(project_id)
        return {'items': _call(exports.list, project),
            'generation_available': models is not None and egress_allowed(records, models, project, 'generation'),
            'local_folder': {'available': False} if getattr(getattr(request.app.state, 'deployment', None),
                'mode', None) == 'server' else {'available': os.name == 'nt', **_call(exports.preferences)}}

    @router.get('/methods')
    def methods(project_id: str, scene: str | None = None):
        return {'items': _call(exports.methods, _project(project_id), scene)}

    @router.post('')
    async def create(project_id: str, request: Request):
        body = await _json(request)
        _fields(body, ('sources', 'document'), ('scene',))
        return await run_in_threadpool(_call, exports.create, _project(project_id), **body)

    def generated(project, body):
        if models is None:
            raise HTTPException(403, 'skill_generation_disabled')
        from .skill_generation import generate_skill
        return _call(generate_skill, models, exports, project,
            sources=body['sources'], scene=body.get('scene'), retry_token=body['key'])

    @router.post('/generate')
    async def generate(project_id: str, request: Request):
        body = await _json(request)
        _fields(body, ('sources', 'key'), ('scene',))
        project = _project(project_id)
        result = await run_in_threadpool(generated, project, body)
        return await run_in_threadpool(_call, exports.create, project,
            sources=body['sources'], scene=body.get('scene'), document=result['document'],
            frozen_sources=result['sources'], generation_turn_id=result['turn_id'])

    @router.get('/{identity}')
    def get(project_id: str, identity: str):
        return _call(exports.get, _project(project_id), _project(identity))

    @router.patch('/{identity}')
    async def edit(project_id: str, identity: str, request: Request):
        body = await _json(request)
        _fields(body, ('expected_revision', 'document'))
        return await run_in_threadpool(_call, exports.edit, _project(project_id), _project(identity), **body)

    @router.post('/{identity}/review')
    async def review(project_id: str, identity: str, request: Request):
        body = await _json(request)
        _fields(body, ('expected_revision',))
        return await run_in_threadpool(_call, exports.review, _project(project_id), _project(identity), **body)

    @router.post('/{identity}/regenerate')
    async def regenerate(project_id: str, identity: str, request: Request):
        body = await _json(request)
        _fields(body, ('expected_revision', 'sources'), ('document', 'scene', 'key'))
        project = _project(project_id)
        identity = _project(identity)
        await run_in_threadpool(_call, exports.require_revision, project, identity, body['expected_revision'])
        if 'document' not in body:
            if 'key' not in body:
                raise HTTPException(400, 'invalid_skill_fields')
            result = await run_in_threadpool(generated, project, body)
            body = {**body, 'document': result['document'], 'frozen_sources': result['sources'],
                'generation_turn_id': result['turn_id']}
        body.pop('key', None)
        return await run_in_threadpool(_call, exports.regenerate, project, identity, **body)

    @router.post('/{identity}/download')
    async def download(project_id: str, identity: str, request: Request):
        body = await _json(request)
        _fields(body, ('expected_revision',))
        project, identity = _project(project_id), _project(identity)
        current = await run_in_threadpool(_call, exports.get, project, identity)
        raw = await run_in_threadpool(_call, exports.download, project, identity, **body)
        return Response(raw, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="' + current['document']['name'] + '.zip"'})

    @router.post('/{identity}/folder')
    async def folder(project_id: str, identity: str, request: Request):
        _desktop(request)
        body = await _json(request)
        _fields(body, ('expected_revision', 'directory', 'confirm_first_export', 'expected_confirmation_revision'))
        return await run_in_threadpool(_call, exports.export_folder, _project(project_id), _project(identity), **body)

    application.include_router(router)
