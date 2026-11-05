from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_evidence_json import encoded
from backend.recognition.product_draft_dependencies import product_draft_source
from backend.shared.document_visibility import LegacyDocumentVisibility
from core.ai_kernel.product_admission import admission_identity
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider.record_lineage import HEAD_COLLECTIONS, RecordLineageError, verify_lineage
from core.storage_provider.sqlite_uow import begin_read

from ..source_egress import SourceEgressService
from ..transaction_records import TransactionRecords
from ..workbench_identity_binding import execution_owner
from .insight_validity import read_validity
from .material_origins import SharedMaterialOwner
from .outcome_source_reads import (
    SourceOwnerSelection, SourceReadRecords, bind_source_reader, outcome_scope, read_scope, source_scope,
)
from .links import similarity
from .policies import get
from .projects import scene_of


INPUTS = 'v2_outcome_inputs'
FRESH = 'v2_outcome_fresh'
ACK = 'v2_outcome_ack'
VERSION = 'stale@1'
UPDATES = 'v2_outcome_update_preparations'
UPDATE_KEYS = 'v2_outcome_update_keys'
_UPDATE_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')


class OutcomeFreshnessError(RecognitionConflict):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _identity(value):
    return {'algorithm': 'sha256', 'value': sha256(encoded(value).encode('utf-8')).hexdigest()}


def _matching_document_sources(sources, material, markdown):
    content = _identity(markdown)
    return [source for source in sources if source.get('verified')
        and source['frozen']['source_type'] == 'document' and source['frozen']['id'] == material['id']
        and source['frozen']['project_id'] == material['project_id']
        and ('namespace' not in material or source['origin']['namespace'] == material['namespace'])
        and source['frozen']['revision'] == material['revision'] and source['frozen']['content'] == content]


def _physical(path):
    value = Path(path).resolve(strict=True).stat()
    return [value.st_dev, value.st_ino]


def _lineage(reader, collection, identity):
    head = reader.read(HEAD_COLLECTIONS[collection], identity)
    if head is None or not isinstance(head.payload, dict) or not isinstance(head.payload.get('fact_id'), str):
        raise OutcomeFreshnessError('outcome_source_birth_unavailable')
    value = {'collection': collection, 'object_id': identity, 'fact_id': head.payload['fact_id']}
    verify_lineage(reader, value)
    return value


@dataclass(frozen=True)
class OutcomeSourceOwner:
    records: object
    documents: object
    sources: object
    root: Path
    namespace: str
    project_id: str
    validate_current: object


class _VisibilityRecords(TransactionRecords):
    def list_matching(self, collection, **fields):
        return tuple(row for row in self.list(collection)
            if all(row.payload.get(key) == value for key, value in fields.items()))


