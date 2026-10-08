from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Barrier, Thread

import pytest

from backend.api.ai_turn_recovery_worker import AIRecoveryWorker
from backend.api.expert_media_job_wait_bridge import (
    ExpertMediaJobWaitBridge,
    ExpertMediaJobTerminal,
    verify_expert_media_job_terminal,
)
from core.ai_kernel import RunLeaseToken, TurnReceipt


TURN_ID = "turn-0123456789abcdef0123456789abcdef"
JOB_REF = "crp://jobs/media-job-001"
SNAPSHOT_REF = f"crp://session/{TURN_ID}/expert-job-wait-snapshot-v1/one"


class _Store:
    def __init__(self) -> None:
        self.wait = {
            "turn_id": TURN_ID, "job_ref": JOB_REF,
            "admission_job_revision": 7, "snapshot_ref": SNAPSHOT_REF,
            "status": "waiting", "terminal_job_revision": None,
        }
        self.snapshot = {
            "schema_version": "1.0.0", "turn_id": TURN_ID,
            "project_id": "project-alpha", "canonical_job_ref": JOB_REF,
            "admission_job_revision": 7, "observed_job_revision": 8,
            "expert_binding_snapshot_ref": "crp://session/binding/one",
            "expert_binding_snapshot_id": "binding-one",
            "source_manifest_ref": "crp://source-manifests/source-001",
            "source_manifest_revision": "source-manifest-7",
            "permission_grant_ids": ["grant-1"],
            "boundary_admission_ref": "crp://boundary/admission-001",
            "outcome_ref": "crp://outcomes/outcome-001",
            "admission_receipt_ref": "crp://receipts/admission-001",
            "analyze_source_correlation": {"tool_call_id": "call-1"},
        }
        self.transitions = 0
        self.terminal_snapshot = None

    def get_expert_job_wait(self, turn_id):
        return dict(self.wait) if turn_id == TURN_ID else None

    def get_immutable_payload(self, turn_id, kind):
        assert turn_id == TURN_ID
        if kind == "expert-job-wait-snapshot-v1":
            return SNAPSHOT_REF, dict(self.snapshot)
        assert kind == "expert-job-terminal-snapshot-v1"
        assert self.terminal_snapshot is not None
        return "crp://session/terminal/one", dict(self.terminal_snapshot)

    def transition_expert_job_wait(self, turn_id, job_ref, revision, **kwargs):
        self.transitions += 1
        if turn_id != TURN_ID or job_ref != JOB_REF or revision != 7:
            return False
        expected, next_status, terminal_revision = (
            kwargs.get("expected_status"), kwargs.get("next_status"),
            kwargs.get("terminal_job_revision"),
        )
        if (
            expected != self.wait["status"]
            or terminal_revision != 9
            or (expected, next_status) not in {
                ("waiting", "wake_enqueued"), ("wake_enqueued", "terminal_observed"),
            }
        ):
            return False
        self.wait.update({"status": next_status, "terminal_job_revision": 9})
        return True


class _Runtime:
    def __init__(self, store) -> None:
        self.store = store
        self.leases: dict[str, RunLeaseToken] = {}
        self.acquires = 0
        self.releases = 0

    def try_acquire_run_lease(self, turn_id, owner_id, *, now, stale_after):
        self.acquires += 1
        if turn_id in self.leases:
            return None
        token = RunLeaseToken(turn_id, owner_id, 1)
        self.leases[turn_id] = token
        return token

    def release_strict_run_lease(self, token):
        self.releases += 1
        assert self.leases.pop(token.turn_id) == token

    def record_expert_job_terminal(self, turn_id, terminal, lease):
        assert self.leases[turn_id] == lease
        assert self.store.wait["status"] == "waiting"
        self.store.terminal_snapshot = dict(terminal)
        self.store.wait.update({"status": "wake_enqueued", "terminal_job_revision": terminal["job_revision"]})
        return "crp://session/terminal/one"


