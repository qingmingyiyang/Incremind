from copy import deepcopy
from urllib.parse import quote

from backend.recognition import RecognitionConflict, WorkScope
from backend.security.user_context import USER_ACCESS
from core.ai_kernel.product_admission import ProductAdmissionItem
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.document_engine import SQLiteDocumentRepository
from ..original_sources import resolve_turn_material
from ..source_egress import SourceEgressService
from .material_origins import ORIGIN_KIND, SharedMaterialOwner, SharedProjectLease
from .material_origin_readers import ReadOnlyMaterialRecords, ReadOnlyMaterialDocuments, ReadOnlyMaterialSources
from .shared_extraction import PersonalDestination


def personal_origin(destination):
    destination.validate()
    return {'owner_user_id': destination.owner_user_id, 'namespace_id': destination.namespace,
        'namespace': '/'.join(('personal', destination.owner_user_id, destination.namespace)),
        'root_identity': list(destination.root_identity)}


def origin_reference(actor_user_id, namespace, turn_id):
    uri = 'crp://material-origin/' + '/'.join(quote(value, safe='') for value in (actor_user_id, namespace, turn_id))
    return {'kind': 'session_event', 'object_id': turn_id, 'uri': uri}


def read_origin_item(request, item, *, actor_user_id, personal):
    if (not isinstance(actor_user_id, str) or not actor_user_id
            or not isinstance(personal, dict)
            or set(personal) != {'owner_user_id', 'namespace_id', 'namespace', 'root_identity'}
            or any(not isinstance(personal[key], str) or not personal[key]
                   for key in ('owner_user_id', 'namespace_id', 'namespace'))
            or personal['namespace'] != '/'.join(('personal', personal['owner_user_id'], personal['namespace_id']))
            or not isinstance(personal['root_identity'], list) or len(personal['root_identity']) != 2
            or any(type(value) is not int or value < 0 for value in personal['root_identity'])):
        raise RecognitionConflict('material_origin_binding_changed')
    reference = origin_reference(actor_user_id, personal['namespace_id'], request['turn_id'])
    scope = {'turn_id': request['turn_id'], 'project_id': request['scope']['project_id'],
        'owner_user_id': personal['owner_user_id'], 'namespace_id': personal['namespace_id'], 'origin_kind': 'personal'}
    if (item.kind != ORIGIN_KIND or item.ref != reference or item.scope != scope
            or request['input']['refs'].count(reference) != 1):
        raise RecognitionConflict('material_origin_binding_changed')
    payload = item.payload
    fields = {'schema_version', 'actor_user_id', 'execution', 'profile', 'shared'}
    if (not isinstance(payload, dict) or set(payload) not in (fields, fields | {'auxiliary'})
            or type(payload['schema_version']) is not int or payload['schema_version'] != 1
            or payload['actor_user_id'] != actor_user_id or payload['execution'] != personal
            or payload['profile'] != {**personal, 'project_id': 'me'}
            or not isinstance(payload['shared'], dict) or set(payload['shared']) != {'namespace', 'lease'}):
        raise RecognitionConflict('material_origin_binding_changed')
    lease = SharedProjectLease.from_payload(payload['shared']['lease'])
    if (lease.project_id != request['scope']['project_id'] or payload['shared']['namespace'] != lease.namespace
            or lease.actor_user_id != actor_user_id):
        raise RecognitionConflict('material_origin_scope_changed')
    auxiliary = payload.get('auxiliary')
    if 'auxiliary' in payload:
        rendered = (isinstance(auxiliary, dict) and isinstance(auxiliary.get('selection'), dict)
            and auxiliary['selection'].get('schema_version') == 3)
        auxiliary_fields = {'purpose', 'materials', 'selection'} | ({'rendered_input'} if rendered else set())
        search_input = isinstance(auxiliary, dict) and 'search_input' in auxiliary
        if search_input:
            auxiliary_fields.add('search_input')
        selection_fields = {'schema_version', 'origin_ref', 'origin_materials', 'sql_identities'} | (
            {'rendered_input_ref'} if rendered else set())
        if (not isinstance(auxiliary, dict) or set(auxiliary) != auxiliary_fields
                or not (request['desired_outcome'].startswith('memory.') or request['desired_outcome'] == 'web.search')
                or auxiliary['purpose'] not in {'embedding', 'rerank', 'generation', 'search'}
                or not isinstance(auxiliary['materials'], list) or not isinstance(auxiliary['selection'], dict)
                or set(auxiliary['selection']) not in ((selection_fields,) if rendered else
                    ({'origin_ref', 'origin_materials'}, selection_fields))
                or 'schema_version' in auxiliary['selection'] and (
                    type(auxiliary['selection']['schema_version']) is not int
                    or auxiliary['selection']['schema_version'] != (3 if rendered else 2)
                    or not isinstance(auxiliary['selection']['sql_identities'], list))
                or auxiliary['selection']['origin_ref'] != reference
                or auxiliary['selection']['origin_materials'] != auxiliary['materials']
                or any(not isinstance(row, dict) or set(row) != {'type', 'id', 'revision', 'project_id', 'namespace'}
                       for row in auxiliary['materials'])):
            raise RecognitionConflict('material_origin_auxiliary_changed')
        descriptors = [{key: row[key] for key in ('type', 'id', 'revision', 'project_id')} for row in auxiliary['materials']]
        if 'schema_version' in auxiliary['selection']:
            from .material_origin_sql import validate_sql_shape
            validate_sql_shape(auxiliary['selection']['sql_identities'])
        if rendered:
            from .shared_rendered_input import read_rendered_capsule
            read_rendered_capsule(request, auxiliary, reference=reference)
        if search_input:
            from .shared_search_input import validate_search_input_shape
            if rendered or auxiliary['purpose'] != 'search' or auxiliary['selection'].get('schema_version') != 2:
                raise RecognitionConflict('material_origin_auxiliary_changed')
            validate_search_input_shape(request, auxiliary['search_input'])
        if descriptors != request['privacy']['material_refs'] or len(descriptors) != len(request['privacy']['source_snapshots']):
            raise RecognitionConflict('material_origin_auxiliary_changed')
    return lease, auxiliary