class OutcomeFreshness:
    def __init__(self, application, *, resolve_current_owner=None, now=utc_now):
        if resolve_current_owner is not None and not callable(resolve_current_owner):
            raise ValueError('outcome_source_owner_resolver_invalid')
        self.application = application
        state = application.state
        self.records, self.documents = state.recognition_records, state.recognition_documents
        self.root = Path(state.recognition_runtime_root).resolve(strict=True)
        self.owner = {'owner_user_id': getattr(state, 'server_user_id', None) or 'local-user',
            'namespace_id': self.documents.namespace_id, 'origin_kind': 'personal'}
        self.resolve_current_owner, self.now = resolve_current_owner, now

    def _validate(self):
        state = self.application.state
        if (state.recognition_records is not self.records or state.recognition_documents is not self.documents
                or Path(state.recognition_runtime_root).resolve(strict=True) != self.root
                or execution_owner(self.application, self.records, self.documents) != self.owner):
            raise OutcomeFreshnessError('outcome_execution_owner_changed')
        return getattr(state, 'ai_turn_store', None)

    def _native(self):
        native = self._validate()
        if (not isinstance(native, SQLiteAITurnStore)
                or Path(native._path).resolve() != self.root / '.rebuild-data/ai-turns.sqlite3'):
            raise OutcomeFreshnessError('outcome_native_owner_unavailable')
        return native

    def _reader(self, reader):
        view = bind_source_reader(self, reader)
        if (not view.connection.in_transaction
                or Path(view.database_path).resolve(strict=True) != Path(self.records.database_path).resolve(strict=True)):
            raise OutcomeFreshnessError('outcome_reader_owner_changed')
        return view

    @contextmanager
    def _read_scope(self, reader=None, *, request=None):
        self._validate()
        with read_scope(self, request=request, reader=reader) as transaction:
            yield self._reader(transaction)

    def source_scope(self, *, request=None, reader=None):
        return source_scope(self, request=request, reader=reader)

    def outcome_scope(self, *, project, document_id, reader=None):
        return outcome_scope(self, project=project, document_id=document_id, reader=reader)

    def _source_owner(self, project, *, origin=None, binding=None, reader=None):
        personal = f"personal/{self.owner['owner_user_id']}/{self.owner['namespace_id']}"
        if origin is None or origin.get('namespace') == personal:
            return OutcomeSourceOwner(self.records, self.documents,
                self.application.state.workspace_domains.query.source_store, self.root, personal, project, self._validate)
        if self.resolve_current_owner is None:
            raise OutcomeFreshnessError('outcome_source_owner_unavailable')
        view = self._reader(reader) if reader is not None else None
        selection = SourceOwnerSelection(view, project, origin.get('namespace'))
        owner = self.resolve_current_owner(self.application, selection, mode='freshness_read')
        if not isinstance(owner, SharedMaterialOwner) or owner.project_id != project:
            raise OutcomeFreshnessError('outcome_source_owner_invalid')
        owner.validate_current()
        return owner

    def _material_owner(self, material, reader):
        namespace = material.get('namespace')
        if isinstance(reader, SourceReadRecords) and namespace is None:
            raise OutcomeFreshnessError('outcome_material_namespace_unavailable')
        return self._source_owner(material['project_id'],
            origin={'namespace': namespace} if namespace is not None else None, reader=reader)

    def _source_reader(self, owner, reader):
        view = self._reader(reader)
        if isinstance(view, SourceReadRecords):
            return view.source_reader(owner)
        if owner.records is not self.records:
            raise OutcomeFreshnessError('outcome_source_scope_required_before_actor_transaction')
        return view

    def _source_authority(self, owner, reader):
        return SourceEgressService(owner if isinstance(owner, SharedMaterialOwner) else owner.records,
            reader=reader, _memo={})

    def _actor(self):
        from backend.security.user_context import USER_ACCESS
        self._validate()
        access = USER_ACCESS.get()
        return {'user_id': access.caller.user_id, 'by': access.by} if access is not None else {
            'user_id': 'local-user', 'by': None}

    def _origin(self, owner):
        owner.validate_current()
        return {'namespace': owner.namespace, 'project_id': owner.project_id,
            'root_identity': _physical(owner.root), 'database_identity': _physical(owner.records.database_path)}

    def _source(self, owner, reader, kind, identity, revision=None):
        owner.validate_current()
        project = owner.project_id
        forgotten, replaced, expires = False, None, None
        if kind == 'original_source':
            before = (owner.sources.revision('sources', identity), owner.sources.incarnation('sources', identity))
            value = owner.sources.read('sources', identity)
            after = (owner.sources.revision('sources', identity), owner.sources.incarnation('sources', identity))
            if value is None or before != after or value.get('project_id', 'default') != project:
                raise OutcomeFreshnessError('outcome_source_unavailable')
            if revision is not None and revision != after[0]:
                raise OutcomeFreshnessError('outcome_source_revision_changed')
            current_revision, incarnation = after
            if not isinstance(incarnation, str) or not incarnation or incarnation == 'legacy':
                raise OutcomeFreshnessError('outcome_source_birth_unavailable')
            content = value.get('metadata', {}).get('content_snapshot')
            label, title = 'source', value.get('title', '')
        else:
            collection = {'recognition': 'recognitions', 'experience': 'recognition_experiences',
                'document': 'documents', 'original_item': 'workspace_items'}[kind]
            row = reader.read(collection, identity)
            if row is None:
                raise OutcomeFreshnessError('outcome_source_unavailable')
            value = row.payload
            own = value.get('scope') if kind in {'recognition', 'experience'} else {
                'user_id': 'local-user', 'project_id': value.get('project_id')}
            if own != {'user_id': 'local-user', 'project_id': project}:
                raise OutcomeFreshnessError('outcome_source_scope_changed')
            if kind in {'recognition', 'experience'} and value.get('state') != 'active':
                raise OutcomeFreshnessError('outcome_source_qualification_unavailable')
            if kind == 'recognition':
                from ..source_egress import recognition_service
                service = (recognition_service(reader, source_owner=owner) if isinstance(owner, SharedMaterialOwner)
                    else self.application.state.recognition_service)
                qualified = service._qualified_recognition(reader, row)
                if not qualified.authorized or not qualified.source_evidence_complete:
                    raise OutcomeFreshnessError('outcome_source_qualification_unavailable')
            incarnation = _lineage(reader, collection, identity)
            current_revision = row.revision
            if kind == 'document':
                documents = SQLiteDocumentRepository(reader, namespace_id=owner.documents.namespace_id)
                document = documents.read(identity)
                if document.get('status') == 'archived':
                    raise OutcomeFreshnessError('outcome_source_qualification_unavailable')
                current_revision = document['revision'] if revision is None else revision
                if type(current_revision) is not int or not 0 < current_revision <= document['revision']:
                    raise OutcomeFreshnessError('outcome_source_revision_changed')
                content = documents.markdown(identity, revision=current_revision)
                label, title = 'note', document.get('title', '')
                recall = reader.read('v2_document_recall', identity)
            elif kind == 'original_item':
                content, label, title = value.get('source_text'), 'source', value.get('title', '')
                recall = None
            else:
                content = {'content': value.get('content'), 'conditions': value.get('conditions', [])}
                if not isinstance(content['content'], str):
                    raise OutcomeFreshnessError('outcome_source_content_unavailable')
                label, title = ('insight' if kind == 'recognition' else 'inspiration'), value.get('content', '')[:80]
                recall = reader.read('recognition_recall_preferences', identity) if kind == 'recognition' else None
                if kind == 'recognition':
                    validity = read_validity(reader, identity) or {}
                    replaced, expires = validity.get('superseded_by'), validity.get('valid_until')
            if kind != 'document' and revision is not None and revision != current_revision:
                raise OutcomeFreshnessError('outcome_source_revision_changed')
            forgotten = bool(recall and recall.payload.get('state') == 'forgotten' and recall.payload.get('by', 'user') != 'auto')
        if content is None or type(current_revision) is not int or current_revision < 1:
            raise OutcomeFreshnessError('outcome_source_content_unavailable')
        owner.validate_current()
        return {'kind': label, 'source_type': kind, 'id': identity, 'project_id': project,
            'revision': current_revision, 'incarnation': incarnation, 'content': _identity(content),
            'title': str(title or ''), 'created_at': value.get('created_at'), 'available': True,
            'manual_forgotten': forgotten, 'replaced_by': replaced, 'expires_at': expires}

    def _freeze_material(self, owner, material, *, reader=None):
        from ..original_sources import resolve_turn_material
        if reader is None and isinstance(owner, SharedMaterialOwner):
            raise OutcomeFreshnessError('outcome_source_scope_required_before_actor_transaction')
        context = begin_read(self.records) if reader is None else nullcontext(reader)
        with context as transaction:
            view = self._source_reader(owner, transaction)
            descriptor = {key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
            scope = WorkScope('local-user', material['project_id'])
            _, roots = resolve_turn_material(view, scope, descriptor)
            authority = self._source_authority(owner, view)
            if material.get('snapshot') is not None:
                authority.validate_snapshot(scope, material['snapshot'])
            snapshot = authority.snapshot(scope, roots)
            selected = [(material['type'], material['id'], material['revision'])]
            if material['type'] == 'experience':
                row = view.read('recognition_experiences', material['id'])
                refs = row.payload.get('provenance', {}).get('source_refs', []) if row else []
                selected += [('document', ref['id'], ref['revision']) for ref in refs
                    if ref.get('type') == 'document' and type(ref.get('revision')) is int]
            result = []
            for kind, identity, revision in selected:
                try:
                    frozen = self._source(owner, view, kind, identity, revision)
                    if kind == 'original_source':
                        nodes = [node for node in snapshot['nodes'] if node['type'] == kind and node['id'] == identity]
                        if len(nodes) != 1 or nodes[0].get('incarnation') != frozen['incarnation']:
                            raise OutcomeFreshnessError('outcome_source_birth_changed')
                    result.append({'material': descriptor, 'origin': self._origin(owner), 'snapshot': snapshot,
                        'frozen': frozen, 'aliases': self._aliases(frozen, owner.namespace), 'verified': True,
                        'proof_ref': deepcopy(material['proof_ref']), 'role': material['role']})
                except (OutcomeFreshnessError, RecordLineageError) as error:
                    result.append({'material': descriptor, 'origin': self._origin(owner), 'verified': False,
                        'reason': str(error), 'proof_ref': deepcopy(material['proof_ref']), 'role': material['role']})
            return result

    def _source_text(self, owner, reader, source):
        frozen = source['frozen']
        kind, identity = frozen['source_type'], frozen['id']
        if kind == 'document':
            text = SQLiteDocumentRepository(reader, namespace_id=owner.documents.namespace_id).markdown(
                identity, revision=frozen['revision'])
            content, conditions = text, []
        elif kind == 'original_source':
            value = owner.sources.read('sources', identity)
            text = value.get('metadata', {}).get('content_snapshot') if value else None
            content, conditions = text, []
        else:
            collection = {'recognition': 'recognitions', 'experience': 'recognition_experiences',
                'original_item': 'workspace_items'}[kind]
            row = reader.read(collection, identity)
            if row is None or row.revision != frozen['revision']:
                raise OutcomeFreshnessError('outcome_update_source_changed')
            text = row.payload.get('source_text') if kind == 'original_item' else row.payload.get('content')
            conditions = row.payload.get('conditions', []) if kind in {'recognition', 'experience'} else []
            content = {'content': text, 'conditions': conditions} if kind in {'recognition', 'experience'} else text
        if not isinstance(text, str) or _identity(content) != frozen['content']:
            raise OutcomeFreshnessError('outcome_update_source_body_changed')
        return {'text': text, 'conditions': deepcopy(conditions)}

    @staticmethod
    def _aliases(source, namespace):
        from ..source_evidence_refs import qualified_material_ref
        collection = {'recognition': 'recognitions', 'experience': 'recognition_experiences',
            'document': 'documents', 'original_item': 'workspace', 'original_source': 'sources'}[source['source_type']]
        result = {f"crp://{namespace}/{collection}/{source['id']}"}
        result.add(qualified_material_ref({'type': source['source_type'], 'id': source['id'],
            'revision': source['revision'], 'project_id': source['project_id'], 'namespace': namespace}))
        if namespace.startswith('personal/'):
            result.add(f"crp://default/{collection}/{source['id']}")
        if source['source_type'] == 'document':
            result.update((f"document://{source['id']}", f"crp://default/documents/{source['id']}.json"))
        elif source['source_type'] == 'original_source':
            result.add(f"source://{source['id']}")
        return sorted(result)

    def prepare_input_basis(self, **options):
        from .outcome_source_preparation import prepare
        return prepare(self, **options)

    def _read_input_preparation(self, *, request, input_kind, preparation_ref, input_ref=None):
        from .outcome_source_preparation import read
        value = read(self._native(), request, preparation_ref, input_kind=input_kind, input_ref=input_ref)
        if value['owner'] != self.owner:
            raise OutcomeFreshnessError('outcome_preparation_owner_changed')
        return value

    def validate_input_preparation(self, *, request, input_kind, preparation_ref, input_ref=None, reader=None):
        from .outcome_source_preparation import answer_input_context, _task_context
        value = self._read_input_preparation(request=request, input_kind=input_kind,
            preparation_ref=preparation_ref, input_ref=input_ref)
        with self._read_scope(reader, request=value['source_request']) as view:
            context = (answer_input_context(self, self._native(), request, input_kind, value['messages'], view)
                if request['desired_outcome'] == 'project.answer'
                else _task_context(self, self._native(), request, value['source_request'], value['messages'], view))
            if context != value['context']:
                raise OutcomeFreshnessError('outcome_prepared_context_changed')
            sources = []
            for material in [*context['materials'], *context.get('privacy_materials', [])]:
                owner = self._material_owner(material, view)
                sources.extend(self._freeze_material(owner, material, reader=view))
            sources.extend(deepcopy(context.get('inherited_sources', [])))
            if sources != value['sources']:
                raise OutcomeFreshnessError('outcome_prepared_source_changed')
            for source in sources:
                if source.get('verified') and get('stale', version='@1').changed(
                        source['frozen'], source['frozen'], reference=self.now())['reasons']:
                    raise OutcomeFreshnessError('outcome_prepared_source_expired')
        return value

    def capture_input_basis(self, *, request, input_kind, input_ref, preparation_ref=None, model_request_id=None):
        from .outcome_source_preparation import BASIS, ANSWER_BINDING
        native = self._native()
        original = native.get_immutable_payload(request['turn_id'], input_kind)
        if original is None or original[0] != input_ref:
            raise OutcomeFreshnessError('outcome_final_input_unavailable')
        reference = original[1].get('outcome_source_preparation_ref')
        if request['desired_outcome'] == 'project.answer':
            if (not isinstance(preparation_ref, str) or not isinstance(model_request_id, str) or not model_request_id
                    or not any(event['type'] == 'model.requested'
                        and event['correlation']['model_request_id'] == model_request_id
                        and event['data'].get('model_call_purpose') == original[1].get('purpose')
                        for event in native.events_after(request['turn_id']))):
                raise OutcomeFreshnessError('outcome_answer_model_owner_changed')
            reference = preparation_ref
            native.get_or_create_immutable_payload(request['turn_id'], ANSWER_BINDING + input_kind,
                {'turn_id': request['turn_id'], 'input_kind': input_kind, 'input_ref': input_ref,
                    'preparation_ref': reference, 'model_request_id': model_request_id})
        value = self.validate_input_preparation(request=request, input_kind=input_kind,
            preparation_ref=reference, input_ref=input_ref)
        payload = {'version': 3, 'turn_id': request['turn_id'], 'owner': deepcopy(self.owner),
            'request_identity': admission_identity(request), 'preparation_ref': reference,
            'model_request_id': model_request_id if request['desired_outcome'] == 'project.answer' else original[1].get('model_request_id'),
            'input_kind': input_kind, 'input_ref': input_ref, 'sources': value['sources'],
            'scope_origin': value['scope_origin']}
        return native.get_or_create_immutable_payload(request['turn_id'], BASIS + input_kind, payload)

    def _basis(self, reference):
        from .outcome_source_preparation import BASIS
        native = self._native()
        value = native.get(reference)
        if (not isinstance(value, dict) or value.get('version') != 3 or value.get('owner') != self.owner
                or not isinstance(value.get('input_kind'), str)
                or native.get_immutable_payload(value['turn_id'], BASIS + value['input_kind']) != (reference, value)):
            raise OutcomeFreshnessError('outcome_basis_changed')
        request = native.get_request(value['turn_id'])
        prepared = self._read_input_preparation(request=request, input_kind=value['input_kind'],
            preparation_ref=value['preparation_ref'], input_ref=value['input_ref'])
        if (value['request_identity'] != admission_identity(request) or value['sources'] != prepared['sources']
                or value['scope_origin'] != prepared['scope_origin']):
            raise OutcomeFreshnessError('outcome_basis_changed')
        if request['desired_outcome'] == 'project.answer':
            from .outcome_source_preparation import ANSWER_BINDING
            binding = native.get_immutable_payload(request['turn_id'], ANSWER_BINDING + value['input_kind'])
            if binding is None or binding[1]['model_request_id'] != value.get('model_request_id'):
                raise OutcomeFreshnessError('outcome_basis_changed')
        elif native.get(value['input_ref']).get('model_request_id') != value.get('model_request_id'):
            raise OutcomeFreshnessError('outcome_basis_changed')
        return value

    def _consumed_basis(self, reference):
        basis = self._basis(reference)
        native = self._native()
        request = native.get_request(basis['turn_id'])
        from core.ai_kernel.product_snapshot import read_model_attempt_bindings
        from core.ai_kernel.contracts import validate_model_wire_attempt_dispatch
        from ..kernel.route_source_snapshot import native_source_connection
        with native_source_connection(self.records) as connection:
            attempted = [attempt for attempt in read_model_attempt_bindings(connection, basis['turn_id'])
                if attempt['model_request_id'] == basis.get('model_request_id') and attempt['dispatch'] is not None]
        if not attempted:
            raise OutcomeFreshnessError('outcome_task_input_not_dispatched')
        for attempt in attempted:
            dispatch = validate_model_wire_attempt_dispatch(attempt['dispatch'])
            if dispatch['turn_id'] != basis['turn_id'] or dispatch['model_request_id'] != basis['model_request_id']:
                raise OutcomeFreshnessError('outcome_model_input_owner_changed')
        if request['desired_outcome'] != 'project.answer':
            from .task_model_inputs import WIRE_PREFIX, read_task_model_input
            composition = self.application.state.agent_runtime_composition
            consumed = []
            for attempt in attempted:
                wire = native.get_immutable_payload(basis['turn_id'], WIRE_PREFIX + attempt['attempt_id'])
                if wire is None or not isinstance(wire[1].get('input_ref'), str):
                    raise OutcomeFreshnessError('outcome_model_input_owner_changed')
                reference = wire[1]['input_ref']
                read_task_model_input(native, composition.store, basis['turn_id'], reference,
                    attempt_id=attempt['attempt_id'])
                if reference == basis['input_ref']:
                    consumed.append(attempt)
            if not consumed:
                raise OutcomeFreshnessError('outcome_task_input_not_dispatched')
        return basis

    def _completed(self, request):
        from core.ai_kernel.turn_receipt_projection import receipt_from_events
        native = self._native()
        if native.get_request(request['turn_id']) != request:
            raise OutcomeFreshnessError('outcome_native_request_changed')
        events = tuple(native.events_after(request['turn_id']))
        receipt = receipt_from_events(events, turn_id=request['turn_id'], request=request)
        if (not events or receipt.status != 'completed' or events[-1]['type'] != 'turn.completed'
                or not isinstance(events[-1]['data'].get('evidence_refs'), list)):
            raise OutcomeFreshnessError('outcome_completed_evidence_unavailable')
        return events[-1]

    def completion_basis_refs(self, native_request, *, reader=None):
        from ..research_packets import authority_stores
        from ..research_reads import turn_tree
        with self._read_scope(reader) as view:
            turns, agents = authority_stores(view)
            allowed = set(turn_tree(turns, agents, native_request['turn_id'], native_request['scope']['project_id']))
            return self._completion_basis_refs(view, allowed)

    def _completion_basis_refs(self, view, allowed):
        from ..kernel.route_source_snapshot import native_source_connection
        from .outcome_source_preparation import BASIS
        with native_source_connection(self.records, reader=view) as connection:
            rows = list(connection.execute('SELECT turn_id,payload_ref FROM ai_turn_immutable_payloads '
                'WHERE turn_id IN (' + ','.join('?' for _ in allowed) + ') AND kind LIKE ? ORDER BY rowid',
                (*sorted(allowed), BASIS + '%')))
        selected = []
        for identity, reference in rows:
            if identity not in allowed:
                continue
            try:
                self._consumed_basis(reference)
            except OutcomeFreshnessError as error:
                if str(error) != 'outcome_task_input_not_dispatched':
                    raise
                continue
            selected.append(reference)
        return selected

    def _completion_context(self, native_request, references):
        from .outcome_source_preparation import read_model_context
        native, materials, proofs, sources = self._native(), [], [], []
        for reference in references:
            basis = self._consumed_basis(reference)
            request = native.get_request(basis['turn_id'])
            context = read_model_context(native, request, input_ref=basis['input_ref'], input_kind=basis['input_kind'])
            materials.extend(context['materials'])
            sources.extend({'basis_ref': reference, **source} for source in context.get('source_proofs', []))
            proofs.append({'basis_ref': reference, 'turn_id': basis['turn_id'], 'input_ref': basis['input_ref']})
        materials = list({encoded(item): item for item in materials}.values())
        return {'turn_id': native_request['turn_id'], 'materials': materials,
            'recognitions': [item for item in materials if item['type'] == 'recognition'],
            'proof_refs': proofs, 'source_proofs': sources}

    def completion_context(self, native_request, *, reader=None):
        return self._completion_context(native_request, self.completion_basis_refs(native_request, reader=reader))

    def validate_material_documents(self, reader, draft):
        from ..original_sources import resolve_turn_material
        from .outcome_source_reads import _shared_request
        view, native = self._reader(reader), self._native()
        request = native.get_request(draft.request['turn_id'])
        if (request is None or request.get('desired_outcome') != 'project.task'
                or admission_identity(request) != admission_identity(draft.request)):
            raise OutcomeFreshnessError('outcome_document_native_owner_changed')
        shared = _shared_request(self, request)
        if shared:
            self.shared_task_sources(view, draft)
            return True
        sources = list(self.completion_context(request, reader=view)['source_proofs'])
        product = json.loads(request['input']['text'])
        outcome = product.get('outcome_input') if isinstance(product, dict) else None
        basis = outcome.get('update_basis') if isinstance(outcome, dict) else None
        if basis is not None:
            if not isinstance(basis, dict) or basis.get('owner') != self.owner:
                raise OutcomeFreshnessError('outcome_update_owner_changed')
            prepared = view.read(UPDATES, basis.get('preparation_id', ''))
            if (prepared is None or prepared.revision != 1 or prepared.payload.get('owner') != self.owner
                    or prepared.payload.get('result', {}).get('update_basis') != basis
                    or prepared.payload.get('result', {}).get('context_ids') != basis.get('context_ids')):
                raise OutcomeFreshnessError('outcome_update_preparation_changed')
            prior = self._fact(view, basis['project_id'], basis['document_id'])
            if prior is None or prior['basis_refs'] != basis.get('basis_refs'):
                raise OutcomeFreshnessError('outcome_update_source_unverifiable')
            for reference in prior['basis_refs']:
                sources.extend(self._consumed_basis(reference)['sources'])
        for retained in draft.material_documents:
            material = retained['material']
            own = WorkScope('local-user', material['project_id'])
            resolve_turn_material(view, own, material)
            historical = view.read('document_revisions', material['id'] + '~r' + str(material['revision']))
            markdown = view.read('document_markdown', material['id'] + '~r' + str(material['revision']))
            if (historical is None or markdown is None
                    or historical.revision != retained['document_revision_record_revision']
                    or markdown.revision != retained['markdown_record_revision']
                    or markdown.payload.get('markdown') != retained['markdown']
                    or historical.payload.get('source_snapshot', {}).get('source_refs') != retained['source_refs']):
                raise OutcomeFreshnessError('outcome_document_material_changed')
            matches = _matching_document_sources(sources, material, retained['markdown'])
            lineage = _lineage(view, 'documents', material['id'])
            if not matches or any(source['frozen']['incarnation'] != lineage for source in matches):
                raise OutcomeFreshnessError('outcome_document_birth_changed')
        return True

    def shared_task_sources(self, reader, draft):
        from ..answer_effective import NativeAnswerSnapshot
        from ..kernel.route_source_snapshot import native_source_connection
        from ..source_graph import SourceGraph
        from core.ai_kernel.product_snapshot import read_product_turn_snapshot
        from .shared_history import historical_origin
        from .outcome_source_reads import task_dependency_scope
        from .privacy import is_private_project
        with task_dependency_scope(self, draft.request['turn_id']), self._read_scope(reader) as view:
            if not isinstance(view, SourceReadRecords) or draft.material_proof is None:
                raise OutcomeFreshnessError('shared_task_source_scope_unavailable')
            with native_source_connection(self.records, reader=view) as connection:
                snapshot = NativeAnswerSnapshot(read_product_turn_snapshot(connection, draft.request['turn_id']))
            actual = snapshot.get_request(snapshot.turn_id)
            if admission_identity(actual) != admission_identity(draft.request):
                raise OutcomeFreshnessError('shared_task_source_native_changed')
            historical_origin(view.query, snapshot)
            self._completed(actual)
            context = self.completion_context(actual, reader=view)
            for material, frozen in zip(draft.qualified_materials, draft.snapshots, strict=True):
                descriptor = {key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
                if not any(source.get('verified') and source.get('role') == 'initial_admission'
                        and source['material'] == descriptor and source['origin']['namespace'] == material['namespace']
                        and source['snapshot'] == frozen for source in context['source_proofs']):
                    raise OutcomeFreshnessError('shared_task_initial_source_ceiling_unavailable')
            from .steer_learning_privacy import publication_privacy_active, qualify_publication_sources
            qualified = (qualify_publication_sources(self, context['source_proofs'], reader=view, local_only=True)
                if publication_privacy_active(self, view) else
                self._qualify_source_proofs(context['source_proofs'], reader=view, local_only=True))
            if not qualified['complete']:
                raise OutcomeFreshnessError('shared_task_source_unverifiable')
            graph = SourceGraph()
            for source in qualified['privacy_ceiling']['sources']:
                graph.snapshot(source['current'], namespace=source['origin']['namespace'])
                origins = source.get('history_origins', [source['origin']] * len(source['history_current']))
                if not isinstance(origins, list) or len(origins) != len(source['history_current']):
                    raise OutcomeFreshnessError('shared_task_history_namespace_changed')
                for current, origin in zip(source['history_current'], origins, strict=True):
                    owner = self._source_owner(origin['project_id'], origin=origin, reader=view)
                    if self._origin(owner) != origin:
                        raise OutcomeFreshnessError('shared_task_history_namespace_changed')
                    graph.snapshot(current, namespace=origin['namespace'])
            for binding in context['proof_refs']:
                basis = self._consumed_basis(binding['basis_ref'])
                prepared = self._read_input_preparation(request=self._native().get_request(basis['turn_id']),
                    input_kind=basis['input_kind'], preparation_ref=basis['preparation_ref'], input_ref=basis['input_ref'])
                for proof in prepared['context']['proof_refs']:
                    if isinstance(proof, dict) and 'shared_read' in proof:
                        graph.add(deepcopy(proof['shared_read']), root=True)
            purposes = set(qualified['privacy_ceiling']['effective_purposes'])
            for namespace, current in self._task_restoration_sources(view, draft, actual):
                graph.snapshot(current, namespace=namespace)
                for node in current['nodes']:
                    purposes.intersection_update(node['effective_purposes'])
            if is_private_project(view.query.records, actual['scope']['project_id']):
                purposes.clear()
            view.validate(self)
            return {'graph': graph.result(), 'effective_purposes': sorted(purposes)}

    def _task_restoration_sources(self, reader, draft, request):
        restored = draft
        while restored.restoration is not None:
            source = restored.source
            if source is None:
                raise OutcomeFreshnessError('shared_task_restoration_source_unavailable')
            birth = ({'source_id': source.restoration['operation_id'], 'locator': 'outcome://' + source.restoration['operation_id']}
                if source.restoration is not None else {'source_id': source.public_turn_id,
                    'locator': 'conversation://' + source.public_turn_id} if source.answer_birth is not None else
                {'source_id': source.revisions['product_turn_id'], 'locator': 'task://' + source.revisions['product_turn_id']})
            references = [reference for reference in restored.restoration['source_refs'] if reference != birth]
            yield from self._task_restoration_materials(reader, request, references)
            restored = source

    def _task_restoration_materials(self, reader, request, references):
        from ..original_sources import document_roots, resolve_turn_material
        from .shared_task_reads import _qualified_reference_material
        from .shared_task_materials import _actor_material
        if not isinstance(reader, SourceReadRecords):
            raise OutcomeFreshnessError('shared_task_restoration_source_scope_unavailable')
        reader.validate(self)
        query = reader.query
        for reference in references:
            locator = reference.get('locator') if isinstance(reference, dict) else None
            if isinstance(locator, str) and locator.startswith('crp://'):
                material = _qualified_reference_material(query, request, reader, locator)
                if reference.get('source_id') != material['id']:
                    raise OutcomeFreshnessError('shared_task_restoration_reference_changed')
                materials = [material]
            else:
                roots = document_roots(reader._plain, WorkScope('local-user', request['scope']['project_id']), [reference])
                materials = [{'type': kind, 'id': identity, 'revision': revision,
                    'project_id': request['scope']['project_id'], 'namespace': query.origin_admission.personal['namespace']}
                    for kind, identity, revision in roots]
            if not materials:
                raise OutcomeFreshnessError('shared_task_restoration_reference_unavailable')
            for material in materials:
                owner = self._material_owner(material, reader)
                actual = self._source_reader(owner, reader)
                if (material['namespace'] == query.origin_admission.personal['namespace']
                        and material['project_id'] != 'me'):
                    _actor_material(query, request, material, reader._plain)
                descriptor = {key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
                own = WorkScope('local-user', material['project_id'])
                _, roots = resolve_turn_material(actual, own, descriptor)
                yield owner.namespace, self._source_authority(owner, actual).snapshot(own, roots)

    def _historical_privacy(self, reader, source, *, with_origins=False):
        from ..source_graph import SourceGraph
        from ..original_sources import original
        graph = SourceGraph()
        graph.snapshot(source['snapshot'])
        snapshots = []
        for node in graph.result()['nodes']:
            if node['kind'] != 'material':
                continue
            scope = WorkScope(**node['scope'])
            owner = self._source_owner(scope.project_id,
                origin={'namespace': node['namespace']} if 'namespace' in node else
                    source['origin'] if scope.project_id == source['origin']['project_id'] else None,
                binding=source, reader=reader)
            source_reader = self._source_reader(owner, reader)
            if node['type'] in {'original_item', 'original_source'}:
                current = original(source_reader, scope, node['type'], node['id'])
                if (node['type'] == 'original_source'
                        and node.get('incarnation') != current.payload.get('_original_incarnation')):
                    raise OutcomeFreshnessError('outcome_source_birth_changed')
            else:
                current = source_reader.read({'recognition': 'recognitions', 'experience': 'recognition_experiences'}[node['type']], node['id'])
            if current is None:
                raise OutcomeFreshnessError('outcome_source_unavailable')
            snapshot = self._source_authority(owner, source_reader).snapshot(scope,
                [{'type': node['type'], 'id': node['id'], 'revision': current.revision}])
            snapshots.append((snapshot, self._origin(owner)))
        pairs = list({encoded({'snapshot': snapshot, 'origin': origin}): (snapshot, origin)
            for snapshot, origin in snapshots}.values())
        return pairs if with_origins else [snapshot for snapshot, _ in pairs]

    def _qualify_source_proofs(self, proofs, *, reader, local_only):
        from ..original_sources import resolve_turn_material
        purposes, ceilings, recognitions, current_sources, unknown = {'generation', 'embedding', 'rerank'}, [], [], [], []
        for source in proofs:
            if not source.get('verified'):
                unknown.append({'reason': source.get('reason', 'outcome_source_proof_unavailable')})
                purposes.clear()
                continue
            frozen, origin = source['frozen'], source['origin']
            try:
                owner = self._source_owner(origin['project_id'], origin=origin, binding=source, reader=reader)
                if self._origin(owner) != origin:
                    raise OutcomeFreshnessError('outcome_source_owner_changed')
                source_reader = self._source_reader(owner, reader)
                current = self._source(owner, source_reader, frozen['source_type'], frozen['id'])
                if current['incarnation'] != frozen['incarnation']:
                    raise OutcomeFreshnessError('outcome_source_birth_changed')
                descriptor = {'type': frozen['source_type'], 'id': frozen['id'],
                    'revision': current['revision'], 'project_id': frozen['project_id']}
                _, roots = resolve_turn_material(source_reader, WorkScope('local-user', frozen['project_id']), descriptor)
                snapshot = self._source_authority(owner, source_reader).snapshot(
                    WorkScope('local-user', frozen['project_id']), roots)
                history_pairs = self._historical_privacy(reader, source, with_origins=True)
                history_current = [item[0] for item in history_pairs]
                for node in [*source['snapshot']['nodes'], *snapshot['nodes'],
                             *(node for previous in history_current for node in previous['nodes'])]:
                    purposes.intersection_update(node['effective_purposes'])
                changed = current['revision'] != frozen['revision'] or current['content'] != frozen['content']
                reasons = get('stale', version='@1').changed(frozen, current, reference=self.now())['reasons']
                current_sources.append({'material': descriptor, 'snapshot': snapshot, 'origin': origin,
                    'privacy_only': changed or bool(reasons) or source.get('role') in {'initial_admission', 'redo_inheritance'}})
                ceiling = {'origin': origin, 'source': descriptor, 'previous': source['snapshot'],
                    'current': snapshot, 'history_current': history_current, 'aliases': deepcopy(source['aliases'])}
                if isinstance(reader, SourceReadRecords) or any(item[1] != origin for item in history_pairs):
                    ceiling['history_origins'] = [item[1] for item in history_pairs]
                ceilings.append(ceiling)
                if (frozen['source_type'] == 'recognition' and not changed and not reasons
                        and source.get('role') not in {'initial_admission', 'redo_inheritance'}):
                    recognitions.append({**descriptor, 'snapshot': snapshot, 'incarnation': current['incarnation']})
            except (RecognitionConflict, RecordLineageError, FileNotFoundError, PermissionError, sqlite3.Error) as error:
                unknown.append({'id': frozen['id'], 'project_id': frozen['project_id'],
                    'reason': str(error) if isinstance(error, OutcomeFreshnessError) else 'outcome_source_unavailable'})
                purposes.clear()
        if not local_only and 'generation' not in purposes:
            raise OutcomeFreshnessError('private_source_remote_blocked')
        return {'complete': not unknown, 'unknown': unknown,
            'current_sources': list({encoded(item): item for item in current_sources}.values()),
            'recognitions': list({encoded(item): item for item in recognitions}.values()),
            'privacy_ceiling': {'effective_purposes': sorted(purposes), 'sources': ceilings}}

    def _learning_context_proofs(self, context, *, reader):
        from .outcome_source_preparation import read_model_context
        view, native = self._reader(reader), self._native()
        if not isinstance(context, dict) or not isinstance(context.get('turn_id'), str):
            raise OutcomeFreshnessError('learning_context_invalid')
        request = native.get_request(context['turn_id'])
        if request is None:
            raise OutcomeFreshnessError('learning_context_native_unavailable')
        if context.get('input_ref') is not None:
            canonical = read_model_context(native, request, input_ref=context['input_ref'], input_kind=context.get('input_kind'))
            if encoded(canonical) != encoded(context):
                raise OutcomeFreshnessError('learning_context_binding_changed')
            basis = native.get_immutable_payload(context['turn_id'],
                'outcome-input-basis-v3-' + canonical['input_kind'])
            if basis is None:
                raise OutcomeFreshnessError('learning_context_source_proof_required')
            self._consumed_basis(basis[0])
            proofs = canonical['source_proofs']
        elif context.get('plan_ref') is not None:
            canonical = read_model_context(native, request)
            if encoded(canonical) != encoded(context):
                raise OutcomeFreshnessError('learning_context_binding_changed')
            completed = self.completion_context(request, reader=view)
            wanted = [{key: item[key] for key in ('type', 'id', 'revision', 'project_id')} for item in context['materials']]
            proofs = [source for source in completed['source_proofs'] if source.get('material') in wanted]
            if any(not any(source.get('material') == material for source in proofs) for material in wanted):
                raise OutcomeFreshnessError('learning_context_source_proof_required')
        elif context.get('context_state') == 'not_yet_consumed':
            sequence = context.get('observed_sequence')
            events = tuple(native.events_after(context['turn_id']))
            canonical = {'turn_id': context['turn_id'], 'plan_ref': None, 'input_ref': None,
                'materials': [], 'recognitions': [], 'proof_refs': [], 'context_state': 'not_yet_consumed',
                'observed_sequence': sequence}
            if (type(sequence) is not int or not events or not 1 <= sequence <= events[-1]['sequence']
                    or any(event['type'] == 'model.attempt.dispatched' and event['sequence'] <= sequence for event in events)
                    or encoded(canonical) != encoded(context)):
                raise OutcomeFreshnessError('learning_context_binding_changed')
            proofs = []
        else:
            canonical = self.completion_context(request, reader=view)
            if encoded(canonical) != encoded(context):
                raise OutcomeFreshnessError('learning_context_binding_changed')
            proofs = canonical['source_proofs']
        return proofs

    def qualify_learning_context(self, context, *, reader, local_only):
        if type(local_only) is not bool:
            raise OutcomeFreshnessError('learning_context_invalid')
        view = self._reader(reader)
        proofs = self._learning_context_proofs(context, reader=view)
        if not proofs:
            from .outcome_source_reads import _shared_request
            request = self._native().get_request(context.get('turn_id'))
            if request is not None and _shared_request(self, request):
                raise OutcomeFreshnessError('outcome_shared_empty_context_privacy_unavailable')
        return self._qualify_source_proofs(proofs, reader=view, local_only=local_only)

    def qualify_published_learning_context(self, context, *, reader, publication_proof, local_only):
        from .steer_learning_publication import require_published_learning_context
        from .steer_learning_privacy import publication_privacy_scope, qualify_publication_sources
        if type(local_only) is not bool:
            raise OutcomeFreshnessError('learning_context_invalid')
        view = self._reader(reader)
        require_published_learning_context(publication_proof, freshness=self, reader=view, context=context)
        with publication_privacy_scope(self, view, context, publication_proof=publication_proof):
            proofs = self._learning_context_proofs(context, reader=view)
            return qualify_publication_sources(self, proofs, reader=view, local_only=local_only)

    def capture_actual_outcome_inputs(self, tx, *, project, document_id, public_turn_id,
                                     native_request, evidence_refs, basis_refs):
        view = self._reader(tx)
        self._validate()
        bound = product_draft_source(view, WorkScope('local-user', project), document_id, 1)
        if bound is None or bound.public_turn_id != public_turn_id or bound.request != native_request:
            raise OutcomeFreshnessError('outcome_birth_changed')
        completed = self._completed(native_request)
        expected = bound.answer_origin['cited_refs'] if bound.answer_origin else completed['data']['evidence_refs']
        if list(expected) != list(evidence_refs) or len(set(basis_refs)) != len(basis_refs):
            raise OutcomeFreshnessError('outcome_cited_evidence_changed')
        bases = [(reference, self._consumed_basis(reference)) for reference in basis_refs]
        from ..research_packets import authority_stores
        from ..research_reads import turn_tree
        turns, agents = authority_stores(view)
        allowed = set(turn_tree(turns, agents, native_request['turn_id'], project))
        if any(basis['turn_id'] not in allowed for _, basis in bases):
            raise OutcomeFreshnessError('outcome_basis_ancestry_changed')
        selected = []
        for evidence in evidence_refs:
            matches = []
            for reference, basis in bases:
                for index, source in enumerate(basis['sources']):
                    if not source.get('verified'):
                        continue
                    frozen = source['frozen']
                    hit = (evidence in source['aliases'] if isinstance(evidence, str) else
                        isinstance(evidence, dict) and all(evidence.get(key) == frozen[key]
                            for key in ('id', 'revision', 'project_id'))
                        and evidence.get('kind') in {frozen['kind'], frozen['source_type'], 'document' if frozen['kind'] == 'note' else frozen['kind']})
                    if hit:
                        matches.append({'basis_ref': reference, 'index': index, 'origin': source['origin'], 'frozen': frozen})
            unique = {encoded((item['origin'], item['frozen'])): item for item in matches}
            selected.append({'evidence': deepcopy(evidence), 'source': next(iter(unique.values())) if len(unique) == 1 else None})
        lineage = view.read('v2_outcome_lineage', document_id)
        document = SQLiteDocumentRepository(view,
            namespace_id=self.documents.namespace_id).read(document_id)
        if (lineage is None or lineage.revision != 1 or lineage.payload.get('project_id') != project
                or lineage.payload.get('turn_id') != public_turn_id or document is None):
            raise OutcomeFreshnessError('outcome_lineage_changed')
        value = {'version': 3, 'policy_version': VERSION, 'owner': deepcopy(self.owner),
            'project_id': project, 'document_id': document_id, 'document_birth': _lineage(view, 'documents', document_id),
            'public_turn_id': public_turn_id, 'native_request': deepcopy(native_request), 'evidence_refs': deepcopy(list(evidence_refs)),
            'basis_refs': deepcopy(list(basis_refs)), 'sources': selected, 'created_at': document['created_at'],
            'task_text': lineage.payload['task_text'], 'scene': lineage.payload['scene'],
            'scope_origin': self._origin(self._source_owner(project)),
            'delivery_event': {'sequence': completed['sequence'], 'evidence_refs': completed['data']['evidence_refs']}}
        current = view.read(INPUTS, document_id)
        if current is not None:
            if current.revision != 1 or current.payload != value:
                raise OutcomeFreshnessError('outcome_inputs_changed')
            return current
        return view.put(INPUTS, document_id, value, expected_revision=0)

    def _fact(self, reader, project, document_id):
        document = reader.read('documents', document_id)
        if document is None or document.payload.get('project_id') != project:
            raise OutcomeFreshnessError('outcome_document_unavailable')
        row = reader.read(INPUTS, document_id)
        if row is None:
            return None
        value = row.payload
        if (row.revision != 1 or value.get('version') != 3 or value.get('owner') != self.owner
                or value.get('policy_version') != VERSION or value.get('document_id') != document_id
                or value.get('project_id') != project):
            raise OutcomeFreshnessError('outcome_input_binding_changed')
        verify_lineage(reader, value['document_birth'])
        if self._origin(self._source_owner(project)) != value['scope_origin']:
            raise OutcomeFreshnessError('outcome_scope_origin_changed')
        completed = self._completed(value['native_request'])
        if value['delivery_event'] != {'sequence': completed['sequence'], 'evidence_refs': completed['data']['evidence_refs']}:
            raise OutcomeFreshnessError('outcome_completed_evidence_changed')
        if [item['evidence'] for item in value['sources']] != value['evidence_refs']:
            raise OutcomeFreshnessError('outcome_cited_evidence_changed')
        for item in value['sources']:
            source = item.get('source')
            if source is not None:
                basis = self._consumed_basis(source['basis_ref'])
                expected = basis['sources'][source['index']]
                if expected.get('origin') != source['origin'] or expected.get('frozen') != source['frozen']:
                    raise OutcomeFreshnessError('outcome_source_binding_changed')
        return value

    def _compare(self, reader, fact, reference):
        changed, unavailable, observed = [], [], []
        for item in fact['sources']:
            source = item.get('source')
            if source is None:
                unavailable.append({'reason': 'outcome_source_proof_unavailable'})
                observed.append({'evidence': item['evidence'], 'verified': False})
                continue
            frozen, origin = source['frozen'], source['origin']
            try:
                owner = self._source_owner(origin['project_id'], origin=origin, binding=source, reader=reader)
                if self._origin(owner) != origin:
                    raise OutcomeFreshnessError('outcome_source_origin_changed')
                current = self._source(owner, self._source_reader(owner, reader), frozen['source_type'], frozen['id'])
                difference = get('stale', version='@1').changed(frozen, current, reference=reference)
            except (RecognitionConflict, RecordLineageError, FileNotFoundError, PermissionError, sqlite3.Error) as error:
                unavailable.append({'kind': frozen['kind'], 'id': frozen['id'],
                    'reason': str(error) if isinstance(error, OutcomeFreshnessError) else 'outcome_source_unavailable'})
                observed.append({'origin': origin, 'id': frozen['id'], 'verified': False})
                continue
            observation = {'origin': origin, 'kind': current['kind'], 'id': current['id'],
                'revision': current['revision'], 'incarnation': current['incarnation'], 'content': current['content'],
                'manual_forgotten': current['manual_forgotten'], 'replaced_by': current['replaced_by'],
                'expires_at': current['expires_at'], 'reasons': difference['reasons'], 'verified': True}
            observed.append(observation)
            if difference['reasons']:
                changed.append({'kind': current['kind'], 'id': current['id'], 'project_id': current['project_id'],
                    'source_type': current['source_type'],
                    'title': current['title'], 'from_revision': difference['from_revision'],
                    'to_revision': difference['to_revision'], 'reasons': difference['reasons'], 'observation': observation})
        return changed, unavailable, observed

    def _fresh_owner(self, reader, fact):
        from .outcome_source_reads import _shared_request
        view = self._reader(reader)
        if isinstance(view, SourceReadRecords) and _shared_request(self, fact.get('native_request')):
            if view.query.origin_admission.lease.project_id != fact['project_id']:
                raise OutcomeFreshnessError('outcome_source_namespace_changed')
            return view.query.material_owner
        return self._source_owner(fact['project_id'], origin=fact['scope_origin'], binding=fact, reader=view)

    def _fresh_candidates(self, reader, fact, identities=None):
        owner = self._fresh_owner(reader, fact)
        source_reader = self._source_reader(owner, reader)
        documents = SQLiteDocumentRepository(_VisibilityRecords(source_reader), namespace_id=owner.documents.namespace_id)
        visibility = LegacyDocumentVisibility.from_repository(documents, project_id=fact['project_id'], document_ids=identities)
        candidates = []
        rows = documents.list() if identities is None else [documents.read(identity) for identity in sorted(identities)]
        for document in rows:
            if document is None:
                raise OutcomeFreshnessError('outcome_fresh_source_unavailable')
            if (document.get('project_id') != fact['project_id'] or document['id'] == fact['document_id']
                    or str(document.get('type', '')).startswith('agent-result-') or not visibility.allows(document)):
                continue
            assigned = scene_of(source_reader, 'document', document['id'])
            if assigned is not None and assigned.get('project_id') != fact['project_id']:
                raise OutcomeFreshnessError('outcome_fresh_source_scope_changed')
            candidate = {'id': document['id'], 'title': document.get('title', ''), 'revision': document['revision'],
                'project_id': fact['project_id'], 'scene': assigned.get('scene') if assigned else None,
                'created_at': document.get('created_at'), 'text': documents.markdown(document['id']), 'available': True}
            from ..original_sources import resolve_turn_material
            _, roots = resolve_turn_material(source_reader, WorkScope('local-user', fact['project_id']),
                {'type': 'document', 'id': document['id'], 'revision': document['revision'], 'project_id': fact['project_id']})
            self._source_authority(owner, source_reader).snapshot(WorkScope('local-user', fact['project_id']), roots)
            _lineage(source_reader, 'documents', document['id'])
            candidates.append(candidate)
        return candidates

    def _project_fresh(self, reader, fact, identities=None):
        task = {'text': fact['task_text'], 'created_at': fact['created_at'], 'project_id': fact['project_id'],
            'scene': fact['scene'], 'excluded_ids': [item['source']['frozen']['id'] for item in fact['sources'] if item.get('source')]}
        return get('stale', version='@1').fresh(task, self._fresh_candidates(reader, fact, identities), score_text=similarity)

    def _read_fresh(self, reader, fact):
        projection = reader.read(FRESH, fact['document_id'])
        if projection is None:
            return [], False
        value = projection.payload
        if (value.get('project_id') != fact['project_id'] or value.get('document_id') != fact['document_id']
                or value.get('policy_version') != VERSION or value.get('scope_origin') != fact['scope_origin']
                or value.get('input_revision') != 1 or not isinstance(value.get('fresh'), list)
                or not isinstance(value.get('births'), dict)):
            raise OutcomeFreshnessError('outcome_fresh_binding_changed')
        owner = self._fresh_owner(reader, fact)
        source_reader = self._source_reader(owner, reader)
        expected_fields = {'project_id', 'document_id', 'policy_version', 'input_revision',
            'scope_origin', 'fresh', 'births', 'at'}
        if isinstance(owner, SharedMaterialOwner):
            expected_fields.add('source_origin')
            if value.get('source_origin') != self._origin(owner):
                raise OutcomeFreshnessError('outcome_fresh_source_origin_unavailable')
        if set(value) != expected_fields:
            raise OutcomeFreshnessError('outcome_fresh_binding_changed')
        identities = {item['document_id'] for item in value['fresh']}
        if set(value['births']) != identities:
            raise OutcomeFreshnessError('outcome_fresh_binding_changed')
        for identity in identities:
            if _lineage(source_reader, 'documents', identity) != value['births'][identity]:
                raise OutcomeFreshnessError('outcome_fresh_source_replaced')
        # 共享资料由其他成员入库时，当前读取事务直接重算个人成果的新依据。
        return self._project_fresh(reader, fact, None if isinstance(owner, SharedMaterialOwner) else identities), True

    def read_changes(self, *, project, document_id, reader=None, reference=None):
        with self.outcome_scope(project=project, document_id=document_id, reader=reader):
            return self._read_changes(project=project, document_id=document_id, reader=reader, reference=reference)

    def _read_changes(self, *, project, document_id, reader=None, reference=None):
        with self._read_scope(reader) as view:
            unavailable_history = None
            try:
                fact = self._fact(view, project, document_id)
            except OutcomeFreshnessError as error:
                if str(error) not in {'outcome_native_owner_unavailable', 'outcome_completed_evidence_unavailable'}:
                    raise
                fact, unavailable_history = None, str(error)
            ack = view.read(ACK, document_id)
            if fact is None:
                return {'stale': False, 'changed': [], 'fresh': [], 'fresh_count': None, 'verified': False,
                    'fresh_verified': False, 'unverifiable': [{'reason': unavailable_history or 'outcome_input_history_unavailable'}],
                    'ack_revision': ack.revision if ack else 0, 'observation': None}
            changed, unavailable, observed = self._compare(view, fact, reference or self.now())
            try:
                projected, fresh_verified = self._read_fresh(view, fact)
            except (RecognitionConflict, RecordLineageError, FileNotFoundError, PermissionError, sqlite3.Error):
                projected, fresh_verified = [], False
            ignored = ack.payload if ack else {}
            if ack and (ignored.get('owner') != self.owner or ignored.get('document_birth') != fact['document_birth']
                    or ignored.get('project_id') != project or ignored.get('policy_version') != VERSION):
                raise OutcomeFreshnessError('outcome_ack_binding_changed')
            changed = [item for item in changed if item['observation'] not in ignored.get('revisions', [])]
            fresh = [item for item in projected if item not in ignored.get('fresh', [])]
            observation = {'document_revision': view.read('documents', document_id).revision,
                'revisions': observed, 'fresh': projected,
                'fresh_verified': fresh_verified}
            return {'stale': bool(changed), 'changed': [{key: value for key, value in item.items() if key != 'observation'} for item in changed],
                'fresh': fresh, 'fresh_count': len(fresh) if fresh_verified else None, 'verified': not unavailable,
                'fresh_verified': fresh_verified, 'unverifiable': unavailable,
                'ack_revision': ack.revision if ack else 0, 'observation': observation}

    def row_projection(self, *, project, document_id, reader=None):
        current = self.read_changes(project=project, document_id=document_id, reader=reader)
        return {'stale': current['stale'], 'fresh_count': current['fresh_count'],
            'changes_verified': current['verified'] and current['fresh_verified']}

    def ack_in_uow(self, tx, *, project, document_id, expected_revision, observation):
        view = self._reader(tx)
        if type(expected_revision) is not int or expected_revision < 0:
            raise OutcomeFreshnessError('outcome_ack_revision_invalid')
        current = self.read_changes(project=project, document_id=document_id, reader=view)
        if (current['ack_revision'] != expected_revision or observation is None
                or encoded(current['observation']) != encoded(observation)):
            raise OutcomeFreshnessError('outcome_observation_changed')
        fact = self._fact(view, project, document_id)
        return view.put(ACK, document_id, {'project_id': project, 'document_id': document_id, 'owner': deepcopy(self.owner),
            'policy_version': VERSION, 'document_birth': fact['document_birth'],
            'revisions': [item for item in observation['revisions'] if item.get('verified')],
            'fresh': observation['fresh'], 'at': self.now()}, expected_revision=expected_revision)

    def rebuild_fresh_in_uow(self, tx, *, project, document_id):
        view = self._reader(tx)
        fact = self._fact(view, project, document_id)
        if fact is None:
            raise OutcomeFreshnessError('outcome_input_history_unavailable')
        fresh = self._project_fresh(view, fact)
        owner = self._fresh_owner(view, fact)
        source_reader = self._source_reader(owner, view)
        source_origin = {'source_origin': self._origin(owner)} if isinstance(owner, SharedMaterialOwner) else {}
        current = view.read(FRESH, document_id)
        return view.put(FRESH, document_id, {'project_id': project, 'document_id': document_id,
            'policy_version': VERSION, 'input_revision': 1, 'scope_origin': fact['scope_origin'],
            **source_origin, 'fresh': fresh,
            'births': {item['document_id']: _lineage(source_reader, 'documents', item['document_id']) for item in fresh},
            'at': self.now()}, expected_revision=current.revision if current else 0)

    def rebuild_project_fresh_in_uow(self, tx, *, project):
        view = self._reader(tx)
        rows = [row for row in view.list(INPUTS) if row.payload.get('project_id') == project]
        return [self.rebuild_fresh_in_uow(view, project=project, document_id=row.object_id) for row in rows]

    def prepare_update(self, *, project, document_id, expected_revision, observation, reader=None):
        with self._read_scope(reader) as view:
            shared = isinstance(view, SourceReadRecords)
            current = self.read_changes(project=project, document_id=document_id, reader=view)
            if (current['ack_revision'] != expected_revision or observation is None
                    or encoded(current['observation']) != encoded(observation)):
                raise OutcomeFreshnessError('outcome_observation_changed')
            if not current['verified'] or not current['fresh_verified']:
                raise OutcomeFreshnessError('outcome_sources_unverifiable')
            from .outcomes import select_outcome
            fact = self._fact(view, project, document_id)
            select_outcome(view, project=project, scene=fact['scene'], document_id=document_id)
            changes, contexts, material_contexts = [], [], []
            for item in current['changed']:
                observed = next((row for row in observation['revisions'] if row.get('verified')
                    and row.get('id') == item['id'] and row.get('origin', {}).get('project_id') == item['project_id']
                    and (not shared or row.get('origin') == item['observation']['origin'])), None)
                if observed is None:
                    raise OutcomeFreshnessError('outcome_observation_changed')
                changes.append({key: deepcopy(item[key]) for key in
                    ('kind', 'id', 'project_id', 'source_type', 'from_revision', 'to_revision', 'reasons')}
                    | {'replacement_id': observed['replaced_by']})
                if get('outcome_update', version='@1')('current_context', item):
                    contexts.append({'kind': item['source_type'], 'id': item['id'], 'revision': item['to_revision'],
                        'project_id': item['project_id']})
                    if shared:
                        material_contexts.append({'type': item['source_type'], 'id': item['id'],
                            'revision': item['to_revision'], 'project_id': item['project_id'],
                            'namespace': observed['origin']['namespace']})
                elif observed['replaced_by']:
                    replacement_owner = self._source_owner(item['project_id'],
                        origin=observed['origin'] if shared else None, reader=view)
                    replacement_reader = self._source_reader(replacement_owner, view)
                    replacement = replacement_reader.read('recognitions', observed['replaced_by'])
                    if (replacement is not None and replacement.payload.get('state') == 'active'
                            and replacement.payload.get('scope') == {'user_id': 'local-user', 'project_id': item['project_id']}):
                        from ..source_egress import recognition_service
                        replacement_service = recognition_service(replacement_reader,
                            source_owner=replacement_owner if isinstance(replacement_owner, SharedMaterialOwner) else replacement_owner.records)
                        qualified = replacement_service._qualified_recognition(replacement_reader, replacement)
                        if qualified.authorized and qualified.source_evidence_complete:
                            current_replacement = self._source(replacement_owner, replacement_reader,
                                'recognition', replacement.object_id)
                            if not get('stale', version='@1').changed(current_replacement, current_replacement,
                                    reference=self.now())['reasons']:
                                contexts.append({'kind': 'insight', 'id': replacement.object_id,
                                    'revision': replacement.revision, 'project_id': item['project_id']})
                                if shared:
                                    material_contexts.append({'type': 'recognition', 'id': replacement.object_id,
                                        'revision': replacement.revision, 'project_id': item['project_id'],
                                        'namespace': replacement_owner.namespace})
            contexts.extend({'kind': 'document', 'id': item['document_id'], 'revision': item['revision'],
                'project_id': project} for item in current['fresh'])
            if shared:
                fresh_owner = self._fresh_owner(view, fact)
                material_contexts.extend({'type': 'document', 'id': item['document_id'], 'revision': item['revision'],
                    'project_id': project, 'namespace': fresh_owner.namespace} for item in current['fresh'])
            contexts = list({encoded(item): item for item in contexts}.values())
            document = view.read('documents', document_id)
            markdown = view.read('document_markdown', document_id + '~r' + str(document.revision))
            if markdown is None:
                raise OutcomeFreshnessError('outcome_update_history_unavailable')
            basis = {'version': 1, 'update_policy': '@1', 'owner': deepcopy(self.owner), 'project_id': project, 'document_id': document_id,
                'document_birth': fact['document_birth'], 'document_revision': document.revision,
                'markdown_record_revision': markdown.revision, 'public_turn_id': fact['public_turn_id'],
                'native_request_identity': admission_identity(fact['native_request']),
                'delivery_event': fact['delivery_event'], 'basis_refs': fact['basis_refs'],
                'ack_revision': expected_revision, 'observation': deepcopy(observation),
                'source_changes': changes, 'context_ids': contexts}
            if shared:
                basis['material_contexts'] = list({encoded(item): item for item in material_contexts}.values())
            return {'project_id': project, 'scene': fact['scene'], 'continue_from': document_id,
                'context_ids': contexts, 'source_changes': changes, 'update_basis': basis,
                'text': fact['task_text'], 'intent': 'do'}

    def prepare_update_in_uow(self, tx, *, project, document_id, expected_revision, observation, submission_key):
        view, actor = self._reader(tx), self._actor()
        if not isinstance(submission_key, str) or not _UPDATE_KEY.fullmatch(submission_key):
            raise ValueError('invalid_outcome_update_key')
        request = {'project_id': project, 'document_id': document_id, 'expected_revision': expected_revision,
            'observation': deepcopy(observation)}
        prior = view.read(UPDATE_KEYS, submission_key)
        if prior is not None:
            if (prior.revision != 1 or prior.payload.get('owner') != self.owner
                    or prior.payload.get('actor') != actor or prior.payload.get('request') != request):
                raise OutcomeFreshnessError('outcome_update_submission_conflict')
            prepared = view.read(UPDATES, prior.payload['preparation_id'])
            if (prepared is None or prepared.revision != 1 or prepared.payload.get('submission_key') != submission_key
                    or prepared.payload.get('owner') != self.owner or prepared.payload.get('actor') != actor):
                raise OutcomeFreshnessError('outcome_update_preparation_changed')
            return deepcopy(prepared.payload['result'])
        result = self.prepare_update(project=project, document_id=document_id,
            expected_revision=expected_revision, observation=observation, reader=view)
        identity = 'outcome-update-' + uuid4().hex
        marker = {'kind': 'outcome_update', 'id': identity, 'revision': 1, 'project_id': project}
        result['context_ids'].append(marker)
        result['update_basis'].update(preparation_id=identity, submission_key=submission_key,
            actor=actor, context_ids=deepcopy(result['context_ids']))
        view.put(UPDATES, identity, {'owner': deepcopy(self.owner), 'actor': actor, 'request': request,
            'submission_key': submission_key, 'result': result, 'created_at': self.now()}, expected_revision=0)
        view.put(UPDATE_KEYS, submission_key, {'owner': deepcopy(self.owner), 'actor': actor,
            'request': request, 'preparation_id': identity}, expected_revision=0)
        return deepcopy(result)

    def read_update_basis(self, *, project, document_id, context_ids, reader):
        view, actor = self._reader(reader), self._actor()
        if not isinstance(context_ids, list):
            raise OutcomeFreshnessError('outcome_update_context_changed')
        markers = [item for item in context_ids if isinstance(item, dict) and item.get('kind') == 'outcome_update']
        if (len(markers) != 1 or set(markers[0]) != {'kind', 'id', 'revision', 'project_id'}
                or type(markers[0]['revision']) is not int or markers[0]['revision'] != 1 or markers[0]['project_id'] != project):
            raise OutcomeFreshnessError('outcome_update_preparation_required')
        row = view.read(UPDATES, markers[0]['id'])
        if (row is None or row.revision != 1 or row.payload.get('owner') != self.owner
                or row.payload.get('actor') != actor or row.payload.get('request', {}).get('project_id') != project
                or row.payload.get('request', {}).get('document_id') != document_id
                or encoded(row.payload.get('result', {}).get('context_ids')) != encoded(context_ids)
                or row.payload['result'].get('continue_from') != document_id):
            raise OutcomeFreshnessError('outcome_update_preparation_changed')
        return deepcopy(row.payload['result']['update_basis'])

    def validate_update_basis(self, *, project, document_id, basis, reader):
        view = self._reader(reader)
        if (not isinstance(basis, dict) or basis.get('owner') != self.owner
                or basis.get('project_id') != project or basis.get('document_id') != document_id
                or basis.get('actor') != self._actor()):
            raise OutcomeFreshnessError('outcome_update_owner_changed')
        actual = self.read_update_basis(project=project, document_id=document_id,
            context_ids=basis.get('context_ids'), reader=view)
        if actual != basis:
            raise OutcomeFreshnessError('outcome_update_preparation_changed')
        prepared = self.prepare_update(project=project, document_id=document_id,
            expected_revision=basis.get('ack_revision'), observation=basis.get('observation'), reader=view)
        expected_basis = {key: value for key, value in basis.items() if key not in {'preparation_id', 'submission_key', 'actor'}}
        expected_basis['context_ids'] = [item for item in expected_basis['context_ids'] if item['kind'] != 'outcome_update']
        if encoded(prepared['update_basis']) != encoded(expected_basis):
            raise OutcomeFreshnessError('outcome_update_basis_changed')
        fact = self._fact(view, project, document_id)
        proofs = []
        for reference in fact['basis_refs']:
            proofs.extend(self._consumed_basis(reference)['sources'])
        qualified = self._qualify_source_proofs(proofs, reader=view, local_only=True)
        if not qualified['complete']:
            raise OutcomeFreshnessError('outcome_update_source_unverifiable')
        document = view.read('documents', document_id)
        markdown = view.read('document_markdown', document_id + '~r' + str(document.revision))
        birth = view.read('document_markdown', document_id + '~r1')
        if markdown is None or birth is None:
            raise OutcomeFreshnessError('outcome_update_history_unavailable')
        current_sources = []
        selected_materials = prepared['update_basis'].get('material_contexts') if isinstance(view, SourceReadRecords) else None
        if isinstance(view, SourceReadRecords) and not isinstance(selected_materials, list):
            raise OutcomeFreshnessError('outcome_update_material_namespace_unavailable')
        for item in selected_materials if selected_materials is not None else prepared['context_ids']:
            label = item['type'] if selected_materials is not None else item['kind']
            kind = {'insight': 'recognition', 'inspiration': 'experience', 'note': 'document',
                'summary': 'document', 'source': 'original_item'}.get(label, label)
            material = {'type': kind, 'id': item['id'], 'revision': item['revision'],
                'project_id': item['project_id'], 'proof_ref': {'outcome_update': document_id}, 'role': 'update',
                **({'namespace': item['namespace']} if selected_materials is not None else {})}
            current_sources.extend(self._freeze_material(self._material_owner(material, view), material, reader=view))
        if any(not item.get('verified') for item in current_sources):
            raise OutcomeFreshnessError('outcome_update_source_unverifiable')
        current_sources = [{**source, **self._source_text(self._source_owner(source['frozen']['project_id'],
            origin=source['origin'], reader=view), self._source_reader(self._source_owner(source['frozen']['project_id'],
            origin=source['origin'], reader=view), view), source)}
            for source in current_sources]
        return {'basis': deepcopy(basis), 'document_id': document_id, 'current_markdown': markdown.payload['markdown'],
            'birth_ai_markdown': birth.payload['markdown'], 'current_source_refs': deepcopy(document.payload.get('source_refs', [])),
            'source_changes': deepcopy(prepared['source_changes']), 'current_sources': current_sources,
            'privacy_ceiling': qualified['privacy_ceiling'],
            **({'namespaced_materials': True} if isinstance(view, SourceReadRecords) else {})}
