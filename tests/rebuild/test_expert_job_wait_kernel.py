from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.expert_turn_binding_runtime import ExpertTurnBindingRuntime
from core.ai_kernel import (
    CapabilityDefinition,
    SQLiteAITurnStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    ToolExecutionBoundaryDecision,
    V1TurnPolicyCapabilityManifestResolver,
)
from core.ai_kernel.recovery import classify_recovery
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    default_video_research_expert_profile,
)
from core.job_runner import SQLiteJobStore
from core.media_hands import (
    MediaHandsPolicy, MediaHandsProvisioner, MediaOperationProfile,
    MediaResourceBudget, SourcePermissionSnapshot,
)
from backend.api.expert_media_job_wait_bridge import ExpertMediaJobWaitBridge
from backend.api.media_hands_composition import _terminal_snapshots_for_media_record
from core.source_processing import SourceManifestCodec


ROOT = Path(__file__).resolve().parents[2]
MODEL_ROUTE_REVISION = "a" * 64
EXPERT_TOOLS = ("analyze_source", "memory.recall", "document.draft.propose")


class _Provider:
    def __init__(self, store: SQLiteAITurnStore) -> None:
        self._store = store

    def invoke(self, request):
        result = {
            "status": "admitted",
            "job_id": "media-job-001",
            "canonical_job_ref": "crp://jobs/media-job-001",
            "job_revision": 7,
            "source_manifest_ref": "crp://source-manifests/source-001",
            "source_manifest_revision": "source-manifest-7",
        }
        receipt_ref = self._store.get_or_create_immutable_payload(
            str(request["turn_id"]),
            f"analyze-source-receipt:{request['tool_call_id']}",
            {
                "schema_version": "1.0.0",
                "turn_id": request["turn_id"],
                "tool_call_id": request["tool_call_id"],
                "tool_name": "analyze_source",
                "outcome": result,
            },
        )
        return {
            "summary": "governed media Job admitted",
            "result": result,
            "receipt_ref": receipt_ref,
        }


class _ExpertManifestResolver:
    def resolve(self, request, capabilities):
        manifest = V1TurnPolicyCapabilityManifestResolver().resolve(request, capabilities)
        return replace(
            manifest,
            model_routing_snapshot_ref=(
                f"crp://session/{request['turn_id']}/turn-model-routing-snapshot-v1/frozen"
            ),
            model_routing_snapshot_revision=MODEL_ROUTE_REVISION,
        )


class _WaitPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        self.calls += 1
        if self.calls == 1:
            return {"type": "tool", "capability_id": "analyze_source", "arguments": {}}
        if any(event.get("type") == "expert.job.observed" for event in events):
            return {
                "type": "complete",
                "summary": "expert media evidence observed",
                "evidence_refs": ["crp://receipts/media-job-001"],
            }
        return {
            "type": "wait_job",
            "job_ref": "crp://jobs/media-job-001",
            "observed_job_revision": 8,
            "summary": "waiting for governed media job",
        }


class _Boundary:
    def evaluate(self, request, capability, decision):
        return ToolExecutionBoundaryDecision(
            outcome="allow", reason_codes=("media.admitted",),
            matched_grant_ids=("grant-media-001",), policy_revision=3,
            requires_receipt=True, redaction_required=False,
            arguments=dict(decision.get("arguments", {})),
        )


