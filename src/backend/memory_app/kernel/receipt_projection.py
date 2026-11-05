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


def frozen_public_turn_requests(records, actual_runtime_root, public_id, *, reader=None):
    from backend.recognition.external_turn_facts import source_store
    from backend.security.audited_records import AuditedRecordStore
    from backend.security.user_context import USER_ACCESS
    from core.storage_provider import SQLiteStructuredRecordStore
    from ..transaction_records import TransactionRecords
    access = USER_ACCESS.get()
    if (not isinstance(records,(SQLiteStructuredRecordStore,AuditedRecordStore))
            or (access is not None and (not isinstance(records,AuditedRecordStore)
                or access.target_user_id != records.user_id))):
        raise ValueError('public_native_actual_owner_required')
    current = records if reader is None else reader
    if reader is not None and (not isinstance(reader,TransactionRecords) or not reader.connection.in_transaction
            or Path(reader.database_path).resolve() != Path(records.database_path).resolve()):
        raise ValueError('public_native_transaction_owner_changed')
    source = source_store(records)
    root = Path(actual_runtime_root).resolve()
    if source.root.resolve() != root / '.rebuild-data':
        raise ValueError('public_native_owner_root_changed')
    databases = [path for path in (root / '.rebuild-data/ai-turns.sqlite3', root / 'ai-turns.sqlite3') if path.is_file()]
    if len(databases) > 1:
        raise ValueError('public_native_owner_ambiguous')
    public = current.read('v2_turns', public_id)
    if public is None:
        raise ValueError('public_native_turn_unavailable')
    observed = {('v2_turns',public_id):public}

    def read(collection, identity):
        row = current.read(collection,identity)
        if row is None:
            raise ValueError('public_native_binding_unavailable')
        observed[(collection,identity)] = row
        return row

    if public.payload.get('intent') == 'multi':
        children = public.payload.get('part_turn_ids')
        parts = public.payload.get('receipt',{}).get('parts')
        if (not isinstance(children,list) or not children or len(children) != len(set(children))
                or not isinstance(parts,list) or len(parts) != len(children)):
            raise ValueError('public_native_parts_changed')
        leaves = [read('v2_turns',identity) for identity in children]
        if any(leaf.payload.get('parent_turn_id') != public_id for leaf in leaves):
            raise ValueError('public_native_parent_changed')
    else:
        leaves = [public]
    mapped = [leaf for leaf in leaves if leaf.payload.get('intent') in {'ask','do'}]
    if not mapped:
        return ()
    if not databases:
        raise ValueError('public_native_request_unavailable')
    owner = getattr(records,'user_id',None) or 'local-user'
    namespace = getattr(records,'namespace',None) or source.namespace_id
    connection = observe_connection(sqlite3.connect(databases[0].resolve().as_uri()+'?mode=ro',uri=True,timeout=5))
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        entries = tuple(_public_native_leaf(connection,read,leaf,owner,namespace,records,current) for leaf in mapped)
        # 公共行和原执行行可能在读取 native 证明时推进，要求本次快照仍来自同一修订。
        for (collection,identity), prior in observed.items():
            fresh = current.read(collection,identity)
            if fresh is None or fresh.revision != prior.revision or fresh.payload != prior.payload:
                raise ValueError('public_native_public_revision_changed')
        return entries
    finally:
        connection.close()


