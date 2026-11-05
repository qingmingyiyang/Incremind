"""在原辅助 Turn 中执行一次搜索，返回尚未入库的冻结证据。"""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict

from pydantic import RootModel

from backend.recognition import RecognitionConflict
from ..kernel.memory_turn import _OUTPUT
from ..kernel.route_source_snapshot import native_source_connection
from ..kernel.receipt_projection import aggregate_cost, kernel_call_groups
from .memory_turn import MemoryTurn
from .policies import get
from .privacy import egress_allowed, is_private_project


_ROUTE_FIELDS = ('provider', 'model', 'base_url', 'revision', 'allow_remote', 'server_model_binding')


def _execution_records(query):
    if query.origin_admission is not None:
        query.origin_admission.validate_execution_owner()
        if query.receipt_records is not query.origin_admission.destination.records:
            raise RecognitionConflict('search_execution_owner_changed')
    return query.receipt_records


def _native_search_snapshot(records, turn_id):
    from core.ai_kernel.product_snapshot import read_product_turn_snapshot
    from ..answer_effective import NativeAnswerSnapshot
    with native_source_connection(records) as connection:
        return NativeAnswerSnapshot(read_product_turn_snapshot(connection, turn_id))


def _search_facts(records, turn_id):
    """只读取原搜索 Turn 的索引、请求和不可变成果，不启动执行。"""
    row = records.read('v2_memory_turn_keys', turn_id)
    if row is None:
        raise RecognitionConflict('search_proof_missing')
    store = _native_search_snapshot(records, turn_id)
    request = store.get_request(turn_id)
    route = store.get_immutable_payload(turn_id, 'memory-model-route-v1')
    output = store.get_immutable_payload(turn_id, _OUTPUT)
    events = tuple(store.events_after(turn_id))
    if (row is None or request is None or route is None or output is None
            or not events or events[-1]['type'] != 'turn.completed'):
        raise RecognitionConflict('search_proof_missing')
    if row.payload['request'] != request:
        raise RecognitionConflict('search_identity_conflicted')
    facts = {'index':{'revision':row.revision, 'payload':deepcopy(row.payload)},
             'request':deepcopy(request), 'route':list(route), 'output':list(output)}
    bundle = store.restore_admission(request)
    sources = [item for item in bundle.items if item.kind == 'material-origin-v1'] if bundle is not None else []
    if sources:
        from .material_origin_admission import read_origin_item
        from .shared_search import read_search_wire_facts
        if len(sources) != 1:
            raise RecognitionConflict('material_origin_binding_changed')
        item = sources[0]
        _, auxiliary = read_origin_item(request, item, actor_user_id=item.payload['actor_user_id'],
            personal=item.payload['execution'])
        if auxiliary is None or 'search_input' not in auxiliary:
            raise RecognitionConflict('shared_search_input_changed')
        facts['search_input'] = deepcopy(auxiliary['search_input'])
        facts['wire'] = read_search_wire_facts(store, request, route, output)
    return facts


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
    if query.origin_admission is not None:
        from .shared_auxiliary import shared_auxiliary_key
        from .shared_search_input import current_search_input
        if (set(binding) != {'turn_id', 'project_id', 'question', 'key', 'facts', 'candidates'}
                or expected.get('search_input', {}).get('producer') != 'question'
                or current_search_input(query, expected['search_input']) != expected['search_input']):
            raise RecognitionConflict('shared_search_input_changed')
        origins = query.origin_admission.restore(request)
        identity['key'] = shared_auxiliary_key(query.origin_admission, binding['key'], origins.auxiliary['materials'], 'search')
    if (request['turn_id'] != binding['turn_id'] or request['desired_outcome'] != 'web.search'
            or request['scope']['project_id'] != binding['project_id']
            or request['input']['text'] != binding['question']
            or expected['index']['payload']['identity'] != identity
            or plan['project_id'] != binding['project_id']):
        raise RecognitionConflict('search_identity_conflicted')
    current = _search_facts(_execution_records(query), binding['turn_id'])
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
    if query.origin_admission is not None:
        from .material_origin_privacy import validate_origin_privacy
        validate_origin_privacy(origins, query.models, project, request['privacy'],
            purpose='search', selection=origins.auxiliary['selection'])
    else:
        validate_frozen_inputs(query.records, query.models, request, purpose='search', query=query)
    # 原 supplement 的输入闭包复验普通材料、画像、导航与历史；移除本证明避免递归。
    original_plan = {key:value for key, value in plan.items() if key != 'search_guard'}
    original_plan['chosen'] = [row for row in plan['chosen'] if row['kind'] != 'search']
    query.validate_ask_plan(original_plan)
    if _search_candidates(plan) != binding['candidates']:
        raise RecognitionConflict('search_evidence_changed')


