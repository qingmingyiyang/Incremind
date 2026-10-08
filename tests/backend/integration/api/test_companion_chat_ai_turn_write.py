from __future__ import annotations

import json
from pathlib import Path

from backend.api.companion_chat_ai_runtime import (
    COMPANION_CHAT_CONTEXT_CAPABILITY,
    COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY,
    COMPANION_CHAT_OUTCOME,
    CompanionChatContextCapability,
    CompanionChatMessageWriteCapability,
    CompanionChatTurnPlanner,
)
from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
)
from core.companion_core import CompanionRepository, default_character_prompt
from tests.backend.integration.api.turn_model_routing_fixture import RoutingSnapshotFixture
from core.model_gateway import ModelResult


ROOT = Path(__file__).resolve().parents[4]


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def invoke(self, request):
        self.calls += 1
        self.requests.append(request)
        assert request.privacy_scope == "remote_allowed"
        assert request.capability == "text"
        assert request.parameters["_model_routing_snapshot"]["requirement"]["required_capability"] == "text"
        assert request.parameters["messages"][-1]["role"] == "user"
        assert request.execution_control is not None
        snapshot = request.parameters["_model_routing_snapshot"]
        sink = request.metadata_sink
        sink.model_call_routed(
            snapshot_ref=request.parameters["_model_routing_snapshot_ref"],
            snapshot_revision=request.parameters["_model_routing_snapshot_revision"],
            prompt_cache_scope_identity=snapshot["prompt_cache_scope"]["identity"],
            provider="provider-test", model="model-test", execution_location="remote",
        )
        sink.model_call_started(provider="provider-test", model="model-test")
        sink.model_call_completed(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        return ModelResult({"text": "我在，先把这件事拆成两步。"}, "provider-test", "model-test", {})


class _PostflightCancelledGateway(_Gateway):
    def invoke(self, request):
        self.calls += 1
        self.requests.append(request)
        snapshot = request.parameters["_model_routing_snapshot"]
        sink = request.metadata_sink
        sink.model_call_routed(
            snapshot_ref=request.parameters["_model_routing_snapshot_ref"],
            snapshot_revision=request.parameters["_model_routing_snapshot_revision"],
            prompt_cache_scope_identity=snapshot["prompt_cache_scope"]["identity"],
            provider="provider-test", model="model-test", execution_location="remote",
        )
        sink.model_call_started(provider="provider-test", model="model-test")
        request.execution_control.cancellation.request()
        request.execution_control.checkpoint()
        raise AssertionError("cancel checkpoint did not stop model result")


def test_companion_chat_turn_approval_writes_one_exchange_and_replays_without_model_or_duplicate_messages(tmp_path: Path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    gateway = _Gateway()
    runtime = _runtime(repository, gateway)

    waiting = runtime.submit_turn(_request())

    assert waiting.status == "waiting_approval"
    assert gateway.calls == 0
    assert repository.list_messages().items == ()
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    action = _approval(waiting, approval)
    completed = runtime.apply_action(action)
    replay = runtime.apply_action(action)

    assert completed.status == "completed" and replay.replayed is True
    assert gateway.calls == 1
    assert gateway.requests[0].execution_control is not None
    messages = repository.list_messages().items
    assert [(item.role, item.content, item.provider_mode) for item in messages] == [
        ("user", "今天有点乱", "none"),
        ("assistant", "我在，先把这件事拆成两步。", "remote"),
    ]
    presentation = runtime.presentation_for(completed.turn_id)
    assert presentation is not None
    assert presentation["text"] == "我在，先把这件事拆成两步。"
    tool_event = next(
        event for event in runtime.events_after(completed.turn_id)
        if event["type"] == "tool.completed" and event["data"]["capability_id"] == COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY
    )
    receipt_ref = tool_event["data"]["receipt_ref"]
    assert receipt_ref.startswith("crp://session/")
    receipt = runtime._payloads.get(receipt_ref)
    assert receipt["kind"] == COMPANION_CHAT_OUTCOME
    assert receipt["content"]["assistant_message_id"] == messages[-1].message_id


def test_companion_chat_turn_fails_closed_when_history_changes_while_waiting_for_approval(tmp_path: Path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    repository.initialize()
    _create_session(repository, "session:existing")
    runtime = _runtime(repository, _Gateway())

    waiting = runtime.submit_turn(_request(turn_id="turn-ffffffffffffffffffffffffffffffff", session_id="session:existing"))
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    repository.append_message(
        message_id="message:history-drift",
        request_id="history-drift",
        session_id="session:existing",
        context_epoch=1,
        role="user",
        status="completed",
        content="审批前的新消息",
        created_at="2026-08-23T08:00:00+00:00",
        provider_mode="none",
        project_id="project-alpha",
    )

    failed = runtime.apply_action(_approval(waiting, approval, suffix="stale"))

    assert failed.status == "failed"
    assert tuple(runtime.events_after(failed.turn_id))[-1]["data"]["error_code"] == "ai.stale_baseline"
    assert len(repository.list_messages(session_id="session:existing").items) == 1


def test_companion_chat_turn_local_only_uses_fallback_after_approval_without_gateway(tmp_path: Path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    runtime = _runtime(repository, None)
    request = _request()
    request["privacy"] = {
        "mode": "local_only", "allow_remote": False, "pii": "possible",
        "consent_refs": [], "retention": "local_durable",
    }

    waiting = runtime.submit_turn(request)
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    completed = runtime.apply_action(_approval(waiting, approval))

    assert completed.status == "completed"
    presentation = runtime.presentation_for(completed.turn_id)
    assert presentation is not None
    assert presentation["provider_call_performed"] is False
    assert presentation["provider_id"] == "local-fallback"
    assert repository.list_messages().items[-1].provider_mode == "local"


def test_companion_chat_model_postflight_cancel_stops_before_domain_write(tmp_path: Path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    gateway = _PostflightCancelledGateway()
    runtime = _runtime(repository, gateway)

    waiting = runtime.submit_turn(_request())
    failed = runtime.apply_action(_approval(waiting, tuple(runtime.events_after(waiting.turn_id))[-1]))

    assert failed.status == "failed"
    assert repository.list_messages().items == ()
    assert tuple(runtime.events_after(failed.turn_id))[-1]["data"]["error_code"] == "ai.tool_cancel_unconfirmed"


def test_companion_chat_turn_anchors_a_published_memory_in_model_context_without_leaking_recall_metadata(tmp_path: Path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    gateway = _Gateway()
    recall_calls = []

    def recall(query, *, review_scope=None, target_memory_id=None):
        recall_calls.append((query, review_scope, target_memory_id))
        return type("Recall", (), {
            "status": "recalled",
            "selected": ({"memory_id": "atom-secret", "source_id": "source-secret", "score": 1.0},),
            "context": ({
                "memory_id": "atom-secret",
                "source_id": "source-secret",
                "text": "这条已发布记忆的正文。",
                "confidence": 0.99,
            },),
        })()

    runtime = _runtime(repository, gateway, memory_recall_loader=recall)
    request = _request(memory_id="atom-secret", text=_topic_prompt("本周项目节奏"))

    waiting = runtime.submit_turn(request)

    assert waiting.status == "waiting_approval"
    assert repository.list_messages().items == ()
    assert gateway.calls == 0
    assert recall_calls == [("本周项目节奏", "memory_topic_discussion", "atom-secret")]
    context_event = next(
        event for event in runtime.events_after(waiting.turn_id)
        if event["type"] == "tool.completed" and event["data"]["capability_id"] == COMPANION_CHAT_CONTEXT_CAPABILITY
    )
    assert context_event["data"]["receipt_ref"] is None
    completed = runtime.apply_action(_approval(waiting, tuple(runtime.events_after(waiting.turn_id))[-1]))

    assert completed.status == "completed"
    serialized_prompt = json.dumps(gateway.requests[0].parameters["messages"], ensure_ascii=False)
    assert "这条已发布记忆的正文。" in serialized_prompt
    assert "source-secret" not in serialized_prompt
    assert "atom-secret" not in serialized_prompt
    presentation = runtime.presentation_for(completed.turn_id)
    assert presentation is not None
    assert "source-secret" not in json.dumps(presentation, ensure_ascii=False)
    receipt = next(
        event for event in runtime.events_after(completed.turn_id)
        if event["type"] == "tool.completed" and event["data"]["capability_id"] == COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY
    )["data"]
    assert receipt["receipt_ref"] is not None
    assert "source-secret" not in json.dumps(receipt, ensure_ascii=False)
    assert "atom-secret" not in json.dumps(receipt, ensure_ascii=False)


def test_companion_chat_turn_rejects_memory_anchor_without_controlled_topic_before_any_write(tmp_path: Path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    runtime = _runtime(repository, _Gateway())

    failed = runtime.submit_turn(_request(memory_id="atom-secret", text="请解释这条记忆"))

    assert failed.status == "failed"
    assert tuple(runtime.events_after(failed.turn_id))[-1]["data"]["error_code"] == "ai.execution_failed"
    assert repository.list_messages().items == ()


def test_companion_chat_write_capability_contract_is_approval_gated_and_receipted() -> None:
    context = CapabilityDefinition(COMPANION_CHAT_CONTEXT_CAPABILITY, 1, "read", False, "read_only", "crp://default/contracts/in", "crp://default/contracts/out")
    write = CapabilityDefinition(COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY, 1, "write", True, "receipt_required", "crp://default/contracts/in", "crp://default/contracts/out")

    assert COMPANION_CHAT_OUTCOME == "companion.chat.respond"
    assert context.mode == "read" and context.requires_approval is False
    assert write.mode == "write" and write.requires_approval is True
    assert write.operation_semantics == "receipt_required"


def _runtime(repository: CompanionRepository, gateway: _Gateway | None, *, memory_recall_loader=None) -> SynchronousAIRuntime:
    payloads = InMemoryTurnPayloadStore()
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition(COMPANION_CHAT_CONTEXT_CAPABILITY, 1, "read", False, "read_only", "crp://default/contracts/in", "crp://default/contracts/out"),
        CompanionChatContextCapability(
            repository=repository,
            character_prompt_loader=default_character_prompt,
            memory_recall_loader=memory_recall_loader,
            project_id="project-alpha",
        ),
    )
    registry.register(
        CapabilityDefinition(COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY, 1, "write", True, "receipt_required", "crp://default/contracts/in", "crp://default/contracts/out"),
        CompanionChatMessageWriteCapability(
            repository=repository,
            character_prompt_loader=default_character_prompt,
            gateway=gateway,
            receipt_store=payloads,
            project_id="project-alpha",
        ),
    )
    routing = RoutingSnapshotFixture(payloads, required_capability="text", egress_purpose="companion_chat")
    return SynchronousAIRuntime(
        planner=CompanionChatTurnPlanner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=payloads,
        state=InMemoryTurnStateStore(),
        manifest_resolver=routing,
        context_manifest_resolver=routing.context_resolver,
    )


def _request(
    *,
    turn_id: str = "turn-0123456789abcdef0123456789abcdef",
    session_id: str | None = None,
    memory_id: str | None = None,
    text: str = "今天有点乱",
) -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request["turn_id"] = turn_id
    request["operation_id"] = "op-companion-chat-0001" if turn_id.startswith("turn-0") else "op-companion-chat-stale"
    request["idempotency_key"] = "companion-chat-turn-0001" if turn_id.startswith("turn-0") else "companion-chat-turn-stale"
    request["desired_outcome"] = COMPANION_CHAT_OUTCOME
    request["scope"] = {"kind": "project", "project_id": "project-alpha", "series_id": None}
    request["input"] = {"kind": "text", "text": text, "refs": []}
    if session_id is not None:
        request["input"]["refs"] = [{"kind": "companion_session", "object_id": session_id, "uri": f"crp://default/companion/sessions/{session_id}"}]
    if memory_id is not None:
        request["input"]["refs"].append({"kind": "atom", "object_id": memory_id, "uri": f"crp://default/memory/{memory_id}"})
    request["capability_policy"] = {
        "allowed": [COMPANION_CHAT_CONTEXT_CAPABILITY, COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY],
        "denied": [],
        "require_approval": [COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY],
    }
    request["privacy"] = {
        "mode": "remote_allowed",
        "allow_remote": True,
        "pii": "possible",
        "consent_refs": ["crp://default/consent/provider-egress-policy"],
        "retention": "local_durable",
    }
    return request


def _approval(waiting, approval, *, suffix: str = "happy") -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "action_id": "action-0123456789abcdef0123456789abcdef" if suffix == "happy" else "action-ffffffffffffffffffffffffffffffff",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "Companion Chat reply approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": f"approve-companion-chat-{suffix}",
        "created_at": "2026-08-23T08:00:00Z",
    }


def _create_session(repository: CompanionRepository, session_id: str) -> None:
    prompt, revision = default_character_prompt()
    assert prompt
    repository.create_session(
        session_id=session_id,
        context_epoch=1,
        prompt_revision=revision,
        profile_revision=1,
        started_at="2026-08-23T07:00:00+00:00",
        project_id="project-alpha",
    )


def _topic_prompt(topic: str) -> str:
    return (
        f"请根据我已经确认发布的长期记忆，围绕《{topic}》说明这条记忆的意义、与当前工作的联系，"
        "并给出两个可继续追问的问题。请区分有证据的事实与推断。"
    )
