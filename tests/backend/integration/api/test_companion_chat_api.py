from __future__ import annotations

import hashlib
import inspect
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes import companion
from core.companion_core import CompanionRepository
from core.memory_core import ObjectStoreMemoryStore
from core.search_and_recall import ObjectStoreRecallIndex, build_recall_entries_from_object_store
from core.storage_provider import JsonObjectStore


def client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def test_chat_local_fallback_roundtrip_restart_and_session_history(tmp_path) -> None:
    first_client = client(tmp_path)
    payload = {"request_id": "request:api-one", "text": "你好 <script>alert(1)</script>"}
    created = first_client.post("/api/rebuild/companion/chat", json=payload)
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    assert body["source"] == "local"
    assert body["reason"] in {"provider_unconfigured", "route_disabled"}
    assert body["user_message"]["content"] == payload["text"]
    assert body["assistant_message"]["content"]
    assert "script" not in str(body["trace"]).lower()
    session_id = body["session"]["session_id"]
    turn_id = "turn-" + hashlib.sha256(b"default\0request:api-one").hexdigest()[:32]
    turn = first_client.get(f"/api/ai/turns/{turn_id}/events")
    assert turn.status_code == 200
    events = turn.json()["events"]
    assert any(event["type"] == "approval.required" for event in events)
    write = next(event for event in events if event["type"] == "tool.completed" and event["data"]["capability_id"] == "companion.chat.message.write")
    assert write["data"]["receipt_ref"].startswith("crp://")
    assert turn.json()["presentation"]["request_id"] == payload["request_id"]
    assert body["user_message"]["request_id"] == payload["request_id"]
    assert body["assistant_message"]["request_id"] == payload["request_id"]
    assert "build_companion_chat_service" not in inspect.getsource(companion.send_companion_chat)

    restarted = client(tmp_path)
    replay = restarted.post("/api/rebuild/companion/chat", json={**payload, "session_id": session_id})
    history = restarted.get("/api/rebuild/companion/chat/messages", params={"session_id": session_id})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["assistant_message"]["message_id"] == body["assistant_message"]["message_id"]
    assert len(restarted.get(f"/api/ai/turns/{turn_id}/events").json()["events"]) == len(events)
    assert history.status_code == 200
    assert [item["role"] for item in history.json()["items"]] == ["user", "assistant"]
    assert CompanionRepository.at_data_root(tmp_path).wallet_integrity().snapshot_balance == 0


def test_chat_completion_schedules_proposal_only_learning_after_terminal(
    tmp_path, monkeypatch,
) -> None:
    observed = []

    def record_completion(container, *, user_message, assistant_message):
        observed.append((container.root_dir, user_message, assistant_message))

    monkeypatch.setattr(companion, "process_completed_companion_episode", record_completion)
    response = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={"request_id": "request:completion-learning", "text": "请记住这次架构决定"},
    )

    assert response.status_code == 201
    assert len(observed) == 1
    root, user, assistant = observed[0]
    assert root == tmp_path
    assert user.status == assistant.status == "completed"
    assert user.request_id == assistant.request_id == "request:completion-learning"


def test_legacy_chat_turn_privacy_matches_runtime_composition_snapshot() -> None:
    common = {
        "project_id": "default", "request_id": "request:privacy", "text": "你好",
        "session_id": None, "memory_id": None,
    }
    local = companion._legacy_chat_turn_request(**common, remote_usable=False)
    remote = companion._legacy_chat_turn_request(**common, remote_usable=True)

    assert local["privacy"]["mode"] == "local_only"
    assert local["privacy"]["allow_remote"] is False
    assert local["privacy"]["consent_refs"] == []
    assert remote["privacy"]["mode"] == "remote_allowed"
    assert remote["privacy"]["allow_remote"] is True
    assert remote["privacy"]["consent_refs"] == ["crp://default/consent/provider-egress-policy"]


