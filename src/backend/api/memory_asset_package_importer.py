from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.product_core.dual_time import (
    optional_rfc3339_timestamp,
    require_rfc3339_timestamp,
)
from core.storage_provider import ObjectStorePort


MEMORY_CANDIDATES_COLLECTION = "memory_candidates"
MEMORY_CONFLICTS_COLLECTION = "memory_candidate_conflicts"
SOURCE_CONFLICTS_COLLECTION = "source_import_conflicts"
ASSET_CONFLICTS_COLLECTION = "source_asset_import_conflicts"
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_SOURCE_ASSET_BYTES = 64 * 1024 * 1024
MAX_MEMORY_RECORDS = 20_000
_MEMORY_FILES = {
    "memories/l1_atomic_facts.ndjson": "L1",
    "memories/l2_scenarios.ndjson": "L2",
    "memories/l3_persona_series_project_skill.ndjson": "L3",
    "memories/l4_persona.ndjson": "L4",
}
_COMPARISON_FIELDS = (
    "memory_id",
    "layer",
    "type",
    "content",
    "summary",
    "tags",
    "confidence",
    "trust_level",
    "source_ref",
    "evidence_refs",
    "privacy_level",
    "project_id",
    "series_id",
    "atom_ids",
    "scenario_ids",
    "project_skill_draft",
    "portable_project_skill_id",
)


class MemoryAssetPackageImportError(ValueError):
    """Raised before writes when an asset package is structurally invalid."""


@dataclass(frozen=True, slots=True)
class ParsedMemoryAssetPackage:
    manifest: dict[str, object]
    memories: tuple[dict[str, object], ...]
    sources: tuple[dict[str, object], ...]
    source_assets: tuple[dict[str, object], ...] = ()
    project_skills: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryAssetPackageImportResult:
    imported_count: int
    skipped_count: int
    conflict_count: int
    failed_count: int
    failures: tuple[dict[str, str], ...]
    conflict_ids: tuple[str, ...]
    source_imported_count: int = 0
    source_skipped_count: int = 0
    source_conflict_count: int = 0
    source_failed_count: int = 0
    source_conflict_ids: tuple[str, ...] = ()
    asset_imported_count: int = 0
    asset_skipped_count: int = 0
    asset_conflict_count: int = 0
    asset_failed_count: int = 0
    asset_conflict_ids: tuple[str, ...] = ()
    project_skill_imported_count: int = 0
    project_skill_skipped_count: int = 0
    project_skill_conflict_count: int = 0
    project_skill_failed_count: int = 0


