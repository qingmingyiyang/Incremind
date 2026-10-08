from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from backend.api.ai_runtime import build_ai_runtime
from backend.api.analyze_source_ai_runtime import analyze_source_capability_definition
from backend.api.media_hands_composition import (
    _terminal_snapshots_for_media_record,
    compose_application_media_hands,
)
from backend.model_route_context import model_route_provider_context_from_record
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.providers import ProviderRegistry
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_boundary import BoundaryGrant
from core.ai_tooling import tool_boundary_target_identity
from core.job_runner import SQLiteJobStore
from core.job_runner.media_execution_receipt import media_job_uri_segment
from core.media_hands import (
    MediaHandsPolicyAuthority,
    MediaOperationReceipt,
    default_personal_workbench_policy_snapshot,
)
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    default_video_research_expert_profile,
)
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import ModelRouteRuntimeService
from core.source_processing import (
    ManifestPermission,
    SourceManifestArtifactRepository,
    SourceManifestCodec,
    SourcePermissionAuthority,
)
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[4]


class _BlockingProvider:
    provider_id = "expert-media-vertical-provider"
    provider_revision = "fixture-r1"
    supported_platforms = frozenset({"bilibili"})

    def __init__(self, *, outcome: str = "completed") -> None:
        self.outcome = outcome
        self.calls = 0
        self.started = Event()
        self.release = Event()

    def execute(self, request):
        self.calls += 1
        self.started.set()
        assert self.release.wait(5), "fixture provider was not released"
        if request.control is not None:
            request.control.checkpoint()
        if self.outcome == "failed":
            raise RuntimeError("fixture provider failure")
        return MediaOperationReceipt(
            output={
                "kind": "document",
                "uri": f"crp://default/jobs/{request.job_id}/outputs/result",
                "object_id": "expert-media-result",
                "published": True,
            },
            checkpoint={
                "resume_step": "execute_operation",
                "checkpoint_uri": f"crp://default/jobs/{request.job_id}/checkpoints/final",
                "state_hash": "sha256:" + "a" * 64,
                "updated_at": "2026-08-28T00:00:00Z",
            },
            consumed={key: 0 for key in request.budget},
            execution_receipt_ref=(
                f"crp://default/jobs/{media_job_uri_segment(request.job_id)}/receipts/execution"
            ),
        )


class _Verifier:
    def __init__(self) -> None:
        self.calls = 0

    def assert_output_committed(self, *, output, request) -> None:
        del output, request
        self.calls += 1


class _UnexpectedPlatformProvider:
    def provide(self, text, *, project_id):
        raise AssertionError(f"expert must consume its frozen source ref: {text} {project_id}")


def _publish_policy(root: Path) -> None:
    snapshot = {
        **default_personal_workbench_policy_snapshot(),
        "enabled": True,
        "revision": "personal-workbench-r1",
    }
    MediaHandsPolicyAuthority(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    ).publish(snapshot, expected_revision=0, command_id="expert-media-policy-0001", actor="test-user", created_at="2026-08-28T00:00:00Z")


def _bind_expert(root: Path) -> None:
    catalog = ExpertCatalog(root)
    catalog.create(default_video_research_expert_profile() | {"status": "active"}, expected_registry_revision=0)
    ExpertProjectBindingStore(root).bind(
        "project-alpha", "video-research-expert", catalog=catalog,
        enabled_expert_revision=1, intent_affinity=["media_analysis"],
        selection_mode="auto", default=True, reason="fixture vertical",
        expected_store_revision=0,
    )


def _grant_analyze_source(root: Path) -> None:
    definition = analyze_source_capability_definition(available=True)
    assert definition.tool_definition is not None
    ProjectBoundaryProfileStore(root).create_grant(
        "project-alpha",
        expected_revision=1,
        grant=BoundaryGrant(
            grant_id="expert-media-analyze-source-grant", subject_id="ai-kernel",
            project_id="project-alpha",
            target_id=tool_boundary_target_identity(definition.tool_definition, "analyze_source"),
            actions=("write",), data_classes=(),
            destinations=("platform",), expires_at=None, revision=1,
        ),
    )


