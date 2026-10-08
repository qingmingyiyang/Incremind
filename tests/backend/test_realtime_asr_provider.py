from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_worker_auth_middleware
from backend.api.desktop_session import (
    DESKTOP_ALLOWED_ORIGIN_ENV,
    DESKTOP_EXPIRES_ENV,
    DESKTOP_INSTANCE_ENV,
    DESKTOP_MODE_ENV,
    DESKTOP_NONCE_ENV,
    DESKTOP_PROTOCOL_ENV,
    DESKTOP_PROTOCOL_VERSION,
    DESKTOP_SECRET_ENV,
    DESKTOP_SESSION_HEADER,
)
from backend.api.qwen_realtime_asr import build_run_task, parse_qwen_event, qwen_realtime_egress_manifest
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.routes.realtime_asr import _propose_confirmed_project_name, router
from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.security.secrets import InMemorySecretStore
from core.product_core.realtime_asr_lexicon import RealtimeAsrLexicon
from core.product_core.realtime_asr_provider_settings import (
    QWEN_REALTIME_ASR_SECRET_REF,
    GetRealtimeAsrProviderSettings,
    SaveRealtimeAsrProviderSettings,
)


def _app(tmp_path: Path, secrets: InMemorySecretStore) -> FastAPI:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path, secret_store=secrets)
    app.include_router(router)
    return app


def test_realtime_asr_is_remote_off_by_default_and_requires_key_and_consent(tmp_path: Path) -> None:
    secrets = InMemorySecretStore()
    client = TestClient(_app(tmp_path, secrets))

    initial = client.get("/api/rebuild/settings/realtime-asr-provider").json()
    assert initial["status"] == "disabled"
    assert initial["model"] == "qwen-audio-3.0-asr-flash-streaming"
    assert initial["remote_processing"] is True
    rejected = client.put(
        "/api/rebuild/settings/realtime-asr-provider",
        json={"enabled": True, "confirm_enable": False, "endpoint": initial["endpoint"]},
    )
    assert rejected.status_code == 409
    enabled = client.put(
        "/api/rebuild/settings/realtime-asr-provider",
        json={"enabled": True, "confirm_enable": True, "endpoint": initial["endpoint"]},
    ).json()
    assert enabled["status"] == "needs_api_key"
    secrets.set(QWEN_REALTIME_ASR_SECRET_REF, "qwen-private-key")
    with_key = client.get("/api/rebuild/settings/realtime-asr-provider").json()
    assert with_key["status"] == "needs_consent"
    consented = client.post(
        "/api/rebuild/settings/realtime-asr-provider/egress-consent",
        json={"manifest_id": with_key["egress_manifest"]["manifest_id"], "confirm": True},
    )
    assert consented.json()["status"] == "ready"
    assert "qwen-private-key" not in consented.text


def test_realtime_asr_settings_support_only_documented_workspace_regions(tmp_path: Path) -> None:
    store, _storage = build_rebuild_object_store(tmp_path)
    saved = SaveRealtimeAsrProviderSettings(store, now="2026-09-05T00:00:00Z").execute(
        enabled=False, confirm_enable=False, region="ap-southeast-1", workspace_id="workspace-demo",
    )
    assert saved.region == "ap-southeast-1"
    assert saved.workspace_id == "workspace-demo"
    assert saved.endpoint == "wss://workspace-demo.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/inference"
    with pytest.raises(ValueError, match="workspace configuration"):
        SaveRealtimeAsrProviderSettings(store, now="2026-09-05T00:00:00Z").execute(
            enabled=False, confirm_enable=False, region="eu-west-1", workspace_id="workspace-demo",
        )


def test_desktop_websocket_uses_an_authenticated_single_use_ticket(
    tmp_path: Path, monkeypatch
) -> None:
    allowed_origin = "http://127.0.0.1:14605"
    session_secret = "s" * 43
    for key, value in {
        DESKTOP_MODE_ENV: "desktop_production",
        DESKTOP_SECRET_ENV: session_secret,
        DESKTOP_INSTANCE_ENV: "desktop-realtime-test",
        DESKTOP_NONCE_ENV: "n" * 43,
        DESKTOP_PROTOCOL_ENV: DESKTOP_PROTOCOL_VERSION,
        DESKTOP_EXPIRES_ENV: "2099-01-01T00:00:00+00:00",
        DESKTOP_ALLOWED_ORIGIN_ENV: allowed_origin,
    }.items():
        monkeypatch.setenv(key, value)

    with TestClient(_app(tmp_path, InMemorySecretStore())) as client:
        issued = client.post(
            "/api/rebuild/workbench/realtime-asr/ticket",
            headers={DESKTOP_SESSION_HEADER: session_secret},
        )
        assert issued.status_code == 201
        ticket = issued.json()["ticket"]
        assert session_secret not in issued.text

        with client.websocket_connect(
            f"/api/rebuild/workbench/realtime-asr/ws?ticket={ticket}",
            headers={"origin": "file://"},
        ) as socket:
            assert socket.receive_json()["code"] == "realtime_asr_disabled"

        with client.websocket_connect(
            f"/api/rebuild/workbench/realtime-asr/ws?ticket={ticket}",
            headers={"origin": "https://untrusted.example"},
        ) as socket:
            assert socket.receive_json()["code"] == "desktop_session_unauthorized"


