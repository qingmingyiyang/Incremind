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


def plan_ladder(candidates, question, *, neighbors=None, token_budget=4000, prompt_overhead=0, history="", answer_question=None, coverage_questions=None, cached_vectors=None, methods=(), situation=None, inspirations=(), evidence_limit=None):
    return start_ladder(candidates, question, neighbors=neighbors, token_budget=token_budget,
        prompt_overhead=prompt_overhead, history=history, answer_question=answer_question,
        coverage_questions=coverage_questions, cached_vectors=cached_vectors,
        methods=methods, situation=situation, inspirations=inspirations, evidence_limit=evidence_limit).advance()


def start_ladder(candidates, question, *, neighbors=None, token_budget=4000, prompt_overhead=0, history="", answer_question=None, coverage_questions=None, cached_vectors=None, methods=(), situation=None, lower_order=None, inspirations=(), evidence_limit=None):
    if evidence_limit is not None and (type(evidence_limit) is not int or evidence_limit < 0):
        raise ValueError('invalid_ladder_evidence_limit')
    candidates = [c for c in candidates if not c.get("persona")]
    terms = coverage_terms(coverage_questions or [question], query_terms)
    chosen, trace = [], []
    vectors = dict(cached_vectors or {})
    edges_by_id = {c["id"]: neighbors(c["id"]) for c in [*candidates, *methods] if c["layer"] == "L3"} if neighbors else {}
    refutes = {frozenset((anchor, edge["other_id"])) for anchor, edges in edges_by_id.items()
               for edge in edges if edge["kind"] == "refutes"}
    def duplicate(candidate):
        fitted = trim_candidate(candidate, question)
        if fitted is None:
            return False
        if fitted["excerpt"] != candidate["excerpt"] or fitted.get("windows") != candidate.get("windows"):
            vectors.pop(candidate_identity(candidate), None)
        compared = get('compose')(lambda candidate, rows, *, operation=None: rows,
                                  fitted, chosen, operation='dedup_comparison')
        if is_duplicate(fitted, compared, vectors=vectors, refutes=refutes):
            return True
        return False
    prompt_question = answer_question if answer_question is not None else question
    fixed_tokens = input_tokens([], prompt_question, reserve_refutes=neighbors is not None, history=history) + prompt_overhead
    evidence_budget = min(int(token_budget * 0.8), max(0, token_budget - fixed_tokens))
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
        fitted = trim_candidate(candidate, question)
        if fitted is None:
            return None
        proposed = chosen + [fitted]
        if (evidence_tokens(proposed) > evidence_budget
                or input_tokens(proposed, prompt_question, reserve_refutes=neighbors is not None, history=history) + prompt_overhead > token_budget):
            return None
        return fitted
    def select(pool, limit):
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
        connected = set()
        for layer, limit in (("L3", 3), ("L2", 2), ("L1", 2), ("L0", 2)):
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
            selected, skipped_budget, duplicate_count = select(ordinary, limit)
            personas, selected_personas = [], []
            if layer == "L3":
                personas = sorted((c for c in candidates if c.get("persona") and not c.get("expansion_only")), key=candidate_order)
                selected_personas, persona_skipped, persona_duplicates = select(personas, 2)
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
                            if kind != 'refutes' and expansions >= 2:
                                continue
                            other = {**pool[identity], 'expanded_from': anchor['id'], 'link_kind': kind}
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
            stopped = get('enough')(EnoughInput(evidence, coverage, detail,
                tuple(c['layer'] for c in chosen if not c.get('persona'))))
            trace.append({"layer": layer, "considered": len(ordinary) + len(personas),
                          "selected": len(selected) + len(selected_personas),
                          "skipped_budget": skipped_budget,
                          "coverage": coverage, "stopped": stopped})
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
            if added == MAX_METHODS:
                break
            if any(row['kind'] == candidate['kind'] and row['id'] == candidate['id'] for row in chosen):
                continue
            if duplicate(candidate):
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
                if added == limit:
                    break
                fitted = admit(candidate)
                if fitted is not None:
                    chosen.append(fitted)
                    added += 1
    def snapshot():
        return {"chosen": list(chosen), "trace": [dict(row) for row in trace], "budget": token_budget}
    def replace_lower(rows, questions, cached):
        nonlocal candidates, terms
        candidates = [c for c in candidates if c['layer'] not in {'L1', 'L0'}] + list(rows)
        terms = coverage_terms(questions, query_terms)
        vectors.update(cached)
    return LadderSelection(steps(), snapshot, replace_lower)