def _activate_model_route(root: Path) -> None:
    provider = ProviderRegistry(root).create({
        "provider_id": "expert-media-local", "name": "expert-media-local",
        "llm_provider": "openai", "base_url": "http://127.0.0.1:8317",
        "api_path": "/chat/completions", "model": "expert-media-model",
        "models": ["expert-media-model"], "enabled": True,
    }, fallback={})
    context = model_route_provider_context_from_record(root, provider)
    registry = ModelRouteRegistry(root)
    route = registry.update(
        "search.answer", {"provider_id": provider["provider_id"],
        "model_name": "expert-media-model", "adapter_kind": "openai-compatible",
        "enabled": True, "reason": "expert media vertical"},
        expected_registry_revision=0, provider=provider, egress_consented=True,
    )
    runtime = ModelRouteRuntimeService(root)
    preview = runtime.preview(route_keys=["search.answer"], compatibility={"search.answer": context}, providers=[context])
    runtime.activate(shadow_token=preview["shadow_token"], route_keys=preview["route_keys"], expected_runtime_revision=0, confirm=True, compatibility={"search.answer": context}, providers=[context])
    ModelRoutingProfileStore(root).update(
        expected_revision=1, rules_version=1, text_default_tier="standard",
        tier_routes={"fast": None, "standard": "search.answer", "deep": None, "vision": None, "image_generation": None},
    )
    ProjectCapabilityProfileStore(root).update(
        "project-alpha", expected_revision=0,
        boundary_profile_id="project-boundary-project-alpha", boundary_profile_revision=2,
    )


def _frozen_source(root: Path) -> str:
    store = JsonObjectStore(root / ".rebuild-data", namespace_id="default")
    fixture = SourceManifestCodec.decode(json.loads((ROOT / "core-contracts/rebuild/source-processing/fixtures/bilibili-video.json").read_text(encoding="utf-8")))
    evidence = "crp://default/evidence/projects/project-alpha/expert-media-r1"
    manifest = replace(
        fixture, platform="bilibili", source_id="expert-media-source", source_ref="crp://default/sources/expert-media-source",
        permission=ManifestPermission(decision="unknown", evidence_refs=(evidence,)),
        provenance_refs=(evidence,),
    )
    artifact = SourceManifestArtifactRepository(store, namespace_id="default").put(
        project_id="project-alpha", manifest_id="expert-media-source-r1", manifest=manifest,
    )
    SourcePermissionAuthority(store, namespace_id="default").grant(
        project_id="project-alpha", permission_id="expert-media-source-permission",
        source_id=manifest.source_id, platform=manifest.platform,
        source_manifest_ref=artifact.public_ref, source_manifest_revision=artifact.revision,
        metadata_evidence_ref=evidence, actor_id="test-user",
        command_id="grant-expert-media-source-0001", created_at="2026-08-28T00:00:01Z",
        expected_revision=0,
    )
    return artifact.public_ref


def _request(source_ref: str) -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts/ai/fixtures/turn-request/valid-project-answer.json").read_text(encoding="utf-8"))
    request.update({
        "turn_id": "expert-media-vertical-turn", "idempotency_key": "expert-media-vertical-key-0001",
        "desired_outcome": "media_analysis",
        "input": {"kind": "references", "text": None, "refs": [{
            "kind": "source_manifest", "object_id": "expert-media-source-r1", "uri": source_ref,
        }]},
        "expert_request": {"expert_id": "video-research-expert", "task_intents": ["media_analysis"], "budget": "normal"},
    })
    request["scope"]["project_id"] = "project-alpha"
    request["capability_policy"]["allowed"] = ["analyze_source", "memory.recall", "document.draft.propose"]
    request["privacy"] = {
        **request["privacy"], "mode": "remote_allowed", "allow_remote": True,
        "pii": "none",
    }
    return request


def _wait_for(predicate, *, message: str):
    deadline = time.monotonic() + 5
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(0.01)
        value = predicate()
    assert value, message
    return value


