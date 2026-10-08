from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

from core.effect_log import Effect, EffectReceipt, EffectRunner, EffectState
from core.product_core.retention import RetentionBackupEvidence

from .ports import ObjectStorePort
from .retention_effect_contract import build_retention_effect, retention_effect_receipt


_ASSET_COLLECTION = "workbench_original_assets"
_LINK_COLLECTION = "source_asset_links"
_LEGACY_OPERATION_COLLECTION = "original_asset_retention_operations"
_INTENT_COLLECTION = "original_asset_retention_intents"
_STEP_COLLECTION = "original_asset_retention_step_facts"
_RECEIPT_COLLECTION = "original_asset_retention_receipts"
_INTENT_SCHEMA = "original-asset-retention-intent-v2"
_RECEIPT_KIND = "original-asset-retention-receipt"
_RECEIPT_SCHEMA = "original-asset-retention-receipt-v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_POINT = 0x0400


class OriginalAssetRetentionError(ValueError):
    """Raised when an original asset cannot be proven safe to purge."""


@dataclass(frozen=True, slots=True)
class OriginalAssetOrphanReconciliation:
    asset_id: str
    status: str
    revision: int
    orphaned_at: str | None


@dataclass(frozen=True, slots=True)
class OriginalAssetRetentionCandidate:
    asset_id: str
    revision: int
    sha256: str
    vault_ref: str
    byte_count: int
    orphaned_at: str
    eligible_after: str
    eligible: bool
    blockers: tuple[str, ...]
    byte_action: str


@dataclass(frozen=True, slots=True)
class OriginalAssetBackupEvidence:
    status: str
    backup_id: str | None
    sha256: str | None
    byte_count: int | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class OriginalAssetRetentionPlan:
    schema_version: str
    plan_id: str
    evaluated_at: str
    observed_vault_fingerprint: str
    backup_evidence: RetentionBackupEvidence
    asset_backup_evidence: OriginalAssetBackupEvidence
    candidate: OriginalAssetRetentionCandidate


@dataclass(frozen=True, slots=True)
class OriginalAssetRetentionResult:
    operation_id: str
    asset_id: str
    status: str
    byte_action: str
    idempotent: bool


class ReconcileOriginalAssetOrphans:
    """Project authoritative Source links into an explicit orphan clock."""

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(self) -> tuple[OriginalAssetOrphanReconciliation, ...]:
        linked = _linked_asset_ids(self._store)
        results: list[OriginalAssetOrphanReconciliation] = []
        for raw in self._store.list(_ASSET_COLLECTION):
            asset_id = _required_id(raw.get("id"), "asset id")
            current_revision = self._store.revision(_ASSET_COLLECTION, asset_id)
            updated = dict(raw)
            if asset_id in linked:
                changed = (
                    updated.get("link_status") != "linked"
                    or updated.get("orphaned_at") is not None
                    or updated.get("orphan_reason") is not None
                )
                updated.update(
                    {
                        "link_status": "linked",
                        "orphaned_at": None,
                        "orphan_reason": None,
                    }
                )
                status = "linked"
                orphaned_at = None
            else:
                existing_orphaned_at = _optional_utc_iso(updated.get("orphaned_at"))
                orphaned_at = existing_orphaned_at or _iso(self._clock())
                changed = (
                    updated.get("link_status") != "orphaned"
                    or updated.get("orphan_reason") != "no_active_source_asset_links"
                    or existing_orphaned_at is None
                )
                updated.update(
                    {
                        "link_status": "orphaned",
                        "orphaned_at": orphaned_at,
                        "orphan_reason": "no_active_source_asset_links",
                    }
                )
                status = "orphaned"
            if changed:
                current_revision = self._store.write(
                    _ASSET_COLLECTION,
                    asset_id,
                    updated,
                    expected_revision=current_revision,
                )
            results.append(
                OriginalAssetOrphanReconciliation(
                    asset_id=asset_id,
                    status=status,
                    revision=current_revision,
                    orphaned_at=orphaned_at,
                )
            )
        return tuple(sorted(results, key=lambda item: item.asset_id))


