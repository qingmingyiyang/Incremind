from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import Lock, RLock

from backend.shared.filesystem import atomic_write_text
from backend.shared.interprocess_lock import interprocess_file_lock
from core.ai_boundary import BoundaryGrant, ProjectBoundaryProfile
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence


_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SCHEMA_VERSION = "1.0.0"
_PROFILE_FIELDS = frozenset({
    "schema_version",
    "profile_id",
    "project_id",
    "mode",
    "revision",
    "remote_default",
    "enabled_sources",
    "denied_effects",
    "persistent_grants",
})
_GRANT_FIELDS = frozenset({
    "grant_id",
    "subject_id",
    "project_id",
    "target_id",
    "actions",
    "data_classes",
    "destinations",
    "expires_at",
    "revision",
    "revoked",
    "redaction_required",
})
_SENSITIVE_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "password",
    "secret",
    "token",
    "local_path",
    "windows_path",
})
_PATH_LOCKS: dict[Path, RLock] = {}
_PATH_LOCKS_GUARD = Lock()


class ProjectBoundaryProfileError(ValueError):
    pass


class ProjectBoundaryProfileConflict(ProjectBoundaryProfileError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectBoundaryProfileSnapshot:
    profile: ProjectBoundaryProfile
    store_revision: int
    persisted: bool


class ProjectBoundaryProfileStore:
    """Versioned non-secret authority for one project's Boundary profile."""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir.resolve()

    def get(self, project_id: str) -> ProjectBoundaryProfileSnapshot:
        project = _project_id(project_id)
        path = self._path(project)
        # Atomic replacement makes an ordinary read coherent; only
        # locked_snapshot holds the cross-process lock across a later action.
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path):
            if not path.exists():
                return ProjectBoundaryProfileSnapshot(_default_profile(project), 0, False)
            profile = _decode_profile(_read_json(path), expected_project_id=project)
            return ProjectBoundaryProfileSnapshot(profile, profile.revision, True)

    @contextmanager
    def locked_snapshot(self, project_id: str) -> Iterator[ProjectBoundaryProfileSnapshot]:
        """Hold the profile authority lock across one policy decision and action."""
        project = _project_id(project_id)
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            if not path.exists():
                yield ProjectBoundaryProfileSnapshot(
                    profile=_default_profile(project),
                    store_revision=0,
                    persisted=False,
                )
            else:
                profile = _decode_profile(_read_json(path), expected_project_id=project)
                yield ProjectBoundaryProfileSnapshot(
                    profile=profile,
                    store_revision=profile.revision,
                    persisted=True,
                )

    def update(
        self,
        project_id: str,
        *,
        mode: str,
        remote_default: str,
        enabled_sources: tuple[str, ...] = (),
        denied_effects: tuple[str, ...] = (),
        persistent_grants: tuple[BoundaryGrant, ...] = (),
        expected_revision: int,
    ) -> ProjectBoundaryProfileSnapshot:
        project = _project_id(project_id)
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current_revision = 0
            if path.exists():
                current = _decode_profile(_read_json(path), expected_project_id=project)
                current_revision = current.revision
            if expected_revision != current_revision:
                raise ProjectBoundaryProfileConflict(
                    f"project boundary profile revision conflict: expected {expected_revision}, current {current_revision}"
                )
            profile = ProjectBoundaryProfile(
                profile_id=f"project-boundary-{project}",
                project_id=project,
                mode=mode,  # type: ignore[arg-type]
                revision=current_revision + 1,
                remote_default=remote_default,  # type: ignore[arg-type]
                enabled_sources=enabled_sources,
                denied_effects=denied_effects,  # type: ignore[arg-type]
                persistent_grants=persistent_grants,
            )
            encoded = _encode_profile(profile)
            _reject_sensitive_material(encoded)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(encoded, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            return ProjectBoundaryProfileSnapshot(
                profile=profile,
                store_revision=profile.revision,
                persisted=True,
            )

    def set_mode(
        self, project_id: str, *, mode: str, remote_default: str, expected_revision: int,
    ) -> ProjectBoundaryProfileSnapshot:
        """Narrow CAS update used by the Boundary mode command.

        Grants, source selection and deny rules are deliberately copied from
        the current authority; this command cannot mint or weaken them.
        """
        project = _project_id(project_id)
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current = _default_profile(project) if not path.exists() else _decode_profile(_read_json(path), expected_project_id=project)
            current_revision = current.revision
            if expected_revision != current_revision:
                raise ProjectBoundaryProfileConflict(f"project boundary profile revision conflict: expected {expected_revision}, current {current_revision}")
            profile = ProjectBoundaryProfile(
                profile_id=current.profile_id, project_id=project, mode=mode,  # type: ignore[arg-type]
                revision=current.revision + 1, remote_default=remote_default,  # type: ignore[arg-type]
                enabled_sources=current.enabled_sources, denied_effects=current.denied_effects,
                persistent_grants=current.persistent_grants,
            )
            encoded = _encode_profile(profile)
            _reject_sensitive_material(encoded)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(encoded, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            return ProjectBoundaryProfileSnapshot(profile, profile.revision, True)

    def create_grant(
        self, project_id: str, *, grant: BoundaryGrant, expected_revision: int,
    ) -> ProjectBoundaryProfileSnapshot:
        """Append one service-derived grant without changing other policy."""
        project = _project_id(project_id)
        expected_revision = _positive_int(expected_revision, "expected profile revision")
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current = _default_profile(project) if not path.exists() else _decode_profile(_read_json(path), expected_project_id=project)
            if expected_revision != current.revision:
                raise ProjectBoundaryProfileConflict(f"project boundary profile revision conflict: expected {expected_revision}, current {current.revision}")
            if grant.project_id != project:
                raise ProjectBoundaryProfileError("grant project identity drifted")
            if (
                grant.subject_id != "ai-kernel"
                or not isinstance(grant.revision, int)
                or isinstance(grant.revision, bool)
                or grant.revision != 1
                or grant.revoked is not False
            ):
                raise ProjectBoundaryProfileError("new grant authority is invalid")
            if any(item.grant_id == grant.grant_id for item in current.persistent_grants):
                raise ProjectBoundaryProfileConflict("grant identity already exists")
            now = datetime.now(timezone.utc)
            if any(item.target_id == grant.target_id and item.is_active_at(now) for item in current.persistent_grants):
                raise ProjectBoundaryProfileConflict("an active grant already exists for this target")
            profile = replace(current, revision=current.revision + 1, persistent_grants=current.persistent_grants + (grant,))
            encoded = _encode_profile(profile)
            _reject_sensitive_material(encoded)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(encoded, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            return ProjectBoundaryProfileSnapshot(profile, profile.revision, True)

    def revoke_grant(
        self, project_id: str, *, grant_id: str, expected_revision: int, expected_grant_revision: int,
    ) -> ProjectBoundaryProfileSnapshot:
        """Revoke one exact grant, preserving every other profile field."""
        project = _project_id(project_id)
        expected_revision = _positive_int(expected_revision, "expected profile revision")
        expected_grant_revision = _positive_int(expected_grant_revision, "expected grant revision")
        path = self._path(project)
        with model_dispatch_authority_fence(self._root_dir), _path_lock(path), interprocess_file_lock(path):
            current = _default_profile(project) if not path.exists() else _decode_profile(_read_json(path), expected_project_id=project)
            if expected_revision != current.revision:
                raise ProjectBoundaryProfileConflict(f"project boundary profile revision conflict: expected {expected_revision}, current {current.revision}")
            grants = list(current.persistent_grants)
            index = next((i for i, item in enumerate(grants) if item.grant_id == grant_id), None)
            if index is None:
                raise ProjectBoundaryProfileConflict("grant is unavailable")
            grant = grants[index]
            if grant.revision != expected_grant_revision or grant.revoked:
                raise ProjectBoundaryProfileConflict("grant revision conflict")
            grants[index] = replace(grant, revision=grant.revision + 1, revoked=True)
            profile = replace(current, revision=current.revision + 1, persistent_grants=tuple(grants))
            encoded = _encode_profile(profile)
            _reject_sensitive_material(encoded)
            atomic_write_text(path, json.dumps(encoded, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            return ProjectBoundaryProfileSnapshot(profile, profile.revision, True)

    def _path(self, project_id: str) -> Path:
        path = self._root_dir / "library" / "projects" / project_id / "boundary" / "profile.json"
        resolved = path.resolve()
        projects_root = (self._root_dir / "library" / "projects").resolve()
        if projects_root not in resolved.parents:
            raise ProjectBoundaryProfileError("project boundary profile path escaped authority root")
        return resolved


def _default_profile(project_id: str) -> ProjectBoundaryProfile:
    return ProjectBoundaryProfile(
        profile_id=f"project-boundary-{project_id}",
        project_id=project_id,
        mode="guarded",
        revision=1,
        remote_default="review",
    )


def _encode_profile(profile: ProjectBoundaryProfile) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "profile_id": profile.profile_id,
        "project_id": profile.project_id,
        "mode": profile.mode,
        "revision": profile.revision,
        "remote_default": profile.remote_default,
        "enabled_sources": list(profile.enabled_sources),
        "denied_effects": list(profile.denied_effects),
        "persistent_grants": [_encode_grant(grant) for grant in profile.persistent_grants],
    }


def _encode_grant(grant: BoundaryGrant) -> dict[str, object]:
    return {
        "grant_id": grant.grant_id,
        "subject_id": grant.subject_id,
        "project_id": grant.project_id,
        "target_id": grant.target_id,
        "actions": list(grant.actions),
        "data_classes": list(grant.data_classes),
        "destinations": list(grant.destinations),
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        "revision": grant.revision,
        "revoked": grant.revoked,
        "redaction_required": grant.redaction_required,
    }


def _decode_profile(payload: Mapping[str, object], *, expected_project_id: str) -> ProjectBoundaryProfile:
    _require_shape(payload, _PROFILE_FIELDS, "project boundary profile")
    _reject_sensitive_material(payload)
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ProjectBoundaryProfileError("project boundary profile schema is unsupported")
    if payload.get("project_id") != expected_project_id:
        raise ProjectBoundaryProfileError("project boundary profile identity drifted")
    grants_raw = payload.get("persistent_grants")
    if not isinstance(grants_raw, list):
        raise ProjectBoundaryProfileError("project boundary grants must be an array")
    try:
        return ProjectBoundaryProfile(
            profile_id=str(payload["profile_id"]),
            project_id=expected_project_id,
            mode=str(payload["mode"]),  # type: ignore[arg-type]
            revision=_positive_int(payload["revision"], "profile revision"),
            remote_default=str(payload["remote_default"]),  # type: ignore[arg-type]
            enabled_sources=_text_tuple(payload["enabled_sources"], "enabled sources"),
            denied_effects=_text_tuple(payload["denied_effects"], "denied effects"),  # type: ignore[arg-type]
            persistent_grants=tuple(_decode_grant(item) for item in grants_raw),
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ProjectBoundaryProfileError):
            raise
        raise ProjectBoundaryProfileError("project boundary profile is invalid") from error


def _decode_grant(value: object) -> BoundaryGrant:
    if not isinstance(value, Mapping):
        raise ProjectBoundaryProfileError("project boundary grant must be an object")
    payload = dict(value)
    _require_shape(payload, _GRANT_FIELDS, "project boundary grant")
    expiry = payload["expires_at"]
    try:
        expires_at = datetime.fromisoformat(expiry) if isinstance(expiry, str) else None
        return BoundaryGrant(
            grant_id=str(payload["grant_id"]),
            subject_id=str(payload["subject_id"]),
            project_id=str(payload["project_id"]),
            target_id=str(payload["target_id"]),
            actions=_text_tuple(payload["actions"], "grant actions"),  # type: ignore[arg-type]
            data_classes=_text_tuple(payload["data_classes"], "grant data classes"),
            destinations=_text_tuple(payload["destinations"], "grant destinations"),  # type: ignore[arg-type]
            expires_at=expires_at,
            revision=_positive_int(payload["revision"], "grant revision"),
            revoked=payload["revoked"] is True,
            redaction_required=payload["redaction_required"] is True,
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, ProjectBoundaryProfileError):
            raise
        raise ProjectBoundaryProfileError("project boundary grant is invalid") from error


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectBoundaryProfileError("project boundary profile is unreadable") from error
    if not isinstance(payload, dict):
        raise ProjectBoundaryProfileError("project boundary profile must be an object")
    return payload


def _require_shape(payload: Mapping[str, object], fields: frozenset[str], label: str) -> None:
    actual = {str(key) for key in payload}
    if actual != fields:
        raise ProjectBoundaryProfileError(f"{label} fields are invalid")


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProjectBoundaryProfileError(f"{label} must be a string array")
    return tuple(value)


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProjectBoundaryProfileError(f"{label} must be positive")
    return value


def _project_id(value: str) -> str:
    project_id = str(value).strip()
    if not _PROJECT_ID.fullmatch(project_id):
        raise ProjectBoundaryProfileError("project identity is invalid")
    return project_id


def _path_lock(path: Path) -> RLock:
    resolved = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(resolved, RLock())


def _reject_sensitive_material(value: object, *, path: str = "profile") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_secret"):
                raise ProjectBoundaryProfileError(f"sensitive material is forbidden at {path}.{key}")
            _reject_sensitive_material(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_material(nested, path=f"{path}[{index}]")
