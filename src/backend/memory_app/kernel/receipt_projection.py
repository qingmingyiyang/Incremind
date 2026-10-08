"""Read kernel facts without schema initialization, runtime construction or writes."""
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from core.storage_provider.observability import observe_connection
from core.storage_provider.connection_scope import borrow_read_connection

from core.ai_kernel.contracts import (
    AIKernelContractError, validate_model_call_receipt, validate_model_wire_attempt_receipt,
    validate_model_wire_attempt_dispatch, validate_turn_request,
)
from ..model_costs import attempt_cost
from ..turn_routing import _revision

PURPOSES = {'memory.organize': '整理', 'memory.propose_insights': '认识',
            'memory.consolidate': '认识', 'memory.link_suggest': '认识', 'memory.place': '整理',
            'project.answer': '问', 'project.task': '干活', 'workbench.route': '问',
            'media.image_read': '识图', 'web.search': '搜索'}
_ROUTES = {'recognition-model-routing-snapshot-v1', 'memory-model-route-v1',
           'organize-model-route-v1', 'product-aux-model-routing-v1', 'workbench-route-model-v1'}


def frozen_answer_request(runtime_root, identity, project, *, question):
    """Validate one existing frozen request; never initialize or recover a Turn."""
    return _frozen_request(runtime_root, identity, project, question=question, kind='project.answer')


def frozen_task_request(runtime_root, identity, project, *, question):
    """Task frame metadata has the same exact, read-only frozen-request seam."""
    return _frozen_request(runtime_root, identity, project, question=question, kind='project.task')


def _frozen_request(runtime_root, identity, project, *, question, kind):
    if kind not in {'project.answer', 'project.task'}:
        return None
    if not all(isinstance(value, str) and value for value in (identity, project, question)):
        return None
    root, found = Path(runtime_root), None
    for database in (root / '.rebuild-data/ai-turns.sqlite3', root / 'ai-turns.sqlite3'):
        if not database.is_file():
            continue
        def open_readonly():
            return observe_connection(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
        connection = borrow_read_connection(database, open_readonly)
        try:
            row = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (identity,)).fetchone()
            if row is None:
                continue
            request = validate_turn_request(_parse(row[0]))
            if (request['turn_id'] != identity or request['scope']['project_id'] != project
                    or request['desired_outcome'] != kind or request['input']['text'] != question):
                return None
            if found is not None and found != request:
                return None
            found = request
        except (sqlite3.Error, AIKernelContractError):
            return None
        finally:
            connection.close()
    return found


def closed_model_attempt_binding(runtime_root, identity, project, *, question, model_request_id, lease):
    """Inspect exact existing reservations; closure proof is supplied separately."""
    return _closed_model_attempt_binding(runtime_root, identity, project, question=question,
        model_request_id=model_request_id, lease=lease, kind='project.answer')


def closed_task_model_attempt_binding(runtime_root, identity, project, *, question, model_request_id, lease):
    return _closed_model_attempt_binding(runtime_root, identity, project, question=question,
        model_request_id=model_request_id, lease=lease, kind='project.task')


