"""Copy fallback assembled by the existing qualified external-context owners."""
from copy import deepcopy
from uuid import uuid4

from backend.recognition import RecognitionError, WorkScope

from ..recall_state import is_recall_excluded
from ..source_egress import SourceEgressService
from ..workspace_contracts import _now
from .external_agent_guard import ExternalAgentGuardError
from .external_agent_settings import external_agent_settings
from .insights import insight_view
from .policies import override, version
from .policies.pipelines import versions_for_turn
from .privacy import external_egress_allowed
from .profile import confirmed_profile


VERSION = 'external-snapshot@1'
BEGIN = '<!-- chriptmas-memory:' + VERSION + ':begin -->'
END = '<!-- chriptmas-memory:' + VERSION + ':end -->'


def _plan(api, workspace, request):
    records, query = api.records, workspace.query
    project, client = request['scope']['project_id'], request['client']
    if not external_egress_allowed(records, project, client):
        raise ExternalAgentGuardError('external_agent_remote_blocked')
    settings = external_agent_settings(records)
    if project == 'me' and not settings['include_profile']:
        raise ExternalAgentGuardError('external_agent_profile_disabled')
    versions = {**versions_for_turn('external.context'), 'compose': version('compose')}
    with override(**versions):
        scope, authority = WorkScope(api.owner_id, project), SourceEgressService(records)
        candidates, selections = [], []
        for entry in sorted(query.service.retrieval_entries(scope=scope), key=lambda row: row['id']):
            if not entry['conditions'] or is_recall_excluded(records, scope, entry['id']):
                continue
            view = insight_view(records, scope, entry['id'], service=query.service)
            if view is None or view['state'] != 'active':
                continue
            try:
                snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': entry['id'],
                    'revision': entry['revision']}])
                authority.require(snapshot, 'generation')
            except RecognitionError:
                continue
            material = {'type': 'recognition', 'id': entry['id'], 'revision': entry['revision'],
                'project_id': project}
            candidates.append(api.recall._candidate({'kind': 'recognition', 'entry': entry,
                'scope': scope, 'snapshot': snapshot, 'document_ids': view['document_ids']}))
            selections.append({**material, 'layer': 'L3', 'windows': []})
        profile = (confirmed_profile(records, query.service)
            if settings['include_profile'] and external_egress_allowed(records, 'me', client) else None)
        basis = deepcopy(profile['basis']) if profile is not None else None
        if profile is not None:
            for row in basis['items']:
                material = {'type': 'recognition', 'id': row['id'], 'revision': row['revision'], 'project_id': 'me'}
                proof = {'material': material, 'entry_kind': 'recognition', 'entry_id': row['id'],
                    'item_id': None, 'document_ids': [], 'snapshot': row['snapshot']}
                if proof not in candidates:
                    candidates.append(proof)
            for row in profile['items']:
                selection = {'type': 'recognition', 'id': row['id'], 'revision': row['revision'],
                    'project_id': 'me', 'layer': 'L3', 'windows': []}
                if selection not in selections:
                    selections.append(selection)
        materials = list({(row['material']['type'], row['material']['id'], row['material']['project_id']):
            row['material'] for row in candidates}.values())
        projects = {project} | ({'me'} if profile is not None else set())
        plan = {'schema_version': 1, 'owner_id': api.owner_id, 'request': deepcopy(request), 'scene': None,
            'policy_versions': versions, 'materials': materials, 'selections': selections,
            'candidates': candidates, 'profile_basis': basis, 'markers': api.recall._markers(records, projects)}
    api.recall.validate(plan)
    return plan


def generate_snapshot(application, workspace, *, client, project_id, budget):
    api = application.state.external_context
    request = {'client': client, 'tool': 'methods', 'query': VERSION,
        'scope': {'user_id': api.owner_id, 'project_id': project_id}, 'budget': budget}
    plan = _plan(api, workspace, request)
    runtime = workspace.query.answer_turns._runtime()
    turn_id, generated_at = 'turn-' + uuid4().hex, _now()
    with override(**plan['policy_versions']):
        api.prepare(turn_id, request, plan['selections'], _recall=plan,
            session_id='session-snapshot-' + uuid4().hex, operation_id='snapshot-' + turn_id,
            idempotency_key=turn_id, created_at=generated_at)
    result = api.execute(turn_id, runtime=runtime, runner=application.state.ai_turn_runner)
    text = '\n'.join((BEGIN, '生成时间：' + generated_at, 'turn_id: ' + turn_id,
        result['text'], END))
    return {'version': VERSION, 'project_id': project_id, 'client': client,
        'generated_at': generated_at, 'turn_id': turn_id, 'text': text, 'result': result}
