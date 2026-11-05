import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from backend.recognition import CANDIDATE_GENERATION_STEP_VERSION, RecognitionConflict, WorkScope, normalize_conditions
from backend.recognition.external_turn_facts import _readonly
from backend.shared.document_visibility import LegacyDocumentVisibility
from backend.shared.secret_detection import contains_secret
from core.document_engine import SQLiteDocumentRepository
from ..document_recognition import CANDIDATE_SOURCE_CONSTRAINTS, ensure_document_experience
from ..generation_sources import generation_source_guard
from ..kernel.memory_turn import MemoryTurn as KernelMemoryTurn
from ..source_egress import SourceEgressService
from ..structured_generation import InvalidStructuredOutput
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _project
from .insight_generation import _MaterialBudgetError, _material_capacity, _prepare_material_input
from .memory_turn import MemoryTurn
from .policies import get, override
from .policies.extract_inspiration import InspirationInput, MAX_DOCUMENTS, MAX_INSIGHTS, render_document
from .projects import scene_of
from .source_sections import _exact
from .turn_requests import _INSTRUCTIONS, freeze_product_turn, validate_frozen_inputs


DETAILS = 'v2_inspiration_details'
INPUTS = 'v2_inspiration_inputs'
REJECTIONS = 'v2_inspiration_budget_rejections'
PLAN = 'manual-inspiration-plan-v1'
APPLIED = 'manual-inspirations-applied-v1'
PROPOSAL = 'manual-inspirations'
POLICY = '@7'


class InspirationExtractionError(ValueError):
    def __init__(self, status, code, *, turn_id=None, retry_token=None):
        super().__init__(code)
        self.status = status
        self.detail = {'code': code, **({'turn_id': turn_id} if turn_id else {}),
            **({'retry_token': retry_token} if retry_token else {})}


class InspirationReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document: StrictInt = Field(ge=1, le=MAX_DOCUMENTS)
    quote: StrictStr = Field(min_length=1, max_length=1200)


class InspirationItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: StrictStr = Field(min_length=1, max_length=240)
    conditions: list[StrictStr] = Field(max_length=8)
    explanation: StrictStr = Field(min_length=1, max_length=1800)
    questions: list[StrictStr] = Field(max_length=2)
    evidence: list[InspirationReference] = Field(min_length=1, max_length=6)


class InspirationOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    complete: StrictBool
    insights: list[InspirationItem] = Field(max_length=MAX_INSIGHTS)


def parse_request(body):
    if (not isinstance(body, dict) or set(body) not in (
            {'project_id', 'scene', 'documents'}, {'project_id', 'scene', 'documents', 'retry_token'})):
        raise InspirationExtractionError(400, 'invalid_inspiration_fields')
    project = _project(body['project_id'])
    scene = body['scene']
    if scene is not None and (not isinstance(scene, str) or not scene.strip() or len(scene) > 200):
        raise InspirationExtractionError(400, 'invalid_inspiration_fields')
    selected, seen = body['documents'], set()
    if not isinstance(selected, list) or not 1 <= len(selected) <= MAX_DOCUMENTS:
        raise InspirationExtractionError(400, 'invalid_inspiration_fields')
    for document in selected:
        if (not isinstance(document, dict) or set(document) != {'document_id', 'expected_revision'}
                or type(document['expected_revision']) is not int or document['expected_revision'] < 1):
            raise InspirationExtractionError(400, 'invalid_inspiration_fields')
        identity = _project(document['document_id'])
        if identity in seen:
            raise InspirationExtractionError(400, 'invalid_inspiration_fields')
        seen.add(identity)
    token = body.get('retry_token')
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 128
            or not token.isascii() or not token.isprintable() or any(char.isspace() for char in token)
            or contains_secret(token)):
        raise InspirationExtractionError(400, 'invalid_inspiration_fields')
    return project, scene, sorted(deepcopy(selected), key=lambda row: row['document_id']), token


