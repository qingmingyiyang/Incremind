from datetime import datetime, timezone
from functools import partial
import re
from threading import RLock
from time import monotonic

from fastapi import APIRouter, HTTPException, Request
from pydantic import RootModel
from starlette.concurrency import run_in_threadpool

from core.storage_provider import SQLiteUnitOfWorkConflict
from ..kernel.receipt_projection import kernel_call_groups, aggregate_usage, aggregate_cost
from ..provider_trial_config import ProviderTrialConfiguration
from ..workspace_contracts import _json, _project
from .memory_turn import MemoryTurn
from .turn_requests import freeze_product_turn


TRIALS = 'v2_model_trials'
_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_CODE = re.compile(r'^(?:provider_trial|provider_connection|server_model|model)_[a-z0-9_]{1,120}$')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_error(error):
    code = getattr(error, 'public_code', str(error))
    if code == 'memory_turn_remote_disabled':
        return 'provider_trial_remote_disabled'
    if code == 'user_unauthorized':
        return 'provider_trial_identity_changed'
    return code if isinstance(code, str) and _CODE.fullmatch(code) else 'provider_trial_failed'


class ModelTrials:
    def __init__(self, records, models, registry):
        if registry._connection_owner is not models or registry.records is not records:
            raise ValueError('provider_trial_owner_invalid')
        self.records, self.models, self.registry = records, models, registry
        self._active, self._lock = set(), RLock()

    def get(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('provider_trial_invalid')
        row = self.records.read(TRIALS, identity)
        if row is None:
            return {'id': identity, 'status': 'not_recorded', 'results': []}
        value = dict(row.payload)
        if value['status'] == 'running' and identity not in self._active:
            value['status'] = 'unknown'
        return {'id': identity, 'revision': row.revision, **value}

    def cancel_unaccepted(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('provider_trial_invalid')
        with self._lock, self.records.begin() as tx:
            row = tx.read(TRIALS, identity)
            if row is None:
                tx.put(TRIALS, identity, {'status': 'cancelled', 'results': [], 'at': _now()}, expected_revision=0)
                tx.commit()
        return self.get(identity)

    def start(self, body):
        fields = {'trial_id', 'project_id', 'text', 'targets', 'draft', 'expected_generation_revision', 'expected_mode_revision', 'expected_fast_revision'}
        if (set(body) != fields or not isinstance(body['trial_id'], str) or not _ID.fullmatch(body['trial_id'])
                or not isinstance(body['text'], str) or not 1 <= len(body['text'].strip()) <= 2000
                or not isinstance(body['targets'], list) or not 1 <= len(body['targets']) <= 2
                or any(not isinstance(target, str) or target not in {'main', 'fast', 'draft'} for target in body['targets'])
                or len(set(body['targets'])) != len(body['targets'])
                or 'draft' in body['targets'] and body['targets'] != ['draft']
                or any(type(body[key]) is not int or body[key] < 0 for key in
                    ('expected_generation_revision', 'expected_mode_revision', 'expected_fast_revision'))):
            raise ValueError('provider_trial_invalid')
        identity, project = body['trial_id'], _project(body['project_id'])
        if project != 'default' and self.records.read('v2_projects', project) is None:
            raise ValueError('provider_trial_project_unavailable')
        if ('draft' in body['targets']) != (body['draft'] is not None):
            raise ValueError('provider_trial_invalid')
        views, admitted = [], False
        try:
            with self.models._lock:
                if (self.models.public_for_purpose('generation')['revision'] != body['expected_generation_revision']
                        or self.models.generation_mode()['revision'] != body['expected_mode_revision']
                        or self.models.fast_model()['revision'] != body['expected_fast_revision']):
                    raise ValueError('provider_trial_configuration_changed')
                for target in body['targets']:
                    views.append(ProviderTrialConfiguration(self.models, target=target, registry=self.registry,
                        draft=body['draft'] if target == 'draft' else None))
                with self._lock, self.records.begin() as tx:
                    if tx.read(TRIALS, identity) is not None:
                        raise ValueError('provider_trial_already_recorded')
                    # 凭据只留本次视图内存；持久记录仅保存用户原文、目标和真实结果。
                    tx.put(TRIALS, identity, {'status': 'running', 'project_id': project,
                        'text': body['text'], 'targets': body['targets'], 'results': [], 'at': _now()}, expected_revision=0)
                    tx.commit()
                    self._active.add(identity)
                    admitted = True
            for target, view in zip(body['targets'], views):
                result = self._run_one(identity, project, body['text'], target, view)
                with self.records.begin() as tx:
                    current = tx.read(TRIALS, identity)
                    tx.put(TRIALS, identity, {**current.payload, 'results': [*current.payload['results'], result]}, expected_revision=current.revision)
                    tx.commit()
            with self.records.begin() as tx:
                current = tx.read(TRIALS, identity)
                status = 'complete' if all(result['status'] == 'complete' for result in current.payload['results']) else 'failed'
                tx.put(TRIALS, identity, {**current.payload, 'status': status, 'completed_at': _now()}, expected_revision=current.revision)
                tx.commit()
            return self.get(identity)
        finally:
            if admitted:
                with self._lock:
                    self._active.discard(body['trial_id'])
            for view in views:
                view.dispose()

    def _run_one(self, identity, project, text, target, view):
        started, turn = monotonic(), None
        result = {'target': target, 'model': None, 'status': 'failed', 'text': None,
            'usage': None, 'cost': None, 'duration_ms': None, 'attempts': 0}
        try:
            public = view.public_for_purpose('generation')
            result['model'] = public['model']
            def validate():
                view.public_for_purpose('generation')
                row = self.records.read(TRIALS, identity)
                if row is None or row.payload['status'] != 'running':
                    raise ValueError('provider_trial_changed')
            freezer = partial(freeze_product_turn, text=text)
            turn = MemoryTurn(self.records, view, kind='project.answer', project=project,
                key=f'provider-trial:{identity}:{target}', materials=(), validate=validate, freeze_request=freezer)
            output, _metadata = turn.generate([{'role': 'user', 'content': text}], response_model=RootModel[str],
                max_tokens=256, governed=True)
            result.update(status='complete', text=output.root)
        except Exception as error:
            result['error'] = _safe_error(error)
        finally:
            result['duration_ms'] = round((monotonic() - started) * 1000)
            if turn is not None:
                result['turn_id'] = turn.turn_id
                groups = kernel_call_groups(self.models.root, turn_id=turn.turn_id, project=project,
                    remote_only=False, records=self.records)
                calls = [call for group in groups for call in group['calls']]
                result['usage'], result['cost'] = aggregate_usage(calls), aggregate_cost(calls)
                result['attempts'] = sum(event['type'] == 'model.attempt.dispatched' for event in turn.store.events_after(turn.turn_id))
        return result


def install_model_trial_routes(application, *, records, models, registry):
    domain = ModelTrials(records, models, registry)
    router = APIRouter(prefix='/api/v2/settings/model-trials')

    @router.post('')
    async def trial_start(request: Request):
        body = await _json(request)
        try:
            return await run_in_threadpool(domain.start, body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'provider_trial_configuration_changed') from None
        except ValueError as error:
            code = _safe_error(error)
            raise HTTPException(403 if code == 'server_model_configuration_readonly' else
                409 if code.endswith(('_changed', '_recorded')) else 400, code) from None

    @router.get('/{identity}')
    async def trial_get(identity: str):
        try:
            return domain.get(identity)
        except ValueError:
            raise HTTPException(400, 'provider_trial_invalid') from None

    @router.post('/{identity}/cancel')
    async def trial_cancel(identity: str, request: Request):
        if await _json(request) != {}:
            raise HTTPException(400, 'provider_trial_invalid')
        try:
            return domain.cancel_unaccepted(identity)
        except ValueError:
            raise HTTPException(400, 'provider_trial_invalid') from None

    application.include_router(router)
