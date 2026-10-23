"""沿原问答 Turn 调查，领域 owner 继续负责资料、权限和真实回执。"""
import asyncio
from copy import deepcopy
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from ..kernel.answer_turns import ACTIVE_ANSWER, RESULT_KIND, gap_answer_input, generate_answer, answer_observation, abandon_answer_model
from ..kernel.ai_execution_control import execution_checkpoint
from ..kernel.answer_continuations import encode, decode
from ..source_egress import SourceEgressService
from .policies import get
from .privacy import freeze_turn_materials, egress_allowed
from .followup import sum_usage


class RagDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['search', 'read', 'answer', 'clarify']
    source: Literal['memory', 'web', 'skill']
    query: StrictStr = Field(max_length=200)
    depth: Literal['summary', 'body', 'original']
    ids: list[StrictStr] = Field(max_length=8)
    questions: list[StrictStr] = Field(max_length=3)


class RagInputBudgetExhausted(ValueError):
    """完整输入不合容量时有界停止，不伪装来源被撤回。"""


def tool_availability(query, project):
    search = query.models.public().get('search', {})
    return {'memory': {'available': True}, 'skill': {'available': True},
            'web': {'available': bool(search.get('enabled') is True and search.get('configured') is True
                and egress_allowed(query.records, query.models, project, 'search')),
                'revision': search.get('revision', 0)}}


def _policy(plan):
    return get('retrieve', version=plan['policy_versions']['retrieve'])


def _checkpoint():
    active = ACTIVE_ANSWER.get()
    if active is None:
        raise RecognitionConflict('rag_turn_unavailable')
    execution_checkpoint(active[0])


async def settled_call(function, *args, **kwargs):
    """取消不能让仍在执行的原线程失去租约和资源归属。"""
    pending = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
        raise


def candidate_id(candidate):
    return ':'.join((candidate['scope'].project_id, candidate['kind'], candidate['layer'], candidate['id']))


def _candidates(plan):
    if getattr(_policy(plan), 'explicit_answer_evidence', False):
        # 同源多次阅读保留全部真实窗口，不能让最后一个片段覆盖先前行动或对象。
        return _merge_observed([*plan['_rag']['observed'], *plan['chosen']])
    rows = {}
    for candidate in [*plan['_rag']['observed'], *plan['chosen']]:
        rows[candidate_id(candidate)] = candidate
    return list(rows.values())


def _merge_observed(rows):
    from .policies.ladder import project_sources_v14, source_identity_v14
    identities, grouped = {}, {}
    for row in rows:
        identity = candidate_id(row)
        binding = source_identity_v14(row)
        if identity in identities and identities[identity] != binding:
            raise RecognitionConflict('rag_material_changed')
        identities[identity] = binding
        grouped.setdefault(identity, []).append(row)
    # 复用现有坐标并集：相邻与重叠只拼真实已读文本，分离窗口不填中间空白。
    result = []
    for group in grouped.values():
        if group[0]['kind'] == 'recognition':
            # 认识的条件及来源说明是原子排版，不可用正文窗口重写excerpt。
            if any(row['windows'] != group[0]['windows'] or row['excerpt'] != group[0]['excerpt'] for row in group):
                raise RecognitionConflict('rag_material_changed')
            result.append(group[0])
        else:
            result.extend(project_sources_v14(group))
    return result


def _remember_observed(plan, pool, row):
    """9同源阅读积累真实窗口；旧策略仍按最后一次阅读覆盖。"""
    identity = candidate_id(row)
    old = pool.get(identity)
    if old is not None and getattr(_policy(plan), 'explicit_answer_evidence', False):
        row = _merge_observed([old, row])[0]
    pool[identity] = row
    return row


def _descriptors(candidates):
    result = {}
    for row in candidates:
        if row['kind'] == 'search':
            continue
        entry = row['entry']
        kind = 'original_source' if row['kind'] == 'source' else row['kind']
        item = {'type': kind, 'id': entry['id'], 'revision': entry['revision'],
                'project_id': row['scope'].project_id}
        result[(kind, item['id'], item['project_id'])] = item
    return list(result.values())