def _read_sources(records, documents, project, selected, scene):
    visibility = LegacyDocumentVisibility.from_repository(documents, project_id=project,
        document_ids={row['document_id'] for row in selected})
    sources = []
    for selected_document in selected:
        identity = selected_document['document_id']
        document = documents.read(identity)
        if (document is None or document.get('project_id') != project or not visibility.allows(document)
                or document.get('status') == 'archived'):
            raise InspirationExtractionError(404, 'inspiration_document_unavailable')
        recall = records.read('v2_document_recall', identity)
        assignment = scene_of(records, 'document', identity)
        if (recall is not None and recall.payload.get('state') == 'forgotten'
                or scene is not None and (assignment is None or assignment.get('project_id') != project
                    or assignment.get('scene') != scene)):
            raise InspirationExtractionError(404, 'inspiration_document_unavailable')
        if document['revision'] != selected_document['expected_revision']:
            raise InspirationExtractionError(409, 'inspiration_sources_changed')
        markdown = documents.markdown(identity, revision=document['revision'])
        if not isinstance(markdown, str) or not markdown.strip():
            raise InspirationExtractionError(409, 'inspiration_sources_changed')
        sources.append({'document_id': identity, 'revision': document['revision'], 'title': document['title'],
            'markdown': markdown})
    return sources


def _key(project, scene, selected):
    return json.dumps({'mode': 'manual_inspiration', 'project_id': project, 'scene': scene,
        'documents': selected, 'extract': POLICY}, sort_keys=True, ensure_ascii=False)


def _response_model(policy, sources):
    class BoundInspirationOutput(InspirationOutput):
        @model_validator(mode='after')
        def actual_evidence(self):
            policy.decide(self.model_dump(), sources, normalize_conditions)
            return self
    return BoundInspirationOutput


def _candidate_id(turn_id, ordinal):
    return 'inspiration-' + turn_id + '-' + str(ordinal)


def _generation(metadata):
    return {'id': metadata['generation_id'], 'step_version': CANDIDATE_GENERATION_STEP_VERSION,
        'model': metadata.get('model'), 'configuration_revision': metadata.get('configuration_revision'),
        'completed_at': metadata['completed_at']}


