"""In-process runtime host for the pinned Codex Hook protocol.

This module deliberately has no HTTP, model, database, or Boundary dependency.
It snapshots declarative handler policy, invokes injected *local* handlers, and
passes their completed results to :func:`evaluate_hook_event`.  The resulting
typed Codex outcome is the only control value; this host does not invent a
second generic verdict or rerun a policy decision.

Audit receipts are appended to a bounded in-memory outbox.  Draining that
outbox is an optional, best-effort operation and failures never affect the
hook data plane.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import re
from queue import Empty, Queue
from threading import Lock, RLock, Thread
from time import monotonic_ns, perf_counter_ns
from typing import Protocol, cast
from uuid import uuid4

from .codex_hook_parity import (
    CODEX_HOOK_PARITY_REVISION,
    HookEvent,
    HookEventOutcome,
    HookRun,
    evaluate_hook_event,
)


class HookRuntimeError(RuntimeError):
    """Raised for invalid local Hook Host configuration or invocation."""


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SAFE_CRP_REF_RE = re.compile(r"^crp://[A-Za-z0-9._~:/-]+$")
_SENSITIVE_REASON_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|[\\/]|\b(?:api[_ -]?key|authorization|cookie|password|secret|token)\b)",
    re.IGNORECASE,
)
_SAFE_REASON = "hook diagnostic redacted"


def _require_safe_id(value: object, field_name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise HookRuntimeError(f"{field_name} must be a safe non-empty identifier")
    return value


def _require_safe_ref(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_CRP_REF_RE.fullmatch(value):
        raise HookRuntimeError(f"{field_name} must be a safe crp reference")
    return value


def _safe_reason(value: object) -> str | None:
    """Keep a bounded, non-sensitive diagnostic out of the receipt authority.

    Hook stdout/stderr and input payloads are deliberately absent from this
    module's serialized contracts.  The normalized reason may be retained only
    when it is plainly a short policy explanation rather than a path, command,
    credential, or other likely sensitive diagnostic.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _SENSITIVE_REASON_RE.search(text):
        return _SAFE_REASON
    return text[:1024]


@dataclass(frozen=True)
class HookHandlerManifest:
    """Immutable declarative description of one locally executable handler."""

    hook_id: str
    revision: str
    event: HookEvent
    config_order: int
    synchronous: bool = True
    enabled: bool = True
    handler_ref: str | None = None
    timeout_ms: int = 1000

    def __post_init__(self) -> None:
        _require_safe_id(self.hook_id, "hook_id")
        _require_safe_id(self.revision, "handler revision")
        if self.config_order < 0:
            raise HookRuntimeError("config_order must be non-negative")
        if self.timeout_ms < 1 or self.timeout_ms > 60000:
            raise HookRuntimeError("handler timeout_ms must be between 1 and 60000")
        handler_ref = self.handler_ref or f"crp://local/hooks/{self.hook_id}"
        _require_safe_ref(handler_ref, "handler_ref")
        object.__setattr__(self, "handler_ref", handler_ref)


@dataclass(frozen=True)
class HookPolicySnapshot:
    """Frozen Hook policy used by a single invocation.

    Revisions are installed atomically in :class:`HookPolicyCatalog`.  Existing
    callers retain this immutable object, so a new policy cannot silently alter
    an in-flight Turn.
    """

    revision: str
    handlers: tuple[HookHandlerManifest, ...]
    codex_revision: str = CODEX_HOOK_PARITY_REVISION
    snapshot_id: str | None = None
    manifest_ref: str = "crp://local/hooks/manifests/default"
    manifest_revision: str | None = None
    local_hard_guard_revision: str = "local-hard-guard-default"

    def __post_init__(self) -> None:
        _require_safe_id(self.revision, "policy revision")
        if self.codex_revision != CODEX_HOOK_PARITY_REVISION:
            raise HookRuntimeError("policy must pin the audited Codex Hook revision")
        snapshot_id = self.snapshot_id or f"hook-snapshot-{self.revision}"
        manifest_revision = self.manifest_revision or self.revision
        _require_safe_id(snapshot_id, "snapshot_id")
        _require_safe_ref(self.manifest_ref, "manifest_ref")
        _require_safe_id(manifest_revision, "manifest_revision")
        _require_safe_id(self.local_hard_guard_revision, "local_hard_guard_revision")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "manifest_revision", manifest_revision)
        identities = {(item.event, item.hook_id) for item in self.handlers}
        if len(identities) != len(self.handlers):
            raise HookRuntimeError("a policy cannot contain duplicate event/hook_id entries")

    def handlers_for(self, event: HookEvent | str) -> tuple[HookHandlerManifest, ...]:
        resolved = HookEvent(event)
        return tuple(sorted(
            (item for item in self.handlers if item.enabled and item.event is resolved),
            key=lambda item: item.config_order,
        ))