def search_once(records, models, project, question, *, key, validate_inputs=None,
                source_query=None, parent=None, number=None):
    if not isinstance(question, str) or not question.strip() or not isinstance(key, str) or not key:
        raise RecognitionConflict('search_identity_invalid')
    public = models.public().get('search', {})
    if (is_private_project(records, project) or public.get('enabled') is not True
            or public.get('configured') is not True or not egress_allowed(records, models, project, 'search')):
        return None
    execution = records
    if source_query is not None:
        from .shared_rendered_preparation import _query_current
        _query_current(source_query)
        if records is not source_query.records or models is not source_query.models or project != source_query.origin_admission.lease.project_id:
            raise RecognitionConflict('search_execution_owner_changed')
        execution = _execution_records(source_query)
    policy = get('search')
    identity = {'kind':'web.search', 'project':project, 'key':key, 'purpose':'search'}
    old = (next((row for row in execution.list('v2_memory_turn_keys') if row.payload['identity'] == identity), None)
        if source_query is None else None)
    if old is not None and old.payload['request']['input']['text'] != question:
        raise RecognitionConflict('search_identity_conflicted')
    expected = {name:public.get(name) for name in _ROUTE_FIELDS}
    if old is not None:
        route = MemoryTurn.store_for(execution).get_immutable_payload(old.object_id, 'memory-model-route-v1')
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
        if source_query is not None and holder.get('wire') is True:
            turn.validate_wire_parent()
        if 'output' in holder:
            stored = turn.store.get_immutable_payload(turn.turn_id, _OUTPUT)
            if stored is None or stored[1]['output'] != holder['output']:
                raise RecognitionConflict('search_output_changed')

    def freeze(kind, **values):
        from .turn_requests import freeze_product_turn
        return freeze_product_turn(kind, text=question, **values)

    if source_query is None:
        turn = MemoryTurn(records, models, kind='web.search', project=project, key=key,
            materials=(), validate=validate, freeze_request=freeze, purpose='search')
    else:
        from .shared_search import SharedSearchTurn
        turn = SharedSearchTurn(source_query, parent=parent, number=number, question=question, key=key, validate=validate)
        route = turn.store.get_immutable_payload(turn.turn_id, 'memory-model-route-v1')
        if route is not None:
            expected = {name: route[1].get(name) for name in _ROUTE_FIELDS}
    holder['turn'] = turn
    policy = get('search', version=turn.request['policy_versions']['search'])

    def invoke(control, current):
        current()
        try:
            if source_query is not None:
                from .shared_search import stage_search_wire
                holder['wire'] = True
                stage_search_wire(turn, control, policy)
            results, metadata = models.search(question, parameters=policy.parameters(),
                messages_factory=policy.messages, normalize_results=policy.results,
                validate_current=current, wire_attempt_sink=control)
            current()
            return RootModel[list[dict]](results), metadata
        finally:
            holder['wire'] = False

    output, metadata = turn.generate([], response_model=RootModel[list[dict]], max_tokens=0, invoke=invoke)
    holder['output'] = output.model_dump(mode='json')

    def validate_current():
        turn.validate()

    validate_current()
    groups = kernel_call_groups(execution.database_path.parent, turn_id=turn.turn_id,
                                project=project, remote_only=False, records=execution)
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
        validate_inputs=lambda:query.validate_ask_plan(original_plan),
        **({'source_query': query, 'parent': key, 'number': 0} if query.origin_admission is not None else {}))
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
            'coordinate_space':'search_result_text_v1', 'search_index':index, 'search_turn_id': result['turn_id'], 'score':0,
            **({'namespace': query.origin_admission.personal['namespace']} if query.origin_admission is not None else {})}
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
        'question':question, 'key':key, 'facts':_search_facts(_execution_records(query), result['turn_id']),
        'candidates':_search_candidates(plan)}


