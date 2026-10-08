"""Persist optional performance diagnostics separately from API responses."""
from contextlib import contextmanager
import logging
import os
from uuid import uuid4

from core.storage_provider.observability import Observation, observation_scope

COLLECTION = 'v2_turn_timings'
_log = logging.getLogger(__name__)


class TurnObservation(Observation):
    def __init__(self, records, operation, turn_id):
        super().__init__(operation, turn_id)
        self.records = records
        self.deferred = False
        self._holders = 1
        self.finished = False

    def defer_finish(self):
        """Retain one background owner before scheduling its work."""
        with self.lock:
            if not self.finished:
                self.deferred = True
                self._holders += 1

    def finish(self):
        with self.lock:
            if self.finished:
                return
            self._holders -= 1
            if self._holders:
                return
            self.finished = True
        try:
            payload = self.snapshot()
            with observation_scope(None), self.records.begin() as tx:
                row = tx.read(COLLECTION, self.turn_id)
                tx.put(COLLECTION, self.turn_id, payload,
                       expected_revision=row.revision if row else 0)
                tx.commit()
        except Exception as error:
            _log.warning('timing_write_failed error_type=%s sqlite_code=%s',
                         type(error).__name__, getattr(error, 'sqlite_errorcode', None))


@contextmanager
def turn_timing(records, operation, *, turn_id=None):
    enabled = os.environ.get('CHRIPTMAS_TURN_TIMINGS', '1').lower() not in {'0', 'false', 'off', 'no'}
    try:
        observation = TurnObservation(records, operation, turn_id or 'timing-' + uuid4().hex) if enabled else None
    except Exception:
        _log.warning('timing_start_failed')
        observation = None
    with observation_scope(observation):
        try:
            yield observation
        finally:
            if observation is not None:
                observation.finish()
