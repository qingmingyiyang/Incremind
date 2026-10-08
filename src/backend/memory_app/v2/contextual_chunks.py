"""Rank contextual chunks while returning only exact parent-material slices."""

from core.search_and_recall.evidence_windows import (
    EvidenceSelection, query_terms, select_evidence_windows, split_evidence_chunks,
)


def select_contextual_windows(content, question, *, title="", summary="", max_chars=10800,
                              chunks=None, vector_scores=None):
    chunks = split_evidence_chunks(content) if chunks is None else chunks
    vectors = vector_scores or {}
    original = None
    if len(content) <= 800:
        original = select_evidence_windows(content, question, title=title, max_chars=max_chars)
        if original.score or not vectors:
            return original
    return _rank_chunks(chunks, question, title, summary, vectors, max_chars, original)


def select_indexed_contextual_windows(length, question, *, chunks, title='', summary='',
                                      vector_scores=None, max_chars=10800):
    """Consume write-time chunks without reconstructing their long parent text."""
    if length <= 800:
        content = chunks[0].text if chunks else ''
        return select_contextual_windows(content, question, title=title, summary=summary,
            chunks=chunks, vector_scores=vector_scores, max_chars=max_chars)
    return _rank_chunks(chunks, question, title, summary, vector_scores or {}, max_chars, None)


def _rank_chunks(chunks, question, title, summary, vectors, max_chars, original):
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
        if not matched and vector <= 0:
            continue
        score = .45 * lexical + .55 * vector if vectors else lexical
        weight = sum(weight for term, weight in terms if term in body_terms)
        ranked.append((score, len(body_terms), weight, -index, chunk, matched))
    if not ranked:
        return original or EvidenceSelection((), "", 0, "none")
    _score, body_count, _weight, _order, best, matched = max(ranked, key=lambda item: item[:4])
    # Hit chunks form bounded neighbourhoods. Prefixes rank them but are not
    # evidence, and its original offsets remain valid through citation drilldown.
    if len(best.text) > max_chars:
        selected = select_evidence_windows(best.text, question, title=title,
                                           max_chars=max_chars, max_windows=1)
        windows = tuple(type(w)(w.start + best.start, w.end + best.start, w.text) for w in selected.windows)
        return EvidenceSelection(windows, selected.excerpt, selected.score, selected.match_in)
    selected = [best]
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
        covered.update(own_terms)
        used += 3 + len(chunk.text)
    selected.sort(key=lambda window: window.start)
    return EvidenceSelection(tuple(selected), "\n…\n".join(w.text for w in selected),
                             max(1, len(covered)), "content" if body_count or vectors else "title")