def test_expert_job_wait_is_atomic_recoverable_and_cas_signalled(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path)
    request = _request()
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    first_planner = _WaitPlanner()
    runtime = _runtime(tmp_path, store, first_planner)

    receipt = runtime.submit_turn(request)

    assert receipt.status == "waiting_job"
    snapshot_ref, snapshot = store.get_immutable_payload(
        str(request["turn_id"]), "expert-job-wait-snapshot-v1",
    )
    assert snapshot_ref
    assert snapshot == {
        "schema_version": "1.0.0",
        "turn_id": request["turn_id"],
        "project_id": "project-alpha",
        "canonical_job_ref": "crp://jobs/media-job-001",
        "admission_job_revision": 7,
        "observed_job_revision": 8,
        "expert_binding_snapshot_ref": store.get_immutable_payload(
            str(request["turn_id"]), "expert-binding-snapshot-v1",
        )[0],
        "expert_binding_snapshot_id": store.get_immutable_payload(
            str(request["turn_id"]), "expert-binding-snapshot-v1",
        )[1]["snapshot_id"],
        "source_manifest_ref": "crp://source-manifests/source-001",
        "source_manifest_revision": "source-manifest-7",
        "permission_grant_ids": ["grant-media-001"],
        "boundary_admission_ref": _tool_event(events := tuple(store.events_after(str(request["turn_id"]))), "tool.requested")["data"]["payload_ref"],
        "outcome_ref": _tool_event(events, "tool.outcome.recorded")["data"]["payload_ref"],
        "admission_receipt_ref": _tool_event(events, "tool.completed")["data"]["receipt_ref"],
        "analyze_source_correlation": _tool_event(events, "tool.completed")["correlation"],
    }
    wait = store.get_expert_job_wait(str(request["turn_id"]))
    assert wait == {
        "turn_id": request["turn_id"],
        "job_ref": "crp://jobs/media-job-001",
        "admission_job_revision": 7,
        "snapshot_ref": snapshot_ref,
        "status": "waiting",
        "terminal_job_revision": None,
    }
    events = tuple(store.events_after(str(request["turn_id"])))
    assert events[-1]["type"] == "expert.job.waiting"
    assert sum(event["type"] == "tool.requested" for event in events) == 1
    assert classify_recovery(
        str(request["turn_id"]), 1, events, payload_loader=store.get,
    ).disposition == "waiting_noop"

    restarted_planner = _WaitPlanner()
    restarted = _runtime(tmp_path, SQLiteAITurnStore(tmp_path / "turns.sqlite3"), restarted_planner)
    replay = restarted.run_accepted_turn(str(request["turn_id"]))
    assert replay.status == "waiting_job"
    assert restarted_planner.calls == 0

    assert store.transition_expert_job_wait(
        str(request["turn_id"]), "crp://jobs/media-job-001", 7, expected_status="waiting",
        next_status="wake_enqueued", terminal_job_revision=9,
    ) is True
    assert store.transition_expert_job_wait(
        str(request["turn_id"]), "crp://jobs/media-job-001", 7, expected_status="wake_enqueued",
        next_status="terminal_observed", terminal_job_revision=9,
    ) is True
    assert store.get_expert_job_wait(str(request["turn_id"]))["status"] == "terminal_observed"


def test_forged_expert_job_wait_without_analyze_source_admission_is_rejected(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path)
    request = _request()
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    runtime = _runtime(tmp_path, store, _ForgedWaitPlanner())

    receipt = runtime.submit_turn(request)

    assert receipt.status == "failed"
    assert store.get_expert_job_wait(str(request["turn_id"])) is None
    assert not [
        event for event in store.events_after(str(request["turn_id"]))
        if event["type"] == "expert.job.waiting"
    ]


def test_terminal_job_observation_cas_keeps_admission_identity_and_revision(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path)
    request = _request()
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    assert _runtime(tmp_path, store, _WaitPlanner()).submit_turn(request).status == "waiting_job"

    assert store.transition_expert_job_wait(
        str(request["turn_id"]), "crp://jobs/media-job-001", 7,
        expected_status="waiting", next_status="wake_enqueued",
        terminal_job_revision=9,
    ) is True
    assert store.get_expert_job_wait(str(request["turn_id"]))["terminal_job_revision"] == 9
    assert store.transition_expert_job_wait(
        str(request["turn_id"]), "crp://jobs/media-job-001", 8,
        expected_status="wake_enqueued", next_status="terminal_observed", terminal_job_revision=9,
    ) is False


