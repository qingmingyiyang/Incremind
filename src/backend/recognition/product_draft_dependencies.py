"""Read completed root product drafts from their existing operation records."""
from collections.abc import Mapping
from dataclasses import dataclass
import json
import re

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
PRODUCT_DRAFT_OWNER_FIELDS = frozenset({'product_turn_id', 'task_draft_operation_id',
    'task_draft_operation_revision', 'task_execution_id', 'task_execution_revision',
    'product_turn_record_revision', 'document_revision', 'document_revision_record_revision',
    'document_markdown_revision'})
PRODUCT_DRAFT_FIELDS = PRODUCT_DRAFT_OWNER_FIELDS | {'current_source_graph'}
RESTORATIONS = 'v2_outcome_restorations'
RESTORED_DRAFT_OWNER_FIELDS = PRODUCT_DRAFT_OWNER_FIELDS | {
    'restoration_id', 'restoration_revision', 'source_document_id', 'source_document_revision',
    'source_revision_record_revision', 'source_markdown_record_revision'}
RESTORED_DRAFT_FIELDS = RESTORED_DRAFT_OWNER_FIELDS | {'current_source_graph'}
ANSWER_OUTCOMES = 'v2_answer_outcomes'
ANSWER_DRAFT_OWNER_FIELDS = frozenset({'answer_turn_id', 'answer_turn_record_revision', 'answer_save_revision',
    'document_revision', 'document_revision_record_revision', 'document_markdown_revision'})
ANSWER_DRAFT_FIELDS = ANSWER_DRAFT_OWNER_FIELDS | {'current_source_graph'}
RESTORED_ANSWER_OWNER_FIELDS = ANSWER_DRAFT_OWNER_FIELDS | (RESTORED_DRAFT_OWNER_FIELDS - PRODUCT_DRAFT_OWNER_FIELDS)
RESTORED_ANSWER_FIELDS = RESTORED_ANSWER_OWNER_FIELDS | {'current_source_graph'}
DRAFT_OWNER_FIELD_SETS = (PRODUCT_DRAFT_OWNER_FIELDS, RESTORED_DRAFT_OWNER_FIELDS,
                        ANSWER_DRAFT_OWNER_FIELDS, RESTORED_ANSWER_OWNER_FIELDS)
DRAFT_DEPENDENCY_FIELDS = (PRODUCT_DRAFT_FIELDS, RESTORED_DRAFT_FIELDS, ANSWER_DRAFT_FIELDS, RESTORED_ANSWER_FIELDS)


class ProductDraftDependencyError(ValueError):
    pass


@dataclass(frozen=True)
class ProductDraftDependencies:
    roots: tuple
    revisions: Mapping
    snapshots: tuple
    request: Mapping
    originals: tuple = ()
    answers: tuple = ()
    restoration: Mapping | None = None
    source: object = None
    answer_birth: Mapping | None = None
    material_documents: tuple = ()
    qualified_materials: tuple = ()
    material_proof: Mapping | None = None

    @property
    def public_turn_id(self):
        return self.revisions.get('answer_turn_id') or self.revisions['task_execution_id']

    @property
    def answer_origin(self):
        bound = self
        while bound.restoration is not None:
            bound = bound.source
        return bound.answer_birth


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
    return _retained_source(reader, scope, document_id, revision, turn_id=turn_id, seen=())


def _retained_source(reader, scope, document_id, revision, *, turn_id=None, seen=()):
    document = reader.read('documents', _identity(document_id))
    if document is not None and str(document.payload.get('type', '')).startswith('agent-result-answer-'):
        return _answer_source(reader, scope, document, revision, turn_id=turn_id)
    if document is not None and str(document.payload.get('type', '')).startswith('agent-result-restore-'):
        return _restored_source(reader, scope, document, revision, turn_id=turn_id, seen=seen)
    return _task_draft_source(reader, scope, document_id, revision, turn_id=turn_id)


def _historical(reader, identity, revision):
    _revision(revision)
    key = f'{identity}~r{revision}'
    record, markdown = reader.read('document_revisions', key), reader.read('document_markdown', key)
    if (record is None or markdown is None or record.payload.get('document_id') != identity
            or type(record.payload.get('revision')) is not int or record.payload['revision'] != revision
            or markdown.payload.get('document_id') != identity or type(markdown.payload.get('revision')) is not int
            or markdown.payload['revision'] != revision or not isinstance(markdown.payload.get('markdown'), str)):
        raise ProductDraftDependencyError('restored document history is unavailable')
    return record, markdown