class HookPolicyCatalog:
    """Small process-local, atomically switched revision catalog."""

    def __init__(self, initial: HookPolicySnapshot) -> None:
        self._lock = RLock()
        self._current = initial
        self._revisions: dict[str, HookPolicySnapshot] = {initial.revision: initial}

    def current(self) -> HookPolicySnapshot:
        with self._lock:
            return self._current

    def get(self, revision: str) -> HookPolicySnapshot | None:
        with self._lock:
            return self._revisions.get(revision)

    def install(self, snapshot: HookPolicySnapshot) -> HookPolicySnapshot:
        """Publish a fully formed snapshot in one critical section."""
        with self._lock:
            known = self._revisions.get(snapshot.revision)
            if known is not None and known != snapshot:
                raise HookRuntimeError("policy revision is immutable once installed")
            self._revisions[snapshot.revision] = snapshot
            self._current = snapshot
            return snapshot


class LocalHookHandlerRunner(Protocol):
    """Injected in-process runner.

    Implementations must be local and deterministic relative to their supplied
    manifest and payload.  They must not perform network, model, or database
    operations; such work belongs outside the Hook interception path.
    """

    def __call__(self, manifest: HookHandlerManifest, payload: Mapping[str, object]) -> HookRun: ...


class RevisionPinnedHookRunner:
    """Resolve executable handlers by the immutable ``(hook_id, revision)`` key."""

    def __init__(
        self,
        handlers: Mapping[
            tuple[str, str],
            Callable[[HookHandlerManifest, Mapping[str, object]], HookRun],
        ],
    ) -> None:
        if not handlers:
            raise HookRuntimeError("revision-pinned runner requires handlers")
        self._handlers = dict(handlers)
        for hook_id, revision in self._handlers:
            _require_safe_id(hook_id, "hook_id")
            _require_safe_id(revision, "handler revision")

    def supports(self, manifest: HookHandlerManifest) -> bool:
        return (manifest.hook_id, manifest.revision) in self._handlers

    def __call__(
        self,
        manifest: HookHandlerManifest,
        payload: Mapping[str, object],
    ) -> HookRun:
        handler = self._handlers.get((manifest.hook_id, manifest.revision))
        if handler is None:
            raise HookRuntimeError("frozen Hook handler revision is unavailable")
        return handler(manifest, payload)


@dataclass(frozen=True)
class HookInvocationReceipt:
    """Typed, receipt-shaped fact of one evaluated Hook event."""

    policy_revision: str
    codex_revision: str
    event: HookEvent
    outcome: HookEventOutcome
    elapsed_ns: int
    handler_ids: tuple[str, ...]
    handler_manifests: tuple[HookHandlerManifest, ...] = ()


@dataclass(frozen=True)
class HookAuditObservation:
    """Non-sensitive best-effort metrics fact for asynchronous export."""

    policy_revision: str
    codex_revision: str
    event: HookEvent
    elapsed_ns: int
    handler_ids: tuple[str, ...]
    run_statuses: tuple[str, ...]


