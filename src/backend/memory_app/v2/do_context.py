"""Body-free projections of the frozen task messages and durable model calls."""
from collections.abc import Mapping
import json
from backend.shared.llm.message_metadata import _estimate_input_tokens
from backend.recognition import RecognitionConflict

KEYS = ('instruction', 'insight', 'source', 'expert_brief', 'question')
EXPERT_PREFIX = '以下是专家团队的研究结论，仅供参考；与资料冲突时以资料为准：\n'


def packet_context(packet, limits=None):
    messages = [dict(message) for message in packet['messages']]
    parts = {key: {'key': key, 'count': 0, 'tokens': 0} for key in KEYS}
    estimate = _estimate_input_tokens(messages)

    def remove(index, text, key, *, tail=False):
        nonlocal estimate
        content = messages[index].get('content')
        if not text or not isinstance(content, str) or text not in content:
            return
        messages[index]['content'] = content[:-len(text)] if tail else content.replace(text, '', 1)
        current = _estimate_input_tokens(messages)
        parts[key]['count'] += 1
        parts[key]['tokens'] += estimate - current
        estimate = current

    for index, message in enumerate(messages):
        text = message.get('content')
        if message.get('role') != 'user' or not isinstance(text, str):
            continue
        if text.startswith(EXPERT_PREFIX):
            remove(index, text, 'expert_brief')
            continue
        question = 'Task:\n' + packet.get('query', '')
        if text.endswith(question):
            remove(index, question, 'question', tail=True)
        for node in packet.get('graph', {}).get('nodes', []):
            content = node.get('metadata', {}).get('content')
            kind = node.get('node_type')
            if kind in {'conclusion', 'source'} and isinstance(content, str):
                remove(index, content, 'insight' if kind == 'conclusion' else 'source')
    parts['instruction'].update(tokens=estimate, count=sum(bool(message.get('content')) for message in messages))
    return {'window': (limits or {}).get('window'), 'reserve': (limits or {}).get('reserve'), 'parts': list(parts.values())}