def _admitted_input_refs(reader, request, execution):
    if execution.payload.get('admission_bundle') is None:
        return request['input']['refs']
    from core.ai_kernel.product_snapshot import read_admission_base_refs
    from .external_turn_facts import _readonly, source_store
    try:
        with _readonly(source_store(reader).root / 'ai-turns.sqlite3') as connection:
            return read_admission_base_refs(connection, request)
    except (ValueError, KeyError, TypeError) as error:
        raise ProductDraftDependencyError('product draft native admission changed') from error


def _restored_source(reader, scope, document, revision, *, turn_id, seen):
    identity = document.object_id
    if identity in seen or len(seen) >= 256 or revision != 1 or scope.user_id != 'local-user':
        raise ProductDraftDependencyError('restored document birth is invalid')
    record, markdown = _historical(reader, identity, revision)
    refs = _mapping(record.payload.get('source_snapshot')).get('source_refs')
    if (document.payload.get('project_id') != scope.project_id or not isinstance(refs, list) or len(refs) != 1
            or not isinstance(refs[0], Mapping) or set(refs[0]) != {'source_id', 'locator'}):
        raise ProductDraftDependencyError('restored document scope is unavailable')
    operation = _identity(refs[0]['source_id'])
    marker = reader.read(RESTORATIONS, operation)
    fields = {'project_id', 'operation_id', 'document_id', 'source_document_id', 'source_revision',
        'source_birth_revision', 'source_lineage', 'previous_id', 'previous_revision', 'root_id',
        'version', 'turn_id', 'scene', 'task_text', 'source_refs', 'changes', 'created_at', 'cited_refs'}
    value = marker.payload if marker is not None else {}
    if (marker is None or marker.revision != 1 or set(value) != fields
            or value['operation_id'] != operation or not operation.startswith('restore-')
            or refs[0]['locator'] != 'outcome://' + operation
            or document.payload.get('type') != 'agent-result-' + operation
            or value['project_id'] != scope.project_id or value['document_id'] != identity):
        raise ProductDraftDependencyError('restored document operation is unavailable')
    source_id = _identity(value['source_document_id'])
    if _revision(value['source_birth_revision']) > _revision(value['source_revision']):
        raise ProductDraftDependencyError('restored document source revision is invalid')
    source = reader.read('documents', source_id)
    source_record, source_markdown = _historical(reader, source_id, value['source_revision'])
    source_lineage = reader.read('v2_outcome_lineage', source_id)
    previous_id = _identity(value['previous_id'])
    previous = reader.read('documents', previous_id)
    _historical(reader, previous_id, value['previous_revision'])
    if (source is None or source.payload.get('project_id') != scope.project_id
            or source_lineage is None or source_lineage.revision != 1
            or source_lineage.payload != value['source_lineage']
            or previous is None or previous.payload.get('project_id') != scope.project_id
            or _mapping(source_record.payload.get('source_snapshot')).get('source_refs') != value['source_refs']
            or source_markdown.payload['markdown'] != markdown.payload['markdown']
            or not isinstance(value['created_at'], str) or not value['created_at']
            or type(value['version']) is not int or value['version'] < 2
            or not isinstance(value['changes'], list) or not isinstance(value['cited_refs'], list)):
        raise ProductDraftDependencyError('restored document source changed')
    bound = _retained_source(reader, scope, source_id, value['source_birth_revision'],
        turn_id=turn_id, seen=(*seen, identity))
    if bound is None or value['turn_id'] != bound.public_turn_id:
        raise ProductDraftDependencyError('restored document source birth is unavailable')
    owner_fields = ANSWER_DRAFT_OWNER_FIELDS if bound.answer_origin is not None else PRODUCT_DRAFT_OWNER_FIELDS
    revisions = {field: bound.revisions[field] for field in owner_fields}
    revisions.update(document_revision=1, document_revision_record_revision=_revision(record.revision),
        document_markdown_revision=_revision(markdown.revision), restoration_id=operation,
        restoration_revision=marker.revision, source_document_id=source_id,
        source_document_revision=_revision(value['source_revision']),
        source_revision_record_revision=_revision(source_record.revision),
        source_markdown_record_revision=_revision(source_markdown.revision))
    return ProductDraftDependencies(bound.roots, revisions, bound.snapshots, bound.request,
        bound.originals, bound.answers, value, bound, material_documents=bound.material_documents,
        qualified_materials=bound.qualified_materials, material_proof=bound.material_proof)


