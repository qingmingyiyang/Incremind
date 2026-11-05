from copy import deepcopy


def v1(parts, changes):
    if not isinstance(changes, list) or len(changes) > len(parts):
        raise ValueError('invalid_route_correction')
    used, output, mapping = [], [], {}
    for index, change in enumerate(changes):
        if not isinstance(change, dict) or set(change) != {'sources', 'intent', 'depends_on'}:
            raise ValueError('invalid_route_correction')
        sources, dependencies = change['sources'], change['depends_on']
        if (not isinstance(sources, list) or not sources
                or any(type(value) is not int or value < 0 or value >= len(parts) for value in sources)
                or sources != sorted(set(sources))
                or not isinstance(change['intent'], str) or change['intent'] not in {'remember', 'ask', 'do'}
                or not isinstance(dependencies, list)
                or any(type(value) is not int or value < 0 or value >= len(changes) or value == index for value in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise ValueError('invalid_route_correction')
        used.extend(sources)
        for source in sources:
            mapping[source] = index
        attachments = list(dict.fromkeys(item for source in sources for item in parts[source].get('item_ids', [])))
        original = parts[sources[0]]
        members = list({member['turn_id']: member for source in sources for member in parts[source].get('source_members', [])}.values())
        output.append({**deepcopy(original), 'sources': sources, 'intent': change['intent'],
            'span': '\n'.join(parts[source]['span'] for source in sources),
            'instruction': original.get('instruction') if len(sources) == 1 and change['intent'] == original['intent'] else None,
            'depends_on': dependencies, 'item_ids': attachments, 'source_members': members,
            'attachment_aggregate': bool(attachments and change['intent'] == 'remember'
                and all(parts[source]['intent'] == 'remember' for source in sources) and len(members) > 1)})
    if used != sorted(set(used)):
        raise ValueError('route_correction_order_changed')
    affected = set()
    for index, part in enumerate(output):
        original = parts[part['sources'][0]]
        old_dependencies = original['depends_on']
        missing = any(source not in mapping for source in old_dependencies)
        remapped = [mapping[source] for source in old_dependencies if source in mapping]
        if (len(part['sources']) != 1 or part['intent'] != original['intent']
                or missing or remapped != part['depends_on']):
            affected.add(index)
        for dependency in part['depends_on']:
            if (output[dependency]['intent'], part['intent']) not in {('remember', 'ask'), ('remember', 'do'), ('ask', 'do')}:
                raise ValueError('route_correction_dependency_direction')
    # 依赖闭包逐层传播；未改变的结果保留其原始执行与来源身份。
    while True:
        expanded = affected | {index for index, part in enumerate(output) if set(part['depends_on']) & affected}
        if expanded == affected:
            break
        affected = expanded
    ready = set()
    while len(ready) < len(output):
        current = {index for index, part in enumerate(output) if index not in ready and set(part['depends_on']) <= ready}
        if not current:
            raise ValueError('route_correction_dependency_cycle')
        ready.update(current)
    return {'parts': output, 'affected': sorted(affected),
        'deleted': [index for index in range(len(parts)) if index not in mapping]}


def rank_examples(candidates):
    return sorted(candidates, key=lambda row: (row['same_project'], row['corrected'], row['score'], row['id']), reverse=True)[:3]


v1.rank_examples = rank_examples
v1.keyword_threshold = .2


def v2(parts, changes):
    if not isinstance(changes, list):
        raise ValueError('invalid_route_correction')
    normalized, desired = [], []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'sources', 'intent', 'depends_on', 'target_source'}:
            raise ValueError('invalid_route_correction')
        sources, intent, target = change['sources'], change['intent'], change['target_source']
        if (not isinstance(sources, list) or type(target) is not int or target not in sources
                or any(type(source) is not int or not 0 <= source < len(parts) for source in sources)):
            raise ValueError('route_correction_target_invalid')
        if intent == 'inspiration' and (len(sources) != 1 or parts[sources[0]]['intent'] != 'inspiration'):
            raise ValueError('invalid_route_correction')
        normalized.append({key: deepcopy(change[key]) for key in ('sources', 'intent', 'depends_on')})
        normalized[-1]['intent'] = 'remember' if intent == 'inspiration' else intent
        desired.append(intent)
    inputs = [{**deepcopy(part), 'intent': 'remember' if part['intent'] == 'inspiration' else part['intent']} for part in parts]
    plan = v1(inputs, normalized)
    affected = set(plan['affected'])
    for index, (part, change, intent) in enumerate(zip(plan['parts'], changes, desired)):
        part['intent'] = intent
        part['target_source'] = change['target_source']
        part['target_scope'] = deepcopy(parts[change['target_source']]['scope'])
        original = parts[part['sources'][0]]
        if (part['target_scope'] != original['scope'] or intent != original['intent']
                or 'turn_id' in original and original['turn_id'] is None):
            affected.add(index)
        part['instruction'] = original.get('instruction') if len(part['sources']) == 1 and intent == original['intent'] else None
    while True:
        expanded = affected | {index for index, part in enumerate(plan['parts']) if set(part['depends_on']) & affected}
        if expanded == affected:
            break
        affected = expanded
    plan['affected'] = sorted(affected)
    if any(plan['parts'][index]['intent'] == 'inspiration' for index in affected):
        raise ValueError('route_correction_inspiration_unchanged_only')
    return plan


v2.rank_examples = rank_examples
v2.keyword_threshold = v1.keyword_threshold
