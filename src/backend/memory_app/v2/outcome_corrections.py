"""Enlist root-outcome edit facts in the document caller's transaction."""
from collections.abc import Mapping
from datetime import datetime
from difflib import SequenceMatcher
import re
from uuid import uuid4

from backend.recognition.product_draft_dependencies import ProductDraftDependencyError, product_draft_source
from .policies import get, version as selected_policy_version

COLLECTION = 'v2_outcome_corrections'
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _root(reader, scope, document_id):
    document = reader.read('documents', document_id)
    if document is None or not str(document.payload.get('type', '')).startswith('agent-result-turn-'):
        return None
    refs = document.payload.get('source_refs')
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], Mapping):
        return None
    kernel_id = refs[0].get('source_id')
    operation_id = 'deliver-' + kernel_id if isinstance(kernel_id, str) else ''
    if not _ID.fullmatch(operation_id):
        return None
    operation = reader.read('v2_task_draft_operations', operation_id)
    result = operation.payload.get('result') if operation else None
    birth = result.get('document_revision') if isinstance(result, Mapping) else None
    if type(birth) is not int or birth < 1:
        return None
    try:
        return product_draft_source(reader, scope, document_id, birth)
    except ProductDraftDependencyError:
        # Unknown publication/birth is not a new permission gate on local edits.
        return None


def _paragraphs(markdown):
    """Keep fenced/indented code atomic, including its interior blank lines."""
    result, lines, fence, indented = [], [], None, False

    def flush():
        if lines:
            result.append(''.join(lines).rstrip('\r\n'))
            lines.clear()

    for line in markdown.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence:
            lines.append(line)
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not match[2].strip()):
                fence = None
                flush()
            continue
        if indented:
            if not line.strip() or line.startswith(('    ', '\t')):
                lines.append(line)
                continue
            indented = False
            flush()
        if match:
            flush()
            fence = match[1]
            lines.append(line)
        elif not line.strip():
            flush()
        else:
            if not lines and line.startswith(('    ', '\t')):
                indented = True
            lines.append(line)
    flush()
    return result


def _changed(before, after, limit):
    old, new = _paragraphs(before), _paragraphs(after)
    removed, added = [], []
    for tag, left, right, start, end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != 'equal':
            removed.extend(old[left:right])
            added.extend(new[start:end])
    return '\n\n'.join(removed)[:limit], '\n\n'.join(added)[:limit], old != new


def record_edit(reader, documents, *, scope, document_id, from_revision, to_revision, now):
    """Record actual history after the original save; never commit for its owner."""
    bound = _root(reader, scope, document_id)
    if bound is None:
        return None
    before = documents.markdown(document_id, revision=from_revision)
    after = documents.markdown(document_id, revision=to_revision)
    if before is None or after is None:
        return None
    candidates = [row for row in reader.list(COLLECTION)
        if row.payload.get('kind') == 'outcome_edit' and row.payload.get('project_id') == scope.project_id
        and row.payload.get('document_id') == document_id
        and row.payload.get('turn_id') == bound.revisions['task_execution_id']
        and type(row.payload.get('to_revision')) is int and row.payload['to_revision'] <= from_revision]
    current = max(candidates, key=lambda row: row.payload['to_revision'], default=None)
    selected = current.payload.get('policy_version') if current else selected_policy_version('outcome_correction')
    if not isinstance(selected, str):
        return None  # A damaged recorded identity cannot borrow the current default.
    try:
        policy = get('outcome_correction', version=selected)
    except ValueError:
        return None  # Corrupt policy identity cannot reinterpret an old fact.
    last = _time(current.payload.get('last_saved_at')) if current else None
    instant = _time(now)
    gap = (instant - last).total_seconds() if instant is not None and last is not None else None
    merge = current is not None and policy(operation='window', gap_seconds=gap)['merge']
    if before == after and not merge:
        return None  # A noop only advances an existing, still-open edit window.
    if merge:
        from_revision = current.payload['from_revision']
        before = documents.markdown(document_id, revision=from_revision)
        if before is None:
            return None
        # Never guess an unknown clock or move a known last-success watermark back.
        saved_at = current.payload.get('last_saved_at') if last is None or instant is None or instant < last else now
    else:
        saved_at = now if instant is not None else None
    removed, added, changed = _changed(before, after, policy(operation='limits')['edit_side_chars'])
    payload = {'kind': 'outcome_edit', 'project_id': scope.project_id,
        'turn_id': bound.revisions['task_execution_id'], 'document_id': document_id,
        'birth_revision': bound.revisions['document_revision'],
        'from_revision': from_revision, 'to_revision': to_revision,
        'before': removed, 'after': added, 'net_change': changed, 'policy_version': selected,
        'created_at': current.payload['created_at'] if merge else saved_at, 'last_saved_at': saved_at}
    return reader.put(COLLECTION, current.object_id if merge else 'outcome-edit-' + uuid4().hex,
        payload, expected_revision=current.revision if merge else 0)