def test_worker_secret_websocket_requires_single_use_ticket(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(DESKTOP_MODE_ENV, raising=False)
    monkeypatch.setenv("CHRIPTMAS_WORKER_SECRET", "worker-test-secret")
    instance_id = "a" * 32
    monkeypatch.setenv("CHRIPTMAS_WORKER_INSTANCE_ID", instance_id)
    monkeypatch.setenv("CHRIPTMAS_WORKER_PORT", "8001")
    app = _app(tmp_path, InMemorySecretStore())
    create_worker_auth_middleware(app)
    seconds = str(int(time.time()))
    nonce = "b" * 32
    payload = f"chriptmas-worker-request/v1\n{instance_id}\nPOST\n/api/rebuild/workbench/realtime-asr/ticket\n{seconds}\n{nonce}"
    proof = hmac.new(b"worker-test-secret", payload.encode("ascii"), hashlib.sha256).hexdigest()
    authorization = f"v1:{seconds}:{nonce}:{proof}"

    with TestClient(app) as client:
        assert client.post("/api/rebuild/workbench/realtime-asr/ticket", headers={"Origin": "https://example.test"}).status_code == 403
        issued = client.post("/api/rebuild/workbench/realtime-asr/ticket", headers={"X-Worker-Secret": authorization})
        assert issued.status_code == 201
        ticket = issued.json()["ticket"]

        with client.websocket_connect("/api/rebuild/workbench/realtime-asr/ws", headers={"Origin": "https://example.test"}) as socket:
            assert socket.receive_json()["code"] == "desktop_session_unauthorized"
        with client.websocket_connect(f"/api/rebuild/workbench/realtime-asr/ws?ticket={ticket}") as socket:
            assert socket.receive_json()["code"] == "realtime_asr_disabled"
        with client.websocket_connect(f"/api/rebuild/workbench/realtime-asr/ws?ticket={ticket}") as socket:
            assert socket.receive_json()["code"] == "desktop_session_unauthorized"


def test_lexicon_api_keeps_automatic_corrections_pending_until_review(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path, InMemorySecretStore()))
    proposed = client.post(
        "/api/rebuild/settings/realtime-asr-lexicon/candidates",
        json={
            "term": "Chriptmas OS",
            "suggested_weight": 4,
            "source_kind": "explicit_correction",
            "source_ref": "workbench-edit-1",
            "confirm_source": True,
        },
    )
    assert proposed.status_code == 201
    assert proposed.json()["activation"] == "pending_review"
    before = client.get("/api/rebuild/settings/realtime-asr-lexicon").json()
    assert before["terms"] == []
    candidate_id = proposed.json()["candidate"]["candidate_id"]
    accepted = client.post(
        f"/api/rebuild/settings/realtime-asr-lexicon/candidates/{candidate_id}/review",
        json={"action": "accept", "weight": 4, "is_super": False},
    )
    assert accepted.status_code == 200
    after = client.get("/api/rebuild/settings/realtime-asr-lexicon").json()
    assert after["terms"][0]["term"] == "Chriptmas OS"
    assert after["pending_candidates"] == []


def test_confirmed_project_name_is_automatic_pending_candidate_only(tmp_path: Path) -> None:
    store, _storage = build_rebuild_object_store(tmp_path)
    store.write("project_skill_index", "project-alpha", {"id": "project-alpha", "skill_id": "skill-alpha"}, expected_revision=None)
    store.write("project_skills", "skill-alpha", {
        "id": "skill-alpha",
        "project_id": "project-alpha",
        "name": "Chriptmas OS",
        "revision": 3,
        "status": "active",
        "trust_status": "user_confirmed",
    }, expected_revision=None)
    lexicon = RealtimeAsrLexicon(store)

    _propose_confirmed_project_name(tmp_path, "project-alpha", lexicon)
    _propose_confirmed_project_name(tmp_path, "project-alpha", lexicon)

    assert not lexicon.select_for_session().terms
    pending = lexicon.list_candidates(status="pending_review")
    assert len(pending) == 1
    assert pending[0].term == "Chriptmas OS"


