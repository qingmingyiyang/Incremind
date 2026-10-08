from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable, Literal, Protocol, TypeVar


WireResultT = TypeVar("WireResultT")


ModelCapability = Literal[
    "text", "structured", "embedding", "vision", "transcription", "image_generation",
]
ModelCallPurpose = Literal["primary", "aux", "probe"]


def validate_model_call_purpose(value: object) -> ModelCallPurpose:
    """Return the only purpose values that may influence model governance."""

    if value not in {"primary", "aux", "probe"}:
        raise ValueError("model call purpose is invalid")
    return value  # type: ignore[return-value]


class ModelExecutionControlPort(Protocol):
    """Provider-neutral deadline and cooperative cancellation control."""

    @property
    def remaining_timeout_ms(self) -> int: ...

    @property
    def cancel_requested(self) -> bool: ...

    def checkpoint(self) -> None: ...


class ModelCallMetadataSinkPort(Protocol):
    """Receives metadata only; model input and output are deliberately excluded."""

    def model_call_routed(
        self,
        *,
        snapshot_ref: str,
        snapshot_revision: str,
        prompt_cache_scope_identity: str,
        provider: str,
        model: str,
        execution_location: str,
        purpose: ModelCallPurpose,
    ) -> None: ...

    def model_call_started(self, *, provider: str, model: str) -> None: ...

    def model_call_completed(self, *, usage: Mapping[str, int]) -> None: ...

    def model_call_failed(self) -> None: ...

    def model_call_cache_observed(self, *, observation: Mapping[str, int]) -> None: ...


class ModelWireAttemptHandlePort(Protocol):
    """Durably closes exactly one authorized provider wire attempt."""

    def succeeded(
        self,
        *,
        usage: Mapping[str, int],
        cache_observation: Mapping[str, int] | None,
    ) -> None: ...

    def failed_transport(self, *, error_code: str) -> None: ...

    def consumer_cancelled(self) -> None: ...

    def invoke_wire(self, handler: Callable[[], WireResultT]) -> WireResultT: ...


class ModelWireAttemptSinkPort(Protocol):
    """Begins one metadata-only attempt after egress authorization."""

    def begin_model_wire_attempt(self) -> ModelWireAttemptHandlePort: ...


@dataclass(frozen=True, slots=True)
class ModelRequest:
    capability: ModelCapability
    input: str
    parameters: Mapping[str, object]
    privacy_scope: str
    execution_control: ModelExecutionControlPort | None = None
    metadata_sink: ModelCallMetadataSinkPort | None = None
    wire_attempt_sink: ModelWireAttemptSinkPort | None = None
    purpose: ModelCallPurpose = "primary"

    def __post_init__(self) -> None:
        validate_model_call_purpose(self.purpose)


@dataclass(frozen=True, slots=True)
class ModelResult:
    output: object
    provider: str
    model: str
    usage: Mapping[str, int]
    routing_evidence: Mapping[str, object] | None = None


class ModelGatewayPort(Protocol):
    """Executes a model request without deciding memory trust or persistence."""

    def invoke(self, request: ModelRequest) -> ModelResult:
        """Return provider metadata with the model output."""
