import json

import httpx
import pytest

from backend.shared.llm.openai_responses import ResponsesCompletion


@pytest.mark.parametrize("status,body,category,retryable", [
    (503, {"detail": "Selected model is at capacity"}, "capacity", True),
    (400, {"error": {"message": "Selected model is at capacity"}}, "capacity", True),
    (500, {"error": {"code": "server_error"}}, "transient", True),
    (503, {"detail": "Direct routing temporarily unavailable"}, "transient", True),
    (429, {"error": {"code": "rate_limit_exceeded"}}, "rate_limit", True),
    (429, {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}, "usage_limit", False),
    (429, {"error": {"code": "insufficient_quota", "message": "model is at capacity"}}, "usage_limit", False),
    (503, {"error": {"code": "subscription_sharing_usage_unavailable"}}, "transient", True),
    (503, {"error": {"code": "subscription_sharing_user_unavailable"}}, "transient", True),
    (403, {"error": {"code": "subscription_sharing_user_not_eligible"}}, "authorization", False),
    (401, {"detail": "model is at capacity"}, "authorization", False),
    (401, {"error": {"code": "server_error"}}, "authorization", False),
    (403, {"detail": "Serving region is restricted"}, "authorization", False),
    (400, {"error": {"code": "subscription_sharing_unsupported_capability"}}, "unsupported", False),
    (403, {"error": {"code": "subscription_sharing_route_not_supported"}}, "unsupported", False),
    (400, {"error": {"code": "content_filter", "message": "model is at capacity"}}, "refusal", False),
    (400, {"error": {"code": "unknown-private-code", "message": "private request details"}}, "unknown", False),
    (400, {"error": ["private malformed error"]}, "unknown", False),
])
@pytest.mark.parametrize("stream", [False, True])
def test_http_errors_are_safely_classified_without_hidden_retry(status, body, category, retryable, stream):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, json=body)

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        adapter = ResponsesCompletion(client=client)
        with pytest.raises(ValueError) as caught:
            value = adapter(model="gpt-fixture", messages=[], api_key="synthetic-access", stream=stream)
            if stream:
                list(value)
    error = caught.value
    assert error.status_code == status
    assert error.category == category
    assert error.retryable is retryable
    assert error.output_started is False
    assert str(error) == "subscription_response_failed"
    assert "private" not in repr(vars(error))
    assert len(calls) == 1
    assert calls[0]["store"] is False and calls[0]["stream"] is True


@pytest.mark.parametrize("code,category", [
    ("subscription_sharing_usage_limit_exceeded", "usage_limit"),
    ("subscription_sharing_usage_unavailable", "transient"),
    ("server_error", "transient"),
])
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("kind", ["response.failed", "error"])
def test_failed_stream_keeps_error_classification_and_blocks_retry_after_output(code, category, started, kind):
    events = [{"type": "response.output_text.delta", "delta": "partial"}] if started else []
    failure = {"code": code, "message": "synthetic private text"}
    events.append({"type": kind, "response": {"error": failure}} if kind == "response.failed"
                  else {"type": "error", **failure})
    calls = []

    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events),
                              headers={"content-type": "text/event-stream"})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        stream = ResponsesCompletion(client=client)(model="gpt-fixture", messages=[], api_key="synthetic-access", stream=True)
        if started:
            assert next(stream)["choices"][0]["delta"]["content"] == "partial"
        with pytest.raises(ValueError) as caught:
            list(stream)
    error = caught.value
    assert error.status_code == 200
    assert error.code == code and error.category == category
    assert error.output_started is started
    assert error.retryable is (not started and category == "transient")
    assert "private" not in str(error) + repr(vars(error))
    assert calls == ["/v1/responses"]


@pytest.mark.parametrize("first", [
    {"type": "response.function_call_arguments.delta", "delta": "{}"},
    {"type": "response.output_item.added", "item": {"type": "function_call"}},
    {"type": "response.custom_tool_call_input.delta", "delta": "partial"},
])
def test_error_after_tool_output_is_never_retryable(first):
    events = [first, {"type": "response.failed", "response": {"error": {"code": "server_error"}}}]
    body = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body,
                                      headers={"content-type": "text/event-stream"}))) as client:
        with pytest.raises(ValueError) as caught:
            list(ResponsesCompletion(client=client)(model="gpt-fixture", messages=[], api_key="synthetic-access", stream=True))
    assert caught.value.output_started is True
    assert caught.value.retryable is False


@pytest.mark.parametrize("body", ["not json private details", "x" * 70_000], ids=["non-json", "oversized"])
def test_non_json_or_oversized_failure_body_stays_private(body):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503, text=body))) as client:
        with pytest.raises(ValueError) as caught:
            ResponsesCompletion(client=client)(model="gpt-fixture", messages=[], api_key="synthetic-access")
    assert caught.value.category == "transient"
    assert str(caught.value) == "subscription_response_failed"
    assert len(repr(vars(caught.value))) < 500


def test_existing_gateway_propagates_safe_failure_with_one_guarded_wire_attempt():
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
    from backend.shared.llm.model_capabilities import ModelCapabilities
    from tests.memory_app.test_governed_generation import WireSink
    calls, guards, finished = [], [], []

    class Lease:
        def finish(self, status, *, error_code=None):
            finished.append((status, error_code))

    def guard(*args, **kwargs):
        guards.append(True)
        return Lease()

    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(503, json={"detail": "Selected model is at capacity"})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        gateway = LiteLLMCompletionGateway(provider="openai", model="gpt-fixture",
            base_url="https://api.openai.com/v1", api_key="synthetic-access", egress_guard=guard,
            completion_fn=ResponsesCompletion(client=client), capabilities=ModelCapabilities(structured_modes=("prompt",)))
        recorder = WireSink()
        with pytest.raises(ValueError) as caught:
            gateway.complete_text_with_usage([{"role": "user", "content": "synthetic question"}], wire_attempt_sink=recorder)
    assert caught.value.category == "capacity" and caught.value.retryable is True
    assert len(guards) == 1 and len(finished) == 1
    assert calls == ["/v1/responses"]
    assert recorder.begin_calls == 1
    assert [name for name, _ in recorder.handle.events] == ["invoke", "failed_transport"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("failure", ["eof", "read"])
def test_interrupted_native_stream_preserves_output_phase_for_json_and_sse(stream, started, failure):
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            if started:
                yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            if failure == "read":
                raise httpx.ReadError("synthetic private transport diagnostic")

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Interrupted(),
                                                    headers={"content-type": "text/event-stream"}))) as client:
        with pytest.raises(ValueError) as caught:
            value = ResponsesCompletion(client=client)(model="gpt-fixture", messages=[], api_key="synthetic-access", stream=stream)
            if stream:
                list(value)
    error = caught.value
    assert error.status_code == 200
    assert error.output_started is started
    assert error.retryable is (failure == "read" and not started)
    assert error.category == ("transient" if failure == "read" else "incomplete")
    assert "private" not in str(error) + repr(vars(error))


@pytest.mark.parametrize("stream", [False, True])
def test_connection_failure_before_response_is_sanitized_without_retry(stream):
    calls = []

    def provider(request):
        calls.append(request.url.path)
        raise httpx.ConnectTimeout("synthetic private transport diagnostic", request=request)

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(ValueError) as caught:
            value = ResponsesCompletion(client=client)(model="gpt-fixture", messages=[], api_key="synthetic-access", stream=stream)
            if stream:
                list(value)
    assert caught.value.status_code is None
    assert caught.value.retryable is True and caught.value.output_started is False
    assert "private" not in str(caught.value) + repr(vars(caught.value))
    assert calls == ["/v1/responses"]