def parse_memory_asset_package(zip_bytes: bytes) -> ParsedMemoryAssetPackage:
    if not zip_bytes:
        raise MemoryAssetPackageImportError("asset package is empty")
    if len(zip_bytes) > MAX_PACKAGE_BYTES:
        raise MemoryAssetPackageImportError("asset package exceeds the 64 MiB limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise MemoryAssetPackageImportError(f"asset package is not a valid ZIP: {exc}") from exc

    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise MemoryAssetPackageImportError("asset package contains duplicate ZIP entries")
        if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
            raise MemoryAssetPackageImportError(
                "asset package exceeds the 128 MiB uncompressed limit"
            )
        try:
            manifest_raw = archive.read("manifest.json")
        except KeyError as exc:
            raise MemoryAssetPackageImportError("asset package is missing manifest.json") from exc
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MemoryAssetPackageImportError(f"manifest.json is invalid: {exc}") from exc
        if not isinstance(manifest, dict):
            raise MemoryAssetPackageImportError("manifest.json must contain an object")
        if manifest.get("format") != "memory_asset_package":
            raise MemoryAssetPackageImportError("manifest format must be memory_asset_package")
        package_version = manifest.get("version")
        if package_version not in {"1.0", "1.1", "1.2"}:
            raise MemoryAssetPackageImportError("unsupported asset package version")

        parsed: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for path, expected_layer in _MEMORY_FILES.items():
            try:
                content = archive.read(path).decode("utf-8")
            except KeyError as exc:
                if package_version == "1.0" and expected_layer == "L4":
                    continue
                raise MemoryAssetPackageImportError(
                    f"asset package is missing required file {path}"
                ) from exc
            except UnicodeDecodeError as exc:
                raise MemoryAssetPackageImportError(f"{path} is not UTF-8") from exc
            for line_number, line in enumerate(content.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MemoryAssetPackageImportError(
                        f"{path}:{line_number} contains invalid JSON: {exc.msg}"
                    ) from exc
                if not isinstance(raw, dict):
                    raise MemoryAssetPackageImportError(
                        f"{path}:{line_number} must contain an object"
                    )
                memory = _normalize_memory(raw, expected_layer, path, line_number)
                memory_id = str(memory["memory_id"])
                if memory_id in seen_ids:
                    raise MemoryAssetPackageImportError(
                        f"asset package contains duplicate memory_id {memory_id}"
                    )
                seen_ids.add(memory_id)
                parsed.append(memory)
                if len(parsed) > MAX_MEMORY_RECORDS:
                    raise MemoryAssetPackageImportError(
                        f"asset package exceeds the {MAX_MEMORY_RECORDS} memory limit"
                    )

        parsed_sources = _parse_source_manifest(archive)
        parsed_project_skills = _parse_project_skill_cards(
            archive,
            sources=parsed_sources,
        )
        parsed_source_assets = (
            _parse_source_assets(archive, parsed_sources)
            if package_version == "1.2"
            else ()
        )
        if package_version == "1.2":
            manifest_asset_count = manifest.get("source_asset_count")
            if (
                isinstance(manifest_asset_count, bool)
                or not isinstance(manifest_asset_count, int)
                or manifest_asset_count != len(parsed_source_assets)
            ):
                raise MemoryAssetPackageImportError(
                    "manifest source_asset_count does not match parsed count"
                )
        source_ids = {str(source["id"]) for source in parsed_sources}
        for memory in parsed:
            required_source = _memory_required_source_id(memory)
            if required_source and required_source not in source_ids:
                raise MemoryAssetPackageImportError(
                    f"memory {memory['memory_id']} references missing source {required_source}"
                )
        for skill in parsed_project_skills:
            for reference in skill["source_refs"]:
                if reference["source_id"] not in source_ids:
                    raise MemoryAssetPackageImportError(
                        f"project skill {skill['id']} references missing source "
                        f"{reference['source_id']}"
                    )
        manifest_project_skill_count = manifest.get("project_skill_count")
        if (
            manifest_project_skill_count is not None
            and (
                isinstance(manifest_project_skill_count, bool)
                or not isinstance(manifest_project_skill_count, int)
                or manifest_project_skill_count != len(parsed_project_skills)
            )
        ):
            raise MemoryAssetPackageImportError(
                "manifest project_skill_count does not match parsed count"
            )

        manifest_count = manifest.get("memory_count")
        if isinstance(manifest_count, bool) or not isinstance(manifest_count, int):
            raise MemoryAssetPackageImportError("manifest memory_count must be an integer")
        if manifest_count != len(parsed):
            raise MemoryAssetPackageImportError(
                f"manifest memory_count {manifest_count} does not match parsed count {len(parsed)}"
            )
        return ParsedMemoryAssetPackage(
            manifest=dict(manifest),
            memories=tuple(parsed),
            sources=parsed_sources,
            source_assets=parsed_source_assets,
            project_skills=parsed_project_skills,
        )


def import_memory_asset_package(
    *,
    store: ObjectStorePort,
    package: ParsedMemoryAssetPackage,
    import_batch_id: str,
    created_at: str,
    library_root: Path | None = None,
) -> MemoryAssetPackageImportResult:
    try:
        local_recorded_at = require_rfc3339_timestamp(
            created_at,
            field="recorded_at",
        )
    except ValueError as exc:
        raise MemoryAssetPackageImportError(str(exc)) from exc
    if package.source_assets and library_root is None:
        raise MemoryAssetPackageImportError(
            "asset package contains original files but no target library root was provided"
        )
    imported = 0
    skipped = 0
    conflicts = 0
    failed = 0
    failures: list[dict[str, str]] = []
    conflict_ids: list[str] = []
    source_imported = 0
    source_skipped = 0
    source_conflicts = 0
    source_failed = 0
    source_conflict_ids: list[str] = []
    blocked_asset_sources: set[str] = set()
    asset_imported = 0
    asset_skipped = 0
    asset_conflicts = 0
    asset_failed = 0
    asset_conflict_ids: list[str] = []
    project_skill_imported = 0
    project_skill_skipped = 0
    project_skill_conflicts = 0
    project_skill_failed = 0

    for source in package.sources:
        source = _source_for_local_import(
            source,
            recorded_at=local_recorded_at,
            package_version=str(package.manifest.get("version", "")),
        )
        source_id = str(source["id"])
        existing_source = store.read("sources", source_id)
        if existing_source is not None:
            if _same_source(existing_source, source):
                source_skipped += 1
                continue
            conflict_id = _source_conflict_id(source_id, existing_source, source)
            conflict = {
                "id": conflict_id,
                "conflict_id": conflict_id,
                "source_id": source_id,
                "existing": dict(existing_source),
                "incoming": source,
                "status": "needs_review",
                "created_at": created_at,
                "import_batch_id": import_batch_id,
            }
            try:
                if store.read(SOURCE_CONFLICTS_COLLECTION, conflict_id) is None:
                    store.write(
                        SOURCE_CONFLICTS_COLLECTION,
                        conflict_id,
                        conflict,
                        expected_revision=0,
                    )
                source_conflicts += 1
                source_conflict_ids.append(conflict_id)
                blocked_asset_sources.add(source_id)
            except Exception as exc:  # noqa: BLE001
                source_failed += 1
                blocked_asset_sources.add(source_id)
                failures.append({
                    "id": source_id,
                    "error": f"source_conflict_save_failed: {exc}",
                })
            continue
        try:
            store.write("sources", source_id, source, expected_revision=0)
            source_imported += 1
        except Exception as exc:  # noqa: BLE001
            source_failed += 1
            blocked_asset_sources.add(source_id)
            failures.append({"id": source_id, "error": f"source_save_failed: {exc}"})

    if library_root is not None:
        for asset in package.source_assets:
            if str(asset["source_id"]) in blocked_asset_sources:
                asset_failed += 1
                failures.append({
                    "id": str(asset["asset_id"]),
                    "error": f"source_conflict_blocks_asset: {asset['source_id']}",
                })
                continue
            outcome = _restore_source_asset(
                store=store,
                library_root=library_root,
                asset=asset,
                import_batch_id=import_batch_id,
                created_at=created_at,
            )
            if outcome[0] == "imported":
                asset_imported += 1
            elif outcome[0] == "skipped":
                asset_skipped += 1
            elif outcome[0] == "conflict":
                asset_conflicts += 1
                asset_conflict_ids.append(outcome[1])
            else:
                asset_failed += 1
                failures.append({"id": str(asset["asset_id"]), "error": outcome[1]})

    imported_memories = [
        *package.memories,
        *(_project_skill_memory(skill) for skill in package.project_skills),
    ]
    for memory in imported_memories:
        is_project_skill = memory.get("type") == "project_skill"
        required_source = _memory_required_source_id(memory)
        if required_source and store.read("sources", required_source) is None:
            failed += 1
            if is_project_skill:
                project_skill_failed += 1
            failures.append({
                "id": str(memory["memory_id"]),
                "error": f"source_unavailable: {required_source}",
            })
            continue
        candidate = _candidate_payload(memory, import_batch_id, created_at)
        if memory.get("type") == "project_skill":
            candidate["project_skill_draft"] = dict(memory["project_skill_draft"])
            candidate["portable_project_skill_id"] = memory["portable_project_skill_id"]
        candidate_id = str(candidate["id"])
        existing = store.read(MEMORY_CANDIDATES_COLLECTION, candidate_id)
        if existing is not None:
            if _same_memory(existing, candidate):
                skipped += 1
                if is_project_skill:
                    project_skill_skipped += 1
                continue
            conflict_id = _conflict_id(candidate_id, existing, candidate)
            conflict = {
                "id": conflict_id,
                "conflict_id": conflict_id,
                "candidate_id": candidate_id,
                "existing": dict(existing),
                "incoming": candidate,
                "status": "needs_review",
                "resolution": "",
                "created_at": created_at,
                "import_batch_id": import_batch_id,
            }
            try:
                if store.read(MEMORY_CONFLICTS_COLLECTION, conflict_id) is None:
                    store.write(
                        MEMORY_CONFLICTS_COLLECTION,
                        conflict_id,
                        conflict,
                        expected_revision=0,
                    )
                conflicts += 1
                if is_project_skill:
                    project_skill_conflicts += 1
                conflict_ids.append(conflict_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if is_project_skill:
                    project_skill_failed += 1
                failures.append({"id": candidate_id, "error": f"conflict_save_failed: {exc}"})
            continue
        try:
            store.write(
                MEMORY_CANDIDATES_COLLECTION,
                candidate_id,
                candidate,
                expected_revision=0,
            )
            imported += 1
            if is_project_skill:
                project_skill_imported += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if is_project_skill:
                project_skill_failed += 1
            failures.append({"id": candidate_id, "error": f"candidate_save_failed: {exc}"})

    return MemoryAssetPackageImportResult(
        imported_count=imported,
        skipped_count=skipped,
        conflict_count=conflicts,
        failed_count=failed,
        failures=tuple(failures),
        conflict_ids=tuple(conflict_ids),
        source_imported_count=source_imported,
        source_skipped_count=source_skipped,
        source_conflict_count=source_conflicts,
        source_failed_count=source_failed,
        source_conflict_ids=tuple(source_conflict_ids),
        asset_imported_count=asset_imported,
        asset_skipped_count=asset_skipped,
        asset_conflict_count=asset_conflicts,
        asset_failed_count=asset_failed,
        asset_conflict_ids=tuple(asset_conflict_ids),
        project_skill_imported_count=project_skill_imported,
        project_skill_skipped_count=project_skill_skipped,
        project_skill_conflict_count=project_skill_conflicts,
        project_skill_failed_count=project_skill_failed,
    )


def _parse_source_assets(
    archive: zipfile.ZipFile,
    sources: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    path = "sources/source_assets.ndjson"
    try:
        content = archive.read(path).decode("utf-8")
    except KeyError as exc:
        raise MemoryAssetPackageImportError(
            f"asset package is missing required file {path}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise MemoryAssetPackageImportError(f"{path} is not UTF-8") from exc
    source_ids = {str(source["id"]) for source in sources}
    result: list[dict[str, object]] = []
    seen_assets: set[str] = set()
    declared_blobs: set[str] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} contains invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(raw, dict):
            raise MemoryAssetPackageImportError(f"{path}:{line_number} must contain an object")
        source_id = str(raw.get("source_id", "")).strip()
        asset_id = str(raw.get("asset_id", "")).strip()
        sha256 = str(raw.get("sha256", "")).strip()
        blob_path = str(raw.get("blob_path", "")).strip()
        byte_count = raw.get("byte_count")
        if source_id not in source_ids:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} references unknown source {source_id}"
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", asset_id):
            raise MemoryAssetPackageImportError(f"{path}:{line_number} asset_id is invalid")
        if asset_id in seen_assets:
            raise MemoryAssetPackageImportError(
                f"asset package contains duplicate asset_id {asset_id}"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise MemoryAssetPackageImportError(f"{path}:{line_number} sha256 is invalid")
        expected_blob_path = f"sources/content/{sha256}"
        if blob_path != expected_blob_path:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} blob_path is invalid"
            )
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise MemoryAssetPackageImportError(f"{path}:{line_number} byte_count is invalid")
        if byte_count > MAX_SOURCE_ASSET_BYTES:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} source asset exceeds the 64 MiB limit"
            )
        display_name = str(raw.get("display_name", ""))
        if (
            not display_name
            or display_name != Path(display_name).name
            or any(ord(character) < 32 for character in display_name)
        ):
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} display_name is invalid"
            )
        try:
            raw_content = archive.read(blob_path)
        except KeyError as exc:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} references missing blob {blob_path}"
            ) from exc
        if len(raw_content) != byte_count:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} blob size does not match byte_count"
            )
        if hashlib.sha256(raw_content).hexdigest() != sha256:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} blob hash does not match sha256"
            )
        seen_assets.add(asset_id)
        declared_blobs.add(blob_path)
        result.append({
            "source_id": source_id,
            "asset_id": asset_id,
            "display_name": display_name,
            "media_type": str(raw.get("media_type", "application/octet-stream")),
            "byte_count": byte_count,
            "sha256": sha256,
            "blob_path": blob_path,
            "content": raw_content,
        })
    undeclared = {
        name
        for name in archive.namelist()
        if name.startswith("sources/content/") and name not in declared_blobs
    }
    if undeclared:
        raise MemoryAssetPackageImportError(
            "asset package contains undeclared source content"
        )
    return tuple(result)