def persist_cited(items, plan, result, *, births=None, query=None):
    """仅把原回答实际使用的网页片段交给现有原件写入者。"""
    if not plan.get('search'):
        return {}
    if not callable(plan.get('search_guard')) and not plan.get('search_materials'):
        # 仅有失败请求时保留费用归属，不把失败结果变成证据。
        return {}
    from backend.recognition.material_logical_scope import native_material_origin
    from ..transaction_records import TransactionRecords
    from .material_logical_scope import bind_material_scope_in_uow
    from .material_origin_privacy import validate_origin_privacy
    from .shared_auxiliary import _qualified_commit
    from .shared_search_input import validate_search_publication_sql

    admission = None if query is None else query.origin_admission
    lease = plan.get('origin_lease')
    if (lease is None) != (admission is None):
        raise RecognitionConflict('search_execution_owner_changed')
    if admission is not None and (admission.lease.payload() != lease or items.records is not _execution_records(query)):
        raise RecognitionConflict('search_execution_owner_changed')
    written = {}
    with (_qualified_commit(admission) if admission is not None else nullcontext()) as qualification:
        if qualification is None:
            plan['search_guard']()
        else:
            binding = getattr(plan.get('search_guard'), 'frozen_binding', None)
            if not isinstance(binding, dict):
                raise RecognitionConflict('search_proof_missing')
            validate_search_binding(query, binding, plan)
        # 原件归操作人，来源资格先取得并保留至个人事务提交。
        with items.records.begin() as transaction:
            actor_reader = TransactionRecords(transaction)
            enlisted = items.with_records(actor_reader)
            checked = set()
            for source in result['sources']:
                candidate = plan.get('search_materials', {}).get(source['number'])
                if candidate is None:
                    continue
                if (source['number'] in written or source['excerpt'] != candidate['excerpt']
                        or candidate['scope'].project_id != plan['project_id']):
                    raise RecognitionConflict('search_evidence_changed')
                child = candidate.get('search_turn_id', plan['search'].get('turn_id'))
                request, origin = native_material_origin(actor_reader, child)
                if (origin is None) != (qualification is None):
                    raise RecognitionConflict('material_origin_binding_missing')
                if qualification is not None and child not in checked:
                    origins = admission.restore(request)
                    validate_search_publication_sql(origins, request, qualification, actor_reader)
                    validate_origin_privacy(origins, query.models, plan['project_id'], request['privacy'],
                        purpose='search', selection=origins.auxiliary['selection'], reader=actor_reader)
                    checked.add(child)
                item = enlisted.create(plan['project_id'], 'text', candidate['title'], source['excerpt'],
                    url=candidate['href'], origin='search', search_turn_id=child)
                if origin is not None:
                    bind_material_scope_in_uow(transaction, items.records, material_type='workspace_item',
                        identity=item['id'], project_id=plan['project_id'],
                        namespace_id=origin['payload']['execution']['namespace_id'], source_request=request)
                written[source['number']] = item['id']
                if births is not None:
                    births[source['number']] = {key: item.get(key) for key in (
                        'id', 'project_id', 'revision', 'input_kind', 'created_at', 'source_text', 'url', 'origin', 'search_turn_id')}
            if qualification is not None:
                qualification._actor_reader(actor_reader)
            transaction.commit()
    plan['search']['used'] = len(written)
    return written


def persist_answer_search(items, plan, result, chosen, *, query=None):
    births = {}
    originals = persist_cited(items, plan, result, births=births, query=query)
    mapped = tuple({**candidate, 'entry': {**candidate['entry'], 'item_id': originals[index],
        'item_revision': births[index]['revision']}} if index in originals else candidate
        for index, candidate in enumerate(chosen, 1))
    for entry in (result.get('context') or {}).get('entries', []):
        if entry.get('url'):
            entry['id'] = next((identity for number, identity in originals.items()
                if mapped[number - 1]['url'] == entry['url']), entry['id'])
    return mapped, births


