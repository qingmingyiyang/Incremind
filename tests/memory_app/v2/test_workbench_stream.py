import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.workbench_transport import negotiate_turn_response
from backend.memory_app.v2.turn_execution import TurnExecutionService
from backend.memory_app.v2.privacy import set_private_project
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.v2.test_workbench_ask import env as workbench_env, assemble, publish


@pytest.mark.parametrize("accept,expected", [
    (None, "json"), ("*/*", "json"), ("text/event-stream", "sse"),
    ("application/json;q=1, text/event-stream;q=0.5", "json"),
    ("application/json;q=0.2, text/event-stream;q=0.8", "sse"),
    ("text/event-stream;q=0, */*;q=1", "json"),
    ("application/json;q=0, */*;q=1", "sse"),
    ("text/*;q=0.7, application/json;q=0.6", "sse"),
    ("text/event-stream;q=0.4, text/*;q=1, application/json;q=0.5", "json"),
    ("application/json, text/event-stream", "json"),
])
def test_accept_quality_specificity_and_zero(accept, expected):
    assert negotiate_turn_response(accept, allow_stream=True) == expected


@pytest.mark.parametrize("accept", ["text/event-stream;q=0, application/json;q=0", "image/png"])
def test_unacceptable_representations_are_rejected(accept):
    with pytest.raises(HTTPException) as error:
        negotiate_turn_response(accept, allow_stream=True)
    assert error.value.status_code == 406


def events(response):
    return [(part.splitlines()[0].removeprefix("event: "),
             json.loads(part.split("data: ", 1)[1]))
            for part in response.text.split("\n\n") if part.startswith("event: ")]


def native_app(env, *, after_chunk=None, invalid=False, finish_reason="stop"):
    calls = []
    closed = []
    raw = json.dumps({"answer": "流式答案😀\n第二行", "citations": [999] if invalid else [1]})
    def completion(**request):
        calls.append(request)
        if not request.get("stream"):
            return {"choices": [{"finish_reason": "stop", "message": {"content": raw}}]}
        def stream():
            try:
                for offset in range(0, len(raw), 5):
                    yield {"choices": [{"delta": {"content": raw[offset:offset + 5]}, "finish_reason": None}]}
                    if after_chunk:
                        after_chunk()
                if finish_reason is not None:
                    yield {"choices": [{"delta": {}, "finish_reason": finish_reason}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
            finally:
                closed.append(True)
        return stream()
    config = ModelConfiguration(env.records, env.root, InMemorySecretStore(), completion_fn=completion)
    config.update("generation", {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
        "api_key": "test-private-value", "allow_remote": True, "expected_revision": 0})
    publish(env)
    app, _ = assemble(env.root, env.records, env.documents, env.service, config)
    return app, config, calls, closed


def test_sse_delivers_incremental_decoded_answer_and_persists_only_final(workbench_env):
    env = workbench_env
    app, _, calls, closed = native_app(env)
    body = {"project_id": "alpha", "text": "alpha?"}
    with TestClient(app) as http:
        result = http.post("/api/v2/workbench/turns", json=body,
            headers={"Accept": "text/event-stream", "Idempotency-Key": "stream-one"})
        parts = events(result)
        assert parts[0][0] == "started" and parts[-1][0] == "done"
        text = "".join(data["text"] for name, data in parts if name == "delta")
        assert text == "流式答案😀\n第二行"
        final = parts[-1][1]
        assert final["turn"]["receipt"]["ask"]["answer"] == text
        saved = http.get(f'/api/v2/workbench/threads/{final["thread_id"]}?project_id=alpha').json()
        assert saved["turns"] == [final["turn"]]
        replay = http.post("/api/v2/workbench/turns", json=body,
            headers={"Accept": "application/json", "Idempotency-Key": "stream-one"})
        assert replay.json() == final
    assert len(calls) == 1 and calls[0]["stream"] is True and closed == [True]
    assert calls[0]["max_tokens"] == 7000
    assert env.records.read("v2_turn_requests", "stream-one").payload["state"] == "completed"
    assert "test-private-value" not in result.text


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_context_matches_actual_gateway_schema_budget_and_persists(workbench_env, accept):
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, _estimate_input_tokens
    env = workbench_env
    app, config, calls, _ = native_app(env)
    config._gateway_factory = lambda **kwargs: LiteLLMCompletionGateway(
        **{**kwargs, "context_window_tokens": 24000})
    before = {name: env.records.list(name) for name in ("workspace_items", "recognitions", "documents")}
    with TestClient(app) as http:
        response = http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "alpha?"},
            headers={"Accept": accept})
        assert response.status_code == 200
        final = events(response)[-1][1] if accept == "text/event-stream" else response.json()
        receipt = final["turn"]["receipt"]["ask"]
        context = receipt["context"]
        assert context["window"] == 24000 and context["reserve"] == 7000
        assert sum(part["tokens"] for part in context["parts"]) == _estimate_input_tokens(calls[-1]["messages"])
        parts = {part["key"]: part for part in context["parts"]}
        assert parts["insight"]["count"] == 1 and parts["persona"]["count"] == 0
        assert parts["instruction"]["tokens"] > 0
        assert set(context) == {"window", "reserve", "parts", "entries", "egress"}
        assert all(set(part) == {"key", "count", "tokens"} for part in context["parts"])
        saved = http.get(f'/api/v2/workbench/threads/{final["thread_id"]}?project_id=alpha').json()
        assert saved["turns"][0]["receipt"]["ask"]["context"] == context
    assert {name: env.records.list(name) for name in before} == before
    assert "test-private-value" not in json.dumps(context)


