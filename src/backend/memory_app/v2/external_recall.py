"""Select external evidence through the existing qualified retrieval owners."""
from copy import deepcopy
import json

from backend.recognition import WorkScope

from ..recall_state import is_recall_excluded
from ..source_egress import SourceEgressService
from .external_agent_guard import ExternalAgentGuardError
from .external_agent_settings import external_agent_settings
from .ladder import plan_ladder
from .layers import summary_of
from .overviews import _Reader
from .policies import override, version
from .policies.pipelines import versions_for_turn
from .privacy import external_egress_allowed, resolve_turn_material
from .profile import confirmed_profile, validate_profile
from .usage import recall_weight


_FIELDS = {'schema_version', 'owner_id', 'request', 'scene', 'policy_versions',
           'materials', 'selections', 'candidates', 'profile_basis', 'markers'}
_CANDIDATE = {'material', 'entry_kind', 'entry_id', 'item_id', 'document_ids', 'snapshot'}
_SIDECARS = ('recognition_recall_preferences', 'v2_document_recall', 'v2_insight_validity',
             'v2_scene_assignments_item', 'v2_scene_assignments_document',
             'v2_scene_assignments_candidate', 'v2_scene_assignments_recognition')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _material(kind, identity, revision, project):
    return {'type': kind, 'id': identity, 'revision': revision, 'project_id': project}