def validate_search_decision(records, parent, number, question, *, query=None):
    """搜索只消费同父轮已完成的真实模型决策，不按项目猜关联。"""
    if query is not None and query.origin_admission is not None:
        from .shared_search_input import decision_facts
        if records is not _execution_records(query):
            raise RecognitionConflict('search_execution_owner_changed')
        return decision_facts(query, parent, number, question)[1]
    store = _native_search_snapshot(records, parent)
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
        parent = validate_search_decision(_execution_records(query), binding['parent_turn_id'], branch['round'], branch['question'], query=query)
        from .agentic_rag import validate_material_proof
        store = _native_search_snapshot(_execution_records(query), binding['parent_turn_id'])
        prepared = store.get_immutable_payload(binding['parent_turn_id'],
            'answer-gap-input-rag-decision-' + str(branch['round']) + '-v1')
        proof = prepared[1]['material_proof']['selection']
        if branch['derived_privacy']['material_refs'] != proof['privacy']['material_refs']:
            raise RecognitionConflict('rag_search_source_closure_changed')
        validate_material_proof(query, plan['project_id'], plan['target'], proof, request=parent)
        if query.origin_admission is not None:
            allowed, privacy, _ = query.freeze_materials(query.models, plan['project_id'], proof['origin_materials'],
                purpose='search', native_request=parent)
            if allowed != proof['origin_materials'] or privacy != branch['derived_privacy'] or not privacy['allow_remote']:
                raise RecognitionConflict('rag_search_source_closure_changed')
        else:
            validate_frozen_inputs(query.records, query.models, {**parent, 'privacy': branch['derived_privacy']},
                                   purpose='search', query=query)
        expected_key = binding['parent_turn_id'] + ':rag:' + str(branch['round'])
        if parent['scope']['project_id'] != plan['project_id'] or branch['key'] != expected_key:
            raise RecognitionConflict('search_identity_conflicted')
        current = _search_facts(_execution_records(query), branch['turn_id'])
        if current != branch['facts']:
            raise RecognitionConflict('search_output_changed')
        request = current['request']
        expected_identity = {'kind': 'web.search', 'project': plan['project_id'], 'key': expected_key, 'purpose': 'search'}
        if query.origin_admission is not None:
            from .shared_auxiliary import shared_auxiliary_key
            from .material_origin_privacy import validate_origin_privacy
            origins = query.origin_admission.restore(request)
            auxiliary = origins.auxiliary
            if auxiliary is None or auxiliary.get('search_input') != current.get('search_input'):
                raise RecognitionConflict('shared_search_input_changed')
            expected_identity['key'] = shared_auxiliary_key(query.origin_admission, expected_key, auxiliary['materials'], 'search')
            validate_origin_privacy(origins, query.models, plan['project_id'], request['privacy'],
                purpose='search', selection=auxiliary['selection'])
        if (request['input']['text'] != branch['question'] or request['scope']['project_id'] != plan['project_id']
                or current['index']['payload']['identity'] != expected_identity
                or {name: public.get(name) for name in _ROUTE_FIELDS}
                    != {name: current['route'][1].get(name) for name in _ROUTE_FIELDS}):
            raise RecognitionConflict('search_authority_changed')
        if query.origin_admission is None:
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
    store = _native_search_snapshot(_execution_records(query), parent)
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
    validate_search_decision(_execution_records(query), parent, number, question, query=query)
    validate_agentic_search_tools(query, plan['project_id'], parent=parent, number=number)
    derived_privacy = validate_derived_query(query, plan, 'search')
    key = parent + ':rag:' + str(number)
    store = MemoryTurn.store_for(_execution_records(query))
    store.get_or_create_immutable_payload(parent, 'answer-rag-search-attempt-' + str(number),
        {'parent_turn_id': parent, 'round': number, 'key': key, 'question': question})
    original = {**plan, 'chosen': list(plan['chosen'])}
    def validate():
        _checkpoint()
        query.validate_ask_plan(original)
        validate_derived_query(query, original, 'search')
    try:
        result = await settled_call(search_once, query.records, query.models, plan['project_id'], question,
            key=key, validate_inputs=validate,
            **({'source_query': query, 'parent': parent, 'number': number} if query.origin_admission is not None else {}))
    except BaseException:
        identity = {'kind': 'web.search', 'project': plan['project_id'], 'key': key, 'purpose': 'search'}
        if query.origin_admission is not None:
            from .shared_search import find_shared_search_index
            child = find_shared_search_index(query, key)
        else:
            child = next((row for row in _execution_records(query).list('v2_memory_turn_keys') if row.payload['identity'] == identity), None)
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
                               'facts': _search_facts(_execution_records(query), result['turn_id'])})
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
            'search_turn_id': result['turn_id'], 'score': 0,
            **({'namespace': query.origin_admission.personal['namespace']} if query.origin_admission is not None else {})}
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