def extract_inspirations(models, service, documents, *, project, scene, selected, retry_token=None,
        execution_guard=None, on_turn=None, retry_version=None):
    records, scope = service.records, WorkScope('local-user', project)
    sources = _read_sources(records, documents, project, selected, scene)
    automatic = {'producer': 'inspirations', 'mode': 'scope', 'scene': scene,
        'anchors': [{'type': 'document', 'id': source['document_id']} for source in sources]}
    completed = _completed_result(records, service, scope, scene, selected)
    if completed is not None:
        if models is not None:
            from .memory_turn import automatic_admission
            binding = automatic_admission(records, models, kind='memory.propose_insights', project=project,
                automatic=automatic)
            if binding is not None:
                original = records.read('v2_memory_turn_keys', completed['turn_id'])
                if original is None:
                    raise RecognitionConflict('inspiration_original_request_unavailable')
                binding.validate_current(original.payload['request'], MemoryTurn.store_for(records))
        return completed
    if retry_token is None:
        rejected = _pending_budget_rejection(records, scope, _key(project, scene, selected), sources)
        if rejected is not None:
            raise InspirationExtractionError(409, 'inspiration_material_budget_insufficient',
                turn_id=rejected, retry_token=rejected)
    if models is None:
        raise InspirationExtractionError(409, 'inspiration_generation_unavailable')
    materials, bindings = [], []
    for source in sources:
        experience_id, revision = ensure_document_experience(documents, service, project, source['document_id'])
        experience = records.read('recognition_experiences', experience_id)
        if revision != source['revision'] or experience is None:
            raise InspirationExtractionError(409, 'inspiration_sources_changed')
        materials.append({'type': 'experience', 'id': experience_id,
            'revision': experience.revision, 'project_id': project})
        bindings.append({key: deepcopy(value) for key, value in source.items() if key != 'markdown'}
            | {'experience_id': experience_id, 'experience_revision': experience.revision})
    validate_sources = generation_source_guard(SourceEgressService(records), models, scope,
        [{key: material[key] for key in ('type', 'id', 'revision')} for material in materials])
    prepared, turn = None, None

    def validate(reader=None):
        if execution_guard is not None:
            execution_guard(reader)
        if not _exact(_read_sources(records, documents, project, selected, scene), sources):
            raise RecognitionConflict('inspiration_sources_changed')
        validate_sources()
        if reader is not None:
            validate_frozen_inputs(TransactionRecords(reader), models, turn.request, source_owner=records)
            turn.validate_commit(reader)
        if prepared is not None and _material_capacity(models, records, turn) != {
                key: prepared[key] for key in ('binding', 'configuration', 'limits')}:
            raise RecognitionConflict('inspiration_model_binding_changed')

    source_by_experience = {binding['experience_id']: source for binding, source in zip(bindings, sources)}

    def freeze(kind, **kwargs):
        kwargs['load_text'] = lambda material: render_document(source_by_experience[material['id']])
        return freeze_product_turn(kind, **kwargs)

    with override(extract=POLICY, **({'retry': retry_version} if retry_version is not None else {})):
        policy = get('extract', version=POLICY)
        try:
            base = _key(project, scene, selected)
            key = _budget_retry_key(records, scope, base, retry_token, sources)
            turn = MemoryTurn(records, models, kind='memory.propose_insights', project=project,
                key=key, materials=materials, validate=validate,
                freeze_request=freeze, retry_token=retry_token, automatic=automatic).select_insight_attempt(retry_token)
            if retry_version is not None and turn.request.get('policy_versions', {}).get('retry') != retry_version:
                raise RecognitionConflict('automatic_inspiration_retry_changed')
            basis = {'project_id': project, 'scene': scene, 'documents': bindings}
            with records.begin() as tx:
                prior = tx.read(INPUTS, turn.turn_id)
                if prior is None:
                    tx.put(INPUTS, turn.turn_id, basis, expected_revision=0)
                elif prior.revision != 1 or not _exact(prior.payload, basis):
                    raise RecognitionConflict('inspiration_sources_changed')
                tx.commit()
            if on_turn is not None:
                on_turn(turn.turn_id)
            response_model = _response_model(policy, sources)
            request = InspirationInput(tuple(sources), CANDIDATE_SOURCE_CONSTRAINTS)
            _, messages, prepared = _prepare_material_input(models, records, turn, policy, request, response_model)
            output, metadata = turn.generate(messages, response_model=response_model,
                max_tokens=prepared['output_budget']['max_tokens'])
            validate()
            rows = policy.decide(output.model_dump(), sources, normalize_conditions)
        except _MaterialBudgetError as error:
            token = _record_budget_rejection(records, turn) if turn is not None else None
            raise InspirationExtractionError(409, 'inspiration_material_budget_insufficient',
                turn_id=turn.turn_id if turn is not None else None, retry_token=token) from error
        except InspirationExtractionError:
            raise
        except Exception as error:
            if isinstance(error, RecognitionConflict):
                known = False
                code = (str(error) if str(error) in {'automatic_inspiration_disabled', 'automatic_inspiration_retry_changed'}
                    else 'inspiration_generation_unknown' if 'result_unavailable' in str(error)
                    else 'inspiration_sources_changed')
            else:
                known = turn is not None and turn._known_failed_attempt()
                code = ('inspiration_invalid_output' if isinstance(error, (InvalidStructuredOutput, ValueError))
                    else 'inspiration_generation_failed')
            raise InspirationExtractionError(409 if code in {'automatic_inspiration_disabled', 'automatic_inspiration_retry_changed', 'inspiration_sources_changed', 'inspiration_generation_unknown'}
                else 502, code, turn_id=turn.turn_id if turn is not None else None,
                retry_token=turn.turn_id if known else None) from error
        generation = _generation(metadata)
        ids = [_candidate_id(turn.turn_id, ordinal) for ordinal in range(1, len(rows) + 1)]
        manifest = {'project_id': project, 'generation_id': generation['id'], 'candidate_ids': ids,
            'proposal_key': PROPOSAL}
        turn.store.get_or_create_immutable_payload(turn.turn_id, PLAN, manifest)
        if not _exact(turn.store.get_immutable_payload(turn.turn_id, PLAN)[1], manifest):
            raise RecognitionConflict('inspiration_generation_unavailable')

        def existing():
            candidates = [records.read('recognition_candidates', identity) for identity in ids]
            if not any(candidates):
                return [] if not ids else None
            if any(candidate is None for candidate in candidates):
                raise RecognitionConflict('inspiration_batch_incomplete')
            for candidate in candidates:
                validate_inspiration_origin(records, scope, candidate, require_proposal=False)
            return ids

        def write():
            with records.begin() as tx:
                validate(tx)
                for ordinal, (identity, row) in enumerate(zip(ids, rows), 1):
                    experience_ids = [bindings[number - 1]['experience_id'] for number in row['source_numbers']]
                    service.propose_in_uow(tx, scope=scope, content=row['text'], conditions=row['conditions'],
                        source_experience_ids=experience_ids, candidate_id=identity, generation=generation)
                    tx.put(DETAILS, identity, {'project_id': project, 'extract_turn_id': turn.turn_id,
                        'generation_id': generation['id'], 'ordinal': ordinal, 'text': row['text'],
                        'conditions': row['conditions'], 'explanation': row['explanation'],
                        'questions': row['questions'], 'evidence': row['evidence']}, expected_revision=0)
                    tx.put('v2_scene_assignments_candidate', identity,
                        {'project_id': project, 'scene': scene}, expected_revision=0)
                tx.commit()
            return ids

        turn.propose(key=PROPOSAL, write=write, existing=existing)
        turn.store.get_or_create_immutable_payload(turn.turn_id, APPLIED, manifest)
        for identity in ids:
            validate_inspiration_origin(records, scope, records.read('recognition_candidates', identity))
        from .insights import insight_view
        return {'turn_id': turn.turn_id, 'items': [view for identity in ids
            if (view := insight_view(records, scope, identity, service=service)) is not None]}


