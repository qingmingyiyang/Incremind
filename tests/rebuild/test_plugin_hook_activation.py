from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.plugin_host.hook_activation import (
    PluginHookActivationAuthority,
    PluginHookActivationConflict,
)
from core.storage_provider import SQLiteStructuredRecordStore


def _files() -> list[dict[str, object]]:
    hand = {
        "schema_version": "1.0.0", "id": "policy-hand", "runtime": "powershell-stdio-v1",
        "entrypoint": "payload/main.py",
        "input_schema": {
            "type": "object", "properties": {"hook_event": {"type": "string"}, "payload": {"type": "object"}},
            "required": ["hook_event", "payload"], "additionalProperties": False,
        },
        "output_schema": {
            "type": "object", "properties": {"exit_code": {"type": "integer"}, "stdout": {"type": "string"}, "stderr": {"type": "string"}},
            "required": ["exit_code", "stdout", "stderr"], "additionalProperties": False,
        },
        "effect": "read", "operation_semantics": "read_only", "requested_resources": [],
    }
    hook = {
        "schema_version": "1.0.0", "id": "pre-tool-policy", "hand_id": "policy-hand",
        "event": "PreToolUse", "order": 10, "sync": True, "timeout_ms": 500,
        "metadata_projection": "codex-hook-v1", "recursion": "deny",
    }
    values = {
        "hands/policy-hand/hand.json": json.dumps(hand).encode(),
        "hands/policy-hand/payload/main.py": b"not executed",
        "hooks/pre-tool-policy/hook.json": json.dumps(hook).encode(),
    }
    return [
        {"relative_path": path, "size_bytes": len(content), "content_base64": base64.b64encode(content).decode("ascii")}
        for path, content in sorted(values.items())
    ]


class _Hands:
    def __init__(self) -> None:
        self.active = SimpleNamespace(
            activation_revision=1, package_record_id="policy-plugin~1.0.0",
        )

    def resolve_active(self, plugin_id: str, *, hand_id: str):
        assert plugin_id == "policy-plugin" and hand_id == "policy-hand"
        return self.active


def _authority(root: Path) -> tuple[PluginHookActivationAuthority, SQLiteStructuredRecordStore, _Hands]:
    records = SQLiteStructuredRecordStore(root / "records.sqlite3")
    with records.begin() as uow:
        uow.put("plugin_package_states", "policy-plugin", {
            "status": "installed_disabled", "enabled": False,
            "package_record_id": "policy-plugin~1.0.0",
        }, expected_revision=0)
        uow.put("plugin_raw_packages", "policy-plugin~1.0.0", {
            "plugin_id": "policy-plugin", "version": "1.0.0", "files": _files(),
        }, expected_revision=0)
        uow.put("plugin_hands_activations", "policy-plugin--policy-hand", {
            "plugin_id": "policy-plugin", "hand_id": "policy-hand",
            "package_record_id": "policy-plugin~1.0.0", "status": "active",
        }, expected_revision=0)
        uow.commit()
    hands = _Hands()
    return PluginHookActivationAuthority(records, hands=hands, now="2026-08-27T00:00:00Z"), records, hands


def _activate(authority: PluginHookActivationAuthority):
    reviewed = authority.review(
        "policy-plugin", hook_id="pre-tool-policy", expected_state_revision=1,
        expected_hand_activation_revision=1, command_id="hook-review-0001",
        confirm=True, reason="review exact Hook binding",
    )
    activated = authority.activate(
        "policy-plugin", hook_id="pre-tool-policy",
        expected_review_revision=reviewed["review_revision"], expected_activation_revision=0,
        command_id="hook-activate-0001", confirm=True,
    )
    return reviewed, activated


def test_review_activate_restart_projection_and_disable_are_explicit(tmp_path: Path) -> None:
    authority, records, hands = _authority(tmp_path)
    reviewed, activated = _activate(authority)

    assert reviewed["review"]["decision"] == "approved_disabled"
    assert activated["activation"]["status"] == "active"
    restored = PluginHookActivationAuthority(records, hands=hands, now="2026-08-27T00:01:00Z")
    binding = restored.resolve_active("policy-plugin", hook_id="pre-tool-policy")
    assert binding is not None
    assert binding.event == "PreToolUse" and binding.hand_id == "policy-hand"
    assert binding.handler_revision == "hook-r1-hand-r1-active-r1"
    assert restored.all_active() == (binding,)

    disabled = restored.disable(
        "policy-plugin", hook_id="pre-tool-policy", expected_activation_revision=1,
        command_id="hook-disable-0001", reason="operator disabled",
    )
    assert disabled["activation"]["status"] == "disabled"
    assert restored.resolve_active("policy-plugin", hook_id="pre-tool-policy") is None


def test_commands_replay_exactly_and_reject_identity_reuse(tmp_path: Path) -> None:
    authority, _records, _hands = _authority(tmp_path)
    reviewed, _activated = _activate(authority)
    replay = authority.review(
        "policy-plugin", hook_id="pre-tool-policy", expected_state_revision=1,
        expected_hand_activation_revision=1, command_id="hook-review-0001",
        confirm=True, reason="review exact Hook binding",
    )
    assert replay["replayed"] is True and replay["review_revision"] == reviewed["review_revision"]
    with pytest.raises(PluginHookActivationConflict, match="command identity"):
        authority.review(
            "policy-plugin", hook_id="pre-tool-policy", expected_state_revision=1,
            expected_hand_activation_revision=1, command_id="hook-review-0001",
            confirm=True, reason="different reason",
        )


def test_hand_activation_or_raw_byte_drift_invalidates_binding(tmp_path: Path) -> None:
    authority, records, hands = _authority(tmp_path)
    _activate(authority)
    hands.active = SimpleNamespace(activation_revision=2, package_record_id="policy-plugin~1.0.0")
    with pytest.raises(PluginHookActivationConflict, match="Hand activation drifted"):
        authority.resolve_active("policy-plugin", hook_id="pre-tool-policy")

    hands.active = SimpleNamespace(activation_revision=1, package_record_id="policy-plugin~1.0.0")
    with records.begin() as uow:
        raw = uow.read("plugin_raw_packages", "policy-plugin~1.0.0")
        assert raw is not None
        files = [dict(item) for item in raw.payload["files"]]
        hook = next(item for item in files if item["relative_path"] == "hooks/pre-tool-policy/hook.json")
        content = base64.b64decode(hook["content_base64"])
        changed = content.replace(b'"timeout_ms": 500', b'"timeout_ms": 501')
        hook["content_base64"] = base64.b64encode(changed).decode("ascii")
        hook["size_bytes"] = len(changed)
        uow.put(raw.collection, raw.object_id, dict(raw.payload) | {"files": files}, expected_revision=raw.revision)
        uow.commit()
    with pytest.raises(PluginHookActivationConflict, match="raw bytes drifted"):
        authority.resolve_active("policy-plugin", hook_id="pre-tool-policy")
