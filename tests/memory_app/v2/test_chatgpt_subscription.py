import json
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.memory_app.chatgpt_subscription import ChatGPTSubscriptions, SubscriptionError
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path):
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key())) | {"kid": "fixture"}
    state = {"calls": [], "scope": "openid offline_access chatgpt.tokens.use.direct", "subject": "synthetic-user", "nonce": None, "jwks_fail": False}
    def token():
        identity = jwt.encode({"iss": "https://auth.openai.com", "aud": "issued-client", "sub": state["subject"],
            "exp": int(time.time()) + 3600, "iat": int(time.time()), "nonce": state["nonce"], "email": "qa@example.invalid", **state.get("claims", {})},
            signing, algorithm="RS256", headers={"kid": "fixture"})
        return {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "token_type": "Bearer",
            "expires_in": 3600, "scope": state["scope"], "id_token": identity}
    def transport(request):
        state["calls"].append((request.method, request.url.path))
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": "https://auth.openai.com", "authorization_endpoint": "https://auth.openai.com/authorize",
                "token_endpoint": "https://auth.openai.com/token", "jwks_uri": "https://auth.openai.com/jwks", "revocation_endpoint": "https://auth.openai.com/revoke"})
        if request.url.path == "/token":
            if state.get("token_failure"):
                status, error = state["token_failure"]
                return httpx.Response(status, json={"error": error})
            if state.get("refresh_started") and b"grant_type=refresh_token" in request.content:
                state["refresh_started"].set()
                assert state["refresh_release"].wait(5), "refresh fixture was not released"
            if state.get("token_disconnect"):
                raise httpx.ReadError("synthetic credential response lost")
            if state.get("invalid_grant"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            value = token()
            if state.get("rotated_refresh") and b"grant_type=refresh_token" in request.content:
                value["refresh_token"] = state["rotated_refresh"]
            return httpx.Response(200, json=value)
        if request.url.path == "/jwks":
            return httpx.Response(503 if state["jwks_fail"] else 200, json={"keys": [public]})
        if request.url.path == "/revoke":
            return httpx.Response(503 if state.get("revoke_fail") else 200)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"models": [{"slug": "gpt-fixture", "display_name": "GPT Fixture", "visibility": "list"}]})
        raise AssertionError(request.url.path)
    records, secrets = SQLiteStructuredRecordStore(tmp_path / "store.sqlite3"), InMemorySecretStore()
    service = ChatGPTSubscriptions(records, secrets, client=httpx.Client(transport=httpx.MockTransport(transport)))
    yield service, state, secrets, records
    service.close()


def login(env, *, mutate=None):
    service, state, _, _ = env
    attempt = service.login(expected_revision=service.status()["revision"])
    params = parse_qs(urlsplit(attempt["authorization_url"]).query)
    state["nonce"] = params["nonce"][0]
    if mutate:
        mutate(params)
    callback = params["redirect_uri"][0] + "?" + urlencode({"state": params["state"][0], "code": "synthetic-code", "client_id": "issued-client"})
    with httpx.Client(follow_redirects=False, trust_env=False) as local:
        response = local.get(callback)
    deadline = time.monotonic() + 5
    while service.attempt(attempt["attempt_id"])["state"] == "pending":
        assert time.monotonic() < deadline
        time.sleep(.01)
    return attempt, params, response


def test_login_pkce_identity_and_safe_status(env):
    service, state, secrets, records = env
    attempt, params, response = login(env)
    assert response.status_code == 200
    assert params["client_id"] == ["dynamic_agent_client"] and params["code_challenge_method"] == ["S256"]
    assert params["resource"] == ["https://api.openai.com/v1"]
    assert params["ext_agent_host_id"][0].startswith("urn:uuid:")
    assert service.status()["connected"] and service.status()["sharing"]
    assert service.attempt(attempt["attempt_id"])["state"] == "completed"
    public = json.dumps(service.status()) + json.dumps([r.payload for r in records.list("v2_subscription_profiles")])
    assert "synthetic-access" not in public and "synthetic-refresh" not in public and "id_token" not in public
    assert service.token() == "synthetic-access"
    assert service.models() == [{"id": "gpt-fixture", "name": "GPT Fixture"}]


