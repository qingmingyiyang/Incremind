from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event, Lock
import time
from uuid import uuid4

import pytest

from core.ai_kernel import (
    NestedModelHandleUnavailable,
    SynchronousToolDispatcher,
    ToolDispatchCancelled,
    ToolDispatchDeadlineExceeded,
    ToolDispatchFailure,
    ToolDispatchRequest,
    ToolProviderFailure,
)


class _Observer:
    def __init__(self) -> None:
        self.events: list[str] = []

    def claimed(self) -> None:
        self.events.append("claimed")

    def started(self) -> None:
        self.events.append("started")

    @contextmanager
    def fence(self):
        self.events.append("fence")
        yield


def test_fence_rejection_precedes_started_and_preserves_confirmed_none() -> None:
    dispatcher = SynchronousToolDispatcher()
    calls = 0

    class RevokedObserver(_Observer):
        @contextmanager
        def fence(self):
            self.events.append("fence")
            raise ToolDispatchFailure(
                "frozen_authorization_denied",
                provider_started=False,
                effect_certainty="confirmed_none",
            )
            yield

    def invoke(_request):
        nonlocal calls
        calls += 1
        return {"ok": True}

    observer = RevokedObserver()
    with pytest.raises(ToolDispatchFailure) as failure:
        dispatcher.dispatch(_Provider(invoke), _request(), observer)
    assert failure.value.error_code == "frozen_authorization_denied"
    assert failure.value.provider_started is False
    assert failure.value.effect_certainty == "confirmed_none"
    assert observer.events == ["claimed", "fence"]
    assert calls == 0


def test_dispatcher_releases_entered_fence_after_provider_exception() -> None:
    dispatcher = SynchronousToolDispatcher()

    class FenceObserver(_Observer):
        def __init__(self) -> None:
            super().__init__()
            self.released = 0

        @contextmanager
        def fence(self):
            self.events.append("fence")
            try:
                yield
            finally:
                self.released += 1

    observer = FenceObserver()
    with pytest.raises(ValueError, match="provider failed"):
        dispatcher.dispatch(_Provider(lambda _request: (_ for _ in ()).throw(ValueError("provider failed"))), _request(), observer)
    assert observer.released == 1


def test_dispatcher_releases_partially_entered_fence_when_observer_rejects() -> None:
    dispatcher = SynchronousToolDispatcher()

    class FenceObserver(_Observer):
        def __init__(self) -> None:
            super().__init__()
            self.released = 0

        @contextmanager
        def fence(self):
            self.events.append("fence")
            try:
                raise RuntimeError("fence rejected")
                yield
            finally:
                self.released += 1

    observer = FenceObserver()
    with pytest.raises(RuntimeError, match="fence rejected"):
        dispatcher.dispatch(_Provider(lambda _request: {"ok": True}), _request(), observer)
    assert observer.released == 1


class _Provider:
    def __init__(self, invoke) -> None:
        self._invoke = invoke

    def invoke(self, request):
        return self._invoke(request)


def test_recovery_dispatch_reuses_fence_and_control_without_second_start() -> None:
    dispatcher = SynchronousToolDispatcher()
    observer = _Observer()
    seen: list[object] = []

    class RecoveryProvider:
        def recover_completed_invocation(self, request):
            context = request.get("execution_context")
            context.checkpoint()
            seen.append(context)
            return {"receipt_ref": "crp://receipt/recovered"}

    result = dispatcher.dispatch_recovery(
        RecoveryProvider(), _request(), observer,
    )

    assert result == {"receipt_ref": "crp://receipt/recovered"}
    assert len(seen) == 1
    assert observer.events == ["fence"]


def _request(
    *,
    mode: str = "parallel",
    locks: tuple[str, ...] = (),
    invocation_id: str | None = None,
    timeout_ms: int = 2_000,
    nested_model_handle_budget: int = 1,
    nested_model_handle_authorized: bool = False,
) -> ToolDispatchRequest:
    return ToolDispatchRequest(
        provider_request={"operation_id": "op-test"},
        execution_mode=mode,  # type: ignore[arg-type]
        resource_locks=locks,
        invocation_id=invocation_id or f"tool-call-{uuid4().hex}",
        attempt=1,
        timeout_ms=timeout_ms,
        nested_model_handle_budget=nested_model_handle_budget,
        nested_model_handle_authorized=nested_model_handle_authorized,
    )