def _database(reader):
    path = getattr(reader, 'database_path', None)
    if path is None:
        path = next(row[2] for row in reader.connection.execute('PRAGMA database_list') if row[1] == 'main')
    path = Path(str(path))
    root = path.parent / '.rebuild-data' if path.name == 'recognition.sqlite3' else path.parent
    return root / 'ai-turns.sqlite3'


def _no_native_turn(reader, turn_id):
    if not _database(reader).is_file():
        return False
    with _readonly(_database(reader)) as connection:
        return not any(connection.execute('SELECT 1 FROM ' + table + ' WHERE turn_id=? LIMIT 1',
            (turn_id,)).fetchone() is not None for table in
            ('ai_turns', 'ai_turn_events', 'ai_turn_immutable_payloads', 'ai_turn_payloads'))


def _zero_budget_rejection(reader, index):
    rejection = reader.read(REJECTIONS, index.object_id)
    request = index.payload.get('request', {})
    return (rejection is not None and rejection.revision == 1
        and _exact(rejection.payload, {'project_id': request.get('scope', {}).get('project_id'),
            'turn_id': index.object_id, 'phase': 'material_budget'})
        and request.get('turn_id') == index.object_id
        and request.get('desired_outcome') == 'memory.propose_insights'
        and request.get('policy_versions', {}).get('extract') == POLICY
        and _no_native_turn(reader, index.object_id))


def _record_budget_rejection(records, turn):
    if not _no_native_turn(records, turn.turn_id):
        return None
    with records.begin() as tx:
        expected = {'project_id': turn.request['scope']['project_id'],
            'turn_id': turn.turn_id, 'phase': 'material_budget'}
        prior = tx.read(REJECTIONS, turn.turn_id)
        if prior is None:
            tx.put(REJECTIONS, turn.turn_id, expected, expected_revision=0)
        elif prior.revision != 1 or not _exact(prior.payload, expected):
            raise RecognitionConflict('inspiration_generation_unavailable')
        tx.commit()
    index = records.read('v2_memory_turn_keys', turn.turn_id)
    return turn.turn_id if index is not None and _zero_budget_rejection(records, index) else None


