from backend.recognition import RecognitionConflict, WorkScope
from .insights import resolve_insight, source_experiences


def statement_source(records, turn, *, source_owner=None):
    value = turn.payload
    if value['intent'] != 'inspiration':
        raise RecognitionConflict('route_statement_kind_invalid')
    scope = WorkScope('local-user', value['project_id'])
    birth = value['receipt']['inspiration']['insight']
    insight = resolve_insight(records, scope, birth['id'])
    if insight is None:
        raise RecognitionConflict('route_statement_missing')
    rows = source_experiences(records, scope, insight.payload.get('source_experience_ids', []),
        insight.payload.get('source_recognition_ids', []),
        experience_revisions=insight.payload.get('source_experience_revisions'),
        recognition_revisions=insight.payload.get('source_recognition_revisions'), source_owner=source_owner)
    rows = [row for row in rows if row.payload.get('content') == value['user_text']
        and row.payload.get('provenance', {}).get('kind') == 'user_statement']
    if not rows or type(birth.get('revision')) is not int or birth['revision'] < 1:
        raise RecognitionConflict('route_statement_changed')
    refs = [{'type': 'experience', 'id': row.object_id, 'revision': row.revision} for row in rows]
    return {'birth': {'id': birth['id'], 'revision': birth['revision']}, 'refs': refs,
        'text': '\n\n'.join(row.payload['content'] for row in rows)}


def remembered_source(records, turn):
    from ..original_sources import original_content_identity
    value = turn.payload
    if value['intent'] != 'remember':
        raise RecognitionConflict('route_statement_kind_invalid')
    identity = value['receipt']['remember']['item_id']
    item = records.read('workspace_items', identity)
    if item is None or item.payload['project_id'] != value['project_id']:
        raise RecognitionConflict('route_statement_missing')
    scope = WorkScope('local-user', value['project_id'])
    span = value['user_text'] if item.payload['input_kind'] == 'text' else None
    content = original_content_identity(records, scope, identity, span=span)
    text = span if span is not None else content['identity']['source_text']
    return {'refs': [{'type': 'original_item', 'id': identity, 'revision': item.revision}],
        'content': content, 'text': text,
        'ref': {'kind': 'source', 'object_id': identity, 'uri': 'crp://default/workspace/' + identity}}
