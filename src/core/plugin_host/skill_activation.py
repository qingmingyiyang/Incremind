from __future__ import annotations

import base64
import os
import re
import shutil
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from threading import RLock
from uuid import uuid4

from core.application_skill import (
    ApplicationSkillCatalog,
    ApplicationSkillError,
    ApplicationSkillSource,
)
from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
)

from .package_intake import PluginPackageIntakeConflict, PluginPackageIntakeError


_RAW = "plugin_raw_packages"
_REPORTS = "plugin_compatibility_reports"
_STATES = "plugin_package_states"
_REVIEWS = "plugin_skill_reviews"
_ACTIVATIONS = "plugin_skill_activations"
_COMMANDS = "plugin_skill_activation_commands"
_SKILL_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_ACTIVATION_LOCK = RLock()


class PluginSkillActivation:
    """Review and expose captured Plugin Skills through the existing Skill runtime."""

    def __init__(self, records: SQLiteStructuredRecordStore, *, managed_root: Path, now: str) -> None:
        self._records = records
        configured_root = managed_root.expanduser()
        _reject_reparse_path(configured_root)
        self._managed_root = configured_root.resolve(strict=False)
        self._now = _text(now, "now", 96)

    def review(
        self,
        plugin_id: str,
        *,
        skill_ids: Sequence[str],
        expected_state_revision: int,
        command_id: str,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        identity = _text(plugin_id, "plugin_id", 64)
        skills = _skill_ids(skill_ids)
        command = _command_id(command_id)
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin Skill review requires explicit confirmation")
        review_reason = _text(reason, "reason", 500)
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                if replay is not None:
                    return _replay(replay, "review", identity, skills)
                state = _required(uow.read(_STATES, identity), "package state")
                if state.revision != _revision(expected_state_revision, "expected_state_revision", allow_zero=False):
                    raise PluginPackageIntakeConflict("Plugin package state revision conflict")
                if state.payload.get("status") != "installed_disabled" or state.payload.get("enabled") is not False:
                    raise PluginPackageIntakeError("Plugin package must be installed disabled before review")
                package_record_id = _payload_text(state.payload, "package_record_id")
                report = _required(uow.read(_REPORTS, package_record_id), "compatibility report")
                raw = _required(uow.read(_RAW, package_record_id), "raw package")
                if report.payload.get("compatible") is not True:
                    raise PluginPackageIntakeError("quarantined Plugin package cannot be reviewed")
                available = _captured_skill_ids(raw.payload)
                if any(skill not in available for skill in skills):
                    raise PluginPackageIntakeError("reviewed Plugin Skill is not present in captured raw")
                review_id = package_record_id
                payload = {
                    "schema_version": "1.0.0",
                    "plugin_id": identity,
                    "package_record_id": package_record_id,
                    "skill_ids": list(skills),
                    "decision": "approved_disabled",
                    "reviewed_by": "local-user",
                    "reviewed_at": self._now,
                    "reason": review_reason,
                }
                current = uow.read(_REVIEWS, review_id)
                if current is None:
                    reviewed = uow.put(_REVIEWS, review_id, payload, expected_revision=0)
                elif dict(current.payload) == payload:
                    reviewed = current
                else:
                    raise PluginPackageIntakeConflict("Plugin Skill review identity drifted")
                result = _review_result(reviewed, replayed=False)
                uow.put(
                    _COMMANDS, command,
                    {"operation": "review", "plugin_id": identity, "skill_ids": list(skills), "result": result},
                    expected_revision=0,
                )
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def activate(
        self,
        plugin_id: str,
        *,
        expected_review_revision: int,
        expected_activation_revision: int,
        command_id: str,
        confirm: bool,
    ) -> dict[str, object]:
        identity = _text(plugin_id, "plugin_id", 64)
        command = _command_id(command_id)
        expected_review_revision = _revision(expected_review_revision, "expected_review_revision", allow_zero=False)
        expected_activation_revision = _revision(expected_activation_revision, "expected_activation_revision")
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin Skill activation requires explicit confirmation")
        with _ACTIVATION_LOCK:
            return self._activate_locked(
                identity,
                expected_review_revision=expected_review_revision,
                expected_activation_revision=expected_activation_revision,
                command=command,
            )

    def _activate_locked(
        self,
        identity: str,
        *,
        expected_review_revision: int,
        expected_activation_revision: int,
        command: str,
    ) -> dict[str, object]:
        state = _required(self._records.read(_STATES, identity), "package state")
        if state.payload.get("status") != "installed_disabled" or state.payload.get("enabled") is not False:
            raise PluginPackageIntakeError("Plugin package must remain installed disabled during activation")
        package_record_id = _payload_text(state.payload, "package_record_id")
        report = _required(self._records.read(_REPORTS, package_record_id), "compatibility report")
        if report.payload.get("compatible") is not True:
            raise PluginPackageIntakeError("quarantined Plugin package cannot be activated")
        review = _required(self._records.read(_REVIEWS, package_record_id), "Skill review")
        if review.payload.get("plugin_id") != identity or review.payload.get("package_record_id") != package_record_id:
            raise PluginPackageIntakeConflict("Plugin Skill review identity drifted")
        if review.revision != expected_review_revision:
            raise PluginPackageIntakeConflict("Plugin Skill review revision conflict")
        skills = _skill_ids(review.payload.get("skill_ids"))
        replay = self._records.read(_COMMANDS, command)
        if replay is not None:
            return _replay(replay, "activate", identity, skills)
        current_activation = self._records.read(_ACTIVATIONS, identity)
        current_revision = current_activation.revision if current_activation is not None else 0
        if current_revision != expected_activation_revision:
            raise PluginPackageIntakeConflict("Plugin Skill activation revision conflict")
        raw = _required(self._records.read(_RAW, package_record_id), "raw package")
        packages = self._materialize(package_record_id, raw.payload, skills)
        activation = {
            "schema_version": "1.0.0",
            "plugin_id": identity,
            "package_record_id": package_record_id,
            "review_revision": review.revision,
            "skill_ids": list(skills),
            "skill_fingerprints": {package.skill_id: package.fingerprint for package in packages},
            "status": "active",
            "activated_at": self._now,
            "disabled_at": None,
        }
        try:
            with self._records.begin() as uow:
                current_command = uow.read(_COMMANDS, command)
                if current_command is not None:
                    return _replay(current_command, "activate", identity, skills)
                current = uow.read(_ACTIVATIONS, identity)
                if (current.revision if current is not None else 0) != expected_activation_revision:
                    raise PluginPackageIntakeConflict("Plugin Skill activation revision conflict")
                if current is None:
                    saved = uow.put(_ACTIVATIONS, identity, activation, expected_revision=0)
                elif current.payload.get("status") == "active" and dict(current.payload) == activation:
                    saved = current
                elif current.payload.get("package_record_id") == package_record_id:
                    saved = uow.put(_ACTIVATIONS, identity, activation, expected_revision=current.revision)
                else:
                    raise PluginPackageIntakeConflict("Plugin Skill upgrade requires a future Gate")
                result = _activation_result(saved, replayed=False)
                uow.put(
                    _COMMANDS, command,
                    {"operation": "activate", "plugin_id": identity, "skill_ids": list(skills), "result": result},
                    expected_revision=0,
                )
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def disable(
        self,
        plugin_id: str,
        *,
        expected_activation_revision: int,
        command_id: str,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        identity = _text(plugin_id, "plugin_id", 64)
        command = _command_id(command_id)
        expected_activation_revision = _revision(expected_activation_revision, "expected_activation_revision", allow_zero=False)
        if confirm is not True:
            raise PluginPackageIntakeError("Plugin Skill disable requires explicit confirmation")
        disable_reason = _text(reason, "reason", 500)
        with _ACTIVATION_LOCK:
            return self._disable_locked(
                identity,
                expected_activation_revision=expected_activation_revision,
                command=command,
                disable_reason=disable_reason,
            )

    def _disable_locked(
        self,
        identity: str,
        *,
        expected_activation_revision: int,
        command: str,
        disable_reason: str,
    ) -> dict[str, object]:
        try:
            with self._records.begin() as uow:
                replay = uow.read(_COMMANDS, command)
                current = _required(uow.read(_ACTIVATIONS, identity), "Skill activation")
                skills = _skill_ids(current.payload.get("skill_ids"))
                if replay is not None:
                    return _replay(replay, "disable", identity, skills)
                if current.revision != expected_activation_revision:
                    raise PluginPackageIntakeConflict("Plugin Skill activation revision conflict")
                disabled = dict(current.payload)
                disabled.update({"status": "disabled", "disabled_at": self._now, "disable_reason": disable_reason})
                saved = uow.put(_ACTIVATIONS, identity, disabled, expected_revision=current.revision)
                result = _activation_result(saved, replayed=False)
                uow.put(
                    _COMMANDS, command,
                    {"operation": "disable", "plugin_id": identity, "skill_ids": list(skills), "result": result},
                    expected_revision=0,
                )
                uow.commit()
                return result
        except SQLiteUnitOfWorkConflict as error:
            raise PluginPackageIntakeConflict(str(error)) from error

    def active_sources(self, plugin_ids: Sequence[str]) -> tuple[ApplicationSkillSource, ...]:
        _reject_reparse_path(self._managed_root)
        allowed = frozenset(_text(item, "plugin_id", 64) for item in plugin_ids)
        sources: list[ApplicationSkillSource] = []
        for record in self._records.list(_ACTIVATIONS):
            if record.object_id not in allowed or record.payload.get("status") != "active":
                continue
            package_record_id = _payload_text(record.payload, "package_record_id")
            root = self._managed_root / package_record_id
            if not root.is_dir() or root.is_symlink():
                continue
            try:
                raw = _required(self._records.read(_RAW, package_record_id), "raw package")
                skills = _skill_ids(record.payload.get("skill_ids"))
                expected = _expected_skill_files(raw.payload, skills)
                packages = _validate_materialized(root, skills, expected)
                fingerprints = record.payload.get("skill_fingerprints")
                if not isinstance(fingerprints, Mapping) or {
                    package.skill_id: package.fingerprint for package in packages
                } != dict(fingerprints):
                    continue
            except (PluginPackageIntakeError, ApplicationSkillError, OSError):
                continue
            sources.append(ApplicationSkillSource(record.object_id, root, "plugin"))
        return tuple(sorted(sources, key=lambda item: item.source_id))

    def all_active_sources(self) -> tuple[ApplicationSkillSource, ...]:
        return self.active_sources(tuple(record.object_id for record in self._records.list(_ACTIVATIONS)))

    def reviewed_skill_ids(self, plugin_ids: Sequence[str]) -> tuple[str, ...]:
        allowed = frozenset(_text(item, "plugin_id", 64) for item in plugin_ids)
        claimed: set[str] = set()
        for review in self._records.list(_REVIEWS):
            if review.payload.get("plugin_id") in allowed:
                claimed.update(_skill_ids(review.payload.get("skill_ids")))
        return tuple(sorted(claimed))

    def _materialize(self, package_record_id: str, raw: Mapping[str, object], skills: tuple[str, ...]):
        _reject_reparse_path(self._managed_root)
        expected = _expected_skill_files(raw, skills)
        target = self._managed_root / package_record_id
        if target.exists():
            return _validate_materialized(target, skills, expected)
        staging_root = self._managed_root / ".staging"
        staging = staging_root / f"activate-{uuid4().hex}"
        try:
            staging.mkdir(parents=True, exist_ok=False)
            _reject_reparse_path(staging)
            package_stage = staging / package_record_id
            package_stage.mkdir()
            for relative, content in sorted(expected.items()):
                output = package_stage.joinpath(*PurePosixPath(relative).parts[1:])
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            packages = _validate_materialized(package_stage, skills, expected)
            self._managed_root.mkdir(parents=True, exist_ok=True)
            os.replace(package_stage, target)
            return packages
        except (ApplicationSkillError, OSError) as error:
            raise PluginPackageIntakeError("Plugin Skill materialization failed") from error
        finally:
            if staging.exists() and staging.is_dir() and staging.parent == staging_root:
                shutil.rmtree(staging, ignore_errors=True)


def _expected_skill_files(raw: Mapping[str, object], skills: tuple[str, ...]) -> dict[str, bytes]:
        files = raw.get("files")
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            raise PluginPackageIntakeError("Plugin raw files are invalid")
        expected: dict[str, bytes] = {}
        for item in files:
            if not isinstance(item, Mapping):
                raise PluginPackageIntakeError("Plugin raw file is invalid")
            relative = _safe_relative(item.get("relative_path"))
            parts = PurePosixPath(relative).parts
            if len(parts) < 3 or parts[0] != "skills" or parts[1] not in skills:
                continue
            try:
                content = base64.b64decode(_text(item.get("content_base64"), "content_base64", 6_000_000), validate=True)
            except Exception as error:
                raise PluginPackageIntakeError("Plugin raw content is invalid") from error
            if len(content) != item.get("size_bytes") or relative in expected:
                raise PluginPackageIntakeError("Plugin raw content identity drifted")
            expected[relative] = content
        return expected


def _validate_materialized(root: Path, skills: tuple[str, ...], expected: Mapping[str, bytes]):
    actual: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PluginPackageIntakeError("materialized Plugin Skill cannot contain links")
        if path.is_file():
            relative = "skills/" + path.relative_to(root).as_posix()
            actual[relative] = path.read_bytes()
    if actual != dict(expected):
        raise PluginPackageIntakeConflict("materialized Plugin Skill bytes drifted")
    catalog = ApplicationSkillCatalog()
    packages = tuple(catalog.inspect_package(root / skill, source_id="plugin-review") for skill in skills)
    return packages


def _captured_skill_ids(raw: Mapping[str, object]) -> tuple[str, ...]:
    files = raw.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise PluginPackageIntakeError("Plugin raw files are invalid")
    found = {
        PurePosixPath(str(item.get("relative_path"))).parts[1]
        for item in files if isinstance(item, Mapping)
        and len(PurePosixPath(str(item.get("relative_path"))).parts) >= 3
        and PurePosixPath(str(item.get("relative_path"))).parts[0] == "skills"
        and PurePosixPath(str(item.get("relative_path"))).parts[2] == "SKILL.md"
    }
    return tuple(sorted(skill for skill in found if _SKILL_ID.fullmatch(skill)))


def _safe_relative(value: object) -> str:
    relative = _text(value, "relative_path", 240)
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise PluginPackageIntakeError("Plugin Skill relative path is unsafe")
    for part in path.parts:
        if not part or part in {".", ".."} or part.rstrip(" .") != part or part.upper() in _WINDOWS_RESERVED:
            raise PluginPackageIntakeError("Plugin Skill relative path is unsafe")
    return relative


def _skill_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not 1 <= len(value) <= 32:
        raise PluginPackageIntakeError("skill_ids must be a non-empty array")
    skills = tuple(sorted(set(value)))
    if len(skills) != len(value) or any(not isinstance(item, str) or not _SKILL_ID.fullmatch(item) for item in skills):
        raise PluginPackageIntakeError("skill_ids are invalid")
    return skills


def _required(record: SQLiteStructuredRecord | None, label: str) -> SQLiteStructuredRecord:
    if record is None:
        raise PluginPackageIntakeError(f"Plugin {label} is missing")
    return record


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    return _text(payload.get(key), key, 160)


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PluginPackageIntakeError(f"{label} is invalid")
    return value.strip()


def _command_id(value: object) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise PluginPackageIntakeError("command_id is invalid")
    return value


def _revision(value: object, label: str, *, allow_zero: bool = True) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise PluginPackageIntakeError(f"{label} is invalid")
    return value


def _reject_reparse_path(path: Path) -> None:
    current = path
    existing: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for candidate in existing:
        try:
            info = candidate.lstat()
        except OSError as error:
            raise PluginPackageIntakeError("Plugin Skill managed path is unavailable") from error
        if candidate.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & reparse_flag):
            raise PluginPackageIntakeError("Plugin Skill managed path cannot cross a reparse point")


def _review_result(record: SQLiteStructuredRecord, *, replayed: bool) -> dict[str, object]:
    return {"review": dict(record.payload), "review_revision": record.revision, "replayed": replayed}


def _activation_result(record: SQLiteStructuredRecord, *, replayed: bool) -> dict[str, object]:
    return {"activation": dict(record.payload), "activation_revision": record.revision, "replayed": replayed}


def _replay(command: SQLiteStructuredRecord, operation: str, plugin_id: str, skills: tuple[str, ...]):
    payload = command.payload
    if payload.get("operation") != operation or payload.get("plugin_id") != plugin_id or payload.get("skill_ids") != list(skills):
        raise PluginPackageIntakeConflict("Plugin Skill command identity drifted")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise PluginPackageIntakeConflict("Plugin Skill command result is invalid")
    return dict(result) | {"replayed": True}