def validate_search_fragments_shape(values, *, project):
    from .shared_search_input import validate_search_input_value
    fields = {'turn_id', 'index', 'project_id', 'namespace', 'source_namespace', 'url', 'title',
              'text', 'windows', 'input_ref', 'output_ref', 'search_input'}
    if not isinstance(values, list):
        raise RecognitionConflict('shared_search_fragment_changed')
    for value in values:
        if (not isinstance(value, dict) or set(value) != fields or value['project_id'] != project
                or type(value['index']) is not int or value['index'] < 0
                or any(not isinstance(value[key], str) or not value[key]
                    for key in fields - {'index', 'windows', 'search_input'})
                or not isinstance(value['windows'], list) or not value['windows']
                or any(not isinstance(window, dict) or set(window) != {'start', 'end', 'text'}
                    or type(window['start']) is not int or type(window['end']) is not int
                    or not 0 <= window['start'] < window['end'] <= len(value['text'])
                    or value['text'][window['start']:window['end']] != window['text']
                    for window in value['windows'])):
            raise RecognitionConflict('shared_search_fragment_changed')
        validate_search_input_value(value['search_input'])


def shared_search_fragments(query, plan, *, binding=None):
    from backend.recognition import WorkScope
    from core.search_and_recall.evidence_windows import EvidenceWindow
    from .shared_rendered_preparation import _query_current
    _query_current(query)
    binding = binding if binding is not None else getattr(plan.get('search_guard'), 'frozen_binding', None)
    candidates = [row for row in plan['chosen'] if row['kind'] == 'search']
    if binding is None:
        if candidates:
            raise RecognitionConflict('search_proof_missing')
        return []
    if binding.get('version') not in {None, 2}:
        raise RecognitionConflict('shared_search_input_changed')
    frozen = [{**deepcopy(row), 'scope': WorkScope(**row['scope']),
        'windows': tuple(EvidenceWindow(**window) for window in row['windows'])} for row in binding['candidates']]
    # 决策可能再次阅读先前已观察的窗口，分支事实和每个实际输入窗口分别核验。
    probe = {key: value for key, value in plan.items() if key not in {'search_guard', 'search_materials', 'state'}}
    probe['chosen'] = [row for row in plan['chosen'] if row['kind'] != 'search'] + frozen
    validate_search_binding(query, binding, probe)
    turns = [branch['turn_id'] for branch in binding['branches']] if binding.get('version') == 2 else [binding['turn_id']]
    owners = {identity: _search_facts(_execution_records(query), identity) for identity in turns}
    return _fragments_from_facts(query, plan, owners)


def _fragments_from_facts(query, plan, owners):
    candidates = [row for row in plan['chosen'] if row['kind'] == 'search']
    result = []
    for candidate in candidates:
        facts = owners.get(candidate.get('search_turn_id'))
        index = candidate.get('search_index')
        rows = facts['output'][1]['output'] if facts is not None else []
        if type(index) is not int or not 0 <= index < len(rows) or not isinstance(rows[index], dict):
            raise RecognitionConflict('shared_search_fragment_changed')
        row, entry = rows[index], candidate['entry']
        if (set(row) != {'url', 'title', 'text'} or any(not isinstance(value, str) or not value for value in row.values())
                or candidate.get('namespace') != query.origin_admission.personal['namespace']
                or candidate['scope'].project_id != plan['project_id']
                or candidate.get('coordinate_space') != 'search_result_text_v1'
                or candidate['id'] != facts['request']['turn_id'] + '-' + str(index)
                or entry != {'id': candidate['id'], 'kind': 'search', 'revision': 1, 'source_id': candidate['id'],
                    'content': row['text'], 'title': row['title'], 'url': row['url']}
                or candidate['title'] != row['title'] or candidate['href'] != row['url']
                or not candidate['windows'] or candidate['excerpt'] != '\n…\n'.join(window.text for window in candidate['windows'])):
            raise RecognitionConflict('shared_search_fragment_changed')
        value = {'turn_id': facts['request']['turn_id'], 'index': index, 'project_id': plan['project_id'],
            'namespace': query.origin_admission.personal['namespace'], 'source_namespace': query.source_namespace,
            **row, 'windows': [asdict(window) for window in candidate['windows']],
            'input_ref': facts['wire']['input'][0], 'output_ref': facts['output'][0],
            'search_input': deepcopy(facts['search_input'])}
        result.append(value)
    validate_search_fragments_shape(result, project=plan['project_id'])
    return result