def _public_native_leaf(connection, read, row, owner, namespace, actual_records, current_reader):
    from core.ai_kernel.product_admission import ADMISSION_KINDS, MANIFEST_KIND
    from core.ai_kernel.product_snapshot import admission_base_refs, restore_admission_snapshot
    value, identity = row.payload, row.object_id
    project, thread_id = value['project_id'], value['thread_id']
    thread = read('v2_threads',thread_id)
    parent_id, part, parent = value.get('parent_turn_id'), None, None
    planned = None
    if parent_id is not None:
        parent = read('v2_turns',parent_id)
        children = parent.payload.get('part_turn_ids')
        parts = parent.payload.get('receipt',{}).get('parts')
        if (parent.payload.get('intent') != 'multi' or not isinstance(children,list)
                or len(children) != len(set(children)) or identity not in children
                or not isinstance(parts,list) or len(parts) != len(children)):
            raise ValueError('public_native_parent_changed')
        part = children.index(identity)
        if parent_id.startswith('turn-route-'):
            from ..route_correction_scopes import planned_child_scope
            from ..transaction_records import TransactionRecords
            planned = planned_child_scope(actual_records,parent_id,identity,
                reader=current_reader if isinstance(current_reader,TransactionRecords) else None)
            if (planned is None or planned['target']['project_id'] != project
                    or planned['target']['thread_id'] != thread_id or planned['target']['namespace'] != f'personal/{owner}/{namespace}'
                    or planned['part_index'] != part):
                raise ValueError('public_native_planned_scope_changed')
        if (parts[part].get('index') != part or parts[part].get('intent') != value['intent']
                or parts[part].get('turn_id') not in {None,identity}
                or parts[part].get('project_id') not in {None,project}
                or planned is None and (parent.payload.get('thread_id') != thread_id
                    or parent.payload.get('project_id') != project)):
            raise ValueError('public_native_part_changed')
    if thread.payload.get('project_id') != project:
        raise ValueError('public_native_thread_changed')
    execution = None
    if value['intent'] == 'ask':
        native_id = identity
    else:
        execution = read('v2_task_executions',identity).payload
        requested = validate_turn_request(execution['request'])
        native_id = requested['turn_id']
        receipt = value.get('receipt',{}).get('do',{})
        if (execution.get('project_id') != project or receipt.get('kernel_turn_id') != native_id
                or receipt.get('executor','builtin') != execution.get('executor','builtin')):
            raise ValueError('public_native_task_changed')
    raw = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?',(native_id,)).fetchone()
    if raw is None:
        raise ValueError('public_native_request_unavailable')
    request = validate_turn_request(json.loads(raw[0]))
    if (request['turn_id'] != native_id or request['scope']['project_id'] != project
            or request['session_id'] != 'session-'+identity):
        raise ValueError('public_native_request_changed')
    if value['intent'] == 'ask':
        question = (parent.payload['receipt']['parts'][part].get('instruction') if parent is not None else None) or value['user_text']
        if (request['desired_outcome'] != 'project.answer' or request['operation_id'] != 'answer-'+identity
                or request['idempotency_key'] != 'answer-'+identity or request['input']['text'] != question
                or request['capability_request'] != {'mode':'execute_exact_v1',
                    'capability_id':'workbench.answer.execute','arguments':{'query':question}}):
            raise ValueError('public_native_answer_changed')
    else:
        # Organization 仅追加其原生 agent 许可，产品冻结输入及其余字段逐项保留。
        additions = {'agent_binding','agent_policy_binding'}
        if (any(request.get(key) != item for key,item in execution['request'].items())
                or set(request)-set(execution['request'])-additions
                or request['desired_outcome'] != 'project.task'
                or json.loads(request['input']['text']).get('task') != execution.get('task_text')):
            raise ValueError('public_native_task_request_changed')
    immutable = {kind:(reference,json.loads(body)) for kind,reference,body in connection.execute(
        'SELECT kind,payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?',(native_id,))}
    if execution is not None and execution.get('executor','builtin') != 'builtin':
        archive = immutable.get('external-task-run-v1')
        if (archive is None or archive[1].get('request') != request or archive[1].get('owner_id') != owner
                or archive[1].get('turn_id') != native_id
                or archive[1].get('launch',{}).get('executor') != execution.get('executor')):
            raise ValueError('public_native_external_owner_changed')
    admission_base_refs(request,immutable)
    qualified, binding = False, None
    if MANIFEST_KIND in immutable or any(kind in immutable for kind in ADMISSION_KINDS):
        bundle = restore_admission_snapshot(request,immutable)
        item = next((item for item in bundle.items if item.kind == 'workbench-steer-binding-v1'),None)
        if item is None:
            raise ValueError('public_native_identity_unavailable')
        binding = item.payload
        expected = {'owner_user_id':owner,'namespace_id':namespace,'origin_kind':'personal',
            'public_turn_id':parent_id or identity,'target_turn_id':identity,'thread_id':thread_id,
            'project_id':project,'part':part,'turn_id':native_id,
            'part_turn_ids':parent.payload['part_turn_ids'] if parent is not None else [identity]}
        if any(binding.get(key) != item for key,item in expected.items()):
            raise ValueError('public_native_identity_changed')
        if planned is not None:
            if binding.get('planned_scope') != planned or binding.get('scene') != planned['target']['scene']:
                raise ValueError('public_native_planned_identity_changed')
        elif 'planned_scope' in binding:
            raise ValueError('public_native_planned_identity_changed')
        parent_proof = binding.get('parent')
        if parent is None:
            if parent_proof is not None:
                raise ValueError('public_native_parent_changed')
        elif (not isinstance(parent_proof,dict) or parent_proof.get('id') != parent_id
                or parent_proof.get('thread_id') != parent.payload['thread_id'] or parent_proof.get('project_id') != parent.payload['project_id']
                or parent_proof.get('uri') != f'crp://{namespace}/v2_turns/{parent_id}'
                or parent_proof.get('part_turn_ids') != parent.payload['part_turn_ids']
                or type(parent_proof.get('revision')) is not int or not 1 <= parent_proof['revision'] <= parent.revision):
            raise ValueError('public_native_parent_changed')
        if execution is not None and binding.get('scene') != execution.get('scene'):
            raise ValueError('public_native_scene_changed')
        qualified = True
    if planned is not None and not qualified:
        raise ValueError('public_native_planned_identity_unavailable')
    return {'public_id':identity,'parent_id':parent_id,'part':part,'public_revision':row.revision,
        'native_request':request,'qualified':qualified,'identity':binding}