class ExternalRecall:
    def __init__(self, query, *, owner_id):
        self.query, self.records, self.owner_id = query, query.records, owner_id

    def _markers(self, reader, projects):
        identities = {row.object_id for collection in ('workspace_items', 'documents', 'recognitions', 'recognition_candidates')
            for row in reader.list(collection)
            if row.payload.get('project_id') in projects
            or row.payload.get('scope') in [{'user_id': self.owner_id, 'project_id': project} for project in projects]}
        return [{'collection': collection, 'id': row.object_id, 'revision': row.revision, 'payload': dict(row.payload)}
            for collection in _SIDECARS for row in sorted(reader.list(collection), key=lambda row: row.object_id)
            if row.object_id in identities or row.payload.get('project_id') in projects]

    def _candidate(self, candidate):
        entry, project = candidate['entry'], candidate['scope'].project_id
        kind = 'recognition' if candidate['kind'] == 'recognition' else 'document' if candidate['kind'] == 'document' else 'original_source'
        return {'material': _material(kind, entry['id'], entry['revision'], project),
            'entry_kind': candidate['kind'], 'entry_id': entry['id'], 'item_id': entry.get('item_id'),
            'document_ids': list(candidate.get('document_ids', [])), 'snapshot': deepcopy(candidate['snapshot'])}

    def _selection(self, candidate):
        entry, project = candidate['entry'], candidate['scope'].project_id
        layer, windows = candidate['layer'], candidate['windows']
        if layer == 'L3':
            return {**_material('recognition', entry['id'], entry['revision'], project), 'layer': layer, 'windows': []}
        if candidate['kind'] == 'document' and layer != 'L0':
            material = _material('document', entry['id'], entry['revision'], project)
            text = self.query.documents.markdown(entry['id'], revision=entry['revision'])
            offset = 0
            if layer == 'L2':
                _summary, offset, end = summary_of(text)
                if any(not offset <= window.start < window.end <= end for window in windows):
                    raise ValueError('external_context_selection_invalid')
        else:
            identity = entry.get('item_id') if candidate['kind'] == 'document' else entry['id']
            kind = 'original_item' if candidate['kind'] == 'document' else 'original_source'
            revision = entry['item_revision'] if kind == 'original_item' else entry['revision']
            material = _material(kind, identity, revision, project)
            row, _roots = resolve_turn_material(self.records, candidate['scope'], material)
            body = row['payload']
            text = body.get('source_text') if kind == 'original_item' else body['metadata'].get('content_snapshot') or body['metadata'].get('content')
            offset = 0
        if not isinstance(text, str) or any(text[window.start:window.end] != window.text for window in windows):
            raise ValueError('external_context_material_changed')
        return {**material, 'layer': layer,
            'windows': [{'start': window.start - offset, 'end': window.end - offset} for window in windows]}

    def plan(self, request, *, scene=None):
        if request['tool'] not in {'recall', 'methods'} or request['scope']['user_id'] != self.owner_id:
            raise ValueError('external_context_binding_invalid')
        project, client = request['scope']['project_id'], request['client']
        if not external_egress_allowed(self.records, project, client):
            raise ValueError('external_agent_remote_blocked')
        if project == 'me' and not external_agent_settings(self.records)['include_profile']:
            raise ExternalAgentGuardError('external_agent_profile_disabled')
        versions = {**versions_for_turn('external.context'), 'compose': version('compose')}
        with override(**versions):
            collected = self.query.collect_candidates(project, request['query'], scene=scene,
                situation=request['query'], external_client=client)
            ordinary = [row for row in collected['candidates'] if row['snapshot'] is not None]
            methods = [row for row in collected.get('method_candidates', []) if row['snapshot'] is not None]
            planned = plan_ladder(ordinary if request['tool'] == 'recall' else [], request['query'],
                token_budget=request['budget'], methods=methods, situation=request['query'])
            settings = external_agent_settings(self.records)
            profile = (confirmed_profile(self.records, self.query.service)
                if settings['include_profile'] and external_egress_allowed(self.records, 'me', client) else None)
            candidates = []
            for row in [*ordinary, *methods]:
                proof = self._candidate(row)
                if proof not in candidates:
                    candidates.append(proof)
            selections = [self._selection(row) for row in planned['chosen']]
            basis = deepcopy(profile['basis']) if profile else None
            if profile:
                selections.extend({**_material('recognition', row['id'], row['revision'], 'me'), 'layer': 'L3', 'windows': []}
                    for row in profile['items'])
                candidates.extend({'material': _material('recognition', row['id'], row['revision'], 'me'),
                    'entry_kind': 'recognition', 'entry_id': row['id'], 'item_id': None,
                    'document_ids': [], 'snapshot': row['snapshot']} for row in basis['items'])
            materials = [row['material'] for row in candidates]
            materials.extend({key: row[key] for key in ('type', 'id', 'revision', 'project_id')} for row in selections)
            materials = list({(row['type'], row['id'], row['project_id']): row for row in materials}.values())
            projects = {project} | ({'me'} if profile else set())
            plan = {'schema_version': 1, 'owner_id': self.owner_id, 'request': deepcopy(request), 'scene': scene,
                'policy_versions': versions, 'materials': materials, 'selections': selections,
                'candidates': candidates, 'profile_basis': basis, 'markers': self._markers(self.records, projects)}
        self.validate(plan)
        return plan

    def validate(self, plan, reader=None):
        if (not isinstance(plan, dict) or set(plan) != _FIELDS or type(plan['schema_version']) is not int
                or plan['schema_version'] != 1 or plan['owner_id'] != self.owner_id
                or plan['request']['tool'] not in {'recall', 'methods'}
                or plan['request']['scope']['user_id'] != self.owner_id
                or set(plan['policy_versions']) != set(versions_for_turn('external.context')) | {'compose'}):
            raise ValueError('external_context_binding_invalid')
        with override(**plan['policy_versions']):
            if reader is None:
                if plan['profile_basis'] is not None:
                    validate_profile(self.records, self.query.service, {'basis': plan['profile_basis']})
                with self.records.begin() as tx:
                    return self.validate(plan, _Reader(tx))
            reader = _Reader(reader)
            if plan['profile_basis'] is not None:
                validate_profile(reader, self.query.service, {'basis': plan['profile_basis']})
            request = plan['request']
            if not external_egress_allowed(reader, request['scope']['project_id'], request['client']):
                raise ValueError('external_agent_remote_blocked')
            projects = {request['scope']['project_id']} | ({'me'} if plan['profile_basis'] is not None else set())
            if _encoded(self._markers(reader, projects)) != _encoded(plan['markers']):
                raise ValueError('external_context_material_changed')
            authority = SourceEgressService(reader)
            for proof in plan['candidates']:
                if not isinstance(proof, dict) or set(proof) != _CANDIDATE:
                    raise ValueError('external_context_binding_invalid')
                material = proof['material']
                scope = WorkScope(self.owner_id, material['project_id'])
                resolved, _roots = resolve_turn_material(reader, scope, material)
                authority.validate_snapshot(scope, proof['snapshot'])
                authority.require(proof['snapshot'], 'generation')
                if material['type'] == 'recognition':
                    recognition = self.query.service.get_recognition(scope=scope, recognition_id=material['id'])
                    if (recognition is None or not recognition.authorized or recognition.revision != material['revision']
                            or is_recall_excluded(reader, scope, material['id'])):
                        raise ValueError('external_context_material_changed')
                else:
                    entries = self.query.query_entries(scope.project_id,
                        selected=[{'kind': proof['entry_kind'], 'id': proof['entry_id']}])
                    if not any(row['id'] == proof['entry_id'] and row['revision'] == material['revision']
                        and row.get('item_id') == proof['item_id'] for row in entries):
                        raise ValueError('external_context_material_changed')
                    documents = [material['id']] if material['type'] == 'document' else proof['document_ids']
                    if documents and all(recall_weight(reader, 'document', identity) == 0 for identity in documents):
                        raise ValueError('external_context_material_changed')
                    if material['type'] == 'original_source':
                        node = next(node for node in proof['snapshot']['nodes']
                            if node['type'] == 'original_source' and node['id'] == material['id'])
                        if node.get('incarnation') != resolved['payload']['_original_incarnation']:
                            raise ValueError('external_context_material_changed')
            return deepcopy(plan)

    def validate_binding(self, plan, binding):
        for proof in plan['candidates']:
            slot = next(index for index, material in enumerate(plan['materials']) if material == proof['material'])
            if _encoded(proof['snapshot']) != _encoded(binding['source_snapshots'][slot]):
                raise ValueError('external_context_material_changed')
