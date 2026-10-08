from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token, copy_context
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from threading import Event, RLock
from time import monotonic
from uuid import uuid4
from typing import Protocol
from functools import wraps

from core.ai_tooling import (
    tool_contract_identity,
    tool_from_capability,
    tool_matches_contract_identity,
)

from .capability_manifest import (
    V1TurnPolicyCapabilityManifestResolver,
    manifest_from_payload,
    manifest_to_payload,
    validate_manifest_for_request,
)
from .context_manifest import (
    ContextManifestError,
    V1TurnContextManifestResolver,
    context_manifest_from_payload,
    context_manifest_to_payload,
    validate_context_manifest_for_request,
)
from .contracts import AIKernelContractError, validate_governed_payload, validate_model_call_receipt, validate_model_dispatch_authority_receipt, validate_model_wire_attempt_dispatch, validate_model_wire_attempt_receipt, validate_prompt_cache_receipt, validate_turn_action, validate_turn_presentation_artifact, validate_turn_request
from .codex_hook_parity import HookEvent
from .codex_hook_runtime import (
    CodexHookHost,
    HookInvocationReceipt,
    HookPolicySnapshot,
    hook_invocation_receipt_to_payload,
    hook_policy_snapshot_from_payload,
    hook_policy_snapshot_to_payload,
)
from .dispatcher import (
    SynchronousToolDispatcher,
    ToolDispatchCancelled,
    ToolDispatchDeadlineExceeded,
    ToolDispatchFailure,
    ToolDispatchRequest,
)
from core.model_gateway import ModelCallPurpose, validate_model_call_purpose
from .turn_templates import turn_purpose, planner_limits
from core.effect_log import (
    Effect,
    EffectClass,
    EffectIntent,
    EffectPurpose,
    EffectRunner,
    EffectState,
)
from core.product_core.expert_execution_receipt import (
    build_expert_execution_receipt,
    validate_expert_execution_receipt,
    verify_expert_execution_receipt_replay,
)
from .execution_projection import ExecutionProjectionView, build_execution_projection
from .ports import AgentPlannerPort, AtomicTurnBundlePort, CapabilityDefinition, CapabilityManifest, CapabilityManifestResolverPort, CapabilityRegistryPort, ContextManifest, ContextManifestResolverPort, DurableModelWireCommitWitness, ExpertTurnBindingPort, RunLeaseToken, ToolDispatcherPort, ToolExecutionBoundaryDecision, ToolExecutionBoundaryPort, TurnEventStorePort, TurnPayloadStorePort, TurnReceipt, TurnStateStorePort, _issue_durable_model_wire_commit_witness, validate_run_lease_token
from .event_store import RunLeaseRevoked, TurnEventConflict
from .recovery import _buffered_result_is_valid
from .state_store import InMemoryTurnStateStore, TurnStateConflict
from .scoped_payloads import ScopedTurnPayloadView, planner_context_payload_refs
from .tool_invocation import (
    EffectCertainty,
    ToolAttemptFailure,
    ToolInvocationIntent,
    ToolInvocationOutcome,
    attempt_failure_from_payload,
    attempt_failure_to_payload,
    build_intent,
    intent_from_payload,
    intent_to_payload,
    outcome_from_payload,
    outcome_to_payload,
)


class AIKernelRuntimeError(AIKernelContractError):
    pass


class FrozenAuthorizationRuntimePort(Protocol):
    """Turn-scoped frozen authority used only by Hook-enabled data paths."""

    def issue(self, request: Mapping[str, object], *, capability_manifest_ref: str,
              context_manifest_ref: str, capabilities: Sequence[CapabilityDefinition]) -> object: ...
    def load_current(self, *, turn_id: str) -> object: ...
    def current_handle(self, *, turn_id: str) -> object: ...
    def authorize_candidate(self, **kwargs: object) -> object | None: ...
    def create_approval(self, **kwargs: object) -> object: ...
    def load_approval(self, **kwargs: object) -> object: ...
    def prepare_intent(self, intent: ToolInvocationIntent) -> bool: ...
    def fence_intent(self, intent: ToolInvocationIntent) -> bool: ...


class _NestedModelReceiptPersistenceError(RuntimeError):
    """Reject a tool success when its nested model audit receipt was not durable."""


class _ToolTurnConverged(RuntimeError):
    pass


@dataclass(frozen=True)
class _ToolSettlementDefinition:
    mode: str
    operation_semantics: str


def _serialize_event_commit(method):
    """Protect sequence allocation and its commit, never provider execution."""
    @wraps(method)
    def run(self, *args, **kwargs):
        identity = args[0] if args else kwargs.get('turn_id', kwargs.get('outcome'))
        turn_id = identity if isinstance(identity, str) else identity.turn_id
        with self._event_commit(turn_id):
            return method(self, *args, **kwargs)
    return run


class _ExpertProposalCheckpointPending(RuntimeError):
    """External proposal may exist; preserve the Turn for outbox recovery."""


_MCP_CONTINUATION_TTL_SECONDS = 900


class _MCPContinuationDispatchProvider:
    """Narrow adapter so Dispatcher keeps its existing invoke-only seam."""

    def __init__(self, provider: object, request_state: bytes) -> None:
        self._provider = provider
        self._request_state = request_state

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        continuation = getattr(self._provider, "continue_request_state", None)
        if not callable(continuation):
            raise ToolDispatchFailure(
                "mcp.continuation_unavailable", provider_started=False,
                effect_certainty="confirmed_none",
            )
        return continuation(request, self._request_state)


