from concurrent.futures import ThreadPoolExecutor
from threading import Event

from backend.memory_app.kernel.task_draft_capability import task_draft_definition
from core.ai_kernel import CapabilityDefinition, SynchronousToolDispatcher
from core.ai_tooling import tool_from_capability
from tests.rebuild.test_ai_kernel_dispatcher import _Observer, _Provider, _request


def definition():
    return CapabilityDefinition('document.draft.propose', 1, 'write', True,
        'receipt_required', 'crp://draft/input', 'crp://draft/output')


def test_draft_parallel_keeps_same_capability_serialized():
    tool = tool_from_capability(task_draft_definition(definition()))
    assert tool.execution_mode == 'parallel'
    assert tool.mutability == 'reversible'
    assert tool.resource_locks == ('legacy:document.draft.propose',)
    dispatcher = SynchronousToolDispatcher()
    entered, release, second = Event(), Event(), Event()
    def first_call(request):
        entered.set()
        assert release.wait(2)
        return {'ok': True}
    def second_call(request):
        second.set()
        return {'ok': True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dispatcher.dispatch, _Provider(first_call),
            _request(mode=tool.execution_mode, locks=tool.resource_locks), _Observer())
        assert entered.wait(1)
        pending = pool.submit(dispatcher.dispatch, _Provider(second_call),
            _request(mode=tool.execution_mode, locks=tool.resource_locks), _Observer())
        try:
            assert not second.wait(.15)
        finally:
            release.set()
        assert first.result(timeout=1)['ok']
        assert pending.result(timeout=1)['ok']


def test_ordinary_write_remains_exclusive():
    tool = tool_from_capability(definition())
    assert tool.execution_mode == 'exclusive'
    assert tool.mutability == 'irreversible'
    assert tool.resource_locks == ('legacy:document.draft.propose',)