def answer_candidates(plan, identities):
    """只交接决策实际看过的窗口，不把目录或全部观察自动变成最终证据。"""
    available = {candidate_id(row): row for row in _candidates(plan)}
    if not identities:
        selected = _merge_observed(plan['chosen'])
        if len(selected) > _policy(plan)(None, operation='agentic_limits')['results']:
            raise RecognitionConflict('rag_answer_outside_observed')
        return selected
    selected = identities
    if (len(selected) > _policy(plan)(None, operation='agentic_limits')['results']
            or len(set(selected)) != len(selected) or not set(selected) <= available.keys()):
        raise RecognitionConflict('rag_answer_outside_observed')
    return [available[identity] for identity in selected]


def _frozen_candidates(rows):
    keys = {'kind', 'id', 'layer', 'scope', 'entry', 'snapshot', 'windows', 'excerpt', 'coordinate_space',
            'title', 'href', 'match_in', 'document_ids', 'bookshelf', 'validity', 'time_scope', 'validity_revision'}
    return encode([{key: value for key, value in row.items() if key in keys or row['kind'] == 'search'} for row in rows])


def _all_descriptors(query, plan):
    descriptors = _descriptors(_candidates(plan))
    for dependency in plan['_rag'].get('history_dependencies', []):
        descriptors.extend(_descriptors(dependency.get('chosen', [])))
        for fact in dependency.get('clarifications', []):
            store = query.answer_turns.application.state.ai_turn_store
            saved = store.get_immutable_payload(fact['id'], 'answer-gap-input-' + fact['marker']['invocation'] + '-v1')
            if saved is None:
                raise RecognitionConflict('rag_clarification_changed')
            descriptors.extend(saved[1]['material_proof']['privacy']['material_refs'])
    descriptors.extend({'type': 'recognition', 'id': row['id'], 'revision': row['revision'], 'project_id': 'me'}
        for row in plan.get('profile', {}).get('items', []) if 'id' in row and 'revision' in row)
    return descriptors


def _validate_steps(plan):
    active = ACTIVE_ANSWER.get()
    if active is None:
        raise RecognitionConflict('rag_turn_unavailable')
    request, store, _, _ = active
    state = plan['_rag']
    if len(state['steps']) != state['round'] - 1:
        raise RecognitionConflict('rag_round_changed')
    for number, step in enumerate(state['steps'], 1):
        invocation = 'rag-decision-' + str(number)
        value = store.get_immutable_payload(request['turn_id'], 'answer-model-result-' + invocation)
        if step['round'] != number or value is None or value[1]['output'] != step['decision']:
            raise RecognitionConflict('rag_previous_decision_changed')


