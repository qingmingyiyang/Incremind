from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from core.model_gateway import ModelCallMetadataSinkPort, ModelExecutionControlPort


class NestedModelCallHandlePort(ModelCallMetadataSinkPort):
    @property
    def model_request_id(self) -> str: ...

    def finalize(self, *, error_code: str | None) -> tuple[str, ...]: ...


def execution_control_from(
    request: Mapping[str, object],
) -> ModelExecutionControlPort | None:
    """Read the dispatcher-owned control without coupling providers to ai_kernel."""

    control = request.get("execution_context")
    if control is None:
        return None
    if not callable(getattr(control, "checkpoint", None)):
        raise ValueError("tool execution control is invalid")
    remaining = getattr(control, "remaining_timeout_ms", None)
    cancelled = getattr(control, "cancel_requested", None)
    if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < 0:
        raise ValueError("tool execution control timeout is invalid")
    if not isinstance(cancelled, bool):
        raise ValueError("tool execution control cancellation state is invalid")
    return cast(ModelExecutionControlPort, control)


def execution_checkpoint(request: Mapping[str, object]) -> ModelExecutionControlPort | None:
    control = execution_control_from(request)
    if control is not None:
        control.checkpoint()
    return control


def begin_nested_model_call(
    request: Mapping[str, object],
    *,
    invocation_key: str | None = None,
    purpose: str | None = None,
) -> NestedModelCallHandlePort:
    """Begin one dispatcher-owned model lifecycle without treating tool control as its sink."""

    control = request.get("execution_context")
    take = getattr(control, "take_nested_model_handle", None)
    if not callable(take):
        raise ValueError("nested model lifecycle is unavailable")
    handle = (
        take()
        if invocation_key is None and purpose is None
        else take(invocation_key=invocation_key, purpose=purpose)
    )
    required = (
        "model_call_routed",
        "model_call_started",
        "model_call_completed",
        "model_call_failed",
        "model_call_cache_observed",
        "finalize",
    )
    if any(not callable(getattr(handle, name, None)) for name in required):
        raise ValueError("nested model lifecycle handle is invalid")
    return cast(NestedModelCallHandlePort, handle)