def test_cancelled_consumer_and_shutdown_preserve_started_refresh(env):
    service, state, _, records = env
    login(env)
    service._write_profile({**service._read_profile(), "expires_at": 0})
    state.update(refresh_started=threading.Event(), refresh_release=threading.Event(),
                 rotated_refresh="synthetic-rotated-refresh")
    service._owns_client = True  # Exercise the production-owned HTTP client shutdown path.
    before = state["calls"].count(("POST", "/token"))

    async def scenario():
        consumer = asyncio.create_task(asyncio.to_thread(service.token))
        assert await asyncio.to_thread(state["refresh_started"].wait, 5)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        close_entered = threading.Event()

        def shutdown():
            close_entered.set()
            service.close()

        closing = asyncio.create_task(asyncio.to_thread(shutdown))
        try:
            assert await asyncio.to_thread(close_entered.wait, 5)
            await asyncio.sleep(.1)
            assert not service.client.is_closed
            assert not closing.done()
        finally:
            state["refresh_release"].set()
            await asyncio.wait_for(closing, 5)

    asyncio.run(scenario())
    assert service.client.is_closed
    assert service._read_profile()["refresh_token"] == "synthetic-rotated-refresh"
    assert "pending_rotation" not in service._read_profile()
    assert state["calls"].count(("POST", "/token")) == before + 1
    assert records.read("v2_subscription_refresh", "chatgpt").payload["owner"] is None


def test_closed_subscription_service_cannot_start_login_or_use_saved_token(env):
    service, _, _, _ = env
    login(env)
    revision = service.status()["revision"]
    service.close()
    for operation in (service.token, service.models, lambda: service.login(expected_revision=revision)):
        with pytest.raises(SubscriptionError, match="subscription_closed"):
            operation()


def test_callback_bind_failure_stops_login_without_exposing_os_details(env, monkeypatch):
    import backend.memory_app.chatgpt_subscription as subscriptions
    service, state, _, _ = env

    def unavailable(*args, **kwargs):
        raise OSError(10048, "synthetic private socket diagnostic")

    monkeypatch.setattr(subscriptions, "ThreadingHTTPServer", unavailable)
    with pytest.raises(SubscriptionError) as caught:
        service.login(expected_revision=0)
    assert caught.value.code == "subscription_callback_unavailable"
    assert caught.value.status == 503
    assert "private" not in str(caught.value)
    assert not service._attempts
    assert not service.status()["connected"]
    assert not any(method == "POST" for method, _ in state["calls"])


@pytest.mark.parametrize("code", ["invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"])
def test_terminal_refresh_codes_clear_tokens_and_keep_registration(env, code):
    service, state, _, _ = env
    login(env)
    service._write_profile({**service._read_profile(), "expires_at": 0})
    state["token_failure"] = (400, {"code": code, "message": "synthetic private provider diagnostic"})
    before = state["calls"].count(("POST", "/token"))
    with pytest.raises(SubscriptionError, match="subscription_reauthentication_required"):
        service.token()
    assert service._read_profile() == {"client_id": "issued-client"}
    assert service.status()["state"] == "reauthentication_required"
    with pytest.raises(SubscriptionError):
        service.token()
    assert state["calls"].count(("POST", "/token")) == before + 1


def test_explicit_temporary_refresh_failure_preserves_grant_without_hidden_retry(env):
    service, state, _, _ = env
    login(env)
    service._write_profile({**service._read_profile(), "expires_at": 0})
    profile, revision = service._read_profile(), service.status()["revision"]
    state["token_failure"] = (503, {"code": "temporarily_unavailable"})
    before = state["calls"].count(("POST", "/token"))
    with pytest.raises(SubscriptionError) as caught:
        service.token()
    assert caught.value.status == 502
    assert service.status()["sharing"]
    assert service.status()["revision"] == revision
    assert service._read_profile() == profile
    assert state["calls"].count(("POST", "/token")) == before + 1
    del state["token_failure"]
    assert service.token() == "synthetic-access"
    assert state["calls"].count(("POST", "/token")) == before + 2


def test_invalid_client_does_not_discard_the_registered_session(env):
    service, state, _, _ = env
    login(env)
    service._write_profile({**service._read_profile(), "expires_at": 0})
    profile = service._read_profile()
    state["token_failure"] = (400, "invalid_client")
    with pytest.raises(SubscriptionError, match="subscription_client_invalid") as caught:
        service.token()
    assert caught.value.status == 400
    assert service._read_profile() == profile and service.status()["sharing"]


