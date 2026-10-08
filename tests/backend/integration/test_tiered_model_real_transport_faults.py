from __future__ import annotations

import json
import multiprocessing
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import sleep
from types import SimpleNamespace

import pytest

from backend.model_provider_health import (
    ModelProviderHealthStore,
    ProviderFailureClass,
    ProviderHealthScope,
    ProviderHealthState,
)
from backend.model_route_context import model_route_provider_context_from_record
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.model_routing_snapshot import (
    project_turn_model_routing_snapshot,
    turn_model_routing_snapshot_revision,
)
import backend.model_runtime as model_runtime_module
from backend.model_runtime import resolve_tiered_model_gateway_runtime
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from backend.providers import ProviderRegistry
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.model_route_context import provider_egress_manifest
from core.model_gateway import ModelRequest
from core.ai_kernel.runtime import SynchronousAIRuntime
from core.ai_kernel.event_store import TurnEventConflict
from core.ai_kernel.recovery import classify_recovery
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import ModelRouteRuntimeService
from tests.openai_transport_testlib import OpenAITransportFixture


class _Secrets:
    def get(self, _key: str) -> str:
        return "fixture-key"


def _container(root):
    return SimpleNamespace(root_dir=root, secret_store=_Secrets())


class _NoSecrets:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, key: str) -> str:
        self.calls.append(key)
        return ""


_OpenAITransportFixture = OpenAITransportFixture
_TURN_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "core-contracts" / "ai" / "fixtures" / "turn-request"
    / "valid-project-answer.json"
)


@pytest.fixture(
    params=[
        (500, ProviderFailureClass.SERVER_ERROR),
        (429, ProviderFailureClass.RATE_LIMITED),
        ("timeout", ProviderFailureClass.TIMEOUT),
        ("drop", ProviderFailureClass.CONNECTION),
    ],
    ids=["http-500", "http-429", "timeout", "connection-drop"],
)
def isolated_loopback_servers(request) -> Iterator[
    tuple[_OpenAITransportFixture, _OpenAITransportFixture, ProviderFailureClass],
]:
    fault, failure_class = request.param
    deep = _OpenAITransportFixture(expected_model="deep-model", fault=fault)
    standard = _OpenAITransportFixture(expected_model="standard-model", fault=None)
    deep.start()
    standard.start()
    try:
        yield deep, standard, failure_class
    finally:
        standard.close()
        deep.close()


def _activate(root, *, deep_base_url: str, standard_base_url: str) -> None:
    registry = ModelRouteRegistry(root)
    contexts = []
    compatibility = {}
    route_specs = (
        ("search.answer", "standard-provider", "standard-model", standard_base_url),
        ("tier.deep", "deep-provider", "deep-model", deep_base_url),
    )
    for revision, (route_key, provider_id, model, base_url) in enumerate(route_specs):
        provider = ProviderRegistry(root).create({
            "provider_id": provider_id,
            "name": provider_id,
            "llm_provider": "openai",
            "base_url": base_url,
            "api_path": "/chat/completions",
            "model": model,
            "models": [model],
            "enabled": True,
        }, fallback={})
        context = model_route_provider_context_from_record(root, provider)
        contexts.append(context)
        compatibility[route_key] = context
        registry.update(route_key, {
            "provider_id": provider_id,
            "model_name": model,
            "adapter_kind": "openai-compatible",
            "enabled": True,
            "reason": "real transport fault fixture",
        }, expected_registry_revision=revision, provider=provider, egress_consented=True)
    runtime = ModelRouteRuntimeService(root)
    preview = runtime.preview(
        route_keys=[spec[0] for spec in route_specs],
        compatibility=compatibility,
        providers=contexts,
    )
    runtime.activate(
        shadow_token=str(preview["shadow_token"]),
        route_keys=preview["route_keys"],
        expected_runtime_revision=0,
        confirm=True,
        compatibility=compatibility,
        providers=contexts,
    )
    ModelRoutingProfileStore(root).update(
        expected_revision=1,
        rules_version=1,
        text_default_tier="standard",
        tier_routes={
            "fast": None,
            "standard": "search.answer",
            "deep": "tier.deep",
            "vision": None,
            "image_generation": None,
        },
    )
    ProjectCapabilityProfileStore(root).update(
        "project-a",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        preferred_model_tier="deep",
    )