def historical_search_fragments(query, plan, *, binding):
    from .material_origin_admission import read_origin_item
    from .shared_rendered_preparation import _query_current
    from .shared_search_input import _read_decision, _read_question
    _query_current(query)
    if binding is None:
        if any(row['kind'] == 'search' for row in plan['chosen']):
            raise RecognitionConflict('search_proof_missing')
        return []
    if binding.get('version') == 2:
        if set(binding) != {'version', 'parent_turn_id', 'project_id', 'branches', 'candidates'}:
            raise RecognitionConflict('search_identity_conflicted')
        branches = binding['branches']
    elif set(binding) == {'turn_id', 'project_id', 'question', 'key', 'facts', 'candidates'}:
        branches = [binding]
    else:
        raise RecognitionConflict('search_identity_conflicted')
    if binding['project_id'] != plan['project_id'] or not branches:
        raise RecognitionConflict('search_identity_conflicted')
    owners = {}
    for branch in branches:
        facts = _search_facts(_execution_records(query), branch['turn_id'])
        if facts != branch['facts']:
            raise RecognitionConflict('search_output_changed')
        snapshot = _native_search_snapshot(_execution_records(query), branch['turn_id'])
        request = facts['request']
        bundle = snapshot.restore_admission(request)
        origins = [item for item in bundle.items if item.kind == 'material-origin-v1'] if bundle is not None else []
        if len(origins) != 1:
            raise RecognitionConflict('material_origin_binding_missing')
        original = origins[0]
        execution = original.payload['execution']
        lease, auxiliary = read_origin_item(request, original,
            actor_user_id=original.payload['actor_user_id'], personal=execution)
        # 历史只认原完成事实；当前执行账号与当前共享空间分别核对逻辑归属。
        if (lease.namespace != query.source_namespace or lease.project_id != plan['project_id']
                or execution['owner_user_id'] != query.origin_admission.destination.owner_user_id
                or execution['namespace_id'] != query.origin_admission.destination.namespace
                or auxiliary is None or auxiliary.get('search_input') != facts['search_input']):
            raise RecognitionConflict('shared_history_material_scope_changed')
        value = facts['search_input']
        parent_snapshot = _native_search_snapshot(_execution_records(query), value['parent_turn_id'])
        if value['producer'] == 'question':
            actual, parent = _read_question(parent_snapshot, value['parent_turn_id'], value['question'], value['key'])
            if binding.get('version') == 2:
                raise RecognitionConflict('shared_search_input_changed')
        else:
            actual, parent, _ = _read_decision(parent_snapshot, value['parent_turn_id'], value['round'], value['question'])
            if (binding.get('version') != 2 or binding['parent_turn_id'] != parent['turn_id']
                    or branch.get('round') != value['round']):
                raise RecognitionConflict('shared_search_input_changed')
        parent_bundle = parent_snapshot.restore_admission(parent)
        parent_origins = [item for item in parent_bundle.items if item.kind == 'material-origin-v1'] if parent_bundle is not None else []
        if len(parent_origins) != 1:
            raise RecognitionConflict('material_origin_binding_missing')
        parent_origin = parent_origins[0]
        parent_lease, _ = read_origin_item(parent, parent_origin,
            actor_user_id=parent_origin.payload['actor_user_id'], personal=parent_origin.payload['execution'])
        if (parent_lease != lease or parent_origin.payload['execution'] != execution
                or actual != value or branch['question'] != value['question'] or branch['key'] != value['key']):
            raise RecognitionConflict('shared_search_input_changed')
        owners[branch['turn_id']] = facts
    return _fragments_from_facts(query, plan, owners)