def material_input(query, plan, models, *, local_only):
    """从原领域 owner 重建每次实际发给决策模型的完整闭包。"""
    from ..context_adapter import format_recognition_content
    from .turn_requests import validate_frozen_inputs
    from .rag_skills import RagSkills
    _validate_steps(plan)
    rows = _candidates(plan)
    probe = {**plan, 'chosen': rows}
    query.validate_ask_plan(probe)
    texts = []
    for candidate in rows:
        entry, coordinates = candidate['entry'], candidate.get('coordinate_space')
        if candidate['kind'] == 'recognition':
            current = query.service.get_recognition(scope=candidate['scope'], recognition_id=entry['id'])
            if current is None or not current.authorized:
                raise RecognitionConflict('rag_material_changed')
            text = format_recognition_content(current.retrieval_projection())
            if candidate['excerpt'] != text:
                raise RecognitionConflict('rag_material_changed')
        else:
            if candidate['kind'] == 'search':
                content = entry['content']
            else:
                current, hydrated = query.retrieval_index.hydrate(candidate['scope'].project_id, entry)
                if current is None:
                    raise RecognitionConflict('rag_material_changed')
                if coordinates == 'document_markdown_v1':
                    content = hydrated.markdown[entry['id']]
                elif coordinates == 'workspace_source_text_v1':
                    item = hydrated.items.get(entry.get('item_id'))
                    if item is None:
                        raise RecognitionConflict('rag_material_changed')
                    content = str(item.payload.get('source_text') or '')
                elif coordinates == 'source_content_v1':
                    content = current['content']
                else:
                    raise RecognitionConflict('rag_coordinates_invalid')
            windows = candidate['windows']
            if (not windows or any(type(window.start) is not int or type(window.end) is not int
                    or not 0 <= window.start < window.end <= len(content)
                    or content[window.start:window.end] != window.text for window in windows)):
                raise RecognitionConflict('rag_window_changed')
            text = '\n…\n'.join(window.text for window in windows)
            if candidate['excerpt'] != text:
                raise RecognitionConflict('rag_material_changed')
        item = {'id': candidate_id(candidate), 'layer': candidate['layer'], 'title': candidate['title'], 'text': text}
        if getattr(_policy(plan), 'explicit_answer_evidence', False):
            chosen = next((row for row in _merge_observed(plan['chosen']) if candidate_id(row) == item['id']), None)
            item['final_evidence'] = chosen is not None and chosen['windows'] == candidate['windows']
        texts.append(item)
    # 画像和历史虽不是证据，辅助决策及其派生查询仍受完整来源约束。
    descriptors = _all_descriptors(query, plan)
    allowed, privacy = freeze_turn_materials(query.records, models, plan['project_id'], descriptors,
        authority=SourceEgressService(query.records), local_only=local_only)
    if len(allowed) != len(descriptors) or (not local_only and not privacy['allow_remote']):
        raise RecognitionConflict('rag_egress_unavailable')
    active = ACTIVE_ANSWER.get()
    frozen = active[1].get_request(active[0]['turn_id'])
    validate_frozen_inputs(query.records, models, {**frozen, 'privacy': privacy}, query=query)
    skills = plan['_rag']['skills']
    RagSkills(query.answer_turns.root).validate(plan['project_id'], skills)
    state = plan['_rag']
    context = {'question': plan['question'], 'retrieval_question': state['question'],
               'original_task_and_conditions': state['task_text'],
               'history': plan.get('history', ''), 'profile': plan.get('profile', {}).get('text', ''),
               'steps': state['steps'], 'materials': texts, 'skills': skills,
               'catalog': state['catalog'], 'round': state['round'],
               'tools': tool_availability(query, plan['project_id']),
               'remaining_decisions': _policy(plan)(None, operation='agentic_limits')['decisions'] - state['round']}
    if getattr(_policy(plan), 'explicit_answer_evidence', False):
        context['final_evidence_ids'] = [item['id'] for item in texts if item['final_evidence']]
    if state.get('fast_path_diagnostics'):
        context['fast_path_diagnostics'] = state['fast_path_diagnostics']
    proof = {'privacy': privacy, 'candidates': _frozen_candidates(rows), 'skills': deepcopy(skills),
             'history': encode(state['history']), 'profile': encode(plan.get('profile')),
             'tools': tool_availability(query, plan['project_id']),
             'search': deepcopy(getattr(plan.get('search_guard'), 'frozen_binding', None))}
    return {'messages': _policy(plan)(None, context, operation='agentic_messages'),
            'privacy': privacy, 'selection': proof}


def validate_derived_query(query, plan, purpose):
    """模型从本机私密材料推导的新词不能取得远程工具的授权。"""
    descriptors = _all_descriptors(query, plan)
    allowed, privacy = freeze_turn_materials(query.records, query.models, plan['project_id'], descriptors,
        authority=SourceEgressService(query.records), purpose=purpose)
    if len(allowed) != len(descriptors) or not privacy['allow_remote']:
        raise RecognitionConflict('rag_derived_egress_blocked')
    return privacy


def validate_material_proof(query, project, target, proof, *, request):
    from .turn_requests import validate_frozen_inputs
    from .followup import validate_history
    candidates, history = decode(proof['candidates']), decode(proof['history'])
    probe = {'project_id': project, 'scope': WorkScope('local-user', project),
             'target': target, 'chosen': candidates, 'profile': decode(proof['profile'])}
    if history:
        probe['history_guard'] = lambda: validate_history(query, project, history, target)
    if proof.get('search'):
        from .search import validate_search_binding
        probe['search_guard'] = lambda: validate_search_binding(query, proof['search'], probe)
    validate_frozen_inputs(query.records, query.models, {**request, 'privacy': proof['privacy']}, query=query)
    query.validate_ask_plan(probe)
    from .rag_skills import RagSkills
    RagSkills(query.answer_turns.root).validate(project, proof['skills'])


def validate_rag_proof(query, plan):
    binding = plan['_rag_proof']
    store = query.answer_turns.application.state.ai_turn_store
    request = store.get_request(binding['turn_id'])
    prepared = store.get_immutable_payload(binding['turn_id'], 'answer-gap-input-' + binding['invocation'] + '-v1')
    if (request is None or request['scope']['project_id'] != plan['project_id']
            or request['input']['text'] != plan['question'] or prepared is None
            or prepared[0] != binding['input_ref']
            or ('proof' in binding and prepared[1]['material_proof']['selection'] != binding['proof'])):
        raise RecognitionConflict('rag_proof_changed')
    # 原不可变输入是证明权威，旧内嵌值仍逐值复验，新计划只保留引用以免递归编码。
    proof = prepared[1]['material_proof']['selection']
    validate_material_proof(query, plan['project_id'], plan['target'], proof, request=request)