class BuildOriginalAssetRetentionPlan:
    """Build one body-free retention plan from current Source-link authority."""

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        library_root: Path,
        minimum_age: timedelta = timedelta(days=7),
    ) -> None:
        if minimum_age < timedelta(days=7):
            raise OriginalAssetRetentionError(
                "original asset retention must be at least seven days"
            )
        self._store = store
        self._library_root = library_root.expanduser().resolve(strict=False)
        self._minimum_age = minimum_age

    def execute(
        self,
        *,
        asset_id: str,
        backup_evidence: RetentionBackupEvidence,
        asset_backup_evidence: OriginalAssetBackupEvidence,
        observed_vault_fingerprint: str,
        evaluated_at: datetime,
    ) -> OriginalAssetRetentionPlan:
        clean_asset_id = _required_id(asset_id, "asset id")
        now = _utc(evaluated_at)
        raw = self._store.read(_ASSET_COLLECTION, clean_asset_id)
        if not isinstance(raw, Mapping):
            raise OriginalAssetRetentionError("original asset authority is unavailable")
        revision = self._store.revision(_ASSET_COLLECTION, clean_asset_id)
        sha256, vault_ref, byte_count = _asset_identity(raw)
        blockers: set[str] = set()
        if clean_asset_id in _linked_asset_ids(self._store):
            blockers.add("source_asset_links_present")
        orphaned = _optional_utc(raw.get("orphaned_at"))
        if raw.get("link_status") != "orphaned" or orphaned is None:
            blockers.add("orphan_state_invalid")
            eligible_after = now + self._minimum_age
            orphaned_iso = ""
        else:
            eligible_after = orphaned + self._minimum_age
            orphaned_iso = _iso(orphaned)
            if now < eligible_after:
                blockers.add("retention_not_elapsed")
        if not _backup_matches(backup_evidence, observed_vault_fingerprint):
            blockers.add("backup_proof_invalid")
        if not _asset_backup_matches(
            asset_backup_evidence,
            sha256=sha256,
            byte_count=byte_count,
        ):
            blockers.add("asset_backup_proof_invalid")
        byte_action = (
            "retain_shared"
            if _has_shared_bytes(
                self._store,
                asset_id=clean_asset_id,
                sha256=sha256,
                vault_ref=vault_ref,
            )
            else "delete"
        )
        if byte_action == "delete":
            try:
                _verified_asset_path(
                    self._library_root,
                    vault_ref=vault_ref,
                    sha256=sha256,
                    byte_count=byte_count,
                )
            except OriginalAssetRetentionError as error:
                blockers.add(_path_blocker(error))
        candidate = OriginalAssetRetentionCandidate(
            asset_id=clean_asset_id,
            revision=revision,
            sha256=sha256,
            vault_ref=vault_ref,
            byte_count=byte_count,
            orphaned_at=orphaned_iso,
            eligible_after=_iso(eligible_after),
            eligible=not blockers,
            blockers=tuple(sorted(blockers)),
            byte_action=byte_action,
        )
        payload = {
            "schema_version": "1.0.0",
            "evaluated_at": _iso(now),
            "observed_vault_fingerprint": observed_vault_fingerprint,
            "backup_evidence": asdict(backup_evidence),
            "asset_backup_evidence": asdict(asset_backup_evidence),
            "candidate": asdict(candidate),
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return OriginalAssetRetentionPlan(
            schema_version="1.0.0",
            plan_id=f"original-asset-retention-{digest}",
            evaluated_at=_iso(now),
            observed_vault_fingerprint=observed_vault_fingerprint,
            backup_evidence=backup_evidence,
            asset_backup_evidence=asset_backup_evidence,
            candidate=candidate,
        )


class ExecuteOriginalAssetRetentionPurge:
    """Delete one verified orphan asset through a body-free resumable saga."""

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        library_root: Path,
        active_fingerprint: Callable[[], str],
        effect_runner: EffectRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        after_step: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._library_root = library_root.expanduser().resolve(strict=False)
        self._active_fingerprint = active_fingerprint
        self._runner = effect_runner
        self._clock = clock or (lambda: datetime.now(UTC))
        self._after_step = after_step

    def execute(
        self,
        *,
        plan: OriginalAssetRetentionPlan,
        expected_asset_revision: int,
        confirm: bool,
    ) -> OriginalAssetRetentionResult:
        if self._runner is None:
            raise OriginalAssetRetentionError(
                "original asset purge requires Core EffectRunner"
            )
        candidate = plan.candidate
        effect_intent, gate_fact = build_retention_effect(
            session_id="original-asset-retention-purge",
            root_id=candidate.asset_id,
            step_key="purge-original-asset",
            kind="original_asset_retention_purge",
            intent_ref_prefix="intent:original-asset-retention",
            gate_decision_id=f"gate:original-asset-retention-confirmed/{plan.plan_id}",
            payload={"asset_id": candidate.asset_id, "plan_id": plan.plan_id},
            policy_revision="original-asset-retention-policy-v2",
            boundary_revision=f"asset-revision-{candidate.revision}",
            workflow_revision="original-asset-retention-workflow-v2",
            intent_schema_version=_INTENT_SCHEMA,
            receipt_kind=_RECEIPT_KIND,
            receipt_schema_version=_RECEIPT_SCHEMA,
        )
        operation_id = effect_intent.operation_id
        receipt = self._store.read(_RECEIPT_COLLECTION, operation_id)
        intent = self._store.read(_INTENT_COLLECTION, operation_id)
        legacy = self._store.read(_LEGACY_OPERATION_COLLECTION, operation_id)
        self._validate(
            plan=plan,
            expected_asset_revision=expected_asset_revision,
            confirm=confirm,
            resume=intent is not None or legacy is not None,
        )
        replayed = receipt is not None
        if receipt is not None:
            _validate_terminal_receipt(receipt, operation_id, plan)
        if intent is None:
            intent = {
                "schema_version": _INTENT_SCHEMA,
                "id": operation_id,
                "kind": "original_asset_retention_purge",
                "asset_id": candidate.asset_id,
                "plan_id": plan.plan_id,
                "asset_revision": candidate.revision,
                "sha256": candidate.sha256,
                "vault_ref": candidate.vault_ref,
                "byte_count": candidate.byte_count,
                "byte_action": candidate.byte_action,
                "snapshot_id": plan.backup_evidence.snapshot_id,
                "snapshot_fingerprint": plan.backup_evidence.snapshot_fingerprint,
                "asset_backup_id": plan.asset_backup_evidence.backup_id,
                "asset_backup_sha256": plan.asset_backup_evidence.sha256,
                "asset_backup_byte_count": plan.asset_backup_evidence.byte_count,
                "created_at": _iso(self._clock()),
            }
            self._store.write(
                _INTENT_COLLECTION,
                operation_id,
                intent,
                expected_revision=0,
            )
        _validate_intent(intent, operation_id, plan)
        self._runner.log.plan_v2(
            effect_intent,
            gate_decision_id=effect_intent.gate_decision_id,
            gate_fact=gate_fact,
            now=int(self._clock().timestamp()),
        )
        outcome = self._runner.execute_planned(
            operation_id,
            lambda _effect: self._execute_claimed(operation_id, plan, intent),
            now=int(self._clock().timestamp()),
        )
        if outcome.state is not EffectState.SETTLED_OK:
            raise OriginalAssetRetentionError(
                "original asset purge Effect is not settled"
            )
        terminal = self._store.read(_RECEIPT_COLLECTION, operation_id)
        if terminal is None:
            raise OriginalAssetRetentionError(
                "original asset purge terminal Receipt is unavailable"
            )
        _validate_terminal_receipt(terminal, operation_id, plan)
        return OriginalAssetRetentionResult(
            operation_id=operation_id,
            asset_id=candidate.asset_id,
            status="completed",
            byte_action=candidate.byte_action,
            idempotent=replayed,
        )

    def handle_effect(self, effect: Effect) -> EffectReceipt:
        """Resume one v2 purge from its immutable domain Intent only."""

        intent = self._store.read(_INTENT_COLLECTION, effect.operation_id)
        if intent is None:
            raise OriginalAssetRetentionError(
                "original asset purge Intent is unavailable"
            )
        plan = _recovery_plan(intent, effect.operation_id)
        return self._execute_claimed(effect.operation_id, plan, intent)

    def verify_effect(self, operation_id: str) -> tuple[EffectState, str | None]:
        receipt = self._store.read(_RECEIPT_COLLECTION, operation_id)
        if receipt is not None:
            if receipt.get("kind") != "original_asset_retention_receipt":
                raise OriginalAssetRetentionError(
                    "original asset purge terminal Receipt drifted"
                )
            return EffectState.SETTLED_OK, f"receipt:original-asset-retention/{operation_id}"
        if self._store.read(_INTENT_COLLECTION, operation_id) is not None:
            return EffectState.PLANNED, f"intent:original-asset-retention/{operation_id}"
        return EffectState.UNKNOWN, "original_asset_retention_intent_missing"

    def _validate(
        self,
        *,
        plan: OriginalAssetRetentionPlan,
        expected_asset_revision: int,
        confirm: bool,
        resume: bool,
    ) -> None:
        candidate = plan.candidate
        if confirm is not True:
            raise OriginalAssetRetentionError(
                "original asset purge requires explicit confirmation"
            )
        if not plan.plan_id.startswith("original-asset-retention-"):
            raise OriginalAssetRetentionError("original asset purge plan is invalid")
        if candidate.eligible is not True or candidate.blockers:
            raise OriginalAssetRetentionError("original asset purge plan is not eligible")
        if (
            not isinstance(expected_asset_revision, int)
            or isinstance(expected_asset_revision, bool)
            or expected_asset_revision != candidate.revision
        ):
            raise OriginalAssetRetentionError("original asset purge revision mismatch")
        if not _backup_matches(
            plan.backup_evidence, plan.observed_vault_fingerprint
        ):
            raise OriginalAssetRetentionError("original asset purge backup proof is invalid")
        if not _asset_backup_matches(
            plan.asset_backup_evidence,
            sha256=candidate.sha256,
            byte_count=candidate.byte_count,
        ):
            raise OriginalAssetRetentionError(
                "original asset purge byte backup proof is invalid"
            )
        if resume:
            return
        if self._active_fingerprint() != plan.observed_vault_fingerprint:
            raise OriginalAssetRetentionError("original asset purge active Vault drifted")
        current = self._store.read(_ASSET_COLLECTION, candidate.asset_id)
        if not isinstance(current, Mapping):
            raise OriginalAssetRetentionError("original asset authority is unavailable")
        if self._store.revision(_ASSET_COLLECTION, candidate.asset_id) != candidate.revision:
            raise OriginalAssetRetentionError("original asset authority revision drifted")
        if _asset_identity(current) != (
            candidate.sha256,
            candidate.vault_ref,
            candidate.byte_count,
        ):
            raise OriginalAssetRetentionError("original asset identity drifted")
        if candidate.asset_id in _linked_asset_ids(self._store):
            raise OriginalAssetRetentionError("original asset gained a Source link")
        current_action = (
            "retain_shared"
            if _has_shared_bytes(
                self._store,
                asset_id=candidate.asset_id,
                sha256=candidate.sha256,
                vault_ref=candidate.vault_ref,
            )
            else "delete"
        )
        if current_action != candidate.byte_action:
            raise OriginalAssetRetentionError("original asset shared-byte state drifted")
        if current_action == "delete":
            _verified_asset_path(
                self._library_root,
                vault_ref=candidate.vault_ref,
                sha256=candidate.sha256,
                byte_count=candidate.byte_count,
            )

    def _execute_claimed(
        self,
        operation_id: str,
        plan: OriginalAssetRetentionPlan,
        intent: Mapping[str, object],
    ) -> EffectReceipt:
        candidate = plan.candidate
        _validate_intent(intent, operation_id, plan)
        terminal = self._store.read(_RECEIPT_COLLECTION, operation_id)
        if terminal is not None:
            _validate_terminal_receipt(terminal, operation_id, plan)
            return _effect_receipt(operation_id)

        if self._step(operation_id, "bytes_staged", candidate) is None:
            if candidate.byte_action == "delete":
                path = _asset_path(
                    self._library_root,
                    vault_ref=candidate.vault_ref,
                )
                quarantine = _quarantine_path(self._library_root, operation_id)
                try:
                    if path.exists():
                        if quarantine.exists():
                            raise OriginalAssetRetentionError(
                                "original asset quarantine identity conflicted"
                            )
                        quarantine.parent.mkdir(parents=True, exist_ok=True)
                        _reject_reparse_path(quarantine.parent, self._library_root)
                        os.replace(path, quarantine)
                    elif quarantine.exists():
                        _verified_file(
                            quarantine,
                            sha256=candidate.sha256,
                            byte_count=candidate.byte_count,
                        )
                    else:
                        raise OriginalAssetRetentionError(
                            "original asset bytes disappeared before quarantine"
                        )
                except OSError as error:
                    raise OriginalAssetRetentionError(
                        "original asset file could not be quarantined"
                    ) from error
                except OriginalAssetRetentionError:
                    raise
            self._write_step(operation_id, "bytes_staged", candidate)
            if self._after_step is not None:
                self._after_step("bytes_staged")
        if self._step(operation_id, "authority", candidate) is None:
            current = self._store.read(_ASSET_COLLECTION, candidate.asset_id)
            if current is not None:
                if self._store.revision(
                    _ASSET_COLLECTION, candidate.asset_id
                ) != candidate.revision:
                    raise OriginalAssetRetentionError(
                        "original asset authority revision drifted during resume"
                    )
                if candidate.asset_id in _linked_asset_ids(self._store):
                    raise OriginalAssetRetentionError(
                        "original asset gained a Source link during resume"
                    )
                self._store.delete(_ASSET_COLLECTION, candidate.asset_id)
            self._write_step(operation_id, "authority", candidate)
            if self._after_step is not None:
                self._after_step("authority")
        if self._step(operation_id, "bytes_deleted", candidate) is None:
            if candidate.byte_action == "delete":
                quarantine = _quarantine_path(self._library_root, operation_id)
                try:
                    if quarantine.exists():
                        _verified_file(
                            quarantine,
                            sha256=candidate.sha256,
                            byte_count=candidate.byte_count,
                        )
                        quarantine.unlink()
                except OSError as error:
                    raise OriginalAssetRetentionError(
                        "original asset quarantine could not be deleted"
                    ) from error
            self._write_step(operation_id, "bytes_deleted", candidate)
            if self._after_step is not None:
                self._after_step("bytes_deleted")
        receipt = {
            "schema_version": "1.0.0", "id": operation_id,
            "kind": "original_asset_retention_receipt",
            "asset_id": candidate.asset_id, "plan_id": plan.plan_id,
            "asset_revision": candidate.revision,
            "sha256": candidate.sha256, "vault_ref": candidate.vault_ref,
            "byte_count": candidate.byte_count, "byte_action": candidate.byte_action,
            "snapshot_id": plan.backup_evidence.snapshot_id,
            "snapshot_fingerprint": plan.backup_evidence.snapshot_fingerprint,
            "asset_backup_id": plan.asset_backup_evidence.backup_id,
            "asset_backup_sha256": plan.asset_backup_evidence.sha256,
            "asset_backup_byte_count": plan.asset_backup_evidence.byte_count,
            "completed_at": _iso(self._clock()),
        }
        existing = self._store.read(_RECEIPT_COLLECTION, operation_id)
        if existing is None:
            self._store.write(_RECEIPT_COLLECTION, operation_id, receipt, expected_revision=0)
        elif existing != receipt:
            raise OriginalAssetRetentionError(
                "original asset purge terminal Receipt drifted"
            )
        return _effect_receipt(operation_id)

    def _step(
        self,
        operation_id: str,
        name: str,
        candidate: OriginalAssetRetentionCandidate,
    ) -> Mapping[str, object] | None:
        step_id = f"{operation_id}-{name}"
        step = self._store.read(_STEP_COLLECTION, step_id)
        if step is not None and (
            step.get("operation_id") != operation_id
            or step.get("step") != name
            or step.get("asset_id") != candidate.asset_id
            or step.get("asset_revision") != candidate.revision
        ):
            raise OriginalAssetRetentionError("original asset purge step fact drifted")
        return step

    def _write_step(
        self,
        operation_id: str,
        name: str,
        candidate: OriginalAssetRetentionCandidate,
    ) -> None:
        step_id = f"{operation_id}-{name}"
        fact = {
            "schema_version": "1.0.0", "id": step_id,
            "operation_id": operation_id, "step": name,
            "asset_id": candidate.asset_id, "asset_revision": candidate.revision,
            "recorded_at": _iso(self._clock()),
        }
        existing = self._store.read(_STEP_COLLECTION, step_id)
        if existing is None:
            self._store.write(_STEP_COLLECTION, step_id, fact, expected_revision=0)
        elif existing != fact:
            raise OriginalAssetRetentionError("original asset purge step fact drifted")


def _linked_asset_ids(store: ObjectStorePort) -> set[str]:
    linked: set[str] = set()
    for raw in store.list(_LINK_COLLECTION):
        asset_id = raw.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise OriginalAssetRetentionError(
                "source asset link catalog contains an invalid asset id"
            )
        linked.add(asset_id)
    return linked


def _has_shared_bytes(
    store: ObjectStorePort,
    *,
    asset_id: str,
    sha256: str,
    vault_ref: str,
) -> bool:
    for raw in store.list(_ASSET_COLLECTION):
        other_id = raw.get("id")
        if other_id == asset_id:
            continue
        if raw.get("sha256") == sha256 or raw.get("vault_ref") == vault_ref:
            return True
    return False


def _asset_identity(raw: Mapping[str, object]) -> tuple[str, str, int]:
    sha256 = raw.get("sha256")
    vault_ref = raw.get("vault_ref")
    byte_count = raw.get("byte_count")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise OriginalAssetRetentionError("original asset sha256 is invalid")
    if not isinstance(vault_ref, str):
        raise OriginalAssetRetentionError("original asset vault_ref is invalid")
    path = PurePosixPath(vault_ref)
    canonical_ref = f"assets/blobs/{sha256[:2]}/{sha256}"
    legacy_prefix = f"assets/originals/{sha256[:2]}/"
    if (
        vault_ref.startswith("/")
        or "\\" in vault_ref
        or any(part in {"", ".", ".."} for part in path.parts)
        or not (
            vault_ref == canonical_ref
            or (
                vault_ref.startswith(legacy_prefix)
                and vault_ref == f"{legacy_prefix}{path.name}"
            )
        )
    ):
        raise OriginalAssetRetentionError("original asset vault_ref is invalid")
    if (
        not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
    ):
        raise OriginalAssetRetentionError("original asset byte_count is invalid")
    return sha256, vault_ref, byte_count


def _asset_path(library_root: Path, *, vault_ref: str) -> Path:
    authority_root = (
        library_root
        / "assets"
        / ("blobs" if vault_ref.startswith("assets/blobs/") else "originals")
    ).resolve(strict=False)
    candidate = (
        library_root / Path(*PurePosixPath(vault_ref).parts)
    ).resolve(strict=False)
    try:
        candidate.relative_to(authority_root)
    except ValueError as error:
        raise OriginalAssetRetentionError(
            "original asset path escapes the active authority root"
        ) from error
    return candidate


def _verified_asset_path(
    library_root: Path,
    *,
    vault_ref: str,
    sha256: str,
    byte_count: int,
) -> Path:
    path = _asset_path(library_root, vault_ref=vault_ref)
    _reject_reparse_path(path.parent, library_root)
    return _verified_file(path, sha256=sha256, byte_count=byte_count)


def _verified_file(path: Path, *, sha256: str, byte_count: int) -> Path:
    if not path.exists():
        raise OriginalAssetRetentionError("original asset file is missing")
    stat = path.lstat()
    attributes = getattr(stat, "st_file_attributes", 0)
    if (
        path.is_symlink()
        or not path.is_file()
        or bool(attributes & _REPARSE_POINT)
    ):
        raise OriginalAssetRetentionError(
            "original asset file is not a regular non-reparse file"
        )
    if stat.st_size != byte_count:
        raise OriginalAssetRetentionError("original asset file size drifted")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise OriginalAssetRetentionError("original asset file hash drifted")
    return path


def _quarantine_path(library_root: Path, operation_id: str) -> Path:
    return (
        library_root
        / "assets"
        / ".retention-quarantine"
        / f"{_required_id(operation_id, 'operation id')}.blob"
    )


def _reject_reparse_path(path: Path, library_root: Path) -> None:
    root = library_root.expanduser().resolve(strict=False)
    current = path
    chain: list[Path] = []
    while current != root:
        try:
            current.relative_to(root)
        except ValueError as error:
            raise OriginalAssetRetentionError(
                "original asset path escapes the Vault root"
            ) from error
        chain.append(current)
        current = current.parent
    for item in reversed(chain):
        if not item.exists():
            continue
        stat = item.lstat()
        attributes = getattr(stat, "st_file_attributes", 0)
        if item.is_symlink() or bool(attributes & _REPARSE_POINT):
            raise OriginalAssetRetentionError(
                "original asset path contains a symlink or reparse point"
            )


def _backup_matches(
    backup: RetentionBackupEvidence, observed_vault_fingerprint: str
) -> bool:
    return (
        backup.status == "verified"
        and isinstance(backup.snapshot_id, str)
        and bool(backup.snapshot_id)
        and isinstance(backup.snapshot_fingerprint, str)
        and bool(backup.snapshot_fingerprint)
        and backup.snapshot_fingerprint == backup.active_fingerprint
        and backup.active_fingerprint == observed_vault_fingerprint
        and isinstance(backup.file_count, int)
        and not isinstance(backup.file_count, bool)
        and backup.file_count > 0
        and backup.error_code is None
    )


def _asset_backup_matches(
    backup: OriginalAssetBackupEvidence,
    *,
    sha256: str,
    byte_count: int,
) -> bool:
    return (
        backup.status == "verified"
        and isinstance(backup.backup_id, str)
        and bool(re.fullmatch(r"asset-backup-[a-z0-9][a-z0-9._-]{0,110}", backup.backup_id))
        and backup.sha256 == sha256
        and backup.byte_count == byte_count
        and backup.error_code is None
    )


def _validate_intent(
    intent: Mapping[str, object],
    operation_id: str,
    plan: OriginalAssetRetentionPlan,
) -> None:
    candidate = plan.candidate
    if (
        intent.get("schema_version") != _INTENT_SCHEMA
        or intent.get("id") != operation_id
        or intent.get("kind") != "original_asset_retention_purge"
        or intent.get("asset_id") != candidate.asset_id
        or intent.get("plan_id") != plan.plan_id
        or intent.get("asset_revision") != candidate.revision
        or intent.get("sha256") != candidate.sha256
        or intent.get("vault_ref") != candidate.vault_ref
        or intent.get("byte_count") != candidate.byte_count
        or intent.get("byte_action") != candidate.byte_action
        or intent.get("snapshot_id") != plan.backup_evidence.snapshot_id
        or intent.get("snapshot_fingerprint")
        != plan.backup_evidence.snapshot_fingerprint
        or intent.get("asset_backup_id")
        != plan.asset_backup_evidence.backup_id
        or intent.get("asset_backup_sha256")
        != plan.asset_backup_evidence.sha256
        or intent.get("asset_backup_byte_count")
        != plan.asset_backup_evidence.byte_count
    ):
        raise OriginalAssetRetentionError(
            "original asset purge intent identity drifted"
        )


def _recovery_plan(
    intent: Mapping[str, object], operation_id: str,
) -> OriginalAssetRetentionPlan:
    if intent.get("schema_version") != _INTENT_SCHEMA or intent.get("id") != operation_id:
        raise OriginalAssetRetentionError(
            "original asset purge recovery Intent drifted"
        )
    required_strings = (
        "asset_id", "plan_id", "sha256", "vault_ref", "byte_action",
        "snapshot_id", "snapshot_fingerprint", "asset_backup_id",
        "asset_backup_sha256", "created_at",
    )
    values = {name: intent.get(name) for name in required_strings}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise OriginalAssetRetentionError(
            "original asset purge recovery Intent is invalid"
        )
    revision = intent.get("asset_revision")
    byte_count = intent.get("byte_count")
    backup_byte_count = intent.get("asset_backup_byte_count")
    if (
        not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
        or not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0
        or backup_byte_count != byte_count
    ):
        raise OriginalAssetRetentionError(
            "original asset purge recovery Intent is invalid"
        )
    stamp = str(values["created_at"])
    candidate = OriginalAssetRetentionCandidate(
        asset_id=str(values["asset_id"]),
        revision=revision,
        sha256=str(values["sha256"]),
        vault_ref=str(values["vault_ref"]),
        byte_count=byte_count,
        orphaned_at=stamp,
        eligible_after=stamp,
        eligible=True,
        blockers=(),
        byte_action=str(values["byte_action"]),
    )
    fingerprint = str(values["snapshot_fingerprint"])
    plan = OriginalAssetRetentionPlan(
        schema_version="1.0.0",
        plan_id=str(values["plan_id"]),
        evaluated_at=stamp,
        observed_vault_fingerprint=fingerprint,
        backup_evidence=RetentionBackupEvidence(
            status="verified",
            snapshot_id=str(values["snapshot_id"]),
            snapshot_fingerprint=fingerprint,
            active_fingerprint=fingerprint,
            file_count=1,
        ),
        asset_backup_evidence=OriginalAssetBackupEvidence(
            status="verified",
            backup_id=str(values["asset_backup_id"]),
            sha256=str(values["asset_backup_sha256"]),
            byte_count=byte_count,
        ),
        candidate=candidate,
    )
    _validate_intent(intent, operation_id, plan)
    return plan


def _validate_terminal_receipt(
    receipt: Mapping[str, object],
    operation_id: str,
    plan: OriginalAssetRetentionPlan,
) -> None:
    candidate = plan.candidate
    if (
        receipt.get("id") != operation_id
        or receipt.get("kind") != "original_asset_retention_receipt"
        or receipt.get("asset_id") != candidate.asset_id
        or receipt.get("plan_id") != plan.plan_id
        or receipt.get("asset_revision") != candidate.revision
        or receipt.get("sha256") != candidate.sha256
        or receipt.get("vault_ref") != candidate.vault_ref
        or receipt.get("byte_count") != candidate.byte_count
        or receipt.get("byte_action") != candidate.byte_action
        or receipt.get("snapshot_id") != plan.backup_evidence.snapshot_id
        or receipt.get("snapshot_fingerprint") != plan.backup_evidence.snapshot_fingerprint
        or receipt.get("asset_backup_id") != plan.asset_backup_evidence.backup_id
        or receipt.get("asset_backup_sha256") != plan.asset_backup_evidence.sha256
        or receipt.get("asset_backup_byte_count") != plan.asset_backup_evidence.byte_count
    ):
        raise OriginalAssetRetentionError(
            "original asset purge terminal Receipt drifted"
        )


def _path_blocker(error: OriginalAssetRetentionError) -> str:
    message = str(error)
    if "missing" in message:
        return "asset_file_missing"
    if "hash" in message or "size" in message:
        return "asset_file_drifted"
    return "asset_file_boundary_invalid"


def _operation_id(plan_id: str, asset_id: str) -> str:
    digest = hashlib.sha256(f"{plan_id}\0{asset_id}".encode("utf-8")).hexdigest()
    return f"original-asset-retention-{digest[:32]}"


def _effect_receipt(operation_id: str) -> EffectReceipt:
    return retention_effect_receipt(
        operation_id,
        receipt_ref_prefix="receipt:original-asset-retention",
        receipt_kind=_RECEIPT_KIND,
        receipt_schema_version=_RECEIPT_SCHEMA,
        intent_schema_version=_INTENT_SCHEMA,
    )


def _required_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,239}", value)
    ):
        raise OriginalAssetRetentionError(f"{label} is invalid")
    return value


def _optional_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _optional_utc_iso(value: object) -> str | None:
    parsed = _optional_utc(value)
    return _iso(parsed) if parsed is not None else None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OriginalAssetRetentionError(
            "original asset retention clock must be timezone-aware"
        )
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")
