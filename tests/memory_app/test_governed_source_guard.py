from __future__ import annotations

import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.recognition import RecognitionConflict
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


class Control:
    remaining_timeout_ms = 75_000

    def checkpoint(self):
        return None


class Metadata:
    def __init__(self):
        self.events = []

    def model_call_routed(self, **values):
        self.events.append(("routed", values))

    def model_call_started(self, **values):
        self.events.append(("started", values))

    def model_call_completed(self, **values):
        self.events.append(("completed", values))

    def model_call_failed(self):
        self.events.append(("failed", {}))

    def model_call_cache_observed(self, **values):
        self.events.append(("cache", values))


class WireHandle:
    def __init__(self):
        self.events = []

    def invoke_wire(self, handler):
        self.events.append("invoke")
        return handler()

    def succeeded(self, **_values):
        self.events.append("succeeded")

    def failed_transport(self, **_values):
        self.events.append("failed_transport")

    def consumer_cancelled(self):
        self.events.append("consumer_cancelled")


class WireSink:
    def __init__(self):
        self.begin_calls = 0
        self.handle = WireHandle()

    def begin_model_wire_attempt(self):
        self.begin_calls += 1
        return self.handle


def _config(tmp_path, completion):
    config = ModelConfiguration(
        SQLiteStructuredRecordStore(tmp_path / "models.sqlite3"),
        tmp_path,
        InMemorySecretStore(),
        gateway_factory=LiteLLMCompletionGateway,
        completion_fn=completion,
    )
    config.update("generation", {
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key": "test-private-key",
        "allow_remote": True,
        "expected_revision": 0,
    })
    return config


def _route(config):
    configuration = config.public()["generation"]
    return {
        "payload_ref": "crp://session/model-routing-snapshot/turn-1",
        "revision": "a" * 64,
        "prompt_cache_scope_identity": "b" * 64,
        "configuration": {key: configuration[key] for key in (
            "purpose", "provider", "base_url", "model", "allow_remote", "revision",
            "configured", "has_api_key",
        )},
        "execution_location": "remote",
    }


def _response():
    return {
        "choices": [{"message": {"content": "accepted"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def _revoked_after(check_number):
    checks = {"count": 0}

    def validate_current():
        checks["count"] += 1
        if checks["count"] >= check_number:
            raise RecognitionConflict("source egress snapshot conflicted")

    return validate_current, checks


def _call(config, *, metadata, wire, validate_current=None):
    return config.complete_governed(
        [{"role": "user", "content": "private source"}],
        routing_snapshot=_route(config),
        execution_control=Control(),
        metadata_sink=metadata,
        wire_attempt_sink=wire,
        validate_current=validate_current,
    )


def test_governed_source_guard_denies_before_any_wire(tmp_path):
    sent = []
    config = _config(tmp_path, lambda **request: sent.append(request) or _response())
    validate_current, checks = _revoked_after(1)
    metadata, wire = Metadata(), WireSink()

    with pytest.raises(RecognitionConflict, match="source egress snapshot conflicted"):
        _call(config, metadata=metadata, wire=wire, validate_current=validate_current)

    assert checks["count"] == 1
    assert sent == []
    assert wire.begin_calls == 0
    assert metadata.events == []


def test_governed_source_guard_denies_at_actual_gateway_wire_boundary(tmp_path):
    sent = []
    config = _config(tmp_path, lambda **request: sent.append(request) or _response())
    validate_current, checks = _revoked_after(2)
    metadata, wire = Metadata(), WireSink()

    with pytest.raises(RecognitionConflict, match="source egress snapshot conflicted"):
        _call(config, metadata=metadata, wire=wire, validate_current=validate_current)

    assert checks["count"] == 2
    assert sent == []
    assert wire.begin_calls == 0
    assert [name for name, _ in metadata.events] == ["routed", "started", "failed"]


def test_governed_source_guard_discards_response_before_metadata_completion(tmp_path):
    sent = []
    config = _config(tmp_path, lambda **request: sent.append(request) or _response())
    validate_current, checks = _revoked_after(3)
    metadata, wire = Metadata(), WireSink()

    with pytest.raises(RecognitionConflict, match="source egress snapshot conflicted"):
        _call(config, metadata=metadata, wire=wire, validate_current=validate_current)

    assert checks["count"] == 3
    assert len(sent) == 1
    assert wire.begin_calls == 1
    assert [name for name, _ in metadata.events] == ["routed", "started", "failed"]


def test_governed_completion_without_source_guard_remains_compatible(tmp_path):
    sent = []
    config = _config(tmp_path, lambda **request: sent.append(request) or _response())

    text, result = _call(config, metadata=Metadata(), wire=WireSink())

    assert text == "accepted"
    assert result["configuration_revision"] == 1
    assert len(sent) == 1
