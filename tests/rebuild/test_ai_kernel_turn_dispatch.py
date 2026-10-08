from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

from core.ai_kernel import SynchronousToolDispatcher
from tests.rebuild.test_ai_kernel_dispatcher import _Observer, _Provider, _request


def request(turn, *, mode='parallel', locks=()):
    original = _request(mode=mode, locks=locks, timeout_ms=3000)
    return replace(original, provider_request={**original.provider_request, 'turn_id': turn})


def test_other_turn_write_does_not_wait_for_agent_wait():
    dispatcher = SynchronousToolDispatcher()
    waiting, release, written = Event(), Event(), Event()

    def wait(_):
        waiting.set()
        assert release.wait(2)
        return {'waited': True}

    def write(_):
        written.set()
        return {'receipt_ref': 'crp://test/write'}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dispatcher.dispatch, _Provider(wait), request('turn-wait'), _Observer())
        assert waiting.wait(1)
        second = pool.submit(dispatcher.dispatch, _Provider(write), request('turn-write', mode='exclusive'), _Observer())
        try:
            assert written.wait(.5), 'another Turn must not be blocked by agent.wait'
        finally:
            release.set()
        assert first.result(timeout=2) == {'waited': True}
        assert second.result(timeout=2) == {'receipt_ref': 'crp://test/write'}


def test_resource_lock_still_serializes_across_turns():
    dispatcher = SynchronousToolDispatcher()
    entered, release, second_entered = Event(), Event(), Event()

    def first_call(_):
        entered.set()
        assert release.wait(2)
        return {'first': True}

    def second_call(_):
        second_entered.set()
        return {'second': True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dispatcher.dispatch, _Provider(first_call), request('turn-a', locks=('shared',)), _Observer())
        assert entered.wait(1)
        second = pool.submit(dispatcher.dispatch, _Provider(second_call), request('turn-b', locks=('shared',)), _Observer())
        try:
            assert not second_entered.wait(.1)
        finally:
            release.set()
        assert first.result(timeout=2) == {'first': True}
        assert second.result(timeout=2) == {'second': True}


def test_exclusive_tool_still_blocks_reads_in_its_own_turn():
    dispatcher = SynchronousToolDispatcher()
    entered, release, read_entered = Event(), Event(), Event()

    def exclusive(_):
        entered.set()
        assert release.wait(2)
        return {'receipt_ref': 'crp://test/delete'}

    def read(_):
        read_entered.set()
        return {'read': True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dispatcher.dispatch, _Provider(exclusive), request('same-turn', mode='exclusive'), _Observer())
        assert entered.wait(1)
        second = pool.submit(dispatcher.dispatch, _Provider(read), request('same-turn'), _Observer())
        try:
            assert not read_entered.wait(.1)
        finally:
            release.set()
        assert first.result(timeout=2) == {'receipt_ref': 'crp://test/delete'}
        assert second.result(timeout=2) == {'read': True}
    assert dispatcher._turn_gates == {}