def test_callback_bind_failure_uses_existing_v2_error_delivery(env, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.v2.subscriptions import install_subscription_routes
    import backend.memory_app.chatgpt_subscription as subscriptions
    service, _, secrets, records = env

    def unavailable(*args, **kwargs):
        raise OSError(10048, "synthetic private socket diagnostic")

    monkeypatch.setattr(subscriptions, "ThreadingHTTPServer", unavailable)
    application = FastAPI()
    config = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=service)
    install_subscription_routes(application, models=config)
    with TestClient(application) as client:
        response = client.post("/api/v2/settings/subscriptions/login", json={"expected_revision": 0})
    assert response.status_code == 503
    assert response.json() == {"detail": "subscription_callback_unavailable"}


@pytest.mark.parametrize("mode", ["complete", "stream", "governed"])
@pytest.mark.parametrize("status,body,category,retryable", [
    (503, {"detail": "Selected model is at capacity"}, "capacity", True),
    (429, {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}, "usage_limit", False),
])
def test_subscription_failure_facts_survive_production_model_boundary(env, tmp_path, mode, status, body, category, retryable):
    from pydantic import BaseModel
    from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
    from backend.shared.llm.openai_responses import ResponsesCompletion
    from tests.memory_app.test_governed_generation import Control, Metadata, WireSink, _route
    service, _, secrets, records = env
    login(env)
    config = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=service)
    config.update("generation", {"expected_revision": 0, "allow_remote": True})
    config.select_subscription(model="gpt-fixture", expected_revision=0)
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, json=body)

    class Answer(BaseModel):
        answer: str

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        config._responses = ResponsesCompletion(client=client)
        with pytest.raises(ModelConfigurationError) as caught:
            if mode == "complete":
                config.complete([{"role": "user", "content": "synthetic question"}])
            elif mode == "stream":
                config.complete_stream([{"role": "user", "content": "synthetic question"}],
                                       response_model=Answer, on_delta=lambda _: None)
            else:
                route = _route(config)
                route["configuration"]["subscription_binding"] = config.public()["generation"]["subscription_binding"]
                config.complete_governed([{"role": "user", "content": "synthetic question"}], routing_snapshot=route,
                                        execution_control=Control(), metadata_sink=Metadata(), wire_attempt_sink=WireSink())
    error = caught.value
    assert error.status_code == status and error.category == category
    assert error.retryable is retryable and error.output_started is False
    assert "Selected model" not in str(error)
    assert len(calls) == 1 and calls[0]["store"] is False
    assert service.status()["sharing"]


def test_subscription_selection_uses_native_responses_and_preserves_api_profile(env, tmp_path):
    from backend.memory_app.model_config import ModelConfiguration
    service, state, secrets, records = env
    login(env)
    config = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=service)
    config.update("generation", {"expected_revision": 0, "model": "api-model", "base_url": "https://api.example.invalid/v1",
        "api_key": "synthetic-api-key", "allow_remote": True})
    previous = records.read("recognition_model_config", "generation")
    selected = config.select_subscription(model="gpt-fixture", expected_revision=0)
    assert selected["model"] == "gpt-fixture" and config.public()["generation_mode"]["mode"] == "subscription"
    assert records.read("recognition_model_config", "generation") == previous
    assert config.snapshot("generation")["api_key"] == "synthetic-access"
    config.select_subscription(model=None, expected_revision=selected["revision"])
    assert config.snapshot("generation")["model"] == "api-model"


def test_logout_invalidates_selected_generation_without_api_fallback(env, tmp_path):
    from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
    service, _, secrets, records = env
    login(env)
    config = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=service)
    config.select_subscription(model="gpt-fixture", expected_revision=0)
    service.logout(expected_revision=service.status()["revision"])
    with pytest.raises(ModelConfigurationError):
        config.snapshot("generation")


