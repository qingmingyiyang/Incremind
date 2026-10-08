from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.api.context_benchmark_observation import (
    context_benchmark_turn_identity,
)
from backend.api.context_benchmark_suite import (
    ContextBenchmarkSuiteArtifactService,
    ContextBenchmarkSuiteError,
)
from backend.model_route_context import (
    model_route_provider_context_from_record,
    provider_egress_manifest,
)
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.model_routing_snapshot import (
    project_turn_model_routing_snapshot,
    turn_model_routing_snapshot_revision,
)
from backend.providers import ProviderRegistry
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.provider_egress import ProviderEgressPolicyStore
from core.ai_kernel import InMemoryTurnPayloadStore, InMemoryTurnStateStore
from core.capability_packages.thought_graph_context import (
    build_model_benchmark_cases,
    build_model_benchmark_turn_pair,
    score_model_benchmark,
    score_model_benchmark_suite,
)
from core.context_graph import FrozenContextRevisions
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import ModelRouteRuntimeService


class _Events:
    def __init__(self) -> None:
        self._events: dict[str, tuple[dict[str, object], ...]] = {}

    def put(self, turn_id: str, events: tuple[dict[str, object], ...]) -> None:
        self._events[turn_id] = events

    def events_after(self, turn_id: str, after_sequence: int = 0):
        assert after_sequence == 0
        return self._events.get(turn_id, ())


class _Secrets:
    def get(self, _key: str) -> str:
        return "unused"


def _activate_route(root) -> None:
    provider = ProviderRegistry(root).create({
        "provider_id": "provider-a",
        "name": "provider-a",
        "llm_provider": "openai",
        "base_url": "https://api.example.test",
        "api_path": "/chat/completions",
        "model": "model-a",
        "models": ["model-a"],
        "enabled": True,
    }, fallback={})
    policy = ProviderEgressPolicyStore(root)
    manifest = provider_egress_manifest(provider, policy)
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    provider_context = model_route_provider_context_from_record(root, provider)
    registry = ModelRouteRegistry(root)
    registry.update(
        "search.answer",
        {
            "provider_id": "provider-a",
            "model_name": "model-a",
            "adapter_kind": "openai-compatible",
            "enabled": True,
            "reason": "LineMap benchmark suite unit route",
        },
        expected_registry_revision=0,
        provider=provider,
        egress_consented=True,
    )
    runtime = ModelRouteRuntimeService(root)
    compatibility = {"search.answer": provider_context}
    shadow = runtime.preview(
        route_keys=["search.answer"],
        compatibility=compatibility,
        providers=[provider_context],
    )
    runtime.activate(
        shadow_token=shadow["shadow_token"],
        route_keys=shadow["route_keys"],
        expected_runtime_revision=0,
        confirm=True,
        compatibility=compatibility,
        providers=[provider_context],
    )
    ModelRoutingProfileStore(root).update(
        expected_revision=1,
        rules_version=1,
        text_default_tier="standard",
        tier_routes={
            "fast": None,
            "standard": "search.answer",
            "deep": None,
            "vision": None,
            "image_generation": None,
        },
    )
    ProjectCapabilityProfileStore(root).update(
        "project-a",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        preferred_model_tier="standard",
    )


def _routing_snapshot(root, request: dict[str, object]) -> dict[str, object]:
    turn_input = request["input"]
    assert isinstance(turn_input, dict)
    return project_turn_model_routing_snapshot(
        SimpleNamespace(root_dir=root, secret_store=_Secrets()),
        turn_id=str(request["turn_id"]),
        project_id="project-a",
        required_capability="structured",
        modality="text",
        output_contract="json_object",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
        privacy_scope="remote_allowed",
        context_policy=request["context_policy"],
        input_refs=turn_input["refs"],
    )


