"""Version one of the current auto-recall state decision."""
from .types import ForgetInput


def v1(request: ForgetInput) -> str:
    return ('forgotten' if request.kind == 'insight' and request.project_id != 'me' and request.score < .0625
            else 'cooled' if request.score < .25
            else 'normal' if request.previous == 'cooled' and request.score >= .5 and request.can_recover
            else request.previous)
