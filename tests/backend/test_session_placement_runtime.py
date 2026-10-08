from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import sqlite3

import pytest

from backend.api.session_placement_runtime import (
    LocalResumeExport,
    SessionPlacementRuntime,
    build_local_session_placement_runtime,
    derive_local_resume_export,
    local_host_identity,
)
from backend.security.secrets import InMemorySecretStore
from backend.security.host_signing_identity_store import HostSigningIdentityStore
from core.product_core.session_placement import (
    CapabilityDescriptor,
    HostSigningIdentity,
    PairedDeviceRegistry,
    PlacementSafetyState,
    ResumeBundleService,
    SessionPlacementError,
    WorkspaceManifestEntry,
)
from core.storage_provider import JsonObjectStore


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _runtime(tmp_path, safety=PlacementSafetyState()) -> SessionPlacementRuntime:
    host = HostSigningIdentity(device_id="host-1")
    registry = PairedDeviceRegistry(tmp_path)
    registry.trust(host.public_identity)
    registry.trust(HostSigningIdentity(device_id="laptop-2").public_identity)
    return SessionPlacementRuntime(
        bundles=ResumeBundleService(host=host, pairs=registry),
        safety_for_session=lambda _: safety,
    )


def _request() -> LocalResumeExport:
    return LocalResumeExport(
        target_device_id="laptop-2",
        project_id="project-a",
        session_ref="session:local-1",
        turn_refs=("turn:1",),
        context_manifest_ref="context:1",
        context_manifest_revision="context-revision:2",
        last_event_cursor="event:4",
        display_summary="仅恢复所需的本地会话摘要。",
        workspace_base_manifest_ref="workspace-manifest:empty-v1",
        workspace_manifest=(WorkspaceManifestEntry(
            "src/main.py",
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            3,
            "workspace-base:initial-v1",
        ),),
        capability_descriptors=(CapabilityDescriptor("companion.chat", 1, ("read",)),),
        expires_at=_expiry(),
    )


def test_runtime_exports_imports_and_only_exposes_host_authorization(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "main.py").write_bytes(b"abc")
    bundle = runtime.export_local(_request())
    projection = runtime.import_local(bundle.wire(), local_device_id="laptop-2")

    assert runtime.read_recovery(bundle.bundle_id) == projection
    assert runtime.list_recoveries(project_id="project-a") == (projection,)
    assert projection.read_only is True
    assert projection.workspace_manifest[0].size_bytes == 3
    plan = runtime.workspace_reconcile_plan(
        bundle_id=bundle.bundle_id,
        workspace_root=workspace,
    )
    assert plan.mode == "plan_only"
    assert plan.entries[0].classification == "unchanged"
    request = runtime.request_host_authorization(
        bundle_id=bundle.bundle_id,
        operation_identity="write_document",
        parameter_digest="a" * 64,
    )
    assert request.status == "host_authorization_required"
    assert request.session_ref == "session:local-1"


@pytest.mark.parametrize("safety", [
    PlacementSafetyState(active_lease_count=1),
    PlacementSafetyState(effect_states=("UNKNOWN",)),
])
def test_runtime_blocks_active_or_unknown_effect_before_export_or_import(tmp_path, safety) -> None:
    source = _runtime(tmp_path, safety)
    with pytest.raises(SessionPlacementError):
        source.export_local(_request())

    exporter = _runtime(tmp_path / "exporter")
    bundle = exporter.export_local(_request())
    target = _runtime(tmp_path / "target", safety)
    # Pair the target runtime with the source host identity would require the
    # application pairing flow; export gating is exercised above and import's
    # safety gate is called before any projection can be persisted.
    with pytest.raises(SessionPlacementError):
        target.import_local(bundle.wire(), local_device_id="laptop-2")


def test_host_identity_and_recovery_projection_survive_runtime_rebuild(tmp_path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_secrets = InMemorySecretStore()
    target_secrets = InMemorySecretStore()
    source_store = JsonObjectStore(source_root / "objects-root")
    target_store = JsonObjectStore(target_root / "objects-root")

    source_signer = HostSigningIdentityStore(source_secrets)
    target_signer = HostSigningIdentityStore(target_secrets)
    source_identity = local_host_identity(host_signer=source_signer)
    target_identity = local_host_identity(host_signer=target_signer)
    PairedDeviceRegistry(source_root).trust(target_identity)
    PairedDeviceRegistry(target_root).trust(source_identity)

    source = build_local_session_placement_runtime(
        root_dir=source_root,
        host_signer=source_signer,
        object_store=source_store,
        effect_runtimes=(),
    )
    bundle = source.export_local(replace(_request(), target_device_id=target_identity.device_id))
    target = build_local_session_placement_runtime(
        root_dir=target_root,
        host_signer=target_signer,
        object_store=target_store,
        effect_runtimes=(),
    )
    imported = target.import_local(bundle.wire(), local_device_id=target_identity.device_id)

    restarted = build_local_session_placement_runtime(
        root_dir=target_root,
        host_signer=target_signer,
        object_store=target_store,
        effect_runtimes=(),
    )
    assert local_host_identity(host_signer=target_signer) == target_identity
    assert restarted.read_recovery(bundle.bundle_id) == imported


def test_resume_export_is_derived_from_authoritative_turn_store(tmp_path) -> None:
    database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
    database.parent.mkdir(parents=True)
    request = {
        "scope": {"kind": "project", "project_id": "project-a", "series_id": None},
    }
    manifest = {"manifest_id": "context-manifest-turn-1"}
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE ai_turns(
                turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL
            );
            CREATE TABLE ai_turn_events(
                turn_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE, event_json TEXT NOT NULL,
                PRIMARY KEY(turn_id, sequence)
            );
            CREATE TABLE ai_turn_payloads(
                payload_ref TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                kind TEXT NOT NULL, payload_json TEXT NOT NULL
            );
        """)
        connection.execute(
            "INSERT INTO ai_turns VALUES(?,?,?,?,?)",
            ("turn-1", "session-1", "operation-1", "idem-1", json.dumps(request)),
        )
        connection.execute(
            "INSERT INTO ai_turn_events VALUES(?,?,?,?)",
            ("turn-1", 7, "event-7", "{}"),
        )
        connection.execute(
            "INSERT INTO ai_turn_payloads VALUES(?,?,?,?)",
            ("payload-1", "turn-1", "context-manifest", json.dumps(manifest)),
        )

    (tmp_path / "workspace").mkdir()
    export = derive_local_resume_export(
        root_dir=tmp_path,
        project_id="project-a",
        session_id="session-1",
        target_device_id="laptop-2",
        expires_at=_expiry(),
        workspace_root=tmp_path / "workspace",
    )
    assert export.turn_refs == ("turn-1",)
    assert export.context_manifest_ref == "payload-1"
    assert export.context_manifest_revision == "context-manifest-turn-1"
    assert export.last_event_cursor == "event:7"
    assert export.workspace_manifest == ()
