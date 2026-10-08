from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.plugin_host.mcp_reference_activation import PluginMCPReferenceActivation
from core.plugin_host.package_intake import PluginPackageIntakeConflict, PluginPackageIntakeError
from core.storage_provider import SQLiteStructuredRecordStore


def _identity() -> dict[str, object]:
    return {"schema_version": "1.0.0", "server_id": "calendar", "approval_revision": 3, "manifest_revision": 7, "endpoint_identity": "calendar-endpoint", "credential_subject_id": "local-user", "transport_generation": 2}


def _snapshot(*, enabled: bool = True, identity: dict[str, object] | None = None):
    value = identity or _identity()
    host = SimpleNamespace(manifest_revision=value["manifest_revision"], endpoint_identity=value["endpoint_identity"], credential_subject_id=value["credential_subject_id"], transport_generation=value["transport_generation"])
    return SimpleNamespace(servers=(SimpleNamespace(server_id=value["server_id"], enabled=enabled, approval_revision=value["approval_revision"], host_connection=host, tool_policies=(SimpleNamespace(tool_id="calendar.read"),)),))


def _seed(root: Path, *, candidate: object | None = None) -> SQLiteStructuredRecordStore:
    store = SQLiteStructuredRecordStore(root / "jobs.sqlite3")
    value = _identity() if candidate is None else candidate
    content = json.dumps(value).encode("utf-8")
    raw = {"plugin_id": "calendar-plugin", "files": [{"relative_path": ".codex-plugin/plugin.json", "content_base64": base64.b64encode(b"{}").decode("ascii"), "size_bytes": 2}, {"relative_path": "mcp/calendar/server-ref.json", "content_base64": base64.b64encode(content).decode("ascii"), "size_bytes": len(content)}]}
    state = {"plugin_id": "calendar-plugin", "package_record_id": "calendar-plugin~1.0.0", "status": "installed_disabled", "enabled": False}
    with store.begin() as uow:
        uow.put("plugin_raw_packages", "calendar-plugin~1.0.0", raw, expected_revision=0)
        uow.put("plugin_package_states", "calendar-plugin", state, expected_revision=0)
        uow.commit()
    return store


def _service(root: Path, snapshot=None) -> PluginMCPReferenceActivation:
    return PluginMCPReferenceActivation(_seed(root), authority_loader=lambda: _snapshot() if snapshot is None else snapshot, now="2026-08-26T00:00:00Z")


def test_review_and_activation_bind_only_an_enabled_matching_approved_server(tmp_path: Path) -> None:
    service = _service(tmp_path)
    reviewed = service.review("calendar-plugin", expected_state_revision=1, command_id="review-0001", confirm=True, reason="project calendar")
    activated = service.activate("calendar-plugin", expected_review_revision=reviewed["review_revision"], expected_activation_revision=0, command_id="activate-0001", confirm=True)
    assert activated["activation"]["status"] == "active"
    bindings = service.active_contributions(["calendar-plugin"])
    assert [(binding.plugin_id, binding.server_id, dict(binding.identity)) for binding in bindings] == [("calendar-plugin", "calendar", _identity())]