def _restore_source_asset(
    *,
    store: ObjectStorePort,
    library_root: Path,
    asset: dict[str, object],
    import_batch_id: str,
    created_at: str,
) -> tuple[str, str]:
    source_id = str(asset["source_id"])
    asset_id = str(asset["asset_id"])
    sha256 = str(asset["sha256"])
    content = asset["content"]
    if not isinstance(content, bytes):
        return "failed", "asset_content_invalid"
    source = store.read("sources", source_id)
    if source is None:
        return "failed", f"source_unavailable: {source_id}"
    existing_link = next(
        (
            item
            for item in store.list("source_asset_links")
            if item.get("source_id") == source_id and item.get("role") == "original"
        ),
        None,
    )
    existing_asset = store.read("workbench_original_assets", asset_id)
    incoming_identity = {
        "source_id": source_id,
        "asset_id": asset_id,
        "sha256": sha256,
        "byte_count": len(content),
    }
    if (
        existing_link is not None
        and existing_link.get("asset_id") != asset_id
    ) or (
        existing_asset is not None
        and (
            existing_asset.get("sha256") != sha256
            or existing_asset.get("byte_count") != len(content)
        )
    ):
        conflict_id = _asset_conflict_id(incoming_identity)
        if store.read(ASSET_CONFLICTS_COLLECTION, conflict_id) is None:
            store.write(ASSET_CONFLICTS_COLLECTION, conflict_id, {
                "id": conflict_id,
                "status": "needs_review",
                "incoming": incoming_identity,
                "existing_asset": dict(existing_asset or {}),
                "existing_link": dict(existing_link or {}),
                "import_batch_id": import_batch_id,
                "created_at": created_at,
            }, expected_revision=0)
        return "conflict", conflict_id

    target_root = library_root.expanduser().resolve(strict=False)
    location = getattr(store, "asset_location", None)
    if callable(location):
        vault_ref, target = location(
            sha256=sha256,
            legacy_filename=asset_id,
        )
        target = Path(target).resolve(strict=False)
    else:
        vault_ref = f"assets/originals/{sha256[:2]}/{asset_id}"
        target = (target_root / vault_ref).resolve(strict=False)
    try:
        target.relative_to(target_root)
    except ValueError:
        return "failed", "asset_target_escape"
    target.parent.mkdir(parents=True, exist_ok=True)
    created_blob = False
    if target.exists():
        if (
            not target.is_file()
            or target.is_symlink()
            or target.stat().st_size != len(content)
            or _sha256_file(target) != sha256
        ):
            conflict_id = _asset_conflict_id(incoming_identity)
            if store.read(ASSET_CONFLICTS_COLLECTION, conflict_id) is None:
                store.write(ASSET_CONFLICTS_COLLECTION, conflict_id, {
                    "id": conflict_id,
                    "status": "needs_review",
                    "incoming": incoming_identity,
                    "reason": "target_blob_conflict",
                    "import_batch_id": import_batch_id,
                    "created_at": created_at,
                }, expected_revision=0)
            return "conflict", conflict_id
    else:
        batch_token = hashlib.sha256(import_batch_id.encode("utf-8")).hexdigest()[:16]
        temporary = target.with_name(f".{asset_id}.{batch_token}.part")
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if _sha256_file(temporary) != sha256:
                return "failed", "asset_staging_hash_mismatch"
            os.replace(temporary, target)
            created_blob = True
        except Exception as exc:  # noqa: BLE001
            temporary.unlink(missing_ok=True)
            return "failed", f"asset_blob_save_failed: {exc}"

    asset_record = {
        "schema_version": "1.0.0",
        "id": asset_id,
        "kind": "workbench_original_asset",
        "status": "stored",
        "asset_ref": f"crp-ref-import-assets-originals-{asset_id}",
        "vault_ref": vault_ref,
        "display_name": str(asset.get("display_name", "")),
        "media_type": str(asset.get("media_type", "application/octet-stream")),
        "byte_count": len(content),
        "sha256": sha256,
        "storage_mode": "stored_original",
        "availability": "available",
        "availability_reason": "memory_asset_package_restored",
        "link_status": "linked",
        "linked_source_ids": [source_id],
        "orphan_reason": None,
        "created_at": created_at,
        "occurred_at": source.get("occurred_at"),
        "recorded_at": created_at,
        "metadata": {
            "source_kind": "memory_asset_package",
            "import_batch_id": import_batch_id,
            "content_hash_basis": "portable_original_bytes_sha256",
        },
    }
    link_id = f"source-asset-{source_id}-{asset_id}"
    link_record = {
        "schema_version": "1.0.0",
        "id": link_id,
        "source_id": source_id,
        "source_uri": str(source.get("storage_uri", "")),
        "asset_id": asset_id,
        "asset_ref": asset_record["asset_ref"],
        "role": "original",
        "content_hash": sha256,
        "size_bytes": len(content),
        "created_at": created_at,
        "occurred_at": source.get("occurred_at"),
        "recorded_at": created_at,
        "provenance": "memory_asset_package_import",
    }
    wrote_asset = False
    wrote_link = False
    try:
        if existing_asset is None:
            store.write("workbench_original_assets", asset_id, asset_record, expected_revision=0)
            wrote_asset = True
        if existing_link is None:
            atomic_link = getattr(store, "link_source_asset", None)
            if callable(atomic_link) and getattr(store, "sqlite_active", False):
                atomic_link(
                    asset_ref=str(asset_record["asset_ref"]),
                    link_id=link_id,
                    link_payload=link_record,
                    source_id=source_id,
                )
            else:
                store.write("source_asset_links", link_id, link_record, expected_revision=0)
            wrote_link = True
        updated_source = dict(source)
        metadata = dict(updated_source.get("metadata") or {})
        metadata.update({
            "roundtrip_reference_only": False,
            "raw_content_restored": True,
            "restored_asset_id": asset_id,
            "restored_sha256": sha256,
        })
        updated_source.update({
            "processing_state": "ready",
            "metadata": metadata,
        })
        store.write(
            "sources",
            source_id,
            updated_source,
            expected_revision=store.revision("sources", source_id),
        )
    except Exception as exc:  # noqa: BLE001
        if wrote_link:
            store.delete("source_asset_links", link_id)
        if wrote_asset:
            store.delete("workbench_original_assets", asset_id)
        if created_blob and existing_asset is None:
            target.unlink(missing_ok=True)
        return "failed", f"asset_metadata_save_failed: {exc}"
    return ("skipped" if existing_asset is not None and existing_link is not None else "imported"), asset_id


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_conflict_id(identity: dict[str, object]) -> str:
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"conflict-source-asset-roundtrip-{hashlib.sha256(payload).hexdigest()[:24]}"


