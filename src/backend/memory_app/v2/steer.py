from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.ai_kernel.event_store import RunLeaseRevoked
from core.ai_kernel.sqlite_store import SQLiteAITurnStore, TurnStateConflict
from core.ai_kernel.product_snapshot import restore_admission_snapshot
from core.storage_provider.observability import observe_connection
from ..workbench_identity_binding import KIND, execution_owner
from ..workspace_contracts import _json
from .policies import get, version
from .turn_frames import FRAMES, STREAMS, append_stream_event, frame_text


_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


def visible_snapshot(records, binding, request, run_lease, cursor, *, limit):
    if cursor is None:
        return {'answer_tail': '', 'visible_sequence': None}
    if type(cursor) is not int or cursor < 1:
        raise ValueError('steer_visible_cursor_invalid')
    stream, frames = records.read(STREAMS, binding['target_turn_id']), records.read(FRAMES, binding['target_turn_id'])
    if (stream is None or frames is None or stream.payload.get('project_id') != binding['project_id']
            or stream.payload.get('thread_id') != binding['thread_id']
            or stream.payload.get('binding') != {'kernel_turn_id': request['turn_id'], 'request': request}
            or frames.payload.get('project_id') != binding['project_id']
            or frames.payload.get('thread_id') != binding['thread_id'] or frame_text(frames.payload) is None):
        raise ValueError('steer_visible_snapshot_unverifiable')
    values = stream.payload.get('events')
    if not isinstance(values, list) or cursor > stream.payload.get('sequence', 0):
        raise ValueError('steer_visible_cursor_unavailable')
    text, found = '', False
    resets = [event.get('sequence') for event in values if event.get('kind') == 'reset']
    start = resets[-1] if resets else 1
    if resets and cursor <= start:
        raise ValueError('steer_visible_cursor_unavailable')
    for expected, event in enumerate(values, 1):
        if event.get('sequence') != expected:
            raise ValueError('steer_visible_sequence_gap')
        if event['sequence'] > cursor:
            break
        if event['sequence'] < start:
            continue
        if event.get('kind') == 'reset':
            if event.get('source') not in {'frame_prefix', 'steer'}:
                raise ValueError('steer_visible_reset_unverifiable')
            prefix = frames.payload.get('text_prefix')
            if not isinstance(prefix, str):
                raise ValueError('steer_visible_reset_unverifiable')
            text = prefix[-limit:]
        if event.get('kind') == 'delta':
            if event.get('run_generation') != run_lease.generation:
                raise ValueError('steer_visible_generation_unverifiable')
            sequence, offset, length = (event.get(key) for key in ('frame_sequence', 'offset', 'length'))
            if (any(type(value) is not int for value in (sequence, offset, length)) or sequence < 1
                    or sequence > len(frames.payload['frames']) or offset < 0 or length < 1):
                raise ValueError('steer_visible_frame_unverifiable')
            source = frames.payload['frames'][sequence - 1]['text']
            if offset + length > len(source):
                raise ValueError('steer_visible_frame_unverifiable')
            text = (text + source[offset:offset + length])[-limit:]
        if event['sequence'] == cursor:
            if event.get('kind') != 'delta':
                raise ValueError('steer_visible_cursor_requires_answer_delta')
            found = True
    if not found:
        raise ValueError('steer_visible_cursor_unavailable')
    return {'answer_tail': text, 'visible_sequence': cursor}


@dataclass
class _Owner:
    request: dict
    binding: dict
    lease: object
    interruption: object = None
    delivery: object = None
    input_ref: str | None = None
    input_kind: str | None = None