def test_qwen_contract_uses_pcm_immediate_vocabulary_and_redacts_provider_failure(tmp_path: Path) -> None:
    store, _storage = build_rebuild_object_store(tmp_path)
    settings = GetRealtimeAsrProviderSettings(store).execute()
    payload = build_run_task(task_id="00000000-0000-4000-8000-000000000000", settings=settings, vocabulary={"专有名词": 4})
    assert payload["payload"]["parameters"] == {"format": "pcm", "sample_rate": 16000, "vocabulary": {"专有名词": 4}}
    event = parse_qwen_event({"header": {"event": "task-failed", "error_code": "CLIENT_ERROR", "error_message": "sensitive upstream detail"}, "payload": {}})
    assert event.kind == "failed"
    assert event.error_code == "CLIENT_ERROR"
    assert "sensitive" not in repr(event)


class _FakeUpstream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] | None = None
        self.sent: list[object] = []

    async def send(self, value: object) -> None:
        self.sent.append(value)
        if self.queue is None:
            self.queue = asyncio.Queue()
        if isinstance(value, str):
            action = json.loads(value).get("header", {}).get("action")
            if action == "run-task":
                await self.queue.put(json.dumps({"header": {"event": "task-started"}, "payload": {}}))
            elif action == "finish-task":
                await self.queue.put(json.dumps({"header": {"event": "task-finished"}, "payload": {}}))
        elif isinstance(value, bytes):
            await self.queue.put(json.dumps({"header": {"event": "result-generated"}, "payload": {"output": {"sentence": {"text": "实时结果", "sentence_end": True, "sentence_id": 1}}, "usage": {"duration": 1}}}, ensure_ascii=False))

    async def recv(self) -> str:
        if self.queue is None:
            self.queue = asyncio.Queue()
        return await self.queue.get()


class _FakeConnector:
    def __init__(self, upstream: _FakeUpstream) -> None:
        self.upstream = upstream

    async def __aenter__(self) -> _FakeUpstream:
        return self.upstream

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_realtime_websocket_freezes_reviewed_hotwords_and_streams_final_text(tmp_path: Path) -> None:
    secrets = InMemorySecretStore({QWEN_REALTIME_ASR_SECRET_REF: "secret-not-rendered"})
    app = _app(tmp_path, secrets)
    store, _storage = build_rebuild_object_store(tmp_path)
    settings = SaveRealtimeAsrProviderSettings(store, now="2026-09-04T00:00:00Z").execute(enabled=True, confirm_enable=True)
    lexicon = RealtimeAsrLexicon(store, now=lambda: "2026-09-04T00:00:00Z")
    term = lexicon.manual_upsert("Chriptmas OS", weight=4)
    manifest = qwen_realtime_egress_manifest(tmp_path, endpoint=settings.endpoint)
    ProviderEgressPolicyStore(tmp_path).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    upstream = _FakeUpstream()
    app.state.qwen_realtime_connector = lambda _endpoint, _headers: _FakeConnector(upstream)

    with TestClient(app) as client:
        with client.websocket_connect("/api/rebuild/workbench/realtime-asr/ws") as socket:
            ready = socket.receive_json()
            assert ready["type"] == "ready"
            assert ready["lexicon_revision"] == 1
            socket.send_bytes(b"\x00\x01" * 160)
            final = socket.receive_json()
            assert final == {"type": "final", "text": "实时结果", "sentence_id": 1, "duration_seconds": 1}
            socket.send_json({"action": "finish"})
            assert socket.receive_json()["type"] == "finished"

    run_task = json.loads(upstream.sent[0])
    assert run_task["payload"]["parameters"]["vocabulary"] == {term.term: 4}
    snapshots = store.list("realtime_asr_session_snapshots")
    assert snapshots[0]["lexicon_revision"] == 1
    assert snapshots[0]["selected_term_ids"] == [term.term_id]
    assert "secret-not-rendered" not in json.dumps(snapshots)
    receipt = store.read("realtime_asr_session_receipts", f"receipt-{snapshots[0]['id']}")
    assert receipt["status"] == "finished"
    assert receipt["receipt_kind"] == "realtime-asr-session-terminal-v1"


def test_realtime_route_never_materializes_a_secret_snapshot(tmp_path: Path) -> None:
    source = (Path(__file__).parents[2] / "src/backend/api/routes/realtime_asr.py").read_text(encoding="utf-8")
    assert "secret_store.get_snapshot" not in source
    assert "QwenRealtimeSecureConnector" in source