def _parse_source_manifest(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, object], ...]:
    path = "sources/source_manifest.ndjson"
    try:
        content = archive.read(path).decode("utf-8")
    except KeyError as exc:
        raise MemoryAssetPackageImportError(
            f"asset package is missing required file {path}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise MemoryAssetPackageImportError(f"{path} is not UTF-8") from exc
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} contains invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(raw, dict):
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} must contain an object"
            )
        source_id = str(raw.get("source_id", "")).strip()
        if not source_id:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} is missing source_id"
            )
        if source_id in seen:
            raise MemoryAssetPackageImportError(
                f"asset package contains duplicate source_id {source_id}"
            )
        seen.add(source_id)
        occurred_at = _occurred_at(raw, path=path, line_number=line_number)
        recorded_at = _recorded_at(raw, path=path, line_number=line_number)
        result.append({
            "schema_version": "1.1.0",
            "id": source_id,
            "type": str(raw.get("source_type", "other")),
            "title": str(raw.get("title", "")),
            "storage_uri": str(raw.get("content_ref", "")),
            "media_type": str(raw.get("media_type", "")),
            "size_bytes": 0,
            "processing_state": "reference_only",
            "trust_status": "imported_unverified",
            "created_at": str(raw.get("created_at", "")),
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "metadata": {
                "roundtrip_reference_only": True,
                "raw_content_restored": False,
                "is_audio_visual": raw.get("is_audio_visual") is True,
                "source_temporal": {
                    "occurred_at": occurred_at,
                    "recorded_at": recorded_at,
                },
            },
        })
    return tuple(result)


