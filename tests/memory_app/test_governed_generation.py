from __future__ import annotations

from pathlib import Path

import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from core.ai_kernel.event_store import RunLeaseRevoked
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


PRIVATE_KEY = "test-private-value"


class Control:
    def __init__(self, *, remaining_timeout_ms: int = 75_000, fail_at: int | None = None):
        self.remaining_timeout_ms = remaining_timeout_ms
        self.fail_at = fail_at
        self.checkpoints = 0

    def checkpoint(self):
        self.checkpoints += 1
        if self.checkpoints == self.fail_at:
            raise RuntimeError("turn stopped")


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


class Gateway:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.after_response = None
        self.failure = None

    def complete_text_with_usage(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.failure is not None:
            raise self.failure
        if self.after_response is not None:
            self.after_response()
        return "accepted", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}, {
            "cache_read_input_tokens": 2,
        }


class WireHandle:
    def __init__(self):
        self.events = []

    def invoke_wire(self, handler):
        self.events.append(("invoke", {}))
        return handler()

    def succeeded(self, **values):
        self.events.append(("succeeded", values))

    def failed_transport(self, **values):
        self.events.append(("failed_transport", values))

    def consumer_cancelled(self):
        self.events.append(("consumer_cancelled", {}))


class WireSink:
    def __init__(self):
        self.handle = WireHandle()
        self.begin_calls = 0

    def begin_model_wire_attempt(self):
        self.begin_calls += 1
        return self.handle


def _settings(**changes):
    return {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "api_key": PRIVATE_KEY,
        "allow_remote": True,
        "expected_revision": 0,
        **changes,
    }


def _config(tmp_path: Path):
    gateways = []

    def factory(**kwargs):
        gateway = Gateway(**kwargs)
        gateways.append(gateway)
        return gateway

    config = ModelConfiguration(
        SQLiteStructuredRecordStore(tmp_path / "models.sqlite3"),
        tmp_path,
        InMemorySecretStore(),
        gateway_factory=factory,
    )
    config.update("generation", _settings())
    return config, gateways


def _route(config: ModelConfiguration):
    configuration = config.public()["generation"]
    return {
        "payload_ref": "crp://session/model-routing-snapshot/turn-1",
        "revision": "a" * 64,
        "prompt_cache_scope_identity": "b" * 64,
        "configuration": {
            key: configuration[key]
            for key in (
                "purpose", "provider", "base_url", "model", "allow_remote", "revision",
                "configured", "has_api_key",
            )
        },
        "execution_location": "remote",
    }


def test_governed_completion_records_route_control_wire_and_metadata(tmp_path):
    config, gateways = _config(tmp_path)
    control = Control()
    metadata = Metadata()
    wire = WireSink()

    text, result = config.complete_governed(
        [{"role": "user", "content": "hello"}],
        routing_snapshot=_route(config),
        execution_control=control,
        metadata_sink=metadata,
        wire_attempt_sink=wire,
    )

    assert text == "accepted"
    assert result["configuration_revision"] == 1
    assert control.checkpoints == 4
    assert gateways[0].calls[0][1]["wire_attempt_sink"] is wire
    assert gateways[0].calls[0][1]["timeout"] == 60.0
    assert [event[0] for event in metadata.events] == ["routed", "started", "cache", "completed"]
    assert metadata.events[0][1]["purpose"] == "primary"


def test_governed_completion_rejects_unknown_purpose_before_dispatch(tmp_path):
    config, gateways = _config(tmp_path)
    metadata = Metadata()
    with pytest.raises(ModelConfigurationError, match="model_call_purpose_invalid"):
        config.complete_governed(
            [{"role": "user", "content": "hello"}],
            routing_snapshot=_route(config), execution_control=Control(),
            metadata_sink=metadata, wire_attempt_sink=WireSink(), purpose="unknown",
        )
    assert gateways == []
    assert metadata.events == []

