"""Disabled managed artifacts for reviewed Plugin Hands.

This is deliberately a storage-and-filesystem boundary only.  It never imports
or executes package bytes, and it has no dependency on the Plugin Host,
activation, API, Session, Boundary, Secret Store, or network layers.
"""
from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from uuid import NAMESPACE_URL, uuid5

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
)

from .package_intake import PluginPackageIntakeConflict, PluginPackageIntakeError


_RAW = "plugin_raw_packages"
_REPORTS = "plugin_compatibility_reports"
_MANIFESTS = "plugin_normalized_manifests"
_STATES = "plugin_package_states"
_REVIEWS = "plugin_hands_reviews"
_ARTIFACTS = "plugin_hands_artifacts"
_OPERATIONS = "plugin_hands_artifact_operations"
_COMMANDS = "plugin_hands_artifact_commands"
_CUTOVERS = "plugin_hands_upgrade_cutovers"
_UPGRADE_ACTIVE = "plugin_hands_upgrade_active"
_PROMOTIONS = "plugin_hands_artifact_promotions"
_ACTIVE_SLOTS = "plugin_hands_active_artifact_slots"
_PRIMARY_SLOT_ID = "primary"
_HAND_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_MAX_FILES = 128
_MAX_BYTES = 4 * 1024 * 1024


class PluginHandsArtifactError(PluginPackageIntakeError):
    """Raised when an untrusted Hand cannot become a managed artifact."""


class PluginHandsArtifactConflict(PluginPackageIntakeConflict):
    """Raised for durable identity, revision, or managed-tree drift."""


class PluginHandsArtifactCrash(RuntimeError):
    """Test-only crash seam after a durable or filesystem transition."""


@dataclass(frozen=True, slots=True)
class _ArtifactSlot:
    cutover_id: str | None
    slot_id: str
    record_id: str
    path_name: str


@dataclass(frozen=True, slots=True)
class ManagedHandsArtifact:
    plugin_id: str
    hand_id: str
    package_record_id: str
    review_revision: int
    materialization_revision: int
    containment_profile_revision: str
    runtime: str
    entrypoint: str
    input_schema: Mapping[str, object] = field(repr=False)
    output_schema: Mapping[str, object] = field(repr=False)
    effect: str
    operation_semantics: str
    requested_resources: tuple[str, ...]
    payload_files: tuple[tuple[str, bytes], ...] = field(repr=False)
    opaque_ref: str
    root: Path = field(repr=False)


