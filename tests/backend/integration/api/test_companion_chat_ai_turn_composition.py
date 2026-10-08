from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.api import ai_runtime
from backend.api.ai_runtime import build_ai_runtime
from backend.api.companion_chat_ai_runtime import (
    COMPANION_CHAT_CONTEXT_CAPABILITY,
    COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY,
    COMPANION_CHAT_OUTCOME,
)
from core.companion_core import CompanionRepository


ROOT = Path(__file__).resolve().parents[4]


def test_shared_runtime_composes_companion_authorities_for_each_turn_project_scope(tmp_path: Path) -> None:
    runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path))

    alpha = _request("a", "project-alpha")
    beta = _request("b", "project-beta")
    alpha_waiting = runtime.submit_turn(alpha)
    beta_waiting = runtime.submit_turn(beta)

    alpha_completed = runtime.apply_action(_approval(runtime, alpha_waiting, "a"))
    beta_completed = runtime.apply_action(_approval(runtime, beta_waiting, "b"))

    assert alpha_completed.status == beta_completed.status == "completed"
    repository = CompanionRepository.at_data_root(tmp_path)
    messages = repository.list_messages().items
    assert {item.project_id for item in messages} == {"project-alpha", "project-beta"}
    sessions = tuple(repository.get_session(item.session_id) for item in messages)
    assert {item.project_id for item in sessions if item is not None} == {"project-alpha", "project-beta"}
    assert all(item.provider_mode == "local" for item in messages if item.role == "assistant")


def test_companion_gateway_is_not_injected_without_existing_egress_consent(tmp_path: Path, monkeypatch) -> None:
    class Gateway:
        calls = 0

        def invoke(self, _request):
            self.calls += 1
            raise AssertionError("unconsented Companion gateway must not be invoked")

    gateway = Gateway()

    def resolve(_container, route_key, **_kwargs):
        return SimpleNamespace(
            gateway=gateway if route_key == "companion.chat" else None,
            egress_consented=False,
        )

    monkeypatch.setattr(ai_runtime, "resolve_model_gateway_runtime", resolve)
    runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path))
    waiting = runtime.submit_turn(_request("c", "project-alpha"))

    completed = runtime.apply_action(_approval(runtime, waiting, "c"))

    assert completed.status == "completed"
    assert gateway.calls == 0
    presentation = runtime.presentation_for(completed.turn_id)
    assert presentation is not None and presentation["provider_id"] == "local-fallback"


def _request(marker: str, project_id: str) -> dict[str, object]:
    request = json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8")
    )
    request["turn_id"] = f"turn-{marker * 32}"
    request["operation_id"] = f"op-companion-chat-{marker}"
    request["idempotency_key"] = f"companion-chat-{marker}"
    request["desired_outcome"] = COMPANION_CHAT_OUTCOME
    request["scope"] = {"kind": "project", "project_id": project_id, "series_id": None}
    request["input"] = {"kind": "text", "text": "今天有点乱", "refs": []}
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


def _approval(runtime, waiting, marker: str) -> dict[str, object]:
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    return {
        "schema_version": "1.0.0",
        "action_id": f"action-{marker * 32}",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "Companion Chat reply approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": f"approve-companion-chat-{marker}",
        "created_at": "2026-08-23T08:00:00Z",
    }