def _budget_retry_key(reader, scope, base, retry_token, sources):
    index = reader.read('v2_memory_turn_keys', retry_token) if retry_token is not None else None
    if index is None or not _zero_budget_rejection(reader, index):
        return base
    request = index.payload['request']
    text = '\n\n'.join([_INSTRUCTIONS['memory.propose_insights'], *(render_document(source) for source in sources)])
    if request['scope']['project_id'] != scope.project_id or request['input']['text'] != text:
        raise RecognitionConflict('inspiration_generation_unavailable')
    _validate_attempt_key(reader, index, base, request)
    return base + ':retry-of:' + index.object_id


def _pending_budget_rejection(reader, scope, base, sources):
    indexes = [row for row in reader.list('v2_memory_turn_keys')
        if row.payload.get('identity', {}).get('kind') == 'memory.propose_insights'
        and row.payload['identity'].get('project') == scope.project_id
        and isinstance(row.payload['identity'].get('key'), str)
        and (row.payload['identity']['key'] == base or row.payload['identity']['key'].startswith(base + ':retry-of:'))]
    keys = {row.payload['identity']['key'] for row in indexes}
    rejected = [row for row in indexes if base + ':retry-of:' + row.object_id not in keys
        and _zero_budget_rejection(reader, row)]
    if len(rejected) > 1:
        raise RecognitionConflict('inspiration_generation_unavailable')
    if not rejected:
        return None
    _budget_retry_key(reader, scope, base, rejected[0].object_id, sources)
    return rejected[0].object_id


class _RetainedAttempt:
    def __init__(self, connection):
        self.connection = connection

    def events_after(self, turn_id):
        return [json.loads(row[0]) for row in self.connection.execute(
            'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (turn_id,))]

    def get(self, reference):
        row = self.connection.execute('SELECT payload_json FROM ai_turn_payloads WHERE payload_ref=? UNION ALL '
            'SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=? LIMIT 1',
            (reference, reference)).fetchone()
        if row is None:
            raise KeyError(reference)
        return json.loads(row[0])

    def get_immutable_payload(self, turn_id, kind):
        row = self.connection.execute('SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads '
            'WHERE turn_id=? AND kind=?', (turn_id, kind)).fetchone()
        return (row[0], json.loads(row[1])) if row is not None else None


def _native(reader, turn_id, *, completed=True):
    with _readonly(_database(reader)) as connection:
        request = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (turn_id,)).fetchone()
        immutable = {kind: json.loads(raw) for kind, raw in connection.execute(
            'SELECT kind,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?', (turn_id,))}
        terminal = [json.loads(row[0]) for row in connection.execute(
            'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (turn_id,))
            if json.loads(row[0]).get('type') in {'turn.completed', 'turn.failed', 'turn.cancelled'}]
    if request is None or completed and (not terminal or terminal[-1]['type'] != 'turn.completed'):
        raise RecognitionConflict('inspiration_generation_unavailable')
    return json.loads(request[0]), immutable


def _attempt_input(reader, request):
    from core.ai_kernel.product_admission import MANIFEST_KIND, bundle_from_payload
    from core.ai_kernel.product_snapshot import read_admission_base_refs
    from .automatic_constraint_input import KIND, automatic_bundle, constraint_markers
    from .automatic_constraints import PREPARED
    value = deepcopy(request['input'])
    staged = reader.read(PREPARED, request['turn_id'])
    with _readonly(_database(reader)) as connection:
        native = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?',
            (request['turn_id'],)).fetchone()
        capsule = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
            (request['turn_id'], KIND)).fetchone()
        if not constraint_markers(request):
            if staged is not None or capsule is not None:
                raise RecognitionConflict('inspiration_generation_unavailable')
            return value
        if (staged is None or staged.revision != 1 or set(staged.payload) != {'request', 'bundle'}
                or not _exact(staged.payload['request'], request)):
            raise RecognitionConflict('inspiration_generation_unavailable')
        admitted = bundle_from_payload(staged.payload['bundle'])
        frozen = automatic_bundle(request, admitted)
        item = next(item for item in admitted.items if item.kind == KIND)
        producer = reader.read('v2_memory_turn_keys', request['turn_id'])
        identity = item.payload['automatic_descriptor']['producer_identity']
        if (producer is None or not _exact(producer.payload.get('request'), request)
                or identity != {'collection': 'v2_memory_turn_keys', **producer.payload['identity']}):
            raise RecognitionConflict('inspiration_generation_unavailable')
        if item.payload['automatic_descriptor']['source'] is None:
            path = Path(reader.database_path).resolve(strict=True)
            file, directory = path.stat(), path.parent.stat()
            if frozen['origin']['physical_identity'] != {'database_path': str(path),
                    'database': [file.st_dev, file.st_ino], 'directory': [directory.st_dev, directory.st_ino]}:
                raise RecognitionConflict('inspiration_generation_unavailable')
        if native is not None:
            if (not _exact(json.loads(native[0]), request) or capsule is None
                    or not _exact(json.loads(capsule[0]), item.payload)):
                raise RecognitionConflict('inspiration_generation_unavailable')
            read_admission_base_refs(connection, request)
        elif capsule is not None:
            raise RecognitionConflict('inspiration_generation_unavailable')
    # 仅投影已冻结的自动证明；材料和其他来源证明继续参与原重试资格比较。
    removed = [item.ref]
    if len(admitted.items) == 1:
        removed.append({'kind': 'session_event', 'object_id': request['turn_id'],
            'uri': f"crp://session/{request['turn_id']}/{MANIFEST_KIND}"})
    value['refs'] = [reference for reference in value['refs'] if reference not in removed]
    return value


