from contextvars import copy_context
from copy import deepcopy
from functools import wraps
import json
import sqlite3
from threading import RLock, Timer

from .external_events import _TOKENS
from .external_runner import ARCHIVE, _archive_fields
from .turn_frames import STREAMS, append_stream_event


COLLECTION = 'v2_external_progress'
EXECUTORS = frozenset({'claude-code', 'codex', 'deepseek-harness'})
_STAGES = frozenset({'turn', 'error', 'reasoning', 'todo_list', 'file_change', 'message', 'tool_loop'})
_TOOLS = frozenset({'command_execution', 'web_search', 'mcp_tool_call', 'file_read', 'file_write', 'agent', 'tool_use'})
_TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'timed_out', 'output_limit'})
_LIMIT = 128


class ExternalProgressWriteError(ValueError):
    pass


def projection_storage(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (sqlite3.Error, OSError):
            raise ExternalProgressWriteError('external_progress_storage_unavailable') from None
    return guarded


def put_projection(tx, collection, identity, payload, *, expected_revision):
    try:
        return tx.put(collection, identity, payload, expected_revision=expected_revision)
    except (sqlite3.Error, OSError):
        raise ExternalProgressWriteError('external_progress_storage_unavailable') from None


def commit_projection(tx):
    try:
        tx.commit()
    except (sqlite3.Error, OSError):
        raise ExternalProgressWriteError('external_progress_storage_unavailable') from None


def normalized_event(event):
    kind = event.get('kind') if isinstance(event, dict) else None
    if kind == 'message':
        return None
    valid = (kind == 'started' and set(event) == {'kind'}
        or kind == 'step' and set(event) == {'kind', 'stage'} and event['stage'] in _STAGES
        or kind == 'tool' and set(event) == {'kind', 'name'} and event['name'] in _TOOLS
        or kind == 'finished' and set(event) == {'kind', 'status'} and event['status'] in _TERMINAL)
    if not valid:
        raise ValueError('external_progress_event_invalid')
    return dict(event)


def external_usage(executor, reported=None, observed=None):
    if executor not in EXECUTORS:
        raise ValueError('external_progress_executor_invalid')
    values = reported if reported is not None else observed
    tokens = {key: value for key, value in (values or {}).items()
        if key in _TOKENS and type(value) is int and 0 <= value <= 2 ** 53 - 1}
    complete = reported is not None and {'input_tokens', 'output_tokens'} <= tokens.keys()
    return {'source': executor, 'status': 'reported' if complete else 'partial'
        if tokens else 'unknown', 'tokens': tokens, 'amount': None}


def validate_progress(value, *, executor=None):
    if (not isinstance(value, dict)
            or set(value) != {'version', 'sequence', 'events', 'total', 'truncated', 'usage', 'terminal'}
            or value['version'] != 'external-progress@1' or value['total'] is not None
            or type(value['sequence']) is not int or not 0 <= value['sequence'] <= 2 ** 53 - 1
            or type(value['truncated']) is not int or value['truncated'] < 0
            or not isinstance(value['events'], list) or len(value['events']) > _LIMIT
            or value['truncated'] + len(value['events']) != value['sequence']
            or value['terminal'] is not None and value['terminal'] not in _TERMINAL):
        raise ValueError('external_progress_invalid')
    for number, event in enumerate(value['events'], value['truncated'] + 1):
        if not isinstance(event, dict) or event.get('sequence') != number:
            raise ValueError('external_progress_invalid')
        if normalized_event({key: item for key, item in event.items() if key != 'sequence'}) is None:
            raise ValueError('external_progress_invalid')
    usage = value['usage']
    if (not isinstance(usage, dict) or set(usage) != {'source', 'status', 'tokens', 'amount'}
            or usage['source'] not in EXECUTORS or executor is not None and usage['source'] != executor
            or usage['status'] not in {'reported', 'partial', 'unknown'} or usage['amount'] is not None
            or not isinstance(usage['tokens'], dict) or not usage['tokens'].keys() <= _TOKENS
            or any(type(counter) is not int or not 0 <= counter <= 2 ** 53 - 1 for counter in usage['tokens'].values())
            or usage['status'] == 'reported' and not {'input_tokens', 'output_tokens'} <= usage['tokens'].keys()
            or (usage['status'] == 'unknown') != (not usage['tokens'])):
        raise ValueError('external_progress_usage_invalid')
    return deepcopy(value)


def progress_frame(event, *, turn_id, project, executor):
    if (set(event) != {'sequence', 'kind', 'turn_id', 'project_id', 'kernel_turn_id', 'executor', 'progress'}
            or event['kind'] != 'task_progress' or event['turn_id'] != turn_id
            or event['project_id'] != project or event['executor'] != executor
            or not isinstance(event['kernel_turn_id'], str)):
        raise ValueError('external_progress_frame_invalid')
    return {key: deepcopy(event[key]) for key in ('turn_id', 'project_id', 'kernel_turn_id', 'executor')} | {
        'progress': validate_progress(event['progress'], executor=executor)}


def bound_product(reader, *, product_id, product_owner, request, executor, turn_id):
    execution, product = reader.read('v2_task_executions', product_id), reader.read('v2_turns', product_id)
    if (execution is None or product is None or not product_owner
            or execution.payload.get('owner') != product_owner or not execution.payload.get('started')
            or execution.payload.get('request') != request or execution.payload.get('executor') != executor
            or execution.payload.get('project_id') != request['scope']['project_id']
            or execution.payload.get('task_text') != product.payload.get('user_text')
            or json.loads(request['input']['text']).get('task') != product.payload.get('user_text')
            or product.payload.get('project_id') != request['scope']['project_id']
            or product.payload.get('intent') != 'do'
            or product.payload['receipt']['do'].get('kernel_turn_id') != turn_id
            or product.payload['receipt']['do'].get('executor') != executor
            or product.payload['receipt']['do'].get('state') != 'running'):
        raise ValueError('external_progress_owner_changed')
    return product


class ExternalProgress:
    @projection_storage
    def __init__(self, records, turns, lease, *, turn_id, owner_id, external_context=None):
        self.records, self.turns, self.lease = records, turns, lease
        self.turn_id, self.owner_id = turn_id, owner_id
        self.external_context = external_context
        self.request = turns.get_request(turn_id)
        self.archive = turns.get_immutable_payload(turn_id, ARCHIVE)
        session = self.request.get('session_id', '')
        self.product_id = session[8:] if session.startswith('session-') else ''
        self.executor = lease.plan.executor
        row = records.read('v2_task_executions', self.product_id)
        self.product_owner = row.payload.get('owner') if row is not None else None
        self.active = row is not None
        self.context = copy_context()
        self.lock, self.timer = RLock(), None
        self.pending, self.error, self.closed = [], None, False
        self.disabled = False
        self.pending_count = 0
        self.physical_owner = None
        if self.active:
            self._frozen()
            with records.begin() as tx:
                self._binding(tx)

    @projection_storage
    def _frozen(self):
        archive = self.turns.get_immutable_payload(self.turn_id, ARCHIVE)
        if (archive != self.archive or archive is None or not _archive_fields(archive[1])
                or self.external_context is None or self.external_context.records is not self.records
                or self.external_context.owner_id != self.owner_id or archive[1]['owner_id'] != self.owner_id
                or archive[1]['turn_id'] != self.turn_id or archive[1]['request'] != self.request
                or self.turns.get_request(self.turn_id) != self.request
                or archive[1]['launch']['executor'] != self.executor
                or archive[1]['delivery']['owner_id'] != self.owner_id
                or archive[1]['delivery']['project_id'] != self.request['scope']['project_id']
                or self.external_context.qualified_delivery(archive[1]['delivery']['turn_id']) != archive[1]['delivery']):
            raise ValueError('external_progress_binding_invalid')

    def _binding(self, tx):
        return bound_product(tx, product_id=self.product_id, product_owner=self.product_owner,
            request=self.request, executor=self.executor, turn_id=self.turn_id)

    def receive(self, event):
        if not self.active or self.disabled:
            return
        value = normalized_event(event)
        if value is None:
            return
        with self.lock:
            if self.error is not None:
                raise self.error
            if self.closed:
                raise ValueError('external_progress_closed')
            self.context.copy().run(self._check_owner)
            if value['kind'] != 'finished' and self.lease._process_owner is None:
                raise ValueError('external_progress_owner_missing')
            self.pending.append(value)
            self.pending = self.pending[-_LIMIT:]
            self.pending_count += 1
            if self.timer is None:
                self.timer = Timer(0.15, self._scheduled)
                self.timer.daemon = True
                self.timer.start()

    def _check_owner(self):
        # 运行中的配置目录可以由CLI自然写入；只复验原签发身份及当前凭据，不重跑启动前空目录条件。
        self.lease._validate_native_current()
        owner = self.lease._process_owner
        if self.physical_owner is None:
            self.physical_owner = owner
        elif owner is not self.physical_owner:
            raise ValueError('external_progress_owner_changed')
        self._check_binding()

    @projection_storage
    def _check_binding(self):
        self._frozen()
        with self.records.begin() as tx:
            self._binding(tx)

    def _scheduled(self):
        with self.lock:
            self.timer = None
            if self.closed or self.disabled:
                return
            try:
                self.context.copy().run(self._flush)
            except ExternalProgressWriteError:
                self.disable()
            except Exception as error:
                self.error = error

    def _flush(self, *, reported=None, observed=None, terminal=None, final=False, usage_update=False):
        self._check_owner()
        self._write(reported=reported, observed=observed, terminal=terminal,
            final=final, usage_update=usage_update)

    @projection_storage
    def _write(self, *, reported=None, observed=None, terminal=None, final=False, usage_update=False):
        binding = {'turn_id': self.turn_id, 'owner_id': self.owner_id, 'executor': self.executor,
            'product_id': self.product_id, 'product_owner': self.product_owner, 'archive_ref': self.archive[0]}
        with self.records.begin() as tx:
            product = self._binding(tx)
            saved = tx.read(COLLECTION, self.turn_id)
            if saved is not None and saved.payload.get('binding') != binding:
                raise ValueError('external_progress_binding_invalid')
            value = (validate_progress(saved.payload['progress'], executor=self.executor) if saved else {
                'version': 'external-progress@1', 'sequence': 0, 'events': [], 'total': None,
                'truncated': 0, 'usage': external_usage(self.executor), 'terminal': None})
            first = value['sequence'] + self.pending_count - len(self.pending) + 1
            events = value['events'] + [{'sequence': first + index, **event} for index, event in enumerate(self.pending)]
            value['sequence'] += self.pending_count
            value['events'] = events[-_LIMIT:]
            value['truncated'] = value['sequence'] - len(value['events'])
            if final or usage_update:
                value['usage'] = external_usage(self.executor, reported, observed)
            if final:
                value['terminal'] = terminal
            value = validate_progress(value, executor=self.executor)
            put_projection(tx, COLLECTION, self.turn_id, {'binding': binding, 'progress': value},
                expected_revision=saved.revision if saved else 0)
            receipt = {**product.payload['receipt']['do'], 'external_progress': value,
                'external_usage': value['usage'], 'progress': {'done': None, 'total': None}}
            put_projection(tx, 'v2_turns', self.product_id, {**product.payload, 'receipt': {'do': receipt}}, expected_revision=product.revision)
            head = tx.read(STREAMS, self.product_id)
            metadata = {'id': product.object_id, **{key: product.payload[key]
                for key in ('thread_id', 'intent', 'user_text', 'created_at')}}
            sealed = {'kernel_turn_id': self.turn_id, 'request': self.request}
            payload = dict(head.payload) if head else {'project_id': self.request['scope']['project_id'],
                'thread_id': product.payload['thread_id'], 'turn': metadata, 'binding': sealed,
                'sequence': 0, 'events': [], 'terminal': None}
            if (payload['binding'] != sealed or payload['turn'] != metadata or payload['terminal'] is not None
                    or payload['project_id'] != self.request['scope']['project_id']):
                raise ValueError('external_progress_stream_changed')
            if head is None:
                append_stream_event(payload, 'started')
            append_stream_event(payload, 'task_progress', turn_id=self.product_id,
                project_id=self.request['scope']['project_id'], kernel_turn_id=self.turn_id,
                executor=self.executor, progress=value)
            put_projection(tx, STREAMS, self.product_id, payload, expected_revision=head.revision if head else 0)
            commit_projection(tx)
        self.pending, self.pending_count = [], 0

    def observe_usage(self, result):
        if not self.active or self.disabled:
            return
        with self.lock:
            if self.error is not None:
                raise self.error
            if self.closed:
                raise ValueError('external_progress_closed')
            self.context.copy().run(self._flush, reported=result.usage,
                observed=result.observed_usage, usage_update=True)

    def finish(self, result):
        if not self.active or self.disabled:
            return
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            if self.error is not None:
                raise self.error
            owner = self.lease._process_owner
            if owner is not None and not owner.closed or result.status == 'completed' and owner is None:
                raise ValueError('external_progress_owner_not_closed')
            self.context.copy().run(self._flush, reported=result.usage,
                observed=result.observed_usage, terminal=result.status, final=True)
            self.closed = True

    def disable(self):
        with self.lock:
            self.disabled = True
            self.pending, self.pending_count = [], 0
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None

    def close(self):
        with self.lock:
            self.closed = True
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
