"""Read exact filing facts; retained-source permission stays with its origin owner."""
from collections.abc import Mapping
import re

from backend.recognition.experience_origins import COPY_ID, ExperienceOriginError, read_experience_origin
from core.document_engine.runtime import _blocks_from_markdown

COLLECTION = 'v2_document_filings'
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
FILED_ID = re.compile(r'^document-filed-v2-[0-9a-f]{32}-[0-9a-f]{12}$')
_EDIT_ID = re.compile(r'^experience-legacy-document-filed-v2-[0-9a-f]{32}-[0-9a-f]{12}-r[1-9][0-9]*$')
_FIELDS = {'user_id', 'source_project_id', 'source_document_id', 'source_document_revision',
    'target_project_id', 'target_document_id', 'target_document_revision', 'target_experience_id',
    'target_experience_revision', 'prior_recall', 'state', 'scene'}
FILED_EDIT_OWNER_FIELDS = frozenset({'filing_document_id', 'filing_revision', 'document_revision',
    'document_revision_record_revision', 'document_markdown_revision', 'copied_experience_id',
    'copied_experience_revision'})
FILED_EDIT_FIELDS = FILED_EDIT_OWNER_FIELDS | {'source_snapshot'}


class DocumentFilingError(ValueError):
    pass


def _fail():
    raise DocumentFilingError('document filing source is unavailable or changed')


def _positive(value):
    return type(value) is int and 0 < value <= 9223372036854775807


def validate_filed_edit_owner(value):
    if not isinstance(value, Mapping) or set(value) != FILED_EDIT_OWNER_FIELDS:
        _fail()
    for field, item in value.items():
        if field in {'filing_document_id', 'copied_experience_id'}:
            if not isinstance(item, str) or not _ID.fullmatch(item):
                _fail()
        elif not _positive(item):
            _fail()
    if not FILED_ID.fullmatch(value['filing_document_id']) or not COPY_ID.fullmatch(value['copied_experience_id']):
        _fail()


def _document(reader, identity, project):
    row = reader.read('documents', identity)
    if (row is None or row.payload.get('id') != identity or row.payload.get('project_id') != project
            or not _positive(row.payload.get('revision'))):
        _fail()
    return row


def _historical(reader, identity, revision):
    key = identity + '~r' + str(revision)
    record, body = reader.read('document_revisions', key), reader.read('document_markdown', key)
    if (record is None or body is None
            or record.revision != 1 or body.revision != 1
            or record.payload.get('document_id') != identity or body.payload.get('document_id') != identity
            or type(record.payload.get('revision')) is not int or record.payload['revision'] != revision
            or type(body.payload.get('revision')) is not int or body.payload['revision'] != revision
            or not isinstance(body.payload.get('markdown'), str)
            or not isinstance(record.payload.get('source_snapshot'), Mapping)):
        _fail()
    refs = record.payload['source_snapshot'].get('source_refs')
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, Mapping) for ref in refs):
        _fail()
    return body.payload['markdown'], refs


def _target_lineage(reader, target, initial_revision, initial_refs, *, revision=None):
    current = target.payload['revision']
    revision = current if revision is None else revision
    if (type(revision) is not int or not initial_revision <= revision <= current
            or target.revision != current):
        _fail()
    previous = None
    for number in range(initial_revision, revision + 1):
        body, refs = _historical(reader, target.object_id, number)
        record = reader.read('document_revisions', f'{target.object_id}~r{number}')
        markdown = reader.read('document_markdown', f'{target.object_id}~r{number}')
        value = record.payload
        if refs != initial_refs or value.get('new_content_hash') != markdown.payload.get('content_hash'):
            _fail()
        if previous is not None:
            blocks = _blocks_from_markdown(body, source_refs=(), origin='user', edited_by_user=True)
            if (type(value.get('parent_revision')) is not int or value['parent_revision'] != number - 1
                    or value.get('operation') != 'user_edit' or value.get('author') != 'user'
                    or value.get('base_content_hash') != previous.payload['new_content_hash']
                    or value.get('changed_blocks') != [
                        {'operation': 'update', 'block_id': block['id'], 'block': block} for block in blocks]):
                _fail()
        previous = record
    if revision == current:
        if (target.payload.get('source_refs') != refs
                or target.payload.get('source_snapshot') != previous.payload['source_snapshot']
                or target.payload.get('content_hash') != previous.payload['new_content_hash']):
            _fail()
        if current > initial_revision and target.payload.get('blocks') != blocks:
            _fail()
    return body, record, markdown


def _edited_ref(payload, scope, document_id, revision, content):
    provenance = payload.get('provenance')
    return (payload.get('id') == f'experience-legacy-{document_id}-r{revision}'
        and payload.get('scope') == {'user_id': scope.user_id, 'project_id': scope.project_id}
        and payload.get('content') == content.strip() and isinstance(provenance, Mapping)
        and provenance.get('kind') == 'user_statement' and provenance.get('actor') == 'local-user'
        and provenance.get('source_refs') == [{'type': 'document', 'id': document_id, 'revision': revision}]
        and type(provenance['source_refs'][0]['revision']) is int)