@pytest.mark.parametrize("streaming", [False, True])
def test_governed_structured_calls_keep_purpose_wire_and_live_deltas(tmp_path, monkeypatch, streaming):
    from backend.memory_app.structured_generation import AskOutput
    config, gateways = _config(tmp_path)
    control, metadata, wire, deltas = Control(), Metadata(), WireSink(), []

    def structured(gateway, messages, **kwargs):
        assert kwargs["wire_attempt_sink"] is wire
        return AskOutput(answer="first second", citations=[1]), {"input_tokens": 2, "output_tokens": 3}, {}

    def stream(gateway, messages, **kwargs):
        assert kwargs["wire_attempt_sink"] is wire
        kwargs["validate_current"]()
        kwargs["on_delta"]("first ")
        assert deltas == ["first "]  # Delivered before the provider finishes.
        kwargs["on_delta"]("second")
        return AskOutput(answer="first second", citations=[1]), {"input_tokens": 2, "output_tokens": 3}

    monkeypatch.setattr(Gateway, "complete_structured_with_usage", structured, raising=False)
    monkeypatch.setattr(Gateway, "stream_structured_with_usage", stream, raising=False)
    output, result = config.complete_governed(
        [{"role": "user", "content": "synthetic"}], routing_snapshot=_route(config),
        execution_control=control, metadata_sink=metadata, wire_attempt_sink=wire,
        response_model=AskOutput, on_delta=deltas.append if streaming else None, purpose="aux",
    )
    assert output.answer == "first second"
    assert deltas == (["first ", "second"] if streaming else [])
    assert metadata.events[0][1]["purpose"] == "aux"
    assert metadata.events[-1][0] == "completed"
    assert result["usage"] == {"input_tokens": 2, "output_tokens": 3}


def test_governed_completion_rejects_changed_configuration_after_provider_returns(tmp_path):
    config, gateways = _config(tmp_path)
    metadata = Metadata()
    gateway = None

    def replace_configuration():
        config.update(
            "generation",
            {
                "expected_revision": 1,
                "model": "changed-model",
                "api_key": "",
            },
        )

    # The fake transport changes settings after the wire has returned. The
    # Turn must discard that output instead of persisting a result from an old route.
    with pytest.raises(ModelConfigurationError, match="model_configuration_changed_during_request"):
        # Configure the only constructed gateway at factory time by patching the
        # list after it exists; no provider or credential is contacted.
        original_factory = config._gateway_factory

        def changing_factory(**kwargs):
            nonlocal gateway
            gateway = original_factory(**kwargs)
            gateway.after_response = replace_configuration
            return gateway

        config._gateway_factory = changing_factory
        config.complete_governed(
            [{"role": "user", "content": "hello"}],
            routing_snapshot=_route(config),
            execution_control=Control(),
            metadata_sink=metadata,
            wire_attempt_sink=WireSink(),
        )

    assert gateway is not None
    assert [event[0] for event in metadata.events] == ["routed", "started", "failed"]
    assert PRIVATE_KEY not in str(metadata.events)


@pytest.mark.parametrize("remaining_timeout_ms, fail_at", [(0, None), (10_000, 3)])
def test_governed_completion_does_not_wire_after_timeout_or_cancellation(tmp_path, remaining_timeout_ms, fail_at):
    config, gateways = _config(tmp_path)
    metadata = Metadata()

    with pytest.raises((ModelConfigurationError, RuntimeError)):
        config.complete_governed(
            [{"role": "user", "content": "hello"}],
            routing_snapshot=_route(config),
            execution_control=Control(remaining_timeout_ms=remaining_timeout_ms, fail_at=fail_at),
            metadata_sink=metadata,
            wire_attempt_sink=WireSink(),
        )

    assert (not gateways) or gateways[0].calls == []
    if fail_at is not None:
        assert [event[0] for event in metadata.events] == ["routed", "started", "failed"]


def test_governed_completion_rejects_snapshot_with_changed_public_model_before_wire(tmp_path):
    config, gateways = _config(tmp_path)
    route = _route(config)
    route["configuration"] = {**route["configuration"], "model": "other-model"}

    with pytest.raises(ModelConfigurationError, match="model_routing_snapshot_invalid"):
        config.complete_governed(
            [{"role": "user", "content": "hello"}],
            routing_snapshot=route,
            execution_control=Control(),
            metadata_sink=Metadata(),
            wire_attempt_sink=WireSink(),
        )

    assert gateways == []


def test_governed_completion_requires_a_durable_wire_attempt_sink(tmp_path):
    config, gateways = _config(tmp_path)
    with pytest.raises(ModelConfigurationError, match="model_wire_attempt_sink_invalid"):
        config.complete_governed(
            [{"role": "user", "content": "hello"}],
            routing_snapshot=_route(config),
            execution_control=Control(),
            metadata_sink=Metadata(),
            wire_attempt_sink=None,
        )

    assert gateways == []


def test_governed_completion_preserves_revoked_turn_lease(tmp_path):
    config, gateways = _config(tmp_path)
    original_factory = config._gateway_factory

    def revoked_factory(**kwargs):
        gateway = original_factory(**kwargs)
        gateway.failure = RunLeaseRevoked()
        return gateway

    config._gateway_factory = revoked_factory
    metadata = Metadata()
    with pytest.raises(RunLeaseRevoked):
        config.complete_governed(
            [{"role": "user", "content": "hello"}],
            routing_snapshot=_route(config),
            execution_control=Control(),
            metadata_sink=metadata,
            wire_attempt_sink=WireSink(),
        )

    assert len(gateways) == 1
    assert [event[0] for event in metadata.events] == ["routed", "started", "failed"]


