"""Read current domain metadata without generating or inventing memory evidence."""
from copy import deepcopy
import json

from backend.recognition import RecognitionError, WorkScope

from ..original_sources import original, resolve
from ..source_egress import SourceEgressService
from .external_agent_settings import external_agent_settings
from .overviews import ScopeOverviews, _Reader
from .privacy import external_catalog_allowed, external_egress_allowed, privacy_revision, resolve_turn_material
from .projects import _BUILTINS, DEFAULT_NAME, display_name, scene_of


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class ExternalCatalog:
    """The caller supplies the existing query owner, never a second retrieval engine."""

    def __init__(self, query, *, owner_id):
        self.query, self.records, self.owner_id = query, query.records, owner_id

    def _proof(self, reader, scope, material, authority):
        row, roots = resolve_turn_material(reader, scope, material)
        snapshot = authority.snapshot(scope, roots)
        if material['type'] == 'original_source':
            node = next((node for node in snapshot['nodes']
                if node['type'] == 'original_source' and node['id'] == material['id']), None)
            if (node is None or node['source_revision'] != row['revision']
                    or node.get('incarnation') != row['payload']['_original_incarnation']):
                raise ValueError('external_context_material_changed')
        authority.require(snapshot, 'generation')
        authority.validate_snapshot(scope, snapshot)
        return snapshot

    def _objects(self, reader, project, authority):
        scope, objects = WorkScope(self.owner_id, project), []
        for entry in sorted(self.query.query_entries(project), key=lambda entry: (entry['kind'], entry['id'])):
            try:
                if entry['kind'] == 'document':
                    material = {'type': 'document', 'id': entry['id'], 'revision': entry['revision'], 'project_id': project}
                    assignment = scene_of(reader, 'document', entry['id'])
                    if assignment is None and entry.get('item_id'):
                        assignment = scene_of(reader, 'item', entry['item_id'])
                else:
                    kind, identity = resolve(reader, scope, entry['id'], kind='source')
                    material = {'type': kind, 'id': identity,
                        'revision': original(reader, scope, kind, identity).revision, 'project_id': project}
                    assignment = scene_of(reader, 'item', entry['id'])
                snapshot = self._proof(reader, scope, material, authority)
            except RecognitionError:
                continue
            objects.append({'kind': 'material', 'id': entry['id'], 'material': material,
                'scene': assignment, 'snapshot': snapshot})
        # This is the existing qualified RO domain read, not TransactionRecords.
        for entry in sorted(self.query.service.retrieval_entries(scope=scope), key=lambda entry: entry['id']):
            row = reader.read('recognitions', entry['id'])
            if (row is None or row.revision != entry['revision'] or row.payload.get('state') != 'active'
                    or row.payload.get('scope') != {'user_id': self.owner_id, 'project_id': project}):
                raise ValueError('external_context_material_changed')
            material = {'type': 'recognition', 'id': row.object_id, 'revision': row.revision, 'project_id': project}
            try:
                snapshot = self._proof(reader, scope, material, authority)
            except RecognitionError:
                continue
            objects.append({'kind': 'recognition', 'id': row.object_id, 'material': material,
                'scene': scene_of(reader, 'recognition', row.object_id), 'snapshot': snapshot})
        return objects

    def _overview(self, reader, project, scene, objects):
        own = [row for row in objects if scene is None or row['scene'] == {'project_id': project, 'scene': scene}]
        text = f"资料{sum(row['kind'] == 'material' for row in own)}份 · 认识{sum(row['kind'] == 'recognition' for row in own)}条"
        service = ScopeOverviews(self.records, self.query.documents, None)
        row = service._row(project, scene, reader)
        if row is None:
            return text, None
        signature, summaries = service._snapshot(project, scene, reader)
        if not summaries or _encoded(row.payload.get('input_revision')) != _encoded(signature):
            return text, None
        current = row.payload.get('text')
        if not isinstance(current, str) or not current.strip() or len(current) > 300:
            raise ValueError('external_context_material_changed')
        return current, {'id': row.object_id, 'revision': row.revision, 'input_revision': signature}

    def snapshot(self, client, reader=None):
        if reader is None:
            with self.records.begin() as tx:
                return self.snapshot(client, _Reader(tx))
        reader = _Reader(reader)
        if not external_catalog_allowed(reader, client):
            raise ValueError('external_context_not_allowed')
        settings = external_agent_settings(reader)
        epoch, rows, proofs = privacy_revision(reader), [], []
        projects = {row.object_id: row for row in reader.list('v2_projects')}
        identities = set(projects) | set(_BUILTINS)
        for collection in ('workspace_items', 'documents', 'recognitions'):
            for row in reader.list(collection):
                project = row.payload.get('project_id')
                if project is None and row.payload.get('scope', {}).get('user_id') == self.owner_id:
                    project = row.payload['scope'].get('project_id')
                if isinstance(project, str):
                    identities.add(project)
        for source in self.query.source_store.list('sources'):
            project = source.get('project_id', 'default')
            if isinstance(project, str):
                identities.add(project)
        authority = SourceEgressService(reader)
        for project in sorted(identities):
            if (not external_egress_allowed(reader, project, client)
                    or (project == 'me' and not settings['include_profile'])):
                continue
            row = projects.get(project)
            name = display_name(project, row.payload.get('name')) if row else _BUILTINS.get(project, DEFAULT_NAME if project == 'default' else project)
            scenes = row.payload.get('scenes') if row else []
            if (not isinstance(name, str) or not name.strip() or not isinstance(scenes, list)
                    or any(not isinstance(scene, str) or not scene.strip() for scene in scenes)):
                raise ValueError('external_context_material_changed')
            objects = self._objects(reader, project, authority)
            overview, origin = self._overview(reader, project, None, objects)
            scene_rows, scene_proofs = [], []
            for scene in scenes:
                text, proof = self._overview(reader, project, scene, objects)
                scene_rows.append({'name': scene, 'overview': text})
                scene_proofs.append({'name': scene, 'overview': proof})
            rows.append({'id': project, 'name': name, 'overview': overview, 'scenes': scene_rows})
            proofs.append({'project_id': project, 'revision': row.revision if row else None,
                'objects': objects, 'overview': origin, 'scenes': scene_proofs})
        for project in proofs:
            scope = WorkScope(self.owner_id, project['project_id'])
            for item in project['objects']:
                authority.validate_snapshot(scope, item['snapshot'])
            for overview in [project['overview'], *(scene['overview'] for scene in project['scenes'])]:
                if overview:
                    for member in overview['input_revision']['members']:
                        if 'source_snapshot' in member:
                            authority.validate_snapshot(scope, member['source_snapshot'])
        if privacy_revision(reader) != epoch or external_agent_settings(reader) != settings:
            raise ValueError('external_context_material_changed')
        return {'schema_version': 1, 'owner_id': self.owner_id, 'client': client,
            'settings_revision': settings['revision'], 'privacy_revision': epoch,
            'projects': rows, 'proofs': proofs}

    def validate(self, frozen, reader=None):
        if (not isinstance(frozen, dict) or frozen.get('owner_id') != self.owner_id
                or _encoded(self.snapshot(frozen['client'], reader)) != _encoded(frozen)):
            raise ValueError('external_context_material_changed')
        return deepcopy(frozen)