def _output(case_id: str, *, correct: bool) -> str:
    if case_id == "project_skill":
        return json.dumps({
            "proposal_status": "proposal_only" if correct else "direct_publish",
            "rules": [
                "proposal-only Gate Effect Runner source revision failure condition"
                if correct else "publish"
            ],
            "source_refs": [
                "source:requirement", "source:architecture", "source:decision",
            ] if correct else ["source:unknown"],
            "excluded_option_ids": ["rejected_direct_write" if correct else "other"],
            "usage_boundaries": [
                "Formal writes require Gate and Effect Runner" if correct else "none"
            ],
            "failure_conditions": ["failure condition" if correct else "unknown"],
            "validation_steps": ["validate source revision" if correct else "inspect"],
            "maintenance_actions": [
                "platform_evidence", "decision", "skill_proposal",
            ] if correct else ["rewrite_everything"],
        }, separators=(",", ":"))
    if case_id == "document":
        return json.dumps({
            "paragraphs": [
                {
                    "paragraph_id": "paragraph_a",
                    "text": (
                        "Paragraph A: Evidence A no longer verifies Fact A."
                        if correct else "Paragraph A uses Evidence A."
                    ),
                    "source_refs": ["source:a", "source:a-evidence"] if correct else ["source:b"],
                },
                {
                    "paragraph_id": "paragraph_b",
                    "text": "Paragraph B follows only from Fact B.",
                    "source_refs": ["source:b"],
                },
            ],
            "affected_paragraph_ids": ["paragraph_a" if correct else "paragraph_b"],
            "regeneration_order": ["paragraph_a" if correct else "paragraph_b"],
            "unchanged_paragraph_ids": ["paragraph_b" if correct else "paragraph_a"],
        }, separators=(",", ":"))
    return json.dumps({
        "supported_hypothesis": "Hypothesis A" if correct else "Hypothesis B",
        "evidence_refs": ["source:evidence-a", "source:open"] if correct else ["source:wrong"],
        "excluded_claim_ids": ["wrong_evidence" if correct else "evidence_a"],
        "stale_conclusion_ids": ["stale_conclusion" if correct else "synthesis"],
        "replay_order": ["stale_conclusion", "synthesis"] if correct else ["synthesis", "stale_conclusion"],
        "open_questions": ["external replication remains open"],
        "included_node_ids": [
            "evidence_a", "open_question", "stale_conclusion", "synthesis",
        ] if correct else ["wrong_evidence"],
        "budget_note": "No selected conclusion was trimmed.",
        "selection_explanation": (
            "The excluded branch is removed and the stale conclusion is replayed."
            if correct else "All branches were accepted."
        ),
    }, separators=(",", ":"))