def _shared_task_materials(reader, request):
    from core.ai_kernel.product_admission import admission_identity
    from core.ai_kernel.product_snapshot import read_product_turn_snapshot, restore_admission_snapshot
    from .external_turn_facts import _readonly, source_store
    with _readonly(source_store(reader).root / 'ai-turns.sqlite3') as connection:
        snapshot = read_product_turn_snapshot(connection, request['turn_id'])
    actual = snapshot['request']
    if admission_identity(actual) != admission_identity(request):
        raise ProductDraftDependencyError('product draft material native request changed')
    if 'material-origin-v1' not in snapshot['immutable']:
        if 'shared-task-materials-v1' in snapshot['immutable']:
            raise ProductDraftDependencyError('product draft material origin unavailable')
        return None
    bundle = restore_admission_snapshot(actual, snapshot['immutable'])
    selected = [item for item in bundle.items if item.kind == 'shared-task-materials-v1']
    origins = [item for item in bundle.items if item.kind == 'material-origin-v1']
    if len(selected) != 1 or len(origins) != 1:
        raise ProductDraftDependencyError('product draft material proof unavailable')
    payload = selected[0].payload
    fields = {'schema_version', 'turn_id', 'execution', 'origin_ref', 'input', 'proof'}
    if (set(payload) != fields or type(payload['schema_version']) is not int or payload['schema_version'] != 1
            or payload['turn_id'] != actual['turn_id']):
        raise ProductDraftDependencyError('product draft material proof changed')
    from core.ai_kernel.product_snapshot import admission_base_refs
    expected_input = {**actual['input'], 'refs': admission_base_refs(actual, snapshot['immutable'])}
    if (payload['input'] != expected_input or payload['execution'] != origins[0].payload.get('execution')
            or payload['origin_ref'] != origins[0].ref or selected[0].scope != origins[0].scope):
        raise ProductDraftDependencyError('product draft material origin changed')
    proof = payload['proof']
    if (not isinstance(proof, dict) or set(proof) != {'schema_version', 'privacy', 'selection', 'bodies'}
            or type(proof['schema_version']) is not int or proof['schema_version'] != 1
            or proof['privacy'] != actual['privacy'] or not isinstance(proof['selection'], dict)):
        raise ProductDraftDependencyError('product draft material proof changed')
    materials, bodies = proof['selection'].get('origin_materials'), proof['bodies']
    if (not isinstance(materials, list) or not isinstance(bodies, list) or len(materials) != len(bodies)
            or proof['selection'].get('origin_ref') != origins[0].ref
            or len(materials) != len(actual['privacy']['material_refs'])):
        raise ProductDraftDependencyError('product draft material proof changed')
    for material, body, descriptor in zip(materials, bodies, actual['privacy']['material_refs']):
        if (not isinstance(material, dict) or set(material) != {'type', 'id', 'revision', 'project_id', 'namespace'}
                or not isinstance(material['namespace'], str) or not material['namespace']
                or {key: material[key] for key in descriptor} != descriptor
                or not isinstance(body, dict) or set(body) != {'material', 'payload', 'markdown', 'ref', 'qualified_ref'}
                or body['material'] != material):
            raise ProductDraftDependencyError('product draft material namespace changed')
    return proof


