"""在原辅助 Turn 中执行一次搜索，返回尚未入库的冻结证据。"""
from copy import deepcopy
from dataclasses import asdict

from pydantic import RootModel

from backend.recognition import RecognitionConflict
from ..kernel.memory_turn import _OUTPUT
from ..kernel.receipt_projection import aggregate_cost, kernel_call_groups
from .memory_turn import MemoryTurn
from .policies import get
from .privacy import egress_allowed, is_private_project


_ROUTE_FIELDS = ('provider', 'model', 'base_url', 'revision', 'allow_remote')


def _search_facts(records, turn_id):
    """只读取原搜索 Turn 的索引、请求和不可变成果，不启动执行。"""
    row = records.read('v2_memory_turn_keys', turn_id)
    store = MemoryTurn.store_for(records)
    request = store.get_request(turn_id)
    route = store.get_immutable_payload(turn_id, 'memory-model-route-v1')
    output = store.get_immutable_payload(turn_id, _OUTPUT)
    events = tuple(store.events_after(turn_id))
    if (row is None or request is None or route is None or output is None
            or not events or events[-1]['type'] != 'turn.completed'):
        raise RecognitionConflict('search_proof_missing')
    if row.payload['request'] != request:
        raise RecognitionConflict('search_identity_conflicted')
    return {'index':{'revision':row.revision, 'payload':deepcopy(row.payload)},
            'request':deepcopy(request), 'route':list(route), 'output':list(output)}


def _search_candidates(plan):
    candidates = (list(plan.get('search_materials', {}).values()) if plan.get('state') == 'completed'
                  else [row for row in plan['chosen'] if row['kind'] == 'search'])
    # 只转换原候选中的两个值对象，完整保留拟合后的窗口与原字段。
    return [{**deepcopy(candidate),
             'scope':asdict(candidate['scope']),
             'windows':[asdict(window) for window in candidate['windows']]}
            for candidate in candidates]


def validate_search_binding(query, binding, plan):
    """在首答依赖中复用原搜索证明，复验不创建 Turn、不重做搜索。"""
    if binding.get('version') == 2:
        return _validate_agentic_search(query, binding, plan)
    from .turn_requests import validate_frozen_inputs

    expected = binding['facts']
    request = expected['request']
    identity = {'kind':'web.search', 'project':binding['project_id'],
                'key':binding['key'], 'purpose':'search'}
    if (request['turn_id'] != binding['turn_id'] or request['desired_outcome'] != 'web.search'
            or request['scope']['project_id'] != binding['project_id']
            or request['input']['text'] != binding['question']
            or expected['index']['payload']['identity'] != identity
            or plan['project_id'] != binding['project_id']):
        raise RecognitionConflict('search_identity_conflicted')
    current = _search_facts(query.records, binding['turn_id'])
    if current['index'] != expected['index'] or current['request'] != request:
        raise RecognitionConflict('search_identity_conflicted')
    if current['route'] != expected['route']:
        raise RecognitionConflict('search_authority_changed')
    if current['output'] != expected['output']:
        raise RecognitionConflict('search_output_changed')
    public = query.models.public().get('search', {})
    project = binding['project_id']
    if (is_private_project(query.records, project) or public.get('enabled') is not True
            or public.get('configured') is not True
            or not egress_allowed(query.records, query.models, project, 'search')
            or {name:public.get(name) for name in _ROUTE_FIELDS}
                != {name:expected['route'][1].get(name) for name in _ROUTE_FIELDS}):
        raise RecognitionConflict('search_authority_changed')
    validate_frozen_inputs(query.records, query.models, request, purpose='search', query=query)
    # 原 supplement 的输入闭包复验普通材料、画像、导航与历史；移除本证明避免递归。
    original_plan = {key:value for key, value in plan.items() if key != 'search_guard'}
    original_plan['chosen'] = [row for row in plan['chosen'] if row['kind'] != 'search']
    query.validate_ask_plan(original_plan)
    if _search_candidates(plan) != binding['candidates']:
        raise RecognitionConflict('search_evidence_changed')


