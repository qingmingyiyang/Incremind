from collections.abc import Mapping
from sqlite3 import Error as SQLiteError

from backend.security.user_context import USER_ACCESS, authorize_user
from ..transaction_records import TransactionRecords
from ..workbench_identity_binding import execution_owner
from ..workspace_contracts import _empty_ask_context
from .material_origins import SharedProjectLease
from .turn_logical_scope import TurnLogicalScope


_DIVISIONS = ('division', 'experts')
_CONTEXT_SOURCES = ('entries', 'feedback')


def _receipt(turn, intent):
    receipt = turn.get('receipt') if isinstance(turn, Mapping) and turn.get('intent') == intent else None
    value = receipt.get(intent) if isinstance(receipt, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _parts(turn):
    receipt = turn.get('receipt') if isinstance(turn, Mapping) and turn.get('intent') == 'multi' else None
    parts = receipt.get('parts') if isinstance(receipt, Mapping) else None
    return parts if isinstance(parts, list) else ()


def _has_ask_sources(turn):
    ask = _receipt(turn, 'ask')
    return (ask.get('context') not in (None, _empty_ask_context())
        or any(ask.get(field) for field in ('citations', 'trace', 'egress_receipt_id', 'excluded_sources', 'search', 'elsewhere'))
        or any((ask.get('layers') or {}).values()))


def has_receipt_sources(turn):
    if _has_ask_sources(turn) or any(_has_ask_sources(part) for part in _parts(turn)):
        return True
    task = _receipt(turn, 'do')
    context = task.get('context')
    if isinstance(context, Mapping) and any(key in context for key in _CONTEXT_SOURCES):
        return True
    for field in _DIVISIONS:
        rows = task.get(field)
        if isinstance(rows, (tuple, list)) and any(isinstance(row, Mapping) and row.get('recalled') for row in rows):
            return True
    return False


def _without_ask_sources(turn):
    ask = _receipt(turn, 'ask')
    public = {key: ask[key] for key in ('answer', 'partial', 'interruption', 'error', 'no_match',
        'model_usage', 'model_cost') if key in ask}
    public.update(citations=[], layers=dict.fromkeys(('insight', 'summary', 'note', 'source', 'persona'), 0),
        trace=[], egress_receipt_id=None, excluded_sources=[], context=None, sources_available=False)
    return {**turn, 'receipt': {**turn['receipt'], 'ask': public}}


def _without_sources(turn):
    if turn['intent'] == 'ask':
        return _without_ask_sources(turn)
    if turn['intent'] == 'multi':
        return {**turn, 'receipt': {**turn['receipt'], 'parts': [
            _without_ask_sources(part) if _has_ask_sources(part) else part for part in _parts(turn)]}}
    task = dict(_receipt(turn, 'do'))
    for field in _DIVISIONS:
        if isinstance(task.get(field), (tuple, list)):
            task[field] = [{key: value for key, value in row.items() if key != 'recalled'}
                if isinstance(row, Mapping) and 'recalled' in row else row for row in task[field]]
    if isinstance(task.get('context'), Mapping):
        task['context'] = {key: value for key, value in task['context'].items() if key not in _CONTEXT_SOURCES}
    return {**turn, 'receipt': {**turn['receipt'], 'do': task}}


def _physical_source(lease):
    return lease.project_id, lease.namespace, lease.root_identity


class SharedReceiptDelivery:
    def __init__(self, application, project):
        self.application, self.project = application, project
        self._bound = {}

    def bind(self, turn, kind, shared_leases):
        # 本次视图已从实际Actor事务核对原生归属，保存同一对象供交付复用。
        self._bound[turn['id']] = (turn, kind, tuple(shared_leases))

    def _historical_scope(self, turn):
        bound = self._bound.get(turn['id'])
        if bound is not None and bound[0] is turn:
            return bound[1], bound[2]
        # 原编号回放可能直接带已保存回执，先重取同一轮的实际归属。
        logical = TurnLogicalScope(self.application)
        try:
            with logical.records.begin_read() as transaction:
                reader = TransactionRecords(transaction)
                row = reader.read('v2_turns', turn['id'])
                if row is None or any(row.payload[field] != turn[field] for field in ('thread_id', 'intent', 'user_text')):
                    return None, ()
                if turn['intent'] == 'do' and (row.payload['project_id'] != self.project
                        or row.payload['receipt']['do'].get('kernel_turn_id') != _receipt(turn, 'do').get('kernel_turn_id')):
                    return None, ()
                leases = []
                kind = logical.read(row, reader=reader, shared_leases=leases)
                if kind == 'shared' and turn['intent'] == 'multi' and not self._matched_parts(logical, reader, row, turn):
                    return None, ()
                return kind, tuple(leases)
        except (OSError, SQLiteError, ValueError, KeyError, TypeError, IndexError):
            return None, ()

    @staticmethod
    def _matched_parts(logical, reader, row, turn):
        from .route_reuse import dependency_parent

        for index, part in enumerate(_parts(turn)):
            if not _has_ask_sources(part):
                continue
            identity = part['turn_id']
            if identity != row.payload['part_turn_ids'][index]:
                return False
            child = reader.read('v2_turns', identity)
            if child is None or child.payload['intent'] != 'ask' or child.payload['project_id'] != part['project_id']:
                return False
            dependency_parent(reader, row.object_id, identity, source_owner=logical.records)
        return True

    def _current_lease(self, project):
        state = self.application.state
        access = USER_ACCESS.get()
        factory = getattr(state, 'shared_query_factory', None)
        if (access is None or factory is None or access.target_user_id != getattr(state, 'server_user_id', None)
                or factory.registry.users is not getattr(getattr(state, 'server_context', None), 'users', None)):
            return None
        registry = factory.registry
        try:
            owner = execution_owner(self.application, state.recognition_records, state.recognition_documents)
            if owner['owner_user_id'] != access.target_user_id:
                return None
            if authorize_user(registry.users, access.caller, access.target_user_id) != access:
                return None
            return SharedProjectLease.acquire(registry, access.caller, project)
        except (ValueError, OSError, SQLiteError):
            return None

    def turns(self, values):
        selected = {index: self._historical_scope(turn) for index, turn in enumerate(values)
            if has_receipt_sources(turn) and (turn['intent'] != 'do' or turn.get('scope_kind') == 'shared')}
        if not selected:
            return values
        projects = {lease.project_id for kind, leases in selected.values() if kind == 'shared' for lease in leases}
        current = {project: self._current_lease(project) for project in projects}
        result = list(values)
        for index, (kind, leases) in selected.items():
            if kind == 'personal' and result[index].get('scope_kind') != 'shared':
                continue
            if kind != 'shared' or not leases or any(current[lease.project_id] is None
                    or _physical_source(lease) != _physical_source(current[lease.project_id]) for lease in leases):
                result[index] = _without_sources(result[index])
        return result

    def result(self, value):
        turn = value.get('turn') if isinstance(value, Mapping) else None
        projected, = self.turns([turn])
        return {**value, 'turn': projected} if projected is not turn else value
