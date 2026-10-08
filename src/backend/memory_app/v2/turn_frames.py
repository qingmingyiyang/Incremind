"""Best-effort derived text frames; the frozen Kernel Turn owns execution."""
import logging
import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, Timer
from time import monotonic, time
from core.storage_provider.sqlite_uow import SQLiteStructuredRecord
from core.storage_provider.observability import observe_connection


FRAMES = 'v2_turn_frames'
STREAMS = 'v2_turn_streams'
MAX_SEQUENCE = 2 ** 53 - 1


def _retry_values(value, *, timed):
    fields = {'attempt', 'delay', 'budget', 'reason', 'used', 'limit'} | ({'retry_at'} if timed else set())
    if (type(value) is not dict or set(value) != fields
            or any(type(value[key]) is not int or not 1 <= value[key] <= MAX_SEQUENCE
                   for key in ('attempt', 'used', 'limit'))
            or value['used'] > value['limit']
            or value['budget'] not in {'before_output', 'thinking_error', 'thinking_stall', 'header', 'fallback'}
            or value['reason'] not in {'server', 'connection', 'timeout', 'stalled',
                                       'rate_limit', 'header_timeout', 'malformed_stream'}
            or type(value['delay']) not in {int, float} or not math.isfinite(value['delay'])
            or not 0 <= value['delay'] <= MAX_SEQUENCE):
        raise ValueError('invalid_retry_projection')
    if timed and (type(value['retry_at']) not in {int, float}
            or not math.isfinite(value['retry_at']) or not 0 < value['retry_at'] <= MAX_SEQUENCE):
        raise ValueError('invalid_retry_projection')
    return dict(value)


def stream_status(event, *, wall_clock=time):
    """Strict derived display DTO; server time is not an execution deadline."""
    if event.get('state') == 'retrying':
        if set(event) != {'sequence', 'kind', 'state', 'retry'}:
            raise ValueError('invalid_stream_status')
        retry = _retry_values(event['retry'], timed=True)
        now = wall_clock()
        if type(now) not in {int, float} or not math.isfinite(now) or not 0 < now <= MAX_SEQUENCE:
            raise ValueError('invalid_stream_server_time')
        return {'state': 'retrying', 'retry': retry, 'server_time': now}
    if set(event) != {'sequence', 'kind', 'state'} or event.get('state') not in {'running', 'interrupted', 'completed'}:
        raise ValueError('invalid_stream_status')
    return {'state': event['state']}


def _read_json(raw):
    def constant(_):
        raise ValueError('invalid_stream_json')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate_stream_json_key')
            result[key] = value
        return result
    return json.loads(raw, parse_constant=constant, object_pairs_hook=pairs)


class _StreamRecords:
    """Only the existing record read protocol, on a read-only snapshot."""
    def __init__(self, connection, path):
        self.connection, self.database_path = connection, path

    def read(self, collection, identity):
        if collection not in {STREAMS, FRAMES, 'v2_turns', 'v2_model_wire_prices',
                              'v2_task_profiles', 'v2_task_methods'}:
            raise ValueError('unsupported_stream_record')
        row = self.connection.execute('''SELECT payload_json,revision
            FROM crp_structured_records WHERE collection=? AND object_id=?''', (collection, identity)).fetchone()
        if row is None:
            return None
        payload = _read_json(row[0])
        if type(payload) is not dict or type(row[1]) is not int or row[1] < 1:
            raise ValueError('invalid_stream_record')
        return SQLiteStructuredRecord(collection, identity, payload, row[1])