def search_once(records, models, project, question, *, key, validate_inputs=None):
    if not isinstance(question, str) or not question.strip() or not isinstance(key, str) or not key:
        raise RecognitionConflict('search_identity_invalid')
    public = models.public().get('search', {})
    if (is_private_project(records, project) or public.get('enabled') is not True
            or public.get('configured') is not True or not egress_allowed(records, models, project, 'search')):
        return None
    policy = get('search')
    identity = {'kind':'web.search', 'project':project, 'key':key, 'purpose':'search'}
    old = next((row for row in records.list('v2_memory_turn_keys') if row.payload['identity'] == identity), None)
    if old is not None and old.payload['request']['input']['text'] != question:
        raise RecognitionConflict('search_identity_conflicted')
    expected = {name:public.get(name) for name in _ROUTE_FIELDS}
    if old is not None:
        route = MemoryTurn.store_for(records).get_immutable_payload(old.object_id, 'memory-model-route-v1')
        if route is not None:
            expected = {name:route[1].get(name) for name in _ROUTE_FIELDS}
    holder = {}

    def validate():
        if validate_inputs is not None:
            validate_inputs()
        current = models.public().get('search', {})
        if (is_private_project(records, project) or current.get('enabled') is not True
                or current.get('configured') is not True or not egress_allowed(records, models, project, 'search')
                or {name:current.get(name) for name in _ROUTE_FIELDS} != expected):
            raise RecognitionConflict('search_authority_changed')
        turn = holder.get('turn')
        if turn is not None and turn.request['input']['text'] != question:
            raise RecognitionConflict('search_identity_conflicted')
        if 'output' in holder:
            stored = turn.store.get_immutable_payload(turn.turn_id, _OUTPUT)
            if stored is None or stored[1]['output'] != holder['output']:
                raise RecognitionConflict('search_output_changed')

    def freeze(kind, **values):
        from .turn_requests import freeze_product_turn
        return freeze_product_turn(kind, text=question, **values)

    turn = MemoryTurn(records, models, kind='web.search', project=project, key=key,
        materials=(), validate=validate, freeze_request=freeze, purpose='search')
    holder['turn'] = turn

    def invoke(control, current):
        current()
        results, metadata = models.search(question, parameters=policy.parameters(),
            messages_factory=policy.messages, normalize_results=policy.results,
            validate_current=current, wire_attempt_sink=control)
        current()
        return RootModel[list[dict]](results), metadata

    output, metadata = turn.generate([], response_model=RootModel[list[dict]], max_tokens=0, invoke=invoke)
    holder['output'] = output.model_dump(mode='json')

    def validate_current():
        turn.validate()

    validate_current()
    groups = kernel_call_groups(records.database_path.parent, turn_id=turn.turn_id,
                                project=project, remote_only=False, records=records)
    calls = [call for group in groups if group['turn_id'] == turn.turn_id and group['kind'] == 'web.search'
             for call in group['calls']]
    return {'turn_id':turn.turn_id, 'results':deepcopy(output.root),
            'usage':dict(metadata.get('usage') or {}), 'model_cost':aggregate_cost(calls),
            'validate_current':validate_current}


