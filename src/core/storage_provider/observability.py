"""Request-local, content-free timing and SQLite counters."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import logging
from threading import Lock
from time import perf_counter

STAGES = ('read_records', 'build_entries', 'keyword', 'vector', 'ladder',
          'prompt_build', 'gateway_send', 'first_token', 'generation', 'persist')
_current = ContextVar('request_observation', default=None)
_frames = ContextVar('request_timing_frames', default=())
_log = logging.getLogger(__name__)


class Observation:
    def __init__(self, operation, turn_id):
        self.operation, self.turn_id = operation, turn_id
        self.started = perf_counter()
        self.stages = dict.fromkeys(STAGES, 0.0)
        self.stage_observations = dict.fromkeys(STAGES, 0)
        self.connections = self.statements = 0
        self.closed = False
        self.lock = Lock()

    def set_turn_id(self, turn_id):
        self.turn_id = turn_id

    def add_duration(self, name, milliseconds):
        with self.lock:
            if not self.closed and name in self.stages:
                self.stages[name] += max(0.0, milliseconds)
                self.stage_observations[name] += 1

    def mark_elapsed(self, name, started_perf_counter):
        try:
            self.add_duration(name, (perf_counter() - started_perf_counter) * 1000)
        except Exception:
            _log.warning('timing_clock_failed')

    def count(self, *, connection=False):
        with self.lock:
            if not self.closed:
                if connection:
                    self.connections += 1
                else:
                    self.statements += 1

    def snapshot(self):
        with self.lock:
            self.closed = True
            return {'turn_id': self.turn_id, 'operation': self.operation,
                    'total_ms': (perf_counter() - self.started) * 1000,
                    'stages_ms': dict(self.stages), 'stage_observations': dict(self.stage_observations), 'connection_count': self.connections,
                    'statement_count': self.statements}


def current_observation():
    return _current.get()


@contextmanager
def observation_scope(observation):
    token = _current.set(observation)
    frames_token = _frames.set(())
    try:
        yield observation
    finally:
        _frames.reset(frames_token)
        _current.reset(token)


@contextmanager
def stage(name):
    token = None
    try:
        observation = current_observation()
        if observation is not None and not observation.closed:
            frames = _frames.get()
            token = _frames.set((*frames, (perf_counter(), 0.0)))
    except Exception:
        _log.warning('timing_stage_failed')
    try:
        yield
    finally:
        if token is not None:
            try:
                started, child_seconds = _frames.get()[-1]
                elapsed = perf_counter() - started
                _frames.reset(token)
                token = None
                parents = _frames.get()
                if parents:
                    parent_start, parent_children = parents[-1]
                    _frames.set((*parents[:-1], (parent_start, parent_children + elapsed)))
                observation.add_duration(name, (elapsed - child_seconds) * 1000)
            except Exception:
                _log.warning('timing_stage_failed')
            finally:
                if token is not None:
                    _frames.reset(token)


def timed_stage(name):
    def decorate(function):
        @wraps(function)
        def timed(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return timed
    return decorate


def observe_connection(connection):
    """Attach a content-discarding callback; count only connections opened now.

    Resolve the observer on each SQL execution so existing shared connections
    follow the executing request, including copied async/thread contexts.
    """
    try:
        observation = current_observation()
        if observation is not None:
            observation.count(connection=True)
        def trace(_sql):
            try:
                current = current_observation()
                if current is not None:
                    current.count()
            except Exception:
                _log.warning('timing_sql_observer_failed')
        connection.set_trace_callback(trace)
    except Exception:
        _log.warning('timing_connection_observer_failed')
    return connection
