"""Version one of the current weighted coverage stop condition."""
from .types import EnoughInput


def v1(request: EnoughInput) -> bool:
    return (bool(request.evidence) and request.coverage >= .6
            and (not request.detail or any(layer in {'L1', 'L0'} for layer in request.layers)))


def coverage_terms(wordings, terms_for):
    """合并原问题的词权重，并保留词项首次出现的顺序。"""
    weighted = {}
    for wording in wordings:
        for term, value in terms_for(wording):
            weighted[term] = max(weighted.get(term, 0), value)
    return list(weighted.items())


def weighted_coverage(terms, evidence):
    weight = sum(value for _, value in terms)
    return sum(value for term, value in terms if term in evidence) / weight if weight else 0.0