async def supplement(query, plan, question, wordings, *, key):
    """将原辅助 Turn 的结果按原 ASK 剩余预算追加，不改变普通材料。"""
    from starlette.concurrency import run_in_threadpool
    from core.search_and_recall.evidence_windows import EvidenceWindow, query_terms
    from .ladder import _DETAIL
    from .budget import evidence_tokens, input_tokens, trim_candidate

    selected = plan['policy_versions'].get('search')
    if selected is None:
        return
    policy = get('search', version=selected)
    decision = policy.sufficient_input(plan['chosen'], wordings, query_terms,
        detail=any(word in question or word in plan['question'] for word in _DETAIL))
    triggers = [*plan['chosen'], *plan.get('profile', {}).get('items', [])]
    if not policy(question, decision, candidates=triggers):
        return
    original_plan = {**plan, 'chosen':list(plan['chosen'])}
    result = await run_in_threadpool(search_once, query.records, query.models, plan['project_id'], question, key=key,
                                   validate_inputs=lambda:query.validate_ask_plan(original_plan))
    if result is None:
        return
    candidates = []
    for index, row in enumerate(result['results']):
        identity = result['turn_id'] + '-' + str(index)
        candidate = {'kind':'search', 'id':identity, 'layer':'L0', 'scope':plan['scope'],
            'entry':{'id':identity, 'kind':'search', 'revision':1, 'source_id':identity,
                     'content':row['text'], 'title':row['title'], 'url':row['url']},
            'title':row['title'], 'href':row['url'], 'excerpt':row['text'], 'match_in':'content',
            'windows':(EvidenceWindow(0, len(row['text']), row['text']),),
            'coordinate_space':'search_result_text_v1', 'search_index':index, 'score':0}
        fitted = policy.fit_evidence(candidate, plan['chosen'], question=plan['question'], history=plan.get('history', ''),
            budget=plan['budget'], overhead=plan['prompt_overhead'], trim=trim_candidate,
            estimate=input_tokens, evidence_tokens=evidence_tokens)
        if fitted is None:
            continue
        plan['chosen'].append(fitted)
        candidates.append(deepcopy(fitted))

    def validate():
        result['validate_current']()
        actual = (list(plan.get('search_materials', {}).values()) if plan['state'] == 'completed'
                  else [row for row in plan['chosen'] if row['kind'] == 'search'])
        if actual != candidates:
            raise RecognitionConflict('search_evidence_changed')

    plan['search_guard'] = validate
    plan['search'] = {'turn_id':result['turn_id'], 'purpose':'搜索', 'policy_version':selected,
                     'selected':len(candidates), 'model_usage':result['usage'], 'model_cost':result['model_cost']}
    validate()
    # 依赖首答需要跨执行复验同一证明；绑定只保存已完成原 Turn 的现有事实。
    validate.frozen_binding = {'turn_id':result['turn_id'], 'project_id':plan['project_id'],
        'question':question, 'key':key, 'facts':_search_facts(query.records, result['turn_id']),
        'candidates':_search_candidates(plan)}


def persist_cited(items, plan, result):
    """仅把原回答实际使用的网页片段交给现有原件写入者。"""
    if not plan.get('search'):
        return {}
    if not callable(plan.get('search_guard')) and not plan.get('search_materials'):
        # 仅有失败请求时保留费用归属，不把失败结果变成证据。
        return {}
    plan['search_guard']()
    written = {}
    from ..transaction_records import TransactionRecords
    with items.records.begin() as transaction:
        enlisted = items.with_records(TransactionRecords(transaction))
        for source in result['sources']:
            candidate = plan.get('search_materials', {}).get(source['number'])
            if candidate is None:
                continue
            item = enlisted.create(plan['project_id'], 'text', candidate['title'], source['excerpt'],
                url=candidate['href'], origin='search', search_turn_id=candidate.get('search_turn_id', plan['search'].get('turn_id')))
            written[source['number']] = item['id']
        transaction.commit()
    plan['search']['used'] = len(written)
    return written


def validate_search_decision(records, parent, number, question):
    """搜索只消费同父轮已完成的真实模型决策，不按项目猜关联。"""
    store = MemoryTurn.store_for(records)
    invocation = 'rag-decision-' + str(number)
    request = store.get_request(parent)
    version = request.get('policy_versions', {}).get('retrieve') if request is not None else None
    try:
        agentic = (isinstance(version, str) and bool(version)
                   and getattr(get('retrieve', version=version), 'agentic', False) is True)
    except ValueError:
        agentic = False
    result = store.get_immutable_payload(parent, 'answer-model-result-' + invocation)
    binding = store.get_immutable_payload(parent, 'answer-gap-binding-' + invocation + '-v1')
    prepared = store.get_immutable_payload(parent, 'answer-gap-input-' + invocation + '-v1')
    if (request is None or request['desired_outcome'] != 'project.answer'
            or not agentic
            or result is None or binding is None or prepared is None
            or binding[1]['input_ref'] != prepared[0]
            or result[1]['output'].get('action') != 'search'
            or result[1]['output'].get('source') != 'web'
            or result[1]['output'].get('query') != question):
        raise RecognitionConflict('rag_search_decision_changed')
    events = [event for event in store.events_after(parent)
              if event.get('correlation', {}).get('model_request_id') == binding[1]['model_request_id']]
    if not any(event['type'] == 'model.completed' for event in events):
        raise RecognitionConflict('rag_search_decision_unavailable')
    return request