def _activate_remote_deep(root) -> None:
    """Activate a consented remote route without ever contacting it."""
    registry = ModelRouteRegistry(root)
    provider = ProviderRegistry(root).create({
        "provider_id": "deep-provider", "name": "deep-provider",
        "llm_provider": "openai", "base_url": "https://api.example.invalid",
        "api_path": "/chat/completions", "model": "deep-model",
        "models": ["deep-model"], "enabled": True,
    }, fallback={})
    policy = ProviderEgressPolicyStore(root)
    manifest = provider_egress_manifest(provider, policy)
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    context = model_route_provider_context_from_record(root, provider)
    assert context.egress_consented is True
    registry.update("tier.deep", {
        "provider_id": "deep-provider", "model_name": "deep-model",
        "adapter_kind": "openai-compatible", "enabled": True,
        "reason": "local only remote rejection fixture",
    }, expected_registry_revision=0, provider=provider, egress_consented=True)
    runtime = ModelRouteRuntimeService(root)
    preview = runtime.preview(
        route_keys=["tier.deep"], compatibility={"tier.deep": context}, providers=[context],
    )
    runtime.activate(
        shadow_token=str(preview["shadow_token"]), route_keys=preview["route_keys"],
        expected_runtime_revision=0, confirm=True,
        compatibility={"tier.deep": context}, providers=[context],
    )
    ModelRoutingProfileStore(root).update(
        expected_revision=1, rules_version=1, text_default_tier="deep",
        tier_routes={"fast": None, "standard": None, "deep": "tier.deep", "vision": None, "image_generation": None},
    )
    ProjectCapabilityProfileStore(root).update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a", boundary_profile_revision=1,
        preferred_model_tier="deep",
    )


def _snapshot(root, *, turn_id: str, privacy_scope: str = "remote_allowed"):
    return project_turn_model_routing_snapshot(
        _container(root),
        turn_id=turn_id,
        project_id="project-a",
        required_capability="structured",
        modality="text",
        output_contract="json_object",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
        privacy_scope=privacy_scope,
    )


class _MeasurementSink:
    """Minimal metadata-only sink for exercising the production gateway directly."""

    def __init__(self) -> None:
        self.observations: list[dict[str, object]] = []
        self.observed = Event()
        self.attempts = 0

    def model_call_routed(self, **_kwargs) -> None: pass
    def model_call_started(self, **_kwargs) -> None: pass
    def model_call_completed(self, **_kwargs) -> None: pass
    def model_call_failed(self) -> None: pass
    def model_call_cache_observed(self, **_kwargs) -> None: pass

    def model_dispatch_authority_observed(self, **measurement) -> None:
        self.observations.append(dict(measurement))
        self.observed.set()

    def begin_model_wire_attempt(self):
        self.attempts += 1

        class _Attempt:
            def succeeded(self, **_kwargs) -> None: pass
            def failed_transport(self, **_kwargs) -> None: pass
            def consumer_cancelled(self) -> None: pass

        return _Attempt()


class _UnusedPlanner:
    def plan(self, *_args, **_kwargs):
        raise AssertionError("planner must not run in transport fixture")


class _EmptyRegistry:
    def get(self, _capability_id): return None
    def list(self): return ()
    def resolve(self, _capability_id): return None


def _durable_control(root, turn_id: str):
    store = SQLiteAITurnStore(root / ".rebuild-data" / "ai" / "turns.sqlite3")
    runtime = SynchronousAIRuntime(
        planner=_UnusedPlanner(), registry=_EmptyRegistry(),
        events=store, payloads=store, state=store,
    )
    request = json.loads(_TURN_FIXTURE.read_text(encoding="utf-8"))
    request.update({
        "turn_id": turn_id,
        "session_id": f"session-{turn_id}",
        "operation_id": f"op-{turn_id}",
        "idempotency_key": f"idempotency-{turn_id}",
        "scope": {"kind": "project", "project_id": "project-a", "series_id": None},
    })
    runtime.accept_turn(request)
    step_id = f"step-{turn_id}"
    model_request_id = f"model-request-{turn_id}"
    runtime._append(  # noqa: SLF001 - exact production lifecycle Gate
        turn_id, "model.requested", "running", "planner step requested",
        step_id=step_id, model_request_id=model_request_id,
    )
    control = runtime._begin_planner_control(  # noqa: SLF001
        turn_id, step_id=step_id, model_request_id=model_request_id,
    )
    return store, runtime, control


