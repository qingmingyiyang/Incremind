"""Version one retains startup delay and the scheduler's fixed interval."""
from .types import TriggerInput


def v1(request: TriggerInput) -> float:
    return request.initial_delay if request.initial else request.interval


def v2(request=None, *, operation='delay', score=0, run_seconds=0, elapsed=0, check_interval=60):
    if operation == 'due':
        return score >= 10 and run_seconds >= 7200
    if operation == 'elapsed':
        return min(max(0, elapsed), 2 * check_interval)
    if operation == 'points':
        return 1
    if operation == 'check':
        return min(check_interval, request.interval)
    return v1(request)
