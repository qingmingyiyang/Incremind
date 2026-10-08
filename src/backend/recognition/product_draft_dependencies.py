"""Read completed root product drafts from their existing operation records."""
from collections.abc import Mapping
from dataclasses import dataclass
import re

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
PRODUCT_DRAFT_OWNER_FIELDS = frozenset({'product_turn_id', 'task_draft_operation_id',
    'task_draft_operation_revision', 'task_execution_id', 'task_execution_revision',
    'product_turn_record_revision', 'document_revision', 'document_revision_record_revision',
    'document_markdown_revision'})
PRODUCT_DRAFT_FIELDS = PRODUCT_DRAFT_OWNER_FIELDS | {'current_source_graph'}


class ProductDraftDependencyError(ValueError):
    pass


@dataclass(frozen=True)
class ProductDraftDependencies:
    roots: tuple
    revisions: Mapping
    snapshots: tuple
    request: Mapping


def _revision(value):
    if type(value) is not int or value < 1:
        raise ProductDraftDependencyError('product draft revision is invalid')
    return value


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProductDraftDependencyError('product draft identity is invalid')
    return value


def _mapping(value):
    if not isinstance(value, Mapping):
        raise ProductDraftDependencyError('product draft owner evidence is invalid')
    return value


def product_draft_source(reader, scope, document_id, revision, *, turn_id=None):
    """Return only a bound final root draft; worker drafts have no root authority."""
    document_id = _identity(document_id)
    document = reader.read('documents', document_id)
    if document is None or not str(document.payload.get('type', '')).startswith('agent-result-turn-'):
        return None
    _revision(revision)
    if scope.user_id != 'local-user' or document.payload.get('project_id') != scope.project_id:
        raise ProductDraftDependencyError('product draft scope is unavailable')
    historical = reader.read('document_revisions', f'{document_id}~r{revision}')
    markdown = reader.read('document_markdown', f'{document_id}~r{revision}')
    refs = _mapping(historical.payload.get('source_snapshot')).get('source_refs') if historical else None
    if (not historical or not markdown or historical.payload.get('document_id') != document_id
            or _revision(historical.payload.get('revision')) != revision or markdown.payload.get('document_id') != document_id
            or _revision(markdown.payload.get('revision')) != revision or not isinstance(refs, list) or len(refs) != 1
            or not isinstance(refs[0], Mapping) or set(refs[0]) != {'source_id', 'locator'}):
        raise ProductDraftDependencyError('product draft historical evidence is unavailable')
    identity = _identity(refs[0]['source_id'])
    if (refs[0]['locator'] != 'task://' + identity or turn_id is not None and turn_id != identity
            or document.payload['type'] != 'agent-result-' + identity):
        raise ProductDraftDependencyError('product draft Turn binding is invalid')
    operation_id = 'deliver-' + identity
    operation = reader.read('v2_task_draft_operations', operation_id)
    matches = [row for row in reader.list('v2_task_executions')
        if isinstance(row.payload.get('request'), Mapping) and row.payload['request'].get('turn_id') == identity]
    if operation is None or len(matches) != 1:
        raise ProductDraftDependencyError('product draft execution is unavailable')
    # Only the original unique match is a selected authority row. Point-read it
    # through the caller's reader; unrelated list results remain unselected.
    execution = reader.read('v2_task_executions', matches[0].object_id)
    if execution is None:
        raise ProductDraftDependencyError('product draft execution is unavailable')
    execution_id = _identity(execution.object_id)
    turn = reader.read('v2_turns', execution_id)
    request = _mapping(execution.payload['request'])
    inputs, result = operation.payload.get('inputs'), operation.payload.get('result')
    receipt = _mapping(_mapping(turn.payload.get('receipt')).get('do')) if turn else {}
    if (not isinstance(inputs, Mapping) or set(inputs) != {'turn_id', 'project_id', 'title', 'markdown'}
            or inputs['turn_id'] != identity or inputs['project_id'] != scope.project_id
            or not isinstance(inputs['title'], str) or not inputs['title'].strip()
            or inputs['markdown'] != markdown.payload.get('markdown')
            or not isinstance(result, Mapping) or set(result) != {'document_id', 'document_revision', 'receipt_ref'}
            or result['document_id'] != document_id or _revision(result['document_revision']) != revision
            or not isinstance(result['receipt_ref'], str)
            or not result['receipt_ref'].endswith('/v2_task_draft_operations/' + operation_id)
            or execution.payload.get('project_id') != scope.project_id or execution.payload.get('started') is not True
            or turn is None or turn.payload.get('project_id') != scope.project_id or turn.payload.get('intent') != 'do'
            or receipt.get('state') != 'done' or receipt.get('document_id') != document_id
            or receipt.get('kernel_turn_id') != identity or receipt.get('title') != inputs['title']
            or request.get('scope') != {'kind': 'project', 'project_id': scope.project_id, 'series_id': None}
            or request.get('desired_outcome') != 'project.task'):
        raise ProductDraftDependencyError('product draft completed evidence is invalid')
    privacy = _mapping(request.get('privacy'))
    snapshots, materials = privacy.get('source_snapshots'), privacy.get('material_refs')
    if not isinstance(snapshots, list) or not isinstance(materials, list) or len(snapshots) != len(materials):
        raise ProductDraftDependencyError('product draft frozen sources are invalid')
    roots, input_refs = [], []
    for material, snapshot in zip(materials, snapshots):
        if (not isinstance(material, Mapping) or set(material) != {'type', 'id', 'revision', 'project_id'}
                or material['type'] not in {'recognition', 'experience', 'original_item', 'original_source'}
                or material['project_id'] not in {scope.project_id, 'me'}
                or not isinstance(snapshot, Mapping)
                or set(snapshot) != {'schema_version', 'scope', 'privacy_revision', 'roots', 'nodes'}
                or type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1
                or snapshot['scope'] != {'user_id': scope.user_id, 'project_id': material['project_id']}
                or snapshot['roots'] != [{'type': material['type'], 'id': material['id'], 'revision': material['revision']}]
                or type(snapshot['privacy_revision']) is not int or snapshot['privacy_revision'] < 0):
            raise ProductDraftDependencyError('product draft frozen source identity is invalid')
        item, version = _identity(material['id']), _revision(material['revision'])
        if type(snapshot['roots'][0]['revision']) is not int:
            raise ProductDraftDependencyError('product draft frozen source revision is invalid')
        kind = material['type']
        collection = {'recognition': 'recognitions', 'experience': 'recognition_experiences',
                      'original_item': 'workspace', 'original_source': 'sources'}[kind]
        roots.append((material['project_id'], kind, item, version))
        input_refs.append({'kind': 'atom' if kind in {'recognition', 'experience'} else 'source',
                           'object_id': item, 'uri': 'crp://default/' + collection + '/' + item})
    if _mapping(request.get('input')).get('refs') != input_refs or len(roots) != len(set(roots)):
        raise ProductDraftDependencyError('product draft frozen input references changed')
    return ProductDraftDependencies(tuple(roots), {
        'product_turn_id': identity, 'task_draft_operation_id': operation_id,
        'task_draft_operation_revision': _revision(operation.revision), 'task_execution_id': execution_id,
        'task_execution_revision': _revision(execution.revision), 'product_turn_record_revision': _revision(turn.revision),
        'document_revision': revision, 'document_revision_record_revision': _revision(historical.revision),
        'document_markdown_revision': _revision(markdown.revision)}, tuple(snapshots), request)


def read_product_draft_dependencies(reader, scope, payload):
    provenance = payload.get('provenance')
    if not isinstance(provenance, Mapping) or provenance.get('kind') != 'model_generated_artifact':
        return None
    refs = provenance.get('source_refs')
    if not isinstance(refs, list) or {row.get('type') for row in refs if isinstance(row, Mapping)} != {'turn', 'document'}:
        return None
    if (len(refs) != 2 or provenance.get('actor') != 'system'
            or provenance.get('artifact_status') != 'committed' or provenance.get('outcome_status') != 'unknown'):
        raise ProductDraftDependencyError('product draft provenance is invalid')
    turn, document = sorted(refs, key=lambda row: row['type'], reverse=True)
    if (set(turn) != {'type', 'id'} or set(document) != {'type', 'id', 'revision'}):
        raise ProductDraftDependencyError('product draft provenance references are invalid')
    bound = product_draft_source(reader, scope, _identity(document['id']),
        _revision(document['revision']), turn_id=_identity(turn['id']))
    if bound is None:
        raise ProductDraftDependencyError('product draft is unavailable')
    return bound
