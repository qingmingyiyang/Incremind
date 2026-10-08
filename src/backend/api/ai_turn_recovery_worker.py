from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

from core.ai_kernel import RunLeaseRevoked


_APPLICATION_WORKER_LOCK = Lock()


def run_safe_ai_turn_recovery(
    store: object,
    runtime: object,
    *,
    limit: int = 32,
    lease_ttl: timedelta = timedelta(seconds=30),
    clock: Callable[[], datetime] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_active: Callable[[object | None], bool | None] | None = None,
) -> int:
    """Run bounded, already-audited safe resumes.

    A queue claim atomically fences the old generation.  Any unknown execution
    result is quarantined; a revoked token is left alone because a newer owner
    may already be authoritative.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
        raise ValueError("recovery worker limit is invalid")
    if not isinstance(lease_ttl, timedelta) or lease_ttl <= timedelta(0):
        raise ValueError("recovery worker lease TTL must be positive")
    now = clock or (lambda: datetime.now(timezone.utc))
    completed = 0
    for _ in range(limit):
        if should_stop is not None and should_stop():
            break
        instant = now()
        try:
            claimed = store.claim_safe_recovery_queue(
                now=instant, stale_after=instant + lease_ttl, limit=1,
            )
        except Exception:
            break
        if not claimed:
            break
        item = claimed[0]
        active_authorized = on_active(item) if on_active is not None else True
        if active_authorized is False or (should_stop is not None and should_stop()):
            try:
                runtime.request_background_cancel(item.turn_id, item.run_lease)
            except Exception:
                pass
            if on_active is not None:
                on_active(None)
            break
        stop_heartbeat = Event()
        lost_lease = Event()

        def heartbeat() -> None:
            interval = max(0.01, lease_ttl.total_seconds() / 3)
            while not stop_heartbeat.wait(interval):
                try:
                    instant = now()
                    if runtime.renew_run_lease(item.run_lease, now=instant, stale_after=instant + lease_ttl) is None:
                        lost_lease.set(); return
                except RunLeaseRevoked:
                    lost_lease.set(); return
                except Exception:
                    # Storage failures are retried; they never authorize a
                    # release or a replacement execution.
                    continue
        pulse = Thread(target=heartbeat, name="ai-turn-recovery-heartbeat", daemon=True)
        pulse.start()
        try:
            receipt = runtime.recover_accepted_turn(item.turn_id, item.run_lease)
        except RunLeaseRevoked:
            # Do not alter a possibly newer owner or release this old token.
            continue
        except Exception:
            try:
                store.quarantine_recovery_queue(
                    item, reason_code="ai.recovery_execution_unknown", observed_at=now(),
                )
            except Exception:
                pass
            continue
        finally:
            stop_heartbeat.set()
            pulse.join(timeout=max(0.05, lease_ttl.total_seconds() / 3 + 0.1))
            if on_active is not None:
                on_active(None)
        if lost_lease.is_set():
            continue
        try:
            if getattr(receipt, "status", None) not in {"completed", "failed", "cancelled", "waiting_approval"}:
                store.quarantine_recovery_queue(
                    item, reason_code="ai.recovery_execution_unknown", observed_at=now(),
                )
            elif store.complete_recovery_queue(
                item,
                observed_at=now(),
                result_status=str(receipt.status),
            ):
                runtime.release_strict_run_lease(item.run_lease)
                completed += 1
        except Exception:
            # The Turn result is known, but queue bookkeeping is not.  Keep
            # the active lease rather than allowing an unsafe replay.
            continue
    return completed


def run_startup_reconciliation(
    reconcile: Callable[[], int] | None,
) -> int:
    """Run one injected, non-executing startup reconciler.

    Job terminal observation is intentionally outside normal AI Turn recovery:
    it never resumes a planner, Tool, or Job.  Keeping this tiny seam here
    lets application composition provide a governed Media snapshot query
    without making the recovery worker depend on Media Hands.
    """
    if reconcile is None:
        return 0
    result = reconcile()
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise ValueError("startup reconciler result is invalid")
    return result


class AIRecoveryWorker:
    """Application-owned background recovery with bounded shutdown."""

    def __init__(
        self,
        store: object,
        runtime: object,
        *,
        limit: int = 32,
        lease_ttl: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] | None = None,
        startup_reconcile: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._limit = limit
        self._lease_ttl = lease_ttl
        self._clock = clock
        self._startup_reconcile = startup_reconcile
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._active: object | None = None
        self._thread = Thread(target=self._run, name="ai-turn-recovery", daemon=True)

    def start(self) -> None:
        self._wake.set()
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def shutdown(self, *, timeout_seconds: float = 1.0) -> bool:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds < 0:
            raise ValueError("recovery worker shutdown timeout must be non-negative")
        with self._lock:
            self._stop.set()
            active = self._active
        self._wake.set()
        if active is not None:
            try:
                self._runtime.request_background_cancel(active.turn_id, active.run_lease)
            except Exception:
                pass
        self._thread.join(timeout=float(timeout_seconds))
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            run_startup_reconciliation(self._startup_reconcile)
        except Exception:
            # A snapshot source outage must not turn into AI execution.  The
            # worker remains available for regular recovery and a later
            # application-owned reconciliation may retry the observation.
            pass
        while not self._stop.is_set():
            self._wake.clear()
            completed = run_safe_ai_turn_recovery(
                self._store,
                self._runtime,
                limit=self._limit,
                lease_ttl=self._lease_ttl,
                clock=self._clock,
                should_stop=self._stop.is_set,
                on_active=self._set_active,
            )
            if completed >= self._limit:
                continue
            self._wake.wait()

    def _set_active(self, item: object | None) -> bool:
        with self._lock:
            if item is not None and self._stop.is_set():
                return False
            self._active = item
            return True


def start_ai_turn_recovery_worker(application: object, store: object, runtime: object) -> AIRecoveryWorker:
    with _APPLICATION_WORKER_LOCK:
        existing = getattr(getattr(application, "state"), "ai_turn_recovery_worker", None)
        if existing is not None and existing.is_alive():
            existing.wake()
            return existing
        reconcile = getattr(getattr(application, "state"), "expert_media_job_wait_reconcile", None)
        worker = AIRecoveryWorker(
            store, runtime, startup_reconcile=reconcile if callable(reconcile) else None,
        )
        worker.start()
        application.state.ai_turn_recovery_worker = worker
        return worker


def shutdown_ai_turn_recovery_worker(application: object) -> None:
    with _APPLICATION_WORKER_LOCK:
        worker = getattr(getattr(application, "state", object()), "ai_turn_recovery_worker", None)
        if worker is not None:
            worker.shutdown()