def _competing_model_attempt_worker(
    root: str,
    snapshot: dict[str, object],
    turn_id: str,
    step_id: str,
    model_request_id: str,
    ready: object,
    start: object,
    outcomes: object,
) -> None:
    """Windows-spawn worker: route is pre-recorded, only wire reservation races."""
    try:
        store = SQLiteAITurnStore(
            Path(root) / ".rebuild-data" / "ai" / "turns.sqlite3",
        )
        runtime = SynchronousAIRuntime(
            planner=_UnusedPlanner(), registry=_EmptyRegistry(),
            events=store, payloads=store, state=store,
        )
        from core.ai_kernel.runtime import _PlannerExecutionContext

        control = _PlannerExecutionContext(
            turn_id=turn_id,
            step_id=step_id,
            model_request_id=model_request_id,
            timeout_ms=12_000,
            _route_recorder=lambda _snapshot_ref: None,
            _attempt_dispatch_recorder=lambda payload: runtime._store_model_attempt_dispatch(  # noqa: SLF001
                turn_id, payload, step_id=step_id, tool_call_id=None, capability_id=None,
            ),
            _attempt_terminal_recorder=lambda payload, dispatch_ref: runtime._store_model_attempt_terminal(  # noqa: SLF001
                turn_id, payload, dispatch_ref=dispatch_ref, step_id=step_id,
                tool_call_id=None, capability_id=None,
            ),
        )
        ready.put("ready")
        if not start.wait(timeout=12):
            outcomes.put(("timeout", "start barrier"))
            return
        gateway = resolve_tiered_model_gateway_runtime(
            _container(Path(root)), required_capability="structured",
            egress_purpose="search_answer",
            egress_categories=("instructions", "source_excerpt"),
        ).gateway
        if gateway is None:
            outcomes.put(("error", "gateway unavailable"))
            return
        _invoke(gateway, snapshot, metadata_sink=control)
        outcomes.put(("completed", ""))
    except TurnEventConflict:
        outcomes.put(("conflict", ""))
    except Exception as error:  # pragma: no cover - failure details are asserted by the parent
        outcomes.put(("error", f"{type(error).__name__}: {error}"))


def _invoke(gateway, snapshot, *, timeout: float | None = None, metadata_sink=None):
    parameters: dict[str, object] = {
        "_routing_project_id": "project-a",
        "_model_routing_snapshot": snapshot,
    }
    if metadata_sink is not None:
        turn_id = str(snapshot["turn"]["turn_id"])
        parameters["_model_routing_snapshot_ref"] = (
            f"crp://session/{turn_id}/turn-model-routing-snapshot-v1/test"
        )
        parameters["_model_routing_snapshot_revision"] = turn_model_routing_snapshot_revision(snapshot)
    if timeout is not None:
        parameters["timeout"] = timeout
    return gateway.invoke(ModelRequest(
        capability="structured",
        input="fixture question",
        parameters=parameters,
        privacy_scope=snapshot["requirement"]["privacy_scope"],
        metadata_sink=metadata_sink,
    ))


def _assert_deep_provider_open(root, snapshot, expected_failure_class: ProviderFailureClass) -> None:
    selected = snapshot["selected"]
    boundary = snapshot["boundary"]
    health = ModelProviderHealthStore(root).get(ProviderHealthScope(
        project_id="project-a",
        boundary_profile_id=boundary["profile_id"],
        boundary_revision=boundary["profile_revision"],
        route_key=selected["route_key"],
        provider_id=selected["provider_id"],
        provider_revision=selected["provider_revision"],
        model_name=selected["model_name"],
    ))
    assert health.state is ProviderHealthState.OPEN
    assert health.failure_class is expected_failure_class


def _assert_next_turn_uses_standard(gateway, root) -> None:
    next_turn = _snapshot(root, turn_id="turn-after-real-transport-fault")
    assert next_turn["selected"]["tier"] == "standard"
    result = _invoke(gateway, next_turn)
    assert result.model == "standard-model"
    assert result.output == {"answer": "fallback"}


