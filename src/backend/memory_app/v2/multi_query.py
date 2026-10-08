"""One guarded multi-query expansion with exact reciprocal-rank fusion."""

import asyncio
from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from backend.recognition import RecognitionError
from .followup import auxiliary_call
from .budget import input_tokens
from .policies import get, override


class QueryVariants(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[StrictStr] = Field(max_length=3)


def _identity(candidate):
    return candidate["scope"].project_id, candidate["kind"], candidate["layer"], candidate["id"]


def fuse_candidates(lists):
    candidates, scores = {}, {}
    for rows in lists:
        seen, rank = set(), 0
        for row in rows:
            identity = _identity(row)
            if identity in candidates:
                previous = candidates[identity]
                if previous["entry"]["revision"] != row["entry"]["revision"]:
                    raise RecognitionError("rewrite_source_changed")
                if previous.get("coordinate_space") == row.get("coordinate_space") and row["kind"] != "recognition":
                    windows = {(w.start, w.end, w.text): w for w in previous["windows"] + row["windows"]}
                    merged = tuple(sorted(windows.values(), key=lambda w: (w.start, w.end)))
                    previous = {**previous, "windows": merged, "excerpt": "\n…\n".join(w.text for w in merged)}
                candidates[identity] = {
                    **previous,
                    "expansion_only": bool(previous.get("expansion_only") and row.get("expansion_only")),
                }
            else:
                candidates[identity] = dict(row)
            if identity in seen or row.get("expansion_only"):
                continue
            seen.add(identity)
            rank += 1
            scores[identity] = scores.get(identity, Fraction()) + Fraction(1, 60 + rank)
    rescore = getattr(get('rank'), 'rescore', None)
    if callable(rescore):
        scores = {identity: score * Fraction(rescore(1, candidates[identity])) for identity, score in scores.items()}
    ranks = {score: rank for rank, score in enumerate(sorted(set(scores.values()), reverse=True))}
    return [
        {**row, "score": float(scores[identity]), "rrf_rank": ranks[scores[identity]]} if identity in scores else row
        for identity, row in candidates.items()
    ]


async def expand_plan(query, project, question, plan, collected, *, turn_id, scene=None):
    observation = {"usage": {}, "receipt_ids": [], "status": "skipped", "usage_complete": True}
    rewrite = {"queries": [], "used": False}
    fixed = input_tokens([], plan["question"], reserve_refutes=True, history=plan.get("history", "")) + plan.get(
        "prompt_overhead", 0
    )
    if fixed > plan["budget"] or (plan["chosen"] and plan["trace"][-1]["coverage"] >= 0.6):
        return plan, observation, rewrite
    messages = [
        {
            "role": "system",
            "content": '为检索资料生成最多3个用词不同但含义相同的问法，不回答问题。只返回JSON {"queries":["问法"]}。',
        },
        {"role": "user", "content": question},
    ]
    observation = await auxiliary_call(
        query, project, messages, QueryVariants, turn_id=turn_id, validate_current=lambda: query.validate_ask_plan(plan)
    )
    output = observation.pop("output")
    if output is None:
        return plan, observation, rewrite
    queries = []
    for value in output.queries:
        value = value.strip()
        if not value or len(value) > 1000:
            observation["status"] = "failed"
            return plan, observation, rewrite
        if value.casefold() != question.casefold() and value.casefold() not in {q.casefold() for q in queries}:
            queries.append(value)
    if not queries:
        return plan, observation, rewrite
    try:
        lists, excluded = [collected["candidates"]], list(collected["excluded_sources"])
        for result in await _collect_variants(query, project, queries, scene=scene,
                rank_reference=collected.get('rank_reference')):
            lists.append(result["candidates"])
            excluded.extend(result["excluded_sources"])
        fused = {"candidates": fuse_candidates(lists), "excluded_sources": excluded[:20]}
        fused.update({key: collected[key] for key in ('policy_versions', 'rank_reference') if key in collected})
        if 'method_candidates' in collected:
            # A rewrite never changes the original task situation or its scope.
            fused['method_candidates'] = collected['method_candidates']
        if 'inspiration_candidates' in collected:
            fused['inspiration_candidates'] = collected['inspiration_candidates']
        if 'policy_versions' in collected:
            fused['policy_versions'] = collected['policy_versions']
        if collected.get('time_scope'):
            fused.update({key: collected[key] for key in ('time_scope', 'time_documents')})
        result = query.prepare_ask(
            project,
            plan["question"],
            scene=scene,
            retrieval_question=question,
            history=plan.get("history", ""),
            collected=fused,
            coverage_questions=[question, *queries],
        )
        if "history_guard" in plan:
            result["history_guard"] = plan["history_guard"]
        return result, observation, {"queries": queries, "used": True}
    except Exception:
        observation["status"] = "failed"
        return plan, observation, rewrite


def _drilldown_input(project, question, plan, scene):
    pending = plan['_drilldown']
    if project != plan['project_id'] or question != pending['retrieval_question']:
        raise RecognitionError('drilldown_question_changed')
    if scene is not None and scene != pending['scene']:
        raise RecognitionError('drilldown_scene_changed')
    return pending


async def expand_drilldown(query, project, question, plan, collected, queries, *, scene=None):
    """Fuse supplied gap queries into lower layers; this function sends no aux."""
    rewrite = {'queries': [], 'used': False}
    pending = _drilldown_input(project, question, plan, scene)
    with override(**plan['policy_versions']):
        if not plan['drilldown_needed']:
            return query.resume_drilldown(plan), rewrite
        try:
            variants = get('retrieve')(None, question, {'queries': list(queries)}, operation='gap_queries')
            if not variants:
                return query.resume_drilldown(plan), rewrite
            query.validate_ask_plan(plan)
            original = pending['collected']
            results = await _collect_variants(query, project, variants, scene=pending['scene'],
                collector=query.collect_lower_candidates, policy_versions=plan['policy_versions'],
                rank_reference=original.get('rank_reference'),
                time_scope=plan.get('time_scope'), time_documents=plan.get('time_documents'),
                time_candidates=[row for row in original['candidates'] if row['layer'] == 'L3'])
            lists = [[row for row in original['candidates'] if row['layer'] in {'L1', 'L0'}]]
            excluded = list(plan['excluded_sources'])
            for result in results:
                lists.append(result['candidates'])
                excluded.extend(result['excluded_sources'])
            fused = {**original, 'candidates': fuse_candidates(lists), 'excluded_sources': excluded[:20]}
        except Exception:
            # Collection/decode/fusion failures leave the paused selection intact.
            return query.resume_drilldown(plan), rewrite
        result = query.resume_drilldown(plan, collected=fused, queries=variants)
        return result, {'queries': list(variants), 'used': True}


async def expand_gap_plan(query, project, question, plan, collected, *, turn_id, scene=None):
    """Use the original rewrite slot for one material-bound gap, then resume."""
    _drilldown_input(project, question, plan, scene)
    observation = {'usage': {}, 'receipt_ids': [], 'status': 'skipped', 'usage_complete': True}
    if not plan['drilldown_needed'] or not plan['chosen']:
        result, rewrite = await expand_drilldown(query, project, question, plan, collected, (), scene=scene)
        return result, observation, rewrite
    observation = await auxiliary_call(query, project, None, QueryVariants, turn_id=turn_id,
        validate_current=lambda: query.validate_ask_plan(plan), gap_plan=plan)
    output = observation.pop('output')
    variants = ()
    if output is not None:
        with override(**plan['policy_versions']):
            try:
                variants = get('retrieve')(None, question, output.model_dump(), operation='gap_queries')
            except (ValueError, TypeError):
                observation['status'] = 'failed'
    result, rewrite = await expand_drilldown(query, project, question, plan, collected, variants, scene=scene)
    return result, observation, rewrite


async def _collect_variants(query, project, variants, *, scene=None, collector=None, rank_reference=None, **options):
    """Keep input order and settle real workers before fallback or cancellation."""
    if rank_reference is not None:
        options['rank_reference'] = rank_reference
    pending = asyncio.gather(*(
        asyncio.to_thread(collector or query.collect_candidates, project, wording, scene=scene, **options)
        for wording in variants
    ), return_exceptions=True)
    try:
        results = await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Cancelling to_thread cannot stop its running SQLite/provider work.
        # Retain the calling request's resources until those workers finish.
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
        raise
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results
