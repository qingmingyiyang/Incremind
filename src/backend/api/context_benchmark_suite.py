from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
import json
from typing import Protocol

from backend.api.context_benchmark_observation import (
    BenchmarkTurnIdentity,
    ContextBenchmarkObservationError,
    context_benchmark_observation_from_turn,
    context_benchmark_turn_identity,
)
from core.ai_kernel import (
    TurnEventStorePort,
    TurnPayloadStorePort,
    TurnStateStorePort,
)
from core.context_graph import (
    ContextBinding,
    TurnModelObservation,
    context_binding_from_payload,
)


class ContextBenchmarkSuiteError(ValueError):
    pass


class ContextBenchmarkCase(Protocol):
    """Minimum case identity the platform needs to rebuild a suite."""

    case_id: str
    decoding_revision: str


CaseBuilder = Callable[[], Sequence[ContextBenchmarkCase]]


class CaseScorer(Protocol):
    def __call__(
        self,
        case: ContextBenchmarkCase,
        linear: TurnModelObservation,
        graph_context: TurnModelObservation,
        *,
        suite_run_id: str,
    ) -> object: ...


class SuiteScorer(Protocol):
    def __call__(self, suite_run_id: str, results: Sequence[object]) -> object: ...


@dataclass(frozen=True, slots=True)
class ContextBenchmarkSuiteArtifact:
    artifact_ref: str
    suite: object
    observations: tuple[TurnModelObservation, ...]


