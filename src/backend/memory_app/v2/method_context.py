"""Freeze applicable method evidence for the existing task profile reader."""
from backend.recognition import RecognitionConflict, WorkScope

from .budget import source_texts, text_tokens
from .insights import insight_view
from .policies import get, override
from .policies.types import ScopeInput

COLLECTION = 'v2_task_methods'


def prepare_methods(query, project, instruction, scene, *, situation=None, allow_situation_pack=True):
    from .situation_packs import enabled, prepare as prepare_situation_pack
    if not allow_situation_pack and enabled():
        with override(compose='@37'):
            return prepare_methods(query, project, instruction, scene)
    situation = situation if enabled() else None
    collected = query.collect_candidates(project, instruction, scene=scene, **({'situation': situation} if situation else {}))
    pack = prepare_situation_pack(query, project, scene, situation or instruction, methods=collected.get('method_candidates'))
    if not pack and not collected.get('method_candidates') and not collected.get('inspiration_candidates'):
        return {'chosen': []}
    plan = query.prepare_ask(project, instruction, scene=scene, situation=situation, collected={**collected, 'candidates': []})
    chosen = [row for row in plan['chosen'] if row.get('supplemented') or row.get('inspiration')]
    if plan.get('_situation_pack'):
        chosen.extend(plan['_situation_pack']['rows'])
    if query.origin_admission is not None:
        for row in chosen:
            if row['scope'].project_id != project or row.get('namespace', query.source_namespace) != query.source_namespace:
                raise RecognitionConflict('shared_task_method_scope_changed')
        chosen = [{**row, 'namespace': query.source_namespace} for row in chosen]
    return {**plan, 'chosen': chosen}


def freeze_task_methods(records, request, plan, product_turn):
    if not plan['chosen']:
        return
    methods = [{key: row[key] for key in ('id', 'kind', 'entry', 'snapshot', 'scene', 'title', 'excerpt', 'namespace') if key in row}
               for row in plan['chosen'] if not row.get('inspiration') and not row.get('situation_pack')]
    inspirations = [{key: row[key] for key in ('id', 'kind', 'entry', 'snapshot', 'scene', 'title', 'excerpt',
        'inspiration_proof', 'inspiration', 'layer', 'namespace') if key in row} for row in plan['chosen'] if row.get('inspiration')]
    text = '\n\n'.join(source_texts([row for row in plan['chosen'] if not row.get('situation_pack')]))
    pack = plan.get('_situation_pack')
    if pack:
        from .situation_packs import render_task
        packed = [{key: row[key] for key in ('id', 'kind', 'entry', 'snapshot', 'scene', 'title', 'excerpt',
            'namespace', 'situation_pack') if key in row} for row in pack['rows']]
        pack_text = render_task(packed, policy=get('situation_pack', version=pack['policy']))
        pack = {**{key: value for key, value in pack.items() if key != 'rows'}, 'members': packed,
                'text': pack_text, 'tokens': text_tokens(pack_text)}
    with records.begin() as tx:
        tx.put(COLLECTION, request['turn_id'], {'turn_id': product_turn,
            'project_id': request['scope']['project_id'], 'input_refs': request['input']['refs'],
            'policy_versions': request['policy_versions'], 'target': plan['target'],
            'requested_scene': plan.get('requested_scene'), 'methods': methods, 'text': text,
            **({'inspirations': inspirations} if inspirations else {}),
            **({'situation_pack': pack} if pack else {}),
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
    pack = values.get('situation_pack')
    with override(**values['policy_versions']):
        for method in [*values['methods'], *(pack['members'] if pack else ())]:
            descriptor = {'type': 'recognition', 'id': method['id'],
                          'revision': method['entry']['revision'], 'project_id': project}
            source_records = query.records if query.origin_admission is not None else records
            view = insight_view(source_records, WorkScope('local-user', project), method['id'], service=query.service)
            if query.origin_admission is not None and method.get('namespace') != query.source_namespace:
                raise RecognitionConflict('shared_task_method_scope_changed')
            if (descriptor not in request['privacy']['material_refs']
                    or method['snapshot'] not in request['privacy']['source_snapshots']
                    or view is None or not get('scope')(ScopeInput(values.get('requested_scene'), view['scene']))):
                raise RecognitionConflict('task method authority changed')
            chosen.append({**{key: value for key, value in method.items() if key != 'situation_pack'},
                'scope': WorkScope('local-user', project)})
        for inspiration in values.get('inspirations', []):
            own = inspiration['entry']['project_id']
            descriptor = {'type': 'experience', 'id': inspiration['id'],
                          'revision': inspiration['entry']['revision'], 'project_id': own}
            if query.origin_admission is not None and (own != project or inspiration.get('namespace') != query.source_namespace):
                raise RecognitionConflict('shared_task_method_scope_changed')
            if (descriptor not in request['privacy']['material_refs']
                    or inspiration['snapshot'] not in request['privacy']['source_snapshots']):
                raise RecognitionConflict('task inspiration authority changed')
            chosen.append({**inspiration, 'scope': WorkScope('local-user', own)})
        query.validate_ask_plan({'project_id': project, 'scope': WorkScope('local-user', project),
                                 'target': values['target'], 'chosen': chosen})
        if pack:
            from .situation_packs import render_task, validate
            from .policies.situation_pack import version_for_compose
            if (pack['project_id'] != project or pack['scene'] != values['requested_scene']
                    or pack['policy'] != version_for_compose(values['policy_versions'].get('compose'))
                    or pack['text'] != render_task(pack['members']) or pack['tokens'] != text_tokens(pack['text'])):
                raise RecognitionConflict('task_situation_pack_binding_changed')
            validate(query, pack, pack['members'])
    return {'text': values['text'], 'tokens': values['tokens'], 'count': len(chosen) - len(pack['members'] if pack else ()),
            **({'situation_pack': {'text': pack['text'], 'tokens': pack['tokens'], 'count': len(pack['members'])}} if pack else {})}