def read_filed_edit_dependencies(reader, scope, payload, *, experience_id):
    """Bind a real user edit to its immutable history and initial copied root."""
    provenance = payload.get('provenance')
    refs = provenance.get('source_refs') if isinstance(provenance, Mapping) else None
    if (not isinstance(refs, (list, tuple)) or len(refs) != 1 or not isinstance(refs[0], Mapping)
            or not isinstance(refs[0].get('id'), str) or not FILED_ID.fullmatch(refs[0]['id'])):
        if _EDIT_ID.fullmatch(experience_id) or isinstance(payload.get('id'), str) and _EDIT_ID.fullmatch(payload['id']):
            _fail()
        return None
    ref = refs[0]
    if set(ref) != {'type', 'id', 'revision'} or ref['type'] != 'document' or not _positive(ref['revision']):
        _fail()
    copied = filing_experience(reader, scope, ref['id'])
    marker = reader.read(COLLECTION, ref['id'])
    if marker is None or ref['revision'] <= marker.payload['target_document_revision']:
        _fail()
    target = _document(reader, ref['id'], scope.project_id)
    _, initial_refs = _historical(reader, ref['id'], marker.payload['target_document_revision'])
    body, record, markdown = _target_lineage(reader, target, marker.payload['target_document_revision'],
        initial_refs, revision=ref['revision'])
    if payload.get('id') != experience_id or not _edited_ref(payload, scope, ref['id'], ref['revision'], body):
        _fail()
    return copied, {'filing_document_id': ref['id'], 'filing_revision': marker.revision,
        'document_revision': ref['revision'], 'document_revision_record_revision': record.revision,
        'document_markdown_revision': markdown.revision, 'copied_experience_id': copied.object_id,
        'copied_experience_revision': copied.revision}


def _marker_value(marker, scope, document_id):
    value = marker.payload
    ids = {'user_id', 'source_project_id', 'source_document_id', 'target_project_id',
           'target_document_id', 'target_experience_id'}
    if (set(value) != _FIELDS
            or any(not isinstance(value[key], str) or not _ID.fullmatch(value[key]) for key in ids)
            or any(not _positive(value[key]) for key in ('source_document_revision',
                'target_document_revision', 'target_experience_revision'))
            or not FILED_ID.fullmatch(document_id)
            or value['user_id'] != scope.user_id or value['target_project_id'] != scope.project_id
            or value['target_document_id'] != marker.object_id or marker.object_id != document_id
            or value['source_project_id'] == scope.project_id or value['source_document_id'] == document_id
            or not isinstance(value['state'], str) or value['state'] not in {'filed', 'undone'}
            or (value['prior_recall'] is not None and not isinstance(value['prior_recall'], Mapping))
            or (value['scene'] is not None and (not isinstance(value['scene'], str)
                or not value['scene'].strip() or len(value['scene']) > 160))):
        _fail()
    return value


def filing_document_candidate(marker, scope, document):
    """Match only discovery metadata; selected material still needs full proof."""
    try:
        value = _marker_value(marker, scope, marker.object_id)
    except DocumentFilingError:
        return False
    return (document is not None and document.object_id == marker.object_id
        and document.payload.get('id') == marker.object_id
        and document.payload.get('project_id') == scope.project_id
        and type(document.payload.get('revision')) is int
        and document.payload['revision'] >= value['target_document_revision']
        and document.payload.get('status') != 'archived')


def filing_experience(reader, scope, document_id, *, _trail=()):
    """Return the already-created copy bound to this exact document revision.

    The leaf never grants permission, creates an experience, or writes a fact.
    Callers send this root through the existing SourceEgress/Recognition owners.
    """
    if hasattr(reader, '_connect'):
        with reader.begin() as tx:
            return filing_experience(tx, scope, document_id, _trail=_trail)
    marker = reader.read(COLLECTION, document_id)
    if marker is None:
        if isinstance(document_id, str) and FILED_ID.fullmatch(document_id):
            _fail()
        return None
    value = _marker_value(marker, scope, document_id)
    visit = (scope.user_id, scope.project_id, document_id)
    if visit in _trail or len(_trail) >= 256:
        _fail()
    target = _document(reader, document_id, scope.project_id)
    if target.payload['revision'] < value['target_document_revision'] or target.payload.get('status') == 'archived':
        _fail()
    source = _document(reader, value['source_document_id'], value['source_project_id'])
    if source.payload['revision'] < value['source_document_revision']:
        _fail()
    source_body, source_refs = _historical(reader, value['source_document_id'], value['source_document_revision'])
    target_body, target_refs = _historical(reader, document_id, value['target_document_revision'])
    if (source_body != target_body or source_refs != target_refs
            or target.payload.get('source_refs') != target_refs):
        _fail()
    _target_lineage(reader, target, value['target_document_revision'], target_refs)
    copied = reader.read('recognition_experiences', value['target_experience_id'])
    if (copied is None or copied.revision != value['target_experience_revision']
            or copied.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}
            or copied.payload.get('state') != 'active' or copied.payload.get('content') != target_body.strip()):
        _fail()
    try:
        origin = read_experience_origin(reader, copied)
    except ExperienceOriginError as error:
        raise DocumentFilingError('document filing origin changed') from error
    if origin is None:
        _fail()
    origin_marker, original = origin
    if origin_marker.payload['source_project_id'] != value['source_project_id']:
        _fail()
    source_scope = type(scope)(scope.user_id, value['source_project_id'])
    source_copy = filing_experience(reader, source_scope, value['source_document_id'], _trail=(*_trail, visit))
    if source_copy is not None:
        if original != source_copy and not _edited_ref(original.payload, source_scope,
                value['source_document_id'], value['source_document_revision'], source_body):
            _fail()
    else:
        provenance = original.payload.get('provenance')
        if not isinstance(provenance, Mapping):
            _fail()
        refs = provenance.get('source_refs', [])
        if not isinstance(refs, (list, tuple)):
            _fail()
        if not any(isinstance(ref, Mapping) and set(ref) == {'type', 'id', 'revision'}
                and ref['type'] == 'document' and ref['id'] == value['source_document_id']
                and type(ref['revision']) is int and ref['revision'] == value['source_document_revision'] for ref in refs):
            _fail()
    return copied
