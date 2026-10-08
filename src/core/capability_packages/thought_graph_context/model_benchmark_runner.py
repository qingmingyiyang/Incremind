"""Pure coordinator for the governed LineMap model-benefit benchmark.

This package code deliberately owns no execution state.  The injected
registry, Turn submitter, and finalizer remain the respective platform
authorities for ContextBindings, model execution, and durable audit evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from core.context_graph import (
    ContextBinding,
    FrozenContextRevisions,
    context_binding_from_payload,
    context_binding_to_payload,
)

from .model_benchmark import (
    ModelBenchmarkError,
    build_model_benchmark_cases,
    build_model_benchmark_turn_pair,
    freeze_benchmark_binding,
)


class ModelBenchmarkRunnerError(ValueError):
    pass


class ContextBindingRegistryPort(Protocol):
    def create(
        self,
        *,
        binding_id: str,
        project_id: str,
        capability_id: str,
        capability_revision: str,
        binding: ContextBinding,
        expected_revision: int,
    ) -> Mapping[str, object]: ...

    def resolve(self, binding_ref: str, *, project_id: str) -> object: ...


class TurnSubmitterPort(Protocol):
    def __call__(self, request: Mapping[str, object]) -> object: ...


class BenchmarkFinalizerPort(Protocol):
    def __call__(
        self,
        *,
        suite_run_id: str,
        turn_ids: Sequence[str],
        coordinator_turn_id: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ModelBenchmarkRun:
    suite_run_id: str
    replicate_index: int
    binding_ids: tuple[str, ...]
    turn_ids: tuple[str, ...]
    finalization: object


class ModelBenchmarkRunner:
    """Submit one frozen six-Turn benchmark through existing authorities."""

    def __init__(
        self,
        *,
        bindings: ContextBindingRegistryPort,
        submit_turn: TurnSubmitterPort,
        finalize: BenchmarkFinalizerPort,
    ) -> None:
        self._bindings = bindings
        self._submit_turn = submit_turn
        self._finalize = finalize

    def run(
        self,
        *,
        suite_run_id: str,
        project_id: str,
        session_id: str,
        revisions: FrozenContextRevisions,
        consent_refs: tuple[str, ...],
        created_at: str,
        replicate_index: int = 0,
    ) -> ModelBenchmarkRun:
        """Create/reuse three bindings, submit six deterministic Turns, finalize.

        Reruns submit the identical deterministic Turn identities.  The Turn
        runtime is therefore solely responsible for its own idempotency and
        recovery; this coordinator never tracks retries or terminal state.
        """
        _identity(suite_run_id, "suite run id", limit=40)
        _identity(project_id, "project id")
        _text(session_id, "session id")
        if isinstance(replicate_index, bool) or not isinstance(replicate_index, int) or not 0 <= replicate_index <= 9_999:
            raise ModelBenchmarkRunnerError("benchmark replicate index is invalid")
        if not consent_refs or any(not isinstance(ref, str) or not ref.strip() for ref in consent_refs):
            raise ModelBenchmarkRunnerError("benchmark consent refs are invalid")
        if not isinstance(created_at, str) or not created_at.strip():
            raise ModelBenchmarkRunnerError("benchmark created time is invalid")

        binding_ids: list[str] = []
        turn_ids: list[str] = []
        for case in build_model_benchmark_cases():
            binding_id = _binding_id(suite_run_id, case.case_id, replicate_index)
            linear_turn_id = _turn_id(suite_run_id, case.case_id, replicate_index, "linear")
            linemap_turn_id = _turn_id(suite_run_id, case.case_id, replicate_index, "linemap")
            try:
                pair = build_model_benchmark_turn_pair(
                    case,
                    project_id=project_id,
                    session_id=session_id,
                    linear_turn_id=linear_turn_id,
                    linemap_turn_id=linemap_turn_id,
                    binding_id=binding_id,
                    suite_run_id=suite_run_id,
                    revisions=revisions,
                    created_at=created_at,
                    consent_refs=consent_refs,
                    replicate_index=replicate_index,
                )
            except ModelBenchmarkError as error:
                raise ModelBenchmarkRunnerError(str(error)) from error
            self._create_or_verify_binding(
                binding_id=binding_id,
                project_id=project_id,
                capability_revision=revisions.capability_revision,
                binding=pair.binding_creation["binding"],
                expected_binding=case.binding_template,
                revisions=revisions,
            )
            binding_ids.append(binding_id)
            for request, expected_turn_id in (
                (pair.linear_turn, linear_turn_id),
                (pair.linemap_turn, linemap_turn_id),
            ):
                receipt = self._submit_turn(request)
                _completed_turn(receipt, expected_turn_id)
                turn_ids.append(expected_turn_id)

        if len(turn_ids) != 6 or len(set(turn_ids)) != 6:
            raise ModelBenchmarkRunnerError("benchmark Turn identity is not unique")
        finalization = self._finalize(
            suite_run_id=suite_run_id,
            turn_ids=tuple(turn_ids),
            coordinator_turn_id=turn_ids[0],
        )
        return ModelBenchmarkRun(
            suite_run_id=suite_run_id,
            replicate_index=replicate_index,
            binding_ids=tuple(binding_ids),
            turn_ids=tuple(turn_ids),
            finalization=finalization,
        )

    def _create_or_verify_binding(
        self,
        *,
        binding_id: str,
        project_id: str,
        capability_revision: str,
        binding: object,
        expected_binding: ContextBinding,
        revisions: FrozenContextRevisions,
    ) -> None:
        expected = _frozen_binding(expected_binding, revisions)
        try:
            generated = context_binding_from_payload(binding)
        except (TypeError, ValueError) as error:
            raise ModelBenchmarkRunnerError("benchmark binding payload is invalid") from error
        if generated != expected:
            raise ModelBenchmarkRunnerError("benchmark binding generation drifted")
        try:
            created = self._bindings.create(
                binding_id=binding_id,
                project_id=project_id,
                capability_id="thought_graph_context",
                capability_revision=capability_revision,
                binding=expected,
                expected_revision=0,
            )
        except ValueError:
            self._verify_existing_binding(
                binding_id=binding_id,
                project_id=project_id,
                expected=expected,
            )
            return
        _verify_created_binding(
            created,
            binding_id=binding_id,
            project_id=project_id,
            capability_revision=capability_revision,
            expected=expected,
        )

    def _verify_existing_binding(
        self,
        *,
        binding_id: str,
        project_id: str,
        expected: ContextBinding,
    ) -> None:
        binding_ref = f"crp://context-bindings/{project_id}/{binding_id}"
        try:
            resolved = self._bindings.resolve(binding_ref, project_id=project_id)
        except (KeyError, TypeError, ValueError) as error:
            raise ModelBenchmarkRunnerError("benchmark binding identity cannot be safely reused") from error
        if (
            getattr(resolved, "binding_id", None) != binding_id
            or getattr(resolved, "project_id", None) != project_id
            or getattr(resolved, "capability_id", None) != "thought_graph_context"
            or getattr(resolved, "capability_revision", None) != expected.capability_revision
            or getattr(resolved, "binding", None) != expected
        ):
            raise ModelBenchmarkRunnerError("benchmark binding identity content drifted")


def run_model_benchmark(
    *,
    bindings: ContextBindingRegistryPort,
    submit_turn: TurnSubmitterPort,
    finalize: BenchmarkFinalizerPort,
    suite_run_id: str,
    project_id: str,
    session_id: str,
    revisions: FrozenContextRevisions,
    consent_refs: tuple[str, ...],
    created_at: str,
    replicate_index: int = 0,
) -> ModelBenchmarkRun:
    """Capability entrypoint backed only by injected platform authorities."""

    return ModelBenchmarkRunner(
        bindings=bindings,
        submit_turn=submit_turn,
        finalize=finalize,
    ).run(
        suite_run_id=suite_run_id,
        project_id=project_id,
        session_id=session_id,
        revisions=revisions,
        consent_refs=consent_refs,
        created_at=created_at,
        replicate_index=replicate_index,
    )


def _frozen_binding(
    template: ContextBinding, revisions: FrozenContextRevisions,
) -> ContextBinding:
    return freeze_benchmark_binding(template, revisions)


def _verify_created_binding(
    created: Mapping[str, object],
    *,
    binding_id: str,
    project_id: str,
    capability_revision: str,
    expected: ContextBinding,
) -> None:
    if (
        created.get("binding_id") != binding_id
        or created.get("project_id") != project_id
        or created.get("capability_id") != "thought_graph_context"
        or created.get("capability_revision") != capability_revision
        or created.get("binding") != context_binding_to_payload(expected)
    ):
        raise ModelBenchmarkRunnerError("benchmark created binding identity drifted")


def _completed_turn(receipt: object, expected_turn_id: str) -> None:
    if isinstance(receipt, Mapping):
        turn_id, status = receipt.get("turn_id"), receipt.get("status")
    else:
        turn_id, status = getattr(receipt, "turn_id", None), getattr(receipt, "status", None)
    if turn_id != expected_turn_id or status != "completed":
        raise ModelBenchmarkRunnerError("benchmark Turn did not complete")


def _identity(value: object, label: str, *, limit: int = 127) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ModelBenchmarkRunnerError(f"benchmark {label} is invalid")
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-" for char in value):
        raise ModelBenchmarkRunnerError(f"benchmark {label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelBenchmarkRunnerError(f"benchmark {label} is invalid")
    return value


def _binding_id(suite_run_id: str, case_id: str, replicate_index: int) -> str:
    return f"binding-lm-{suite_run_id}-{case_id}-r{replicate_index}"


def _turn_id(suite_run_id: str, case_id: str, replicate_index: int, variant: str) -> str:
    return f"turn-lm-{suite_run_id}-{case_id}-r{replicate_index}-{variant}"
