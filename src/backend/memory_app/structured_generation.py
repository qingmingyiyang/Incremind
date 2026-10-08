"""Shared output contracts for memory generation and legacy model adapters."""
import inspect
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator
from backend.shared.llm.json_mode import validate_json_response


class InvalidStructuredOutput(ValueError):
    """Schema failure, distinct from model or source-authority failures."""


class AskOutput(BaseModel):
    answer: StrictStr
    citations: list[Annotated[StrictInt, Field(ge=1)]]

    @field_validator("answer")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("empty answer")
        return value


class GeneratedInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr
    conditions: list[StrictStr]

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("empty insight")
        return value


class InsightOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    insights: list[GeneratedInsight]


def generate_structured(models, messages, *, response_model, max_tokens, validate_current, on_delta=None, wire_attempt_sink=None, timeout_seconds=None, retry_policy=None):
    """Use native structured generation; retain the existing text-adapter contract.

    Custom/domain adapters that implement only complete still receive identical
    messages and authority callbacks. Their outputs pass the same shared schema.
    """
    stream = getattr(models, "complete_stream", None)
    if on_delta is not None and callable(stream):
        return stream(messages, response_model=response_model, max_tokens=max_tokens,
            validate_current=validate_current, on_delta=on_delta)
    native = getattr(models, "complete_structured", None)
    wire = {'wire_attempt_sink': wire_attempt_sink} if wire_attempt_sink is not None else {}
    method = native if callable(native) else models.complete
    if timeout_seconds is not None and "timeout_seconds" in inspect.signature(method).parameters:
        wire["timeout_seconds"] = timeout_seconds
    if retry_policy is not None and "retry_policy" in inspect.signature(method).parameters:
        wire["retry_policy"] = retry_policy
    if callable(native):
        result = native(messages, response_model=response_model, max_tokens=max_tokens,
            validate_current=validate_current, **wire)
        if on_delta is not None:
            on_delta(result[0].answer)
        return result
    text, metadata = models.complete(messages, max_tokens=max_tokens, validate_current=validate_current, **wire)
    validate_current()
    try:
        output = validate_json_response(raw_text=text, response_model=response_model)
    except (ValueError, TypeError):
        raise InvalidStructuredOutput("invalid structured output") from None
    if on_delta is not None:
        on_delta(output.answer)
    return output, metadata