def frozen_answer_request(runtime_root, identity, project, *, question):
    """Validate one existing frozen request; never initialize or recover a Turn."""
    return _frozen_request(runtime_root, identity, project, question=question, kind='project.answer')


def frozen_task_request(runtime_root, identity, project, *, question):
    """Task frame metadata has the same exact, read-only frozen-request seam."""
    return _frozen_request(runtime_root, identity, project, question=question, kind='project.task')


def frozen_task_assignment(runtime_root, original, assignment_id):
    if not isinstance(assignment_id, str) or not assignment_id:
        return None
    identity, project = original.get('turn_id'), original.get('scope', {}).get('project_id')
    question = original.get('input', {}).get('text')
    accepted = frozen_task_request(runtime_root, identity, project, question=question)
    if accepted is None or any(original.get(name) != accepted.get(name) for name in (
            'turn_id', 'session_id', 'operation_id', 'idempotency_key', 'scope', 'input',
            'desired_outcome', 'privacy', 'execution_policy', 'policy_versions', 'created_at')):
        return None
    found = None
    for database in (Path(runtime_root) / '.rebuild-data/ai-turns.sqlite3', Path(runtime_root) / 'ai-turns.sqlite3'):
        if not database.is_file():
            continue
        def open_readonly():
            return observe_connection(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
        connection = borrow_read_connection(database, open_readonly)
        try:
            row = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
                (identity, 'agent.dispatch.task.' + assignment_id + '.v1')).fetchone()
            if row is None:
                continue
            payload = _parse(row[0])
            if (payload.get('schema_version') != '1.0.0' or payload.get('assignment_id') != assignment_id
                    or payload.get('task') != question or 'division' in payload):
                return None
            if found is not None and found != payload:
                return None
            found = payload
        except sqlite3.Error:
            return None
        finally:
            connection.close()
    return found


def _frozen_request(runtime_root, identity, project, *, question, kind):
    if kind not in {'project.answer', 'project.task', 'agent.steward.plan'}:
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


def closed_model_attempt_binding(runtime_root, identity, project, *, question, model_request_id, lease, stopped=False):
    """Inspect exact existing reservations; closure proof is supplied separately."""
    return _closed_model_attempt_binding(runtime_root, identity, project, question=question,
        model_request_id=model_request_id, lease=lease, kind='project.answer', stopped=stopped)


def closed_task_model_attempt_binding(runtime_root, identity, project, *, question, model_request_id, lease,
                                      kind='project.task', stopped=False):
    if kind != 'project.task' and not (stopped and kind == 'agent.steward.plan'):
        return None
    return _closed_model_attempt_binding(runtime_root, identity, project, question=question,
        model_request_id=model_request_id, lease=lease, kind=kind, stopped=stopped)


def _closed_model_attempt_binding(runtime_root, identity, project, *, question, model_request_id, lease, kind, stopped=False):
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
                        or terminal['status'] not in ({'failed_transport', 'consumer_cancelled'} if stopped else {'failed_transport'})
                        or row[9] != 'UNKNOWN'
                        or not stopped and terminal['error_code'] == 'ai.consumer_cancelled'
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