@contextmanager
def read_stream_records(records):
    """Never call a store connector that could create or initialize its DB."""
    path = Path(records.database_path)
    if not path.is_file():
        raise ValueError('stream_database_unavailable')
    connection = observe_connection(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield _StreamRecords(connection, path)
    finally:
        connection.close()


def failed_stream_event(runtime_root, identity, frozen, *, safe_errors):
    """Read one exact terminal fact; this never restores execution authority."""
    found, present = None, False
    root = Path(runtime_root)
    for path in (root / '.rebuild-data/ai-turns.sqlite3', root / 'ai-turns.sqlite3'):
        if not path.is_file():
            continue
        connection = observe_connection(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            saved = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (identity,)).fetchone()
            if saved is None:
                continue
            if _read_json(saved[0]) != frozen:
                raise ValueError('stream_terminal_binding_changed')
            row = connection.execute('''SELECT sequence,event_json FROM ai_turn_events
                WHERE turn_id=? ORDER BY sequence DESC LIMIT 1''', (identity,)).fetchone()
            event = _read_json(row[1]) if row is not None else None
            if event is not None and (type(event) is not dict or event.get('turn_id') != identity
                    or event.get('session_id') != frozen['session_id']
                    or type(event.get('sequence')) is not int or event['sequence'] != row[0]
                    or event.get('schema_version') != '1.0.0'):
                raise ValueError('invalid_stream_terminal')
            if present and found != event:
                raise ValueError('ambiguous_stream_terminal')
            found, present = event, True
        finally:
            connection.close()
    if found is not None:
        status = {'turn.failed': 'failed', 'turn.cancelled': 'cancelled'}.get(found.get('type'))
        if status is not None:
            if not isinstance(found.get('data'), dict) or found['data'].get('status') != status:
                raise ValueError('invalid_stream_terminal_status')
            code = found['data'].get('error_code')
            return {'code': code if isinstance(code, str) and code in safe_errors else 'answer_generation_failed'}
    return None


def append_stream_event(payload, kind, **values):
    """Allocate only a derived delivery cursor, never an execution identity."""
    sequence = payload['sequence'] + 1
    if sequence > MAX_SEQUENCE:
        raise ValueError('stream_sequence_exhausted')
    event = {'sequence': sequence, 'kind': kind, **values}
    payload['sequence'] = sequence
    payload['events'] = [*payload['events'], event]
    return event


def complete_stream(tx, result):
    """Best-effort terminal projection inside the existing result transaction."""
    savepoint = False
    try:
        turn = result['turn']
        row = tx.read(STREAMS, turn['id'])
        if row is None or row.payload.get('terminal') is not None:
            return
        # Malformed derived reads and failed savepoint creation are also
        # independent of the authoritative result writes already enlisted.
        tx.connection.execute('SAVEPOINT workbench_stream_projection')
        savepoint = True
        payload = dict(row.payload)
        if (payload['thread_id'] != result['thread_id'] or payload['turn']['id'] != turn['id']
                or payload['turn']['user_text'] != turn['user_text']
                or payload['turn']['intent'] != turn['intent']):
            raise ValueError('stream_result_mismatch')
        partial = turn['receipt'].get(turn['intent'], {}).get('partial')
        if partial is not None:
            append_stream_event(payload, 'reset', source='terminal_partial')
            append_stream_event(payload, 'status', state='interrupted')
        else:
            append_stream_event(payload, 'status', state='completed')
        terminal = append_stream_event(payload, 'done')
        payload['terminal'] = terminal['sequence']
        payload['turn'] = {key: turn[key] for key in ('id', 'thread_id', 'intent', 'user_text', 'created_at')}
        # UoW.put rolls back its whole transaction on failure. Only these
        # derived rows use the public enlisted connection so this savepoint
        # can isolate their failure from the authoritative result writes.
        serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False)
        updated = tx.connection.execute('''UPDATE crp_structured_records
            SET payload_json=?, revision=revision+1
            WHERE collection=? AND object_id=? AND revision=?''',
            (serialized, STREAMS, turn['id'], row.revision))
        if updated.rowcount != 1:
            raise ValueError('stream_projection_conflict')
        if partial is None:
            frames = tx.read(FRAMES, turn['id'])
            if frames is not None and frames.payload.get('project_id') == payload['project_id']:
                deleted = tx.connection.execute('''DELETE FROM crp_structured_records
                    WHERE collection=? AND object_id=? AND revision=?''',
                    (FRAMES, turn['id'], frames.revision))
                if deleted.rowcount != 1:
                    raise ValueError('stream_frame_cleanup_conflict')
        tx.connection.execute('RELEASE workbench_stream_projection')
    except Exception:
        if savepoint:
            try:
                tx.connection.execute('ROLLBACK TO workbench_stream_projection')
                tx.connection.execute('RELEASE workbench_stream_projection')
            except Exception:
                logging.getLogger(__name__).warning('turn_stream_projection_cleanup_failed')
        logging.getLogger(__name__).warning('turn_stream_projection_failed')