def hook_policy_snapshot_to_payload(snapshot: HookPolicySnapshot) -> dict[str, object]:
    """Serialize the safe immutable snapshot contract.

    It intentionally copies only declarative handler identity/order metadata.
    Handler implementation, command, path, payload, and credentials cannot
    enter this payload because they are not fields on the serializable view.
    """

    return {
        "schema_version": "1.0.0",
        "kind": "codex-hook-policy-snapshot",
        "snapshot_id": snapshot.snapshot_id,
        "revision": snapshot.revision,
        "upstream_revision": snapshot.codex_revision,
        "manifest_ref": snapshot.manifest_ref,
        "manifest_revision": snapshot.manifest_revision,
        "handlers": [
            {
                "handler_id": handler.hook_id,
                "handler_revision": handler.revision,
                "event": handler.event.value,
                "order": handler.config_order,
                "sync": handler.synchronous,
                "enabled": handler.enabled,
                "handler_ref": handler.handler_ref,
                "timeout_ms": handler.timeout_ms,
            }
            for handler in snapshot.handlers
        ],
        "local_hard_guard_revision": snapshot.local_hard_guard_revision,
        "audit_delivery": "async_best_effort",
    }


def hook_policy_snapshot_from_payload(value: object) -> HookPolicySnapshot:
    """Restore an immutable snapshot without accepting executable hook data."""

    payload = _mapping(value, "policy snapshot")
    _require_exact_keys(
        payload,
        {
            "schema_version", "kind", "snapshot_id", "revision", "upstream_revision",
            "manifest_ref", "manifest_revision", "handlers", "local_hard_guard_revision",
            "audit_delivery",
        },
        "policy snapshot",
    )
    if payload["schema_version"] != "1.0.0" or payload["kind"] != "codex-hook-policy-snapshot":
        raise HookRuntimeError("policy snapshot has an unsupported shape")
    if payload["upstream_revision"] != CODEX_HOOK_PARITY_REVISION:
        raise HookRuntimeError("policy snapshot must pin the audited Codex Hook revision")
    if payload["audit_delivery"] != "async_best_effort":
        raise HookRuntimeError("policy snapshot audit delivery must be async_best_effort")
    handlers_value = payload["handlers"]
    if not isinstance(handlers_value, list):
        raise HookRuntimeError("policy snapshot handlers must be a list")
    handlers: list[HookHandlerManifest] = []
    for item in handlers_value:
        handler = _mapping(item, "policy snapshot handler")
        _require_exact_keys(
            handler, {
                "handler_id", "handler_revision", "event", "order", "sync",
                "enabled", "handler_ref", "timeout_ms",
            },
            "policy snapshot handler",
        )
        handler_id = _require_safe_id(handler["handler_id"], "handler_id")
        revision = _require_safe_id(handler["handler_revision"], "handler_revision")
        order = handler["order"]
        sync = handler["sync"]
        enabled = handler["enabled"]
        timeout_ms = handler["timeout_ms"]
        if not isinstance(order, int) or isinstance(order, bool) or order < 0:
            raise HookRuntimeError("handler order must be a non-negative integer")
        if not isinstance(sync, bool):
            raise HookRuntimeError("handler sync must be boolean")
        if not isinstance(enabled, bool):
            raise HookRuntimeError("handler enabled must be boolean")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise HookRuntimeError("handler timeout_ms must be an integer")
        try:
            event = HookEvent(cast(str, handler["event"]))
        except (TypeError, ValueError) as exc:
            raise HookRuntimeError("handler event is unsupported") from exc
        handlers.append(HookHandlerManifest(
            hook_id=cast(str, handler_id), revision=cast(str, revision), event=event,
            config_order=order, synchronous=sync, enabled=enabled,
            handler_ref=_require_safe_ref(handler["handler_ref"], "handler_ref"),
            timeout_ms=timeout_ms,
        ))
    return HookPolicySnapshot(
        revision=cast(str, _require_safe_id(payload["revision"], "revision")),
        handlers=tuple(handlers),
        codex_revision=CODEX_HOOK_PARITY_REVISION,
        snapshot_id=cast(str, _require_safe_id(payload["snapshot_id"], "snapshot_id")),
        manifest_ref=_require_safe_ref(payload["manifest_ref"], "manifest_ref"),
        manifest_revision=cast(str, _require_safe_id(payload["manifest_revision"], "manifest_revision")),
        local_hard_guard_revision=cast(str, _require_safe_id(
            payload["local_hard_guard_revision"], "local_hard_guard_revision"
        )),
    )