class SharedOriginAdmission:
    def __init__(self, registry, actor, application, project_id, *, lease=None):
        self.registry, self.actor, self.application = registry, actor, application
        access = USER_ACCESS.get()
        target = getattr(application.state, 'server_user_id', None) if lease is not None else actor.user_id
        if access is not None:
            if access.caller != actor or access.target_user_id != getattr(application.state, 'server_user_id', None):
                raise RecognitionConflict('material_origin_execution_owner_changed')
            target = access.target_user_id
        self.destination = PersonalDestination.from_application(registry, actor, application, target_user_id=target)
        self.lease = lease if lease is not None else SharedProjectLease.acquire(registry, actor, project_id)
        if self.lease.project_id != project_id or self.lease.actor_user_id != actor.user_id:
            raise RecognitionConflict('material_origin_execution_owner_changed')
        self.personal = personal_origin(self.destination)

    def validate_execution_owner(self):
        self.destination.validate_application(self.registry, self.application)
        access = USER_ACCESS.get()
        if access is not None and (access.caller != self.actor or access.target_user_id != self.destination.owner_user_id):
            raise RecognitionConflict('material_origin_turn_owner_changed')

    def _store(self):
        self.validate_execution_owner()
        store = getattr(self.application.state, 'ai_turn_store', None)
        if (not isinstance(store, SQLiteAITurnStore)
                or store._path != self.destination.root / '.rebuild-data' / 'ai-turns.sqlite3'):
            raise RecognitionConflict('material_origin_turn_owner_changed')
        return store

    def _scope(self, request):
        if request['scope']['project_id'] != self.lease.project_id:
            raise RecognitionConflict('material_origin_scope_changed')
        return {'turn_id': request['turn_id'], 'project_id': self.lease.project_id,
            'owner_user_id': self.destination.owner_user_id, 'namespace_id': self.destination.namespace, 'origin_kind': 'personal'}

    def _reference(self, request):
        return origin_reference(self.actor.user_id, self.destination.namespace, request['turn_id'])

    def prepare(self, request, workbench_identity=None):
        self._store()
        if workbench_identity is not None and (not isinstance(workbench_identity, dict)
                or workbench_identity.get('owner_user_id') != self.destination.owner_user_id
                or workbench_identity.get('namespace_id') != self.destination.namespace
                or workbench_identity.get('origin_kind') != 'personal'
                or workbench_identity.get('project_id') != self.lease.project_id):
            raise RecognitionConflict('material_origin_execution_owner_changed')
        self.lease.validate(self.registry, self.actor)
        if personal_origin(self.destination) != self.personal:
            raise RecognitionConflict('material_origin_execution_owner_changed')
        payload = {'schema_version': 1, 'actor_user_id': self.actor.user_id,
            'execution': deepcopy(self.personal),
            'profile': {**deepcopy(self.personal), 'project_id': 'me'},
            'shared': {'namespace': self.lease.namespace, 'lease': self.lease.payload()}}
        items = (ProductAdmissionItem(ORIGIN_KIND, self._reference(request), self._scope(request), payload),)
        constraints = getattr(self, 'constraint_input', None)
        return constraints.augment(request, self._store(), items) if constraints is not None else items

    def restore(self, request):
        store = self._store()
        if store.get_request(request['turn_id']) != request:
            raise RecognitionConflict('material_origin_native_request_changed')
        bundle = store.restore_admission(request)
        if bundle is None:
            raise RecognitionConflict('material_origin_binding_missing')
        return self._bind_bundle(request, bundle)

    def _bind_bundle(self, request, bundle):
        items = [item for item in bundle.items if item.kind == ORIGIN_KIND]
        if len(items) != 1:
            raise RecognitionConflict('material_origin_binding_changed')
        self._scope(request)
        lease, auxiliary = read_origin_item(request, items[0], actor_user_id=self.actor.user_id,
                                            personal=personal_origin(self.destination))
        lease.validate(self.registry, self.actor)
        return BoundMaterialOrigins(self, lease, items[0].ref, auxiliary=auxiliary)


