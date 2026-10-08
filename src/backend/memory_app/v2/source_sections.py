"""Capture-owned codepoint sections beside, never inside, the original payload."""
from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from backend.recognition import RecognitionConflict, WorkScope, normalize_conditions
from core.storage_provider.connection_scope import borrow_read_connection
from core.storage_provider.observability import observe_connection
from ..original_sources import document_roots, original, source_store
from .policies.extract_comments import _sources


SECTIONS = 'v2_original_sections'
EXTRACT_INPUTS = 'v2_comment_extract_inputs'
CANDIDATES = 'v2_comment_candidates'
_BASE = {'item_id', 'project_id', 'owner_birth', 'state'}
_CAPTURE = {'source_text', 'origin', 'body', 'comment_section', 'comments'}
_BOUND = _BASE | _CAPTURE | {'capture_id', 'capture_owner_revision', 'capture_run_id'}
_PAIR = _BASE | {'input_binding'}
_INPUT = {'id', 'owner_revision', 'request_url', 'group_revision', 'group', 'images'}
_X_CAPTURE = _CAPTURE | {'input_binding', 'ocr_read', 'ocr_range', 'comment_status'}
_X_BOUND = _BASE | _X_CAPTURE | {'capture_id', 'capture_owner_revision', 'capture_run_id'}


def _image_inputs(reader, row, binding):
    from ..workspace_audio import _audio_original_identity
    from ..workspace_media_url import media_platform
    if (not isinstance(binding, Mapping) or set(binding) != _INPUT
            or type(binding['owner_revision']) is not int
            or not 0 < binding['owner_revision'] <= row.revision
            or type(binding['group_revision']) is not int or binding['group_revision'] < 0
            or row.payload.get('input_kind') != 'image'):
        _fail()
    _token(binding['id'])
    if (not isinstance(binding['images'], list) or not binding['images']
            or any(not isinstance(image, Mapping) or set(image) != {'item_id', 'ordinal', 'identity'}
                or type(image['ordinal']) is not int or image['ordinal'] != ordinal
                or image['item_id'] != row.object_id
                for ordinal, image in enumerate(binding['images'], 1))):
        _fail()
    _identity(binding['request_url'])
    if media_platform(binding['request_url']) != 'xiaohongshu':
        _fail()
    group = reader.read('v2_image_groups', row.object_id)
    actual_group = group.payload if group else None
    if (binding['group_revision'] != (group.revision if group else 0)
            or binding['group'] != actual_group):
        _fail()
    try:
        entries = actual_group['images'] if actual_group else [{'path': row.payload['original_path'],
            'name': row.payload.get('original_name') or row.payload['title']}]
        images = [{'item_id': row.object_id, 'ordinal': ordinal,
            'identity': _audio_original_identity(Path(entry['path']))}
            for ordinal, entry in enumerate(entries, 1)]
        if images != binding['images'] or entries[0]['path'] != row.payload['original_path']:
            _fail()
        if any(type(value) is not type(saved['identity'][key])
                for actual, saved in zip(images, binding['images'])
                for key, value in actual['identity'].items()):
            _fail()
    except (KeyError, OSError, TypeError, ValueError):
        _fail()


def bind_image_link(tx, row, url, images):
    """Join an actual upload to this input without changing the original row."""
    proof = tx.read(SECTIONS, row.object_id)
    if (proof is None or proof.payload.get('owner_birth') is None
            or proof.payload.get('item_id') != row.object_id
            or proof.payload.get('project_id') != row.payload['project_id']):
        _fail()
    _token(proof.payload['owner_birth'])
    group = tx.read('v2_image_groups', row.object_id)
    binding = {'id': uuid4().hex, 'owner_revision': row.revision, 'request_url': url,
        'group_revision': group.revision if group else 0, 'group': deepcopy(group.payload) if group else None,
        'images': deepcopy(images)}
    _image_inputs(tx, row, binding)
    if proof.payload.get('state') == 'paired':
        old = paired_input(tx, row)
        if {key: value for key, value in binding.items() if key != 'id'} != {
                key: value for key, value in old.items() if key != 'id'}:
            _fail()
        return old
    if set(proof.payload) != _BASE or proof.payload.get('state') != 'unbound':
        _fail()
    tx.put(SECTIONS, row.object_id, {**proof.payload, 'state': 'paired',
        'input_binding': binding}, expected_revision=proof.revision)
    return binding


