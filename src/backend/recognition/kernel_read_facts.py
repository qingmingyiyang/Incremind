"""Narrow read-only projections of the existing Turn and Agent authorities."""
import json
import sqlite3

from core.ai_kernel.agent_contracts import agent_run_from_payload
from core.ai_kernel.sqlite_store import (
    _immutable_payload_identity, _immutable_payload_ref, _mapping_json,
)
from .service import RecognitionConflict
from .external_turn_facts import _readonly


_UNAVAILABLE = 'research terminal authority is unavailable'
_OPERATION = 'SELECT kind,request_json,result_json FROM ai_agent_operations WHERE operation_id=?'
_SCHEMA = (
    'SELECT turn_id,request_json FROM ai_turns LIMIT 0',
    'SELECT turn_id,sequence,event_json FROM ai_turn_events LIMIT 0',
    'SELECT payload_ref,payload_json FROM ai_turn_payloads LIMIT 0',
    'SELECT payload_ref,turn_id,kind,payload_json FROM ai_turn_immutable_payloads LIMIT 0',
    'SELECT run_id,turn_id,project_id,parent_run_id,revision,payload_json FROM ai_agent_runs LIMIT 0',
    'SELECT operation_id,kind,request_json,result_json FROM ai_agent_operations LIMIT 0',
)


def authority_facts(database):
    """Validate the fixed existing schema without initializing or migrating it."""
    try:
        with _readonly(database) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise RecognitionConflict(_UNAVAILABLE) from None
    return _TurnFacts(database), _AgentFacts(database)


class _Facts:
    def __init__(self, database):
        self._database = database

    def _rows(self, statement, parameters, decode=lambda row: row):
        try:
            with _readonly(self._database) as connection:
                return tuple(decode(row) for row in connection.execute(statement, parameters))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise RecognitionConflict(_UNAVAILABLE) from None


class _TurnFacts(_Facts):
    def get_request(self, turn_id):
        rows = self._rows('SELECT request_json FROM ai_turns WHERE turn_id=?',
            (turn_id,), lambda row: _mapping_json(row[0]))
        return rows[0] if rows else None

    def events_after(self, turn_id, after_sequence=0):
        return self._rows('SELECT event_json FROM ai_turn_events WHERE turn_id=? AND sequence>? ORDER BY sequence',
            (turn_id, after_sequence), lambda row: _mapping_json(row[0]))

    def get(self, payload_ref):
        rows = self._rows('SELECT payload_json FROM ai_turn_payloads WHERE payload_ref=? UNION ALL '
            'SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=? LIMIT 1',
            (payload_ref, payload_ref), lambda row: json.loads(str(row[0])))
        if not rows:
            raise KeyError(payload_ref)
        return rows[0]

    def immutable_payload_reference(self, turn_id, kind):
        try:
            _immutable_payload_identity(turn_id, kind)
        except ValueError:
            raise RecognitionConflict(_UNAVAILABLE) from None
        rows = self._rows('SELECT payload_ref FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
            (turn_id, kind))
        return str(rows[0][0]) if rows else _immutable_payload_ref(turn_id, kind)


class _AgentFacts(_Facts):
    def get_run(self, run_id):
        rows = self._rows('SELECT payload_json FROM ai_agent_runs WHERE run_id=?',
            (run_id,), lambda row: agent_run_from_payload(json.loads(str(row[0]))))
        return rows[0] if rows else None

    def get_run_by_turn_id(self, turn_id, *, project_id):
        rows = self._rows('SELECT revision,payload_json FROM ai_agent_runs WHERE turn_id=? AND project_id=?',
            (turn_id, project_id), lambda row: (agent_run_from_payload(json.loads(str(row[1]))), int(row[0])))
        return rows[0] if rows else None

    def list_runs(self, *, project_id, parent_run_id=None):
        where, parameters = ('project_id=?', (project_id,)) if parent_run_id is None else (
            'project_id=? AND parent_run_id=?', (project_id, parent_run_id))
        return self._rows(f'SELECT payload_json FROM ai_agent_runs WHERE {where} ORDER BY run_id',
            parameters, lambda row: agent_run_from_payload(json.loads(str(row[0]))))

    def _read_one(self, statement, parameters):
        """Compatibility seam for the one original product admission operation."""
        if (statement != _OPERATION or not isinstance(parameters, tuple) or len(parameters) != 1
                or not isinstance(parameters[0], str) or not parameters[0]):
            raise RecognitionConflict(_UNAVAILABLE)
        rows = self._rows(_OPERATION, parameters, _operation)
        return rows[0] if rows else None


def _operation(row):
    if row[0] == 'write_run':
        # Only parse the original fact shape. The caller retains kind, equality,
        # main Run, project, frozen policy and admission qualification checks.
        agent_run_from_payload(json.loads(str(row[1])))
        agent_run_from_payload(json.loads(str(row[2])))
    return row