def aggregate_usage(rows):
    seen = set()
    total = {'input_tokens': 0, 'output_tokens': 0}
    if not rows:
        return None
    for row in rows:
        identity = (row['turn_id'], row['model_request_id'])
        if identity in seen:
            continue
        seen.add(identity)
        usage = row.get('usage')
        if not isinstance(usage, Mapping):
            return None
        for key, alias in [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')]:
            value = usage.get(key, usage.get(alias))
            if type(value) is not int or value < 0:
                return None
            total[key] += value
    return total


def kernel_task_context(records, project, identity):
    from ..kernel.receipt_projection import kernel_call_groups, aggregate_usage as kernel_usage, egress_basis, aggregate_cost
    from ..original_sources import source_store
    groups = kernel_call_groups(source_store(records).root.parent, turn_id=identity,
                                project=project, remote_only=False, records=records)
    calls = [call for group in groups for call in group['calls']]
    main_sent = any(call['turn_id'] == identity for call in calls)
    request = next((group['request'] for group in groups if group['turn_id'] == identity), {})
    refs = request.get('privacy', {}).get('material_refs', [])
    attributed = set()
    parts = [{'key': key, 'count': 1 if key == 'question' and request else
              len(refs) if key == 'source' else None, 'tokens': None} for key in KEYS]
    profile_row = records.read('v2_task_profiles', identity)
    if profile_row and profile_row.payload['project_id'] == project:
        profile = profile_row.payload['profile']
        if profile['text'] and calls:
            parts.insert(1, {'key':'persona', 'count':profile['count'], 'tokens':profile['tokens']})
            profile_ids = {item['id'] for item in profile['items']}
            attributed.update(('recognition', 'me', identity) for identity in profile_ids)
            next(part for part in parts if part['key'] == 'source')['count'] = sum(
                not (ref['type'] == 'recognition' and ref['project_id'] == 'me' and ref['id'] in profile_ids)
                for ref in refs)
    egress = egress_basis(calls)
    usage = kernel_usage(calls)
    if usage and usage.get('observed_only'):
        usage = None
    # Legacy task packets contain category token budgets. Organization calls do
    # not persist that attribution; unknown values remain unknown, not invented.
    context = {'window': None, 'reserve': None, 'parts': parts, 'egress': egress}
    methods = records.read('v2_task_methods', identity)
    if (methods is not None and methods.revision == 1 and methods.payload['project_id'] == project
            and methods.payload['input_refs'] == request.get('input', {}).get('refs')
            and methods.payload['policy_versions'] == request.get('policy_versions') and calls
            and all({'type': 'recognition', 'id': row['id'], 'revision': row['entry']['revision'],
                     'project_id': project} in refs for row in methods.payload['methods'])):
        context['entries'] = [{'layer': 'insight', 'id': row['id'], 'title': row['title'],
                               'supplemented': True, 'object_revision': row['entry']['revision']}
                              for row in methods.payload['methods']]
        context['feedback'] = {'turn_id': methods.payload['turn_id'], 'project_id': project}
        next(part for part in parts if part['key'] == 'insight')['count'] = len(context['entries'])
        method_ids = {row['id'] for row in methods.payload['methods']}
        attributed.update(('recognition', project, identity) for identity in method_ids)
        source = next(part for part in parts if part['key'] == 'source')
        source['count'] -= sum(ref['type'] == 'recognition' and ref['project_id'] == project
                               and ref['id'] in method_ids for ref in refs)
        inspirations = methods.payload.get('inspirations', [])
        if inspirations and all({'type': 'experience', 'id': row['id'], 'revision': row['entry']['revision'],
                                'project_id': row['entry']['project_id']} in refs for row in inspirations):
            context['entries'].extend({'layer': 'inspiration', 'id': row['id'], 'title': row['title']}
                                      for row in inspirations)
            parts.append({'key': 'inspiration', 'count': len(inspirations), 'tokens': None})
            source['count'] -= len(inspirations)
    try:
        product = json.loads(request.get('input', {}).get('text', 'null'))
    except (ValueError, TypeError):
        product = None
    if isinstance(product, dict) and main_sent:
        from .style_context import style_input
        style = records.read('v2_task_styles', identity)
        if (style is not None and style.revision == 1 and style.payload['project_id'] == project
                and style.payload['input_refs'] == request.get('input', {}).get('refs')
                and product.get('style_input') == style_input(style.payload['style'])
                and style.payload['style']['text']):
            writing = style.payload['style']
            parts.append({'key':'style', 'count':writing['count'], 'tokens':writing['tokens']})
            identifiers = {('recognition', item['project_id'], item['id']) for item in writing['items']}
            source = next(part for part in parts if part['key'] == 'source')
            source['count'] -= sum((ref['type'], ref['project_id'], ref['id']) in identifiers - attributed
                                   for ref in refs)
        prior = product.get('outcome_input')
        if isinstance(prior, dict) and prior.get('document_id') is not None and isinstance(prior.get('current_markdown'), str):
            from .budget import text_tokens
            parts.append({'key':'previous', 'count':1, 'tokens':text_tokens(prior['current_markdown'])})
    return {'context': context, 'model_usage': usage, 'model_cost': aggregate_cost(calls),
            'model': egress['model'], 'egress': egress}


def task_context(records, scope, status, previous, models, usage_reader):
    packet = records.read('recognition_context_packets', status.get('context_packet_id'))
    if packet is None or packet.payload.get('project_id') != scope.project_id:
        raise RecognitionConflict('task context is unavailable in this project')
    reader = getattr(models, 'generation_budget_limits', None)
    limits = reader(expected_revision=packet.payload.get('model_revision'), max_tokens=7000) if callable(reader) else None
    rows = []
    if usage_reader is not None:
        task_turn = status.get('turn_id')
        if task_turn:
            rows.extend(usage_reader(task_turn, scope.project_id, descendants=False))
        research_turn = previous.get('research_turn_id')
        if research_turn:
            rows.extend(usage_reader(research_turn, scope.project_id, descendants=True))
    from .task_egress import task_call_groups
    from ..original_sources import source_store
    groups = task_call_groups(records, source_store(records).root.parent,
        task_id=status.get('task_id'), project=scope.project_id) if status.get('task_id') else []
    if groups:
        rows = [call for group in groups for call in group]
    context = packet_context(packet.payload, limits)
    models_used = sorted({row['model_id'] for row in rows if isinstance(row.get('model_id'), str)})
    bases=[row['egress'] for row in rows if row.get('egress') is not None]
    latest=bases[-1] if bases else {}
    egress={'model':' · '.join(models_used) or None,
        'consent_scope':latest.get('consent_scope'), 'settings_revision':latest.get('settings_revision')}
    context['egress']=egress
    from ..kernel.receipt_projection import aggregate_cost
    return {'context': context, 'model_usage': aggregate_usage(rows), 'model_cost': aggregate_cost(rows),
            'model': egress['model'], 'egress':egress}