def _validate_attempt_key(reader, index, base, request):
    current, visited = index, set()
    while True:
        identity = current.payload['identity']
        expected = {'kind': 'memory.propose_insights', 'project': request['scope']['project_id'], 'key': base}
        if _exact(identity, expected):
            return
        prefix = base + ':retry-of:'
        key = identity.get('key')
        if (current.object_id in visited or identity.get('kind') != expected['kind']
                or identity.get('project') != expected['project'] or set(identity) != set(expected)
                or not isinstance(key, str) or not key.startswith(prefix)
                or not isinstance(current.payload.get('retry_token'), str) or not current.payload['retry_token']):
            raise RecognitionConflict('inspiration_generation_unavailable')
        visited.add(current.object_id)
        predecessor_id = key[len(prefix):]
        predecessor = reader.read('v2_memory_turn_keys', predecessor_id)
        if predecessor is None or predecessor_id in visited:
            raise RecognitionConflict('inspiration_generation_unavailable')
        zero_send = _zero_budget_rejection(reader, predecessor)
        prior_request = predecessor.payload['request'] if zero_send else _native(reader, predecessor_id, completed=False)[0]
        if (not _exact(prior_request, predecessor.payload['request'])
                or prior_request.get('turn_id') != predecessor_id
                or prior_request.get('desired_outcome') != 'memory.propose_insights'
                or prior_request.get('scope') != request['scope']
                or _attempt_input(reader, prior_request) != _attempt_input(reader, request)
                or prior_request.get('policy_versions', {}).get('extract') != POLICY):
            raise RecognitionConflict('inspiration_generation_unavailable')
        if not zero_send:
            with _readonly(_database(reader)) as connection:
                retained = _RetainedAttempt(connection)
                if not KernelMemoryTurn._known_failed_attempt(SimpleNamespace(turn_id=predecessor_id, store=retained)):
                    raise RecognitionConflict('inspiration_generation_unavailable')
        current = predecessor