class _PersonalProfileOwner:
    def __init__(self, origins, *, actor_project=False):
        if type(actor_project) is not bool:
            raise RecognitionConflict('material_origin_personal_scope_changed')
        self.origins = origins
        self._actor_project = actor_project
        destination = origins.admission.destination
        self.records = ReadOnlyMaterialRecords(destination.records.database_path, validate=origins.validate_current)
        self.documents = ReadOnlyMaterialDocuments(SQLiteDocumentRepository(self.records, namespace_id=destination.namespace),
                                                    validate=origins.validate_current)
        self.sources = ReadOnlyMaterialSources(destination.workspace.query.source_store, validate=origins.validate_current)

    @property
    def namespace(self):
        return self.origins.admission.personal['namespace']

    @property
    def project_id(self):
        return self.origins.lease.project_id if self._actor_project else 'me'

    def validate_current(self):
        self.origins.validate_current()

    def validate_snapshot(self, snapshot, *, purpose=None):
        self.origins.validate_current()
        if snapshot.get('scope') != {'user_id': 'local-user', 'project_id': self.project_id}:
            raise RecognitionConflict('material_origin_profile_scope_changed')
        authority = SourceEgressService(self.origins.admission.destination.records)
        authority.validate_snapshot(WorkScope('local-user', self.project_id), snapshot)
        if purpose is not None:
            authority.require(snapshot, purpose)
        self.origins.validate_current()


class BoundMaterialOrigins:
    def __init__(self, admission, lease, reference, *, auxiliary=None):
        self.admission, self.lease, self.reference = admission, lease, deepcopy(reference)
        self.auxiliary = deepcopy(auxiliary)
        self.shared = SharedMaterialOwner(admission.registry, admission.actor, lease)
        self.profile = _PersonalProfileOwner(self)
        self.actor = _PersonalProfileOwner(self, actor_project=True)

    def validate_current(self):
        self.admission._store()
        if personal_origin(self.admission.destination) != self.admission.personal:
            raise RecognitionConflict('material_origin_execution_owner_changed')
        self.lease.validate(self.admission.registry, self.admission.actor)

    def owner(self, project_id, namespace):
        self.validate_current()
        for owner in (self.shared, self.profile, self.actor):
            if owner.project_id == project_id and owner.namespace == namespace:
                return owner
        raise RecognitionConflict('material_origin_scope_changed')

    def resolve_for_invocation(self, request, material, *, proof_kind, proof_reference, purpose=None):
        if (not isinstance(material, dict) or set(material) != {'type', 'id', 'revision', 'project_id', 'namespace'}
                or not isinstance(proof_kind, str) or not proof_kind.startswith('answer-gap-input-') or not proof_kind.endswith('-v1')):
            raise RecognitionConflict('material_origin_invocation_invalid')
        restored = self.admission.restore(request)
        if restored.reference != self.reference or restored.lease != self.lease:
            raise RecognitionConflict('material_origin_binding_changed')
        proof = self.admission._store().get_immutable_payload(request['turn_id'], proof_kind)
        if proof is None or proof[0] != proof_reference:
            raise RecognitionConflict('material_origin_invocation_missing')
        body = proof[1].get('material_proof', {})
        selection, privacy = body.get('selection', {}), body.get('privacy', {})
        materials = selection.get('origin_materials')
        if (selection.get('origin_ref') != self.reference or not isinstance(materials, list)
                or material not in materials or len(materials) != len(privacy.get('material_refs', ()))
                or len(materials) != len(privacy.get('source_snapshots', ()))):
            raise RecognitionConflict('material_origin_invocation_changed')
        position = materials.index(material)
        descriptor = {field: material[field] for field in ('type', 'id', 'revision', 'project_id')}
        if descriptor != privacy['material_refs'][position]:
            raise RecognitionConflict('material_origin_invocation_changed')
        owner = self.owner(material['project_id'], material['namespace'])
        _, roots = resolve_turn_material(owner.records, WorkScope('local-user', owner.project_id), descriptor)
        snapshot = privacy['source_snapshots'][position]
        current = SourceEgressService(owner.records).snapshot(WorkScope('local-user', owner.project_id), roots)
        if snapshot.get('roots') != current['roots']:
            raise RecognitionConflict('material_origin_invocation_changed')
        owner.validate_snapshot(snapshot, purpose=purpose)
        self.validate_current()
        return owner
