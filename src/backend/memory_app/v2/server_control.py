"""Server user administration and access-history read models."""
from pathlib import Path
import re
from typing import Literal
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from core.storage_provider import JsonObjectStore, SourceAssetRuntimeStore

from backend.security.user_context import USER_ACCESS, UserError
from .devices import _qr


_SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')


def _scoped_owner(owner, user_root):
    path = Path(getattr(owner, 'database_path', getattr(owner, 'root', user_root))).resolve()
    if not path.is_relative_to(user_root):
        raise UserError('user_forbidden')
    return owner


def _record_writer(records, collection, object_id, revision, user_root):
    _scoped_owner(records, user_root)
    current = records.read_projected(collection, object_id, fields=('id',))
    if current is None:
        raise UserError('attribution_not_found')
    if current.revision != revision:
        raise UserError('attribution_revision_conflict')
    return records.writer_for(collection, object_id, revision)


def _original_owner(child):
    domains = child.state.workspace_domains
    return (domains.confirmations.source_store if domains.confirmations is not None
            else domains.legacy_reviews.object_store)


def _object_writer(owner, collection, object_id, revision, user_root):
    _scoped_owner(owner, user_root)
    if isinstance(owner, SourceAssetRuntimeStore) and owner._is_sqlite_collection(collection):
        owner._guard_source_authority(collection)
        return _record_writer(owner._records_or_raise(), owner._sqlite_collection(collection),
                              object_id, revision, user_root)
    current = owner.revision(collection, object_id)
    if current == 0:
        raise UserError('attribution_not_found')
    if current != revision:
        raise UserError('attribution_revision_conflict')
    return owner.attribution(collection, object_id, revision)


def response(status, value):
    return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})


async def body(request, allowed, required=()):
    try:
        value = await request.json()
    except ValueError:
        raise UserError('user_request_invalid') from None
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise UserError('user_request_invalid')
    return value


def install_server_routes(application, *, users, registry, audit, jobs):
    router = APIRouter(prefix='/api/v2/server')

    @router.get('/users')
    def list_users():
        access = USER_ACCESS.get()
        caller = users.get(access.caller.user_id)
        items = users.list_for(access.caller) if users.can(access.caller, action='manage_users') else []
        for row in items:
            row['device_count'] = sum(item['revoked_at'] is None for item in registry.list_devices(row['user_id']))
            row['storage_bytes'] = jobs.storage_bytes(row['user_id'])
            row['job_seconds_today'] = jobs.job_seconds(row['user_id'])
        return response(200, {'caller': caller, 'target_user_id': access.target_user_id, 'items': items})

    @router.post('/users')
    async def create_user(request: Request):
        data = await body(request, {'name'}, {'name'})
        return response(201, users.create(USER_ACCESS.get().caller, **data))

    @router.patch('/users/{user_id}')
    async def update_user(user_id: str, request: Request):
        data = await body(request, {'expected_revision', 'disabled', 'storage_limit_mb', 'job_minutes_per_day'}, {'expected_revision'})
        return response(200, users.update(USER_ACCESS.get().caller, user_id, **data))

    @router.post('/users/{user_id}/pair')
    async def pair_user(user_id: str, request: Request):
        await body(request, set())
        actor = USER_ACCESS.get().caller
        # Pairing another space is a highest-role operation, not impersonation.
        with users.records.begin() as tx:
            users._admin(tx, actor)
        users.get(user_id)
        issued = registry.issue_pairing(user_id=user_id, actor=actor.device_id)
        url = str(request.base_url).rstrip('/') + '/pair#code=' + issued['code']
        return response(200, {'expires_at': issued['expires_at'], 'url': url, 'qr': _qr(url)})

    @router.get('/audit')
    def access_history():
        return response(200, {'items': audit.list_for(USER_ACCESS.get())})

    @router.get('/attribution')
    async def attribution(collection: str, object_id: str, revision: int = Query(gt=0),
                          authority: Literal['records', 'originals', 'audio'] = 'records'):
        access = USER_ACCESS.get()
        if not _SEGMENT.fullmatch(collection) or not _SEGMENT.fullmatch(object_id):
            raise UserError('attribution_invalid')
        user_root = users.root_for(access.target_user_id).resolve()
        async with application.state.server_user_pool.lease(access.target_user_id) as child:
            if Path(child.state.recognition_runtime_root).resolve() != user_root:
                raise UserError('user_forbidden')
            if authority == 'records':
                writer = _record_writer(child.state.recognition_records, collection, object_id,
                                        revision, user_root)
            else:
                owner = _original_owner(child)
                if authority == 'audio':
                    owner = JsonObjectStore(user_root / 'workspace' / 'asr-internal',
                                            namespace_id=owner.namespace_id)
                writer = _object_writer(owner, collection, object_id, revision, user_root)
            if writer is not None and writer.get('target_user_id') != access.target_user_id:
                raise UserError('user_forbidden')
        return response(200, {'by': writer})

    application.include_router(router)

    async def user_error(_request, error):
        code = str(error)
        status = {'user_unauthorized': 401, 'user_forbidden': 403, 'user_not_found': 404,
            'user_revision_conflict': 409, 'user_name_conflict': 409,
            'attribution_not_found': 404, 'attribution_revision_conflict': 409}.get(code, 400)
        return response(status, {'detail': code})
    application.add_exception_handler(UserError, user_error)