@pytest.mark.parametrize("stream", [False, True])
def test_native_responses_delivery_and_usage(stream):
    from backend.shared.llm.openai_responses import ResponsesCompletion
    requests = []
    response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"answer":"你好","citations":[]}'}]}],
        "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}}
    def remote(request):
        requests.append(json.loads(request.content))
        assert request.url == "https://api.openai.com/v1/responses"
        events = [{"type": "response.output_text.delta", "delta": '{"answer":"你好","citations":[]}'}, {"type": "response.completed", "response": response}]
        return httpx.Response(200, text="".join("data: " + json.dumps(e) + "\r\n\r\n" for e in events), headers={"content-type": "text/event-stream"})
    adapter = ResponsesCompletion(client=httpx.Client(transport=httpx.MockTransport(remote)))
    value = adapter(model="openai/gpt-fixture", messages=[{"role": "user", "content": "问"}], api_key="synthetic-access",
        max_tokens=123, stream=stream, response_format={"type": "json_object"})
    if stream:
        chunks = list(value)
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert chunks[-1]["usage"]["total_tokens"] == 13
        assert chunks[0]["choices"][0]["delta"]["content"] == response["output"][0]["content"][0]["text"]
    else:
        assert value["choices"][0]["message"]["content"] == response["output"][0]["content"][0]["text"]
    assert requests == [{"model": "gpt-fixture", "input": [{"role": "user", "content": "问"}], "store": False,
        "stream": True, "max_output_tokens": 123, "text": {"format": {"type": "json_object"}}}]


@pytest.mark.parametrize("event", ["response.incomplete", "response.failed", "error", "eof"])
def test_native_responses_requires_completed_terminal(event):
    from backend.shared.llm.openai_responses import ResponsesCompletion
    content = "" if event == "eof" else 'data: ' + json.dumps({"type": event}) + '\n\n'
    adapter = ResponsesCompletion(client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=content))))
    with pytest.raises(ValueError):
        list(adapter(model="gpt-fixture", messages=[], api_key="synthetic-access", stream=True))


@pytest.mark.parametrize("claims", [{"iss": "https://invalid.example"}, {"aud": "other-client"},
    {"exp": 1}, {"iat": 9999999999}, {"azp": "other-client"}, {"aud": ["issued-client", "other"]}])
def test_oidc_verifies_issuer_audience_time_and_authorized_party(env, claims):
    service, state, _, _ = env
    state["claims"] = claims
    attempt, _, _ = login(env)
    assert service.attempt(attempt["attempt_id"])["state"] == "failed"
    assert not service.status()["sharing"]


def test_invalid_refresh_grant_requires_login_and_never_retries(env):
    service, state, _, _ = env
    login(env)
    service._write_profile({**service._read_profile(), "expires_at": 0})
    state["invalid_grant"] = True
    before = state["calls"].count(("POST", "/token"))
    with pytest.raises(SubscriptionError, match="reauthentication"):
        service.token()
    with pytest.raises(SubscriptionError):
        service.token()
    assert state["calls"].count(("POST", "/token")) == before + 1
    assert service.status()["state"] == "reauthentication_required"


def test_uncertain_refresh_response_is_not_automatically_reposted(env):
    service, state, _, _ = env
    login(env)
    service._write_profile({**service._read_profile(), "expires_at": 0})
    state["token_disconnect"] = True
    before = state["calls"].count(("POST", "/token"))
    for _ in range(2):
        with pytest.raises(SubscriptionError):
            service.token()
    assert state["calls"].count(("POST", "/token")) == before + 1
    assert service.status()["state"] == "reauthentication_required"


def test_new_login_requires_explicit_reselection_and_restores_local_mode(env, tmp_path):
    from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
    service, _, secrets, records = env
    login(env)
    config = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=service)
    folder = tmp_path / 'data/models/qwen2.5-1.5b-instruct'
    folder.mkdir(parents=True)
    (folder / 'model.safetensors').write_bytes(b'synthetic-model')
    config.update_generation_mode(mode='local', local_enabled=True, local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
    config.select_subscription(model='gpt-fixture', expected_revision=0)
    assert not config.local_generation_allowed()
    assert config.generation_mode()['local_enabled']
    service.logout(expected_revision=service.status()['revision'])
    login(env)
    assert not config.public()['generation']['configured']
    with pytest.raises(ModelConfigurationError, match='selection_required'):
        config.snapshot('generation')
    config.select_subscription(model='gpt-fixture', expected_revision=1)
    assert config.snapshot('generation')['api_key'] == 'synthetic-access'
    config.update_generation_mode(mode='local', local_enabled=True, local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=config.generation_mode()['revision'])
    assert config.local_generation_allowed() and config.snapshot('generation')['model'] == 'qwen2.5-1.5b-instruct'


def test_two_service_instances_refresh_the_grant_once(env):
    service, state, secrets, records = env
    login(env)
    service._write_profile({**service._read_profile(), "expires_at": 0})
    second = ChatGPTSubscriptions(records, secrets, client=service.client)
    before = state['calls'].count(('POST', '/token'))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda instance: instance.token(), [service, second]))
        assert results == ['synthetic-access', 'synthetic-access']
        assert state['calls'].count(('POST', '/token')) == before + 1
    finally:
        second.close()