def test_terminal_finalize_crash_window_cannot_append_a_second_turn_terminal(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path)
    request = _request()
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    runtime = _runtime(tmp_path, store, _WaitPlanner())
    assert runtime.submit_turn(request).status == "waiting_job"
    now = datetime.now(timezone.utc)
    lease = runtime.try_acquire_run_lease(
        str(request["turn_id"]), "terminal-finalize-test", now=now,
        stale_after=now + timedelta(seconds=30),
    )
    assert lease is not None
    terminal = {
        "schema_version": "1.0.0", "turn_id": request["turn_id"],
        "canonical_job_ref": "crp://jobs/media-job-001", "terminal_job_id": "media_hands:media-job-001:analyze_source", "job_revision": 9,
        "status": "completed", "receipt_ref": "crp://receipts/media-job-001",
        "terminal_evidence": {"kind": "media_execution_receipt", "status": "completed", "job_id": "media_hands:media-job-001:analyze_source", "job_revision": 9, "execution_id": "execution-media-job-001"},
        "source_manifest_ref": "crp://source-manifests/source-001",
        "source_manifest_revision": "source-manifest-7",
    }
    runtime.record_expert_job_terminal(str(request["turn_id"]), terminal, lease)
    assert runtime.resume_expert_job_wait(str(request["turn_id"]), lease).status == "completed"
    runtime.release_strict_run_lease(lease)

    # This models a process dying after the atomic finalizer committed but
    # before the bridge could make its following best-effort CAS call.
    restarted = _runtime(tmp_path, SQLiteAITurnStore(tmp_path / "turns.sqlite3"), _WaitPlanner())
    later = datetime.now(timezone.utc)
    recovery_lease = restarted.try_acquire_run_lease(
        str(request["turn_id"]), "terminal-finalize-restart", now=later,
        stale_after=later + timedelta(seconds=30),
    )
    assert recovery_lease is not None
    with pytest.raises(Exception, match="wake is not ready"):
        restarted.resume_expert_job_wait(str(request["turn_id"]), recovery_lease)
    restarted.release_strict_run_lease(recovery_lease)
    events = tuple(store.events_after(str(request["turn_id"])))
    observed_index = next(
        index for index, event in enumerate(events)
        if event["type"] == "expert.job.observed"
    )
    observed_recovery = classify_recovery(
        str(request["turn_id"]), 2, events[: observed_index + 1],
        payload_loader=store.get,
    )
    assert observed_recovery.disposition == "safe_resume"
    assert sum(event["type"] == "turn.completed" for event in events) == 1
    assert sum(event["type"] == "expert.job.observed" for event in events) == 1
    assert store.get_expert_job_wait(str(request["turn_id"]))["status"] == "terminal_observed"


