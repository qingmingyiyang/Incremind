from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.api import ai_runtime, companion_vision_ai_runtime
from backend.api.ai_runtime import build_ai_runtime, get_or_build_ai_runtime
from backend.api.companion_vision_ai_runtime import (
    COMPANION_VISION_ANALYZE_CAPABILITY,
    COMPANION_VISION_CONTEXT_CAPABILITY,
    COMPANION_VISION_OUTCOME,
)
from core.model_gateway import ModelResult


ROOT = Path(__file__).resolve().parents[4]


class GrantStore:
    def __init__(self, *, session_id: str, suffix: str) -> None:
        self.session_id = session_id
        self.grant_id = f"vision-grant-{suffix * 48}"
        self._public = {
            "grant_id": self.grant_id,
            "media_type": "image/jpeg",
            "byte_length": 11,
            "sha256": suffix * 64,
        }
        self.consume_calls = 0

    def inspect(self, grant_id):
        assert grant_id == self.grant_id
        return dict(self._public)

    def consume_expected(self, grant_id, expected_public):
        assert grant_id == self.grant_id
        assert expected_public == self._public
        self.consume_calls += 1
        return dict(self._public), b"\xff\xd8\xffpixels"


class Gateway:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, request):
        self.calls.append(request)
        return ModelResult("已看见画面", "fixture", "vision-1", {})


def test_shared_runtime_resolves_the_current_desktop_grant_store_without_issuing_grants(tmp_path, monkeypatch) -> None:
    current = {"instance_id": "desktop-a"}
    monkeypatch.setattr(companion_vision_ai_runtime, "desktop_session", lambda: SimpleNamespace(**current))
    monkeypatch.setattr(companion_vision_ai_runtime, "_current_prompt_profile", lambda _root: ("视觉提示", 3, 7))
    first = GrantStore(session_id="desktop-a", suffix="a")
    application = SimpleNamespace(state=SimpleNamespace(companion_vision_grant_store=first))
    container = SimpleNamespace(root_dir=tmp_path)
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=application), container)

    first_completed = _approve(runtime, runtime.submit_turn(_request("a", first.grant_id)))
    assert first_completed.status == "completed" and first.consume_calls == 1

    current["instance_id"] = "desktop-b"
    second = GrantStore(session_id="desktop-b", suffix="b")
    application.state.companion_vision_grant_store = second
    assert get_or_build_ai_runtime(SimpleNamespace(app=application), container) is runtime
    second_completed = _approve(runtime, runtime.submit_turn(_request("b", second.grant_id)))
    assert second_completed.status == "completed" and second.consume_calls == 1


def test_vision_composition_fails_closed_without_current_grant_or_matching_desktop_instance(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(companion_vision_ai_runtime, "desktop_session", lambda: SimpleNamespace(instance_id="desktop-a"))
    monkeypatch.setattr(companion_vision_ai_runtime, "_current_prompt_profile", lambda _root: ("视觉提示", 1, 1))
    application = SimpleNamespace(state=SimpleNamespace())
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=application), SimpleNamespace(root_dir=tmp_path))

    missing = runtime.submit_turn(_request("c", "vision-grant-" + "c" * 48))
    assert missing.status == "failed"
    assert list(runtime.events_after(missing.turn_id))[-1]["data"]["error_code"] == "ai.tool_failed"

    mismatched = GrantStore(session_id="other-desktop", suffix="d")
    application.state.companion_vision_grant_store = mismatched
    wrong_instance = runtime.submit_turn(_request("d", mismatched.grant_id))
    assert wrong_instance.status == "failed" and mismatched.consume_calls == 0