def test_real_transport_fault_is_isolated_by_provider_and_falls_back_next_turn(
    tmp_path,
    isolated_loopback_servers: tuple[
        _OpenAITransportFixture, _OpenAITransportFixture, ProviderFailureClass,
    ],
) -> None:
    """No completion monkeypatch: each provider endpoint is independently observable."""
    deep_server, standard_server, expected_failure_class = isolated_loopback_servers
    _activate(
        tmp_path,
        deep_base_url=deep_server.base_url,
        standard_base_url=standard_server.base_url,
    )
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path),
        required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None

    failed_turn = _snapshot(tmp_path, turn_id="turn-real-transport-fault")
    assert failed_turn["selected"]["tier"] == "deep"
    with pytest.raises(Exception):
        _invoke(
            gateway,
            failed_turn,
            timeout=0.15 if deep_server.fault == "timeout" else None,
        )

    assert deep_server.requests == [{
        "model": "deep-model", "path": "/v1/chat/completions", "ordinal": 1,
        "has_authorization": False,
    }]
    assert standard_server.requests == []
    _assert_deep_provider_open(tmp_path, failed_turn, expected_failure_class)
    _assert_next_turn_uses_standard(gateway, tmp_path)
    assert len(deep_server.requests) == 1
    assert standard_server.requests == [{
        "model": "standard-model", "path": "/v1/chat/completions", "ordinal": 1,
        "has_authorization": False,
    }]


def test_local_loopback_turn_executes_without_secret_or_authorization_header(tmp_path) -> None:
    """A canonical local_only snapshot permits only manifest-derived loopback execution."""
    local = _OpenAITransportFixture(expected_model="deep-model", fault=None)
    local.start()
    try:
        _activate(
            tmp_path, deep_base_url=local.base_url, standard_base_url=local.base_url,
        )
        secrets = _NoSecrets()
        container = SimpleNamespace(root_dir=tmp_path, secret_store=secrets)
        gateway = resolve_tiered_model_gateway_runtime(
            container,
            required_capability="structured",
            egress_purpose="search_answer",
            egress_categories=("instructions", "source_excerpt"),
        ).gateway
        assert gateway is not None

        snapshot = _snapshot(
            tmp_path, turn_id="turn-local-loopback", privacy_scope="local_only",
        )
        assert snapshot["selected"] is not None
        assert snapshot["selected"]["tier"] == "deep"
        assert snapshot["selected"]["execution_location"] == "local_loopback"
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        assert local.base_url not in serialized
        assert "/v1/chat/completions" not in serialized

        result = _invoke(gateway, snapshot)
        assert result.output == {"answer": "fallback"}
        assert result.model == "deep-model"
        assert secrets.calls == []
        assert local.requests == [{
            "model": "deep-model", "path": "/v1/chat/completions", "ordinal": 1,
            "has_authorization": False,
        }]
    finally:
        local.close()


def test_local_only_snapshot_excludes_remote_provider_before_secret_or_dispatch(tmp_path) -> None:
    _activate_remote_deep(tmp_path)
    snapshot = _snapshot(
        tmp_path, turn_id="turn-local-remote-denied", privacy_scope="local_only",
    )
    assert snapshot["selected"] is None
    deep = next(item for item in snapshot["tiers"] if item["tier"] == "deep")
    assert deep["execution_location"] == "remote"
    assert "privacy_scope_ineligible" in deep["exclusion_reasons"]


def test_remote_allowed_turn_keeps_loopback_provider_keyless(tmp_path) -> None:
    local = _OpenAITransportFixture(expected_model="deep-model", fault=None)
    local.start()
    try:
        _activate(tmp_path, deep_base_url=local.base_url, standard_base_url=local.base_url)
        secrets = _NoSecrets()
        gateway = resolve_tiered_model_gateway_runtime(
            SimpleNamespace(root_dir=tmp_path, secret_store=secrets),
            required_capability="structured", egress_purpose="search_answer",
            egress_categories=("instructions", "source_excerpt"),
        ).gateway
        assert gateway is not None
        snapshot = _snapshot(tmp_path, turn_id="turn-remote-allowed-loopback")
        assert snapshot["selected"]["execution_location"] == "local_loopback"
        assert _invoke(gateway, snapshot).output == {"answer": "fallback"}
        assert secrets.calls == []
        assert local.requests[0]["has_authorization"] is False
    finally:
        local.close()