def test_real_sqlite_wait_keyset_pages_reach_130th_turn(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    connection = store._connect()
    try:
        connection.execute("BEGIN")
        for index in range(130):
            turn_id = f"page-turn-{index:03d}"
            ref = f"crp://session/{turn_id}/expert-job-wait-snapshot-v1/frozen"
            connection.execute(
                "INSERT INTO ai_turns(turn_id,session_id,operation_id,idempotency_key,request_json) VALUES(?,?,?,?,?)",
                (turn_id, "session-page", f"op-{index}", f"page-key-{index}", "{}"),
            )
            connection.execute(
                "INSERT INTO ai_turn_immutable_payloads(payload_ref,turn_id,kind,payload_json) VALUES(?,?,?,?)",
                (ref, turn_id, "expert-job-wait-snapshot-v1", "{}"),
            )
            connection.execute(
                "INSERT INTO ai_expert_job_waits(turn_id,job_ref,admission_job_revision,snapshot_ref,status,terminal_job_revision) VALUES(?,?,?,?,?,?)",
                (turn_id, "crp://jobs/media-job-001", 1, ref, "waiting", None),
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    first = store.list_expert_job_waits_after(after_turn_id=None, limit=64)
    second = store.list_expert_job_waits_after(after_turn_id=first[-1]["turn_id"], limit=64)
    third = store.list_expert_job_waits_after(after_turn_id=second[-1]["turn_id"], limit=64)
    assert [len(first), len(second), len(third)] == [64, 64, 2]
    assert third[-1]["turn_id"] == "page-turn-129"


@pytest.mark.parametrize("mutate", (
    lambda value: value.__setitem__("receipt_ref", "https://forged.example/receipt"),
    lambda value: value.__setitem__("receipt_ref", "crp://receipts/other-valid-looking-receipt"),
    lambda value: value.__setitem__("terminal_evidence", {"anything": "goes"}),
    lambda value: value.__setitem__("canonical_job_ref", "crp://jobs/other-job-001"),
    lambda value: value.__setitem__("job_revision", 0),
    lambda value: value.__setitem__("source_manifest_ref", "crp://source-manifests/other"),
))
def test_direct_terminal_record_attack_is_rejected_before_wake(tmp_path: Path, mutate) -> None:
    _bind_video_expert(tmp_path)
    request = _request()
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    runtime = _runtime(tmp_path, store, _WaitPlanner())
    assert runtime.submit_turn(request).status == "waiting_job"
    now = datetime.now(timezone.utc)
    lease = runtime.try_acquire_run_lease(str(request["turn_id"]), "direct-attack", now=now, stale_after=now + timedelta(seconds=30))
    assert lease is not None
    terminal = {
        "schema_version": "1.0.0", "turn_id": request["turn_id"],
        "canonical_job_ref": "crp://jobs/media-job-001", "terminal_job_id": "media_hands:media-job-001:analyze_source", "job_revision": 9,
        "status": "completed", "receipt_ref": "crp://receipts/media-job-001",
        "terminal_evidence": {"kind": "media_execution_receipt", "status": "completed", "job_id": "media_hands:media-job-001:analyze_source", "job_revision": 9, "execution_id": "execution-media-job-001"},
        "source_manifest_ref": "crp://source-manifests/source-001", "source_manifest_revision": "source-manifest-7",
    }
    mutate(terminal)
    with pytest.raises(Exception, match="terminal"):
        runtime.record_expert_job_terminal(str(request["turn_id"]), terminal, lease)
    runtime.release_strict_run_lease(lease)
    assert store.get_expert_job_wait(str(request["turn_id"]))["status"] == "waiting"
    assert not [event for event in store.events_after(str(request["turn_id"])) if event["type"] == "expert.job.terminal"]


def test_pre_effect_cancelled_media_job_authority_wakes_waiting_expert_without_provider(tmp_path: Path) -> None:
    _bind_video_expert(tmp_path)
    request = _request()
    turn_store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    runtime = _runtime(tmp_path, turn_store, _WaitPlanner())
    assert runtime.submit_turn(request).status == "waiting_job"
    job_store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    manifest = SourceManifestCodec.decode(json.loads((ROOT / "core-contracts/rebuild/source-processing/fixtures/bilibili-video.json").read_text(encoding="utf-8")))
    manifest = replace(manifest, source_id="media-job-001")
    budget = MediaResourceBudget(100, 100, 100, 0, 0, 0, 100)
    policy = MediaHandsPolicy(
        "policy-cancel-r1", {lane: 1 for lane in ("download", "asr", "vision", "model", "media_cpu")},
        {lane: 1 for lane in ("download", "asr", "vision", "model", "media_cpu")},
        {"analyze_source": MediaOperationProfile(("download",), budget)},
    )
    admitted = MediaHandsProvisioner(job_store, policy).provision(
        manifest=manifest, manifest_ref="crp://source-manifests/source-001",
        manifest_revision="source-manifest-7", operation="analyze_source",
        idempotency_key="cancel-before-effect-key-0001", created_at="2026-08-28T00:00:00Z",
        permission_snapshot=SourcePermissionSnapshot(
            project_id="project-alpha", manifest_ref="crp://source-manifests/source-001",
            manifest_revision="source-manifest-7", grant_ref="crp://grants/media-001",
            grant_revision="r1", revocation_generation=0,
        ),
    )
    # The admitted Tool snapshot observed revision 7. Advance harmless
    # pending metadata before cancellation so the real Job authority is a
    # later, still pre-effect terminal revision.
    pending = admitted.record
    for index in range(5):
        payload = dict(pending.payload)
        payload["updated_at"] = f"2026-08-28T00:00:0{index}Z"
        pending = job_store.save(payload, expected_revision=pending.revision)
    cancelled = job_store.request_cancel(pending.payload["id"], request_id="cancel-before-effect-001", now="2026-08-28T00:00:09Z")
    assert cancelled.payload["status"] == "cancelled" and cancelled.payload["lease"] is None
    bridge = ExpertMediaJobWaitBridge(
        turn_store, runtime, notify_wake=lambda *_args: None,
        continue_turn=lambda turn_id, _terminal, lease: runtime.resume_expert_job_wait(turn_id, lease),
    )
    snapshots = _terminal_snapshots_for_media_record(
        bridge, payload=cancelled.payload, revision=cancelled.revision,
        repository=SimpleNamespace(sqlite=job_store),
    )
    assert len(snapshots) == 1 and snapshots[0]["receipt_ref"] is None
    assert bridge.observe_terminal(snapshots[0]) is True
    assert turn_store.get_expert_job_wait(str(request["turn_id"]))["status"] == "terminal_observed"
    events = tuple(turn_store.events_after(str(request["turn_id"])))
    assert events[-1]["type"] == "turn.cancelled"
    assert not job_store.get_media_execution_receipt(str(admitted.record.payload["id"]), execution_id="cancel-before-effect-key-0001")


def _runtime(root: Path, store: SQLiteAITurnStore, planner: _WaitPlanner) -> SynchronousAIRuntime:
    runtime = SynchronousAIRuntime(
        planner=planner,
        registry=_registry(store),
        events=store,
        payloads=store,
        state=store,
        manifest_resolver=_ExpertManifestResolver(),
        expert_binding=ExpertTurnBindingRuntime(root),
        execution_boundary=_Boundary(),
    )
    runtime.configure_expert_job_terminal_verifier(
        lambda terminal: (
            terminal.get("terminal_job_id") == "media_hands:media-job-001:analyze_source"
            and terminal.get("receipt_ref") == "crp://receipts/media-job-001"
            and isinstance(terminal.get("terminal_evidence"), Mapping)
            and terminal["terminal_evidence"].get("execution_id") == "execution-media-job-001"
        )
    )
    return runtime


def _bind_video_expert(root: Path) -> None:
    catalog = ExpertCatalog(root)
    catalog.create(default_video_research_expert_profile() | {"status": "active"}, expected_registry_revision=0)
    ExpertProjectBindingStore(root).bind(
        "project-alpha", "video-research-expert", catalog=catalog,
        enabled_expert_revision=1, intent_affinity=["media_analysis"],
        selection_mode="auto", default=False, reason="test binding", expected_store_revision=0,
    )


def _registry(store: SQLiteAITurnStore) -> ScopedCapabilityRegistry:
    registry = ScopedCapabilityRegistry()
    for capability_id, version in (
        ("analyze_source", 3), ("memory.recall", 1),
        ("document.draft.propose", 4),
    ):
        registry.register(CapabilityDefinition(
            capability_id, version,
            "write" if capability_id == "analyze_source" else "read",
            False, "receipt_required",
            "crp://default/contracts/input", "crp://default/contracts/output",
        ), _Provider(store))
    return registry


def _request() -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request["turn_id"] = "expert-job-wait-turn"
    request["idempotency_key"] = "expert-job-wait-key"
    request["desired_outcome"] = "media_analysis"
    request["capability_policy"]["allowed"] = list(EXPERT_TOOLS)
    return request


def _tool_event(events, event_type):
    return next(event for event in events if event["type"] == event_type)


class _ForgedWaitPlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        return {
            "type": "wait_job",
            "job_ref": "crp://jobs/media-job-001",
            "observed_job_revision": 8,
        }
