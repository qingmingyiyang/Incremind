from copy import deepcopy

from backend.recognition.sql_source_identities import CollectingReader, capture_identities

from .external_catalog import ExternalCatalog, _encoded
from .material_origin_readers import qualified_record_read
from .overviews import _Reader
from .privacy import is_private_project, privacy_revision


class SharedExternalCatalog:
    def __init__(self, authority, records, *, owner_id):
        self.authority, self.records, self.owner_id = authority, records, owner_id

    def resolve(self, actor):
        authority = self.authority
        return {row['id']: authority.factory.query(authority.application, actor, row['id'])
            for row in authority.registry.list(actor) if row['state'] == 'active'}

    def snapshot(self, client, native, *, central, actor_reader, queries):
        authority = self.authority
        actor = authority.validate(central, native, client, 'projects')
        authority.require_external(actor_reader, client)
        rows, proofs = [], []
        projects = sorted((row for row in authority.registry.list(actor) if row['state'] == 'active'),
            key=lambda row: row['id'])
        if set(queries) != {row['id'] for row in projects}:
            raise ValueError('external_context_material_changed')
        for project in projects:
            project_id = project['id']
            query = queries[project_id]
            admission, namespace = query.origin_admission, query.source_namespace
            if admission.actor != actor or admission.lease.project_id != project_id:
                raise ValueError('external_context_material_changed')
            with qualified_record_read(query.records, validate=query.validate_retrieval_origin) as source:
                if is_private_project(source, project_id):
                    continue
                collector = CollectingReader(source)
                reader = _Reader(collector)
                stored = reader.read('v2_projects', project_id)
                if stored is None:
                    raise ValueError('external_context_material_changed')
                catalog = ExternalCatalog(query, owner_id=self.owner_id)
                row, proof = catalog.project_snapshot(reader, project_id, name=project['name'],
                    scenes=stored.payload['scenes'], revision=stored.revision)
                catalog.validate_project(reader, proof)
                for item in proof['objects']:
                    item['material']['namespace'] = namespace
                rows.append({**row, 'scope_kind': 'shared', 'namespace': namespace})
                proofs.append({**proof, 'namespace': namespace, 'lease': admission.lease.payload(),
                    'privacy_revision': privacy_revision(source), 'sql_identities': capture_identities(source, collector)})
        authority.validate(central, native, client, 'projects')
        return {'schema_version': 2, 'owner_id': self.owner_id, 'client': client, 'projects': rows, 'proofs': proofs}

    def validate(self, frozen, native, *, central, actor_reader, queries):
        if (not isinstance(frozen, dict) or frozen.get('owner_id') != self.owner_id
                or _encoded(self.snapshot(frozen['client'], native, central=central, actor_reader=actor_reader, queries=queries))
                    != _encoded(frozen)):
            raise ValueError('external_context_material_changed')
        return deepcopy(frozen)
