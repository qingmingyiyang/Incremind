"""Append user strikes against actual frozen context, preserving every receipt."""
from fastapi import APIRouter, HTTPException, Request

from ..workspace_contracts import _json, _now, _project

COLLECTION = 'v2_context_feedback'


def _frozen_method(records, turn, identity, revision, read_model_input):
    from types import SimpleNamespace
    task = turn.payload.get('receipt', {}).get('do', {})
    if task.get('kernel_turn_id'):
        methods = records.read('v2_task_methods', task['kernel_turn_id'])
        method = next((row for row in methods.payload['methods'] if row['id'] == identity
                       and row['entry']['revision'] == revision), None) if methods else None
        if method:
            entry = method['entry']
            return SimpleNamespace(object_id=identity, revision=revision, payload={
                'scope': {'user_id': 'local-user', 'project_id': turn.payload['project_id']},
                'content': entry['content'], 'conditions': entry.get('conditions', []),
                'source_experience_ids': [ref['id'] for ref in entry['source_refs'] if ref['type'] == 'experience'],
                'source_recognition_ids': [ref['id'] for ref in entry['source_refs'] if ref['type'] == 'recognition'],
                'source_experience_revisions': {ref['id']: ref['revision'] for ref in entry['source_refs'] if ref['type'] == 'experience'},
                'source_recognition_revisions': {ref['id']: ref['revision'] for ref in entry['source_refs'] if ref['type'] == 'recognition'}})
    frozen = read_model_input(turn.object_id) if read_model_input is not None else None
    if frozen is None:
        return None
    version = next((row for row in records.list('recognition_versions')
                    if row.payload['recognition_id'] == identity and row.payload['recognition_revision'] == revision), None)
    if version is None:
        return None
    payload = version.payload['snapshot']
    if payload['scope'] != {'user_id': 'local-user', 'project_id': turn.payload['project_id']}:
        return None
    from ..context_adapter import format_recognition_content
    # Historical versions retain the statement and conditions. Qualification
    # metadata in the model block is richer than the unqualified version row.
    rendered = format_recognition_content(payload).rsplit('\n\n', 1)[0]
    entries = turn.payload.get('receipt', {}).get('ask', {}).get('context', {}).get('entries', [])
    # The confirmed persona block precedes sources but has no citation number.
    numbered = [entry for entry in entries if not entry.get('persona')]
    prefixes = [f"情境补全方法：\n[{number}] {entry['title']}\n{rendered}"
                for number, entry in enumerate(numbered, 1) if entry.get('id') == identity
                and entry.get('object_revision') == revision and entry.get('supplemented')]
    if not any(prefix in message['content'] for prefix in prefixes for message in frozen['messages']):
        return None
    return SimpleNamespace(object_id=identity, revision=revision, payload=payload)


def struck_methods(records, project):
    return {row.payload['object_id'] for row in records.list(COLLECTION)
            if row.payload.get('project_id') == project and row.payload.get('action') == 'strike'
            and row.payload.get('object_kind') == 'recognition'}


def _turn(reader, identity, project):
    turn = reader.read('v2_turns', identity)
    if turn is None or turn.payload.get('project_id') != project:
        raise HTTPException(404, 'workbench_not_found')
    return turn


def _entries(reader, turn):
    receipts = turn.payload.get('receipt', {})
    entries = [entry for receipt in receipts.values()
               for entry in receipt.get('context', {}).get('entries', [])
               if entry.get('supplemented')]
    task = receipts.get('do', {})
    if task.get('kernel_turn_id'):
        from .do_context import kernel_task_context
        context = kernel_task_context(reader, turn.payload['project_id'], task['kernel_turn_id'])['context']
        if context.get('feedback') == {'turn_id': turn.object_id, 'project_id': turn.payload['project_id']}:
            entries.extend(entry for entry in context.get('entries', []) if entry.get('supplemented'))
    return entries


def strike(records, turn_id, *, project_id, object_kind, object_id, object_revision, expected_revision,
           read_model_input=None):
    if (object_kind != 'recognition' or type(object_revision) is not int or object_revision < 1
            or type(expected_revision) is not int or expected_revision < 0):
        raise HTTPException(400, 'invalid_context_feedback')
    identity = _project('strike-' + turn_id + '-' + object_id)
    with records.begin() as tx:
        turn = _turn(tx, turn_id, project_id)
        if not any(entry['id'] == object_id and entry.get('object_revision') == object_revision
                   for entry in _entries(tx, turn)):
            raise HTTPException(409, 'context_entry_changed')
        previous = tx.read(COLLECTION, identity)
        if previous is not None:
            if previous.payload.get('object_revision') != object_revision:
                raise HTTPException(409, 'context_feedback_conflict')
            tx.rollback()
            return {'id': identity, 'revision': previous.revision}
        if expected_revision != 0:
            raise HTTPException(409, 'context_feedback_conflict')
        row = tx.put(COLLECTION, identity, {
            'turn_id': turn_id, 'project_id': project_id, 'object_kind': object_kind,
            'object_id': object_id, 'object_revision': object_revision,
            'action': 'strike', 'at': _now()}, expected_revision=expected_revision)
        method = _frozen_method(records, turn, object_id, object_revision, read_model_input)
        if method is not None:
            from backend.shared.memory_sidecars import record_correction
            record_correction(tx, method, method, object_kind='recognition', event_type='strike', after='',
                event_id='correction-' + identity, at=row.payload['at'], turn_id=turn_id)
        tx.commit()
    return {'id': identity, 'revision': row.revision}


def install_context_feedback_routes(application, *, records):
    router = APIRouter(prefix='/api/v2/workbench/turns')

    def read_model_input(identity):
        store = getattr(application.state, 'ai_turn_store', None)
        payload = store.get_immutable_payload(identity, 'answer-model-input-answer') if store is not None else None
        return payload[1] if payload is not None else None

    @router.get('/{turn_id}/context-feedback')
    def list_feedback(turn_id: str, project_id: str):
        project_id = _project(project_id)
        _turn(records, turn_id, project_id)
        return {'items': [dict(row.payload) for row in records.list(COLLECTION)
                          if row.payload.get('turn_id') == turn_id and row.payload.get('project_id') == project_id]}

    @router.post('/{turn_id}/context-feedback')
    async def post_feedback(turn_id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id', 'object_kind', 'object_id', 'object_revision', 'expected_revision'}:
            raise HTTPException(400, 'invalid_context_feedback')
        return strike(records, turn_id, read_model_input=read_model_input, **{**body, 'project_id': _project(body['project_id']),
                                         'object_id': _project(body['object_id'])})

    application.include_router(router)