def paired_input(reader, row):
    proof = reader.read(SECTIONS, row.object_id)
    if proof is None:
        return None
    if proof.payload.get('state') != 'paired':
        if 'input_binding' in proof.payload:
            if (set(proof.payload) != _X_BOUND or proof.payload.get('origin') != 'xiaohongshu'
                    or proof.payload.get('state') != 'bound'):
                _fail()
            paired_image_matches(reader, row, (reader.read('v2_image_reads', row.object_id).payload
                if reader.read('v2_image_reads', row.object_id) else {}))
        return None
    if (set(proof.payload) != _PAIR or proof.payload.get('item_id') != row.object_id
            or proof.payload.get('project_id') != row.payload['project_id']):
        _fail()
    _token(proof.payload['owner_birth'])
    binding = proof.payload['input_binding']
    _image_inputs(reader, row, binding)
    return deepcopy(binding)


def build_xhs_capture(reader, row, binding, media, ocr, spans, *, run_id, maximum, unavailable=False):
    """Build coordinates from the actual note and same ordered OCR projection."""
    body = media['source_text']
    section = '\n\n## 评论区\n\n'
    saved = reader.read('v2_image_reads', row.object_id)
    frozen = None
    if saved is not None and saved.payload.get('run_id') == run_id:
        frozen = {'revision': saved.revision,
            'payload': {key: deepcopy(value) for key, value in saved.payload.items()
                if key not in {'owner_revision', 'owner_status'}}, 'spans': deepcopy(spans)}
    status = 'unavailable' if unavailable else 'empty' if not ocr else 'available'
    try:
        ocr.encode('utf-8')
    except UnicodeEncodeError:
        status = 'unavailable'
    if status == 'available' and len(body) + len(section) + len(ocr) > maximum:
        status = 'budget'
    combined, comments, ocr_range, comment_section = body, [], None, None
    if status == 'available':
        start = len(body) + len(section)
        combined = body + section + ocr
        comments = [{**span, 'start': start + span['start'], 'end': start + span['end']} for span in spans]
        ocr_range = {'start': start, 'end': len(combined)}
        comment_section = {'start': len(body), 'end': len(combined)}
    return {'source_text': combined, 'origin': 'xiaohongshu', 'body': {'start': 0, 'end': len(body)},
        'comment_section': comment_section, 'comments': comments,
        'input_binding': {**deepcopy(binding), 'request_url': media['canonical_url']},
        'ocr_read': frozen, 'ocr_range': ocr_range, 'comment_status': status}