def record_division_adjust(reader, before, saved):
    """Append the original sample owner's successful adjustment in its transaction."""
    return reader.put(COLLECTION, 'division-adjust-' + uuid4().hex, {
        'kind': 'division_adjust', 'project_id': saved.payload['project_id'], 'turn_id': saved.object_id,
        'before_goals': [item['goal'] for item in before.payload['items']],
        'after_goals': [item['goal'] for item in saved.payload['items']],
        'division_from_revision': before.revision, 'division_to_revision': saved.revision,
        'created_at': saved.payload['adjusted_at'],
    }, expected_revision=0)


def _redo_result(reader, documents, *, scope, turn_id, limit):
    turn = reader.read('v2_turns', turn_id)
    if turn is None or turn.payload.get('project_id') != scope.project_id or turn.payload.get('intent') != 'do':
        return None
    receipt = turn.payload.get('receipt')
    result = receipt.get('do') if isinstance(receipt, Mapping) else None
    if not isinstance(result, Mapping) or result.get('state') != 'done':
        return None
    identity = result.get('document_id')
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        return None
    bound = _root(reader, scope, identity)
    if bound is None or bound.revisions['task_execution_id'] != turn_id:
        return None
    document = documents.read(identity)
    revision = document.get('revision') if document is not None else None
    if type(revision) is not int or revision < bound.revisions['document_revision']:
        return None
    historical = documents.revision(identity, revision)
    markdown = documents.markdown(identity, revision=revision)
    if (historical is None or historical.get('document_id') != identity or historical.get('revision') != revision
            or not isinstance(document.get('title'), str) or not isinstance(markdown, str)):
        return None
    return {'document_id': identity, 'revision': revision, 'birth_revision': bound.revisions['document_revision'],
            'title': document['title'], 'text': markdown[:limit]}


def record_redo(reader, documents, *, scope, turn_id, new_turn_id, now):
    """Freeze the actual old root result alongside its newly accepted product Turn."""
    selected = selected_policy_version('outcome_correction')
    limit = get('outcome_correction', version=selected)(operation='limits')['outcome_side_chars']
    before = _redo_result(reader, documents, scope=scope, turn_id=turn_id, limit=limit)
    if before is None:
        return None
    return reader.put(COLLECTION, 'outcome-redo-' + uuid4().hex, {
        'kind': 'outcome_redo', 'project_id': scope.project_id, 'turn_id': turn_id, 'new_turn_id': new_turn_id,
        'document_id': before['document_id'], 'birth_revision': before['birth_revision'],
        'from_revision': before['revision'], 'before_title': before['title'], 'before': before['text'],
        'policy_version': selected, 'created_at': now,
    }, expected_revision=0)


def complete_redos(reader, documents, *, scope, turn_id, now):
    """Fill each pending after excerpt only in the real completed root's transaction."""
    for row in reader.list(COLLECTION):
        payload = row.payload
        if (payload.get('kind') != 'outcome_redo' or payload.get('project_id') != scope.project_id
                or payload.get('new_turn_id') != turn_id or 'after' in payload):
            continue
        if not isinstance(payload.get('policy_version'), str):
            continue  # Missing frozen identity is unknown, never the current default.
        try:
            policy = get('outcome_correction', version=payload.get('policy_version'))
        except ValueError:
            continue  # Damaged learning identity cannot reject the original result publication.
        after = _redo_result(reader, documents, scope=scope, turn_id=turn_id,
            limit=policy(operation='limits')['outcome_side_chars'])
        if after is not None:
            reader.put(COLLECTION, row.object_id, {**payload,
                'new_document_id': after['document_id'], 'new_birth_revision': after['birth_revision'],
                'to_revision': after['revision'], 'after_title': after['title'], 'after': after['text'],
                'completed_at': now}, expected_revision=row.revision)
