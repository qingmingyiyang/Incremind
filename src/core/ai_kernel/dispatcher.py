from __future__ import annotations

import re
from inspect import Signature, signature
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition, Event, RLock
from time import monotonic
from typing import Iterator, Literal

from .contracts import AIKernelContractError
from .ports import CapabilityProviderPort, ToolDispatchObserverPort
from core.model_gateway import ModelCallPurpose, validate_model_call_purpose


ExecutionMode = Literal["parallel", "exclusive"]
EffectCertainty = Literal["confirmed_none", "confirmed_applied", "unknown"]
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")


class ToolDispatcherError(AIKernelContractError):
    pass


class ToolProviderFailure(AIKernelContractError):
    """Provider-declared failure with explicit side-effect evidence."""

    def __init__(
        self,
        error_code: str,
        *,
        effect_certainty: EffectCertainty,
        retry_after_ms: int | None = None,
        continuation_state: bytes | None = None,
    ) -> None:
        if not _ERROR_CODE.fullmatch(error_code):
            raise ToolDispatcherError("tool provider error code is invalid")
        if effect_certainty not in {"confirmed_none", "confirmed_applied", "unknown"}:
            raise ToolDispatcherError("tool provider effect certainty is invalid")
        if retry_after_ms is not None and (
            not isinstance(retry_after_ms, int)
            or isinstance(retry_after_ms, bool)
            or retry_after_ms < 0
            or retry_after_ms > 300_000
        ):
            raise ToolDispatcherError("tool provider retry delay is invalid")
        super().__init__(error_code)
        self.error_code = error_code
        self.effect_certainty = effect_certainty
        self.retry_after_ms = retry_after_ms
        self.continuation_state = continuation_state


