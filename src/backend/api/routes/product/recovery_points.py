"""Recovery points ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
import json, os, re
from pathlib import Path

from core.storage_provider import VaultOperationalRecoveryError


def _vault_recovery_roots(runtime_root: Path) -> tuple[Path, Path, Path]:
    from backend.shared.deployment import runtime_backup_roots
    return runtime_backup_roots(runtime_root)


def _recovery_point_record(snapshot_root: Path) -> dict[str, object] | None:
    from core.storage_provider.vault_backup_restore import read_backup_catalog
    return read_backup_catalog(snapshot_root)


def _write_recovery_point_record(
    snapshot_root: Path, payload: Mapping[str, object]
) -> None:
    from core.storage_provider.vault_backup_restore import write_backup_catalog
    write_backup_catalog(snapshot_root, payload)


def _external_recovery_point_records(snapshots_root: Path) -> tuple[dict[str, object], ...]:
    if not snapshots_root.is_dir() or snapshots_root.is_symlink():
        return ()
    records: list[dict[str, object]] = []
    for snapshot_root in sorted(snapshots_root.iterdir(), key=lambda item: item.name):
        if not snapshot_root.is_dir() or snapshot_root.is_symlink():
            continue
        record = _recovery_point_record(snapshot_root)
        if record is not None:
            records.append(record)
    return tuple(records)


def _operation_snapshot_ids(
    *, operations_root: Path, snapshots_root: Path
) -> frozenset[str]:
    if not operations_root.exists():
        return frozenset()
    if not operations_root.is_dir() or operations_root.is_symlink():
        raise VaultOperationalRecoveryError("Vault recovery operations root is unsafe")
    protected: set[str] = set()
    for path in sorted(operations_root.iterdir(), key=lambda item: item.name):
        if path.suffix != ".json":
            continue
        if not path.is_file() or path.is_symlink():
            raise VaultOperationalRecoveryError("Vault recovery operation entry is unsafe")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VaultOperationalRecoveryError(
                "Vault recovery operation entry is unreadable"
            ) from error
        if not isinstance(payload, dict) or not isinstance(
            payload.get("snapshot_root"), str
        ):
            raise VaultOperationalRecoveryError(
                "Vault recovery operation entry is invalid"
            )
        snapshot_root = Path(payload["snapshot_root"]).resolve(strict=False)
        if snapshot_root.parent == snapshots_root.resolve(strict=False):
            protected.add(snapshot_root.name)
    return frozenset(protected)
