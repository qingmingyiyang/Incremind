from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.routes.credential_capture import capture_credential
from backend.api.routes.settings import save_provider_secret
from backend.api.contracts import SaveProviderSecretRequest
from backend.security import InMemorySecretStore


class _Request:
    def __init__(self, payload: object, *, header: str = "desktop-secret") -> None:
        self._payload = payload
        self.headers = {"X-Chriptmas-Desktop-Session": header}
        self.app = SimpleNamespace(state=SimpleNamespace())

    async def json(self) -> object:
        return self._payload


@pytest.fixture(autouse=True)
def _desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_MODE", "desktop_production")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_SECRET", "desktop-secret" + "x" * 32)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_INSTANCE_ID", "instance")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_NONCE", "n" * 43)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_PROTOCOL_VERSION", "desktop-loopback/1")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT", "2099-01-01T00:00:00+00:00")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN", "http://127.0.0.1:1234")


def _request(payload: object, *, authorized: bool = True) -> _Request:
    header = "desktop-secret" + "x" * 32 if authorized else "wrong"
    return _Request(payload, header=header)


@pytest.mark.asyncio
async def test_capture_writes_secret_finalizes_provider_and_never_echoes_value(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemorySecretStore()
    request = _request({
        "credential_kind": "provider_api_key",
        "credential_subject": "openai",
        "value": "sensitive-provider-value",
        "command_id": "cmd-credential-0001",
    })

    finalized: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "backend.api.provider_credentials.store_provider_credential",
        lambda _container, provider_id, value: (finalized.append((provider_id, value)), _container.secret_store.set(f"provider:{provider_id}", value), _container.secret_store.get_generation(f"provider:{provider_id}"))[-1],
    )
    response = await capture_credential(request, SimpleNamespace(secret_store=store))

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body == {"stored": True, "secret_ref": "provider:openai", "generation": 1, "authorization_revision": 1}
    assert "sensitive-provider-value" not in response.body.decode()
    assert store.get_snapshot("provider:openai").value == "sensitive-provider-value"
    assert finalized == [("openai", "sensitive-provider-value")]


@pytest.mark.asyncio
async def test_capture_rejects_untrusted_expanded_and_replayed_requests() -> None:
    store = InMemorySecretStore()
    payload = {"credential_kind": "xiaohongshu_cookie", "credential_subject": "account-1", "value": "cookie-data", "command_id": "cmd-credential-0002"}
    assert (await capture_credential(_request(payload, authorized=False), SimpleNamespace(secret_store=store))).status_code == 403
    assert (await capture_credential(_request({**payload, "extra": True}), SimpleNamespace(secret_store=store))).status_code == 400
    request = _request(payload)
    assert (await capture_credential(request, SimpleNamespace(secret_store=store))).status_code == 201
    assert (await capture_credential(request, SimpleNamespace(secret_store=store))).status_code == 409


@pytest.mark.asyncio
async def test_capture_stores_tokenhub_asr_key_without_registering_an_llm_provider() -> None:
    store = InMemorySecretStore()
    response = await capture_credential(
        _request({
            "credential_kind": "tokenhub_asr_api_key",
            "credential_subject": "tokenhub-hy-asr",
            "value": "tokenhub-secret",
            "command_id": "cmd-tokenhub-asr-0001",
        }),
        SimpleNamespace(secret_store=store),
    )
    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["secret_ref"] == "asr:tokenhub-hy-asr"
    assert "tokenhub-secret" not in response.body.decode()
    assert store.get_snapshot("asr:tokenhub-hy-asr").value == "tokenhub-secret"


@pytest.mark.asyncio
async def test_capture_stores_qwen_realtime_asr_key_in_dedicated_ref() -> None:
    store = InMemorySecretStore()
    response = await capture_credential(
        _request({
            "credential_kind": "qwen_realtime_asr_api_key",
            "credential_subject": "qwen-realtime",
            "value": "qwen-secret",
            "command_id": "cmd-qwen-realtime-0001",
        }),
        SimpleNamespace(secret_store=store),
    )
    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["secret_ref"] == "asr:qwen-realtime"
    assert "qwen-secret" not in response.body.decode()
    assert store.get_snapshot("asr:qwen-realtime").value == "qwen-secret"


def test_legacy_provider_secret_endpoint_fails_closed_in_desktop_production() -> None:
    with pytest.raises(HTTPException) as error:
        save_provider_secret("openai", SaveProviderSecretRequest(api_key="not-stored"), SimpleNamespace())
    assert error.value.status_code == 403
    assert error.value.detail == "provider_secret_desktop_ipc_required"


@pytest.mark.asyncio
async def test_capture_rejects_unknown_provider_before_leaving_a_secret(tmp_path) -> None:
    store = InMemorySecretStore()
    response = await capture_credential(
        _request({
            "credential_kind": "provider_api_key",
            "credential_subject": "unknown-provider",
            "value": "must-not-persist",
            "command_id": "cmd-credential-unknown-provider",
        }),
        SimpleNamespace(
            root_dir=tmp_path,
            secret_store=store,
            settings_service=SimpleNamespace(get_provider_settings=lambda: SimpleNamespace(
                llm_provider="openai", openai_base_url="", openai_model="",
            )),
            invalidate_agent_graph_service=lambda: None,
        ),
    )
    assert response.status_code == 400
    assert store.has_secret("provider:unknown-provider") is False