def test_logout_erases_tokens_even_when_remote_revoke_fails(env):
    service, state, secrets, _ = env
    login(env)
    state["revoke_fail"] = True
    assert service.logout(expected_revision=service.status()["revision"]) == {"remote_revoked": False}
    from backend.memory_app.chatgpt_subscription import PROFILE_REFERENCE
    assert json.loads(secrets.get_snapshot(PROFILE_REFERENCE).value) == {"client_id": "issued-client"}
    assert not service.status()["connected"]


def test_cancelled_callback_cannot_save_grant(env):
    service, state, _, _ = env
    attempt = service.login(expected_revision=0)
    assert service.status()["login"]["attempt_id"] == attempt["attempt_id"]
    service.cancel(attempt["attempt_id"])
    assert service.status()["login"] is None
    assert ("POST", "/token") not in state["calls"]
    assert not service.status()["connected"]


def test_protected_profile_survives_restart_without_plaintext_credentials(env, tmp_path):
    from backend.security.secrets import DPAPIFileSecretStore
    service, state, _, records = env
    protected = DPAPIFileSecretStore(tmp_path / "protected.json")
    service.secrets = protected
    login((service, state, protected, records))
    disk = (tmp_path / "protected.json").read_text()
    assert "synthetic-access" not in disk and "synthetic-refresh" not in disk and "qa@example.invalid" not in disk
    restarted = ChatGPTSubscriptions(records, DPAPIFileSecretStore(tmp_path / "protected.json"), client=service.client)
    try:
        assert restarted.status()["sharing"]
        assert restarted.token() == "synthetic-access"
    finally:
        restarted.close()