def test_governed_provider_failure_does_not_mask_cancellation_or_expose_its_traceback(tmp_path):
    import traceback

    config, _ = _config(tmp_path)
    original_factory = config._gateway_factory

    def failing_factory(**kwargs):
        gateway = original_factory(**kwargs)
        gateway.failure = RuntimeError("private-provider-request-body")
        return gateway

    config._gateway_factory = failing_factory
    with pytest.raises(RuntimeError, match="turn stopped") as stopped:
        config.complete_governed([{"role": "user", "content": "hello"}],
            routing_snapshot=_route(config), execution_control=Control(fail_at=4),
            metadata_sink=Metadata(), wire_attempt_sink=WireSink())
    assert "private-provider-request-body" not in "".join(traceback.format_exception(stopped.value))


def test_governed_completion_starts_real_wire_attempt_and_omits_empty_cache_metadata(tmp_path):
    from backend.memory_app.turn_routing import RecognitionModelRoutingSnapshotAuthority
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore

    completion_calls = []

    def completion(**request):
        completion_calls.append(request)
        return {
            "choices": [{"message": {"content": "accepted"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }

    config = ModelConfiguration(
        SQLiteStructuredRecordStore(tmp_path / "models.sqlite3"),
        tmp_path,
        InMemorySecretStore(),
        gateway_factory=LiteLLMCompletionGateway,
        completion_fn=completion,
    )
    config.update("generation", _settings())
    metadata = Metadata()
    wire = WireSink()
    turns = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    turns.claim_turn(dict(turn_id="turn-one", session_id="session-one",
                         operation_id="operation-one", idempotency_key="key-one"))
    route = RecognitionModelRoutingSnapshotAuthority(config, turns).acquire(
        turn_id="turn-one", project_id="project-a", context_packet_id="packet-one",
        project_profile_id="profile-one", project_profile_revision=1,
        boundary_profile_id="boundary-one", boundary_profile_revision=1,
        capability_ids=("recognition.task.execute",), agent_binding=None, allow_remote=True,
    )

    text, _ = config.complete_governed(
        [{"role": "user", "content": "hello"}],
        routing_snapshot=route.generation_binding(),
        execution_control=Control(),
        metadata_sink=metadata,
        wire_attempt_sink=wire,
    )

    assert text == "accepted"
    assert len(completion_calls) == 1
    assert wire.begin_calls == 1
    assert [event[0] for event in wire.handle.events] == ["invoke", "succeeded"]
    assert [event[0] for event in metadata.events] == ["routed", "started", "completed"]


@pytest.mark.parametrize("outcome", ["valid", "invalid_json", "format_rejected", "changed_configuration"])
def test_governed_structured_real_gateway_is_single_wire_and_preserves_authority(tmp_path, outcome):
    from pydantic import BaseModel, StrictStr

    class Output(BaseModel):
        answer: StrictStr

    calls = []

    def completion(**request):
        calls.append(request)
        if outcome == "format_rejected":
            raise RuntimeError("response_format json_schema is not supported")
        if outcome == "changed_configuration":
            config.update("generation", {"expected_revision": 1, "model": "changed-model", "api_key": ""})
        return {"choices": [{"message": {"content": "not-json" if outcome == "invalid_json" else '{"answer":"accepted"}'}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}

    config = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / "models.sqlite3"), tmp_path,
        InMemorySecretStore(), gateway_factory=LiteLLMCompletionGateway, completion_fn=completion)
    config.update("generation", _settings())
    metadata, wire = Metadata(), WireSink()
    options = dict(routing_snapshot=_route(config), execution_control=Control(remaining_timeout_ms=4000),
        metadata_sink=metadata, wire_attempt_sink=wire, purpose="aux", response_model=Output, max_tokens=400)
    if outcome == "valid":
        output, result = config.complete_governed([{"role": "user", "content": "return answer"}], **options)
        assert isinstance(output, Output)
        assert output.answer == "accepted"
        assert result["usage"]["total_tokens"] == 5
        assert [event[0] for event in metadata.events] == ["routed", "started", "completed"]
    else:
        with pytest.raises(ModelConfigurationError) as failure:
            config.complete_governed([{"role": "user", "content": "return answer"}], **options)
        assert "not-json" not in str(failure.value)
        assert [event[0] for event in metadata.events] == ["routed", "started", "failed"]
    assert len(calls) == wire.begin_calls == 1
    assert 0 < calls[0]["timeout"] <= 4
    assert calls[0]["max_tokens"] == 400
    assert metadata.events[0][1]["purpose"] == "aux"
    assert PRIVATE_KEY not in str(metadata.events)
