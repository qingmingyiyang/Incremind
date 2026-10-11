"""Select bounded evidence by layer, stopping when query coverage is sufficient."""
from datetime import datetime, timezone

from core.search_and_recall.evidence_windows import query_terms
from .usage import timestamp
from .budget import evidence_tokens, input_tokens, trim_candidate
from .recall_dedup import identity as candidate_identity, is_duplicate
from .policies import get
from .policies.types import EnoughInput
from .policies.enough import coverage_terms, weighted_coverage
from .policies.scope import prefer_scene_ties


_DETAIL = ("原文", "原话", "具体", "出处", "哪一段", "怎么说", "数据", "多少", "引用")


def overview_question(question):
    return any(word in question for word in ("最近在忙什么", "整体进展", "总体进展", "项目概览", "场景概览", "整体情况"))


def candidate_order(candidate):
    """Keep scores intact; use current-object recency only for rounded ties."""
    at = timestamp(candidate.get("sort_time"), datetime.min.replace(tzinfo=timezone.utc))
    return candidate.get("rrf_rank", -round(candidate["score"], 3)), -at.timestamp(), candidate["id"]


class LadderSelection:
    """Request-local continuation of one bounded selection, without I/O."""

    def __init__(self, steps, snapshot, replace_lower):
        self._steps = steps
        self._snapshot = snapshot
        self._replace_lower = replace_lower
        self._finished = False

    def advance(self, *, stop_after=None):
        if not self._finished:
            for layer in self._steps:
                if layer == stop_after:
                    return self._snapshot()
            self._finished = True
        return self._snapshot()

    def replace_lower(self, candidates, *, coverage_questions, cached_vectors):
        if self._finished:
            raise ValueError('ladder_selection_completed')
        self._replace_lower(candidates, coverage_questions, cached_vectors)


def plan_ladder(candidates, question, *, neighbors=None, token_budget=4000, prompt_overhead=0, history="", answer_question=None, coverage_questions=None, cached_vectors=None, methods=(), situation=None, inspirations=(), evidence_limit=None, layer_order=None, admission=None, cost=None, fit=None, semantic_anchors=()):
    return start_ladder(candidates, question, neighbors=neighbors, token_budget=token_budget,
        prompt_overhead=prompt_overhead, history=history, answer_question=answer_question,
        coverage_questions=coverage_questions, cached_vectors=cached_vectors,
        methods=methods, situation=situation, inspirations=inspirations, evidence_limit=evidence_limit,
        layer_order=layer_order, admission=admission, cost=cost, fit=fit, semantic_anchors=semantic_anchors).advance()


