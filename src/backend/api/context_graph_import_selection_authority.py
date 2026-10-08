"""Core-owned, opaque authorization for LineMap graph-file import selections.

The renderer only ever receives an opaque selection receipt.  The selected
Vault path remains process-local and is disclosed solely to the import
composition after a one-time, scope-checked consume.
"""

from __future__ import annotations

import secrets
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from backend.api.context_graph_import_composition import (
    ContextGraphFileSelection,
    ContextGraphFileSelectionError,
)
from backend.security import DesktopFileGrant
from core.product_core.ports import ObjectStorePort
from core.storage_provider import (
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)


_SELECTIONS = "context_graph_import_selections"
_COMMANDS = "context_graph_import_selection_commands"
_SCHEMA_VERSION = "1.0.0"
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:-]{0,255}$")
_SOURCE_TYPE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_REPARSE_POINT = 0x0400


class ContextGraphImportSelectionError(ContextGraphFileSelectionError):
    """Raised when an opaque import selection is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class ContextGraphImportSelectionReceipt:
    """Safe renderer DTO.  It intentionally contains no local file path."""

    selection_id: str
    project_id: str
    display_name: str
    source_type: str
    expires_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "selection_id": self.selection_id,
            "project_id": self.project_id,
            "display_name": self.display_name,
            "source_type": self.source_type,
            "expires_at": self.expires_at,
        }


class ContextGraphImportSelectionAuthority:
    """Persist, scope and consume one Vault-original selection exactly once."""

    def __init__(
        self,
        *,
        records: SQLiteStructuredRecordStore,
        object_store: ObjectStorePort,
        managed_assets_root: Path,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=10),
        selection_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise ContextGraphImportSelectionError("context_graph_import_selection_store_invalid")
        if not isinstance(managed_assets_root, Path):
            raise ContextGraphImportSelectionError("context_graph_import_selection_root_invalid")
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ContextGraphImportSelectionError("context_graph_import_selection_ttl_invalid")
        if not callable(getattr(object_store, "read", None)):
            raise ContextGraphImportSelectionError("context_graph_import_selection_object_store_invalid")
        if now is not None and not callable(now):
            raise ContextGraphImportSelectionError("context_graph_import_selection_clock_invalid")
        if selection_id_factory is not None and not callable(selection_id_factory):
            raise ContextGraphImportSelectionError("context_graph_import_selection_id_factory_invalid")
        self._records = records
        self._object_store = object_store
        self._assets_root = managed_assets_root.expanduser().resolve(strict=False)
        self._now = now or (lambda: datetime.now(UTC))
        self._ttl = ttl
        self._selection_id_factory = selection_id_factory or _new_selection_id

    def create(
        self,
        command_id: str,
        project_id: str,
        source_type: str,
        asset_id: str,
        actor_id: str,
        session_instance_id: str,
        *,
        file_grant: DesktopFileGrant,
    ) -> ContextGraphImportSelectionReceipt:
        """Authorize one existing managed original asset for a graph import."""

        _validate_file_grant(file_grant, session_instance_id=session_instance_id)
        identity = _identity(
            command_id=command_id,
            project_id=project_id,
            source_type=source_type,
            asset_id=asset_id,
            actor_id=actor_id,
            session_instance_id=session_instance_id,
            file_grant_id=file_grant.grant_id,
            file_grant_sha256=file_grant.sha256,
        )
        try:
            with self._records.begin() as uow:
                command = uow.read(_COMMANDS, command_id)
                if command is not None:
                    selection_id = _command_selection_id(command.payload, identity)
                    selection = uow.read(_SELECTIONS, selection_id)
                    if selection is None:
                        raise ContextGraphImportSelectionError("context_graph_import_selection_command_drift")
                    result = _receipt(_selection_payload(selection.payload))
                    uow.commit()
                    return result

                asset, path, source_revision = self._resolve_asset(
                    asset_id,
                    expected_sha256=file_grant.sha256,
                    expected_size=file_grant.size_bytes,
                )
                selection_id = self._new_unique_selection_id(uow)
                created_at = _timestamp(self._now())
                expires_at = _timestamp(_parse_timestamp(created_at) + self._ttl)
                payload = {
                    "schema_version": _SCHEMA_VERSION,
                    **identity,
                    "selection_id": selection_id,
                    "display_name": _display_name(asset),
                    "source_revision": source_revision,
                    "created_at": created_at,
                    "expires_at": expires_at,
                    "consumed_at": None,
                }
                # ``path`` is deliberately not persisted: the authority
                # deterministically resolves the asset again at consumption.
                del path
                uow.put(_SELECTIONS, selection_id, payload, expected_revision=0)
                uow.put(
                    _COMMANDS,
                    command_id,
                    {"schema_version": _SCHEMA_VERSION, **identity, "selection_id": selection_id},
                    expected_revision=0,
                )
                uow.commit()
                return _receipt(payload)
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise ContextGraphImportSelectionError("context_graph_import_selection_write_conflict") from error

    def consume(
        self,
        selection_id: str,
        *,
        project_id: str,
        actor_id: str,
        source_type: str,
        session_instance_id: str,
    ) -> ContextGraphFileSelection:
        """CAS-consume the selection and return the private import input."""

        _required_id("selection_id", selection_id)
        _required_id("project_id", project_id)
        _required_id("actor_id", actor_id)
        _required_id("source_type", source_type)
        _required_id("session_instance_id", session_instance_id)
        try:
            with self._records.begin() as uow:
                record = uow.read(_SELECTIONS, selection_id)
                if record is None:
                    raise ContextGraphImportSelectionError("context_graph_import_selection_unavailable")
                payload = _selection_payload(record.payload)
                if payload["project_id"] != project_id or payload["actor_id"] != actor_id:
                    raise ContextGraphImportSelectionError("context_graph_import_selection_scope_mismatch")
                if payload["source_type"] != source_type:
                    raise ContextGraphImportSelectionError("context_graph_import_selection_source_type_mismatch")
                if payload["session_instance_id"] != session_instance_id:
                    raise ContextGraphImportSelectionError("context_graph_import_selection_session_mismatch")
                if payload["consumed_at"] is not None:
                    raise ContextGraphImportSelectionError("context_graph_import_selection_already_consumed")
                if _parse_timestamp(payload["expires_at"]) <= self._now().astimezone(UTC):
                    raise ContextGraphImportSelectionError("context_graph_import_selection_expired")
                _, path, source_revision = self._resolve_asset(
                    str(payload["asset_id"]),
                    expected_sha256=str(payload["file_grant_sha256"]),
                )
                if source_revision != payload["source_revision"]:
                    raise ContextGraphImportSelectionError("context_graph_import_selection_revision_drift")
                updated = dict(payload)
                updated["consumed_at"] = _timestamp(self._now())
                uow.put(_SELECTIONS, selection_id, updated, expected_revision=record.revision)
                uow.commit()
                return ContextGraphFileSelection(
                    selection_id=selection_id,
                    project_id=project_id,
                    selected_path=path,
                    source_revision=source_revision,
                    selection_evidence_ref=f"context-import-selection:{selection_id}",
                )
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise ContextGraphImportSelectionError("context_graph_import_selection_consume_conflict") from error

    def _resolve_asset(
        self,
        asset_id: str,
        *,
        expected_sha256: str,
        expected_size: int | None = None,
    ) -> tuple[Mapping[str, object], Path, str]:
        _required_id("asset_id", asset_id)
        asset = self._object_store.read("workbench_original_assets", asset_id)
        if not isinstance(asset, Mapping) or asset.get("id") != asset_id:
            raise ContextGraphImportSelectionError("context_graph_import_asset_unavailable")
        vault_ref = asset.get("vault_ref")
        size = asset.get("byte_count")
        sha256 = asset.get("sha256")
        if (
            not isinstance(vault_ref, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise ContextGraphImportSelectionError("context_graph_import_asset_record_invalid")
        if sha256 != expected_sha256 or (
            expected_size is not None and size != expected_size
        ):
            raise ContextGraphImportSelectionError("context_graph_import_asset_grant_mismatch")
        path = _managed_original_path(self._assets_root, vault_ref)
        stat = _regular_file_stat(path, self._assets_root)
        if stat.st_size != size:
            raise ContextGraphImportSelectionError("context_graph_import_asset_size_drift")
        return asset, path, _source_revision(stat)

    def _new_unique_selection_id(self, uow) -> str:
        for _ in range(8):
            selection_id = self._selection_id_factory()
            _required_id("selection_id", selection_id)
            if uow.read(_SELECTIONS, selection_id) is None:
                return selection_id
        raise ContextGraphImportSelectionError("context_graph_import_selection_id_collision")


def _identity(**values: str) -> dict[str, str]:
    for label, value in values.items():
        _required_id(label, value)
    return dict(values)


def _selection_payload(value: object) -> dict[str, object]:
    fields = {
        "schema_version", "command_id", "project_id", "source_type", "asset_id", "actor_id",
        "session_instance_id", "file_grant_id", "file_grant_sha256", "selection_id",
        "display_name", "source_revision", "created_at", "expires_at", "consumed_at",
    }
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != _SCHEMA_VERSION:
        raise ContextGraphImportSelectionError("context_graph_import_selection_payload_invalid")
    result = dict(value)
    for key in fields - {"schema_version", "consumed_at"}:
        if not isinstance(result.get(key), str) or not str(result[key]).strip():
            raise ContextGraphImportSelectionError("context_graph_import_selection_payload_invalid")
    if result["consumed_at"] is not None and not isinstance(result["consumed_at"], str):
        raise ContextGraphImportSelectionError("context_graph_import_selection_payload_invalid")
    _parse_timestamp(str(result["created_at"]))
    _parse_timestamp(str(result["expires_at"]))
    return result


def _command_selection_id(value: object, identity: Mapping[str, str]) -> str:
    fields = {"schema_version", *identity.keys(), "selection_id"}
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != _SCHEMA_VERSION:
        raise ContextGraphImportSelectionError("context_graph_import_selection_command_drift")
    if any(value.get(key) != expected for key, expected in identity.items()):
        raise ContextGraphImportSelectionError("context_graph_import_selection_command_drift")
    selection_id = value.get("selection_id")
    _required_id("selection_id", selection_id)
    return selection_id


def _receipt(payload: Mapping[str, object]) -> ContextGraphImportSelectionReceipt:
    return ContextGraphImportSelectionReceipt(
        selection_id=str(payload["selection_id"]),
        project_id=str(payload["project_id"]),
        display_name=str(payload["display_name"]),
        source_type=str(payload["source_type"]),
        expires_at=str(payload["expires_at"]),
    )


def _managed_original_path(root: Path, vault_ref: str) -> Path:
    if vault_ref.startswith("/") or "\\" in vault_ref:
        raise ContextGraphImportSelectionError("context_graph_import_asset_path_invalid")
    relative = PurePosixPath(vault_ref)
    if relative.parts[:2] != ("assets", "originals") or len(relative.parts) < 3 or any(part in {"", ".", ".."} for part in relative.parts):
        raise ContextGraphImportSelectionError("context_graph_import_asset_path_invalid")
    path = (root / Path(*relative.parts[2:])).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ContextGraphImportSelectionError("context_graph_import_asset_path_escape") from error
    return path


def _regular_file_stat(path: Path, root: Path):
    if not path.is_file() or path.is_symlink():
        raise ContextGraphImportSelectionError("context_graph_import_asset_file_unavailable")
    current = path.parent
    while True:
        if current.is_symlink() or bool(getattr(current.lstat(), "st_file_attributes", 0) & _REPARSE_POINT):
            raise ContextGraphImportSelectionError("context_graph_import_asset_path_escape")
        if current == root:
            break
        if current.parent == current:
            raise ContextGraphImportSelectionError("context_graph_import_asset_path_escape")
        current = current.parent
    stat = path.lstat()
    if bool(getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT):
        raise ContextGraphImportSelectionError("context_graph_import_asset_file_unavailable")
    return stat


def _source_revision(stat) -> str:
    return f"mtime-{stat.st_mtime_ns}:size-{stat.st_size}"


def _display_name(asset: Mapping[str, object]) -> str:
    value = asset.get("display_name")
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ContextGraphImportSelectionError("context_graph_import_asset_display_name_invalid")
    return value


def _validate_file_grant(
    value: object, *, session_instance_id: str,
) -> None:
    if (
        not isinstance(value, DesktopFileGrant)
        or value.session_instance_id != session_instance_id
        or value.source_kind != "file"
        or re.fullmatch(r"[0-9a-f]{64}", value.sha256) is None
        or type(value.size_bytes) is not int
        or value.size_bytes < 0
    ):
        raise ContextGraphImportSelectionError("context_graph_import_file_grant_invalid")


def _required_id(label: str, value: object) -> None:
    pattern = _SOURCE_TYPE if label == "source_type" else _OPAQUE_ID
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContextGraphImportSelectionError(f"context_graph_import_selection_{label}_invalid")


def _new_selection_id() -> str:
    return f"ctxsel-{secrets.token_urlsafe(32)}"


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ContextGraphImportSelectionError("context_graph_import_selection_clock_invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ContextGraphImportSelectionError("context_graph_import_selection_timestamp_invalid") from error
    if parsed.tzinfo is None:
        raise ContextGraphImportSelectionError("context_graph_import_selection_timestamp_invalid")
    return parsed.astimezone(UTC)
