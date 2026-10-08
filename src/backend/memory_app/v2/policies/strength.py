"""Version one of the existing exponential half-life formula."""
import math

from .types import StrengthInput, StrengthOutput


def v1(request: StrengthInput) -> StrengthOutput:
    half_life = 30 * (1 + math.log1p(request.count)) * (3 if request.project_id == 'me' else 1)
    score = None
    if request.score is not None:
        days = max(0, (request.now - request.updated_at).total_seconds() / 86400)
        score = request.score * .5 ** (days / half_life)
    return StrengthOutput(half_life, score)