class SteerService:
    def __init__(self, *, application, records, runtime_root):
        self.application, self.records, self.runtime = application, records, None
        self.native_path = (Path(runtime_root) / '.rebuild-data/ai-turns.sqlite3').resolve()
        self.policy_version = version('steer')
        self.policy, self._lock, self._owners = get('steer', version=self.policy_version), RLock(), {}

    @property
    def store(self):
        if self.runtime is None:
            raise ValueError('steer_runtime_not_initialized')
        return self.runtime._state

    def bind_runtime(self, runtime):
        if not isinstance(runtime._state, SQLiteAITurnStore) or Path(runtime._state._path).resolve() != self.native_path:
            raise ValueError('steer_native_owner_changed')
        with self._lock:
            if self.runtime is not None and self.runtime is not runtime:
                raise ValueError('steer_runtime_owner_changed')
            self.runtime = runtime
            runtime.product_steers = self
        continuations = getattr(runtime, 'task_continuations', None)
        if continuations is not None and getattr(runtime, 'product_task_steers', None) is None:
            from .task_steer import TaskSteerRuntime
            owner = TaskSteerRuntime(self, runtime, continuations.planner)
            runtime.product_task_steers = owner
            continuations.planner.task_steers = owner
            continuations.planner.composition.coordinator.product_task_steers = owner

    def _actor(self):
        return execution_owner(self.application, self.records, self.application.state.recognition_documents)

    def _rows(self, public_turn_id):
        if not self.native_path.is_file():
            return ()
        connection = observe_connection(sqlite3.connect(self.native_path.as_uri() + '?mode=ro', uri=True, timeout=5))
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_turn_steers'").fetchone():
                return ()
            rows = connection.execute('SELECT fact.payload_json,steer.payload_ref,steer.state,steer.input_ref,steer.decision_ref,steer.revision,steer.event_id '
                'FROM ai_turn_steers steer JOIN ai_turn_immutable_payloads fact ON fact.payload_ref=steer.payload_ref '
                'WHERE steer.public_turn_id=? ORDER BY fact.rowid', (public_turn_id,)).fetchall()
            verified, result = {}, []
            for row in rows:
                fact = json.loads(row[0])
                turn_id = fact['turn_id']
                if turn_id not in verified:
                    native = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (turn_id,)).fetchone()
                    immutable = {kind: (reference, json.loads(payload)) for kind, reference, payload in connection.execute(
                        'SELECT kind,payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?', (turn_id,))}
                    if native is None:
                        raise ValueError('steer_native_request_unavailable')
                    restore_admission_snapshot(json.loads(native[0]), immutable)
                    verified[turn_id] = immutable[KIND]
                reference, binding = verified[turn_id]
                source = connection.execute('SELECT event_json FROM ai_turn_events WHERE turn_id=? AND event_id=?', (turn_id, row[6])).fetchone()
                event = json.loads(source[0]) if source is not None else None
                if (reference != fact.get('binding_ref') or event is None or event['type'] != 'turn.steer.received'
                        or row[1] not in event['data']['evidence_refs']
                        or any(fact.get(key) != binding.get(key) for key in ('public_turn_id', 'target_turn_id', 'project_id',
                            'scene', 'thread_id', 'owner_user_id', 'namespace_id', 'origin_kind', 'part', 'part_turn_ids'))):
                    raise ValueError('steer_native_binding_changed')
                lease = connection.execute('SELECT owner_id,generation,status,stale_after FROM ai_turn_run_leases WHERE turn_id=?', (turn_id,)).fetchone()
                last = connection.execute('SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence DESC LIMIT 1', (turn_id,)).fetchone()
                active = (lease is not None and lease[0] == fact['lease']['owner_id'] and lease[1] == fact['lease']['generation']
                    and lease[2] == 'active' and datetime.fromisoformat(lease[3]) > datetime.now(timezone.utc)
                    and last is not None and json.loads(last[0])['type'] not in {'turn.completed', 'turn.failed', 'turn.cancelled'})
                applied_step = None
                recovered_unapplied = False
                if row[4] is not None:
                    decision = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=?', (row[4],)).fetchone()
                    if decision is None:
                        raise ValueError('steer_decision_unavailable')
                    recovered_unapplied = json.loads(decision[0]).get('reason') == 'run_owner_recovered'
                if row[3] is not None:
                    applied = [json.loads(raw[0]) for raw in connection.execute(
                        "SELECT event_json FROM ai_turn_events WHERE turn_id=? AND event_json LIKE '%turn.steer.applied%' ORDER BY sequence", (turn_id,))]
                    matches = [event for event in applied if event['type'] == 'turn.steer.applied'
                        and row[3] in event['data'].get('evidence_refs', [])]
                    if len(matches) != 1:
                        raise ValueError('steer_applied_event_unavailable')
                    applied_step = matches[0]['correlation']['step_id']
                result.append({'payload': fact, 'payload_ref': row[1], 'state': row[2], 'input_ref': row[3],
                    'decision_ref': row[4], 'revision': row[5], 'owner_active': active,
                    'recovered_unapplied': recovered_unapplied,
                    'applied_step': applied_step,
                    'public_project_id': binding['parent']['project_id'] if binding.get('parent') is not None else binding['project_id']})
            return tuple(result)
        finally:
            connection.close()

    def _current_owner(self, owner):
        try:
            lease = self.store.assert_active_run_lease(owner.lease)
        except RunLeaseRevoked:
            return False
        return (lease is not None and lease.stale_after > datetime.now(timezone.utc)
            and self.runtime.receipt_for(owner.request['turn_id']).status not in {'completed', 'failed', 'cancelled'})

    def _targets(self, public_id, actor):
        with self._lock:
            owners = list(self._owners.values())
        return sorted((owner for owner in owners if owner.binding['public_turn_id'] == public_id
            and owner.request.get('policy_versions', {}).get('steer', self.policy_version) == '@2'
            and all(owner.binding.get(key) == value for key, value in actor.items()) and self._current_owner(owner)),
            key=lambda owner: (owner.request['desired_outcome'] != 'project.task', owner.binding['part'] or 0, owner.request['turn_id']))

    def _scope(self, public_id, project_id, actor, rows, owners):
        public = self.records.read('v2_turns', public_id)
        if public is not None:
            if public.payload.get('project_id') != project_id or public.payload.get('intent') not in {'ask', 'do', 'multi'}:
                raise HTTPException(404, 'workbench_not_found')
            return
        projects = {row['public_project_id'] for row in rows if all(row['payload'].get(key) == value for key, value in actor.items())}
        projects.update(owner.binding['parent']['project_id'] if owner.binding.get('parent') is not None else owner.binding['project_id'] for owner in owners)
        if projects != {project_id}:
            raise HTTPException(404, 'workbench_not_found')

    @staticmethod
    def _entry(row):
        fact = row['payload']
        state = ('queued' if row['owner_active'] else 'unapplied') if row['state'] == 'queued' else (
            'unapplied' if row['state'] == 'unapplied' or row.get('recovered_unapplied')
                or row['state'] == 'injected' and not row['owner_active'] else 'applied')
        return {'id': fact['id'], 'text': fact['text'], 'at': fact['created_at'], 'state': state,
            'applied_to': {'part': fact['part'], 'step': row.get('applied_step') if state == 'applied' else None}}

    def projection(self, public_id, project_id):
        actor, rows = self._actor(), self._rows(public_id)
        if any(any(row['payload'].get(key) != value for key, value in actor.items()) for row in rows):
            raise HTTPException(403, 'steer_owner_changed')
        targets = self._targets(public_id, actor)
        self._scope(public_id, project_id, actor, rows, targets)
        target = targets[0] if targets and len(rows) < self.policy.maximum_count else None
        return {'steers': [self._entry(row) for row in rows],
            'steer_target': {'part': target.binding['part'], 'intent': 'do' if target.request['desired_outcome'] == 'project.task' else 'ask'} if target else None,
            'steer_remaining': max(0, self.policy.maximum_count - len(rows))}

    def submission(self, public_id, project_id, submission_key, *, text=None):
        self.projection(public_id, project_id)
        for row in self._rows(public_id):
            if row['payload']['submission_key'] == submission_key:
                if text is not None and row['payload']['text'] != text:
                    raise HTTPException(409, 'steer_submission_conflict')
                entry = self._entry(row)
                return {'steer_id': entry['id'], 'state': entry['state'], 'steer': entry}
        raise HTTPException(404, 'steer_not_found')

    @contextmanager
    def run_scope(self, turn_id):
        lease, native = self.runtime._run_lease_context.get(), self.store.get_request(turn_id)
        capsule = self.store.get_immutable_payload(turn_id, KIND)
        if (capsule is None or native['desired_outcome'] not in {'project.answer', 'project.task'}
                or native['desired_outcome'] == 'project.task' and native.get('agent_binding', {}).get('role') != 'main'):
            yield
            return
        if lease is None or lease.turn_id != turn_id:
            raise ValueError('steer_run_owner_unavailable')
        self.store.restore_admission(native)
        self.store.retire_unconsumed_steers(turn_id, run_lease=lease)
        owner = _Owner(deepcopy(native), deepcopy(capsule[1]), lease)
        with self._lock:
            if turn_id in self._owners:
                raise ValueError('steer_run_already_registered')
            self._owners[turn_id] = owner
        try:
            yield
        finally:
            with self._lock:
                if self._owners.get(turn_id) is owner:
                    del self._owners[turn_id]

    def registered(self, turn_id):
        with self._lock:
            return turn_id in self._owners

    def bind_delivery(self, turn_id, callback):
        with self._lock:
            owner = self._owners.get(turn_id)
            if owner is None or not callable(callback):
                raise ValueError('steer_delivery_owner_unavailable')
            owner.delivery = callback

    def bind_model_context(self, turn_id, input_ref, *, input_kind=None):
        with self._lock:
            owner = self._owners.get(turn_id)
            if owner is None or not isinstance(input_ref, str):
                raise ValueError('steer_context_owner_unavailable')
            owner.input_ref = input_ref
            owner.input_kind = input_kind

    @contextmanager
    def attempt_scope(self, turn_id, interruption):
        with self._lock:
            owner = self._owners.get(turn_id)
            if owner is None or owner.interruption is not None:
                raise ValueError('steer_attempt_owner_unavailable')
            owner.interruption = interruption
            pending = bool(self.pending(turn_id))
        if pending:
            interruption.request()
        try:
            yield
        finally:
            with self._lock:
                if self._owners.get(turn_id) is owner and owner.interruption is interruption:
                    owner.interruption = None

    def pending(self, turn_id):
        return self._selected(turn_id, {'queued'})

    def adopted(self, turn_id):
        return self._selected(turn_id, {'injected', 'released', 'amended'})

    def _selected(self, turn_id, states):
        with self._lock:
            owner = self._owners.get(turn_id)
        if owner is None:
            return ()
        return tuple(row for row in self.store.workbench_steers(owner.binding['public_turn_id'])
            if row['payload']['turn_id'] == turn_id and row['state'] in states
            and row['payload']['lease'] == {'owner_id': owner.lease.owner_id, 'generation': owner.lease.generation})

    def _publish(self, owner, rows):
        values = [{**self._entry(row), 'part': owner.binding['part']} for row in rows]
        with self.records.begin() as tx:
            head = tx.read(STREAMS, owner.binding['target_turn_id'])
            if head is None or head.payload.get('terminal') is not None:
                return
            if head.payload.get('binding') != {'kernel_turn_id': owner.request['turn_id'], 'request': owner.request}:
                raise ValueError('steer_stream_binding_changed')
            payload = dict(head.payload)
            events = [append_stream_event(payload, 'steer', **value) for value in values]
            tx.put(STREAMS, owner.binding['target_turn_id'], payload, expected_revision=head.revision)
            self.store.assert_active_run_lease(owner.lease)
            tx.commit()
        if owner.delivery is not None:
            for event, value in zip(events, values):
                owner.delivery(event['sequence'], 'steer', value)

    def freeze_input(self, turn_id, *, kind, payload, steer_ids):
        with self._lock:
            owner = self._owners.get(turn_id)
        if owner is None:
            raise ValueError('steer_running_owner_unavailable')
        with self.runtime._event_commit(turn_id):
            pending = {row['payload']['id']: row for row in self.pending(turn_id)}
            if not steer_ids or len(set(steer_ids)) != len(steer_ids) or any(identity not in pending for identity in steer_ids):
                raise ValueError('steer_input_selection_changed')
            rows = tuple(pending[identity] for identity in steer_ids)
            frozen = {**deepcopy(payload), 'turn_id': turn_id, 'project_id': owner.binding['project_id'],
                'run_generation': owner.lease.generation, 'run_owner_id': owner.lease.owner_id,
                'steer_refs': [row['payload_ref'] for row in rows]}
            boundaries = [event for event in self.store.events_after(turn_id)
                if event['correlation'].get('step_id') is not None]
            step = boundaries[-1]['correlation']['step_id'] if boundaries else None
            event = self.runtime._new_event(turn_id, 'turn.steer.applied', 'running', '已采用插话输入', step_id=step)
            receipt = self.store.apply_steer_input(event, expected_sequence=event['sequence'] - 1,
                run_lease=owner.lease, steer_ids=steer_ids, kind=kind, payload=frozen)
        selected = [row for row in self._rows(owner.binding['public_turn_id']) if row['payload']['id'] in steer_ids]
        self._publish(owner, selected)
        return {'input_ref': receipt.immutable_payload_ref, 'payload': frozen, 'steers': rows}

    def interruption_proof(self, turn_id, handle, error):
        from backend.shared.llm.model_transport import ModelSteerInterrupt, is_provider_closed_witness
        from core.ai_kernel.contracts import validate_model_wire_attempt_receipt
        with self._lock:
            owner = self._owners.get(turn_id)
        control = getattr(handle, 'steer_control', None)
        if (owner is None or not isinstance(error, ModelSteerInterrupt) or not is_provider_closed_witness(error.close_witness)
                or handle.terminal != 'failed' or error.identity[0] != turn_id or error.identity[2] != handle.model_request_id
                or control is None or error.identity != control.identity or error.attempt_generation != control.generation
                or error.attempt_generation < 1 or control.registration is not None):
            raise ValueError('steer_provider_close_unproven')
        events = [event for event in self.store.events_after(turn_id) if event['correlation']['model_request_id'] == handle.model_request_id]
        dispatch = [event for event in events if event['type'] == 'model.attempt.dispatched']
        terminals = [event['data']['receipt_ref'] for event in events if event['type'] == 'model.attempt.terminal']
        if not dispatch or not any(event['type'] == 'model.failed' for event in events) or len(dispatch) != len(terminals):
            raise ValueError('steer_wire_settlement_incomplete')
        for reference in terminals:
            receipt = validate_model_wire_attempt_receipt(self.store.get(reference))
            if receipt['turn_id'] != turn_id or receipt['model_request_id'] != handle.model_request_id:
                raise ValueError('steer_wire_receipt_owner_changed')
        fact = {'turn_id': turn_id, 'run_owner_id': owner.lease.owner_id, 'run_generation': owner.lease.generation,
            'model_request_id': handle.model_request_id, 'wire_receipt_refs': terminals, 'provider_closed': True,
            'transport_generation': error.attempt_generation}
        return self.store.get_or_create_immutable_payload(turn_id, 'turn-steer-interruption-' + handle.model_request_id, fact)

    def closed_attempt_receipts(self, turn_id):
        from ..answer_effective import closed_steer_attempt_receipts
        return closed_steer_attempt_receipts(self.store, turn_id)

    def accept(self, public_id, project_id, submission_key, text):
        if not isinstance(submission_key, str) or _KEY.fullmatch(submission_key) is None:
            raise HTTPException(400, 'invalid_steer_submission')
        if not isinstance(text, str) or not text.strip() or len(text) > self.policy.maximum_characters:
            raise HTTPException(400, 'invalid_steer_text')
        try:
            return self.submission(public_id, project_id, submission_key, text=text)
        except HTTPException as error:
            if error.status_code != 404 or error.detail != 'steer_not_found':
                raise
        rows, actor = self._rows(public_id), self._actor()
        if len(rows) >= self.policy.maximum_count:
            raise HTTPException(409, 'steer_limit')
        targets = self._targets(public_id, actor)
        if not targets:
            raise HTTPException(409, 'turn_finished')
        owner = targets[0]
        if any(ref['uri'].startswith('crp://material-origin/') for ref in owner.request['input']['refs']):
            from backend.recognition import RecognitionConflict
            from ..kernel.agent_coordinator import AgentCoordinatorError
            from .turn_logical_scope import TurnLogicalScope
            stops = self.runtime.product_stops
            factory = getattr(self.application.state, 'shared_query_factory', None)
            if stops is None or factory is None:
                raise HTTPException(409, 'source_changed_retry')
            try:
                if stops.cancellation_requested(owner.request['turn_id']):
                    raise HTTPException(409, 'turn_finished')
                root = owner.request
                if root.get('agent_binding', {}).get('parent_run_id'):
                    if stops.planner is None:
                        raise RecognitionConflict('steer_source_root_unavailable')
                    root = stops.planner.profile_request(root)
                if TurnLogicalScope(self.application).request(root) != 'shared':
                    raise RecognitionConflict('steer_source_root_changed')
                factory.dispatch(self.application, root)
            except (AgentCoordinatorError, ValueError):
                raise HTTPException(409, 'source_changed_retry') from None
        from .outcome_source_preparation import read_model_context
        context = read_model_context(self.store, owner.request, input_ref=owner.input_ref, input_kind=owner.input_kind)
        events = list(self.store.events_after(owner.request['turn_id']))
        steps = [event for event in events if event['type'] in {'step.started', 'step.completed', 'tool.outcome.recorded'}]
        snapshot = {'step': '回答中' if owner.request['desired_outcome'] == 'project.answer' else (
            steps[-1]['data']['summary'] if steps else None), 'step_refs': [event['event_id'] for event in steps[-10:]],
            'context': context, 'answer_tail': '', 'visible_sequence': None}
        stream = self.records.read(STREAMS, owner.binding['target_turn_id'])
        if owner.request['desired_outcome'] == 'project.answer' and stream is not None:
            cursors = [event['sequence'] for event in stream.payload['events'] if event['kind'] == 'delta'
                and event.get('run_generation') == owner.lease.generation]
            resets = [event['sequence'] for event in stream.payload['events'] if event['kind'] == 'reset']
            cursor = cursors[-1] if cursors and (not resets or cursors[-1] > resets[-1]) else None
            snapshot.update(visible_snapshot(self.records, owner.binding, owner.request, owner.lease, cursor,
                limit=self.policy.snapshot_characters))
        try:
            with self.runtime._event_commit(owner.request['turn_id']):
                event = self.runtime._new_event(owner.request['turn_id'], 'turn.steer.received', 'running', '已收到插话', actor='user')
                self.store.accept_steer_bundle(event, expected_sequence=event['sequence'] - 1, run_lease=owner.lease,
                    submission_key=submission_key, text=text, snapshot=snapshot, visible_sequence=snapshot['visible_sequence'],
                    maximum_count=self.policy.maximum_count, policy_version=self.policy_version)
        except TurnStateConflict as error:
            # 同一公共轮次的并发接纳仍以原生唯一编号为准，回读同一份实际结果。
            try:
                return self.submission(public_id, project_id, submission_key, text=text)
            except HTTPException as missing:
                if missing.status_code != 404 or missing.detail != 'steer_not_found':
                    raise
                if len(self._rows(public_id)) >= self.policy.maximum_count:
                    raise HTTPException(409, 'steer_limit') from None
                if not self._current_owner(owner):
                    raise HTTPException(409, 'turn_finished') from None
                raise error
        with self._lock:
            interruption = owner.interruption if self._owners.get(owner.request['turn_id']) is owner else None
        if interruption is not None:
            interruption.request()
        selected = [row for row in self._rows(public_id) if row['payload']['submission_key'] == submission_key]
        self._publish(owner, selected)
        return self.submission(public_id, project_id, submission_key, text=text)


def install_steer_routes(application, *, service):
    router = APIRouter(prefix='/api/v2/workbench')

    @router.get('/turns/{turn_id}/steer')
    def read(turn_id: str, project_id: str, request: Request):
        key = request.headers.get('Idempotency-Key')
        if key is not None and _KEY.fullmatch(key) is None:
            raise HTTPException(400, 'invalid_steer_submission')
        result = service.projection(turn_id, project_id) if key is None else service.submission(turn_id, project_id, key)
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @router.post('/turns/{turn_id}/steer')
    async def accept(turn_id: str, request: Request):
        body = await _json(request)
        if not isinstance(body, dict) or set(body) != {'project_id', 'text'} or not isinstance(body['project_id'], str):
            raise HTTPException(400, 'invalid_steer_submission')
        try:
            result = await run_in_threadpool(service.accept, turn_id, body['project_id'], request.headers.get('Idempotency-Key'), body['text'])
        except (TurnStateConflict, RunLeaseRevoked) as error:
            raise HTTPException(409, str(error)) from error
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    application.include_router(router)
