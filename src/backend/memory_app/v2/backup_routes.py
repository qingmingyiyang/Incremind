import asyncio
import sqlite3
import zipfile
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.memory_app.backup_archive import BackupArchiveError, backup_io_path
from backend.memory_app.server_jobs import QuotaError, retain_until_finished
from backend.memory_app.workspace_contracts import _json
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.connection_scope import with_connection_scope
from .backup_schedule import BackupSchedule
from .backup_transfers import BackupTransfers, command_id
from ..runtime_restore_preview import RestorePreviewError


class _Download(StreamingResponse):
    def __init__(self, archive, owner, request, cap, transfer):
        self.archive, self.owner, self.request = archive, owner, request
        self.cap, self.transfer = cap, transfer
        super().__init__(self.content(), media_type='application/zip', headers={
            'Content-Length': str(archive.size_bytes), 'Content-Disposition': f'attachment; filename="{archive.filename}"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    async def content(self):
        with backup_io_path(self.archive.path).open('rb') as stream:
            while True:
                chunk = await retain_until_finished(asyncio.to_thread(with_connection_scope(self.read_chunk), stream))
                if not chunk:
                    return
                yield chunk

    def read_chunk(self, stream):
        self.owner.authorize(self.request)
        if self.cap is not None:
            self.cap.validate()
        return stream.read(self.owner.limits['chunk_bytes'])

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                # 断连可能让生成器暂停在 yield，先关闭真实文件再清除 Windows 目录。
                await retain_until_finished(self.body_iterator.aclose())
            finally:
                await retain_until_finished(self.transfer.__aexit__(None, None, None))


def _failure(error):
    if isinstance(error, QuotaError):
        return JSONResponse({'detail': str(error)}, status_code=429, headers={'Cache-Control': 'no-store'})
    code = error.code if isinstance(error, BackupArchiveError) else 'backup_archive_invalid'
    status = 413 if code == 'backup_archive_limit_exceeded' else 409 if 'changed' in code or 'exists' in code else 400
    return JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})


def install_backup_routes(application, *, runtime_root, records):
    owner, schedule = BackupTransfers(application, runtime_root, records), BackupSchedule(records)
    application.state.memory_backup_transfers = owner
    application.state.memory_backup_schedule = schedule
    router = APIRouter(prefix='/api/v2/backups')

    @router.post('/restore-previews/{preview_id}/release')
    async def release_restore_preview(preview_id: str, request: Request):
        owner.authorize(request)
        if re.fullmatch(r'preview-[a-f0-9]{32}', preview_id) is None:
            raise HTTPException(400, 'restore_preview_invalid')
        previews = getattr(application.state, 'runtime_restore_previews', None)
        try:
            result = await previews.release(request, preview_id) if previews is not None else {
                'preview_id': preview_id, 'status': 'settled'}
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except RestorePreviewError as error:
            return JSONResponse({'detail': str(error)}, status_code=409, headers={'Cache-Control': 'no-store'})

    @router.get('/snapshots/{snapshot_id}/download')
    async def download(snapshot_id: str, request: Request):
        try:
            transfer = owner.download(snapshot_id, request)
            archive, cap = await transfer.__aenter__()
            try:
                return _Download(archive, owner, request, cap, transfer)
            except BaseException:
                await retain_until_finished(transfer.__aexit__(None, None, None))
                raise
        except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as error:
            return _failure(error)

    @router.put('/imports/{identity}')
    async def import_archive(identity: str, request: Request):
        try:
            return await owner.receive(identity, request)
        except TimeoutError:
            result = await owner.read(identity, request)
            return JSONResponse(result, status_code=202, headers={'Cache-Control': 'no-store'})
        except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as error:
            return _failure(error)

    @router.get('/imports/{identity}')
    async def read_import(identity: str, request: Request):
        try:
            result = await owner.read(identity, request)
            if result is None:
                raise HTTPException(404, 'backup_command_not_found')
            return result
        except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as error:
            return _failure(error)

    @router.post('/imports/{identity}/cancel-unreceived')
    async def cancel_import(identity: str, request: Request):
        body = await _json(request)
        if set(body) != {'expected_revision'}:
            raise HTTPException(400, 'backup_cancel_fields_invalid')
        try:
            return await owner.cancel_unreceived(identity, request, expected_revision=body['expected_revision'])
        except (OSError, ValueError, sqlite3.Error) as error:
            return _failure(error)

    @router.get('/schedule')
    @with_connection_scope
    def read_schedule(request: Request):
        owner.authorize(request)
        return schedule.view()

    @router.patch('/schedule')
    async def save_schedule(request: Request):
        body = await _json(request)
        try:
            command_id(body.get('command_id'))
            owner.authorize(request)
            return await retain_until_finished(asyncio.to_thread(with_connection_scope(schedule.save), body,
                validate=lambda: owner.authorize(request)))
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'backup_schedule_changed') from None
        except ValueError as error:
            return _failure(error)

    @router.get('/schedule/commands/{identity}')
    @with_connection_scope
    def read_schedule_command(identity: str, request: Request):
        owner.authorize(request)
        value = schedule.command(command_id(identity))
        if value is None:
            raise HTTPException(404, 'backup_command_not_found')
        return value

    application.include_router(router)
