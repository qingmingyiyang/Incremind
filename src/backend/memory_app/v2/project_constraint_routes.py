from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.recognition import WorkScope
from ..constraints import ProjectConstraintError
from ..workspace_contracts import _json, _project


_FIELDS = {'content', 'enabled', 'valid_from', 'valid_until', 'activation',
           'expected_revision', 'expected_scope_revision', 'expected_project_revision'}


def _revision(body, name, minimum):
    value = body.get(name)
    if type(value) is not int or value < minimum:
        raise HTTPException(400, f'invalid_{name}')
    return value


def _failure(error):
    code = str(error)
    if code == 'constraint_project_unavailable':
        return HTTPException(404, code)
    if (code.endswith('_invalid') or code in {'constraint_scene_unavailable'}
            or code.startswith(('content ', 'enabled ', 'valid_from ', 'valid_until '))):
        return HTTPException(400, code)
    return HTTPException(409, code)


def install_project_constraint_routes(application, *, service):
    # 个人路径只接受装配明确提供的个人 owner，共享路径由其实际成员边界另行装配。
    if service.origin_kind != 'personal':
        raise ValueError('personal_constraint_owner_required')
    router = APIRouter(prefix='/api/v2/projects')

    @router.get('/{project_id}/constraints')
    def listing(project_id: str):
        try:
            return service.list(WorkScope('local-user', _project(project_id)))
        except ProjectConstraintError as error:
            raise _failure(error) from None

    @router.put('/{project_id}/constraints/{constraint_id}')
    async def save(project_id: str, constraint_id: str, request: Request):
        body = await _json(request)
        if set(body).difference(_FIELDS):
            raise HTTPException(400, 'invalid_constraint_fields')
        activation = body.get('activation')
        if not isinstance(activation, dict) or set(activation) != {'mode', 'scene', 'keywords'}:
            raise HTTPException(400, 'invalid_constraint_activation')
        revisions = {name: _revision(body, name, 1 if name == 'expected_project_revision' else 0)
                     for name in ('expected_revision', 'expected_scope_revision', 'expected_project_revision')}
        try:
            return service.upsert(WorkScope('local-user', _project(project_id)), constraint_id,
                revisions['expected_revision'], body.get('content'),
                expected_scope_revision=revisions['expected_scope_revision'],
                expected_project_revision=revisions['expected_project_revision'],
                enabled=body.get('enabled', True), valid_from=body.get('valid_from'),
                valid_until=body.get('valid_until'), **activation)
        except ProjectConstraintError as error:
            raise _failure(error) from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @router.post('/{project_id}/constraints/activation-preview')
    async def preview_activation(project_id: str, request: Request):
        body = await _json(request)
        if set(body) != {'mode', 'scene', 'keywords'}:
            raise HTTPException(400, 'invalid_constraint_activation')
        try:
            return service.preview_activation(WorkScope('local-user', _project(project_id)), **body)
        except ProjectConstraintError as error:
            raise _failure(error) from None
        except ValueError as error:
            return JSONResponse(status_code=400, content={'detail': str(error), 'limits': service.activation_limits()})

    application.include_router(router)