def start_ladder(candidates, question, *, neighbors=None, token_budget=4000, prompt_overhead=0, history="", answer_question=None, coverage_questions=None, cached_vectors=None, methods=(), situation=None, lower_order=None, inspirations=(), evidence_limit=None, layer_order=None, admission=None, cost=None, fit=None, semantic_anchors=()):
    if evidence_limit is not None and (type(evidence_limit) is not int or evidence_limit < 0):
        raise ValueError('invalid_ladder_evidence_limit')
    candidates = [c for c in candidates if not c.get("persona")]
    terms = coverage_terms(coverage_questions or [question], query_terms)
    chosen, trace = [], []
    vectors = dict(cached_vectors or {})
    policy = get('ladder')
    if getattr(policy, 'semantic_rerank', False):
        candidates = [{key: value for key, value in row.items() if key != '_semantic_rerank'} for row in candidates]
    if getattr(policy, 'bounded_discovery', False):
        candidates = [{key: value for key, value in row.items() if key != '_chain_candidates'} for row in candidates]
    caps = policy.limits(question) if hasattr(policy, 'limits') else policy()
    dynamic = hasattr(policy, 'demand')
    # 只有新策略允许资格读取方补入真实父正文；旧版本不执行外部拟合回调。
    fit_candidate = fit if dynamic and fit is not None else trim_candidate
    edges_by_id = {c["id"]: neighbors(c["id"]) for c in [*candidates, *methods] if c["layer"] == "L3"} if neighbors else {}
    refutes = {frozenset((anchor, edge["other_id"])) for anchor, edges in edges_by_id.items()
               for edge in edges if edge["kind"] == "refutes"}
    def duplicate(candidate):
        fitted = candidate if dynamic else trim_candidate(candidate, question)
        if fitted is None:
            return False
        if fitted["excerpt"] != candidate["excerpt"] or fitted.get("windows") != candidate.get("windows"):
            vectors.pop(candidate_identity(candidate), None)
        # 新需求或局部关系不能被词袋近似度当作重复合并。
        if dynamic and getattr(policy, 'dedup_gain', policy.relational_gain)(fitted, chosen, demand):
            return False
        compared = get('compose')(lambda candidate, rows, *, operation=None: rows,
                                  fitted, chosen, operation='dedup_comparison')
        if is_duplicate(fitted, compared, vectors=vectors, refutes=refutes):
            return True
        return False
    def measured_cost(rows):
        value = cost(rows)
        if type(value) is not int or value < 0:
            raise ValueError('invalid_ladder_admission_cost')
        return value
    def within_budget(rows):
        if evidence_tokens(rows) > evidence_budget:
            return False
        actual = measured_cost(rows) if dynamic and cost is not None else input_tokens(
            rows, prompt_question, reserve_refutes=neighbors is not None, history=history) + prompt_overhead
        return actual <= token_budget and (not dynamic or admission is None or admission(rows) is True)
    prompt_question = answer_question if answer_question is not None else question
    fixed_tokens = input_tokens([], prompt_question, reserve_refutes=neighbors is not None, history=history) + prompt_overhead
    if dynamic and cost is not None:
        fixed_tokens = measured_cost([])
    evidence_budget = max(0, token_budget - fixed_tokens) if dynamic else min(
        int(token_budget * caps['evidence_share']), max(0, token_budget - fixed_tokens))
    demand = policy.demand(prompt_question, reference_score=max((c.get('score', 0) for c in candidates), default=0), semantic_anchors=semantic_anchors) if dynamic else None
    if getattr(policy, 'semantic_rerank', False):
        demand['_retrieval_question'] = question
    bounded_discovery = (getattr(policy, 'bounded_discovery', False) and demand['relation'] == 'topic'
        and bool(policy.demand(question)['actors']))
    chain_ledger, chain_added = [], 0
    skipped_identities = set()
    def skip(candidate):
        key = (candidate["layer"], candidate.get("kind"), candidate["id"])
        fresh = key not in skipped_identities
        skipped_identities.add(key)
        return int(fresh)
    def admit(candidate):
        # 普通材料、邻居、方法和灵感都经过同一入口，在冻结之前共用条数预算。
        if evidence_limit is not None and len(chosen) >= evidence_limit:
            return None
        fitted = fit_candidate(candidate, question, **({'limit': evidence_budget, 'admission': lambda row: within_budget(chosen + [row])} if dynamic else {}))
        if fitted is None or not within_budget(chosen + [fitted]):
            return None
        return fitted
    def select_dynamic_legacy(pool):
        selected, skipped, duplicates = [], 0, 0
        available = [c for c in [*candidates, *methods] if c['layer'] == 'L3' and c.get('kind') == 'recognition']
        for candidate in pool:
            if any(candidate_identity(candidate) == candidate_identity(old) for old in chosen):
                continue
            if policy.progress(demand, chosen)['heuristic_sufficient']:
                break
            if policy.marginal(candidate, chosen, demand) <= 0:
                continue
            # 反驳闭包共同准入；预算或权限前沿不足时不能只留下其中一方。
            group, pending, seen = [], [candidate], set()
            missing_pair = False
            while pending:
                row = pending.pop(0)
                if candidate_identity(row) in seen or any(candidate_identity(row) == candidate_identity(old) and row.get('scope') == old.get('scope') for old in chosen):
                    continue
                seen.add(candidate_identity(row))
                group.append(row)
                for pair in refutes:
                    if row['layer'] != 'L3' or row.get('kind') != 'recognition' or len(pair) < 2 or row['id'] not in pair:
                        continue
                    other_id = next(identity for identity in pair if identity != row['id'])
                    other = next((item for item in available if item['id'] == other_id and item.get('scope') == row.get('scope')), None)
                    if other is None:
                        missing_pair = True
                    elif candidate_identity(other) not in seen:
                        pending.append({**other, 'expanded_from': candidate['id'], 'link_kind': 'refutes'})
            if missing_pair or evidence_limit is not None and len(chosen) + len(group) > evidence_limit:
                skipped += skip(candidate)
                continue
            fitted = []
            for row in group:
                value = fit_candidate(row, question, limit=evidence_budget,
                    admission=lambda item: within_budget(chosen + fitted + [item]))
                if value is not None and (value.get('windows') != row.get('windows') or value['excerpt'] != row['excerpt']):
                    vectors.pop(candidate_identity(row), None)
                fitted.append(value)
                if value is None:
                    break
            if any(row is None for row in fitted):
                skipped += skip(candidate)
                continue
            if hasattr(policy, 'post_fit_gain') and not any(policy.post_fit_gain(row, chosen, demand) for row in fitted):
                continue
            if any(duplicate(row) for row in fitted):
                duplicates += 1
                continue
            if not within_budget(chosen + fitted):
                skipped += skip(candidate)
                continue
            if len(fitted) > 1:
                fitted = [{**row, 'link_kind': 'refutes'} for row in fitted]
            chosen.extend(fitted)
            selected.extend(fitted)
        return selected, skipped, duplicates
    def select_dynamic(pool):
        nonlocal chain_added
        if not bounded_discovery or not pool or any(row['layer'] not in {'L1', 'L0'} for row in pool):
            return select_dynamic_legacy(pool)
        from .policies.ladder import project_sources_v14, representation_identity_v14, source_identity_v14
        # ledger只存已独立准入表示；来源union仅供真实成本与公开投影。
        if not chain_ledger:
            chain_ledger.extend(chosen)
        selected, skipped, duplicates = [], 0, 0
        chain_added = 0
        original_sources = {source_identity_v14(row) for row in chosen}
        for source in pool:
            for candidate in (source, *source.get('_chain_candidates', ())):
                chain = bool(candidate.get('_discovered_chain'))
                if any((representation_identity_v14(candidate) == representation_identity_v14(old) if chain
                        else candidate_identity(candidate) == candidate_identity(old)) for old in chain_ledger):
                    continue
                if policy.progress(demand, chosen)['heuristic_sufficient']:
                    break
                if policy.marginal(candidate, chain_ledger, demand) <= 0:
                    continue
                def projected(extra):
                    return project_sources_v14([*chain_ledger, *extra])
                if evidence_limit is not None and len(projected([candidate])) > evidence_limit:
                    if not chain:
                        skipped += skip(candidate)
                    continue
                value = fit_candidate(candidate, question, limit=evidence_budget,
                    admission=lambda row: within_budget(projected([row])))
                if value is None:
                    if not chain:
                        skipped += skip(candidate)
                    continue
                if not policy.post_fit_gain(value, chain_ledger, demand):
                    continue
                if not getattr(policy, 'dedup_gain', policy.relational_gain)(value, chain_ledger, demand):
                    if is_duplicate(value, chain_ledger, vectors={} if chain else vectors, refutes=refutes):
                        if not chain:
                            duplicates += 1
                        continue
                proposal = projected([value])
                if not within_budget(proposal):
                    if not chain:
                        skipped += skip(candidate)
                    continue
                chain_ledger.append(value)
                chosen[:] = proposal
                chain_added += 1
        selected = [row for row in chosen if source_identity_v14(row) not in original_sources]
        return selected, skipped, duplicates

    def select(pool, limit):
        if dynamic:
            return select_dynamic(pool)

        selected, skipped, duplicates = [], 0, 0
        for candidate in pool:
            if len(selected) == limit:
                break
            if duplicate(candidate):
                duplicates += 1
                continue
            fitted = admit(candidate)
            if fitted is None:
                skipped += skip(candidate)
            else:
                chosen.append(fitted)
                selected.append(fitted)
        return selected, skipped, duplicates
    detail = any(word in question or word in (answer_question or "") for word in _DETAIL)
    def steps():
        nonlocal demand
        connected = set()
        for layer, limit in caps['layers']:
            if layer == "L1":
                connected = {c["document_id"] for c in chosen if c["layer"] == "L2" and c.get("document_id")}
            elif layer == "L0":
                connected = {c["document_id"] for c in chosen if c.get("document_id") and not c.get("persona")}
            ordinary = [c for c in candidates if c["layer"] == layer and not c.get("persona") and not c.get("expansion_only")]
            ordinary.sort(key=lambda c: (not (set(c.get("document_ids", [])) | {c.get("document_id")}) & connected if layer != "L3" else False,
                                         c.get("overview_rank", float("inf")) if layer == "L2" else 0,
                                         *candidate_order(c)))
            ordinary = prefer_scene_ties(ordinary, score_key=lambda c: candidate_order(c)[0])
            if lower_order is not None and layer in {'L1', 'L0'}:
                ordinary = lower_order(ordinary)
            if layer_order is not None and (evidence_limit is None or len(chosen) < evidence_limit):
                # 连接和概览优先级保留硬分组；场景仍只在融合分数相同的时候优先。
                def priority(candidate):
                    return (not bool((set(candidate.get('document_ids', [])) | {candidate.get('document_id')}) & connected) if layer != 'L3' else False,
                            candidate.get('overview_rank', float('inf')) if layer == 'L2' else 0)
                ordinary = layer_order(layer, ordinary, priority=priority)
            if hasattr(policy, 'prepare_frontier'):
                demand = policy.prepare_frontier(ordinary, demand)
            selected, skipped_budget, duplicate_count = select(ordinary, limit)
            personas, selected_personas = [], []
            if layer == "L3":
                personas = sorted((c for c in candidates if c.get("persona") and not c.get("expansion_only")), key=candidate_order)
                selected_personas, persona_skipped, persona_duplicates = select(personas, caps['persona'])
                skipped_budget += persona_skipped
                duplicate_count += persona_duplicates
                if neighbors is not None:
                    pool = {c['id']: c for c in candidates if c['layer'] == 'L3'}
                    expansions = 0
                    for anchor in list(chosen):
                        edges = list(edges_by_id.get(anchor['id'], ()))
                        # Pair contradictions first, then spend at most two additional neighbor slots.
                        edges.sort(key=lambda e: (e['kind'] != 'refutes', -(e.get('score') or 0), e['other_id']))
                        for edge in edges:
                            kind, identity = edge['kind'], edge['other_id']
                            if kind not in {'related', 'supports', 'refutes'} or identity not in pool:
                                continue
                            if any(c['id'] == identity for c in chosen):
                                if kind == 'refutes':
                                    for c in chosen:
                                        if c['id'] == identity: c['link_kind'] = 'refutes'
                                continue
                            if not dynamic and kind != 'refutes' and expansions >= caps['neighbors']:
                                continue
                            other = {**pool[identity], 'expanded_from': anchor['id'], 'link_kind': kind}
                            if dynamic:
                                values, rejected, repeated = select_dynamic([other])
                                trace.append({'layer': 'L3', 'expanded_from': anchor['id'], 'id': identity,
                                    'link_kind': kind, 'considered': 1, 'selected': len(values),
                                    'skipped_budget': rejected, 'skipped_duplicate': repeated, 'coverage': 0, 'stopped': False})
                                continue
                            if duplicate(other):
                                trace.append({'layer': 'L3', 'expanded_from': anchor['id'], 'id': identity,
                                              'link_kind': kind, 'considered': 1, 'selected': 0, 'skipped_budget': 0,
                                              'skipped_duplicate': 1, 'coverage': 0, 'stopped': False})
                                continue
                            other = admit(other)
                            if other is None:
                                if not skip(pool[identity]):
                                    continue
                                trace.append({'layer': 'L3', 'expanded_from': anchor['id'], 'id': identity,
                                              'link_kind': kind, 'considered': 1, 'selected': 0, 'skipped_budget': 1,
                                              'coverage': 0, 'stopped': False})
                                continue
                            chosen.append(other)
                            if kind != 'refutes': expansions += 1
                            trace.append({'layer': 'L3', 'expanded_from': anchor['id'], 'id': identity,
                                          'link_kind': kind, 'considered': 1, 'selected': 1, 'skipped_budget': 0, 'coverage': 0, 'stopped': False})

            for candidate in selected:
                connected.update(candidate.get("document_ids", []))
                if candidate.get("document_id"):
                    connected.add(candidate["document_id"])
            evidence = "\n".join(c["excerpt"].casefold() for c in chosen if not c.get("persona"))
            coverage = weighted_coverage(terms, evidence)
            progress = policy.progress(demand, chosen) if dynamic else None
            stopped = (progress['heuristic_sufficient'] if dynamic else get('enough')(EnoughInput(
                evidence, coverage, detail, tuple(c['layer'] for c in chosen if not c.get('persona')))))
            trace.append({"layer": layer, "considered": len(ordinary) + len(personas),
                          "selected": len(selected) + len(selected_personas),
                          "skipped_budget": skipped_budget,
                          "coverage": coverage, "stopped": stopped})
            if dynamic:
                trace[-1]['stop_reason'] = ('heuristic_sufficient' if stopped else 'budget_exhausted'
                    if skipped_budget or evidence_limit is not None and len(chosen) >= evidence_limit else
                    'no_marginal_gain' if ordinary and not selected and (not bounded_discovery or not chain_added) else 'pool_exhausted')
                trace[-1]['missing'] = list(progress['missing'])
                if evidence_limit is not None and len(chosen) >= evidence_limit:
                    stopped = True
                    trace[-1]['stopped'] = True
            if duplicate_count:
                trace[-1]["skipped_duplicate"] = duplicate_count
            yield layer
            if stopped:
                break
        def no_methods(rows, instruction, situation, *, operation=None):
            return []
        from .policies.method_situation import MAX_METHODS
        pool = get('compose')(no_methods, methods, prompt_question, situation or question, operation='methods')
        pool.sort(key=lambda row: (row.get('previously_struck', False), *candidate_order(row)))
        added = 0
        for candidate in pool:
            if added >= MAX_METHODS:
                break
            if any(row['kind'] == candidate['kind'] and row['id'] == candidate['id'] for row in chosen):
                continue
            if duplicate(candidate):
                continue
            if dynamic:
                values, _, _ = select_dynamic([{**candidate, 'supplemented': True}])
                added += len(values)
                continue
            fitted = admit({**candidate, 'supplemented': True})
            if fitted is not None:
                chosen.append(fitted)
                added += 1
        rules = getattr(get('scope'), 'inspirations', None)
        if rules is not None:
            limit = rules(prompt_question)['limit']
            added = 0
            for candidate in sorted(inspirations, key=candidate_order):
                if added >= limit:
                    break
                if dynamic:
                    values, _, _ = select_dynamic([candidate])
                    added += len(values)
                    continue
                fitted = admit(candidate)
                if fitted is not None:
                    chosen.append(fitted)
                    added += 1
    def snapshot():
        # 实验评分资格只存在本轮闭包，不进入冻结计划或持久候选字段。
        rows = [{key: value for key, value in row.items() if key not in {
            '_semantic_rerank', '_original_vector_spans'}} for row in chosen] if getattr(
                policy, 'semantic_rerank', False) else list(chosen)
        if getattr(policy, 'bounded_discovery', False):
            rows = [{key: value for key, value in row.items() if key not in {
                '_discovery_chains', '_discovered_chain', '_chain_candidates'}} for row in rows]
        return {"chosen": rows, "trace": [dict(row) for row in trace], "budget": token_budget}
    def replace_lower(rows, questions, cached):
        nonlocal candidates, terms, demand
        candidates = [c for c in candidates if c['layer'] not in {'L1', 'L0'}] + list(rows)
        terms = coverage_terms(questions, query_terms)
        if getattr(policy, 'refresh_demand', False):
            semantic = {key: demand[key] for key in ('_retrieval_question', '_semantic_references') if key in demand}
            demand = policy.demand(prompt_question, reference_score=demand['reference_score'],
                semantic_anchors=tuple(dict.fromkeys((*semantic_anchors, *questions))))
            demand.update(semantic)
        vectors.update(cached)
    return LadderSelection(steps(), snapshot, replace_lower)
