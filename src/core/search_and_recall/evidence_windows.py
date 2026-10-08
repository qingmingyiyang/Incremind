"""Bounded, source-offset-preserving excerpts for current recall evidence."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass


_LATIN_STOP = {"the", "and", "for", "what", "does", "say", "about", "with"}
_CHINESE_STOP = {"什么", "哪些", "如何", "是否", "怎么", "当前"}
_JOIN = "\n…\n"


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    windows: tuple[EvidenceWindow, ...]
    excerpt: str
    score: int
    match_in: str


def split_evidence_chunks(
    content: str, min_chars: int = 400, max_chars: int = 800,
) -> tuple[EvidenceWindow, ...]:
    """Split exact source slices, preferring paragraphs and one-sentence overlap.

    Offsets count Unicode characters, including original whitespace. A final
    chunk may be shorter than ``min_chars``. A sentence that cannot fit with
    new text is hard-cut without overlap so every iteration makes progress.
    Boundaries depend only on source text and these limits, never on a query.
    """
    if (type(min_chars) is not int or type(max_chars) is not int
            or not 0 < min_chars <= max_chars):
        raise ValueError("chunk limits must be positive integers with min <= max")
    if not content:
        return ()
    sentence_ends = [0, *(match.end() for match in re.finditer(
        r'''(?:[。！？!?]+|(?<!\d)\.(?!\d))["'”’」』）)]*\s*''', content,
    ))]
    paragraph_ends = [match.end() for match in re.finditer(
        r"(?:\r?\n)[ \t]*(?:\r?\n)(?:[ \t]*(?:\r?\n))*", content,
    )]
    chunks: list[EvidenceWindow] = []
    start, previous_end = 0, 0
    while start < len(content):
        limit = min(len(content), start + max_chars)
        end = limit
        if limit < len(content):
            minimum = max(start + min_chars, previous_end + 1)
            for boundaries in (paragraph_ends, sentence_ends):
                index = bisect_right(boundaries, limit) - 1
                if index >= 0 and boundaries[index] >= minimum:
                    end = boundaries[index]
                    break
        chunks.append(EvidenceWindow(start, end, content[start:end]))
        if end == len(content):
            break
        # A boundary includes trailing whitespace, so the preceding boundary
        # starts exactly the final sentence even across blank lines.
        index = bisect_left(sentence_ends, end) - 1
        overlap_start = sentence_ends[index] if index >= 0 else start
        previous_end = end
        start = overlap_start if start < overlap_start and end - overlap_start < max_chars else end
    return tuple(chunks)


def select_evidence_windows(
    content: str, query: str, *, title: str = "", max_chars: int = 2400,
    max_windows: int = 3,
) -> EvidenceSelection:
    """Select exact slices of current content; offsets are Unicode character indices.

    A title-only hit returns bounded leading context, explicitly marked as such.
    Disjoint windows never include their intervening source text.
    """
    if max_chars <= 0 or max_windows <= 0:
        raise ValueError("evidence window budget must be positive")
    terms = _terms(query)
    if not terms:
        return EvidenceSelection((), "", 0, "none")
    folded, offsets = _fold_with_offsets(content)
    hits: list[tuple[int, int, int, int]] = []
    for term_index, (term, weight) in enumerate(terms):
        first: list[tuple[int, int, int, int]] = []
        last: deque[tuple[int, int, int, int]] = deque(maxlen=64)
        count = 0
        cursor = 0
        while (position := folded.find(term, cursor)) >= 0:
            start = offsets[position]
            end = offsets[position + len(term) - 1] + 1
            hit = (start, end, term_index, weight)
            if count < 64:
                first.append(hit)
            else:
                last.append(hit)
            count += 1
            cursor = position + max(1, len(term))
        # Bound scoring work while retaining both ends of a repetitive source.
        hits.extend(first)
        hits.extend(last)
    if not hits:
        title_folded = title.casefold()
        if any(term in title_folded for term, _weight in terms):
            end = min(len(content), max_chars)
            windows = (EvidenceWindow(0, end, content[:end]),) if end else ()
            return EvidenceSelection(windows, content[:end], 1, "title")
        return EvidenceSelection((), "", 0, "none")

    count = min(max_windows, max(1, max_chars // 64))
    size = max(1, min(800, (max_chars - len(_JOIN) * (count - 1)) // count))
    # The same hit set is scored for each greedy window. Index once by term;
    # within one term both original starts and ends increase monotonically.
    positions: list[list[tuple[int, int]]] = [[] for _ in terms]
    for start, end, term_index, _weight in hits:
        positions[term_index].append((start, end))
    starts_by_term: list[list[int]] = []
    ends_by_term: list[list[int]] = []
    for group in positions:
        group.sort()
        starts_by_term.append([start for start, _end in group])
        ends_by_term.append([end for _start, end in group])
    choices: list[tuple[int, int, int]] = []
    for match_start, match_end, _term_index, _weight in hits:
        left = max(0, min(match_start - size // 3, max(0, len(content) - size)))
        left = min(match_start, max(left, match_end - size))
        right = min(len(content), left + size)
        mask = 0
        for term_index, starts in enumerate(starts_by_term):
            first = bisect_left(starts, left)
            if first < len(starts) and ends_by_term[term_index][first] <= right:
                mask |= 1 << term_index
        choices.append((left, right, mask))
    selected: list[tuple[int, int]] = []
    uncovered = 0
    for _start, _end, term_index, _weight in hits:
        uncovered |= 1 << term_index
    covered_all = 0
    while uncovered and len(selected) < count:
        best: tuple[int, int, int, int, int, int] | None = None
        for left, right, mask in choices:
            if any(left < old_end and right > old_start for old_start, old_end in selected):
                continue
            covered = mask & uncovered
            if covered:
                weight = sum(term_weight for index, (_term, term_weight) in enumerate(terms)
                             if covered & (1 << index))
                candidate = (covered.bit_count(), weight, -left, left, right, covered)
                if best is None or candidate > best:
                    best = candidate
        if best is None:
            break
        _coverage, _weight, _order, left, right, covered = best
        selected.append((left, right))
        uncovered &= ~covered
        covered_all |= covered
    selected.sort()
    windows = tuple(EvidenceWindow(start, end, content[start:end]) for start, end in selected)
    excerpt = _JOIN.join(window.text for window in windows)
    if len(excerpt) > max_chars:
        raise AssertionError("evidence budget exceeded")
    matched = covered_all.bit_count()
    return EvidenceSelection(windows, excerpt, matched, "content")


def query_terms(query: str) -> tuple[tuple[str, int], ...]:
    ordered: dict[str, int] = {}
    phrase = query.strip().casefold()
    if 2 <= len(phrase) <= 200 and not re.search(r"[?？!！。]", phrase):
        ordered[phrase] = 5
    for word in re.findall(r"[^\W_]+", phrase):
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            continue
        if word not in _LATIN_STOP:
            ordered[word] = max(ordered.get(word, 0), 2)
    for run in re.findall(r"[\u4e00-\u9fff]+", phrase):
        if len(run) == 1:
            ordered[run] = max(ordered.get(run, 0), 1)
        for index in range(len(run) - 1):
            part = run[index:index + 2]
            if part not in _CHINESE_STOP:
                ordered[part] = max(ordered.get(part, 0), 1)
    if not ordered and phrase and not phrase.isspace():
        ordered[phrase] = 1
    return tuple(ordered.items())[:32]


_terms = query_terms


def _fold_with_offsets(content: str) -> tuple[str, list[int]]:
    pieces: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(content):
        folded = char.casefold()
        pieces.append(folded)
        offsets.extend([index] * len(folded))
    return "".join(pieces), offsets