async def _decide(query, plan):
    from ..answer_proof import review_input_tokens
    limits = _policy(plan)(None, operation='agentic_limits')
    invocation = 'rag-decision-' + str(plan['_rag']['round'])
    selected, _, prepared = gap_answer_input(query.models, plan)
    budget = selected.generation_budget_limits(expected_revision=query.ask_target()['revision'], max_tokens=limits['output_tokens'])
    if review_input_tokens(prepared['messages'], response_model=RagDecision) > budget['window'] - budget['reserve']:
        raise RagInputBudgetExhausted('rag_input_budget_exhausted')
    deadline = monotonic() + limits['decision_seconds']
    def validate():
        _checkpoint()
        if monotonic() >= deadline:
            raise TimeoutError('rag_decision_timeout')
        query.validate_ask_plan(plan)
    def invoke():
        return generate_answer(query.models, prepared['messages'], response_model=RagDecision,
            max_tokens=limits['output_tokens'], validate_current=validate, purpose='aux',
            invocation_key=invocation, gap_plan=plan, timeout_seconds=limits['decision_seconds'])
    try:
        output, metadata = await settled_call(invoke)
        return _policy(plan)(None, output.model_dump(), operation='agentic_decision'), metadata.get('usage', {})
    except BaseException:
        abandon_answer_model(invocation)
        raise


def _catalog(rows, limit):
    return [{'id': candidate_id(row), 'title': row['title'], 'layer': row['layer']}
            for row in rows[:limit]]


def _skill_context(query, plan):
    project = query.records.read('v2_projects', plan['project_id'])
    summary = str(project.payload.get('name', '')) if project is not None else ''
    refs = '\n'.join('source:crp://default/' + row['kind'] + '/' + row['id'] for row in _candidates(plan)
                     if row['kind'] != 'search')
    return {'task_text': plan['_rag']['task_text'], 'project_summary': '\n'.join(filter(None, (summary, refs)))}


