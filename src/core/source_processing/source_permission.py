from __future__ import annotations

"""Immutable, project-scoped user permission for processing a resolved source.

This authority deliberately records only a user's permission to process one
normalised source.  It does not grant network, tool, secret, or Boundary
capabilities.  Those remain separate control-plane concerns.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Literal

from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError


class SourcePermissionError(ValueError):
    pass


class SourcePermissionConflict(SourcePermissionError):
    pass


PermissionState = Literal["granted", "revoked"]
PermissionAction = Literal["grant", "revoke"]
_PERMISSION_LOCK = RLock()


@dataclass(frozen=True)
class SourcePermissionRevision:
    namespace_id: str
    project_id: str
    permission_id: str
    revision: int
    public_ref: str
    action: PermissionAction
    state: PermissionState
    source_id: str
    platform: str
    scope: str
    source_manifest_ref: str
    source_manifest_revision: str
    metadata_evidence_ref: str
    predecessor_ref: str | None
    actor_id: str
    command_id: str
    created_at: str
    revocation_generation: int


class SourcePermissionAuthority:
    """Append-only user-consent revisions stored independently of runtime jobs."""

    collection = "source_permissions"
    head_collection = "source_permission_heads"
    source_index_collection = "source_permission_source_index"
    _SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
    _REVISION = re.compile(r"^r[1-9][0-9]*$")
    _UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$")
    _REF = re.compile(r"^crp://[^/]+/.+$")
    _SCOPE_NAME = "media_process"

    def __init__(self, object_store: ObjectStorePort, *, namespace_id: str) -> None:
        self._object_store = object_store
        self._scope(namespace_id, "namespace_id")
        if getattr(object_store, "namespace_id", None) != namespace_id:
            raise SourcePermissionError("source permission namespace does not match its object store")
        self.namespace_id = namespace_id

    def public_ref(self, *, project_id: str, permission_id: str, revision: int) -> str:
        self._scope(project_id, "project_id")
        self._scope(permission_id, "permission_id")
        self._revision(revision)
        return f"crp://{self.namespace_id}/source-permissions/projects/{project_id}/{permission_id}/r{revision}"

    def current(self, *, project_id: str, permission_id: str) -> SourcePermissionRevision | None:
        with _PERMISSION_LOCK:
            return self._current(project_id=project_id, permission_id=permission_id)

    def _current(self, *, project_id: str, permission_id: str) -> SourcePermissionRevision | None:
        self._scope(project_id, "project_id")
        self._scope(permission_id, "permission_id")
        head_id = self._head_id(project_id, permission_id)
        raw_head = self._object_store.read(self.head_collection, head_id)
        if raw_head is None:
            first = self._read_revision(project_id, permission_id, 1)
            if first is None:
                return None
            current = first
            self._validate_transition(None, current)
            self._advance_head(current, expected_head_revision=0)
        else:
            current = self._head_record(raw_head, project_id=project_id, permission_id=permission_id)
            stored = self._read_revision(project_id, permission_id, current.revision)
            if stored != current:
                raise SourcePermissionError("source permission head does not match immutable revision")
            self._validate_chain_to(current)
        while True:
            successor = self._read_revision(project_id, permission_id, current.revision + 1)
            if successor is None:
                return current
            self._validate_transition(current, successor)
            try:
                self._advance_head(successor, expected_head_revision=current.revision)
            except ObjectStoreRevisionError:
                refreshed = self._object_store.read(self.head_collection, head_id)
                if refreshed is None:
                    raise SourcePermissionError("source permission head disappeared during repair")
                current = self._head_record(
                    refreshed, project_id=project_id, permission_id=permission_id
                )
                if current.revision < successor.revision:
                    raise SourcePermissionError("source permission head CAS metadata drifted")
                continue
            current = successor

    def current_for_source(
        self,
        *,
        project_id: str,
        source_id: str,
        metadata_evidence_ref: str,
    ) -> SourcePermissionRevision | None:
        self._scope(project_id, "project_id")
        self._scope(source_id, "source_id")
        self._project_ref(metadata_evidence_ref, project_id, "metadata_evidence_ref")
        raw = self._object_store.read(
            self.source_index_collection, self._source_index_id(project_id, source_id)
        )
        if raw is None:
            # The production grant API derives permission_id from source_id.
            # This direct fallback upgrades revisions created before the index
            # existed without scanning the permission collection.
            candidate = self.current(project_id=project_id, permission_id=source_id)
            if candidate is None:
                return None
            if candidate.source_id != source_id or candidate.metadata_evidence_ref != metadata_evidence_ref:
                raise SourcePermissionError("source permission legacy identity drifted")
            self._ensure_source_index(candidate)
            return candidate
        if set(raw) != {
            "schema_version", "kind", "namespace_id", "project_id", "source_id",
            "metadata_evidence_ref", "permission_id",
        } or raw.get("schema_version") != "1.0.0" or raw.get("kind") != "source_permission_source_index":
            raise SourcePermissionError("source permission source index fields are invalid")
        if (
            raw.get("namespace_id") != self.namespace_id
            or raw.get("project_id") != project_id
            or raw.get("source_id") != source_id
            or raw.get("metadata_evidence_ref") != metadata_evidence_ref
            or not isinstance(raw.get("permission_id"), str)
        ):
            raise SourcePermissionError("source permission source index identity drifted")
        current = self.current(project_id=project_id, permission_id=raw["permission_id"])
        if current is None or current.source_id != source_id or current.metadata_evidence_ref != metadata_evidence_ref:
            raise SourcePermissionError("source permission source index target drifted")
        return current

    def grant(
        self,
        *,
        project_id: str,
        permission_id: str,
        source_id: str,
        platform: str,
        source_manifest_ref: str,
        source_manifest_revision: str,
        metadata_evidence_ref: str,
        actor_id: str,
        command_id: str,
        created_at: str,
        expected_revision: int,
    ) -> SourcePermissionRevision:
        return self._append(
            action="grant",
            project_id=project_id,
            permission_id=permission_id,
            source_id=source_id,
            platform=platform,
            source_manifest_ref=source_manifest_ref,
            source_manifest_revision=source_manifest_revision,
            metadata_evidence_ref=metadata_evidence_ref,
            actor_id=actor_id,
            command_id=command_id,
            created_at=created_at,
            expected_revision=expected_revision,
        )

    def revoke(
        self,
        *,
        project_id: str,
        permission_id: str,
        actor_id: str,
        command_id: str,
        created_at: str,
        expected_revision: int,
    ) -> SourcePermissionRevision:
        current = self.current(project_id=project_id, permission_id=permission_id)
        if current is None:
            raise SourcePermissionConflict("source permission does not exist")
        return self._append(
            action="revoke",
            project_id=project_id,
            permission_id=permission_id,
            source_id=current.source_id,
            platform=current.platform,
            source_manifest_ref=current.source_manifest_ref,
            source_manifest_revision=current.source_manifest_revision,
            metadata_evidence_ref=current.metadata_evidence_ref,
            actor_id=actor_id,
            command_id=command_id,
            created_at=created_at,
            expected_revision=expected_revision,
        )

    def _append(
        self,
        *,
        action: PermissionAction,
        project_id: str,
        permission_id: str,
        source_id: str,
        platform: str,
        source_manifest_ref: str,
        source_manifest_revision: str,
        metadata_evidence_ref: str,
        actor_id: str,
        command_id: str,
        created_at: str,
        expected_revision: int,
    ) -> SourcePermissionRevision:
        with _PERMISSION_LOCK:
            return self._append_locked(
                action=action, project_id=project_id, permission_id=permission_id,
                source_id=source_id, platform=platform,
                source_manifest_ref=source_manifest_ref,
                source_manifest_revision=source_manifest_revision,
                metadata_evidence_ref=metadata_evidence_ref, actor_id=actor_id,
                command_id=command_id, created_at=created_at,
                expected_revision=expected_revision,
            )

    def _append_locked(
        self,
        *,
        action: PermissionAction,
        project_id: str,
        permission_id: str,
        source_id: str,
        platform: str,
        source_manifest_ref: str,
        source_manifest_revision: str,
        metadata_evidence_ref: str,
        actor_id: str,
        command_id: str,
        created_at: str,
        expected_revision: int,
    ) -> SourcePermissionRevision:
        self._scope(project_id, "project_id")
        self._scope(permission_id, "permission_id")
        self._scope(source_id, "source_id")
        self._scope(platform, "platform")
        self._scope(actor_id, "actor_id")
        self._scope(command_id, "command_id")
        self._manifest_ref(source_manifest_ref, project_id)
        self._project_ref(metadata_evidence_ref, project_id, "metadata_evidence_ref")
        if not self._REVISION.fullmatch(source_manifest_revision):
            raise SourcePermissionError("source_manifest_revision is invalid")
        if not self._UTC.fullmatch(created_at):
            raise SourcePermissionError("created_at must be a UTC timestamp")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise SourcePermissionError("expected_revision must be a non-negative integer")

        prior = self.current(project_id=project_id, permission_id=permission_id)
        existing_command = self._find_command(
            project_id=project_id, permission_id=permission_id, command_id=command_id
        )
        if existing_command is not None:
            if (
                existing_command.action != action
                or existing_command.source_id != source_id
                or existing_command.platform != platform
                or existing_command.source_manifest_ref != source_manifest_ref
                or existing_command.source_manifest_revision != source_manifest_revision
                or existing_command.metadata_evidence_ref != metadata_evidence_ref
                or existing_command.actor_id != actor_id
            ):
                raise SourcePermissionConflict("source permission command id conflicts with immutable revision")
            return existing_command
        actual_revision = 0 if prior is None else prior.revision
        if expected_revision != actual_revision:
            raise SourcePermissionConflict(
                f"source permission revision conflict: expected {expected_revision}, current {actual_revision}"
            )
        if prior is not None and (
            prior.source_id != source_id or prior.platform != platform
            or prior.source_manifest_ref != source_manifest_ref
            or prior.source_manifest_revision != source_manifest_revision
            or prior.metadata_evidence_ref != metadata_evidence_ref
        ):
            raise SourcePermissionConflict("source permission identity cannot change across revisions")
        if action == "revoke" and (prior is None or prior.state != "granted"):
            raise SourcePermissionConflict("only an active source permission can be revoked")
        if action == "grant" and prior is not None and prior.state == "granted":
            raise SourcePermissionConflict("source permission is already granted")
        revision = actual_revision + 1
        record = self._candidate(
            action=action, project_id=project_id, permission_id=permission_id,
            source_id=source_id, platform=platform, source_manifest_ref=source_manifest_ref,
            source_manifest_revision=source_manifest_revision, metadata_evidence_ref=metadata_evidence_ref,
            actor_id=actor_id, command_id=command_id, created_at=created_at,
            predecessor=None if prior is None else prior.public_ref,
            revision=revision,
            revocation_generation=(0 if prior is None else prior.revocation_generation) + (1 if action == "revoke" else 0),
        )
        object_id = self._object_id(project_id, permission_id, revision)
        payload = self._payload(record)
        try:
            stored_revision = self._object_store.write(self.collection, object_id, payload, expected_revision=0)
            if stored_revision != 1:
                raise SourcePermissionError("immutable source permission revision must start at storage revision 1")
        except ObjectStoreRevisionError:
            stored = self._object_store.read(self.collection, object_id)
            if stored is None:
                raise
            replay = self._record(stored)
            if replay != record:
                raise SourcePermissionConflict("source permission revision conflicts with an immutable record")
            self._ensure_head(replay)
            return replay
        self._ensure_head(record)
        return record

    def _ensure_head(self, record: SourcePermissionRevision) -> None:
        current = self.current(project_id=record.project_id, permission_id=record.permission_id)
        if current is None or current.revision < record.revision:
            raise SourcePermissionError("source permission head did not reach immutable revision")
        if current.revision == record.revision and current != record:
            raise SourcePermissionError("source permission head identity drifted")
        self._ensure_source_index(record)

    def _ensure_source_index(self, record: SourcePermissionRevision) -> None:
        object_id = self._source_index_id(record.project_id, record.source_id)
        payload = {
            "schema_version": "1.0.0",
            "kind": "source_permission_source_index",
            "namespace_id": self.namespace_id,
            "project_id": record.project_id,
            "source_id": record.source_id,
            "metadata_evidence_ref": record.metadata_evidence_ref,
            "permission_id": record.permission_id,
        }
        existing = self._object_store.read(self.source_index_collection, object_id)
        if existing is not None:
            if dict(existing) != payload:
                raise SourcePermissionConflict("source permission source index identity conflicts")
            return
        try:
            self._object_store.write(
                self.source_index_collection, object_id, payload, expected_revision=0
            )
        except ObjectStoreRevisionError:
            existing = self._object_store.read(self.source_index_collection, object_id)
            if existing is None or dict(existing) != payload:
                raise SourcePermissionConflict("source permission source index identity conflicts")

    def _read_revision(
        self, project_id: str, permission_id: str, revision: int
    ) -> SourcePermissionRevision | None:
        raw = self._object_store.read(
            self.collection, self._object_id(project_id, permission_id, revision)
        )
        return None if raw is None else self._record(raw)

    def _validate_chain_to(self, head: SourcePermissionRevision) -> None:
        previous: SourcePermissionRevision | None = None
        for revision in range(1, head.revision + 1):
            current = self._read_revision(
                head.project_id, head.permission_id, revision
            )
            if current is None:
                raise SourcePermissionError("source permission immutable chain is incomplete")
            self._validate_transition(previous, current)
            previous = current
        if previous != head:
            raise SourcePermissionError("source permission head does not match immutable chain")

    def _advance_head(
        self, record: SourcePermissionRevision, *, expected_head_revision: int
    ) -> None:
        self._object_store.write(
            self.head_collection,
            self._head_id(record.project_id, record.permission_id),
            self._head_payload(record),
            expected_revision=expected_head_revision,
        )

    @staticmethod
    def _head_payload(record: SourcePermissionRevision) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "kind": "source_permission_head_projection",
            "namespace_id": record.namespace_id,
            "project_id": record.project_id,
            "permission_id": record.permission_id,
            "revision": record.revision,
            "revision_ref": record.public_ref,
        }

    def _head_record(
        self, value: Mapping[str, object], *, project_id: str, permission_id: str
    ) -> SourcePermissionRevision:
        if set(value) != {
            "schema_version", "kind", "namespace_id", "project_id",
            "permission_id", "revision", "revision_ref",
        } or value.get("schema_version") != "1.0.0" or value.get("kind") != "source_permission_head_projection":
            raise SourcePermissionError("source permission head fields are invalid")
        if (
            value.get("namespace_id") != self.namespace_id
            or value.get("project_id") != project_id
            or value.get("permission_id") != permission_id
        ):
            raise SourcePermissionError("source permission head scope drifted")
        revision = value.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise SourcePermissionError("source permission head revision is invalid")
        expected_ref = self.public_ref(
            project_id=project_id, permission_id=permission_id, revision=revision
        )
        if value.get("revision_ref") != expected_ref:
            raise SourcePermissionError("source permission head reference drifted")
        record = self._read_revision(project_id, permission_id, revision)
        if record is None:
            raise SourcePermissionError("source permission head revision is missing")
        return record

    @staticmethod
    def _validate_transition(
        previous: SourcePermissionRevision | None, current: SourcePermissionRevision
    ) -> None:
        if previous is None:
            if current.revision != 1 or current.action != "grant" or current.revocation_generation != 0 or current.predecessor_ref is not None:
                raise SourcePermissionError("source permission first revision is invalid")
            return
        if current.revision != previous.revision + 1 or current.predecessor_ref != previous.public_ref:
            raise SourcePermissionError("source permission predecessor chain drifted")
        expected_generation = previous.revocation_generation + (1 if current.action == "revoke" else 0)
        if current.revocation_generation != expected_generation:
            raise SourcePermissionError("source permission revocation generation drifted")

    def _find_command(self, *, project_id: str, permission_id: str, command_id: str) -> SourcePermissionRevision | None:
        records = [
            item for item in (self._record(raw) for raw in self._object_store.list(self.collection))
            if item.project_id == project_id and item.permission_id == permission_id and item.command_id == command_id
        ]
        if len(records) > 1:
            raise SourcePermissionError("source permission command id appears in multiple revisions")
        return records[0] if records else None

    def _previous(self, project_id: str, permission_id: str, revision: int) -> str:
        if revision <= 1:
            raise SourcePermissionError("first source permission revision has no predecessor")
        return self.public_ref(project_id=project_id, permission_id=permission_id, revision=revision - 1)

    def _candidate(self, *, action: PermissionAction, project_id: str, permission_id: str, source_id: str, platform: str, source_manifest_ref: str, source_manifest_revision: str, metadata_evidence_ref: str, actor_id: str, command_id: str, created_at: str, predecessor: str | None, revision: int, revocation_generation: int) -> SourcePermissionRevision:
        return SourcePermissionRevision(
            namespace_id=self.namespace_id, project_id=project_id, permission_id=permission_id,
            revision=revision, public_ref=self.public_ref(project_id=project_id, permission_id=permission_id, revision=revision),
            action=action, state="granted" if action == "grant" else "revoked",
            source_id=source_id, platform=platform, scope=self._SCOPE_NAME,
            source_manifest_ref=source_manifest_ref, source_manifest_revision=source_manifest_revision,
            metadata_evidence_ref=metadata_evidence_ref, predecessor_ref=predecessor,
            actor_id=actor_id, command_id=command_id, created_at=created_at,
            revocation_generation=revocation_generation,
        )

    @staticmethod
    def _payload(record: SourcePermissionRevision) -> dict[str, object]:
        return {
            "schema_version": "1.0.0", "kind": "source_permission_revision",
            "namespace_id": record.namespace_id, "project_id": record.project_id,
            "permission_id": record.permission_id, "revision": record.revision,
            "public_ref": record.public_ref, "action": record.action, "state": record.state,
            "source_id": record.source_id, "platform": record.platform, "scope": record.scope,
            "source_manifest_ref": record.source_manifest_ref,
            "source_manifest_revision": record.source_manifest_revision,
            "metadata_evidence_ref": record.metadata_evidence_ref,
            "predecessor_ref": record.predecessor_ref, "actor_id": record.actor_id,
            "command_id": record.command_id, "created_at": record.created_at,
            "revocation_generation": record.revocation_generation,
        }

    def _record(self, value: Mapping[str, object]) -> SourcePermissionRevision:
        required = {
            "schema_version", "kind", "namespace_id", "project_id", "permission_id", "revision", "public_ref",
            "action", "state", "source_id", "platform", "scope", "source_manifest_ref", "source_manifest_revision",
            "metadata_evidence_ref", "predecessor_ref", "actor_id", "command_id", "created_at", "revocation_generation",
        }
        if set(value) != required or value.get("schema_version") != "1.0.0" or value.get("kind") != "source_permission_revision":
            raise SourcePermissionError("stored source permission revision fields are invalid")
        strings = ("namespace_id", "project_id", "permission_id", "public_ref", "action", "state", "source_id", "platform", "scope", "source_manifest_ref", "source_manifest_revision", "metadata_evidence_ref", "actor_id", "command_id", "created_at")
        if any(not isinstance(value[name], str) for name in strings):
            raise SourcePermissionError("stored source permission revision is invalid")
        predecessor = value["predecessor_ref"]
        if predecessor is not None and not isinstance(predecessor, str):
            raise SourcePermissionError("stored source permission predecessor is invalid")
        revision = value["revision"]
        generation = value["revocation_generation"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1 or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise SourcePermissionError("stored source permission numeric fields are invalid")
        action = value["action"]
        state = value["state"]
        if action not in {"grant", "revoke"} or state != ("granted" if action == "grant" else "revoked"):
            raise SourcePermissionError("stored source permission state is invalid")
        if value["namespace_id"] != self.namespace_id or value["scope"] != self._SCOPE_NAME:
            raise SourcePermissionError("stored source permission authority scope is invalid")
        for name in ("project_id", "permission_id", "source_id", "platform", "actor_id", "command_id"):
            self._scope(value[name], name)  # type: ignore[arg-type]
        self._manifest_ref(value["source_manifest_ref"], value["project_id"])  # type: ignore[arg-type]
        self._project_ref(value["metadata_evidence_ref"], value["project_id"], "metadata_evidence_ref")  # type: ignore[arg-type]
        if not self._REVISION.fullmatch(value["source_manifest_revision"]):
            raise SourcePermissionError("stored source manifest revision is invalid")
        if not self._UTC.fullmatch(value["created_at"]):
            raise SourcePermissionError("stored source permission timestamp is invalid")
        expected_ref = self.public_ref(project_id=value["project_id"], permission_id=value["permission_id"], revision=revision)  # type: ignore[arg-type]
        if value["public_ref"] != expected_ref:
            raise SourcePermissionError("stored source permission public ref is invalid")
        if revision == 1 and predecessor is not None:
            raise SourcePermissionError("first source permission revision cannot have a predecessor")
        return SourcePermissionRevision(
            namespace_id=value["namespace_id"], project_id=value["project_id"], permission_id=value["permission_id"], revision=revision,
            public_ref=value["public_ref"], action=action, state=state, source_id=value["source_id"], platform=value["platform"], scope=value["scope"],
            source_manifest_ref=value["source_manifest_ref"], source_manifest_revision=value["source_manifest_revision"], metadata_evidence_ref=value["metadata_evidence_ref"],
            predecessor_ref=predecessor, actor_id=value["actor_id"], command_id=value["command_id"], created_at=value["created_at"], revocation_generation=generation,
        )  # type: ignore[arg-type]

    def _object_id(self, project_id: str, permission_id: str, revision: int) -> str:
        object_id = f"{project_id}~{permission_id}~r{revision}"
        if len(object_id) > 128:
            raise SourcePermissionError("source permission storage identity is too long")
        return object_id

    def _head_id(self, project_id: str, permission_id: str) -> str:
        self._scope(project_id, "project_id")
        self._scope(permission_id, "permission_id")
        object_id = f"{project_id}~{permission_id}"
        if len(object_id) > 128:
            raise SourcePermissionError("source permission head storage identity is too long")
        return object_id

    def _source_index_id(self, project_id: str, source_id: str) -> str:
        self._scope(project_id, "project_id")
        self._scope(source_id, "source_id")
        object_id = f"{project_id}~{source_id}"
        if len(object_id) > 128:
            raise SourcePermissionError("source permission source index identity is too long")
        return object_id

    @classmethod
    def _scope(cls, value: str, label: str) -> None:
        if not isinstance(value, str) or not cls._SCOPE.fullmatch(value):
            raise SourcePermissionError(f"{label} is invalid")

    @classmethod
    def _revision(cls, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SourcePermissionError("revision is invalid")

    @classmethod
    def _ref(cls, value: str, label: str) -> None:
        if not isinstance(value, str) or not cls._REF.fullmatch(value):
            raise SourcePermissionError(f"{label} must be a controlled crp reference")

    def _manifest_ref(self, value: str, project_id: str) -> None:
        self._project_ref(value, project_id, "source_manifest_ref")
        prefix = f"crp://{self.namespace_id}/source-manifests/projects/{project_id}/"
        manifest_id = value.removeprefix(prefix)
        if not value.startswith(prefix) or "/" in manifest_id or not self._SCOPE.fullmatch(manifest_id):
            raise SourcePermissionError("source_manifest_ref is outside project source-manifests scope")

    def _project_ref(self, value: str, project_id: str, label: str) -> None:
        self._ref(value, label)
        prefix = f"crp://{self.namespace_id}/"
        project_marker = f"/projects/{project_id}/"
        if not value.startswith(prefix) or project_marker not in value:
            raise SourcePermissionError(f"{label} is outside namespace/project scope")
