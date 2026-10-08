from __future__ import annotations

"""Governed terminal-Job observation for an expert Turn wait.

This is deliberately an adapter, not a Media Job lifecycle owner.  It consumes
only terminal snapshots supplied by the later Media Hands composition and the
immutable expert wait snapshot recorded by the AI Kernel.  A winner may notify
an internal application callback; it never applies a user ``resume`` action,
invokes a Tool, or starts/restarts a Job.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from core.ai_kernel import RunLeaseToken


_JOB_REF = re.compile(r"crp://jobs/[a-z][a-z0-9-]{2,127}")
_REF = re.compile(r"crp://[A-Za-z0-9._~-]{1,64}/[A-Za-z0-9._~/-]{1,384}")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class ExpertMediaJobTerminal:
    """Safe, portable projection of a Media Job terminal snapshot."""

    job_ref: str
    job_revision: int
    status: str
    receipt_ref: str | None
    source_manifest_ref: str
    source_manifest_revision: str


def verify_expert_media_job_terminal(
    snapshot: Mapping[str, object], *, wait_snapshot: Mapping[str, object],
) -> ExpertMediaJobTerminal:
    """Validate exact terminal schema and bind it to the frozen wait facts."""
    required = {
        "schema_version", "turn_id", "canonical_job_ref", "terminal_job_id", "job_revision", "status",
        "receipt_ref", "terminal_evidence", "source_manifest_ref", "source_manifest_revision",
    }
    if set(snapshot) != required or snapshot.get("schema_version") != "1.0.0":
        raise ValueError("expert Media Job terminal snapshot schema is invalid")
    turn_id = snapshot.get("turn_id")
    job_ref = snapshot.get("canonical_job_ref")
    terminal_job_id = snapshot.get("terminal_job_id")
    job_revision = snapshot.get("job_revision")
    status = snapshot.get("status")
    receipt_ref = snapshot.get("receipt_ref")
    source_manifest_ref = snapshot.get("source_manifest_ref")
    source_manifest_revision = snapshot.get("source_manifest_revision")
    if (
        not isinstance(turn_id, str) or not turn_id
        or not isinstance(job_ref, str) or _JOB_REF.fullmatch(job_ref) is None
        or not isinstance(terminal_job_id, str) or not terminal_job_id
        or not isinstance(job_revision, int) or isinstance(job_revision, bool)
        or job_revision < 1
        or status not in _TERMINAL_STATUSES
        or (receipt_ref is not None and (not isinstance(receipt_ref, str) or _REF.fullmatch(receipt_ref) is None))
        or not isinstance(source_manifest_ref, str) or _REF.fullmatch(source_manifest_ref) is None
        or not isinstance(source_manifest_revision, str) or not source_manifest_revision
    ):
        raise ValueError("expert Media Job terminal snapshot is invalid")
    evidence = snapshot.get("terminal_evidence")
    if not isinstance(evidence, Mapping) or evidence.get("status") != status or evidence.get("job_id") != terminal_job_id:
        raise ValueError("expert Media Job terminal evidence is invalid")
    if status == "completed" and receipt_ref is None:
        raise ValueError("completed expert Media Job terminal requires execution receipt")
    if status != "completed" and receipt_ref is not None:
        raise ValueError("failed expert Media Job terminal must not forge execution receipt")
    source_id = job_ref.rsplit("/", 1)[-1]
    if not terminal_job_id.startswith(f"media_hands:{source_id}:"):
        raise ValueError("expert Media Job terminal job identity drifted")
    if (
        wait_snapshot.get("turn_id") != turn_id
        or wait_snapshot.get("canonical_job_ref") != job_ref
        or wait_snapshot.get("source_manifest_ref") != source_manifest_ref
        or wait_snapshot.get("source_manifest_revision") != source_manifest_revision
        or not isinstance(wait_snapshot.get("admission_job_revision"), int)
        or job_revision < int(wait_snapshot["admission_job_revision"])
    ):
        raise ValueError("expert Media Job terminal snapshot drifted from frozen wait")
    return ExpertMediaJobTerminal(
        job_ref=job_ref, job_revision=job_revision, status=str(status),
        receipt_ref=receipt_ref, source_manifest_ref=source_manifest_ref,
        source_manifest_revision=source_manifest_revision,
    )


class ExpertMediaJobWaitBridge:
    """CAS- and lease-guarded internal wake dispatcher.

    ``notify_wake`` is an application-private delivery seam.  Its only input
    is the validated terminal projection and the frozen turn identity.  It is
    not a public API action and has no path to planner/tool/job execution.
    """

    def __init__(
        self,
        store: object,
        runtime: object,
        *,
        notify_wake: Callable[[str, ExpertMediaJobTerminal], None],
        continue_turn: Callable[[str, ExpertMediaJobTerminal, RunLeaseToken], None] | None = None,
        owner_id: str = "expert-media-job-wait-bridge",
        clock: Callable[[], datetime] | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("expert Media Job bridge owner is invalid")
        if not isinstance(lease_ttl, timedelta) or lease_ttl <= timedelta(0):
            raise ValueError("expert Media Job bridge lease TTL is invalid")
        self._store = store
        self._runtime = runtime
        self._notify_wake = notify_wake
        self._continue_turn = continue_turn
        self._owner_id = owner_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lease_ttl = lease_ttl

    def observe_terminal(self, snapshot: Mapping[str, object]) -> bool:
        """Claim one matching terminal observation and emit one internal wake."""
        if not isinstance(snapshot, Mapping):
            raise ValueError("expert Media Job terminal snapshot is invalid")
        turn_id = snapshot.get("turn_id")
        job_ref = snapshot.get("canonical_job_ref")
        if (
            not isinstance(turn_id, str) or not turn_id
            or not isinstance(job_ref, str) or _JOB_REF.fullmatch(job_ref) is None
        ):
            raise ValueError("expert Media Job terminal snapshot is invalid")
        wait = self._wait_for_turn(turn_id, job_ref)
        if wait is None:
            return False
        wait_snapshot = self._load_wait_snapshot(turn_id, wait["snapshot_ref"])
        terminal = verify_expert_media_job_terminal(snapshot, wait_snapshot=wait_snapshot)
        now = self._clock()
        lease = self._try_acquire(turn_id, now)
        if lease is None:
            return False
        try:
            if wait["status"] == "waiting":
                record = getattr(self._runtime, "record_expert_job_terminal", None)
                if not callable(record):
                    raise RuntimeError("expert Media Job terminal bundle runtime is unavailable")
                # This is the one ownership transition: the immutable terminal
                # payload, expert.job.terminal event and wake_enqueued state
                # are committed by the Kernel under this exact strict lease.
                record(turn_id, dict(snapshot), lease)
            else:
                stored = self._load_terminal_snapshot(turn_id)
                if stored != dict(snapshot):
                    raise ValueError("expert Media Job terminal payload drifted from enqueued wake")
                if wait["terminal_job_revision"] != terminal.job_revision:
                    raise ValueError("expert Media Job terminal revision drifted from enqueued wake")
            self._notify_wake(turn_id, terminal)
            # The optional production continuation receives the exact fresh
            # strict lease.  A failure leaves ``wake_enqueued`` durable for a
            # later bounded reconciliation; legacy observers remain signal-only.
            if self._continue_turn is not None:
                self._continue_turn(turn_id, terminal, lease)
            observed = self._store.transition_expert_job_wait(
                turn_id, terminal.job_ref, int(wait["admission_job_revision"]),
                expected_status="wake_enqueued", next_status="terminal_observed",
                terminal_job_revision=terminal.job_revision, run_lease=lease,
            )
            if observed:
                return True
            # Production continuation atomically observes the wait together
            # with the Turn terminal event. Its following bridge CAS is then
            # deliberately a no-op, not a failed delivery.
            final = self._wait_for_turn_terminal(turn_id, terminal)
            return final
        finally:
            # The pending Turn reached a safe, inert wait before the Job
            # completed.  This bridge never keeps a lease past its tiny
            # observation/delivery critical section.
            self._release(lease)

    def reconcile_startup(
        self, terminal_snapshots: Callable[[], Iterable[Mapping[str, object]]],
    ) -> int:
        """Replay a supplied terminal snapshot query without owning Media I/O."""
        if not callable(terminal_snapshots):
            raise ValueError("expert Media Job terminal snapshot query is invalid")
        observed = 0
        for snapshot in terminal_snapshots():
            if self.observe_terminal(snapshot):
                observed += 1
        return observed

    def pending_waits(self, *, limit: int = 64) -> Iterable[Mapping[str, object]]:
        """Expose the bounded read-only wait projection to trusted composition."""
        listing = getattr(self._store, "list_expert_job_waits", None)
        if not callable(listing):
            raise RuntimeError("expert Media Job wait listing is unavailable")
        return listing(limit=limit)

    def iter_pending_waits(
        self, *, batch_size: int = 64, max_batches: int = 256,
    ) -> Iterable[Mapping[str, object]]:
        """Yield stable keyset pages without letting a busy first page starve later waits."""
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 256:
            raise ValueError("expert Media Job wait batch size is invalid")
        if not isinstance(max_batches, int) or isinstance(max_batches, bool) or max_batches < 1:
            raise ValueError("expert Media Job wait batch count is invalid")
        page = getattr(self._store, "list_expert_job_waits_after", None)
        if not callable(page):
            yield from self.pending_waits(limit=batch_size)
            return
        cursor: str | None = None
        for _ in range(max_batches):
            items = tuple(page(after_turn_id=cursor, limit=batch_size))
            if not items:
                return
            last = items[-1].get("turn_id") if isinstance(items[-1], Mapping) else None
            if not isinstance(last, str) or not last or last == cursor:
                raise RuntimeError("expert Media Job wait page cursor is invalid")
            yield from items
            if len(items) < batch_size:
                return
            cursor = last

    def _wait_for_turn(self, turn_id: str, job_ref: str) -> dict[str, object] | None:
        lookup = getattr(self._store, "get_expert_job_wait", None)
        if not callable(lookup):
            raise RuntimeError("expert Media Job wait lookup is unavailable")
        value = lookup(turn_id)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise RuntimeError("expert Media Job wait lookup is invalid")
        item = dict(value)
        if (
            item.get("turn_id") != turn_id
            or item.get("job_ref") != job_ref
            or item.get("status") not in {"waiting", "wake_enqueued"}
            or not isinstance(item.get("snapshot_ref"), str)
            or not isinstance(item.get("admission_job_revision"), int)
        ):
            return None
        if item["status"] == "wake_enqueued" and item.get("terminal_job_revision") is None:
            raise RuntimeError("expert Media Job enqueued wake is missing terminal revision")
        return item

    def _load_wait_snapshot(self, turn_id: str, snapshot_ref: object) -> Mapping[str, object]:
        if not isinstance(snapshot_ref, str):
            raise RuntimeError("expert Media Job wait snapshot ref is invalid")
        load = getattr(self._store, "get_immutable_payload", None)
        if not callable(load):
            raise RuntimeError("expert Media Job wait snapshot loader is unavailable")
        stored = load(turn_id, "expert-job-wait-snapshot-v1")
        if not isinstance(stored, tuple) or len(stored) != 2 or stored[0] != snapshot_ref:
            raise RuntimeError("expert Media Job wait snapshot identity drifted")
        payload = stored[1]
        if not isinstance(payload, Mapping):
            raise RuntimeError("expert Media Job wait snapshot is invalid")
        return payload

    def _load_terminal_snapshot(self, turn_id: str) -> Mapping[str, object]:
        load = getattr(self._store, "get_immutable_payload", None)
        if not callable(load):
            raise RuntimeError("expert Media Job terminal snapshot loader is unavailable")
        stored = load(turn_id, "expert-job-terminal-snapshot-v1")
        if not isinstance(stored, tuple) or len(stored) != 2 or not isinstance(stored[1], Mapping):
            raise RuntimeError("expert Media Job terminal snapshot is unavailable")
        return stored[1]

    def _wait_for_turn_terminal(self, turn_id: str, terminal: ExpertMediaJobTerminal) -> bool:
        lookup = getattr(self._store, "get_expert_job_wait", None)
        current = lookup(turn_id) if callable(lookup) else None
        return bool(
            isinstance(current, Mapping)
            and current.get("status") == "terminal_observed"
            and current.get("job_ref") == terminal.job_ref
            and current.get("terminal_job_revision") == terminal.job_revision
        )

    def _try_acquire(self, turn_id: str, now: datetime) -> RunLeaseToken | None:
        acquire = getattr(self._runtime, "try_acquire_run_lease", None)
        if not callable(acquire):
            raise RuntimeError("expert Media Job bridge lease acquisition is unavailable")
        token = acquire(turn_id, self._owner_id, now=now, stale_after=now + self._lease_ttl)
        if token is not None and not isinstance(token, RunLeaseToken):
            raise RuntimeError("expert Media Job bridge lease is invalid")
        return token

    def _release(self, lease: RunLeaseToken) -> None:
        release = getattr(self._runtime, "release_strict_run_lease", None)
        if not callable(release):
            raise RuntimeError("expert Media Job bridge lease release is unavailable")
        release(lease)
