"""Asset retention storage ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib, json, os, re, shutil, time
from pathlib import Path, PurePosixPath

from core.product_core.original_asset_retention import (
    OriginalAssetBackupEvidence,
    OriginalAssetRetentionCandidate,
    OriginalAssetRetentionError,
    OriginalAssetRetentionPlan,
)
from core.product_core.retention import RetentionBackupEvidence
from core.storage_provider import JsonObjectStore, VaultBackupRestoreError


_ORIGINAL_ASSET_PLAN_ID = re.compile(r"original-asset-retention-[0-9a-f]{64}")


_ORIGINAL_ASSET_BACKUP_ID = re.compile(
    r"asset-backup-[a-z0-9][a-z0-9._-]{0,110}"
)


_ORIGINAL_ASSET_PLAN_TTL_SECONDS = 10 * 60


_REPARSE_POINT = 0x0400


def _original_asset_plan_root(operations_root: Path) -> Path:
    root = (operations_root / "asset-plans").resolve(strict=False)
    if root.parent != operations_root.resolve(strict=False):
        raise OriginalAssetRetentionError(
            "original asset retention plan root is unsafe"
        )
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise OriginalAssetRetentionError(
            "original asset retention plan root is unsafe"
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _original_asset_plan_path(operations_root: Path, plan_id: str) -> Path:
    if _ORIGINAL_ASSET_PLAN_ID.fullmatch(plan_id) is None:
        raise OriginalAssetRetentionError(
            "original asset retention plan identity is invalid"
        )
    root = _original_asset_plan_root(operations_root)
    digest = plan_id.removeprefix("original-asset-retention-")
    path = root / f"p-{digest[:32]}.json"
    if path.parent != root:
        raise OriginalAssetRetentionError(
            "original asset retention plan path is unsafe"
        )
    return path


def _write_original_asset_plan(
    *,
    operations_root: Path,
    plan: OriginalAssetRetentionPlan,
) -> str:
    created = datetime.now(timezone.utc)
    expires = created.timestamp() + _ORIGINAL_ASSET_PLAN_TTL_SECONDS
    payload = {
        "schema_version": "1.0.0",
        "kind": "original_asset_retention_plan",
        "plan_id": plan.plan_id,
        "asset_id": plan.candidate.asset_id,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "expires_at": datetime.fromtimestamp(expires, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "plan": asdict(plan),
    }
    path = _original_asset_plan_path(operations_root, plan.plan_id)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    try:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(encoded)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        existing = _read_original_asset_plan(
            operations_root=operations_root,
            plan_id=plan.plan_id,
        )
        if existing != payload:
            raise OriginalAssetRetentionError(
                "original asset retention plan identity collided"
            )
    return str(payload["expires_at"])


def _read_original_asset_plan(
    *, operations_root: Path, plan_id: str
) -> dict[str, object]:
    path = _original_asset_plan_path(operations_root, plan_id)
    if not path.is_file() or path.is_symlink():
        raise OriginalAssetRetentionError(
            "original asset retention plan is unavailable"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OriginalAssetRetentionError(
            "original asset retention plan is unreadable"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "1.0.0"
        or payload.get("kind") != "original_asset_retention_plan"
        or payload.get("plan_id") != plan_id
    ):
        raise OriginalAssetRetentionError(
            "original asset retention plan is invalid"
        )
    return payload


def _original_asset_backup_root(snapshots_root: Path) -> Path:
    management_root = snapshots_root.resolve(strict=False).parent
    root = management_root / "original-asset-backups"
    if root.parent != management_root:
        raise OriginalAssetRetentionError("original asset backup root is unsafe")
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise OriginalAssetRetentionError("original asset backup root is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _original_asset_source_path(
    *,
    runtime_root: Path,
    store,
    asset: Mapping[str, object],
) -> tuple[Path, str, int]:
    sha256 = asset.get("sha256")
    vault_ref = asset.get("vault_ref")
    byte_count = asset.get("byte_count")
    if (
        not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or not isinstance(vault_ref, str)
        or "\\" in vault_ref
        or ".." in Path(vault_ref).parts
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
    ):
        raise OriginalAssetRetentionError(
            "original asset backup identity is invalid"
        )
    library_root = (runtime_root / "library").resolve(strict=False)
    location = getattr(store, "asset_location", None)
    if callable(location):
        expected_ref, expected_path = location(
            sha256=sha256,
            legacy_filename=PurePosixPath(vault_ref).name,
        )
        if vault_ref != expected_ref:
            raise OriginalAssetRetentionError(
                "original asset backup authority path drifted"
            )
        path = Path(expected_path).resolve(strict=False)
        authority_root = (
            library_root
            / "assets"
            / ("blobs" if getattr(store, "sqlite_active", False) else "originals")
        ).resolve(strict=False)
    else:
        if not vault_ref.startswith(f"assets/originals/{sha256[:2]}/"):
            raise OriginalAssetRetentionError(
                "original asset backup identity is invalid"
            )
        authority_root = (library_root / "assets" / "originals").resolve(
            strict=False
        )
        path = (
            library_root / Path(*PurePosixPath(vault_ref).parts)
        ).resolve(strict=False)
    try:
        path.relative_to(authority_root)
    except ValueError as error:
        raise OriginalAssetRetentionError(
            "original asset backup path escapes originals root"
        ) from error
    if not path.is_file() or path.is_symlink():
        raise OriginalAssetRetentionError(
            "original asset backup requires a regular file"
        )
    current = path.parent
    while current != library_root:
        current_stat = current.lstat()
        if current.is_symlink() or bool(
            getattr(current_stat, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise OriginalAssetRetentionError(
                "original asset backup path contains a reparse directory"
            )
        current = current.parent
    stat = path.lstat()
    if bool(getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT):
        raise OriginalAssetRetentionError(
            "original asset backup rejects reparse files"
        )
    if stat.st_size != byte_count:
        raise OriginalAssetRetentionError(
            "original asset backup byte count drifted"
        )
    if _sha256_path(path) != sha256:
        raise OriginalAssetRetentionError("original asset backup hash drifted")
    return path, sha256, byte_count


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_original_asset_backup(
    *,
    runtime_root: Path,
    snapshots_root: Path,
    snapshot_id: str,
    store,
    asset: Mapping[str, object],
) -> OriginalAssetBackupEvidence:
    source, sha256, byte_count = _original_asset_source_path(
        runtime_root=runtime_root,
        store=store,
        asset=asset,
    )
    asset_id = str(asset.get("id", ""))
    backup_id = f"asset-backup-{time.time_ns():x}"
    if _ORIGINAL_ASSET_BACKUP_ID.fullmatch(backup_id) is None:
        raise OriginalAssetRetentionError(
            "original asset backup identity is invalid"
        )
    root = _original_asset_backup_root(snapshots_root)
    backup_root = root / backup_id
    backup_root.mkdir(parents=False, exist_ok=False)
    payload = backup_root / "payload.bin"
    partial = backup_root / ".payload.partial"
    try:
        with source.open("rb") as input_stream, partial.open("xb") as output:
            shutil.copyfileobj(input_stream, output, length=256 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if partial.stat().st_size != byte_count or _sha256_path(partial) != sha256:
            raise OriginalAssetRetentionError(
                "original asset backup copy verification failed"
            )
        os.replace(partial, payload)
        manifest = {
            "schema_version": "1.0.0",
            "kind": "original_asset_retention_backup",
            "id": backup_id,
            "asset_id": asset_id,
            "snapshot_id": snapshot_id,
            "sha256": sha256,
            "byte_count": byte_count,
            "vault_ref": str(asset.get("vault_ref", "")),
            "created_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        manifest_path = backup_root / "manifest.json"
        with manifest_path.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(
                manifest,
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        shutil.rmtree(backup_root, ignore_errors=True)
        raise
    return _verify_original_asset_backup(
        snapshots_root=snapshots_root,
        backup_id=backup_id,
        asset_id=asset_id,
        snapshot_id=snapshot_id,
    )


def _verify_original_asset_backup(
    *,
    snapshots_root: Path,
    backup_id: str,
    asset_id: str,
    snapshot_id: str,
) -> OriginalAssetBackupEvidence:
    if _ORIGINAL_ASSET_BACKUP_ID.fullmatch(backup_id) is None:
        raise OriginalAssetRetentionError(
            "original asset backup identity is invalid"
        )
    root = _original_asset_backup_root(snapshots_root)
    backup_root = root / backup_id
    if (
        backup_root.parent != root
        or not backup_root.is_dir()
        or backup_root.is_symlink()
        or {entry.name for entry in backup_root.iterdir()}
        != {"manifest.json", "payload.bin"}
    ):
        raise OriginalAssetRetentionError(
            "original asset backup layout is invalid"
        )
    manifest_path = backup_root / "manifest.json"
    payload = backup_root / "payload.bin"
    if (
        manifest_path.is_symlink()
        or payload.is_symlink()
        or not manifest_path.is_file()
        or not payload.is_file()
    ):
        raise OriginalAssetRetentionError(
            "original asset backup files are invalid"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OriginalAssetRetentionError(
            "original asset backup manifest is unreadable"
        ) from error
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != "1.0.0"
        or manifest.get("kind") != "original_asset_retention_backup"
        or manifest.get("id") != backup_id
        or manifest.get("asset_id") != asset_id
        or manifest.get("snapshot_id") != snapshot_id
        or not isinstance(manifest.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256"))) is None
        or not isinstance(manifest.get("byte_count"), int)
        or isinstance(manifest.get("byte_count"), bool)
        or manifest.get("byte_count") < 0
        or not _valid_original_asset_vault_ref(
            manifest.get("vault_ref"),
            str(manifest.get("sha256")),
        )
    ):
        raise OriginalAssetRetentionError(
            "original asset backup manifest is invalid"
        )
    sha256 = str(manifest["sha256"])
    byte_count = int(manifest["byte_count"])
    if payload.stat().st_size != byte_count or _sha256_path(payload) != sha256:
        raise OriginalAssetRetentionError(
            "original asset backup payload verification failed"
        )
    return OriginalAssetBackupEvidence(
        status="verified",
        backup_id=backup_id,
        sha256=sha256,
        byte_count=byte_count,
    )


def _valid_original_asset_vault_ref(value: object, sha256: str) -> bool:
    if not isinstance(value, str) or "\\" in value:
        return False
    path = PurePosixPath(value)
    if value.startswith("/") or any(part in {".", ".."} for part in path.parts):
        return False
    legacy_prefix = f"assets/originals/{sha256[:2]}/"
    canonical = f"assets/blobs/{sha256[:2]}/{sha256}"
    return (
        value == canonical
        or (
            value.startswith(legacy_prefix)
            and path.name
            and value == f"{legacy_prefix}{path.name}"
        )
    )


def _remove_original_asset_backups_for_snapshot(
    *,
    snapshots_root: Path,
    snapshot_record: Mapping[str, object],
) -> None:
    raw = snapshot_record.get("original_asset_backup_ids", [])
    if not isinstance(raw, list):
        raise VaultBackupRestoreError(
            "recovery point original asset backup catalog is invalid"
        )
    if not raw:
        return
    root = _original_asset_backup_root(snapshots_root)
    for value in raw:
        backup_id = str(value)
        if _ORIGINAL_ASSET_BACKUP_ID.fullmatch(backup_id) is None:
            raise VaultBackupRestoreError(
                "recovery point original asset backup identity is invalid"
            )
        backup_root = root / backup_id
        if not backup_root.exists():
            continue
        if (
            backup_root.parent != root
            or not backup_root.is_dir()
            or backup_root.is_symlink()
            or {entry.name for entry in backup_root.iterdir()}
            != {"manifest.json", "payload.bin"}
        ):
            raise VaultBackupRestoreError(
                "recovery point original asset backup layout is invalid"
            )
        shutil.rmtree(backup_root)


def _original_asset_plan_objects(
    payload: Mapping[str, object],
) -> OriginalAssetRetentionPlan:
    raw = payload.get("plan")
    if not isinstance(raw, Mapping):
        raise OriginalAssetRetentionError(
            "original asset retention plan payload is invalid"
        )
    candidate_raw = raw.get("candidate")
    backup_raw = raw.get("backup_evidence")
    asset_backup_raw = raw.get("asset_backup_evidence")
    if (
        not isinstance(candidate_raw, Mapping)
        or not isinstance(backup_raw, Mapping)
        or not isinstance(asset_backup_raw, Mapping)
        or not isinstance(candidate_raw.get("blockers"), list)
    ):
        raise OriginalAssetRetentionError(
            "original asset retention plan payload is invalid"
        )
    backup = RetentionBackupEvidence(
        status=str(backup_raw.get("status", "")),
        snapshot_id=backup_raw.get("snapshot_id")
        if isinstance(backup_raw.get("snapshot_id"), str)
        else None,
        snapshot_fingerprint=backup_raw.get("snapshot_fingerprint")
        if isinstance(backup_raw.get("snapshot_fingerprint"), str)
        else None,
        active_fingerprint=backup_raw.get("active_fingerprint")
        if isinstance(backup_raw.get("active_fingerprint"), str)
        else None,
        file_count=backup_raw.get("file_count")
        if isinstance(backup_raw.get("file_count"), int)
        else None,
        error_code=backup_raw.get("error_code")
        if isinstance(backup_raw.get("error_code"), str)
        else None,
    )
    asset_backup = OriginalAssetBackupEvidence(
        status=str(asset_backup_raw.get("status", "")),
        backup_id=asset_backup_raw.get("backup_id")
        if isinstance(asset_backup_raw.get("backup_id"), str)
        else None,
        sha256=asset_backup_raw.get("sha256")
        if isinstance(asset_backup_raw.get("sha256"), str)
        else None,
        byte_count=asset_backup_raw.get("byte_count")
        if isinstance(asset_backup_raw.get("byte_count"), int)
        else None,
        error_code=asset_backup_raw.get("error_code")
        if isinstance(asset_backup_raw.get("error_code"), str)
        else None,
    )
    candidate = OriginalAssetRetentionCandidate(
        asset_id=str(candidate_raw.get("asset_id", "")),
        revision=int(candidate_raw.get("revision", 0)),
        sha256=str(candidate_raw.get("sha256", "")),
        vault_ref=str(candidate_raw.get("vault_ref", "")),
        byte_count=int(candidate_raw.get("byte_count", -1)),
        orphaned_at=str(candidate_raw.get("orphaned_at", "")),
        eligible_after=str(candidate_raw.get("eligible_after", "")),
        eligible=candidate_raw.get("eligible") is True,
        blockers=tuple(str(item) for item in candidate_raw["blockers"]),
        byte_action=str(candidate_raw.get("byte_action", "")),
    )
    return OriginalAssetRetentionPlan(
        schema_version=str(raw.get("schema_version", "")),
        plan_id=str(raw.get("plan_id", "")),
        evaluated_at=str(raw.get("evaluated_at", "")),
        observed_vault_fingerprint=str(
            raw.get("observed_vault_fingerprint", "")
        ),
        backup_evidence=backup,
        asset_backup_evidence=asset_backup,
        candidate=candidate,
    )


def _original_asset_receipt_exists(
    store: JsonObjectStore, *, plan_id: str, asset_id: str
) -> bool:
    return any(
        record.get("plan_id") == plan_id and record.get("asset_id") == asset_id
        for collection in (
            "original_asset_retention_receipts",
            "original_asset_retention_intents",
            "original_asset_retention_operations",
        )
        for record in store.list(collection)
    )