def _validate_agentic_search(query, binding, plan):
    from .turn_requests import validate_frozen_inputs
    if (set(binding) != {'version', 'parent_turn_id', 'project_id', 'branches', 'candidates'}
            or binding['project_id'] != plan['project_id'] or not binding['branches']):
        raise RecognitionConflict('search_identity_conflicted')
    public = query.models.public().get('search', {})
    if (is_private_project(query.records, plan['project_id']) or public.get('enabled') is not True
            or public.get('configured') is not True
            or not egress_allowed(query.records, query.models, plan['project_id'], 'search')):
        raise RecognitionConflict('search_authority_changed')
    owners = {}
    for branch in binding['branches']:
        parent = validate_search_decision(query.records, binding['parent_turn_id'], branch['round'], branch['question'])
        from .agentic_rag import validate_material_proof
        store = MemoryTurn.store_for(query.records)
        prepared = store.get_immutable_payload(binding['parent_turn_id'],
            'answer-gap-input-rag-decision-' + str(branch['round']) + '-v1')
        proof = prepared[1]['material_proof']['selection']
        if branch['derived_privacy']['material_refs'] != proof['privacy']['material_refs']:
            raise RecognitionConflict('rag_search_source_closure_changed')
        validate_material_proof(query, plan['project_id'], plan['target'], proof, request=parent)
        validate_frozen_inputs(query.records, query.models, {**parent, 'privacy': branch['derived_privacy']},
                               purpose='search', query=query)
        expected_key = binding['parent_turn_id'] + ':rag:' + str(branch['round'])
        if parent['scope']['project_id'] != plan['project_id'] or branch['key'] != expected_key:
            raise RecognitionConflict('search_identity_conflicted')
        current = _search_facts(query.records, branch['turn_id'])
        if current != branch['facts']:
            raise RecognitionConflict('search_output_changed')
        request = current['request']
        if (request['input']['text'] != branch['question'] or request['scope']['project_id'] != plan['project_id']
                or current['index']['payload']['identity'] != {'kind': 'web.search', 'project': plan['project_id'],
                    'key': expected_key, 'purpose': 'search'}
                or {name: public.get(name) for name in _ROUTE_FIELDS}
                    != {name: current['route'][1].get(name) for name in _ROUTE_FIELDS}):
            raise RecognitionConflict('search_authority_changed')
        validate_frozen_inputs(query.records, query.models, request, purpose='search', query=query)
        owners[branch['turn_id']] = current['output'][1]['output']
    original = {key: value for key, value in plan.items() if key != 'search_guard'}
    original['chosen'] = [row for row in plan['chosen'] if row['kind'] != 'search']
    query.validate_ask_plan(original)
    actual = _search_candidates(plan)
    if actual != binding['candidates']:
        raise RecognitionConflict('search_evidence_changed')
    for candidate in actual:
        results = owners.get(candidate.get('search_turn_id'), ())
        index = candidate.get('search_index')
        if (type(index) is not int or not 0 <= index < len(results)
                or candidate['entry']['content'] != results[index]['text']
                or candidate['href'] != results[index]['url']):
            raise RecognitionConflict('search_evidence_changed')


def validate_agentic_search_tools(query, project, *, parent, number):
    """先复验本轮工具快照，再区分确实不可用与权限已经变化。"""
    from .agentic_rag import tool_availability
    store = MemoryTurn.store_for(query.records)
    prepared = store.get_immutable_payload(parent, 'answer-gap-input-rag-decision-' + str(number) + '-v1')
    current = tool_availability(query, project)['web']
    if prepared is None or prepared[1]['material_proof']['selection']['tools']['web'] != current:
        raise RecognitionConflict('search_authority_changed')
    return current