def _answer_source(reader, scope, document, revision, *, turn_id):
    from .external_turn_facts import completed_answer_facts
    if revision != 1 or scope.user_id != 'local-user' or document.payload.get('project_id') != scope.project_id:
        raise ProductDraftDependencyError('saved answer document scope is invalid')
    record, markdown = _historical(reader, document.object_id, revision)
    refs = _mapping(record.payload.get('source_snapshot')).get('source_refs')
    if (not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], Mapping)
            or set(refs[0]) != {'source_id', 'locator'}):
        raise ProductDraftDependencyError('saved answer document source is invalid')
    identity = _identity(refs[0]['source_id'])
    marker = reader.read(ANSWER_OUTCOMES, identity)
    fields = {'project_id', 'turn_id', 'turn_revision', 'document_id', 'result_ref', 'question',
              'scene', 'created_at', 'cited_refs', 'publication'}
    value = marker.payload if marker is not None else {}
    if (marker is None or marker.revision != 1 or set(value) != fields
            or value['project_id'] != scope.project_id or value['turn_id'] != identity
            or value['document_id'] != document.object_id or refs[0]['locator'] != 'conversation://' + identity
            or document.payload.get('type') != 'agent-result-answer-' + identity
            or turn_id is not None and turn_id != identity):
        raise ProductDraftDependencyError('saved answer document birth is invalid')
    try:
        facts = completed_answer_facts(reader, project_id=scope.project_id, turn_id=identity)
    except (ValueError, KeyError, TypeError) as error:
        raise ProductDraftDependencyError('saved answer completed evidence is unavailable') from error
    publication = {key: facts['answer'][key] for key in ('unverified_claims', 'unverified_claim_spans')
                   if key in facts['answer']}
    if (facts['public_revision'] != value['turn_revision'] or facts['result_ref'] != value['result_ref']
            or facts['question'] != value['question'] or facts['answer']['answer'] != markdown.payload['markdown']
            or publication != value['publication'] or not isinstance(value['cited_refs'], list)
            or not isinstance(value['created_at'], str) or not value['created_at']):
        raise ProductDraftDependencyError('saved answer completed evidence changed')
    return ProductDraftDependencies((), {'answer_turn_id': identity,
        'answer_turn_record_revision': _revision(value['turn_revision']), 'answer_save_revision': marker.revision,
        'document_revision': 1, 'document_revision_record_revision': _revision(record.revision),
        'document_markdown_revision': _revision(markdown.revision)}, (), facts['request'], answer_birth=value)


def _material_document(reader, scope, material, snapshot):
    from backend.memory_app.original_sources import document_roots
    from .document_filings import filing_experience
    own = type(scope)(scope.user_id, material['project_id'])
    document = reader.read('documents', material['id'])
    if document is None or document.payload.get('project_id') != own.project_id:
        raise ProductDraftDependencyError('product draft document scope changed')
    historical, markdown = _historical(reader, material['id'], material['revision'])
    source_refs = _mapping(historical.payload.get('source_snapshot')).get('source_refs')
    filed = filing_experience(reader, own, material['id'])
    derived = ([('experience', filed.object_id, filed.revision)] if filed is not None else
        list(document_roots(reader, own, source_refs)))
    frozen = snapshot.get('roots')
    if (not derived or not isinstance(frozen, list) or not frozen
            or any(not isinstance(item, Mapping) or set(item) != {'type', 'id', 'revision'}
                or item['type'] not in {'recognition', 'experience', 'original_item', 'original_source'}
                or _revision(item['revision']) < 1 for item in frozen)
            or {(item['type'], item['id']) for item in frozen} != {(kind, identity) for kind, identity, _ in derived}):
        raise ProductDraftDependencyError('product draft document roots changed')
    return ([(own.project_id, item['type'], _identity(item['id']), _revision(item['revision'])) for item in frozen],
        {'material': dict(material), 'markdown': markdown.payload['markdown'], 'source_refs': source_refs,
         'document_revision_record_revision': historical.revision, 'markdown_record_revision': markdown.revision})