def test_invalid_final_citations_emit_error_and_never_save_partial_answer(workbench_env):
    env = workbench_env
    app, _, calls, _ = native_app(env, invalid=True)
    body = {"project_id": "alpha", "text": "alpha?"}
    with TestClient(app) as http:
        result = http.post("/api/v2/workbench/turns", json=body,
            headers={"Accept": "text/event-stream", "Idempotency-Key": "invalid-one"})
        assert events(result)[-1] == ("error", {"code": "answer_generation_failed"})
        assert not any(name == "done" for name, _ in events(result))
        retry = http.post("/api/v2/workbench/turns", json=body, headers={"Idempotency-Key": "invalid-one"})
        assert retry.status_code == 502
    assert len(calls) == 1 and env.records.list("v2_turns") == ()
    assert env.records.read("v2_turn_requests", "invalid-one").payload["state"] == "failed"


def test_explicit_sse_q_zero_keeps_json_and_never_streams(workbench_env):
    app, _, calls, _ = native_app(workbench_env)
    with TestClient(app) as http:
        response = http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "alpha?"},
            headers={"Accept": "text/event-stream;q=0, */*;q=1"})
    assert response.headers["content-type"].startswith("application/json")
    assert len(calls) == 1 and not calls[0].get("stream")


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter"])
def test_incomplete_provider_stream_never_persists_valid_looking_json(workbench_env, finish_reason):
    from backend.memory_app.v2.followup import read_history
    before = {name: workbench_env.records.list(name) for name in ('v2_usage_insight', 'v2_usage_document')}
    app, _, calls, closed = native_app(workbench_env, finish_reason=finish_reason)
    with TestClient(app) as http:
        result = http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "alpha?"},
            headers={"Accept": "text/event-stream", "Idempotency-Key": "provider-incomplete"})
    assert len(calls) == 1 and closed == [True]
    if finish_reason is None:
        name, saved = events(result)[-1]
        assert name == 'done'
        turn = saved['turn']
        assert turn['receipt']['ask']['answer'] is None
        assert turn['receipt']['ask']['citations'] == []
        assert turn['receipt']['ask']['partial'] == ''
        assert turn['receipt']['ask']['interruption'] == 'connection'
        assert app.state.ai_turn_store.get_immutable_payload(turn['id'], 'product-answer-result-v2') is None
        assert {name: workbench_env.records.list(name) for name in before} == before
        assert read_history(workbench_env.records, 'alpha', saved['thread_id'], '下一问', query=workbench_env.domains.query)['turns'] == []
    else:
        assert events(result)[-1] == ("error", {"code": "answer_generation_failed"})
        assert workbench_env.records.list("v2_turns") == ()
        assert workbench_env.records.read("v2_turn_requests", "provider-incomplete").payload["state"] == "failed"