def _terminal(**override: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1.0.0", "turn_id": TURN_ID,
        "canonical_job_ref": JOB_REF, "terminal_job_id": "media_hands:media-job-001:analyze_source", "job_revision": 9, "status": "completed",
        "receipt_ref": "crp://receipts/media-job-001",
        "terminal_evidence": {"kind": "media_execution_receipt", "status": "completed", "job_id": "media_hands:media-job-001:analyze_source", "job_revision": 9, "execution_id": "execution-media-job-001"},
        "source_manifest_ref": "crp://source-manifests/source-001",
        "source_manifest_revision": "source-manifest-7",
    }
    value.update(override)
    return value


def test_verifier_requires_exact_terminal_schema_and_frozen_identity() -> None:
    store = _Store()
    verified = verify_expert_media_job_terminal(_terminal(), wait_snapshot=store.snapshot)
    assert verified == ExpertMediaJobTerminal(
        JOB_REF, 9, "completed", "crp://receipts/media-job-001",
        "crp://source-manifests/source-001", "source-manifest-7",
    )
    with pytest.raises(ValueError, match="schema"):
        verify_expert_media_job_terminal(_terminal(extra=True), wait_snapshot=store.snapshot)
    with pytest.raises(ValueError, match="drifted"):
        verify_expert_media_job_terminal(_terminal(source_manifest_revision="other"), wait_snapshot=store.snapshot)
    with pytest.raises(ValueError, match="drifted"):
        verify_expert_media_job_terminal(_terminal(job_revision=6), wait_snapshot=store.snapshot)


def test_terminal_observation_cas_notifies_once_and_releases_internal_lease() -> None:
    store = _Store(); runtime = _Runtime(store)
    observed: list[tuple[str, ExpertMediaJobTerminal]] = []
    bridge = ExpertMediaJobWaitBridge(store, runtime, notify_wake=lambda turn_id, terminal: observed.append((turn_id, terminal)))

    assert bridge.observe_terminal(_terminal()) is True
    assert bridge.observe_terminal(_terminal()) is False
    # The first state change is now the runtime-owned atomic terminal bundle;
    # the bridge itself only acknowledges terminal observation.
    assert store.transitions == 1
    assert [item[0] for item in observed] == [TURN_ID]
    assert runtime.acquires == 1 and runtime.releases == 1 and runtime.leases == {}


def test_two_bridges_receive_same_terminal_but_only_one_wins() -> None:
    store = _Store(); runtime = _Runtime(store)
    observed: list[str] = []
    first = ExpertMediaJobWaitBridge(store, runtime, notify_wake=lambda turn_id, _terminal: observed.append(turn_id))
    second = ExpertMediaJobWaitBridge(store, runtime, notify_wake=lambda turn_id, _terminal: observed.append(turn_id))
    barrier = Barrier(3)
    results: list[bool] = []

    def observe(bridge: ExpertMediaJobWaitBridge) -> None:
        barrier.wait()
        results.append(bridge.observe_terminal(_terminal()))

    threads = (Thread(target=observe, args=(first,)), Thread(target=observe, args=(second,)))
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(1)
    assert sorted(results) == [False, True]
    assert observed == [TURN_ID]


def test_callback_failure_leaves_enqueued_wake_for_startup_replay() -> None:
    store = _Store(); runtime = _Runtime(store)

    def fails_once(_turn_id, _terminal):
        raise RuntimeError("simulated callback crash")

    first = ExpertMediaJobWaitBridge(store, runtime, notify_wake=fails_once)
    with pytest.raises(RuntimeError, match="callback crash"):
        first.observe_terminal(_terminal())
    assert store.wait["status"] == "wake_enqueued"
    assert store.wait["terminal_job_revision"] == 9
    assert runtime.leases == {}

    observed: list[str] = []
    after_restart = ExpertMediaJobWaitBridge(
        store, runtime, notify_wake=lambda turn_id, _terminal: observed.append(turn_id),
    )
    assert after_restart.reconcile_startup(lambda: (_terminal(),)) == 1
    assert observed == [TURN_ID]
    assert store.wait["status"] == "terminal_observed"


