from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from backend.api.capability_package_runtime import CapabilityPackageContributionCatalog
from backend.api.context_benchmark_composition import (
    ContextBenchmarkCompositionError,
    ContextBenchmarkPrepareCommand,
    _issue_binding,
    build_context_benchmark_runtime_factory,
    compose_context_benchmark_runtime,
    ordinary_turn_submitter,
    resolve_context_benchmark_definition,
)
from backend.api.context_benchmark_confirmation import (
    ContextBenchmarkConfirmationCreateRequest,
    ContextBenchmarkConfirmationRepository,
)
from backend.api.context_binding_composition import SQLiteCompilationFactRepository
from backend.api.context_binding_runtime import ContextBindingRegistry
from backend.api.context_revision_resolver import (
    ContextRevisionResolution,
    ProjectContextRevisionResolver,
)
from core.ai_kernel import SQLiteAITurnStore
from core.capability_packages.thought_graph_context.model_benchmark import (
    build_model_benchmark_definition,
)
from core.context_graph import (
    CapabilityPackageLoader,
    CapabilityPackageManifest,
    FrozenContextRevisions,
    context_binding_from_payload,
)
from core.storage_provider import SQLiteStructuredRecordStore


_DEFINITION = "context.evaluate.model_suite_definition"
_REVISIONS = FrozenContextRevisions(
    "4.2.0", "boundary-r1", "provider-r1", "route-r1", "2.0.0",
)


def _manifest(capability_id: str = "thought_graph_context") -> CapabilityPackageManifest:
    return CapabilityPackageManifest(
        schema_version="1.0.0", core_api="2", capability_id=capability_id,
        capability_revision="4.2.0", display_name="Fixture", kind="context_extension",
        execution_state_owner="core_effect_log", recovery_owner="core_reaper",
        secret_access="lease_reference_only", memory_write="proposal_only",
        document_write="draft_only", project_skill_write="proposal_only",
        contributions=("evaluation_fixture",), provides={}, tools=(), workflows=(),
        permissions={"net": [], "fs": [], "secrets": []},
        budgets={"max_bytes": 1, "max_seconds": 1}, effects={}, ui={}, tests=(),
        context=({"id": _DEFINITION, "kind": "evaluation_fixture", "entrypoint": "fixture.py:definition"},),
        artifact_id="fixture-artifact",
    )


def _catalog(factory=build_model_benchmark_definition) -> CapabilityPackageContributionCatalog:
    return CapabilityPackageContributionCatalog({}, {}, {_DEFINITION: factory})


def _loader(*manifests: CapabilityPackageManifest) -> CapabilityPackageLoader:
    loader = CapabilityPackageLoader()
    loader._active = {item.capability_id: item for item in manifests}  # type: ignore[attr-defined]
    return loader


def _runtime(tmp_path: Path, monkeypatch, *, location: str = "remote", factory=None, clock=None):
    root = tmp_path / "root"
    root.mkdir()
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "context-graphs.sqlite3")
    bindings = ContextBindingRegistry(root, namespace_id="benchmark-tests")
    facts = SQLiteCompilationFactRepository(records, lambda: "2026-08-30T00:00:00Z")
    resolver = ProjectContextRevisionResolver(object(), lambda _capability: "4.2.0")
    monkeypatch.setattr(
        resolver, "resolve",
        lambda *_args: ContextRevisionResolution(_REVISIONS, location),
    )
    submitted: list[dict[str, object]] = []

    def submitter_factory(_request, _container):
        def submit(envelope):
            payload = dict(envelope)
            submitted.append(payload)
            return {"turn_id": payload["turn_id"]}
        return submit

    runtime = compose_context_benchmark_runtime(
        packages=_loader(_manifest()), contributions=_catalog(factory or build_model_benchmark_definition),
        records=records, bindings=bindings, compilation_facts=facts, revisions=resolver,
        turn_store=SQLiteAITurnStore(root / ".rebuild-data" / "ai-turns.sqlite3"),
        request=object(), container=object(), clock=clock or (lambda: "2026-08-30T00:00:00Z"),
        submitter_factory=submitter_factory,
    )
    return runtime, records, bindings, facts, submitted


def _command(**changes: object) -> ContextBenchmarkPrepareCommand:
    values = {
        "run_id": "benchmark-run-a", "suite_run_id": "suite-a", "project_id": "project-alpha",
        "session_id": "session-alpha", "actor_id": "desktop-agent",
        "consent_refs": ("crp://consents/project-alpha/benchmark-a",), "confirmed": True,
    }
    values.update(changes)
    return ContextBenchmarkPrepareCommand(**values)