def _xhs_source(reader, row, payload):
    from ..workspace_xhs_media import _source_url
    binding = payload['input_binding']
    _image_inputs(reader, row, binding)
    _note, canonical, request = _source_url(binding['request_url'])
    if request != canonical or payload['source_text'] != row.payload['source_text']:
        _fail()
    body = payload['body']
    if (set(body) != {'start', 'end'} or type(body['start']) is not int or type(body['end']) is not int
            or body['start'] != 0 or not 0 < body['end'] <= len(payload['source_text'])):
        _fail()
    saved = reader.read('v2_image_reads', row.object_id)
    frozen = payload['ocr_read']
    if frozen is not None:
        current = saved.payload if saved else None
        if (not isinstance(frozen, Mapping) or set(frozen) != {'revision', 'payload', 'spans'}
                or type(frozen['revision']) is not int or frozen['revision'] < 1
                or saved is None or saved.revision < frozen['revision']
                or {key: value for key, value in current.items() if key not in {'owner_revision', 'owner_status'}} != frozen['payload']
                or current.get('images') != binding['images'] or current.get('run_id') != payload['capture_run_id']):
            _fail()
        previous, previous_ordinal = 0, 0
        for span in frozen['spans']:
            if (set(span) != {'ordinal', 'start', 'end'} or type(span['ordinal']) is not int
                    or type(span['start']) is not int or type(span['end']) is not int
                    or not previous_ordinal < span['ordinal'] <= len(binding['images'])
                    or not previous <= span['start'] < span['end'] <= len(current['source_text'])):
                _fail()
            previous, previous_ordinal = span['end'], span['ordinal']
    elif saved is not None and saved.payload.get('run_id') == payload['capture_run_id']:
        _fail()
    status = payload['comment_status']
    if status != 'available':
        if (status not in {'empty', 'unavailable', 'budget'} or payload['comments'] != []
                or payload['comment_section'] is not None or payload['ocr_range'] is not None
                or body['end'] != len(payload['source_text'])):
            _fail()
        return None
    if frozen is None:
        _fail()
    ocr_range = payload['ocr_range']
    if (set(ocr_range) != {'start', 'end'} or type(ocr_range['start']) is not int
            or type(ocr_range['end']) is not int
            or not body['end'] <= ocr_range['start'] < ocr_range['end'] == len(payload['source_text'])
            or payload['source_text'][ocr_range['start']:ocr_range['end']] != frozen['payload']['source_text']):
        _fail()
    ordinals = set()
    for comment in payload['comments']:
        if (set(comment) != {'ordinal', 'start', 'end'} or type(comment['ordinal']) is not int
                or not 1 <= comment['ordinal'] <= len(binding['images']) or comment['ordinal'] in ordinals):
            _fail()
        ordinals.add(comment['ordinal'])
    if payload['comments'] != [{**span, 'start': ocr_range['start'] + span['start'],
            'end': ocr_range['start'] + span['end']} for span in frozen['spans']]:
        _fail()
    return _plain_source(row, payload)


def paired_image_matches(reader, row, image):
    proof = reader.read(SECTIONS, row.object_id)
    if proof is None or proof.payload.get('origin') != 'xiaohongshu':
        return False
    if (set(proof.payload) != _X_BOUND or proof.payload.get('state') != 'bound'
            or proof.payload['item_id'] != row.object_id or proof.payload['project_id'] != row.payload['project_id']):
        _fail()
    if (type(proof.payload['capture_owner_revision']) is not int
            or not 0 < proof.payload['capture_owner_revision'] <= row.revision):
        _fail()
    _identity(proof.payload['capture_run_id'])
    _token(proof.payload['owner_birth'])
    _token(proof.payload['capture_id'])
    _model_source(row, proof.payload, reader=reader)
    frozen = proof.payload['ocr_read']
    return frozen is not None and frozen['payload'] == {
        key: value for key, value in image.items() if key not in {'owner_revision', 'owner_status'}}


def validated_xhs_text(reader, row):
    proof = reader.read(SECTIONS, row.object_id)
    if proof is None or proof.payload.get('origin') != 'xiaohongshu':
        return None
    saved = reader.read('v2_image_reads', row.object_id)
    paired_image_matches(reader, row, saved.payload if saved else {})
    return row.payload['source_text']


def _fail():
    raise RecognitionConflict('comment_source_invalid')


def _token(value):
    if (not isinstance(value, str) or len(value) != 32
            or any(char not in '0123456789abcdef' for char in value)):
        _fail()


def _identity(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(char) < 32 for char in value)):
        _fail()


def create_section_owner(tx, row):
    """Same creation UOW; replacing an ID always starts a new unbound birth."""
    old = tx.read(SECTIONS, row.object_id)
    tx.put(SECTIONS, row.object_id, {'item_id': row.object_id,
        'project_id': row.payload['project_id'], 'owner_birth': uuid4().hex,
        'state': 'unbound'}, expected_revision=old.revision if old else 0)