def _parse_project_skill_cards(
    archive: zipfile.ZipFile,
    *,
    sources: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    path = "project_skills/project_skill_cards.ndjson"
    try:
        content = archive.read(path).decode("utf-8")
    except KeyError:
        return ()
    except UnicodeDecodeError as exc:
        raise MemoryAssetPackageImportError(f"{path} is not UTF-8") from exc
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} contains invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(raw, dict):
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} must contain an object"
            )
        skill_id = str(raw.get("id", "")).strip()
        project_id = str(raw.get("project_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", skill_id):
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} project skill id is invalid"
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", project_id):
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} project_id is invalid"
            )
        canonical_skill_id = f"skill-{project_id}"
        if canonical_skill_id in seen:
            raise MemoryAssetPackageImportError(
                f"asset package contains multiple project skills for {project_id}"
            )
        seen.add(canonical_skill_id)
        purpose = raw.get("purpose", raw.get("description"))
        for field, value in (("name", raw.get("name")), ("purpose", purpose)):
            if not isinstance(value, str) or not value.strip():
                raise MemoryAssetPackageImportError(
                    f"{path}:{line_number} project skill {field} is required"
                )
        refs_value = raw.get("source_refs")
        if "source_refs" not in raw and len(sources) == 1:
            refs_value = [{
                "source_id": str(sources[0]["id"]),
                "locator": "source",
            }]
        refs = _normalized_project_skill_refs(
            refs_value,
            path=path,
            line_number=line_number,
        )
        proposed = {
            key: raw[key]
            for key in (
                "name",
                "purpose",
                "required_context",
                "output_rules",
                "style_preferences",
                "outline",
                "update_rules",
                "source_refs",
                "evidence_refs",
                "decision_log",
            )
            if key in raw
        }
        try:
            json.dumps(proposed, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} project skill is not JSON serializable"
            ) from exc
        result.append({
            "id": canonical_skill_id,
            "portable_skill_id": skill_id,
            "project_id": project_id,
            "name": str(raw["name"]).strip(),
            "purpose": purpose.strip(),
            "source_refs": refs,
            "project_skill_draft": proposed,
        })
    return tuple(result)


