from datetime import datetime, timezone
from pathlib import Path
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.security.device_identity import server_identity, server_mode
from core.product_core.cloud_asr_provider_settings import GetCloudAsrProviderSettings, TOKENHUB_ASR_SECRET_REF
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..asr_credentials import AsrCredentialError, asr_credential_authority, store_asr_credential
from ..rebuild_storage import build_rebuild_object_store
from ..workspace_contracts import _json
from .devices import _request_owner, DeviceError


COMMANDS = 'v2_asr_key_commands'
_ID = re.compile(r'cmd-[a-z0-9][a-z0-9._-]{7,122}\Z')


class AsrCredentials:
    def __init__(self, records, runtime_root, container):
        if container is None or Path(container.root_dir).resolve() != Path(runtime_root).resolve():
            raise AsrCredentialError('asr_key_owner_unavailable')
        self.records, self.root, self.container = records, Path(runtime_root).resolve(), container

    def _id(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise AsrCredentialError('asr_key_command_invalid')

    def current(self):
        objects, _ = build_rebuild_object_store(self.root)
        settings = GetCloudAsrProviderSettings(objects).execute()
        return {'settings_revision': settings.settings_revision,
            'secret_generation': self.container.secret_store.get_generation(TOKENHUB_ASR_SECRET_REF),
            'has_api_key': self.container.secret_store.has_secret(TOKENHUB_ASR_SECRET_REF)}

    def get(self, identity, *, validate, cancel=False):
        self._id(identity)
        with asr_credential_authority(self.container, validate=validate, independent=False):
            if cancel:
                with self.records.begin() as tx:
                    old = tx.read(COMMANDS, identity)
                    if old is None:
                        tx.put(COMMANDS, identity, {'status': 'cancelled', 'at': datetime.now(timezone.utc).isoformat()}, expected_revision=0)
                        tx.commit()
            row = self.records.read(COMMANDS, identity)
            value = dict(row.payload) if row is not None else {'status': 'not_recorded'}
            if value['status'] == 'writing':
                # 加密文件与 SQLite 无跨介质原子提交，代次变化不能证明本命令完成。
                value['status'] = 'unknown'
            return {'id': identity, **value, 'current': self.current()}

    def save(self, body, *, validate):
        if not isinstance(body, dict) or set(body) != {'api_key', 'command_id', 'expected_settings_revision', 'expected_generation'}:
            raise AsrCredentialError('asr_key_request_invalid')
        identity, value = body['command_id'], body['api_key']
        self._id(identity)
        if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 16 * 1024
                or any(char in value for char in '\r\n\x00')
                or any(type(body[key]) is not int or body[key] < 0 for key in ('expected_settings_revision', 'expected_generation'))):
            raise AsrCredentialError('asr_key_request_invalid')
        with asr_credential_authority(self.container, validate=validate):
            existing = self.records.read(COMMANDS, identity)
            if existing is not None:
                return self.get(identity, validate=validate)
            current = self.current()
            if (current['settings_revision'] != body['expected_settings_revision']
                    or current['secret_generation'] != body['expected_generation']):
                raise AsrCredentialError('asr_key_revision_conflict')
            validate()
            with self.records.begin() as tx:
                tx.put(COMMANDS, identity, {'status': 'writing', 'at': datetime.now(timezone.utc).isoformat(),
                    'expected_settings_revision': body['expected_settings_revision'],
                    'expected_generation': body['expected_generation']}, expected_revision=0)
                tx.commit()
            generation = store_asr_credential(self.container, value, validate=validate)
            with self.records.begin() as tx:
                command = tx.read(COMMANDS, identity)
                tx.put(COMMANDS, identity, {**command.payload, 'status': 'stored', 'generation': generation},
                    expected_revision=command.revision)
                tx.commit()
            return self.get(identity, validate=validate)


def install_asr_credential_routes(application, *, records, runtime_root):
    container = getattr(application.state, 'container', None)
    if container is None:
        return
    owner = AsrCredentials(records, runtime_root, container)
    application.state.asr_credentials = owner
    router = APIRouter(prefix='/api/v2/settings/asr-key')

    def validator(request):
        try:
            expected = _request_owner(request)
        except DeviceError:
            raise HTTPException(403, 'asr_key_identity_changed') from None
        def validate():
            try:
                if _request_owner(request) != expected or server_mode(request) and server_identity(request) is None:
                    raise DeviceError('device_unauthorized')
            except DeviceError:
                raise AsrCredentialError('asr_key_identity_changed') from None
        return validate

    async def run(action, *args, **kwargs):
        try:
            result = await run_in_threadpool(action, *args, **kwargs)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'asr_key_revision_conflict') from None
        except (ValueError, RuntimeError, OSError) as error:
            code = str(error)
            if code not in {'asr_key_revision_conflict', 'server_model_configuration_readonly',
                    'asr_key_identity_changed', 'asr_key_request_invalid', 'asr_key_command_invalid'}:
                code = 'asr_key_store_unavailable'
            raise HTTPException(409 if code.endswith('conflict') else 403 if code in {
                'server_model_configuration_readonly', 'asr_key_identity_changed'} else 400 if code.endswith('invalid') else 503, code) from None
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @router.put('')
    async def save(request: Request):
        validate = validator(request)
        return await run(owner.save, await _json(request), validate=validate)

    @router.get('/operations/{identity}')
    async def read(identity: str, request: Request):
        return await run(owner.get, identity, validate=validator(request))

    @router.post('/operations/{identity}/cancel')
    async def cancel(identity: str, request: Request):
        if await _json(request) != {}:
            raise HTTPException(400, 'asr_key_request_invalid')
        return await run(owner.get, identity, validate=validator(request), cancel=True)

    application.include_router(router)