def trimmed_capture(raw, sections):
    """Translate the existing intake strip; never parse a heading for evidence."""
    if not isinstance(sections, Mapping) or set(sections) != _CAPTURE - {'source_text'}:
        _fail()
    result = deepcopy(dict(sections))
    source, shift = raw.strip(), len(raw) - len(raw.lstrip())
    try:
        for span in [result['body'], result['comment_section'], *result['comments']]:
            if (type(span['start']) is not int or type(span['end']) is not int
                    or not 0 <= span['start'] < span['end'] <= len(raw)):
                _fail()
            span['start'] -= shift
            span['end'] -= shift
        result['body']['start'] = max(0, result['body']['start'])
        result['comment_section']['end'] = min(len(source), result['comment_section']['end'])
    except (KeyError, TypeError):
        _fail()
    return {**result, 'source_text': source}


def _plain_source(row, payload):
    source = {'source_type': 'original_item', 'source_id': row.object_id,
        'project_id': row.payload['project_id'], 'revision': row.revision,
        'coordinate_space': 'workspace_source_text_v1', 'text': row.payload['source_text'],
        'body': payload['body'], 'comment_section': payload['comment_section'],
        'comments': [{key: comment[key] for key in ('ordinal', 'start', 'end')}
            for comment in payload['comments']]}
    _sources([source])
    return deepcopy(source)


def _model_source(row, payload, *, reader=None):
    try:
        if payload['origin'] == 'xiaohongshu':
            return _xhs_source(reader, row, payload)
        source = _plain_source(row, payload)
        comments = payload['comments']
        ids = set()
        if payload['origin'] != 'bilibili' or len(comments) > 30:
            _fail()
        for ordinal, comment in enumerate(comments, 1):
            if set(comment) != {'ordinal', 'rpid', 'like_count', 'start', 'end'}:
                _fail()
            identity = comment['rpid']
            if (comment['ordinal'] != ordinal or type(comment['like_count']) is not int
                    or comment['like_count'] < 0 or not isinstance(identity, str)
                    or not identity.isascii() or not identity.isdecimal() or int(identity) <= 0
                    or identity in ids):
                _fail()
            ids.add(identity)
        return deepcopy(source)
    except (KeyError, TypeError, ValueError):
        _fail()


def record_source_change(tx, before, after, *, capture=None, run_id=None):
    """Join the original's write: capture, or permanently invalidate changed raw."""
    proof = tx.read(SECTIONS, after.object_id)
    if capture is not None:
        xhs = isinstance(capture, Mapping) and capture.get('origin') == 'xiaohongshu'
        if (not isinstance(capture, Mapping) or set(capture) != (_X_CAPTURE if xhs else _CAPTURE)
                or before.payload.get('status') != 'processing'
                or (not xhs and (before.payload.get('platform') != 'bilibili' or before.payload.get('input_kind') != 'link'))
                or not run_id or before.payload.get('processing_run_id') != run_id
                or capture['source_text'] != after.payload.get('source_text')
                or proof is None or proof.payload.get('item_id') != after.object_id
                or proof.payload.get('project_id') != after.payload.get('project_id')):
            _fail()
        _token(proof.payload.get('owner_birth'))
        if xhs:
            pending = paired_input(tx, before)
            if pending is None or any(pending[key] != capture['input_binding'][key] for key in _INPUT - {'request_url'}):
                _fail()
            _model_source(after, {**capture, 'capture_run_id': run_id}, reader=tx)
        else:
            _model_source(after, capture)
        tx.put(SECTIONS, after.object_id, {**deepcopy(dict(capture)),
            'item_id': after.object_id, 'project_id': after.payload['project_id'],
            'owner_birth': proof.payload['owner_birth'], 'state': 'bound',
            'capture_id': uuid4().hex, 'capture_owner_revision': after.revision,
            'capture_run_id': run_id}, expected_revision=proof.revision)
    elif (proof is not None and proof.payload.get('state') == 'bound'
            and before.payload.get('source_text') != after.payload.get('source_text')):
        tx.put(SECTIONS, after.object_id, {**proof.payload, 'state': 'invalidated'},
            expected_revision=proof.revision)


