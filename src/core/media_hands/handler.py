from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from core.job_runner import JobStepBlockedError, JobStepResult
from core.job_runner.media_execution_receipt import media_job_uri_segment

from .effect_contract import provider_revision_identity
from .provisioner import SourcePermissionSnapshot


_STATE_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_PROVIDER_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_OUTPUT_KINDS = {
    "source", "asset", "atom", "scenario", "series_memory",
    "project_skill", "document", "job_log", "other",
}


@dataclass(frozen=True, slots=True)
class MediaOperationRequest:
    job_id: str
    source_id: str
    operation: str
    manifest_ref: str
    manifest_revision: str
    checkpoint: Mapping[str, object] | None
    budget: Mapping[str, int]
    permission_snapshot: SourcePermissionSnapshot
    control: "MediaOperationControlPort | None" = None


class MediaOperationControlPort(Protocol):
    """Local cooperative cancellation, lease and permission checkpoint."""

    def checkpoint(self) -> None: ...

    def begin_recipe_step(
        self, step_name: str, input_state_hash: str
    ) -> Mapping[str, object] | None: ...

    def complete_recipe_step(
        self, step_name: str, *, output_ref: str, output_state_hash: str,
        consumed: Mapping[str, int],
    ) -> None: ...

    def mark_recipe_step_unknown(self, step_name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class MediaOperationReceipt:
    output: Mapping[str, object]
    checkpoint: Mapping[str, object]
    consumed: Mapping[str, int]
    execution_receipt_ref: str
    log_refs: tuple[str, ...] = ()


class MediaOperationProviderPort(Protocol):
    provider_id: str
    provider_revision: str

    def execute(self, request: MediaOperationRequest) -> MediaOperationReceipt: ...


class SourcePermissionRevokedError(ValueError):
    """Raised when the admission grant is no longer active."""


class SourcePermissionRevocationCheckerPort(Protocol):
    """Local execution-time check over the already-issued permission snapshot.

    Implementations must not perform model, Boundary/Governor or network calls.
    A revoked/mismatched revision or generation raises
    :class:`SourcePermissionRevokedError`.
    """

    def assert_active(self, snapshot: SourcePermissionSnapshot) -> None: ...


class CanonicalMediaOutputVerifierPort(Protocol):
    """Read-only confirmation that a Provider output is canonical and in scope.

    The verifier follows the existing output reference to the authoritative
    output owner.  It must not publish, mutate, or create an output.  Keeping
    the complete immutable request alongside the output prevents a valid
    object from being replayed into a different source, manifest, or project
    scope.
    """

    def assert_output_committed(
        self,
        *,
        output: Mapping[str, object],
        request: MediaOperationRequest,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MediaOperationStepResult(JobStepResult):
    """Validated provider outcome with the receipt used to settle execution evidence."""

    execution_receipt_ref: str = ""


class MediaHandsOperationHandler:
    """Runs a registered media operation without owning durable Job state."""

    job_type = "media_hands"

    def __init__(
        self,
        provider: MediaOperationProviderPort,
        permission_checker: SourcePermissionRevocationCheckerPort,
        output_verifier: CanonicalMediaOutputVerifierPort,
    ) -> None:
        if not callable(getattr(permission_checker, "assert_active", None)):
            raise TypeError("media source permission checker is required")
        if not callable(getattr(output_verifier, "assert_output_committed", None)):
            raise TypeError("canonical media output verifier is required")
        self._provider = provider
        self._permission_checker = permission_checker
        self._output_verifier = output_verifier

    @property
    def configuration_identity(self) -> tuple[str, str, str]:
        """Freeze the declared provider identity used by one app lifecycle."""

        return (self.job_type, *self.provider_identity)

    @property
    def provider_identity(self) -> tuple[str, str]:
        """Return the explicit provider revision required for durable effects.

        Object identity is intentionally not an execution identity: a restarted
        process must make the same reservation for the same provider revision.
        """

        provider_id = getattr(self._provider, "provider_id", None)
        provider_revision = getattr(self._provider, "provider_revision", None)
        if (
            not isinstance(provider_id, str)
            or _PROVIDER_IDENTITY.fullmatch(provider_id) is None
            or not isinstance(provider_revision, str)
            or _PROVIDER_IDENTITY.fullmatch(provider_revision) is None
        ):
            raise JobStepBlockedError(
                code="media.provider_identity_unavailable",
                message="Media provider must declare a stable provider_id and provider_revision.",
            )
        return provider_id, provider_revision

    @property
    def supports_durable_recipe_resume(self) -> bool:
        return getattr(self._provider, "supports_durable_recipe_resume", False) is True

    def run_step(self, step_name: str, job: Mapping[str, object]) -> JobStepResult:
        request = self.preflight_step(step_name, job)
        return self.execute_prepared_step(request)

    def preflight_step(
        self, step_name: str, job: Mapping[str, object]
    ) -> MediaOperationRequest:
        """Validate the immutable input and permission before effect reservation."""

        if step_name != "execute_operation":
            raise ValueError(f"unsupported media step: {step_name}")
        media = _mapping(job, "media_hands")
        manifest = _mapping(media, "manifest")
        permission_snapshot = _permission_snapshot(media, manifest=manifest)
        budget = _integer_mapping(media, "budget")
        request = MediaOperationRequest(
            job_id=_string(job, "id"),
            source_id=_string(job, "source_id"),
            operation=_string(media, "operation"),
            manifest_ref=_string(manifest, "ref"),
            manifest_revision=_string(manifest, "revision"),
            checkpoint=_optional_mapping(job.get("checkpoint"), "checkpoint"),
            budget=budget,
            permission_snapshot=permission_snapshot,
        )
        self._assert_permission_active(permission_snapshot)
        return request

    def execute_prepared_step(
        self,
        request: MediaOperationRequest,
        *,
        expected_provider_revision_identity: str | None = None,
        execution_authorizer: Callable[[], None] | None = None,
    ) -> MediaOperationStepResult:
        """Invoke a provider through the final local authorization fence.

        Permission and, for Effect-v2 callers, the frozen Provider identity are
        rechecked immediately before the external call.  This closes the
        reservation-commit window without introducing a second execution
        state machine.
        """

        if execution_authorizer is not None:
            if not callable(execution_authorizer):
                raise TypeError("media execution authorizer must be callable")
            execution_authorizer()
        if expected_provider_revision_identity is not None:
            self._assert_provider_identity(expected_provider_revision_identity)
        self._assert_permission_active(request.permission_snapshot)
        if request.control is not None:
            # The Effect-v2 adapter supplies a Core-fenced control here.  It
            # is the final cancellation/lease checkpoint before Provider I/O.
            request.control.checkpoint()

        receipt = self._provider.execute(request)
        if not isinstance(receipt, MediaOperationReceipt):
            raise TypeError("media provider must return MediaOperationReceipt")
        output = _validate_output(receipt.output)
        self._assert_output_committed(output, request=request)
        checkpoint = _validate_checkpoint(receipt.checkpoint, request=request)
        consumed = _validate_consumed(receipt.consumed, budget=request.budget)
        log_refs = tuple(_crp_ref(ref, "log_ref") for ref in receipt.log_refs)
        execution_receipt_ref = _crp_ref(receipt.execution_receipt_ref, "execution_receipt_ref")
        return MediaOperationStepResult(
            published_outputs=(output,),
            consume_staged_outputs=True,
            checkpoint=checkpoint,
            staged_output_refs=(_string(output, "uri"),),
            log_refs=log_refs,
            resource_consumed=consumed,
            execution_receipt_ref=execution_receipt_ref,
        )

    def attach_execution_control(
        self,
        request: MediaOperationRequest,
        checkpoint: Callable[[], None],
        recipe_control: object | None = None,
    ) -> MediaOperationRequest:
        """Attach execution fencing plus a local permission recheck.

        Effect-v2 callers intentionally pass no recipe control: recipe steps
        are legacy Job evidence and cannot become a second execution state
        machine.  The Provider can still checkpoint its Core Effect fence.
        """

        handler = self

        class _Control:
            def checkpoint(self) -> None:
                checkpoint()
                handler._assert_permission_active(request.permission_snapshot)

            def begin_recipe_step(
                self, step_name: str, input_state_hash: str
            ) -> Mapping[str, object] | None:
                self.checkpoint()
                if recipe_control is None:
                    return None
                begin = getattr(recipe_control, "begin_recipe_step", None)
                if not callable(begin):
                    raise JobStepBlockedError(
                        code="media.recipe_control_unavailable",
                        message="Media recipe step control is unavailable.",
                    )
                return begin(step_name, input_state_hash)

            def complete_recipe_step(
                self, step_name: str, *, output_ref: str, output_state_hash: str,
                consumed: Mapping[str, int],
            ) -> None:
                self.checkpoint()
                if recipe_control is None:
                    return None
                complete = getattr(recipe_control, "complete_recipe_step", None)
                if not callable(complete):
                    raise JobStepBlockedError(
                        code="media.recipe_control_unavailable",
                        message="Media recipe step control is unavailable.",
                    )
                complete(
                    step_name, output_ref=output_ref,
                    output_state_hash=output_state_hash, consumed=consumed,
                )

            def mark_recipe_step_unknown(self, step_name: str) -> None:
                self.checkpoint()
                if recipe_control is None:
                    return None
                mark = getattr(recipe_control, "mark_recipe_step_unknown", None)
                if not callable(mark):
                    raise JobStepBlockedError(
                        code="media.recipe_control_unavailable",
                        message="Media recipe step control is unavailable.",
                    )
                mark(step_name)

        return replace(request, control=_Control())

    def verify_published_outputs(
        self,
        job: Mapping[str, object],
        outputs: tuple[Mapping[str, object], ...],
    ) -> None:
        """Re-verify recovered receipt outputs without re-entering a Provider."""

        request = self.preflight_step("execute_operation", job)
        if not outputs:
            raise JobStepBlockedError(
                code="media.needs_reconcile",
                message="Media provider execution receipt has no canonical output.",
            )
        for value in outputs:
            try:
                output = _validate_output(value)
                self._assert_output_committed(output, request=request)
            except JobStepBlockedError:
                raise
            except Exception as exc:
                raise JobStepBlockedError(
                    code="media.needs_reconcile",
                    message="Media provider output is no longer canonical and requires reconciliation.",
                ) from exc

    def _assert_permission_active(self, snapshot: SourcePermissionSnapshot) -> None:
        try:
            self._permission_checker.assert_active(snapshot)
        except SourcePermissionRevokedError as exc:
            raise JobStepBlockedError(
                code="media.source_permission_revoked",
                message=str(exc) or "Media source permission is revoked.",
            ) from exc
        except Exception as exc:
            raise JobStepBlockedError(
                code="media.source_permission_unavailable",
                message="Media source permission could not be verified locally.",
            ) from exc

    def _assert_provider_identity(self, expected_revision_identity: str) -> None:
        try:
            current = provider_revision_identity(*self.provider_identity)
        except Exception as exc:
            raise JobStepBlockedError(
                code="media.provider_identity_unavailable",
                message="Media provider identity could not be verified locally.",
            ) from exc
        if current != expected_revision_identity:
            raise JobStepBlockedError(
                code="media.provider_identity_drift",
                message="Media provider identity drifted after execution reservation.",
            )

    def _assert_output_committed(
        self, output: Mapping[str, object], *, request: MediaOperationRequest
    ) -> None:
        try:
            self._output_verifier.assert_output_committed(output=output, request=request)
        except JobStepBlockedError:
            raise
        except Exception as exc:
            raise JobStepBlockedError(
                code="media.needs_reconcile",
                message="Media provider output is not verified by the canonical authority.",
            ) from exc


def _validate_checkpoint(
    value: Mapping[str, object],
    *,
    request: MediaOperationRequest,
) -> dict[str, object]:
    checkpoint = dict(_mapping({"checkpoint": value}, "checkpoint"))
    if set(checkpoint) != {"resume_step", "checkpoint_uri", "state_hash", "updated_at"}:
        raise ValueError("media checkpoint must use the formal Job checkpoint shape")
    if checkpoint.get("resume_step") != "execute_operation":
        raise ValueError("media checkpoint resume_step does not match the operation step")
    uri = _crp_ref(checkpoint.get("checkpoint_uri"), "checkpoint_uri")
    safe_job_ref = media_job_uri_segment(request.job_id)
    if f"/jobs/{request.job_id}/" not in uri and f"/jobs/{safe_job_ref}/" not in uri:
        raise ValueError("media checkpoint is not bound to the current Job")
    state_hash = checkpoint.get("state_hash")
    if not isinstance(state_hash, str) or _STATE_HASH.fullmatch(state_hash) is None:
        raise ValueError("media checkpoint state_hash is invalid")
    _string(checkpoint, "updated_at")
    return checkpoint


def _validate_output(value: Mapping[str, object]) -> dict[str, object]:
    output = dict(_mapping({"output": value}, "output"))
    if set(output) != {"kind", "uri", "object_id", "published"}:
        raise ValueError("media output must use the formal Job output shape")
    if output.get("kind") not in _OUTPUT_KINDS:
        raise ValueError("media output kind is invalid")
    _crp_ref(output.get("uri"), "output uri")
    _string(output, "object_id")
    if output.get("published") is not True:
        raise ValueError("media operation output must be committed before completion")
    return output


def _validate_consumed(value: Mapping[str, int], *, budget: Mapping[str, int]) -> dict[str, int]:
    consumed = dict(value)
    if set(consumed) != set(budget):
        raise ValueError("media consumption must exactly match the frozen budget fields")
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in consumed.values()):
        raise ValueError("media consumption must contain non-negative integers")
    return consumed


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"{key} is required")
    return item


def _optional_mapping(value: object, name: str) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object or null")
    return value


def _integer_mapping(value: Mapping[str, object], key: str) -> Mapping[str, int]:
    mapping = _mapping(value, key)
    if any(not isinstance(item, int) or isinstance(item, bool) for item in mapping.values()):
        raise ValueError(f"{key} must contain integer values")
    return dict(mapping)  # type: ignore[return-value]


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} is required")
    return item


def _crp_ref(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("crp://"):
        raise ValueError(f"{name} must be a crp reference")
    return value


def _permission_snapshot(
    media: Mapping[str, object], *, manifest: Mapping[str, object]
) -> SourcePermissionSnapshot:
    try:
        snapshot = SourcePermissionSnapshot.from_mapping(media.get("permission_snapshot"))
    except ValueError as exc:
        raise JobStepBlockedError(
            code="media.source_permission_unavailable",
            message="Media source permission snapshot is missing or invalid.",
        ) from exc
    if (
        snapshot.manifest_ref != _string(manifest, "ref")
        or snapshot.manifest_revision != _string(manifest, "revision")
    ):
        raise JobStepBlockedError(
            code="media.source_permission_revoked",
            message="Media source permission no longer binds the admitted manifest revision.",
        )
    return snapshot
