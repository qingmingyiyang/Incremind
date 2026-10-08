"""本机合并未解答的问题并判断到期，不依赖模型或存储。"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from .reask import _normalized, _terms, OVERLAP_THRESHOLD
from .enough import coverage_terms, weighted_coverage

_TRACE = object()


@dataclass(frozen=True)
class GapPolicy:
    expiry_days: int = 90
    coverage_threshold: float = .6
    overlap_threshold: float = OVERLAP_THRESHOLD

    def related(self, first, second):
        if set(re.findall(r'\d+', first)) != set(re.findall(r'\d+', second)):
            return False
        left, right = _terms(first), _terms(second)
        return bool(left and right and (_normalized(first) == _normalized(second)
            or len(left & right) / min(len(left), len(right)) >= self.overlap_threshold))

    def coverage(self, wordings, evidence, terms_for):
        return weighted_coverage(coverage_terms(wordings, terms_for), evidence.casefold())

    def insufficient(self, answer, *, coverage=_TRACE):
        if not isinstance(answer, dict):
            return None
        citations = answer.get('citations')
        if answer.get('no_match') is True or isinstance(citations, list) and not citations:
            return True
        if not isinstance(citations, list):
            return None
        if coverage is _TRACE:
            trace = answer.get('trace')
            if not isinstance(trace, list) or not trace or not isinstance(trace[-1], dict):
                return None
            coverage = trace[-1].get('coverage')
        if type(coverage) not in {float, int} or not 0 <= coverage <= 1:
            return None
        return coverage < self.coverage_threshold

    def covered(self, result):
        if not isinstance(result, dict) or result.get('status') != 'known':
            return False
        coverage = result.get('coverage')
        return type(coverage) in {int, float} and self.coverage_threshold <= coverage <= 1

    def __call__(self, items, *, now, dismissed=(), enabled=True):
        if not enabled:
            return []
        return sorted((item for item in items if item['id'] not in dismissed
            and item['unresolved'] and datetime.fromisoformat(item['last_at']) <= now
            < datetime.fromisoformat(item['last_at']) + timedelta(days=self.expiry_days)),
            key=lambda item: (item['last_at'], item['id']), reverse=True)


v1 = GapPolicy()