def _alias(reader, scope, identity, item):
    bound = original(reader, scope, 'original_source', identity)
    payload = bound.payload
    if (payload.get('identity_method') != 'workspace_confirmation'
            or payload.get('workspace_item_id') != item.object_id
            or payload.get('metadata', {}).get('content_snapshot') != item.payload.get('source_text')):
        _fail()
    return {'id': identity, 'revision': bound.revision,
        'incarnation': payload['_original_incarnation']}


def _bound_source(reader, row, payload):
    if (row is None or payload.get('item_id') != row.object_id
            or payload.get('project_id') != row.payload.get('project_id')
            or set(payload) != (_X_BOUND if payload.get('origin') == 'xiaohongshu' else _BOUND)
            or payload.get('state') != 'bound'
            or row.payload.get('status') not in {'ready', 'confirming', 'confirmed'}
            or payload.get('source_text') != row.payload.get('source_text')
            or type(payload.get('capture_owner_revision')) is not int
            or not 0 < payload['capture_owner_revision'] <= row.revision):
        _fail()
    _token(payload.get('owner_birth'))
    _token(payload.get('capture_id'))
    _identity(payload.get('capture_run_id'))
    return _model_source(row, payload, reader=reader)


def comment_section_for_item(reader, row):
    """Project only current captured ranges; headings never establish provenance."""
    proof = reader.read(SECTIONS, row.object_id)
    if proof is None:
        return None
    payload = proof.payload
    if (payload.get('item_id') != row.object_id
            or payload.get('project_id') != row.payload.get('project_id')):
        _fail()
    _token(payload.get('owner_birth'))
    if payload.get('state') == 'unbound':
        if set(payload) != _BASE:
            _fail()
        return None
    source = _bound_source(reader, row, payload)
    if source is None:
        return None
    entries = []
    for comment in payload['comments']:
        text = source['text'][comment['start']:comment['end']]
        if not text.strip():
            continue
        entry = {'ordinal': comment['ordinal'], 'text': text}
        if payload['origin'] == 'bilibili':
            entry['like_count'] = comment['like_count']
        entries.append(entry)
    return {'origin': payload['origin'], 'count': len(entries), 'entries': entries} if entries else None


def resolve_comment_sources(records, documents, project_id, document_id, *, revision=None, reader=None):
    """Resolve actual L0 owners/confirmation aliases; snapshots only verify raw."""
    reader = records if reader is None else reader
    document_row = reader.read('documents', document_id)
    document = document_row.payload if document_row else documents.read(document_id)
    if (document is None or document.get('project_id') != project_id
            or document.get('status') == 'archived' or type(document.get('revision')) is not int
            or revision is not None and document['revision'] != revision):
        _fail()
    scope = WorkScope('local-user', project_id)
    # Keep historical ordinary documents' existing optional-source behavior.
    try:
        roots = document_roots(reader, scope, document.get('source_refs', []), optional=True)
    except RecognitionConflict:
        # A document filed from another project keeps its source refs, whose
        # original stays in the source scope; it has no comment section here.
        if reader.read('v2_document_filings', document_id) is None:
            raise
        roots = ()
    owners, aliases = {}, {}
    for kind, identity, _revision in roots:
        if kind == 'original_item':
            owners[identity] = reader.read('workspace_items', identity)
        else:
            resolved = original(reader, scope, kind, identity)
            item_id = resolved.payload.get('workspace_item_id')
            if resolved.payload.get('identity_method') == 'workspace_confirmation':
                owners[item_id] = reader.read('workspace_items', item_id)
                aliases.setdefault(item_id, set()).add(identity)
    sources, bindings = [], []
    for identity, row in sorted(owners.items()):
        proof = reader.read(SECTIONS, identity)
        if proof is None:
            continue
        payload = proof.payload
        if (payload.get('item_id') != identity or payload.get('project_id') != project_id):
            _fail()
        _token(payload.get('owner_birth'))
        if payload.get('state') == 'unbound':
            if set(payload) != _BASE:
                _fail()
            continue
        if (row is None or row.payload.get('project_id') != project_id
                or row.payload.get('status') != 'confirmed'
                or row.payload.get('document_id') != document_id):
            _fail()
        source = _bound_source(reader, row, payload)
        alias_ids = aliases.get(identity, set())
        if row.payload.get('source_id'):
            alias_ids = alias_ids | {row.payload['source_id']}
        alias_inputs = [_alias(reader, scope, alias_id, row) for alias_id in sorted(alias_ids)]
        if source is not None:
            sources.append(source)
        bindings.append({'item_id': identity, 'owner_revision': row.revision,
            'section_revision': proof.revision, 'owner_birth': payload['owner_birth'],
            'capture_id': payload['capture_id'], 'capture_owner_revision': payload['capture_owner_revision'],
            'aliases': alias_inputs})
    return {'document': {'id': document_id, 'revision': document['revision']},
        'sources': sources, 'bindings': bindings}