def _basis(reader, scope, turn_id, *, require_proposal=True):
    request, immutable = _native(reader, turn_id)
    index, inputs = reader.read('v2_memory_turn_keys', turn_id), reader.read(INPUTS, turn_id)
    generated, manifest = immutable.get('memory-generation-output-v1'), immutable.get(PLAN)
    if (index is None or inputs is None or inputs.revision != 1 or generated is None or manifest is None
            or not _exact(request, index.payload['request']) or request.get('turn_id') != turn_id
            or request.get('desired_outcome') != 'memory.propose_insights'
            or request.get('scope', {}).get('project_id') != scope.project_id
            or request.get('policy_versions', {}).get('extract') != POLICY
            or inputs.payload.get('project_id') != scope.project_id):
        raise RecognitionConflict('inspiration_generation_unavailable')
    bindings = inputs.payload['documents']
    documents = SQLiteDocumentRepository(reader)
    sources = [{key: binding[key] for key in ('document_id', 'revision', 'title')}
        | {'markdown': documents.markdown(binding['document_id'], revision=binding['revision'])} for binding in bindings]
    if any(not isinstance(source['markdown'], str) for source in sources):
        raise RecognitionConflict('inspiration_generation_unavailable')
    selected = [{'document_id': binding['document_id'], 'expected_revision': binding['revision']} for binding in bindings]
    base = _key(scope.project_id, inputs.payload['scene'], selected)
    text = '\n\n'.join([_INSTRUCTIONS['memory.propose_insights'], *(render_document(source) for source in sources)])
    materials = [{'type': 'experience', 'id': binding['experience_id'], 'revision': binding['experience_revision'],
        'project_id': scope.project_id} for binding in bindings]
    _validate_attempt_key(reader, index, base, request)
    if (request['input']['text'] != text
            or not _exact(request['privacy']['material_refs'], materials)):
        raise RecognitionConflict('inspiration_generation_unavailable')
    rows = get('extract', version=POLICY).decide(generated['output'], sources, normalize_conditions)
    ids = [_candidate_id(turn_id, ordinal) for ordinal in range(1, len(rows) + 1)]
    expected_manifest = {'project_id': scope.project_id, 'generation_id': generated['metadata']['generation_id'],
        'candidate_ids': ids, 'proposal_key': PROPOSAL}
    if (not _exact(manifest, expected_manifest)
            or require_proposal and (not _exact(immutable.get(APPLIED), expected_manifest)
                or immutable.get('memory-proposal-' + PROPOSAL) != {'committed': True, 'key': PROPOSAL})):
        raise RecognitionConflict('inspiration_generation_unavailable')
    return {'request': request, 'generated': generated, 'bindings': bindings,
        'materials': materials, 'rows': rows, 'ids': ids, 'base': base}


def _completed_result(reader, service, scope, scene, selected):
    base = _key(scope.project_id, scene, selected)
    if not _database(reader).is_file():
        return None
    completed = []
    for index in reader.list('v2_memory_turn_keys'):
        identity = index.payload.get('identity', {})
        key = identity.get('key')
        if (identity.get('kind') != 'memory.propose_insights' or identity.get('project') != scope.project_id
                or not isinstance(key, str) or not (key == base or key.startswith(base + ':retry-of:'))):
            continue
        with _readonly(_database(reader)) as connection:
            applied = connection.execute('SELECT 1 FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
                (index.object_id, APPLIED)).fetchone()
        if applied is None:
            continue
        basis = _basis(reader, scope, index.object_id)
        if basis['base'] != base:
            raise RecognitionConflict('inspiration_generation_unavailable')
        for identity in basis['ids']:
            candidate = reader.read('recognition_candidates', identity)
            if candidate is None:
                raise RecognitionConflict('inspiration_generation_unavailable')
            validate_inspiration_origin(reader, scope, candidate)
        completed.append((index.object_id, basis['ids']))
    if not completed:
        return None
    if len(completed) != 1:
        raise RecognitionConflict('inspiration_generation_unavailable')
    from .insights import insight_view
    turn_id, ids = completed[0]
    return {'turn_id': turn_id, 'items': [view for identity in ids
        if (view := insight_view(reader, scope, identity, service=service)) is not None]}