def test_parallel_calls_with_disjoint_resources_can_overlap() -> None:
    dispatcher = SynchronousToolDispatcher()
    barrier = Barrier(2)

    def invoke(request):
        barrier.wait(timeout=2)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dispatcher.dispatch, _Provider(invoke), _request(locks=("a",)), _Observer())
        second = pool.submit(dispatcher.dispatch, _Provider(invoke), _request(locks=("b",)), _Observer())
        assert first.result(timeout=3) == {"ok": True}
        assert second.result(timeout=3) == {"ok": True}


def test_conflicting_resource_locks_serialize_parallel_calls() -> None:
    dispatcher = SynchronousToolDispatcher()
    guard = Lock()
    active = 0
    maximum = 0

    def invoke(request):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with guard:
            active -= 1
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                dispatcher.dispatch,
                _Provider(invoke),
                _request(locks=("document:one",)),
                _Observer(),
            )
            for _ in range(2)
        ]
        assert [future.result(timeout=3) for future in futures] == [{"ok": True}, {"ok": True}]
    assert maximum == 1


def test_exclusive_call_waits_for_parallel_work_and_forms_a_barrier() -> None:
    dispatcher = SynchronousToolDispatcher()
    parallel_entered = Event()
    release_parallel = Event()
    exclusive_entered = Event()

    def parallel(request):
        parallel_entered.set()
        assert release_parallel.wait(timeout=2)
        return {"kind": "parallel"}

    def exclusive(request):
        exclusive_entered.set()
        return {"kind": "exclusive"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            dispatcher.dispatch,
            _Provider(parallel),
            _request(mode="parallel"),
            _Observer(),
        )
        assert parallel_entered.wait(timeout=1)
        second = pool.submit(
            dispatcher.dispatch,
            _Provider(exclusive),
            _request(mode="exclusive"),
            _Observer(),
        )
        assert not exclusive_entered.wait(timeout=0.05)
        release_parallel.set()
        assert first.result(timeout=2) == {"kind": "parallel"}
        assert second.result(timeout=2) == {"kind": "exclusive"}


def test_dispatcher_releases_resource_lock_after_provider_failure() -> None:
    dispatcher = SynchronousToolDispatcher()

    def fail(request):
        raise ValueError("failure")

    with pytest.raises(ValueError, match="failure"):
        dispatcher.dispatch(
            _Provider(fail),
            _request(locks=("source:one",)),
            _Observer(),
        )
    observer = _Observer()
    result = dispatcher.dispatch(
        _Provider(lambda request: {"ok": True}),
        _request(locks=("source:one",)),
        observer,
    )
    assert result == {"ok": True}
    assert observer.events == ["claimed", "fence", "started"]


def test_cancelled_waiter_never_invokes_provider() -> None:
    dispatcher = SynchronousToolDispatcher()
    occupied = Event()
    release = Event()
    waiting_provider_calls = 0

    def hold(_request):
        occupied.set()
        assert release.wait(timeout=2)
        return {"ok": True}

    def waiting(_request):
        nonlocal waiting_provider_calls
        waiting_provider_calls += 1
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            dispatcher.dispatch,
            _Provider(hold),
            _request(locks=("document:one",), invocation_id="tool-call-hold"),
            _Observer(),
        )
        assert occupied.wait(timeout=1)
        second = pool.submit(
            dispatcher.dispatch,
            _Provider(waiting),
            _request(locks=("document:one",), invocation_id="tool-call-wait"),
            _Observer(),
        )
        assert dispatcher.request_cancel("tool-call-wait") is True
        with pytest.raises(ToolDispatchCancelled) as cancelled:
            second.result(timeout=2)
        assert cancelled.value.provider_started is False
        assert waiting_provider_calls == 0
        release.set()
        assert first.result(timeout=2) == {"ok": True}


def test_cancel_reserved_before_dispatch_never_invokes_provider() -> None:
    dispatcher = SynchronousToolDispatcher()
    provider_calls = 0

    def invoke(_request):
        nonlocal provider_calls
        provider_calls += 1
        return {"ok": True}

    dispatcher.prepare("tool-call-reserved")
    assert dispatcher.request_cancel("tool-call-reserved") is True

    with pytest.raises(ToolDispatchCancelled) as cancelled:
        dispatcher.dispatch(
            _Provider(invoke),
            _request(invocation_id="tool-call-reserved"),
            _Observer(),
        )

    assert cancelled.value.provider_started is False
    assert provider_calls == 0