async def investigate(query, project, question, plan, *, turn_id, scene=None, history=None,
                      initial_memory=None, initial_route=None):
    from .multi_query import fuse_candidates
    from .rag_skills import RagSkills
    from .search import agentic_search, validate_agentic_search_tools
    limits = _policy(plan)(None, operation='agentic_limits')
    previous_clarification = (history or {}).get('clarification')
    task_text = (previous_clarification['question'] + '\n用户补充：' + plan['question']
                 if previous_clarification else plan['question'])
    plan['_rag'] = {'round': 1, 'question': question, 'steps': [], 'catalog': [], 'observed': [], 'skills': [],
                    'task_text': task_text, 'depths': [], 'history': history or {},
                    'history_dependencies': (history or {}).get('dependencies', [])}
    from .privacy import privacy_revision
    from .bookshelf import _validate
    privacy_version = privacy_revision(query.records)
    def empty_bookshelf():
        _validate(query, [], privacy_version, project, scene)
    empty_bookshelf.continuation = {'spines': [], 'version': privacy_version, 'project': project, 'scene': scene}
    empty_bookshelf.frozen_binding = {'hits': [], 'version': privacy_version, 'project': project, 'scene': scene}
    plan['bookshelf_guard'] = empty_bookshelf
    if initial_route is not None and not getattr(_policy(plan), 'initial_memory_probe', False):
        raise RecognitionConflict('initial_memory_policy_required')
    if not query.ask_target()['model'] or (query.ask_target()['execution_location'] == 'remote'
            and not egress_allowed(query.records, query.models, project, 'generation')):
        if initial_route is not None:
            initial_route['reason'] = 'model_unavailable'
        plan.pop('_rag')
        plan['_rag_summary'] = {'version': 1, 'status': 'skipped', 'stop': 'model_unavailable', 'steps': []}
        return plan, {'usage': {}, 'receipt_ids': [], 'status': 'skipped', 'usage_complete': True}, {'queries': [], 'used': False}
    usages, receipts, seen, no_progress, pool = [], [], set(), 0, {}
    initial_seed_ids = set()
    initial_complete = None
    raw_attempt = {'usage': {}, 'receipt_ids': [], 'usage_complete': True}
    if initial_memory is not None:
        query.validate_initial_candidates(project, question, initial_memory, scene=scene)
        raw_probe = getattr(_policy(plan), 'raw_evidence_probe', False)
        evidence_probe = getattr(_policy(plan), 'evidence_answer_probe', False)
        if evidence_probe:
            from .evidence_fast_path import attempt
            probe = {**plan, 'chosen': list(plan['chosen'])}
            raw_attempt = await attempt(query, probe, initial_memory, initial_route)
            initial_complete = initial_memory
            if raw_attempt.get('accepted'):
                plan.update(chosen=probe['chosen'], evidence_source_basis=probe['evidence_source_basis'],
                            evidence_answer_proof=probe['evidence_answer_proof'],
                            evidence_retrieved_recognitions=probe['evidence_retrieved_recognitions'])
                plan['_rag_summary'] = {'version': 1, 'status': 'skipped',
                                       'stop': 'evidence_answer_accepted', 'steps': []}
                plan.pop('_rag')
                return plan, {'usage': {}, 'receipt_ids': [], 'status': 'skipped',
                              'usage_complete': raw_attempt['usage_complete']}, {'queries': [], 'used': False}
            if raw_attempt.get('diagnostics'):
                plan['_rag']['fast_path_diagnostics'] = raw_attempt['diagnostics']
            assessment = {'status': 'insufficient', 'reason': initial_route['reason']}
        elif raw_probe:
            from .raw_fast_path import attempt
            # The raw contract has its own exact input budget. Do not require
            # the old dialogue-oriented two-draft fitter to understand it.
            probe = {**plan, 'chosen': list(initial_memory['candidates'])}
            raw_attempt = await attempt(query, probe, initial_memory, initial_route)
            initial_complete = initial_memory  # This probe already included L0.
            if raw_attempt.get('accepted'):
                plan.update(chosen=probe['chosen'], raw_source_basis=probe['raw_source_basis'],
                            raw_answer_proof=probe['raw_answer_proof'])
                plan['_rag_summary'] = {'version': 1, 'status': 'skipped',
                                       'stop': 'raw_answer_accepted', 'steps': []}
                plan.pop('_rag')
                return plan, {'usage': {}, 'receipt_ids': [], 'status': 'skipped',
                              'usage_complete': raw_attempt['usage_complete']}, {'queries': [], 'used': False}
            assessment = {'status': 'insufficient', 'reason': initial_route['reason']}
        else:
            from .initial_answer import assess
            assessment = assess(query, plan, reference=initial_memory.get('rank_reference'))
        initial_route['reason'] = assessment['reason']
        if assessment['status'] == 'sufficient':
            proof = assessment['proof']
            selected = [row for row in plan['chosen']
                        if row['kind'] == 'recognition' and row['id'] == proof['source_id']]
            if query.select_rag_answer(plan, selected):
                plan['initial_answer_proof'] = proof
                query.validate_ask_plan(plan)
                initial_route.update(route='direct', reason='initial_evidence_sufficient')
                plan['_rag_summary'] = {'version': 1, 'status': 'skipped',
                                       'stop': 'initial_evidence_sufficient', 'steps': []}
                plan.pop('_rag')
                return plan, {'usage': {}, 'receipt_ids': [], 'status': 'skipped',
                              'usage_complete': True}, {'queries': [], 'used': False}
            initial_route['reason'] = 'answer_budget_insufficient'
        # These actual selected windows are displayed to the first real decision.
        for row in initial_memory['candidates'] if raw_probe or evidence_probe else plan['chosen']:
            _remember_observed(plan, pool, row)
            initial_seed_ids.add(candidate_id(row))
        plan['_rag']['observed'] = list(pool.values())
        plan['_rag']['catalog'] = _catalog(list(pool.values()), limits['results'])
        plan['_rag']['depths'] = ['summary', 'body', 'original'] if raw_probe or evidence_probe else ['summary', 'body']
    status, stop = 'completed', 'round_limit'
    for number in range(1, limits['decisions'] + 1):
        _checkpoint()
        plan['_rag']['round'] = number
        invocation = 'rag-decision-' + str(number)
        if initial_route is not None:
            initial_route['decision_invocations'] += 1
        try:
            decision, usage = await _decide(query, plan)
            usages.append(usage)
        except RecognitionConflict:
            raise
        except RagInputBudgetExhausted:
            status, stop = 'stopped', 'input_budget'
            break
        except (ValueError, TimeoutError):
            status, stop = 'failed', 'decision_failed'
            break
        finally:
            observation = answer_observation(invocation)
            receipts.extend(observation['receipt_ids'])
            if len(usages) < number:
                usages.append(sum_usage(*observation['observations']))
        action = decision['action']
        if action in {'answer', 'clarify'}:
            if action == 'answer' and getattr(_policy(plan), 'explicit_answer_evidence', False):
                selected = answer_candidates(plan, decision['ids'])
                if not query.select_rag_answer(plan, selected):
                    # 明确选中的必要证据不能被旧chosen替代后继续作答。
                    raise RagInputBudgetExhausted('rag_answer_input_budget_exhausted')
            stop = action
            plan['_rag_last'] = {'version': 1, 'status': action, 'round': number,
                'invocation': invocation, 'question': task_text, 'questions': decision['questions']}
            break
        if number == limits['decisions']:
            stop = 'decision_budget'
            break
        key = (action, decision['source'], decision['query'].strip().casefold(), decision['depth'], tuple(decision['ids']))
        if key in seen:
            stop = 'repeated_tool'
            break
        seen.add(key)
        previous = ({candidate_id(row) for row in _candidates(plan)} | {row['id'] for row in plan['_rag']['skills']}
                    | {'depth:' + depth for depth in plan['_rag']['depths']})
        result = {'status': 'completed', 'items': []}
        try:
            _checkpoint()
            if action == 'search' and decision['source'] == 'memory':
                public = query.models.public()
                for purpose in ('embedding', 'rerank'):
                    config = public.get(purpose, {})
                    address = str(config.get('base_url') or '')
                    if config.get('enabled') and address and urlsplit(address).hostname not in {'127.0.0.1', 'localhost', '::1'}:
                        validate_derived_query(query, plan, purpose)
                if initial_memory is not None and decision['query'] == question:
                    if initial_complete is None:
                        initial_complete = await settled_call(query.collect_initial_remainder,
                            project, question, initial_memory, scene=scene)
                        initial_route['remainder_collections'] += 1
                    else:
                        await settled_call(query.validate_initial_candidates,
                            project, question, initial_complete, scene=scene)
                    collected = initial_complete
                    initial_route['reused_queries'] += 1
                else:
                    collected = await settled_call(query.collect_candidates, project, decision['query'], scene=scene)
                policy = _policy(plan)
                depth = policy(None, decision['depth'], plan['_rag']['depths'], operation='agentic_depth')
                drilldown = getattr(policy, 'empty_layer_drilldown', False)
                candidates = [row for row in collected['candidates']
                    if row['kind'] in {'recognition', 'document', 'source'} and not row.get('inspiration')]
                if not drilldown:
                    layers = {'summary': {'L3', 'L2'}, 'body': {'L1'}, 'original': {'L0'}}[depth]
                    candidates = [row for row in candidates if row['layer'] in layers]
                    candidates = sorted(candidates, key=lambda row: -row.get('score', 0))[:limits['results']]
                if depth != 'summary' or drilldown:
                    # A probe observation is not yet a model-selected navigation anchor.
                    upper = [row for row in pool.values() if row['layer'] in {'L3', 'L2'}
                             and candidate_id(row) not in initial_seed_ids]
                    if upper:
                        allowed = set()
                        from .insights import source_documents
                        for row in upper:
                            allowed.update(node['id'] for node in row.get('snapshot', {}).get('nodes', [])
                                if node['type'] in {'original_source', 'original_item'})
                            if row['kind'] == 'document':
                                allowed.add(row['entry']['id'])
                            elif row['kind'] == 'recognition':
                                allowed.update(source_documents(query.records, row['scope'],
                                    [node['id'] for node in row['snapshot']['nodes'] if node['type'] == 'experience']))
                        candidates = [row for row in candidates
                            if row['layer'] in {'L3', 'L2'} or row['entry']['id'] in allowed]
                depths = (policy(None, decision['depth'], plan['_rag']['depths'],
                    {row['layer'] for row in candidates}, operation='agentic_search_depths')
                    if drilldown else (depth,))
                depth = depths[-1]
                layers = {'summary': {'L3', 'L2'}, 'body': {'L1'}, 'original': {'L0'}}[depth]
                rows = [row for row in candidates if row['layer'] in layers]
                rows = sorted(rows, key=lambda row: -row.get('score', 0))[:limits['results']]
                plan['_rag']['depths'].extend(depths)
                result['actual_depth'] = depth
                for row in rows:
                    initial_seed_ids.discard(candidate_id(row))
                    old = pool.get(candidate_id(row))
                    if old is not None and old['entry']['revision'] != row['entry']['revision']:
                        raise RecognitionConflict('rag_material_changed')
                    _remember_observed(plan, pool, row)
                plan['_rag']['observed'] = list(pool.values())
                plan['_rag']['catalog'] = _catalog(rows, limits['results'])
                # 搜索即给真实窗口，下一轮模型读得到本轮事实；read还可单独选目录阅读。
                combined = fuse_candidates([plan['chosen'], rows])
                next_plan = query.prepare_ask(project, plan['question'], scene=scene,
                    retrieval_question=question, history=plan.get('history', ''),
                    part_context=plan.get('part_context'),
                    collected={**collected, 'candidates': combined, '_rerank_state': plan.get('_rerank_state', {})})
                for name in ('_rag', 'history_guard', 'search_guard', 'search', 'bookshelf_guard'):
                    if name in plan:
                        next_plan[name] = plan[name]
                plan = next_plan
                if plan.get('search_guard'):
                    from .search import rebind_agentic_search
                    rebind_agentic_search(query, plan, plan['search_guard'].frozen_binding)
                result['items'] = _catalog(rows, limits['results'])
            elif action == 'search' and decision['source'] == 'skill':
                result['items'] = RagSkills(query.answer_turns.root).search(project, decision['query'], limits['results'],
                                                                          **_skill_context(query, plan))
                plan['_rag']['catalog'] = result['items']
            elif action == 'search' and decision['source'] == 'web':
                tools = validate_agentic_search_tools(query, project, parent=turn_id, number=number)
                if tools['available']:
                    validate_derived_query(query, plan, 'search')
                    result['items'] = await agentic_search(query, plan, decision['query'], parent=turn_id, number=number)
                else:
                    result.update(status='unavailable', reason='search_not_configured_or_enabled')
                plan['_rag']['catalog'] = result['items']
            elif action == 'read':
                catalog_ids = {row['id'] for row in plan['_rag']['catalog']}
                if not set(decision['ids']) <= catalog_ids:
                    raise RecognitionConflict('rag_read_outside_catalog')
                for identity in decision['ids']:
                    if identity.startswith('skill:'):
                        fact = RagSkills(query.answer_turns.root).read(project, identity, **_skill_context(query, plan))
                        if fact not in plan['_rag']['skills']:
                            plan['_rag']['skills'].append(fact)
                    elif identity not in pool and not any(candidate_id(row) == identity for row in plan['chosen']):
                        raise RecognitionConflict('rag_read_outside_catalog')
                    else:
                        candidate = pool.get(identity) or next(row for row in plan['chosen'] if candidate_id(row) == identity)
                        initial_seed_ids.discard(identity)
                        depth = _policy(plan)(None, decision['depth'], plan['_rag']['depths'], operation='agentic_depth')
                        rows = await settled_call(query.rag_read, plan, candidate, depth)
                        for row in rows:
                            _remember_observed(plan, pool, row)
                            if not any(candidate_id(old) == candidate_id(row) and old['windows'] == row['windows'] for old in plan['chosen']):
                                plan['chosen'].append(row)
                        plan['_rag']['observed'] = list(pool.values())
                        plan['_rag']['depths'].append(depth)
                        if plan.get('search_guard'):
                            from .search import rebind_agentic_search
                            rebind_agentic_search(query, plan, plan['search_guard'].frozen_binding)
                result['items'] = decision['ids']
            _checkpoint()
            query.validate_ask_plan(plan)
        except RecognitionConflict:
            raise
        except (ValueError, RuntimeError):
            status, stop = 'failed', 'tool_failed'
            break
        current = ({candidate_id(row) for row in _candidates(plan)} | {row['id'] for row in plan['_rag']['skills']}
                   | {'depth:' + depth for depth in plan['_rag']['depths']})
        no_progress = no_progress + 1 if current == previous else 0
        plan['_rag']['steps'].append({'round': number, 'decision': decision, 'result': result})
        if no_progress >= limits['no_progress']:
            stop = 'no_new_material'
            break
    plan['_rag_summary'] = {'version': 1, 'status': status, 'stop': stop,
        'steps': deepcopy(plan['_rag']['steps']), **plan.get('_rag_last', {})}
    active = ACTIVE_ANSWER.get()
    prepared = None
    for index in range(number, 0, -1):
        invocation = 'rag-decision-' + str(index)
        prepared = active[1].get_immutable_payload(turn_id, 'answer-gap-input-' + invocation + '-v1')
        if prepared is not None:
            break
    if prepared is not None:
        plan['_rag_proof'] = {'turn_id': turn_id, 'invocation': invocation, 'input_ref': prepared[0]}
    # 冻结给最终作答/继续消费者的数据不含可执行闭包和临时选择器。
    plan.pop('_rag', None)
    return plan, {'usage': sum_usage(raw_attempt['usage'], *usages),
                  'receipt_ids': [*raw_attempt['receipt_ids'], *receipts], 'status': status,
                  'usage_complete': raw_attempt['usage_complete'] and all(answer_observation('rag-decision-' + str(index))['complete']
                      for index in range(1, number + 1))}, {'queries': [step['decision']['query']
                      for step in plan['_rag_summary']['steps'] if step['decision']['action'] == 'search'],
                      'used': bool(plan['_rag_summary']['steps'])}


