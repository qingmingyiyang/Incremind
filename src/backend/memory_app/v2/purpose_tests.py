import asyncio
from contextlib import asynccontextmanager
import re
from threading import RLock
from time import monotonic

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.shared.llm.image_input import image_content
from backend.shared.secret_detection import redact_secrets
from core.storage_provider.connection_scope import with_connection_scope
from ..kernel.image_read import read_images
from ..kernel.memory_turn import MemoryTurn
from ..kernel.receipt_projection import aggregate_cost, kernel_call_groups
from ..local_image_provider import local_image_provider
from ..server_jobs import QuotaError, retain_until_finished
from ..workspace_contracts import _json, _now, _project
from .devices import _request_owner, DeviceError
from .policies import get
from .privacy import egress_allowed, is_private_project
from .purpose_samples import CATALOG, CHECKS, PROOF, SEARCH_QUERY, RegisteredImageReference, RegisteredPurposeSample, sample_catalog
from .receipt_history import cost_totals, usage_totals, usage_view, wire_costs
from .search import search_once


_ID = re.compile(r'test-[a-z0-9][a-z0-9_-]{7,100}\Z')
_FIELDS = {'id', 'purpose', 'project_id', 'sample_id', 'expected'}


def safe_error(error):
    code = getattr(error, 'public_code', str(error))
    return code if isinstance(code, str) and re.fullmatch(r'(?:purpose_test|server_model|server_asr|vision|search|model)_[a-z0-9_]{1,100}', code) else 'purpose_test_failed'