def _normalized_project_skill_refs(
    value: object,
    *,
    path: str,
    line_number: int,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise MemoryAssetPackageImportError(
            f"{path}:{line_number} project skill source_refs are required"
        )
    result: list[dict[str, str]] = []
    for reference in value:
        if not isinstance(reference, Mapping):
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} project skill source_refs are invalid"
            )
        source_id = reference.get("source_id")
        locator = reference.get("locator")
        if (
            not isinstance(source_id, str)
            or not source_id.strip()
            or not isinstance(locator, str)
            or not locator.strip()
        ):
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} project skill source_refs are invalid"
            )
        normalized = {"source_id": source_id.strip(), "locator": locator.strip()}
        if normalized not in result:
            result.append(normalized)
    return result


def _project_skill_memory(skill: dict[str, object]) -> dict[str, object]:
    refs = list(skill["source_refs"])
    first = refs[0]
    return {
        "memory_id": str(skill["id"]),
        "layer": "L3",
        "type": "project_skill",
        "content": str(skill["purpose"]),
        "summary": str(skill["name"]),
        "tags": [],
        "confidence": 1.0,
        "trust_level": "imported_unverified",
        "source_platform": "roundtrip",
        "source_type": "project_skill",
        "source_role": "user",
        "source_ref": first["source_id"],
        "evidence_refs": [
            f"{reference['source_id']}#{reference['locator']}"
            for reference in refs
        ],
        "created_at": "",
        "updated_at": "",
        "occurred_at": None,
        "recorded_at": "",
        "conflict_refs": [],
        "privacy_level": "private",
        "project_id": str(skill["project_id"]),
        "series_id": "",
        "atom_ids": [],
        "scenario_ids": [],
        "project_skill_draft": dict(skill["project_skill_draft"]),
        "portable_project_skill_id": str(skill["portable_skill_id"]),
    }


