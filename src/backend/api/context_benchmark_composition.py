"""Production composition for governed external LineMap benchmark runs.

This module is intentionally a thin authority bridge.  It resolves a declared
capability fixture, freezes a remote Core routing projection, records the
user's confirmation, and delegates each frozen envelope to the ordinary Turn
runner.  It owns neither a worker, retry loop, model client, Secret, nor a
private persistence database.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from backend.api.capability_package_runtime import CapabilityPackageContributionCatalog
from backend.api.context_benchmark_confirmation import (
    ContextBenchmarkConfirmationCreateRequest,
    ContextBenchmarkConfirmationFact,
    ContextBenchmarkConfirmationRepository,
)
from backend.api.context_benchmark_run_authority import (
    ContextBenchmarkRunError,
    ContextBenchmarkRunPlan,
    ContextBenchmarkRunPrepareRequest,
    ContextBenchmarkRunRepository,
    ContextBenchmarkRunService,
    ContextBenchmarkTurnStatus,
)
from backend.api.context_benchmark_suite import ContextBenchmarkSuiteArtifactService
from backend.api.context_binding_composition import SQLiteCompilationFactRepository
from backend.api.context_binding_runtime import ContextBindingRegistry, ContextBindingRegistryError
from backend.api.context_revision_resolver import (
    ContextRevisionResolution,
    ContextRevisionResolverError,
    ProjectContextRevisionResolver,
)
from core.ai_kernel import SQLiteAITurnStore
from core.context_graph import (
    CapabilityPackageLoader,
    ContextBinding,
    FrozenContextRevisions,
    context_binding_from_payload,
    context_binding_to_payload,
)
from core.storage_provider import SQLiteStructuredRecordStore


_DEFINITION_ID = "context.evaluate.model_suite_definition"
_DEFINITION_FIELDS = {
    "schema_version", "case_builder", "pair_builder", "case_scorer", "suite_scorer",
}


class ContextBenchmarkCompositionError(ValueError):
    """The Core benchmark composition cannot prove its required authority."""


class OrdinarySubmitterFactory(Protocol):
    def __call__(self, request: object, container: object) -> Callable[[Mapping[str, object]], object]: ...


@dataclass(frozen=True, slots=True)
class ContextBenchmarkDefinition:
    capability_id: str
    capability_revision: str
    case_builder: Callable[[], Sequence[object]]
    pair_builder: Callable[..., object]
    case_scorer: Callable[..., object]
    suite_scorer: Callable[..., object]


@dataclass(frozen=True, slots=True)
class ContextBenchmarkPrepareCommand:
    """Server-side command; clients never supply a Turn envelope or confirmation ref."""

    run_id: str
    suite_run_id: str
    project_id: str
    session_id: str
    actor_id: str
    consent_refs: tuple[str, ...]
    confirmed: bool
    replicate_index: int = 0


class ContextBenchmarkRuntime:
    """Request-scoped façade over the durable, Core-owned benchmark authorities."""

    def __init__(
        self,
        *,
        service: ContextBenchmarkRunService,
        confirmation: ContextBenchmarkConfirmationRepository,
        resolver: ProjectContextRevisionResolver,
        capability_id: str,
        clock: Callable[[], str],
    ) -> None:
        self._service = service
        self._confirmation = confirmation
        self._resolver = resolver
        self._capability_id = capability_id
        self._clock = clock

    def prepare(self, command: ContextBenchmarkPrepareCommand) -> ContextBenchmarkRunPlan:
        if not isinstance(command, ContextBenchmarkPrepareCommand):
            raise ContextBenchmarkCompositionError("benchmark prepare command is invalid")
        try:
            existing = self._service.get(command.run_id)
        except ContextBenchmarkRunError as error:
            if str(error) != "benchmark run is unavailable":
                raise ContextBenchmarkCompositionError(str(error)) from error
            existing = None
        confirmation = None
        if existing is None:
            _validate_prepare_command_confirmation(command)
            try:
                confirmation = self._confirmation.get(
                    project_id=command.project_id, run_id=command.run_id,
                )
            except ValueError as error:
                raise ContextBenchmarkCompositionError(str(error)) from error
        if existing is not None:
            request = _request_from_existing_plan(
                existing, command, self._capability_id,
            )
        elif confirmation is not None:
            request = _request_from_confirmation(
                confirmation, command, self._capability_id,
            )
        else:
            request = self._prepare_request(command)
        # This is deliberately before plan construction: a plan cannot exist
        # until the same Core routing authority has proved remote execution.
        revisions = self._remote_revisions(request)
        try:
            if existing is None and confirmation is None:
                fact = self._confirmation.create(ContextBenchmarkConfirmationCreateRequest(
                    run_id=request.run_id,
                    suite_run_id=request.suite_run_id,
                    project_id=request.project_id,
                    session_id=request.session_id,
                    actor_id=request.actor_id,
                    capability_id=self._capability_id,
                    replicate_index=request.replicate_index,
                    consent_refs=request.consent_refs,
                    confirmed_at=request.created_at,
                    revisions=revisions,
                ))
                if fact.confirmation_ref != request.confirmation_ref:
                    raise ContextBenchmarkCompositionError("benchmark confirmation reference drifted")
            self._confirmation.verify(
                request.confirmation_ref,
                run_id=request.run_id,
                suite_run_id=request.suite_run_id,
                project_id=request.project_id,
                session_id=request.session_id,
                actor_id=request.actor_id,
                capability_id=self._capability_id,
                replicate_index=request.replicate_index,
                consent_refs=request.consent_refs,
                revisions=revisions,
            )
            return self._service.prepare(request)
        except (ContextBenchmarkRunError, ValueError) as error:
            raise ContextBenchmarkCompositionError(str(error)) from error

    def get(self, run_id: str, *, session_id: str) -> ContextBenchmarkRunPlan:
        plan = self._service.get(run_id)
        self._assert_session(plan, session_id)
        return plan

    def statuses(self, run_id: str, *, session_id: str) -> tuple[ContextBenchmarkTurnStatus, ...]:
        self.get(run_id, session_id=session_id)
        return self._service.statuses(run_id)

    def submit(
        self, run_id: str, turn_id: str, *, session_id: str, confirmed: bool,
    ) -> object:
        plan = self.get(run_id, session_id=session_id)
        self._verify_plan_authority(plan)
        try:
            return self._service.submit(run_id, turn_id, confirmed=confirmed)
        except (ContextBenchmarkRunError, ValueError) as error:
            raise ContextBenchmarkCompositionError(str(error)) from error

    def finalize(self, run_id: str, *, session_id: str) -> object:
        plan = self.get(run_id, session_id=session_id)
        self._verify_plan_authority(plan)
        try:
            return self._service.finalize(run_id)
        except (ContextBenchmarkRunError, ValueError) as error:
            raise ContextBenchmarkCompositionError(str(error)) from error

    def _verify_plan_authority(self, plan: ContextBenchmarkRunPlan) -> None:
        request = _request_from_plan(plan)
        current = self._remote_revisions(request)
        if current != plan.revisions:
            raise ContextBenchmarkCompositionError("benchmark frozen revisions drifted")
        try:
            self._confirmation.verify(
                plan.confirmation_ref,
                run_id=plan.run_id,
                suite_run_id=plan.suite_run_id,
                project_id=plan.project_id,
                session_id=plan.session_id,
                actor_id=plan.actor_id,
                capability_id=plan.capability_id,
                replicate_index=plan.replicate_index,
                consent_refs=plan.consent_refs,
                revisions=current,
            )
        except ValueError as error:
            raise ContextBenchmarkCompositionError(str(error)) from error

    def _remote_revisions(self, request: ContextBenchmarkRunPrepareRequest) -> FrozenContextRevisions:
        try:
            resolution = self._resolver.resolve(
                request.project_id, self._capability_id,
                f"benchmark-{request.run_id}", True,
            )
            if not isinstance(resolution, ContextRevisionResolution):
                raise ContextBenchmarkCompositionError("benchmark routing resolution is invalid")
            return resolution.require_execution_location("remote").revisions
        except (ContextRevisionResolverError, ValueError) as error:
            raise ContextBenchmarkCompositionError("benchmark requires remote routing") from error

    def _prepare_request(self, command: ContextBenchmarkPrepareCommand) -> ContextBenchmarkRunPrepareRequest:
        if not isinstance(command, ContextBenchmarkPrepareCommand):
            raise ContextBenchmarkCompositionError("benchmark prepare command is invalid")
        _validate_prepare_command_confirmation(command)
        created_at = self._clock()
        if not isinstance(created_at, str) or not created_at.strip():
            raise ContextBenchmarkCompositionError("benchmark clock is invalid")
        return ContextBenchmarkRunPrepareRequest(
            run_id=command.run_id,
            suite_run_id=command.suite_run_id,
            project_id=command.project_id,
            session_id=command.session_id,
            actor_id=command.actor_id,
            consent_refs=command.consent_refs,
            confirmation_ref=(
                f"crp://context-benchmark-confirmations/{command.project_id}/{command.run_id}"
            ),
            created_at=created_at,
            confirmed=command.confirmed,
            replicate_index=command.replicate_index,
        )

    @staticmethod
    def _assert_session(plan: ContextBenchmarkRunPlan, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id.strip() or plan.session_id != session_id:
            raise ContextBenchmarkCompositionError("benchmark session drifted")


def compose_context_benchmark_runtime(
    *,
    packages: CapabilityPackageLoader,
    contributions: CapabilityPackageContributionCatalog,
    records: SQLiteStructuredRecordStore,
    bindings: ContextBindingRegistry,
    compilation_facts: SQLiteCompilationFactRepository,
    revisions: ProjectContextRevisionResolver,
    turn_store: SQLiteAITurnStore,
    request: object,
    container: object,
    clock: Callable[[], str] | None = None,
    submitter_factory: OrdinarySubmitterFactory | None = None,
) -> ContextBenchmarkRuntime:
    """Compose a request-scoped façade backed by shared Core stores only."""

    if not isinstance(records, SQLiteStructuredRecordStore):
        raise ContextBenchmarkCompositionError("benchmark records must use the Core context graph store")
    if not isinstance(bindings, ContextBindingRegistry) or not isinstance(compilation_facts, SQLiteCompilationFactRepository):
        raise ContextBenchmarkCompositionError("benchmark binding authority is invalid")
    if not isinstance(revisions, ProjectContextRevisionResolver) or not isinstance(turn_store, SQLiteAITurnStore):
        raise ContextBenchmarkCompositionError("benchmark Core runtime authority is invalid")
    definition = resolve_context_benchmark_definition(packages, contributions)
    active_clock = clock or _utc_clock
    confirmation = ContextBenchmarkConfirmationRepository(records)

    def readiness(value: ContextBenchmarkRunPrepareRequest) -> FrozenContextRevisions:
        resolution = revisions.resolve(
            value.project_id, definition.capability_id, f"benchmark-{value.run_id}", True,
        ).require_execution_location("remote")
        confirmation.verify(
            value.confirmation_ref,
            run_id=value.run_id,
            suite_run_id=value.suite_run_id,
            project_id=value.project_id,
            session_id=value.session_id,
            actor_id=value.actor_id,
            capability_id=definition.capability_id,
            replicate_index=value.replicate_index,
            consent_refs=value.consent_refs,
            revisions=resolution.revisions,
        )
        return resolution.revisions

    def binding_issuer(
        *, case: object, binding_creation: Mapping[str, object], project_id: str,
        revisions: FrozenContextRevisions,
    ) -> Mapping[str, object]:
        del case
        return _issue_binding(
            binding_creation=binding_creation,
            project_id=project_id,
            capability_id=definition.capability_id,
            revisions=revisions,
            registry=bindings,
            facts=compilation_facts,
        )

    finalizer = ContextBenchmarkSuiteArtifactService(
        state=turn_store,
        events=turn_store,
        payloads=turn_store,
        capability_id=definition.capability_id,
        capability_revision=definition.capability_revision,
        case_builder=definition.case_builder,
        case_scorer=definition.case_scorer,
        suite_scorer=definition.suite_scorer,
    )
    submit = (submitter_factory or ordinary_turn_submitter)(request, container)
    if not callable(submit):
        raise ContextBenchmarkCompositionError("benchmark ordinary submitter is invalid")
    service = ContextBenchmarkRunService(
        repository=ContextBenchmarkRunRepository(records),
        readiness=readiness,
        case_builder=definition.case_builder,
        pair_builder=definition.pair_builder,
        binding_issuer=binding_issuer,
        capability_id=definition.capability_id,
        capability_revision=definition.capability_revision,
        turn_submitter=submit,
        evidence_reader=turn_store,
        finalizer=finalizer,
    )
    return ContextBenchmarkRuntime(
        service=service,
        confirmation=confirmation,
        resolver=revisions,
        capability_id=definition.capability_id,
        clock=active_clock,
    )


def resolve_context_benchmark_definition(
    packages: CapabilityPackageLoader,
    contributions: CapabilityPackageContributionCatalog,
) -> ContextBenchmarkDefinition:
    """Resolve exactly one active, declared pure benchmark definition."""

    if not isinstance(packages, CapabilityPackageLoader) or not isinstance(contributions, CapabilityPackageContributionCatalog):
        raise ContextBenchmarkCompositionError("benchmark capability catalog is invalid")
    matches = [
        (manifest, declaration)
        for manifest in packages.active()
        for declaration in manifest.context
        if declaration.get("id") == _DEFINITION_ID
        and declaration.get("kind") == "evaluation_fixture"
    ]
    if not matches:
        raise ContextBenchmarkCompositionError("benchmark suite definition is unavailable")
    if len(matches) != 1:
        raise ContextBenchmarkCompositionError("benchmark suite definition is ambiguous")
    manifest, _declaration = matches[0]
    factory = contributions.context.get(_DEFINITION_ID)
    if not callable(factory):
        raise ContextBenchmarkCompositionError("benchmark suite definition factory is unavailable")
    try:
        value = factory()
    except Exception as error:
        raise ContextBenchmarkCompositionError("benchmark suite definition factory failed") from error
    if not isinstance(value, Mapping) or set(value) != _DEFINITION_FIELDS or value.get("schema_version") != "1.0.0":
        raise ContextBenchmarkCompositionError("benchmark suite definition is malformed")
    callables = {name: value.get(name) for name in _DEFINITION_FIELDS - {"schema_version"}}
    if not all(callable(item) for item in callables.values()):
        raise ContextBenchmarkCompositionError("benchmark suite definition is malformed")
    return ContextBenchmarkDefinition(
        capability_id=manifest.capability_id,
        capability_revision=manifest.capability_revision,
        case_builder=callables["case_builder"],  # type: ignore[arg-type]
        pair_builder=callables["pair_builder"],  # type: ignore[arg-type]
        case_scorer=callables["case_scorer"],  # type: ignore[arg-type]
        suite_scorer=callables["suite_scorer"],  # type: ignore[arg-type]
    )


def ordinary_turn_submitter(request: object, container: object) -> Callable[[Mapping[str, object]], object]:
    """Use only the normal request-scoped AI runtime and its Turn runner."""

    def submit(envelope: Mapping[str, object]) -> object:
        from backend.api.ai_runtime import get_or_build_ai_runtime
        from backend.api.ai_turn_runner import get_or_build_ai_turn_runner

        runtime = get_or_build_ai_runtime(request, container)
        return get_or_build_ai_turn_runner(request, runtime).accept_and_submit(envelope)

    return submit


def _issue_binding(
    *, binding_creation: Mapping[str, object], project_id: str,
    capability_id: str, revisions: FrozenContextRevisions,
    registry: ContextBindingRegistry, facts: SQLiteCompilationFactRepository,
) -> Mapping[str, object]:
    expected = {
        "binding_id", "project_id", "capability_id", "capability_revision",
        "expected_revision", "binding",
    }
    if not isinstance(binding_creation, Mapping) or set(binding_creation) != expected:
        raise ContextBenchmarkCompositionError("benchmark binding creation is malformed")
    binding_id = binding_creation.get("binding_id")
    if (
        not isinstance(binding_id, str) or not binding_id.strip()
        or binding_creation.get("project_id") != project_id
        or binding_creation.get("capability_id") != capability_id
        or binding_creation.get("capability_revision") != revisions.capability_revision
        or binding_creation.get("expected_revision") != 0
    ):
        raise ContextBenchmarkCompositionError("benchmark binding creation authority drifted")
    try:
        binding = context_binding_from_payload(binding_creation.get("binding"))
    except (TypeError, ValueError) as error:
        raise ContextBenchmarkCompositionError("benchmark ContextBinding is malformed") from error
    if _binding_revisions(binding) != revisions:
        raise ContextBenchmarkCompositionError("benchmark ContextBinding revisions drifted")
    try:
        created = registry.create(
            binding_id=binding_id,
            project_id=project_id,
            capability_id=capability_id,
            capability_revision=revisions.capability_revision,
            binding=binding,
            expected_revision=0,
        )
        registry_revision = created.get("registry_revision")
        binding_ref = created.get("binding_ref")
    except ContextBindingRegistryError as error:
        if "already exists" not in str(error):
            raise ContextBenchmarkCompositionError("benchmark ContextBinding registry rejected") from error
        try:
            prior = registry.resolve(
                f"crp://context-bindings/{project_id}/{binding_id}", project_id=project_id,
            )
        except ContextBindingRegistryError as resolve_error:
            raise ContextBenchmarkCompositionError("benchmark ContextBinding registry unavailable") from resolve_error
        if (
            prior.capability_id != capability_id
            or prior.capability_revision != revisions.capability_revision
            or _canonical_binding(prior.binding) != _canonical_binding(binding)
        ):
            raise ContextBenchmarkCompositionError("benchmark ContextBinding identity drifted")
        registry_revision, binding_ref = prior.registry_revision, prior.payload_ref
    if type(registry_revision) is not int or registry_revision < 1 or not isinstance(binding_ref, str):
        raise ContextBenchmarkCompositionError("benchmark ContextBinding registry response is invalid")
    try:
        facts.record(
            project_id, binding.graph_id, binding.graph_revision,
            revisions, binding_id, registry_revision,
        )
        if facts.binding_fact(
            project_id, binding.graph_id, binding.graph_revision,
            binding_id, registry_revision, revisions,
        ) is not True:
            raise ContextBenchmarkCompositionError("benchmark compilation fact is unavailable")
    except (TypeError, ValueError) as error:
        if isinstance(error, ContextBenchmarkCompositionError):
            raise
        raise ContextBenchmarkCompositionError("benchmark compilation fact could not be recorded") from error
    return {
        "binding_id": binding_id,
        "project_id": project_id,
        "capability_id": capability_id,
        "capability_revision": revisions.capability_revision,
        "binding_ref": binding_ref,
    }


def _binding_revisions(binding: ContextBinding) -> FrozenContextRevisions:
    return FrozenContextRevisions(
        capability_revision=binding.capability_revision,
        boundary_revision=binding.boundary_revision,
        provider_revision=binding.provider_revision,
        model_route_revision=binding.model_route_revision,
        compiler_revision=binding.compiler_revision,
    )


def _canonical_binding(binding: ContextBinding) -> object:
    """Registry serialization normalizes list/tuple representation."""

    import json

    return json.loads(json.dumps(context_binding_to_payload(binding), sort_keys=True))


def _request_from_plan(plan: ContextBenchmarkRunPlan) -> ContextBenchmarkRunPrepareRequest:
    return ContextBenchmarkRunPrepareRequest(
        run_id=plan.run_id,
        suite_run_id=plan.suite_run_id,
        project_id=plan.project_id,
        session_id=plan.session_id,
        actor_id=plan.actor_id,
        consent_refs=plan.consent_refs,
        confirmation_ref=plan.confirmation_ref,
        created_at=plan.created_at,
        confirmed=True,
        replicate_index=plan.replicate_index,
    )


def _request_from_existing_plan(
    plan: ContextBenchmarkRunPlan, command: ContextBenchmarkPrepareCommand,
    capability_id: str,
) -> ContextBenchmarkRunPrepareRequest:
    if (
        command.confirmed is not True
        or plan.capability_id != capability_id
        or (command.suite_run_id, command.project_id, command.session_id, command.actor_id,
            command.consent_refs, command.replicate_index)
        != (plan.suite_run_id, plan.project_id, plan.session_id, plan.actor_id,
            plan.consent_refs, plan.replicate_index)
    ):
        raise ContextBenchmarkCompositionError("benchmark run identity drifted")
    return _request_from_plan(plan)


def _request_from_confirmation(
    fact: ContextBenchmarkConfirmationFact,
    command: ContextBenchmarkPrepareCommand,
    capability_id: str,
) -> ContextBenchmarkRunPrepareRequest:
    _validate_prepare_command_confirmation(command)
    if (
        fact.capability_id != capability_id
        or (fact.run_id, fact.suite_run_id, fact.project_id, fact.session_id,
            fact.actor_id, fact.replicate_index, fact.consent_refs)
        != (command.run_id, command.suite_run_id, command.project_id,
            command.session_id, command.actor_id, command.replicate_index,
            command.consent_refs)
    ):
        raise ContextBenchmarkCompositionError("benchmark confirmation authority drifted")
    return ContextBenchmarkRunPrepareRequest(
        run_id=fact.run_id,
        suite_run_id=fact.suite_run_id,
        project_id=fact.project_id,
        session_id=fact.session_id,
        actor_id=fact.actor_id,
        consent_refs=fact.consent_refs,
        confirmation_ref=fact.confirmation_ref,
        created_at=fact.confirmed_at,
        confirmed=True,
        replicate_index=fact.replicate_index,
    )


def _validate_prepare_command_confirmation(
    command: ContextBenchmarkPrepareCommand,
) -> None:
    if command.confirmed is not True:
        raise ContextBenchmarkCompositionError("benchmark preparation requires confirmation")
    if (
        type(command.replicate_index) is not int
        or not 0 <= command.replicate_index <= 9_999
    ):
        raise ContextBenchmarkCompositionError("benchmark replicate index is invalid")


def _utc_clock() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_context_benchmark_runtime_factory(
    container: object,
    packages: CapabilityPackageLoader,
    contributions: CapabilityPackageContributionCatalog,
    context_graph_runtime: object,
    turn_store: SQLiteAITurnStore,
    *,
    clock: Callable[[], str] | None = None,
) -> Callable[[object], ContextBenchmarkRuntime]:
    """Build the one app-level factory without duplicating Core wiring in app.py."""

    root = getattr(container, "root_dir", None)
    bindings = getattr(context_graph_runtime, "bindings", None)
    facts = getattr(context_graph_runtime, "compilation_facts", None)
    if not isinstance(root, (str, Path)):
        raise ContextBenchmarkCompositionError("benchmark runtime root is invalid")
    if not isinstance(bindings, ContextBindingRegistry) or not isinstance(facts, SQLiteCompilationFactRepository):
        raise ContextBenchmarkCompositionError("benchmark context graph runtime is invalid")
    if not isinstance(packages, CapabilityPackageLoader) or not isinstance(turn_store, SQLiteAITurnStore):
        raise ContextBenchmarkCompositionError("benchmark factory dependencies are invalid")
    frozen_definition = resolve_context_benchmark_definition(packages, contributions)

    def active_revision(capability_id: str) -> str | None:
        matches = [
            item.capability_revision for item in packages.active()
            if item.capability_id == capability_id
        ]
        return matches[0] if len(matches) == 1 else None

    resolver = ProjectContextRevisionResolver(container, active_revision)
    records = SQLiteStructuredRecordStore(
        Path(root).resolve(strict=False) / ".rebuild-data" / "context-graphs.sqlite3"
    )

    def factory(request: object) -> ContextBenchmarkRuntime:
        current_definition = resolve_context_benchmark_definition(packages, contributions)
        if (
            current_definition.capability_id,
            current_definition.capability_revision,
        ) != (
            frozen_definition.capability_id,
            frozen_definition.capability_revision,
        ):
            raise ContextBenchmarkCompositionError(
                "benchmark capability revision drifted"
            )
        return compose_context_benchmark_runtime(
            packages=packages,
            contributions=contributions,
            records=records,
            bindings=bindings,
            compilation_facts=facts,
            revisions=resolver,
            turn_store=turn_store,
            request=request,
            container=container,
            clock=clock,
        )

    return factory