class PurposeTests:
    def __init__(self, records, root, models, container):
        self.records, self.root, self.models, self.container = records, root, models, container
        self._active, self._lock = set(), RLock()

    def expected(self, purpose):
        if purpose == 'asr':
            from ..purpose_test_asr import asr_test_configuration
            return asr_test_configuration(self.root, self.container)['identity']
        value = self.models.public_for_purpose(purpose)
        return {key: value.get(key) for key in ('revision', 'mode', 'mode_revision', 'server_model_binding')}

    def _identity(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise ValueError('purpose_test_identity_invalid')

    def get(self, identity, *, validate):
        self._identity(identity)
        validate()
        row = self.records.read(CHECKS, identity)
        if row is None:
            return {'id': identity, 'status': 'not_recorded'}
        value = dict(row.payload)
        if value['status'] == 'running' and identity not in self._active:
            value['status'] = 'unknown'
        if value['status'] in {'running', 'unknown'} and value.get('purpose') == 'asr' and value.get('cleanup') != 'failed':
            from ..purpose_test_asr import read_asr_test
            result = read_asr_test(self.records, self.root, identity)
            if result is not None:
                value.update(result)
        if value['status'] in {'running', 'unknown'} and value.get('purpose') == 'search' and value.get('cleanup') != 'failed':
            expected = {'kind': 'web.search', 'project': value['project_id'], 'key': 'purpose-test:' + identity, 'purpose': 'search'}
            matches = [row for row in self.records.list('v2_memory_turn_keys') if row.payload['identity'] == expected]
            if len(matches) > 1:
                raise ValueError('purpose_test_proof_unavailable')
            if matches:
                value['turn_id'] = matches[0].object_id
        if value['status'] in {'unknown', 'running'} and value.get('turn_id') and value.get('cleanup') != 'failed':
            store = MemoryTurn.store_for(self.records)
            output = store.get_immutable_payload(value['turn_id'], 'memory-generation-output-v1')
            proof = store.get_immutable_payload(value['turn_id'], PROOF)
            request = store.get_request(value['turn_id'])
            events = tuple(store.events_after(value['turn_id']))
            complete = bool(events and events[-1]['type'] == 'turn.completed')
            if (complete and output is not None and request is not None and proof is not None
                    and request['scope']['project_id'] == value['project_id']
                    and proof[1]['command_id'] == identity and proof[1]['id'] == value['sample_id']):
                if value['purpose'] == 'vision':
                    images = output[1]['output'].get('images')
                    if isinstance(images, list) and len(images) == 1 and isinstance(images[0].get('text'), str):
                        value.update(status='complete', text=redact_secrets(images[0]['text']))
            if (complete and output is not None and request is not None and value.get('purpose') == 'search'
                    and request['desired_outcome'] == 'web.search' and request['scope']['project_id'] == value['project_id']
                    and request['input']['text'] == SEARCH_QUERY and request['input']['refs'] == []):
                results = output[1]['output']
                if not isinstance(results, list):
                    raise ValueError('purpose_test_proof_unavailable')
                value.update(status='complete', result_count=len(results), titles=[redact_secrets(row['title'])[:200] for row in results[:3]])
        value.pop('input_proof', None)
        value.pop('frozen_input', None)
        if value.get('turn_id'):
            value.update(self._calls(value['turn_id'], value['project_id']))
        return {'id': identity, **value}

    def cancel(self, identity, *, validate):
        self._identity(identity)
        validate()
        with self._lock, self.records.begin() as tx:
            if tx.read(CHECKS, identity) is None:
                validate()
                tx.put(CHECKS, identity, {'status': 'cancelled', 'at': _now()}, expected_revision=0)
                tx.commit()
        return self.get(identity, validate=validate)

    def _save(self, identity, changes, *, validate):
        validate()
        with self.records.begin() as tx:
            row = tx.read(CHECKS, identity)
            tx.put(CHECKS, identity, {**row.payload, **changes}, expected_revision=row.revision)
            tx.commit()

    def _calls(self, turn_id, project):
        calls = [call for group in kernel_call_groups(self.root, turn_id=turn_id, project=project,
            remote_only=False, records=self.records, include_attempts=True) for call in group['calls']]
        return {'usage': usage_view(usage_totals(calls)), 'cost': cost_totals(wire_costs(calls)),
            'model_call_count': len(calls), 'attempt_count': sum(len(call['wire_attempts']) for call in calls),
            'model_cost': aggregate_cost(calls)}

    def _dispose(self, sample):
        if sample.purpose == 'asr':
            from ..purpose_test_asr import dispose_asr_test
            dispose_asr_test(sample)
        sample.dispose()

    def cleanup(self, identity, *, validate):
        self._identity(identity)
        validate()
        if identity in self._active:
            return self.get(identity, validate=validate)
        row = self.records.read(CHECKS, identity)
        if row is None or row.payload.get('cleanup') != 'failed':
            return self.get(identity, validate=validate)
        sample = RegisteredPurposeSample.for_cleanup(self.records, self.root, identity)
        self._dispose(sample)
        self._save(identity, {'status': row.payload['result_status'], 'error': row.payload.get('result_error'),
            'cleanup': 'complete'}, validate=validate)
        return self.get(identity, validate=validate)

    def admit(self, body, *, validate):
        if (not isinstance(body, dict) or set(body) != _FIELDS or not isinstance(body.get('purpose'), str) or body['purpose'] not in CATALOG
                or body['sample_id'] != CATALOG[body['purpose']]['id'] or not isinstance(body['expected'], dict)):
            raise ValueError('purpose_test_request_invalid')
        identity, purpose, project = body['id'], body['purpose'], _project(body['project_id'])
        self._identity(identity)
        if project != 'default' and self.records.read('v2_projects', project) is None:
            raise ValueError('purpose_test_project_unavailable')
        validate()
        with self._lock, self.records.begin() as tx:
            old = tx.read(CHECKS, identity)
            if old is not None:
                return False
            if body['expected'] != self.expected(purpose):
                raise ValueError('purpose_test_configuration_changed')
            tx.put(CHECKS, identity, {'status': 'running', 'at': _now(), 'purpose': purpose,
                'project_id': project, 'sample_id': body['sample_id'], 'expected': body['expected']}, expected_revision=0)
            tx.commit()
        return True

    def start(self, body, *, consume, validate):
        if not self.admit(body, validate=validate):
            return self.get(body['id'], validate=validate)
        identity, purpose, project = body['id'], body['purpose'], body['project_id']
        with self._lock:
            self._active.add(identity)
        sample, started = None, monotonic()
        try:
            def current():
                validate()
                if self.expected(purpose) != body['expected']:
                    raise ValueError('purpose_test_configuration_changed')
                row = self.records.read(CHECKS, identity)
                if row is None or row.payload.get('status') != 'running':
                    raise ValueError('purpose_test_changed')
            current()
            sample = RegisteredPurposeSample.create(self.records, self.root, identity, project, purpose,
                consume=consume, validate=current)
            self._save(identity, {'input_proof': sample.descriptor}, validate=current)
            if purpose == 'vision':
                result = self._vision(sample, current)
            elif purpose == 'search':
                if is_private_project(self.records, project) or not egress_allowed(self.records, self.models, project, 'search'):
                    raise ValueError('purpose_test_remote_disabled')
                output = search_once(self.records, self.models, project, SEARCH_QUERY, key='purpose-test:' + identity,
                    validate_inputs=current)
                if output is None:
                    raise ValueError('purpose_test_unconfigured')
                result = {'status': 'complete', 'turn_id': output['turn_id'], 'result_count': len(output['results']),
                    'titles': [redact_secrets(row['title'])[:200] for row in output['results'][:3]]}
            else:
                from ..purpose_test_asr import run_asr_test
                result = run_asr_test(sample, self.container, validate=current)
            self._save(identity, {**result, 'completed_at': _now(), 'duration_ms': round((monotonic() - started) * 1000)}, validate=validate)
        except Exception as error:
            self._save(identity, {'status': 'failed', 'error': safe_error(error), 'completed_at': _now(),
                'duration_ms': round((monotonic() - started) * 1000)}, validate=validate)
        finally:
            try:
                if sample is not None:
                    try:
                        self._dispose(sample)
                    except (ValueError, OSError):
                        result = self.records.read(CHECKS, identity).payload
                        self._save(identity, {'status': 'unknown', 'cleanup': 'failed', 'result_status': result['status'],
                            'result_error': result.get('error'), 'error': 'purpose_test_cleanup_unavailable'}, validate=validate)
            finally:
                with self._lock:
                    self._active.discard(identity)
        return self.get(identity, validate=validate)

    def _vision(self, sample, validate):
        public = self.models.public_for_purpose('vision')
        if public['mode'] == 'remote':
            sample.require_remote(self.models)
            if not public['configured']:
                raise ValueError('purpose_test_unconfigured')
            messages = get('image_read').prepare([image_content(sample.raw)])
            def freeze(kind, **values):
                request = sample.freeze(kind, **values)
                self._save(sample.command_id, {'turn_id': request['turn_id']}, validate=validate)
                return request
            output, _metadata, turn_id = read_images(self.records, self.models,
                project=sample.project_id, key='purpose-test:' + sample.command_id, materials=(), validate=validate,
                freeze_request=freeze, validate_request=sample.validate_request, remote_allowed=egress_allowed,
                messages=messages, load_messages=lambda request: sample.image_messages(request, self.models))
            return {'status': 'complete', 'turn_id': turn_id, 'text': redact_secrets(output.images[0].text),
                'description': redact_secrets(output.images[0].description), 'mode': 'remote'}
        reference = RegisteredImageReference(sample)
        result = local_image_provider(self.root, reference).extract_text(source={'id': sample.command_id,
            'type': 'image', 'metadata': {'image_reference': reference.reference,
                'image_authorization': {'authorization_id': reference.record['id']}}}, job={'required_capability': 'ocr'})
        validate()
        sample.validate()
        return {'status': 'complete', 'text': redact_secrets(result.text), 'mode': 'local',
            'receipt_kind': 'local_ocr', 'usage': usage_view(None), 'cost': cost_totals([None])}


def install_purpose_test_routes(application, *, records, runtime_root, models, workspace):
    if workspace is None or getattr(application.state, 'container', None) is None:
        return
    owner = PurposeTests(records, runtime_root, models, application.state.container)
    application.state.purpose_tests = owner
    router = APIRouter(prefix='/api/v2/settings/purpose-tests')

    def validator(request):
        try:
            expected = _request_owner(request)
        except DeviceError:
            raise HTTPException(403, 'purpose_test_identity_changed') from None
        def validate():
            try:
                if _request_owner(request) != expected:
                    raise DeviceError('device_unauthorized')
            except DeviceError:
                raise ValueError('purpose_test_identity_changed') from None
        return validate

    @asynccontextmanager
    async def intake():
        admission = workspace.intake.admission
        if admission is None:
            yield lambda _size: None
        else:
            async with admission() as check:
                yield check

    @router.get('/samples/{purpose}')
    def samples(purpose: str, request: Request):
        validator(request)()
        if purpose not in CATALOG:
            raise HTTPException(400, 'purpose_test_purpose_invalid')
        try:
            return {**next(sample for sample in sample_catalog() if sample['purpose'] == purpose), 'expected': owner.expected(purpose)}
        except ValueError as error:
            raise HTTPException(400, safe_error(error)) from None

    @router.post('')
    async def start(request: Request):
        validate = validator(request)
        body = await _json(request)
        try:
            async with intake() as consume:
                result = await retain_until_finished(asyncio.to_thread(with_connection_scope(owner.start), body,
                    consume=consume, validate=validate))
        except QuotaError as error:
            raise HTTPException(429, str(error)) from None
        except ValueError as error:
            raise HTTPException(400, safe_error(error)) from None
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @router.get('/{identity}')
    async def status(identity: str, request: Request):
        try:
            result = await retain_until_finished(asyncio.to_thread(with_connection_scope(owner.get), identity, validate=validator(request)))
        except ValueError as error:
            raise HTTPException(400, safe_error(error)) from None
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @router.post('/{identity}/cancel')
    async def cancel(identity: str, request: Request):
        if await _json(request) != {}:
            raise HTTPException(400, 'purpose_test_request_invalid')
        try:
            result = await retain_until_finished(asyncio.to_thread(with_connection_scope(owner.cancel), identity, validate=validator(request)))
        except ValueError as error:
            raise HTTPException(400, safe_error(error)) from None
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @router.post('/{identity}/cleanup')
    async def cleanup(identity: str, request: Request):
        if await _json(request) != {}:
            raise HTTPException(400, 'purpose_test_request_invalid')
        try:
            result = await retain_until_finished(asyncio.to_thread(with_connection_scope(owner.cleanup), identity, validate=validator(request)))
        except (ValueError, OSError) as error:
            raise HTTPException(409, safe_error(error)) from None
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    application.include_router(router)