def test_abandoned_reservation_releases_invocation_identity() -> None:
    dispatcher = SynchronousToolDispatcher()
    dispatcher.prepare("tool-call-abandoned")

    dispatcher.abandon_prepared("tool-call-abandoned")
    dispatcher.prepare("tool-call-abandoned")

    assert dispatcher.dispatch(
        _Provider(lambda _request: {"ok": True}),
        _request(invocation_id="tool-call-abandoned"),
        _Observer(),
    ) == {"ok": True}


def test_deadline_while_waiting_never_invokes_provider() -> None:
    dispatcher = SynchronousToolDispatcher()
    occupied = Event()
    release = Event()
    waiting_provider_calls = 0

    def hold(_request):
        occupied.set()
        assert release.wait(timeout=2)
        return {"ok": True}

    def waiting(_request):
        nonlocal waiting_provider_calls
        waiting_provider_calls += 1
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            dispatcher.dispatch,
            _Provider(hold),
            _request(locks=("source:one",), invocation_id="tool-call-long"),
            _Observer(),
        )
        assert occupied.wait(timeout=1)
        second = pool.submit(
            dispatcher.dispatch,
            _Provider(waiting),
            _request(
                locks=("source:one",),
                invocation_id="tool-call-deadline",
                timeout_ms=20,
            ),
            _Observer(),
        )
        with pytest.raises(ToolDispatchDeadlineExceeded) as exceeded:
            second.result(timeout=2)
        assert exceeded.value.provider_started is False
        assert waiting_provider_calls == 0
        release.set()
        assert first.result(timeout=2) == {"ok": True}


def test_provider_receives_cooperative_execution_context() -> None:
    dispatcher = SynchronousToolDispatcher()

    def invoke(request):
        context = request["execution_context"]
        assert context.invocation_id == "tool-call-context"
        assert context.attempt == 1
        assert context.remaining_timeout_ms > 0
        context.checkpoint()
        return {"ok": True}

    assert dispatcher.dispatch(
        _Provider(invoke),
        _request(invocation_id="tool-call-context"),
        _Observer(),
    ) == {"ok": True}


def test_dispatcher_injects_a_nested_model_handle_once_per_invocation() -> None:
    dispatcher = SynchronousToolDispatcher()
    factory_calls = 0
    handle = object()

    class NestedModelObserver(_Observer):
        def nested_model_handle_factory(self):
            nonlocal factory_calls
            factory_calls += 1
            return handle

    def invoke(request):
        context = request["execution_context"]
        assert context.take_nested_model_handle() is handle
        with pytest.raises(NestedModelHandleUnavailable, match="nested model handle is unavailable"):
            context.take_nested_model_handle()
        return {"ok": True}

    assert dispatcher.dispatch(
        _Provider(invoke),
        _request(
            invocation_id="tool-call-nested-model",
            nested_model_handle_authorized=True,
        ),
        NestedModelObserver(),
    ) == {"ok": True}
    assert factory_calls == 1


def test_dispatcher_injects_independent_keyed_nested_model_handles_within_budget() -> None:
    dispatcher = SynchronousToolDispatcher()
    factory_calls: list[tuple[str, str | None]] = []

    class NestedModelObserver(_Observer):
        def nested_model_handle_factory(self, *, invocation_key: str, purpose: str | None):
            factory_calls.append((invocation_key, purpose))
            return object()

    def invoke(request):
        context = request["execution_context"]
        first = context.take_nested_model_handle(invocation_key="chunk-1", purpose="aux")
        second = context.take_nested_model_handle(invocation_key="chunk-2", purpose="aux")
        assert first is not second
        with pytest.raises(NestedModelHandleUnavailable, match="already allocated"):
            context.take_nested_model_handle(invocation_key="chunk-1", purpose="probe")
        with pytest.raises(NestedModelHandleUnavailable, match="budget is exhausted"):
            context.take_nested_model_handle(invocation_key="chunk-3", purpose="aux")
        return {"ok": True}

    assert dispatcher.dispatch(
        _Provider(invoke),
        _request(
            invocation_id="tool-call-keyed-nested-model",
            nested_model_handle_budget=2,
            nested_model_handle_authorized=True,
        ),
        NestedModelObserver(),
    ) == {"ok": True}
    assert factory_calls == [("chunk-1", "aux"), ("chunk-2", "aux")]


