"""Closed JSON continuation facts; original Turn/lease remain the authority."""
import math
from datetime import datetime, timezone, timedelta
from time import monotonic

from backend.recognition import WorkScope
from core.search_and_recall.evidence_windows import EvidenceWindow
from core.storage_provider import SQLiteStructuredRecord


COLLECTION = 'v2_answer_continuations'
PLAN_KIND = 'answer-continuation-plan-v1'


def encode(value):
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is WorkScope:
        return {'tag': 'scope', 'values': [value.user_id, value.project_id]}
    if type(value) is EvidenceWindow:
        return {'tag': 'window', 'values': [value.start, value.end, value.text]}
    if type(value) is SQLiteStructuredRecord:
        return {'tag': 'record', 'values': [value.collection, value.object_id, encode(value.payload), value.revision]}
    if type(value) in (tuple, list):
        return {'tag': 'tuple' if type(value) is tuple else 'list', 'values': [encode(item) for item in value]}
    if type(value) is dict:
        return {'tag': 'map', 'values': [[encode(key), encode(item)] for key, item in value.items()]}
    raise ValueError('invalid_answer_continuation_facts')


def decode(value, _depth=0):
    if _depth > 32:
        raise ValueError('invalid_answer_continuation_depth')
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is not dict or set(value) != {'tag', 'values'} or type(value['values']) is not list:
        raise ValueError('invalid_answer_continuation_facts')
    tag, values = value['tag'], value['values']
    try:
        if tag == 'scope' and len(values) == 2:
            return WorkScope(*values)
        if (tag == 'window' and len(values) == 3 and type(values[0]) is int and type(values[1]) is int
                and 0 <= values[0] <= values[1] and isinstance(values[2], str)):
            return EvidenceWindow(*values)
        if (tag == 'record' and len(values) == 4 and all(isinstance(item, str) and item for item in values[:2])
                and type(values[3]) is int and values[3] > 0):
            payload = decode(values[2], _depth + 1)
            if type(payload) is dict:
                return SQLiteStructuredRecord(values[0], values[1], payload, values[3])
        if tag in ('tuple', 'list'):
            decoded = [decode(item, _depth + 1) for item in values]
            return tuple(decoded) if tag == 'tuple' else decoded
        if tag == 'map':
            result = {}
            for pair in values:
                if type(pair) is not list or len(pair) != 2:
                    raise ValueError('invalid_answer_continuation_map')
                key = decode(pair[0], _depth + 1)
                if key in result:
                    raise ValueError('duplicate_answer_continuation_key')
                result[key] = decode(pair[1], _depth + 1)
            return result
    except (TypeError, KeyError) as error:
        raise ValueError('invalid_answer_continuation_facts') from error
    raise ValueError('invalid_answer_continuation_facts')


def save_plan(query, identity, plan, history, *, request, store, runtime):
    """Freeze the existing selected facts, never collect different candidates."""
    guards = {}
    for name in ('overview_guard', 'bookshelf_guard', 'search_guard'):
        guard = plan.get(name)
        if guard is not None:
            # 搜索沿首答已冻结的原 Turn 证明恢复，不编码可执行闭包。
            value = getattr(guard, 'frozen_binding' if name == 'search_guard' else 'continuation', None)
            if value is None:
                raise ValueError('answer_guard_is_not_reconstructible')
            guards[name] = value
    facts = {key: value for key, value in plan.items() if key not in
        ('overview_guard', 'bookshelf_guard', 'search_guard', 'history_guard', 'expires_monotonic', 'state', 'result')}
    capsule = encode({'plan': facts, 'guards': guards, 'history': history,
        'preview_lifetime': max(0, plan['expires_monotonic'] - monotonic())})
    lease = runtime._run_lease_context.get()
    frozen = store.get_request(identity)
    if (lease is None or lease.turn_id != identity or request['turn_id'] != identity
            or frozen['scope']['project_id'] != plan['project_id'] or frozen['input']['text'] != plan['question']
            or store.assert_active_run_lease(lease) is None):
        raise ValueError('answer_continuation_lease_unavailable')
    ref = store.get_or_create_immutable_payload(identity, PLAN_KIND,
        {'turn_id': identity, 'project_id': plan['project_id'], 'question': plan['question'],
         'request': frozen, 'lease': {'owner_id': lease.owner_id, 'generation': lease.generation}, 'capsule': capsule})
    if store.assert_active_run_lease(lease) is None:
        raise ValueError('answer_continuation_lease_changed')
    with query.records.begin() as tx:
        old = tx.read(COLLECTION, identity)
        if old is not None:
            raise ValueError('answer_continuation_already_exists')
        tx.put(COLLECTION, identity, {'project_id': plan['project_id'], 'question': plan['question'],
            'state': 'prepared', 'plan_ref': ref}, expected_revision=0)
        tx.commit()


def plan_binding(query, row):
    store = query.answer_turns.application.state.ai_turn_store
    saved = store.get_immutable_payload(row.object_id, PLAN_KIND)
    if saved is None or row.payload.get('plan_ref') != saved[0] or 'capsule' in row.payload:
        raise ValueError('invalid_answer_continuation_plan_reference')
    binding = saved[1]
    if (type(binding) is not dict or set(binding) != {'turn_id', 'project_id', 'question', 'request', 'lease', 'capsule'}
            or binding['turn_id'] != row.object_id or binding['project_id'] != row.payload['project_id']
            or binding['question'] != row.payload['question'] or binding['request'] != store.get_request(row.object_id)
            or type(binding['lease']) is not dict or set(binding['lease']) != {'owner_id', 'generation'}
            or not isinstance(binding['lease']['owner_id'], str) or type(binding['lease']['generation']) is not int
            or binding['lease']['generation'] < 1):
        raise ValueError('invalid_answer_continuation_plan_binding')
    return binding


def restore_plan(query, row):
    frozen = decode(plan_binding(query, row)['capsule'])
    if type(frozen) is not dict or set(frozen) != {'plan', 'guards', 'history', 'preview_lifetime'}:
        raise ValueError('invalid_answer_continuation_plan')
    plan, guards, history = frozen['plan'], frozen['guards'], frozen['history']
    project = row.payload['project_id']
    if (type(plan) is not dict or plan.get('project_id') != project or plan.get('question') != row.payload['question']
            or plan.get('scope') != WorkScope('local-user', project) or type(guards) is not dict):
        raise ValueError('invalid_answer_continuation_scope')
    if set(guards) - {'overview_guard', 'bookshelf_guard', 'search_guard'} or 'bookshelf_guard' not in guards:
        raise ValueError('invalid_answer_continuation_guards')
    lifetime = frozen['preview_lifetime']
    if type(lifetime) not in (int, float) or not math.isfinite(lifetime) or lifetime <= 0:
        raise ValueError('invalid_answer_continuation_preview')
    plan.update(state='pending', expires_monotonic=monotonic() + lifetime,
        expires_at=(datetime.now(timezone.utc) + timedelta(seconds=lifetime)).isoformat())
    return plan, guards, history
