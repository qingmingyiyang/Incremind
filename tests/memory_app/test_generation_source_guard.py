from __future__ import annotations

import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.recognition import RecognitionConflict
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


PRIVATE_KEY = "source-guard-private-key"
PRIVATE_INPUT = "source-guard-private-input"


def _config(tmp_path, completion):
    config = ModelConfiguration(
        SQLiteStructuredRecordStore(tmp_path / "models.sqlite3"),
        tmp_path,
        InMemorySecretStore(),
        gateway_factory=LiteLLMCompletionGateway,
        completion_fn=completion,
    )
    config.update(
        "generation",
        {
            "base_url": "https://example.com/v1",
            "model": "example-model",
            "api_key": PRIVATE_KEY,
            "allow_remote": True,
            "expected_revision": 0,
        },
    )
    return config


def _response():
    return {
        "choices": [{"message": {"content": "accepted"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def _revoked_after(check_number: int):
    checks = {"count": 0}

    def validate_current():
        checks["count"] += 1
        if checks["count"] >= check_number:
            raise RecognitionConflict("source egress snapshot conflicted")

    return validate_current, checks


def test_generation_source_guard_blocks_revoked_source_before_gateway_work(tmp_path):
    sent = []
    config = _config(tmp_path, lambda **request: sent.append(request) or _response())
    validate_current, checks = _revoked_after(1)

    with pytest.raises(RecognitionConflict, match="source egress snapshot conflicted"):
        config.complete(
            [{"role": "user", "content": PRIVATE_INPUT}],
            validate_current=validate_current,
        )

    assert checks["count"] == 1
    assert sent == []


def test_generation_source_guard_blocks_revocation_at_real_gateway_wire_boundary(tmp_path):
    sent = []
    config = _config(tmp_path, lambda **request: sent.append(request) or _response())
    validate_current, checks = _revoked_after(2)

    with pytest.raises(RecognitionConflict, match="source egress snapshot conflicted"):
        config.complete(
            [{"role": "user", "content": PRIVATE_INPUT}],
            validate_current=validate_current,
        )

    # The LiteLLM gateway invokes its egress guard after constructing the
    # request and immediately before completion_fn.  No provider wire occurs.
    assert checks["count"] == 2
    assert sent == []


def test_generation_source_guard_discards_response_when_source_is_revoked(tmp_path):
    sent = []
    config = _config(tmp_path, lambda **request: sent.append(request) or _response())
    validate_current, checks = _revoked_after(3)

    with pytest.raises(RecognitionConflict, match="source egress snapshot conflicted"):
        config.complete(
            [{"role": "user", "content": PRIVATE_INPUT}],
            validate_current=validate_current,
        )

    assert checks["count"] == 3
    assert len(sent) == 1


def test_generation_complete_without_source_guard_remains_compatible(tmp_path):
    sent = []
    config = _config(tmp_path, lambda **request: sent.append(request) or _response())

    text, metadata = config.complete([{"role": "user", "content": "hello"}])

    assert text == "accepted"
    assert metadata["configuration_revision"] == 1
    assert len(sent) == 1


def test_generation_provider_failure_does_not_leak_source_input_or_key(tmp_path):
    def failing_completion(**request):
        raise RuntimeError(f"provider saw {request['messages']} with {request['api_key']}")

    config = _config(tmp_path, failing_completion)

    with pytest.raises(ModelConfigurationError) as failure:
        config.complete([{"role": "user", "content": PRIVATE_INPUT}])

    assert PRIVATE_INPUT not in str(failure.value)
    assert PRIVATE_KEY not in str(failure.value)