def _approve_expert_media_turn(runtime, turn_store, *, turn_id: str, suffix: str):
    """Approve the one frozen admission without duplicating its UI protocol."""
    approval = tuple(turn_store.events_after(turn_id))[-1]
    assert approval["type"] == "approval.required"
    return runtime.apply_action({
        "schema_version": "1.0.0", "action_id": f"expert-media-{suffix}-approve-0000000001",
        "turn_id": turn_id, "type": "approve", "target_event_id": approval["event_id"],
        "reason": "fixture approval", "actor": "user", "expected_sequence": approval["sequence"],
        "idempotency_key": f"expert-media-{suffix}-approve-key-0001",
        "created_at": "2026-08-28T00:00:02Z",
    })


@pytest.mark.parametrize(
    ("provider_outcome", "terminal_turn_type", "terminal_job_status", "verifier_calls"),
    (
        ("completed", "turn.completed", "completed", 1),
        ("failed", "turn.failed", "failed", 0),
    ),
)
def test_selected_video_expert_waits_for_one_real_media_job_then_wakes_same_turn(
    tmp_path: Path,
    provider_outcome: str,
    terminal_turn_type: str,
    terminal_job_status: str,
    verifier_calls: int,
) -> None:
    root = tmp_path.resolve()
    _publish_policy(root)
    _bind_expert(root)
    _grant_analyze_source(root)
    _activate_model_route(root)
    source_ref = _frozen_source(root)
    provider, verifier = _BlockingProvider(outcome=provider_outcome), _Verifier()
    container = SimpleNamespace(
        root_dir=root, media_operation_provider=provider,
        media_output_verifier=verifier,
        platform_manifest_providers={"bilibili": _UnexpectedPlatformProvider()},
    )
    application = SimpleNamespace(state=SimpleNamespace(container=container))

    # This is the production composition order: Hands first, then AI attaches
    # the one bridge to the already-singular lifecycle.
    compose_application_media_hands(application, container)
    runtime = build_ai_runtime(container, application=application)
    failures: list[Exception] = []
    original_fail = runtime._fail
    runtime._fail = lambda turn_id, error: (failures.append(error), original_fail(turn_id, error))[1]
    receipt = runtime.submit_turn(_request(source_ref))
    events_at_failure = tuple(application.state.ai_turn_store.events_after("expert-media-vertical-turn"))
    assert receipt.status == "waiting_approval"
    approval = events_at_failure[-1]
    assert approval["type"] == "approval.required"
    receipt = runtime.apply_action({
        "schema_version": "1.0.0", "action_id": "expert-media-approve-0000000000000001",
        "turn_id": "expert-media-vertical-turn", "type": "approve",
        "target_event_id": approval["event_id"], "reason": "fixture approval",
        "actor": "user", "expected_sequence": approval["sequence"],
        "idempotency_key": "expert-media-approve-key-0001", "created_at": "2026-08-28T00:00:02Z",
    })
    events_after_approval = tuple(application.state.ai_turn_store.events_after("expert-media-vertical-turn"))
    completed_tool = next(event for event in events_after_approval if event["type"] == "tool.completed")
    assert receipt.status == "waiting_job", failures
    assert provider.started.wait(5)

    turn_store = application.state.ai_turn_store
    wait = turn_store.get_expert_job_wait("expert-media-vertical-turn")
    assert wait is not None and wait["status"] == "waiting"
    job_id = next(iter(SQLiteJobStore(root / ".rebuild-data" / "jobs.sqlite3").all())).payload["id"]
    assert provider.calls == 1
    if provider_outcome == "completed":
        # Simulate the process-local callback dying after the durable terminal
        # bundle commits. The next application builds fresh runtime objects
        # against the same durable Job/Turn stores.
        application.state.expert_media_job_wait_bridge._notify_wake = (
            lambda *_args: (_ for _ in ()).throw(RuntimeError("callback crash"))
        )
    provider.release.set()
    if provider_outcome == "completed":
        _wait_for(
            lambda: turn_store.get_expert_job_wait("expert-media-vertical-turn")["status"] == "wake_enqueued",
            message="callback crash did not retain the durable wake",
        )
        rebuilt_application = SimpleNamespace(state=SimpleNamespace(container=container))
        compose_application_media_hands(rebuilt_application, container)
        build_ai_runtime(container, application=rebuilt_application)
        assert rebuilt_application.state.expert_media_job_wait_reconcile() == 1
    _wait_for(
        lambda: tuple(turn_store.events_after("expert-media-vertical-turn"))[-1]["type"] == terminal_turn_type,
        message="expert turn did not finish",
    )
    _wait_for(
        lambda: turn_store.get_expert_job_wait("expert-media-vertical-turn")["status"] == "terminal_observed",
        message="terminal media result did not wake the same turn",
    )
    events = tuple(turn_store.events_after("expert-media-vertical-turn"))
    assert events[-1]["type"] == terminal_turn_type, events
    assert sum(event["type"] == "tool.completed" for event in events) == (
        2 if provider_outcome == "completed" else 1
    )
    assert sum(event["type"] == terminal_turn_type for event in events) == 1
    assert sum(event["type"] == "expert.memory.proposed" for event in events) == (
        1 if provider_outcome == "completed" else 0
    )
    assert provider.calls == 1 and verifier.calls == verifier_calls
    job = SQLiteJobStore(root / ".rebuild-data" / "jobs.sqlite3").read(job_id)
    assert job is not None and job.payload["status"] == terminal_job_status
    binding = turn_store.get_immutable_payload("expert-media-vertical-turn", "expert-binding-snapshot-v1")
    assert binding is not None and binding[1]["project_id"] == "project-alpha"
    execution = turn_store.get_immutable_payload(
        "expert-media-vertical-turn", "expert-execution-receipt-v1",
    )
    result = turn_store.get_immutable_payload(
        "expert-media-vertical-turn", "expert-result-v1",
    )
    if provider_outcome == "completed":
        assert execution is not None and result is not None
        assert execution[1]["snapshot_id"] == binding[1]["snapshot_id"]
        assert execution[1]["output_refs"] == [result[0]]
        assert len(execution[1]["tool_invocation_refs"]) == 2
        assert result[1]["media_terminal_ref"].startswith(
            "crp://session/expert-media-vertical-turn/"
        )
        proposal_receipt = turn_store.get_immutable_payload(
            "expert-media-vertical-turn", "expert-memory-proposal-v1",
        )
        assert proposal_receipt is not None
        assert proposal_receipt[1]["status"] == "pending_review"
        assert proposal_receipt[1]["memory_publication_state"] == "not_published"
        object_store = JsonObjectStore(root / ".rebuild-data", namespace_id="default")
        proposal = object_store.read(
            "external_agent_proposals", proposal_receipt[1]["proposal_id"],
        )
        candidate = object_store.read(
            "memory_candidates", proposal_receipt[1]["memory_candidate_id"],
        )
        assert proposal is not None and proposal["review"]["state"] == "pending_review"
        assert candidate is not None and candidate["status"] == "pending_review"
        assert proposal["memory_publication"] == "not_started"
        projection = runtime.execution_projection_for(
            "expert-media-vertical-turn", "simple",
        )
        assert projection["expert"] == {
            "selected_expert": "video-research-expert",
            "selection_reason": "由你明确指定",
            "current_phase": "completed",
            "evidence_source_count": len(execution[1]["input_evidence_refs"]),
            "receipt_status": "completed",
        }
    else:
        assert execution is None and result is None
        assert turn_store.get_immutable_payload(
            "expert-media-vertical-turn", "expert-memory-proposal-v1",
        ) is None
        object_store = JsonObjectStore(root / ".rebuild-data", namespace_id="default")
        assert object_store.list("external_agent_proposals") == ()
        assert object_store.list("memory_candidates") == ()