def test_real_tiered_gateway_releases_authority_fence_after_wire_commit(tmp_path) -> None:
    """A committed call does not hold the root authority fence across Provider I/O."""
    local = _OpenAITransportFixture(
        expected_model="deep-model", fault=None, block_first_request=True,
    )
    local.start()
    try:
        _activate(tmp_path, deep_base_url=local.base_url, standard_base_url=local.base_url)
        gateway = resolve_tiered_model_gateway_runtime(
            _container(tmp_path), required_capability="structured",
            egress_purpose="search_answer", egress_categories=("instructions", "source_excerpt"),
        ).gateway
        assert gateway is not None
        first_snapshot = _snapshot(tmp_path, turn_id="turn-single-flight-first")
        second_snapshot = _snapshot(tmp_path, turn_id="turn-single-flight-second")
        store, _first_runtime, first_sink = _durable_control(
            tmp_path, "turn-single-flight-first",
        )
        _store_again, _second_runtime, second_sink = _durable_control(
            tmp_path, "turn-single-flight-second",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(_invoke, gateway, first_snapshot, metadata_sink=first_sink)
            assert local.first_request_started.wait(timeout=5)
            second = executor.submit(_invoke, gateway, second_snapshot, metadata_sink=second_sink)
            for _ in range(50):
                if len(local.requests) == 2:
                    break
                sleep(0.02)
            assert len(local.requests) == 2
            assert second.result(timeout=10).output == {"answer": "fallback"}
            local.release_first_request.set()
            assert first.result(timeout=10).output == {"answer": "fallback"}

        assert [item["ordinal"] for item in local.requests] == [1, 2]
        with sqlite3.connect(store._path) as connection:  # noqa: SLF001
            assert connection.execute(
                "SELECT COUNT(*) FROM ai_model_attempt_reservations"
            ).fetchone()[0] == 2
        observations = (
            first_sink.dispatch_authority_receipt_payload(
                turn_id="turn-single-flight-first",
            ),
            second_sink.dispatch_authority_receipt_payload(
                turn_id="turn-single-flight-second",
            ),
        )
        assert all(item is not None and item["outcome"] == "completed" for item in observations)
        assert all(int(item["wait_ms"]) < 100 for item in observations if item is not None)
        for observation in observations:
            assert observation is not None
            assert {"wait_ms", "hold_ms", "outcome"} <= set(observation)
            assert observation["input_recorded"] is False
            assert observation["output_recorded"] is False
            assert str(tmp_path) not in str(observation)
    finally:
        local.release_first_request.set()
        local.close()


def test_committed_transport_failure_cannot_strand_later_invocation(tmp_path) -> None:
    """Provider failure occurs after the short authority fence has completed."""
    local = _OpenAITransportFixture(
        expected_model="deep-model", fault=None, block_first_request=True,
        faults_by_ordinal={1: "drop", 2: None},
    )
    local.start()
    try:
        _activate(tmp_path, deep_base_url=local.base_url, standard_base_url=local.base_url)
        gateway = resolve_tiered_model_gateway_runtime(
            _container(tmp_path), required_capability="structured",
            egress_purpose="search_answer", egress_categories=("instructions", "source_excerpt"),
        ).gateway
        assert gateway is not None
        failed_snapshot = _snapshot(tmp_path, turn_id="turn-single-flight-failure")
        successful_snapshot = _snapshot(tmp_path, turn_id="turn-single-flight-recovery")
        _store, _failed_runtime, failed_sink = _durable_control(
            tmp_path, "turn-single-flight-failure",
        )
        _store_again, _successful_runtime, successful_sink = _durable_control(
            tmp_path, "turn-single-flight-recovery",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            failed = executor.submit(_invoke, gateway, failed_snapshot, metadata_sink=failed_sink)
            assert local.first_request_started.wait(timeout=5)
            successful = executor.submit(_invoke, gateway, successful_snapshot, metadata_sink=successful_sink)
            for _ in range(50):
                if len(local.requests) == 2:
                    break
                sleep(0.02)
            assert len(local.requests) == 2
            assert successful.result(timeout=10).output == {"answer": "fallback"}
            local.release_first_request.set()
            with pytest.raises(Exception):
                failed.result(timeout=10)

        assert [item["ordinal"] for item in local.requests] == [1, 2]
        failed_observation = failed_sink.dispatch_authority_receipt_payload(
            turn_id="turn-single-flight-failure",
        )
        successful_observation = successful_sink.dispatch_authority_receipt_payload(
            turn_id="turn-single-flight-recovery",
        )
        assert failed_observation is not None and failed_observation["outcome"] == "completed"
        assert successful_observation is not None and successful_observation["outcome"] == "completed"
        assert int(successful_observation["wait_ms"]) < 100
    finally:
        local.release_first_request.set()
        local.close()


def test_authority_mutation_after_commit_does_not_wait_for_provider_io(tmp_path) -> None:
    """A completed commit-to-wire is not retroactively revocable."""
    local = _OpenAITransportFixture(
        expected_model="deep-model", fault=None, block_first_request=True,
    )
    local.start()
    try:
        _activate(tmp_path, deep_base_url=local.base_url, standard_base_url=local.base_url)
        gateway = resolve_tiered_model_gateway_runtime(
            _container(tmp_path), required_capability="structured",
            egress_purpose="search_answer", egress_categories=("instructions", "source_excerpt"),
        ).gateway
        assert gateway is not None
        snapshot = _snapshot(tmp_path, turn_id="turn-commit-before-mutation")
        store, _runtime, sink = _durable_control(
            tmp_path, "turn-commit-before-mutation",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            invocation = executor.submit(_invoke, gateway, snapshot, metadata_sink=sink)
            assert local.first_request_started.wait(timeout=5)
            mutation = executor.submit(
                ModelRoutingProfileStore(tmp_path).update,
                expected_revision=2,
                rules_version=2,
                text_default_tier="deep",
                tier_routes={
                    "fast": None, "standard": "search.answer", "deep": "tier.deep",
                    "vision": None, "image_generation": None,
                },
            )
            updated = mutation.result(timeout=2)
            assert updated.profile.revision == 3
            assert not invocation.done()
            local.release_first_request.set()
            assert invocation.result(timeout=10).output == {"answer": "fallback"}

        with sqlite3.connect(store._path) as connection:  # noqa: SLF001
            assert connection.execute(
                "SELECT status FROM ai_model_attempt_reservations"
            ).fetchone() == ("terminal",)
        assert len(local.requests) == 1
        observation = sink.dispatch_authority_receipt_payload(
            turn_id="turn-commit-before-mutation",
        )
        assert observation is not None and int(observation["hold_ms"]) < 500
    finally:
        local.release_first_request.set()
        local.close()


def test_non_durable_attempt_sink_keeps_conservative_full_io_fence(tmp_path) -> None:
    local = _OpenAITransportFixture(
        expected_model="deep-model", fault=None, block_first_request=True,
    )
    local.start()
    try:
        _activate(tmp_path, deep_base_url=local.base_url, standard_base_url=local.base_url)
        gateway = resolve_tiered_model_gateway_runtime(
            _container(tmp_path), required_capability="structured",
            egress_purpose="search_answer", egress_categories=("instructions", "source_excerpt"),
        ).gateway
        assert gateway is not None
        first_sink, second_sink = _MeasurementSink(), _MeasurementSink()
        first_snapshot = _snapshot(tmp_path, turn_id="turn-nondurable-first")
        second_snapshot = _snapshot(tmp_path, turn_id="turn-nondurable-second")

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(_invoke, gateway, first_snapshot, metadata_sink=first_sink)
            assert local.first_request_started.wait(timeout=5)
            second = executor.submit(_invoke, gateway, second_snapshot, metadata_sink=second_sink)
            sleep(0.2)
            assert len(local.requests) == 1
            local.release_first_request.set()
            assert first.result(timeout=10).output == {"answer": "fallback"}
            assert second.result(timeout=10).output == {"answer": "fallback"}
    finally:
        local.release_first_request.set()
        local.close()


def test_spawned_processes_competing_for_one_model_attempt_emit_at_most_one_post(tmp_path) -> None:
    """The durable commit, rather than a process-local lock, owns model egress."""
    local = _OpenAITransportFixture(expected_model="deep-model", fault=None)
    local.start()
    processes = []
    try:
        _activate(tmp_path, deep_base_url=local.base_url, standard_base_url=local.base_url)
        turn_id = "turn-cross-process-model-attempt"
        step_id = f"step-{turn_id}"
        model_request_id = f"model-request-{turn_id}"
        snapshot = _snapshot(tmp_path, turn_id=turn_id)
        store, runtime, _control = _durable_control(tmp_path, turn_id)
        runtime._append(  # noqa: SLF001 - prebuild lifecycle before workers race
            turn_id, "model.routed", "running", "route was pre-recorded",
            step_id=step_id, model_request_id=model_request_id,
        )

        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        outcomes = context.Queue()
        # Each worker receives an equivalent frozen snapshot and independently
        # reconstructs the gateway.  They share only the durable Turn store.
        processes = [
            context.Process(
                target=_competing_model_attempt_worker,
                args=(
                    str(tmp_path), dict(snapshot), turn_id, step_id, model_request_id,
                    ready, start, outcomes,
                ),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        assert [ready.get(timeout=12) for _ in processes] == ["ready", "ready"]
        start.set()
        results = [outcomes.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
        assert [process.exitcode for process in processes] == [0, 0], results
        assert sorted(result[0] for result in results) == ["completed", "conflict"], results
        assert len(local.requests) <= 1
        with sqlite3.connect(store._path) as connection:  # noqa: SLF001
            assert connection.execute(
                "SELECT COUNT(*) FROM ai_model_attempt_reservations WHERE turn_id=?",
                (turn_id,),
            ).fetchone()[0] == 1
        assert sum(
            event["type"] == "model.attempt.dispatched"
            for event in store.events_after(turn_id)
        ) == 1
        assert len(local.requests) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
        local.close()


def test_crash_after_durable_commit_before_http_is_quarantined_without_wire(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = _OpenAITransportFixture(expected_model="deep-model", fault=None)
    local.start()
    try:
        _activate(tmp_path, deep_base_url=local.base_url, standard_base_url=local.base_url)
        gateway = resolve_tiered_model_gateway_runtime(
            _container(tmp_path), required_capability="structured",
            egress_purpose="search_answer", egress_categories=("instructions", "source_excerpt"),
        ).gateway
        assert gateway is not None
        turn_id = "turn-commit-before-http-crash"
        snapshot = _snapshot(tmp_path, turn_id=turn_id)
        store, _runtime, sink = _durable_control(tmp_path, turn_id)

        def crash_after_commit() -> None:
            raise RuntimeError("fault after durable commit")

        monkeypatch.setattr(
            model_runtime_module, "_after_durable_model_wire_commit", crash_after_commit,
        )
        with pytest.raises(RuntimeError, match="fault after durable commit"):
            _invoke(gateway, snapshot, metadata_sink=sink)

        assert local.requests == []
        events = tuple(store.events_after(turn_id))
        assert events[-1]["type"] == "model.attempt.dispatched"
        decision = classify_recovery(
            turn_id, 1, events, payload_loader=store.get,
        )
        assert decision.disposition == "quarantine"
        assert decision.reason_code == "ai.recovery_model_incomplete"
        with sqlite3.connect(store._path) as connection:  # noqa: SLF001
            assert connection.execute(
                "SELECT status,terminal_receipt_ref FROM ai_model_attempt_reservations"
            ).fetchone() == ("committed", None)
    finally:
        local.close()


def test_authority_measurement_observer_failure_never_masks_primary_failure(tmp_path) -> None:
    def failing_observer(_measurement) -> None:
        raise AssertionError("observer must be isolated")

    with pytest.raises(RuntimeError, match="provider failure"):
        with model_dispatch_authority_fence(tmp_path, measurement_sink=failing_observer):
            raise RuntimeError("provider failure")


def test_anonymous_loopback_gateway_fails_closed_for_unimplemented_streaming() -> None:
    gateway = LiteLLMCompletionGateway(
        provider="openai", model="local-model", base_url="http://127.0.0.1:12345",
        api_key=None, anonymous=True,
        egress_guard=lambda *_args: (_ for _ in ()).throw(AssertionError("must not authorize")),
    )
    with pytest.raises(RuntimeError, match="streaming is unsupported"):
        next(gateway.stream_text([{"role": "user", "content": "do not send"}]))


def test_anonymous_loopback_transport_rejects_redirect_without_second_request() -> None:
    local = _OpenAITransportFixture(expected_model="local-model", fault="redirect")
    local.start()
    try:
        class Lease:
            def finish(self, *_args, **_kwargs) -> None:
                return

        gateway = LiteLLMCompletionGateway(
            provider="openai", model="local-model", base_url=local.base_url,
            api_key=None, anonymous=True, egress_guard=lambda *_args: Lease(),
        )
        with pytest.raises(RuntimeError, match="HTTP 302"):
            gateway.test_connection()
        assert local.requests == [{
            "model": "local-model", "path": "/v1/chat/completions", "ordinal": 1,
            "has_authorization": False,
        }]
    finally:
        local.close()