def hook_invocation_receipt_to_payload(
    receipt: HookInvocationReceipt,
    *,
    turn_id: str | None,
    project_id: str | None,
    policy_snapshot_ref: str,
    snapshot: HookPolicySnapshot | None = None,
    invocation_id: str | None = None,
) -> dict[str, object]:
    """Return append-only safe evidence for one Hook invocation.

    The caller supplies authority references rather than a payload/body.  Each
    handler revision is resolved from the frozen snapshot, preventing a newer
    manifest revision from being silently attributed to an old Turn.
    """

    if receipt.codex_revision != CODEX_HOOK_PARITY_REVISION:
        raise HookRuntimeError("receipt must pin the audited Codex Hook revision")
    if snapshot is not None and receipt.policy_revision != snapshot.revision:
        raise HookRuntimeError("receipt policy revision must match its frozen snapshot")
    _require_safe_id(turn_id, "turn_id", nullable=True)
    _require_safe_id(project_id, "project_id", nullable=True)
    _require_safe_ref(policy_snapshot_ref, "policy_snapshot_ref")
    stable_invocation_id = invocation_id or _default_invocation_id(receipt, turn_id, project_id)
    _require_safe_id(stable_invocation_id, "invocation_id")
    frozen_handlers = (
        snapshot.handlers_for(receipt.event)
        if snapshot is not None
        else receipt.handler_manifests
    )
    handler_by_id = {handler.hook_id: handler for handler in frozen_handlers}
    handler_runs = []
    for result in receipt.outcome.runs:
        manifest = handler_by_id.get(result.run.hook_id)
        if manifest is None:
            raise HookRuntimeError("receipt handler must be present in the frozen snapshot")
        handler_runs.append({
            "handler_id": manifest.hook_id,
            "handler_revision": manifest.revision,
            "order": manifest.config_order,
            "sync": manifest.synchronous,
            "status": result.status.value,
            "control_ignored": result.control_ignored,
            "reason": _safe_reason(_run_reason(receipt.outcome, result)),
            "duration_ms": 0,
        })
    return {
        "schema_version": "1.0.0",
        "kind": "codex-hook-invocation-receipt",
        "invocation_id": stable_invocation_id,
        "turn_id": turn_id,
        "project_id": project_id,
        "event": receipt.event.value,
        "upstream_revision": receipt.codex_revision,
        "policy_snapshot_ref": policy_snapshot_ref,
        "policy_snapshot_revision": receipt.policy_revision,
        "handler_runs": handler_runs,
        "normalized_outcome": _normalized_outcome_payload(receipt.outcome),
        "duration_ms": max(0, receipt.elapsed_ns // 1_000_000),
        "audit_delivery": "async_best_effort",
    }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HookRuntimeError(f"{label} must be an object")
    return value


def _require_exact_keys(payload: Mapping[str, object], keys: set[str], label: str) -> None:
    if set(payload) != keys:
        raise HookRuntimeError(f"{label} shape is invalid")


def _default_invocation_id(receipt: HookInvocationReceipt, turn_id: str | None, project_id: str | None) -> str:
    # No payload, path, or authority identifier participates in this ID.  A
    # random local nonce also stays inside the schema limit when Turn IDs use
    # their full allowed length.
    del receipt, turn_id, project_id
    return f"hook-invocation-{uuid4().hex}"


def _run_reason(outcome: HookEventOutcome, result: object) -> str | None:
    run_result = cast("HookRunResult", result)
    return run_result.diagnostic or _outcome_reason(outcome)


def _outcome_reason(outcome: HookEventOutcome) -> str | None:
    return outcome.stop_reason or (outcome.feedback[0] if outcome.feedback else None)


def _normalized_outcome_payload(outcome: HookEventOutcome) -> dict[str, object]:
    reason = _safe_reason(_outcome_reason(outcome))
    if outcome.event is HookEvent.PRE_TOOL_USE:
        return {
            "event": outcome.event.value,
            "dispatch_blocked": outcome.dispatch_blocked,
            "reason": reason,
            "input_rewrite_applied": outcome.updated_input is not None,
        }
    if outcome.event is HookEvent.PERMISSION_REQUEST:
        permission = "denied" if outcome.permission_denied else (
            "allowed" if outcome.permission_allowed else "no_decision"
        )
        return {"event": outcome.event.value, "permission": permission, "reason": reason}
    if outcome.event is HookEvent.POST_TOOL_USE:
        return {
            "event": outcome.event.value,
            "follow_up_blocked": outcome.block_follow_up,
            "turn_stopped": outcome.should_stop,
            "reason": reason,
        }
    if outcome.event in {
        HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT, HookEvent.SESSION_START,
        HookEvent.USER_PROMPT_SUBMIT,
    }:
        return {
            "event": outcome.event.value,
            "turn_stopped": outcome.should_stop,
            "reason": reason,
            "additional_context_count": len(outcome.additional_context),
        }
    if outcome.event in {HookEvent.STOP, HookEvent.SUBAGENT_STOP}:
        return {
            "event": outcome.event.value,
            "stop_allowed": bool(outcome.allow_stop),
            "reason": reason,
        }
    if outcome.event is HookEvent.SUBAGENT_START:
        return {"event": outcome.event.value, "additional_context_count": len(outcome.additional_context)}
    return {"event": HookEvent.SESSION_END.value, "observed": True}


# Short aliases keep the public adapter readable at call sites that already use
# the existing manifest/context ``*_to_payload`` convention.
policy_snapshot_to_payload = hook_policy_snapshot_to_payload
policy_snapshot_from_payload = hook_policy_snapshot_from_payload
receipt_to_payload = hook_invocation_receipt_to_payload


@dataclass(frozen=True)
class HookRuntimeMetrics:
    audit_queue_depth: int
    audit_dropped: int
    audit_failures: int
    latency_samples_ns: tuple[int, ...]
    cumulative_hook_time_ns: int
    p50_ms: float
    p95_ms: float
    p99_ms: float


@dataclass
class _MutableMetrics:
    audit_dropped: int = 0
    audit_failures: int = 0
    latency_samples_ns: deque[int] = field(default_factory=deque)
    cumulative_hook_time_ns: int = 0


class HookAuditOutbox:
    """Bounded non-blocking local receipt queue.

    The producer never waits on an audit destination.  Consumers explicitly
    call :meth:`drain`; an exception increments a metric and leaves the failed
    receipt queued for a later retry.
    """

    def __init__(self, *, max_items: int = 256, latency_sample_limit: int = 512) -> None:
        if max_items <= 0 or latency_sample_limit <= 0:
            raise HookRuntimeError("outbox limits must be positive")
        self._max_items = max_items
        self._latency_sample_limit = latency_sample_limit
        self._items: deque[HookAuditObservation] = deque()
        self._metrics = _MutableMetrics()
        self._lock = Lock()

    def append(self, receipt: HookAuditObservation) -> None:
        with self._lock:
            if len(self._items) >= self._max_items:
                self._items.popleft()
                self._metrics.audit_dropped += 1
            self._items.append(receipt)
            samples = self._metrics.latency_samples_ns
            samples.append(receipt.elapsed_ns)
            self._metrics.cumulative_hook_time_ns += receipt.elapsed_ns
            while len(samples) > self._latency_sample_limit:
                samples.popleft()

    def drain(self, sink: Callable[[HookAuditObservation], None], *, limit: int = 32) -> int:
        """Best-effort audit export; sink failure never raises into a Turn."""
        if limit <= 0:
            return 0
        delivered = 0
        while delivered < limit:
            with self._lock:
                if not self._items:
                    return delivered
                receipt = self._items[0]
            try:
                sink(receipt)
            except Exception:  # audit must not block or fail the data plane
                with self._lock:
                    self._metrics.audit_failures += 1
                return delivered
            with self._lock:
                if self._items and self._items[0] is receipt:
                    self._items.popleft()
                    delivered += 1
        return delivered

    def metrics(self) -> HookRuntimeMetrics:
        with self._lock:
            samples = tuple(self._metrics.latency_samples_ns)
            return HookRuntimeMetrics(
                audit_queue_depth=len(self._items),
                audit_dropped=self._metrics.audit_dropped,
                audit_failures=self._metrics.audit_failures,
                latency_samples_ns=samples,
                cumulative_hook_time_ns=self._metrics.cumulative_hook_time_ns,
                p50_ms=_latency_percentile_ms(samples, 50),
                p95_ms=_latency_percentile_ms(samples, 95),
                p99_ms=_latency_percentile_ms(samples, 99),
            )


def _latency_percentile_ms(samples: tuple[int, ...], percentile: int) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(1, (len(ordered) * percentile + 99) // 100)
    return ordered[rank - 1] / 1_000_000


class CodexHookHost:
    """The thin local Hook data-plane host.

    No Boundary, Governor, database, or remote audit call is made during
    :meth:`invoke`.  Callers consume ``receipt.outcome`` directly according to
    the event-specific Codex contract.
    """

    def __init__(self, *, catalog: HookPolicyCatalog, runner: LocalHookHandlerRunner, audit_outbox: HookAuditOutbox | None = None) -> None:
        self._catalog = catalog
        self._runner = runner
        self._audit_outbox = audit_outbox or HookAuditOutbox()
        self._emergency_lock = RLock()
        self._disabled_handler_prefixes: set[str] = set()

    @property
    def audit_outbox(self) -> HookAuditOutbox:
        return self._audit_outbox

    def current_snapshot(self) -> HookPolicySnapshot:
        """Freeze the currently published policy without exposing the catalog."""

        return self._catalog.current()

    def install_snapshot(self, snapshot: HookPolicySnapshot) -> HookPolicySnapshot:
        """Atomically publish a fully validated control-plane projection."""

        if not isinstance(snapshot, HookPolicySnapshot):
            raise HookRuntimeError("Hook policy snapshot is invalid")
        self.assert_snapshot_executable(snapshot)
        return self._catalog.install(snapshot)

    def set_handler_prefix_enabled(self, prefix: str, *, enabled: bool) -> None:
        """Independent local kill switch for a handler family.

        The switch is checked after snapshot selection, so it also fences
        future invocations that still hold an older frozen snapshot.
        """

        if not isinstance(prefix, str) or not prefix:
            raise HookRuntimeError("Hook handler prefix is invalid")
        with self._emergency_lock:
            if enabled:
                self._disabled_handler_prefixes.discard(prefix)
            else:
                self._disabled_handler_prefixes.add(prefix)

    def assert_snapshot_executable(self, snapshot: HookPolicySnapshot) -> None:
        """Fail closed when a historical snapshot lacks revision-pinned code."""

        supports = getattr(self._runner, "supports", None)
        if not callable(supports) or any(
            not supports(handler) for handler in snapshot.handlers if handler.enabled
        ):
            raise HookRuntimeError("frozen Hook handler revision is unavailable")

    def invoke(
        self,
        event: HookEvent | str,
        payload: Mapping[str, object],
        *,
        snapshot: HookPolicySnapshot | None = None,
    ) -> HookInvocationReceipt:
        """Run the selected local handlers and evaluate their typed outcome."""
        resolved_event = HookEvent(event)
        frozen = snapshot or self._catalog.current()
        started = perf_counter_ns()
        manifests = frozen.handlers_for(resolved_event)
        with self._emergency_lock:
            disabled = tuple(self._disabled_handler_prefixes)
        if disabled:
            manifests = tuple(
                manifest for manifest in manifests
                if not any((manifest.handler_ref or "").startswith(prefix) for prefix in disabled)
            )
        handler_ids = [manifest.hook_id for manifest in manifests]
        runs = self._invoke_handlers(manifests, payload)
        receipt = HookInvocationReceipt(
            policy_revision=frozen.revision,
            codex_revision=frozen.codex_revision,
            event=resolved_event,
            outcome=evaluate_hook_event(resolved_event, runs),
            elapsed_ns=perf_counter_ns() - started,
            handler_ids=tuple(handler_ids),
            handler_manifests=manifests,
        )
        self._audit_outbox.append(HookAuditObservation(
            policy_revision=receipt.policy_revision,
            codex_revision=receipt.codex_revision,
            event=receipt.event,
            elapsed_ns=receipt.elapsed_ns,
            handler_ids=receipt.handler_ids,
            run_statuses=tuple(item.status.value for item in receipt.outcome.runs),
        ))
        return receipt

    def _invoke_handlers(
        self,
        manifests: tuple[HookHandlerManifest, ...],
        payload: Mapping[str, object],
    ) -> list[HookRun]:
        """Invoke all selected local handlers concurrently within their deadlines.

        ``timeout_ms`` limits how long the Hook data plane waits for an
        in-process handler.  Python cannot safely kill arbitrary code running
        in the same interpreter, so an overrun is detached as a daemon worker
        and reported as a failed, fail-open run.  Production handler adapters
        must remain cooperative and local; this host never makes a network or
        model call while waiting.

        Completion order is assigned at the local result queue, not manifest
        order.  This preserves Codex's PreToolUse rule: when no deny wins, the
        last *completed* valid input rewrite wins.
        """

        if not manifests:
            return []
        completed: Queue[tuple[int, HookRun | Exception]] = Queue()
        started_at = monotonic_ns()
        deadlines = {
            index: started_at + manifest.timeout_ms * 1_000_000
            for index, manifest in enumerate(manifests)
        }

        def execute(index: int, manifest: HookHandlerManifest) -> None:
            try:
                completed.put((index, self._runner(manifest, payload)))
            except Exception as exc:
                completed.put((index, exc))

        for index, manifest in enumerate(manifests):
            Thread(
                target=execute,
                args=(index, manifest),
                name=f"codex-hook-{manifest.hook_id}",
                daemon=True,
            ).start()

        pending = set(range(len(manifests)))
        runs: list[HookRun] = []
        completion_order = 0
        while pending:
            try:
                index, supplied = completed.get(timeout=max(
                    0.0,
                    (min(deadlines[item] for item in pending) - monotonic_ns()) / 1_000_000_000,
                ))
            except Empty:
                now = monotonic_ns()
                expired = [index for index in pending if deadlines[index] <= now]
                for index in expired:
                    manifest = manifests[index]
                    pending.remove(index)
                    runs.append(HookRun(
                        config_order=manifest.config_order,
                        completion_order=completion_order,
                        synchronous=manifest.synchronous,
                        exit_code=1,
                        stderr="local hook runner timed out",
                        hook_id=manifest.hook_id,
                    ))
                    completion_order += 1
                continue
            if index not in pending:
                continue
            manifest = manifests[index]
            pending.remove(index)
            if isinstance(supplied, Exception):
                supplied = HookRun(
                    config_order=manifest.config_order,
                    completion_order=completion_order,
                    synchronous=manifest.synchronous,
                    exit_code=1,
                    stderr=f"local hook runner failed: {type(supplied).__name__}",
                    hook_id=manifest.hook_id,
                )
            runs.append(replace(
                supplied,
                config_order=manifest.config_order,
                completion_order=completion_order,
                synchronous=manifest.synchronous,
                hook_id=manifest.hook_id,
            ))
            completion_order += 1
        return runs
