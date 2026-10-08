"""Image calls reuse the durable auxiliary Turn and its original wire sink."""
import inspect
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .memory_turn import MemoryTurn
from .policy_runtime import retry_policy_for


class ReadImage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: StrictStr
    description: StrictStr = Field(max_length=200)


class ImageReadOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    images: list[ReadImage] = Field(min_length=1)


def read_images(records, models, *, project, key, materials, validate,
                freeze_request, validate_request, remote_allowed, messages, load_messages):
    turn = MemoryTurn(records, models, kind='media.image_read', purpose='vision',
        project=project, key=key, materials=materials, validate=validate,
        freeze_request=freeze_request, validate_request=validate_request,
        remote_allowed=remote_allowed)
    def invoke(control, current):
        frozen = load_messages(turn.request)
        retry = ({'retry_policy': retry_policy_for(turn.request),
                  'timeout_seconds': control.remaining_timeout_ms / 1000}
                 if 'retry_policy' in inspect.signature(models.complete_vision).parameters else {})
        output, metadata = models.complete_vision(frozen, response_model=ImageReadOutput,
            validate_current=current, wire_attempt_sink=control, **retry)
        expected = sum(part.get('type') == 'image_url' for message in frozen
            for part in message.get('content', []) if isinstance(part, dict))
        if len(output.images) != expected:
            raise ValueError('image_read_output_count_invalid')
        return output, metadata
    output, metadata = turn.generate(messages, response_model=ImageReadOutput,
        max_tokens=4000, invoke=invoke)
    return output, metadata, turn.turn_id
