"""Freeze applicable method evidence for the existing task profile reader."""
from backend.recognition import RecognitionConflict, WorkScope

from .budget import source_texts, text_tokens
from .insights import insight_view
from .policies import get, override
from .policies.types import ScopeInput

COLLECTION = 'v2_task_methods'


def prepare_methods(query, project, instruction, scene):
    collected = query.collect_candidates(project, instruction, scene=scene)
    if not collected.get('method_candidates') and not collected.get('inspiration_candidates'):
        return {'chosen': []}
    plan = query.prepare_ask(project, instruction, scene=scene, collected={**collected, 'candidates': []})
    return {**plan, 'chosen': [row for row in plan['chosen'] if row.get('supplemented') or row.get('inspiration')]}


def freeze_task_methods(records, request, plan, product_turn):
    if not plan['chosen']:
        return
    methods = [{key: row[key] for key in ('id', 'kind', 'entry', 'snapshot', 'scene', 'title', 'excerpt')}
               for row in plan['chosen'] if not row.get('inspiration')]
    inspirations = [{key: row[key] for key in ('id', 'kind', 'entry', 'snapshot', 'scene', 'title', 'excerpt',
        'inspiration_proof', 'inspiration', 'layer')} for row in plan['chosen'] if row.get('inspiration')]
    text = '\n\n'.join(source_texts(plan['chosen']))
    with records.begin() as tx:
        tx.put(COLLECTION, request['turn_id'], {'turn_id': product_turn,
            'project_id': request['scope']['project_id'], 'input_refs': request['input']['refs'],
            'policy_versions': request['policy_versions'], 'target': plan['target'],
            'requested_scene': plan.get('requested_scene'), 'methods': methods, 'text': text,
            **({'inspirations': inspirations} if inspirations else {}),
            'tokens': text_tokens(text)}, expected_revision=0)
        tx.commit()


def frozen_task_methods(records, query, request):
    row = records.read(COLLECTION, request['turn_id'])
    if row is None:
        return {}
    values = row.payload
    if (row.revision != 1 or values['project_id'] != request['scope']['project_id']
            or values['input_refs'] != request['input']['refs']
            or values['policy_versions'] != request.get('policy_versions')):
        raise RecognitionConflict('task method binding changed')
    project = values['project_id']
    chosen = []
    with override(**values['policy_versions']):
        for method in values['methods']:
            descriptor = {'type': 'recognition', 'id': method['id'],
                          'revision': method['entry']['revision'], 'project_id': project}
            view = insight_view(records, WorkScope('local-user', project), method['id'], service=query.service)
            if (descriptor not in request['privacy']['material_refs']
                    or method['snapshot'] not in request['privacy']['source_snapshots']
                    or view is None or not get('scope')(ScopeInput(values.get('requested_scene'), view['scene']))):
                raise RecognitionConflict('task method authority changed')
            chosen.append({**method, 'scope': WorkScope('local-user', project)})
        for inspiration in values.get('inspirations', []):
            own = inspiration['entry']['project_id']
            descriptor = {'type': 'experience', 'id': inspiration['id'],
                          'revision': inspiration['entry']['revision'], 'project_id': own}
            if (descriptor not in request['privacy']['material_refs']
                    or inspiration['snapshot'] not in request['privacy']['source_snapshots']):
                raise RecognitionConflict('task inspiration authority changed')
            chosen.append({**inspiration, 'scope': WorkScope('local-user', own)})
        query.validate_ask_plan({'project_id': project, 'scope': WorkScope('local-user', project),
                                 'target': values['target'], 'chosen': chosen})
    return {'text': values['text'], 'tokens': values['tokens'], 'count': len(chosen)}