def frame_text(payload):
    """Read only a contiguous derived sequence; malformed frames confer nothing."""
    values = payload.get('frames') if isinstance(payload, dict) else None
    if not isinstance(values, list) or (not values and 'text_prefix' not in payload):
        return None
    pieces = []
    for sequence, value in enumerate(values, 1):
        if (not isinstance(value, dict) or set(value) != {'sequence', 'text'}
                or type(value['sequence']) is not int or value['sequence'] != sequence
                or not isinstance(value['text'], str)):
            return None
        pieces.append(value['text'])
    if 'text_from' in payload or 'text_prefix' in payload:
        start, prefix = payload.get('text_from'), payload.get('text_prefix')
        if type(start) is not int or not 1 <= start <= len(pieces) + 1 or type(prefix) is not str:
            return None
        return prefix + ''.join(pieces[start - 1:])
    return ''.join(pieces)


class TurnFrames:
    def __init__(self, records, *, turn_id, project_id, recipe, turn=None, clock=monotonic, schedule=True,
                 projection=None, text_prefix=None, request=None, on_frame=None, wall_clock=time):
        self.records, self.turn_id, self.project_id = records, turn_id, project_id
        self.clock, self.schedule = clock, schedule
        self.wall_clock, self.retrying = wall_clock, False
        limits = recipe({'kind': 'partial_limits'})
        self.characters, self.seconds = limits['frame_characters'], limits['frame_seconds']
        self.lock, self.timer, self.timers = RLock(), None, []
        self.turn = dict(turn) if turn is not None else None
        self.projection, self.text_prefix, self.text_from = projection, text_prefix, None
        self.request, self.on_frame, self.stream = request, on_frame, False
        self.text, self.pending, self.closed = '', '', False
        self.last_write = clock()

    def start(self):
        if self.request is None or self.turn is None:
            return False
        try:
            with self.records.begin() as tx:
                row = tx.read(STREAMS, self.turn_id)
                binding = {'kernel_turn_id': self.request['turn_id'], 'request': self.request}
                if row is not None:
                    if (row.payload.get('binding') != binding
                            or row.payload.get('project_id') != self.project_id
                            or row.payload.get('thread_id') != self.turn['thread_id']
                            or row.payload.get('turn') != self.turn):
                        return False
                    if self.text_prefix is None:
                        self.stream = True
                        return True
                    if row.payload.get('terminal') is None:
                        return False
                    payload = dict(row.payload)
                else:
                    payload = {'project_id': self.project_id, 'thread_id': self.turn['thread_id'],
                        'turn': self.turn, 'binding': binding, 'sequence': 0, 'events': [], 'terminal': None}
                    event = append_stream_event(payload, 'started')
                if self.text_prefix is not None:
                    if type(self.text_prefix) is not str:
                        return False
                    previous = tx.read(FRAMES, self.turn_id)
                    if previous is not None and (previous.payload.get('project_id') != self.project_id
                            or previous.payload.get('thread_id') != self.turn['thread_id']
                            or frame_text(previous.payload) is None):
                        return False
                    frames = previous.payload['frames'] if previous is not None else []
                    self.text_from = len(frames) + 1
                    tx.put(FRAMES, self.turn_id, {'project_id': self.project_id, 'thread_id': self.turn['thread_id'],
                        'turn': self.turn, 'frames': frames, 'text_from': self.text_from,
                        'text_prefix': self.text_prefix,
                        **({'projection': self.projection} if self.projection is not None else {})},
                        expected_revision=previous.revision if previous else 0)
                    payload['terminal'] = None
                    event = append_stream_event(payload, 'reset', source='frame_prefix')
                tx.put(STREAMS, self.turn_id, payload, expected_revision=row.revision if row else 0)
                tx.commit()
            self.stream = True
            self._deliver(event, {'text': self.text_prefix} if self.text_prefix is not None else
                {'thread_id': self.turn['thread_id'], 'turn':
                 {key: self.turn[key] for key in ('id', 'intent', 'user_text')}})
            return True
        except Exception:
            return False

    def _deliver(self, event, value):
        if self.on_frame is not None:
            try:
                self.on_frame(event['sequence'], event['kind'], value)
            except Exception:
                pass

    def _status(self, state, *, retry=None):
        try:
            with self.records.begin() as tx:
                row = tx.read(STREAMS, self.turn_id)
                if (row is None or row.payload.get('terminal') is not None
                        or row.payload.get('binding') != {'kernel_turn_id': self.request['turn_id'], 'request': self.request}
                        or row.payload.get('project_id') != self.project_id
                        or row.payload.get('thread_id') != self.turn['thread_id']
                        or row.payload.get('turn') != self.turn):
                    return False
                payload = dict(row.payload)
                event = append_stream_event(payload, 'status', state=state,
                    **({'retry': retry} if retry is not None else {}))
                value = stream_status(event, wall_clock=self.wall_clock)
                tx.put(STREAMS, self.turn_id, payload, expected_revision=row.revision)
                tx.commit()
            self._deliver(event, value)
            return True
        except Exception:
            logging.getLogger(__name__).warning('turn_retry_projection_failed')
            return False

    def retry(self, value):
        with self.lock:
            if self.closed or (self.text_prefix is not None and not self.stream):
                return
            try:
                safe = _retry_values(value, timed=False)
                safe['retry_at'] = self.wall_clock() + safe['delay']
                _retry_values(safe, timed=True)
            except (ValueError, TypeError, KeyError):
                return
            if not self.stream and not self.start():
                return
            if self._status('retrying', retry=safe):
                self.retrying = True

    def _clear_retry(self):
        if self.retrying and self._status('running'):
            self.retrying = False

    def delta(self, text):
        with self.lock:
            if self.closed:
                return
            if text:
                self._clear_retry()
            self.text += text
            self.pending += text
            if len(self.pending) >= self.characters:
                self._flush()
            if self.schedule and self.timer is None:
                self.timer = Timer(self.seconds, self._tick)
                self.timer.daemon = True
                self.timers.append(self.timer)
                self.timer.start()

    def _tick(self):
        with self.lock:
            self.timer = None
            if not self.closed:
                self.flush_due()
                if self.pending:
                    self.timer = Timer(self.seconds, self._tick)
                    self.timer.daemon = True
                    self.timers.append(self.timer)
                    self.timer.start()

    def flush_due(self):
        with self.lock:
            if self.pending and self.clock() - self.last_write >= self.seconds:
                self._flush()

    def _flush(self):
        if not self.pending:
            return
        try:
            with self.records.begin() as tx:
                row = tx.read(FRAMES, self.turn_id)
                if row is not None and row.payload.get('project_id') != self.project_id:
                    return
                if (row is not None and self.projection is not None
                        and row.payload.get('projection') != self.projection):
                    return
                frames = list(row.payload['frames']) if row else []
                frames.append({'sequence': frames[-1]['sequence'] + 1 if frames else 1, 'text': self.pending})
                if self.text_prefix is not None and self.text_from is None:
                    self.text_from = frames[-1]['sequence']
                tx.put(FRAMES, self.turn_id, {'project_id': self.project_id, 'frames': frames,
                    **({'thread_id': self.turn['thread_id'], 'turn': self.turn} if self.turn is not None else {}),
                    **({'projection': self.projection} if self.projection is not None else {}),
                    **({'text_from': self.text_from, 'text_prefix': self.text_prefix}
                       if self.text_prefix is not None else {})},
                    expected_revision=row.revision if row else 0)
                event = None
                if self.stream:
                    head = tx.read(STREAMS, self.turn_id)
                    if head is None or head.payload['terminal'] is not None:
                        raise ValueError('stream_projection_closed')
                    payload = dict(head.payload)
                    event = append_stream_event(payload, 'delta', frame_sequence=frames[-1]['sequence'],
                        offset=0, length=len(self.pending))
                    tx.put(STREAMS, self.turn_id, payload, expected_revision=head.revision)
                tx.commit()
            delivered = self.pending
            self.pending, self.last_write = '', self.clock()
            if event is not None:
                self._deliver(event, {'text': delivered})
        except Exception:
            # Text delivery and model ownership do not depend on this projection.
            return

    def close(self, *, completed):
        with self.lock:
            self._clear_retry()
            self.closed = True
            timers, self.timers, self.timer = self.timers, [], None
            for timer in timers:
                timer.cancel()
            if not completed or self.stream:
                self._flush()
            if completed and not self.stream:
                try:
                    with self.records.begin() as tx:
                        row = tx.read(FRAMES, self.turn_id)
                        if row is not None and row.payload.get('project_id') == self.project_id:
                            tx.delete(FRAMES, self.turn_id, expected_revision=row.revision)
                            tx.commit()
                except Exception:
                    logging.getLogger(__name__).warning('turn_frame_cleanup_failed')
        for timer in timers:
            timer.join()
