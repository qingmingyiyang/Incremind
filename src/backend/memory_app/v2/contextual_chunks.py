"""Rank contextual chunks while returning only exact parent-material slices."""

from dataclasses import replace

from core.search_and_recall.evidence_windows import (
    EvidenceSelection, EvidenceWindow, query_terms, select_evidence_windows, split_evidence_chunks,
)
from .policies import get


def select_contextual_windows(content, question, *, title="", summary="", max_chars=None,
                              chunks=None, vector_scores=None):
    chunks = split_evidence_chunks(content) if chunks is None else chunks
    if hasattr(get('ladder'), 'demand'):
        return dynamic_contextual_windows(content, question, title=title, summary=summary,
            chunks=chunks, vector_scores=vector_scores, max_chars=max_chars)
    max_chars = 10800 if max_chars is None else max_chars
    vectors = vector_scores or {}
    contextual = getattr(get('rank'), 'contextual', None)
    if contextual is not None:
        return _contextual_selection(contextual, chunks, question, title, summary, vectors, max_chars)
    original = None
    if len(content) <= 800:
        original = select_evidence_windows(content, question, title=title, max_chars=max_chars)
        meaning = getattr(get('rank'), 'meaning', None)
        if not vectors or (original.score and meaning is None):
            return original
        if original.score:
            # Meaning orders keyword hits with equal counts across materials.
            return replace(original, score=meaning(original.score, max(vectors.values())))
    return _rank_chunks(chunks, question, title, summary, vectors, max_chars, original)


def select_indexed_contextual_windows(length, question, *, chunks, title='', summary='',
                                      vector_scores=None, max_chars=None, content=None):
    """Consume write-time chunks without reconstructing their long parent text."""
    if hasattr(get('ladder'), 'demand'):
        if not isinstance(content, str) or len(content) != length:
            raise ValueError('dynamic_parent_content_required')
        return dynamic_contextual_windows(content, question, title=title, summary=summary,
            chunks=chunks, vector_scores=vector_scores, max_chars=max_chars)
    max_chars = 10800 if max_chars is None else max_chars
    if length <= 800:
        content = chunks[0].text if chunks else ''
        return select_contextual_windows(content, question, title=title, summary=summary,
            chunks=chunks, vector_scores=vector_scores, max_chars=max_chars)
    return _rank_chunks(chunks, question, title, summary, vector_scores or {}, max_chars, None)


def _rank_chunks(chunks, question, title, summary, vectors, max_chars, original):
    contextual = getattr(get('rank'), 'contextual', None)
    if contextual is not None:
        return _contextual_selection(contextual, chunks, question, title, summary, vectors, max_chars)
    # rank@3 adds keyword-and-meaning recall; earlier versions keep the floor score.
    meaning = getattr(get('rank'), 'meaning', None)
    terms = query_terms(question)
    prefix = (title + "\n" + summary[:120]).casefold()
    context_terms = {term for term, _ in terms if term in prefix}
    ranked = []
    for index, chunk in enumerate(chunks):
        folded = chunk.text.casefold()
        body_terms = {term for term, _ in terms if term in folded}
        matched = context_terms | body_terms
        lexical = len(matched) / len(terms) if terms else 0.0
        vector = vectors.get(index, 0.0)
        # A chunk without any matched term needs enough meaning on its own.
        if not matched and (vector <= 0 if meaning is None else meaning(0, vector if vectors else None) is None):
            continue
        score = .45 * lexical + .55 * vector if vectors else lexical
        weight = sum(weight for term, weight in terms if term in body_terms)
        ranked.append((score, len(body_terms), weight, -index, chunk, matched))
    if not ranked:
        return original or EvidenceSelection((), "", 0, "none")
    _score, body_count, _weight, order, best, matched = max(ranked, key=lambda item: item[:4])
    # Hit chunks form bounded neighbourhoods. Prefixes rank them but are not
    # evidence, and its original offsets remain valid through citation drilldown.
    if len(best.text) > max_chars:
        selected = select_evidence_windows(best.text, question, title=title,
                                           max_chars=max_chars, max_windows=1)
        windows = tuple(type(w)(w.start + best.start, w.end + best.start, w.text) for w in selected.windows)
        return EvidenceSelection(windows, selected.excerpt, selected.score, selected.match_in)
    selected = [best]
    indexes = [-order]
    covered = set(matched)
    used = len(best.text)
    # Preserve the existing disjoint-evidence contract: additional blocks must
    # contribute previously uncovered query terms, not repeat the shared prefix.
    for item in sorted(ranked, key=lambda item: item[:4], reverse=True):
        chunk, own_terms = item[4], item[5]
        if len(selected) == 3:
            break
        if not (own_terms - covered):
            continue
        if any(chunk.start < old.end and chunk.end > old.start for old in selected):
            continue
        if used + 3 + len(chunk.text) > max_chars:
            continue
        selected.append(chunk)
        indexes.append(-item[3])
        covered.update(own_terms)
        used += 3 + len(chunk.text)
    selected.sort(key=lambda window: window.start)
    # Keyword count leads; the closest selected block's meaning orders ties across
    # materials and ranks meaning-only material below every keyword hit.
    similarity = max(vectors.get(index, 0.0) for index in indexes) if vectors else None
    score = max(1, len(covered)) if meaning is None else meaning(len(covered), similarity) or 1
    return EvidenceSelection(tuple(selected), "\n…\n".join(w.text for w in selected), score,
                             "content" if body_count or vectors else "title")