def _seed_terminal_evidence(
    *,
    request: dict[str, object],
    root,
    payloads: InMemoryTurnPayloadStore,
    events: _Events,
) -> None:
    _request, identity = context_benchmark_turn_identity(request)
    correct = identity.variant == "linemap"
    input_tokens, output_tokens = ((70, 20) if correct else (100, 20))
    turn_id = identity.turn_id
    model_request_id = f"model-request-{turn_id}"
    snapshot = _routing_snapshot(root, request)
    selected = snapshot["selected"]
    assert isinstance(selected, dict)
    snapshot_revision = turn_model_routing_snapshot_revision(snapshot)
    routing_ref = payloads.put(turn_id, "model-routing", snapshot)
    receipt_ref = payloads.put(turn_id, "model-receipt", {
        "schema_version": "1.0.0",
        "receipt_id": f"model-receipt-{turn_id}",
        "turn_id": turn_id,
        "model_request_id": model_request_id,
        "status": "completed",
        "requested_at": "2026-08-30T00:00:00+00:00",
        "completed_at": "2026-08-30T00:00:01+00:00",
        "duration_ms": 1000,
        "provider_id": selected["provider_id"],
        "model_id": selected["model_name"],
        "usage_status": "recorded",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "input_recorded": False,
        "output_recorded": False,
        "error_code": None,
    })
    attempt_ref = payloads.put(turn_id, "model-wire-attempt-receipt", {
        "schema_version": "1.0.0",
        "attempt_id": f"model-wire-attempt-{turn_id}",
        "turn_id": turn_id,
        "model_request_id": model_request_id,
        "attempt_number": 1,
        "routing_snapshot_revision": snapshot_revision,
        "provider_id": selected["provider_id"],
        "model_id": selected["model_name"],
        "execution_location": selected["execution_location"],
        "status": "succeeded",
        "started_at": "2026-08-30T00:00:00+00:00",
        "completed_at": "2026-08-30T00:00:01+00:00",
        "duration_ms": 1000,
        "usage_status": "reported",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "cache_status": "unavailable",
        "cache_metadata": None,
        "input_stored": False,
        "output_stored": False,
        "error_code": None,
    })
    correlation = {
        "step_id": f"step-{turn_id}",
        "model_request_id": model_request_id,
    }
    events.put(turn_id, (
        {
            "event_id": f"event-model-{turn_id}",
            "turn_id": turn_id,
            "type": "model.completed",
            "correlation": correlation,
            "data": {
                "receipt_ref": receipt_ref,
                "evidence_refs": [routing_ref, attempt_ref],
            },
        },
        {
            "event_id": f"event-terminal-{turn_id}",
            "turn_id": turn_id,
            "type": "turn.completed",
            "correlation": correlation,
            "data": {"summary": _output(identity.case_id, correct=correct)},
        },
    ))


def _suite(
    tmp_path,
    *,
    provider_revision: str | None = None,
    capability_id: str = "thought_graph_context",
    capability_revision: str = "4.0.0",
    service_capability_id: str | None = None,
    case_builder=build_model_benchmark_cases,
    case_scorer=score_model_benchmark,
    suite_scorer=score_model_benchmark_suite,
):
    _activate_route(tmp_path)
    state = InMemoryTurnStateStore()
    payloads = InMemoryTurnPayloadStore()
    events = _Events()
    suite_run_id = "suite-persist-a"
    turn_ids = []
    route_probe = _routing_snapshot(tmp_path, {
        "turn_id": "turn-route-probe",
        "input": {"refs": []},
        "context_policy": {
            "include_project_skill": False,
            "include_memory": False,
            "include_session_history": False,
            "max_context_bytes": 262144,
        },
    })
    selected = route_probe["selected"]
    boundary = route_probe["boundary"]
    assert isinstance(selected, dict) and isinstance(boundary, dict)
    frozen = FrozenContextRevisions(
        capability_revision,
        str(boundary["profile_revision"]),
        provider_revision or str(selected["provider_revision"]),
        str(selected["route_revision"]),
        "2.0.0",
    )
    for case in build_model_benchmark_cases():
        pair = build_model_benchmark_turn_pair(
            case,
            project_id="project-a",
            session_id="session-linemap-benchmark",
            linear_turn_id=f"turn-{case.case_id}-linear",
            linemap_turn_id=f"turn-{case.case_id}-linemap",
            binding_id=f"binding-{case.case_id}-r1",
            suite_run_id=suite_run_id,
            revisions=frozen,
            created_at="2026-08-30T00:00:00Z",
            consent_refs=("crp://consents/project-a/benchmark-r1",),
        )
        state.claim_turn(pair.linear_turn)
        state.claim_turn(pair.linemap_turn)
        turn_ids.extend((
            str(pair.linear_turn["turn_id"]),
            str(pair.linemap_turn["turn_id"]),
        ))
        payloads.get_or_create_immutable_payload(
            str(pair.linemap_turn["turn_id"]),
            "context-binding-v1",
            {
                "schema_version": "1.0.0",
                "binding_id": pair.binding_creation["binding_id"],
                "project_id": pair.binding_creation["project_id"],
                "capability_id": capability_id,
                "capability_revision": pair.binding_creation["capability_revision"],
                "registry_revision": 1,
                "binding": pair.binding_creation["binding"],
            },
        )
        _seed_terminal_evidence(
            request=pair.linear_turn,
            root=tmp_path,
            payloads=payloads,
            events=events,
        )
        _seed_terminal_evidence(
            request=pair.linemap_turn,
            root=tmp_path,
            payloads=payloads,
            events=events,
        )
    service = ContextBenchmarkSuiteArtifactService(
        state=state,
        events=events,
        payloads=payloads,
        capability_id=service_capability_id or capability_id,
        capability_revision=capability_revision,
        case_builder=case_builder,
        case_scorer=case_scorer,
        suite_scorer=suite_scorer,
    )
    return service, payloads, suite_run_id, tuple(turn_ids)


