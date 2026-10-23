"""按冻结领域策略投影资源额度，保持原注册表和权限不变。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from ..v2.policies import get

_BUDGET = ContextVar('product_answer_handle_budget', default=3)
_TIMEOUT = ContextVar('product_answer_timeout_ms', default=240_000)


def answer_lifecycle(request):
    selected = (request.get('policy_versions') or {}).get('retry', '@1')
    if selected == '@1':
        return {'tool_timeout_ms': 240_000, 'wait_timeout_seconds': 250}
    return get('retry', version=selected)({'kind': 'answer_lifecycle'})


@contextmanager
def answer_contract_scope(request):
    budget = 3
    timeout = 240_000
    if (isinstance(request, dict) and request.get('desired_outcome') == 'project.answer'
            and (request.get('policy_versions') or {}).get('compose') in {'@14', '@15', '@16', '@17', '@18', '@19', '@20', '@21', '@22', '@23', '@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}):
        budget = get('compose', version=request['policy_versions']['compose'])(None, operation='answer_contract')['nested_model_handle_budget']
    if isinstance(request, dict) and request.get('desired_outcome') == 'project.answer':
        timeout = answer_lifecycle(request)['tool_timeout_ms']
        selected = (request.get('policy_versions') or {}).get('retrieve')
        if selected and getattr(get('retrieve', version=selected), 'agentic', False):
            budget = get('retrieve', version=selected)(None, operation='agentic_limits')['nested_model_handle_budget']
    token = _BUDGET.set(budget)
    timeout_token = _TIMEOUT.set(timeout)
    try:
        yield
    finally:
        _BUDGET.reset(token)
        _TIMEOUT.reset(timeout_token)


class AnswerContractRegistry:
    """同一 provider 的只读合同视图；注册和注销仍由原注册表负责。"""
    def __init__(self, registry):
        self._registry = registry

    def _project(self, definition):
        if (definition is None or definition.capability_id != 'workbench.answer.execute'
                or _BUDGET.get() == 3 and _TIMEOUT.get() == 240_000):
            return definition
        return replace(definition, tool_definition=replace(definition.tool_definition,
            nested_model_handle_budget=_BUDGET.get(), timeout_ms=_TIMEOUT.get()))

    def register(self, definition, provider):
        return self._registry.register(definition, provider)

    def resolve(self, capability_id):
        resolved = self._registry.resolve(capability_id)
        return (self._project(resolved[0]), resolved[1]) if resolved else None

    def get(self, capability_id):
        return self._project(self._registry.get(capability_id))

    def list(self):
        return tuple(self._project(definition) for definition in self._registry.list())

    def snapshot(self):
        snapshot = self._registry.snapshot()
        return replace(snapshot, definitions=tuple(self._project(definition) for definition in snapshot.definitions))