def _closed_model_attempt_binding(runtime_root, identity, project, *, question, model_request_id, lease, kind):
    if _frozen_request(runtime_root, identity, project, question=question, kind=kind) is None:
        return None
    if (not isinstance(model_request_id, str) or not model_request_id or type(lease) is not dict
            or set(lease) != {'owner_id', 'generation'} or not isinstance(lease['owner_id'], str)
            or type(lease['generation']) is not int or lease['generation'] < 1):
        return None
    found = None
    root = Path(runtime_root)
    for database in (root / '.rebuild-data/ai-turns.sqlite3', root / 'ai-turns.sqlite3'):
        if not database.is_file():
            continue
        def open_readonly():
            return observe_connection(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
        connection = borrow_read_connection(database, open_readonly)
        try:
            rows = connection.execute('SELECT r.attempt_id,r.attempt_number,r.status,r.dispatch_payload_ref,'
                'r.terminal_receipt_ref,r.lease_owner_id,r.lease_generation,d.payload_json,t.payload_json,e.state '
                'FROM ai_model_attempt_reservations r '
                'JOIN ai_turn_payloads d ON d.payload_ref=r.dispatch_payload_ref '
                'LEFT JOIN ai_turn_payloads t ON t.payload_ref=r.terminal_receipt_ref '
                'LEFT JOIN effect e ON e.operation_id=r.attempt_id '
                'WHERE r.turn_id=? AND r.model_request_id=? ORDER BY r.attempt_number',
                (identity, model_request_id)).fetchall()
            if not rows:
                continue
            values = []
            for number, row in enumerate(rows, 1):
                if (row[1] != number or row[2] != 'terminal' or not row[4]
                        or row[5] != lease['owner_id'] or row[6] != lease['generation']):
                    return None
                dispatched = validate_model_wire_attempt_dispatch(_parse(row[7]))
                terminal = validate_model_wire_attempt_receipt(_parse(row[8]))
                if (terminal['turn_id'] != identity or terminal['model_request_id'] != model_request_id
                        or terminal['attempt_id'] != row[0] or terminal['attempt_number'] != number
                        or terminal['status'] != 'failed_transport' or row[9] != 'UNKNOWN'
                        or terminal['error_code'] == 'ai.consumer_cancelled'
                        or any(dispatched.get(key) != terminal.get(key) for key in
                            ('attempt_id', 'attempt_number', 'turn_id', 'model_request_id', 'routing_snapshot_revision', 'provider_id', 'model_id'))):
                    return None
                values.append({'attempt_id': row[0], 'model_request_id': model_request_id,
                    'dispatch_ref': row[3], 'terminal_ref': row[4], 'lease': dict(lease)})
            if found is not None and found != values:
                return None
            found = values
        except (sqlite3.Error, AIKernelContractError):
            return None
        finally:
            connection.close()
    return found


def _parse(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def aggregate_usage(calls):
    """Preserve partial counters; an unreported part never becomes zero."""
    result = {}
    reported = []
    for call in calls:
        usage = dict(call.get('usage') or {})
        if 'total_tokens' not in usage and all(type(usage.get(k)) is int for k in ('input_tokens', 'output_tokens')):
            usage['total_tokens'] = usage['input_tokens'] + usage['output_tokens']
        reported.append(set(usage))
        for key in ('input_tokens', 'output_tokens', 'total_tokens'):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                result[key] = result.get(key, 0) + value
    complete = {key: value for key, value in result.items() if all(key in keys for keys in reported)}
    if complete:
        return complete
    if result:
        result['observed_only'] = True
    return result or None


def aggregate_cost(calls):
    """A known partial amount is never presented as the full expense."""
    if not calls:
        return None
    amount = Decimal(0)
    for call in calls:
        cost = call.get('cost')
        if not isinstance(cost, dict) or cost.get('currency') != 'CNY':
            return None
        try:
            value = Decimal(cost['amount'])
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return None
        if not value.is_finite() or value < 0:
            return None
        amount += value
    return {'currency': 'CNY', 'amount': format(amount, 'f')}


def egress_basis(calls):
    models = sorted({call['model_id'] for call in calls if call.get('model_id')})
    bases = [call['egress'] for call in calls if call.get('egress')]
    latest = bases[-1] if bases else {}
    return {'model': ' · '.join(models) or None, 'consent_scope': latest.get('consent_scope'),
            'settings_revision': latest.get('settings_revision')}


def _calls(connection, identity, project, tables, *, remote_only, records=None, turn_kind=None):
    immutable = {}
    if 'ai_turn_immutable_payloads' in tables:
        immutable = {kind: _parse(raw) for kind, raw in connection.execute(
            'SELECT kind,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?', (identity,))}
    parsed = []
    for kind, raw in connection.execute("SELECT kind,payload_json FROM ai_turn_payloads WHERE turn_id=? "
            "AND kind IN ('model-call-receipt','model-wire-attempt-receipt')", (identity,)):
        try:
            validator = validate_model_call_receipt if kind == 'model-call-receipt' else validate_model_wire_attempt_receipt
            safe = validator(_parse(raw))
            if safe['turn_id'] == identity:
                parsed.append((kind, safe))
        except AIKernelContractError:
            continue
    wires, receipts = {}, {}
    for kind, safe in parsed:
        key = safe['model_request_id']
        if kind == 'model-call-receipt':
            receipts[key] = safe
        elif safe.get('execution_location') in ({'remote'} if remote_only else {'remote', 'local', 'local_loopback'}):
            wires.setdefault(key, {})[safe['attempt_id']] = safe
    result = []
    for key, attempts in wires.items():
        attempts = list(attempts.values())
        call = receipts.get(key)
        # A crashed call can still have a durable paid wire receipt.
        latest = max(attempts, key=lambda item: item['completed_at'])
        call = dict(call or {'turn_id': identity, 'model_request_id': key,
            'model_id': latest['model_id'], 'completed_at': latest['completed_at'],
            'duration_ms': sum(item['duration_ms'] for item in attempts), 'usage': None,
            'usage_status': 'not_recorded'})
        wire_usage = aggregate_usage(attempts)
        # Count actual attempts, including paid responses whose logical call
        # failed validation. Never count the logical receipt a second time.
        call['cost'] = aggregate_cost([{'cost': attempt_cost(records, attempt)} for attempt in attempts])
        if wire_usage:
            call['usage'] = {k: v for k, v in wire_usage.items() if k != 'observed_only'}
            call['usage_status'] = 'recorded' if not wire_usage.get('observed_only') else 'partial'
        basis = []
        matching = []
        for kind, snapshot in immutable.items():
            if kind not in _ROUTES:
                continue
            if snapshot.get('turn_id', identity) != identity or snapshot.get('project_id', project) != project:
                continue
            config = snapshot.get('configuration', snapshot)
            if not isinstance(config, dict) or config.get('model') != call['model_id']:
                continue
            if config.get('allow_remote') is True and type(config.get('revision')) is int:
                purpose = {'media.image_read':'vision', 'web.search':'search'}.get(turn_kind, 'generation')
                value = {'model': call['model_id'], 'consent_scope': 'global_setting',
                    'settings_revision': {purpose: config['revision']}}
                if _revision(snapshot) == latest['routing_snapshot_revision']:
                    matching.append(value)
                elif kind != 'product-aux-model-routing-v1':
                    basis.append(value)
        # New routes disambiguate equal model names by the actual wire's
        # immutable route revision. Retain the unique historical projection.
        basis = matching or basis
        call['egress'] = basis[0] if len(basis) == 1 and latest.get('execution_location') == 'remote' else None
        result.append(call)
    return result, immutable


def kernel_call_groups(runtime_root, *, turn_id=None, project=None, remote_only=True, records=None, include_answer_input=False, include_no_wire_answers=False):
    if runtime_root is None:
        return []
    root = Path(runtime_root)
    groups = []
    seen_calls = set()
    for database in (root / '.rebuild-data/ai-turns.sqlite3', root / 'ai-turns.sqlite3'):
        if not database.is_file():
            continue
        def open_readonly():
            return observe_connection(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
        connection = borrow_read_connection(database, open_readonly)
        try:
            connection.execute('BEGIN')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'ai_turns', 'ai_turn_payloads'} <= tables:
                continue
            requests = {identity: _parse(raw) for identity, raw in connection.execute('SELECT turn_id,request_json FROM ai_turns')}
            parents = {}
            if 'ai_agent_runs' in tables:
                runs = list(connection.execute('SELECT run_id,turn_id,parent_run_id,project_id FROM ai_agent_runs'))
                by_run = {run: (turn, own_project) for run, turn, _, own_project in runs}
                for _, child, parent, own_project in runs:
                    if parent in by_run and by_run[parent][1] == own_project:
                        parents[child] = by_run[parent][0]
            def ancestor(identity):
                seen = set()
                while identity in parents and identity not in seen:
                    seen.add(identity)
                    identity = parents[identity]
                return identity
            def completed_answer(identity, request, immutable):
                if (not include_no_wire_answers or request.get('desired_outcome') != 'project.answer'
                        or request.get('turn_id') != identity or request.get('operation_id') != 'answer-' + identity
                        or 'ai_turn_events' not in tables or not immutable.get('product-answer-result-v2')):
                    return None
                events = [_parse(raw) for (raw,) in connection.execute(
                    'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (identity,))]
                terminal = [event for event in events if event.get('type') in
                    {'turn.completed', 'turn.failed', 'turn.cancelled'}]
                return terminal[-1] if terminal and terminal[-1].get('turn_id') == identity and terminal[-1]['type'] == 'turn.completed' else None
            selected = {}
            for identity, request in requests.items():
                scope = request.get('scope')
                own_project = scope.get('project_id') if isinstance(scope, dict) else None
                if not isinstance(own_project, str):
                    continue
                parent = ancestor(identity)
                if (project is not None and own_project != project) or (turn_id is not None and parent != turn_id and identity != turn_id):
                    continue
                root_request = requests.get(parent, {})
                root_scope = root_request.get('scope')
                if not isinstance(root_scope, dict) or root_scope.get('project_id') != own_project:
                    continue
                calls, immutable = _calls(connection, identity, own_project, tables, remote_only=remote_only, records=records,
                    turn_kind=request.get('desired_outcome'))
                terminal = completed_answer(identity, root_request, immutable) if identity == parent else None
                if not calls and terminal is None:
                    continue
                key = (parent, own_project)
                group = selected.setdefault(key, {'turn_id': parent, 'project_id': own_project,
                    'kind': root_request.get('desired_outcome'), 'request': root_request,
                    'calls': [], 'answer': None})
                group['calls'].extend(calls)
                if identity == parent:
                    group['answer'] = immutable.get('product-answer-result-v2')
                    if include_no_wire_answers:
                        group['terminal'] = terminal
                    if include_answer_input:
                        group['answer_input'] = immutable.get('answer-model-input-answer')
            for group in selected.values():
                fresh = []
                for call in group['calls']:
                    identity = (call['turn_id'], call['model_request_id'])
                    if identity not in seen_calls:
                        seen_calls.add(identity)
                        fresh.append(call)
                if fresh or include_no_wire_answers and group.get('terminal') is not None:
                    group['calls'] = fresh
                    groups.append(group)
        except sqlite3.DatabaseError:
            if not include_no_wire_answers:
                raise
        finally:
            connection.close()
    return groups


def _question_search_calls(runtime_root, group, search, *, records):
    """只合并原父轮明确绑定的搜索，不按项目或模型名称猜关联。"""
    if records is None or not isinstance(search, dict):
        return None
    identity = search.get('turn_id')
    version = group['request'].get('policy_versions', {}).get('search')
    if not isinstance(identity, str) or not version or search.get('policy_version') != version:
        return None
    row = records.read('v2_memory_turn_keys', identity)
    expected = {'kind':'web.search', 'project':group['project_id'],
                'key':group['turn_id'], 'purpose':'search'}
    if row is None or row.revision != 1 or row.payload.get('identity') != expected:
        return None
    groups = kernel_call_groups(runtime_root, turn_id=identity, project=group['project_id'],
                                remote_only=False, records=records)
    auxiliary = next((value for value in groups if value['turn_id'] == identity and value['kind'] == 'web.search'), None)
    if auxiliary is None:
        return None
    request = auxiliary['request']
    if (row.payload.get('request') != request or request.get('turn_id') != identity
            or request.get('input', {}).get('text') != group['request'].get('input', {}).get('text')
            or request.get('policy_versions') != {'search':version}):
        return None
    return auxiliary['calls']


def question_receipt(runtime_root, identity, project, previous, *, records=None):
    groups = kernel_call_groups(runtime_root, turn_id=identity, project=project, remote_only=False, records=records)
    group = next((group for group in groups if group['turn_id'] == identity and group['kind'] == 'project.answer'), None)
    if group is None:
        return previous
    saved = (group['answer'] or {}).get('receipt', {}).get('ask')
    result = dict(saved if isinstance(saved, dict) else previous)
    calls = group['calls']
    if isinstance(saved, dict) and 'search' in saved:
        search_calls = _question_search_calls(runtime_root, group, saved['search'], records=records)
        if search_calls is not None:
            calls = [*calls, *search_calls]
            result['search'] = {**saved['search'], 'model_usage':aggregate_usage(search_calls) or {},
                                'model_cost':aggregate_cost(search_calls)}
    else:
        search_calls = ()
    result['model_usage'] = aggregate_usage(calls) or {}
    result['model_cost'] = aggregate_cost(calls) if search_calls is not None else None
    if search_calls is None:
        result['model_usage']['observed_only'] = True
    if isinstance(saved, dict) and saved.get('model_usage', {}).get('observed_only'):
        result['model_usage']['observed_only'] = True
    context = result.get('context')
    if isinstance(context, dict):
        basis = egress_basis(group['calls'])
        mode = context.get('egress', {}).get('settings_revision', {}) or {}
        if isinstance(saved, dict) and basis['settings_revision'] and type(mode.get('mode')) is int:
            basis['settings_revision'] = {**basis['settings_revision'], 'mode': mode['mode']}
        result['context'] = {**context, 'egress': {**basis, 'excluded_private': None}}
    return result