def test_cancelled_media_job_rebuilds_after_callback_crash_without_provider_or_success_receipt(
    tmp_path: Path,
) -> None:
    """A pre-effect cancellation is still a real terminal Job wake after restart."""
    root = tmp_path.resolve()
    _publish_policy(root)
    _bind_expert(root)
    _grant_analyze_source(root)
    _activate_model_route(root)
    provider = _BlockingProvider()
    container = SimpleNamespace(
        root_dir=root, media_operation_provider=provider, media_output_verifier=_Verifier(),
        platform_manifest_providers={"bilibili": _UnexpectedPlatformProvider()},
    )
    application = SimpleNamespace(state=SimpleNamespace(container=container))
    compose_application_media_hands(application, container)
    runtime = build_ai_runtime(container, application=application)
    # Keep the production lifecycle composed but prevent dispatch until the
    # user cancellation is durably recorded.  This proves no Provider effect
    # was even started, rather than cancelling an already-running Provider.
    application.state.rebuild_job_lifecycle.enqueue_durable = lambda _job_id: True
    source_ref = _frozen_source(root)
    assert runtime.submit_turn(_request(source_ref)).status == "waiting_approval"
    turn_store = application.state.ai_turn_store
    assert _approve_expert_media_turn(
        runtime, turn_store, turn_id="expert-media-vertical-turn", suffix="cancel",
    ).status == "waiting_job"
    job_store = SQLiteJobStore(root / ".rebuild-data" / "jobs.sqlite3")
    job_id = next(iter(job_store.all())).payload["id"]
    cancelled = job_store.request_cancel(
        job_id, request_id="expert-media-cancel-before-effect-0001",
        now="2026-08-28T00:00:09Z",
    )
    assert cancelled.payload["status"] == "cancelled"
    assert provider.calls == 0

    # This is the real app lifecycle terminal callback.  It crashes only
    # after the Kernel stored terminal evidence and wake_enqueued atomically.
    application.state.expert_media_job_wait_bridge._notify_wake = (
        lambda *_args: (_ for _ in ()).throw(RuntimeError("fixture callback crash"))
    )
    with pytest.raises(RuntimeError, match="callback crash"):
        application.state.rebuild_job_lifecycle._terminal_observer(cancelled)
    assert turn_store.get_expert_job_wait("expert-media-vertical-turn")["status"] == "wake_enqueued"

    rebuilt_application = SimpleNamespace(state=SimpleNamespace(container=container))
    compose_application_media_hands(rebuilt_application, container)
    build_ai_runtime(container, application=rebuilt_application)
    assert rebuilt_application.state.expert_media_job_wait_reconcile() == 1
    _wait_for(
        lambda: turn_store.get_expert_job_wait("expert-media-vertical-turn")["status"] == "terminal_observed",
        message="cancelled expert wait did not reconcile after application rebuild",
    )
    events = tuple(turn_store.events_after("expert-media-vertical-turn"))
    assert [event["type"] for event in events].count("turn.cancelled") == 1
    assert not [event for event in events if event["type"] == "turn.completed"]
    terminal = turn_store.get_immutable_payload(
        "expert-media-vertical-turn", "expert-job-terminal-snapshot-v1",
    )
    assert terminal is not None and terminal[1]["status"] == "cancelled"
    assert terminal[1]["receipt_ref"] is None
    job = job_store.read(job_id)
    assert job is not None and job.payload["published_outputs"] == []
    assert job_store.get_media_execution_receipt(job_id, execution_id=job.payload["idempotency_key"]) is None
    assert provider.calls == 0
    object_store = JsonObjectStore(root / ".rebuild-data", namespace_id="default")
    assert object_store.list("external_agent_proposals") == ()
    assert object_store.list("memory_candidates") == ()
    # A second recovery scan must be a no-op and cannot append another Turn terminal.
    assert rebuilt_application.state.expert_media_job_wait_reconcile() == 0
    assert [event["type"] for event in turn_store.events_after("expert-media-vertical-turn")].count("turn.cancelled") == 1