def _answer_input(connection, identity, request, immutable):
    from ..answer_proof import (EVIDENCE_ANSWER_KEY, RAW_ANSWER_KEY, REVIEW_KEY,
        has_evidence_answer_proof, has_raw_answer_proof, select_answer_input,
        select_evidence_answer_input, select_raw_answer_input, read_answer_input)
    from core.ai_kernel.product_snapshot import read_product_turn_snapshot, requires_product_answer_snapshot
    if requires_product_answer_snapshot(request, immutable):
        from ..answer_effective import NativeAnswerSnapshot
        snapshot = read_product_turn_snapshot(connection, identity)
        if snapshot is None:
            raise ValueError('answer_effective_snapshot_unavailable')
        selected = read_answer_input(NativeAnswerSnapshot(snapshot), identity)
        return selected[1] if selected is not None else None
    capsule = immutable.get('answer-continuation-plan-v1')
    product = immutable.get('product-answer-result-v2')
    if has_evidence_answer_proof(request, capsule):
        return select_evidence_answer_input(request, product, immutable.get('answer-model-input-' + EVIDENCE_ANSWER_KEY),
            immutable.get('answer-model-result-' + EVIDENCE_ANSWER_KEY), capsule)
    if has_raw_answer_proof(request, capsule):
        return select_raw_answer_input(request, product, immutable.get('answer-model-input-' + RAW_ANSWER_KEY),
            immutable.get('answer-model-result-' + RAW_ANSWER_KEY), capsule)
    return select_answer_input(request, product, immutable.get('answer-model-input-answer'),
        immutable.get('answer-model-result-answer'), immutable.get('answer-model-input-' + REVIEW_KEY),
        immutable.get('answer-model-result-' + REVIEW_KEY), capsule=capsule)


def _calls(connection, identity, project, tables, *, remote_only, records=None, turn_kind=None, include_attempts=False):
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
        costs = [attempt_cost(records,attempt) for attempt in attempts]
        call['cost'] = aggregate_cost([{'cost':cost} for cost in costs])
        if include_attempts:
            fields = ('attempt_id','turn_id','model_request_id','attempt_number','started_at',
                'completed_at','duration_ms','status','usage_status','usage')
            call['wire_attempts'] = [{**{field:attempt[field] for field in fields},'cost':cost}
                for attempt,cost in zip(attempts,costs)]
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
                # 新辅助路由记录真实用途；旧回执沿用原 kind 映射。
                purpose = config.get('purpose', {'media.image_read':'vision', 'web.search':'search'}.get(turn_kind, 'generation'))
                if not isinstance(purpose, str) or purpose not in {'generation', 'embedding', 'rerank', 'asr', 'vision', 'search'}:
                    continue
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


def kernel_call_groups(runtime_root, *, turn_id=None, project=None, remote_only=True, records=None, include_answer_input=False, include_no_wire_answers=False, include_attempts=False):
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
                    turn_kind=request.get('desired_outcome'),include_attempts=include_attempts)
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
                        group['answer_input'] = _answer_input(connection, identity, root_request, immutable)
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
    if search.get('version') == 2:
        from ..v2.search import validate_search_decision
        from ..v2.policies import get
        version = group['request'].get('policy_versions', {}).get('retrieve')
        try:
            agentic = (isinstance(version, str) and bool(version)
                       and getattr(get('retrieve', version=version), 'agentic', False) is True)
        except ValueError:
            agentic = False
        if (search.get('parent_turn_id') != group['turn_id']
                or not agentic):
            return None
        calls, seen = [], set()
        for branch in search.get('branches', []):
            try:
                validate_search_decision(records, group['turn_id'], branch['round'], branch['question'])
            except (ValueError, KeyError):
                return None
            identity = branch['turn_id']
            row = records.read('v2_memory_turn_keys', identity)
            key = group['turn_id'] + ':rag:' + str(branch['round'])
            if (branch['key'] != key or row is None or row.payload.get('identity') != {
                    'kind': 'web.search', 'project': group['project_id'], 'key': key, 'purpose': 'search'}):
                return None
            children = kernel_call_groups(runtime_root, turn_id=identity, project=group['project_id'],
                                          remote_only=False, records=records)
            child = next((value for value in children if value['turn_id'] == identity and value['kind'] == 'web.search'), None)
            if (child is None or child['request']['input']['text'] != branch['question']
                    or row.payload['request'] != child['request']
                    or child['request'].get('policy_versions') != {'search': search['policy_version']}):
                return None
            for call in child['calls']:
                identifier = (call['turn_id'], call['model_request_id'])
                if identifier not in seen:
                    calls.append(call)
                    seen.add(identifier)
        return calls
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
