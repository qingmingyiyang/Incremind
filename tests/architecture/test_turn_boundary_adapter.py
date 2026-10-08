from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "src" / "backend" / "security" / "turn_boundary_adapter.py"


def test_turn_boundary_adapter_uses_server_authorities_without_model_or_network() -> None:
    text = ADAPTER.read_text(encoding="utf-8")
    for required in (
        "validate_turn_request",
        "ProjectBoundaryProfileStore",
        "BoundaryPolicyEngine",
        "tool_from_capability",
        "DesktopFileGrant",
    ):
        assert required in text
    for forbidden in (
        "LiteLLM",
        "ModelGateway",
        "httpx",
        "urllib",
        "secret_store",
        "ProviderRegistry",
    ):
        assert forbidden not in text


def test_v1_constraints_narrow_after_boundary_allow() -> None:
    text = ADAPTER.read_text(encoding="utf-8")
    boundary_evaluate = text.index("self._engine.evaluate")
    remote_narrow = text.index("turn_remote_not_allowed")
    approval_narrow = text.index("legacy_turn_approval_required")
    assert boundary_evaluate < remote_narrow < approval_narrow


def test_boundary_request_projection_does_not_copy_file_metadata_or_turn_text() -> None:
    text = ADAPTER.read_text(encoding="utf-8")
    request_block = text[text.index("return BoundaryRequest(") : text.index("def _narrow")]
    for forbidden in (
        "display_name",
        "sha256",
        "expires_at_ms",
        "session_instance_id",
        'turn["input"]',
        "consent_refs",
    ):
        assert forbidden not in request_block