def _origin(reader, scope, candidate, *, require_proposal=True):
    marker = reader.read(DETAILS, candidate.object_id)
    if marker is None:
        if candidate.object_id.startswith('inspiration-memory-'):
            raise RecognitionConflict('inspiration_generation_unavailable')
        return None
    data = marker.payload
    if (marker.revision != 1 or data.get('project_id') != scope.project_id
            or candidate.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}
            or candidate.payload.get('generation', {}).get('id') != data.get('generation_id')):
        raise RecognitionConflict('inspiration_generation_unavailable')
    turn_id = data['extract_turn_id']
    basis = _basis(reader, scope, turn_id, require_proposal=require_proposal)
    request, generated, bindings = basis['request'], basis['generated'], basis['bindings']
    rows, ids, materials = basis['rows'], basis['ids'], basis['materials']
    ordinal = data['ordinal']
    if (generated['metadata']['generation_id'] != data['generation_id']
            or type(ordinal) is not int or not 1 <= ordinal <= len(rows) or candidate.object_id != ids[ordinal - 1]):
        raise RecognitionConflict('inspiration_generation_unavailable')
    row = rows[ordinal - 1]
    expected_data = {'project_id': scope.project_id, 'extract_turn_id': turn_id,
        'generation_id': data['generation_id'], 'ordinal': ordinal,
        **{key: row[key] for key in ('text', 'conditions', 'explanation', 'questions', 'evidence')}}
    experience_ids = [bindings[number - 1]['experience_id'] for number in row['source_numbers']]
    revisions = {bindings[number - 1]['experience_id']: bindings[number - 1]['experience_revision']
        for number in row['source_numbers']}
    if (not _exact(data, expected_data) or not _exact(candidate.payload.get('generation'), _generation(generated['metadata']))
            or not _exact(candidate.payload.get('source_experience_ids'), experience_ids)
            or not _exact(candidate.payload.get('source_experience_revisions'), revisions)
            or candidate.payload.get('source_recognition_ids') != []
            or candidate.payload.get('initial_content', candidate.payload.get('content')) != row['text']
            or not _exact(candidate.payload.get('initial_conditions', candidate.payload.get('conditions')), row['conditions'])):
        raise RecognitionConflict('inspiration_generation_unavailable')
    return {'row': row, 'materials': materials, 'request': request}


def validate_inspiration_origin(reader, scope, record, *, require_proposal=True):
    candidate = reader.read('recognition_candidates', record.object_id)
    candidates = ([candidate] if candidate is not None else [row for row in reader.list('recognition_candidates')
        if row.payload.get('scope') == {'user_id': scope.user_id, 'project_id': scope.project_id}
        and row.payload.get('state') == 'published' and row.payload.get('recognition_id') == record.object_id])
    materials = []
    for candidate in candidates:
        try:
            origin = _origin(reader, scope, candidate, require_proposal=require_proposal)
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
            raise RecognitionConflict('inspiration_generation_unavailable') from error
        if origin is not None:
            materials.extend(material for material in origin['materials'] if material not in materials)
    return materials


def inspiration_details(reader, scope, record, aliases=()):
    for identity in (record.object_id, *aliases):
        if reader.read(DETAILS, identity) is None:
            continue
        candidate = reader.read('recognition_candidates', identity)
        if candidate is None:
            raise RecognitionConflict('inspiration_generation_unavailable')
        try:
            origin = _origin(reader, scope, candidate)
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
            raise RecognitionConflict('inspiration_generation_unavailable') from error
        row = origin['row']
        if record.payload.get('content') != row['text'] or not _exact(record.payload.get('conditions'), row['conditions']):
            return None
        return {key: deepcopy(row[key]) for key in ('explanation', 'questions', 'evidence')}
    return None