def test_suite_service_reloads_six_turns_and_persists_one_immutable_artifact(
    tmp_path,
) -> None:
    service, payloads, suite_run_id, turn_ids = _suite(tmp_path)
    result = service.finalize(
        suite_run_id=suite_run_id,
        turn_ids=turn_ids,
        coordinator_turn_id=turn_ids[0],
    )

    assert result.suite.native_canvas_eligible is True
    assert len(result.observations) == 6
    artifact = payloads.get(result.artifact_ref)
    assert artifact["schema_version"] == "1.0.0"
    assert artifact["suite_run_id"] == suite_run_id
    assert len(artifact["observations"]) == 6
    assert artifact["suite_result"]["all_case_gates_passed"] is True
    assert {item.execution_location for item in result.observations} == {"remote"}
    assert {item["execution_location"] for item in artifact["observations"]} == {"remote"}

    replay = service.finalize(
        suite_run_id=suite_run_id,
        turn_ids=turn_ids,
        coordinator_turn_id=turn_ids[0],
    )
    assert replay.artifact_ref == result.artifact_ref


def test_suite_service_fails_closed_on_binding_and_model_revision_drift(tmp_path) -> None:
    service, _payloads, suite_run_id, turn_ids = _suite(
        tmp_path, provider_revision="provider-r3",
    )

    with pytest.raises(ContextBenchmarkSuiteError, match="revision drifted"):
        service.finalize(
            suite_run_id=suite_run_id,
            turn_ids=turn_ids,
            coordinator_turn_id=turn_ids[0],
        )


def test_suite_service_is_generic_when_a_capability_definition_is_injected(tmp_path) -> None:
    service, _payloads, suite_run_id, turn_ids = _suite(
        tmp_path,
        capability_id="example_context",
        case_builder=lambda: build_model_benchmark_cases(),
        case_scorer=lambda case, linear, graph_context, *, suite_run_id: (
            score_model_benchmark(case, linear, graph_context, suite_run_id=suite_run_id)
        ),
        suite_scorer=lambda suite_run_id, results: score_model_benchmark_suite(
            suite_run_id, results,
        ),
    )

    result = service.finalize(
        suite_run_id=suite_run_id,
        turn_ids=turn_ids,
        coordinator_turn_id=turn_ids[0],
    )

    assert result.suite.all_case_gates_passed is True


def test_suite_service_fails_closed_on_capability_identity_drift(tmp_path) -> None:
    service, _payloads, suite_run_id, turn_ids = _suite(
        tmp_path, service_capability_id="other_context",
    )

    with pytest.raises(ContextBenchmarkSuiteError, match="identity drifted"):
        service.finalize(
            suite_run_id=suite_run_id,
            turn_ids=turn_ids,
            coordinator_turn_id=turn_ids[0],
        )

    with pytest.raises(ContextBenchmarkSuiteError, match="identity is invalid"):
        ContextBenchmarkSuiteArtifactService(
            state=InMemoryTurnStateStore(),
            events=_Events(),
            payloads=InMemoryTurnPayloadStore(),
            capability_id="",
            capability_revision="4.0.0",
            case_builder=build_model_benchmark_cases,
            case_scorer=score_model_benchmark,
            suite_scorer=score_model_benchmark_suite,
        )