@pytest.mark.parametrize("quality", ["-1", "2", "0.1234", "invalid"])
def test_invalid_accept_quality_is_rejected(quality):
    with pytest.raises(HTTPException) as error:
        negotiate_turn_response("text/event-stream;q=" + quality, allow_stream=True)
    assert error.value.status_code == 400


def test_unacceptable_accept_never_starts_model_or_request_record(workbench_env):
    env = workbench_env
    app, _, calls, _ = native_app(env)
    with TestClient(app) as http:
        result = http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "alpha?"},
            headers={"Accept": "text/event-stream;q=0, application/json;q=0", "Idempotency-Key": "excluded-one"})
    assert result.status_code == 406 and calls == []
    assert env.records.read("v2_turn_requests", "excluded-one") is None


def test_stream_revalidates_privacy_and_closes_provider_before_more_delivery(workbench_env):
    env = workbench_env
    def revoke():
        if not env.records.read("v2_private_scopes", "alpha"):
            set_private_project(env.records, "alpha", True)
    app, _, calls, closed = native_app(env, after_chunk=revoke)
    with TestClient(app) as http:
        result = http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "alpha?"},
            headers={"Accept": "text/event-stream", "Idempotency-Key": "revoked-one"})
    parts = events(result)
    assert parts[-1][0] == "error" and not any(name == "done" for name, _ in parts)
    assert env.records.list("v2_turns") == () and closed == [True] and len(calls) == 1
    assert env.records.read("v2_turn_requests", "revoked-one").payload["state"] == "failed"


def test_a_key_cannot_be_reused_for_a_different_question(workbench_env):
    app, _, calls, _ = native_app(workbench_env)
    with TestClient(app) as http:
        first = http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "alpha?"},
            headers={"Idempotency-Key": "same-key"})
        second = http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "different?"},
            headers={"Idempotency-Key": "same-key"})
    assert first.status_code == 200 and second.status_code == 409
    assert second.json()["detail"] == "idempotency_key_conflict" and len(calls) == 1


def test_disconnect_does_not_cancel_business_and_same_key_cannot_generate_twice(workbench_env):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def execute(body, **kwargs):
            calls.append(body)
            entered.set()
            await release.wait()
            return {"thread_id": "thread-1", "turn": {"id": "turn-1"}}
        service = TurnExecutionService(workbench_env.records, execute, instance="one", read_result=lambda row: row["result"])
        task = asyncio.create_task(service.run({"project_id": "alpha", "text": "alpha?"}, "request-one"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(HTTPException) as running:
            await service.run({"project_id": "alpha", "text": "alpha?"}, "request-one")
        assert running.value.detail == "turn_in_progress"
        release.set()
        await asyncio.gather(*service.tasks.values())
        assert (await service.run({"project_id": "alpha", "text": "alpha?"}, "request-one"))["thread_id"] == "thread-1"
        assert len(calls) == 1
    asyncio.run(scenario())


def test_restart_marks_running_request_interrupted_without_resending(workbench_env):
    env = workbench_env
    body = {"project_id": "alpha", "text": "alpha?"}
    with env.records.begin() as tx:
        tx.put("v2_turn_requests", "interrupted-one", {"body": body, "state": "running", "instance": "old"}, expected_revision=0)
        tx.commit()
    calls = []
    async def execute(*args, **kwargs):
        calls.append(True)
    service = TurnExecutionService(env.records, execute, instance="new", read_result=lambda row: row)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.run(body, "interrupted-one"))
    assert error.value.detail == "turn_interrupted" and calls == []