def validate_comment_inputs(records, documents, project_id, frozen, *, reader=None):
    reader = records if reader is None else reader
    document_id, revision = frozen['document']['id'], frozen['document']['revision']
    if type(revision) is not int or revision < 1:
        _fail()
    ordinary = frozen['sources'] == [] and frozen['bindings'] == []
    current = resolve_comment_sources(records, documents, project_id, document_id,
        revision=None if ordinary else revision, reader=reader)
    if ordinary and current['sources'] == [] and current['bindings'] == [] and current['document']['revision'] != revision:
        # Ordinary drafts already retain their original revision during a model
        # call. A later edit cannot turn that historical body into comment proof.
        key = f'{document_id}~r{revision}'
        retained = reader.read('document_revisions', key)
        markdown = reader.read('document_markdown', key)
        if (retained is None or markdown is None
                or any(row.payload.get('document_id') != document_id
                    or type(row.payload.get('revision')) is not int
                    or row.payload['revision'] != revision for row in (retained, markdown))
                or not isinstance(markdown.payload.get('markdown'), str)):
            _fail()
        current['document']['revision'] = revision
    if current != frozen:
        _fail()


def freeze_comment_inputs(records, documents, project_id, turn_id, inputs):
    """Freeze before dispatch; retries never replace an existing proof binding."""
    with records.begin() as tx:
        validate_comment_inputs(records, documents, project_id, inputs, reader=tx)
        saved = tx.read(EXTRACT_INPUTS, turn_id)
        if saved is None:
            saved = tx.put(EXTRACT_INPUTS, turn_id, inputs, expected_revision=0)
        elif saved.payload != inputs:
            _fail()
        tx.commit()
    return deepcopy(saved.payload)


def record_comment_candidate(tx, candidate_id, project_id, generation_id, turn_id, hint, frozen):
    """Join propose_in_uow without extending the existing relation hint shape."""
    tx.put(CANDIDATES, candidate_id, {'project_id': project_id, 'generation_id': generation_id,
        'extract_turn_id': turn_id, 'comment_source': deepcopy(hint['comment_source']),
        'comparison_source': deepcopy(hint['comparison_source']),
        'source_bindings': deepcopy(frozen['bindings'])}, expected_revision=0)


