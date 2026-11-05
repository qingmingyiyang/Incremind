from contextlib import contextmanager

from backend.recognition import WorkScope
from backend.recognition.sql_source_identities import CollectingReader, capture_identities
from core.document_engine import SQLiteDocumentRepository

from ..source_egress import SourceEgressService
from ..transaction_records import TransactionRecords
from .budget import text_tokens
from .external_context import DELIVERIES, SHARED_ARCHIVE_VERSION, ExternalContextError, _encoded, _entry_key, _selections
from .external_executor_plugins import effective_external_agent_settings
from .external_recall import ExternalRecall
from .material_origin_readers import qualified_record_read
from .policies import get
from .privacy import external_egress_allowed, resolve_turn_material
from .profile import confirmed_profile, validate_profile


MATERIAL = {'type', 'id', 'revision', 'project_id', 'namespace'}
_USAGE = {'original_item': None, 'original_source': None, 'document': 'document', 'recognition': 'insight'}
_KIND = {'recall', 'methods', 'read'}
_SHARED = {'schema_version', 'kind', 'native', 'source_namespace', 'proofs', 'plan', 'profile_basis', 'parent'}


def _material(value):
    if (not isinstance(value, dict) or not MATERIAL <= value.keys()
            or value['type'] not in _USAGE or type(value['revision']) is not int or value['revision'] < 1
            or any(not isinstance(value[key], str) or not value[key] for key in ('id', 'project_id', 'namespace'))):
        raise ExternalContextError('external_context_material_changed')
    return {key: value[key] for key in MATERIAL}


def _material_key(material):
    return tuple(material[key] for key in ('namespace', 'project_id', 'type', 'id', 'revision'))


def _selections_with_scope(values):
    material = [_material(row) for row in values]
    local = _selections([{key: value for key, value in row.items() if key != 'namespace'} for row in values])
    return [{**row, 'namespace': item['namespace']} for row, item in zip(local, material, strict=True)]


def _delivery(tx, api, turn, ref, outcome):
    current = tx.read(DELIVERIES, turn)
    if (current is None or current.revision != 1 or current.payload.get('owner_id') != api.owner_id
            or current.payload.get('turn_id') != turn or current.payload.get('immutable_ref') != ref
            or current.payload.get('outcome_ref') != outcome):
        raise ExternalContextError('external_context_not_delivered')