@pytest.mark.parametrize("factory", [
    lambda: {"schema_version": "1.0.0"},
    lambda: {"schema_version": "1.0.0", "case_builder": lambda: (), "pair_builder": lambda: None, "case_scorer": lambda: None, "suite_scorer": lambda: None, "unexpected": True},
])
def test_definition_fails_closed_when_malformed(factory) -> None:
    with pytest.raises(ContextBenchmarkCompositionError, match="malformed"):
        resolve_context_benchmark_definition(_loader(_manifest()), _catalog(factory))


def test_definition_fails_closed_when_missing_or_duplicate() -> None:
    with pytest.raises(ContextBenchmarkCompositionError, match="unavailable"):
        resolve_context_benchmark_definition(_loader(_manifest()), CapabilityPackageContributionCatalog({}, {}, {}))
    with pytest.raises(ContextBenchmarkCompositionError, match="ambiguous"):
        resolve_context_benchmark_definition(_loader(_manifest("fixture_one"), _manifest("fixture_two")), _catalog())


def test_prepare_requires_remote_route_before_confirmation_or_binding(tmp_path, monkeypatch) -> None:
    runtime, records, _bindings, _facts, _submitted = _runtime(tmp_path, monkeypatch, location="local_loopback")

    with pytest.raises(ContextBenchmarkCompositionError, match="requires remote routing"):
        runtime.prepare(_command())

    assert ContextBenchmarkConfirmationRepository(records).get(
        project_id="project-alpha", run_id="benchmark-run-a",
    ) is None


@pytest.mark.parametrize("changes", ({"confirmed": False}, {"replicate_index": -1}))
def test_prepare_rejects_unconfirmed_or_invalid_workload_before_fact(
    tmp_path, monkeypatch, changes,
) -> None:
    runtime, records, _bindings, _facts, _submitted = _runtime(
        tmp_path, monkeypatch,
    )

    with pytest.raises(ContextBenchmarkCompositionError):
        runtime.prepare(_command(**changes))

    assert ContextBenchmarkConfirmationRepository(records).get(
        project_id="project-alpha", run_id="benchmark-run-a",
    ) is None


def test_prepare_freezes_confirmation_then_submitter_delegates_ordinary_turn(tmp_path, monkeypatch) -> None:
    runtime, _records, _bindings, _facts, submitted = _runtime(tmp_path, monkeypatch)

    plan = runtime.prepare(_command())
    receipt = runtime.submit(
        plan.run_id, plan.envelopes[0].turn_id, session_id="session-alpha", confirmed=True,
    )

    assert len(plan.envelopes) == 6
    assert receipt == {"turn_id": plan.envelopes[0].turn_id}
    assert submitted == [dict(plan.envelopes[0].request)]


def test_prepare_retry_reuses_persisted_confirmation_time_without_new_clock(tmp_path, monkeypatch) -> None:
    times = iter(("2026-08-30T00:00:00Z", "2026-08-30T00:01:00Z"))
    runtime, _records, _bindings, _facts, _submitted = _runtime(
        tmp_path, monkeypatch, clock=lambda: next(times),
    )

    first = runtime.prepare(_command())
    replay = runtime.prepare(_command())

    assert replay == first
    assert first.created_at == "2026-08-30T00:00:00Z"


def test_prepare_recovers_confirmation_written_before_plan_without_new_clock(
    tmp_path, monkeypatch,
) -> None:
    runtime, records, _bindings, _facts, _submitted = _runtime(
        tmp_path, monkeypatch, clock=lambda: (_ for _ in ()).throw(AssertionError("clock")),
    )
    ContextBenchmarkConfirmationRepository(records).create(
        ContextBenchmarkConfirmationCreateRequest(
            run_id="benchmark-run-a", suite_run_id="suite-a",
            project_id="project-alpha", session_id="session-alpha",
            actor_id="desktop-agent", capability_id="thought_graph_context",
            replicate_index=0,
            consent_refs=("crp://consents/project-alpha/benchmark-a",),
            confirmed_at="2026-08-30T00:00:00Z", revisions=_REVISIONS,
        )
    )

    plan = runtime.prepare(_command())

    assert plan.created_at == "2026-08-30T00:00:00Z"