class PluginHandsArtifactService:
    """Review and materialize frozen Hand bytes into a host-owned tree.

    The source inbox is intentionally absent from this class.  Every operation
    consumes only the immutable raw capture held by SQLite, so deleting or
    changing the original package can never alter a reviewed artifact.
    """

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        *,
        managed_root: Path,
        now: str,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._records = records
        self._now = _text(now, "now", 96)
        self._fault = fault
        root = Path(os.path.abspath(Path(managed_root).expanduser()))
        _assert_existing_ancestors_not_links(root)
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve(strict=True)
        self._staging = self._root / ".staging"
        self._artifacts = self._root / "artifacts"
        self._staging.mkdir(exist_ok=True)
        self._artifacts.mkdir(exist_ok=True)
        _assert_existing_ancestors_not_links(self._root)
        if _is_link(self._staging) or _is_link(self._artifacts):
            raise PluginHandsArtifactError("managed artifact directories cannot be links")
        if os.path.splitdrive(str(self._staging))[0].lower() != os.path.splitdrive(str(self._artifacts))[0].lower():
            raise PluginHandsArtifactError("staging and artifact roots must share a volume")

    def review(
        self,
        plugin_id: str,
        *,
        hand_id: str,
        expected_state_revision: int,
        command_id: str,
        confirm: bool,
        reason: str,
        containment_profile_revision: str,
        candidate_cutover_id: str | None = None,
    ) -> dict[str, object]:
        plugin, hand, command = _plugin(plugin_id), _hand(hand_id), _command(command_id)
        if confirm is not True:
            raise PluginHandsArtifactError("Plugin Hand review requires explicit confirmation")
        reason = _text(reason, "reason", 500)
        profile = _profile(containment_profile_revision)
        expected = _revision(expected_state_revision, "expected_state_revision", allow_zero=False)
        slot = _slot(plugin, hand, candidate_cutover_id)
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    replay_payload = _command_payload(replay, "review", plugin, hand)
                    if (replay_payload.get("expected_state_revision") != expected or replay_payload.get("reason") != reason
                            or replay_payload.get("containment_profile_revision") != profile
                            or replay_payload.get("candidate_cutover_id") != slot.cutover_id):
                        raise PluginHandsArtifactConflict("Plugin Hand review command identity drifted")
                    return _replay(replay, "review", plugin, hand)
                state = _installed_state(uow.read(_STATES, plugin), expected)
                package = self._package_for_slot(uow, plugin, hand, state, slot)
                raw = _required(uow.read(_RAW, package), "raw package")
                if slot.cutover_id is not None:
                    self._assert_candidate_bundle(uow.read, raw, package, plugin, hand)
                files, descriptor = _captured_hand(raw, plugin, hand)
                payload = {
                    "schema_version": "1.0.0", "plugin_id": plugin, "hand_id": hand,
                    "package_record_id": package, "state_revision": state.revision,
                    "files": _files_payload(files), "decision": "approved_disabled",
                    "descriptor": descriptor, "containment_profile_revision": profile,
                    "reviewed_by": "local-user", "reviewed_at": self._now, "reason": reason,
                }
                if slot.cutover_id is not None:
                    payload["candidate_cutover_id"] = slot.cutover_id
                current = uow.read(_REVIEWS, slot.record_id)
                if current is None:
                    reviewed = uow.put(_REVIEWS, slot.record_id, payload, expected_revision=0)
                elif dict(current.payload) == payload:
                    reviewed = current
                else:
                    raise PluginHandsArtifactConflict("Plugin Hand review identity drifted")
                result = _review_result(reviewed, replayed=False)
                uow.put(_COMMANDS, command, {
                    "operation": "review", "plugin_id": plugin, "hand_id": hand,
                    "package_record_id": package, "expected_state_revision": expected,
                    "reason": reason, "containment_profile_revision": profile,
                    "candidate_cutover_id": slot.cutover_id, "result": result,
                }, expected_revision=0)
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as exc:
            raise PluginHandsArtifactConflict(str(exc)) from exc

    def materialize(
        self,
        plugin_id: str,
        *,
        hand_id: str,
        expected_review_revision: int,
        expected_materialization_revision: int,
        command_id: str,
        confirm: bool,
        candidate_cutover_id: str | None = None,
    ) -> dict[str, object]:
        plugin, hand, command = _plugin(plugin_id), _hand(hand_id), _command(command_id)
        review_revision = _revision(expected_review_revision, "expected_review_revision", allow_zero=False)
        materialization_revision = _revision(expected_materialization_revision, "expected_materialization_revision", allow_zero=True)
        if confirm is not True:
            raise PluginHandsArtifactError("Plugin Hand materialization requires explicit confirmation")
        slot = _slot(plugin, hand, candidate_cutover_id)
        operation = self._prepare_materialization(plugin, hand, review_revision, materialization_revision, command, slot)
        self._trip("after_intent")
        return self.resume(str(operation["operation_id"]))

    def resume(self, operation_id: str) -> dict[str, object]:
        operation_key = _text(operation_id, "operation_id", 128)
        operation = _required(self._records.read(_OPERATIONS, operation_key), "Hands artifact operation")
        payload = _operation_payload(operation)
        if payload["status"] == "ready":
            return _operation_result(payload, replayed=True)
        self._assert_operation_authority(payload)
        slot = _slot_from_payload(payload)
        stage = self._safe_child(self._staging, operation_key)
        target = self._artifact_path(payload["plugin_id"], payload["hand_id"], slot)
        files = _files_from_payload(payload["files"])
        self._ensure_exact_tree(stage, files, create=True)
        self._trip("after_stage")
        if target.exists():
            self._ensure_exact_tree(target, files, create=False)
            _remove_exact_tree(stage, files)
        else:
            if _is_link(target):
                raise PluginHandsArtifactConflict("managed artifact target is a link")
            os.replace(stage, target)
        self._trip("after_promote")
        return self._finalize(operation_key, payload, target)

    def reconcile(self) -> tuple[dict[str, object], ...]:
        """Resume durable materializations left incomplete by a process crash."""

        recovered: list[dict[str, object]] = []
        for record in self._records.list(_OPERATIONS):
            try:
                payload = _operation_payload(record)
                if payload["status"] == "materializing":
                    recovered.append(self.resume(record.object_id))
            except (PluginHandsArtifactError, PluginHandsArtifactConflict):
                continue
        return tuple(recovered)

    def resolve(self, plugin_id: str, *, hand_id: str, candidate_cutover_id: str | None = None) -> ManagedHandsArtifact:
        plugin, hand = _plugin(plugin_id), _hand(hand_id)
        requested_slot = _slot(plugin, hand, candidate_cutover_id)
        if requested_slot.cutover_id is None:
            pointer = self._records.read(_ACTIVE_SLOTS, _active_slot_id(plugin, hand))
            if pointer is not None:
                requested_slot = _stable_slot(plugin, hand, _active_slot_payload(pointer, plugin, hand)["slot_id"])
        artifact = _required(self._records.read(_ARTIFACTS, requested_slot.record_id), "Hands artifact")
        payload = _artifact_payload(artifact)
        if payload["status"] != "ready":
            raise PluginHandsArtifactError("Plugin Hand artifact is not ready")
        slot = _slot_from_payload(payload)
        if candidate_cutover_id is not None and requested_slot != slot:
            raise PluginHandsArtifactConflict("Plugin Hand candidate slot identity drifted")
        if candidate_cutover_id is None and slot.cutover_id is not None:
            self._assert_promoted_slot_authority(plugin, hand, artifact, slot)
            self._assert_frozen_candidate_authority(payload, slot)
        else:
            self._assert_operation_authority(payload, slot)
        root = self._artifact_path(plugin, hand, slot)
        files = _files_from_payload(payload["files"])
        self._ensure_exact_tree(root, files, create=False)
        descriptor = _descriptor(payload["descriptor"])
        return ManagedHandsArtifact(
            plugin, hand, payload["package_record_id"], payload["review_revision"], artifact.revision,
            payload["containment_profile_revision"], descriptor["runtime"], descriptor["entrypoint"],
            _frozen_schema(descriptor["input_schema"]), _frozen_schema(descriptor["output_schema"]),
            str(descriptor["effect"]), str(descriptor["operation_semantics"]),
            tuple(descriptor["requested_resources"]),
            _payload_files(files),
            _opaque_ref(plugin, hand, artifact.revision, slot), root,
        )

    def promote_candidate_to_primary(
        self,
        uow: SQLiteStructuredRecordUnitOfWork,
        plugin_id: str,
        *,
        hand_id: str,
        cutover_id: str,
    ) -> dict[str, object]:
        """CAS-switch the durable executable slot inside the caller's UoW."""
        plugin, hand = _plugin(plugin_id), _hand(hand_id)
        candidate = _slot(plugin, hand, cutover_id)
        self._candidate_package_for_slot(uow.read, plugin, hand, candidate)
        candidate_review = _required(uow.read(_REVIEWS, candidate.record_id), "candidate Hands review")
        verified_review = _review_payload(candidate_review)
        self._assert_review_authority(verified_review, candidate_review.revision, candidate, uow=uow)
        candidate_artifact = _required(uow.read(_ARTIFACTS, candidate.record_id), "candidate Hands artifact")
        verified_artifact = _artifact_payload(candidate_artifact)
        if verified_artifact["status"] != "ready" or _artifact_identity(verified_artifact) != _review_identity(verified_review, candidate_review.revision):
            raise PluginHandsArtifactConflict("Plugin Hand candidate artifact authority drifted")
        self._ensure_exact_tree(self._artifact_path(plugin, hand, candidate), _files_from_payload(verified_artifact["files"]), create=False)
        active_id = _active_slot_id(plugin, hand)
        previous = uow.read(_ACTIVE_SLOTS, active_id)
        old_slot_id = _active_slot_payload(previous, plugin, hand)["slot_id"] if previous is not None else _PRIMARY_SLOT_ID
        self._assert_uow_slot_authority(uow, plugin, hand, old_slot_id)
        promotion_id = _promotion_id(plugin, hand, candidate.cutover_id)
        existing = uow.read(_PROMOTIONS, promotion_id)
        if existing is not None:
            promotion = _promotion_payload(existing, plugin, hand, candidate.cutover_id)
            if promotion["status"] != "promoted" or promotion["candidate_review_revision"] != candidate_review.revision or promotion["candidate_artifact_revision"] != candidate_artifact.revision:
                raise PluginHandsArtifactConflict("Plugin Hand promotion identity drifted")
            if old_slot_id != promotion["new_slot_id"]:
                raise PluginHandsArtifactConflict("Plugin Hand promotion pointer drifted")
            return _promotion_result(existing, replayed=True)
        snapshot = {
            "schema_version": "1.0.0", "plugin_id": plugin, "hand_id": hand,
            "cutover_id": candidate.cutover_id, "status": "promoted",
            "old_slot_id": old_slot_id, "new_slot_id": candidate.slot_id,
            "candidate_review_revision": candidate_review.revision,
            "candidate_artifact_revision": candidate_artifact.revision,
            "promoted_at": self._now, "rolled_back_at": None,
        }
        saved = uow.put(_PROMOTIONS, promotion_id, snapshot, expected_revision=0)
        pointer = {"schema_version": "1.0.0", "plugin_id": plugin, "hand_id": hand, "slot_id": candidate.slot_id}
        uow.put(_ACTIVE_SLOTS, active_id, pointer, expected_revision=previous.revision if previous is not None else 0)
        return _promotion_result(saved, replayed=False)

    def rollback_primary_promotion(
        self,
        uow: SQLiteStructuredRecordUnitOfWork,
        plugin_id: str,
        *,
        hand_id: str,
        cutover_id: str,
    ) -> dict[str, object]:
        """Atomically restore the prior durable executable slot."""
        plugin, hand = _plugin(plugin_id), _hand(hand_id)
        candidate = _slot(plugin, hand, cutover_id)
        promotion_id = _promotion_id(plugin, hand, candidate.cutover_id)
        record = _required(uow.read(_PROMOTIONS, promotion_id), "Hands primary promotion")
        promotion = _promotion_payload(record, plugin, hand, candidate.cutover_id)
        if promotion["status"] == "rolled_back":
            return _promotion_result(record, replayed=True)
        active_id = _active_slot_id(plugin, hand)
        pointer = _required(uow.read(_ACTIVE_SLOTS, active_id), "Hands active artifact slot")
        current = _active_slot_payload(pointer, plugin, hand)
        if current["slot_id"] != promotion["new_slot_id"]:
            raise PluginHandsArtifactConflict("Plugin Hand primary rollback pointer drifted")
        self._assert_uow_slot_authority(uow, plugin, hand, promotion["old_slot_id"])
        uow.put(_ACTIVE_SLOTS, active_id, {"schema_version": "1.0.0", "plugin_id": plugin, "hand_id": hand, "slot_id": promotion["old_slot_id"]}, expected_revision=pointer.revision)
        rolled_back = dict(record.payload) | {"status": "rolled_back", "rolled_back_at": self._now}
        saved = uow.put(_PROMOTIONS, promotion_id, rolled_back, expected_revision=record.revision)
        return _promotion_result(saved, replayed=False)

    def _prepare_materialization(self, plugin: str, hand: str, review_revision: int, materialization_revision: int, command: str, slot: "_ArtifactSlot") -> Mapping[str, object]:
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    replay_payload = _command_payload(replay, "materialize", plugin, hand)
                    if (replay_payload.get("review_revision") != review_revision
                            or replay_payload.get("expected_materialization_revision") != materialization_revision
                            or replay_payload.get("confirm") is not True
                            or replay_payload.get("candidate_cutover_id") != slot.cutover_id):
                        raise PluginHandsArtifactConflict("Plugin Hand materialize command identity drifted")
                    return {"operation_id": replay_payload["operation_id"]}
                review = _required(uow.read(_REVIEWS, slot.record_id), "Hands review")
                verified = _review_payload(review)
                if review.revision != review_revision:
                    raise PluginHandsArtifactConflict("Plugin Hand review revision conflict")
                self._assert_review_authority(verified, review.revision, slot, uow=uow)
                operation_id = _operation_id(plugin, hand, review.revision, slot)
                current = uow.read(_ARTIFACTS, slot.record_id)
                if current is not None:
                    artifact = _artifact_payload(current)
                    if current.revision != materialization_revision:
                        raise PluginHandsArtifactConflict("Plugin Hand materialization revision conflict")
                    if _artifact_identity(artifact) != _review_identity(verified, review.revision):
                        raise PluginHandsArtifactConflict("Plugin Hand artifact identity drifted")
                    operation_id = artifact["operation_id"]
                else:
                    if materialization_revision != 0:
                        raise PluginHandsArtifactConflict("Plugin Hand materialization revision conflict")
                    artifact = {
                    **_review_identity(verified, review.revision), "operation_id": operation_id,
                    "status": "materializing", "materializing_at": self._now, "ready_at": None,
                    "files": verified["files"], "descriptor": verified["descriptor"],
                    }
                    if slot.cutover_id is not None:
                        artifact["candidate_cutover_id"] = slot.cutover_id
                    uow.put(_ARTIFACTS, slot.record_id, artifact, expected_revision=0)
                existing_operation = uow.read(_OPERATIONS, operation_id)
                operation_payload = {
                    **_review_identity(verified, review.revision), "operation_id": operation_id,
                    "status": "materializing", "created_at": self._now, "ready_at": None,
                    "files": verified["files"], "descriptor": verified["descriptor"],
                }
                if slot.cutover_id is not None:
                    operation_payload["candidate_cutover_id"] = slot.cutover_id
                if existing_operation is None:
                    uow.put(_OPERATIONS, operation_id, operation_payload, expected_revision=0)
                elif _operation_payload(existing_operation) != operation_payload and _operation_payload(existing_operation).get("status") != "ready":
                    raise PluginHandsArtifactConflict("Plugin Hand materialization operation drifted")
                uow.put(_COMMANDS, command, {
                    "operation": "materialize", "plugin_id": plugin, "hand_id": hand,
                    "operation_id": operation_id, "review_revision": review.revision,
                    "expected_materialization_revision": materialization_revision, "confirm": True,
                    "candidate_cutover_id": slot.cutover_id,
                }, expected_revision=0)
                uow.commit()
                return {"operation_id": operation_id}
        except SQLiteUnitOfWorkConflict as exc:
            raise PluginHandsArtifactConflict(str(exc)) from exc

    def _finalize(self, operation_id: str, expected: Mapping[str, object], target: Path) -> dict[str, object]:
        self._ensure_exact_tree(target, _files_from_payload(expected["files"]), create=False)
        try:
            with self._records.begin() as uow:
                operation = _required(uow.read(_OPERATIONS, operation_id), "Hands artifact operation")
                current = _operation_payload(operation)
                if current["status"] == "ready":
                    return _operation_result(current, replayed=True)
                if current != dict(expected):
                    raise PluginHandsArtifactConflict("Plugin Hand operation identity drifted")
                slot = _slot_from_payload(current)
                artifact = _required(uow.read(_ARTIFACTS, slot.record_id), "Hands artifact")
                materializing = _artifact_payload(artifact)
                if materializing["status"] not in {"materializing", "ready"} or _artifact_identity(materializing) != _operation_identity(current):
                    raise PluginHandsArtifactConflict("Plugin Hand artifact durable state drifted")
                ready = dict(current) | {"status": "ready", "ready_at": self._now}
                if materializing["status"] != "ready":
                    uow.put(_ARTIFACTS, artifact.object_id, dict(materializing) | {"status": "ready", "ready_at": self._now}, expected_revision=artifact.revision)
                finalized = uow.put(_OPERATIONS, operation_id, ready, expected_revision=operation.revision)
                for command in uow.list(_COMMANDS):
                    command_payload = command.payload
                    if command_payload.get("operation") == "materialize" and command_payload.get("operation_id") == operation_id:
                        uow.put(_COMMANDS, command.object_id, dict(command_payload) | {"result": _operation_result(ready, replayed=False)}, expected_revision=command.revision)
                uow.commit()
                self._trip("after_finalize")
                return _operation_result(finalized.payload, replayed=False)
        except SQLiteUnitOfWorkConflict as exc:
            raise PluginHandsArtifactConflict(str(exc)) from exc

    def _assert_operation_authority(self, payload: Mapping[str, object], slot: "_ArtifactSlot" | None = None) -> None:
        resolved_slot = slot or _slot_from_payload(payload)
        review = _required(self._records.read(_REVIEWS, resolved_slot.record_id), "Hands review")
        verified = _review_payload(review)
        if review.revision != payload["review_revision"] or _review_identity(verified, review.revision) != _operation_identity(payload):
            raise PluginHandsArtifactConflict("Plugin Hand review authority drifted")
        if payload.get("files") != verified["files"] or payload.get("descriptor") != verified["descriptor"]:
            raise PluginHandsArtifactConflict("Plugin Hand artifact authority drifted")
        self._assert_review_authority(verified, review.revision, resolved_slot)

    def _assert_review_authority(self, review: Mapping[str, object], review_revision: int, slot: "_ArtifactSlot", *, uow=None) -> None:
        reader = uow.read if uow is not None else self._records.read
        if slot.cutover_id is not None:
            package = self._candidate_package_for_slot(reader, review["plugin_id"], review["hand_id"], slot)
            if package != review["package_record_id"]:
                raise PluginHandsArtifactConflict("Plugin Hand candidate package identity drifted")
            raw = _required(reader(_RAW, package), "raw package")
            self._assert_candidate_bundle(reader, raw, package, review["plugin_id"], review["hand_id"])
            captured, descriptor = _captured_hand(raw, review["plugin_id"], review["hand_id"])
            if _files_payload(captured) != review["files"] or descriptor != review["descriptor"]:
                raise PluginHandsArtifactConflict("Plugin Hand raw bytes drifted")
            return
        state = _required(reader(_STATES, review["plugin_id"]), "package state")
        if state.revision != review["state_revision"] or state.payload.get("status") != "installed_disabled" or state.payload.get("enabled") is not False:
            raise PluginHandsArtifactConflict("Plugin package state drifted after Hand review")
        if state.payload.get("package_record_id") != review["package_record_id"]:
            raise PluginHandsArtifactConflict("Plugin Hand package identity drifted")
        raw = _required(reader(_RAW, review["package_record_id"]), "raw package")
        captured, descriptor = _captured_hand(raw, review["plugin_id"], review["hand_id"])
        if _files_payload(captured) != review["files"] or descriptor != review["descriptor"]:
            raise PluginHandsArtifactConflict("Plugin Hand raw bytes drifted")

    def _ensure_exact_tree(self, root: Path, files: tuple[tuple[str, bytes], ...], *, create: bool) -> None:
        if root.exists() and _is_link(root):
            raise PluginHandsArtifactConflict("managed Hand tree cannot be a link")
        if not root.exists():
            if not create:
                raise PluginHandsArtifactConflict("managed Hand tree is missing")
            root.mkdir(parents=True)
        if not root.is_dir():
            raise PluginHandsArtifactConflict("managed Hand tree is not a directory")
        expected = {relative: content for relative, content in files}
        observed: set[str] = set()
        expected_directories = {
            PurePosixPath(relative).parent.as_posix()
            for relative in expected
            if PurePosixPath(relative).parent.as_posix() != "."
        }
        observed_directories: set[str] = set()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            if _is_link(path):
                raise PluginHandsArtifactConflict("managed Hand tree contains a link")
            if path.is_dir():
                observed_directories.add(relative)
                continue
            if not path.is_file() or relative not in expected:
                raise PluginHandsArtifactConflict("managed Hand tree contains an unexpected entry")
            content = path.read_bytes()
            if content != expected[relative]:
                raise PluginHandsArtifactConflict("managed Hand artifact bytes drifted")
            observed.add(relative)
        if observed and (observed != set(expected) or observed_directories != expected_directories):
            raise PluginHandsArtifactConflict("managed Hand tree is incomplete")
        if not observed:
            if not create:
                raise PluginHandsArtifactConflict("managed Hand tree is empty")
            for relative, content in files:
                destination = self._safe_child(root, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise PluginHandsArtifactConflict("managed Hand stage changed while materializing")
                with destination.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            self._ensure_exact_tree(root, files, create=False)

    def _artifact_path(self, plugin: str, hand: str, slot: "_ArtifactSlot") -> Path:
        return self._safe_child(self._artifacts, slot.path_name)

    def _package_for_slot(self, uow, plugin: str, hand: str, state: SQLiteStructuredRecord, slot: "_ArtifactSlot") -> str:
        if slot.cutover_id is None:
            return _text(state.payload.get("package_record_id"), "package_record_id", 160)
        return self._candidate_package_for_slot(uow.read, plugin, hand, slot)

    def _candidate_package_for_slot(self, read, plugin: str, hand: str, slot: "_ArtifactSlot") -> str:
        assert slot.cutover_id is not None
        active = _required(read(_UPGRADE_ACTIVE, _upgrade_active_id(plugin, hand)), "Hands upgrade active record")
        if set(active.payload) != {"plugin_id", "hand_id", "cutover_id", "status"} or active.payload.get("plugin_id") != plugin or active.payload.get("hand_id") != hand or active.payload.get("cutover_id") != slot.cutover_id or active.payload.get("status") != "active":
            raise PluginHandsArtifactConflict("Plugin Hand candidate cutover is not active")
        cutover = _required(read(_CUTOVERS, slot.cutover_id), "Hands upgrade cutover")
        payload = dict(cutover.payload)
        new = payload.get("new")
        if payload.get("schema_version") != "1.0.0" or payload.get("plugin_id") != plugin or payload.get("hand_id") != hand or payload.get("stage") not in {"prepared", "old_revoked", "new_switched", "new_registered", "completed", "rollback_prepared", "rollback_new_revoked", "rollback_old_switched", "rollback_old_registered"} or not isinstance(new, Mapping):
            raise PluginHandsArtifactConflict("Plugin Hand candidate cutover authority drifted")
        return _text(new.get("package_record_id"), "candidate_package_record_id", 160)

    def _assert_candidate_bundle(self, read, raw: SQLiteStructuredRecord, package: str, plugin: str, hand: str) -> None:
        report = _required(read(_REPORTS, package), "candidate compatibility report")
        manifest = _required(read(_MANIFESTS, package), "candidate normalized manifest")
        version = raw.payload.get("version")
        expected = f"{plugin}~{version}"
        if (raw.payload.get("schema_version") != "1.0.0" or raw.payload.get("plugin_id") != plugin
                or not isinstance(version, str) or package != expected
                or report.payload.get("schema_version") != "1.0.0" or report.payload.get("plugin_id") != plugin
                or report.payload.get("version") != version or report.payload.get("compatible") is not True
                or manifest.payload.get("schema_version") != "1.0.0" or manifest.payload.get("plugin_id") != plugin
                or manifest.payload.get("version") != version or manifest.payload.get("compatible") is not True):
            raise PluginHandsArtifactConflict("Plugin Hand candidate bundle drifted")
        hands = manifest.payload.get("hands_candidates")
        if not isinstance(hands, list) or not any(isinstance(item, Mapping) and item.get("id") == hand for item in hands):
            raise PluginHandsArtifactConflict("Plugin Hand candidate manifest drifted")

    def _assert_promoted_slot_authority(self, plugin: str, hand: str, artifact: SQLiteStructuredRecord, slot: _ArtifactSlot, *, uow: SQLiteStructuredRecordUnitOfWork | None = None) -> None:
        cutover = _command(artifact.payload.get("candidate_cutover_id"))
        reader = uow.read if uow is not None else self._records.read
        promotion = _required(reader(_PROMOTIONS, _promotion_id(plugin, hand, cutover)), "Hands artifact promotion")
        verified = _promotion_payload(promotion, plugin, hand, cutover)
        if (verified["status"] != "promoted" or verified["new_slot_id"] != slot.slot_id
                or verified["candidate_artifact_revision"] != artifact.revision):
            raise PluginHandsArtifactConflict("Plugin Hand active artifact promotion drifted")

    def _assert_frozen_candidate_authority(self, payload: Mapping[str, object], slot: _ArtifactSlot, *, uow: SQLiteStructuredRecordUnitOfWork | None = None) -> None:
        reader = uow.read if uow is not None else self._records.read
        plugin, hand = _plugin(payload["plugin_id"]), _hand(payload["hand_id"])
        review = _required(reader(_REVIEWS, slot.record_id), "candidate Hands review")
        verified = _review_payload(review)
        if (verified.get("candidate_cutover_id") != payload.get("candidate_cutover_id")
                or review.revision != payload["review_revision"]
                or _review_identity(verified, review.revision) != _operation_identity(payload)):
            raise PluginHandsArtifactConflict("Plugin Hand promoted review authority drifted")
        raw = _required(reader(_RAW, verified["package_record_id"]), "candidate raw package")
        self._assert_candidate_bundle(reader, raw, verified["package_record_id"], plugin, hand)
        captured, descriptor = _captured_hand(raw, plugin, hand)
        if _files_payload(captured) != verified["files"] or descriptor != verified["descriptor"]:
            raise PluginHandsArtifactConflict("Plugin Hand promoted raw bytes drifted")

    def _assert_uow_slot_authority(self, uow: SQLiteStructuredRecordUnitOfWork, plugin: str, hand: str, slot_id: str) -> None:
        slot = _stable_slot(plugin, hand, slot_id)
        review = _required(uow.read(_REVIEWS, slot.record_id), "active Hands review")
        artifact = _required(uow.read(_ARTIFACTS, slot.record_id), "active Hands artifact")
        verified_review, verified_artifact = _review_payload(review), _artifact_payload(artifact)
        if verified_artifact["status"] != "ready" or _artifact_identity(verified_artifact) != _review_identity(verified_review, review.revision):
            raise PluginHandsArtifactConflict("Plugin Hand active artifact authority drifted")
        if slot_id == _PRIMARY_SLOT_ID:
            self._assert_review_authority(verified_review, review.revision, slot, uow=uow)
        else:
            self._assert_promoted_slot_authority(plugin, hand, artifact, _slot_from_payload(verified_artifact), uow=uow)
            self._assert_frozen_candidate_authority(verified_artifact, _slot_from_payload(verified_artifact), uow=uow)
        self._ensure_exact_tree(self._artifact_path(plugin, hand, _slot_from_payload(verified_artifact)), _files_from_payload(verified_artifact["files"]), create=False)

    @staticmethod
    def _safe_child(root: Path, relative: str) -> Path:
        candidate = root / PurePosixPath(relative)
        try:
            candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise PluginHandsArtifactError("managed artifact path escaped its root") from exc
        return candidate

    def _trip(self, point: str) -> None:
        if self._fault is not None:
            self._fault(point)


def _captured_hand(raw: SQLiteStructuredRecord, plugin: str, hand: str) -> tuple[tuple[tuple[str, bytes], ...], dict[str, object]]:
    payload = raw.payload
    if payload.get("plugin_id") != plugin:
        raise PluginHandsArtifactConflict("Plugin raw package identity drifted")
    entries = payload.get("files")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise PluginHandsArtifactError("Plugin raw package files are invalid")
    prefix = f"hands/{hand}/"
    files: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise PluginHandsArtifactError("Plugin raw package file is invalid")
        path = entry.get("relative_path")
        if not isinstance(path, str) or not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        _hand_relative(relative)
        encoded, size = entry.get("content_base64"), entry.get("size_bytes")
        if not isinstance(encoded, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise PluginHandsArtifactError("Plugin Hand raw file is invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise PluginHandsArtifactError("Plugin Hand raw file is not base64") from exc
        if len(content) != size or relative in seen:
            raise PluginHandsArtifactConflict("Plugin Hand raw bytes drifted")
        seen.add(relative)
        files.append((relative, content))
    files.sort(key=lambda item: item[0])
    if not files or len(files) > _MAX_FILES or sum(len(item[1]) for item in files) > _MAX_BYTES:
        raise PluginHandsArtifactError("Plugin Hand file limits are invalid")
    if "hand.json" not in seen or any(item[0] != "hand.json" and not item[0].startswith("payload/") for item in files):
        raise PluginHandsArtifactError("Plugin Hand must contain hand.json and payload only")
    descriptor = _validate_hand_manifest(dict(files)["hand.json"], hand)
    entrypoint = str(descriptor["entrypoint"])
    if entrypoint not in seen:
        raise PluginHandsArtifactError("hand.json entrypoint is not captured")
    if not any(item[0].startswith("payload/") for item in files):
        raise PluginHandsArtifactError("Plugin Hand payload is required")
    return tuple(files), descriptor


def _validate_hand_manifest(content: bytes, hand: str) -> dict[str, object]:
    if len(content) > 64 * 1024:
        raise PluginHandsArtifactError("hand.json exceeds the size limit")
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginHandsArtifactError("hand.json must be strict UTF-8 JSON") from exc
    allowed = {
        "schema_version", "id", "runtime", "entrypoint", "input_schema",
        "output_schema", "effect", "operation_semantics", "requested_resources",
    }
    if not isinstance(value, Mapping) or set(value) != allowed or value.get("schema_version") != "1.0.0":
        raise PluginHandsArtifactError("hand.json fields are invalid")
    if value.get("id") != hand or value.get("runtime") not in {"python-stdio-v1", "powershell-stdio-v1"}:
        raise PluginHandsArtifactError("hand.json identity is invalid")
    entrypoint = value.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint or len(entrypoint) > 240 or "\x00" in entrypoint or not entrypoint.startswith("payload/"):
        raise PluginHandsArtifactError("hand.json entrypoint is invalid")
    _hand_relative(entrypoint)
    input_schema = _closed_schema(value.get("input_schema"), "input_schema")
    output_schema = _closed_schema(value.get("output_schema"), "output_schema")
    effect, semantics = value.get("effect"), value.get("operation_semantics")
    if effect not in {"read", "write"} or (effect == "read" and semantics != "read_only") or (effect == "write" and semantics != "receipt_required"):
        raise PluginHandsArtifactError("hand.json effect semantics are invalid")
    resources = value.get("requested_resources")
    if not isinstance(resources, Sequence) or isinstance(resources, (str, bytes)):
        raise PluginHandsArtifactError("hand.json requested resources are invalid")
    allowed_resources = {"workspace_input", "workspace_output"}
    if any(not isinstance(item, str) or item not in allowed_resources for item in resources) or len(set(resources)) != len(resources):
        raise PluginHandsArtifactError("hand.json requested resources are invalid")
    if effect == "read" and "workspace_output" in resources:
        raise PluginHandsArtifactError("read-only Plugin Hands cannot request workspace_output")
    return {
        "schema_version": "1.0.0", "id": hand, "runtime": value["runtime"], "entrypoint": entrypoint,
        "input_schema": input_schema, "output_schema": output_schema, "effect": effect,
        "operation_semantics": semantics, "requested_resources": list(resources),
    }


def _closed_schema(value: object, label: str) -> dict[str, object]:
    fields = {"type", "properties", "required", "additionalProperties"}
    if not isinstance(value, Mapping) or set(value) != fields or value.get("type") != "object" or value.get("additionalProperties") is not False:
        raise PluginHandsArtifactError(f"hand.json {label} is invalid")
    properties, required = value.get("properties"), value.get("required")
    if not isinstance(properties, Mapping) or len(properties) > 64 or any(not isinstance(key, str) or not key or len(key) > 128 or not isinstance(item, Mapping) for key, item in properties.items()):
        raise PluginHandsArtifactError(f"hand.json {label} properties are invalid")
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required) or len(required) != len(set(required)) or any(item not in properties for item in required):
        raise PluginHandsArtifactError(f"hand.json {label} required is invalid")
    if _has_ref(value):
        raise PluginHandsArtifactError(f"hand.json {label} cannot contain $ref")
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise PluginHandsArtifactError(f"hand.json {label} is invalid") from exc


def _frozen_schema(value: object) -> Mapping[str, object]:
    """Return an immutable copy after the durable closed-schema revalidation."""

    if not isinstance(value, Mapping):
        raise PluginHandsArtifactConflict("durable Hand schema is invalid")

    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            return MappingProxyType({str(key): freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    frozen = freeze(value)
    if not isinstance(frozen, Mapping):
        raise PluginHandsArtifactConflict("durable Hand schema is invalid")
    return frozen


def _has_ref(value: object) -> bool:
    if isinstance(value, Mapping):
        return "$ref" in value or any(_has_ref(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_ref(item) for item in value)
    return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PluginHandsArtifactError("hand.json contains duplicate fields")
        result[key] = value
    return result


def _files_payload(files: tuple[tuple[str, bytes], ...]) -> list[dict[str, object]]:
    return [{"relative_path": path, "size_bytes": len(content), "content_base64": base64.b64encode(content).decode("ascii")} for path, content in files]


def _files_from_payload(value: object) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PluginHandsArtifactConflict("durable Hand files are invalid")
    entries: list[tuple[str, bytes]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise PluginHandsArtifactConflict("durable Hand file is invalid")
        path, size, encoded = item.get("relative_path"), item.get("size_bytes"), item.get("content_base64")
        if not isinstance(path, str) or not isinstance(size, int) or isinstance(size, bool) or not isinstance(encoded, str):
            raise PluginHandsArtifactConflict("durable Hand file is invalid")
        _hand_relative(path)
        try:
            content = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise PluginHandsArtifactConflict("durable Hand file encoding drifted") from exc
        if len(content) != size:
            raise PluginHandsArtifactConflict("durable Hand file bytes drifted")
        entries.append((path, content))
    if len({path for path, _ in entries}) != len(entries) or tuple(path for path, _ in entries) != tuple(sorted(path for path, _ in entries)):
        raise PluginHandsArtifactConflict("durable Hand file ordering drifted")
    return tuple(entries)


def _payload_files(files: tuple[tuple[str, bytes], ...]) -> tuple[tuple[str, bytes], ...]:
    """Expose only reviewed runtime payload bytes, never the artifact root."""

    payload = tuple((path, content) for path, content in files if path.startswith("payload/"))
    if not payload:
        raise PluginHandsArtifactConflict("durable Hand payload is missing")
    for path, _ in payload:
        _hand_relative(path)
        if not path.startswith("payload/"):
            raise PluginHandsArtifactConflict("durable Hand payload is invalid")
    return payload


def _descriptor(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise PluginHandsArtifactConflict("durable Hand descriptor is invalid")
    # Reuse the exact closed-schema and effect/resource grammar consumed from
    # the immutable raw descriptor.  Encoding it again from canonical JSON
    # avoids trusting a mutable SQLite payload shape.
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginHandsArtifactConflict("durable Hand descriptor is invalid") from exc
    hand = value.get("id")
    if not isinstance(hand, str):
        raise PluginHandsArtifactConflict("durable Hand descriptor identity is invalid")
    try:
        return _validate_hand_manifest(encoded, _hand(hand))
    except PluginHandsArtifactError as exc:
        raise PluginHandsArtifactConflict("durable Hand descriptor is invalid") from exc


def _review_payload(record: SQLiteStructuredRecord) -> dict[str, object]:
    return _review_payload_value(dict(record.payload))


def _review_payload_value(value: dict[str, object]) -> dict[str, object]:
    required = {"schema_version", "plugin_id", "hand_id", "package_record_id", "state_revision", "files", "descriptor", "containment_profile_revision", "decision", "reviewed_by", "reviewed_at", "reason"}
    candidate = value.get("candidate_cutover_id")
    if candidate is not None:
        required.add("candidate_cutover_id")
        value["candidate_cutover_id"] = _command(candidate)
    if set(value) != required or value.get("schema_version") != "1.0.0" or value.get("decision") != "approved_disabled":
        raise PluginHandsArtifactConflict("Plugin Hand review record is invalid")
    value["plugin_id"] = _plugin(value["plugin_id"])
    value["hand_id"] = _hand(value["hand_id"])
    value["package_record_id"] = _text(value["package_record_id"], "package_record_id", 160)
    value["state_revision"] = _revision(value["state_revision"], "state_revision", allow_zero=False)
    value["files"] = _files_payload(_files_from_payload(value["files"]))
    value["descriptor"] = _descriptor(value["descriptor"])
    if value["descriptor"]["id"] != value["hand_id"]:
        raise PluginHandsArtifactConflict("Plugin Hand descriptor identity drifted")
    value["containment_profile_revision"] = _profile(value["containment_profile_revision"])
    return value


def _artifact_payload(record: SQLiteStructuredRecord) -> dict[str, object]:
    value = dict(record.payload)
    if value.get("status") not in {"materializing", "ready"}:
        raise PluginHandsArtifactConflict("Plugin Hand artifact status is invalid")
    return _operation_payload_value(value)


def _active_slot_payload(record: SQLiteStructuredRecord, plugin: str, hand: str) -> dict[str, str]:
    value = dict(record.payload)
    if set(value) != {"schema_version", "plugin_id", "hand_id", "slot_id"} or value.get("schema_version") != "1.0.0" or value.get("plugin_id") != plugin or value.get("hand_id") != hand:
        raise PluginHandsArtifactConflict("Plugin Hand active artifact slot is invalid")
    slot = _stable_slot(plugin, hand, value.get("slot_id"))
    return {"slot_id": slot.slot_id}


def _promotion_payload(record: SQLiteStructuredRecord, plugin: str, hand: str, cutover_id: str | None) -> dict[str, object]:
    value = dict(record.payload)
    required = {"schema_version", "plugin_id", "hand_id", "cutover_id", "status", "old_slot_id", "new_slot_id", "candidate_review_revision", "candidate_artifact_revision", "promoted_at", "rolled_back_at"}
    if set(value) != required or value.get("schema_version") != "1.0.0" or value.get("plugin_id") != plugin or value.get("hand_id") != hand or value.get("cutover_id") != cutover_id or value.get("status") not in {"promoted", "rolled_back"}:
        raise PluginHandsArtifactConflict("Plugin Hand promotion record is invalid")
    old_slot = _stable_slot(plugin, hand, value.get("old_slot_id"))
    new_slot = _stable_slot(plugin, hand, value.get("new_slot_id"))
    if new_slot.slot_id == _PRIMARY_SLOT_ID or (value.get("status") == "promoted" and value.get("rolled_back_at") is not None) or (value.get("status") == "rolled_back" and not isinstance(value.get("rolled_back_at"), str)):
        raise PluginHandsArtifactConflict("Plugin Hand promotion record is invalid")
    value["old_slot_id"], value["new_slot_id"] = old_slot.slot_id, new_slot.slot_id
    value["candidate_review_revision"] = _revision(value.get("candidate_review_revision"), "candidate_review_revision", allow_zero=False)
    value["candidate_artifact_revision"] = _revision(value.get("candidate_artifact_revision"), "candidate_artifact_revision", allow_zero=False)
    if not isinstance(value.get("promoted_at"), str):
        raise PluginHandsArtifactConflict("Plugin Hand promotion record is invalid")
    return value


def _promotion_result(record: SQLiteStructuredRecord, *, replayed: bool) -> dict[str, object]:
    value = _promotion_payload(record, _plugin(record.payload.get("plugin_id")), _hand(record.payload.get("hand_id")), _command(record.payload.get("cutover_id")))
    return {"cutover_id": value["cutover_id"], "status": value["status"], "old_slot_id": value["old_slot_id"], "new_slot_id": value["new_slot_id"], "promotion_revision": record.revision, "replayed": replayed}


def _operation_payload(record: SQLiteStructuredRecord) -> dict[str, object]:
    return _operation_payload_value(dict(record.payload))


def _operation_payload_value(value: dict[str, object]) -> dict[str, object]:
    required = {"plugin_id", "hand_id", "package_record_id", "review_revision", "operation_id", "status", "files", "descriptor", "containment_profile_revision"}
    if not required <= set(value) or value.get("status") not in {"materializing", "ready"}:
        raise PluginHandsArtifactConflict("Plugin Hand operation record is invalid")
    value["plugin_id"] = _plugin(value["plugin_id"])
    value["hand_id"] = _hand(value["hand_id"])
    value["package_record_id"] = _text(value["package_record_id"], "package_record_id", 160)
    value["review_revision"] = _revision(value["review_revision"], "review_revision", allow_zero=False)
    value["operation_id"] = _text(value["operation_id"], "operation_id", 128)
    value["files"] = _files_payload(_files_from_payload(value["files"]))
    value["descriptor"] = _descriptor(value["descriptor"])
    if value["descriptor"]["id"] != value["hand_id"]:
        raise PluginHandsArtifactConflict("Plugin Hand descriptor identity drifted")
    value["containment_profile_revision"] = _profile(value["containment_profile_revision"])
    candidate = value.get("candidate_cutover_id")
    if candidate is not None:
        value["candidate_cutover_id"] = _command(candidate)
    allowed = required | {"schema_version", "materializing_at", "ready_at", "created_at", "candidate_cutover_id"}
    if not set(value) <= allowed:
        raise PluginHandsArtifactConflict("Plugin Hand operation record is invalid")
    return value


def _review_identity(review: Mapping[str, object], revision: int) -> dict[str, object]:
    identity = {"plugin_id": review["plugin_id"], "hand_id": review["hand_id"], "package_record_id": review["package_record_id"], "review_revision": revision, "containment_profile_revision": review["containment_profile_revision"]}
    if review.get("candidate_cutover_id") is not None:
        identity["candidate_cutover_id"] = review["candidate_cutover_id"]
    return identity


def _operation_identity(value: Mapping[str, object]) -> dict[str, object]:
    keys = ("plugin_id", "hand_id", "package_record_id", "review_revision", "containment_profile_revision")
    identity = {key: value[key] for key in keys}
    if value.get("candidate_cutover_id") is not None:
        identity["candidate_cutover_id"] = value["candidate_cutover_id"]
    return identity


def _artifact_identity(value: Mapping[str, object]) -> dict[str, object]:
    return _operation_identity(value)


def _operation_result(value: Mapping[str, object], *, replayed: bool) -> dict[str, object]:
    result = {"plugin_id": value["plugin_id"], "hand_id": value["hand_id"], "package_record_id": value["package_record_id"], "review_revision": value["review_revision"], "operation_id": value["operation_id"], "status": value["status"], "replayed": replayed}
    if value.get("candidate_cutover_id") is not None:
        result["candidate_cutover_id"] = value["candidate_cutover_id"]
    return result


def _review_result(record: SQLiteStructuredRecord, *, replayed: bool) -> dict[str, object]:
    payload = _review_payload(record)
    return {"review": payload, "review_revision": record.revision, "replayed": replayed}


def _command_payload(record: SQLiteStructuredRecord, operation: str, plugin: str, hand: str) -> dict[str, object]:
    value = dict(record.payload)
    if value.get("operation") != operation or value.get("plugin_id") != plugin or value.get("hand_id") != hand:
        raise PluginHandsArtifactConflict("Plugin Hand command identity drifted")
    return value


def _replay(record: SQLiteStructuredRecord, operation: str, plugin: str, hand: str) -> dict[str, object]:
    value = _command_payload(record, operation, plugin, hand)
    result = value.get("result")
    if not isinstance(result, Mapping):
        raise PluginHandsArtifactConflict("Plugin Hand command is not complete")
    return dict(result) | {"replayed": True}


def _installed_state(record: SQLiteStructuredRecord | None, expected: int) -> SQLiteStructuredRecord:
    value = _required(record, "package state")
    if value.revision != expected:
        raise PluginHandsArtifactConflict("Plugin package state revision conflict")
    if value.payload.get("status") != "installed_disabled" or value.payload.get("enabled") is not False:
        raise PluginHandsArtifactError("Plugin package must remain installed disabled")
    return value


def _required(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise PluginHandsArtifactError(f"Plugin {label} is missing")
    return record


def _remove_exact_tree(root: Path, files: tuple[tuple[str, bytes], ...]) -> None:
    if not root.exists():
        return
    if _is_link(root):
        raise PluginHandsArtifactConflict("staging tree unexpectedly changed")
    expected = {relative for relative, _ in files}
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not _is_link(path)
    }
    expected_directories = {
        PurePosixPath(relative).parent.as_posix()
        for relative in expected
        if PurePosixPath(relative).parent.as_posix() != "."
    }
    actual_directories = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and not _is_link(path)
    }
    if actual != expected or actual_directories != expected_directories or any(_is_link(path) for path in root.rglob("*")):
        raise PluginHandsArtifactConflict("staging tree unexpectedly changed")
    for relative in sorted(expected, reverse=True):
        (root / PurePosixPath(relative)).unlink()
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
        directory.rmdir()
    root.rmdir()


def _is_link(path: Path) -> bool:
    try:
        details = path.lstat()
        attributes = getattr(details, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return stat.S_ISLNK(details.st_mode) or bool(reparse and attributes & reparse)
    except FileNotFoundError:
        return False


def _assert_existing_ancestors_not_links(path: Path) -> None:
    current = path
    while True:
        if current.exists() and _is_link(current):
            raise PluginHandsArtifactError("managed artifact ancestor cannot be a link or reparse point")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _hand_relative(value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or len(path.parts) > 32 or len(value) > 240:
        raise PluginHandsArtifactError("Plugin Hand relative path is invalid")


def _plugin(value: object) -> str:
    return _text(value, "plugin_id", 64)


def _hand(value: object) -> str:
    if not isinstance(value, str) or not _HAND_ID.fullmatch(value):
        raise PluginHandsArtifactError("hand_id is invalid")
    return value


def _profile(value: object) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise PluginHandsArtifactError("containment_profile_revision is invalid")
    return value


def _command(value: object) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise PluginHandsArtifactError("command_id is invalid")
    return value


def _review_id(plugin: str, hand: str) -> str:
    return f"{plugin}--{hand}"


def _slot(plugin: str, hand: str, candidate_cutover_id: str | None) -> _ArtifactSlot:
    if candidate_cutover_id is None:
        key = _review_id(plugin, hand)
        return _ArtifactSlot(None, _PRIMARY_SLOT_ID, key, key)
    cutover = _command(candidate_cutover_id)
    token = uuid5(NAMESPACE_URL, f"plugin-hands-artifact-slot:{plugin}:{hand}:{cutover}").hex
    slot_id = f"candidate-{token}"
    return _ArtifactSlot(cutover, slot_id, f"{_review_id(plugin, hand)}--{slot_id}", slot_id)


def _stable_slot(plugin: str, hand: str, slot_id: object) -> _ArtifactSlot:
    if slot_id == _PRIMARY_SLOT_ID:
        return _slot(plugin, hand, None)
    if not isinstance(slot_id, str) or not re.fullmatch(r"candidate-[0-9a-f]{32}", slot_id):
        raise PluginHandsArtifactConflict("Plugin Hand active artifact slot is invalid")
    return _ArtifactSlot(None, slot_id, f"{_review_id(plugin, hand)}--{slot_id}", slot_id)


def _slot_from_payload(payload: Mapping[str, object]) -> _ArtifactSlot:
    return _slot(_plugin(payload["plugin_id"]), _hand(payload["hand_id"]), payload.get("candidate_cutover_id"))


def _operation_id(plugin: str, hand: str, review_revision: int, slot: _ArtifactSlot) -> str:
    if slot.cutover_id is None:
        return f"hands-artifact-{plugin}-{hand}-r{review_revision}"
    return "hands-artifact-" + uuid5(
        NAMESPACE_URL, f"plugin-hands-artifact-operation:{plugin}:{hand}:{slot.cutover_id}:{review_revision}"
    ).hex


def _upgrade_active_id(plugin: str, hand: str) -> str:
    return "hands-upgrade-" + uuid5(NAMESPACE_URL, f"{plugin}:{hand}").hex


def _active_slot_id(plugin: str, hand: str) -> str:
    return "hands-active-artifact-" + uuid5(NAMESPACE_URL, f"{plugin}:{hand}").hex


def _promotion_id(plugin: str, hand: str, cutover_id: str | None) -> str:
    return "hands-artifact-promotion-" + uuid5(
        NAMESPACE_URL, f"{plugin}:{hand}:{_command(cutover_id)}"
    ).hex


def _opaque_ref(plugin: str, hand: str, revision: int, slot: _ArtifactSlot) -> str:
    if slot.cutover_id is None:
        return f"plugin-hands-artifact:{plugin}:{hand}:r{revision}"
    return f"plugin-hands-artifact:{plugin}:{hand}:candidate-{slot.cutover_id}:r{revision}"


def _revision(value: object, label: str, *, allow_zero: bool) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise PluginHandsArtifactError(f"{label} is invalid")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PluginHandsArtifactError(f"{label} is invalid")
    return value.strip()