def _contextual_selection(policy, chunks, question, title, summary, vectors, max_chars):
    def trim(chunk, room):
        # 小预算仍围绕真实命中取窗，不能给无关前缀沿用整块分数。
        result = select_evidence_windows(chunk.text, question, title=title, max_chars=room, max_windows=1)
        if result.windows:
            window = result.windows[0]
            return replace(chunk, start=chunk.start+window.start, end=chunk.start+window.end, text=window.text)
        return None
    selected, score, match_in = policy(chunks, query_terms(question), title, summary, vectors, max_chars, trim)
    return EvidenceSelection(tuple(selected), "\n…\n".join(w.text for w in selected), score, match_in)



def dynamic_contextual_windows(content, question, *, title='', summary='', chunks=None,
                               vector_scores=None, max_chars=None, seeds=None, admission=None):
    """从真实种子逐次补全原记录；远处命中仍保留独立连续链。"""
    policy = get('ladder')
    if not hasattr(policy, 'context_ranges'):
        raise ValueError('dynamic_ladder_policy_required')
    if max_chars is not None and (type(max_chars) is not int or max_chars < 0):
        raise ValueError('invalid_dynamic_context_budget')
    chunks = tuple(split_evidence_chunks(content) if chunks is None else chunks)
    candidates = tuple(seeds) if seeds is not None else chunks
    for window in candidates:
        if (type(window.start) is not int or type(window.end) is not int
                or not 0 <= window.start < window.end <= len(content)
                or window.text != content[window.start:window.end]):
            raise ValueError('dynamic_context_coordinates_changed')
    if hasattr(policy, 'context_spans'):
        return _demand_contextual_windows(policy, content, question, candidates,
            title, summary, vector_scores or {}, max_chars, admission)
    if hasattr(policy, 'seed_scores'):
        return _frontier_contextual_windows(policy, content, question, candidates,
            title, summary, vector_scores or {}, max_chars, admission)
    ranges = policy.context_ranges(content)
    order = range(len(candidates)) if seeds is not None else policy.seed_order(
        candidates, question, title, summary, vector_scores or {})
    selected = []
    demand = policy.demand(question)
    for index in order:
        seed = candidates[index]
        touched = [(a, b) for a, b in ranges if a < seed.end and b > seed.start]
        if not touched:
            continue
        proposed = sorted([*( (w.start, w.end) for w in selected),
                           (touched[0][0], touched[-1][1])])
        merged = []
        for start, end in proposed:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        windows = tuple(EvidenceWindow(start, end, content[start:end]) for start, end in merged)
        excerpt = '\n…\n'.join(window.text for window in windows)
        if max_chars is not None and len(excerpt) > max_chars:
            continue
        result = EvidenceSelection(windows, excerpt, 1, 'content')
        if admission is not None and not admission(result):
            continue
        selected = list(windows)
        if policy.progress(demand, [{'excerpt': excerpt, 'layer': 'L0'}])['heuristic_sufficient']:
            break
    windows = tuple(selected)
    if not windows:
        return EvidenceSelection((), '', 0, 'none')
    # 扩窗只决定原文范围；相关性仍由原 rank 的纯排序计算，保留关键词和向量分差。
    indexes = [index for index, chunk in enumerate(candidates)
        if any(window.start <= chunk.start and chunk.end <= window.end for window in windows)]
    scored = _rank_chunks(tuple(candidates[index] for index in indexes), question, title, summary,
        {position: vector_scores[index] for position, index in enumerate(indexes)
         if vector_scores is not None and index in vector_scores},
        sum(len(candidates[index].text) + 3 for index in indexes), None)
    return EvidenceSelection(windows, '\n…\n'.join(window.text for window in windows),
                             scored.score, scored.match_in)