class ToolDispatchFailure(ToolDispatcherError):
    def __init__(
        self,
        error_code: str,
        *,
        provider_started: bool,
        effect_certainty: EffectCertainty,
        retry_after_ms: int | None = None,
        continuation_state: bytes | None = None,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.provider_started = provider_started
        self.effect_certainty = effect_certainty
        self.retry_after_ms = retry_after_ms
        self.continuation_state = continuation_state


class ToolDispatchCancelled(ToolDispatcherError):
    def __init__(
        self,
        *,
        provider_started: bool,
        result: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__("tool dispatch cancellation was requested")
        self.provider_started = provider_started
        self.result = dict(result) if result is not None else None


class ToolDispatchDeadlineExceeded(ToolDispatcherError):
    def __init__(self, *, provider_started: bool) -> None:
        super().__init__("tool dispatch deadline was exceeded")
        self.provider_started = provider_started


class NestedModelHandleUnavailable(ToolDispatcherError):
    """A tool attempted nested model work without a dispatcher-scoped handle."""

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or "nested model handle is unavailable for this tool invocation")


@dataclass(slots=True)
class ToolCancellationToken:
    _requested: Event = field(default_factory=Event, repr=False)

    def request(self) -> None:
        self._requested.set()

    @property
    def is_requested(self) -> bool:
        return self._requested.is_set()


@dataclass(slots=True)
class ToolExecutionContext:
    invocation_id: str
    attempt: int
    timeout_ms: int
    cancellation: ToolCancellationToken
    nested_model_handle_budget: int = 1
    _deadline: float = field(init=False, repr=False)
    _provider_started: bool = field(default=False, init=False, repr=False)
    _nested_model_handle_factory: Callable[..., object] | None = field(default=None, repr=False)
    _nested_model_handle_keys: set[str] = field(default_factory=set, init=False, repr=False)
    _nested_model_handle_guard: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._deadline = monotonic() + (self.timeout_ms / 1000)

    @property
    def remaining_timeout_ms(self) -> int:
        return max(0, int((self._deadline - monotonic()) * 1000))

    @property
    def cancel_requested(self) -> bool:
        return self.cancellation.is_requested

    def mark_provider_started(self) -> None:
        self._provider_started = True

    def checkpoint(self) -> None:
        if self.cancel_requested:
            raise ToolDispatchCancelled(provider_started=self._provider_started)
        if self.remaining_timeout_ms <= 0:
            raise ToolDispatchDeadlineExceeded(provider_started=self._provider_started)

    def take_nested_model_handle(
        self,
        *,
        invocation_key: str | None = None,
        purpose: ModelCallPurpose | None = None,
    ) -> object:
        """Return one independently-owned nested-model handle within the budget.

        The context does not implement model metadata methods.  A Runtime-owned
        observer may inject a closure which creates the separate handle after
        tool.start has become durable.  Keys are unique per tool invocation, so
        concurrent work must declare separate identities rather than reusing a
        metadata sink.  The historical no-argument form remains the single
        ``default`` allocation and therefore remains compatible with budget 1.
        """

        key = _nested_model_handle_key(invocation_key=invocation_key, purpose=purpose)
        with self._nested_model_handle_guard:
            if self._nested_model_handle_factory is None:
                raise NestedModelHandleUnavailable()
            if key in self._nested_model_handle_keys:
                raise NestedModelHandleUnavailable(
                    "nested model handle is unavailable for this tool invocation: "
                    "nested model invocation key is already allocated"
                )
            if len(self._nested_model_handle_keys) >= self.nested_model_handle_budget:
                raise NestedModelHandleUnavailable(
                    "nested model handle is unavailable for this tool invocation: "
                    "nested model handle budget is exhausted"
                )
            # Reserve before entering the factory.  A factory which fails after
            # it creates durable lifecycle evidence must not be retried under
            # the same key and accidentally create a second logical request.
            self._nested_model_handle_keys.add(key)
        handle = _call_nested_model_handle_factory(
            self._nested_model_handle_factory,
            invocation_key=key,
            purpose=_nested_model_purpose(purpose),
        )
        if handle is None:
            raise NestedModelHandleUnavailable()
        return handle


@dataclass(frozen=True, slots=True)
class ToolDispatchRequest:
    provider_request: Mapping[str, object]
    execution_mode: ExecutionMode
    resource_locks: tuple[str, ...]
    invocation_id: str
    attempt: int
    timeout_ms: int
    nested_model_handle_budget: int = 1
    nested_model_handle_authorized: bool = False

    def __post_init__(self) -> None:
        if self.execution_mode not in {"parallel", "exclusive"}:
            raise ToolDispatcherError("tool execution mode is unsupported")
        if len(self.resource_locks) != len(set(self.resource_locks)):
            raise ToolDispatcherError("tool resource locks must be unique")
        if any(not isinstance(item, str) or not item.strip() for item in self.resource_locks):
            raise ToolDispatcherError("tool resource lock must be non-empty")
        if not isinstance(self.invocation_id, str) or not self.invocation_id.strip():
            raise ToolDispatcherError("tool invocation identity must be non-empty")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise ToolDispatcherError("tool attempt must be positive")
        if not isinstance(self.timeout_ms, int) or isinstance(self.timeout_ms, bool) or self.timeout_ms < 1:
            raise ToolDispatcherError("tool timeout must be positive")
        if (
            not isinstance(self.nested_model_handle_budget, int)
            or isinstance(self.nested_model_handle_budget, bool)
            or not 1 <= self.nested_model_handle_budget <= 16
        ):
            raise ToolDispatcherError("nested model handle budget must be between 1 and 16")
        if not isinstance(self.nested_model_handle_authorized, bool):
            raise ToolDispatcherError("nested model handle authorization must be boolean")


class SynchronousToolDispatcher:
    """Single-process fair dispatcher for bounded synchronous providers."""

    def __init__(self) -> None:
        self._gate = _ExecutionGate()
        self._lock_guard = RLock()
        self._turn_gates: dict[str, tuple[_ExecutionGate, int]] = {}
        self._resource_locks: dict[str, RLock] = {}
        self._active_guard = RLock()
        self._active: dict[str, ToolCancellationToken] = {}
        self._reserved: set[str] = set()

    def prepare(self, invocation_id: str) -> None:
        with self._active_guard:
            if invocation_id in self._active:
                raise ToolDispatcherError("tool invocation is already active")
            self._active[invocation_id] = ToolCancellationToken()
            self._reserved.add(invocation_id)

    def abandon_prepared(self, invocation_id: str) -> None:
        with self._active_guard:
            if invocation_id not in self._reserved:
                return
            self._reserved.remove(invocation_id)
            self._active.pop(invocation_id, None)

    def request_cancel(self, invocation_id: str) -> bool:
        with self._active_guard:
            token = self._active.get(invocation_id)
            if token is None:
                return False
            token.request()
            return True

    def dispatch(
        self,
        provider: CapabilityProviderPort,
        request: ToolDispatchRequest,
        observer: ToolDispatchObserverPort,
    ) -> Mapping[str, object]:
        invoke = getattr(provider, "invoke", None)
        if not callable(invoke):
            raise ToolDispatcherError("capability provider is not invokable")
        token: ToolCancellationToken
        with self._active_guard:
            if request.invocation_id in self._reserved:
                token = self._active[request.invocation_id]
                self._reserved.remove(request.invocation_id)
            elif request.invocation_id in self._active:
                raise ToolDispatcherError("tool invocation is already active")
            else:
                token = ToolCancellationToken()
                self._active[request.invocation_id] = token
        context = ToolExecutionContext(
            invocation_id=request.invocation_id,
            attempt=request.attempt,
            timeout_ms=request.timeout_ms,
            cancellation=token,
            nested_model_handle_budget=request.nested_model_handle_budget,
            _nested_model_handle_factory=(
                _nested_model_handle_factory(observer)
                if request.nested_model_handle_authorized else None
            ),
        )
        try:
            locks = self._locks_for(request.resource_locks)
            with self._turn_gate(request) as gate, gate.acquire(request.execution_mode, context.checkpoint):
                with _acquire_locks(locks, context.checkpoint):
                    context.checkpoint()
                    observer.claimed()
                    with observer.fence():
                        # The observer fence is the final pre-effect check.
                        # Do not publish ``tool.started`` until it has passed:
                        # a frozen authorization or lease denial must remain a
                        # confirmed-no-effect outcome with no provider start.
                        observer.started()
                        context.mark_provider_started()
                        provider_request = dict(request.provider_request)
                        provider_request["execution_context"] = context
                        try:
                            result = invoke(provider_request)
                        except Exception as error:
                            if token.is_requested:
                                raise ToolDispatchCancelled(provider_started=True) from error
                            if isinstance(error, ToolProviderFailure):
                                raise ToolDispatchFailure(
                                    error.error_code,
                                    provider_started=True,
                                    effect_certainty=error.effect_certainty,
                                    retry_after_ms=error.retry_after_ms,
                                    continuation_state=error.continuation_state,
                                ) from error
                            if isinstance(error, TimeoutError):
                                raise ToolDispatchFailure("timeout", provider_started=True, effect_certainty="unknown") from error
                            if isinstance(error, ConnectionError):
                                raise ToolDispatchFailure("temporarily_unavailable", provider_started=True, effect_certainty="unknown") from error
                            legacy_failure = _legacy_provider_failure(error)
                            if legacy_failure is not None:
                                raise legacy_failure from error
                            raise
                        if token.is_requested and not (isinstance(result, Mapping) and isinstance(result.get("receipt_ref"), str)):
                            raise ToolDispatchCancelled(provider_started=True, result=result if isinstance(result, Mapping) else None)
        finally:
            with self._active_guard:
                self._active.pop(request.invocation_id, None)
                self._reserved.discard(request.invocation_id)
        if not isinstance(result, Mapping):
            raise ToolDispatcherError("capability provider returned an invalid result")
        return dict(result)

    def dispatch_recovery(
        self,
        provider: CapabilityProviderPort,
        request: ToolDispatchRequest,
        observer: ToolDispatchObserverPort,
    ) -> Mapping[str, object] | None:
        """Run provider-owned recovery under the ordinary execution fences.

        Recovery deliberately omits ``claimed`` and ``started`` because those
        events already exist for the interrupted attempt.  It still holds the
        same admission gate, resource locks, observer authorization fence,
        deadline and cancellation token as a normal dispatch.
        """

        recover = getattr(provider, "recover_completed_invocation", None)
        if not callable(recover):
            return None
        token = ToolCancellationToken()
        with self._active_guard:
            if request.invocation_id in self._active:
                raise ToolDispatcherError("tool invocation is already active")
            self._active[request.invocation_id] = token
        context = ToolExecutionContext(
            invocation_id=request.invocation_id,
            attempt=request.attempt,
            timeout_ms=request.timeout_ms,
            cancellation=token,
            nested_model_handle_budget=request.nested_model_handle_budget,
        )
        try:
            locks = self._locks_for(request.resource_locks)
            with self._turn_gate(request) as gate, gate.acquire(request.execution_mode, context.checkpoint):
                with _acquire_locks(locks, context.checkpoint):
                    context.checkpoint()
                    with observer.fence():
                        provider_request = dict(request.provider_request)
                        provider_request["execution_context"] = context
                        result = recover(provider_request)
        finally:
            with self._active_guard:
                self._active.pop(request.invocation_id, None)
        if result is not None and not isinstance(result, Mapping):
            raise ToolDispatcherError("capability provider returned an invalid recovery result")
        return None if result is None else dict(result)

    @contextmanager
    def _turn_gate(self, request: ToolDispatchRequest) -> Iterator[_ExecutionGate]:
        turn_id = request.provider_request.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id.strip():
            # Historical direct dispatcher clients have no Turn identity.
            yield self._gate
            return
        with self._lock_guard:
            gate, users = self._turn_gates.get(turn_id, (_ExecutionGate(), 0))
            self._turn_gates[turn_id] = gate, users + 1
        try:
            yield gate
        finally:
            with self._lock_guard:
                _, users = self._turn_gates[turn_id]
                if users == 1:
                    del self._turn_gates[turn_id]
                else:
                    self._turn_gates[turn_id] = gate, users - 1

    def _locks_for(self, identities: tuple[str, ...]) -> tuple[RLock, ...]:
        normalized = tuple(sorted(item.strip() for item in identities))
        with self._lock_guard:
            return tuple(
                self._resource_locks.setdefault(identity, RLock())
                for identity in normalized
            )


class _ExecutionGate:
    def __init__(self) -> None:
        self._condition = Condition(RLock())
        self._parallel_active = 0
        self._exclusive_active = False
        self._exclusive_waiting = 0

    @contextmanager
    def acquire(self, mode: ExecutionMode, checkpoint) -> Iterator[None]:
        if mode == "exclusive":
            self._acquire_exclusive(checkpoint)
            try:
                yield
            finally:
                self._release_exclusive()
            return
        self._acquire_parallel(checkpoint)
        try:
            yield
        finally:
            self._release_parallel()

    def _acquire_exclusive(self, checkpoint) -> None:
        with self._condition:
            self._exclusive_waiting += 1
            try:
                while self._exclusive_active or self._parallel_active:
                    checkpoint()
                    self._condition.wait(timeout=0.05)
                checkpoint()
                self._exclusive_active = True
            finally:
                self._exclusive_waiting -= 1

    def _release_exclusive(self) -> None:
        with self._condition:
            self._exclusive_active = False
            self._condition.notify_all()

    def _acquire_parallel(self, checkpoint) -> None:
        with self._condition:
            while self._exclusive_active or self._exclusive_waiting:
                checkpoint()
                self._condition.wait(timeout=0.05)
            checkpoint()
            self._parallel_active += 1

    def _release_parallel(self) -> None:
        with self._condition:
            self._parallel_active -= 1
            self._condition.notify_all()


@contextmanager
def _acquire_locks(locks: tuple[RLock, ...], checkpoint) -> Iterator[None]:
    acquired: list[RLock] = []
    try:
        for lock in locks:
            while not lock.acquire(timeout=0.05):
                checkpoint()
            acquired.append(lock)
            checkpoint()
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


def _legacy_provider_failure(error: Exception) -> ToolDispatchFailure | None:
    if not isinstance(error, ValueError):
        return None
    message = str(error).casefold()
    if "baseline is stale" in message:
        return ToolDispatchFailure(
            "ai.stale_baseline",
            provider_started=True,
            effect_certainty="confirmed_none",
        )
    if "turn model routing snapshot identity drifted" in message:
        return ToolDispatchFailure(
            "ai.stale_baseline",
            provider_started=True,
            effect_certainty="confirmed_none",
        )
    if "turn model routing snapshot is unavailable" in message:
        return ToolDispatchFailure(
            "ai.model_routing_unavailable",
            provider_started=True,
            effect_certainty="confirmed_none",
        )
    if "needs verified project evidence" in message:
        return ToolDispatchFailure(
            "ai.insufficient_evidence",
            provider_started=True,
            effect_certainty="confirmed_none",
        )
    return None


def _nested_model_handle_factory(observer: ToolDispatchObserverPort) -> Callable[..., object] | None:
    """Read the optional factory without widening the legacy observer protocol."""

    factory = getattr(observer, "nested_model_handle_factory", None)
    if factory is None:
        return None
    if not callable(factory):
        raise ToolDispatcherError("nested model handle factory is not callable")
    return factory


def _nested_model_handle_key(
    *, invocation_key: str | None, purpose: ModelCallPurpose | None,
) -> str:
    key = _optional_non_empty_text(invocation_key, "nested model invocation key")
    normalized_purpose = _nested_model_purpose(purpose)
    if key is not None:
        return key
    if normalized_purpose is not None:
        return f"purpose:{normalized_purpose}"
    return "default"


def _nested_model_purpose(value: ModelCallPurpose | None) -> ModelCallPurpose:
    return validate_model_call_purpose("primary" if value is None else value)


def _optional_non_empty_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ToolDispatcherError(f"{label} must be non-empty when provided")
    return text


def _call_nested_model_handle_factory(
    factory: Callable[..., object],
    *,
    invocation_key: str,
    purpose: ModelCallPurpose,
) -> object:
    """Support legacy zero-argument factories while exposing the keyed contract."""

    try:
        factory_signature: Signature = signature(factory)
    except (TypeError, ValueError):
        # Callables without inspectable signatures must implement the current,
        # explicit contract.  Falling back after an arbitrary TypeError could
        # run a durable factory twice.
        return factory(invocation_key=invocation_key, purpose=purpose)
    try:
        factory_signature.bind(invocation_key=invocation_key, purpose=purpose)
    except TypeError:
        try:
            factory_signature.bind()
        except TypeError as error:
            raise ToolDispatcherError(
                "nested model handle factory must accept invocation_key and purpose or no arguments"
            ) from error
        return factory()
    return factory(invocation_key=invocation_key, purpose=purpose)