def clarification(query, row):
    """可见标记必须与原完成 Turn 的真实模型决策一致。"""
    trace = row.payload.get('receipt', {}).get('ask', {}).get('trace', [])
    marker = trace[0].get('agentic') if trace else None
    if not isinstance(marker, dict) or marker.get('version') != 1 or marker.get('status') != 'clarify':
        return None
    store = query.answer_turns.application.state.ai_turn_store
    saved = store.get_immutable_payload(row.object_id, RESULT_KIND)
    request = store.get_request(row.object_id)
    events = tuple(store.events_after(row.object_id))
    if (saved is None or not request or request['scope']['project_id'] != row.payload['project_id']
            or request['input']['text'] != row.payload['user_text']
            or not events or events[-1]['type'] != 'turn.completed'
            or saved[1]['receipt']['ask']['trace'][0].get('agentic') != marker):
        raise RecognitionConflict('rag_clarification_changed')
    invocation = marker['invocation']
    model = store.get_immutable_payload(row.object_id, 'answer-model-result-' + invocation)
    prepared = store.get_immutable_payload(row.object_id, 'answer-gap-input-' + invocation + '-v1')
    if (model is None or prepared is None or model[1]['output']['action'] != 'clarify'
            or model[1]['output']['questions'] != marker['questions']):
        raise RecognitionConflict('rag_clarification_changed')
    validate_material_proof(query, row.payload['project_id'], query.ask_target(),
                            prepared[1]['material_proof']['selection'], request=request)
    return marker