def test_dispatcher_rejects_unknown_nested_model_purpose() -> None:
    dispatcher = SynchronousToolDispatcher()

    def invoke(request):
        with pytest.raises(ValueError, match="model call purpose"):
            request["execution_context"].take_nested_model_handle(purpose="summarize")
        return {"ok": True}

    assert dispatcher.dispatch(
        _Provider(invoke), _request(nested_model_handle_authorized=True), _Observer(),
    ) == {"ok": True}


@pytest.mark.parametrize("budget", [0, True, 17])
def test_dispatch_request_rejects_invalid_nested_model_handle_budget(budget: object) -> None:
    with pytest.raises(Exception, match="nested model handle budget"):
        ToolDispatchRequest(
            provider_request={},
            execution_mode="parallel",
            resource_locks=(),
            invocation_id="tool-call-invalid-nested-budget",
            attempt=1,
            timeout_ms=1_000,
            nested_model_handle_budget=budget,  # type: ignore[arg-type]
        )


def test_dispatcher_reports_nested_model_handle_unavailable_without_factory() -> None:
    dispatcher = SynchronousToolDispatcher()

    def invoke(request):
        context = request["execution_context"]
        with pytest.raises(NestedModelHandleUnavailable, match="nested model handle is unavailable"):
            context.take_nested_model_handle()
        return {"ok": True}

    assert dispatcher.dispatch(
        _Provider(invoke),
        _request(invocation_id="tool-call-no-nested-model"),
        _Observer(),
    ) == {"ok": True}


def test_dispatcher_does_not_read_observer_factory_when_request_is_unauthorized() -> None:
    dispatcher = SynchronousToolDispatcher()
    factory_reads = 0

    class UnauthorizedObserver(_Observer):
        @property
        def nested_model_handle_factory(self):
            nonlocal factory_reads
            factory_reads += 1
            return lambda: object()

    def invoke(request):
        with pytest.raises(NestedModelHandleUnavailable, match="nested model handle is unavailable"):
            request["execution_context"].take_nested_model_handle()
        return {"ok": True}

    assert dispatcher.dispatch(
        _Provider(invoke),
        _request(invocation_id="tool-call-unauthorized-nested-model"),
        UnauthorizedObserver(),
    ) == {"ok": True}
    assert factory_reads == 0


def test_dispatch_request_rejects_invalid_nested_model_handle_authorization() -> None:
    with pytest.raises(Exception, match="nested model handle authorization"):
        ToolDispatchRequest(
            provider_request={},
            execution_mode="parallel",
            resource_locks=(),
            invocation_id="tool-call-invalid-nested-authorization",
            attempt=1,
            timeout_ms=1_000,
            nested_model_handle_authorized=1,  # type: ignore[arg-type]
        )


def test_provider_failure_preserves_effect_certainty_and_retry_delay() -> None:
    dispatcher = SynchronousToolDispatcher()

    def invoke(_request):
        raise ToolProviderFailure(
            "temporarily_unavailable",
            effect_certainty="confirmed_none",
            retry_after_ms=125,
        )

    with pytest.raises(ToolDispatchFailure) as failure:
        dispatcher.dispatch(
            _Provider(invoke),
            _request(invocation_id="tool-call-classified"),
            _Observer(),
        )

    assert failure.value.error_code == "temporarily_unavailable"
    assert failure.value.provider_started is True
    assert failure.value.effect_certainty == "confirmed_none"
    assert failure.value.retry_after_ms == 125


@pytest.mark.parametrize(
    ("message", "error_code"),
    (
        ("Turn model routing snapshot is unavailable", "ai.model_routing_unavailable"),
        ("Turn model routing snapshot identity drifted", "ai.stale_baseline"),
    ),
)
def test_model_routing_pre_egress_failures_are_confirmed_none(
    message: str,
    error_code: str,
) -> None:
    dispatcher = SynchronousToolDispatcher()

    def invoke(_request):
        raise ValueError(message)

    with pytest.raises(ToolDispatchFailure) as failure:
        dispatcher.dispatch(
            _Provider(invoke),
            _request(invocation_id=f"tool-call-{error_code.replace('.', '-')}"),
            _Observer(),
        )

    assert failure.value.error_code == error_code
    assert failure.value.provider_started is True
    assert failure.value.effect_certainty == "confirmed_none"
