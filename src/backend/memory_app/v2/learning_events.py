"""Read completed domain facts; retain consumption separately from their owners."""
from datetime import datetime
import json

from .policies import get
from .outcome_corrections import COLLECTION as OUTCOMES, _time
from ..workspace_contracts import _now


COLLECTION = 'v2_learning_accumulation'


def _outcome_ready(payload, instant):
    """Unknown/unfinished facts stay pending; the original writer owns completion."""
    def identity(key):
        value = payload.get(key)
        return isinstance(value, str) and bool(value.strip())

    def revision(key):
        value = payload.get(key)
        return type(value) is int and value > 0

    if (not identity('project_id') or not identity('turn_id')
            or _time(payload.get('created_at')) is None):
        return False
    kind = payload.get('kind')
    if kind == 'division_adjust':
        return (revision('division_from_revision') and revision('division_to_revision')
            and payload['division_to_revision'] > payload['division_from_revision']
            and all(isinstance(payload.get(key), list) and payload[key]
                    and all(isinstance(goal, str) and goal.strip() for goal in payload[key])
                    for key in ('before_goals', 'after_goals')))
    if (kind not in ('outcome_edit', 'outcome_redo') or not identity('document_id')
            or not revision('birth_revision') or not revision('from_revision')
            or payload['from_revision'] < payload['birth_revision']
            or not isinstance(payload.get('before'), str)
            or not isinstance(payload.get('after'), str)):
        return False
    selected = payload.get('policy_version')
    if not isinstance(selected, str):
        return False
    try:
        policy = get('outcome_correction', version=selected)
    except ValueError:
        return False
    if kind == 'outcome_edit':
        last = _time(payload.get('last_saved_at'))
        return (payload.get('net_change') is True and revision('to_revision')
            and payload['to_revision'] > payload['from_revision']
            and instant is not None and last is not None
            and policy(operation='window', gap_seconds=(instant - last).total_seconds())['mature'])
    return (identity('new_turn_id') and payload['new_turn_id'] != payload['turn_id']
        and identity('new_document_id') and payload['new_document_id'] != payload['document_id']
        and revision('new_birth_revision') and revision('to_revision')
        and payload['to_revision'] >= payload['new_birth_revision']
        and isinstance(payload.get('before_title'), str) and isinstance(payload.get('after_title'), str)
        and _time(payload.get('completed_at')) is not None)


def _signal_decision_project(reader, payload):
    """Explicit human decisions remain feedback after implicit recording stops."""
    if (set(payload) != {'review_key', 'kind', 'action', 'turn_ids', 'object', 'at', 'by'}
            or payload['action'] != 'confirm' or payload['kind'] not in ('reask', 'stop')
            or payload['by'] != 'user' or payload['object'] is not None):
        return None
    identities = payload['turn_ids']
    expected = 2 if payload['kind'] == 'reask' else 1
    if (not isinstance(identities, list) or len(identities) != expected
            or any(not isinstance(identity, str) or not identity for identity in identities)
            or len(set(identities)) != len(identities)):
        return None
    try:
        binding = json.loads(payload['review_key'])
        at = datetime.fromisoformat(payload['at'])
        if (not isinstance(binding, list) or len(binding) != 3 or not isinstance(binding[0], str)
                or not binding[0] or binding[1:] != [payload['kind'], identities] or at.tzinfo is None):
            return None
        rows = [reader.read('v2_turns', identity) for identity in identities]
    except (ValueError, TypeError):
        return None
    project = binding[0]
    if any(row is None or row.payload.get('project_id') != project or row.payload.get('intent') != 'ask'
            or row.payload.get('by') == 'admin' or not isinstance(row.payload.get('receipt'), dict)
            or not isinstance(row.payload['receipt'].get('ask'), dict) for row in rows):
        return None
    return project


def events(reader, *, now=None):
    result = {}
    def add(project, identity):
        if project:
            result.setdefault(project, set()).add(identity)
    for row in reader.list('v2_correction_events'):
        add(row.payload.get('project_id'), 'correction:' + row.object_id)
    for row in reader.list('v2_signal_decisions'):
        add(_signal_decision_project(reader, row.payload), 'signal-decision:' + row.object_id)
    for row in reader.list('recognition_versions'):
        if row.payload.get('action') == 'publish':
            add(row.payload.get('snapshot', {}).get('scope', {}).get('project_id'), 'confirm:' + row.object_id)
    for row in reader.list('workspace_items'):
        payload = row.payload
        if payload.get('status') == 'confirmed' and payload.get('document_id'):
            document = reader.read('documents', payload['document_id'])
            if document and document.payload.get('project_id') == payload.get('project_id'):
                add(payload['project_id'], 'organized:' + document.object_id)
    instant = _time(_now() if now is None else now)
    for row in reader.list(OUTCOMES):
        if _outcome_ready(row.payload, instant):
            add(row.payload['project_id'], 'outcome:' + row.object_id)
    return result


def checkpoint(records, elapsed, *, clock_sample=None, previous=None, check_interval=60):
    with records.begin() as tx:
        facts = events(tx)
        projects = set(facts) | {row.object_id for row in tx.list(COLLECTION)}
        states = {}
        for project in sorted(projects):
            current = tx.read(COLLECTION, project)
            state = dict(current.payload) if current else {'score': 0, 'run_seconds': 0, 'seen_event_ids': []}
            seen = set(state['seen_event_ids'])
            incoming = facts.get(project, set()) - seen
            project_elapsed = elapsed
            reset = state.get('run_clock_reset')
            if (previous is not None and clock_sample is not None and reset is not None
                    and reset['session'] == clock_sample['session']):
                project_elapsed = get('trigger')(None, operation='elapsed',
                    elapsed=clock_sample['cursor'] - max(previous, reset['cursor']),
                    check_interval=check_interval)
            state.update(score=state['score'] + sum(get('trigger')(None, operation='points') for _ in incoming),
                         run_seconds=state['run_seconds'] + project_elapsed,
                         seen_event_ids=sorted(seen | incoming))
            if current is None or state != current.payload:
                tx.put(COLLECTION, project, state, expected_revision=current.revision if current else 0)
            states[project] = state
        tx.commit()
    return states


def completed(records, project, *, completion_clock=None):
    with records.begin() as tx:
        complete_in_transaction(tx, project,
            clock_sample=completion_clock() if completion_clock is not None else None)
        tx.commit()


def complete_in_transaction(tx, project, *, clock_sample=None):
    current = tx.read(COLLECTION, project)
    seen = set(current.payload.get('seen_event_ids', [])) if current else set()
    state = {'score': 0, 'run_seconds': 0, 'seen_event_ids': sorted(seen | events(tx).get(project, set()))}
    if clock_sample is not None:
        state['run_clock_reset'] = dict(clock_sample)
    tx.put(COLLECTION, project, state, expected_revision=current.revision if current else 0)
