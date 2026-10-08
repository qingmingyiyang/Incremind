"""Durable, disabled-by-default authority for reviewed Plugin Hands.

This module only records an activation decision over an already managed
artifact.  It deliberately has no Host, runtime, API, registry, Session,
Boundary, Secret Store, or network dependency; activation never executes
plugin bytes.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from threading import RLock

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
)

from .hands_artifact import (
    PluginHandsArtifactConflict,
    PluginHandsArtifactError,
    PluginHandsArtifactService,
)
from .hands_upgrade import PluginHandsUpgradeSnapshot


_STATES = "plugin_package_states"
_RAW = "plugin_raw_packages"
_REVIEWS = "plugin_hands_reviews"
_ARTIFACTS = "plugin_hands_artifacts"
_ACTIVATIONS = "plugin_hands_activations"
_COMMANDS = "plugin_hands_activation_commands"
_UPGRADE_RECEIPTS = "plugin_hands_activation_upgrade_receipts"
_CUTOVERS = "plugin_hands_upgrade_cutovers"
_UPGRADE_ACTIVE = "plugin_hands_upgrade_active"
_LOCK = RLock()


class PluginHandsActivationError(PluginHandsArtifactError):
    """Raised when a Hands activation command is unsafe or incomplete."""


class PluginHandsActivationConflict(PluginHandsArtifactConflict):
    """Raised when an activation identity or its revision changed."""


@dataclass(frozen=True, slots=True)
class PluginHandsActivation:
    """A safe, opaque view of one currently active Hand contract."""

    plugin_id: str
    hand_id: str
    package_record_id: str
    review_revision: int
    materialization_revision: int
    containment_profile_revision: str
    artifact_opaque_ref: str
    runtime: str
    entrypoint: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    effect: str
    operation_semantics: str
    requested_resources: tuple[str, ...]
    activation_revision: int


class PluginHandsActivationAuthority:
    """CAS/replay-safe activation state over ``PluginHandsArtifactService``."""

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        *,
        artifacts: PluginHandsArtifactService,
        now: str,
    ) -> None:
        self._records = records
        self._artifacts = artifacts
        self._now = _text(now, "now", 96)

    def activate(
        self,
        plugin_id: str,
        *,
        hand_id: str,
        expected_review_revision: int,
        expected_materialization_revision: int,
        expected_activation_revision: int,
        containment_profile_revision: str,
        command_id: str,
        confirm: bool,
    ) -> dict[str, object]:
        plugin, hand, command = _plugin(plugin_id), _hand(hand_id), _command(command_id)
        review_revision = _revision(expected_review_revision, "expected_review_revision", allow_zero=False)
        artifact_revision = _revision(expected_materialization_revision, "expected_materialization_revision", allow_zero=False)
        activation_revision = _revision(expected_activation_revision, "expected_activation_revision")
        profile = _profile(containment_profile_revision)
        if confirm is not True:
            raise PluginHandsActivationError("Plugin Hand activation requires explicit confirmation")
        with _LOCK:
            return self._activate(
                plugin, hand, review_revision, artifact_revision, activation_revision,
                profile, command,
            )

    def disable(
        self,
        plugin_id: str,
        *,
        hand_id: str,
        expected_activation_revision: int,
        command_id: str,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        plugin, hand, command = _plugin(plugin_id), _hand(hand_id), _command(command_id)
        expected = _revision(expected_activation_revision, "expected_activation_revision", allow_zero=False)
        if confirm is not True:
            raise PluginHandsActivationError("Plugin Hand disable requires explicit confirmation")
        disable_reason = _text(reason, "reason", 500)
        with _LOCK:
            try:
                with self._records.begin() as uow:
                    replay = uow.read(_COMMANDS, command)
                    current = _required(uow.read(_ACTIVATIONS, _activation_id(plugin, hand)), "Hands activation")
                    if replay is not None:
                        return _replay(replay, "disable", plugin, hand, {
                            "expected_activation_revision": expected, "reason": disable_reason,
                        })
                    payload = _activation_payload(current, plugin, hand)
                    if current.revision != expected:
                        raise PluginHandsActivationConflict("Plugin Hand activation revision conflict")
                    disabled = dict(payload) | {
                        "status": "disabled", "disabled_at": self._now, "disable_reason": disable_reason,
                    }
                    saved = uow.put(_ACTIVATIONS, current.object_id, disabled, expected_revision=current.revision)
                    result = _result(saved, replayed=False)
                    uow.put(_COMMANDS, command, {
                        "operation": "disable", "plugin_id": plugin, "hand_id": hand,
                        "expected_activation_revision": expected, "reason": disable_reason, "result": result,
                    }, expected_revision=0)
                    uow.commit()
                    return result
            except SQLiteUnitOfWorkConflict as exc:
                raise PluginHandsActivationConflict(str(exc)) from exc

    def switch_upgrade(
        self,
        plugin_id: str,
        *,
        hand_id: str,
        cutover_id: str,
        phase: str,
        old: PluginHandsUpgradeSnapshot,
        new: PluginHandsUpgradeSnapshot,
    ) -> dict[str, object]:
        """Apply one durable upgrade switch inside the activation transaction.

        This is intentionally separate from :meth:`activate`: ordinary
        activation may never replace a package.  The Upgrade authority calls
        this only for its two switch phases and supplies its frozen snapshots.
        """
        plugin, hand = _plugin(plugin_id), _hand(hand_id)
        cutover, phase_key = _command(cutover_id), _upgrade_phase(phase)
        if not isinstance(old, PluginHandsUpgradeSnapshot) or not isinstance(new, PluginHandsUpgradeSnapshot):
            raise PluginHandsActivationError("Plugin Hand upgrade snapshots are invalid")
        if old == new:
            raise PluginHandsActivationConflict("Plugin Hand upgrade snapshots must differ")
        # A completed/finalized cutover intentionally makes its candidate slot
        # unreadable.  Check the durable phase receipt first so a crash retry
        # remains replay-safe after the state machine has advanced.
        receipt_id = _upgrade_receipt_id(cutover, phase_key)
        with self._records.begin() as uow:
            receipt = uow.read(_UPGRADE_RECEIPTS, receipt_id)
            if receipt is not None:
                return _upgrade_replay(receipt, plugin, hand, cutover, phase_key, old, new)
            uow.rollback()
        candidate = self._candidate_contract(plugin, hand, cutover, new) if phase_key == "old_revoked" else None
        with _LOCK:
            try:
                with self._records.begin() as uow:
                    receipt = uow.read(_UPGRADE_RECEIPTS, receipt_id)
                    if receipt is not None:
                        return _upgrade_replay(receipt, plugin, hand, cutover, phase_key, old, new)
                    cutover_record = _required(uow.read(_CUTOVERS, cutover), "Hands upgrade cutover")
                    _validate_cutover(cutover_record, plugin, hand, cutover, phase_key, old, new)
                    active_owner = _required(uow.read(_UPGRADE_ACTIVE, _upgrade_active_id(plugin, hand)), "Hands upgrade active owner")
                    if (set(active_owner.payload) != {"plugin_id", "hand_id", "cutover_id", "status"}
                            or active_owner.payload.get("plugin_id") != plugin
                            or active_owner.payload.get("hand_id") != hand
                            or active_owner.payload.get("cutover_id") != cutover
                            or active_owner.payload.get("status") != "active"):
                        raise PluginHandsActivationConflict("Plugin Hand upgrade active owner drifted")
                    current = _required(uow.read(_ACTIVATIONS, _activation_id(plugin, hand)), "Hands activation")
                    current_payload = _activation_payload(current, plugin, hand)
                    target, expected_current = (new, old) if phase_key == "old_revoked" else (old, new)
                    _matches_snapshot(current_payload, current.revision, expected_current)
                    if phase_key == "old_revoked":
                        # This participant proves candidate review/artifact/tree
                        # again through the same SQLite UoW before changing the
                        # stable artifact pointer.
                        self._artifacts.promote_candidate_to_primary(
                            uow, plugin, hand_id=hand, cutover_id=cutover,
                        )
                        assert candidate is not None
                        payload = _activation_payload_for_contract(plugin, hand, candidate, self._now)
                    else:
                        forward = _required(uow.read(_UPGRADE_RECEIPTS, _upgrade_receipt_id(cutover, "old_revoked")), "Hands forward switch receipt")
                        prior = _upgrade_prior_activation(forward, plugin, hand, cutover, old, new)
                        self._artifacts.rollback_primary_promotion(
                            uow, plugin, hand_id=hand, cutover_id=cutover,
                        )
                        payload = dict(prior) | {
                            "status": "active", "activated_at": self._now,
                            "disabled_at": None, "disable_reason": None,
                        }
                    saved = uow.put(_ACTIVATIONS, current.object_id, payload, expected_revision=current.revision)
                    _matches_snapshot(
                        saved.payload, saved.revision, target,
                        activation_revision=phase_key == "old_revoked",
                    )
                    result = _result(saved, replayed=False)
                    uow.put(_UPGRADE_RECEIPTS, receipt_id, {
                        "schema_version": "1.0.0", "plugin_id": plugin, "hand_id": hand,
                        "cutover_id": cutover, "phase": phase_key,
                        "old": asdict(old), "new": asdict(new),
                        "prior_activation": dict(current_payload), "result": result,
                    }, expected_revision=0)
                    uow.commit()
                    return result
            except SQLiteUnitOfWorkConflict as exc:
                raise PluginHandsActivationConflict(str(exc)) from exc
            except (PluginHandsArtifactError, PluginHandsArtifactConflict) as exc:
                raise PluginHandsActivationConflict(str(exc)) from exc

    def resolve_active(self, plugin_id: str, *, hand_id: str) -> PluginHandsActivation | None:
        """Return a revalidated active contract, or ``None`` while disabled.

        It exposes no file path, argv, environment, containment details, or
        execution method.  A later outer bridge may consume this immutable
        decision after composing the other authorities.
        """
        plugin, hand = _plugin(plugin_id), _hand(hand_id)
        record = self._records.read(_ACTIVATIONS, _activation_id(plugin, hand))
        if record is None:
            return None
        payload = _activation_payload(record, plugin, hand)
        if payload["status"] != "active":
            return None
        try:
            contract = self._revalidate(plugin, hand, payload)
        except (PluginHandsArtifactError, PluginHandsArtifactConflict) as exc:
            raise PluginHandsActivationConflict(str(exc)) from exc
        return PluginHandsActivation(
            plugin, hand, contract["package_record_id"], contract["review_revision"],
            contract["materialization_revision"], contract["containment_profile_revision"],
            contract["artifact_opaque_ref"], contract["runtime"], contract["entrypoint"],
            dict(contract["input_schema"]), dict(contract["output_schema"]), contract["effect"],
            contract["operation_semantics"], tuple(contract["requested_resources"]), record.revision,
        )

    def all_active(self) -> tuple[PluginHandsActivation, ...]:
        """Return the revalidated durable active snapshot for Registry projection."""

        active: list[PluginHandsActivation] = []
        for record in self._records.list(_ACTIVATIONS):
            plugin = record.payload.get("plugin_id")
            hand = record.payload.get("hand_id")
            if not isinstance(plugin, str) or not isinstance(hand, str):
                continue
            try:
                resolved = self.resolve_active(plugin, hand_id=hand)
            except (PluginHandsArtifactError, PluginHandsArtifactConflict):
                continue
            if resolved is not None:
                active.append(resolved)
        return tuple(sorted(active, key=lambda item: (item.plugin_id, item.hand_id)))

    def _activate(
        self, plugin: str, hand: str, review_revision: int, artifact_revision: int,
        activation_revision: int, profile: str, command: str,
    ) -> dict[str, object]:
        requested = {
            "expected_review_revision": review_revision,
            "expected_materialization_revision": artifact_revision,
            "expected_activation_revision": activation_revision,
            "containment_profile_revision": profile,
        }
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    return _replay(replay, "activate", plugin, hand, requested)
            contract = self._revalidate(plugin, hand, requested)
            try:
                with self._records.begin() as uow:
                    replay = uow.read(_COMMANDS, command)
                    if replay is not None:
                        return _replay(replay, "activate", plugin, hand, requested)
                    # The earlier resolve verified the managed tree.  Re-read
                    # every durable identity through this committing snapshot;
                    # calling a second SQLite connection inside BEGIN IMMEDIATE
                    # would turn the authority check itself into a lock hazard.
                    self._revalidate(plugin, hand, requested, uow=uow)
                    current = uow.read(_ACTIVATIONS, _activation_id(plugin, hand))
                    if (current.revision if current is not None else 0) != activation_revision:
                        raise PluginHandsActivationConflict("Plugin Hand activation revision conflict")
                    payload = {
                        "schema_version": "1.0.0", "plugin_id": plugin, "hand_id": hand,
                        **contract, "status": "active", "activated_at": self._now,
                        "disabled_at": None, "disable_reason": None,
                    }
                    if current is None:
                        saved = uow.put(_ACTIVATIONS, _activation_id(plugin, hand), payload, expected_revision=0)
                    elif current.payload.get("package_record_id") != contract["package_record_id"]:
                        raise PluginHandsActivationConflict("Plugin Hand upgrade requires a future Gate")
                    elif dict(current.payload) == payload:
                        saved = current
                    else:
                        saved = uow.put(_ACTIVATIONS, current.object_id, payload, expected_revision=current.revision)
                    result = _result(saved, replayed=False)
                    uow.put(_COMMANDS, command, {
                        "operation": "activate", "plugin_id": plugin, "hand_id": hand,
                        **requested, "result": result,
                    }, expected_revision=0)
                    uow.commit()
                    return result
            except SQLiteUnitOfWorkConflict as exc:
                raise PluginHandsActivationConflict(str(exc)) from exc
        except (PluginHandsArtifactError, PluginHandsArtifactConflict) as exc:
            if isinstance(exc, PluginHandsActivationError | PluginHandsActivationConflict):
                raise
            raise PluginHandsActivationConflict(str(exc)) from exc

    def _revalidate(self, plugin: str, hand: str, expected: Mapping[str, object], *, uow=None) -> dict[str, object]:
        # This is the authoritative byte/tree check.  It rejects package state,
        # raw, review, descriptor, profile, or managed artifact drift.
        resolved = self._artifacts.resolve(plugin, hand_id=hand) if uow is None else None
        reader = uow.read if uow is not None else self._records.read
        review = _required(reader(_REVIEWS, _activation_id(plugin, hand)), "Hands review")
        durable = _required(reader(_ARTIFACTS, _activation_id(plugin, hand)), "Hands artifact")
        artifact_payload = _artifact_payload(durable, plugin, hand)
        package_record_id = _text(artifact_payload.get("package_record_id"), "package_record_id", 160)
        if resolved is not None and resolved.package_record_id != package_record_id:
            # A stable artifact pointer may deliberately select a promoted
            # candidate while the package intake pointer remains on the old
            # installed-disabled package.  Artifact authority has already
            # validated the promotion receipt, frozen candidate bundle and
            # exact tree; activation now binds its immutable contract to it.
            return _resolved_contract(resolved, expected)
        state = _required(reader(_STATES, plugin), "package state")
        raw = _required(reader(_RAW, package_record_id), "raw package")
        review_payload = _review_payload(review, plugin, hand)
        if state.payload.get("status") != "installed_disabled" or state.payload.get("enabled") is not False:
            raise PluginHandsActivationConflict("Plugin package state drifted")
        if state.payload.get("package_record_id") != package_record_id or raw.payload.get("plugin_id") != plugin:
            raise PluginHandsActivationConflict("Plugin Hand package identity drifted")
        expected_review = _expected_revision(expected, "expected_review_revision", "review_revision")
        expected_artifact = _expected_revision(expected, "expected_materialization_revision", "materialization_revision")
        expected_profile = _expected_text(expected, "containment_profile_revision")
        if review.revision != expected_review or review_payload["package_record_id"] != package_record_id:
            raise PluginHandsActivationConflict("Plugin Hand review authority drifted")
        if durable.revision != expected_artifact or artifact_payload["status"] != "ready":
            raise PluginHandsActivationConflict("Plugin Hand artifact authority drifted")
        descriptor = _descriptor(review_payload["descriptor"], hand)
        if descriptor != _descriptor(artifact_payload["descriptor"], hand):
            raise PluginHandsActivationConflict("Plugin Hand descriptor drifted")
        if review_payload["containment_profile_revision"] != expected_profile or artifact_payload["containment_profile_revision"] != expected_profile:
            raise PluginHandsActivationConflict("Plugin Hand containment profile drifted")
        opaque_ref = f"plugin-hands-artifact:{plugin}:{hand}:r{durable.revision}"
        if resolved is not None and (
            resolved.package_record_id != package_record_id
            or resolved.review_revision != review.revision
            or resolved.materialization_revision != durable.revision
            or resolved.containment_profile_revision != artifact_payload["containment_profile_revision"]
            or resolved.opaque_ref != opaque_ref
        ):
            raise PluginHandsActivationConflict("Plugin Hand artifact resolution drifted")
        return {
            "package_record_id": package_record_id,
            "review_revision": review.revision,
            "materialization_revision": durable.revision,
            "containment_profile_revision": artifact_payload["containment_profile_revision"],
            "artifact_opaque_ref": opaque_ref,
            "runtime": descriptor["runtime"], "entrypoint": descriptor["entrypoint"],
            "input_schema": descriptor["input_schema"], "output_schema": descriptor["output_schema"],
            "effect": descriptor["effect"], "operation_semantics": descriptor["operation_semantics"],
            "requested_resources": descriptor["requested_resources"],
        }

    def _candidate_contract(
        self, plugin: str, hand: str, cutover_id: str, snapshot: PluginHandsUpgradeSnapshot,
    ) -> dict[str, object]:
        """Resolve the frozen candidate before the committing UoW.

        The transaction participant subsequently repeats the durable candidate
        checks.  Splitting file verification from the IMMEDIATE transaction
        avoids opening a second SQLite connection while it owns the writer.
        """
        resolved = self._artifacts.resolve(plugin, hand_id=hand, candidate_cutover_id=cutover_id)
        contract = {
            "package_record_id": resolved.package_record_id,
            "review_revision": resolved.review_revision,
            "materialization_revision": resolved.materialization_revision,
            "containment_profile_revision": resolved.containment_profile_revision,
            "artifact_opaque_ref": resolved.opaque_ref,
            "runtime": resolved.runtime, "entrypoint": resolved.entrypoint,
            "input_schema": _mutable_json(resolved.input_schema), "output_schema": _mutable_json(resolved.output_schema),
            "effect": resolved.effect, "operation_semantics": resolved.operation_semantics,
            "requested_resources": list(resolved.requested_resources),
        }
        _matches_snapshot(contract, snapshot.activation_revision, snapshot, activation_revision=False)
        return contract


def _activation_payload(record: SQLiteStructuredRecord, plugin: str, hand: str) -> dict[str, object]:
    value = dict(record.payload)
    required = {
        "schema_version", "plugin_id", "hand_id", "package_record_id", "review_revision",
        "materialization_revision", "containment_profile_revision", "artifact_opaque_ref", "runtime",
        "entrypoint", "input_schema", "output_schema", "effect", "operation_semantics",
        "requested_resources", "status", "activated_at", "disabled_at", "disable_reason",
    }
    if set(value) != required or value.get("schema_version") != "1.0.0" or value.get("plugin_id") != plugin or value.get("hand_id") != hand:
        raise PluginHandsActivationConflict("Plugin Hand activation identity drifted")
    if value.get("status") not in {"active", "disabled"}:
        raise PluginHandsActivationConflict("Plugin Hand activation status is invalid")
    _contract_from_payload(value, hand)
    return value


def _review_payload(record: SQLiteStructuredRecord, plugin: str, hand: str) -> dict[str, object]:
    value = dict(record.payload)
    required = {"schema_version", "plugin_id", "hand_id", "package_record_id", "state_revision", "files", "descriptor", "containment_profile_revision", "decision", "reviewed_by", "reviewed_at", "reason"}
    if set(value) != required or value.get("schema_version") != "1.0.0" or value.get("plugin_id") != plugin or value.get("hand_id") != hand or value.get("decision") != "approved_disabled":
        raise PluginHandsActivationConflict("Plugin Hand review identity drifted")
    _descriptor(value.get("descriptor"), hand)
    _profile(value.get("containment_profile_revision"))
    return value


def _artifact_payload(record: SQLiteStructuredRecord, plugin: str, hand: str) -> dict[str, object]:
    value = dict(record.payload)
    if value.get("plugin_id") != plugin or value.get("hand_id") != hand or value.get("status") != "ready":
        raise PluginHandsActivationConflict("Plugin Hand artifact identity drifted")
    _descriptor(value.get("descriptor"), hand)
    _profile(value.get("containment_profile_revision"))
    return value


def _contract_from_payload(value: Mapping[str, object], hand: str) -> None:
    _text(value.get("package_record_id"), "package_record_id", 160)
    _revision(value.get("review_revision"), "review_revision", allow_zero=False)
    _revision(value.get("materialization_revision"), "materialization_revision", allow_zero=False)
    _profile(value.get("containment_profile_revision"))
    opaque = _text(value.get("artifact_opaque_ref"), "artifact_opaque_ref", 320)
    if not opaque.startswith("plugin-hands-artifact:"):
        raise PluginHandsActivationConflict("Plugin Hand artifact reference is invalid")
    descriptor = _descriptor({
        "schema_version": "1.0.0", "id": hand, "runtime": value.get("runtime"),
        "entrypoint": value.get("entrypoint"), "input_schema": value.get("input_schema"),
        "output_schema": value.get("output_schema"), "effect": value.get("effect"),
        "operation_semantics": value.get("operation_semantics"), "requested_resources": value.get("requested_resources"),
    }, hand)
    if descriptor["id"] != hand:
        raise PluginHandsActivationConflict("Plugin Hand activation descriptor drifted")


def _descriptor(value: object, hand: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise PluginHandsActivationConflict("Plugin Hand descriptor is invalid")
    required = {"schema_version", "id", "runtime", "entrypoint", "input_schema", "output_schema", "effect", "operation_semantics", "requested_resources"}
    if set(value) != required or value.get("schema_version") != "1.0.0" or value.get("id") != hand or value.get("runtime") not in {"python-stdio-v1", "powershell-stdio-v1"}:
        raise PluginHandsActivationConflict("Plugin Hand descriptor is invalid")
    entrypoint = _text(value.get("entrypoint"), "entrypoint", 240)
    if not entrypoint.startswith("payload/"):
        raise PluginHandsActivationConflict("Plugin Hand descriptor is invalid")
    schemas = (value.get("input_schema"), value.get("output_schema"))
    for schema in schemas:
        if not isinstance(schema, Mapping) or set(schema) != {"type", "properties", "required", "additionalProperties"} or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise PluginHandsActivationConflict("Plugin Hand schema is invalid")
    effect, semantics, resources = value.get("effect"), value.get("operation_semantics"), value.get("requested_resources")
    if effect not in {"read", "write"} or (effect == "read" and semantics != "read_only") or (effect == "write" and semantics != "receipt_required"):
        raise PluginHandsActivationConflict("Plugin Hand effect contract is invalid")
    if not isinstance(resources, list) or any(item not in {"workspace_input", "workspace_output"} for item in resources) or len(resources) != len(set(resources)) or (effect == "read" and "workspace_output" in resources):
        raise PluginHandsActivationConflict("Plugin Hand resource contract is invalid")
    return {key: (dict(value[key]) if key in {"input_schema", "output_schema"} else list(value[key]) if key == "requested_resources" else value[key]) for key in required}


def _replay(record: SQLiteStructuredRecord, operation: str, plugin: str, hand: str, expected: Mapping[str, object]) -> dict[str, object]:
    value = record.payload
    if value.get("operation") != operation or value.get("plugin_id") != plugin or value.get("hand_id") != hand or any(value.get(key) != item for key, item in expected.items()) or not isinstance(value.get("result"), Mapping):
        raise PluginHandsActivationConflict("Plugin Hand activation command identity drifted")
    return dict(value["result"]) | {"replayed": True}


def _result(record: SQLiteStructuredRecord, *, replayed: bool) -> dict[str, object]:
    return {"activation": dict(record.payload), "activation_revision": record.revision, "replayed": replayed}


def _activation_payload_for_contract(plugin: str, hand: str, contract: Mapping[str, object], now: str) -> dict[str, object]:
    payload = {
        "schema_version": "1.0.0", "plugin_id": plugin, "hand_id": hand,
        **dict(contract), "status": "active", "activated_at": now,
        "disabled_at": None, "disable_reason": None,
    }
    _activation_payload(SQLiteStructuredRecord(_ACTIVATIONS, _activation_id(plugin, hand), payload, 1), plugin, hand)
    return payload


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_mutable_json(item) for item in value]
    return value


def _resolved_contract(resolved, expected: Mapping[str, object]) -> dict[str, object]:
    contract = {
        "package_record_id": resolved.package_record_id,
        "review_revision": resolved.review_revision,
        "materialization_revision": resolved.materialization_revision,
        "containment_profile_revision": resolved.containment_profile_revision,
        "artifact_opaque_ref": resolved.opaque_ref,
        "runtime": resolved.runtime, "entrypoint": resolved.entrypoint,
        "input_schema": _mutable_json(resolved.input_schema), "output_schema": _mutable_json(resolved.output_schema),
        "effect": resolved.effect, "operation_semantics": resolved.operation_semantics,
        "requested_resources": list(resolved.requested_resources),
    }
    for key, value in contract.items():
        if expected.get(key) != value:
            raise PluginHandsActivationConflict("Plugin Hand promoted artifact resolution drifted")
    return contract


def _upgrade_phase(value: object) -> str:
    if value not in {"old_revoked", "rollback_new_revoked"}:
        raise PluginHandsActivationError("Plugin Hand upgrade switch phase is invalid")
    return str(value)


def _upgrade_receipt_id(cutover: str, phase: str) -> str:
    return _command(f"activation-{cutover}-{phase}")


def _upgrade_active_id(plugin: str, hand: str) -> str:
    from uuid import NAMESPACE_URL, uuid5
    return "hands-upgrade-" + uuid5(NAMESPACE_URL, f"{plugin}:{hand}").hex


def _snapshot_payload(snapshot: PluginHandsUpgradeSnapshot) -> dict[str, object]:
    return asdict(snapshot)


def _validate_cutover(
    record: SQLiteStructuredRecord,
    plugin: str,
    hand: str,
    cutover: str,
    phase: str,
    old: PluginHandsUpgradeSnapshot,
    new: PluginHandsUpgradeSnapshot,
) -> None:
    value = dict(record.payload)
    expected_stage = "old_revoked" if phase == "old_revoked" else "rollback_new_revoked"
    expected_pointer = "old" if phase == "old_revoked" else "new"
    if (record.object_id != cutover or value.get("schema_version") != "1.0.0"
            or value.get("plugin_id") != plugin or value.get("hand_id") != hand
            or value.get("stage") != expected_stage or value.get("active_pointer") != expected_pointer
            or value.get("old") != _snapshot_payload(old) or value.get("new") != _snapshot_payload(new)):
        raise PluginHandsActivationConflict("Plugin Hand upgrade cutover authority drifted")


def _matches_snapshot(
    contract: Mapping[str, object], revision: int, snapshot: PluginHandsUpgradeSnapshot, *, activation_revision: bool = True,
) -> None:
    if (contract.get("package_record_id") != snapshot.package_record_id
            or contract.get("review_revision") != snapshot.review_revision
            or contract.get("materialization_revision") != snapshot.materialization_revision):
        raise PluginHandsActivationConflict("Plugin Hand upgrade artifact snapshot drifted")
    if activation_revision and revision != snapshot.activation_revision:
        raise PluginHandsActivationConflict("Plugin Hand upgrade activation revision drifted")


def _upgrade_receipt_payload(
    record: SQLiteStructuredRecord,
    plugin: str,
    hand: str,
    cutover: str,
    phase: str,
    old: PluginHandsUpgradeSnapshot,
    new: PluginHandsUpgradeSnapshot,
) -> dict[str, object]:
    value = dict(record.payload)
    required = {"schema_version", "plugin_id", "hand_id", "cutover_id", "phase", "old", "new", "prior_activation", "result"}
    if (set(value) != required or value.get("schema_version") != "1.0.0"
            or value.get("plugin_id") != plugin or value.get("hand_id") != hand
            or value.get("cutover_id") != cutover or value.get("phase") != phase
            or value.get("old") != _snapshot_payload(old) or value.get("new") != _snapshot_payload(new)
            or not isinstance(value.get("prior_activation"), Mapping) or not isinstance(value.get("result"), Mapping)):
        raise PluginHandsActivationConflict("Plugin Hand upgrade receipt identity drifted")
    _activation_payload(SQLiteStructuredRecord(_ACTIVATIONS, _activation_id(plugin, hand), dict(value["prior_activation"]), 1), plugin, hand)
    return value


def _upgrade_replay(
    record: SQLiteStructuredRecord, plugin: str, hand: str, cutover: str, phase: str,
    old: PluginHandsUpgradeSnapshot, new: PluginHandsUpgradeSnapshot,
) -> dict[str, object]:
    value = _upgrade_receipt_payload(record, plugin, hand, cutover, phase, old, new)
    return dict(value["result"]) | {"replayed": True}


def _upgrade_prior_activation(
    record: SQLiteStructuredRecord, plugin: str, hand: str, cutover: str,
    old: PluginHandsUpgradeSnapshot, new: PluginHandsUpgradeSnapshot,
) -> dict[str, object]:
    value = _upgrade_receipt_payload(record, plugin, hand, cutover, "old_revoked", old, new)
    prior = dict(value["prior_activation"])
    _matches_snapshot(prior, old.activation_revision, old)
    return prior


def _required(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise PluginHandsActivationError(f"Plugin {label} is missing")
    return record


def _activation_id(plugin: str, hand: str) -> str:
    return f"{plugin}--{hand}"


def _plugin(value: object) -> str:
    return _text(value, "plugin_id", 64)


def _hand(value: object) -> str:
    value = _text(value, "hand_id", 64)
    if not value[0].isalnum() or any(not (item.isalnum() or item in "._-") for item in value):
        raise PluginHandsActivationError("hand_id is invalid")
    return value


def _command(value: object) -> str:
    return _profile(value)


def _profile(value: object) -> str:
    value = _text(value, "command_id", 128)
    if len(value) < 8 or not value[0].isalnum() or any(not (item.isalnum() or item in "._~-") for item in value):
        raise PluginHandsActivationError("command_id is invalid")
    return value


def _revision(value: object, label: str, *, allow_zero: bool = True) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise PluginHandsActivationError(f"{label} is invalid")
    return value


def _expected_revision(value: Mapping[str, object], requested_key: str, frozen_key: str) -> int:
    candidate = value.get(requested_key, value.get(frozen_key))
    return _revision(candidate, requested_key, allow_zero=False)


def _expected_text(value: Mapping[str, object], key: str) -> str:
    return _profile(value.get(key))


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PluginHandsActivationError(f"{label} is invalid")
    return value.strip()