class SharedMcpMaterials:
    def __init__(self, shared):
        self.shared, self.api, self.authority = shared, shared.context, shared.authority

    def _recall(self, query, actor_reader, source):
        def allowed(reader, project, client):
            if (project != query.origin_admission.lease.project_id
                    or not query.source_namespace):
                raise ExternalContextError('external_context_material_changed')
            self.authority.require_external(actor_reader, client, project=project, source_reader=source)
            query.validate_retrieval_origin()
            return True
        return ExternalRecall(query, owner_id=self.api.owner_id, external_authority=allowed, include_profile=False)

    def _profile(self, query, request):
        actor = query.profile_owner
        settings = effective_external_agent_settings(actor.records, client=request['client'])
        if settings['include_profile'] and external_egress_allowed(actor.records, 'me', request['client']):
            return confirmed_profile(actor.records, actor.service), settings['revision']
        return None, settings['revision']

    def _profile_current(self, query, actor_reader, request, basis):
        if (not effective_external_agent_settings(actor_reader, client=request['client'])['include_profile']
                or not external_egress_allowed(actor_reader, 'me', request['client'])):
            raise ExternalContextError('external_agent_profile_disabled')
        if basis is not None:
            validate_profile(TransactionRecords(actor_reader), query.profile_owner.service, {'basis': basis})

    def _capture(self, query, source, actor_reader, request, material):
        lease, personal = query.origin_admission.lease, query.origin_admission.personal
        project, namespace = material['project_id'], material['namespace']
        if (project, namespace) == (lease.project_id, lease.namespace):
            reader, owner = source, query
        elif (project, namespace) == ('me', personal['namespace']):
            self._profile_current(query, actor_reader, request, None)
            reader, owner = actor_reader, query.profile_owner
        else:
            raise ExternalContextError('external_context_material_changed')
        collector = CollectingReader(reader)
        scope = WorkScope(self.api.owner_id, project)
        row, roots = resolve_turn_material(collector, scope,
            {key: material[key] for key in MATERIAL - {'namespace'}})
        authority = SourceEgressService(owner.records, reader=collector)
        snapshot = authority.snapshot(scope, roots)
        authority.require(snapshot, 'generation')
        authority.validate_snapshot(scope, snapshot)
        if material['type'] == 'original_source':
            node = next((part for part in snapshot['nodes'] if part['type'] == 'original_source'
                and part['id'] == material['id']), None)
            if (node is None or node['source_revision'] != material['revision']
                    or node.get('incarnation') != row['payload']['_original_incarnation']):
                raise ExternalContextError('external_context_material_changed')
        if material['type'] == 'document':
            documents = SQLiteDocumentRepository(TransactionRecords(collector), namespace_id=owner.documents.namespace_id)
            if (documents.revision(material['id'], material['revision']) is None
                    or documents.markdown(material['id'], revision=material['revision']) is None):
                raise ExternalContextError('external_context_material_changed')
        proof = {'material': material, 'snapshot': snapshot, 'usage_kind': _USAGE[material['type']],
            'sql_identities': capture_identities(reader, collector)}
        return proof, row, owner, reader

    def _render(self, query, source, actor_reader, request, materials, selections):
        captured = [self._capture(query, source, actor_reader, request, material) for material in materials]
        by_material = {_material_key(row[0]['material']): row for row in captured}
        if len(captured) != len(by_material):
            raise ExternalContextError('external_context_selection_invalid')
        entries, profile, by_entry = [], [], {}
        for selection in selections:
            material = _material(selection)
            proof, row, owner, reader = by_material[_material_key(material)]
            entry = self.api._entry(reader, row, selection, proof['snapshot'], owner=owner)
            entry['sources'] = [{**part, 'namespace': material['namespace']} for part in entry['sources']]
            (entries if owner is query else profile).append(entry)
            by_entry[_entry_key(entry, material['project_id'])] = proof
        handoff = get('handoff', version='@4')(entries, profile=profile, count_tokens=text_tokens, budget=request['budget'])
        mapping = {entry['id']: by_entry[_entry_key(entry, 'me' if entry['id'].startswith('P')
            else request['scope']['project_id'])] for entry in [*handoff['entries'], *handoff['profile']]}
        return handoff, mapping, [row[0] for row in captured], [row[1]['ref'] for row in captured]

    def _request(self, request, query):
        return {**request, 'scope_kind': 'shared', 'source_namespace': query.source_namespace}

    @contextmanager
    def _source(self, query, actor_reader, request):
        with qualified_record_read(query.records, validate=query.validate_retrieval_origin) as source:
            self.authority.require_external(actor_reader, request['client'],
                project=query.origin_admission.lease.project_id, source_reader=source)
            yield source

    def _query(self, request):
        native, actor = self.authority.current(request['client'], request['tool'])
        project = request['scope']['project_id']
        query = self.authority.factory.incoming_query(self.authority.application, project, scope_kind='shared')
        if query.origin_admission.actor != actor:
            raise ExternalContextError('external_context_owner_changed')
        return native, query

    def prepare_recall(self, turn_id, request, *, scene=None, **identity):
        native, query = self._query(request)
        request = self._request(request, query)
        self.authority.require_external(self.api.records, request['client'],
            project=query.origin_admission.lease.project_id, source_reader=query.records)
        profile, settings_revision = self._profile(query, request)
        basis = profile['basis'] if profile and profile['items'] else None
        with self.authority.registry.records.begin() as central, self.api.records.begin() as tx, self._source(query, tx, request) as source:
            self.authority.validate(central, native, request['client'], request['tool'])
            if self.shared._settings_revision(tx, request) != settings_revision:
                raise ExternalContextError('external_context_binding_invalid')
            recall = self._recall(query, tx, source)
            plan = recall.plan(request, scene=scene, reader=source)
            selections = [{**row, 'namespace': query.source_namespace} for row in plan['selections']]
            materials = [{**row, 'namespace': query.source_namespace} for row in plan['materials']]
            if basis is not None:
                self._profile_current(query, tx, request, basis)
                namespace = query.origin_admission.personal['namespace']
                materials.extend({'type': 'recognition', 'id': row['id'], 'revision': row['revision'],
                    'project_id': 'me', 'namespace': namespace} for row in basis['items'])
                selections.extend({'type': 'recognition', 'id': row['id'], 'revision': row['revision'],
                    'project_id': 'me', 'namespace': namespace, 'layer': 'L3', 'windows': []} for row in profile['items'])
            selections = _selections_with_scope(selections)
            handoff, mapping, proofs, refs = self._render(query, source, tx, request, materials, selections)
            self._validate_plan(query, source, tx, plan, proofs)
            binding = self.shared._binding(tx, turn_id, request, materials, [proof['snapshot'] for proof in proofs])
            tx.commit()
        shared = {'schema_version': 1, 'kind': request['tool'], 'native': native, 'proofs': proofs,
            'source_namespace': query.source_namespace,
            'plan': plan, 'profile_basis': basis, 'parent': None}
        return self.shared._save(turn_id, request, binding, shared, handoff, selections=selections, mapping=mapping,
            refs=refs, admission=query.origin_admission, **identity)

    def _validate_plan(self, query, source, actor_reader, plan, proofs):
        self._recall(query, actor_reader, source).validate(plan, source)
        by_material = {_material_key(proof['material']): proof for proof in proofs}
        for candidate in plan['candidates']:
            material = {**candidate['material'], 'namespace': query.source_namespace}
            if _encoded(by_material[_material_key(material)]['snapshot']) != _encoded(candidate['snapshot']):
                raise ExternalContextError('external_context_material_changed')

    def _parent(self, tx, request, native, origin):
        if (not isinstance(origin, dict) or not {'turn_id', 'id'} <= origin.keys()
                or any(not isinstance(origin[key], str) or not origin[key] for key in ('turn_id', 'id'))):
            raise ExternalContextError('external_context_binding_invalid')
        ref, archive, outcome = self.api._completed(origin['turn_id'])
        if archive['schema_version'] != SHARED_ARCHIVE_VERSION:
            raise ExternalContextError('external_context_binding_invalid')
        parent = self.shared.request(archive)
        if (archive['shared']['kind'] not in _KIND or archive['shared']['native'] != native
                or origin['id'] not in archive['mapping']
                or any(parent[key] != request[key] for key in ('client', 'scope', 'source_namespace'))):
            raise ExternalContextError('external_context_binding_invalid')
        _delivery(tx, self.api, origin['turn_id'], ref, outcome)
        relation = {'turn_id': origin['turn_id'], 'id': origin['id'], 'immutable_ref': ref, 'outcome_ref': outcome}
        if set(origin) != {'turn_id', 'id'} and origin != relation:
            raise ExternalContextError('external_context_binding_invalid')
        return relation, archive, archive['mapping'][origin['id']]

    @staticmethod
    def _read_refs(proof, selections):
        material, snapshot = proof['material'], proof['snapshot']
        roots = [{**{key: material[key] for key in ('project_id', 'namespace')}, 'type': node['type'],
            'id': node['id'], 'revision': node['source_revision']}
            for node in snapshot['nodes'] if node['type'] in {'original_item', 'original_source'}]
        if not roots or roots != [_material(selection) for selection in selections]:
            raise ExternalContextError('external_context_material_changed')
        return roots

    def prepare_read(self, turn_id, request, selections, *, origin, **identity):
        native, query = self._query(request)
        if request['source_namespace'] != query.source_namespace or turn_id == origin['turn_id']:
            raise ExternalContextError('external_context_material_changed')
        selections = _selections_with_scope(selections)
        with self.authority.registry.records.begin() as central, self.api.records.begin() as tx, self._source(query, tx, request) as source:
            self.authority.validate(central, native, request['client'], 'read')
            parent, old, proof = self._parent(tx, request, native, origin)
            self._validate_current_in_source(old, query, source, tx, link=False)
            materials = self._read_refs(proof, selections)
            handoff, mapping, proofs, refs = self._render(query, source, tx, request, materials, selections)
            self._read_snapshots(proof, proofs)
            binding = self.shared._binding(tx, turn_id, request, materials, [part['snapshot'] for part in proofs])
            tx.commit()
        shared = {'schema_version': 1, 'kind': 'read', 'native': native, 'proofs': proofs,
            'source_namespace': query.source_namespace,
            'plan': None, 'profile_basis': None, 'parent': parent}
        return self.shared._save(turn_id, request, binding, shared, handoff, selections=selections, mapping=mapping,
            refs=refs, admission=query.origin_admission, **identity)

    @staticmethod
    def _read_snapshots(parent, proofs):
        nodes = {(part['type'], part['id']): part for part in parent['snapshot']['nodes']}
        for proof in proofs:
            for part in proof['snapshot']['nodes']:
                if _encoded(nodes.get((part['type'], part['id']))) != _encoded(part):
                    raise ExternalContextError('external_context_material_changed')

    def validate_archive(self, archive):
        request = self.shared.request(archive)
        shared, binding = archive['shared'], archive['binding']
        if (set(shared) != _SHARED or shared['kind'] not in _KIND or shared['kind'] != request['tool']
                or set(request) != {'client', 'tool', 'query', 'budget', 'scope', 'scope_kind', 'source_namespace'}
                or set(request['scope']) != {'user_id', 'project_id'} or request['scope']['user_id'] != self.api.owner_id
                or not isinstance(request['source_namespace'], str) or not request['source_namespace']
                or archive['request']['policy_versions']['handoff'] != '@4' or archive['handoff']['version'] != 'handoff@4'
                or not isinstance(shared['proofs'], list) or not isinstance(binding['material_refs'], list)
                or [_material(row) for row in binding['material_refs']] != binding['material_refs']
                or _selections_with_scope(archive['selections']) != archive['selections']):
            raise ExternalContextError('external_context_binding_invalid')
        if (shared['kind'] == 'read' and (shared['plan'] is not None or shared['profile_basis'] is not None
                or not isinstance(shared['parent'], dict) or shared['parent']['turn_id'] == archive['turn_id'])
                or shared['kind'] != 'read' and (shared['parent'] is not None or not isinstance(shared['plan'], dict)
                or _encoded(shared['plan']['request']) != _encoded(request))):
            raise ExternalContextError('external_context_binding_invalid')
        if ([proof['material'] for proof in shared['proofs']] != binding['material_refs']
                or [proof['snapshot'] for proof in shared['proofs']] != binding['source_snapshots']
                or any(set(proof) != {'material', 'snapshot', 'usage_kind', 'sql_identities'}
                    or proof['usage_kind'] != _USAGE[proof['material']['type']] for proof in shared['proofs'])):
            raise ExternalContextError('external_context_binding_invalid')
        entries = [*archive['handoff']['entries'], *archive['handoff']['profile']]
        if (len(entries) != len(archive['mapping']) or set(archive['mapping']) != {entry['id'] for entry in entries}
                or any(proof not in shared['proofs'] for proof in archive['mapping'].values())):
            raise ExternalContextError('external_context_binding_invalid')

    def _validate_current_in_source(self, archive, query, source, tx, *, link=True):
        request = self.shared._validate_binding(tx, archive)
        shared = archive['shared']
        origins = query.origin_admission.restore(archive['request'])
        if (origins.lease != query.origin_admission.lease or origins.lease.namespace != request['source_namespace']):
            raise ExternalContextError('external_context_material_changed')
        if shared['profile_basis'] is not None:
            self._profile_current(query, tx, request, shared['profile_basis'])
        if shared['plan'] is not None:
            self._validate_plan(query, source, tx, shared['plan'], shared['proofs'])
        if link and shared['parent'] is not None:
            _relation, old, parent = self._parent(tx, request, shared['native'], shared['parent'])
            self._validate_current_in_source(old, query, source, tx, link=False)
            self._read_refs(parent, archive['selections'])
            self._read_snapshots(parent, shared['proofs'])
        handoff, mapping, proofs, _refs = self._render(query, source, tx, request,
            archive['binding']['material_refs'], archive['selections'])
        if (_encoded(handoff) != _encoded(archive['handoff']) or _encoded(mapping) != _encoded(archive['mapping'])
                or _encoded(proofs) != _encoded(shared['proofs'])):
            raise ExternalContextError('external_context_material_changed')

    def resolve(self, archive):
        return self.authority.factory.dispatch(self.authority.application, archive['request'])

    @contextmanager
    def current(self, archive, query, *, central, actor_reader):
        request = self.shared.request(archive)
        actor = self.authority.validate(central, archive['shared']['native'], request['client'], request['tool'])
        if query.origin_admission.actor != actor:
            raise ExternalContextError('external_context_owner_changed')
        with self._source(query, actor_reader, request) as source:
            self._validate_current_in_source(archive, query, source, actor_reader)
            yield query, source
