from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backend.security.file_grant import DesktopFileGrant
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.turn_boundary_adapter import TurnBoundaryAdapterError, TurnBoundaryRequestFactory
from core.ai_boundary import EphemeralTokenVault, SensitiveTextScanner
from core.ai_kernel.ports import CapabilityDefinition


ROOT = Path(__file__).resolve().parents[4]


def test_project_read_uses_guarded_default_and_is_allowed(tmp_path) -> None:
    evaluation = _factory(tmp_path).evaluate(
        _turn(),
        _capability("memory.recall", mode="read", approval=False, semantics="read_only"),
        destination_id="local-runtime",
    )
    assert evaluation.profile.persisted is False
    assert evaluation.profile.profile.mode == "guarded"
    assert evaluation.request.destination_kind == "local"
    assert evaluation.decision.outcome == "allow"


def test_global_turn_ignores_any_project_named_global_profile(tmp_path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update("global", mode="open", remote_default="allow", expected_revision=0)
    turn = _turn()
    turn["scope"] = {"kind": "global", "project_id": None, "series_id": None}
    evaluation = TurnBoundaryRequestFactory(profiles).evaluate(
        turn,
        _capability("memory.recall", mode="read", approval=False, semantics="read_only"),
        destination_id="local-runtime",
    )
    assert evaluation.profile.persisted is False
    assert evaluation.profile.profile.profile_id == "global-boundary-compatibility"
    assert evaluation.profile.profile.mode == "guarded"


def test_v1_denied_or_unlisted_capability_is_rejected_before_boundary(tmp_path) -> None:
    with pytest.raises(TurnBoundaryAdapterError, match="outside the V1 turn policy"):
        _factory(tmp_path).evaluate(
            _turn(),
            _capability("external.web_search", mode="external", approval=True),
            destination_id="provider-manifest",
        )
    with pytest.raises(TurnBoundaryAdapterError, match="outside the V1 turn policy"):
        _factory(tmp_path).evaluate(
            _turn(),
            _capability("unknown.read", mode="read", approval=False, semantics="read_only"),
            destination_id="local-runtime",
        )


def test_local_only_turn_cannot_be_widened_by_open_project_profile(tmp_path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update(
        "project-alpha",
        mode="open",
        remote_default="allow",
        expected_revision=0,
    )
    turn = _turn()
    turn["capability_policy"]["allowed"].append("answer.remote")  # type: ignore[index]
    evaluation = TurnBoundaryRequestFactory(profiles).evaluate(
        turn,
        _capability("answer.remote", mode="external", approval=False),
        destination_id="egress-current",
        sanitization=_sanitize("普通项目内容"),
    )
    assert evaluation.decision.outcome == "deny"
    assert evaluation.decision.reason_codes == ("turn_remote_not_allowed",)


def test_legacy_requires_approval_narrows_boundary_allow_to_ask(tmp_path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update("project-alpha", mode="open", remote_default="allow", expected_revision=0)
    turn = _turn()
    turn["capability_policy"]["allowed"].append("document.write")  # type: ignore[index]
    evaluation = TurnBoundaryRequestFactory(profiles).evaluate(
        turn,
        _capability("document.write", mode="write", approval=True),
        destination_id="local-runtime",
    )
    assert evaluation.decision.outcome == "ask"
    assert evaluation.decision.reason_codes == ("legacy_turn_approval_required",)


def test_scanner_result_maps_to_remote_boundary_without_plaintext(tmp_path) -> None:
    turn = _remote_turn("answer.remote")
    sanitization = _sanitize("联系 alice@example.com")
    evaluation = _factory(tmp_path).evaluate(
        turn,
        _capability("answer.remote", mode="external", approval=False),
        destination_id="egress-current",
        sanitization=sanitization,
    )
    assert evaluation.request.scan_state == "redacted"
    assert "email" in evaluation.request.data_classes
    assert evaluation.decision.outcome == "allow_redacted"
    assert "alice@example.com" not in repr(evaluation.request)
    assert "alice@example.com" not in repr(evaluation.decision)


def test_scanner_block_maps_to_sensitive_deny(tmp_path) -> None:
    evaluation = _factory(tmp_path).evaluate(
        _remote_turn("answer.remote"),
        _capability("answer.remote", mode="external", approval=False),
        destination_id="egress-current",
        sanitization=_sanitize("api_key=abcdefghijklmnop"),
    )
    assert evaluation.request.scan_state == "sensitive"
    assert evaluation.decision.outcome == "deny"


def test_verified_file_grant_only_adds_asset_classification_to_local_read(tmp_path) -> None:
    grant = DesktopFileGrant(
        grant_id="file-grant-abcdefghijklmnopqrstuvwxyzABCDEF",
        session_instance_id="session-1",
        display_name="private-notes.txt",
        media_type="text/plain",
        source_kind="file",
        size_bytes=128,
        sha256="a" * 64,
        expires_at_ms=9999999999999,
    )
    evaluation = _factory(tmp_path).evaluate(
        _turn(),
        _capability("memory.recall", mode="read", approval=False, semantics="read_only"),
        destination_id="local-runtime",
        verified_file_grant=grant,
    )
    encoded = repr(evaluation.request)
    assert "asset_file" in evaluation.request.data_classes
    assert grant.display_name not in encoded
    assert grant.sha256 not in encoded
    assert grant.grant_id not in encoded


def test_file_grant_cannot_authorize_remote_or_mutating_tool(tmp_path) -> None:
    grant = DesktopFileGrant("file-grant-abcdefghijklmnopqrstuvwxyzABCDEF", "session-1", "a.txt", "text/plain", "file", 1, "a" * 64, 9999999999999)
    turn = _remote_turn("answer.remote")
    with pytest.raises(TurnBoundaryAdapterError, match="bounded local read"):
        _factory(tmp_path).evaluate(
            turn,
            _capability("answer.remote", mode="external", approval=False),
            destination_id="egress-current",
            sanitization=_sanitize("普通内容"),
            verified_file_grant=grant,
        )


def _factory(root: Path) -> TurnBoundaryRequestFactory:
    return TurnBoundaryRequestFactory(ProjectBoundaryProfileStore(root))


def _turn() -> dict[str, object]:
    path = ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _remote_turn(capability_id: str) -> dict[str, object]:
    turn = copy.deepcopy(_turn())
    turn["privacy"] = {
        "mode": "remote_allowed",
        "allow_remote": True,
        "pii": "none",
        "consent_refs": [],
        "retention": "local_durable",
    }
    turn["capability_policy"]["allowed"].append(capability_id)  # type: ignore[index]
    return turn


def _capability(
    capability_id: str,
    *,
    mode: str,
    approval: bool,
    semantics: str = "receipt_required",
) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=capability_id,
        version=1,
        mode=mode,
        requires_approval=approval,
        operation_semantics=semantics,
        input_schema_uri="crp://input",
        output_schema_uri="crp://output",
    )


def _sanitize(text: str):
    return SensitiveTextScanner().sanitize_for_remote(
        text,
        vault=EphemeralTokenVault(),
        turn_id="turn-0123456789abcdef0123456789abcdef",
        destination_id="egress-current",
    )