def test_one_media_terminal_fans_out_to_two_waiting_turns_with_one_receipt_and_no_replay(
    tmp_path: Path,
) -> None:
    """Composition projects one verified Media terminal to every frozen wait."""
    root = tmp_path.resolve()
    _publish_policy(root)
    _bind_expert(root)
    _grant_analyze_source(root)
    _activate_model_route(root)
    source_ref = _frozen_source(root)
    provider, verifier = _BlockingProvider(), _Verifier()
    container = SimpleNamespace(
        root_dir=root, media_operation_provider=provider, media_output_verifier=verifier,
        platform_manifest_providers={"bilibili": _UnexpectedPlatformProvider()},
    )
    application = SimpleNamespace(state=SimpleNamespace(container=container))
    compose_application_media_hands(application, container)
    runtime = build_ai_runtime(container, application=application)
    turn_store = application.state.ai_turn_store
    assert runtime.submit_turn(_request(source_ref)).status == "waiting_approval"
    assert _approve_expert_media_turn(
        runtime, turn_store, turn_id="expert-media-vertical-turn", suffix="fanout-primary",
    ).status == "waiting_job"
    assert provider.started.wait(5)
    primary_wait = turn_store.get_expert_job_wait("expert-media-vertical-turn")
    assert primary_wait is not None and primary_wait["status"] == "waiting"

    # Model the internal projection after a shared Job admission has already
    # been governed. The continuation still freezes its own expert binding and
    # Context before it can complete; it never creates a second Provider call.
    secondary_id = "expert-media-fanout-secondary-turn"
    secondary_request = _request(source_ref)
    secondary_request.update({
        "turn_id": secondary_id,
        "idempotency_key": "expert-media-fanout-secondary-key-0001",
    })
    assert runtime.accept_turn(secondary_request).status == "accepted"
    primary_snapshot = turn_store.get_immutable_payload(
        "expert-media-vertical-turn", "expert-job-wait-snapshot-v1",
    )
    primary_binding = turn_store.get_immutable_payload(
        "expert-media-vertical-turn", "expert-binding-snapshot-v1",
    )
    assert primary_snapshot is not None and primary_binding is not None
    secondary_snapshot = dict(primary_snapshot[1])
    secondary_snapshot.update({
        "turn_id": secondary_id,
        "expert_binding_snapshot_ref": primary_binding[0],
        "expert_binding_snapshot_id": primary_binding[1]["snapshot_id"],
    })
    waiting_event = runtime._new_event(
        secondary_id, "expert.job.waiting", "waiting_job", "shared Media Job wait frozen",
    )
    turn_store.append_expert_job_wait_bundle(
        waiting_event, expected_sequence=int(waiting_event["sequence"]) - 1,
        immutable_kind="expert-job-wait-snapshot-v1", immutable_payload=secondary_snapshot,
        job_ref=primary_wait["job_ref"], admission_job_revision=primary_wait["admission_job_revision"],
    )
    assert turn_store.get_expert_job_wait(secondary_id)["status"] == "waiting"
    assert len(tuple(SQLiteJobStore(root / ".rebuild-data" / "jobs.sqlite3").all())) == 1

    provider.release.set()
    for turn_id in ("expert-media-vertical-turn", secondary_id):
        _wait_for(
            lambda turn_id=turn_id: turn_store.get_expert_job_wait(turn_id)["status"] == "terminal_observed",
            message=f"{turn_id} did not observe the shared Media terminal",
        )
    _wait_for(
        lambda: any(
            event["type"] == "turn.completed"
            for event in turn_store.events_after("expert-media-vertical-turn")
        ),
        message="valid primary expert consumer did not finish synthesis continuation",
    )
    primary_events = tuple(turn_store.events_after("expert-media-vertical-turn"))
    secondary_events = tuple(turn_store.events_after(secondary_id))
    assert [event["type"] for event in primary_events].count("turn.completed") == 1
    assert [event["type"] for event in secondary_events].count("expert.job.observed") == 1
    assert [event["type"] for event in secondary_events].count("turn.completed") == 1
    primary_terminal = turn_store.get_immutable_payload(
        "expert-media-vertical-turn", "expert-job-terminal-snapshot-v1",
    )
    secondary_terminal = turn_store.get_immutable_payload(
        secondary_id, "expert-job-terminal-snapshot-v1",
    )
    assert primary_terminal is not None and secondary_terminal is not None
    assert primary_terminal[1]["receipt_ref"] == secondary_terminal[1]["receipt_ref"]
    assert provider.calls == 1 and verifier.calls == 1
    # The canonical production scanner reads _terminal_snapshots_for_media_record
    # only for unresolved waits.  Once both are observed, replay is inert.
    job = next(iter(SQLiteJobStore(root / ".rebuild-data" / "jobs.sqlite3").all()))
    assert _terminal_snapshots_for_media_record(
        application.state.expert_media_job_wait_bridge,
        payload=job.payload, revision=job.revision,
        repository=SimpleNamespace(sqlite=SQLiteJobStore(root / ".rebuild-data" / "jobs.sqlite3")),
    ) == ()
    assert application.state.expert_media_job_wait_reconcile() == 0
    for turn_id in ("expert-media-vertical-turn", secondary_id):
        assert [event["type"] for event in turn_store.events_after(turn_id)].count("turn.completed") == 1
