import asyncio
import os
import sqlite3
import zipfile
import zlib
from functools import partial

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.memory_app.backup_archive import BackupArchiveError, backup_io_path
from backend.memory_app.server_jobs import QuotaError, retain_until_finished
from backend.security.device_identity import server_identity
from backend.security.user_context import USER_ACCESS, UserError
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.connection_scope import with_connection_scope
from .backup_transfers import command_id
from .shared_projects import SharedProjectError


class _SharedDownload(StreamingResponse):
    def __init__(self, transfer, archive, validate, owner):
        self.transfer, self.archive, self.validate, self.owner = transfer, archive, validate, owner
        super().__init__(self.content(), media_type='application/zip', headers={
            'Content-Length': str(archive.size_bytes),
            'Content-Disposition': 'attachment; filename="' + archive.filename + '"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    def read_chunk(self, stream):
        self.validate()
        opened, current = os.fstat(stream.fileno()), backup_io_path(self.archive.path).stat()
        if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)):
            raise SharedProjectError('shared_backup_archive_changed', status=409)
        chunk = stream.read(self.owner.limits['chunk_bytes'])
        self.validate()
        return chunk

    async def content(self):
        with backup_io_path(self.archive.path).open('rb') as stream:
            while True:
                chunk = await retain_until_finished(asyncio.to_thread(with_connection_scope(self.read_chunk), stream))
                if not chunk:
                    return
                yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await retain_until_finished(self.body_iterator.aclose())
            finally:
                await retain_until_finished(self.transfer.__aexit__(None, None, None))


def install_shared_backup_routes(application, *, owner):
    from .shared_backup_import import SharedBackupImports
    imports = SharedBackupImports(owner)
    application.state.shared_backup_imports = imports
    router = APIRouter(prefix='/api/v2/server/shared-projects')

    def actor(request):
        identity, access = server_identity(request), USER_ACCESS.get()
        if identity is None or access is None or access.caller != identity:
            raise HTTPException(401, 'shared_server_identity_required')
        return identity, access.target_user_id

    async def execute(call):
        try:
            value = await call()
            return JSONResponse(value, headers={'Cache-Control': 'no-store'})
        except SharedProjectError as error:
            status, code = error.status, str(error)
        except UserError:
            status, code = 403, 'shared_backup_forbidden'
        except QuotaError:
            status, code = 429, 'shared_backup_quota_exceeded'
        except (SQLiteUnitOfWorkConflict, TimeoutError):
            status, code = 409, 'shared_backup_busy'
        except BackupArchiveError as error:
            status, code = 409 if 'changed' in error.code else 400, error.code
        except (OSError, sqlite3.Error, zipfile.BadZipFile, zlib.error, ValueError):
            status, code = 409, 'shared_backup_incomplete'
        return JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})

    @router.post('/{project_id}/backups')
    async def create(project_id: str, request: Request):
        identity, target = actor(request)
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_backup_request_invalid') from None
        keys = request.headers.getlist('idempotency-key')
        if value != {} or len(keys) != 1:
            raise HTTPException(400, 'shared_backup_request_invalid')
        try:
            key = command_id(keys[0])
        except BackupArchiveError:
            raise HTTPException(400, 'shared_backup_command_invalid') from None
        return await execute(partial(owner.run, 'create', identity, project_id, key, owner_user_id=target))

    @router.get('/{project_id}/backups')
    async def listing(project_id: str, request: Request):
        identity, target = actor(request)
        return await execute(partial(owner.query, 'list', identity, project_id, owner_user_id=target))

    @router.get('/backups/owned')
    async def owned(request: Request):
        identity, target = actor(request)
        return await execute(partial(owner.query, 'list', identity, owner_user_id=target))

    @router.get('/backup-commands/{identity}')
    async def command(identity: str, request: Request):
        caller, target = actor(request)
        return await execute(partial(owner.query, 'read', caller, identity, owner_user_id=target))

    @router.get('/backups/{identity}/download')
    async def download(identity: str, request: Request):
        async def open_download():
            caller, target = actor(request)
            transfer = owner.opened(caller, identity, owner_user_id=target)
            archive, validate = await transfer.__aenter__()
            try:
                return _SharedDownload(transfer, archive, validate, owner)
            except BaseException:
                await transfer.__aexit__(None, None, None)
                raise
        # 流式响应在发送前取得真实租约，响应结束及断连后才释放。
        try:
            return await open_download()
        except (SharedProjectError, UserError, BackupArchiveError, OSError, ValueError, zipfile.BadZipFile, zlib.error) as error:
            async def failed(failure=error):
                raise failure
            return await execute(failed)

    @router.post('/backups/{backup_id}/projects')
    async def restore_project(backup_id: str, request: Request):
        identity, target = actor(request)
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_backup_import_request_invalid') from None
        keys = request.headers.getlist('idempotency-key')
        if (not isinstance(value, dict) or set(value) != {'name', 'approved', 'batch_size'}
                or value['approved'] is not True or len(keys) != 1):
            raise HTTPException(400, 'shared_backup_import_request_invalid')
        return await execute(partial(imports.run, identity, backup_id, keys[0],
            name=value['name'], batch_size=value['batch_size'], owner_user_id=target))

    @router.get('/backup-import-commands/{identity}')
    async def import_command(identity: str, request: Request):
        caller, target = actor(request)
        return await execute(partial(imports.query, caller, identity, owner_user_id=target))

    application.include_router(router)
