"""在原领域事务内追加纠正事实，时钟仅在缺少已有时间时读取。"""
from datetime import datetime, timezone
import json


def record_correction(transaction, previous, current, *, object_kind, event_type='edit', after=None,
                      event_id=None, at=None, turn_id=None, clock=None):
    """复用调用者的事务与修订，不建立第二个存储或提交边界。"""
    scope = previous.payload['scope']
    refs = {}
    for row in (previous, current):
        for name, collection, kind in (('experience', 'recognition_experiences', 'experience'),
                                       ('recognition', 'recognitions', 'recognition')):
            for identity in row.payload.get('source_' + name + '_ids', []):
                source = transaction.read(collection, identity)
                if source is not None:
                    revision = row.payload.get('source_' + name + '_revisions', {}).get(identity, source.revision)
                    ref = {'type': kind, 'id': identity, 'revision': revision,
                           'project_id': source.payload['scope']['project_id']}
                    refs[(kind, identity, revision)] = ref
    text = lambda row: json.dumps({'text': row.payload['content'],
        'conditions': row.payload.get('conditions', [])}, ensure_ascii=False, separators=(',', ':'))
    identity = event_id or 'correction-' + object_kind + '-' + current.object_id + '-' + str(current.revision)
    payload = {'project_id': scope['project_id'], 'user_id': scope['user_id'],
        'object_kind': object_kind, 'object_id': current.object_id, 'object_revision': current.revision,
        'type': event_type, 'before': text(previous), 'after': text(current) if after is None else after,
        'source_refs': list(refs.values()),
        **({'turn_id': turn_id} if turn_id is not None else {}),
        'at': at or (current.payload.get('reviewed_at') if event_type == 'reject' else current.payload.get('updated_at'))
              or (datetime.now(timezone.utc) if clock is None else clock()).isoformat()}
    transaction.put('v2_correction_events', identity, payload, expected_revision=0)