def _exact(left, right):
    """JSON equality that cannot substitute a boolean for an identity integer."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_exact(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_exact(a, b) for a, b in zip(left, right))
    return left == right


def _comment_generation(reader, turn_id):
    """Read the existing MemoryTurn authority without constructing its runtime."""
    path = getattr(reader, 'database_path', None)
    if path is None:
        path = next(row[2] for row in reader.connection.execute('PRAGMA database_list') if row[1] == 'main')
    path = Path(path)
    root = path.parent / '.rebuild-data' if path.name == 'recognition.sqlite3' else path.parent
    database = root / 'ai-turns.sqlite3'
    if not database.is_file():
        return None
    def open_readonly():
        return observe_connection(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
    connection = borrow_read_connection(database, open_readonly)
    try:
        connection.execute('BEGIN')
        request = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (turn_id,)).fetchone()
        output = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
            (turn_id, 'memory-generation-output-v1')).fetchone()
        return (json.loads(request[0]), json.loads(output[0])) if request and output else None
    finally:
        connection.close()


def comment_source_for_candidate(reader, scope, candidate):
    """Qualify local pending provenance against actual @3 and current L0 births."""
    saved = reader.read(CANDIDATES, candidate.object_id)
    if saved is None or candidate.payload.get('state') != 'pending':
        return None
    try:
        payload = saved.payload
        if (set(payload) != {'project_id', 'generation_id', 'extract_turn_id', 'comment_source',
                'comparison_source', 'source_bindings'} or payload['project_id'] != scope.project_id
                or candidate.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}
                or candidate.payload.get('generation', {}).get('id') != payload['generation_id']):
            return None
        turn_id = payload['extract_turn_id']
        _identity(turn_id)
        immutable = _comment_generation(reader, turn_id)
        index = reader.read('v2_memory_turn_keys', turn_id)
        inputs = reader.read(EXTRACT_INPUTS, turn_id)
        comparative = reader.read('v2_extract_inputs', turn_id)
        relation = reader.read('v2_candidate_hints', candidate.object_id)
        if immutable is None or index is None or inputs is None or comparative is None or relation is None:
            return None
        request, generated = immutable
        if (request['turn_id'] != turn_id or request['desired_outcome'] != 'memory.propose_insights'
                or request['scope']['project_id'] != scope.project_id
                or request.get('policy_versions', {}).get('extract') not in {'@3', '@4', '@5'}
                or not _exact(request, index.payload['request'])
                or index.payload['identity']['kind'] != 'memory.propose_insights'
                or index.payload['identity']['project'] != scope.project_id
                or generated['metadata']['generation_id'] != payload['generation_id']):
            return None
        frozen = inputs.payload
        if set(frozen) != {'document', 'sources', 'bindings'} or set(frozen['document']) != {'id', 'revision'}:
            return None
        _identity(frozen['document']['id'])
        if type(frozen['document']['revision']) is not int or frozen['document']['revision'] < 1:
            return None
        documents = SimpleNamespace(read=lambda identity: source_store(reader).read('documents', identity))
        current = resolve_comment_sources(reader, documents, scope.project_id, frozen['document']['id'],
            revision=frozen['document']['revision'])
        if not _exact(current, frozen) or not _exact(payload['source_bindings'], frozen['bindings']):
            return None
        refs = request['privacy']['material_refs']
        if any(not any(_exact(ref, {'type': source['source_type'], 'id': source['source_id'],
                'project_id': source['project_id'], 'revision': source['revision']}) for ref in refs)
                for source in frozen['sources']):
            return None
        from .policies import get
        from .policies.extract_comments import CommentExtractDecodeInput
        decoded = get('extract', version=request['policy_versions']['extract']).decide(CommentExtractDecodeInput(generated['output'],
            normalize_conditions, comparative.payload['neighbors'], comparative.payload['projects'],
            scope.project_id, frozen['sources']))
        if not decoded.valid:
            return None
        prefix = f"candidate-v2-{frozen['document']['id']}-r{frozen['document']['revision']}-"
        for ordinal, ((text, conditions), hint) in enumerate(zip(decoded.rows, decoded.hints), 1):
            if (candidate.object_id == prefix + str(ordinal) and candidate.payload.get('content') == text
                    and _exact(candidate.payload.get('conditions'), list(conditions))
                    and _exact(relation.payload, {'project_id': scope.project_id,
                        **{key: hint[key] for key in ('relation', 'target_id', 'scope_hint')}})
                    and _exact(hint.get('comment_source'), payload['comment_source'])
                    and _exact(hint.get('comparison_source'), payload['comparison_source'])):
                return deepcopy(payload['comment_source'])
    except (RecognitionConflict, KeyError, TypeError, ValueError, AttributeError, OSError, sqlite3.Error):
        return None
    return None