def _normalize_memory(
    raw: dict[str, Any],
    expected_layer: str,
    path: str,
    line_number: int,
) -> dict[str, object]:
    memory_id = str(raw.get("memory_id") or raw.get("id") or "").strip()
    content = raw.get("content")
    layer = str(raw.get("layer") or "").strip()
    if not memory_id:
        raise MemoryAssetPackageImportError(f"{path}:{line_number} is missing memory_id")
    if not isinstance(content, str) or not content.strip():
        raise MemoryAssetPackageImportError(f"{path}:{line_number} is missing content")
    if layer != expected_layer:
        raise MemoryAssetPackageImportError(
            f"{path}:{line_number} layer {layer!r} does not match {expected_layer}"
        )
    memory_type = str(raw.get("type", "other"))
    if expected_layer == "L4" and memory_type not in {"persona", "preference", "rule"}:
        raise MemoryAssetPackageImportError(
            f"{path}:{line_number} L4 type {memory_type!r} is not a Persona candidate"
        )
    for field in ("tags", "evidence_refs", "conflict_refs", "atom_ids", "scenario_ids"):
        value = raw.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise MemoryAssetPackageImportError(
                f"{path}:{line_number} field {field} must be an array of strings"
            )
    confidence = raw.get("confidence", 0.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise MemoryAssetPackageImportError(
            f"{path}:{line_number} confidence must be a number"
        )
    if not 0.0 <= float(confidence) <= 1.0:
        raise MemoryAssetPackageImportError(
            f"{path}:{line_number} confidence must be between 0 and 1"
        )
    return {
        "memory_id": memory_id,
        "layer": layer,
        "type": memory_type,
        "content": content,
        "summary": str(raw.get("summary", "")),
        "tags": list(raw.get("tags", [])),
        "confidence": float(confidence),
        "trust_level": str(raw.get("trust_level", "unverified")),
        "source_platform": str(raw.get("source_platform", "roundtrip")),
        "source_type": str(raw.get("source_type", "document")),
        "source_role": str(raw.get("source_role", "user")),
        "source_ref": str(raw.get("source_ref", "")),
        "evidence_refs": list(raw.get("evidence_refs", [])),
        "created_at": str(raw.get("created_at", "")),
        "updated_at": str(raw.get("updated_at", "")),
        "occurred_at": _occurred_at(raw, path=path, line_number=line_number),
        "recorded_at": _recorded_at(raw, path=path, line_number=line_number),
        "conflict_refs": list(raw.get("conflict_refs", [])),
        "privacy_level": str(raw.get("privacy_level", "private")),
        "project_id": str(raw.get("project_id", "")),
        "series_id": str(raw.get("series_id", "")),
        "atom_ids": list(raw.get("atom_ids", [])),
        "scenario_ids": list(raw.get("scenario_ids", [])),
    }


def _candidate_payload(
    memory: dict[str, object],
    import_batch_id: str,
    created_at: str,
) -> dict[str, object]:
    candidate_id = str(memory["memory_id"])
    payload = {
        "schema_version": "1.0.0",
        "id": candidate_id,
        **memory,
        "created_at": str(memory.get("created_at") or created_at),
        "updated_at": created_at,
        "observed_at": created_at,
        "occurred_at": memory.get("occurred_at"),
        "recorded_at": created_at,
        "status": "pending_review",
        "import_batch_id": import_batch_id,
        "provider_boundary": "local",
        "group": "needs_review",
        "target_layer": _candidate_target_layer(memory),
        "portable_object_id": candidate_id,
        "candidate_type": _modern_candidate_type(memory),
        "proposed_content": str(memory.get("content", "")),
        "source_refs": _candidate_source_refs(memory),
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": None,
            "document_revision": None,
            "source_content_read_id": (
                f"memory-asset-package:{import_batch_id}:{candidate_id}"
            ),
            "input_refs": [
                {
                    "kind": "source",
                    "object_id": ref["source_id"],
                    "uri": f"crp://default/sources/{ref['source_id']}.json",
                }
                for ref in _candidate_source_refs(memory)
            ] + [{
                "kind": "source_content_read",
                "object_id": (
                    f"memory-asset-package:{import_batch_id}:{candidate_id}"
                ),
                "uri": (
                    f"crp://default/memory-import-batches/{import_batch_id}"
                    f"/reads/{candidate_id}.json"
                ),
            }],
            "import_batch_id": import_batch_id,
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "资产包导入候选需要用户确认后进入 staging。",
            "reviewed_by": None,
            "reviewed_at": None,
        },
    }
    if memory.get("layer") == "L4":
        source_id, source_refs = _l4_source_identity(memory)
        payload.update({
            "target_layer": "persona",
            "candidate_type": str(memory.get("type", "preference")),
            "proposed_content": str(memory.get("content", "")),
            "source_id": source_id,
            "source_refs": source_refs,
        })
    return payload


def _occurred_at(
    raw: Mapping[str, object],
    *,
    path: str,
    line_number: int,
) -> str | None:
    """Accept v1.0-v1.2 legacy records and validate an explicit occurrence."""
    value = raw.get("occurred_at") if "occurred_at" in raw else raw.get("created_at")
    try:
        return optional_rfc3339_timestamp(
            value,
            field=f"{path}:{line_number} occurred_at",
        )
    except ValueError as exc:
        raise MemoryAssetPackageImportError(str(exc)) from exc


def _recorded_at(
    raw: Mapping[str, object],
    *,
    path: str,
    line_number: int,
) -> str:
    value = raw.get("recorded_at") if "recorded_at" in raw else raw.get("created_at")
    if value is None or value == "":
        return ""
    try:
        return require_rfc3339_timestamp(
            value,
            field=f"{path}:{line_number} recorded_at",
        )
    except ValueError as exc:
        raise MemoryAssetPackageImportError(str(exc)) from exc


def _source_for_local_import(
    source: Mapping[str, object],
    *,
    recorded_at: str,
    package_version: str,
) -> dict[str, object]:
    """Materialize a portable source as a schema-valid local authority record."""

    result = dict(source)
    metadata = dict(source.get("metadata")) if isinstance(source.get("metadata"), dict) else {}
    exported_recorded_at = source.get("recorded_at")
    temporal = dict(metadata.get("source_temporal")) if isinstance(metadata.get("source_temporal"), dict) else {}
    temporal["exported_recorded_at"] = exported_recorded_at
    temporal["local_recorded_at"] = recorded_at
    metadata.update({
        "source_temporal": temporal,
        "content_hash_basis": "portable_content_reference_not_original_bytes",
    })
    content_ref = str(source.get("storage_uri", ""))
    occurred_at = source.get("occurred_at")
    result.update({
        "schema_version": "1.1.0",
        "capture_mode": "reference",
        "original_url": None,
        "content_hash": hashlib.sha256(content_ref.encode("utf-8")).hexdigest(),
        "parser_version": f"memory-asset-package:{package_version or 'legacy'}",
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "created_at": occurred_at or recorded_at,
        "observed_at": recorded_at,
        "imported_from_legacy": package_version == "1.0",
        "metadata": metadata,
    })
    return result