def test_vision_composition_with_unconsented_or_wrong_adapter_gateway_uses_local_fallback_and_never_persists_pixels(tmp_path, monkeypatch) -> None:
    current = {"instance_id": "desktop-a"}
    monkeypatch.setattr(companion_vision_ai_runtime, "desktop_session", lambda: SimpleNamespace(**current))
    monkeypatch.setattr(companion_vision_ai_runtime, "_current_prompt_profile", lambda _root: ("视觉提示", 4, 9))
    gateway = Gateway()

    route = {"consented": False, "adapter_kind": "openai-compatible-vision"}

    def resolve(_container, route_key, **_kwargs):
        return SimpleNamespace(
            gateway=gateway if route_key == "companion.vision" else None,
            egress_consented=route["consented"],
            adapter_kind=route["adapter_kind"],
        )

    monkeypatch.setattr(ai_runtime, "resolve_model_gateway_runtime", resolve)
    store = GrantStore(session_id="desktop-a", suffix="e")
    application = SimpleNamespace(state=SimpleNamespace(companion_vision_grant_store=store))
    runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path), application=application)
    assert runtime.composition_metadata["companion_vision_remote_usable"] is False

    completed = _approve(runtime, runtime.submit_turn(_request("e", store.grant_id)))
    assert completed.status == "completed" and gateway.calls == [] and store.consume_calls == 1
    assert runtime.presentation_for(completed.turn_id)["provider_id"] == "local-fallback"
    for event in runtime.events_after(completed.turn_id):
        _assert_no_pixels(event)
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        for key in ("payload_ref", "receipt_ref"):
            if isinstance(data.get(key), str):
                _assert_no_pixels(runtime._payloads.get(data[key]))

    route.update(consented=True, adapter_kind="openai-compatible")
    wrong_adapter_store = GrantStore(session_id="desktop-a", suffix="f")
    application.state.companion_vision_grant_store = wrong_adapter_store
    wrong_adapter_runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path / "wrong-adapter"), application=application)
    assert wrong_adapter_runtime.composition_metadata["companion_vision_remote_usable"] is False
    wrong_adapter = _approve(wrong_adapter_runtime, wrong_adapter_runtime.submit_turn(_request("f", wrong_adapter_store.grant_id)))
    assert wrong_adapter.status == "completed" and gateway.calls == [] and wrong_adapter_store.consume_calls == 1


def _request(marker: str, grant_id: str) -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request.update({
        "turn_id": f"turn-{marker * 32}",
        "operation_id": f"op-vision-composition-{marker}",
        "idempotency_key": f"vision-composition-{marker}",
        "desired_outcome": COMPANION_VISION_OUTCOME,
        "input": {"kind": "text", "text": "请描述屏幕", "refs": [{"kind": "companion_vision_grant", "object_id": grant_id, "uri": f"crp://default/companion/vision/grants/{grant_id}"}]},
        "privacy": {"mode": "remote_allowed", "allow_remote": True, "pii": "possible", "consent_refs": ["crp://default/consent/provider-egress-policy"], "retention": "local_durable"},
        "capability_policy": {"allowed": [COMPANION_VISION_CONTEXT_CAPABILITY, COMPANION_VISION_ANALYZE_CAPABILITY], "denied": [], "require_approval": [COMPANION_VISION_ANALYZE_CAPABILITY]},
        "context_policy": {"include_project_skill": False, "include_memory": False, "include_session_history": False, "max_context_bytes": 4096},
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
    })
    return request


def _approve(runtime, waiting):
    approval = next(event for event in reversed(tuple(runtime.events_after(waiting.turn_id))) if event["type"] == "approval.required")
    return runtime.apply_action({"schema_version": "1.0.0", "action_id": f"action-{waiting.turn_id[5:]}", "turn_id": waiting.turn_id, "type": "approve", "target_event_id": approval["event_id"], "reason": "analyze screen", "actor": "user", "expected_sequence": approval["sequence"], "idempotency_key": f"approve-{waiting.turn_id[5:]}", "created_at": "2026-08-23T00:00:01+00:00"})


def _assert_no_pixels(value):
    if isinstance(value, dict):
        assert not ({"sha256", "path", "pixels", "bytes", "base64", "image_payload"} & set(value))
        for child in value.values():
            _assert_no_pixels(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_pixels(child)