def test_session_and_revision_drift_fail_closed(tmp_path, monkeypatch) -> None:
    runtime, _records, _bindings, _facts, _submitted = _runtime(tmp_path, monkeypatch)
    plan = runtime.prepare(_command())

    with pytest.raises(ContextBenchmarkCompositionError, match="session drifted"):
        runtime.statuses(plan.run_id, session_id="session-other")

    resolver = runtime._resolver  # type: ignore[attr-defined]
    monkeypatch.setattr(
        resolver, "resolve",
        lambda *_args: ContextRevisionResolution(
            FrozenContextRevisions("4.2.0", "boundary-r2", "provider-r1", "route-r1", "2.0.0"), "remote",
        ),
    )
    with pytest.raises(ContextBenchmarkCompositionError, match="frozen revisions drifted"):
        runtime.submit(plan.run_id, plan.envelopes[0].turn_id, session_id="session-alpha", confirmed=True)


def test_binding_issuer_repairs_fact_after_binding_already_exists(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "context-graphs.sqlite3")
    registry = ContextBindingRegistry(root, namespace_id="benchmark-tests")
    facts = SQLiteCompilationFactRepository(records, lambda: "2026-08-30T00:00:00Z")
    definition = build_model_benchmark_definition()
    case = definition["case_builder"]()[0]
    pair = definition["pair_builder"](
        case, project_id="project-alpha", session_id="session-alpha",
        linear_turn_id="turn-linear", linemap_turn_id="turn-linemap",
        binding_id="binding-case-a", suite_run_id="suite-a", revisions=_REVISIONS,
        created_at="2026-08-30T00:00:00Z", consent_refs=("crp://consents/project-alpha/benchmark-a",),
    )
    creation = pair.binding_creation
    binding = registry.create(
        binding_id="binding-case-a", project_id="project-alpha", capability_id="thought_graph_context",
        capability_revision="4.2.0", binding=context_binding_from_payload(creation["binding"]),
        expected_revision=0,
    )
    parsed = context_binding_from_payload(creation["binding"])
    assert facts.binding_fact("project-alpha", parsed.graph_id, parsed.graph_revision, "binding-case-a", binding["registry_revision"], _REVISIONS) is False

    issued = _issue_binding(
        binding_creation=creation, project_id="project-alpha", capability_id="thought_graph_context",
        revisions=_REVISIONS, registry=registry, facts=facts,
    )

    assert issued["binding_ref"] == "crp://context-bindings/project-alpha/binding-case-a"
    assert facts.binding_fact("project-alpha", parsed.graph_id, parsed.graph_revision, "binding-case-a", binding["registry_revision"], _REVISIONS) is True


def test_startup_factory_reuses_context_graph_authorities_and_core_store(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "context-graphs.sqlite3")
    graph_runtime = type("GraphRuntime", (), {
        "bindings": ContextBindingRegistry(root, namespace_id="benchmark-tests"),
        "compilation_facts": SQLiteCompilationFactRepository(records, lambda: "2026-08-30T00:00:00Z"),
    })()
    container = type("Container", (), {"root_dir": root})()
    packages = _loader(_manifest())
    factory = build_context_benchmark_runtime_factory(
        container, packages, _catalog(), graph_runtime,
        SQLiteAITurnStore(root / ".rebuild-data" / "ai-turns.sqlite3"),
    )

    runtime = factory(type("Request", (), {})())

    assert runtime.__class__.__name__ == "ContextBenchmarkRuntime"
    packages._active["thought_graph_context"] = replace(  # type: ignore[attr-defined]
        _manifest(), capability_revision="4.3.0",
    )
    with pytest.raises(ContextBenchmarkCompositionError, match="revision drifted"):
        factory(type("Request", (), {})())


def test_ordinary_submitter_lazily_uses_only_normal_ai_runtime_and_turn_runner(
    monkeypatch,
) -> None:
    request = object()
    container = object()
    runtime = object()
    calls: list[tuple[str, object]] = []

    class Runner:
        def accept_and_submit(self, envelope):
            calls.append(("submit", dict(envelope)))
            return {"turn_id": envelope["turn_id"]}

    monkeypatch.setattr(
        "backend.api.ai_runtime.get_or_build_ai_runtime",
        lambda actual_request, actual_container: (
            calls.append(("runtime", (actual_request, actual_container))) or runtime
        ),
    )
    monkeypatch.setattr(
        "backend.api.ai_turn_runner.get_or_build_ai_turn_runner",
        lambda actual_request, actual_runtime: (
            calls.append(("runner", (actual_request, actual_runtime))) or Runner()
        ),
    )
    submit = ordinary_turn_submitter(request, container)
    assert calls == []

    receipt = submit({"turn_id": "turn-a"})

    assert receipt == {"turn_id": "turn-a"}
    assert calls == [
        ("runtime", (request, container)),
        ("runner", (request, runtime)),
        ("submit", {"turn_id": "turn-a"}),
    ]