async def agentic_search(query, plan, question, *, parent, number):
    """同一问答可有多个受管搜索，始终保留有序的原结果证明。"""
    from core.search_and_recall.evidence_windows import EvidenceWindow
    from .agentic_rag import settled_call, candidate_id, validate_derived_query, _checkpoint
    from .budget import trim_candidate, input_tokens, evidence_tokens
    validate_search_decision(query.records, parent, number, question)
    validate_agentic_search_tools(query, plan['project_id'], parent=parent, number=number)
    derived_privacy = validate_derived_query(query, plan, 'search')
    key = parent + ':rag:' + str(number)
    store = MemoryTurn.store_for(query.records)
    store.get_or_create_immutable_payload(parent, 'answer-rag-search-attempt-' + str(number),
        {'parent_turn_id': parent, 'round': number, 'key': key, 'question': question})
    original = {**plan, 'chosen': list(plan['chosen'])}
    def validate():
        _checkpoint()
        query.validate_ask_plan(original)
        validate_derived_query(query, original, 'search')
    try:
        result = await settled_call(search_once, query.records, query.models, plan['project_id'], question,
                                    key=key, validate_inputs=validate)
    except BaseException:
        identity = {'kind': 'web.search', 'project': plan['project_id'], 'key': key, 'purpose': 'search'}
        child = next((row for row in query.records.list('v2_memory_turn_keys') if row.payload['identity'] == identity), None)
        if child is not None:
            previous = plan.get('search', {})
            plan['search'] = {**previous, 'version': 2, 'parent_turn_id': parent, 'purpose': '搜索',
                'policy_version': plan['policy_versions']['search'], 'selected': previous.get('selected', 0),
                'branches': [*previous.get('branches', []), {'turn_id': child.object_id,
                    'round': number, 'question': question, 'key': key, 'status': 'failed'}],
                'model_usage': {**previous.get('model_usage', {}), 'observed_only': True}, 'model_cost': None}
        raise
    if result is None:
        return []
    policy = get('search', version=plan['policy_versions']['search'])
    previous = getattr(plan.get('search_guard'), 'frozen_binding', None)
    binding = deepcopy(previous) if previous else {'version': 2, 'parent_turn_id': parent,
        'project_id': plan['project_id'], 'branches': [], 'candidates': []}
    binding['branches'].append({'turn_id': result['turn_id'], 'round': number, 'question': question,
                               'key': key, 'derived_privacy': derived_privacy,
                               'facts': _search_facts(query.records, result['turn_id'])})
    added = []
    for index, row in enumerate(result['results']):
        if any(candidate['kind'] == 'search' and candidate['href'] == row['url']
               and candidate['entry']['content'] == row['text'] for candidate in plan['chosen']):
            continue
        identity = result['turn_id'] + '-' + str(index)
        candidate = {'kind': 'search', 'id': identity, 'layer': 'L0', 'scope': plan['scope'],
            'entry': {'id': identity, 'kind': 'search', 'revision': 1, 'source_id': identity,
                      'content': row['text'], 'title': row['title'], 'url': row['url']},
            'title': row['title'], 'href': row['url'], 'excerpt': row['text'], 'match_in': 'content',
            'windows': (EvidenceWindow(0, len(row['text']), row['text']),),
            'coordinate_space': 'search_result_text_v1', 'search_index': index,
            'search_turn_id': result['turn_id'], 'score': 0}
        fitted = policy.fit_evidence(candidate, plan['chosen'], question=plan['question'], history=plan.get('history', ''),
            budget=plan['budget'], overhead=plan['prompt_overhead'], trim=trim_candidate,
            estimate=input_tokens, evidence_tokens=evidence_tokens)
        if fitted is not None:
            plan['chosen'].append(fitted)
            added.append({'id': candidate_id(fitted), 'title': fitted['title'], 'layer': 'L0'})
    previous_search = plan.get('search', {})
    previous_usage = previous_search.get('model_usage', {})
    rebind_agentic_search(query, plan, binding)
    from .followup import sum_usage
    plan['search'] = {'version': 2, 'parent_turn_id': parent, 'purpose': '搜索',
        'policy_version': plan['policy_versions']['search'], 'selected': len(binding['candidates']),
        'branches': [*previous_search.get('branches', []),
            {'turn_id': result['turn_id'], 'round': number, 'question': question, 'key': key, 'status': 'completed'}],
        'model_usage': sum_usage(previous_usage, result['usage']), 'model_cost': None}
    plan['search_guard']()
    return added


def rebind_agentic_search(query, plan, binding):
    binding = deepcopy(binding)
    binding['candidates'] = _search_candidates(plan)
    def guard():
        _validate_agentic_search(query, binding, plan)
    guard.frozen_binding = binding
    plan['search_guard'] = guard