def pending_clarification(query, records, project, thread):
    if not thread:
        return None
    rows = records.list_matching('v2_turns', project_id=project, thread_id=thread)
    rows = sorted(rows, key=lambda row: (row.payload.get('created_at', ''), row.object_id))
    if not rows or rows[-1].payload.get('intent') != 'ask':
        return None
    row = rows[-1]
    try:
        return clarification(query, row)
    except RecognitionError as error:
        if str(error) not in {'turn material revision conflicted', 'source changed during question',
                             'source excluded from recall during question'}:
            raise
        # 原完成轮的标记和模型证明已严格核对，仅正常资料变化可以解除旧澄清控制。
        marker = row.payload['receipt']['ask']['trace'][0]['agentic']
        store = query.answer_turns.application.state.ai_turn_store
        prepared = store.get_immutable_payload(row.object_id, 'answer-gap-input-' + marker['invocation'] + '-v1')
        selection = prepared[1]['material_proof']['selection']
        from backend.shared.memory_sidecars import recall_weight
        for material in selection['privacy']['material_refs']:
            if material['type'] != 'document':
                continue
            current = records.read('documents', material['id'])
            if (current is not None and current.payload.get('project_id') == material['project_id']
                    and (current.revision > material['revision']
                         or recall_weight(records, 'document', material['id']) == 0)):
                return None
        if str(error) == 'source excluded from recall during question':
            for candidate in decode(selection['candidates']):
                if candidate['kind'] != 'source':
                    continue
                documents = [records.read('documents', identity) for identity in candidate.get('document_ids', [])]
                if documents and all(document is not None
                        and document.payload.get('project_id') == candidate['scope'].project_id
                        and recall_weight(records, 'document', document.object_id) == 0 for document in documents):
                    return None
        raise