def _task_draft_source(reader, scope, document_id, revision, *, turn_id=None):
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
    roots, input_refs, material_documents = [], [], []
    qualified = _shared_task_materials(reader, request)
    for position, (material, snapshot) in enumerate(zip(materials, snapshots)):
        if (not isinstance(material, Mapping) or set(material) != {'type', 'id', 'revision', 'project_id'}
                or material['type'] not in {'recognition', 'experience', 'original_item', 'original_source', 'document'}
                or material['project_id'] not in {scope.project_id, 'me'}
                or not isinstance(snapshot, Mapping)
                or set(snapshot) != {'schema_version', 'scope', 'privacy_revision', 'roots', 'nodes'}
                or type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1
                or snapshot['scope'] != {'user_id': scope.user_id, 'project_id': material['project_id']}
                or material['type'] != 'document' and snapshot['roots'] != [
                    {'type': material['type'], 'id': material['id'], 'revision': material['revision']}]
                or type(snapshot['privacy_revision']) is not int or snapshot['privacy_revision'] < 0):
            raise ProductDraftDependencyError('product draft frozen source identity is invalid')
        item, version = _identity(material['id']), _revision(material['revision'])
        if qualified is not None:
            input_refs.append(qualified['bodies'][position]['qualified_ref'])
            continue
        if material['type'] == 'document':
            inherited, retained = _material_document(reader, scope, material, snapshot)
            roots.extend(root for root in inherited if root not in roots)
            material_documents.append(retained)
            input_refs.append({'kind': 'document', 'object_id': item, 'uri': 'crp://default/documents/' + item})
            continue
        if type(snapshot['roots'][0]['revision']) is not int:
            raise ProductDraftDependencyError('product draft frozen source revision is invalid')
        kind = material['type']
        collection = {'recognition': 'recognitions', 'experience': 'recognition_experiences',
                      'original_item': 'workspace', 'original_source': 'sources'}[kind]
        root = (material['project_id'], kind, item, version)
        if root not in roots:
            roots.append(root)
        input_refs.append({'kind': 'atom' if kind in {'recognition', 'experience'} else 'source',
                           'object_id': item, 'uri': 'crp://default/' + collection + '/' + item})
    originals = ()
    answers = ()
    bound = reader.read('v2_part_contexts', identity)
    if bound is not None:
        context = _mapping(bound.payload.get('context'))
        refs = _mapping(request.get('input')).get('refs')
        marker = {'kind': 'session_event', 'object_id': identity,
                  'uri': 'crp://default/v2_part_contexts/' + identity}
        try:
            product = json.loads(request['input']['text'])
        except (TypeError, ValueError):
            raise ProductDraftDependencyError('product draft dependency input is invalid') from None
        if (bound.revision != 1 or bound.payload.get('project_id') != scope.project_id
                or bound.payload.get('input_refs') != refs or context.get('project_id') != scope.project_id
                or not isinstance(context.get('refs'), list) or not isinstance(context.get('originals'), list)
                or not isinstance(context.get('answers'), list) or not isinstance(product, dict)
                or product.get('dependency_context') != context.get('text')):
            raise ProductDraftDependencyError('product draft dependency binding is invalid')
        originals = tuple(context['originals'])
        answers = tuple(context['answers'])
        parent = reader.read('v2_turns', context.get('parent', ''))
        if (turn.payload.get('parent_turn_id') != context.get('parent') or parent is None
                or parent.payload.get('intent') != 'multi' or parent.payload.get('project_id') != scope.project_id
                or execution_id not in parent.payload.get('part_turn_ids', [])):
            raise ProductDraftDependencyError('product draft dependency parent changed')
        if qualified is not None:
            if (context.get('scope_kind') != 'shared' or not isinstance(context.get('shared_answers'), list)
                    or originals or answers or not isinstance(context.get('actor_namespace'), str)):
                raise ProductDraftDependencyError('product draft dependency namespace changed')
            expected = [{'kind': 'session_event', 'object_id': answer['id'],
                'uri': 'crp://' + context['actor_namespace'] + '/v2_turns/' + answer['id']}
                for answer in context['shared_answers']]
        else:
            expected = [{'kind': 'source', 'object_id': original['id'],
                         'uri': 'crp://default/workspace/' + original['id']} for original in originals]
            expected += [{'kind': 'session_event', 'object_id': answer['id'],
                          'uri': 'crp://default/v2_turns/' + answer['id']} for answer in answers]
        def ref_key(ref):
            return ref['kind'], ref['object_id'], ref['uri']
        if sorted(context['refs'], key=ref_key) != sorted(expected, key=ref_key):
            raise ProductDraftDependencyError('product draft dependency references changed')
        input_refs += context['refs'] + [marker]
    if _admitted_input_refs(reader, request, execution) != input_refs or len(roots) != len(set(roots)):
        raise ProductDraftDependencyError('product draft frozen input references changed')
    return ProductDraftDependencies(tuple(roots), {
        'product_turn_id': identity, 'task_draft_operation_id': operation_id,
        'task_draft_operation_revision': _revision(operation.revision), 'task_execution_id': execution_id,
        'task_execution_revision': _revision(execution.revision), 'product_turn_record_revision': _revision(turn.revision),
        'document_revision': revision, 'document_revision_record_revision': _revision(historical.revision),
        'document_markdown_revision': _revision(markdown.revision)}, tuple(snapshots), request, originals, answers,
        material_documents=tuple(material_documents),
        qualified_materials=tuple(qualified['selection']['origin_materials']) if qualified is not None else (),
        material_proof=qualified)


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