def test_bridge_refuses_drift_before_lease_or_cas() -> None:
    store = _Store(); runtime = _Runtime(store)
    bridge = ExpertMediaJobWaitBridge(store, runtime, notify_wake=lambda *_args: pytest.fail("must not wake"))
    with pytest.raises(ValueError, match="drifted"):
        bridge.observe_terminal(_terminal(source_manifest_ref="crp://source-manifests/other"))
    assert store.transitions == 0 and runtime.acquires == 0


def test_startup_reconcile_uses_injected_query_and_never_executes_runtime() -> None:
    store = _Store(); runtime = _Runtime(store)
    observed: list[str] = []
    bridge = ExpertMediaJobWaitBridge(store, runtime, notify_wake=lambda turn_id, _terminal: observed.append(turn_id))
    query_calls = 0

    def query():
        nonlocal query_calls
        query_calls += 1
        return (_terminal(),)

    assert bridge.reconcile_startup(query) == 1
    assert query_calls == 1 and observed == [TURN_ID]
    assert not hasattr(runtime, "apply_action") and not hasattr(runtime, "run_accepted_turn")


def test_pending_wait_keyset_pages_reach_later_waits_without_revisiting_first_page() -> None:
    class PagedStore(_Store):
        def __init__(self):
            super().__init__()
            self.rows = tuple({"turn_id": f"turn-{index:03d}"} for index in range(130))
            self.cursors: list[str | None] = []

        def list_expert_job_waits_after(self, *, after_turn_id, limit):
            self.cursors.append(after_turn_id)
            return tuple(item for item in self.rows if after_turn_id is None or item["turn_id"] > after_turn_id)[:limit]

    store = PagedStore()
    bridge = ExpertMediaJobWaitBridge(store, _Runtime(store), notify_wake=lambda *_args: None)
    assert [item["turn_id"] for item in bridge.iter_pending_waits(batch_size=64)] == [
        f"turn-{index:03d}" for index in range(130)
    ]
    assert store.cursors == [None, "turn-063", "turn-127"]


def test_recovery_worker_accepts_injected_startup_reconciler_without_turn_execution() -> None:
    class RecoveryStore:
        def claim_safe_recovery_queue(self, **_kwargs):
            return ()

    class RecoveryRuntime:
        pass

    calls: list[str] = []
    worker = AIRecoveryWorker(
        RecoveryStore(), RecoveryRuntime(), startup_reconcile=lambda: calls.append("reconciled") or 0,
        lease_ttl=timedelta(milliseconds=20),
    )
    worker.start()
    for _ in range(50):
        if calls:
            break
        __import__("time").sleep(0.005)
    assert calls == ["reconciled"]
    assert worker.shutdown(timeout_seconds=0.5) is True


def test_runner_releases_lease_for_safe_waiting_job_receipt() -> None:
    from backend.api.ai_turn_runner import AITurnRunner

    class Runtime(_Runtime):
        def accept_turn(self, payload):
            return TurnReceipt(str(payload["turn_id"]), "session-test", "op-test", "accepted", 1, False)

        def run_accepted_turn(self, turn_id, _run_lease):
            return TurnReceipt(turn_id, "session-test", "op-test", "waiting_job", 2, False)

    runtime = Runtime(_Store())
    runner = AITurnRunner(runtime, max_workers=1)
    request = json.loads((Path(__file__).resolve().parents[4] / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    runner.accept_and_submit(request)
    for _ in range(50):
        if not runtime.leases:
            break
        __import__("time").sleep(0.005)
    assert runtime.leases == {}
    runner.shutdown()