def test_review_requires_confirmation_cas_and_command_replay(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(PluginPackageIntakeError, match="explicit confirmation"):
        service.review("calendar-plugin", expected_state_revision=1, command_id="review-0001", confirm=False, reason="no")
    reviewed = service.review("calendar-plugin", expected_state_revision=1, command_id="review-0001", confirm=True, reason="yes")
    assert service.review("calendar-plugin", expected_state_revision=99, command_id="review-0001", confirm=True, reason="changed")["replayed"] is True
    with pytest.raises(PluginPackageIntakeConflict, match="revision conflict"):
        service.activate("calendar-plugin", expected_review_revision=reviewed["review_revision"] + 1, expected_activation_revision=0, command_id="activate-0001", confirm=True)
    activated = service.activate("calendar-plugin", expected_review_revision=reviewed["review_revision"], expected_activation_revision=0, command_id="activate-0001", confirm=True)
    assert service.activate("calendar-plugin", expected_review_revision=999, expected_activation_revision=999, command_id="activate-0001", confirm=True)["replayed"] is True
    assert activated["activation_revision"] == 1


def test_active_query_fails_closed_for_disabled_authority_raw_or_identity_drift(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    service = PluginMCPReferenceActivation(store, authority_loader=lambda: _snapshot(), now="2026-08-26T00:00:00Z")
    reviewed = service.review("calendar-plugin", expected_state_revision=1, command_id="review-0001", confirm=True, reason="yes")
    service.activate("calendar-plugin", expected_review_revision=reviewed["review_revision"], expected_activation_revision=0, command_id="activate-0001", confirm=True)
    assert PluginMCPReferenceActivation(store, authority_loader=lambda: _snapshot(enabled=False), now="x").all_active_references() == ()
    drifted = _identity() | {"transport_generation": 9}
    assert PluginMCPReferenceActivation(store, authority_loader=lambda: _snapshot(identity=drifted), now="x").all_active_references() == ()
    raw = store.read("plugin_raw_packages", "calendar-plugin~1.0.0")
    assert raw is not None
    tampered = dict(raw.payload) | {"files": list(raw.payload["files"])}
    equivalent = json.dumps(_identity(), indent=2).encode("utf-8")
    tampered["files"][1] = dict(tampered["files"][1]) | {"content_base64": base64.b64encode(equivalent).decode("ascii"), "size_bytes": len(equivalent)}
    with store.begin() as uow:
        uow.put("plugin_raw_packages", raw.object_id, tampered, expected_revision=raw.revision)
        uow.commit()
    assert service.all_active_references() == ()


def test_reference_status_projection_is_read_only_and_distinguishes_review_unavailable_invalid(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    service = PluginMCPReferenceActivation(store, authority_loader=lambda: _snapshot(), now="2026-08-26T00:00:00Z")
    reviewed = service.review("calendar-plugin", expected_state_revision=1, command_id="review-0001", confirm=True, reason="yes")
    service.activate("calendar-plugin", expected_review_revision=reviewed["review_revision"], expected_activation_revision=0, command_id="activate-0001", confirm=True)
    assert service.reference_statuses(("calendar-plugin",))[-1].status == "active"
    drifted = _identity() | {"manifest_revision": 8}
    assert PluginMCPReferenceActivation(store, authority_loader=lambda: _snapshot(identity=drifted), now="x").reference_statuses(("calendar-plugin",))[-1].status == "needs_review"
    assert PluginMCPReferenceActivation(store, authority_loader=lambda: _snapshot(enabled=False), now="x").reference_statuses(("calendar-plugin",))[-1].status == "unavailable"
    raw = store.read("plugin_raw_packages", "calendar-plugin~1.0.0")
    assert raw is not None
    tampered = dict(raw.payload) | {"plugin_id": "different-plugin"}
    with store.begin() as uow:
        uow.put("plugin_raw_packages", raw.object_id, tampered, expected_revision=raw.revision)
        uow.commit()
    assert service.reference_statuses(("calendar-plugin",))[-1].status == "invalid"


@pytest.mark.parametrize("candidate", [{"server_id": "calendar"}, {**_identity(), "endpoint_url": "https://evil.example"}])
def test_unknown_or_incomplete_candidate_shape_is_rejected(tmp_path: Path, candidate: object) -> None:
    service = PluginMCPReferenceActivation(_seed(tmp_path, candidate=candidate), authority_loader=lambda: _snapshot(), now="2026-08-26T00:00:00Z")
    with pytest.raises(PluginPackageIntakeError, match="identity is invalid"):
        service.review("calendar-plugin", expected_state_revision=1, command_id="review-0001", confirm=True, reason="no")