def _modern_candidate_type(memory: Mapping[str, object]) -> str:
    memory_type = str(memory.get("type", "other")).strip().lower()
    if memory_type in {"fact", "atomic_fact"}:
        return "answer_fact"
    if memory_type == "decision":
        return "answer_decision"
    if memory_type == "action":
        return "answer_action"
    if memory_type in {"summary", "scenario", "series_memory", "persona", "preference", "rule"}:
        return "answer_summary"
    if memory_type in {
        "answer_fact",
        "answer_decision",
        "answer_action",
        "answer_summary",
        "document_takeaway",
        "other",
    }:
        return memory_type
    return "other"


def _candidate_target_layer(memory: dict[str, object]) -> str:
    return {
        "L1": "atom",
        "L2": "scenario",
        "L3": (
            "project_skill"
            if str(memory.get("type", "")).lower() in {"project_skill", "skill"}
            else "series_memory"
        ),
        "L4": "persona",
    }[str(memory["layer"])]


def _candidate_source_refs(memory: dict[str, object]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    values = memory.get("evidence_refs")
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, str) or "#" not in value:
                continue
            source_id, locator = value.split("#", 1)
            if source_id and locator:
                refs.append({"source_id": source_id, "locator": locator})
    source_ref = str(memory.get("source_ref", "")).strip()
    if source_ref and not any(ref["source_id"] == source_ref for ref in refs):
        refs.append({"source_id": source_ref, "locator": "source"})
    if not refs:
        raise MemoryAssetPackageImportError(
            f"memory {memory.get('memory_id')} requires source refs"
        )
    return refs


def _l4_source_identity(
    memory: dict[str, object],
) -> tuple[str, list[dict[str, str]]]:
    source_ref = str(memory.get("source_ref", "")).strip()
    evidence_refs = memory.get("evidence_refs")
    values = (
        evidence_refs
        if isinstance(evidence_refs, list)
        else []
    )
    source_id = source_ref
    locator = "source"
    for value in values:
        if not isinstance(value, str) or "#" not in value:
            continue
        candidate_source, candidate_locator = value.split("#", 1)
        if candidate_source:
            source_id = candidate_source
            locator = candidate_locator or "source"
            break
    if not source_id:
        raise MemoryAssetPackageImportError(
            f"L4 memory {memory.get('memory_id')} requires a source reference"
        )
    return source_id, [{"source_id": source_id, "locator": locator}]


def _same_memory(existing: dict[str, object], incoming: dict[str, object]) -> bool:
    return all(existing.get(field) == incoming.get(field) for field in _COMPARISON_FIELDS)


def _memory_required_source_id(memory: dict[str, object]) -> str:
    source_ref = str(memory.get("source_ref", "")).strip()
    if source_ref:
        return source_ref
    evidence_refs = memory.get("evidence_refs")
    if isinstance(evidence_refs, list):
        for value in evidence_refs:
            if isinstance(value, str) and "#" in value:
                source_id = value.split("#", 1)[0].strip()
                if source_id:
                    return source_id
    return ""


def _same_source(
    existing: dict[str, object],
    incoming: dict[str, object],
) -> bool:
    identity_fields = (
        "id",
        "type",
        "title",
        "storage_uri",
        "media_type",
    )
    if not all(existing.get(field) == incoming.get(field) for field in identity_fields):
        return False
    existing_metadata = existing.get("metadata")
    incoming_metadata = incoming.get("metadata")
    if not isinstance(existing_metadata, dict) or not isinstance(incoming_metadata, dict):
        return existing == incoming
    # A repeated v1.2 import compares the same source identity after its
    # reference-only record was promoted by a verified original-file restore.
    return (
        existing_metadata.get("is_audio_visual")
        == incoming_metadata.get("is_audio_visual")
        and (
            existing_metadata.get("roundtrip_reference_only") is True
            or existing_metadata.get("raw_content_restored") is True
        )
    )


def _source_conflict_id(
    source_id: str,
    existing: dict[str, object],
    incoming: dict[str, object],
) -> str:
    payload = json.dumps(
        {
            "source_id": source_id,
            "existing": existing,
            "incoming": incoming,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"conflict-source-roundtrip-{hashlib.sha256(payload).hexdigest()[:24]}"


def _conflict_id(
    candidate_id: str,
    existing: dict[str, object],
    incoming: dict[str, object],
) -> str:
    payload = json.dumps(
        {
            "candidate_id": candidate_id,
            "existing": {field: existing.get(field) for field in _COMPARISON_FIELDS},
            "incoming": {field: incoming.get(field) for field in _COMPARISON_FIELDS},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"conflict-roundtrip-{hashlib.sha256(payload).hexdigest()[:24]}"