@pytest.mark.parametrize("stream", [False, True])
def test_selected_subscription_real_workbench_json_sse_and_idempotency(env, tmp_path, stream):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.workspace import install_workspace_routes
    from backend.memory_app.v2 import install_v2_routes
    from backend.recognition import RecognitionService, WorkScope
    from backend.shared.llm.openai_responses import ResponsesCompletion
    from core.document_engine import SQLiteDocumentRepository
    service, _, secrets, records = env
    login(env)
    owned_clients = []
    def owned_factory():
        client = httpx.Client(transport=httpx.MockTransport(remote))
        owned_clients.append(client)
        return client
    model = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=service,
        model_http_client_factory=owned_factory)
    model.select_subscription(model="gpt-fixture", expected_revision=0, allow_remote=True, expected_generation_revision=0)
    calls = []
    raw = json.dumps({"answer": "订阅答案😀", "citations": [1]})
    def remote(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-access"
        response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": raw}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
        events = [{"type": "response.output_text.delta", "delta": raw}, {"type": "response.completed", "response": response}]
        return httpx.Response(200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events), headers={"content-type": "text/event-stream"})
    borrowed = httpx.Client(transport=httpx.MockTransport(remote))
    original_hooks = {kind: list(values) for kind, values in borrowed.event_hooks.items()}
    model._responses = ResponsesCompletion(client=borrowed)
    documents, recognition = SQLiteDocumentRepository(records), RecognitionService(records)
    scope = WorkScope("local-user", "default")
    experience = recognition.stage_experience(scope=scope, content="每周回看原文。")
    candidate = recognition.propose(scope=scope, content="每周回看原文。", source_experience_ids=[experience])
    recognition.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user")
    app = FastAPI()
    from backend.memory_app.v2.devices import DeviceRegistry
    app.state.device_registry = DeviceRegistry(tmp_path / 'server')
    workspace = install_workspace_routes(app, runtime_root=tmp_path, records=records, models=model, documents=documents, service=recognition)
    install_v2_routes(app, runtime_root=tmp_path, records=records, models=model, documents=documents, service=recognition, workspace=workspace)
    with TestClient(app) as client:
        status = client.get('/api/v2/settings/subscriptions').json()
        assert status["sharing"] and status["selection"]["model"] == "gpt-fixture"
        assert client.patch('/api/v2/settings/subscriptions/selection', json={"model": "unknown", "expected_revision": 1}).status_code == 400
        headers = {"Idempotency-Key": "subscription-fixture", "Accept": "text/event-stream" if stream else "application/json"}
        body = {"intent": "ask", "project_id": "default", "text": "每周回看原文？"}
        response = client.post('/api/v2/workbench/turns', json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert "订阅答案" in response.text
        if stream:
            assert "event: delta" in response.text and "event: done" in response.text
        assert client.post('/api/v2/workbench/turns', json=body, headers=headers).status_code == 200
        assert len(calls) == 1
        selection = client.get('/api/v2/settings/subscriptions').json()
        assert client.patch('/api/v2/settings/subscriptions/selection', json={"model": "gpt-fixture", "expected_revision": selection['selection']['revision'],
            "allow_remote": False, "expected_generation_revision": selection['generation_revision']}).status_code == 200
        denied = client.post('/api/v2/workbench/turns', json=body, headers={"Idempotency-Key": "disabled-fixture"})
        assert denied.status_code == 409 and denied.json()["detail"] == "remote_disabled"
        assert len(calls) == 1
        assert len(owned_clients) == 1 and owned_clients[0].is_closed
        assert borrowed.is_closed is False and borrowed.event_hooks == original_hooks
    borrowed.close()


@pytest.mark.parametrize("problem", ["nonce", "subject", "scope"])
def test_invalid_identity_or_permission_cannot_authorize_generation(env, problem):
    service, state, _, _ = env
    if problem == "scope":
        state["scope"] = "openid offline_access"
    def mutate(params):
        if problem == "nonce":
            state["nonce"] = "wrong"
        elif problem == "subject":
            state["subject"] = ""
    login(env, mutate=mutate)
    assert not service.status()["sharing"]
    with pytest.raises(SubscriptionError):
        service.token()


def test_wrong_state_does_not_consume_attempt_and_host_id_survives_restart(env):
    service, state, secrets, records = env
    attempt = service.login(expected_revision=0)
    params = parse_qs(urlsplit(attempt["authorization_url"]).query)
    with httpx.Client(trust_env=False) as local:
        response = local.get(params["redirect_uri"][0] + "?state=wrong&code=synthetic-code&client_id=issued-client")
    assert response.status_code == 400 and service.attempt(attempt["attempt_id"])["state"] == "pending"
    assert not any(path == "/token" for _, path in state["calls"])
    second = ChatGPTSubscriptions(records, secrets, client=service.client)
    assert second.host_id() == params["ext_agent_host_id"][0]
    second.close()


def test_refresh_is_serialized_and_logout_removes_local_credentials(env):
    service, state, _, _ = env
    login(env)
    profile = service._read_profile()
    profile["expires_at"] = 0
    service._write_profile(profile)
    before = sum(path == "/token" for _, path in state["calls"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: service.token(), range(4))) == ["synthetic-access"] * 4
    assert sum(path == "/token" for _, path in state["calls"]) == before + 1
    service.logout(expected_revision=service.status()["revision"])
    assert not service.status()["connected"] and not service.status()["sharing"]
    with pytest.raises(SubscriptionError):
        service.token()


def test_refresh_checkpoint_survives_identity_service_failure_without_double_refresh(env):
    service, state, _, _ = env
    login(env)
    profile = service._read_profile()
    profile["expires_at"] = 0
    service._write_profile(profile)
    state["jwks_fail"] = True
    with pytest.raises(SubscriptionError):
        service.token()
    count = sum(path == "/token" for _, path in state["calls"])
    state["jwks_fail"] = False
    assert service.token() == "synthetic-access"
    assert sum(path == "/token" for _, path in state["calls"]) == count


def test_logout_revision_conflict_preserves_live_session(env):
    service, _, _, _ = env
    login(env)
    with pytest.raises(SubscriptionError) as error:
        service.logout(expected_revision=0)
    assert error.value.code == "subscription_revision_conflict"
    assert service.status()["sharing"]
