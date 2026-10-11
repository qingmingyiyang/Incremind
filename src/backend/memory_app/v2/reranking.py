"""工作台在每层选取前重排已授权片段，复用模型适配器和辅助回执。"""
from copy import deepcopy
from dataclasses import replace
import math
import json
from urllib.parse import urlsplit

from backend.recognition import RecognitionConflict, WorkScope
from ..context_adapter import format_recognition_content
from ..retrieval_models import configured_adapter, _public_identity
from ..source_egress import SourceEgressService
from .budget import text_tokens, trim_candidate
from .insights import insight_view
from .memory_turn import embedding_request
from .policies import get, version
from .policies.ladder import _RerankBatch
from .policies.scope import scene_priority
from .policies.types import ScopeInput
from .privacy import egress_allowed, is_private_project, privacy_revision
from .projects import scene_of


def _bounded_candidate(row, question, limit):
    # 全局 trim_candidate 的限额按窗口计算；重排需另给整条含标题限额。
    count = len(row.get('windows', ()))
    if row['layer'] == 'L3' or not count:
        fitted = trim_candidate(row, question, limit=limit)
    else:
        overhead = text_tokens(row['title']) + text_tokens('\n…\n') * (count - 1) + count + 4
        if overhead >= limit:
            return None
        fitted = trim_candidate(row, question, limit=max(1, (limit - overhead) // count))
    return fitted if fitted is not None and text_tokens(fitted['title'] + '\n' + fitted['excerpt']) <= limit else None


def _validate_scene(query, project, scene, candidate, scope_policy, documents):
    scope = candidate['scope']
    entry = candidate['entry']
    if scope.project_id != project:
        raise RecognitionConflict('rerank_scope_changed')
    if candidate['kind'] == 'recognition':
        view = insight_view(query.records, scope, entry['id'], service=query.service)
        if view is None or view['state'] != 'active' or view['scene'] != candidate.get('scene'):
            raise RecognitionConflict('rerank_scene_changed')
        assigned = view['scene']
    else:
        assignment = scene_of(query.records, 'document', entry['id']) if candidate['kind'] == 'document' else None
        if assignment is None and entry.get('item_id'):
            assignment = scene_of(query.records, 'item', entry['item_id'])
        assigned = assignment['scene'] if assignment and assignment.get('project_id') == project else None
        if candidate['kind'] == 'source':
            linked = []
            for row in documents:
                document = row.payload
                if document.get('project_id') != project or document.get('status') == 'archived':
                    continue
                if not any(isinstance(ref, dict) and ref.get('source_id') == entry['id']
                           for ref in document.get('source_refs', [])):
                    continue
                marker = scene_of(query.records, 'document', row.object_id)
                value = marker['scene'] if marker and marker.get('project_id') == project else None
                if scene is None or scope_policy(ScopeInput(scene, value)):
                    linked.append((row.object_id, value))
            if sorted(identity for identity, _ in linked) != sorted(candidate.get('document_ids', [])):
                raise RecognitionConflict('rerank_source_scene_changed')
            if assignment is None and linked:
                linked.sort(key=lambda value: (scene_priority(ScopeInput(scene, value[1]), scope_policy), value[0]))
                assigned = linked[0][1]
        if assigned != candidate.get('scene'):
            raise RecognitionConflict('rerank_scene_changed')
    if not scope_policy(ScopeInput(scene, assigned)):
        raise RecognitionConflict('rerank_scene_excluded')


def _validate_excerpt(query, project, candidate):
    if candidate['kind'] == 'recognition':
        if candidate['excerpt'] != format_recognition_content(candidate['entry']):
            raise RecognitionConflict('rerank_recognition_fragment_changed')
        return
    current, hydrated = query.retrieval_index.hydrate(project, candidate['entry'])
    if current is None:
        raise RecognitionConflict('rerank_fragment_unavailable')
    coordinates = candidate.get('coordinate_space')
    if coordinates == 'document_markdown_v1':
        content = hydrated.markdown[candidate['entry']['id']]
    elif coordinates == 'workspace_source_text_v1':
        item = hydrated.items.get(candidate['entry'].get('item_id'))
        content = str(item.payload.get('source_text') or '') if item else ''
    elif coordinates in {'source_content_v1', 'workspace_query_content_v1'}:
        content = current['content']
    else:
        raise RecognitionConflict('rerank_fragment_coordinates_invalid')
    windows = candidate.get('windows', ())
    if (not windows or '\n…\n'.join(window.text for window in windows) != candidate['excerpt']
            or any(not 0 <= window.start < window.end <= len(content)
                   or content[window.start:window.end] != window.text for window in windows)):
        raise RecognitionConflict('rerank_fragment_changed')
    return content


def _dynamic_frontier_candidate(query, project, row, question, admission):
    """复用完整记录扩窗；给重排的表示与最终候选分别保留真实坐标。"""
    if admission(row):
        return deepcopy(row)
    fitted = query._fit_dynamic_candidate(row, question, admission=admission)
    return fitted if fitted is not None and admission(fitted) else None


def _payload_tokens(question, payloads, model):
    # 与 HttpReranker 的真实 JSON 形状一致，标题、转义及装包开销均计入。
    payload = {'model': model, 'query': question,
               'documents': [payload['content'] for payload in payloads]}
    return (len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 2) // 3


def _candidate_binding(candidate):
    return {'kind': candidate['kind'], 'id': candidate['entry']['id'],
        'revision': candidate['entry']['revision'], 'item_revision': candidate['entry'].get('item_revision'),
        'coordinates': candidate.get('coordinate_space'), 'scene': candidate.get('scene'),
        'title': candidate['title'], 'excerpt': candidate['excerpt'],
        'windows': [[window.start, window.end] for window in candidate.get('windows', ())],
        'snapshot': deepcopy(candidate['snapshot'])}


def _semantic_rerank_candidates(rows, selected, positions, scores, question, batch):
    """只为真实原问评分的完整表示保留请求内资格，不给截片广播分数。"""
    evidence = []
    for candidate, position in zip(selected, positions):
        original = rows[position]
        if candidate['id'] != original['id']:
            continue
        seeds = []
        for start, end, score in original.get('_original_vector_spans', original.get('_vector_spans', ())):
            if type(score) not in {int, float} or not math.isfinite(score):
                continue
            window = next((w for w in candidate.get('windows', ()) if w.start <= start < end <= w.end), None)
            if window is not None:
                seeds.append((start, end, score, window.text[start-window.start:end-window.start]))
        if seeds:
            # 确认资料的引用身份属于item，缓存binding仍绑定document，分别精确核验。
            evidence.append({'query': question, 'batch': batch, 'position': position, 'score': scores[str(position)],
                'candidate_id': original['id'], 'scope': original['scope'],
                'binding': _candidate_binding(candidate), 'seeds': tuple(seeds)})
    return evidence


def layer_reranker(query, project, question, *, scene=None, token_budget=4000, state=None):
    """返回仅本轮可用的排序闭包；只读和外部上下文不创建云端调用。"""
    state = state if state is not None else {'used_layers': [], 'failed': False}
    if query.retrieval_index.read_only or state['failed'] and not state.get('layers'):
        return None
    policy, policy_version = get('rerank'), version('rerank')
    scope_policy = get('scope')
    limits = policy()
    dynamic = limits.get('dynamic_frontier', False) is True
    try:
        configured = deepcopy(query.models.public().get('rerank', {}))
        if not configured.get('configured') or not configured.get('enabled'):
            state['failed'] = True
            return None
        remote = urlsplit(str(configured.get('base_url', ''))).hostname not in {'localhost', '127.0.0.1', '::1'}
        if is_private_project(query.records, project) or (remote and not egress_allowed(query.records, query.models, project, 'rerank')):
            state['failed'] = True
            return None
        frozen_privacy = privacy_revision(query.records)
    except Exception:
        state['failed'] = True
        return None
    authority = SourceEgressService(query.records)
    scope = WorkScope('local-user', project)
    semantic_rerank = getattr(get('ladder'), 'semantic_rerank', False)
    semantic_evidence = {}
    bounded_discovery = (getattr(get('ladder'), 'bounded_discovery', False)
        and get('ladder').demand(question)['relation'] == 'topic'
        and bool(get('ladder').demand(question)['actors']))
    def order(layer, rows, *, priority):
        if semantic_rerank:
            # 已有计划恢复缺少原轮种子证明时关闭新通道，不从缓存或辅助结果倒造资格。
            rows = [{key: value for key, value in row.items() if key != '_semantic_rerank'} for row in rows]
        if bounded_discovery:
            rows = [{key: value for key, value in row.items() if key != '_chain_candidates'} for row in rows]
        original_rows = rows
        discovery = bounded_discovery and layer in {'L1', 'L0'}
        cached = state.get('layers', {}).get(layer)
        if (len(rows) < 2 and not discovery) or (state['failed'] or layer in state['used_layers']) and cached is None:
            return rows
        try:
            if discovery:
                from .policies.ladder import discovery_rows_v14, representation_identity_v14, source_identity_v14, pack_frontier_v14
                rows, origins = [], []
                for origin, row in enumerate(original_rows):
                    for representation in (row, *discovery_rows_v14(row, question)):
                        if any(representation_identity_v14(representation) == representation_identity_v14(old) for old in rows):
                            continue
                        rows.append(representation)
                        origins.append(origin)
                ceiling = min(token_budget, limits['input_tokens'])
                if type(token_budget) is not int or token_budget < 0:
                    raise ValueError('invalid_rerank_frontier_budget')
                positions, discovery_reference, _private_observations = pack_frontier_v14(rows, question, ceiling,
                    lambda values: _payload_tokens(question, values, configured['model']), priority)
                selected = [deepcopy(rows[index]) for index in positions]
                payloads = [{'id': str(index), 'content': row['title']+'\n'+row['excerpt']}
                    for index, row in zip(positions, selected)]
                def source_positions(indices):
                    return sorted({origins[index] for index in indices
                        if _candidate_binding(rows[index]) == _candidate_binding(original_rows[origins[index]])
                        and rows[index]['id'] == original_rows[origins[index]]['id']})
                state.setdefault('frontiers', {})[layer] = {
                    'capacity_basis': limits['capacity_basis'], 'input_limit': ceiling,
                    'input_tokens': _payload_tokens(question, payloads, configured['model']),
                    'considered': len(original_rows), 'representation_positions': source_positions(positions),
                    'scored_positions': [], 'unscored_positions': list(range(len(original_rows))), 'status': 'prepared'}
            else:
                selected, payloads, positions = [], [], []
                consumed = text_tokens(question)
                if dynamic and (type(token_budget) is not int or token_budget < 0):
                    raise ValueError('invalid_rerank_frontier_budget')
                ceiling = min(token_budget, limits['input_tokens']) if dynamic else None
                # 旧版只考察固定前部；新版按同一单请求预算装入真实片段，未评分保持原位置。
                frontier = rows if dynamic else rows[:limits['candidate_limit']]
                considered = 0
                for position, row in enumerate(frontier):
                    considered += 1
                    if dynamic:
                        if row.get('persona') or row.get('inspiration') or row.get('bookshelf'):
                            continue
                        snapshot = row.get('snapshot')
                        if not snapshot or not snapshot.get('roots'):
                            continue
                        try:
                            authority.validate_snapshot(row['scope'], snapshot)
                            authority.require(snapshot, 'rerank')
                        except Exception:
                            continue
                        prepared_tokens = _payload_tokens(question, payloads, configured['model'])
                        candidate_ceiling = policy.frontier_ceiling(ceiling, prepared_tokens, len(payloads))
                        def admission(value):
                            proposed = [*payloads, {'id': str(position), 'content': value['title'] + '\n' + value['excerpt']}]
                            return _payload_tokens(question, proposed, configured['model']) <= candidate_ceiling
                        fitted = _dynamic_frontier_candidate(query, project, row, question, admission)
                        # 一个完整原记录超过均分空间时仍尝试真实总额度，避免误把长记录排除。
                        if fitted is None and candidate_ceiling < ceiling:
                            candidate_ceiling = ceiling
                            fitted = _dynamic_frontier_candidate(query, project, row, question, admission)
                        if fitted is None and selected:
                            break
                    else:
                        fitted = _bounded_candidate(row, question, limits['candidate_tokens'])
                    if fitted is None or fitted.get('persona') or fitted.get('inspiration') or fitted.get('bookshelf'):
                        continue
                    snapshot = fitted.get('snapshot')
                    if not snapshot or not snapshot.get('roots'):
                        continue
                    try:
                        authority.validate_snapshot(fitted['scope'], snapshot)
                        authority.require(snapshot, 'rerank')
                    except Exception:
                        continue
                    text = fitted['title'] + '\n' + fitted['excerpt']
                    cost = text_tokens(text)
                    if not dynamic and consumed + cost > min(token_budget, limits['input_tokens']):
                        continue
                    consumed += cost
                    positions.append(position)
                    selected.append(deepcopy(fitted))
                    payloads.append({'id': str(position), 'content': text})
                if dynamic:
                    state.setdefault('frontiers', {})[layer] = {
                        'capacity_basis': limits['capacity_basis'], 'input_limit': ceiling,
                        'input_tokens': _payload_tokens(question, payloads, configured['model']),
                        'considered': considered, 'representation_positions': list(positions),
                        'scored_positions': [], 'unscored_positions': list(range(len(rows))),
                        'status': 'prepared'}
            cached_scores = None
            if cached is not None:
                if (cached['configuration'] != list(_public_identity(configured))
                        or cached['policy'] != policy_version or cached['query'] != question
                        or dynamic and cached.get('input_limit') != ceiling):
                    if dynamic:
                        state['frontiers'][layer]['status'] = 'cache_binding_changed'
                    return original_rows if discovery else rows
                # 重写只复用同身份、修订、片段和来源的分数，新增材料保持原槽位。
                retained, cached_scores = [], {}
                for candidate, payload, position in zip(selected, payloads, positions):
                    previous = next((item for item in cached['candidates']
                        if item['binding'] == _candidate_binding(candidate)), None)
                    if previous is not None:
                        retained.append((candidate, payload, position))
                        cached_scores[str(position)] = previous['score']
                selected = [candidate for candidate, _, _ in retained]
                payloads = [payload for _, payload, _ in retained]
                positions = [position for _, _, position in retained]
            groups = {}
            for position in positions:
                groups.setdefault(priority(rows[position]), []).append(str(position))
            if not any(len(values) > 1 for values in groups.values()):
                if dynamic:
                    state['frontiers'][layer]['status'] = 'insufficient_group'
                    state['frontiers'][layer]['scored_positions'] = []
                    state['frontiers'][layer]['unscored_positions'] = list(range(len(original_rows) if discovery else len(rows)))
                return original_rows if discovery else rows

            def validate():
                if (privacy_revision(query.records) != frozen_privacy
                        or is_private_project(query.records, project)
                        or _public_identity(query.models.public().get('rerank', {})) != _public_identity(configured)
                        or remote and not egress_allowed(query.records, query.models, project, 'rerank')):
                    raise RecognitionConflict('rerank_authority_changed')
                query._validate_ask_sources({'project_id': project, 'scope': scope,
                    'target': {'execution_location': 'local'}, 'chosen': selected}, local_only=True)
                documents = (query.records.list_matching('documents', project_id=project)
                    if any(row['kind'] == 'source' for row in selected) else ())
                for candidate in selected:
                    authority.validate_snapshot(candidate['scope'], candidate['snapshot'])
                    authority.require(candidate['snapshot'], 'rerank')
                    _validate_scene(query, project, scene, candidate, scope_policy, documents)
                    _validate_excerpt(query, project, candidate)

            validate()
            bindings = [_candidate_binding(candidate) for candidate in selected]
            materials = []
            for candidate in selected:
                entry = candidate['entry']
                # 原文引用还绑定整理稿自身与原件根，不能只验证其中一边。
                descriptors = ([{'type': candidate['kind'], 'id': entry['id'], 'revision': entry['revision']}]
                    if candidate['kind'] in {'recognition', 'document'} else [])
                for descriptor in [*descriptors, *candidate['snapshot']['roots']]:
                    material = {**descriptor, 'project_id': project}
                    if material not in materials:
                        materials.append(material)
            if cached_scores is not None:
                scores = cached_scores
            else:
                adapter = configured_adapter(query.models, 'rerank', validate_current=validate)
                original = adapter.client

                class ObservedTransport:
                    def post_json(self, **request):
                        return embedding_request(query.records, query.models, project, materials,
                            {'namespace': 'workbench-layer-rerank-v1', 'purpose': 'rerank', 'layer': layer,
                             'policy': policy_version, 'query': question, 'candidates': payloads,
                             'bindings': bindings,
                             'materials': materials, 'privacy_revision': frozen_privacy,
                             'configuration': list(_public_identity(configured)),
                             **({'input_limit': ceiling} if dynamic else {})},
                            validate, original, request, purpose='rerank')

                state['used_layers'].append(layer)
                scores = replace(adapter, client=ObservedTransport()).rerank(query=question, candidates=payloads)
            if dynamic and (set(scores) != {payload['id'] for payload in payloads}
                    or any(isinstance(score, bool) or not isinstance(score, (float, int))
                           or not math.isfinite(score) for score in scores.values())):
                raise ValueError('invalid_rerank_frontier_scores')
            validate()
            if semantic_rerank and cached_scores is None:
                semantic_evidence[layer] = _semantic_rerank_candidates(
                    rows, selected, positions, scores, question, _RerankBatch())
                if discovery:
                    from .policies.ladder import rerank_evidence_v13
                    reference = next((value for value in semantic_evidence[layer]
                        if value['position'] == discovery_reference and rerank_evidence_v13(
                            {**rows[discovery_reference], '_semantic_rerank': value},
                            {**get('ladder').demand(question), '_retrieval_question': question}) is not None), None)
                    if reference is not None:
                        for value in semantic_evidence[layer]:
                            value['discovery_reference'] = (value['batch'], reference['score'])
            if cached_scores is None:
                state.setdefault('layers', {})[layer] = {'configuration': list(_public_identity(configured)),
                    'policy': policy_version, 'query': question,
                    'candidates': [{'binding': binding, 'score': scores[payload['id']]}
                        for binding, payload in zip(bindings, payloads)]}
                if dynamic:
                    state['layers'][layer]['input_limit'] = ceiling
            if dynamic:
                state['frontiers'][layer].update(status='cached' if cached_scores is not None else 'used',
                    scored_positions=source_positions(positions) if discovery else list(positions),
                    input_tokens=_payload_tokens(question, payloads, configured['model']),
                    unscored_positions=[index for index in range(len(original_rows) if discovery else len(rows))
                        if index not in (source_positions(positions) if discovery else positions)])
            result = list(rows)
            if semantic_rerank and not state['failed']:
                for position, row in enumerate(result):
                    if position not in positions:
                        continue
                    evidence = next((value for value in semantic_evidence.get(layer, ())
                        if value['candidate_id'] == row['id'] and value['binding'] == _candidate_binding(row)), None)
                    if evidence is not None:
                        result[position] = {**row, '_semantic_rerank': evidence}
            annotated = list(result)
            for identities in groups.values():
                ordered = policy(identities, {identity: scores[identity] for identity in identities},
                    scene_priorities={identity: rows[int(identity)].get('scope_priority', 0) for identity in identities})
                for identity, replacement in zip(identities, ordered):
                    result[int(identity)] = annotated[int(replacement)]
            if discovery:
                # 来源行与chain评分分开传递；只有完整同表示才可给原来源行附凭证。
                projected = [dict(row) for row in original_rows]
                for origin, row in enumerate(projected):
                    exact = next((value for value in result
                        if value['id'] == row['id'] and _candidate_binding(value) == _candidate_binding(row)), None)
                    if exact is not None and exact.get('_semantic_rerank'):
                        row['_semantic_rerank'] = exact['_semantic_rerank']
                    chains = tuple(value for value in result if value.get('_discovered_chain')
                        and value.get('_semantic_rerank') and source_identity_v14(value) == source_identity_v14(row))
                    if chains:
                        row['_chain_candidates'] = chains
                # 原来源的排序只使用其自身完整表示的真实分数，chain-only不搬给父行。
                source_scores = {str(origins[index]): scores[str(index)] for index in positions
                    if origins[index] in source_positions((index,))}
                source_groups = {}
                for identity in source_scores:
                    source_groups.setdefault(priority(original_rows[int(identity)]), []).append(identity)
                source_result = list(projected)
                for identities in source_groups.values():
                    ordered_sources = policy(identities, {identity: source_scores[identity] for identity in identities}, scene_priorities={
                        identity: original_rows[int(identity)].get('scope_priority', 0) for identity in identities})
                    for identity, replacement in zip(identities, ordered_sources):
                        source_result[int(identity)] = projected[int(replacement)]
                return source_result
            return result
        except Exception:
            # 一次失败即停止本轮后续重排，避免多层重复超时；异常不带资料写入日志。
            state['failed'] = True
            if dynamic and layer in state.get('frontiers', {}):
                state['frontiers'][layer]['status'] = 'failed'
                state['frontiers'][layer]['scored_positions'] = []
                state['frontiers'][layer]['unscored_positions'] = list(range(len(original_rows) if discovery else len(rows)))
            return original_rows if discovery else rows
    return order