def _frontier_contextual_windows(policy, content, question, candidates, title, summary,
                                 vectors, max_chars, admission):
    from bisect import bisect_right, bisect_left
    ranges = policy.context_ranges(content)
    starts = tuple(start for start, _ in ranges)
    ends = tuple(end for _, end in ranges)
    selected, accepted = [], []
    demand = policy.demand(question)
    for score, index, similarity in policy.seed_scores(candidates, question, title, summary, vectors):
        seed = candidates[index]
        first = bisect_right(ends, seed.start)
        last = bisect_left(starts, seed.end)-1
        if first >= len(ranges) or last < first:
            continue
        start, end = policy.context_neighborhood(content, ranges, first, last)
        if any(window.start <= start and end <= window.end for window in selected):
            continue
        row = {'excerpt': content[start:end], 'layer': 'L0', 'score': score, 'vector_score': similarity}
        if policy.marginal(row, accepted, demand) <= 0:
            continue
        spans = sorted([*((window.start, window.end) for window in selected), (start, end)])
        merged = []
        for left, right in spans:
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
            else:
                merged.append((left, right))
        windows = tuple(EvidenceWindow(left, right, content[left:right]) for left, right in merged)
        excerpt = '\n…\n'.join(window.text for window in windows)
        if max_chars is not None and len(excerpt) > max_chars:
            continue
        value = EvidenceSelection(windows, excerpt, max([score, *(old['score'] for old in accepted)]), 'content')
        if admission is not None and not admission(value):
            continue
        selected, accepted = list(windows), [*accepted, row]
        if policy.progress(demand, accepted)['heuristic_sufficient']:
            break
    windows = tuple(selected)
    return EvidenceSelection(windows, '\n…\n'.join(window.text for window in windows),
        max((row['score'] for row in accepted), default=0), 'content' if windows else 'none')



def _demand_contextual_windows(policy, content, question, candidates, title, summary,
                               vectors, max_chars, admission):
    from bisect import bisect_right, bisect_left
    ranges = policy.context_ranges(content)
    starts, ends = tuple(a for a, _ in ranges), tuple(b for _, b in ranges)
    selected, accepted = [], []
    demand = policy.demand(question)
    for score, index, similarity in policy.seed_scores(candidates, question, title, summary, vectors):
        seed = candidates[index]
        first, last = bisect_right(ends, seed.start), bisect_left(starts, seed.end)-1
        if first >= len(ranges) or last < first:
            continue
        for start, end, bound_similarity in policy.context_spans(
                content, ranges, first, last, seed, question, score, similarity):
            if any(w.start <= start and end <= w.end for w in selected):
                continue
            row = {'excerpt': content[start:end], 'layer': 'L0', 'score': score,
                   'vector_score': bound_similarity, '_span': (start, end)}
            if policy.marginal(row, accepted, demand) <= 0:
                continue
            retained = policy.refine_context(accepted, row, demand)
            merged = []
            for left, right in sorted([*(old['_span'] for old in retained), (start, end)]):
                if merged and left <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                else:
                    merged.append((left, right))
            windows = tuple(EvidenceWindow(a, b, content[a:b]) for a, b in merged)
            excerpt = '\n…\n'.join(w.text for w in windows)
            value = EvidenceSelection(windows, excerpt, max([score, *(r['score'] for r in retained)]), 'content')
            if max_chars is not None and len(excerpt) > max_chars or admission is not None and not admission(value):
                continue
            selected, accepted = list(windows), [*retained, row]
            if policy.progress(demand, accepted)['heuristic_sufficient']:
                break
        if policy.progress(demand, accepted)['heuristic_sufficient']:
            break
    windows = tuple(selected)
    return EvidenceSelection(windows, '\n…\n'.join(w.text for w in windows),
        max((row['score'] for row in accepted), default=0), 'content' if windows else 'none')
