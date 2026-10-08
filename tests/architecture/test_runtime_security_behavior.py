"""T0.7 behavioral witnesses for externally visible security boundaries."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from threading import RLock

import pytest
from fastapi import HTTPException

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.workspace_intake import WorkspaceIntake
from backend.memory_app.workspace_items import WorkspaceItems
from backend.memory_app.v2.privacy import set_private_project
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_wechat_links import transport
from tests.memory_app.v2.test_chatgpt_subscription import env as subscription_env


@pytest.mark.parametrize("private", [False, True])
def test_user_download_is_separate_from_model_egress(tmp_path, monkeypatch, private):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
    models.update("generation", {"expected_revision": 0, "base_url": "https://example.com/v1",
        "model": "test-model", "api_key": "synthetic-test-value", "allow_remote": private})
    if private:
        set_private_project(records, "alpha", True, 0)
    intake = WorkspaceIntake(tmp_path, WorkspaceItems(records, None, RLock()), models)
    calls = transport(monkeypatch, b'<html><div id="js_content">article</div></html>')
    item = asyncio.run(intake.add_link({"project_id": "alpha", "url": "https://mp.weixin.qq.com/s/test"}))
    assert item["source_text"] == "article"
    request, = [call for call in calls if call[0] == "request"]
    assert request == ("request", "GET", "/s/test", {"User-Agent": "ChriptmasWorkspace/1", "Accept-Encoding": "identity"})
    assert [call for call in calls if call[0] == "dns"] == [("dns", "mp.weixin.qq.com", 443)]
    assert ("connect", ("93.184.216.34", 443)) in calls


@pytest.mark.parametrize("private", [False, True])
def test_downloaded_article_model_processing_still_requires_authorization(tmp_path, monkeypatch, private):
    from tests.memory_app.v2.test_intake_authorization import RemoteModel
    from tests.memory_app.test_workspace import client
    model = RemoteModel()
    model.allowed = private
    http, records = client(tmp_path, model)
    if private:
        set_private_project(records, "alpha", True, 0)
    transport(monkeypatch, b'<html><div id="js_content">public article</div></html>')
    response = http.post("/api/workspace/v1/items/link", json={"project_id": "alpha", "url": "https://mp.weixin.qq.com/s/test"})
    assert response.status_code == 200
    item = response.json()
    before = records.read("workspace_items", item["id"])
    response = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "alpha"})
    assert response.status_code == 409
    assert response.json()["detail"] == ("private_project_remote_blocked" if private else "remote_disabled")
    assert records.read("workspace_items", item["id"]) == before
    assert model.calls == model.wire_calls == 0


def test_model_terminal_requires_effect_authority_even_with_committed_reservation(tmp_path):
    from tests.rebuild.test_ai_kernel_sqlite_store import test_model_attempt_terminal_rejects_pre_migration_reservation_without_effect
    test_model_attempt_terminal_rejects_pre_migration_reservation_without_effect(tmp_path)


@pytest.mark.parametrize("takeover", [False, True])
def test_model_terminal_rejects_expired_or_replaced_worker(tmp_path, takeover):
    from tests.rebuild.test_ai_kernel_sqlite_store import (
        SQLiteAITurnStore, NOW, RunLeaseRevoked, _request, _turn_event,
        _model_attempt_dispatch, _model_attempt_event, _model_attempt_receipt,
    )
    database = tmp_path / "attempt.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request()
    turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    lease = store.try_acquire_run_lease(turn_id, "old-worker", now=NOW, stale_after=NOW + timedelta(seconds=30))
    assert lease is not None
    dispatch = _model_attempt_dispatch(request)
    store.commit_model_attempt_dispatch_bundle(
        _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch),
        expected_sequence=1, dispatch_payload=dispatch, run_lease=lease,
    )
    assert store.mark_run_lease_stale(lease, now=NOW + timedelta(seconds=31)) is not None
    if takeover:
        replacement = store.takeover_run_lease(turn_id, expected_generation=lease.generation, owner_id="new-worker", now=NOW + timedelta(seconds=31), stale_after=NOW + timedelta(seconds=60), disposition="safe")
        assert replacement is not None and replacement != lease
    with pytest.raises(RunLeaseRevoked):
        store.append_model_attempt_terminal_bundle(
            _model_attempt_event(request, 3, "model.attempt.terminal", "terminal", _model_attempt_receipt(dispatch)),
            expected_sequence=2, attempt_receipt_payload=_model_attempt_receipt(dispatch), run_lease=lease,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status,terminal_receipt_ref FROM ai_model_attempt_reservations").fetchone() == ("committed", None)
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_payloads WHERE kind='model-wire-attempt-receipt'").fetchone()[0] == 0


def test_media_receipt_does_not_self_settle_and_old_effect_worker_cannot_bind(tmp_path):
    from tests.rebuild.media_hands.test_effect_execution import test_receipt_before_settle_recovery_and_exact_replay
    from tests.rebuild.test_effect_log import test_stale_generation_cannot_bind_receipt_after_expired_takeover
    first = tmp_path / "media"
    first.mkdir()
    second = tmp_path / "lease"
    second.mkdir()
    test_receipt_before_settle_recovery_and_exact_replay(first)
    test_stale_generation_cannot_bind_receipt_after_expired_takeover(second)


def test_desktop_local_coordination_behaves_as_authenticated_main_owned_transport():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(["node", "--test", "apps/desktop-electron/test/context-graph-import-ipc-controller.test.cjs", "apps/desktop-electron/test/session-placement-transfer-ipc-controller.test.cjs"], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_connection_diagnostic_only_sends_fixed_content_and_honors_provider_switch(tmp_path, monkeypatch):
    from backend.shared.llm.connection_diagnostic import run_model_connection_diagnostic
    import backend.shared.llm.litellm_gateway as gateway_module
    from backend.security.provider_egress import ProviderEgressPolicyStore, ProviderEgressError, build_provider_egress_guard, DEFAULT_PROVIDER_EGRESS_CATEGORIES
    calls = []
    def wire(**request):
        calls.append(request)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(gateway_module, "_load_litellm_completion", lambda: wire)
    monkeypatch.setattr(gateway_module, "_load_litellm_acompletion", lambda: wire)
    endpoint = "https://example.com/v1"
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = policy.manifest(provider_id="probe", endpoint=endpoint, purposes=("connection_test",), payload_categories=DEFAULT_PROVIDER_EGRESS_CATEGORIES)
    guard = build_provider_egress_guard(tmp_path, provider_id="probe", endpoint=endpoint, purposes=("connection_test",), payload_categories=DEFAULT_PROVIDER_EGRESS_CATEGORIES, max_payload_bytes=256 * 1024)
    arguments = dict(provider="openai", model_name="test-model", base_url=endpoint, api_key_provider=lambda: "synthetic-test-value", anonymous=False, reasoning_effort=None, egress_guard=guard)
    with pytest.raises(ProviderEgressError):
        run_model_connection_diagnostic(**arguments)
    assert calls == []
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    assert run_model_connection_diagnostic(**arguments) == "ok"
    assert len(calls) == 1
    assert calls[0]["messages"] == [{"role": "user", "content": "Reply with exactly: ok"}]
    assert calls[0]["max_tokens"] == 256
    policy.revoke("probe")
    with pytest.raises(ProviderEgressError):
        run_model_connection_diagnostic(**arguments)
    assert len(calls) == 1


@pytest.mark.parametrize("denial", ["global", "private", "source_revision"])
def test_real_model_gateway_workbench_rechecks_authority_before_wire(tmp_path, denial):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.workspace import install_workspace_routes
    from backend.recognition import RecognitionService
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
    from core.document_engine import DocumentDraft, SQLiteDocumentRepository
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = documents.create(DocumentDraft(title="Evidence", document_type="legacy",
        markdown="alphaomega private evidence", project_id="alpha",
        source_refs=({"source_id": "synthetic", "locator": "text:0:1"},)))
    calls = []
    def wire(**request):
        calls.append(request)
        return {"choices": [{"message": {"content": '{"answer":"alphaomega","citations":[1]}'}, "finish_reason": "stop"}]}
    def gateway_factory(**kwargs):
        if denial == "source_revision":
            documents.save(document["id"], "alphaomega revised evidence", document["source_refs"], expected_revision=document["revision"])
        return LiteLLMCompletionGateway(**kwargs)
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), gateway_factory=gateway_factory, completion_fn=wire)
    models.update("generation", {"expected_revision": 0, "base_url": "https://example.com/v1", "model": "test-model", "api_key": "synthetic-test-value", "allow_remote": denial != "global"})
    if denial == "private":
        set_private_project(records, "alpha", True, 0)
    app = FastAPI()
    install_workspace_routes(app, runtime_root=tmp_path, records=records, models=models, documents=documents, service=RecognitionService(records))
    with TestClient(app) as client:
        response = client.post("/api/workspace/v1/ask", json={"project_id": "alpha", "question": "alphaomega"})
    # All denials stop before wire. The current domain source-revision check
    # reports a retryable 409 while preserving the strict no-wire assertion.
    assert response.status_code == 409, response.text
    if denial == "source_revision":
        assert documents.read(document["id"])["revision"] == 2
        assert response.json()["detail"] == "source_changed_retry"
    assert calls == []


@pytest.mark.parametrize("case,code", [
    ("private_address", "url_address_blocked"),
    ("loopback_v6", "url_address_blocked"),
    ("lan", "url_address_blocked"),
    ("mixed_dns", "url_address_blocked"),
    ("port", "invalid_url"),
    ("redirect", "link_redirect_disallowed"),
    ("size", "link_too_large"),
    ("gzip", "link_too_large"),
])
def test_article_fetcher_enforces_transport_boundaries(monkeypatch, case, code):
    import gzip
    from backend.memory_app import workspace_links
    monkeypatch.setattr(workspace_links, "_MAX_FILE", 128)
    data = b'<html><div id="js_content">article</div></html>'
    if case == "size":
        data = b"a" * 129
    if case == "gzip":
        data = gzip.compress(b"a" * 129)
    addresses = {"private_address": ["127.0.0.1"], "loopback_v6": ["::1"], "lan": ["192.168.1.2"], "mixed_dns": ["93.184.216.34", "10.0.0.1"]}.get(case)
    calls = transport(monkeypatch, data, status=302 if case == "redirect" else 200,
                      encoding="gzip" if case == "gzip" else "identity", addresses=addresses)
    url = "https://mp.weixin.qq.com:444/s/test" if case == "port" else "https://mp.weixin.qq.com/s/test"
    with pytest.raises(HTTPException) as error:
        workspace_links._fetch_url(url)
    assert error.value.detail == code
    if case in {"private_address", "loopback_v6", "lan", "mixed_dns", "port"}:
        assert not any(call[0] == "request" for call in calls)


@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "https://b23.tv:444/private", "https://attacker.invalid/data", "https://www.xiaohongshu.com/explore/test", "https://b23.tv/again"])
def test_short_link_redirects_pin_dns_and_reject_unsafe_targets(monkeypatch, location):
    import socket
    from backend.memory_app import workspace_media_url as media
    calls = []
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    monkeypatch.setattr(media.socket, "create_connection", lambda address, **kwargs: calls.append(address))
    class Connection:
        def __init__(self, host, port, timeout):
            self.status = 302
        def request(self, method, path, headers):
            assert method == "GET" and path in {"/test", "/again"}
            assert headers == {"User-Agent": "ChriptmasWorkspace/1", "Accept-Encoding": "identity"}
            self._create_connection(("untrusted.second.lookup", 443), timeout=12)
        def getresponse(self):
            return self
        def getheader(self, name):
            assert name == "location"
            return location
        def close(self):
            pass
    monkeypatch.setattr(media.http.client, "HTTPSConnection", Connection)
    with pytest.raises(ValueError, match="media_short_link_redirect_limit" if location.endswith("/again") else "media_short_link_target_invalid"):
        media.resolve_media_url("https://b23.tv/test")
    assert calls == [("93.184.216.34", 443)] * (3 if location.endswith("/again") else 1)


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "10.0.0.1"])
def test_short_link_rejects_private_dns_without_connecting(monkeypatch, address):
    import socket
    from backend.memory_app import workspace_media_url as media
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    calls = []
    monkeypatch.setattr(media.socket, "create_connection", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="media_short_link_address_blocked"):
        media.resolve_media_url("https://b23.tv/test")
    assert calls == []


@pytest.mark.parametrize("denial", ["global", "private", "revoked_during_wire"])
def test_retrieval_stream_uses_real_configuration_and_source_authority(tmp_path, monkeypatch, denial):
    import json
    import httpx
    from backend.memory_app.retrieval_models import configured_retrieve
    from backend.recognition import RecognitionConflict
    from tests.memory_app.test_retrieval_source_policy import setup_sources
    service, authority, scope, _sources = setup_sources(tmp_path)
    models = ModelConfiguration(service.records, tmp_path, InMemorySecretStore())
    for purpose in ("embedding", "rerank"):
        models.update(purpose, {"expected_revision": 0, "base_url": "https://example.com/v1", "model": purpose,
            "api_key": "synthetic-test-value", "allow_remote": denial != "global", "enabled": True})
    if denial == "private":
        set_private_project(service.records, scope.project_id, True, 0)
    calls = []
    def wire(request):
        calls.append(request.url.path)
        payload = json.loads(request.content)
        assert request.url.path.endswith("/embeddings")
        set_private_project(service.records, scope.project_id, True, 0)
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(payload["input"]))]})
    client_type = httpx.Client
    monkeypatch.setattr("backend.memory_app.retrieval_models.httpx.Client", lambda **kwargs: client_type(transport=httpx.MockTransport(wire), **kwargs))
    def retrieve():
        return configured_retrieve(models, tmp_path, scope.project_id, "alpha plan", service.retrieval_entries(scope=scope), source_egress=authority, source_scope=scope)
    if denial == "revoked_during_wire":
        with pytest.raises(RecognitionConflict):
            retrieve()
        assert calls == ["/v1/embeddings"]
    else:
        result = retrieve()
        assert len(result.hits) == 3
        assert calls == []


@pytest.mark.parametrize("denial", ["global", "private", "source_revision"])
def test_governed_gateway_rechecks_real_source_and_configuration(tmp_path, denial):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import RecognitionConflict
    from backend.memory_app.model_config import ModelConfigurationError
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
    from tests.memory_app.test_governed_generation import Control, Metadata, WireSink, _route
    from tests.memory_app.test_retrieval_source_policy import setup_sources
    service, authority, scope, sources = setup_sources(tmp_path)
    entries = service.retrieval_entries(scope=scope)
    snapshot = authority.snapshot(scope, [{"type": "recognition", "id": entry["id"], "revision": entry["revision"]} for entry in entries])
    def validate():
        authority.validate_snapshot(scope, snapshot)
        authority.require(snapshot, "generation")
    calls = []
    def wire(**request):
        calls.append(request)
        raise AssertionError("revoked source reached transport")
    def factory(**kwargs):
        if denial == "private":
            set_private_project(service.records, scope.project_id, True, 0)
        elif denial == "source_revision":
            service.revoke_experience(scope=scope, experience_id=sources[0][0], expected_revision=1)
        return LiteLLMCompletionGateway(**kwargs)
    models = ModelConfiguration(service.records, tmp_path, InMemorySecretStore(), gateway_factory=factory, completion_fn=wire)
    models.update("generation", {"expected_revision": 0, "base_url": "https://example.com/v1", "model": "test-model", "api_key": "synthetic-test-value", "allow_remote": True})
    route = _route(models)
    if denial == "global":
        models.update("generation", {"expected_revision": 1, "allow_remote": False})
    with pytest.raises((RecognitionConflict, ModelConfigurationError)):
        models.complete_governed([{"role": "user", "content": "alpha plan"}], routing_snapshot=route,
            execution_control=Control(), metadata_sink=Metadata(), wire_attempt_sink=WireSink(), validate_current=validate)
    assert calls == []



@pytest.mark.parametrize("denial", ["global", "private", "source_revision"])
def test_subscription_responses_rechecks_real_global_and_source_authority(subscription_env, tmp_path, denial):
    import httpx
    from backend.memory_app.model_config import ModelConfigurationError
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
    from backend.shared.llm.openai_responses import ResponsesCompletion
    from tests.memory_app.v2.test_chatgpt_subscription import login
    subscription, _state, secrets, records = subscription_env
    login(subscription_env)
    recognition = RecognitionService(records)
    scope = WorkScope("user", "project")
    eid = recognition.stage_experience(scope=scope, content="synthetic evidence")
    authority = SourceEgressService(records)
    snapshot = authority.snapshot(scope, [{"type": "experience", "id": eid, "revision": 1}])
    calls = []
    def wire(request):
        calls.append(request)
        raise AssertionError("denied source reached Responses transport")
    def validate():
        authority.validate_snapshot(scope, snapshot)
        authority.require(snapshot, "generation")
    def factory(**kwargs):
        if denial == "private":
            set_private_project(records, scope.project_id, True, 0)
        elif denial == "source_revision":
            recognition.revoke_experience(scope=scope, experience_id=eid, expected_revision=1)
        return LiteLLMCompletionGateway(**kwargs)
    models = ModelConfiguration(records, tmp_path, secrets=secrets, subscriptions=subscription, gateway_factory=factory)
    models.select_subscription(model="gpt-fixture", expected_revision=0, allow_remote=denial != "global", expected_generation_revision=0)
    with httpx.Client(transport=httpx.MockTransport(wire)) as client:
        models._responses = ResponsesCompletion(client=client)
        with pytest.raises((RecognitionConflict, ModelConfigurationError)):
            models.complete([{"role": "user", "content": "synthetic evidence"}], validate_current=validate)
    assert calls == []


@pytest.mark.parametrize("duration", [1.0, 3600.0])
def test_expiry_during_model_handler_cannot_persist_terminal(tmp_path, monkeypatch, duration):
    from tests.rebuild.test_ai_kernel_sqlite_store import (
        SQLiteAITurnStore, NOW, _request, _turn_event,
        _model_attempt_dispatch, _model_attempt_event, _model_attempt_receipt,
    )
    from datetime import datetime, timezone
    import core.effect_log.core as effect_core
    database = tmp_path / "expiry.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request()
    turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    current = datetime.now(timezone.utc).replace(microsecond=900000)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current
    monkeypatch.setattr("core.ai_kernel.sqlite_store.datetime", Clock)
    lease = store.try_acquire_run_lease(turn_id, "old-worker", now=current, stale_after=current + timedelta(seconds=1))
    dispatch = _model_attempt_dispatch(request)
    store.commit_model_attempt_dispatch_bundle(
        _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch),
        expected_sequence=1, dispatch_payload=dispatch, run_lease=lease,
    )
    clock = [0.0]
    monkeypatch.setattr(effect_core.time, "monotonic", lambda: clock[0])
    def handler():
        clock[0] = duration
        terminal = store.append_model_attempt_terminal_bundle(
            _model_attempt_event(request, 3, "model.attempt.terminal", "terminal", _model_attempt_receipt(dispatch)),
            expected_sequence=2, attempt_receipt_payload=_model_attempt_receipt(dispatch), run_lease=lease,
        )
        return "provider-value", terminal.attempt_receipt_ref
    from core.ai_kernel.event_store import RunLeaseRevoked
    with pytest.raises(RunLeaseRevoked):
        store.execute_model_attempt_handler(dispatch["attempt_id"], handler, run_lease=lease)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status,terminal_receipt_ref FROM ai_model_attempt_reservations").fetchone() == ("committed", None)
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_payloads WHERE kind='model-wire-attempt-receipt'").fetchone()[0] == 0


@pytest.mark.parametrize("scenario", ["takeover", "heartbeat"])
def test_model_terminal_tracks_takeover_and_live_heartbeat(tmp_path, monkeypatch, scenario):
    from tests.rebuild.test_ai_kernel_sqlite_store import (
        SQLiteAITurnStore, _request, _turn_event, _model_attempt_dispatch,
        _model_attempt_event, _model_attempt_receipt,
    )
    from core.ai_kernel.event_store import RunLeaseRevoked
    from datetime import datetime, timezone
    import core.effect_log.core as effect_core
    current = datetime.now(timezone.utc)
    store = SQLiteAITurnStore(tmp_path / "live.sqlite3")
    request = _request()
    turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    lease = store.try_acquire_run_lease(turn_id, "old", now=current, stale_after=current + timedelta(seconds=30))
    dispatch = _model_attempt_dispatch(request)
    store.commit_model_attempt_dispatch_bundle(_model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch), expected_sequence=1, dispatch_payload=dispatch, run_lease=lease)
    clock = [0.0]
    monkeypatch.setattr(effect_core.time, "monotonic", lambda: clock[0])
    replacement = []
    def finish(token, item, sequence):
        receipt = _model_attempt_receipt(item)
        terminal = store.append_model_attempt_terminal_bundle(_model_attempt_event(request, sequence + 1, "model.attempt.terminal", "terminal", receipt), expected_sequence=sequence, attempt_receipt_payload=receipt, run_lease=token)
        return "value", terminal.attempt_receipt_ref
    def old_handler():
        clock[0] = 20.0
        if scenario == "heartbeat":
            assert store.renew_run_lease(lease, now=current + timedelta(seconds=20), stale_after=current + timedelta(seconds=150)) is not None
            clock[0] = 100.0
        else:
            clock[0] = 31.0
            assert store.mark_run_lease_stale(lease, now=current + timedelta(seconds=31)) is not None
            replacement.append(store.takeover_run_lease(turn_id, expected_generation=lease.generation, owner_id="new", now=current + timedelta(seconds=31), stale_after=current + timedelta(seconds=150), disposition="safe"))
        return finish(lease, dispatch, 2)
    if scenario == "heartbeat":
        assert store.execute_model_attempt_handler(dispatch["attempt_id"], old_handler, run_lease=lease) == "value"
    else:
        with pytest.raises(RunLeaseRevoked):
            store.execute_model_attempt_handler(dispatch["attempt_id"], old_handler, run_lease=lease)
        assert len(store.events_after(turn_id)) == 2
        newer = dict(dispatch, attempt_id="model-wire-attempt-1123456789abcdef0123456789abcdef", attempt_number=2)
        store.commit_model_attempt_dispatch_bundle(_model_attempt_event(request, 3, "model.attempt.dispatched", "dispatched", newer), expected_sequence=2, dispatch_payload=newer, run_lease=replacement[0].token)
        assert store.execute_model_attempt_handler(newer["attempt_id"], lambda: finish(replacement[0].token, newer, 3), run_lease=replacement[0].token) == "value"
    with sqlite3.connect(store._path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_payloads WHERE kind='model-wire-attempt-receipt'").fetchone()[0] == 1