class _RecoveryProvider:
    """Dispatcher adapter for one provider-owned recovery transaction."""

    def __init__(
        self,
        recover: Callable[[Mapping[str, object]], Mapping[str, object] | None],
    ) -> None:
        self._recover = recover

    def recover_completed_invocation(
        self, request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        return self._recover(request)


class _PlannerCancelled(RuntimeError):
    pass


class _PlannerDeadlineExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class _PlannerExecutionContext:
    turn_id: str
    step_id: str
    model_request_id: str
    timeout_ms: int
    purpose: ModelCallPurpose = "primary"
    _route_recorder: Callable[[str], None] | None = field(default=None, repr=False)
    _attempt_dispatch_recorder: Callable[[Mapping[str, object]], "_StoredModelAttemptDispatch"] | None = field(default=None, repr=False)
    _attempt_terminal_recorder: Callable[[Mapping[str, object], str], str] | None = field(default=None, repr=False)
    _cancel_requested: Event = field(default_factory=Event, repr=False)
    _deadline: float = field(init=False, repr=False)
    _started_monotonic: float = field(init=False, repr=False)
    _requested_at: str = field(init=False, repr=False)
    _metadata_lock: RLock = field(default_factory=RLock, repr=False)
    _provider_id: str | None = field(default=None, init=False, repr=False)
    _model_id: str | None = field(default=None, init=False, repr=False)
    _execution_location: str | None = field(default=None, init=False, repr=False)
    _usage: dict[str, int] | None = field(default=None, init=False, repr=False)
    _call_started: bool = field(default=False, init=False, repr=False)
    _call_status: str | None = field(default=None, init=False, repr=False)
    _model_terminal: bool = field(default=False, init=False, repr=False)
    _routing_snapshot_ref: str | None = field(default=None, init=False, repr=False)
    _routing_snapshot_revision: str | None = field(default=None, init=False, repr=False)
    _prompt_cache_scope_identity: str | None = field(default=None, init=False, repr=False)
    _prompt_cache_observation: dict[str, int] | None = field(default=None, init=False, repr=False)
    _wire_attempt_number: int = field(default=0, init=False, repr=False)
    _active_wire_attempt_id: str | None = field(default=None, init=False, repr=False)
    _active_wire_executor: Callable[[Callable[[], object]], object] | None = field(
        default=None, init=False, repr=False,
    )
    _wire_attempt_receipt_refs: list[str] = field(default_factory=list, init=False, repr=False)
    _dispatch_authority_measurement: dict[str, object] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        validate_model_call_purpose(self.purpose)
        self._started_monotonic = monotonic()
        self._deadline = self._started_monotonic + (self.timeout_ms / 1000)
        self._requested_at = datetime.now(timezone.utc).isoformat()

    @property
    def remaining_timeout_ms(self) -> int:
        return max(0, int((self._deadline - monotonic()) * 1000))

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def request_cancel(self) -> None:
        self._cancel_requested.set()

    def checkpoint(self) -> None:
        if self.cancel_requested:
            raise _PlannerCancelled("planner execution was cancelled")
        if self.remaining_timeout_ms <= 0:
            raise _PlannerDeadlineExceeded("planner execution deadline was exceeded")

    def model_call_started(self, *, provider: str, model: str) -> None:
        with self._metadata_lock:
            self._call_started = True
            if self._provider_id is None:
                self._provider_id = _safe_model_metadata(provider, model=False)
            if self._model_id is None:
                self._model_id = _safe_model_metadata(model, model=True)

    def model_call_routed(
        self,
        *,
        snapshot_ref: str,
        snapshot_revision: str,
        prompt_cache_scope_identity: str,
        provider: str,
        model: str,
        execution_location: str,
        purpose: ModelCallPurpose = "primary",
    ) -> None:
        safe_provider = _safe_model_metadata(provider, model=False)
        safe_model = _safe_model_metadata(model, model=True)
        if (
            not snapshot_ref.startswith("crp://session/")
            or not re.fullmatch(r"[a-f0-9]{64}", snapshot_revision)
            or not re.fullmatch(r"[a-f0-9]{64}", prompt_cache_scope_identity)
            or safe_provider is None
            or safe_model is None
            or execution_location not in {"remote", "local_loopback"}
            or validate_model_call_purpose(purpose) != self.purpose
        ):
            raise AIKernelRuntimeError("model routing metadata is invalid")
        with self._metadata_lock:
            if self._routing_snapshot_ref is not None:
                raise AIKernelRuntimeError("model route was already recorded")
        if self._route_recorder is None:
            raise AIKernelRuntimeError("model route recorder is unavailable")
        self._route_recorder(snapshot_ref)
        with self._metadata_lock:
            self._routing_snapshot_ref = snapshot_ref
            self._routing_snapshot_revision = snapshot_revision
            self._prompt_cache_scope_identity = prompt_cache_scope_identity
            self._provider_id = safe_provider
            self._model_id = safe_model
            self._execution_location = execution_location

    def model_call_completed(self, *, usage: Mapping[str, int]) -> None:
        with self._metadata_lock:
            self._call_status = "completed"
            self._usage = _normalized_model_usage(usage)

    def model_call_cache_observed(self, *, observation: Mapping[str, int]) -> None:
        allowed = {
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_miss_input_tokens",
        }
        if not observation or set(observation) - allowed:
            raise AIKernelRuntimeError("prompt cache observation is invalid")
        normalized: dict[str, int] = {}
        for key, value in observation.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AIKernelRuntimeError("prompt cache observation is invalid")
            normalized[key] = value
        with self._metadata_lock:
            if self._prompt_cache_observation is not None:
                raise AIKernelRuntimeError("prompt cache observation was already recorded")
            self._prompt_cache_observation = normalized

    def model_call_failed(self) -> None:
        with self._metadata_lock:
            self._call_status = "failed"
            self._usage = None

    def model_dispatch_authority_observed(
        self,
        *,
        wait_ms: int,
        hold_ms: int,
        outcome: str,
    ) -> None:
        """Accept one fence observation without exposing a provider or root path."""

        candidate = {
            "wait_ms": wait_ms,
            "hold_ms": hold_ms,
            "outcome": outcome,
        }
        if (
            not isinstance(wait_ms, int)
            or isinstance(wait_ms, bool)
            or not isinstance(hold_ms, int)
            or isinstance(hold_ms, bool)
            or outcome not in {"completed", "failed", "timed_out"}
        ):
            raise AIKernelRuntimeError("model dispatch authority measurement is invalid")
        with self._metadata_lock:
            if self._dispatch_authority_measurement is not None:
                raise AIKernelRuntimeError("model dispatch authority measurement was already recorded")
            self._dispatch_authority_measurement = candidate

    def dispatch_authority_receipt_payload(self, *, turn_id: str) -> dict[str, object] | None:
        with self._metadata_lock:
            measurement = self._dispatch_authority_measurement
            if measurement is None:
                return None
            return validate_model_dispatch_authority_receipt({
                "schema_version": "1.0.0",
                "receipt_id": f"model-dispatch-authority-receipt-{uuid4().hex}",
                "turn_id": turn_id,
                "model_request_id": self.model_request_id,
                **measurement,
                "input_recorded": False,
                "output_recorded": False,
            })

    def begin_model_wire_attempt(self) -> "_ModelWireAttemptHandle":
        with self._metadata_lock:
            if self._active_wire_attempt_id is not None:
                raise AIKernelRuntimeError("model wire attempt is already active")
            if not all((
                self._routing_snapshot_revision,
                self._provider_id,
                self._model_id,
                self._execution_location,
            )):
                raise AIKernelRuntimeError("model wire attempt requires a frozen route")
            if self._attempt_dispatch_recorder is None or self._attempt_terminal_recorder is None:
                raise AIKernelRuntimeError("model wire attempt recorder is unavailable")
            attempt_number = self._wire_attempt_number + 1
            attempt_id = f"model-wire-attempt-{uuid4().hex}"
            dispatched_at = datetime.now(timezone.utc).isoformat()
            started_monotonic = monotonic()
            dispatch = validate_model_wire_attempt_dispatch({
                "schema_version": "1.0.0",
                "attempt_id": attempt_id,
                "turn_id": self.turn_id,
                "model_request_id": self.model_request_id,
                "attempt_number": attempt_number,
                "routing_snapshot_revision": self._routing_snapshot_revision,
                "provider_id": self._provider_id,
                "model_id": self._model_id,
                "execution_location": self._execution_location,
                "dispatched_at": dispatched_at,
                "input_stored": False,
                "output_stored": False,
            })
            stored_dispatch = self._attempt_dispatch_recorder(dispatch)
            if not isinstance(stored_dispatch, _StoredModelAttemptDispatch):
                raise AIKernelRuntimeError("model wire attempt commit result is invalid")
            dispatch_ref = stored_dispatch.dispatch_ref
            self._wire_attempt_number = attempt_number
            self._active_wire_attempt_id = attempt_id
            self._active_wire_executor = stored_dispatch.executor
        return _ModelWireAttemptHandle(
            control=self,
            dispatch=dispatch,
            dispatch_ref=dispatch_ref,
            started_monotonic=started_monotonic,
            durable_commit_witness=(
                _issue_durable_model_wire_commit_witness()
                if stored_dispatch.durable else None
            ),
            _provider_checkpoint_recorder=stored_dispatch.checkpoint_recorder,
            _provider_resume_binder=stored_dispatch.resume_binder,
        )

    def _finish_model_wire_attempt(
        self,
        *,
        dispatch: Mapping[str, object],
        dispatch_ref: str,
        started_monotonic: float,
        status: str,
        usage: Mapping[str, int] | None,
        cache_observation: Mapping[str, int] | None,
        error_code: str | None,
    ) -> None:
        with self._metadata_lock:
            if self._active_wire_attempt_id != dispatch["attempt_id"]:
                raise AIKernelRuntimeError("model wire attempt identity drifted")
        normalized_usage = _normalized_wire_usage(usage or {})
        cache_metadata = _normalized_wire_cache_metadata(cache_observation)
        receipt = validate_model_wire_attempt_receipt({
            "schema_version": "1.0.0",
            "attempt_id": dispatch["attempt_id"],
            "turn_id": dispatch["turn_id"],
            "model_request_id": dispatch["model_request_id"],
            "attempt_number": dispatch["attempt_number"],
            "routing_snapshot_revision": dispatch["routing_snapshot_revision"],
            "provider_id": dispatch["provider_id"],
            "model_id": dispatch["model_id"],
            "execution_location": dispatch["execution_location"],
            "status": status,
            "started_at": dispatch["dispatched_at"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": min(86_400_000, max(0, int((monotonic() - started_monotonic) * 1000))),
            "usage_status": "reported" if normalized_usage is not None else "unavailable",
            "usage": normalized_usage,
            "cache_status": "reported" if cache_metadata is not None else "unavailable",
            "cache_metadata": cache_metadata,
            "input_stored": False,
            "output_stored": False,
            "error_code": error_code,
        })
        assert self._attempt_terminal_recorder is not None
        receipt_ref = self._attempt_terminal_recorder(receipt, dispatch_ref)
        with self._metadata_lock:
            self._wire_attempt_receipt_refs.append(receipt_ref)
            self._active_wire_attempt_id = None
            self._active_wire_executor = None
        return receipt_ref

    @property
    def wire_attempt_receipt_refs(self) -> tuple[str, ...]:
        with self._metadata_lock:
            return tuple(self._wire_attempt_receipt_refs)

    @property
    def model_terminal(self) -> bool:
        with self._metadata_lock:
            return self._model_terminal

    def terminal_receipt_payload(
        self,
        *,
        turn_id: str,
        status: str,
        error_code: str | None,
    ) -> dict[str, object]:
        with self._metadata_lock:
            usage = dict(self._usage) if self._usage is not None else None
            payload: dict[str, object] = {
                "schema_version": "1.0.0",
                "receipt_id": f"model-receipt-{uuid4().hex}",
                "turn_id": turn_id,
                "model_request_id": self.model_request_id,
                "model_call_purpose": self.purpose,
                "status": status,
                "requested_at": self._requested_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": min(86_400_000, max(0, int((monotonic() - self._started_monotonic) * 1000))),
                "provider_id": self._provider_id,
                "model_id": self._model_id,
                "usage_status": "recorded" if usage is not None else "not_recorded",
                "usage": usage,
                "input_recorded": False,
                "output_recorded": False,
                "error_code": error_code,
            }
            return validate_model_call_receipt(payload)

    def prompt_cache_receipt_payload(self, *, turn_id: str) -> dict[str, object] | None:
        with self._metadata_lock:
            if not all((
                self._routing_snapshot_ref,
                self._routing_snapshot_revision,
                self._prompt_cache_scope_identity,
                self._provider_id,
                self._model_id,
            )):
                return None
            observation = dict(self._prompt_cache_observation or {})
            reported = bool(observation)
            payload: dict[str, object] = {
                "schema_version": "1.0.0",
                "receipt_id": f"prompt-cache-receipt-{uuid4().hex}",
                "turn_id": turn_id,
                "model_request_id": self.model_request_id,
                "routing_snapshot_revision": self._routing_snapshot_revision,
                "prompt_cache_scope_identity": self._prompt_cache_scope_identity,
                "provider_id": self._provider_id,
                "model_id": self._model_id,
                "cache_status": "reported" if reported else "unavailable",
                "source_format": "provider_usage" if reported else "unavailable",
                "cache_read_input_tokens": observation.get("cache_read_input_tokens"),
                "cache_write_input_tokens": observation.get("cache_creation_input_tokens"),
                "uncached_input_tokens": observation.get("cache_miss_input_tokens"),
                "input_recorded": False,
                "output_recorded": False,
            }
            return validate_prompt_cache_receipt(payload)

    def receipt_terminal(
        self,
        *,
        fallback_status: str,
        fallback_error_code: str | None,
    ) -> tuple[str, str | None] | None:
        with self._metadata_lock:
            if not self._call_started:
                return None
            if self._call_status == "completed":
                return "completed", None
            if self._call_status == "failed":
                return "failed", "ai.model_call_failed"
            return fallback_status, fallback_error_code

    def mark_model_terminal(self) -> None:
        with self._metadata_lock:
            self._model_terminal = True


@dataclass(frozen=True, slots=True)
class _StoredModelAttemptDispatch:
    dispatch_ref: str
    durable: bool
    executor: Callable[[Callable[[], object]], object] | None = field(
        default=None, repr=False,
    )
    checkpoint_recorder: Callable[[Mapping[str, object], str | None], str] | None = field(
        default=None, repr=False,
    )
    resume_binder: Callable[[Mapping[str, object]], str] | None = field(default=None, repr=False)


@dataclass(slots=True)
class _ModelWireAttemptHandle:
    control: _PlannerExecutionContext
    dispatch: Mapping[str, object]
    dispatch_ref: str
    started_monotonic: float
    durable_commit_witness: DurableModelWireCommitWitness | None = field(
        default=None, repr=False,
    )
    _provider_checkpoint_recorder: Callable[[Mapping[str, object], str | None], str] | None = field(
        default=None, repr=False,
    )
    _provider_checkpoint_ref: str | None = field(default=None, init=False, repr=False)
    _provider_resume_binder: Callable[[Mapping[str, object]], str] | None = field(default=None, repr=False)
    _finished: bool = field(default=False, init=False, repr=False)
    _receipt_ref: str | None = field(default=None, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def observe_provider_checkpoint(self, cursor: Mapping[str, object]) -> str:
        """Persist metadata for this active wire; a cursor grants no execution right."""
        with self._lock:
            if self._finished or self.control._active_wire_attempt_id != self.dispatch["attempt_id"]:
                raise AIKernelRuntimeError("model wire attempt is not checkpoint-eligible")
            recorder = self._provider_checkpoint_recorder
            if recorder is None:
                raise AIKernelRuntimeError("model provider checkpoint recorder is unavailable")
            ref = recorder(cursor, self._provider_checkpoint_ref)
            self._provider_checkpoint_ref = ref
            return ref

    def invoke_wire(self, handler: Callable[[], object]) -> object:
        if not callable(handler):
            raise TypeError("model wire Handler must be callable")
        executor = self.control._active_wire_executor
        if executor is None:
            if self.durable_commit_witness is not None:
                raise AIKernelRuntimeError("durable model wire Handler executor is unavailable")
            return handler()

        def execute_claimed() -> object:
            result = handler()
            with self._lock:
                if self._receipt_ref is None:
                    raise AIKernelRuntimeError("model wire Handler did not persist a Receipt")
                receipt_ref = self._receipt_ref
            return result, receipt_ref

        return executor(execute_claimed)

    def bind_provider_resume(self, source: Mapping[str, object]) -> str:
        """在原实际 Handler 内绑定来源；数据绑定不授予产品继续权限。"""
        with self._lock:
            if self._finished or self.control._active_wire_attempt_id != self.dispatch["attempt_id"]:
                raise AIKernelRuntimeError("model wire attempt is not resume-eligible")
            if self._provider_resume_binder is None:
                raise AIKernelRuntimeError("model provider resume binder is unavailable")
            return self._provider_resume_binder(source)

    def succeeded(
        self,
        *,
        usage: Mapping[str, int],
        cache_observation: Mapping[str, int] | None,
    ) -> None:
        self._finish(
            status="succeeded",
            usage=usage,
            cache_observation=cache_observation,
            error_code=None,
        )

    def failed_transport(self, *, error_code: str,
                         usage: Mapping[str, int] | None = None,
                         cache_observation: Mapping[str, int] | None = None) -> None:
        self._finish(
            status="failed_transport",
            usage=usage,
            cache_observation=cache_observation,
            error_code=error_code,
        )

    def consumer_cancelled(self, *, usage: Mapping[str, int] | None = None,
                           cache_observation: Mapping[str, int] | None = None) -> None:
        self._finish(
            status="consumer_cancelled",
            usage=usage,
            cache_observation=cache_observation,
            error_code="ai.consumer_cancelled",
        )

    def _finish(
        self,
        *,
        status: str,
        usage: Mapping[str, int] | None,
        cache_observation: Mapping[str, int] | None,
        error_code: str | None,
    ) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        receipt_ref = self.control._finish_model_wire_attempt(
            dispatch=self.dispatch,
            dispatch_ref=self.dispatch_ref,
            started_monotonic=self.started_monotonic,
            status=status,
            usage=usage,
            cache_observation=cache_observation,
            error_code=error_code,
        )
        with self._lock:
            self._receipt_ref = receipt_ref


@dataclass(slots=True)
class _NestedModelCallHandle:
    runtime: "SynchronousAIRuntime"
    turn_id: str
    step_id: str
    tool_call_id: str
    control: _PlannerExecutionContext
    _finalized: bool = field(default=False, init=False, repr=False)
    _finalize_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def model_request_id(self) -> str:
        return self.control.model_request_id

    def model_call_routed(
        self,
        *,
        snapshot_ref: str,
        snapshot_revision: str,
        prompt_cache_scope_identity: str,
        provider: str,
        model: str,
        execution_location: str,
        purpose: ModelCallPurpose = "primary",
    ) -> None:
        self.control.model_call_routed(
            snapshot_ref=snapshot_ref,
            snapshot_revision=snapshot_revision,
            prompt_cache_scope_identity=prompt_cache_scope_identity,
            provider=provider,
            model=model,
            execution_location=execution_location,
            purpose=purpose,
        )

    def model_call_started(self, *, provider: str, model: str) -> None:
        self.control.model_call_started(provider=provider, model=model)

    def model_call_completed(self, *, usage: Mapping[str, int]) -> None:
        self.control.model_call_completed(usage=usage)

    def model_call_failed(self) -> None:
        self.control.model_call_failed()

    def model_dispatch_authority_observed(
        self,
        *,
        wait_ms: int,
        hold_ms: int,
        outcome: str,
    ) -> str:
        self.control.model_dispatch_authority_observed(
            wait_ms=wait_ms,
            hold_ms=hold_ms,
            outcome=outcome,
        )

    def model_call_cache_observed(self, *, observation: Mapping[str, int]) -> None:
        self.control.model_call_cache_observed(observation=observation)

    def begin_model_wire_attempt(self) -> _ModelWireAttemptHandle:
        return self.control.begin_model_wire_attempt()

    def finalize(self, *, error_code: str | None) -> tuple[str, ...]:
        with self._finalize_lock:
            if self._finalized:
                raise AIKernelRuntimeError("nested model lifecycle was already finalized")
            self._finalized = True
        return self.runtime._record_nested_model_terminal(
            self.turn_id,
            self.control,
            tool_call_id=self.tool_call_id,
            fallback_status="failed" if error_code is not None else "completed",
            fallback_error_code=error_code,
        )


class SynchronousAIRuntime:
    def __init__(self, *, planner: AgentPlannerPort, registry: CapabilityRegistryPort, events: TurnEventStorePort, payloads: TurnPayloadStorePort, state: TurnStateStorePort | None = None, manifest_resolver: CapabilityManifestResolverPort | None = None, context_manifest_resolver: ContextManifestResolverPort | None = None, expert_binding: ExpertTurnBindingPort | None = None, expert_memory_proposal_sink: object | None = None, execution_boundary: ToolExecutionBoundaryPort | None = None, dispatcher: ToolDispatcherPort | None = None, hook_host: CodexHookHost | None = None, frozen_authorization: FrozenAuthorizationRuntimePort | None = None, frozen_hook_authorization_check: Callable[[Mapping[str, object], CapabilityDefinition, Mapping[str, object]], bool] | None = None, mcp_continuation_reconnector: Callable[[str], None] | None = None, external_tool_outcome_projector: Callable[[ToolInvocationIntent, Effect], Mapping[str, object] | None] | None = None, effect_runner: EffectRunner | None = None, max_steps: int = 8, planner_timeout_ms: int = 120_000) -> None:
        if not isinstance(planner_timeout_ms, int) or isinstance(planner_timeout_ms, bool) or planner_timeout_ms < 1:
            raise ValueError("planner timeout must be a positive integer")
        self._planner = planner
        self._registry = registry
        self._events = events
        self._payloads = payloads
        self._state = state or InMemoryTurnStateStore()
        self._atomic_store = (
            self._state
            if self._events is self._payloads is self._state
            and isinstance(self._state, AtomicTurnBundlePort)
            else None
        )
        self._manifest_resolver = manifest_resolver or V1TurnPolicyCapabilityManifestResolver()
        self._context_manifest_resolver = context_manifest_resolver or V1TurnContextManifestResolver()
        self._expert_binding = expert_binding
        self._expert_memory_proposal_sink = expert_memory_proposal_sink
        self._execution_boundary = execution_boundary
        self._dispatcher = dispatcher or SynchronousToolDispatcher()
        self._hook_host = hook_host
        if hook_host is not None and frozen_authorization is None and frozen_hook_authorization_check is None:
            raise ValueError("hook-enabled runtime requires a frozen authorization fact check")
        self._frozen_authorization = frozen_authorization
        self._frozen_hook_authorization_check = frozen_hook_authorization_check
        self._mcp_continuation_reconnector = mcp_continuation_reconnector
        self._external_tool_outcome_projector = external_tool_outcome_projector
        self._effect_runner = effect_runner
        self._expert_job_terminal_verifier: Callable[[Mapping[str, object]], bool] | None = None
        self._expert_job_terminal_verifier_lock = RLock()
        self._agent_tool_budget_reader: Callable[[Mapping[str, object]], int] | None = None
        self._agent_tool_budget_reader_lock = RLock()
        self._hook_snapshots: dict[str, tuple[str, HookPolicySnapshot]] = {}
        self._hook_snapshots_lock = RLock()
        self._max_steps = max_steps
        self._planner_timeout_ms = planner_timeout_ms
        self._planner_controls: dict[str, _PlannerExecutionContext] = {}
        self._planner_controls_lock = RLock()
        self._run_lease_context: ContextVar[RunLeaseToken | None] = ContextVar("ai_run_lease", default=None)
        self._event_locks_guard = RLock()
        self._event_locks: dict[str, tuple[RLock, int]] = {}
        self._batch_terminals: ContextVar[list | None] = ContextVar("ai_batch_terminals", default=None)
        self._batch_waitings: ContextVar[list | None] = ContextVar("ai_batch_waitings", default=None)

    @contextmanager
    def _event_commit(self, turn_id):
        with self._event_locks_guard:
            lock, users = self._event_locks.get(turn_id, (RLock(), 0))
            self._event_locks[turn_id] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._event_locks_guard:
                _, users = self._event_locks[turn_id]
                if users == 1:
                    del self._event_locks[turn_id]
                else:
                    self._event_locks[turn_id] = (lock, users - 1)

    def submit_turn(self, request: Mapping[str, object]) -> TurnReceipt:
        receipt = self.accept_turn(request)
        if receipt.replayed:
            return receipt
        return self.run_accepted_turn(receipt.turn_id)

    def capability_registry_snapshot(self):
        """Expose a provider-free registry snapshot for local diagnostics.

        Production composition uses ``ScopedCapabilityRegistry``.  Test-only
        registry ports that do not support snapshots fail closed rather than
        exposing their providers through this runtime.
        """
        snapshot = getattr(self._registry, "snapshot", None)
        if not callable(snapshot):
            raise AIKernelRuntimeError("capability registry snapshot is unavailable")
        return snapshot()

    def configure_agent_tool_budget_reader(self, reader: Callable[[Mapping[str, object]], int]) -> None:
        """Bind the application's frozen Agent Run authority exactly once."""
        if not callable(reader):
            raise ValueError('Agent tool budget reader must be callable')
        with self._agent_tool_budget_reader_lock:
            if self._agent_tool_budget_reader is not None and self._agent_tool_budget_reader != reader:
                raise AIKernelRuntimeError('Agent tool budget reader is already configured')
            self._agent_tool_budget_reader = reader

    def _check_tool_call_budget(self, turn_id, invocation_ids):
        request = self._require_request(turn_id)
        limit = self._max_steps
        policy = request.get('execution_policy')
        if isinstance(policy, Mapping):
            limit = min(limit, policy['budget']['max_steps'])
        if request.get('agent_binding') is not None:
            if self._agent_tool_budget_reader is None:
                raise AIKernelRuntimeError('frozen Agent tool budget authority is unavailable')
            frozen_limit = self._agent_tool_budget_reader(request)
            if type(frozen_limit) is not int or frozen_limit < 0:
                raise AIKernelRuntimeError('frozen Agent tool budget is invalid')
            limit = min(limit, frozen_limit)
        events = tuple(self._events.events_after(turn_id))
        reserved = {_correlation_text(event, 'tool_call_id') for event in events
                    if event.get('type') in {'tool.requested', 'tool.intent.recorded'}}
        reserved.discard(None)
        # An immutable batch reserves all members even before its first
        # approval or intent. The planner's durable request anchors that fact.
        for event in events:
            if event.get('type') != 'model.requested' or _correlation_text(event, 'tool_call_id') is not None:
                continue
            step_id = _correlation_text(event, 'step_id')
            stored = self._payloads.get_immutable_payload(turn_id, f'tool-batch-v1-{step_id}')
            if stored is None:
                continue
            batch = stored[1]
            if (batch.get('step_id') != step_id
                or batch.get('model_request_id') != _correlation_text(event, 'model_request_id')):
                raise AIKernelRuntimeError('tool budget batch identity drifted')
            reserved.update(call['_execution']['tool_call_id'] for call in batch['calls'])
        if len(reserved | set(invocation_ids)) > limit:
            raise AIKernelRuntimeError('frozen tool call budget is exhausted')

    def accept_turn(self, request: Mapping[str, object]) -> TurnReceipt:
        """Durably accept a Turn without binding its execution to the caller."""
        payload = validate_turn_request(request)
        try:
            turn_id, created = self._state.claim_turn(payload)
        except TurnStateConflict as error:
            raise AIKernelRuntimeError(str(error)) from error
        existing = tuple(self._events.events_after(turn_id))
        if not created and existing:
            if not any(
                event.get("type") in {"turn.completed", "turn.failed", "turn.cancelled"}
                for event in existing
            ):
                try:
                    self._ensure_subagent_start_hook(turn_id, payload)
                except Exception as error:
                    return self._fail(turn_id, error)
            return self._receipt(turn_id, replayed=True)
        if self._hook_host is not None:
            self._freeze_hook_snapshot(turn_id)
        if not existing:
            try:
                self._append(turn_id, "turn.accepted", "accepted", "turn accepted")
            except Exception:
                # A concurrent process may have won the append after the
                # durable idempotency claim.  Never create a second stream.
                existing = tuple(self._events.events_after(turn_id))
                if existing:
                    return self._receipt(turn_id, replayed=True)
                raise
        # A coordinator-issued child binding is structurally validated with the
        # Turn request.  Its authority remains separately verified by the
        # model-routing path; this Hook observation cannot grant any authority.
        # Record it after the per-Turn Hook snapshot and acceptance boundary so
        # a restart can safely repair a missing observation without duplicating
        # one that was already made durable.
        try:
            self._ensure_subagent_start_hook(turn_id, payload)
        except Exception as error:
            return self._fail(turn_id, error)
        if self._hook_event_enabled(turn_id, HookEvent.SESSION_START) and not any(
            event.get("type") == "hook.invoked"
            and self._hook_event_for(event) is HookEvent.SESSION_START
            for event in self._events.events_after(turn_id)
        ):
            try:
                session = self.invoke_lifecycle_hook(
                    turn_id,
                    HookEvent.SESSION_START,
                    {"turn_id": turn_id, "session_id": payload["session_id"]},
                )
            except Exception as error:
                return self._fail(turn_id, error)
            if session.outcome.should_stop:
                self._append_turn_terminal(
                    turn_id, "turn.cancelled", "cancelled",
                    "SessionStart hook stopped the Turn",
                    error_code="ai.hook_session_start_stopped",
                )
                return self._receipt(turn_id)
        if self._hook_event_enabled(turn_id, HookEvent.USER_PROMPT_SUBMIT) and not any(
            event.get("type") == "hook.invoked"
            and self._hook_event_for(event) is HookEvent.USER_PROMPT_SUBMIT
            for event in self._events.events_after(turn_id)
        ):
            try:
                prompt = self.invoke_lifecycle_hook(
                    turn_id,
                    HookEvent.USER_PROMPT_SUBMIT,
                    {
                        "turn_id": turn_id,
                        "session_id": payload["session_id"],
                        "operation_id": payload["operation_id"],
                    },
                )
            except Exception as error:
                return self._fail(turn_id, error)
            if prompt.outcome.additional_context:
                context_ref = self._payloads.put(
                    turn_id,
                    "codex-hook-additional-context",
                    {
                        "schema_version": "1.0.0",
                        "event": HookEvent.USER_PROMPT_SUBMIT.value,
                        "items": list(prompt.outcome.additional_context),
                    },
                )
                self._append(
                    turn_id, "hook.context", "running",
                    "UserPromptSubmit hook supplied planner context",
                    payload_ref=context_ref,
                )
            if prompt.outcome.should_stop:
                self._append_turn_terminal(
                    turn_id, "turn.cancelled", "cancelled",
                    "UserPromptSubmit hook stopped the Turn",
                    error_code="ai.hook_prompt_stopped",
                )
                return self._receipt(turn_id)
        return self._receipt(turn_id, replayed=not created)

    def run_accepted_turn(self, turn_id: str, run_lease: RunLeaseToken | None = None) -> TurnReceipt:
        scope = self._bind_run_lease(turn_id, run_lease)
        try:
            from contextlib import nullcontext
            from core.storage_provider.connection_scope import connection_scope
            request = self._require_request(turn_id)
            with connection_scope() if request.get("desired_outcome") == "project.answer" else nullcontext():
                return self._run_accepted_turn(turn_id)
        finally:
            self._run_lease_context.reset(scope)

    def resume_expert_job_wait(
        self, turn_id: str, run_lease: RunLeaseToken,
    ) -> TurnReceipt:
        """Finish one frozen terminal Job wake without re-entering Planner/Tools."""
        wait = self._state.get_expert_job_wait(turn_id)
        if not isinstance(wait, Mapping) or wait.get("status") != "wake_enqueued":
            raise AIKernelRuntimeError("expert Job wake is not ready for delivery")
        scope = self._bind_run_lease(turn_id, run_lease)
        try:
            stored = self._payloads.get_immutable_payload(
                turn_id, "expert-job-terminal-snapshot-v1",
            )
            if not isinstance(stored, tuple) or len(stored) != 2 or not isinstance(stored[0], str) or not isinstance(stored[1], Mapping):
                raise AIKernelRuntimeError("expert Job terminal snapshot is unavailable")
            snapshot_ref, terminal = stored
            if (
                terminal.get("turn_id") != turn_id
                or terminal.get("canonical_job_ref") != wait.get("job_ref")
                or terminal.get("job_revision") != wait.get("terminal_job_revision")
                or terminal.get("status") not in {"completed", "failed", "cancelled"}
            ):
                raise AIKernelRuntimeError("expert Job terminal snapshot drifted")
            event_type, status = {
                "completed": ("expert.job.observed", "running"),
                "failed": ("turn.failed", "failed"),
                "cancelled": ("turn.cancelled", "cancelled"),
            }[str(terminal["status"])]
            finalize = getattr(self._state, "finalize_expert_job_terminal_bundle", None)
            if not callable(finalize):
                raise AIKernelRuntimeError("durable expert Job terminal finalizer is unavailable")
            receipt_ref = terminal.get("receipt_ref")
            if terminal["status"] == "completed" and not isinstance(receipt_ref, str):
                raise AIKernelRuntimeError("completed expert Job terminal receipt is invalid")
            if terminal["status"] != "completed" and receipt_ref is not None:
                raise AIKernelRuntimeError("failed expert Job terminal receipt drifted")
            with self._event_commit(turn_id):
                event = self._new_event(
                    turn_id, event_type, status, "expert Media Job terminal observed",
                    payload_ref=snapshot_ref,
                    evidence_refs=((str(receipt_ref),) if isinstance(receipt_ref, str) else ()),
                )
                finalize(
                    event, expected_sequence=int(event["sequence"]) - 1,
                    job_ref=str(wait["job_ref"]),
                    admission_job_revision=int(wait["admission_job_revision"]),
                    terminal_job_revision=int(wait["terminal_job_revision"]),
                    run_lease=run_lease,
                )
            if terminal["status"] == "completed":
                # Media completion is governed evidence for the expert, not
                # the expert's final answer. Continue the same frozen Turn so
                # synthesis can consume Context and emit its own Receipt.
                # A crash after the atomic observation is a safe resume and
                # never executes the Media Job again.
                return self.run_accepted_turn(turn_id, run_lease)
            return self._receipt(turn_id)
        finally:
            self._run_lease_context.reset(scope)

    def record_expert_job_terminal(
        self, turn_id: str, terminal: Mapping[str, object], run_lease: RunLeaseToken,
    ) -> str:
        """Atomically persist the verified Media terminal evidence before wake."""
        scope = self._bind_run_lease(turn_id, run_lease)
        try:
            wait = self._state.get_expert_job_wait(turn_id)
            if not isinstance(wait, Mapping) or wait.get("status") != "waiting":
                raise AIKernelRuntimeError("expert Job terminal is not ready for persistence")
            required = {
                "schema_version", "turn_id", "canonical_job_ref", "terminal_job_id", "job_revision", "status",
                "receipt_ref", "terminal_evidence", "source_manifest_ref", "source_manifest_revision",
            }
            if set(terminal) != required or terminal.get("schema_version") != "1.0.0":
                raise AIKernelRuntimeError("expert Job terminal schema is invalid")
            job_ref = terminal.get("canonical_job_ref")
            terminal_job_id = terminal.get("terminal_job_id")
            job_revision = terminal.get("job_revision")
            receipt_ref = terminal.get("receipt_ref")
            evidence = terminal.get("terminal_evidence")
            wait_stored = self._payloads.get_immutable_payload(turn_id, "expert-job-wait-snapshot-v1")
            wait_snapshot = wait_stored[1] if isinstance(wait_stored, tuple) and len(wait_stored) == 2 else None
            if (
                not isinstance(job_ref, str) or not isinstance(terminal_job_id, str) or not isinstance(job_revision, int)
                or terminal.get("turn_id") != turn_id or job_ref != wait.get("job_ref")
                or job_revision < int(wait.get("admission_job_revision", 0))
                or not isinstance(wait_snapshot, Mapping)
                or terminal.get("source_manifest_ref") != wait_snapshot.get("source_manifest_ref")
                or terminal.get("source_manifest_revision") != wait_snapshot.get("source_manifest_revision")
                or terminal.get("status") not in {"completed", "failed", "cancelled"}
                or not _valid_expert_job_terminal_evidence(
                    evidence, status=str(terminal.get("status")), job_ref=job_ref, terminal_job_id=terminal_job_id,
                    job_revision=job_revision, receipt_ref=receipt_ref,
                )
            ):
                raise AIKernelRuntimeError("expert Job terminal identity is invalid")
            if terminal.get("status") == "completed":
                verifier = self._expert_job_terminal_verifier
                if verifier is None or not verifier(terminal):
                    raise AIKernelRuntimeError("expert Job terminal receipt is not verified by Job authority")
            append = getattr(self._state, "append_expert_job_terminal_bundle", None)
            if not callable(append):
                raise AIKernelRuntimeError("durable expert Job terminal store is unavailable")
            with self._event_commit(turn_id):
                event = self._new_event(turn_id, "expert.job.terminal", "wake_enqueued", "expert Media Job terminal recorded")
                receipt = append(
                    event, expected_sequence=int(event["sequence"]) - 1,
                    immutable_kind="expert-job-terminal-snapshot-v1", immutable_payload=dict(terminal),
                    job_ref=job_ref, admission_job_revision=int(wait["admission_job_revision"]),
                    terminal_job_revision=job_revision, run_lease=run_lease,
                )
            return receipt.immutable_payload_ref
        finally:
            self._run_lease_context.reset(scope)

    def configure_expert_job_terminal_verifier(
        self, verifier: Callable[[Mapping[str, object]], bool],
    ) -> None:
        """Install the sole Job-authority verifier before terminal delivery.

        The Kernel does not read the Job database itself. Production composition
        registers one verifier that re-reads the durable execution receipt from
        the existing Job authority. Single assignment prevents runtime swapping.
        """
        if not callable(verifier):
            raise ValueError("expert Job terminal verifier must be callable")
        with self._expert_job_terminal_verifier_lock:
            if self._expert_job_terminal_verifier is not None:
                raise AIKernelRuntimeError("expert Job terminal verifier is already configured")
            self._expert_job_terminal_verifier = verifier

    def recover_accepted_turn(self, turn_id: str, run_lease: RunLeaseToken) -> TurnReceipt:
        """Resume only an audited, fenced recovery generation.

        An approved decision is resumed from its durable pending record; an
        existing intent is resumed before asking the planner for new work.
        """
        scope = self._bind_run_lease(turn_id, run_lease)
        try:
            events = tuple(self._events.events_after(turn_id))
            if not events:
                raise AIKernelRuntimeError("accepted Turn was not found")
            if events[-1].get("type") in {"turn.completed", "turn.failed", "turn.cancelled"}:
                return self._receipt(turn_id, replayed=True)
            if self._hook_host is not None and self._frozen_authorization is not None:
                try:
                    self._frozen_authorization.load_current(turn_id=turn_id)
                except Exception as error:
                    return self._fail(turn_id, error)
            pending = self._state.get_pending(turn_id)
            approval_resolution = _latest_approval_resolution(events)
            if pending is not None and approval_resolution == 'approve' and '_tool_batch_step_id' in pending:
                recovered_pending = self._restore_frozen_approval(turn_id, pending, events)
                self._save_batch_approval(turn_id, recovered_pending)
                self._state.clear_pending(turn_id)
            if pending is not None and approval_resolution == "reject":
                self._state.clear_pending(turn_id)
                self._append_turn_terminal(
                    turn_id, "turn.cancelled", "cancelled",
                    "rejected approval converged during recovery", actor="human",
                )
                return self._receipt(turn_id)
            resumed, receipt = self._resume_incomplete_tool(turn_id)
            if resumed:
                if pending is not None and approval_resolution == "approve":
                    self._state.clear_pending(turn_id)
                return receipt or self._run(turn_id)
            if pending is not None and approval_resolution == "approve":
                try:
                    recovered_pending = self._restore_frozen_approval(
                        turn_id, pending, events
                    )
                    self._invoke_tool(turn_id, recovered_pending)
                except _ToolTurnConverged:
                    return self._receipt(turn_id)
                except Exception as error:
                    self._state.clear_pending(turn_id)
                    return self._fail(turn_id, error)
                self._state.clear_pending(turn_id)
                return self._run(turn_id)
            return self._run_accepted_turn(turn_id)
        finally:
            self._run_lease_context.reset(scope)

    def _restore_frozen_approval(
        self,
        turn_id: str,
        pending: Mapping[str, object],
        events: tuple[Mapping[str, object], ...],
    ) -> Mapping[str, object]:
        if pending.get("_frozen_authorization_ref") is None:
            return pending
        if self._frozen_authorization is None:
            raise AIKernelRuntimeError("frozen authorization authority is unavailable")
        resolved_event = next(
            (event for event in reversed(events) if event.get("type") == "approval.resolved"),
            None,
        )
        data = resolved_event.get("data") if isinstance(resolved_event, Mapping) else None
        refs = data.get("evidence_refs") if isinstance(data, Mapping) else None
        action_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        execution = pending.get("_execution")
        tool_call_id = execution.get("tool_call_id") if isinstance(execution, Mapping) else None
        if (
            not isinstance(refs, list)
            or len(refs) != 1
            or not isinstance(refs[0], str)
            or not isinstance(action_ref, str)
            or not isinstance(tool_call_id, str)
        ):
            raise AIKernelRuntimeError("frozen approval recovery evidence is unavailable")
        handle = self._frozen_authorization.load_approval(
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            approval_ref=refs[0],
            approval_revision="approval-v1",
        )
        fact = getattr(handle, "fact", None)
        target_event_id = getattr(fact, "target_event_id", None)
        if not isinstance(target_event_id, str) or getattr(fact, "action_ref", None) != action_ref:
            raise AIKernelRuntimeError("frozen approval recovery binding drifted")
        result = dict(pending)
        result["_frozen_approval_ref"] = refs[0]
        result["_frozen_approval_revision"] = "approval-v1"
        result["_frozen_approval_action_ref"] = action_ref
        result["_frozen_approval_target_event_id"] = target_event_id
        return result

    def _run_accepted_turn(self, turn_id: str) -> TurnReceipt:
        """Continue one already accepted Turn; safe for an application runner."""
        events = tuple(self._events.events_after(turn_id))
        if not events:
            raise AIKernelRuntimeError("accepted Turn was not found")
        if events[-1].get("type") in {"turn.completed", "turn.failed", "turn.cancelled"}:
            return self._receipt(turn_id, replayed=True)
        request = self._require_request(turn_id)
        context_already_resolved = any(
            event.get("type") == "context.resolved" for event in events
        )
        if not context_already_resolved:
            try:
                manifest = self._resolve_manifest(request)
                manifest_payload = manifest_to_payload(manifest)
                validate_governed_payload(manifest_payload)
                manifest_ref = self._payloads.put(turn_id, "capability-manifest", manifest_payload)
                self._ensure_expert_selection(
                    turn_id,
                    request,
                    manifest,
                    allow_create=True,
                )
                context_manifest = self._context_manifest_resolver.resolve(request, manifest_ref, manifest)
                validate_context_manifest_for_request(
                    context_manifest, request, capability_manifest=manifest
                )
                context_payload = context_manifest_to_payload(context_manifest)
                validate_governed_payload(context_payload)
                context_ref = self._payloads.put(turn_id, "context-manifest", context_payload)
                self._append(turn_id, "context.resolved", "running", "context policy resolved", payload_ref=context_ref)
                if self._hook_host is not None and self._frozen_authorization is not None:
                    self._frozen_authorization.issue(
                        request,
                        capability_manifest_ref=manifest_ref,
                        context_manifest_ref=context_ref,
                        capabilities=self._manifest_capabilities(manifest),
                    )
            except Exception as error:
                return self._fail(turn_id, error)
        elif self._hook_host is not None and self._frozen_authorization is not None:
            try:
                try:
                    self._frozen_authorization.load_current(turn_id=turn_id)
                except Exception:
                    context_event = next(
                        event for event in reversed(events)
                        if event.get("type") == "context.resolved"
                    )
                    context_ref = context_event.get("data", {}).get("payload_ref")
                    if not isinstance(context_ref, str):
                        raise AIKernelRuntimeError("durable context manifest ref is unavailable")
                    context_manifest = context_manifest_from_payload(
                        self._payloads.get(context_ref)
                    )
                    manifest_ref = context_manifest.capability_manifest_ref
                    manifest = manifest_from_payload(
                        self._payloads.get(manifest_ref)
                    )
                    self._frozen_authorization.issue(
                        request,
                        capability_manifest_ref=manifest_ref,
                        context_manifest_ref=context_ref,
                        capabilities=self._manifest_capabilities(manifest),
                    )
            except Exception as error:
                return self._fail(turn_id, error)
        if self._expert_binding is not None:
            try:
                manifest = self._manifest_for(turn_id, request)
                context_event = next(
                    event for event in reversed(tuple(self._events.events_after(turn_id)))
                    if event.get("type") == "context.resolved"
                )
                context_data = context_event.get("data")
                context_ref = (
                    context_data.get("payload_ref")
                    if isinstance(context_data, Mapping) else None
                )
                if not isinstance(context_ref, str):
                    raise AIKernelRuntimeError("durable context manifest ref is unavailable")
                context_manifest = context_manifest_from_payload(
                    self._payloads.get(context_ref)
                )
                selection = self._ensure_expert_selection(
                    turn_id,
                    request,
                    manifest,
                    allow_create=not context_already_resolved,
                )
                if selection is not None:
                    self._ensure_expert_binding_snapshot(
                        turn_id,
                        request,
                        selection,
                        manifest,
                        context_manifest,
                    )
            except Exception as error:
                return self._fail(turn_id, error)
        return self._run(turn_id)

    def fail_accepted_turn(self, turn_id: str, run_lease: RunLeaseToken | None = None) -> TurnReceipt:
        scope = self._bind_run_lease(turn_id, run_lease)
        try:
            return self._fail_accepted_turn(turn_id)
        finally:
            self._run_lease_context.reset(scope)

    def _fail_accepted_turn(self, turn_id: str) -> TurnReceipt:
        """Converge an unexpected host-runner failure without exposing it."""
        events = tuple(self._events.events_after(turn_id))
        if not events:
            raise AIKernelRuntimeError("accepted Turn was not found")
        if events[-1].get("type") in {"turn.completed", "turn.failed", "turn.cancelled"}:
            return self._receipt(turn_id, replayed=True)
        return self._fail(turn_id, AIKernelRuntimeError("background Turn execution failed"))

    def try_claim_run_lease(self, turn_id: str, owner_id: str) -> int | None:
        return self._state.try_claim_run_lease(turn_id, owner_id)

    def release_run_lease(self, turn_id: str, owner_id: str, generation: int) -> None:
        self._state.release_run_lease(turn_id, owner_id, generation)

    def try_acquire_run_lease(self, turn_id: str, owner_id: str, *, now: datetime, stale_after: datetime) -> RunLeaseToken | None:
        return self._state.try_acquire_run_lease(turn_id, owner_id, now=now, stale_after=stale_after)

    def release_strict_run_lease(self, run_lease: RunLeaseToken) -> None:
        self._state.release_strict_run_lease(run_lease)

    def renew_run_lease(self, run_lease: RunLeaseToken, *, now: datetime, stale_after: datetime):
        return self._state.renew_run_lease(run_lease, now=now, stale_after=stale_after)

    def request_background_cancel(self, turn_id: str, run_lease: RunLeaseToken | None = None) -> bool:
        scope = self._bind_run_lease(turn_id, run_lease)
        try:
            control = self._request_planner_cancel(turn_id, reason="application shutdown", payload_ref=None)
            if control is not None:
                return True
            if self._pending_tool_batch(turn_id) is not None:
                self._append(turn_id, 'turn.cancel.requested', 'running', 'application shutdown')
                return self._cancel_batch_dispatches(turn_id)
            incomplete = self._incomplete_tool_intent(turn_id)
            return incomplete is not None and self._dispatcher.request_cancel(incomplete[0].invocation_id)
        finally:
            self._run_lease_context.reset(scope)

    def _cancel_batch_dispatches(self, turn_id):
        events = tuple(self._events.events_after(turn_id))
        finished = {_correlation_text(event, 'tool_call_id') for event in events
                    if event.get('type') == 'tool.outcome.recorded'}
        requested = False
        for event in events:
            if event.get('type') != 'tool.intent.recorded':
                continue
            call_id = _correlation_text(event, 'tool_call_id')
            if call_id is not None and call_id not in finished:
                requested = self._dispatcher.request_cancel(call_id) or requested
        return requested

    def events_after(self, turn_id: str, after_sequence: int = 0) -> Iterable[Mapping[str, object]]:
        return self._events.events_after(turn_id, after_sequence)

    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt:
        """Return the current durable Turn receipt without advancing execution."""
        return self._receipt(turn_id, replayed=replayed)

    def presentation_for(self, turn_id: str) -> Mapping[str, object] | None:
        events = tuple(self._events.events_after(turn_id))
        if not events or events[-1].get("type") != "turn.completed":
            return None
        data = events[-1].get("data")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if not isinstance(payload_ref, str):
            return None
        try:
            artifact = validate_turn_presentation_artifact(self._payloads.get(payload_ref))
        except (AIKernelContractError, KeyError, ValueError):
            return None
        return dict(artifact["content"])  # type: ignore[arg-type]

    def execution_projection_for(
        self,
        turn_id: str,
        view: ExecutionProjectionView = "simple",
    ) -> Mapping[str, object]:
        events = tuple(self._events.events_after(turn_id))
        return build_execution_projection(
            events,
            view=view,
            payload_loader=self._payloads.get,
        )

    def apply_action(
        self,
        action: Mapping[str, object],
        run_lease: RunLeaseToken | None = None,
    ) -> TurnReceipt:
        """Apply an action under the runner lease when it may start a Tool.

        Direct callers remain compatible for existing low-risk action paths.
        Application runners pass the lease so approval resolution, Tool intent,
        nested model attempts and terminal convergence share one durable fence.
        """
        turn_id = action.get("turn_id") if isinstance(action, Mapping) else None
        if not isinstance(turn_id, str):
            # Preserve validate_turn_action's canonical error rather than
            # accidentally binding an untrusted identity.
            return self._apply_action(action)
        scope = self._bind_run_lease(turn_id, run_lease)
        try:
            return self._apply_action(action)
        finally:
            self._run_lease_context.reset(scope)

    def _apply_action(self, action: Mapping[str, object]) -> TurnReceipt:
        payload = validate_turn_action(action)
        action_key = str(payload["idempotency_key"])
        prior_action = self._state.get_action(action_key)
        if prior_action is not None:
            prior_payload, prior = prior_action
            if dict(prior_payload) != payload:
                raise AIKernelRuntimeError("turn action idempotency identity conflict")
            return TurnReceipt(prior.turn_id, prior.session_id, prior.operation_id, prior.status, prior.current_sequence, True)
        turn_id = str(payload["turn_id"])
        events = tuple(self._events.events_after(turn_id))
        recovered = self._recover_applied_action(payload, events)
        if recovered is not None:
            return recovered
        if not events or int(payload["expected_sequence"]) != len(events):
            raise AIKernelRuntimeError("turn action expected sequence conflict")
        if payload["type"] == "resume":
            if events[-1].get("type") in {"turn.completed", "turn.failed", "turn.cancelled"}:
                raise AIKernelRuntimeError("terminal turn cannot resume")
            action_ref = self._payloads.put(turn_id, "turn-action", payload)
            self._append(
                turn_id,
                "turn.resumed",
                "running",
                str(payload["reason"]),
                actor="user",
                payload_ref=action_ref,
            )
            try:
                _, terminal = self._resume_incomplete_tool(turn_id)
                receipt = terminal or self._run(turn_id)
            except _ToolTurnConverged:
                receipt = self._receipt(turn_id)
            except Exception as error:
                receipt = self._fail(turn_id, error)
            self._state.save_action(payload, receipt)
            return receipt
        if payload["type"] == "cancel":
            action_ref = self._payloads.put(turn_id, "turn-action", payload)
            planner_control = self._request_planner_cancel(
                turn_id,
                reason=str(payload["reason"]),
                payload_ref=action_ref,
            )
            if planner_control is not None:
                return self._receipt(turn_id)
            latest_events = tuple(self._events.events_after(turn_id))
            if latest_events and latest_events[-1].get("type") in {
                "turn.completed",
                "turn.failed",
                "turn.cancelled",
            }:
                raise AIKernelRuntimeError("terminal turn cannot cancel")
            if self._pending_tool_batch(turn_id) is not None:
                self._append(turn_id, 'turn.cancel.requested', 'running', str(payload['reason']),
                             actor='user', payload_ref=action_ref)
                if self._cancel_batch_dispatches(turn_id):
                    return self._receipt(turn_id)
                self._state.clear_pending(turn_id)
                self._resume_tool_batch(turn_id)
                return self._remember_action(payload, turn_id)
            incomplete = self._incomplete_tool_intent(turn_id)
            if incomplete is not None:
                intent, _ = incomplete
                self._append(
                    turn_id,
                    "turn.cancel.requested",
                    "running",
                    str(payload["reason"]),
                    actor="user",
                    payload_ref=action_ref,
                    step_id=intent.step_id,
                    tool_call_id=intent.invocation_id,
                )
                if self._dispatcher.request_cancel(intent.invocation_id):
                    return self._receipt(turn_id)
                attempts = self._tool_attempt_count(turn_id, intent.invocation_id)
                if attempts:
                    retry_failure = self._latest_retryable_attempt_failure(intent)
                    if retry_failure is not None:
                        self._record_cancelled_tool_outcome(
                            intent,
                            error_code="ai.tool_cancelled",
                        )
                        return self._remember_action(payload, turn_id)
                    return self._record_unknown_tool_outcome(
                        intent,
                        attempt=attempts,
                        error_code="ai.tool_cancel_unconfirmed",
                    )
                self._record_cancelled_tool_outcome(intent, error_code="ai.tool_cancelled")
                return self._remember_action(payload, turn_id)
            self._state.clear_pending(turn_id)
            self._append_turn_terminal(turn_id, "turn.cancelled", "cancelled", str(payload["reason"]), actor="user", payload_ref=action_ref)
            return self._remember_action(payload, turn_id)
        pending = self._pending_decision(turn_id, events[-1])
        if pending is None or payload["target_event_id"] != events[-1]["event_id"]:
            raise AIKernelRuntimeError("turn has no matching pending approval")
        if payload["type"] in {"mcp_continue", "mcp_reject"} and pending.get("_kind") != "mcp_request_state_v1":
            raise AIKernelRuntimeError("turn has no matching MCP continuation")
        action_ref = (
            f"crp://session/{turn_id}/turn-action/{payload['action_id']}"
            if self._atomic_store is not None and payload["type"] == "approve"
            and self._frozen_authorization is not None
            else self._payloads.put(turn_id, "turn-action", payload)
        )
        approved_decision = dict(pending)
        approval_evidence: tuple[str, ...] = ()
        if payload["type"] == "approve" and self._frozen_authorization is not None:
            try:
                facts_ref = approved_decision.get("_frozen_authorization_ref")
                facts_revision = approved_decision.get("_frozen_authorization_revision")
                execution = approved_decision.get("_execution")
                tool_call_id = execution.get("tool_call_id") if isinstance(execution, Mapping) else None
                resolved = self._registry.resolve(str(approved_decision["capability_id"]))
                if (
                    not isinstance(facts_ref, str)
                    or not isinstance(facts_revision, str)
                    or not isinstance(tool_call_id, str)
                    or resolved is None
                ):
                    raise AIKernelRuntimeError("frozen approval binding is unavailable")
                approval = self._frozen_authorization.create_approval(
                    turn_id=turn_id,
                    facts_ref=facts_ref,
                    facts_revision=facts_revision,
                    capability=resolved[0],
                    tool_call_id=tool_call_id,
                    action_ref=action_ref,
                    target_event_id=str(payload["target_event_id"]),
                )
                approval_ref = getattr(approval, "approval_ref", None)
                approval_revision = getattr(approval, "approval_revision", None)
                if not isinstance(approval_ref, str) or not isinstance(approval_revision, str):
                    raise AIKernelRuntimeError("frozen approval evidence is invalid")
            except Exception as error:
                receipt = self._fail(turn_id, error)
                self._state.save_action(payload, receipt)
                return receipt
            approved_decision["_frozen_approval_ref"] = approval_ref
            approved_decision["_frozen_approval_revision"] = approval_revision
            approved_decision["_frozen_approval_action_ref"] = action_ref
            approved_decision["_frozen_approval_target_event_id"] = str(payload["target_event_id"])
            approval_evidence = (approval_ref,)
        if (
            payload["type"] == "approve"
            and self._atomic_store is not None
            and approval_evidence
        ):
            execution = approved_decision.get("_execution")
            tool_call_id = execution.get("tool_call_id") if isinstance(execution, Mapping) else None
            if not isinstance(tool_call_id, str):
                raise AIKernelRuntimeError("approval tool call binding is unavailable")
            with self._event_commit(turn_id):
                event = self._new_event(
                    turn_id, "approval.resolved", "running", str(payload["type"]),
                    actor="human", capability_id=str(pending["capability_id"]),
                    payload_ref=action_ref,
                )
                self._atomic_store.append_approval_bundle(
                    event,
                    expected_sequence=int(event["sequence"]) - 1,
                    action_kind="turn-action",
                    action_payload=payload,
                    approval_kind=f"frozen-tool-approval-fact-v1:{tool_call_id}",
                    approval_payload=self._payloads.get(approval_evidence[0]),
                    run_lease=self._run_lease_context.get(),
                )
        else:
            pending_execution = pending.get("_execution")
            pending_step_id = pending_execution.get("step_id") if isinstance(pending_execution, Mapping) else None
            pending_tool_call_id = pending_execution.get("tool_call_id") if isinstance(pending_execution, Mapping) else None
            self._append(
                turn_id, "approval.resolved", "running", str(payload["type"]),
                actor="human", capability_id=str(pending["capability_id"]),
                payload_ref=action_ref, evidence_refs=approval_evidence,
                step_id=pending_step_id if isinstance(pending_step_id, str) else None,
                tool_call_id=pending_tool_call_id if isinstance(pending_tool_call_id, str) else None,
            )
        if payload["type"] == "mcp_reject":
            intent_ref = pending.get("_mcp_intent_ref")
            if not isinstance(intent_ref, str):
                raise AIKernelRuntimeError("MCP continuation intent is unavailable")
            try:
                intent = intent_from_payload(self._payloads.get(intent_ref))
            except Exception as error:
                raise AIKernelRuntimeError("MCP continuation intent is unavailable") from error
            self._state.clear_pending(turn_id)
            if '_tool_batch_step_id' in pending:
                self._append(turn_id, 'turn.cancel.requested', 'running', str(payload['reason']),
                             actor='human', payload_ref=action_ref)
                token = self._batch_terminals.set([])
                try:
                    self._record_cancelled_tool_outcome(intent, error_code="mcp.continuation_rejected")
                finally:
                    self._batch_terminals.reset(token)
                self._resume_tool_batch(turn_id)
                return self._remember_action(payload, turn_id)
            self._record_cancelled_tool_outcome(intent, error_code="mcp.continuation_rejected")
            return self._remember_action(payload, turn_id)
        if payload["type"] == "reject":
            self._state.clear_pending(turn_id)
            self._append_turn_terminal(turn_id, "turn.cancelled", "cancelled", str(payload["reason"]), actor="human")
            return self._remember_action(payload, turn_id)
        if payload["type"] == "mcp_continue":
            if '_tool_batch_step_id' in pending:
                terminals = []
                token = self._batch_terminals.set(terminals)
                try:
                    try:
                        self._continue_mcp_request_state(turn_id, pending)
                    except _ToolTurnConverged:
                        pass
                    except Exception as error:
                        self._fail(turn_id, error)
                finally:
                    self._batch_terminals.reset(token)
                self._state.clear_pending(turn_id)
                if terminals:
                    self._append(turn_id, 'turn.cancel.requested', 'running', 'MCP batch continuation stopped')
                paused = self._resume_tool_batch(turn_id)
                if terminals and self._receipt(turn_id).status == 'running':
                    args, kwargs = terminals[0]
                    self._append_turn_terminal(*args, **kwargs)
                    paused = True
                receipt = self._receipt(turn_id) if paused else self._run(turn_id)
                self._state.save_action(payload, receipt)
                return receipt
            try:
                self._continue_mcp_request_state(turn_id, pending)
            except _ToolTurnConverged:
                receipt = self._receipt(turn_id)
                self._state.save_action(payload, receipt)
                return receipt
            except Exception as error:
                self._state.clear_pending(turn_id)
                receipt = self._fail(turn_id, error)
                self._state.save_action(payload, receipt)
                return receipt
            self._state.clear_pending(turn_id)
            receipt = self._complete_exact_capability_turn(turn_id, pending)
            self._state.save_action(payload, receipt)
            return receipt
        try:
            if '_tool_batch_step_id' in approved_decision:
                self._save_batch_approval(turn_id, approved_decision)
                self._state.clear_pending(turn_id)
                paused = self._resume_tool_batch(turn_id)
                receipt = self._receipt(turn_id) if paused else self._run(turn_id)
                self._state.save_action(payload, receipt)
                return receipt
            self._invoke_tool(turn_id, approved_decision)
        except _ToolTurnConverged:
            receipt = self._receipt(turn_id)
            self._state.save_action(payload, receipt)
            return receipt
        except Exception as error:  # planner/capability boundaries must converge durably
            self._state.clear_pending(turn_id)
            receipt = self._fail(turn_id, error)
            self._state.save_action(payload, receipt)
            return receipt
        self._state.clear_pending(turn_id)
        if _exact_capability_request(self._require_request(turn_id)) is not None:
            receipt = self._complete_exact_capability_turn(turn_id, approved_decision)
        else:
            receipt = self._run(turn_id)
        self._state.save_action(payload, receipt)
        return receipt

    def _run_exact_capability_request(
        self,
        turn_id: str,
        request: Mapping[str, object],
        manifest: CapabilityManifest,
        exact: Mapping[str, object],
    ) -> TurnReceipt:
        """Execute one caller-selected capability without creating model events.

        This remains a normal governed Tool invocation: the exact selection is
        narrowed by the immutable manifest before Hook, Boundary, approval,
        intent, dispatcher, provider and Receipt handling run.
        """

        capability_id = str(exact["capability_id"])
        completed = next(
            (
                event for event in reversed(tuple(self._events.events_after(turn_id)))
                if event.get("type") == "tool.completed"
                and isinstance(event.get("data"), Mapping)
                and event["data"].get("capability_id") == capability_id
            ),
            None,
        )
        if completed is not None:
            return self._complete_exact_capability_turn(
                turn_id,
                {
                    "_execution": {
                        "step_id": _correlation_text(completed, "step_id"),
                        "tool_call_id": _correlation_text(completed, "tool_call_id"),
                    }
                },
            )
        step_id = f"step-{uuid4().hex}"
        tool_call_id = f"tool-call-{uuid4().hex}"
        decision: dict[str, object] = {
            "type": "tool",
            "capability_id": capability_id,
            "arguments": dict(_mapping(exact.get("arguments"), "exact capability arguments")),
        }
        try:
            execution_decision, waiting = self._execute_tool_decision(
                turn_id,
                request=request,
                manifest=manifest,
                decision=decision,
                step_id=step_id,
                tool_call_id=tool_call_id,
                requested_summary="exact capability requested",
            )
            if waiting:
                return self._receipt(turn_id)
            return self._complete_exact_capability_turn(turn_id, execution_decision)
        except _ToolTurnConverged:
            return self._receipt(turn_id)
        except Exception as error:
            return self._fail(turn_id, error)

    def _execute_tool_decision(
        self,
        turn_id: str,
        *,
        request: Mapping[str, object],
        manifest: CapabilityManifest,
        decision: Mapping[str, object],
        step_id: str,
        tool_call_id: str,
        requested_summary: str,
        planner_control: _PlannerExecutionContext | None = None,
    ) -> tuple[dict[str, object], bool]:
        execution_decision = self._preflight_tool_decision(
            turn_id, request=request, manifest=manifest, decision=decision,
            step_id=step_id, tool_call_id=tool_call_id,
            requested_summary=requested_summary, planner_control=planner_control,
        )
        if execution_decision['_requires_approval']:
            return self._wait_for_tool_approval(
                turn_id, execution_decision, planner_control=planner_control,
            )
        with self._planner_controls_lock:
            if planner_control is not None:
                planner_control.checkpoint()
            intent, intent_ref, provider = self._record_tool_intent(turn_id, execution_decision)
            self._dispatcher.prepare(intent.invocation_id)
            if planner_control is not None:
                self._planner_controls.pop(turn_id, None)
        self._execute_tool_intent(intent, intent_ref, provider, attempt=1)
        return execution_decision, False

    def _preflight_tool_decision(
        self, turn_id, *, request, manifest, decision, step_id, tool_call_id,
        requested_summary, planner_control=None, persist_kind=None,
    ):
        with self._event_commit(turn_id):
            self._check_tool_call_budget(turn_id, (tool_call_id,))
        capability_id = str(decision.get("capability_id", ""))
        policy = request.get("capability_policy")
        if not isinstance(policy, Mapping):
            raise AIKernelRuntimeError("turn capability policy is unavailable")
        if capability_id not in policy["allowed"] or capability_id in policy["denied"]:
            raise AIKernelRuntimeError("planner selected capability outside turn policy")
        if capability_id not in manifest.capability_ids:
            raise AIKernelRuntimeError("planner selected capability outside Turn manifest")
        resolved = self._registry.resolve(capability_id)
        if resolved is None:
            raise AIKernelRuntimeError("planner selected unavailable capability")
        definition, _ = resolved
        decision_with_contract = dict(decision)
        tool = tool_from_capability(definition)
        decision_with_contract["_tool_contract"] = tool_contract_identity(tool)
        decision_with_contract["_capability_requires_approval"] = definition.requires_approval
        decision_with_contract = self._apply_pre_tool_hook(
            turn_id,
            decision_with_contract,
            step_id=step_id,
            tool_call_id=tool_call_id,
        )
        if self._hook_host is not None:
            if self._frozen_authorization is not None:
                handle = self._frozen_authorization.current_handle(turn_id=turn_id)
                candidate = self._frozen_authorization.authorize_candidate(
                    turn_id=turn_id,
                    facts_ref=getattr(handle, "facts_ref", ""),
                    facts_revision=getattr(handle, "facts_revision", ""),
                    capability=definition,
                    arguments=decision_with_contract.get("arguments", {}),
                    tool_call_id=tool_call_id,
                    allow_pending_approval=True,
                )
                if candidate is None:
                    raise AIKernelRuntimeError("frozen authorization facts denied tool execution")
                execution_decision = dict(decision_with_contract)
                execution_decision["arguments"] = dict(getattr(candidate, "sanitized_arguments", {}))
                execution_decision["_frozen_authorization_ref"] = getattr(candidate, "facts_ref", "")
                execution_decision["_frozen_authorization_revision"] = getattr(candidate, "facts_revision", "")
                execution_decision["_frozen_requires_approval"] = bool(
                    getattr(candidate, "requires_approval", False)
                )
            else:
                checker = self._frozen_hook_authorization_check
                if checker is None or not checker(request, definition, decision_with_contract):
                    raise AIKernelRuntimeError("frozen authorization facts denied tool execution")
                execution_decision = dict(decision_with_contract)
            boundary = None
        else:
            execution_decision, boundary = self._evaluate_execution_boundary(
                request, definition, decision_with_contract
            )
        execution_decision["_execution"] = {
            "step_id": step_id,
            "tool_call_id": tool_call_id,
        }
        boundary_ref = None
        if boundary is not None:
            boundary_payload = _boundary_payload(boundary)
            validate_governed_payload(boundary_payload)
            boundary_ref = self._payloads.put(turn_id, "boundary-decision", boundary_payload)
        needs_approval = (
            definition.requires_approval
            or capability_id in policy["require_approval"]
            or (boundary is not None and boundary.outcome == "ask")
            or execution_decision.get("_frozen_requires_approval") is True
        )
        execution_decision['_requires_approval'] = needs_approval
        execution_decision['_boundary_ref'] = boundary_ref
        if persist_kind is not None:
            self._payloads.get_or_create_immutable_payload(turn_id, persist_kind, execution_decision)
        with self._planner_controls_lock:
            if planner_control is not None:
                planner_control.checkpoint()
            self._append(
                turn_id,
                "tool.requested",
                "running",
                requested_summary,
                capability_id=capability_id,
                payload_ref=boundary_ref,
                step_id=step_id,
                tool_call_id=tool_call_id,
            )
        if boundary is not None and boundary.outcome == "deny":
            raise AIKernelRuntimeError("Boundary denied tool execution")
        return execution_decision

    def _wait_for_tool_approval(
        self, turn_id, execution_decision, *, planner_control=None,
    ):
        capability_id = str(execution_decision['capability_id'])
        step_id = execution_decision['_execution']['step_id']
        tool_call_id = execution_decision['_execution']['tool_call_id']
        if self._hook_event_enabled(turn_id, HookEvent.PERMISSION_REQUEST):
            permission = self.invoke_lifecycle_hook(
                turn_id,
                HookEvent.PERMISSION_REQUEST,
                {
                    "turn_id": turn_id,
                    "step_id": step_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": capability_id,
                },
                step_id=step_id,
                tool_call_id=tool_call_id,
            )
            if permission.outcome.permission_denied:
                raise AIKernelRuntimeError(
                    permission.outcome.stop_reason
                    or "PermissionRequest hook denied the request"
                )
        decision_ref = self._payloads.put(turn_id, "approval-decision", execution_decision)
        with self._planner_controls_lock:
            if planner_control is not None:
                planner_control.checkpoint()
            self._append(
                turn_id,
                "approval.required",
                "waiting_approval",
                "user approval required",
                capability_id=capability_id,
                payload_ref=decision_ref,
                step_id=step_id,
                tool_call_id=tool_call_id,
            )
            self._state.put_pending(turn_id, execution_decision)
            if planner_control is not None:
                self._planner_controls.pop(turn_id, None)
        return execution_decision, True
    def _complete_exact_capability_turn(
        self,
        turn_id: str,
        decision: Mapping[str, object],
    ) -> TurnReceipt:
        events = tuple(self._events.events_after(turn_id))
        if events and events[-1].get("type") in {
            "turn.completed", "turn.failed", "turn.cancelled",
        }:
            return self._receipt(turn_id)
        execution = decision.get("_execution")
        execution_map = execution if isinstance(execution, Mapping) else {}
        self._append_turn_terminal(
            turn_id,
            "turn.completed",
            "completed",
            "exact capability completed",
            step_id=(
                str(execution_map["step_id"])
                if isinstance(execution_map.get("step_id"), str) else None
            ),
            tool_call_id=(
                str(execution_map["tool_call_id"])
                if isinstance(execution_map.get("tool_call_id"), str) else None
            ),
        )
        return self._receipt(turn_id)

    def _run(self, turn_id: str) -> TurnReceipt:
        request = self._require_request(turn_id)
        if self._pending_tool_batch(turn_id) is not None:
            if self._resume_tool_batch(turn_id):
                return self._receipt(turn_id)
        if self._expert_job_wait_is_active(turn_id):
            return self._receipt(turn_id)
        manifest = self._manifest_for(turn_id, request)
        recoverable_organize = (
            request.get("desired_outcome") == "memory.organize"
            and self._hook_host is None
            and self._expert_binding is None
            and not manifest.capability_ids
        )
        if recoverable_organize and self._resume_organize_completion(turn_id):
            return self._receipt(turn_id)
        exact = _exact_capability_request(request)
        if exact is not None:
            expert_capability_ids = self._expert_capability_ids(turn_id)
            if expert_capability_ids is not None:
                return self._fail(
                    turn_id,
                    AIKernelRuntimeError(
                        "exact capability cannot bypass the frozen expert recipe"
                    ),
                )
            return self._run_exact_capability_request(turn_id, request, manifest, exact)
        remaining_steps, planner_timeout = planner_limits(
            request, self._events.events_after(turn_id),
            max_steps=self._max_steps, timeout_ms=self._planner_timeout_ms,
        )
        purpose = turn_purpose(request)
        for _ in range(remaining_steps):
            planner_control: _PlannerExecutionContext | None = None
            try:
                if self._turn_cancel_requested(turn_id):
                    self._append_turn_terminal(turn_id, "turn.cancelled", "cancelled", "tool cancellation converged")
                    return self._receipt(turn_id)
                step_id = f"step-{uuid4().hex}"
                model_request_id = f"model-request-{uuid4().hex}"
                capabilities = self._manifest_capabilities(manifest)
                expert_capability_ids = self._expert_capability_ids(turn_id)
                if expert_capability_ids is not None:
                    capabilities = tuple(
                        item for item in capabilities
                        if item.capability_id in expert_capability_ids
                    )
                    if {item.capability_id for item in capabilities} != set(expert_capability_ids):
                        raise AIKernelRuntimeError(
                            "expert binding capability set drifted from Turn manifest"
                        )
                self._append(
                    turn_id,
                    "model.requested",
                    "running",
                    "planner step requested",
                    step_id=step_id,
                    model_request_id=model_request_id,
                    model_call_purpose=purpose,
                )
                planner_events = tuple(self._events.events_after(turn_id))
                planner_payloads = ScopedTurnPayloadView(
                    self._payloads,
                    turn_id=turn_id,
                    allowed_refs=planner_context_payload_refs(planner_events, self._payloads, turn_id=turn_id),
                )
                planner_control = self._begin_planner_control(
                    turn_id,
                    step_id=step_id,
                    model_request_id=model_request_id,
                    purpose=purpose,
                    timeout_ms=planner_timeout,
                )
                planner_completed = False
                try:
                    planner_control.checkpoint()
                    decision = dict(self._planner.plan(
                        request,
                        planner_events,
                        capabilities,
                        planner_payloads,
                        planner_control,
                    ))
                    validate_governed_payload(decision)
                    with self._planner_controls_lock:
                        planner_control.checkpoint()
                        if decision.get('type') == 'tools':
                            self._freeze_tool_batch(turn_id, decision, step_id, model_request_id)
                        if recoverable_organize and decision.get("type") == "complete":
                            self._payloads.get_or_create_immutable_payload(
                                turn_id, "organize-completion-v1",
                                {"decision": decision, "step_id": step_id,
                                 "model_request_id": model_request_id},
                            )
                        receipt_failed = self._record_planner_model_terminal(
                            turn_id,
                            planner_control,
                            fallback_status="completed",
                            fallback_error_code=None,
                        )
                        if receipt_failed:
                            raise AIKernelRuntimeError("model call receipt persistence failed")
                        planner_completed = True
                finally:
                    if not planner_completed and planner_control.model_terminal:
                        self._finish_planner_control(turn_id, planner_control)
                if decision.get("type") == "complete":
                    stop = self.invoke_lifecycle_hook(
                        turn_id,
                        HookEvent.STOP,
                        {
                            "turn_id": turn_id,
                            "step_id": step_id,
                            "model_request_id": model_request_id,
                        },
                        step_id=step_id,
                        model_request_id=model_request_id,
                    ) if self._hook_event_enabled(turn_id, HookEvent.STOP) else None
                    if stop is not None and stop.outcome.allow_stop is False:
                        continuation_ref = self._payloads.put(
                            turn_id,
                            "codex-hook-continuation",
                            {
                                "schema_version": "1.0.0",
                                "event": HookEvent.STOP.value,
                                "prompt": stop.outcome.continuation_prompt,
                            },
                        )
                        self._append(
                            turn_id, "hook.continuation", "running",
                            "Stop hook requested continuation",
                            payload_ref=continuation_ref,
                            step_id=step_id,
                            model_request_id=model_request_id,
                        )
                        self._finish_planner_control(turn_id, planner_control)
                        continue
                    terminal_payload_ref = _optional_ref(decision.get("payload_ref"))
                    terminal_evidence_refs = _refs(decision.get("evidence_refs"))
                    if expert_capability_ids is not None:
                        terminal_payload_ref, expert_receipt_ref = (
                            self._ensure_expert_execution_receipt(
                                turn_id, decision, planner_events,
                            )
                        )
                        expert_proposal_ref = self._ensure_expert_memory_proposal(turn_id)
                        terminal_evidence_refs = tuple(dict.fromkeys((
                            expert_receipt_ref,
                            *((expert_proposal_ref,) if expert_proposal_ref is not None else ()),
                            *terminal_evidence_refs,
                        )))
                    with self._planner_controls_lock:
                        planner_control.checkpoint()
                        self._append_turn_terminal(turn_id, "turn.completed", "completed", str(decision.get("summary", "turn completed")), payload_ref=terminal_payload_ref, evidence_refs=terminal_evidence_refs, step_id=step_id, model_request_id=model_request_id)
                        self._planner_controls.pop(turn_id, None)
                    return self._receipt(turn_id)
                if decision.get("type") == "wait_job":
                    if expert_capability_ids is None:
                        raise AIKernelRuntimeError(
                            "only a frozen expert binding may wait for a Job"
                        )
                    self._record_expert_job_wait(
                        turn_id,
                        decision,
                        step_id=step_id,
                        model_request_id=model_request_id,
                    )
                    self._finish_planner_control(turn_id, planner_control)
                    return self._receipt(turn_id)
                if decision.get('type') == 'tools':
                    self._finish_planner_control(turn_id, planner_control)
                    if self._resume_tool_batch(turn_id):
                        return self._receipt(turn_id)
                    continue
                if decision.get("type") != "tool":
                    raise AIKernelRuntimeError("planner returned unsupported decision")
                if (
                    expert_capability_ids is not None
                    and decision.get("capability_id") not in expert_capability_ids
                ):
                    raise AIKernelRuntimeError(
                        "planner selected a capability outside the frozen expert binding"
                    )
                tool_call_id = f"tool-call-{uuid4().hex}"
                _, waiting = self._execute_tool_decision(
                    turn_id,
                    request=request,
                    manifest=manifest,
                    decision=decision,
                    step_id=step_id,
                    tool_call_id=tool_call_id,
                    requested_summary="tool requested",
                    planner_control=planner_control,
                )
                if waiting:
                    return self._receipt(turn_id)
            except _ToolTurnConverged:
                if planner_control is not None:
                    self._finish_planner_control(turn_id, planner_control)
                return self._receipt(turn_id)
            except _ExpertProposalCheckpointPending:
                if planner_control is not None:
                    self._finish_planner_control(turn_id, planner_control)
                return self._receipt(turn_id)
            except _PlannerCancelled:
                return self._converge_planner_terminal(
                    turn_id,
                    planner_control,
                    event_type="turn.cancelled",
                    status="cancelled",
                    summary="planner cancellation completed",
                    error_code="ai.planner_cancelled",
                )
            except _PlannerDeadlineExceeded:
                return self._converge_planner_terminal(
                    turn_id,
                    planner_control,
                    event_type="turn.failed",
                    status="failed",
                    summary="planner deadline exceeded",
                    error_code="ai.planner_timeout",
                )
            except Exception as error:  # planner/capability boundaries must converge durably
                converged = self._converge_active_planner_failure(
                    turn_id,
                    planner_control,
                    error,
                )
                if converged is not None:
                    return converged
                return self._fail(turn_id, error)
        self._append_turn_terminal(turn_id, "turn.failed", "failed", "agent step limit reached", error_code="ai.step_limit")
        return self._receipt(turn_id)

    @_serialize_event_commit
    def _freeze_tool_batch(self, turn_id, decision, step_id, model_request_id):
        calls = decision.get('calls')
        if not isinstance(calls, list) or not 1 <= len(calls) <= 16:
            raise AIKernelRuntimeError('tool batch must contain between one and sixteen calls')
        normalized = []
        for call in calls:
            if (not isinstance(call, Mapping)
                or set(call) - {'capability_id', 'arguments'}
                or not isinstance(call.get('capability_id'), str)
                or not isinstance(call.get('arguments', {}), Mapping)):
                raise AIKernelRuntimeError('tool batch call is invalid')
            normalized.append({
                'type': 'tool', 'capability_id': call['capability_id'],
                'arguments': dict(call.get('arguments', {})),
                '_execution': {'step_id': step_id, 'tool_call_id': f'tool-call-{uuid4().hex}'},
            })
        self._check_tool_call_budget(turn_id, (call['_execution']['tool_call_id'] for call in normalized))
        return self._payloads.get_or_create_immutable_payload(
            turn_id, f'tool-batch-v1-{step_id}',
            {'schema_version': '1.0.0', 'step_id': step_id,
             'model_request_id': model_request_id, 'calls': normalized},
        )

    def _save_batch_approval(self, turn_id, decision):
        call_id = decision['_execution']['tool_call_id']
        self._payloads.get_or_create_immutable_payload(
            turn_id, f'tool-batch-approved-v1-{call_id}', dict(decision),
        )

    def _pending_tool_batch(self, turn_id):
        events = tuple(self._events.events_after(turn_id))
        if events and events[-1].get('type') in {'turn.completed', 'turn.failed', 'turn.cancelled'}:
            return None
        terminal_calls = {
            _correlation_text(event, 'tool_call_id') for event in events
            if event.get('type') in {'tool.completed', 'tool.failed', 'tool.cancelled'}
        }
        for event in reversed(events):
            if (event.get('type') != 'model.completed'
                or _correlation_text(event, 'tool_call_id') is not None):
                continue
            step_id = _correlation_text(event, 'step_id')
            stored = self._payloads.get_immutable_payload(turn_id, f'tool-batch-v1-{step_id}')
            if stored is None:
                continue
            batch = stored[1]
            if batch['step_id'] != step_id or batch['model_request_id'] != _correlation_text(event, 'model_request_id'):
                raise AIKernelRuntimeError('tool batch model identity drifted')
            if any(call['_execution']['tool_call_id'] not in terminal_calls for call in batch['calls']):
                return batch
            failed_calls = {_correlation_text(item, 'tool_call_id') for item in events
                            if item.get('type') in {'tool.failed', 'tool.cancelled'}}
            if any(call['_execution']['tool_call_id'] in failed_calls for call in batch['calls']):
                return batch
        return None

    def _resume_tool_batch(self, turn_id):
        """Resume only durable members; never re-plan a partially executed batch."""
        batch = self._pending_tool_batch(turn_id)
        if batch is None:
            return False
        member_ids = {call['_execution']['tool_call_id'] for call in batch['calls']}
        with self._event_commit(turn_id):
            self._check_tool_call_budget(turn_id, member_ids)
        if self._batch_cancel_requested(turn_id) and not any(
            event.get('type') == 'tool.intent.recorded'
            and _correlation_text(event, 'tool_call_id') in member_ids
            for event in self._events.events_after(turn_id)
        ):
            self._state.clear_pending(turn_id)
            self._append_turn_terminal(turn_id, 'turn.cancelled', 'cancelled', 'batch cancelled before execution')
            return True
        request = self._require_request(turn_id)
        manifest = self._manifest_for(turn_id, request)
        expert_ids = self._expert_capability_ids(turn_id)
        executions = []
        for call in batch['calls']:
            call_id = call['_execution']['tool_call_id']
            if expert_ids is not None and call['capability_id'] not in expert_ids:
                raise AIKernelRuntimeError('planner selected a capability outside the frozen expert binding')
            saved = self._payloads.get_immutable_payload(turn_id, f'tool-batch-preflight-v1-{call_id}')
            if saved is None:
                execution = self._preflight_tool_decision(
                    turn_id, request=request, manifest=manifest, decision=call,
                    step_id=batch['step_id'], tool_call_id=call_id,
                    requested_summary='batch tool requested',
                    persist_kind=f'tool-batch-preflight-v1-{call_id}',
                )
            else:
                execution = dict(saved[1])
                if not any(event.get('type') == 'tool.requested' and _correlation_text(event, 'tool_call_id') == call_id
                           for event in self._events.events_after(turn_id)):
                    self._append(turn_id, 'tool.requested', 'running', 'batch tool requested',
                                 capability_id=execution['capability_id'], payload_ref=execution['_boundary_ref'],
                                 step_id=batch['step_id'], tool_call_id=call_id)
            frozen_boundary = execution.get('boundary')
            if frozen_boundary is not None:
                boundary_ref = execution.get('_boundary_ref')
                if (not isinstance(frozen_boundary, Mapping) or not isinstance(boundary_ref, str)
                    or dict(self._payloads.get(boundary_ref)) != dict(frozen_boundary)
                    or frozen_boundary.get('outcome') not in {'allow', 'allow_redacted', 'ask', 'deny'}):
                    raise AIKernelRuntimeError('frozen batch Boundary decision drifted')
                if frozen_boundary['outcome'] == 'deny':
                    self._fail(turn_id, AIKernelRuntimeError('Boundary denied tool execution'))
                    return True
            execution['_tool_batch_step_id'] = batch['step_id']
            approved = self._payloads.get_immutable_payload(turn_id, f'tool-batch-approved-v1-{call_id}')
            if approved is not None:
                execution = dict(approved[1])
            executions.append(execution)
        # Pause before every effect; a pending approval remains the final event.
        for execution in executions:
            call_id = execution['_execution']['tool_call_id']
            if execution['_requires_approval'] and self._payloads.get_immutable_payload(
                turn_id, f'tool-batch-approved-v1-{call_id}',
            ) is None:
                self._wait_for_tool_approval(turn_id, execution)
                return True
        events = tuple(self._events.events_after(turn_id))
        rejection = self._recorded_batch_mcp_rejection(turn_id, member_ids, events)
        if rejection is not None:
            rejected_intent, action_ref, action = rejection
            self._state.clear_pending(turn_id)
            if not any(event.get('type') == 'turn.cancel.requested'
                       and event['data'].get('payload_ref') == action_ref for event in events):
                self._append(turn_id, 'turn.cancel.requested', 'running', str(action['reason']),
                             actor='human', payload_ref=action_ref)
            events = tuple(self._events.events_after(turn_id))
        settled_ids = {_correlation_text(event, 'tool_call_id') for event in events
                       if event.get('type') == 'tool.outcome.recorded'}
        for call in batch['calls']:
            call_id = call['_execution']['tool_call_id']
            queued = self._payloads.get_immutable_payload(turn_id, f'tool-batch-mcp-wait-v1-{call_id}')
            if queued is None or call_id in settled_ids or self._tool_attempt_count(turn_id, call_id) > 1:
                continue
            if self._batch_cancel_requested(turn_id):
                continue
            if (events[-1].get('type') != 'mcp.continuation.required'
                or _correlation_text(events[-1], 'tool_call_id') != call_id):
                frozen = queued[1]
                intent = intent_from_payload(self._payloads.get(frozen['waiting']['_mcp_intent_ref']))
                self._persist_mcp_waiting(intent, frozen['waiting']['_mcp_round'], frozen['payload'], dict(frozen['waiting']))
            return True
        intents = []
        events = tuple(self._events.events_after(turn_id))
        for execution in executions:
            call_id = execution['_execution']['tool_call_id']
            recorded = next((event for event in events if event.get('type') == 'tool.intent.recorded'
                             and _correlation_text(event, 'tool_call_id') == call_id), None)
            if recorded is None:
                intent, intent_ref, provider = self._record_tool_intent(turn_id, execution)
            else:
                intent_ref = str(recorded['data']['payload_ref'])
                intent = intent_from_payload(self._payloads.get(intent_ref))
                resolved = self._registry.resolve(intent.capability_id)
                provider = resolved[1] if resolved is not None else None
            intents.append((intent, intent_ref, provider))
        terminals = []
        waitings = []
        token = self._batch_terminals.set(terminals)
        waiting_token = self._batch_waitings.set(waitings)
        paused = False
        try:
            cursor = 0
            while cursor < len(intents):
                end = cursor + 1
                if intents[cursor][0].execution_mode == 'parallel':
                    while end < len(intents) and intents[end][0].execution_mode == 'parallel':
                        end += 1
                segment = []
                for intent, intent_ref, provider in intents[cursor:end]:
                    events = tuple(self._events.events_after(turn_id))
                    if any(event.get('type') == 'tool.outcome.recorded'
                           and _correlation_text(event, 'tool_call_id') == intent.invocation_id for event in events):
                        while self._converge_recorded_tool_outcome(turn_id) is not None:
                            pass
                        continue
                    buffered = self._payloads.get_immutable_payload(turn_id, f'tool-batch-buffer-v1-{intent.invocation_id}')
                    if (buffered is not None
                        and self._tool_attempt_count(turn_id, intent.invocation_id) == 1
                        and self._latest_retryable_attempt_failure(intent) is None):
                        prepared, dispatched = self._restore_buffered_dispatch(intent, intent_ref, buffered)
                        try:
                            self._settle_tool_invocation(intent, intent_ref, prepared, dispatched,
                                                         buffer_ref=buffered[0])
                        except _ToolTurnConverged:
                            paused = True
                        continue
                    if terminals or self._batch_cancel_requested(turn_id):
                        self._record_cancelled_tool_outcome(intent, error_code=(
                            'mcp.continuation_rejected' if rejection is not None
                            and rejected_intent.invocation_id == intent.invocation_id else 'ai.tool_cancelled'))
                        continue
                    if self._tool_attempt_count(turn_id, intent.invocation_id):
                        try:
                            self._resume_recorded_tool_intent(intent, intent_ref)
                        except _ToolTurnConverged:
                            pass
                        if not any(
                            event.get('type') == 'tool.outcome.recorded'
                            and _correlation_text(event, 'tool_call_id') == intent.invocation_id
                            for event in self._events.events_after(turn_id)
                        ):
                            paused = True
                            break
                        continue
                    self._dispatcher.prepare(intent.invocation_id)
                    try:
                        prepared = self._prepare_tool_invocation(intent, intent_ref, provider, attempt=1)
                    except _ToolTurnConverged:
                        paused = True
                        break
                    except RunLeaseRevoked:
                        for pending_intent, _, _ in segment:
                            self._dispatcher.abandon_prepared(pending_intent.invocation_id)
                        raise
                    except Exception:
                        for pending_intent, _, _ in segment:
                            self._dispatcher.abandon_prepared(pending_intent.invocation_id)
                        if not self._fail_unstarted_tool_batch(turn_id, intents):
                            raise
                        paused = True
                        break
                    segment.append((intent, intent_ref, prepared))
                if paused:
                    for intent, _, _ in segment:
                        self._dispatcher.abandon_prepared(intent.invocation_id)
                    break
                if segment:
                    with ThreadPoolExecutor(max_workers=len(segment), thread_name_prefix='ai-tool') as pool:
                        futures = [pool.submit(copy_context().run, self._dispatch_prepared_tool, prepared)
                                   for _, _, prepared in segment]
                        for (intent, intent_ref, prepared), future in zip(segment, futures):
                            dispatched = future.result()
                            if waitings:
                                self._buffer_tool_dispatch(intent, intent_ref, prepared, dispatched)
                                continue
                            try:
                                self._settle_tool_invocation(intent, intent_ref, prepared, dispatched)
                            except _ToolTurnConverged:
                                paused = True
                if waitings:
                    break
                cursor = end
        finally:
            self._batch_waitings.reset(waiting_token)
            self._batch_terminals.reset(token)
        if terminals:
            args, kwargs = terminals[0]
            self._append_turn_terminal(*args, **kwargs)
            return True
        member_ids = {call['_execution']['tool_call_id'] for call in batch['calls']}
        for event in self._events.events_after(turn_id):
            if event.get('type') != 'tool.outcome.recorded' or _correlation_text(event, 'tool_call_id') not in member_ids:
                continue
            outcome = outcome_from_payload(self._payloads.get(event['data']['payload_ref']))
            if outcome.status != 'completed':
                event_type, status, summary = _tool_outcome_turn_terminal(outcome.status)
                self._append_turn_terminal(turn_id, event_type, status, summary, error_code=outcome.error_code)
                return True
        if waitings:
            self._persist_mcp_waiting(*waitings[0])
            return True
        return paused

    def _recorded_batch_mcp_rejection(self, turn_id, member_ids, events):
        """Bind a durable rejection to its original waiting RPC, never re-open it."""
        for resolved in reversed(events):
            data = resolved.get('data')
            if (resolved.get('type') != 'approval.resolved' or not isinstance(data, Mapping)
                or data.get('summary') != 'mcp_reject'):
                continue
            action_ref = data.get('payload_ref')
            if not isinstance(action_ref, str):
                raise AIKernelRuntimeError('MCP rejection action reference is unavailable')
            action = validate_turn_action(self._payloads.get(action_ref))
            waiting = next((event for event in events
                            if event.get('event_id') == action.get('target_event_id')), None)
            if (action.get('type') != 'mcp_reject' or action.get('turn_id') != turn_id
                or waiting is None or waiting.get('type') != 'mcp.continuation.required'
                or waiting.get('sequence') != action.get('expected_sequence')
                or int(waiting['sequence']) >= int(resolved['sequence'])
                or _correlation_text(waiting, 'tool_call_id') not in member_ids
                or _correlation_text(waiting, 'tool_call_id') != _correlation_text(resolved, 'tool_call_id')
                or waiting['data'].get('capability_id') != data.get('capability_id')):
                raise AIKernelRuntimeError('MCP rejection waiting binding drifted')
            state_ref = waiting['data'].get('payload_ref')
            if not isinstance(state_ref, str):
                raise AIKernelRuntimeError('MCP rejection waiting receipt is unavailable')
            frozen = self._payloads.get(state_ref)
            intent = intent_from_payload(self._payloads.get(frozen['intent_ref']))
            if (intent.turn_id != turn_id
                or intent.invocation_id != _correlation_text(waiting, 'tool_call_id')
                or intent.step_id != _correlation_text(waiting, 'step_id')
                or intent.step_id != _correlation_text(resolved, 'step_id')
                or intent.capability_id != data.get('capability_id')
                or (intent.tool_contract or {}).get('source') != 'mcp'):
                raise AIKernelRuntimeError('MCP rejection frozen intent drifted')
            self._settle_mcp_initial_effect(intent, state_ref, frozen)
            return intent, action_ref, action
        return None

    def _fail_unstarted_tool_batch(self, turn_id, intents):
        """Close failed preparation only when durable facts prove no dispatch."""
        events = tuple(self._events.events_after(turn_id))
        finished = {_correlation_text(event, 'tool_call_id') for event in events
                    if event.get('type') == 'tool.outcome.recorded'}
        remaining = [intent for intent, _, _ in intents if intent.invocation_id not in finished]
        if any(event.get('type') == 'tool.started'
               and _correlation_text(event, 'tool_call_id') in {intent.invocation_id for intent in remaining}
               for event in events):
            return False
        effects = []
        if self._effect_runner is not None:
            for intent in remaining:
                effect = self._effect_runner.log.get(intent.invocation_id)
                if effect is not None:
                    if (effect.state not in {EffectState.PLANNED, EffectState.INFLIGHT}
                        or (effect.state is EffectState.INFLIGHT
                            and effect.lease_owner != self._effect_runner.owner_id)):
                        return False
                    effects.append(effect)
            now = int(datetime.now(timezone.utc).timestamp())
            for effect in effects:
                if effect.state is EffectState.PLANNED:
                    effect, claimed = self._effect_runner.claim_planned(effect.operation_id, now=now)
                    if not claimed:
                        return False
                self._effect_runner.settle_error(effect, error_ref='ai.tool_prepare_failed', now=now)
        for intent in remaining:
            self._dispatcher.abandon_prepared(intent.invocation_id)
            self._record_failed_tool_outcome(intent, attempt=1, error_code='ai.tool_prepare_failed',
                                             effect_certainty='confirmed_none')
        return True

    def _buffer_tool_dispatch(self, intent, intent_ref, prepared, dispatched):
        """Keep a returned provider result while an earlier member awaits input."""
        succeeded, result = dispatched
        if succeeded:
            captured = {'kind': 'result', 'result': dict(result)}
        elif isinstance(result, (ToolDispatchCancelled, ToolDispatchDeadlineExceeded)):
            captured = {'kind': 'timeout' if isinstance(result, ToolDispatchDeadlineExceeded) else 'cancelled',
                        'provider_started': result.provider_started}
        else:
            request_state = None
            invalid_state = False
            if isinstance(result, ToolDispatchFailure) and result.continuation_state is not None:
                try:
                    request_state = result.continuation_state.decode('utf-8', errors='strict')
                except UnicodeDecodeError:
                    invalid_state = True
            certainty = (result.effect_certainty if isinstance(result, ToolDispatchFailure) else
                         'confirmed_none' if prepared[0].mode == 'read' else 'unknown')
            if invalid_state:
                certainty = 'confirmed_none' if prepared[0].mode == 'read' else 'unknown'
            captured = {'kind': 'failure',
                        'error_code': ('mcp.continuation_invalid' if invalid_state else
                                       result.error_code if isinstance(result, ToolDispatchFailure) else 'ai.tool_failed'),
                        'effect_certainty': certainty,
                        'provider_started': result.provider_started if isinstance(result, ToolDispatchFailure) else True,
                        'retry_after_ms': result.retry_after_ms if isinstance(result, ToolDispatchFailure) else None,
                        'request_state': request_state}
        receipt = {'schema_version': '1.0.0', 'turn_id': intent.turn_id,
                   'invocation_id': intent.invocation_id, 'intent_ref': intent_ref,
                   'step_id': intent.step_id, 'operation_id': intent.operation_id,
                   'attempt': prepared[2], 'dispatch': captured}
        stored = self._payloads.get_or_create_immutable_payload(
            intent.turn_id, f'tool-batch-buffer-v1-{intent.invocation_id}', receipt,
        )
        # A valid returned result closes the provider effect now. Publishing its
        # Tool outcome waits for earlier members; replay reads this same receipt.
        if succeeded and self._tool_result_has_receipt(prepared[0], result):
            self._settle_buffered_effect(intent, stored, succeeded=True)

    @staticmethod
    def _tool_result_has_receipt(definition, result):
        return _buffered_result_is_valid(result, definition.operation_semantics)

    def _settle_buffered_effect(self, intent, buffer_ref, *, succeeded):
        if self._effect_runner is None:
            return None
        effect = self._effect_runner.log.get(intent.invocation_id)
        if effect is None:
            raise AIKernelRuntimeError('buffered provider effect is unavailable')
        now = int(datetime.now(timezone.utc).timestamp())
        if effect.state is EffectState.PLANNED:
            effect, claimed = self._effect_runner.claim_planned(effect.operation_id, now=now)
            if not claimed:
                raise _ToolTurnConverged()
        if succeeded:
            if effect.state is EffectState.SETTLED_OK:
                if effect.result_ref != buffer_ref:
                    raise AIKernelRuntimeError('buffered provider receipt drifted')
            elif effect.state is EffectState.UNKNOWN:
                effect = self._effect_runner.settle_verified_ok(effect, receipt_ref=buffer_ref,
                    receipt_kind='buffered-tool-dispatch-receipt', now=now)
            elif effect.state is EffectState.INFLIGHT:
                effect = self._effect_runner.settle_ok(effect, receipt_ref=buffer_ref,
                    receipt_kind='buffered-tool-dispatch-receipt', now=now)
            else:
                raise AIKernelRuntimeError('buffered provider effect cannot settle')
        return effect

    def _restore_buffered_dispatch(self, intent, intent_ref, stored):
        buffer_ref, receipt = stored
        validate_governed_payload(receipt)
        if (not isinstance(receipt, Mapping)
            or set(receipt) != {'schema_version', 'turn_id', 'invocation_id', 'intent_ref',
                                'step_id', 'operation_id', 'attempt', 'dispatch'}
            or receipt.get('schema_version') != '1.0.0'
            or buffer_ref != f'crp://session/{intent.turn_id}/tool-batch-buffer-v1-{intent.invocation_id}'
            or type(receipt.get('attempt')) is not int or receipt['attempt'] != 1
            or receipt.get('turn_id') != intent.turn_id or receipt.get('invocation_id') != intent.invocation_id
            or receipt.get('intent_ref') != intent_ref or receipt.get('step_id') != intent.step_id
            or receipt.get('operation_id') != intent.operation_id
            or not any(event.get('type') == 'tool.started' and _correlation_text(event, 'tool_call_id') == intent.invocation_id
                       and event['data'].get('payload_ref') == intent_ref for event in self._events.events_after(intent.turn_id))):
            raise AIKernelRuntimeError('buffered provider receipt binding drifted')
        contract = intent.tool_contract or {}
        definition = _ToolSettlementDefinition(str(contract.get('effect', 'read')),
                                               str(contract.get('operation_semantics', 'none')))
        captured = receipt['dispatch']
        shapes = {'result': {'kind', 'result'}, 'timeout': {'kind', 'provider_started'},
                  'cancelled': {'kind', 'provider_started'},
                  'failure': {'kind', 'error_code', 'effect_certainty', 'provider_started',
                              'retry_after_ms', 'request_state'}}
        if not isinstance(captured, Mapping) or set(captured) != shapes.get(captured.get('kind')):
            raise AIKernelRuntimeError('buffered provider dispatch envelope is invalid')
        kind = captured['kind']
        if kind == 'result':
            result = dict(captured['result'])
            valid = self._tool_result_has_receipt(definition, result)
            effect = self._settle_buffered_effect(intent, buffer_ref, succeeded=True) if valid else None
            dispatched = (True, result)
        elif kind in {'timeout', 'cancelled'}:
            error_type = ToolDispatchDeadlineExceeded if kind == 'timeout' else ToolDispatchCancelled
            dispatched = (False, error_type(provider_started=captured['provider_started']))
            effect = self._settle_buffered_effect(intent, buffer_ref, succeeded=False)
        elif kind == 'failure':
            state_text = captured.get('request_state')
            dispatched = (False, ToolDispatchFailure(captured['error_code'],
                provider_started=captured['provider_started'], effect_certainty=captured['effect_certainty'],
                retry_after_ms=captured.get('retry_after_ms'),
                continuation_state=state_text.encode('utf-8') if state_text is not None else None))
            effect = self._settle_buffered_effect(intent, buffer_ref, succeeded=False)
        else:
            raise AIKernelRuntimeError('buffered provider receipt kind is invalid')
        return (definition, effect, receipt['attempt'], None, None, None), dispatched

    def _resume_organize_completion(self, turn_id: str) -> bool:
        """Finish a receipted, tool-free organization without another model step."""
        stored = self._payloads.get_immutable_payload(turn_id, "organize-completion-v1")
        if stored is None:
            return False
        _, snapshot = stored
        events = tuple(self._events.events_after(turn_id))
        latest = events[-1]
        if (
            latest.get("type") != "model.completed"
            or latest.get("correlation", {}).get("model_request_id") != snapshot["model_request_id"]
            or latest.get("correlation", {}).get("step_id") != snapshot["step_id"]
        ):
            return False
        if self._turn_cancel_requested(turn_id):
            self._append_turn_terminal(turn_id, "turn.cancelled", "cancelled", "organize recovery cancelled")
            return True
        decision = snapshot["decision"]
        validate_governed_payload(decision)
        self._append_turn_terminal(
            turn_id, "turn.completed", "completed", str(decision.get("summary", "turn completed")),
            payload_ref=_optional_ref(decision.get("payload_ref")),
            evidence_refs=_refs(decision.get("evidence_refs")),
            step_id=snapshot["step_id"], model_request_id=snapshot["model_request_id"],
        )
        return True

    def _begin_planner_control(
        self,
        turn_id: str,
        *,
        step_id: str,
        model_request_id: str,
        purpose: ModelCallPurpose = "primary",
        timeout_ms: int | None = None,
    ) -> _PlannerExecutionContext:
        control = _PlannerExecutionContext(
            turn_id=turn_id,
            step_id=step_id,
            model_request_id=model_request_id,
            timeout_ms=self._planner_timeout_ms if timeout_ms is None else timeout_ms,
            purpose=purpose,
            _route_recorder=lambda snapshot_ref: self._append(
                turn_id,
                "model.routed",
                "running",
                "frozen model route verified",
                model_call_purpose=purpose,
                payload_ref=snapshot_ref,
                step_id=step_id,
                model_request_id=model_request_id,
            ),
            _attempt_dispatch_recorder=lambda payload: self._store_model_attempt_dispatch(
                turn_id,
                payload,
                step_id=step_id,
                tool_call_id=None,
                capability_id=None,
            ),
            _attempt_terminal_recorder=lambda payload, dispatch_ref: self._store_model_attempt_terminal(
                turn_id,
                payload,
                dispatch_ref=dispatch_ref,
                step_id=step_id,
                tool_call_id=None,
                capability_id=None,
            ),
        )
        with self._planner_controls_lock:
            if turn_id in self._planner_controls:
                raise AIKernelRuntimeError("planner execution is already active")
            self._planner_controls[turn_id] = control
        return control

    def _finish_planner_control(
        self,
        turn_id: str,
        control: _PlannerExecutionContext,
    ) -> bool:
        with self._planner_controls_lock:
            if self._planner_controls.get(turn_id) is control:
                self._planner_controls.pop(turn_id, None)

    def _request_planner_cancel(
        self,
        turn_id: str,
        *,
        reason: str,
        payload_ref: str | None,
    ) -> _PlannerExecutionContext | None:
        with self._planner_controls_lock:
            control = self._planner_controls.get(turn_id)
            if control is None:
                return None
            control.request_cancel()
            self._append(
                turn_id,
                "turn.cancel.requested",
                "running",
                reason,
                actor="user",
                payload_ref=payload_ref,
                step_id=control.step_id,
                model_request_id=control.model_request_id,
            )
            return control

    def _converge_planner_terminal(
        self,
        turn_id: str,
        control: _PlannerExecutionContext | None,
        *,
        event_type: str,
        status: str,
        summary: str,
        error_code: str,
    ) -> TurnReceipt:
        with self._planner_controls_lock:
            if control is not None and not control.model_terminal:
                fallback_status, _ = _model_terminal_for(error_code)
                self._record_planner_model_terminal(
                    turn_id,
                    control,
                    fallback_status=fallback_status,
                    fallback_error_code=error_code,
                )
            self._append_turn_terminal(
                turn_id,
                event_type,
                status,
                summary,
                error_code=error_code,
            )
            if control is not None and self._planner_controls.get(turn_id) is control:
                self._planner_controls.pop(turn_id, None)
        return self._receipt(turn_id)

    def _converge_active_planner_failure(
        self,
        turn_id: str,
        control: _PlannerExecutionContext | None,
        error: Exception,
    ) -> TurnReceipt | None:
        if control is None:
            return None
        with self._planner_controls_lock:
            if self._planner_controls.get(turn_id) is not control:
                return None
            if control.model_terminal:
                self._planner_controls.pop(turn_id, None)
                return None
            if control.cancel_requested:
                event_type, status = "turn.cancelled", "cancelled"
                summary, error_code = "planner cancellation completed", "ai.planner_cancelled"
            elif control.remaining_timeout_ms <= 0:
                event_type, status = "turn.failed", "failed"
                summary, error_code = "planner deadline exceeded", "ai.planner_timeout"
            else:
                message = str(error).casefold()
                if "needs verified project evidence" in message:
                    error_code, summary = "ai.insufficient_evidence", "verified project evidence is required"
                elif "baseline is stale" in message:
                    error_code, summary = "ai.stale_baseline", "generation baseline is stale"
                else:
                    error_code, summary = "ai.execution_failed", "AI execution failed"
                event_type, status = "turn.failed", "failed"
            fallback_status, _ = _model_terminal_for(error_code)
            self._record_planner_model_terminal(
                turn_id,
                control,
                fallback_status=fallback_status,
                fallback_error_code=error_code,
            )
            self._append_turn_terminal(
                turn_id,
                event_type,
                status,
                summary,
                error_code=error_code,
            )
            self._planner_controls.pop(turn_id, None)
        return self._receipt(turn_id)

    def _record_planner_model_terminal(
        self,
        turn_id: str,
        control: _PlannerExecutionContext,
        *,
        fallback_status: str,
        fallback_error_code: str | None,
    ) -> bool:
        receipt_failed, _ = self._record_model_terminal(
            turn_id,
            control,
            fallback_status=fallback_status,
            fallback_error_code=fallback_error_code,
            tool_call_id=None,
            completed_summary="planner step completed",
        )
        return receipt_failed

    def _record_nested_model_terminal(
        self,
        turn_id: str,
        control: _PlannerExecutionContext,
        *,
        tool_call_id: str,
        fallback_status: str,
        fallback_error_code: str | None,
    ) -> tuple[str, ...]:
        receipt_failed, evidence_refs = self._record_model_terminal(
            turn_id,
            control,
            fallback_status=fallback_status,
            fallback_error_code=fallback_error_code,
            tool_call_id=tool_call_id,
            completed_summary="nested model call completed",
        )
        if receipt_failed:
            raise _NestedModelReceiptPersistenceError(
                "nested model receipt persistence failed"
            )
        return evidence_refs

    @_serialize_event_commit
    def _record_model_terminal(
        self,
        turn_id: str,
        control: _PlannerExecutionContext,
        *,
        fallback_status: str,
        fallback_error_code: str | None,
        tool_call_id: str | None,
        completed_summary: str,
    ) -> tuple[bool, tuple[str, ...]]:
        receipt_terminal = control.receipt_terminal(
            fallback_status=fallback_status,
            fallback_error_code=fallback_error_code,
        )
        if self._atomic_store is not None:
            model_status, error_code = (
                receipt_terminal if receipt_terminal is not None
                else (fallback_status, fallback_error_code)
            )
            evidence_refs: list[str] = []
            if control._routing_snapshot_ref is not None:
                evidence_refs.append(control._routing_snapshot_ref)
            evidence_refs.extend(control.wire_attempt_receipt_refs)
            try:
                model_payload = (
                    control.terminal_receipt_payload(
                        turn_id=turn_id, status=model_status, error_code=error_code,
                    )
                    if receipt_terminal is not None else None
                )
                dispatch_payload = control.dispatch_authority_receipt_payload(turn_id=turn_id)
                cache_payload = (
                    control.prompt_cache_receipt_payload(turn_id=turn_id)
                    if model_payload is not None else None
                )
                event = self._new_event(
                    turn_id,
                    f"model.{model_status}",
                    "running" if model_status == "completed" else (
                        "cancelled" if model_status == "cancelled" else "failed"
                    ),
                    completed_summary if model_status == "completed" else _model_terminal_summary(model_status),
                    evidence_refs=tuple(evidence_refs), error_code=error_code,
                    step_id=control.step_id, tool_call_id=tool_call_id,
                    model_request_id=control.model_request_id,
                    model_call_purpose=control.purpose,
                )
                committed = self._atomic_store.append_model_terminal_bundle(
                    event, expected_sequence=int(event["sequence"]) - 1,
                    model_receipt_payload=model_payload,
                    dispatch_authority_receipt_payload=dispatch_payload,
                    prompt_cache_receipt_payload=cache_payload,
                    run_lease=self._run_lease_context.get(),
                )
                for reference in (
                    committed.dispatch_authority_receipt_ref,
                    committed.prompt_cache_receipt_ref,
                ):
                    if reference is not None:
                        evidence_refs.append(reference)
                control.mark_model_terminal()
                return False, tuple(evidence_refs)
            except Exception:
                # SQLite rolls the complete bundle back. Append a receipt-free
                # terminal marker only after the failed transaction is gone.
                self._append(
                    turn_id, "model.failed", "failed", _model_terminal_summary("failed"),
                    evidence_refs=tuple(evidence_refs), error_code="ai.model_receipt_failed",
                    step_id=control.step_id, tool_call_id=tool_call_id,
                    model_request_id=control.model_request_id,
                    model_call_purpose=control.purpose,
                )
                control.mark_model_terminal()
                return True, tuple(evidence_refs)
        if receipt_terminal is None:
            model_status = fallback_status
            error_code = fallback_error_code
            receipt_ref = None
            receipt_failed = False
        else:
            model_status, error_code = receipt_terminal
            try:
                receipt_ref = self._store_model_call_receipt(
                    turn_id,
                    control,
                    status=model_status,
                    error_code=error_code,
                )
                receipt_failed = False
            except Exception:
                model_status = "failed"
                error_code = "ai.model_receipt_failed"
                receipt_ref = None
                receipt_failed = True
        evidence_refs: list[str] = []
        if control._routing_snapshot_ref is not None:
            evidence_refs.append(control._routing_snapshot_ref)
        evidence_refs.extend(control.wire_attempt_receipt_refs)
        try:
            dispatch_authority_ref = self._store_model_dispatch_authority_receipt(turn_id, control)
        except Exception:
            # A governed call cannot be reported as complete when its required
            # local authority observation was not made durable.
            dispatch_authority_ref = None
            model_status = "failed"
            error_code = "ai.model_receipt_failed"
            receipt_ref = None
            receipt_failed = True
        if dispatch_authority_ref is not None:
            evidence_refs.append(dispatch_authority_ref)
        if receipt_ref is not None:
            try:
                prompt_cache_ref = self._store_prompt_cache_receipt(turn_id, control)
            except Exception:
                prompt_cache_ref = None
            if prompt_cache_ref is not None:
                evidence_refs.append(prompt_cache_ref)
        self._append(
            turn_id,
            f"model.{model_status}",
            "running" if model_status == "completed" else (
                "cancelled" if model_status == "cancelled" else "failed"
            ),
            completed_summary if model_status == "completed" else _model_terminal_summary(model_status),
            receipt_ref=receipt_ref,
            evidence_refs=evidence_refs,
            error_code=error_code,
            step_id=control.step_id,
            tool_call_id=tool_call_id,
            model_request_id=control.model_request_id,
            model_call_purpose=control.purpose,
        )
        control.mark_model_terminal()
        return receipt_failed, tuple(evidence_refs)

    def _store_model_call_receipt(
        self,
        turn_id: str,
        control: _PlannerExecutionContext,
        *,
        status: str,
        error_code: str | None,
    ) -> str:
        receipt = control.terminal_receipt_payload(
            turn_id=turn_id,
            status=status,
            error_code=error_code,
        )
        return self._payloads.put(turn_id, "model-call-receipt", receipt)

    def _store_model_dispatch_authority_receipt(
        self,
        turn_id: str,
        control: _PlannerExecutionContext,
    ) -> str | None:
        receipt = control.dispatch_authority_receipt_payload(turn_id=turn_id)
        if receipt is None:
            return None
        return self._payloads.put(
            turn_id,
            "model-dispatch-authority-receipt",
            receipt,
        )

    @_serialize_event_commit
    def _store_model_attempt_dispatch(
        self,
        turn_id: str,
        payload: Mapping[str, object],
        *,
        step_id: str,
        tool_call_id: str | None,
        capability_id: str | None,
    ) -> _StoredModelAttemptDispatch:
        dispatch = validate_model_wire_attempt_dispatch(payload)
        if dispatch["turn_id"] != turn_id:
            raise AIKernelRuntimeError("model wire attempt Turn identity drifted")
        if self._atomic_store is not None:
            event = self._new_event(
                turn_id,
                "model.attempt.dispatched",
                "running",
                f"model wire attempt {dispatch['attempt_number']} dispatched",
                capability_id=capability_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                model_request_id=str(dispatch["model_request_id"]),
            )
            committed = self._atomic_store.commit_model_attempt_dispatch_bundle(
                event,
                expected_sequence=int(event["sequence"]) - 1,
                dispatch_payload=dispatch,
                run_lease=self._run_lease_context.get(),
            )
            run_lease = self._run_lease_context.get()
            return _StoredModelAttemptDispatch(
                committed.dispatch_payload_ref,
                durable=True,
                executor=lambda handler: self._atomic_store.execute_model_attempt_handler(
                    committed.attempt_id, handler, run_lease=run_lease,
                ),
                checkpoint_recorder=lambda cursor, previous_ref: self._atomic_store.commit_model_provider_checkpoint(
                    dispatch_payload=dispatch,
                    dispatch_payload_ref=committed.dispatch_payload_ref,
                    cursor=cursor,
                    expected_previous_ref=previous_ref,
                    run_lease=run_lease,
                ),
                resume_binder=lambda source: self._atomic_store.commit_model_provider_resume_binding(
                    dispatch_payload=dispatch, dispatch_payload_ref=committed.dispatch_payload_ref,
                    source=source, run_lease=run_lease,
                ),
            )
        dispatch_ref = self._payloads.put(
            turn_id,
            "model-wire-attempt-dispatch",
            dispatch,
        )
        self._append(
            turn_id,
            "model.attempt.dispatched",
            "running",
            f"model wire attempt {dispatch['attempt_number']} dispatched",
            capability_id=capability_id,
            payload_ref=dispatch_ref,
            step_id=step_id,
            tool_call_id=tool_call_id,
            model_request_id=str(dispatch["model_request_id"]),
        )
        return _StoredModelAttemptDispatch(dispatch_ref, durable=False)

    @_serialize_event_commit
    def _store_model_attempt_terminal(
        self,
        turn_id: str,
        payload: Mapping[str, object],
        *,
        dispatch_ref: str,
        step_id: str,
        tool_call_id: str | None,
        capability_id: str | None,
    ) -> str:
        receipt = validate_model_wire_attempt_receipt(payload)
        if receipt["turn_id"] != turn_id:
            raise AIKernelRuntimeError("model wire attempt receipt Turn identity drifted")
        if self._atomic_store is not None:
            status = str(receipt["status"])
            event = self._new_event(
                turn_id,
                "model.attempt.terminal",
                "completed" if status == "succeeded" else (
                    "cancelled" if status == "consumer_cancelled" else "failed"
                ),
                f"model wire attempt {receipt['attempt_number']} {status}",
                capability_id=capability_id,
                evidence_refs=(dispatch_ref,),
                error_code=receipt["error_code"] if isinstance(receipt["error_code"], str) else None,
                step_id=step_id,
                tool_call_id=tool_call_id,
                model_request_id=str(receipt["model_request_id"]),
            )
            committed = self._atomic_store.append_model_attempt_terminal_bundle(
                event,
                expected_sequence=int(event["sequence"]) - 1,
                attempt_receipt_payload=receipt,
                run_lease=self._run_lease_context.get(),
            )
            return committed.attempt_receipt_ref
        receipt_ref = self._payloads.put(
            turn_id,
            "model-wire-attempt-receipt",
            receipt,
        )
        status = str(receipt["status"])
        self._append(
            turn_id,
            "model.attempt.terminal",
            "completed" if status == "succeeded" else (
                "cancelled" if status == "consumer_cancelled" else "failed"
            ),
            f"model wire attempt {receipt['attempt_number']} {status}",
            capability_id=capability_id,
            receipt_ref=receipt_ref,
            evidence_refs=(dispatch_ref,),
            error_code=receipt["error_code"] if isinstance(receipt["error_code"], str) else None,
            step_id=step_id,
            tool_call_id=tool_call_id,
            model_request_id=str(receipt["model_request_id"]),
        )
        return receipt_ref

    def _store_prompt_cache_receipt(
        self,
        turn_id: str,
        control: _PlannerExecutionContext,
    ) -> str | None:
        receipt = control.prompt_cache_receipt_payload(turn_id=turn_id)
        if receipt is None:
            return None
        return self._payloads.put(turn_id, "prompt-cache-receipt", receipt)

    def _fail(self, turn_id: str, error: Exception) -> TurnReceipt:
        message = str(error).casefold()
        if "needs verified project evidence" in message:
            code, summary = "ai.insufficient_evidence", "verified project evidence is required"
        elif "baseline is stale" in message:
            code, summary = "ai.stale_baseline", "generation baseline is stale"
        else:
            code, summary = "ai.execution_failed", "AI execution failed"
        self._append_turn_terminal(turn_id, "turn.failed", "failed", summary, error_code=code)
        return self._receipt(turn_id)

    def _invoke_tool(self, turn_id: str, decision: Mapping[str, object]) -> None:
        intent, intent_ref, provider = self._record_tool_intent(turn_id, decision)
        self._execute_tool_intent(intent, intent_ref, provider, attempt=1)

    def _continue_mcp_request_state(
        self, turn_id: str, pending: Mapping[str, object],
    ) -> None:
        """Resume only the frozen stateless RPC; never re-plan or replay it."""
        state_ref = pending.get("_mcp_request_state_ref")
        intent_ref = pending.get("_mcp_intent_ref")
        if not isinstance(state_ref, str) or not isinstance(intent_ref, str):
            raise AIKernelRuntimeError("MCP continuation binding is unavailable")
        frozen = self._payloads.get(state_ref)
        if not isinstance(frozen, Mapping) or frozen.get("intent_ref") != intent_ref:
            raise AIKernelRuntimeError("MCP continuation binding drifted")
        request_state = frozen.get("request_state")
        if not isinstance(request_state, str) or not request_state or len(request_state.encode("utf-8")) > 4096:
            raise AIKernelRuntimeError("MCP continuation state is invalid")
        expires_at = frozen.get("expires_at")
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise AIKernelRuntimeError("MCP continuation expiry is invalid") from error
        if expiry.tzinfo is None or datetime.now(timezone.utc) >= expiry.astimezone(timezone.utc):
            raise AIKernelRuntimeError("MCP continuation has expired")
        try:
            intent = intent_from_payload(self._payloads.get(intent_ref))
        except Exception as error:
            raise AIKernelRuntimeError("MCP continuation intent is unavailable") from error
        if intent.turn_id != turn_id or frozen.get("capability_id") != intent.capability_id or frozen.get("operation_id") != intent.operation_id:
            raise AIKernelRuntimeError("MCP continuation intent identity drifted")
        contract = frozen.get("connection")
        if not isinstance(contract, Mapping) or dict(contract) != dict(intent.tool_contract or {}):
            raise AIKernelRuntimeError("MCP continuation connection drifted")
        self._settle_mcp_initial_effect(intent, state_ref, frozen)
        connection = contract.get("connection_identity")
        server_id = connection.get("server_id") if isinstance(connection, Mapping) else None
        if not isinstance(server_id, str) or not server_id or self._mcp_continuation_reconnector is None:
            raise AIKernelRuntimeError("MCP continuation fresh provider is unavailable")
        self._mcp_continuation_reconnector(server_id)
        resolved = self._registry.resolve(intent.capability_id)
        if resolved is None:
            raise AIKernelRuntimeError("MCP continuation provider is unavailable")
        definition, provider = resolved
        if (
            definition.mode != "read"
            or definition.operation_semantics != "read_only"
            or not _definition_matches_intent(definition, intent)
            or not hasattr(provider, "continue_request_state")
        ):
            raise AIKernelRuntimeError("MCP continuation fresh contract drifted")
        # A continuation is a new wire effect. Re-evaluate the current
        # Boundary before dispatch, while preserving the frozen arguments.
        request = self._require_request(turn_id)
        continuation_decision, boundary = self._evaluate_execution_boundary(
            request,
            definition,
            {"capability_id": intent.capability_id, "arguments": dict(intent.arguments)},
        )
        if (
            boundary is not None
            and (
                boundary.outcome not in {"allow", "allow_redacted"}
                or continuation_decision.get("arguments") != dict(intent.arguments)
            )
        ):
            raise AIKernelRuntimeError("MCP continuation was denied by current Boundary")
        # The provider receives arguments exclusively from the immutable
        # intent payload. The opaque requestState does not enter Event data.
        self._execute_tool_intent(
            intent, intent_ref, provider,
            attempt=int(frozen.get("round", 1)) + 1,
            continuation_state=request_state.encode("utf-8"),
        )

    @_serialize_event_commit
    def _record_tool_intent(
        self,
        turn_id: str,
        decision: Mapping[str, object],
    ) -> tuple[ToolInvocationIntent, str, object]:
        capability_id = str(decision["capability_id"])
        request = self._require_request(turn_id)
        manifest = self._manifest_for(turn_id, request)
        if capability_id not in manifest.capability_ids:
            raise AIKernelRuntimeError("capability is outside Turn manifest")
        resolved = self._registry.resolve(capability_id)
        if resolved is None:
            raise AIKernelRuntimeError("capability was revoked before invocation")
        definition, provider = resolved
        execution = decision.get("_execution")
        execution_map = execution if isinstance(execution, Mapping) else {}
        step_id = str(execution_map.get("step_id") or f"step-{uuid4().hex}")
        tool_call_id = str(execution_map.get("tool_call_id") or f"tool-call-{uuid4().hex}")
        self._check_tool_call_budget(turn_id, (tool_call_id,))
        arguments = decision.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise AIKernelRuntimeError("tool arguments must be an object")
        tool = tool_from_capability(definition)
        frozen_contract = decision.get("_tool_contract")
        if frozen_contract is None:
            if definition.tool_definition is not None:
                raise AIKernelRuntimeError("native tool contract snapshot is unavailable")
        elif not tool_matches_contract_identity(tool, frozen_contract):
            raise AIKernelRuntimeError("tool definition drifted after Boundary decision")
        frozen_approval = decision.get("_capability_requires_approval")
        if frozen_approval is None:
            if definition.tool_definition is not None:
                raise AIKernelRuntimeError("native capability approval snapshot is unavailable")
        elif not isinstance(frozen_approval, bool) or frozen_approval != definition.requires_approval:
            raise AIKernelRuntimeError("capability approval requirement drifted after Boundary decision")
        authorization_facts_ref = _optional_ref(decision.get("_frozen_authorization_ref"))
        authorization_facts_revision = decision.get("_frozen_authorization_revision")
        approval_fact_ref = _optional_ref(decision.get("_frozen_approval_ref"))
        if authorization_facts_ref is not None:
            if self._frozen_authorization is None or not isinstance(authorization_facts_revision, str):
                raise AIKernelRuntimeError("frozen authorization authority is unavailable")
            candidate = self._frozen_authorization.authorize_candidate(
                turn_id=turn_id,
                facts_ref=authorization_facts_ref,
                facts_revision=authorization_facts_revision,
                capability=definition,
                arguments=arguments,
                tool_call_id=tool_call_id,
                approval_ref=approval_fact_ref,
                approval_revision=decision.get("_frozen_approval_revision"),
                action_ref=decision.get("_frozen_approval_action_ref"),
                target_event_id=decision.get("_frozen_approval_target_event_id"),
            )
            if candidate is None:
                raise AIKernelRuntimeError("frozen authorization intent binding was denied")
            arguments = getattr(candidate, "sanitized_arguments", {})
            if not isinstance(arguments, Mapping):
                raise AIKernelRuntimeError("frozen authorization arguments are invalid")
        intent = build_intent(
            invocation_id=tool_call_id,
            turn_id=turn_id,
            step_id=step_id,
            operation_id=str(request["operation_id"]),
            tool=tool,
            arguments=arguments,
            requires_approval=(
                decision.get("_frozen_requires_approval") is True
                if authorization_facts_ref is not None
                else definition.requires_approval
            ),
            authorization_facts_ref=authorization_facts_ref,
            authorization_facts_revision=(
                authorization_facts_revision
                if isinstance(authorization_facts_revision, str)
                else None
            ),
            approval_fact_ref=approval_fact_ref,
        )
        intent_payload = intent_to_payload(intent)
        if self._atomic_store is not None:
            event = self._new_event(
                turn_id,
                "tool.intent.recorded",
                "running",
                "tool invocation intent recorded",
                capability_id=capability_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
            )
            committed = self._atomic_store.append_intent_bundle(
                event,
                expected_sequence=int(event["sequence"]) - 1,
                intent_kind="tool-invocation-intent",
                intent_payload=intent_payload,
                run_lease=self._run_lease_context.get(),
            )
            intent_ref = committed.intent_payload_ref
        else:
            intent_ref = self._payloads.put(turn_id, "tool-invocation-intent", intent_payload)
            self._append(
                turn_id,
                "tool.intent.recorded",
                "running",
                "tool invocation intent recorded",
                capability_id=capability_id,
                payload_ref=intent_ref,
                step_id=step_id,
                tool_call_id=tool_call_id,
            )
        self._ensure_tool_effect(intent, intent_ref)
        return intent, intent_ref, provider

    def _ensure_tool_effect(
        self, intent: ToolInvocationIntent, intent_ref: str, *, continuation_round: int | None = None,
    ) -> Effect | None:
        if self._effect_runner is None:
            return None
        request = self._require_request(intent.turn_id)
        frozen_contract = dict(intent.tool_contract or {})
        resolved = self._registry.resolve(intent.capability_id)
        frozen_mode = (
            str(frozen_contract.get("effect"))
            if frozen_contract.get("effect") is not None
            else (resolved[0].mode if resolved is not None else "external")
        )
        effect_class = (
            EffectClass.AT_MOST_ONCE
            if intent.idempotency == "never_retry"
            else EffectClass.PURE
            if frozen_mode == "read"
            else {
                "idempotent": EffectClass.IDEMPOTENT,
                "verify_before_retry": EffectClass.QUERYABLE,
                "never_retry": EffectClass.AT_MOST_ONCE,
            }.get(intent.idempotency, EffectClass.NEEDS_REAUTH)
        )
        suffix = f'.continue.{continuation_round}' if continuation_round is not None else ''
        if suffix:
            parent = self._effect_runner.log.get(intent.invocation_id)
            if parent is None or parent.state is not EffectState.SETTLED_OK:
                raise AIKernelRuntimeError('MCP continuation parent RPC is not settled')
            effect_class = EffectClass.AT_MOST_ONCE
        effect_intent = EffectIntent(
            session_id=str(request["session_id"]),
            turn_id=intent.turn_id,
            root_id=str(request["operation_id"]),
            parent_id=intent.invocation_id if suffix else None,
            step_key=f"tool:{intent.step_id}:{intent.invocation_id}{suffix}",
            kind=f"tool_call_{effect_class.value.lower()}",
            effect_class=effect_class,
            purpose=EffectPurpose.PRIMARY,
            intent_ref=intent_ref,
            gate_decision_id=(
                intent.authorization_facts_ref
                or f"turn-gate:{intent.turn_id}:{intent.invocation_id}"
            ),
            rev_set={
                "capability_revision": str(intent.capability_version),
                "authorization_revision": intent.authorization_facts_revision or "none",
                "tool_contract_revision": str(frozen_contract.get("version", "legacy-v1")),
            },
            payload=intent_to_payload(intent),
            idem_key=f'{intent.idempotency_key}{suffix}' if suffix else intent.idempotency_key,
            operation_id_override=f'{intent.invocation_id}{suffix}',
        )
        effect, _ = self._effect_runner.log.plan(
            effect_intent, now=int(datetime.now(timezone.utc).timestamp()),
        )
        return effect

    def _claim_tool_effect(
        self, intent: ToolInvocationIntent, intent_ref: str, *, continuation_round: int | None = None,
    ) -> tuple[Effect | None, bool]:
        effect = self._ensure_tool_effect(intent, intent_ref, continuation_round=continuation_round)
        if effect is None or self._effect_runner is None:
            return effect, True
        now = int(datetime.now(timezone.utc).timestamp())
        return self._effect_runner.claim_planned(
            effect.operation_id,
            now=now,
            lease_expires_at=now + max(30, (intent.timeout_ms // 1000) + 5),
        )

    def _prepare_tool_invocation(
        self, intent, intent_ref, provider, *, attempt, continuation_state=None,
    ):
        turn_id = intent.turn_id
        # Recovery may enter with an already recorded intent, without running
        # preflight again. Verify its frozen authority before claiming any new
        # effect; the same invocation keeps its existing budget reservation.
        self._check_tool_call_budget(turn_id, (intent.invocation_id,))
        claimed_effect, claimed = self._claim_tool_effect(
            intent, intent_ref, continuation_round=attempt if continuation_state is not None else None,
        )
        if not claimed:
            raise _ToolTurnConverged()
        if claimed_effect is not None and continuation_state is None:
            attempt = self._tool_attempt_count(turn_id, intent.invocation_id) + 1
        try:
            resolved = self._registry.resolve(intent.capability_id)
            if resolved is None or not _definition_matches_intent(resolved[0], intent):
                raise AIKernelRuntimeError("tool definition drifted after invocation intent")
            if intent.authorization_facts_ref is not None and (
                self._frozen_authorization is None
                or not self._frozen_authorization.prepare_intent(intent)
            ):
                raise AIKernelRuntimeError("frozen authorization intent is unavailable")
            definition = resolved[0]
            turn_request = self._require_request(turn_id)
            observer = _RuntimeToolDispatchObserver(self, intent, intent_ref, attempt, turn_request)
            provider_request = {
                "turn_id": turn_id,
                "operation_id": intent.operation_id,
                # These are frozen, opaque host-provider bindings.  They are
                # intentionally not model-visible Event data and must never
                # be forwarded by a provider into an untrusted child runtime.
                "intent_ref": intent_ref,
                "capability_id": intent.capability_id,
                "capability_version": intent.capability_version,
                "authorization_facts_ref": intent.authorization_facts_ref,
                "authorization_facts_revision": intent.authorization_facts_revision,
                "approval_fact_ref": intent.approval_fact_ref,
                "scope": turn_request["scope"],
                # Nested model bindings must verify the same immutable
                # privacy authority that governed the accepted Turn.  This
                # stays inside the provider request and is never emitted as
                # a tool event payload.
                "privacy": turn_request["privacy"],
                "arguments": dict(intent.arguments),
                "tool_call_id": intent.invocation_id,
                "attempt": attempt,
                "idempotency_key": intent.idempotency_key,
                "timeout_ms": intent.timeout_ms,
                "resource_locks": list(intent.resource_locks),
                "tool_contract": dict(intent.tool_contract) if intent.tool_contract is not None else None,
            }
        except Exception:
            self._dispatcher.abandon_prepared(intent.invocation_id)
            raise
        dispatch_provider = (
            _MCPContinuationDispatchProvider(provider, continuation_state)
            if continuation_state is not None else provider
        )
        dispatch_request = ToolDispatchRequest(
                provider_request=provider_request,
                execution_mode=intent.execution_mode,  # type: ignore[arg-type]
                resource_locks=intent.resource_locks,
                invocation_id=intent.invocation_id,
                attempt=attempt,
                timeout_ms=intent.timeout_ms,
                # A tool must explicitly opt into additional nested model
                # work at its dispatch boundary.  Existing capabilities
                # retain one compatible, independently-owned handle.
                nested_model_handle_budget=(
                    int(intent.tool_contract.get("nested_model_handle_budget", 1))
                    if intent.tool_contract is not None else 1
                ),
                # The immutable Tool contract is the only authority for
                # nested model access. Third-party Plugin and MCP providers
                # remain denied even while their legacy budget field is 1.
                nested_model_handle_authorized=(
                    intent.tool_contract is not None
                    and intent.tool_contract.get("source") == "core"
                ),
            )
        return definition, claimed_effect, attempt, dispatch_provider, dispatch_request, observer

    def _dispatch_prepared_tool(self, prepared):
        _, _, _, provider, request, observer = prepared
        try:
            return True, self._dispatcher.dispatch(provider, request, observer)
        except Exception as error:
            return False, error

    def _execute_tool_intent(
        self, intent, intent_ref, provider, *, attempt, continuation_state=None,
    ):
        prepared = self._prepare_tool_invocation(
            intent, intent_ref, provider, attempt=attempt,
            continuation_state=continuation_state,
        )
        self._settle_tool_invocation(
            intent, intent_ref, prepared, self._dispatch_prepared_tool(prepared),
            continuation_state=continuation_state,
        )

    def _settle_tool_invocation(
        self, intent, intent_ref, prepared, dispatched, *, continuation_state=None, buffer_ref=None,
    ):
        turn_id = intent.turn_id
        definition, claimed_effect, attempt, _, _, _ = prepared
        succeeded, result = dispatched
        try:
            if not succeeded:
                raise result
            result = dict(result)
            receipt_ref = _optional_ref(result.get("receipt_ref"))
            operation_receipt_payload = result.get("operation_receipt")
            if operation_receipt_payload is not None and receipt_ref is not None:
                raise AIKernelRuntimeError("capability returned both receipt reference and receipt payload")
            payload_ref = _optional_ref(result.get("payload_ref"))
            result_payload = result.get("result") if payload_ref is None and "result" in result else None
            if (
                definition.operation_semantics == "receipt_required"
                and receipt_ref is None
                and operation_receipt_payload is None
            ):
                raise AIKernelRuntimeError("capability did not return required operation receipt")
            evidence_refs = _refs(result.get("evidence_refs"))
            outcome = ToolInvocationOutcome(
                invocation_id=intent.invocation_id,
                turn_id=turn_id,
                capability_id=intent.capability_id,
                attempt=attempt,
                status="completed",
                effect_certainty=(
                    "confirmed_none"
                    if definition.mode == "read"
                    else "confirmed_applied"
                ),
                payload_ref=payload_ref,
                receipt_ref=receipt_ref,
                evidence_refs=evidence_refs,
                error_code=None,
                retryable=False,
            )
            outcome_ref, receipt_ref, committed_result_ref = self._append_tool_outcome_bundle(
                outcome,
                turn_id,
                summary="tool outcome recorded",
                capability_id=intent.capability_id,
                evidence_refs=evidence_refs,
                step_id=intent.step_id,
                tool_call_id=intent.invocation_id,
                operation_receipt_payload=operation_receipt_payload,
                result_payload=result_payload,
            )
            payload_ref = committed_result_ref or payload_ref
            if self._effect_runner is not None and claimed_effect is not None and buffer_ref is None:
                self._effect_runner.settle_ok(
                    claimed_effect,
                    receipt_ref=outcome_ref,
                    receipt_kind="tool-outcome-receipt",
                    now=int(datetime.now(timezone.utc).timestamp()),
                )
            self._append(
                turn_id, "tool.completed", "running",
                str(result.get("summary", "tool completed")),
                capability_id=intent.capability_id, payload_ref=payload_ref,
                receipt_ref=receipt_ref, evidence_refs=evidence_refs,
                step_id=intent.step_id, tool_call_id=intent.invocation_id,
            )
            post_should_stop = self._run_post_tool_use_hook(
                turn_id=turn_id,
                step_id=intent.step_id,
                tool_call_id=intent.invocation_id,
                capability_id=intent.capability_id,
                payload_ref=payload_ref,
            )
            if post_should_stop:
                self._append_turn_terminal(
                    turn_id, "turn.cancelled", "cancelled",
                    "PostToolUse hook stopped the Turn after tool execution",
                    error_code="ai.hook_post_tool_stopped",
                    step_id=intent.step_id,
                    tool_call_id=intent.invocation_id,
                )
                raise _ToolTurnConverged()
        except _ToolTurnConverged:
            raise
        except (ToolDispatchCancelled, ToolDispatchDeadlineExceeded) as error:
            safe_to_cancel = definition.mode == "read" or not error.provider_started
            if safe_to_cancel:
                error_code = (
                    "ai.tool_cancelled"
                    if isinstance(error, ToolDispatchCancelled)
                    else "ai.tool_deadline_exceeded"
                )
                if self._effect_runner is not None and claimed_effect is not None:
                    self._effect_runner.settle_error(
                        claimed_effect,
                        error_ref=error_code,
                        now=int(datetime.now(timezone.utc).timestamp()),
                    )
                self._record_cancelled_tool_outcome(
                    intent,
                    error_code=error_code,
                    timed_out=isinstance(error, ToolDispatchDeadlineExceeded),
                )
            else:
                self._record_unknown_tool_outcome(
                    intent,
                    attempt=attempt,
                    error_code=(
                        "ai.tool_cancel_unconfirmed"
                        if isinstance(error, ToolDispatchCancelled)
                        else "ai.tool_timeout_unconfirmed"
                    ),
                )
            raise _ToolTurnConverged() from error
        except ToolDispatchFailure as error:
            # Stateless 2026 continuation is an explicit user-controlled
            # pause.  The opaque state is persisted only in an immutable
            # payload; the public event carries its reference, never value.
            if error.error_code == "mcp.input_required" and error.continuation_state is not None:
                # This slice supports exactly one explicit continuation. A
                # second input_required is an uncertain remote outcome and is
                # quarantined rather than becoming another resumable round.
                if attempt != 1:
                    self._record_unknown_tool_outcome(
                        intent, attempt=attempt, error_code="mcp.continuation_round_exhausted",
                    )
                    raise _ToolTurnConverged() from error
                try:
                    state_text = error.continuation_state.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    self._record_failed_tool_outcome(intent, attempt=attempt, error_code="mcp.continuation_invalid", effect_certainty="confirmed_none")
                    raise _ToolTurnConverged() from error
                # Only the immutable payload carries opaque remote state and
                # frozen arguments. Pending is intentionally refs/identity.
                continuation_payload = {
                    "schema_version": "1.0.0",
                    "request_state": state_text,
                    "intent_ref": intent_ref,
                    "capability_id": intent.capability_id,
                    "connection": dict(intent.tool_contract or {}),
                    "operation_id": intent.operation_id,
                    "round": attempt,
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=_MCP_CONTINUATION_TTL_SECONDS)
                    ).isoformat(),
                }
                waiting = {
                    "_kind": "mcp_request_state_v1",
                    "capability_id": intent.capability_id,
                    "_execution": {"step_id": intent.step_id, "tool_call_id": intent.invocation_id},
                    "_mcp_request_state_ref": None,
                    "_mcp_intent_ref": intent_ref,
                    "_mcp_round": attempt,
                }
                deferred = self._batch_waitings.get()
                if deferred is not None:
                    waiting['_tool_batch_step_id'] = intent.step_id
                    self._payloads.get_or_create_immutable_payload(
                        turn_id, f'tool-batch-mcp-wait-v1-{intent.invocation_id}',
                        {'payload': continuation_payload, 'waiting': waiting},
                    )
                    deferred.append((intent, attempt, continuation_payload, waiting))
                else:
                    self._persist_mcp_waiting(intent, attempt, continuation_payload, waiting)
                raise _ToolTurnConverged() from error
            effect_certainty = (
                error.effect_certainty
                if continuation_state is not None
                else ("confirmed_none" if definition.mode == "read" else error.effect_certainty)
            )
            recoverable_by_core = (
                intent.idempotency == "idempotent"
                and effect_certainty == "confirmed_none"
                and error.error_code in intent.retryable_error_codes
                and attempt < intent.max_attempts
            )
            if recoverable_by_core:
                delay_ms = max(
                    intent.retry_backoff_ms,
                    error.retry_after_ms or 0,
                )
                self._record_retryable_attempt_failure(
                    intent,
                    attempt=attempt,
                    error_code=error.error_code,
                    backoff_ms=delay_ms,
                )
                raise _ToolTurnConverged() from error
            if effect_certainty == "unknown":
                self._record_unknown_tool_outcome(
                    intent,
                    attempt=attempt,
                    error_code="ai.tool_outcome_unknown",
                )
            else:
                exhausted = (
                    error.error_code in intent.retryable_error_codes
                    and attempt >= intent.max_attempts
                )
                terminal_error = (
                    "ai.tool_retry_exhausted" if exhausted else error.error_code
                )
                if self._effect_runner is not None and claimed_effect is not None:
                    self._effect_runner.settle_error(
                        claimed_effect,
                        error_ref=terminal_error,
                        now=int(datetime.now(timezone.utc).timestamp()),
                    )
                self._record_failed_tool_outcome(
                    intent,
                    attempt=attempt,
                    error_code=(
                        terminal_error
                    ),
                    effect_certainty=effect_certainty,
                )
            raise _ToolTurnConverged() from error
        except Exception as error:
            if definition.mode != "read":
                self._record_unknown_tool_outcome(
                    intent,
                    attempt=attempt,
                    error_code="ai.tool_outcome_unknown",
                )
            else:
                if self._effect_runner is not None and claimed_effect is not None:
                    self._effect_runner.settle_error(
                        claimed_effect,
                        error_ref="ai.tool_failed",
                        now=int(datetime.now(timezone.utc).timestamp()),
                    )
                self._record_failed_tool_outcome(
                    intent,
                    attempt=attempt,
                    error_code="ai.tool_failed",
                    effect_certainty="confirmed_none",
                )
            raise _ToolTurnConverged() from error

    @_serialize_event_commit
    def _persist_mcp_waiting(self, intent, attempt, continuation_payload, waiting):
        turn_id = intent.turn_id
        event = self._new_event(
            turn_id, "mcp.continuation.required", "waiting_approval",
            "MCP continuation requires explicit user action",
            capability_id=intent.capability_id, step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        kind = (f'mcp-request-state-v1-{attempt}-{intent.invocation_id}'
                if '_tool_batch_step_id' in waiting else f'mcp-request-state-v1-{attempt}')
        if self._atomic_store is not None:
            self._atomic_store.append_waiting_mcp_continuation_bundle(
                event, expected_sequence=int(event["sequence"]) - 1,
                immutable_kind=kind, immutable_payload=continuation_payload,
                pending=waiting, run_lease=self._run_lease_context.get(),
            )
        else:
            state_ref = self._payloads.get_or_create_immutable_payload(turn_id, kind, continuation_payload)
            waiting["_mcp_request_state_ref"] = state_ref
            self._append(turn_id, "mcp.continuation.required", "waiting_approval",
                         "MCP continuation requires explicit user action", capability_id=intent.capability_id,
                         payload_ref=state_ref, step_id=intent.step_id, tool_call_id=intent.invocation_id)
            self._state.put_pending(turn_id, waiting)
        state_ref = self._payloads.get_immutable_payload(turn_id, kind)[0]
        self._settle_mcp_initial_effect(intent, state_ref, continuation_payload)

    def _settle_mcp_initial_effect(self, intent, state_ref, frozen):
        """A durable input_required response proves the first RPC returned."""
        if self._effect_runner is None:
            return
        if (not isinstance(frozen, Mapping) or frozen.get('intent_ref') is None
            or frozen.get('capability_id') != intent.capability_id
            or frozen.get('operation_id') != intent.operation_id
            or frozen.get('round') != 1
            or dict(frozen.get('connection', {})) != dict(intent.tool_contract or {})
            or not state_ref.startswith(f'crp://session/{intent.turn_id}/')
            or dict(self._payloads.get(state_ref)) != dict(frozen)):
            raise AIKernelRuntimeError('MCP input_required receipt binding drifted')
        intent_ref = frozen['intent_ref']
        if intent_from_payload(self._payloads.get(intent_ref)) != intent:
            raise AIKernelRuntimeError('MCP input_required frozen intent drifted')
        events = tuple(self._events.events_after(intent.turn_id))
        if (self._tool_attempt_count(intent.turn_id, intent.invocation_id) != 1
            or not any(event.get('type') == 'tool.started'
                       and _correlation_text(event, 'tool_call_id') == intent.invocation_id
                       and _correlation_text(event, 'step_id') == intent.step_id
                       and event['data'].get('payload_ref') == intent_ref for event in events)
            or not any(event.get('type') == 'mcp.continuation.required'
                   and _correlation_text(event, 'tool_call_id') == intent.invocation_id
                   and _correlation_text(event, 'step_id') == intent.step_id
                   and event['data'].get('payload_ref') == state_ref for event in events)):
            raise AIKernelRuntimeError('MCP input_required receipt is not durable')
        effect = self._effect_runner.log.get(intent.invocation_id)
        if effect is None:
            raise AIKernelRuntimeError('MCP initial RPC effect is unavailable')
        now = int(datetime.now(timezone.utc).timestamp())
        if effect.state is EffectState.SETTLED_OK:
            if effect.result_ref != state_ref:
                raise AIKernelRuntimeError('MCP initial RPC receipt drifted')
        elif effect.state is EffectState.UNKNOWN:
            self._effect_runner.settle_verified_ok(effect, receipt_ref=state_ref,
                receipt_kind='mcp-input-required-receipt', now=now)
        elif effect.state is EffectState.INFLIGHT:
            self._effect_runner.settle_ok(effect, receipt_ref=state_ref,
                receipt_kind='mcp-input-required-receipt', now=now)
        else:
            raise AIKernelRuntimeError('MCP initial RPC effect is not continuable')

    def _evaluate_execution_boundary(
        self,
        request: Mapping[str, object],
        definition: CapabilityDefinition,
        decision: Mapping[str, object],
    ) -> tuple[dict[str, object], ToolExecutionBoundaryDecision | None]:
        if self._execution_boundary is None:
            return dict(decision), None
        boundary = self._execution_boundary.evaluate(request, definition, decision)
        if boundary.outcome not in {"allow", "allow_redacted", "ask", "deny"}:
            raise AIKernelRuntimeError("Boundary returned an unsupported outcome")
        if boundary.policy_revision < 1:
            raise AIKernelRuntimeError("Boundary returned an invalid policy revision")
        execution_decision = dict(decision)
        execution_decision["arguments"] = dict(boundary.arguments)
        execution_decision["boundary"] = _boundary_payload(boundary)
        validate_governed_payload(execution_decision)
        return execution_decision, boundary

    def _resolve_manifest(self, request: Mapping[str, object]) -> CapabilityManifest:
        registered = self._registry.list()
        manifest = self._manifest_resolver.resolve(request, registered)
        return validate_manifest_for_request(manifest, request, registered)

    def _manifest_for(
        self,
        turn_id: str,
        request: Mapping[str, object],
    ) -> CapabilityManifest:
        for event in self._events.events_after(turn_id):
            if event.get("type") != "context.resolved":
                continue
            data = event.get("data")
            payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
            if isinstance(payload_ref, str):
                stored = self._payloads.get(payload_ref)
                try:
                    context_manifest = context_manifest_from_payload(stored)
                except ContextManifestError:
                    manifest = manifest_from_payload(stored)
                else:
                    manifest = manifest_from_payload(
                        self._payloads.get(context_manifest.capability_manifest_ref)
                    )
                    validate_context_manifest_for_request(
                        context_manifest, request, capability_manifest=manifest
                    )
                if manifest.turn_id != turn_id:
                    raise AIKernelRuntimeError("Turn capability manifest identity drifted")
                return validate_manifest_for_request(
                    manifest,
                    request,
                    self._registry.list(),
                )
        return self._resolve_manifest(request)

    def _apply_pre_tool_hook(
        self,
        turn_id: str,
        decision: Mapping[str, object],
        *,
        step_id: str,
        tool_call_id: str,
    ) -> dict[str, object]:
        """Run the local Codex PreToolUse interceptor before any Boundary work.

        A deny terminates this invocation before the Boundary evaluator, frozen
        authorization checks, dispatcher, or Provider can run.  A pass only
        supplies the original or Codex-updated input to the existing execution
        path; it does not grant authority.
        """

        if self._hook_host is None:
            return dict(decision)
        arguments = decision.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise AIKernelRuntimeError("tool arguments must be an object")
        receipt = self.invoke_lifecycle_hook(
            turn_id,
            HookEvent.PRE_TOOL_USE,
            {
                "turn_id": turn_id,
                "step_id": step_id,
                "tool_call_id": tool_call_id,
                "tool_name": str(decision.get("capability_id", "")),
                "tool_input": dict(arguments),
            },
            step_id=step_id,
            tool_call_id=tool_call_id,
        )
        if receipt.outcome.dispatch_blocked:
            raise AIKernelRuntimeError(
                receipt.outcome.stop_reason or "PreToolUse hook blocked dispatch"
            )
        updated = receipt.outcome.updated_input
        if updated is None:
            return dict(decision)
        if not isinstance(updated, Mapping):
            raise AIKernelRuntimeError("PreToolUse updatedInput must be an object")
        result = dict(decision)
        result["arguments"] = dict(updated)
        return result

    def invoke_lifecycle_hook(
        self,
        turn_id: str,
        event: HookEvent | str,
        payload: Mapping[str, object],
        *,
        step_id: str | None = None,
        tool_call_id: str | None = None,
        model_request_id: str | None = None,
    ) -> HookInvocationReceipt:
        """Invoke any Codex lifecycle event against the Turn-frozen snapshot.

        This is the single Harness entry used by the built-in Turn/Tool
        lifecycle and by compaction or subagent coordinators.  The Hook Host
        applies the event-specific Codex outcome locally; this method only
        appends its safe receipt to the authoritative Turn stream.
        """

        if self._hook_host is None:
            raise AIKernelRuntimeError("Hook Host is unavailable")
        resolved_event = HookEvent(event)
        snapshot_ref, snapshot = self._hook_snapshot_for(turn_id)
        receipt = self._hook_host.invoke(resolved_event, payload, snapshot=snapshot)
        request = self._require_request(turn_id)
        scope = request.get("scope")
        project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
        receipt_payload = hook_invocation_receipt_to_payload(
            receipt,
            turn_id=turn_id,
            project_id=project_id if isinstance(project_id, str) else None,
            policy_snapshot_ref=snapshot_ref,
            snapshot=snapshot,
        )
        if self._atomic_store is not None:
            with self._event_commit(turn_id):
                event_payload = self._new_event(
                    turn_id,
                    "hook.invoked",
                    "running",
                    f"Codex {resolved_event.value} Hook invocation recorded",
                    step_id=step_id,
                    tool_call_id=tool_call_id,
                    model_request_id=model_request_id,
                )
                self._atomic_store.append_hook_receipt_bundle(
                    event_payload,
                    expected_sequence=int(event_payload["sequence"]) - 1,
                    receipt_kind="codex-hook-invocation-receipt",
                    receipt_payload=receipt_payload,
                    run_lease=self._run_lease_context.get(),
                )
        else:
            receipt_ref = self._payloads.put(
                turn_id, "codex-hook-invocation-receipt", receipt_payload
            )
            self._append(
                turn_id,
                "hook.invoked",
                "running",
                f"Codex {resolved_event.value} Hook invocation recorded",
                receipt_ref=receipt_ref,
                step_id=step_id,
                tool_call_id=tool_call_id,
                model_request_id=model_request_id,
            )
        return receipt

    def _hook_event_for(self, event: Mapping[str, object]) -> HookEvent | None:
        data = event.get("data")
        receipt_ref = data.get("receipt_ref") if isinstance(data, Mapping) else None
        if not isinstance(receipt_ref, str):
            return None
        try:
            receipt = self._payloads.get(receipt_ref)
            return HookEvent(receipt.get("event")) if isinstance(receipt, Mapping) else None
        except (KeyError, TypeError, ValueError):
            return None

    def _run_post_tool_use_hook(
        self,
        *,
        turn_id: str,
        step_id: str,
        tool_call_id: str,
        capability_id: str,
        payload_ref: str | None,
    ) -> bool:
        if not self._hook_event_enabled(turn_id, HookEvent.POST_TOOL_USE):
            return False
        for event in reversed(tuple(self._events.events_after(turn_id))):
            if (
                event.get("type") != "hook.invoked"
                or _correlation_text(event, "tool_call_id") != tool_call_id
                or self._hook_event_for(event) is not HookEvent.POST_TOOL_USE
            ):
                continue
            data = event.get("data")
            receipt_ref = data.get("receipt_ref") if isinstance(data, Mapping) else None
            receipt = self._payloads.get(receipt_ref) if isinstance(receipt_ref, str) else None
            normalized = receipt.get("normalized_outcome") if isinstance(receipt, Mapping) else None
            return bool(
                isinstance(normalized, Mapping)
                and normalized.get("turn_stopped") is True
            )
        post = self.invoke_lifecycle_hook(
            turn_id,
            HookEvent.POST_TOOL_USE,
            {
                "turn_id": turn_id,
                "step_id": step_id,
                "tool_call_id": tool_call_id,
                "tool_name": capability_id,
                "tool_status": "completed",
                "result_ref": payload_ref,
            },
            step_id=step_id,
            tool_call_id=tool_call_id,
        )
        if post.outcome.feedback:
            feedback_ref = self._payloads.put(
                turn_id,
                "codex-hook-tool-feedback",
                {
                    "schema_version": "1.0.0",
                    "event": HookEvent.POST_TOOL_USE.value,
                    "items": list(post.outcome.feedback),
                },
            )
            self._append(
                turn_id, "hook.feedback", "running",
                "PostToolUse hook supplied planner feedback",
                payload_ref=feedback_ref,
                step_id=step_id,
                tool_call_id=tool_call_id,
            )
        return post.outcome.should_stop

    def _hook_event_enabled(self, turn_id: str, event: HookEvent | str) -> bool:
        if self._hook_host is None:
            return False
        _, snapshot = self._hook_snapshot_for(turn_id)
        return bool(snapshot.handlers_for(HookEvent(event)))

    def _freeze_hook_snapshot(self, turn_id: str) -> tuple[str, HookPolicySnapshot]:
        """Bind the catalog revision to one Turn before its first event runs."""

        if self._hook_host is None:
            raise AIKernelRuntimeError("Hook Host is unavailable")
        existing = self._payloads.get_immutable_payload(
            turn_id, "codex-hook-policy-snapshot"
        )
        if existing is not None:
            ref, payload = existing
            snapshot = hook_policy_snapshot_from_payload(payload)
        else:
            candidate = self._hook_host.current_snapshot()
            try:
                ref = self._payloads.get_or_create_immutable_payload(
                    turn_id,
                    "codex-hook-policy-snapshot",
                    hook_policy_snapshot_to_payload(candidate),
                )
                snapshot = candidate
            except ValueError:
                winner = self._payloads.get_immutable_payload(
                    turn_id, "codex-hook-policy-snapshot"
                )
                if winner is None:
                    raise
                ref, payload = winner
                snapshot = hook_policy_snapshot_from_payload(payload)
        self._hook_host.assert_snapshot_executable(snapshot)
        with self._hook_snapshots_lock:
            cached = self._hook_snapshots.get(turn_id)
            if cached is not None and cached != (ref, snapshot):
                raise AIKernelRuntimeError("Turn Hook policy snapshot identity drifted")
            self._hook_snapshots[turn_id] = (ref, snapshot)
        return ref, snapshot

    def _hook_snapshot_for(self, turn_id: str) -> tuple[str, HookPolicySnapshot]:
        with self._hook_snapshots_lock:
            cached = self._hook_snapshots.get(turn_id)
        if cached is not None:
            return cached
        existing = self._payloads.get_immutable_payload(
            turn_id, "codex-hook-policy-snapshot"
        )
        if existing is None:
            raise AIKernelRuntimeError("Turn Hook policy snapshot is unavailable")
        ref, payload = existing
        snapshot = hook_policy_snapshot_from_payload(payload)
        self._hook_host.assert_snapshot_executable(snapshot)
        with self._hook_snapshots_lock:
            cached = self._hook_snapshots.setdefault(turn_id, (ref, snapshot))
        if cached != (ref, snapshot):
            raise AIKernelRuntimeError("Turn Hook policy snapshot identity drifted")
        return cached

    def _ensure_expert_selection(
        self, turn_id: str, request: Mapping[str, object],
        manifest: CapabilityManifest, *, allow_create: bool,
    ) -> Mapping[str, object] | None:
        if self._expert_binding is None:
            return None
        existing = self._payloads.get_immutable_payload(turn_id, "expert-selection-receipt-v1")
        if existing is not None:
            _, payload = existing
            if not isinstance(payload, Mapping):
                raise AIKernelRuntimeError("expert selection receipt is invalid")
            return payload
        if not allow_create:
            return None
        receipt = self._expert_binding.select(
            request, manifest, self._manifest_capabilities(manifest)
        )
        if not isinstance(receipt, Mapping):
            raise AIKernelRuntimeError("expert selection authority returned invalid receipt")
        validate_governed_payload(receipt)
        try:
            self._append_expert_immutable_event(
                turn_id, event_type="expert.selection.recorded", summary="expert selection recorded",
                immutable_kind="expert-selection-receipt-v1", payload=receipt,
            )
        except (TurnEventConflict, ValueError):
            winner = self._payloads.get_immutable_payload(
                turn_id, "expert-selection-receipt-v1"
            )
            if winner is None or not isinstance(winner[1], Mapping):
                raise
            return winner[1]
        return receipt

    def _ensure_expert_binding_snapshot(
        self, turn_id: str, request: Mapping[str, object],
        selection: Mapping[str, object], manifest: CapabilityManifest,
        context_manifest: ContextManifest,
    ) -> Mapping[str, object] | None:
        if self._expert_binding is None:
            return None
        capabilities = self._manifest_capabilities(manifest)
        existing = self._payloads.get_immutable_payload(turn_id, "expert-binding-snapshot-v1")
        if existing is not None:
            _, payload = existing
            if not isinstance(payload, Mapping):
                raise AIKernelRuntimeError("expert binding snapshot is invalid")
            self._expert_binding.verify_replay(
                request, selection, payload, manifest, context_manifest, capabilities,
            )
            return payload
        snapshot = self._expert_binding.freeze(
            request, selection, manifest, context_manifest, capabilities,
        )
        if snapshot is None:
            return None
        if not isinstance(snapshot, Mapping):
            raise AIKernelRuntimeError("expert binding authority returned invalid snapshot")
        validate_governed_payload(snapshot)
        try:
            self._append_expert_immutable_event(
                turn_id, event_type="expert.binding.frozen", summary="expert binding frozen",
                immutable_kind="expert-binding-snapshot-v1", payload=snapshot,
            )
        except (TurnEventConflict, ValueError):
            winner = self._payloads.get_immutable_payload(
                turn_id, "expert-binding-snapshot-v1"
            )
            if winner is None or not isinstance(winner[1], Mapping):
                raise
            self._expert_binding.verify_replay(
                request, selection, winner[1], manifest, context_manifest, capabilities,
            )
            return winner[1]
        return snapshot

    @_serialize_event_commit
    def _append_expert_immutable_event(
        self, turn_id: str, *, event_type: str, summary: str,
        immutable_kind: str, payload: Mapping[str, object],
    ) -> str:
        if self._atomic_store is not None:
            event = self._new_event(turn_id, event_type, "running", summary)
            committed = self._atomic_store.append_event_with_immutable_payload(
                event, expected_sequence=int(event["sequence"]) - 1,
                immutable_kind=immutable_kind, immutable_payload=payload,
                run_lease=self._run_lease_context.get(),
            )
            return committed.immutable_payload_ref
        ref = self._payloads.get_or_create_immutable_payload(turn_id, immutable_kind, payload)
        self._append(turn_id, event_type, "running", summary, evidence_refs=(ref,))
        return ref

    def _ensure_expert_execution_receipt(
        self,
        turn_id: str,
        decision: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
    ) -> tuple[str, str]:
        """Persist context-grounded expert result + Receipt before Turn completion."""
        binding = self._payloads.get_immutable_payload(
            turn_id, "expert-binding-snapshot-v1",
        )
        terminal = self._payloads.get_immutable_payload(
            turn_id, "expert-job-terminal-snapshot-v1",
        )
        if binding is None or not isinstance(binding[1], Mapping):
            raise AIKernelRuntimeError("expert completion requires a frozen binding")
        waited_for_job = any(
            event.get("type") in {"expert.job.waiting", "expert.job.observed"}
            for event in events
        )
        if waited_for_job and (
            terminal is None or not isinstance(terminal[1], Mapping)
            or terminal[1].get("status") != "completed"
        ):
            raise AIKernelRuntimeError(
                "expert Job completion requires verified terminal evidence"
            )
        context_event = next(
            (event for event in reversed(events) if event.get("type") == "context.resolved"),
            None,
        )
        context_data = context_event.get("data") if isinstance(context_event, Mapping) else None
        context_ref = context_data.get("payload_ref") if isinstance(context_data, Mapping) else None
        if not isinstance(context_ref, str):
            raise AIKernelRuntimeError("expert completion context manifest is unavailable")
        manifest = context_manifest_from_payload(self._payloads.get(context_ref))
        snapshot = binding[1]
        if (
            manifest.turn_id != turn_id
            or manifest.project_id != snapshot.get("project_id")
            or manifest.manifest_id != snapshot.get("context_manifest_revision")
        ):
            raise AIKernelRuntimeError("expert completion context identity drifted")
        context_entry_refs = tuple(
            entry.payload_ref for entry in manifest.entries
            if entry.disclosure == "model" and entry.payload_ref is not None
        )
        # Reading each allowlisted payload is the consumption boundary. Bodies
        # are never copied into the result or Receipt; only frozen refs survive.
        for ref in context_entry_refs:
            self._payloads.get(ref)
        terminal_payload = terminal[1] if terminal is not None and isinstance(terminal[1], Mapping) else None
        media_receipt_ref = terminal_payload.get("receipt_ref") if terminal_payload is not None else None
        source_manifest_ref = terminal_payload.get("source_manifest_ref") if terminal_payload is not None else None
        if waited_for_job and (
            not isinstance(media_receipt_ref, str)
            or not isinstance(source_manifest_ref, str)
        ):
            raise AIKernelRuntimeError("expert completion evidence is invalid")
        summary = str(decision.get("summary") or "expert execution completed").strip()
        result_payload = {
            "schema_version": "1.0.0",
            "snapshot_id": snapshot.get("snapshot_id"),
            "project_id": snapshot.get("project_id"),
            "context_manifest_revision": manifest.manifest_id,
            "context_entry_refs": list(context_entry_refs),
            "media_terminal_ref": terminal[0] if terminal is not None else None,
            "summary": summary,
            "evidence_refs": list(dict.fromkeys(
                ref for ref in (
                    context_ref, media_receipt_ref, source_manifest_ref,
                    *context_entry_refs, *_refs(decision.get("evidence_refs")),
                ) if isinstance(ref, str)
            )),
        }
        validate_governed_payload(result_payload)
        result_ref = self._payloads.get_or_create_immutable_payload(
            turn_id, "expert-result-v1", result_payload,
        )
        tool_refs = tuple(dict.fromkeys(
            f"tool-invocation:{tool_call_id}"
            for event in events if event.get("type") == "tool.completed"
            if (tool_call_id := _correlation_text(event, "tool_call_id")) is not None
        ))
        receipt_payload = build_expert_execution_receipt(
            snapshot=snapshot,
            status="completed",
            stages=((
                ({"stage": "media_analysis", "status": "completed"},)
                if waited_for_job else ()
            ) + ({"stage": "context_grounding", "status": "completed"},)),
            tool_invocation_refs=tool_refs,
            input_evidence_refs=result_payload["evidence_refs"],
            output_refs=(result_ref,),
            # The result may contain multiline model/user-facing prose.  The
            # immutable Receipt is metadata-only and must never duplicate that
            # body (or fail because the prose contains newlines/sensitive
            # vocabulary).
            summary="expert execution completed",
        )
        existing = self._payloads.get_immutable_payload(
            turn_id, "expert-execution-receipt-v1",
        )
        if existing is not None:
            receipt_ref, stored = existing
            if not isinstance(stored, Mapping):
                raise AIKernelRuntimeError("expert execution Receipt is invalid")
            validated = validate_expert_execution_receipt(stored)
            if validated != receipt_payload or verify_expert_execution_receipt_replay(
                validated, snapshot=snapshot,
            ).get("status") != "ok":
                raise AIKernelRuntimeError("expert execution Receipt drifted")
        else:
            receipt_ref = self._append_expert_immutable_event(
                turn_id,
                event_type="expert.execution.receipted",
                summary="expert execution receipt persisted",
                immutable_kind="expert-execution-receipt-v1",
                payload=receipt_payload,
            )
        return result_ref, receipt_ref

    def _ensure_expert_memory_proposal(self, turn_id: str) -> str | None:
        """Submit one idempotent pending-review proposal and freeze its result."""
        if self._expert_memory_proposal_sink is None:
            return None
        prepare = getattr(self._expert_memory_proposal_sink, "prepare", None)
        commit = getattr(self._expert_memory_proposal_sink, "commit", None)
        if not callable(prepare) or not callable(commit):
            raise AIKernelRuntimeError("expert memory proposal outbox is unavailable")
        binding = self._payloads.get_immutable_payload(turn_id, "expert-binding-snapshot-v1")
        receipt = self._payloads.get_immutable_payload(turn_id, "expert-execution-receipt-v1")
        result = self._payloads.get_immutable_payload(turn_id, "expert-result-v1")
        if any(item is None or not isinstance(item[1], Mapping) for item in (binding, receipt, result)):
            raise AIKernelRuntimeError("expert memory proposal inputs are unavailable")
        assert binding is not None and receipt is not None and result is not None
        existing_result = self._payloads.get_immutable_payload(
            turn_id, "expert-memory-proposal-v1",
        )
        if existing_result is not None:
            stored = existing_result[1]
            if not isinstance(stored, Mapping) or (
                stored.get("status") != "pending_review"
                or stored.get("review_state") != "pending_review"
                or stored.get("memory_publication_state") != "not_published"
                or stored.get("expert_receipt_id") != receipt[1].get("receipt_id")
            ):
                raise AIKernelRuntimeError("expert memory proposal Receipt drifted")
            return existing_result[0]
        existing_intent = self._payloads.get_immutable_payload(
            turn_id, "expert-memory-proposal-intent-v1",
        )
        if existing_intent is not None:
            if not isinstance(existing_intent[1], Mapping):
                raise AIKernelRuntimeError("expert memory proposal intent is invalid")
            intent_payload = existing_intent[1]
            validate_governed_payload(intent_payload)
        else:
            intent_payload = prepare(binding[1], receipt[1], result[1])
            if not isinstance(intent_payload, Mapping):
                raise AIKernelRuntimeError("expert memory proposal intent is invalid")
            validate_governed_payload(intent_payload)
            self._append_expert_immutable_event(
                turn_id,
                event_type="expert.memory.proposal.intent.recorded",
                summary="expert memory proposal outbox intent recorded",
                immutable_kind="expert-memory-proposal-intent-v1",
                payload=dict(intent_payload),
            )
        try:
            proposal = commit(intent_payload, receipt[1])
        except Exception as error:
            raise _ExpertProposalCheckpointPending(
                "expert memory proposal commit requires recovery"
            ) from error
        if (
            not isinstance(proposal, Mapping)
            or proposal.get("status") != "pending_review"
            or proposal.get("review_state") != "pending_review"
            or proposal.get("memory_publication_state") != "not_published"
            or proposal.get("expert_receipt_id") != receipt[1].get("receipt_id")
        ):
            raise AIKernelRuntimeError("expert memory proposal boundary is invalid")
        payload = dict(proposal)
        validate_governed_payload(payload)
        try:
            return self._append_expert_immutable_event(
                turn_id,
                event_type="expert.memory.proposed",
                summary="expert memory proposal pending review",
                immutable_kind="expert-memory-proposal-v1",
                payload=payload,
            )
        except Exception as error:
            raise _ExpertProposalCheckpointPending(
                "expert memory proposal result requires recovery"
            ) from error

    def _expert_capability_ids(self, turn_id: str) -> frozenset[str] | None:
        existing = self._payloads.get_immutable_payload(turn_id, "expert-binding-snapshot-v1")
        if existing is None:
            return None
        _, payload = existing
        tools = payload.get("tools") if isinstance(payload, Mapping) else None
        if not isinstance(tools, list) or not tools or any(
            not isinstance(item, str) or not item for item in tools
        ):
            raise AIKernelRuntimeError("expert binding snapshot tools are invalid")
        return frozenset(tools)

    def _expert_job_wait_is_active(self, turn_id: str) -> bool:
        """Keep an unresolved expert Job inert across process restart.

        `wake_enqueued` remains inert until the internal Media bridge verifies
        and delivers the governed terminal snapshot. After that snapshot is
        atomically observed, the same frozen Turn may continue into expert
        synthesis without executing a second Tool or Job.
        """
        lookup = getattr(self._state, "get_expert_job_wait", None)
        if not callable(lookup):
            return False
        wait = lookup(turn_id)
        if not isinstance(wait, Mapping):
            return False
        return wait.get("status") in {"waiting", "wake_enqueued"}

    def _record_expert_job_wait(
        self,
        turn_id: str,
        decision: Mapping[str, object],
        *,
        step_id: str,
        model_request_id: str,
    ) -> None:
        if self._expert_job_wait_is_active(turn_id):
            return
        job_ref = decision.get("job_ref")
        observed_job_revision = decision.get("observed_job_revision")
        if (
            not isinstance(job_ref, str)
            or not re.fullmatch(r"crp://jobs/[a-z][a-z0-9-]{2,127}", job_ref)
            or not isinstance(observed_job_revision, int)
            or isinstance(observed_job_revision, bool)
            or observed_job_revision < 1
        ):
            raise AIKernelRuntimeError("expert Job wait decision is invalid")
        binding = self._payloads.get_immutable_payload(
            turn_id, "expert-binding-snapshot-v1",
        )
        if binding is None:
            raise AIKernelRuntimeError("expert Job wait requires a frozen expert binding")
        binding_ref, binding_payload = binding
        binding_snapshot_id = (
            binding_payload.get("snapshot_id") if isinstance(binding_payload, Mapping) else None
        )
        if not isinstance(binding_payload, Mapping) or not isinstance(binding_snapshot_id, str) or not binding_snapshot_id:
            raise AIKernelRuntimeError("expert Job wait expert binding is invalid")
        admission = self._expert_job_admission(
            turn_id, job_ref=job_ref, observed_job_revision=observed_job_revision,
        )
        project_id = self._require_request(turn_id).get("scope", {}).get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise AIKernelRuntimeError("expert Job wait project identity is unavailable")
        snapshot = {
            "schema_version": "1.0.0",
            "turn_id": turn_id,
            "project_id": project_id,
            "canonical_job_ref": job_ref,
            "admission_job_revision": admission["admission_job_revision"],
            "observed_job_revision": observed_job_revision,
            "expert_binding_snapshot_ref": binding_ref,
            "expert_binding_snapshot_id": binding_snapshot_id,
            "source_manifest_ref": admission["source_manifest_ref"],
            "source_manifest_revision": admission["source_manifest_revision"],
            "permission_grant_ids": admission["permission_grant_ids"],
            "boundary_admission_ref": admission["boundary_admission_ref"],
            "outcome_ref": admission["outcome_ref"],
            "admission_receipt_ref": admission["admission_receipt_ref"],
            "analyze_source_correlation": admission["analyze_source_correlation"],
        }
        append_wait = getattr(self._state, "append_expert_job_wait_bundle", None)
        if not callable(append_wait) or self._atomic_store is None:
            raise AIKernelRuntimeError("durable expert Job wait store is unavailable")
        with self._event_commit(turn_id):
            event = self._new_event(
                turn_id,
                "expert.job.waiting",
                "waiting_job",
                str(decision.get("summary", "expert Job wait recorded")),
                step_id=step_id,
                model_request_id=model_request_id,
                model_call_purpose="primary",
            )
            append_wait(
                event,
                expected_sequence=int(event["sequence"]) - 1,
                immutable_kind="expert-job-wait-snapshot-v1",
                immutable_payload=snapshot,
                job_ref=job_ref,
                admission_job_revision=admission["admission_job_revision"],
                run_lease=self._run_lease_context.get(),
            )

    def _expert_job_admission(
        self,
        turn_id: str,
        *,
        job_ref: str,
        observed_job_revision: int,
    ) -> Mapping[str, object]:
        """Read the one completed, Boundary-admitted ``analyze_source`` path.

        The Planner supplies only a wake condition.  Every identity frozen for
        the wait comes from the preceding immutable Tool outcome/receipt and
        the Boundary admission already recorded by the Kernel.
        """
        events = tuple(self._events.events_after(turn_id))
        terminal_tools = [
            event for event in events
            if event.get("type") in {"tool.completed", "tool.failed", "tool.cancelled"}
        ]
        if not terminal_tools:
            raise AIKernelRuntimeError("expert Job wait requires an admitted analyze_source completion")
        completed = terminal_tools[-1]
        data = completed.get("data")
        correlation = completed.get("correlation")
        if (
            completed.get("type") != "tool.completed"
            or not isinstance(data, Mapping)
            or data.get("capability_id") != "analyze_source"
            or not isinstance(correlation, Mapping)
            or not isinstance(correlation.get("tool_call_id"), str)
            or not isinstance(correlation.get("step_id"), str)
            or not isinstance(data.get("payload_ref"), str)
            or not isinstance(data.get("receipt_ref"), str)
        ):
            raise AIKernelRuntimeError("expert Job wait requires the latest admitted analyze_source completion")
        result_payload = self._payloads.get(str(data["payload_ref"]))
        result = _admitted_analyze_source_result(result_payload)
        if result is None:
            raise AIKernelRuntimeError("expert Job admission result is unavailable")
        admission_revision = _admission_job_revision(result)
        if (
            result.get("canonical_job_ref") != job_ref
            or admission_revision is None
            or observed_job_revision < admission_revision
            or not isinstance(result.get("source_manifest_ref"), str)
            or not isinstance(result.get("source_manifest_revision"), str)
        ):
            raise AIKernelRuntimeError("expert Job wait admission identity drifted")
        call_id = str(correlation["tool_call_id"])
        requested = next(
            (
                event for event in reversed(events)
                if event.get("type") == "tool.requested"
                and _correlation_text(event, "tool_call_id") == call_id
            ),
            None,
        )
        outcome = next(
            (
                event for event in reversed(events)
                if event.get("type") == "tool.outcome.recorded"
                and _correlation_text(event, "tool_call_id") == call_id
            ),
            None,
        )
        intent_event = next(
            (
                event for event in reversed(events)
                if event.get("type") == "tool.intent.recorded"
                and _correlation_text(event, "tool_call_id") == call_id
            ),
            None,
        )
        request_data = requested.get("data") if isinstance(requested, Mapping) else None
        outcome_data = outcome.get("data") if isinstance(outcome, Mapping) else None
        intent_data = intent_event.get("data") if isinstance(intent_event, Mapping) else None
        boundary_ref = request_data.get("payload_ref") if isinstance(request_data, Mapping) else None
        outcome_ref = outcome_data.get("payload_ref") if isinstance(outcome_data, Mapping) else None
        intent_ref = intent_data.get("payload_ref") if isinstance(intent_data, Mapping) else None
        if not isinstance(outcome_ref, str) or not isinstance(intent_ref, str):
            raise AIKernelRuntimeError("expert Job wait admission evidence is unavailable")
        intent = intent_from_payload(self._payloads.get(intent_ref))
        if (
            intent.turn_id != turn_id
            or intent.invocation_id != call_id
            or intent.capability_id != "analyze_source"
        ):
            raise AIKernelRuntimeError("expert Job wait intent evidence drifted")
        outcome_payload = outcome_from_payload(self._payloads.get(outcome_ref))
        if (
            outcome_payload.turn_id != turn_id
            or outcome_payload.invocation_id != call_id
            or outcome_payload.capability_id != "analyze_source"
            or outcome_payload.status != "completed"
            or outcome_payload.effect_certainty != "confirmed_applied"
            or outcome_payload.payload_ref != data["payload_ref"]
            or outcome_payload.receipt_ref != data["receipt_ref"]
        ):
            raise AIKernelRuntimeError("expert Job wait outcome evidence drifted")
        receipt = self._payloads.get(str(data["receipt_ref"]))
        if isinstance(receipt, Mapping) and "tool_name" in receipt and (
            receipt.get("turn_id") != turn_id
            or receipt.get("tool_call_id") != call_id
            or receipt.get("tool_name") != "analyze_source"
            or receipt.get("outcome") != result
        ):
            raise AIKernelRuntimeError("expert Job wait admission receipt drifted")
        if isinstance(boundary_ref, str):
            boundary = self._payloads.get(boundary_ref)
            grants = boundary.get("matched_grant_ids") if isinstance(boundary, Mapping) else None
            if (
                not isinstance(grants, list)
                or not grants
                or any(not isinstance(grant, str) or not grant for grant in grants)
            ):
                raise AIKernelRuntimeError("expert Job wait permission grant is unavailable")
            admission_authority_ref = boundary_ref
        elif (
            isinstance(intent.authorization_facts_ref, str)
            and isinstance(intent.authorization_facts_revision, str)
            and isinstance(intent.approval_fact_ref, str)
        ):
            # Hook-enabled production dispatch has already fenced this durable
            # intent against frozen Boundary facts and an approval fact.  It
            # deliberately has no second live Boundary event to read here.
            grants = []
            admission_authority_ref = intent.authorization_facts_ref
        else:
            raise AIKernelRuntimeError("expert Job wait authorization evidence is unavailable")
        return {
            "admission_job_revision": admission_revision,
            "source_manifest_ref": result["source_manifest_ref"],
            "source_manifest_revision": result["source_manifest_revision"],
            "permission_grant_ids": list(grants),
            "boundary_admission_ref": admission_authority_ref,
            "outcome_ref": outcome_ref,
            "admission_receipt_ref": data["receipt_ref"],
            "analyze_source_correlation": dict(correlation),
        }

    def _manifest_capabilities(
        self,
        manifest: CapabilityManifest,
    ) -> tuple[CapabilityDefinition, ...]:
        definitions: list[CapabilityDefinition] = []
        for capability_id in manifest.capability_ids:
            definition = self._registry.get(capability_id)
            if definition is not None:
                definitions.append(definition)
        return tuple(definitions)

    @_serialize_event_commit
    def _append(
        self,
        turn_id: str,
        event_type: str,
        status: str,
        summary: str,
        *,
        actor: str = "kernel",
        capability_id: str | None = None,
        payload_ref: str | None = None,
        receipt_ref: str | None = None,
        evidence_refs: tuple[str, ...] = (),
        error_code: str | None = None,
        retryable: bool = False,
        step_id: str | None = None,
        tool_call_id: str | None = None,
        model_request_id: str | None = None,
        model_call_purpose: ModelCallPurpose | None = None,
    ) -> Mapping[str, object]:
        run_lease = self._run_lease_context.get()
        event = self._new_event(
            turn_id, event_type, status, summary, actor=actor,
            capability_id=capability_id, payload_ref=payload_ref,
            receipt_ref=receipt_ref, evidence_refs=evidence_refs,
            error_code=error_code, retryable=retryable, step_id=step_id,
            tool_call_id=tool_call_id, model_request_id=model_request_id,
            model_call_purpose=model_call_purpose,
        )
        return self._events.append(
            event, expected_sequence=int(event["sequence"]) - 1, run_lease=run_lease,
        )

    def _append_turn_terminal(
        self,
        turn_id: str,
        event_type: str,
        status: str,
        summary: str,
        **kwargs: object,
    ) -> Mapping[str, object]:
        """Observe SessionEnd immediately before the terminal state boundary."""

        if event_type not in {"turn.completed", "turn.failed", "turn.cancelled"}:
            raise AIKernelRuntimeError("terminal append requires a terminal Turn event")
        deferred = self._batch_terminals.get()
        if deferred is not None:
            deferred.append(((turn_id, event_type, status, summary), kwargs))
            return {}
        # SubagentStop is an audit observation.  A handler may request that a
        # stop be blocked under its own parity contract, but a child Turn has
        # already reached its authoritative terminal boundary and must still
        # converge.  A durable prior invocation suppresses replay duplicates.
        try:
            self._ensure_subagent_stop_hook(
                turn_id, event_type=event_type, terminal_status=status,
            )
        except Exception:
            pass
        try:
            if self._hook_event_enabled(turn_id, HookEvent.SESSION_END):
                self.invoke_lifecycle_hook(
                    turn_id,
                    HookEvent.SESSION_END,
                    {
                        "turn_id": turn_id,
                        "terminal_type": event_type,
                        "terminal_status": status,
                    },
                    step_id=kwargs.get("step_id") if isinstance(kwargs.get("step_id"), str) else None,
                    tool_call_id=kwargs.get("tool_call_id") if isinstance(kwargs.get("tool_call_id"), str) else None,
                    model_request_id=kwargs.get("model_request_id") if isinstance(kwargs.get("model_request_id"), str) else None,
                )
        except Exception:
            # SessionEnd is observational and audit delivery is best effort;
            # it cannot prevent the authoritative Turn from converging.
            pass
        return self._append(turn_id, event_type, status, summary, **kwargs)  # type: ignore[arg-type]

    def _ensure_subagent_start_hook(
        self,
        turn_id: str,
        request: Mapping[str, object],
    ) -> None:
        binding = _subagent_binding(request)
        if binding is None or not self._hook_event_enabled(turn_id, HookEvent.SUBAGENT_START):
            return
        if self._has_hook_invocation(turn_id, HookEvent.SUBAGENT_START):
            return
        self.invoke_lifecycle_hook(
            turn_id,
            HookEvent.SUBAGENT_START,
            _subagent_hook_identity_payload(turn_id, request, binding),
        )

    def _ensure_subagent_stop_hook(
        self,
        turn_id: str,
        *,
        event_type: str,
        terminal_status: str,
    ) -> None:
        request = self._require_request(turn_id)
        binding = _subagent_binding(request)
        if binding is None or not self._hook_event_enabled(turn_id, HookEvent.SUBAGENT_STOP):
            return
        if self._has_hook_invocation(turn_id, HookEvent.SUBAGENT_STOP):
            return
        payload = _subagent_hook_identity_payload(turn_id, request, binding)
        payload.update({
            "terminal_type": event_type,
            "terminal_status": terminal_status,
        })
        self.invoke_lifecycle_hook(turn_id, HookEvent.SUBAGENT_STOP, payload)

    def _has_hook_invocation(self, turn_id: str, event: HookEvent) -> bool:
        return any(
            item.get("type") == "hook.invoked"
            and self._hook_event_for(item) is event
            for item in self._events.events_after(turn_id)
        )

    def _new_event(
        self, turn_id: str, event_type: str, status: str, summary: str, *,
        actor: str = "kernel", capability_id: str | None = None,
        payload_ref: str | None = None, receipt_ref: str | None = None,
        evidence_refs: tuple[str, ...] = (), error_code: str | None = None,
        retryable: bool = False, step_id: str | None = None,
        tool_call_id: str | None = None, model_request_id: str | None = None,
        model_call_purpose: ModelCallPurpose | None = None,
    ) -> Mapping[str, object]:
        run_lease = self._run_lease_context.get()
        if run_lease is not None:
            if run_lease.turn_id != turn_id or self._state.assert_active_run_lease(run_lease) is None:
                raise RunLeaseRevoked()
        request = self._require_request(turn_id)
        sequence = len(tuple(self._events.events_after(turn_id))) + 1
        data: dict[str, object] = {"status": status, "summary": summary, "capability_id": capability_id, "payload_ref": payload_ref, "receipt_ref": receipt_ref, "evidence_refs": list(evidence_refs), "error_code": error_code, "retryable": retryable}
        if model_call_purpose is not None:
            data["model_call_purpose"] = validate_model_call_purpose(model_call_purpose)
        return {"schema_version": "1.0.0", "event_id": f"event-{uuid4().hex}", "turn_id": turn_id, "session_id": request["session_id"], "sequence": sequence, "type": event_type, "actor": actor, "correlation": {"step_id": step_id, "tool_call_id": tool_call_id, "model_request_id": model_request_id, "operation_id": request["operation_id"]}, "data": data, "occurred_at": datetime.now(timezone.utc).isoformat()}

    def _bind_run_lease(self, turn_id: str, run_lease: RunLeaseToken | None) -> Token[RunLeaseToken | None]:
        if run_lease is None and self._state.get_run_lease(turn_id) is not None:
            raise RunLeaseRevoked()
        if run_lease is not None:
            validate_run_lease_token(run_lease)
            if run_lease.turn_id != turn_id or self._state.assert_active_run_lease(run_lease) is None:
                raise RunLeaseRevoked()
        return self._run_lease_context.set(run_lease)

    def _resume_incomplete_tool(
        self,
        turn_id: str,
    ) -> tuple[bool, TurnReceipt | None]:
        if self._pending_tool_batch(turn_id) is not None:
            paused = self._resume_tool_batch(turn_id)
            return True, self._receipt(turn_id) if paused else None
        if self._recover_missing_post_tool_use_hook(turn_id):
            return True, self._receipt(turn_id)
        recorded_outcome = self._converge_recorded_tool_outcome(turn_id)
        if recorded_outcome is not None:
            return True, (
                None if recorded_outcome == "completed" else self._receipt(turn_id)
            )
        incomplete = self._incomplete_tool_intent(turn_id)
        if incomplete is None:
            return False, None
        intent, intent_ref = incomplete
        return self._resume_recorded_tool_intent(intent, intent_ref)

    def _resume_recorded_tool_intent(self, intent, intent_ref):
        turn_id = intent.turn_id
        attempts = self._tool_attempt_count(turn_id, intent.invocation_id)
        # A manual MCP continuation is a separate at-most-once effect. The
        # initial, completed read RPC cannot establish that child's outcome.
        child_id = f'{intent.invocation_id}.continue.2'
        child = None
        if self._effect_runner is not None and (intent.tool_contract or {}).get('source') == 'mcp':
            try:
                child = self._effect_runner.log.get(child_id)
            except KeyError:
                pass
        if (intent.tool_contract or {}).get('source') == 'mcp' and (child is not None or attempts > 1):
            if child is not None and child.state is EffectState.INFLIGHT:
                return True, self._receipt(turn_id)
            return True, self._record_unknown_tool_outcome(
                intent, attempt=max(attempts, 2), error_code='mcp.continuation_outcome_unknown',
            )
        effect = self._ensure_tool_effect(intent, intent_ref) if self._effect_runner is not None else None
        if effect is not None and self._external_tool_outcome_projector is not None:
            if self._project_external_tool_completion(intent, effect):
                current = self._receipt(turn_id)
                return True, current if current.status != "running" else None
        resolved = self._registry.resolve(intent.capability_id)
        if resolved is None or not _definition_matches_intent(resolved[0], intent):
            return True, self._record_unknown_tool_outcome(
                intent,
                attempt=self._tool_attempt_count(turn_id, intent.invocation_id),
                error_code="ai.tool_definition_drift",
            )
        if self._effect_runner is not None:
            assert effect is not None
            if effect.state is EffectState.INFLIGHT:
                return True, self._receipt(turn_id)
            if effect.state is EffectState.UNKNOWN:
                return True, self._record_unknown_tool_outcome(
                    intent,
                    attempt=max(attempts, effect.attempt),
                    error_code="ai.tool_outcome_unknown",
                )
            if effect.state is EffectState.PLANNED:
                if attempts >= intent.max_attempts:
                    return True, self._record_failed_tool_outcome(
                        intent,
                        attempt=attempts,
                        error_code="ai.tool_retry_exhausted",
                        effect_certainty="confirmed_none",
                    )
                self._execute_tool_intent(
                    intent,
                    intent_ref,
                    resolved[1],
                    attempt=attempts + 1,
                )
                return True, None
        if attempts == 0:
            self._execute_tool_intent(
                intent,
                intent_ref,
                resolved[1],
                attempt=1,
            )
            return True, None
        retry_failure = self._latest_retryable_attempt_failure(intent)
        if retry_failure is not None:
            return True, self._record_failed_tool_outcome(
                intent,
                attempt=attempts,
                error_code="ai.tool_retry_exhausted",
                effect_certainty="confirmed_none",
            )
        if resolved[0].mode == "read":
            return True, self._record_failed_tool_outcome(
                intent,
                attempt=attempts,
                error_code="ai.tool_interrupted",
                effect_certainty="confirmed_none",
            )
        recovery = getattr(resolved[1], "recover_completed_invocation", None)
        if callable(recovery):
            recovered = self._project_recovered_tool_completion(
                intent, intent_ref, recovery
            )
            if recovered:
                current = self._receipt(turn_id)
                return True, current if current.status != "running" else None
        return True, self._record_unknown_tool_outcome(
            intent,
            attempt=max(attempts, 1),
            error_code="ai.tool_outcome_unknown",
        )

    def _project_external_tool_completion(
        self, intent: ToolInvocationIntent, effect: Effect,
    ) -> bool:
        """Commit verified external facts without resolving or invoking a Provider."""

        projector = self._external_tool_outcome_projector
        if projector is None or effect.state not in {EffectState.INFLIGHT, EffectState.UNKNOWN}:
            return False
        try:
            projected = projector(intent, effect)
        except Exception:
            return False
        if projected is None:
            return False
        result = projected.get("result")
        operation_receipt = projected.get("operation_receipt")
        evidence_refs = _refs(projected.get("evidence_refs"))
        if not isinstance(result, Mapping) or not isinstance(operation_receipt, Mapping):
            raise AIKernelRuntimeError("external Tool outcome projection is invalid")
        outcome = ToolInvocationOutcome(
            invocation_id=intent.invocation_id,
            turn_id=intent.turn_id,
            capability_id=intent.capability_id,
            attempt=max(effect.attempt, 1),
            status="completed",
            effect_certainty="confirmed_applied",
            payload_ref=None,
            receipt_ref=None,
            evidence_refs=evidence_refs,
            error_code=None,
            retryable=False,
        )
        try:
            outcome_ref, receipt_ref, result_ref = self._append_tool_outcome_bundle(
                outcome,
                intent.turn_id,
                summary="external tool outcome projected from immutable facts",
                capability_id=intent.capability_id,
                evidence_refs=evidence_refs,
                step_id=intent.step_id,
                tool_call_id=intent.invocation_id,
                operation_receipt_payload=operation_receipt,
                result_payload=dict(result),
            )
        except TurnEventConflict:
            if any(
                event.get("type") == "tool.outcome.recorded"
                and _correlation_text(event, "tool_call_id") == intent.invocation_id
                for event in self._events.events_after(intent.turn_id)
            ):
                return True
            raise
        if result_ref is None:
            raise AIKernelRuntimeError("external Tool result projection is unavailable")
        if self._effect_runner is not None and effect.state is EffectState.INFLIGHT:
            self._effect_runner.settle_ok(
                effect,
                receipt_ref=outcome_ref,
                receipt_kind="tool-outcome-receipt",
                now=int(datetime.now(timezone.utc).timestamp()),
            )
        self._append(
            intent.turn_id,
            "tool.completed",
            "running",
            "external tool completion recovered",
            capability_id=intent.capability_id,
            payload_ref=result_ref,
            receipt_ref=receipt_ref,
            evidence_refs=evidence_refs,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        post_should_stop = self._run_post_tool_use_hook(
            turn_id=intent.turn_id,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
            capability_id=intent.capability_id,
            payload_ref=result_ref,
        )
        if post_should_stop:
            self._append_turn_terminal(
                intent.turn_id, "turn.cancelled", "cancelled",
                "PostToolUse hook stopped the Turn after tool execution",
                error_code="ai.hook_post_tool_stopped",
                step_id=intent.step_id,
                tool_call_id=intent.invocation_id,
            )
        return True

    def _recover_missing_post_tool_use_hook(self, turn_id: str) -> bool:
        if not self._hook_event_enabled(turn_id, HookEvent.POST_TOOL_USE):
            return False
        events = tuple(self._events.events_after(turn_id))
        stopped = any(
            event.get("type") == "turn.cancelled"
            and isinstance(event.get("data"), Mapping)
            and event["data"].get("error_code") == "ai.hook_post_tool_stopped"
            for event in events
        )
        for event in events:
            if event.get("type") != "tool.completed":
                continue
            tool_call_id = _correlation_text(event, "tool_call_id")
            step_id = _correlation_text(event, "step_id")
            data = event.get("data")
            capability_id = data.get("capability_id") if isinstance(data, Mapping) else None
            payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
            if not all(isinstance(value, str) for value in (tool_call_id, step_id, capability_id)):
                continue
            should_stop = self._run_post_tool_use_hook(
                turn_id=turn_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                capability_id=capability_id,
                payload_ref=payload_ref if isinstance(payload_ref, str) else None,
            )
            if should_stop and not stopped:
                self._append_turn_terminal(
                    turn_id, "turn.cancelled", "cancelled",
                    "PostToolUse hook stopped the Turn after tool execution",
                    error_code="ai.hook_post_tool_stopped",
                    step_id=step_id,
                    tool_call_id=tool_call_id,
                )
                return True
        return stopped

    def _project_recovered_tool_completion(
        self,
        intent: ToolInvocationIntent,
        intent_ref: str,
        recover: Callable[[Mapping[str, object]], Mapping[str, object] | None],
    ) -> bool:
        """Project a provider-owned receipt without creating another Tool attempt."""
        try:
            if intent.authorization_facts_ref is not None and (
                self._frozen_authorization is None
                or not self._frozen_authorization.prepare_intent(intent)
            ):
                return False
            turn_request = self._require_request(intent.turn_id)
            provider_request = {
                "turn_id": intent.turn_id,
                "operation_id": intent.operation_id,
                "intent_ref": None,
                "capability_id": intent.capability_id,
                "capability_version": intent.capability_version,
                "authorization_facts_ref": intent.authorization_facts_ref,
                "authorization_facts_revision": intent.authorization_facts_revision,
                "approval_fact_ref": intent.approval_fact_ref,
                "scope": turn_request["scope"],
                "privacy": turn_request["privacy"],
                "arguments": dict(intent.arguments),
                "tool_call_id": intent.invocation_id,
                "attempt": 1,
                "idempotency_key": intent.idempotency_key,
                "timeout_ms": intent.timeout_ms,
                "execution_mode": intent.execution_mode,
                "resource_locks": list(intent.resource_locks),
                "tool_contract": dict(intent.tool_contract) if intent.tool_contract is not None else None,
            }
            observer = _RuntimeToolDispatchObserver(
                self, intent, intent_ref, 1, turn_request
            )
            dispatch_recovery = getattr(self._dispatcher, "dispatch_recovery", None)
            if not callable(dispatch_recovery):
                return False
            result = dispatch_recovery(
                _RecoveryProvider(recover),
                ToolDispatchRequest(
                    provider_request=provider_request,
                    execution_mode=intent.execution_mode,
                    resource_locks=intent.resource_locks,
                    invocation_id=intent.invocation_id,
                    attempt=1,
                    timeout_ms=intent.timeout_ms,
                ),
                observer,
            )
        except Exception:
            return False
        if not isinstance(result, Mapping):
            return False
        values = dict(result)
        receipt_ref = _optional_ref(values.get("receipt_ref"))
        operation_receipt_payload = values.get("operation_receipt")
        if receipt_ref is not None and operation_receipt_payload is not None:
            return False
        if receipt_ref is None and operation_receipt_payload is None:
            return False
        evidence_refs = _refs(values.get("evidence_refs"))
        outcome = ToolInvocationOutcome(
            invocation_id=intent.invocation_id,
            turn_id=intent.turn_id,
            capability_id=intent.capability_id,
            attempt=1,
            status="completed",
            effect_certainty="confirmed_applied",
            payload_ref=None,
            receipt_ref=receipt_ref,
            evidence_refs=evidence_refs,
            error_code=None,
            retryable=False,
        )
        _outcome_ref, committed_receipt_ref, _result_ref = self._append_tool_outcome_bundle(
            outcome,
            intent.turn_id,
            summary="recovered tool outcome recorded",
            capability_id=intent.capability_id,
            evidence_refs=evidence_refs,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
            operation_receipt_payload=operation_receipt_payload,
        )
        self._append(
            intent.turn_id,
            "tool.completed",
            "running",
            str(values.get("summary", "tool completion recovered")),
            capability_id=intent.capability_id,
            receipt_ref=committed_receipt_ref,
            evidence_refs=evidence_refs,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        post_should_stop = self._run_post_tool_use_hook(
            turn_id=intent.turn_id,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
            capability_id=intent.capability_id,
            payload_ref=None,
        )
        if post_should_stop:
            self._append_turn_terminal(
                intent.turn_id, "turn.cancelled", "cancelled",
                "PostToolUse hook stopped the Turn after tool execution",
                error_code="ai.hook_post_tool_stopped",
                step_id=intent.step_id,
                tool_call_id=intent.invocation_id,
            )
            return True
        return True

    def _converge_recorded_tool_outcome(self, turn_id: str) -> str | None:
        """Close a durable tool outcome without re-dispatching its provider.

        A process can stop after writing ``tool.outcome.recorded`` but before
        its companion tool and Turn terminal events. The outcome payload is
        already the authoritative result, so recovery only projects its missing
        terminal events; it must never reconstruct or invoke the tool intent.
        """
        events = tuple(self._events.events_after(turn_id))
        terminal_calls = {
            _correlation_text(event, "tool_call_id")
            for event in events
            if event.get("type") in {"tool.completed", "tool.failed", "tool.cancelled"}
        }
        for event in events:
            if event.get("type") != "tool.outcome.recorded":
                continue
            tool_call_id = _correlation_text(event, "tool_call_id")
            if tool_call_id is None or tool_call_id in terminal_calls:
                continue
            step_id = _correlation_text(event, "step_id")
            data = event.get("data")
            payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
            capability_id = data.get("capability_id") if isinstance(data, Mapping) else None
            if not isinstance(step_id, str) or not isinstance(payload_ref, str) or not isinstance(capability_id, str):
                raise AIKernelRuntimeError("durable tool outcome correlation is unavailable")
            outcome = outcome_from_payload(self._payloads.get(payload_ref))
            if (
                outcome.turn_id != turn_id
                or outcome.invocation_id != tool_call_id
                or outcome.capability_id != capability_id
            ):
                raise AIKernelRuntimeError("durable tool outcome identity drifted")
            event_type, status, summary = _tool_outcome_terminal(outcome.status)
            self._append(
                turn_id,
                event_type,
                "running",
                summary,
                capability_id=outcome.capability_id,
                payload_ref=outcome.payload_ref,
                receipt_ref=outcome.receipt_ref,
                evidence_refs=outcome.evidence_refs,
                error_code=outcome.error_code,
                step_id=step_id,
                tool_call_id=tool_call_id,
            )
            if outcome.status == "completed":
                post_should_stop = self._run_post_tool_use_hook(
                    turn_id=turn_id,
                    step_id=step_id,
                    tool_call_id=tool_call_id,
                    capability_id=outcome.capability_id,
                    payload_ref=outcome.payload_ref,
                )
                if post_should_stop:
                    self._append_turn_terminal(
                        turn_id, "turn.cancelled", "cancelled",
                        "PostToolUse hook stopped the Turn after tool execution",
                        error_code="ai.hook_post_tool_stopped",
                        step_id=step_id,
                        tool_call_id=tool_call_id,
                    )
                    return "cancelled"
                return "completed"
            turn_event, turn_status, turn_summary = _tool_outcome_turn_terminal(outcome.status)
            self._append_turn_terminal(
                turn_id,
                turn_event,
                turn_status,
                turn_summary,
                error_code=outcome.error_code,
            )
            return outcome.status
        return None

    def _incomplete_tool_intent(
        self,
        turn_id: str,
    ) -> tuple[ToolInvocationIntent, str] | None:
        events = tuple(self._events.events_after(turn_id))
        completed_calls = {
            str(event.get("correlation", {}).get("tool_call_id"))
            for event in events
            if event.get("type") == "tool.outcome.recorded"
            and isinstance(event.get("correlation"), Mapping)
        }
        for event in reversed(events):
            if event.get("type") != "tool.intent.recorded":
                continue
            correlation = event.get("correlation")
            tool_call_id = (
                correlation.get("tool_call_id")
                if isinstance(correlation, Mapping)
                else None
            )
            if not isinstance(tool_call_id, str) or tool_call_id in completed_calls:
                continue
            data = event.get("data")
            intent_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
            if not isinstance(intent_ref, str):
                raise AIKernelRuntimeError("tool invocation intent ref is unavailable")
            intent = intent_from_payload(self._payloads.get(intent_ref))
            if intent.turn_id != turn_id or intent.invocation_id != tool_call_id:
                raise AIKernelRuntimeError("tool invocation intent identity drifted")
            return intent, intent_ref
        return None

    def _tool_attempt_count(self, turn_id: str, tool_call_id: str) -> int:
        return sum(
            1
            for event in self._events.events_after(turn_id)
            if event.get("type") == "tool.started"
            and isinstance(event.get("correlation"), Mapping)
            and event["correlation"].get("tool_call_id") == tool_call_id
        )

    def _record_unknown_tool_outcome(
        self,
        intent: ToolInvocationIntent,
        *,
        attempt: int,
        error_code: str,
    ) -> TurnReceipt:
        outcome = ToolInvocationOutcome(
            invocation_id=intent.invocation_id,
            turn_id=intent.turn_id,
            capability_id=intent.capability_id,
            attempt=max(attempt, 1),
            status="unknown_effect",
            effect_certainty="unknown",
            payload_ref=None,
            receipt_ref=None,
            evidence_refs=(),
            error_code=error_code,
            retryable=False,
        )
        self._append_tool_outcome_bundle(
            outcome,
            intent.turn_id,
            summary="interrupted tool outcome recorded",
            capability_id=intent.capability_id,
            error_code=error_code,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        self._append(
            intent.turn_id,
            "tool.failed",
            "running",
            "interrupted tool was not automatically repeated",
            capability_id=intent.capability_id,
            error_code=error_code,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        self._append_turn_terminal(
            intent.turn_id,
            "turn.failed",
            "failed",
            "tool execution outcome requires review",
            error_code=error_code,
        )
        return self._receipt(intent.turn_id)

    def _record_failed_tool_outcome(
        self,
        intent: ToolInvocationIntent,
        *,
        attempt: int,
        error_code: str,
        effect_certainty: EffectCertainty,
    ) -> TurnReceipt:
        outcome = ToolInvocationOutcome(
            invocation_id=intent.invocation_id,
            turn_id=intent.turn_id,
            capability_id=intent.capability_id,
            attempt=max(attempt, 1),
            status="failed",
            effect_certainty=effect_certainty,
            payload_ref=None,
            receipt_ref=None,
            evidence_refs=(),
            error_code=error_code,
            retryable=False,
        )
        self._append_tool_outcome_bundle(
            outcome,
            intent.turn_id,
            summary="tool failure outcome recorded",
            capability_id=intent.capability_id,
            error_code=error_code,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        self._append(
            intent.turn_id,
            "tool.failed",
            "running",
            "tool execution failed without automatic repetition",
            capability_id=intent.capability_id,
            error_code=error_code,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        self._append_turn_terminal(
            intent.turn_id,
            "turn.failed",
            "failed",
            "tool execution failed",
            error_code=error_code,
        )
        return self._receipt(intent.turn_id)

    def _record_retryable_attempt_failure(
        self,
        intent: ToolInvocationIntent,
        *,
        attempt: int,
        error_code: str,
        backoff_ms: int,
    ) -> None:
        failure = ToolAttemptFailure(
            invocation_id=intent.invocation_id,
            turn_id=intent.turn_id,
            capability_id=intent.capability_id,
            attempt=attempt,
            error_code=error_code,
            effect_certainty="confirmed_none",
            backoff_ms=backoff_ms,
        )
        failure_ref = self._payloads.put(
            intent.turn_id,
            "tool-attempt-failure",
            attempt_failure_to_payload(failure),
        )
        self._append(
            intent.turn_id,
            "tool.attempt.failed",
            "running",
            f"tool attempt {attempt} failed with confirmed no effect",
            capability_id=intent.capability_id,
            payload_ref=failure_ref,
            error_code=error_code,
            retryable=True,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )

    def _latest_retryable_attempt_failure(
        self,
        intent: ToolInvocationIntent,
    ) -> ToolAttemptFailure | None:
        events = tuple(self._events.events_after(intent.turn_id))
        latest_started = max(
            (
                int(event["sequence"])
                for event in events
                if event.get("type") == "tool.started"
                and isinstance(event.get("correlation"), Mapping)
                and event["correlation"].get("tool_call_id") == intent.invocation_id
            ),
            default=0,
        )
        for event in reversed(events):
            if event.get("type") != "tool.attempt.failed":
                continue
            correlation = event.get("correlation")
            if not isinstance(correlation, Mapping) or correlation.get("tool_call_id") != intent.invocation_id:
                continue
            if int(event["sequence"]) < latest_started:
                return None
            data = event.get("data")
            payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
            retryable = data.get("retryable") if isinstance(data, Mapping) else None
            if not isinstance(payload_ref, str):
                return None
            failure = attempt_failure_from_payload(self._payloads.get(payload_ref))
            if (
                retryable is True
                and failure.invocation_id == intent.invocation_id
                and failure.turn_id == intent.turn_id
                and failure.capability_id == intent.capability_id
                and failure.attempt == self._tool_attempt_count(intent.turn_id, intent.invocation_id)
                and failure.error_code in intent.retryable_error_codes
            ):
                return failure
            return None
        return None

    def _record_cancelled_tool_outcome(
        self,
        intent: ToolInvocationIntent,
        *,
        error_code: str,
        timed_out: bool = False,
    ) -> None:
        outcome = ToolInvocationOutcome(
            invocation_id=intent.invocation_id,
            turn_id=intent.turn_id,
            capability_id=intent.capability_id,
            attempt=max(self._tool_attempt_count(intent.turn_id, intent.invocation_id), 1),
            status="timed_out" if timed_out else "cancelled",
            effect_certainty="confirmed_none",
            payload_ref=None,
            receipt_ref=None,
            evidence_refs=(),
            error_code=error_code,
            retryable=False,
        )
        self._append_tool_outcome_bundle(
            outcome,
            intent.turn_id,
            summary="tool cancellation outcome recorded" if not timed_out else "tool deadline outcome recorded",
            capability_id=intent.capability_id,
            error_code=error_code,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        self._append(
            intent.turn_id,
            "tool.cancelled" if not timed_out else "tool.failed",
            "running",
            "tool cancelled before a side effect was confirmed" if not timed_out else "tool deadline exceeded before a side effect was confirmed",
            capability_id=intent.capability_id,
            error_code=error_code,
            step_id=intent.step_id,
            tool_call_id=intent.invocation_id,
        )
        self._append_turn_terminal(
            intent.turn_id,
            "turn.failed" if timed_out else "turn.cancelled",
            "failed" if timed_out else "cancelled",
            "tool deadline exceeded" if timed_out else "tool cancellation completed",
            error_code=error_code,
        )
    @_serialize_event_commit
    def _append_tool_outcome_bundle(
        self,
        outcome: ToolInvocationOutcome,
        turn_id: str,
        *,
        summary: str,
        capability_id: str,
        error_code: str | None = None,
        evidence_refs: tuple[str, ...] = (),
        step_id: str | None = None,
        tool_call_id: str | None = None,
        operation_receipt_payload: object | None = None,
        result_payload: object | None = None,
    ) -> tuple[str, str | None, str | None]:
        """Persist the authoritative Tool outcome before recovery can observe it.

        Durable Session stores commit the outcome, optional operation receipt and
        event as one unit. In-memory fixtures retain their legacy independent
        ports so callers can still exercise non-durable runtime behavior.
        """
        event = self._new_event(
            turn_id, "tool.outcome.recorded", "running", summary,
            capability_id=capability_id,
            receipt_ref=outcome.receipt_ref,
            evidence_refs=evidence_refs,
            error_code=error_code,
            step_id=step_id,
            tool_call_id=tool_call_id,
        )
        payload = outcome_to_payload(outcome)
        if self._atomic_store is not None:
            committed = self._atomic_store.append_tool_outcome_bundle(
                event,
                expected_sequence=int(event["sequence"]) - 1,
                outcome_kind="tool-invocation-outcome",
                outcome_payload=payload,
                result_kind="tool-result" if result_payload is not None else None,
                result_payload=result_payload,
                operation_receipt_kind=(
                    "tool-operation-receipt" if operation_receipt_payload is not None else None
                ),
                operation_receipt_payload=operation_receipt_payload,
                run_lease=self._run_lease_context.get(),
            )
            return (
                committed.outcome_payload_ref,
                committed.operation_receipt_ref or outcome.receipt_ref,
                committed.result_payload_ref or outcome.payload_ref,
            )
        result_ref = outcome.payload_ref
        if result_payload is not None:
            result_ref = self._payloads.put(turn_id, "tool-result", result_payload)
            payload["payload_ref"] = result_ref
        outcome_ref = self._payloads.put(turn_id, "tool-invocation-outcome", payload)
        self._append(
            turn_id, "tool.outcome.recorded", "running", summary,
            capability_id=capability_id,
            payload_ref=outcome_ref,
            receipt_ref=outcome.receipt_ref,
            evidence_refs=evidence_refs,
            error_code=error_code,
            step_id=step_id,
            tool_call_id=tool_call_id,
        )
        return outcome_ref, outcome.receipt_ref, result_ref

    def _turn_cancel_requested(self, turn_id: str) -> bool:
        events = tuple(self._events.events_after(turn_id))
        latest_request = max(
            (
                int(event["sequence"])
                for event in events
                if event.get("type") == "turn.cancel.requested"
            ),
            default=0,
        )
        latest_outcome = max(
            (
                int(event["sequence"])
                for event in events
                if event.get("type") == "tool.outcome.recorded"
            ),
            default=0,
        )
        return latest_request > 0 and latest_outcome > latest_request

    def _batch_cancel_requested(self, turn_id: str) -> bool:
        # A paused batch has no first outcome yet. Its durable cancellation
        # must stop unstarted members without waiting for that projection.
        return any(event.get('type') == 'turn.cancel.requested'
                   for event in self._events.events_after(turn_id))

    def _receipt(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt:
        events = tuple(self._events.events_after(turn_id))
        from .turn_receipt_projection import receipt_from_events
        return receipt_from_events(
            events, turn_id=turn_id, request=self._require_request(turn_id), replayed=replayed,
        )

    def _remember_action(self, action: Mapping[str, object], turn_id: str) -> TurnReceipt:
        receipt = self._receipt(turn_id)
        self._state.save_action(action, receipt)
        return receipt

    def _require_request(self, turn_id: str) -> Mapping[str, object]:
        request = self._state.get_request(turn_id)
        if request is None:
            raise AIKernelRuntimeError("turn request was not found")
        return request

    def _pending_decision(self, turn_id: str, approval_event: Mapping[str, object]) -> Mapping[str, object] | None:
        pending = self._state.get_pending(turn_id)
        if pending is not None:
            return pending
        data = approval_event.get("data")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if isinstance(payload_ref, str):
            recovered = self._payloads.get(payload_ref)
            return dict(recovered) if isinstance(recovered, Mapping) else None
        return None

    def _recover_applied_action(self, action: Mapping[str, object], events: tuple[Mapping[str, object], ...]) -> TurnReceipt | None:
        for event in reversed(events):
            if event.get("type") not in {"approval.resolved", "turn.cancel.requested", "turn.cancelled"}:
                continue
            data = event.get("data")
            payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
            if not isinstance(payload_ref, str):
                continue
            stored = self._payloads.get(payload_ref)
            if not isinstance(stored, Mapping) or dict(stored) != dict(action):
                continue
            latest_type = events[-1].get("type")
            if latest_type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                receipt = self._receipt(str(action["turn_id"]))
                self._state.save_action(action, receipt)
                return TurnReceipt(receipt.turn_id, receipt.session_id, receipt.operation_id, receipt.status, receipt.current_sequence, True)
            if action.get('type') == 'mcp_reject' and self._pending_tool_batch(str(action['turn_id'])) is not None:
                self._resume_tool_batch(str(action['turn_id']))
                receipt = self._receipt(str(action['turn_id']))
                self._state.save_action(action, receipt)
                return receipt
            if event.get("type") == "turn.cancel.requested":
                incomplete = self._incomplete_tool_intent(str(action["turn_id"]))
                if incomplete is not None:
                    self._dispatcher.request_cancel(incomplete[0].invocation_id)
                receipt = self._receipt(str(action["turn_id"]), replayed=True)
                return receipt
            pending = self._state.get_pending(str(action["turn_id"]))
            if (pending is not None and action.get('type') == 'approve'
                and '_tool_batch_step_id' in pending):
                self._save_batch_approval(str(action['turn_id']),
                    self._restore_frozen_approval(str(action['turn_id']), pending, events))
                self._state.clear_pending(str(action['turn_id']))
            handled, terminal = self._resume_incomplete_tool(str(action["turn_id"]))
            if terminal is not None:
                self._state.clear_pending(str(action["turn_id"]))
                self._state.save_action(action, terminal)
                return terminal
            if not handled:
                if pending is None:
                    raise AIKernelRuntimeError("approved capability recovery payload is unavailable")
                if self._frozen_authorization is not None:
                    self._frozen_authorization.load_current(
                        turn_id=str(action["turn_id"]),
                    )
                    pending = self._restore_frozen_approval(
                        str(action["turn_id"]), pending, events,
                    )
                self._invoke_tool(str(action["turn_id"]), pending)
            self._state.clear_pending(str(action["turn_id"]))
            receipt = self._run(str(action["turn_id"]))
            self._state.save_action(action, receipt)
            return receipt
        return None


class _RuntimeToolDispatchObserver:
    def __init__(
        self,
        runtime: SynchronousAIRuntime,
        intent: ToolInvocationIntent,
        intent_ref: str,
        attempt: int,
        request: Mapping[str, object],
    ) -> None:
        self._runtime = runtime
        self._intent = intent
        self._intent_ref = intent_ref
        self._attempt = attempt
        self._request = request

    def nested_model_handle_factory(
        self,
        *,
        invocation_key: str = "default",
        purpose: ModelCallPurpose = "primary",
    ) -> _NestedModelCallHandle:
        # Dispatcher owns uniqueness. Purpose is immutable governance data,
        # rather than arbitrary provider metadata.
        del invocation_key
        purpose = validate_model_call_purpose(purpose)
        model_request_id = f"model-request-{uuid4().hex}"
        self._runtime._append(
            self._intent.turn_id,
            "model.requested",
            "running",
            "nested model call requested",
            capability_id=self._intent.capability_id,
            step_id=self._intent.step_id,
            tool_call_id=self._intent.invocation_id,
            model_request_id=model_request_id,
            model_call_purpose=purpose,
        )
        control = _PlannerExecutionContext(
            turn_id=self._intent.turn_id,
            step_id=self._intent.step_id,
            model_request_id=model_request_id,
            timeout_ms=max(1, self._intent.timeout_ms),
            purpose=purpose,
            _route_recorder=lambda snapshot_ref: self._runtime._append(
                self._intent.turn_id,
                "model.routed",
                "running",
                "frozen nested model route verified",
                capability_id=self._intent.capability_id,
                payload_ref=snapshot_ref,
                step_id=self._intent.step_id,
                tool_call_id=self._intent.invocation_id,
                model_request_id=model_request_id,
                model_call_purpose=purpose,
            ),
            _attempt_dispatch_recorder=lambda payload: self._runtime._store_model_attempt_dispatch(
                self._intent.turn_id,
                payload,
                step_id=self._intent.step_id,
                tool_call_id=self._intent.invocation_id,
                capability_id=self._intent.capability_id,
            ),
            _attempt_terminal_recorder=lambda payload, dispatch_ref: self._runtime._store_model_attempt_terminal(
                self._intent.turn_id,
                payload,
                dispatch_ref=dispatch_ref,
                step_id=self._intent.step_id,
                tool_call_id=self._intent.invocation_id,
                capability_id=self._intent.capability_id,
            ),
        )
        return _NestedModelCallHandle(
            runtime=self._runtime,
            turn_id=self._intent.turn_id,
            step_id=self._intent.step_id,
            tool_call_id=self._intent.invocation_id,
            control=control,
        )

    def claimed(self) -> None:
        self._runtime._append(
            self._intent.turn_id,
            "tool.dispatch.claimed",
            "running",
            f"tool dispatch attempt {self._attempt} claimed",
            capability_id=self._intent.capability_id,
            payload_ref=self._intent_ref,
            step_id=self._intent.step_id,
            tool_call_id=self._intent.invocation_id,
        )

    def started(self) -> None:
        self._runtime._append(
            self._intent.turn_id,
            "tool.started",
            "running",
            f"tool attempt {self._attempt} started",
            capability_id=self._intent.capability_id,
            payload_ref=self._intent_ref,
            step_id=self._intent.step_id,
            tool_call_id=self._intent.invocation_id,
        )

    @contextmanager
    def fence(self):
        run_lease = self._runtime._run_lease_context.get()
        if run_lease is not None and self._runtime._state.assert_active_run_lease(run_lease) is None:
            raise RunLeaseRevoked()
        if self._intent.authorization_facts_ref is not None:
            authority = self._runtime._frozen_authorization
            if authority is None or not authority.fence_intent(self._intent):
                raise ToolDispatchFailure(
                    "frozen_authorization_denied",
                    provider_started=False,
                    effect_certainty="confirmed_none",
                )
            yield
            return
        boundary = self._runtime._execution_boundary
        dispatch_fence = getattr(boundary, "dispatch_fence", None)
        if callable(dispatch_fence):
            with dispatch_fence(self._request):
                yield
            return
        yield


def _optional_ref(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith("crp://") else None


def _subagent_binding(request: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the already-validated child binding without widening authority."""
    binding = request.get("agent_binding")
    if not isinstance(binding, Mapping) or binding.get("role") != "subagent":
        return None
    return binding


def _subagent_hook_identity_payload(
    turn_id: str,
    request: Mapping[str, object],
    binding: Mapping[str, object],
) -> dict[str, object]:
    """Expose only stable lifecycle identity to local child-Agent Hooks."""
    return {
        "turn_id": turn_id,
        "session_id": request["session_id"],
        "agent_run_id": binding["run_id"],
        "parent_run_id": binding["parent_run_id"],
        "profile_id": binding["profile_id"],
        "profile_revision": binding["profile_revision"],
        "depth": binding["depth"],
        "spawn_operation_id": binding["spawn_operation_id"],
    }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AIKernelRuntimeError(f"{label} must be an object")
    return value


def _exact_capability_request(request: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the already-validated direct execution request, if supplied."""

    value = request.get("capability_request")
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("mode") != "execute_exact_v1":
        raise AIKernelRuntimeError("exact capability request is invalid")
    capability_id = value.get("capability_id")
    arguments = value.get("arguments")
    if not isinstance(capability_id, str) or not capability_id or not isinstance(arguments, Mapping):
        raise AIKernelRuntimeError("exact capability request is invalid")
    return value


def _refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.startswith("crp://"))


def _correlation_text(event: Mapping[str, object], field: str) -> str | None:
    correlation = event.get("correlation")
    value = correlation.get(field) if isinstance(correlation, Mapping) else None
    return value if isinstance(value, str) and value else None


def _admitted_analyze_source_result(value: object) -> Mapping[str, object] | None:
    """Read only the two persisted result shapes produced by Tool Runtime.

    Current durable Tool Runtime stores the capability's ``result`` directly.
    Older records retain the outer result envelope.  Both must still carry the
    explicit admission marker; a generic planner payload is never a Job
    admission merely because it contains a reference-like field.
    """
    if not isinstance(value, Mapping):
        return None
    nested = value.get("result")
    result = nested if isinstance(nested, Mapping) else value
    if result.get("status") != "admitted":
        return None
    return result


def _admission_job_revision(result: Mapping[str, object]) -> int | None:
    """Accept the current ``job_revision`` and its explicit legacy alias."""
    revision = result.get("job_revision")
    legacy = result.get("admission_job_revision")
    if revision is None:
        revision = legacy
    elif legacy is not None and legacy != revision:
        return None
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return None
    return revision


def _valid_expert_job_terminal_evidence(
    evidence: object, *, status: str, job_ref: str, terminal_job_id: str, job_revision: int, receipt_ref: object,
) -> bool:
    """Validate the direct runtime ingress without trusting the bridge caller."""
    if not isinstance(evidence, Mapping):
        return False
    if not terminal_job_id.startswith(f"media_hands:{job_ref.rsplit('/', 1)[-1]}:"):
        return False
    if status == "completed":
        return bool(
            isinstance(receipt_ref, str)
            and re.fullmatch(r"crp://[A-Za-z0-9._~/-]{1,384}", receipt_ref)
            and set(evidence) == {"kind", "status", "job_id", "job_revision", "execution_id"}
            and evidence.get("kind") == "media_execution_receipt"
            and evidence.get("status") == "completed"
            and evidence.get("job_id") == terminal_job_id
            and evidence.get("job_revision") == job_revision
            and isinstance(evidence.get("execution_id"), str) and bool(evidence["execution_id"])
        )
    return bool(
        receipt_ref is None
        and set(evidence) == {"kind", "status", "job_id", "job_revision", "code"}
        and evidence.get("kind") == "media_job_terminal"
        and evidence.get("status") == status
        and evidence.get("job_id") == terminal_job_id
        and evidence.get("job_revision") == job_revision
        and isinstance(evidence.get("code"), str) and bool(evidence["code"])
    )


def _tool_outcome_terminal(status: str) -> tuple[str, str, str]:
    return {
        "completed": ("tool.completed", "running", "recorded tool completion converged"),
        "failed": ("tool.failed", "running", "recorded tool failure converged"),
        "timed_out": ("tool.failed", "running", "recorded tool deadline converged"),
        "cancelled": ("tool.cancelled", "running", "recorded tool cancellation converged"),
        "unknown_effect": ("tool.failed", "running", "recorded tool outcome requires review"),
    }[status]


def _tool_outcome_turn_terminal(status: str) -> tuple[str, str, str]:
    return {
        "failed": ("turn.failed", "failed", "recorded tool failure converged"),
        "timed_out": ("turn.failed", "failed", "recorded tool deadline converged"),
        "cancelled": ("turn.cancelled", "cancelled", "recorded tool cancellation converged"),
        "unknown_effect": ("turn.failed", "failed", "recorded tool outcome requires review"),
    }[status]


def _latest_approval_resolution(events: tuple[Mapping[str, object], ...]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "approval.required":
            return None
        if event.get("type") != "approval.resolved":
            continue
        data = event.get("data")
        summary = data.get("summary") if isinstance(data, Mapping) else None
        return summary if summary in {"approve", "reject"} else None
    return None


_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~/-]{0,127}$")


def _safe_model_metadata(value: object, *, model: bool) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    pattern = _MODEL_ID if model else _PROVIDER_ID
    return normalized if pattern.fullmatch(normalized) else None


def _normalized_wire_usage(usage: Mapping[str, int]) -> dict[str, int] | None:
    # Providers may report just a total or only input/output counters. Keep
    # observed counters without inventing the missing portions.
    result = {}
    for key, aliases in (("input_tokens", ("input_tokens", "prompt_tokens")),
                         ("output_tokens", ("output_tokens", "completion_tokens")),
                         ("total_tokens", ("total_tokens",))):
        value = next((usage[name] for name in aliases if name in usage), None)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2_147_483_647:
            result[key] = value
    return result or None


def _normalized_model_usage(usage: Mapping[str, int]) -> dict[str, int] | None:
    aliases = (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens",),
    )
    values: list[int] = []
    for candidates in aliases:
        value = next((usage[key] for key in candidates if key in usage), None)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        values.append(value)
    return {
        "input_tokens": values[0],
        "output_tokens": values[1],
        "total_tokens": values[2],
    }


def _normalized_wire_cache_metadata(
    observation: Mapping[str, int] | None,
) -> dict[str, object] | None:
    if not observation:
        return None
    allowed = {
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_miss_input_tokens",
    }
    if set(observation) - allowed:
        return None
    normalized: dict[str, int] = {}
    for key, value in observation.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        normalized[key] = value
    return {
        "source_format": "provider_usage",
        "cache_read_input_tokens": normalized.get("cache_read_input_tokens"),
        "cache_write_input_tokens": normalized.get("cache_creation_input_tokens"),
        "uncached_input_tokens": normalized.get("cache_miss_input_tokens"),
    }


def _model_terminal_for(error_code: str) -> tuple[str, str]:
    if error_code == "ai.planner_cancelled":
        return "cancelled", "model.cancelled"
    if error_code == "ai.planner_timeout":
        return "timed_out", "model.timed_out"
    return "failed", "model.failed"


def _model_terminal_summary(status: str) -> str:
    return {
        "cancelled": "planner model call cancelled",
        "timed_out": "planner model call timed out",
        "failed": "planner model call failed",
    }[status]


def _definition_matches_intent(
    definition: CapabilityDefinition,
    intent: ToolInvocationIntent,
) -> bool:
    if definition.version != intent.capability_version:
        return False
    if intent.tool_contract is None:
        return definition.tool_definition is None
    if intent.requires_approval is None or intent.requires_approval != definition.requires_approval:
        return False
    try:
        tool = tool_from_capability(definition)
    except ValueError:
        return False
    return tool_matches_contract_identity(tool, intent.tool_contract)


def _boundary_payload(decision: ToolExecutionBoundaryDecision) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "outcome": decision.outcome,
        "reason_codes": list(decision.reason_codes),
        "matched_grant_ids": list(decision.matched_grant_ids),
        "policy_revision": decision.policy_revision,
        "requires_receipt": decision.requires_receipt,
        "redaction_required": decision.redaction_required,
    }