class ContextBenchmarkSuiteArtifactService:
    """Rebuild one six-Turn benchmark result from existing platform authorities.

    The service never runs a model and owns no execution or recovery state.  It
    reads accepted Turn requests, append-only events and immutable payloads,
    then stores one immutable audit artifact under an existing coordinator
    Turn.
    """

    def __init__(
        self,
        *,
        state: TurnStateStorePort,
        events: TurnEventStorePort,
        payloads: TurnPayloadStorePort,
        capability_id: str,
        capability_revision: str,
        case_builder: CaseBuilder,
        case_scorer: CaseScorer,
        suite_scorer: SuiteScorer,
    ) -> None:
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise ContextBenchmarkSuiteError("benchmark capability identity is invalid")
        if not isinstance(capability_revision, str) or not capability_revision.strip():
            raise ContextBenchmarkSuiteError("benchmark capability revision is invalid")
        if not all(callable(item) for item in (case_builder, case_scorer, suite_scorer)):
            raise ContextBenchmarkSuiteError("benchmark capability definition is incomplete")
        self._state = state
        self._events = events
        self._payloads = payloads
        self._capability_id = capability_id
        self._capability_revision = capability_revision
        self._case_builder = case_builder
        self._case_scorer = case_scorer
        self._suite_scorer = suite_scorer

    def finalize(
        self,
        *,
        suite_run_id: str,
        turn_ids: Sequence[str],
        coordinator_turn_id: str,
    ) -> ContextBenchmarkSuiteArtifact:
        if len(turn_ids) != 6 or len(set(turn_ids)) != 6:
            raise ContextBenchmarkSuiteError(
                "benchmark suite requires six distinct durable Turns"
            )
        if coordinator_turn_id not in turn_ids:
            raise ContextBenchmarkSuiteError(
                "benchmark coordinator must be one of the suite Turns"
            )
        requests: dict[tuple[str, str], tuple[Mapping[str, object], BenchmarkTurnIdentity]] = {}
        replicate_indices: set[int] = set()
        for turn_id in turn_ids:
            request = self._state.get_request(turn_id)
            if not isinstance(request, Mapping):
                raise ContextBenchmarkSuiteError(
                    "benchmark durable Turn request is unavailable"
                )
            try:
                durable_request, identity = context_benchmark_turn_identity(request)
            except ContextBenchmarkObservationError as error:
                raise ContextBenchmarkSuiteError(str(error)) from error
            if identity.turn_id != turn_id or identity.suite_run_id != suite_run_id:
                raise ContextBenchmarkSuiteError("benchmark suite Turn identity drifted")
            key = (identity.case_id, identity.variant)
            if key in requests:
                raise ContextBenchmarkSuiteError(
                    "benchmark suite contains a duplicate case variant"
                )
            requests[key] = (durable_request, identity)
            replicate_indices.add(identity.replicate_index)

        try:
            built_cases = tuple(self._case_builder())
        except (TypeError, ValueError) as error:
            raise ContextBenchmarkSuiteError("benchmark capability cases are unavailable") from error
        try:
            valid_cases = bool(built_cases) and all(
                isinstance(case.case_id, str)
                and bool(case.case_id.strip())
                and isinstance(case.decoding_revision, str)
                and bool(case.decoding_revision.strip())
                for case in built_cases
            )
        except AttributeError as error:
            raise ContextBenchmarkSuiteError("benchmark capability cases are invalid") from error
        if not valid_cases:
            raise ContextBenchmarkSuiteError("benchmark capability cases are invalid")
        cases = {case.case_id: case for case in built_cases}
        if len(cases) != len(built_cases):
            raise ContextBenchmarkSuiteError("benchmark capability cases are duplicated")
        expected = {(case_id, variant) for case_id in cases for variant in ("linear", "linemap")}
        if set(requests) != expected or len(replicate_indices) != 1:
            raise ContextBenchmarkSuiteError(
                "benchmark suite case, variant or replicate set drifted"
            )

        observations: list[TurnModelObservation] = []
        results = []
        frozen_revisions: set[tuple[str, str, str, str, str]] = set()
        for case_id, case in cases.items():
            linemap_request, linemap_identity = requests[(case_id, "linemap")]
            binding = self._binding_for_turn(linemap_request, linemap_identity.turn_id)
            binding_revisions = (
                binding.capability_revision,
                binding.compiler_revision,
                binding.boundary_revision,
                binding.provider_revision,
                binding.model_route_revision,
            )
            frozen_revisions.add(binding_revisions)
            pair: dict[str, TurnModelObservation] = {}
            for variant in ("linear", "linemap"):
                request, identity = requests[(case_id, variant)]
                try:
                    observation = context_benchmark_observation_from_turn(
                        request=request,
                        events=self._events.events_after(identity.turn_id),
                        payload_loader=self._payloads.get,
                        capability_revision=binding.capability_revision,
                        compiler_revision=binding.compiler_revision,
                        decoding_revision=case.decoding_revision,
                    )
                except (ContextBenchmarkObservationError, KeyError, TypeError, ValueError) as error:
                    raise ContextBenchmarkSuiteError(
                        "benchmark Turn evidence could not be reconstructed"
                    ) from error
                if (
                    observation.boundary_revision != binding.boundary_revision
                    or observation.provider_revision != binding.provider_revision
                    or observation.route_revision != binding.model_route_revision
                ):
                    raise ContextBenchmarkSuiteError(
                        "benchmark ContextBinding revision drifted from model evidence"
                    )
                pair[variant] = observation
                observations.append(observation)
            try:
                results.append(self._case_scorer(
                    case, pair["linear"], pair["linemap"], suite_run_id=suite_run_id,
                ))
            except (TypeError, ValueError) as error:
                raise ContextBenchmarkSuiteError(str(error)) from error

        if len(frozen_revisions) != 1:
            raise ContextBenchmarkSuiteError(
                "benchmark suite ContextBinding revisions drifted"
            )
        try:
            suite = self._suite_scorer(suite_run_id, tuple(results))
        except (TypeError, ValueError) as error:
            raise ContextBenchmarkSuiteError(str(error)) from error
        frozen = next(iter(frozen_revisions))
        artifact_payload = _jsonable({
            "schema_version": "1.0.0",
            "suite_run_id": suite_run_id,
            "coordinator_turn_id": coordinator_turn_id,
            "turn_ids": list(turn_ids),
            "frozen_context_revisions": {
                "capability_revision": frozen[0],
                "compiler_revision": frozen[1],
                "boundary_revision": frozen[2],
                "provider_revision": frozen[3],
                "model_route_revision": frozen[4],
            },
            "observations": [asdict(item) for item in observations],
            "suite_result": _dataclass_payload(suite),
        })
        artifact_ref = self._payloads.get_or_create_immutable_payload(
            coordinator_turn_id,
            "context-benchmark-suite-v1",
            artifact_payload,
        )
        return ContextBenchmarkSuiteArtifact(
            artifact_ref=artifact_ref,
            suite=suite,
            observations=tuple(observations),
        )

    def _binding_for_turn(
        self,
        request: Mapping[str, object],
        turn_id: str,
    ) -> ContextBinding:
        stored = self._payloads.get_immutable_payload(turn_id, "context-binding-v1")
        if stored is None:
            raise ContextBenchmarkSuiteError(
                "benchmark graph-context Turn lacks an immutable ContextBinding"
            )
        _payload_ref, payload = stored
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version", "binding_id", "project_id", "capability_id",
            "capability_revision", "registry_revision", "binding",
        }:
            raise ContextBenchmarkSuiteError(
                "benchmark ContextBinding snapshot shape is invalid"
            )
        scope = request.get("scope")
        project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
        turn_input = request.get("input")
        refs = turn_input.get("refs") if isinstance(turn_input, Mapping) else None
        selected_ref = refs[0] if isinstance(refs, list) and len(refs) == 1 else None
        if (
            payload.get("schema_version") != "1.0.0"
            or payload.get("capability_id") != self._capability_id
            or payload.get("capability_revision") != self._capability_revision
            or payload.get("project_id") != project_id
            or not isinstance(selected_ref, Mapping)
            or selected_ref.get("object_id") != payload.get("binding_id")
        ):
            raise ContextBenchmarkSuiteError(
                "benchmark ContextBinding snapshot identity drifted"
            )
        try:
            binding = context_binding_from_payload(payload.get("binding"))
        except (TypeError, ValueError) as error:
            raise ContextBenchmarkSuiteError(
                "benchmark ContextBinding snapshot is invalid"
            ) from error
        if payload.get("capability_revision") != binding.capability_revision:
            raise ContextBenchmarkSuiteError(
                "benchmark ContextBinding capability revision drifted"
            )
        return binding


def _jsonable(value: object) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as error:
        raise ContextBenchmarkSuiteError(
            "benchmark suite artifact is not serializable"
        ) from error


def _dataclass_payload(value: object) -> Mapping[str, object]:
    try:
        payload = asdict(value)
    except TypeError as error:
        raise ContextBenchmarkSuiteError(
            "benchmark capability suite result must be an immutable dataclass"
        ) from error
    if not isinstance(payload, dict):
        raise ContextBenchmarkSuiteError("benchmark capability suite result is invalid")
    return payload
