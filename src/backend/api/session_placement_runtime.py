"""Local, host-authoritative Session Placement runtime.

This module is intentionally independent of FastAPI and Electron wiring.  It
provides the narrow local boundary that routes can later call: export a signed
resume pointer, import it once for a paired device, expose a read-only
projection, and return every requested side effect to the originating host.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import json
import sqlite3
import time
from typing import Callable, Mapping, Protocol

from backend.security.host_signing_identity_store import HostSigningIdentityStore
from core.storage_provider import ObjectStorePort

from core.product_core.session_placement import (
    CapabilityDescriptor,
    DeviceIdentity,
    PairedDeviceRegistry,
    PlacementSafetyState,
    ResumeBundle,
    ResumeBundleService,
    ResumedSessionProjection,
    SessionPlacementError,
    WorkspaceManifestEntry,
)
from core.product_core.workspace_resume_reconcile import (
    WorkspaceReconcilePlan,
    reconcile_workspace_manifests,
)
from core.product_core.workspace_snapshot import (
    WorkspaceSnapshotBudget,
    WorkspaceSnapshotError,
    create_workspace_manifest_snapshot,
)


_EMPTY_WORKSPACE_BASE_REF = "workspace-manifest:empty-v1"
_WORKSPACE_BASE_REVISION = "workspace-base:initial-v1"
_PLACEMENT_WORKSPACE_BUDGET = WorkspaceSnapshotBudget(
    max_entries=512,
    max_file_bytes=8 * 1024 * 1024,
    max_total_bytes=32 * 1024 * 1024,
    max_scan_seconds=3.0,
)


@dataclass(frozen=True, slots=True)
class LocalResumeExport:
    """Host-derived references only; this is never session content or a secret."""

    target_device_id: str
    project_id: str
    session_ref: str
    turn_refs: tuple[str, ...]
    context_manifest_ref: str
    context_manifest_revision: str
    last_event_cursor: str
    display_summary: str
    workspace_base_manifest_ref: str
    workspace_manifest: tuple[WorkspaceManifestEntry, ...]
    capability_descriptors: tuple[CapabilityDescriptor, ...]
    expires_at: str


@dataclass(frozen=True, slots=True)
class HostAuthorizationRequired:
    """A paired-device effect request is evidence, never an execution grant."""

    status: str
    session_ref: str
    source_device_id: str
    operation_identity: str
    parameter_digest: str
    host_authorization_required: bool = True
    source_host_transport_required: bool = True
    execution_supported: bool = False


class SessionPlacementRuntime:
    """Keeps local recovery read-only while the host retains all authority."""

    def __init__(
        self,
        *,
        bundles: ResumeBundleService,
        safety_for_session: Callable[[str], PlacementSafetyState],
        recovery_store: "ResumedSessionProjectionStore | None" = None,
    ) -> None:
        self._bundles = bundles
        self._safety_for_session = safety_for_session
        self._recovery_store = recovery_store
        self._recoveries: dict[str, ResumedSessionProjection] = {}

    def export_local(self, request: LocalResumeExport) -> ResumeBundle:
        if not isinstance(request, LocalResumeExport):
            raise SessionPlacementError("local resume export request is invalid")
        return self._bundles.create(
            target_device_id=request.target_device_id,
            project_id=request.project_id,
            session_ref=request.session_ref,
            turn_refs=request.turn_refs,
            context_manifest_ref=request.context_manifest_ref,
            context_manifest_revision=request.context_manifest_revision,
            last_event_cursor=request.last_event_cursor,
            display_summary=request.display_summary,
            workspace_base_manifest_ref=request.workspace_base_manifest_ref,
            workspace_manifest=request.workspace_manifest,
            capability_descriptors=request.capability_descriptors,
            expires_at=request.expires_at,
            safety=self._safety_for_session(request.session_ref),
        )

    def import_local(
        self,
        wire: Mapping[str, object],
        *,
        local_device_id: str,
        now: datetime | None = None,
    ) -> ResumedSessionProjection:
        session_ref = wire.get("session_ref") if isinstance(wire, Mapping) else None
        if not isinstance(session_ref, str):
            raise SessionPlacementError("resume bundle session reference is invalid")
        projection = self._bundles.import_for_paired_device(
            wire,
            local_device_id=local_device_id,
            safety=self._safety_for_session(session_ref),
            now=now,
        )
        self._recoveries[projection.bundle_id] = projection
        if self._recovery_store is not None:
            self._recovery_store.save(projection)
        return projection

    def read_recovery(self, bundle_id: str) -> ResumedSessionProjection | None:
        """Return the imported projection only; no session mutation is exposed."""
        projection = self._recoveries.get(bundle_id)
        if projection is None and self._recovery_store is not None:
            projection = self._recovery_store.get(bundle_id)
        if projection is not None:
            self._bundles.validate_projection_access(projection)
        return projection

    def list_recoveries(self, *, project_id: str, limit: int = 32) -> tuple[ResumedSessionProjection, ...]:
        project_id = _project_id(project_id)
        limit = _bounded_limit(limit)
        if self._recovery_store is not None and hasattr(self._recovery_store, "list"):
            values = self._recovery_store.list()
        else:
            values = tuple(self._recoveries.values())
        actionable = []
        for item in values:
            if item.project_id != project_id:
                continue
            try:
                self._bundles.validate_projection_access(item)
            except SessionPlacementError:
                continue
            actionable.append(item)
            if len(actionable) >= limit:
                break
        return tuple(actionable)

    def workspace_reconcile_plan(
        self,
        *,
        bundle_id: str,
        workspace_root: str | Path,
    ) -> WorkspaceReconcilePlan:
        """Create a plan-only diff from a newly scanned, host-owned workspace."""
        projection = self.read_recovery(bundle_id)
        if projection is None:
            raise SessionPlacementError("resumed session projection does not exist")
        if projection.workspace_base_manifest_ref != _EMPTY_WORKSPACE_BASE_REF:
            raise SessionPlacementError("workspace reconciliation base is unsupported")
        try:
            host_snapshot = create_workspace_manifest_snapshot(
                workspace_root,
                base_revision=_WORKSPACE_BASE_REVISION,
                budget=_PLACEMENT_WORKSPACE_BUDGET,
            )
        except WorkspaceSnapshotError as error:
            raise SessionPlacementError("workspace snapshot was rejected") from error
        source_manifest = {
            entry.relative_path: {
                "digest": entry.digest,
                "size_bytes": entry.size_bytes,
                "base_revision": entry.base_revision,
            }
            for entry in projection.workspace_manifest
        }
        host_manifest = {
            entry.relative_path: {
                "digest": entry.digest,
                "size_bytes": entry.size_bytes,
                "base_revision": entry.base_revision,
            }
            for entry in host_snapshot.entries
        }
        return reconcile_workspace_manifests(
            source_manifest=source_manifest,
            host_manifest=host_manifest,
            base_manifest={},
        )

    def request_host_authorization(
        self,
        *,
        bundle_id: str,
        operation_identity: str,
        parameter_digest: str,
    ) -> HostAuthorizationRequired:
        projection = self.read_recovery(bundle_id)
        if projection is None:
            raise SessionPlacementError("resumed session projection does not exist")
        if not isinstance(operation_identity, str) or not operation_identity.strip():
            raise SessionPlacementError("operation identity is invalid")
        if (
            not isinstance(parameter_digest, str)
            or len(parameter_digest) != 64
            or any(character not in "0123456789abcdef" for character in parameter_digest)
        ):
            raise SessionPlacementError("operation parameter digest is invalid")
        return HostAuthorizationRequired(
            status="host_authorization_required",
            session_ref=projection.session_ref,
            source_device_id=projection.source_device_id,
            operation_identity=operation_identity,
            parameter_digest=parameter_digest,
        )


class ResumedSessionProjectionStore(Protocol):
    def save(self, projection: ResumedSessionProjection) -> None: ...
    def get(self, bundle_id: str) -> ResumedSessionProjection | None: ...


class ObjectStoreResumedSessionProjectionStore:
    collection = "session_placement_recoveries"

    def __init__(self, store: ObjectStorePort) -> None:
        self._store = store

    def save(self, projection: ResumedSessionProjection) -> None:
        payload = _projection_payload(projection)
        existing = self._store.read(self.collection, projection.bundle_id)
        if existing is not None:
            if dict(existing) != payload:
                raise SessionPlacementError("resumed session projection identity conflicts")
            return
        self._store.write(self.collection, projection.bundle_id, payload, expected_revision=0)

    def get(self, bundle_id: str) -> ResumedSessionProjection | None:
        item = self._store.read(self.collection, bundle_id)
        return None if item is None else _projection_from_payload(item)

    def list(self) -> tuple[ResumedSessionProjection, ...]:
        values = []
        for item in self._store.list(self.collection):
            try:
                values.append(_projection_from_payload(item))
            except SessionPlacementError:
                # v1 records without an explicit project scope are never listed.
                continue
        return tuple(values)


def list_local_resume_candidates(*, root_dir: Path, project_id: str, limit: int = 32) -> tuple[dict[str, object], ...]:
    project_id = _project_id(project_id)
    limit = _bounded_limit(limit)
    database = Path(root_dir) / ".rebuild-data" / "ai-turns.sqlite3"
    if not database.is_file(): return ()
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT session_id,COUNT(*),MAX(rowid) FROM ai_turns GROUP BY session_id ORDER BY MAX(rowid) DESC LIMIT ?", (limit * 8,)).fetchall()
        session_scopes = {
            str(session_id): connection.execute(
                "SELECT request_json FROM ai_turns WHERE session_id=?", (session_id,)
            ).fetchall()
            for session_id, _count, _rowid in rows
        }
    result = []
    for session_id, count, _rowid in rows:
        projects = set()
        try:
            for (request_json,) in session_scopes[str(session_id)]:
                scope = json.loads(str(request_json)).get("scope", {})
                candidate_project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
                if not isinstance(candidate_project_id, str) or not candidate_project_id:
                    raise ValueError("project scope is invalid")
                projects.add(candidate_project_id)
        except (json.JSONDecodeError, AttributeError, ValueError):
            continue
        if projects == {project_id}:
            result.append({"session_id": str(session_id), "project_id": project_id, "turn_count": int(count)})
        if len(result) >= limit:
            break
    return tuple(result)


def build_local_session_placement_runtime(
    *,
    root_dir: Path,
    host_signer: HostSigningIdentityStore,
    object_store: ObjectStorePort,
    effect_runtimes: tuple[object, ...],
) -> SessionPlacementRuntime:
    """Compose one persistent host identity and read-only local placement runtime."""

    if not isinstance(host_signer, HostSigningIdentityStore):
        raise TypeError("host signing identity capability is required")
    host = host_signer.load_or_create()
    pairs = PairedDeviceRegistry(Path(root_dir))
    existing = pairs.identity(host.device_id)
    if existing is None:
        pairs.trust(host.public_identity)
    elif existing != host.public_identity:
        raise SessionPlacementError("host signing identity drifted")
    return SessionPlacementRuntime(
        bundles=ResumeBundleService(host=host, pairs=pairs),
        safety_for_session=lambda session_ref: _placement_safety(effect_runtimes, session_ref),
        recovery_store=ObjectStoreResumedSessionProjectionStore(object_store),
    )


def local_host_identity(*, host_signer: HostSigningIdentityStore) -> DeviceIdentity:
    if not isinstance(host_signer, HostSigningIdentityStore):
        raise TypeError("host signing identity capability is required")
    return host_signer.public_identity()


def derive_local_resume_export(
    *, root_dir: Path, project_id: str, session_id: str, target_device_id: str,
    expires_at: str, workspace_root: str | Path,
) -> LocalResumeExport:
    """Read bounded Session/Turn facts from the AI authority, never the request body."""

    requested_project_id = _project_id(project_id)
    database = Path(root_dir) / ".rebuild-data" / "ai-turns.sqlite3"
    if not database.is_file():
        raise SessionPlacementError("AI Turn authority is unavailable")
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT turn_id,request_json FROM ai_turns WHERE session_id=? ORDER BY rowid LIMIT 65",
            (session_id,),
        ).fetchall()
        if not rows or len(rows) > 64:
            raise SessionPlacementError("session Turn set is unavailable or exceeds the bundle budget")
        turn_ids = tuple(str(row[0]) for row in rows)
        projects = set()
        for _turn_id, encoded in rows:
            try:
                request = json.loads(str(encoded))
            except json.JSONDecodeError as error:
                raise SessionPlacementError("session Turn request is invalid") from error
            scope = request.get("scope") if isinstance(request, Mapping) else None
            turn_project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
            if not isinstance(turn_project_id, str) or not turn_project_id:
                raise SessionPlacementError("session project authority is unavailable")
            projects.add(turn_project_id)
        if projects != {requested_project_id}:
            raise SessionPlacementError("session spans multiple projects")
        placeholders = ",".join("?" for _ in turn_ids)
        cursor = connection.execute(
            f"SELECT COALESCE(MAX(sequence),0) FROM ai_turn_events WHERE turn_id IN ({placeholders})",
            turn_ids,
        ).fetchone()[0]
        manifest = connection.execute(
            f"SELECT payload_ref,payload_json FROM ai_turn_payloads WHERE kind='context-manifest' "
            f"AND turn_id IN ({placeholders}) ORDER BY rowid DESC LIMIT 1",
            turn_ids,
        ).fetchone()
    if manifest is None:
        context_ref, context_revision = "context:none", "context-revision:none"
    else:
        context_ref = str(manifest[0])
        try:
            context_payload = json.loads(str(manifest[1]))
        except json.JSONDecodeError as error:
            raise SessionPlacementError("session context manifest is invalid") from error
        revision = context_payload.get("manifest_id") if isinstance(context_payload, Mapping) else None
        if not isinstance(revision, str) or not revision:
            raise SessionPlacementError("session context manifest revision is invalid")
        context_revision = revision
    try:
        workspace_snapshot = create_workspace_manifest_snapshot(
            workspace_root,
            base_revision=_WORKSPACE_BASE_REVISION,
            budget=_PLACEMENT_WORKSPACE_BUDGET,
        )
    except WorkspaceSnapshotError as error:
        raise SessionPlacementError("workspace snapshot was rejected") from error
    return LocalResumeExport(
        target_device_id=target_device_id,
        project_id=requested_project_id,
        session_ref=session_id,
        turn_refs=turn_ids,
        context_manifest_ref=context_ref,
        context_manifest_revision=context_revision,
        last_event_cursor=f"event:{int(cursor)}",
        display_summary=f"同一项目会话，包含 {len(turn_ids)} 个 Turn。",
        workspace_base_manifest_ref=_EMPTY_WORKSPACE_BASE_REF,
        workspace_manifest=tuple(WorkspaceManifestEntry(
            item.relative_path, item.digest, item.size_bytes, item.base_revision,
        ) for item in workspace_snapshot.entries),
        capability_descriptors=(),
        expires_at=expires_at,
    )


def _placement_safety(runtimes: tuple[object, ...], session_ref: str) -> PlacementSafetyState:
    active = 0
    states: list[str] = []
    now = time.time()
    for runtime in runtimes:
        log = getattr(runtime, "log", None)
        connect = getattr(log, "_connect", None)
        if not callable(connect):
            continue
        with connect() as connection:
            rows = connection.execute(
                "SELECT state,lease_expires_at FROM effect WHERE session_id=?",
                (session_ref,),
            ).fetchall()
        for state, lease_expires_at in rows:
            states.append(str(state))
            if str(state) == "INFLIGHT" and isinstance(lease_expires_at, (int, float)) and lease_expires_at > now:
                active += 1
    return PlacementSafetyState(active_lease_count=active, effect_states=tuple(states))


def _projection_payload(projection: ResumedSessionProjection) -> dict[str, object]:
    return {
        "schema_version": "resumed-session-projection.v2",
        "bundle_id": projection.bundle_id,
        "project_id": projection.project_id,
        "session_ref": projection.session_ref,
        "turn_refs": list(projection.turn_refs),
        "context_manifest_ref": projection.context_manifest_ref,
        "context_manifest_revision": projection.context_manifest_revision,
        "last_event_cursor": projection.last_event_cursor,
        "display_summary": projection.display_summary,
        "workspace_base_manifest_ref": projection.workspace_base_manifest_ref,
        "workspace_manifest": [asdict(entry) for entry in projection.workspace_manifest],
        "capability_descriptors": [
            {"capability_id": item.capability_id, "revision": item.revision,
             "effect_classes": list(item.effect_classes)}
            for item in projection.capability_descriptors
        ],
        "source_device_id": projection.source_device_id,
        "target_device_id": projection.target_device_id,
        "bundle_source_trust_revision": projection.bundle_source_trust_revision,
        "bundle_target_trust_revision": projection.bundle_target_trust_revision,
        "local_source_trust_revision": projection.local_source_trust_revision,
        "local_target_trust_revision": projection.local_target_trust_revision,
        "expires_at": projection.expires_at,
        "created_at": projection.created_at,
        "host_authorization_required": True,
        "read_only": True,
    }


def _projection_from_payload(value: Mapping[str, object]) -> ResumedSessionProjection:
    common = {
        "schema_version", "bundle_id", "project_id", "session_ref", "turn_refs",
        "context_manifest_ref", "context_manifest_revision", "last_event_cursor",
        "display_summary", "workspace_base_manifest_ref", "workspace_manifest",
        "capability_descriptors", "source_device_id", "created_at",
        "host_authorization_required", "read_only",
    }
    schema_version = value.get("schema_version")
    versioned = (
        {
            "target_device_id", "bundle_source_trust_revision", "bundle_target_trust_revision",
            "local_source_trust_revision", "local_target_trust_revision", "expires_at",
        }
        if schema_version == "resumed-session-projection.v2" else set()
    )
    if (
        set(value) != common | versioned
        or schema_version not in {"resumed-session-projection.v1", "resumed-session-projection.v2"}
        or value.get("host_authorization_required") is not True
        or value.get("read_only") is not True
        or not isinstance(value.get("turn_refs"), list)
        or not isinstance(value.get("workspace_manifest"), list)
        or not isinstance(value.get("capability_descriptors"), list)
    ):
        raise SessionPlacementError("resumed session projection schema is invalid")
    try:
        return ResumedSessionProjection(
            bundle_id=str(value["bundle_id"]), project_id=_project_id(value["project_id"]), session_ref=str(value["session_ref"]),
            turn_refs=tuple(str(item) for item in value["turn_refs"]),
            context_manifest_ref=str(value["context_manifest_ref"]),
            context_manifest_revision=str(value["context_manifest_revision"]),
            last_event_cursor=str(value["last_event_cursor"]),
            display_summary=str(value["display_summary"]),
            workspace_base_manifest_ref=str(value["workspace_base_manifest_ref"]),
            workspace_manifest=tuple(WorkspaceManifestEntry(**item) for item in value["workspace_manifest"]),
            capability_descriptors=tuple(CapabilityDescriptor(
                capability_id=str(item["capability_id"]), revision=int(item["revision"]),
                effect_classes=tuple(str(effect) for effect in item["effect_classes"]),
            ) for item in value["capability_descriptors"]),
            source_device_id=str(value["source_device_id"]),
            target_device_id=(str(value["target_device_id"]) if schema_version.endswith(".v2") else "legacy-target-unavailable"),
            bundle_source_trust_revision=(
                _optional_revision(value["bundle_source_trust_revision"]) if schema_version.endswith(".v2") else None
            ),
            bundle_target_trust_revision=(
                _optional_revision(value["bundle_target_trust_revision"]) if schema_version.endswith(".v2") else None
            ),
            local_source_trust_revision=(_required_revision(value["local_source_trust_revision"]) if schema_version.endswith(".v2") else 0),
            local_target_trust_revision=(_required_revision(value["local_target_trust_revision"]) if schema_version.endswith(".v2") else 0),
            expires_at=(_created_at(value["expires_at"]) if schema_version.endswith(".v2") else _created_at(value["created_at"])),
            created_at=_created_at(value["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SessionPlacementError("resumed session projection is invalid") from error


def _project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise SessionPlacementError("project scope is invalid")
    return value


def _bounded_limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 32:
        raise SessionPlacementError("session placement limit is invalid")
    return value


def _created_at(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise SessionPlacementError("resumed session projection timestamp is invalid")
    return value


def _optional_revision(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SessionPlacementError("resumed session trust provenance is invalid")
    return value


def _required_revision(value: object) -> int:
    revision = _optional_revision(value)
    if revision is None:
        raise SessionPlacementError("resumed session local trust revision is invalid")
    return revision