def test_project_chat_followup_inherits_session_scope_after_restart(tmp_path) -> None:
    first = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={"request_id": "request:project-first", "text": "先回顾项目", "project_id": "project-alpha"},
    )
    assert first.status_code == 201
    session_id = first.json()["session"]["session_id"]
    assert first.json()["session"]["project_id"] == "project-alpha"

    followup = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={"request_id": "request:project-followup", "session_id": session_id, "text": "下一步呢"},
    )
    assert followup.status_code == 201
    assert followup.json()["session"]["project_id"] == "project-alpha"
    assert followup.json()["user_message"]["project_id"] == "project-alpha"
    assert followup.json()["assistant_message"]["project_id"] == "project-alpha"

    conflicting = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={
            "request_id": "request:project-conflict", "session_id": session_id,
            "text": "切换项目", "project_id": "project-beta",
        },
    )
    assert conflicting.status_code == 409
    history = client(tmp_path).get(
        "/api/rebuild/companion/chat/messages", params={"session_id": session_id},
    ).json()
    assert history["session"]["project_id"] == "project-alpha"
    assert {item["project_id"] for item in history["items"]} == {"project-alpha"}


def test_chat_api_rejects_extra_fields_oversized_text_and_replay_drift(tmp_path) -> None:
    api = client(tmp_path)
    assert api.post("/api/rebuild/companion/chat", json={"request_id": "request:bad", "text": "ok", "prompt": "override"}).status_code == 400
    assert api.post("/api/rebuild/companion/chat", json={"request_id": "request:bad", "text": "x" * 4001}).status_code == 400
    assert api.post("/api/rebuild/companion/chat", json={"request_id": 1, "text": "ok"}).status_code == 400
    assert api.post("/api/rebuild/companion/chat", json={
        "request_id": "request:bad-memory", "text": "ok", "memory_id": "../private",
    }).status_code == 400
    wrong_intent = api.post("/api/rebuild/companion/chat", json={
        "request_id": "request:wrong-anchor", "text": "普通问题", "memory_id": "atom-build",
    })
    assert wrong_intent.status_code == 400
    assert wrong_intent.json()["error"]["code"] == "invalid_chat"
    created = api.post("/api/rebuild/companion/chat", json={"request_id": "request:fixed", "text": "原文"})
    session_id = created.json()["session"]["session_id"]
    conflict = api.post("/api/rebuild/companion/chat", json={"request_id": "request:fixed", "session_id": session_id, "text": "改写"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "chat_conflict"


def test_chat_history_requires_known_bounded_session(tmp_path) -> None:
    api = client(tmp_path)
    missing = api.get("/api/rebuild/companion/chat/messages", params={"session_id": "session:missing"})
    bad_limit = api.get("/api/rebuild/companion/chat/messages", params={"session_id": "session:missing", "limit": 101})
    assert missing.status_code == 404
    assert bad_limit.status_code in {400, 422}


def test_chat_history_restores_persisted_memory_review_without_trace_body(tmp_path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    repository.create_session(
        session_id="session:review", context_epoch=1, prompt_revision=1,
        profile_revision=1, started_at="2026-07-20T08:00:00+00:00",
    )
    repository.append_message(
        message_id="message:assistant:review", request_id="request:review", session_id="session:review",
        context_epoch=1, role="assistant", status="completed", content="已完成今日回顾。",
        created_at="2026-07-20T08:01:00+00:00", provider_mode="remote",
        memory_review={
            "review_scope": "today_memory_review", "status": "recalled", "matched_count": 1,
            "memory_ids": ["atom-review-one"], "generated": True,
        },
    )

    response = client(tmp_path).get(
        "/api/rebuild/companion/chat/messages", params={"session_id": "session:review"},
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["memory_review"] == {
        "review_scope": "today_memory_review", "status": "recalled", "matched_count": 1,
        "memory_ids": ["atom-review-one"], "generated": True,
    }
    assert "source_id" not in str(response.json())


def test_chat_recalls_only_published_indexed_memory_with_explainable_trace(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library", namespace_id="default")
    ObjectStoreMemoryStore(store).publish("atom", {
        "schema_version": "1.0.0", "id": "atom-companion-tea",
        "source_id": "message:user:remembered", "content": "御主喜欢乌龙茶。",
        "atom_type": "preference", "tags": ["companion"], "confidence": 0.9,
        "source_refs": [{"source_id": "message:user:remembered", "locator": "companion:message"}],
        "revision": 1, "created_at": "2026-07-23T04:00:00+00:00",
        "updated_at": "2026-07-23T04:00:00+00:00", "trust_status": "user_confirmed",
    })
    index = ObjectStoreRecallIndex(store)
    index.rebuild(build_recall_entries_from_object_store(store), source="companion-test")

    response = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={"request_id": "request:memory-recall", "text": "乌龙茶"},
    )

    assert response.status_code == 201
    trace = response.json()["trace"]["memory_recall"]
    assert trace["status"] == "recalled"
    assert trace["backend"] == "object_store_lexical"
    assert trace["selected"] == [{
        "memory_id": "atom-companion-tea", "source_id": "message:user:remembered",
        "score": 1.0, "rank": 1,
    }]
    assert "御主喜欢乌龙茶" not in str(trace)

    topic_prompt = (
        "请根据我已经确认发布的长期记忆，围绕《任意相似标题》说明这条记忆的意义、与当前工作的联系，"
        "并给出两个可继续追问的问题。请区分有证据的事实与推断。"
    )
    anchored = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={
            "request_id": "request:memory-anchor", "text": topic_prompt,
            "memory_id": "atom-companion-tea",
        },
    )

    assert anchored.status_code == 201
    anchored_trace = anchored.json()["trace"]["memory_recall"]
    assert anchored_trace["review_scope"] == "memory_topic_discussion"
    assert anchored_trace["selected"][0]["memory_id"] == "atom-companion-tea"
    assert "御主喜欢乌龙茶" not in str(anchored_trace)


def test_chat_anchor_reads_the_selected_non_default_project(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_COMPANION_E2E_CLOCK_MODE", "packaged-fixed")
    monkeypatch.setenv("CHRIPTMAS_COMPANION_E2E_CLOCK_UTC", "2026-08-13T04:00:00Z")
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library", namespace_id="default")
    ObjectStoreMemoryStore(store).publish("atom", {
        "schema_version": "1.0.0", "id": "atom-project-build",
        "project_id": "project-alpha", "source_id": "source:project:build",
        "content": "project-alpha 使用分层构建缓存。", "atom_type": "fact",
        "tags": ["build"], "confidence": 0.9,
        "source_refs": [{"source_id": "source:project:build", "locator": "source:section:cache"}],
        "revision": 1, "created_at": "2026-08-13T04:00:00+00:00",
        "updated_at": "2026-08-13T04:00:00+00:00", "trust_status": "user_confirmed",
    })
    index = ObjectStoreRecallIndex(store)
    index.rebuild(build_recall_entries_from_object_store(store), source="companion-project-test")
    scoped_pulse = client(tmp_path).get("/api/rebuild/pet/mood?project_id=project-alpha")
    other_pulse = client(tmp_path).get("/api/rebuild/pet/mood?project_id=project-beta")
    assert scoped_pulse.status_code == 200
    assert scoped_pulse.json()["published_memory_count"] == 1
    assert other_pulse.status_code == 200
    assert other_pulse.json()["published_memory_count"] == 0
    topic_prompt = (
        "请根据我已经确认发布的长期记忆，围绕《项目构建缓存》说明这条记忆的意义、与当前工作的联系，"
        "并给出两个可继续追问的问题。请区分有证据的事实与推断。"
    )

    project_review = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={
            "request_id": "request:project-daily-review",
            "text": "请根据我已经确认发布的长期记忆，回顾今天值得注意的变化。请区分有证据的事实与暂无依据的推断。",
            "project_id": "project-alpha",
        },
    )
    assert project_review.status_code == 201
    review_trace = project_review.json()["trace"]["memory_recall"]
    assert review_trace["review_scope"] == "today_memory_review"
    assert [item["memory_id"] for item in review_trace["selected"]] == ["atom-project-build"]

    response = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={
            "request_id": "request:project-memory-anchor", "text": topic_prompt,
            "memory_id": "atom-project-build", "project_id": "project-alpha",
        },
    )

    assert response.status_code == 201
    trace = response.json()["trace"]["memory_recall"]
    assert trace["review_scope"] == "memory_topic_discussion"
    assert trace["selected"][0]["memory_id"] == "atom-project-build"
    assert "project-alpha 使用分层构建缓存" not in str(trace)

    wrong_project = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={
            "request_id": "request:wrong-project-anchor", "text": topic_prompt,
            "memory_id": "atom-project-build", "project_id": "project-beta",
        },
    )
    assert wrong_project.status_code == 201
    wrong_trace = wrong_project.json()["trace"]["memory_recall"]
    assert wrong_trace["selected"] == []
    assert wrong_trace["dropped_reasons"] == ["outside_project"]

    ordinary_project = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={
            "request_id": "request:ordinary-project", "text": "普通问题",
            "project_id": "project-alpha",
        },
    )
    assert ordinary_project.status_code == 201
