from datetime import datetime, timezone

from .external_progress import EXECUTORS
from .policies import get


COLLECTION = 'v2_external_watch'
EXECUTIONS = 'v2_task_executions'


def _running(payload):
    return payload.get('started') is True and bool(payload.get('owner')) and payload.get('executor') in EXECUTORS


def _sequence(receipt):
    if receipt.get('state') != 'running' or receipt.get('executor') not in EXECUTORS:
        return None
    progress = receipt.get('external_progress')
    if progress is None:
        return 0
    if not isinstance(progress, dict) or progress.get('terminal') is not None:
        return None
    sequence = progress.get('sequence')
    return sequence if type(sequence) is int and sequence >= 0 else None


def _matches(execution, turn, receipt):
    return (execution is not None and _running(execution.payload)
            and execution.payload.get('project_id') == turn.payload.get('project_id')
            and execution.payload['executor'] == receipt.get('executor'))


def check(records, now=None):
    now = now if now is not None else datetime.now(timezone.utc)
    active = {row.object_id for executor in EXECUTORS for row in records.list_matching(EXECUTIONS, executor=executor)
              if _running(row.payload)}
    if not active and not records.list(COLLECTION):
        return
    with records.begin() as tx:
        previous = {row.object_id: row for row in tx.list(COLLECTION)}
        for identity in active | previous.keys():
            saved = previous.get(identity)
            execution = tx.read(EXECUTIONS, identity)
            turn = tx.read('v2_turns', identity)
            receipt = turn.payload.get('receipt', {}).get('do', {}) if turn else {}
            sequence = _sequence(receipt) if turn and _matches(execution, turn, receipt) else None
            if sequence is None:
                if saved is not None:
                    tx.delete(COLLECTION, identity, expected_revision=saved.revision)
            elif saved is None or saved.payload['sequence'] != sequence:
                tx.put(COLLECTION, identity, {'sequence': sequence, 'seen_at': now.isoformat()},
                       expected_revision=saved.revision if saved is not None else 0)
        tx.commit()


def idle_minutes(records, turn, receipt, now):
    sequence = _sequence(receipt)
    if sequence is None:
        return None
    saved = records.read(COLLECTION, turn.object_id)
    if saved is None or saved.payload['sequence'] != sequence:
        return None
    if not _matches(records.read(EXECUTIONS, turn.object_id), turn, receipt):
        return None
    elapsed = (now - datetime.fromisoformat(saved.payload['seen_at'])).total_seconds()
    return int(elapsed // 60) if elapsed >= get('watch')() else None
