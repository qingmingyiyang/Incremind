from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from uuid import uuid4
from contextlib import nullcontext
from .object_locks import object_lock, source_locked
from threading import Lock
from collections.abc import Mapping, Sequence, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .ports import ObjectStorePort
from .settings import StorageConfigurationError


class ObjectStoreRevisionError(ValueError):
    """Raised when a caller writes with a stale expected revision."""


class ObjectStorePathError(ValueError):
    """Raised when a logical object cannot map to one unambiguous safe path."""


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_WINDOWS_COMPATIBLE_PATH_LIMIT = 259
_SHORT_OBJECT_FILE_PREFIX = "~h-"
_SHORT_OBJECT_HASH_LENGTH = 64
_REPLACE_LOCK_GUARD = Lock()


def _valid_mutation_attribution(value, namespace, collection, object_id, revision):
    fields = {'by', 'actor_user_id', 'target_user_id', 'device_id', 'namespace_id',
              'collection', 'object_id', 'revision', 'action'}
    return (isinstance(value, Mapping) and set(value) == fields
        and value['by'] == 'admin' and value['action'] == 'write'
        and value['namespace_id'] == namespace and value['collection'] == collection
        and value['object_id'] == object_id and type(value['revision']) is int and value['revision'] == revision
        and all(isinstance(value[key], str) and _SAFE_SEGMENT.fullmatch(value[key])
                for key in ('actor_user_id', 'target_user_id', 'device_id')))


@dataclass(slots=True)
class _TargetReplaceLock:
    lock: object
    users: int = 0


_TARGET_REPLACE_LOCKS: dict[str, _TargetReplaceLock] = {}


def _is_logically_deleted_source(collection: str, payload: Mapping[str, object]) -> bool:
    if collection != "sources":
        return False
    lifecycle = payload.get("library_lifecycle")
    return isinstance(lifecycle, Mapping) and lifecycle.get("status") == "deleted"


@dataclass(frozen=True, slots=True)
class JsonObjectStoreRecord:
    """One validated logical object read directly from the JSON file layout."""

    object_id: str
    payload: Mapping[str, object]
    payload_bytes: bytes


@dataclass(frozen=True, slots=True)
class _ObjectPaths:
    payload_path: Path
    meta_path: Path
    uses_short_name: bool


@dataclass(slots=True)
class JsonObjectStore:
    """Small JSON repository used for rebuild runtime smoke tests.

    The store writes only under the configured rebuild root and rejects roots
    that overlap the legacy library path.
    """

    root: Path
    legacy_root: Path | None = None
    namespace_id: str = "default"
    vector_cache_path: Path | None = field(default=None, kw_only=True)
    mutation_attribution: Callable[[str, str, int], Mapping[str, object] | None] | None = field(default=None, kw_only=True, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve(strict=False)
        if self.vector_cache_path is not None:
            self.vector_cache_path = Path(self.vector_cache_path).expanduser().resolve(strict=False)
        if self.legacy_root is not None:
            self.legacy_root = self.legacy_root.expanduser().resolve(strict=False)
        _validate_segment("namespace_id", self.namespace_id)
        self._validate_boundary()

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        payload = self.read_including_deleted(collection, object_id)
        if payload is not None and _is_logically_deleted_source(collection, payload):
            return None
        return payload

    @source_locked
    def read_including_deleted(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        paths = self._object_paths(collection, object_id)
        if not paths.payload_path.exists():
            return None
        payload = json.loads(paths.payload_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"stored object {collection}/{object_id} must be a JSON object")
        return dict(payload)

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        return tuple(
            item
            for item in self.list_including_deleted(collection)
            if not _is_logically_deleted_source(collection, item)
        )

    def list_including_deleted(self, collection: str) -> Sequence[Mapping[str, object]]:
        """List one collection for an explicit lifecycle or retention inventory."""

        directory = self._collection_path(collection)
        if not directory.exists():
            return ()
        objects: list[Mapping[str, object]] = []
        for path in sorted(directory.glob("*.json")):
            if path.name.endswith(".meta.json"):
                continue
            _logical_object_id_from_payload_path(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"stored object {path.name} must be a JSON object")
            objects.append(dict(payload))
        return tuple(objects)

    def collection_names(self) -> tuple[str, ...]:
        """Return the current namespace collection catalog without reading payloads."""

        namespace = self.root / "objects" / self.namespace_id
        if not namespace.exists():
            return ()
        if not namespace.is_dir() or namespace.is_symlink():
            raise ObjectStorePathError("object store namespace must be a non-symlink directory")
        names: list[str] = []
        for path in sorted(namespace.iterdir(), key=lambda item: item.name):
            if path.is_symlink():
                raise ObjectStorePathError("object store collection cannot be a symlink")
            if not path.is_dir():
                raise ObjectStorePathError("object store namespace contains a non-directory entry")
            _validate_segment("collection", path.name)
            names.append(path.name)
        return tuple(names)

    def delete(self, collection: str, object_id: str) -> bool:
        if collection == 'sources':
            from .source_retrieval_index import source_mutation
            with source_mutation(self, object_id) as tx:
                return self._delete_object(collection, object_id, source_tx=tx)
        with self.locked(collection, object_id):
            return self._delete_object(collection, object_id)

    def _delete_object(self, collection, object_id, *, source_tx=None):
        paths = self._object_paths(collection, object_id)
        existed = paths.payload_path.exists()
        if collection == 'sources':
            from .source_retrieval_index import invalidate_source
            invalidate_source(source_tx, self, object_id)
        if paths.payload_path.exists():
            paths.payload_path.unlink()
        if paths.meta_path.exists():
            paths.meta_path.unlink()
        return existed

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        if collection == 'sources':
            from .source_retrieval_index import source_mutation, refresh_source
            with source_mutation(self, object_id) as tx:
                revision, incarnation, token = self._write_object(
                    collection, object_id, payload, expected_revision, source_tx=tx)
            refresh_source(self, object_id, revision, incarnation, token)
            return revision
        with self.locked(collection, object_id):
            return self._write_object(collection, object_id, payload, expected_revision)[0]

    def _write_object(self, collection, object_id, payload, expected_revision, *, source_tx=None):
        if expected_revision is not None and expected_revision < 0:
            raise ObjectStoreRevisionError("expected_revision must be non-negative")
        paths = self._object_paths(collection, object_id, for_write=True)
        current_revision = self._read_revision(paths.meta_path)
        if expected_revision is not None and current_revision != expected_revision:
            raise ObjectStoreRevisionError(
                f"expected revision {expected_revision}, found {current_revision}"
            )
        token = None
        if collection == 'sources':
            from .source_retrieval_index import invalidate_source
            token = invalidate_source(source_tx, self, object_id, payload.get('project_id', 'default'))
        new_revision = current_revision + 1
        attribution = (self.mutation_attribution(collection, object_id, new_revision)
                       if self.mutation_attribution is not None else None)
        if attribution is not None:
            if not _valid_mutation_attribution(attribution, self.namespace_id, collection, object_id, new_revision):
                raise ValueError('mutation_attribution_invalid')
        paths.payload_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(paths.payload_path, dict(payload))
        metadata: dict[str, object] = {"revision": new_revision}
        if attribution is not None:
            metadata['server_admin_attribution'] = dict(attribution)
        if collection == "sources":
            prior = json.loads(paths.meta_path.read_text(encoding="utf-8")) if paths.meta_path.exists() else {}
            metadata["incarnation"] = prior.get("incarnation", "legacy") if current_revision else uuid4().hex
        if paths.uses_short_name:
            metadata["object_id"] = object_id
        _write_json_atomic(paths.meta_path, metadata)
        return new_revision, metadata.get('incarnation'), token

    @source_locked
    def revision(self, collection: str, object_id: str) -> int:
        """Return the current CAS metadata revision without exposing storage paths."""
        return self._read_revision(self._object_paths(collection, object_id).meta_path)

    @source_locked
    def attribution(self, collection: str, object_id: str, revision: int):
        """Read a current sidecar binding without exposing private payloads."""
        path = self._object_paths(collection, object_id).meta_path
        metadata = json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
        value = metadata.get('server_admin_attribution')
        if (type(metadata.get('revision')) is not int or metadata.get('revision') != revision
                or not _valid_mutation_attribution(value, self.namespace_id, collection, object_id, revision)):
            return None
        return dict(value)

    def locked(self, collection: str, object_id: str):
        if collection not in ("sources","memory_persona","project_skills"):
            return nullcontext()
        path = self._object_paths(collection, object_id, for_write=True).meta_path
        return object_lock(path.with_suffix(".lock"))

    @source_locked
    def incarnation(self, collection: str, object_id: str) -> str:
        path = self._object_paths(collection, object_id).meta_path
        meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return meta.get("incarnation", "legacy")

    def is_revision_conflict(self, error: BaseException) -> bool:
        """Classify CAS conflicts for domain ports without leaking this type."""

        return isinstance(error, ObjectStoreRevisionError)

    def _validate_boundary(self) -> None:
        if self.legacy_root is None:
            return
        if self.root == self.legacy_root:
            raise StorageConfigurationError("rebuild storage cannot equal legacy library")
        if _contains(self.legacy_root, self.root):
            raise StorageConfigurationError("rebuild storage cannot be inside legacy library")
        if _contains(self.root, self.legacy_root):
            raise StorageConfigurationError("legacy library cannot be inside rebuild storage")

    def _collection_path(self, collection: str) -> Path:
        _validate_segment("collection", collection)
        return self.root / "objects" / self.namespace_id / collection

    def _object_path(self, collection: str, object_id: str) -> Path:
        return self._object_paths(collection, object_id).payload_path

    def _meta_path(self, collection: str, object_id: str) -> Path:
        return self._object_paths(collection, object_id).meta_path

    def _object_paths(
        self,
        collection: str,
        object_id: str,
        *,
        for_write: bool = False,
    ) -> _ObjectPaths:
        _validate_segment("object_id", object_id)
        directory = self._collection_path(collection)
        legacy = _ObjectPaths(
            payload_path=directory / f"{object_id}.json",
            meta_path=directory / f"{object_id}.meta.json",
            uses_short_name=False,
        )
        short_stem = _short_object_file_stem(object_id)
        short = _ObjectPaths(
            payload_path=directory / f"{short_stem}.json",
            meta_path=directory / f"{short_stem}.meta.json",
            uses_short_name=True,
        )
        legacy_exists = legacy.payload_path.exists() or legacy.meta_path.exists()
        short_exists = short.payload_path.exists() or short.meta_path.exists()
        if legacy_exists and short_exists:
            raise ObjectStorePathError("object storage has ambiguous legacy and short layouts")
        if short_exists:
            _validate_short_object_layout(short, object_id)
            return short
        if legacy_exists:
            return legacy
        if not for_write or _paths_fit_windows_budget(legacy):
            return legacy
        if _paths_fit_windows_budget(short):
            return short
        raise ObjectStorePathError("object storage path exceeds Windows-compatible budget")

    def _read_revision(self, meta_path: Path) -> int:
        return _metadata_revision(meta_path)


@dataclass(frozen=True, slots=True)
class ObjectStoreAnswerFeedbackRepository:
    """Persists answer feedback records in isolated rebuild object storage."""

    object_store: ObjectStorePort
    collection: str = "answer_feedback"

    def save(self, feedback: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(feedback)
        _validate_answer_feedback_payload(payload)
        feedback_id = _required_answer_feedback_str(payload, "id")
        try:
            self.object_store.write(self.collection, feedback_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"answer feedback already exists: {feedback_id}") from exc
        return payload

    def get(self, feedback_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, feedback_id)
        return dict(item) if item is not None else None

    def list_by_recall_result(self, recall_result_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.object_store.list(self.collection)
            if item.get("recall_result_id") == recall_result_id
        )


def _validate_segment(label: str, value: str) -> None:
    if not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} must be a safe repository segment")


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def read_json_object_store_collection(
    object_store_root: Path,
    *,
    namespace_id: str,
    collection: str,
) -> tuple[JsonObjectStoreRecord, ...]:
    """Read one physical JSON collection while preserving logical object IDs.

    Migration tooling uses this narrow reader because it must support both the
    original ``<object_id>.json`` layout and the path-budget short-name codec.
    It returns no absolute paths and validates short-name metadata before any
    caller can treat a filename as a logical identity.
    """

    _validate_segment("namespace_id", namespace_id)
    _validate_segment("collection", collection)
    root = object_store_root.expanduser().resolve(strict=False)
    directory = root / "objects" / namespace_id / collection
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise ObjectStorePathError("object store collection root must be a directory")
    records: list[JsonObjectStoreRecord] = []
    object_ids: set[str] = set()
    for payload_path in sorted(directory.glob("*.json"), key=lambda path: path.name):
        if payload_path.name.endswith(".meta.json"):
            continue
        if payload_path.is_symlink() or not payload_path.is_file():
            raise ObjectStorePathError("object store contains an invalid payload entry")
        object_id = _logical_object_id_from_payload_path(payload_path)
        if object_id in object_ids:
            raise ObjectStorePathError("object store has duplicate logical object layouts")
        try:
            payload_bytes = payload_path.read_bytes()
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ObjectStorePathError("object store requires readable JSON objects") from exc
        if not isinstance(payload, dict):
            raise ObjectStorePathError("object store requires JSON object payloads")
        object_ids.add(object_id)
        records.append(
            JsonObjectStoreRecord(
                object_id=object_id,
                payload=dict(payload),
                payload_bytes=payload_bytes,
            )
        )
    for meta_path in sorted(directory.glob(f"{_SHORT_OBJECT_FILE_PREFIX}*.meta.json")):
        if meta_path.is_symlink() or not meta_path.is_file():
            raise ObjectStorePathError("short object metadata entry is invalid")
        payload_name = meta_path.name[: -len(".meta.json")] + ".json"
        if not (directory / payload_name).exists():
            raise ObjectStorePathError("short object metadata has no payload")
    return tuple(records)


def _logical_object_id_from_payload_path(payload_path: Path) -> str:
    stem = payload_path.stem
    if not stem.startswith(_SHORT_OBJECT_FILE_PREFIX):
        _validate_segment("object_id", stem)
        return stem
    short = _ObjectPaths(
        payload_path=payload_path,
        meta_path=payload_path.with_name(f"{stem}.meta.json"),
        uses_short_name=True,
    )
    return _validate_short_object_layout(short)


def _short_object_file_stem(object_id: str) -> str:
    _validate_segment("object_id", object_id)
    return _SHORT_OBJECT_FILE_PREFIX + hashlib.sha256(object_id.encode("utf-8")).hexdigest()


def _paths_fit_windows_budget(paths: _ObjectPaths) -> bool:
    return max(len(str(paths.payload_path)), len(str(paths.meta_path))) <= _WINDOWS_COMPATIBLE_PATH_LIMIT


def _validate_short_object_layout(paths: _ObjectPaths, expected_object_id: str | None = None) -> str:
    payload_exists = paths.payload_path.exists()
    meta_exists = paths.meta_path.exists()
    if payload_exists != meta_exists:
        raise ObjectStorePathError("short object layout is incomplete")
    if not payload_exists or not meta_exists:
        raise ObjectStorePathError("short object layout is incomplete")
    if (
        paths.payload_path.is_symlink()
        or paths.meta_path.is_symlink()
        or not paths.payload_path.is_file()
        or not paths.meta_path.is_file()
    ):
        raise ObjectStorePathError("short object layout requires regular files")
    stem = paths.payload_path.stem
    if (
        not stem.startswith(_SHORT_OBJECT_FILE_PREFIX)
        or len(stem) != len(_SHORT_OBJECT_FILE_PREFIX) + _SHORT_OBJECT_HASH_LENGTH
        or any(character not in "0123456789abcdef" for character in stem[len(_SHORT_OBJECT_FILE_PREFIX) :])
    ):
        raise ObjectStorePathError("short object filename is invalid")
    try:
        metadata = json.loads(paths.meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObjectStorePathError("short object metadata must be readable JSON") from exc
    if not isinstance(metadata, dict):
        raise ObjectStorePathError("short object metadata must be a JSON object")
    object_id = metadata.get("object_id")
    if not isinstance(object_id, str):
        raise ObjectStorePathError("short object metadata requires object identity")
    _validate_segment("object_id", object_id)
    if _short_object_file_stem(object_id) != stem:
        raise ObjectStorePathError("short object metadata identity does not match filename")
    if expected_object_id is not None and object_id != expected_object_id:
        raise ObjectStorePathError("short object metadata identity does not match request")
    _metadata_revision(paths.meta_path)
    return object_id


def _metadata_revision(meta_path: Path) -> int:
    if not meta_path.exists():
        return 0
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ObjectStoreRevisionError("object metadata must be a JSON object")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ObjectStoreRevisionError("object metadata revision must be non-negative integer")
    return revision


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tmp-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _replace_temporary_file_serialized(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _replace_temporary_file(temporary: Path, path: Path) -> None:
    temporary.replace(path)


def _replace_temporary_file_serialized(temporary: Path, path: Path) -> None:
    """Serialize only same-process replacement of the same final path.

    Windows can reject simultaneous ``ReplaceFile``/``MoveFileEx`` operations
    against one destination even after distinct temporary writers have fsynced.
    Temporary payload preparation remains parallel; this protects only the final
    destination handoff and does not attempt a cross-process lock.
    """

    key = os.path.normcase(os.path.abspath(path))
    with _REPLACE_LOCK_GUARD:
        entry = _TARGET_REPLACE_LOCKS.get(key)
        if entry is None:
            entry = _TargetReplaceLock(lock=Lock())
            _TARGET_REPLACE_LOCKS[key] = entry
        entry.users += 1
    try:
        with entry.lock:
            _replace_temporary_file(temporary, path)
    finally:
        with _REPLACE_LOCK_GUARD:
            entry.users -= 1
            if entry.users == 0 and _TARGET_REPLACE_LOCKS.get(key) is entry:
                _TARGET_REPLACE_LOCKS.pop(key, None)


def _validate_answer_feedback_payload(payload: Mapping[str, object]) -> None:
    if _required_answer_feedback_str(payload, "schema_version") != "1.0.0":
        raise ValueError("answer feedback schema_version must be 1.0.0")
    candidate_status = _required_answer_feedback_str(payload, "candidate_status")
    feedback_type = _required_answer_feedback_str(payload, "feedback_type")
    if feedback_type != _answer_feedback_type(candidate_status):
        raise ValueError("answer feedback type does not match candidate status")
    review = _answer_feedback_mapping(payload, "review")
    if candidate_status == "promoted" and review.get("reviewed_by") != "user":
        raise ValueError("promoted answer feedback requires user reviewer")
    if not _answer_feedback_source_refs(payload.get("source_refs")):
        raise ValueError("answer feedback requires source refs")
    input_refs = payload.get("input_refs")
    if not isinstance(input_refs, Sequence) or isinstance(input_refs, (str, bytes)):
        raise ValueError("answer feedback requires input refs")
    kinds = {ref.get("kind") for ref in input_refs if isinstance(ref, Mapping)}
    for required in ("recall_result", "model_request", "model_result", "memory_candidate"):
        if required not in kinds:
            raise ValueError(f"answer feedback input refs require {required}")
    if payload.get("document_id") is not None and "document" not in kinds:
        raise ValueError("answer feedback document_id requires document input ref")


def _answer_feedback_type(candidate_status: str) -> str:
    if candidate_status == "promoted":
        return "candidate_promoted_to_draft"
    if candidate_status == "rejected":
        return "candidate_rejected"
    raise ValueError("answer feedback requires rejected or promoted candidate")


def _answer_feedback_source_refs(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    refs: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("source_id"), str) and isinstance(item.get("locator"), str):
            refs.append(item)
    return refs


def _answer_feedback_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _required_answer_feedback_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


@dataclass(frozen=True, slots=True)
class ObjectStorePlatformRecoveryActionRepository:
    """Persists platform recovery action records in isolated rebuild object storage."""

    object_store: ObjectStorePort
    collection: str = "platform_recovery_actions"

    def save(self, action: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(action)
        _validate_platform_recovery_action_payload(payload)
        action_id = _required_platform_recovery_str(payload, "id")
        try:
            self.object_store.write(self.collection, action_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"platform recovery action already exists: {action_id}") from exc
        return payload

    def update(self, action: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(action)
        _validate_platform_recovery_action_payload(payload)
        action_id = _required_platform_recovery_str(payload, "id")
        if self.object_store.read(self.collection, action_id) is None:
            raise ValueError(f"platform recovery action not found: {action_id}")
        self.object_store.write(self.collection, action_id, payload, expected_revision=None)
        return payload

    def get(self, action_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, action_id)
        return dict(item) if item is not None else None

    def list_open(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.object_store.list(self.collection)
            if item.get("status") == "open"
        )


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyLibraryDryRunRepository:
    """Persists read-only legacy library dry-run reports in isolated rebuild storage."""

    object_store: ObjectStorePort
    collection: str = "legacy_library_dry_runs"

    def save_report(self, report: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(report)
        _validate_legacy_library_dry_run_payload(payload)
        report_id = _required_legacy_dry_run_str(payload, "id")
        try:
            self.object_store.write(self.collection, report_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy library dry-run report already exists: {report_id}") from exc
        return payload

    def get(self, report_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, report_id)
        return dict(item) if item is not None else None

    def update_report(self, report: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(report)
        _validate_legacy_library_dry_run_payload(payload)
        report_id = _required_legacy_dry_run_str(payload, "id")
        if self.object_store.read(self.collection, report_id) is None:
            raise ValueError(f"legacy library dry-run report not found: {report_id}")
        self.object_store.write(self.collection, report_id, payload, expected_revision=None)
        return payload

    def list_reports(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationPlanRepository:
    """Persists planning-only legacy migration dry-run artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_plans"

    def save(self, plan: Mapping[str, object]) -> dict[str, object]:
        payload = dict(plan)
        _validate_legacy_migration_plan_payload(payload)
        plan_id = _required_legacy_plan_str(payload, "id")
        try:
            self.object_store.write(self.collection, plan_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration plan already exists: {plan_id}") from exc
        return payload

    def get(self, plan_id: str) -> dict[str, object] | None:
        item = self.object_store.read(self.collection, plan_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationSelectionManifestRepository:
    """Persists selection-only legacy migration dry-run manifests."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_selection_manifests"

    def save(self, manifest: Mapping[str, object]) -> dict[str, object]:
        payload = dict(manifest)
        _validate_legacy_migration_selection_manifest_payload(payload)
        manifest_id = _required_legacy_selection_str(payload, "id")
        try:
            self.object_store.write(self.collection, manifest_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration selection manifest already exists: {manifest_id}") from exc
        return payload

    def get(self, manifest_id: str) -> dict[str, object] | None:
        item = self.object_store.read(self.collection, manifest_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationRollbackManifestRepository:
    """Persists rollback-only legacy migration dry-run manifests."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_rollback_manifests"

    def save(self, manifest: Mapping[str, object]) -> dict[str, object]:
        payload = dict(manifest)
        _validate_legacy_migration_rollback_manifest_payload(payload)
        manifest_id = _required_legacy_rollback_str(payload, "id")
        try:
            self.object_store.write(self.collection, manifest_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration rollback manifest already exists: {manifest_id}") from exc
        return payload

    def get(self, manifest_id: str) -> dict[str, object] | None:
        item = self.object_store.read(self.collection, manifest_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationExecutionApprovalRepository:
    """Persists legacy migration execution approval artifacts without Job creation."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_execution_approvals"

    def save(self, approval: Mapping[str, object]) -> dict[str, object]:
        payload = dict(approval)
        _validate_legacy_migration_execution_approval_payload(payload)
        approval_id = _required_legacy_approval_str(payload, "id")
        try:
            self.object_store.write(self.collection, approval_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration execution approval already exists: {approval_id}") from exc
        return payload

    def get(self, approval_id: str) -> dict[str, object] | None:
        item = self.object_store.read(self.collection, approval_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationExecutionJobRepository:
    """Persists Phase 8 legacy migration dry-run Jobs with execution guards."""

    object_store: ObjectStorePort
    collection: str = "jobs"

    def save(self, job: Mapping[str, object]) -> None:
        payload = dict(job)
        _validate_legacy_migration_execution_job_payload(payload)
        job_id = _required_legacy_job_str(payload, "id")
        try:
            self.object_store.write(self.collection, job_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration execution job already exists: {job_id}") from exc

    def get(self, job_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, job_id)
        return dict(item) if item is not None else None

    def all(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationCompletionBlockRepository:
    """Persists Phase 8 legacy migration completion blocker artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_completion_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_completion_block_payload(payload)
        block_id = _required_legacy_completion_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration completion block already exists: {block_id}") from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationWorkerEvidenceRepository:
    """Persists Phase 8 legacy migration worker dry-run evidence artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_worker_evidence"

    def save(self, evidence: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(evidence)
        _validate_legacy_migration_worker_evidence_payload(payload)
        evidence_id = _required_legacy_worker_evidence_str(payload, "id")
        try:
            self.object_store.write(self.collection, evidence_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration worker evidence already exists: {evidence_id}") from exc
        return payload

    def update(self, evidence: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(evidence)
        _validate_legacy_migration_worker_evidence_payload(payload)
        evidence_id = _required_legacy_worker_evidence_str(payload, "id")
        if self.object_store.read(self.collection, evidence_id) is None:
            raise ValueError(f"legacy migration worker evidence not found: {evidence_id}")
        self.object_store.write(self.collection, evidence_id, payload, expected_revision=None)
        return payload

    def get(self, evidence_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, evidence_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationVerifiedCompletionJobRepository:
    """Persists completed Phase 8 legacy migration dry-run Jobs with verification guards."""

    object_store: ObjectStorePort
    collection: str = "jobs"

    def save(self, job: Mapping[str, object]) -> None:
        payload = dict(job)
        _validate_legacy_migration_verified_completion_job_payload(payload)
        job_id = _required_legacy_verified_job_str(payload, "id")
        if self.object_store.read(self.collection, job_id) is None:
            raise ValueError(f"legacy migration execution job not found: {job_id}")
        self.object_store.write(self.collection, job_id, payload, expected_revision=None)

    def get(self, job_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, job_id)
        return dict(item) if item is not None else None

    def all(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationImportCandidateRepository:
    """Persists reviewable Phase 8 legacy migration import candidates."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_import_candidates"

    def save(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(candidate)
        _validate_legacy_migration_import_candidate_payload(payload)
        candidate_id = _required_legacy_import_candidate_str(payload, "id")
        try:
            self.object_store.write(self.collection, candidate_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration import candidate already exists: {candidate_id}") from exc
        return payload

    def update(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(candidate)
        _validate_legacy_migration_import_candidate_payload(payload)
        candidate_id = _required_legacy_import_candidate_str(payload, "id")
        if self.object_store.read(self.collection, candidate_id) is None:
            raise ValueError(f"legacy migration import candidate not found: {candidate_id}")
        self.object_store.write(self.collection, candidate_id, payload, expected_revision=None)
        return payload

    def get(self, candidate_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, candidate_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationMemoryPublicationBlockRepository:
    """Persists Phase 8 legacy migration Memory publication block records."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_memory_publication_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_memory_publication_block_payload(payload)
        block_id = _required_legacy_memory_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration Memory publication block already exists: {block_id}") from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationDraftMemoryProposalRepository:
    """Persists Phase 8 legacy migration draft-only Memory proposals."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_draft_memory_proposals"

    def save(self, proposal: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(proposal)
        _validate_legacy_migration_draft_memory_proposal_payload(payload)
        proposal_id = _required_legacy_memory_proposal_str(payload, "id")
        try:
            self.object_store.write(self.collection, proposal_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration draft Memory proposal already exists: {proposal_id}") from exc
        return payload

    def update(self, proposal: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(proposal)
        _validate_legacy_migration_draft_memory_proposal_payload(payload)
        proposal_id = _required_legacy_memory_proposal_str(payload, "id")
        if self.object_store.read(self.collection, proposal_id) is None:
            raise ValueError(f"legacy migration draft Memory proposal not found: {proposal_id}")
        self.object_store.write(self.collection, proposal_id, payload, expected_revision=None)
        return payload

    def get(self, proposal_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, proposal_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationMemoryProposalPromotionBlockRepository:
    """Persists Phase 8 legacy migration proposal promotion block records."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_memory_proposal_promotion_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_memory_proposal_promotion_block_payload(payload)
        block_id = _required_legacy_promotion_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration proposal promotion block already exists: {block_id}") from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationStagedMemoryCandidateRepository:
    """Persists Phase 8 migration-only staged Atom/Scenario candidate records."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_staged_memory_candidates"

    def save(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(candidate)
        _validate_legacy_migration_staged_memory_candidate_payload(payload)
        candidate_id = _required_legacy_staged_candidate_str(payload, "id")
        try:
            self.object_store.write(self.collection, candidate_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration staged Memory candidate already exists: {candidate_id}") from exc
        return payload

    def update(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(candidate)
        _validate_legacy_migration_staged_memory_candidate_payload(payload)
        candidate_id = _required_legacy_staged_candidate_str(payload, "id")
        if self.object_store.read(self.collection, candidate_id) is None:
            raise ValueError(f"legacy migration staged Memory candidate not found: {candidate_id}")
        self.object_store.write(self.collection, candidate_id, payload, expected_revision=None)
        return payload

    def get(self, candidate_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, candidate_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationStagedMemoryCandidateReviewBlockRepository:
    """Persists Phase 8 staged Atom/Scenario review block records."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_staged_memory_candidate_review_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_staged_memory_candidate_review_block_payload(payload)
        block_id = _required_legacy_staged_review_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration staged Memory candidate review block already exists: {block_id}") from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalMemoryPublicationBlockRepository:
    """Persists Phase 8 final Memory publication blocker records."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_final_memory_publication_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_final_memory_publication_block_payload(payload)
        block_id = _required_legacy_final_publication_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration final Memory publication block already exists: {block_id}") from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalPublicationApprovalRepository:
    """Persists Phase 8 final publication approval artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_migration_final_publication_approvals"

    def save(self, approval: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(approval)
        _validate_legacy_migration_final_publication_approval_payload(payload)
        approval_id = _required_legacy_final_approval_str(payload, "id")
        try:
            self.object_store.write(self.collection, approval_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration final publication approval already exists: {approval_id}") from exc
        return payload

    def get(self, approval_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, approval_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalPublicationTransactionDryRunRepository:
    """Persists Phase 8 final publication transaction dry-run plans."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_publication_txn_dry_runs"

    def save(self, transaction: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(transaction)
        _validate_legacy_migration_final_publication_transaction_payload(payload)
        transaction_id = _required_legacy_final_transaction_str(payload, "id")
        try:
            self.object_store.write(self.collection, transaction_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration final publication transaction dry-run already exists: {transaction_id}") from exc
        return payload

    def get(self, transaction_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, transaction_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalPublishCommitBlockRepository:
    """Persists Phase 8 final publish commit blocker artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_publish_commit_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_final_publish_commit_block_payload(payload)
        block_id = _required_legacy_final_commit_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration final publish commit block already exists: {block_id}") from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalPublishAuditEvidenceRepository:
    """Persists Phase 8 final publish audit evidence artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_publish_audit_evidence"

    def save(self, evidence: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(evidence)
        _validate_legacy_migration_final_publish_audit_evidence_payload(payload)
        evidence_id = _required_legacy_final_audit_evidence_str(payload, "id")
        try:
            self.object_store.write(self.collection, evidence_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration final publish audit evidence already exists: {evidence_id}") from exc
        return payload

    def get(self, evidence_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, evidence_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalPublishImplementationBlockRepository:
    """Persists Phase 8 final publish implementation blocker artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_publish_implementation_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_final_publish_implementation_block_payload(payload)
        block_id = _required_legacy_final_implementation_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration final publish implementation block already exists: {block_id}") from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalPublishWriterContractGateRepository:
    """Persists Phase 8 final publish writer contract gate artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_publish_writer_contract_gates"

    def save(self, gate: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(gate)
        _validate_legacy_migration_final_publish_writer_contract_gate_payload(payload)
        gate_id = _required_legacy_final_writer_contract_gate_str(payload, "id")
        try:
            self.object_store.write(self.collection, gate_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"legacy migration final publish writer contract gate already exists: {gate_id}") from exc
        return payload

    def get(self, gate_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, gate_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterContractValidationRepository:
    """Persists Phase 8 final writer contract dry-run validation artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_contract_validations"

    def save(self, validation: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(validation)
        _validate_legacy_migration_final_writer_contract_validation_payload(payload)
        validation_id = _required_legacy_final_writer_contract_validation_str(payload, "id")
        try:
            self.object_store.write(self.collection, validation_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                f"legacy migration final writer contract dry-run validation already exists: {validation_id}"
            ) from exc
        return payload

    def get(self, validation_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, validation_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterImplementationReadinessBlockRepository:
    """Persists Phase 8 final writer implementation readiness blocker artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_implementation_readiness_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_final_writer_implementation_readiness_block_payload(payload)
        block_id = _required_legacy_final_writer_readiness_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                f"legacy migration final writer implementation readiness block already exists: {block_id}"
            ) from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterImplementationDesignGateRepository:
    """Persists Phase 8 final writer implementation design gate artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_design_gates"

    def save(self, design_gate: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(design_gate)
        _validate_legacy_migration_final_writer_implementation_design_gate_payload(payload)
        design_gate_id = _required_legacy_final_writer_design_gate_str(payload, "id")
        try:
            self.object_store.write(self.collection, design_gate_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                f"legacy migration final writer implementation design gate already exists: {design_gate_id}"
            ) from exc
        return payload

    def get(self, design_gate_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, design_gate_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterImplementationDesignReviewBlockRepository:
    """Persists Phase 8 final writer implementation design review blocker artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_design_review_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_final_writer_implementation_design_review_block_payload(payload)
        block_id = _required_legacy_final_writer_design_review_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                f"legacy migration final writer implementation design review block already exists: {block_id}"
            ) from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterImplementationDryRunPlanBlockRepository:
    """Persists Phase 8 final writer implementation dry-run plan blocker artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_dry_run_plan_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_final_writer_implementation_dry_run_plan_block_payload(payload)
        block_id = _required_legacy_final_writer_dry_run_plan_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                f"legacy migration final writer implementation dry-run plan block already exists: {block_id}"
            ) from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterImplementationDryRunPlanReviewBlockRepository:
    """Persists Phase 8 final writer implementation dry-run plan review blocker artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_dry_run_plan_review_blocks"

    def save(self, block: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(block)
        _validate_legacy_migration_final_writer_implementation_dry_run_plan_review_block_payload(payload)
        block_id = _required_legacy_final_writer_dry_run_plan_review_block_str(payload, "id")
        try:
            self.object_store.write(self.collection, block_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                f"legacy migration final writer implementation dry-run plan review block already exists: {block_id}"
            ) from exc
        return payload

    def get(self, block_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, block_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterImplementationPreflightGuardRepository:
    """Persists Phase 8 final writer implementation preflight guard artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_implementation_preflight_guards"

    def save(self, guard: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(guard)
        _validate_legacy_migration_final_writer_implementation_preflight_guard_payload(payload)
        guard_id = _required_legacy_final_writer_preflight_guard_str(payload, "id")
        try:
            self.object_store.write(self.collection, guard_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                f"legacy migration final writer implementation preflight guard already exists: {guard_id}"
            ) from exc
        return payload

    def get(self, guard_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, guard_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterImplementationNoWriteSmokeRepository:
    """Persists Phase 8 final writer implementation no-write smoke artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_implementation_no_write_smokes"

    def save(self, smoke: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(smoke)
        _validate_legacy_migration_final_writer_implementation_no_write_smoke_payload(payload)
        smoke_id = _required_legacy_final_writer_no_write_smoke_str(payload, "id")
        try:
            self.object_store.write(self.collection, smoke_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                f"legacy migration final writer implementation no-write smoke already exists: {smoke_id}"
            ) from exc
        return payload

    def get(self, smoke_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, smoke_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStoreLegacyMigrationFinalWriterImplementationRegressionConsolidationRepository:
    """Persists Phase 8 final writer implementation regression consolidation artifacts."""

    object_store: ObjectStorePort
    collection: str = "legacy_final_writer_implementation_regression_consolidations"

    def save(self, consolidation: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(consolidation)
        _validate_legacy_migration_final_writer_implementation_regression_consolidation_payload(payload)
        consolidation_id = _required_legacy_final_writer_regression_consolidation_str(payload, "id")
        try:
            self.object_store.write(self.collection, consolidation_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(
                "legacy migration final writer implementation regression consolidation "
                f"already exists: {consolidation_id}"
            ) from exc
        return payload

    def get(self, consolidation_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, consolidation_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


@dataclass(frozen=True, slots=True)
class ObjectStorePhase7PerformanceBudgetRepository:
    """Persists Phase 7 performance budget evidence in isolated rebuild object storage."""

    object_store: ObjectStorePort
    collection: str = "phase7_performance_budgets"

    def save(self, record: Mapping[str, object]) -> dict[str, object]:
        payload = dict(record)
        _validate_phase7_performance_budget_payload(payload)
        record_id = _required_phase7_budget_str(payload, "id")
        try:
            self.object_store.write(self.collection, record_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ValueError(f"phase 7 performance budget already exists: {record_id}") from exc
        return payload

    def get(self, record_id: str) -> dict[str, object] | None:
        item = self.object_store.read(self.collection, record_id)
        return dict(item) if item is not None else None

    def list(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list(self.collection))


def _validate_platform_recovery_action_payload(payload: Mapping[str, object]) -> None:
    if _required_platform_recovery_str(payload, "schema_version") != "1.0.0":
        raise ValueError("platform recovery action schema_version must be 1.0.0")
    capability = _required_platform_recovery_str(payload, "capability")
    source = _platform_recovery_mapping(payload, "source")
    if source.get("health_status") != "degraded":
        raise ValueError("platform recovery action requires degraded Product Health")
    evidence = _platform_recovery_mapping(payload, "evidence")
    evidence_capabilities: set[str] = set()
    for key in ("degraded_capabilities", "missing_capabilities", "os_path_leaks"):
        value = evidence.get(key)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"platform recovery action evidence requires {key}")
        evidence_capabilities.update(item for item in value if isinstance(item, str))
    if capability not in evidence_capabilities:
        raise ValueError("platform recovery action evidence must include capability")
    execution = _platform_recovery_mapping(payload, "execution")
    if execution.get("auto_execute") is not False:
        raise ValueError("platform recovery action must not auto execute")
    status = _required_platform_recovery_str(payload, "status")
    user_decision = payload.get("user_decision")
    if status == "open" and user_decision is not None:
        raise ValueError("open platform recovery action must not have user decision")
    resolution = payload.get("resolution")
    if status != "resolved" and resolution is not None:
        raise ValueError("platform recovery action resolution only belongs to resolved status")
    if status in {"acknowledged", "dismissed"}:
        if not isinstance(user_decision, Mapping):
            raise ValueError("platform recovery action decision requires user_decision")
        if user_decision.get("decision") != status:
            raise ValueError("platform recovery action decision must match status")
        if user_decision.get("execution_requested") is not False:
            raise ValueError("platform recovery action decision must not execute repair")
        _required_platform_recovery_str(user_decision, "decided_by")
        _required_platform_recovery_str(user_decision, "decided_at")
    if status == "resolved":
        if not isinstance(resolution, Mapping):
            raise ValueError("resolved platform recovery action requires resolution")
        if resolution.get("health_status") != "ready":
            raise ValueError("resolved platform recovery action requires ready health")
        if resolution.get("readiness_verified") is not True:
            raise ValueError("resolved platform recovery action requires verified readiness")
        if resolution.get("recovered_capability") != capability:
            raise ValueError("resolved platform recovery action capability must match")
        _required_platform_recovery_str(resolution, "resolved_by")
        _required_platform_recovery_str(resolution, "resolved_at")
        _required_platform_recovery_str(resolution, "health_ref")
        for key in (
            "remaining_degraded_capabilities",
            "remaining_missing_capabilities",
            "remaining_os_path_leaks",
        ):
            value = resolution.get(key)
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ValueError(f"resolved platform recovery action requires {key}")
            if capability in value:
                raise ValueError("resolved platform recovery action must not list recovered capability")


def _platform_recovery_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _required_platform_recovery_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_library_dry_run_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_dry_run_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy library dry-run schema_version must be 1.0.0")
    status = _required_legacy_dry_run_str(payload, "status")
    if status not in {"review_ready", "accepted_for_planning", "rejected"}:
        raise ValueError("legacy library dry-run status must be reviewable")
    if _required_legacy_dry_run_str(payload, "mode") != "read_only":
        raise ValueError("legacy library dry-run mode must be read_only")
    legacy_root_uri = _required_legacy_dry_run_str(payload, "legacy_root_uri")
    if not legacy_root_uri.startswith("legacy-readonly://"):
        raise ValueError("legacy library dry-run requires legacy-readonly URI")
    if _looks_like_os_path(legacy_root_uri):
        raise ValueError("legacy library dry-run must not store OS paths")
    candidates = payload.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("legacy library dry-run requires candidates")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ValueError("legacy library dry-run candidate must be an object")
        relative_path = _required_legacy_dry_run_str(candidate, "relative_path")
        if _is_absolute_or_parent_relative(relative_path) or _looks_like_os_path(relative_path):
            raise ValueError("legacy library dry-run candidate path must be relative")
        if not _required_legacy_dry_run_str(candidate, "content_sha256").startswith("sha256:"):
            raise ValueError("legacy library dry-run candidate requires sha256 content hash")
        if candidate.get("imported") is not False:
            raise ValueError("legacy library dry-run candidate must not be imported")
    safety = _legacy_dry_run_mapping(payload, "safety")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy library dry-run must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy library dry-run must not execute migration")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)):
        raise ValueError("legacy library dry-run requires published_outputs")
    if len(published_outputs) != 0:
        raise ValueError("legacy library dry-run must not publish outputs")
    review_decision = payload.get("review_decision")
    if status == "review_ready" and review_decision is not None:
        raise ValueError("review-ready legacy dry-run report must not have review decision")
    if status in {"accepted_for_planning", "rejected"}:
        if not isinstance(review_decision, Mapping):
            raise ValueError("reviewed legacy dry-run report requires review decision")
        if review_decision.get("decision") != status:
            raise ValueError("legacy dry-run review decision must match status")
        if review_decision.get("migration_approved") is not False:
            raise ValueError("legacy dry-run review must not approve migration")
        if review_decision.get("migration_executed") is not False:
            raise ValueError("legacy dry-run review must not execute migration")
        review_outputs = review_decision.get("published_outputs")
        if not isinstance(review_outputs, Sequence) or isinstance(review_outputs, (str, bytes)):
            raise ValueError("legacy dry-run review requires published_outputs")
        if len(review_outputs) != 0:
            raise ValueError("legacy dry-run review must not publish outputs")
        _required_legacy_dry_run_str(review_decision, "reviewed_by")
        _required_legacy_dry_run_str(review_decision, "reviewed_at")


def _legacy_dry_run_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_dry_run_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_plan_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_plan_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration plan schema_version must be 1.0.0")
    if _required_legacy_plan_str(payload, "kind") != "legacy_migration_dry_run_plan":
        raise ValueError("legacy migration plan kind is invalid")
    status = _required_legacy_plan_str(payload, "status")
    if status not in {"plan_ready", "blocked"}:
        raise ValueError("legacy migration plan status is invalid")
    source_status = _required_legacy_plan_str(payload, "source_report_status")
    if source_status not in {"accepted_for_planning", "review_ready", "rejected"}:
        raise ValueError("legacy migration plan source report status is invalid")
    created_at = _required_legacy_plan_str(payload, "created_at")
    if "T" not in created_at or not created_at.endswith("Z"):
        raise ValueError("legacy migration plan created_at must be UTC")
    blocking = payload.get("blocking_check_names")
    if not isinstance(blocking, Sequence) or isinstance(blocking, (str, bytes)):
        raise ValueError("legacy migration plan requires blocking_check_names")
    if status == "plan_ready" and blocking:
        raise ValueError("ready legacy migration plan cannot have blockers")
    if status == "blocked" and not blocking:
        raise ValueError("blocked legacy migration plan requires blockers")
    candidate_count = payload.get("candidate_count")
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool) or candidate_count < 0:
        raise ValueError("legacy migration plan candidate_count must be non-negative")
    total_bytes = payload.get("total_bytes")
    if not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes < 0:
        raise ValueError("legacy migration plan total_bytes must be non-negative")
    groups = payload.get("candidate_groups")
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
        raise ValueError("legacy migration plan requires candidate_groups")
    grouped_count = 0
    grouped_bytes = 0
    for group in groups:
        if not isinstance(group, Mapping):
            raise ValueError("legacy migration plan candidate group must be an object")
        _required_legacy_plan_str(group, "kind")
        count = _required_legacy_plan_int(group, "count")
        size = _required_legacy_plan_int(group, "total_bytes")
        grouped_count += count
        grouped_bytes += size
        refs = group.get("candidate_refs")
        if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or not refs:
            raise ValueError("legacy migration plan candidate group requires refs")
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith("legacy-readonly://") or _looks_like_os_path(ref):
                raise ValueError("legacy migration plan candidate refs must be portable")
    if status == "plan_ready" and grouped_count != candidate_count:
        raise ValueError("legacy migration plan grouped candidate count mismatch")
    if status == "plan_ready" and grouped_bytes != total_bytes:
        raise ValueError("legacy migration plan grouped total bytes mismatch")
    if status == "plan_ready" and candidate_count <= 0:
        raise ValueError("ready legacy migration plan requires candidates")
    policy = _legacy_plan_mapping(payload, "import_policy")
    if policy.get("trust_level") != "imported_unverified":
        raise ValueError("legacy migration plan must keep imported_unverified trust")
    if policy.get("auto_promote_l3") is not False:
        raise ValueError("legacy migration plan must not auto-promote L3")
    if policy.get("preserve_source_refs") is not True:
        raise ValueError("legacy migration plan must preserve source refs")
    safety = _legacy_plan_mapping(payload, "safety")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration plan must not allow legacy writes")
    if safety.get("migration_approved") is not False:
        raise ValueError("legacy migration plan must not approve migration")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration plan must not execute migration")
    if safety.get("job_created") is not False:
        raise ValueError("legacy migration plan must not create jobs")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration plan must not publish memory")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)):
        raise ValueError("legacy migration plan requires published_outputs")
    if published_outputs:
        raise ValueError("legacy migration plan must not publish outputs")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)) or not evidence_refs:
        raise ValueError("legacy migration plan requires evidence refs")
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref or _looks_like_os_path(ref):
            raise ValueError("legacy migration plan evidence refs must be portable")


def _legacy_plan_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_plan_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_plan_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_selection_manifest_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_selection_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration selection manifest schema_version must be 1.0.0")
    if _required_legacy_selection_str(payload, "kind") != "legacy_migration_selection_manifest":
        raise ValueError("legacy migration selection manifest kind is invalid")
    status = _required_legacy_selection_str(payload, "status")
    if status not in {"selection_ready", "blocked"}:
        raise ValueError("legacy migration selection manifest status is invalid")
    source_status = _required_legacy_selection_str(payload, "source_plan_status")
    if source_status not in {"plan_ready", "blocked"}:
        raise ValueError("legacy migration selection manifest source plan status is invalid")
    created_at = _required_legacy_selection_str(payload, "created_at")
    if "T" not in created_at or not created_at.endswith("Z"):
        raise ValueError("legacy migration selection manifest created_at must be UTC")
    blocking = payload.get("blocking_check_names")
    if not isinstance(blocking, Sequence) or isinstance(blocking, (str, bytes)):
        raise ValueError("legacy migration selection manifest requires blocking_check_names")
    if status == "selection_ready" and blocking:
        raise ValueError("ready legacy migration selection manifest cannot have blockers")
    if status == "blocked" and not blocking:
        raise ValueError("blocked legacy migration selection manifest requires blockers")
    available_count = _required_legacy_selection_int(payload, "available_candidate_count")
    selected_count = _required_legacy_selection_int(payload, "selected_candidate_count")
    excluded_count = _required_legacy_selection_int(payload, "excluded_candidate_count")
    selected_refs = _legacy_selection_ref_list(payload, "selected_candidate_refs", allow_empty=status == "blocked")
    excluded_refs = _legacy_selection_ref_list(payload, "excluded_candidate_refs", allow_empty=True)
    if status == "selection_ready" and selected_count <= 0:
        raise ValueError("ready legacy migration selection manifest requires selected candidates")
    if status == "selection_ready" and selected_count != len(selected_refs):
        raise ValueError("legacy migration selection manifest selected count mismatch")
    if status == "selection_ready" and excluded_count != len(excluded_refs):
        raise ValueError("legacy migration selection manifest excluded count mismatch")
    if status == "selection_ready" and available_count != selected_count + excluded_count:
        raise ValueError("legacy migration selection manifest available count mismatch")
    if set(selected_refs).intersection(set(excluded_refs)):
        raise ValueError("legacy migration selection manifest refs cannot be both selected and excluded")
    groups = payload.get("candidate_groups")
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
        raise ValueError("legacy migration selection manifest requires candidate_groups")
    grouped_selected: list[str] = []
    for group in groups:
        if not isinstance(group, Mapping):
            raise ValueError("legacy migration selection manifest group must be an object")
        _required_legacy_selection_str(group, "kind")
        group_count = _required_legacy_selection_int(group, "selected_count")
        group_refs = _legacy_selection_ref_list(group, "selected_candidate_refs", allow_empty=False)
        if group_count != len(group_refs):
            raise ValueError("legacy migration selection manifest group selected count mismatch")
        grouped_selected.extend(group_refs)
    if status == "selection_ready" and tuple(grouped_selected) != tuple(selected_refs):
        raise ValueError("legacy migration selection manifest grouped refs mismatch")
    policy = _legacy_selection_mapping(payload, "import_policy")
    if policy.get("trust_level") != "imported_unverified":
        raise ValueError("legacy migration selection manifest must keep imported_unverified trust")
    if policy.get("auto_promote_l3") is not False:
        raise ValueError("legacy migration selection manifest must not auto-promote L3")
    if policy.get("preserve_source_refs") is not True:
        raise ValueError("legacy migration selection manifest must preserve source refs")
    execution = _legacy_selection_mapping(payload, "execution_policy")
    if execution.get("mode") != "dry_run_selection_only":
        raise ValueError("legacy migration selection manifest must be dry-run selection only")
    if execution.get("requires_rollback_manifest") is not True:
        raise ValueError("legacy migration selection manifest must require rollback manifest")
    if execution.get("requires_explicit_user_approval") is not True:
        raise ValueError("legacy migration selection manifest must require explicit approval")
    safety = _legacy_selection_mapping(payload, "safety")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration selection manifest must not allow legacy writes")
    if safety.get("migration_approved") is not False:
        raise ValueError("legacy migration selection manifest must not approve migration")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration selection manifest must not execute migration")
    if safety.get("job_created") is not False:
        raise ValueError("legacy migration selection manifest must not create jobs")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration selection manifest must not publish memory")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)):
        raise ValueError("legacy migration selection manifest requires published_outputs")
    if published_outputs:
        raise ValueError("legacy migration selection manifest must not publish outputs")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)) or not evidence_refs:
        raise ValueError("legacy migration selection manifest requires evidence refs")
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref or _looks_like_os_path(ref):
            raise ValueError("legacy migration selection manifest evidence refs must be portable")


def _legacy_selection_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_selection_ref_list(
    mapping: Mapping[str, object],
    key: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{key} is required")
    if not allow_empty and not value:
        raise ValueError(f"{key} is required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref.startswith("legacy-readonly://") or _looks_like_os_path(ref):
            raise ValueError("legacy migration selection manifest refs must be portable")
        refs.append(ref)
    if len(set(refs)) != len(refs):
        raise ValueError("legacy migration selection manifest refs must be unique")
    return tuple(refs)


def _required_legacy_selection_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_selection_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_rollback_manifest_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_rollback_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration rollback manifest schema_version must be 1.0.0")
    if _required_legacy_rollback_str(payload, "kind") != "legacy_migration_rollback_manifest":
        raise ValueError("legacy migration rollback manifest kind is invalid")
    status = _required_legacy_rollback_str(payload, "status")
    if status not in {"rollback_ready", "blocked"}:
        raise ValueError("legacy migration rollback manifest status is invalid")
    source_status = _required_legacy_rollback_str(payload, "source_selection_status")
    if source_status not in {"selection_ready", "blocked"}:
        raise ValueError("legacy migration rollback manifest source selection status is invalid")
    _validate_segment("target_namespace_id", _required_legacy_rollback_str(payload, "target_namespace_id"))
    created_at = _required_legacy_rollback_str(payload, "created_at")
    if "T" not in created_at or not created_at.endswith("Z"):
        raise ValueError("legacy migration rollback manifest created_at must be UTC")
    blocking = payload.get("blocking_check_names")
    if not isinstance(blocking, Sequence) or isinstance(blocking, (str, bytes)):
        raise ValueError("legacy migration rollback manifest requires blocking_check_names")
    if status == "rollback_ready" and blocking:
        raise ValueError("ready legacy migration rollback manifest cannot have blockers")
    if status == "blocked" and not blocking:
        raise ValueError("blocked legacy migration rollback manifest requires blockers")
    selected_count = _required_legacy_rollback_int(payload, "selected_candidate_count")
    selected_refs = _legacy_rollback_ref_list(payload, "selected_candidate_refs", allow_empty=status == "blocked")
    if status == "rollback_ready" and selected_count <= 0:
        raise ValueError("ready legacy migration rollback manifest requires selected candidates")
    if status == "rollback_ready" and selected_count != len(selected_refs):
        raise ValueError("legacy migration rollback manifest selected count mismatch")
    rollback_policy = _legacy_rollback_mapping(payload, "rollback_policy")
    if rollback_policy.get("mode") != "namespace_delete_only":
        raise ValueError("legacy migration rollback manifest mode must be namespace_delete_only")
    if rollback_policy.get("delete_target_namespace") is not True:
        raise ValueError("legacy migration rollback manifest must delete target namespace")
    if rollback_policy.get("restore_legacy_library") is not False:
        raise ValueError("legacy migration rollback manifest must not restore legacy library")
    if rollback_policy.get("requires_explicit_user_approval") is not True:
        raise ValueError("legacy migration rollback manifest must require explicit approval")
    actions = payload.get("rollback_actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise ValueError("legacy migration rollback manifest requires rollback actions")
    action_names: list[str] = []
    for action in actions:
        if not isinstance(action, Mapping):
            raise ValueError("legacy migration rollback action must be an object")
        action_name = _required_legacy_rollback_str(action, "action")
        action_names.append(action_name)
        if action.get("executed") is not False:
            raise ValueError("legacy migration rollback action must not be executed")
        if _required_legacy_rollback_str(action, "target_namespace_id") != payload["target_namespace_id"]:
            raise ValueError("legacy migration rollback action namespace mismatch")
        action_refs = _legacy_rollback_ref_list(action, "source_refs", allow_empty=False)
        if tuple(action_refs) != tuple(selected_refs):
            raise ValueError("legacy migration rollback action refs mismatch")
    if status == "rollback_ready" and tuple(action_names) != (
        "delete_target_namespace",
        "verify_legacy_hashes_unchanged",
    ):
        raise ValueError("legacy migration rollback manifest actions must keep expected order")
    safety = _legacy_rollback_mapping(payload, "safety")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration rollback manifest must not allow legacy writes")
    if safety.get("migration_approved") is not False:
        raise ValueError("legacy migration rollback manifest must not approve migration")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration rollback manifest must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration rollback manifest must not execute rollback")
    if safety.get("job_created") is not False:
        raise ValueError("legacy migration rollback manifest must not create jobs")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration rollback manifest must not publish memory")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)):
        raise ValueError("legacy migration rollback manifest requires published_outputs")
    if published_outputs:
        raise ValueError("legacy migration rollback manifest must not publish outputs")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)) or not evidence_refs:
        raise ValueError("legacy migration rollback manifest requires evidence refs")
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref or _looks_like_os_path(ref):
            raise ValueError("legacy migration rollback manifest evidence refs must be portable")


def _legacy_rollback_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_rollback_ref_list(
    mapping: Mapping[str, object],
    key: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{key} is required")
    if not allow_empty and not value:
        raise ValueError(f"{key} is required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref.startswith("legacy-readonly://") or _looks_like_os_path(ref):
            raise ValueError("legacy migration rollback manifest refs must be portable")
        refs.append(ref)
    if len(set(refs)) != len(refs):
        raise ValueError("legacy migration rollback manifest refs must be unique")
    return tuple(refs)


def _required_legacy_rollback_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_rollback_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_execution_approval_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_approval_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration execution approval schema_version must be 1.0.0")
    if _required_legacy_approval_str(payload, "kind") != "legacy_migration_execution_approval":
        raise ValueError("legacy migration execution approval kind is invalid")
    status = _required_legacy_approval_str(payload, "status")
    if status not in {"approved_for_dry_run_execution", "blocked"}:
        raise ValueError("legacy migration execution approval status is invalid")
    approved_at = _required_legacy_approval_str(payload, "approved_at")
    if "T" not in approved_at or not approved_at.endswith("Z"):
        raise ValueError("legacy migration execution approval approved_at must be UTC")
    blocking = payload.get("blocking_check_names")
    if not isinstance(blocking, Sequence) or isinstance(blocking, (str, bytes)):
        raise ValueError("legacy migration execution approval requires blocking_check_names")
    if status == "approved_for_dry_run_execution" and blocking:
        raise ValueError("approved legacy migration execution approval cannot have blockers")
    if status == "blocked" and not blocking:
        raise ValueError("blocked legacy migration execution approval requires blockers")
    selected_count = _required_legacy_approval_int(payload, "selected_candidate_count")
    if status == "approved_for_dry_run_execution" and selected_count <= 0:
        raise ValueError("approved legacy migration execution approval requires selected candidates")
    _validate_segment("target_namespace_id", _required_legacy_approval_str(payload, "target_namespace_id"))
    approval = _legacy_approval_mapping(payload, "approval")
    if approval.get("scope") != "dry_run_execution_handoff_only":
        raise ValueError("legacy migration execution approval scope must be dry-run handoff only")
    user_approved = approval.get("user_approved")
    if status == "approved_for_dry_run_execution" and user_approved is not True:
        raise ValueError("approved legacy migration execution approval requires user approval")
    if status == "blocked" and user_approved is True:
        raise ValueError("blocked legacy migration execution approval cannot carry user approval")
    if approval.get("allows_job_creation") is not False:
        raise ValueError("legacy migration execution approval must not allow job creation")
    if approval.get("allows_legacy_writes") is not False:
        raise ValueError("legacy migration execution approval must not allow legacy writes")
    if approval.get("allows_memory_publication") is not False:
        raise ValueError("legacy migration execution approval must not allow memory publication")
    safety = _legacy_approval_mapping(payload, "safety")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration execution approval must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration execution approval must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration execution approval must not execute rollback")
    if safety.get("job_created") is not False:
        raise ValueError("legacy migration execution approval must not create jobs")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration execution approval must not publish memory")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)):
        raise ValueError("legacy migration execution approval requires published_outputs")
    if published_outputs:
        raise ValueError("legacy migration execution approval must not publish outputs")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)) or len(evidence_refs) < 4:
        raise ValueError("legacy migration execution approval requires evidence refs")
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref or _looks_like_os_path(ref):
            raise ValueError("legacy migration execution approval evidence refs must be portable")


def _legacy_approval_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_approval_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_approval_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_execution_job_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_job_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration execution job schema_version must be 1.0.0")
    if _required_legacy_job_str(payload, "kind") != "job":
        raise ValueError("legacy migration execution job kind is invalid")
    if _required_legacy_job_str(payload, "job_type") != "legacy_migration_execute_dry_run":
        raise ValueError("legacy migration execution job type is invalid")
    if _required_legacy_job_str(payload, "status") != "pending":
        raise ValueError("legacy migration execution job must be pending")
    if _required_legacy_job_str(payload, "execution_mode") != "dry_run":
        raise ValueError("legacy migration execution job must be dry-run")
    if payload.get("attempt") != 0:
        raise ValueError("legacy migration execution job attempt must start at zero")
    if payload.get("max_attempts") != 1:
        raise ValueError("legacy migration execution job must use one dry-run attempt")
    if payload.get("lease") is not None:
        raise ValueError("legacy migration execution job must not be leased")
    if payload.get("requires_worker_verification") is not True:
        raise ValueError("legacy migration execution job requires worker verification")
    if payload.get("completion_blocked_until") != "legacy_migration_worker_dry_run_evidence":
        raise ValueError("legacy migration execution job completion blocker is invalid")
    _validate_segment("target_namespace_id", _required_legacy_job_str(payload, "target_namespace_id"))
    selected_count = _required_legacy_job_int(payload, "selected_candidate_count")
    if selected_count <= 0:
        raise ValueError("legacy migration execution job requires selected candidates")
    for key in (
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "created_by",
    ):
        _required_legacy_job_str(payload, key)
    progress = _legacy_job_mapping(payload, "progress")
    if progress.get("current") != 0 or progress.get("total") != 3 or progress.get("percent") != 0:
        raise ValueError("legacy migration execution job progress must be pending")
    steps = payload.get("steps")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)) or len(steps) != 3:
        raise ValueError("legacy migration execution job requires three pending steps")
    expected_steps = (
        "load_approval_evidence",
        "execute_legacy_migration_dry_run",
        "verify_dry_run_traceability",
    )
    for step, expected_name in zip(steps, expected_steps, strict=True):
        if not isinstance(step, Mapping):
            raise ValueError("legacy migration execution job step must be an object")
        if step.get("name") != expected_name or step.get("status") != "pending":
            raise ValueError("legacy migration execution job steps must be pending")
        if step.get("staged_output_refs") != [] or step.get("log_refs") != [] or step.get("error") is not None:
            raise ValueError("legacy migration execution job steps must not contain outputs")
        for ref in _legacy_job_ref_list(step.get("input_refs"), allow_empty=False):
            if not ref.startswith("crp://") or _looks_like_os_path(ref):
                raise ValueError("legacy migration execution job input refs must be portable")
    safety = _legacy_job_mapping(payload, "safety")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration execution job must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration execution job must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration execution job must not execute rollback")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration execution job must not publish memory")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration execution job must be dry-run only")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration execution job safety must not publish outputs")
    if payload.get("staged_outputs") != [] or payload.get("published_outputs") != []:
        raise ValueError("legacy migration execution job must not contain outputs")
    if payload.get("checkpoint") is not None or payload.get("error") is not None:
        raise ValueError("legacy migration execution job must not contain checkpoint or error")
    for key in ("created_at", "updated_at"):
        value = _required_legacy_job_str(payload, key)
        if "T" not in value or not value.endswith("Z"):
            raise ValueError(f"legacy migration execution job {key} must be UTC")
    for ref in _legacy_job_ref_list(payload.get("log_refs"), allow_empty=False):
        if not ref.startswith("crp://") or _looks_like_os_path(ref):
            raise ValueError("legacy migration execution job log refs must be portable")
    evidence_refs = _legacy_job_ref_list(payload.get("evidence_refs"), allow_empty=False)
    if len(evidence_refs) < 6:
        raise ValueError("legacy migration execution job requires evidence refs")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration execution job evidence refs must be portable")


def _legacy_job_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_job_ref_list(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("legacy migration execution job refs are required")
    if not allow_empty and not value:
        raise ValueError("legacy migration execution job refs are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError("legacy migration execution job refs must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_job_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_job_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_completion_block_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_completion_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration completion block schema_version must be 1.0.0")
    if _required_legacy_completion_block_str(payload, "kind") != "legacy_migration_execution_completion_block":
        raise ValueError("legacy migration completion block kind is invalid")
    if _required_legacy_completion_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration completion block must be blocked")
    if payload.get("completion_allowed") is not False:
        raise ValueError("legacy migration completion block must not allow completion")
    if _required_legacy_completion_block_str(payload, "required_evidence_kind") != "legacy_migration_worker_dry_run_evidence":
        raise ValueError("legacy migration completion block requires worker dry-run evidence")
    if _required_legacy_completion_block_str(payload, "job_status_after_check") != "pending":
        raise ValueError("legacy migration completion block must leave job pending")
    _validate_segment("target_namespace_id", _required_legacy_completion_block_str(payload, "target_namespace_id"))
    selected_count = _required_legacy_completion_block_int(payload, "selected_candidate_count")
    if selected_count <= 0:
        raise ValueError("legacy migration completion block requires selected candidates")
    for key in (
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "checked_by",
    ):
        _required_legacy_completion_block_str(payload, key)
    checked_at = _required_legacy_completion_block_str(payload, "checked_at")
    if "T" not in checked_at or not checked_at.endswith("Z"):
        raise ValueError("legacy migration completion block checked_at must be UTC")
    blockers = payload.get("blocking_check_names")
    if not isinstance(blockers, Sequence) or isinstance(blockers, (str, bytes)) or not blockers:
        raise ValueError("legacy migration completion block requires blockers")
    for blocker in blockers:
        if not isinstance(blocker, str) or not blocker:
            raise ValueError("legacy migration completion block blockers must be strings")
    safety = _legacy_completion_block_mapping(payload, "safety")
    if safety.get("job_completed") is not False:
        raise ValueError("legacy migration completion block must not complete job")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration completion block must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration completion block must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration completion block must not execute rollback")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration completion block must not publish memory")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration completion block must not publish outputs")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)) or len(evidence_refs) < 8:
        raise ValueError("legacy migration completion block requires evidence refs")
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref or _looks_like_os_path(ref):
            raise ValueError("legacy migration completion block evidence refs must be portable")


def _legacy_completion_block_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_completion_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_completion_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_worker_evidence_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_worker_evidence_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration worker evidence schema_version must be 1.0.0")
    if _required_legacy_worker_evidence_str(payload, "kind") != "legacy_migration_worker_dry_run_evidence":
        raise ValueError("legacy migration worker evidence kind is invalid")
    status = _required_legacy_worker_evidence_str(payload, "status")
    if status not in {"ready_for_review", "accepted", "rejected"}:
        raise ValueError("legacy migration worker evidence status is invalid")
    reviewed = payload.get("reviewed")
    review = payload.get("review")
    if status == "ready_for_review":
        if reviewed is not False or review is not None:
            raise ValueError("ready legacy migration worker evidence must not be reviewed")
    else:
        if reviewed is not (status == "accepted"):
            raise ValueError("legacy migration worker evidence reviewed flag mismatch")
        if not isinstance(review, Mapping):
            raise ValueError("reviewed legacy migration worker evidence requires review")
        if review.get("decision") != status:
            raise ValueError("legacy migration worker evidence review decision must match status")
        reviewed_at = _required_legacy_worker_evidence_str(review, "reviewed_at")
        if "T" not in reviewed_at or not reviewed_at.endswith("Z"):
            raise ValueError("legacy migration worker evidence reviewed_at must be UTC")
        _required_legacy_worker_evidence_str(review, "reviewed_by")
    for key in (
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "target_namespace_id",
        "created_by",
    ):
        _required_legacy_worker_evidence_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_worker_evidence_str(payload, "target_namespace_id"))
    selected_count = _required_legacy_worker_evidence_int(payload, "selected_candidate_count")
    if selected_count <= 0:
        raise ValueError("legacy migration worker evidence requires selected candidates")
    for key in ("created_at", "updated_at"):
        value = _required_legacy_worker_evidence_str(payload, key)
        if "T" not in value or not value.endswith("Z"):
            raise ValueError(f"legacy migration worker evidence {key} must be UTC")
    worker_result = _legacy_worker_evidence_mapping(payload, "worker_result")
    if worker_result.get("mode") != "dry_run":
        raise ValueError("legacy migration worker evidence result must be dry-run")
    if worker_result.get("status") not in {"succeeded", "failed"}:
        raise ValueError("legacy migration worker evidence result status is invalid")
    if worker_result.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration worker evidence target namespace mismatch")
    processed_count = _required_legacy_worker_evidence_int(worker_result, "processed_candidate_count")
    if processed_count != selected_count:
        raise ValueError("legacy migration worker evidence candidate count mismatch")
    staged_refs = _legacy_worker_evidence_ref_list(worker_result.get("staged_output_refs"), allow_empty=True)
    for ref in staged_refs:
        if not ref.startswith("crp://") or _looks_like_os_path(ref):
            raise ValueError("legacy migration worker evidence staged refs must be portable")
    published_outputs = worker_result.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration worker evidence must not publish outputs")
    worker_safety = _legacy_worker_evidence_mapping(worker_result, "safety")
    if worker_safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration worker evidence must not allow legacy writes")
    if worker_safety.get("migration_executed") is not False:
        raise ValueError("legacy migration worker evidence must not execute migration")
    if worker_safety.get("memory_published") is not False:
        raise ValueError("legacy migration worker evidence must not publish memory")
    safety = _legacy_worker_evidence_mapping(payload, "safety")
    if safety.get("job_completed") is not False:
        raise ValueError("legacy migration worker evidence must not complete job")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration worker evidence must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration worker evidence must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration worker evidence must not execute rollback")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration worker evidence must not publish memory")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration worker evidence must be dry-run only")
    evidence_published = safety.get("published_outputs")
    if not isinstance(evidence_published, Sequence) or isinstance(evidence_published, (str, bytes)) or evidence_published:
        raise ValueError("legacy migration worker evidence must not publish outputs")
    evidence_refs = _legacy_worker_evidence_ref_list(payload.get("evidence_refs"), allow_empty=False)
    if len(evidence_refs) < 7:
        raise ValueError("legacy migration worker evidence requires evidence refs")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration worker evidence refs must be portable")


def _legacy_worker_evidence_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_worker_evidence_ref_list(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("legacy migration worker evidence refs are required")
    if not allow_empty and not value:
        raise ValueError("legacy migration worker evidence refs are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError("legacy migration worker evidence refs must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_worker_evidence_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_worker_evidence_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_verified_completion_job_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_verified_job_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration verified completion job schema_version must be 1.0.0")
    if _required_legacy_verified_job_str(payload, "kind") != "job":
        raise ValueError("legacy migration verified completion job kind is invalid")
    if _required_legacy_verified_job_str(payload, "job_type") != "legacy_migration_execute_dry_run":
        raise ValueError("legacy migration verified completion job type is invalid")
    if _required_legacy_verified_job_str(payload, "status") != "completed":
        raise ValueError("legacy migration verified completion job must be completed")
    if _required_legacy_verified_job_str(payload, "execution_mode") != "dry_run":
        raise ValueError("legacy migration verified completion job must be dry-run")
    if payload.get("attempt") != 1:
        raise ValueError("legacy migration verified completion job attempt must be one")
    if payload.get("max_attempts") != 1:
        raise ValueError("legacy migration verified completion job must use one dry-run attempt")
    if payload.get("lease") is not None:
        raise ValueError("legacy migration verified completion job must not be leased")
    if payload.get("requires_worker_verification") is not True:
        raise ValueError("legacy migration verified completion job requires worker verification")
    if payload.get("completion_blocked_until") is not None:
        raise ValueError("legacy migration verified completion job must clear completion blocker")
    if _required_legacy_verified_job_str(payload, "memory_publication_blocked_until") != "legacy_migration_verified_import_candidate_review":
        raise ValueError("legacy migration verified completion job must keep memory publication blocked")
    _validate_segment("target_namespace_id", _required_legacy_verified_job_str(payload, "target_namespace_id"))
    selected_count = _required_legacy_verified_job_int(payload, "selected_candidate_count")
    if selected_count <= 0:
        raise ValueError("legacy migration verified completion job requires selected candidates")
    for key in (
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "created_by",
        "completed_by",
        "verified_worker_evidence_id",
        "verified_output_uri",
    ):
        _required_legacy_verified_job_str(payload, key)
    verified_output_uri = _required_legacy_verified_job_str(payload, "verified_output_uri")
    if not verified_output_uri.startswith("crp://") or _looks_like_os_path(verified_output_uri):
        raise ValueError("legacy migration verified completion job output uri must be portable")
    progress = _legacy_verified_job_mapping(payload, "progress")
    if progress.get("current") != 3 or progress.get("total") != 3 or progress.get("percent") != 100:
        raise ValueError("legacy migration verified completion job progress must be completed")
    steps = payload.get("steps")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)) or len(steps) != 3:
        raise ValueError("legacy migration verified completion job requires three completed steps")
    expected_steps = (
        "load_approval_evidence",
        "execute_legacy_migration_dry_run",
        "verify_dry_run_traceability",
    )
    verify_step_seen = False
    for step, expected_name in zip(steps, expected_steps, strict=True):
        if not isinstance(step, Mapping):
            raise ValueError("legacy migration verified completion job step must be an object")
        if step.get("name") != expected_name or step.get("status") != "completed":
            raise ValueError("legacy migration verified completion job steps must be completed")
        if step.get("progress") != 100 or step.get("error") is not None:
            raise ValueError("legacy migration verified completion job steps must be successful")
        for key in ("started_at", "completed_at"):
            value = _required_legacy_verified_job_str(step, key)
            if "T" not in value or not value.endswith("Z"):
                raise ValueError(f"legacy migration verified completion job step {key} must be UTC")
        for ref in _legacy_verified_job_ref_list(step.get("input_refs"), allow_empty=False):
            if not ref.startswith("crp://") or _looks_like_os_path(ref):
                raise ValueError("legacy migration verified completion job input refs must be portable")
        for ref in _legacy_verified_job_ref_list(step.get("staged_output_refs"), allow_empty=True):
            if not ref.startswith("crp://") or _looks_like_os_path(ref):
                raise ValueError("legacy migration verified completion job staged refs must be portable")
            if expected_name == "verify_dry_run_traceability" and ref == verified_output_uri:
                verify_step_seen = True
    if not verify_step_seen:
        raise ValueError("legacy migration verified completion job verify step requires verified output")
    safety = _legacy_verified_job_mapping(payload, "safety")
    if safety.get("job_completed") is not True:
        raise ValueError("legacy migration verified completion job must mark job completed")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration verified completion job must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration verified completion job must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration verified completion job must not execute rollback")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration verified completion job must not publish memory")
    if safety.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration verified completion job must not allow memory publication")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration verified completion job must be dry-run only")
    if payload.get("staged_outputs") != []:
        raise ValueError("legacy migration verified completion job must not retain staged outputs")
    job_outputs = _legacy_verified_job_output_list(payload.get("published_outputs"))
    safety_outputs = _legacy_verified_job_output_list(safety.get("published_outputs"))
    if job_outputs != safety_outputs:
        raise ValueError("legacy migration verified completion job safety outputs must match job outputs")
    if len(job_outputs) != 1:
        raise ValueError("legacy migration verified completion job requires one verified output")
    output = job_outputs[0]
    if output.get("kind") != "other" or output.get("published") is not True or output.get("uri") != verified_output_uri:
        raise ValueError("legacy migration verified completion job verified output is invalid")
    if payload.get("checkpoint") is not None or payload.get("error") is not None:
        raise ValueError("legacy migration verified completion job must not contain checkpoint or error")
    for key in ("created_at", "updated_at", "completed_at"):
        value = _required_legacy_verified_job_str(payload, key)
        if "T" not in value or not value.endswith("Z"):
            raise ValueError(f"legacy migration verified completion job {key} must be UTC")
    for ref in _legacy_verified_job_ref_list(payload.get("log_refs"), allow_empty=False):
        if not ref.startswith("crp://") or _looks_like_os_path(ref):
            raise ValueError("legacy migration verified completion job log refs must be portable")
    evidence_refs = _legacy_verified_job_ref_list(payload.get("evidence_refs"), allow_empty=False)
    if len(evidence_refs) < 10:
        raise ValueError("legacy migration verified completion job requires evidence refs")
    if "R072:legacy-migration-execution-job-verified-completion" not in evidence_refs:
        raise ValueError("legacy migration verified completion job requires R072 evidence ref")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration verified completion job evidence refs must be portable")


def _legacy_verified_job_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_verified_job_ref_list(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("legacy migration verified completion job refs are required")
    if not allow_empty and not value:
        raise ValueError("legacy migration verified completion job refs are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError("legacy migration verified completion job refs must be strings")
        refs.append(ref)
    return tuple(refs)


def _legacy_verified_job_output_list(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("legacy migration verified completion job requires outputs")
    outputs: list[Mapping[str, object]] = []
    for output in value:
        if not isinstance(output, Mapping):
            raise ValueError("legacy migration verified completion job output must be an object")
        outputs.append(output)
    return tuple(outputs)


def _required_legacy_verified_job_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_verified_job_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_import_candidate_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_import_candidate_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration import candidate schema_version must be 1.0.0")
    if _required_legacy_import_candidate_str(payload, "kind") != "legacy_migration_verified_import_candidate":
        raise ValueError("legacy migration import candidate kind is invalid")
    status = _required_legacy_import_candidate_str(payload, "status")
    if status not in {"pending_review", "accepted_for_memory_publication_review", "rejected"}:
        raise ValueError("legacy migration import candidate status is invalid")
    reviewed = payload.get("reviewed")
    review = payload.get("review")
    if status == "pending_review":
        if reviewed is not False or review is not None:
            raise ValueError("pending legacy migration import candidate must not be reviewed")
    else:
        if reviewed is not (status == "accepted_for_memory_publication_review"):
            raise ValueError("legacy migration import candidate reviewed flag mismatch")
        if not isinstance(review, Mapping):
            raise ValueError("reviewed legacy migration import candidate requires review")
        if review.get("decision") != status:
            raise ValueError("legacy migration import candidate review decision must match status")
        _required_legacy_import_candidate_str(review, "reviewed_by")
        reviewed_at = _required_legacy_import_candidate_str(review, "reviewed_at")
        if "T" not in reviewed_at or not reviewed_at.endswith("Z"):
            raise ValueError("legacy migration import candidate reviewed_at must be UTC")
    for key in (
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "verified_output_uri",
        "target_namespace_id",
        "created_by",
    ):
        _required_legacy_import_candidate_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_import_candidate_str(payload, "target_namespace_id"))
    selected_count = _required_legacy_import_candidate_int(payload, "selected_candidate_count")
    if selected_count <= 0:
        raise ValueError("legacy migration import candidate requires selected candidates")
    verified_output_uri = _required_legacy_import_candidate_str(payload, "verified_output_uri")
    if not verified_output_uri.startswith("crp://") or _looks_like_os_path(verified_output_uri):
        raise ValueError("legacy migration import candidate output uri must be portable")
    for key in ("created_at", "updated_at"):
        value = _required_legacy_import_candidate_str(payload, key)
        if "T" not in value or not value.endswith("Z"):
            raise ValueError(f"legacy migration import candidate {key} must be UTC")
    import_policy = _legacy_import_candidate_mapping(payload, "import_policy")
    if import_policy.get("mode") != "verified_dry_run_import_candidate":
        raise ValueError("legacy migration import candidate policy mode is invalid")
    if import_policy.get("requires_memory_publication_review") is not True:
        raise ValueError("legacy migration import candidate must require Memory publication review")
    if import_policy.get("allows_memory_publication") is not False:
        raise ValueError("legacy migration import candidate must not allow Memory publication")
    if import_policy.get("allows_legacy_writes") is not False:
        raise ValueError("legacy migration import candidate must not allow legacy writes")
    policy_output = _required_legacy_import_candidate_str(import_policy, "verified_output_ref")
    if policy_output != verified_output_uri:
        raise ValueError("legacy migration import candidate output ref mismatch")
    for key in ("target_namespace_ref", "verified_output_ref"):
        ref = _required_legacy_import_candidate_str(import_policy, key)
        if not ref.startswith("crp://") or _looks_like_os_path(ref):
            raise ValueError("legacy migration import candidate policy refs must be portable")
    safety = _legacy_import_candidate_mapping(payload, "safety")
    if safety.get("import_candidate_created") is not True:
        raise ValueError("legacy migration import candidate must mark candidate created")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration import candidate must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration import candidate must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration import candidate must not execute rollback")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration import candidate must not publish Memory")
    if safety.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration import candidate must not allow Memory publication")
    if safety.get("memory_atoms_written") is not False:
        raise ValueError("legacy migration import candidate must not write Memory atoms")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration import candidate must be dry-run only")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration import candidate must not publish outputs")
    evidence_refs = _legacy_import_candidate_ref_list(payload.get("evidence_refs"))
    if len(evidence_refs) < 10:
        raise ValueError("legacy migration import candidate requires evidence refs")
    if "R073:legacy-migration-verified-import-candidate-review" not in evidence_refs:
        raise ValueError("legacy migration import candidate requires R073 evidence ref")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration import candidate evidence refs must be portable")


def _legacy_import_candidate_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_import_candidate_ref_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("legacy migration import candidate refs are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError("legacy migration import candidate refs must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_import_candidate_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_import_candidate_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_memory_publication_block_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_memory_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration Memory publication block schema_version must be 1.0.0")
    if _required_legacy_memory_block_str(payload, "kind") != "legacy_migration_memory_publication_block":
        raise ValueError("legacy migration Memory publication block kind is invalid")
    if _required_legacy_memory_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration Memory publication block status must be blocked")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration Memory publication block must not allow Memory publication")
    for key in (
        "source_import_candidate_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "verified_output_uri",
        "target_namespace_id",
        "checked_by",
        "required_next_gate",
    ):
        _required_legacy_memory_block_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_memory_block_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_memory_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration Memory publication block output uri must be portable")
    checked_at = _required_legacy_memory_block_str(payload, "checked_at")
    if "T" not in checked_at or not checked_at.endswith("Z"):
        raise ValueError("legacy migration Memory publication block checked_at must be UTC")
    if _required_legacy_memory_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration Memory publication block requires selected candidates")
    blockers = _legacy_memory_block_ref_list(payload.get("blocking_check_names"), "blocking checks")
    required_blockers = {
        "memory_publication_review_not_implemented",
        "draft_memory_proposal_not_created",
        "provenance_recheck_required",
    }
    if not required_blockers.issubset(set(blockers)):
        raise ValueError("legacy migration Memory publication block requires blocker checks")
    if payload.get("required_next_gate") != "legacy_migration_accepted_import_candidate_to_draft_memory_proposal_guard":
        raise ValueError("legacy migration Memory publication block next gate is invalid")
    safety = _legacy_memory_block_mapping(payload, "safety")
    if safety.get("block_created") is not True:
        raise ValueError("legacy migration Memory publication block must mark block created")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration Memory publication block must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration Memory publication block must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration Memory publication block must not execute rollback")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration Memory publication block must not publish Memory")
    if safety.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration Memory publication block must not allow Memory publication")
    if safety.get("memory_atoms_written") is not False:
        raise ValueError("legacy migration Memory publication block must not write Memory atoms")
    if safety.get("draft_memory_written") is not False:
        raise ValueError("legacy migration Memory publication block must not write draft Memory")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration Memory publication block must be dry-run only")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration Memory publication block must not publish outputs")
    evidence_refs = _legacy_memory_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    if len(evidence_refs) < 12:
        raise ValueError("legacy migration Memory publication block requires evidence refs")
    if "R073:legacy-migration-verified-import-candidate-review" not in evidence_refs:
        raise ValueError("legacy migration Memory publication block requires R073 evidence ref")
    if "R074:legacy-migration-memory-publication-blocker" not in evidence_refs:
        raise ValueError("legacy migration Memory publication block requires R074 evidence ref")
    if f"{payload['source_import_candidate_id']}:accepted-import-candidate" not in evidence_refs:
        raise ValueError("legacy migration Memory publication block requires accepted candidate ref")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration Memory publication block evidence refs must be portable")


def _legacy_memory_block_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_memory_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration Memory publication block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration Memory publication block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_memory_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_memory_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_draft_memory_proposal_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_memory_proposal_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration draft Memory proposal schema_version must be 1.0.0")
    if _required_legacy_memory_proposal_str(payload, "kind") != "legacy_migration_draft_memory_proposal":
        raise ValueError("legacy migration draft Memory proposal kind is invalid")
    status = _required_legacy_memory_proposal_str(payload, "status")
    if status not in {"pending_review", "accepted_for_staging_review", "rejected"}:
        raise ValueError("legacy migration draft Memory proposal status is invalid")
    reviewed = payload.get("reviewed")
    review = payload.get("review")
    if status == "pending_review":
        if reviewed is not False or review is not None:
            raise ValueError("legacy migration draft Memory proposal must not be reviewed")
    else:
        if reviewed is not (status == "accepted_for_staging_review"):
            raise ValueError("legacy migration draft Memory proposal reviewed flag mismatch")
        if not isinstance(review, Mapping):
            raise ValueError("reviewed legacy migration draft Memory proposal requires review")
        if review.get("decision") != status:
            raise ValueError("legacy migration draft Memory proposal review decision must match status")
        _required_legacy_memory_proposal_str(review, "reviewed_by")
        reviewed_at = _required_legacy_memory_proposal_str(review, "reviewed_at")
        if "T" not in reviewed_at or not reviewed_at.endswith("Z"):
            raise ValueError("legacy migration draft Memory proposal reviewed_at must be UTC")
    for key in (
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "verified_output_uri",
        "target_namespace_id",
        "created_by",
        "required_next_gate",
    ):
        _required_legacy_memory_proposal_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_memory_proposal_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_memory_proposal_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration draft Memory proposal output uri must be portable")
    if _required_legacy_memory_proposal_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration draft Memory proposal requires selected candidates")
    for key in ("created_at", "updated_at"):
        value = _required_legacy_memory_proposal_str(payload, key)
        if "T" not in value or not value.endswith("Z"):
            raise ValueError(f"legacy migration draft Memory proposal {key} must be UTC")
    expected_next_gate = (
        "legacy_migration_approved_draft_proposal_to_staged_atom_scenario_guard"
        if status == "accepted_for_staging_review"
        else "legacy_migration_draft_memory_proposal_review_promotion_blocker"
    )
    if payload.get("required_next_gate") != expected_next_gate:
        raise ValueError("legacy migration draft Memory proposal next gate is invalid")
    policy = _legacy_memory_proposal_mapping(payload, "proposal_policy")
    if policy.get("mode") != "draft_only_memory_proposal":
        raise ValueError("legacy migration draft Memory proposal policy mode is invalid")
    if policy.get("requires_publication_block") is not True:
        raise ValueError("legacy migration draft Memory proposal must require publication block")
    if policy.get("requires_provenance_review") is not True:
        raise ValueError("legacy migration draft Memory proposal must require provenance review")
    if policy.get("allows_memory_publication") is not False:
        raise ValueError("legacy migration draft Memory proposal must not allow Memory publication")
    if policy.get("allows_l1_l2_publication") is not False:
        raise ValueError("legacy migration draft Memory proposal must not allow L1/L2 publication")
    if policy.get("allows_legacy_writes") is not False:
        raise ValueError("legacy migration draft Memory proposal must not allow legacy writes")
    block_ref = _required_legacy_memory_proposal_str(policy, "source_block_ref")
    if block_ref != f"{payload['source_memory_publication_block_id']}:memory-publication-block":
        raise ValueError("legacy migration draft Memory proposal block ref mismatch")
    if _required_legacy_memory_proposal_str(policy, "verified_output_ref") != output_uri:
        raise ValueError("legacy migration draft Memory proposal output ref mismatch")
    scope = _legacy_memory_proposal_mapping(payload, "proposal_scope")
    layers = _legacy_memory_proposal_ref_list(scope.get("target_layers"), "target layers")
    if set(layers) != {"L1 Atom", "L2 Scenario"}:
        raise ValueError("legacy migration draft Memory proposal target layers are invalid")
    if scope.get("proposed_item_count") != payload.get("selected_candidate_count"):
        raise ValueError("legacy migration draft Memory proposal item count mismatch")
    for key in ("draft_memory_refs", "published_memory_refs"):
        refs = scope.get(key)
        if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or refs:
            raise ValueError(f"legacy migration draft Memory proposal {key} must be empty")
    safety = _legacy_memory_proposal_mapping(payload, "safety")
    if safety.get("proposal_created") is not True:
        raise ValueError("legacy migration draft Memory proposal must mark proposal created")
    if safety.get("draft_only") is not True:
        raise ValueError("legacy migration draft Memory proposal must be draft only")
    if safety.get("source_block_required") is not True:
        raise ValueError("legacy migration draft Memory proposal must require source block")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration draft Memory proposal must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration draft Memory proposal must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration draft Memory proposal must not execute rollback")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration draft Memory proposal must not publish Memory")
    if safety.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration draft Memory proposal must not allow Memory publication")
    if safety.get("l1_l2_publication_allowed") is not False:
        raise ValueError("legacy migration draft Memory proposal must not allow L1/L2 publication")
    if safety.get("memory_atoms_written") is not False:
        raise ValueError("legacy migration draft Memory proposal must not write Memory atoms")
    if safety.get("staging_atoms_written") is not False:
        raise ValueError("legacy migration draft Memory proposal must not write staging atoms")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration draft Memory proposal must be dry-run only")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration draft Memory proposal must not publish outputs")
    evidence_refs = _legacy_memory_proposal_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R073:legacy-migration-verified-import-candidate-review",
        "R074:legacy-migration-memory-publication-blocker",
        "R075:legacy-migration-draft-memory-proposal-guard",
        f"{payload['source_import_candidate_id']}:accepted-import-candidate",
        f"{payload['source_memory_publication_block_id']}:memory-publication-block",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration draft Memory proposal requires provenance evidence refs")
    if status != "pending_review" and "R076:legacy-migration-draft-memory-proposal-review-promotion-blocker" not in evidence_refs:
        raise ValueError("legacy migration draft Memory proposal requires R076 evidence ref")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration draft Memory proposal evidence refs must be portable")


def _legacy_memory_proposal_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_memory_proposal_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration draft Memory proposal {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration draft Memory proposal {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_memory_proposal_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_memory_proposal_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_memory_proposal_promotion_block_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_promotion_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration proposal promotion block schema_version must be 1.0.0")
    if _required_legacy_promotion_block_str(payload, "kind") != "legacy_migration_memory_proposal_promotion_block":
        raise ValueError("legacy migration proposal promotion block kind is invalid")
    if _required_legacy_promotion_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration proposal promotion block status must be blocked")
    if payload.get("staging_allowed") is not False:
        raise ValueError("legacy migration proposal promotion block must not allow staging")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration proposal promotion block must not allow Memory publication")
    for key in (
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "verified_output_uri",
        "target_namespace_id",
        "reviewed_by",
        "required_next_gate",
    ):
        _required_legacy_promotion_block_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_promotion_block_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_promotion_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration proposal promotion block output uri must be portable")
    reviewed_at = _required_legacy_promotion_block_str(payload, "reviewed_at")
    if "T" not in reviewed_at or not reviewed_at.endswith("Z"):
        raise ValueError("legacy migration proposal promotion block reviewed_at must be UTC")
    if _required_legacy_promotion_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration proposal promotion block requires selected candidates")
    blockers = _legacy_promotion_block_ref_list(payload.get("blocking_check_names"), "blocking checks")
    required_blockers = {
        "staging_guard_not_implemented",
        "atom_scenario_staging_not_created",
        "provenance_recheck_required",
    }
    if not required_blockers.issubset(set(blockers)):
        raise ValueError("legacy migration proposal promotion block requires blocker checks")
    if payload.get("required_next_gate") != "legacy_migration_approved_draft_proposal_to_staged_atom_scenario_guard":
        raise ValueError("legacy migration proposal promotion block next gate is invalid")
    safety = _legacy_promotion_block_mapping(payload, "safety")
    if safety.get("promotion_block_created") is not True:
        raise ValueError("legacy migration proposal promotion block must mark block created")
    if safety.get("proposal_reviewed") is not True:
        raise ValueError("legacy migration proposal promotion block requires reviewed proposal")
    if safety.get("legacy_write_allowed") is not False:
        raise ValueError("legacy migration proposal promotion block must not allow legacy writes")
    if safety.get("migration_executed") is not False:
        raise ValueError("legacy migration proposal promotion block must not execute migration")
    if safety.get("rollback_executed") is not False:
        raise ValueError("legacy migration proposal promotion block must not execute rollback")
    if safety.get("memory_published") is not False:
        raise ValueError("legacy migration proposal promotion block must not publish Memory")
    if safety.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration proposal promotion block must not allow Memory publication")
    if safety.get("l1_l2_publication_allowed") is not False:
        raise ValueError("legacy migration proposal promotion block must not allow L1/L2 publication")
    if safety.get("memory_atoms_written") is not False:
        raise ValueError("legacy migration proposal promotion block must not write Memory atoms")
    if safety.get("staging_atoms_written") is not False:
        raise ValueError("legacy migration proposal promotion block must not write staging atoms")
    if safety.get("staging_allowed") is not False:
        raise ValueError("legacy migration proposal promotion block must not allow staging")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration proposal promotion block must be dry-run only")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration proposal promotion block must not publish outputs")
    evidence_refs = _legacy_promotion_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R075:legacy-migration-draft-memory-proposal-guard",
        "R076:legacy-migration-draft-memory-proposal-review-promotion-blocker",
        f"{payload['source_proposal_id']}:accepted_for_staging_review",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration proposal promotion block requires provenance evidence refs")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration proposal promotion block evidence refs must be portable")


def _legacy_promotion_block_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_promotion_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration proposal promotion block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration proposal promotion block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_promotion_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_promotion_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_staged_memory_candidate_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_staged_candidate_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration staged Memory candidate schema_version must be 1.0.0")
    if _required_legacy_staged_candidate_str(payload, "kind") != "legacy_migration_staged_atom_scenario_candidate":
        raise ValueError("legacy migration staged Memory candidate kind is invalid")
    status = _required_legacy_staged_candidate_str(payload, "status")
    if status not in {"staged_for_review", "accepted_for_final_memory_review", "rejected"}:
        raise ValueError("legacy migration staged Memory candidate status is invalid")
    review = payload.get("review")
    if status == "staged_for_review":
        if payload.get("reviewed") is not False or review is not None:
            raise ValueError("legacy migration staged Memory candidate must be unreviewed")
    else:
        if payload.get("reviewed") is not (status == "accepted_for_final_memory_review"):
            raise ValueError("legacy migration staged Memory candidate review flag is invalid")
        if not isinstance(review, Mapping):
            raise ValueError("legacy migration staged Memory candidate review is required")
        if review.get("decision") != status:
            raise ValueError("legacy migration staged Memory candidate review decision is invalid")
        _required_legacy_staged_candidate_str(review, "reviewed_by")
        reviewed_at = _required_legacy_staged_candidate_str(review, "reviewed_at")
        if "T" not in reviewed_at or not reviewed_at.endswith("Z"):
            raise ValueError("legacy migration staged Memory candidate review timestamp must be UTC")
    for key in (
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "created_by",
        "created_at",
        "updated_at",
        "required_next_gate",
    ):
        _required_legacy_staged_candidate_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_staged_candidate_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_staged_candidate_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration staged Memory candidate output uri must be portable")
    for key in ("created_at", "updated_at"):
        timestamp = _required_legacy_staged_candidate_str(payload, key)
        if "T" not in timestamp or not timestamp.endswith("Z"):
            raise ValueError("legacy migration staged Memory candidate timestamps must be UTC")
    selected_count = _required_legacy_staged_candidate_int(payload, "selected_candidate_count")
    if selected_count <= 0:
        raise ValueError("legacy migration staged Memory candidate requires selected candidates")
    expected_next_gate = (
        "legacy_migration_final_memory_publication_blocker"
        if status == "accepted_for_final_memory_review"
        else "legacy_migration_staged_atom_scenario_review_blocker"
    )
    if payload.get("required_next_gate") != expected_next_gate:
        raise ValueError("legacy migration staged Memory candidate next gate is invalid")
    policy = _legacy_staged_candidate_mapping(payload, "staging_policy")
    if policy.get("mode") != "staging_only_atom_scenario_candidate":
        raise ValueError("legacy migration staged Memory candidate policy mode is invalid")
    if policy.get("requires_promotion_block") is not True:
        raise ValueError("legacy migration staged Memory candidate must require promotion block")
    if policy.get("requires_staged_candidate_review") is not True:
        raise ValueError("legacy migration staged Memory candidate must require staged review")
    if policy.get("allows_memory_publication") is not False:
        raise ValueError("legacy migration staged Memory candidate must not allow Memory publication")
    if policy.get("allows_l1_l2_publication") is not False:
        raise ValueError("legacy migration staged Memory candidate must not allow L1/L2 publication")
    if policy.get("allows_legacy_writes") is not False:
        raise ValueError("legacy migration staged Memory candidate must not allow legacy writes")
    expected_block_ref = f"{payload['source_promotion_block_id']}:proposal-promotion-block"
    if policy.get("source_promotion_block_ref") != expected_block_ref:
        raise ValueError("legacy migration staged Memory candidate promotion block ref is invalid")
    staged = _legacy_staged_candidate_mapping(payload, "staged_candidates")
    if staged.get("layers") != ["L1 Atom", "L2 Scenario"]:
        raise ValueError("legacy migration staged Memory candidate must stage L1 Atom and L2 Scenario")
    if staged.get("source_item_count") != selected_count:
        raise ValueError("legacy migration staged Memory candidate source item count must match")
    for key in ("atom_candidate_ref", "scenario_candidate_ref"):
        ref = staged.get(key)
        if not isinstance(ref, str) or not ref.startswith("crp://") or _looks_like_os_path(ref):
            raise ValueError("legacy migration staged Memory candidate refs must be portable")
    if staged.get("published_memory_refs") != []:
        raise ValueError("legacy migration staged Memory candidate must not publish Memory refs")
    safety = _legacy_staged_candidate_mapping(payload, "safety")
    if safety.get("staged_candidate_created") is not True:
        raise ValueError("legacy migration staged Memory candidate must mark candidate created")
    if safety.get("staging_only") is not True:
        raise ValueError("legacy migration staged Memory candidate must remain staging-only")
    if safety.get("source_promotion_block_required") is not True:
        raise ValueError("legacy migration staged Memory candidate must require promotion block")
    for key in (
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration staged Memory candidate must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration staged Memory candidate must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration staged Memory candidate must be dry-run-only")
    evidence_refs = _legacy_staged_candidate_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R075:legacy-migration-draft-memory-proposal-guard",
        "R076:legacy-migration-draft-memory-proposal-review-promotion-blocker",
        "R077:legacy-migration-approved-draft-proposal-to-staged-atom-scenario-guard",
        f"{payload['source_proposal_id']}:accepted_for_staging_review",
        expected_block_ref,
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration staged Memory candidate requires provenance evidence refs")
    if status != "staged_for_review":
        review_ref = f"{payload['id']}:{status}"
        if review_ref not in evidence_refs or "R078:legacy-migration-staged-atom-scenario-review-blocker" not in evidence_refs:
            raise ValueError("legacy migration staged Memory candidate requires review evidence refs")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration staged Memory candidate evidence refs must be portable")


def _legacy_staged_candidate_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_staged_candidate_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration staged Memory candidate {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration staged Memory candidate {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_staged_candidate_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_staged_candidate_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_staged_memory_candidate_review_block_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_staged_review_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration staged Memory candidate review block schema_version must be 1.0.0")
    if _required_legacy_staged_review_block_str(payload, "kind") != "legacy_migration_staged_atom_scenario_review_block":
        raise ValueError("legacy migration staged Memory candidate review block kind is invalid")
    if _required_legacy_staged_review_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration staged Memory candidate review block status must be blocked")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration staged Memory candidate review block must not allow Memory publication")
    for key in (
        "source_staged_candidate_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "reviewed_by",
        "reviewed_at",
        "required_next_gate",
    ):
        _required_legacy_staged_review_block_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_staged_review_block_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_staged_review_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration staged Memory candidate review block output uri must be portable")
    reviewed_at = _required_legacy_staged_review_block_str(payload, "reviewed_at")
    if "T" not in reviewed_at or not reviewed_at.endswith("Z"):
        raise ValueError("legacy migration staged Memory candidate review block reviewed_at must be UTC")
    if _required_legacy_staged_review_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration staged Memory candidate review block requires selected candidates")
    blockers = _legacy_staged_review_block_ref_list(payload.get("blocking_check_names"), "blocking checks")
    required_blockers = {
        "final_memory_publication_guard_not_implemented",
        "l1_l2_memory_publish_not_approved",
        "source_provenance_final_recheck_required",
    }
    if not required_blockers.issubset(set(blockers)):
        raise ValueError("legacy migration staged Memory candidate review block requires blocker checks")
    if payload.get("required_next_gate") != "legacy_migration_final_memory_publication_blocker":
        raise ValueError("legacy migration staged Memory candidate review block next gate is invalid")
    safety = _legacy_staged_review_block_mapping(payload, "safety")
    if safety.get("review_block_created") is not True:
        raise ValueError("legacy migration staged Memory candidate review block must mark block created")
    if safety.get("staged_candidate_reviewed") is not True:
        raise ValueError("legacy migration staged Memory candidate review block requires reviewed staged candidate")
    if safety.get("final_memory_publication_blocked") is not True:
        raise ValueError("legacy migration staged Memory candidate review block must block final publication")
    for key in (
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration staged Memory candidate review block must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration staged Memory candidate review block must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration staged Memory candidate review block must be dry-run-only")
    evidence_refs = _legacy_staged_review_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R077:legacy-migration-approved-draft-proposal-to-staged-atom-scenario-guard",
        "R078:legacy-migration-staged-atom-scenario-review-blocker",
        f"{payload['source_staged_candidate_id']}:accepted_for_final_memory_review",
        f"{payload['source_promotion_block_id']}:proposal-promotion-block",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration staged Memory candidate review block requires provenance evidence refs")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration staged Memory candidate review block evidence refs must be portable")


def _legacy_staged_review_block_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_staged_review_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration staged Memory candidate review block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration staged Memory candidate review block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_staged_review_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_staged_review_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_memory_publication_block_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_final_publication_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final Memory publication block schema_version must be 1.0.0")
    if _required_legacy_final_publication_block_str(payload, "kind") != "legacy_migration_final_memory_publication_block":
        raise ValueError("legacy migration final Memory publication block kind is invalid")
    if _required_legacy_final_publication_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final Memory publication block status must be blocked")
    if payload.get("final_approval_required") is not True:
        raise ValueError("legacy migration final Memory publication block must require final approval")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration final Memory publication block must not allow Memory publication")
    for key in (
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "created_by",
        "created_at",
        "required_next_gate",
    ):
        _required_legacy_final_publication_block_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_final_publication_block_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_final_publication_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final Memory publication block output uri must be portable")
    created_at = _required_legacy_final_publication_block_str(payload, "created_at")
    if "T" not in created_at or not created_at.endswith("Z"):
        raise ValueError("legacy migration final Memory publication block created_at must be UTC")
    if _required_legacy_final_publication_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final Memory publication block requires selected candidates")
    blockers = _legacy_final_publication_block_ref_list(payload.get("blocking_check_names"), "blocking checks")
    required_blockers = {
        "explicit_final_approval_missing",
        "memory_publish_commit_guard_not_implemented",
        "l1_l2_memory_write_not_authorized",
        "source_provenance_final_recheck_required",
    }
    if not required_blockers.issubset(set(blockers)):
        raise ValueError("legacy migration final Memory publication block requires blocker checks")
    if payload.get("required_next_gate") != "legacy_migration_final_publication_approval_guard":
        raise ValueError("legacy migration final Memory publication block next gate is invalid")
    safety = _legacy_final_publication_block_mapping(payload, "safety")
    if safety.get("final_publication_block_created") is not True:
        raise ValueError("legacy migration final Memory publication block must mark block created")
    if safety.get("staged_candidate_reviewed") is not True:
        raise ValueError("legacy migration final Memory publication block requires reviewed staged candidate")
    if safety.get("staged_review_block_required") is not True:
        raise ValueError("legacy migration final Memory publication block requires staged review block")
    if safety.get("final_approval_required") is not True:
        raise ValueError("legacy migration final Memory publication block must require final approval safety")
    for key in (
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final Memory publication block must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final Memory publication block must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final Memory publication block must be dry-run-only")
    evidence_refs = _legacy_final_publication_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R077:legacy-migration-approved-draft-proposal-to-staged-atom-scenario-guard",
        "R078:legacy-migration-staged-atom-scenario-review-blocker",
        "R079:legacy-migration-final-memory-publication-blocker",
        f"{payload['source_staged_candidate_id']}:accepted_for_final_memory_review",
        f"{payload['source_staged_review_block_id']}:staged-review-block",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final Memory publication block requires provenance evidence refs")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final Memory publication block evidence refs must be portable")


def _legacy_final_publication_block_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_publication_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final Memory publication block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final Memory publication block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_publication_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_publication_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_publication_approval_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_final_approval_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final publication approval schema_version must be 1.0.0")
    if _required_legacy_final_approval_str(payload, "kind") != "legacy_migration_final_publication_approval":
        raise ValueError("legacy migration final publication approval kind is invalid")
    if _required_legacy_final_approval_str(payload, "status") != "approved_for_publish_transaction_dry_run":
        raise ValueError("legacy migration final publication approval status is invalid")
    if payload.get("final_approval_recorded") is not True:
        raise ValueError("legacy migration final publication approval must record final approval")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration final publication approval must not allow Memory publication")
    for key in (
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "approved_by",
        "approved_at",
        "approval_note",
        "required_next_gate",
    ):
        _required_legacy_final_approval_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_final_approval_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_final_approval_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final publication approval output uri must be portable")
    approved_at = _required_legacy_final_approval_str(payload, "approved_at")
    if "T" not in approved_at or not approved_at.endswith("Z"):
        raise ValueError("legacy migration final publication approval approved_at must be UTC")
    if _required_legacy_final_approval_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final publication approval requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_publication_transaction_dry_run_guard":
        raise ValueError("legacy migration final publication approval next gate is invalid")
    scope = _legacy_final_approval_mapping(payload, "approval_scope")
    if scope.get("allows_publish_transaction_dry_run") is not True:
        raise ValueError("legacy migration final publication approval must allow transaction dry-run only")
    if scope.get("allows_memory_publication") is not False:
        raise ValueError("legacy migration final publication approval must not allow Memory publication")
    if scope.get("allows_l1_l2_publication") is not False:
        raise ValueError("legacy migration final publication approval must not allow L1/L2 publication")
    if scope.get("allows_legacy_writes") is not False:
        raise ValueError("legacy migration final publication approval must not allow legacy writes")
    if scope.get("requires_transaction_dry_run") is not True:
        raise ValueError("legacy migration final publication approval must require transaction dry-run")
    if scope.get("requires_final_commit_guard") is not True:
        raise ValueError("legacy migration final publication approval must require final commit guard")
    safety = _legacy_final_approval_mapping(payload, "safety")
    if safety.get("final_approval_recorded") is not True:
        raise ValueError("legacy migration final publication approval safety must record approval")
    if safety.get("publish_transaction_dry_run_allowed") is not True:
        raise ValueError("legacy migration final publication approval may only allow dry-run transaction")
    for key in (
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final publication approval must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final publication approval must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final publication approval must be dry-run-only")
    evidence_refs = _legacy_final_approval_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R079:legacy-migration-final-memory-publication-blocker",
        "R080:legacy-migration-final-publication-approval-guard",
        f"{payload['source_final_publication_block_id']}:final-publication-block",
        f"{payload['source_final_publication_block_id']}:final-approval-recorded",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final publication approval requires provenance evidence refs")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final publication approval evidence refs must be portable")


def _legacy_final_approval_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_approval_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final publication approval {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final publication approval {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_approval_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_approval_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_publication_transaction_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_final_transaction_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final publication transaction dry-run schema_version must be 1.0.0")
    if _required_legacy_final_transaction_str(payload, "kind") != "legacy_migration_final_publication_transaction_dry_run":
        raise ValueError("legacy migration final publication transaction dry-run kind is invalid")
    if _required_legacy_final_transaction_str(payload, "status") != "planned_dry_run":
        raise ValueError("legacy migration final publication transaction dry-run status is invalid")
    if _required_legacy_final_transaction_str(payload, "transaction_mode") != "dry_run":
        raise ValueError("legacy migration final publication transaction must be dry-run")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration final publication transaction must not allow Memory publication")
    for key in (
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "planned_by",
        "planned_at",
        "required_next_gate",
    ):
        _required_legacy_final_transaction_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_final_transaction_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_final_transaction_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final publication transaction output uri must be portable")
    planned_at = _required_legacy_final_transaction_str(payload, "planned_at")
    if "T" not in planned_at or not planned_at.endswith("Z"):
        raise ValueError("legacy migration final publication transaction planned_at must be UTC")
    if _required_legacy_final_transaction_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final publication transaction requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_publish_commit_blocker":
        raise ValueError("legacy migration final publication transaction next gate is invalid")
    plan = _legacy_final_transaction_mapping(payload, "transaction_plan")
    if plan.get("mode") != "dry_run":
        raise ValueError("legacy migration final publication transaction plan must be dry-run")
    if plan.get("requires_final_commit_guard") is not True:
        raise ValueError("legacy migration final publication transaction must require final commit guard")
    if plan.get("commit_guard") != "legacy_migration_final_publish_commit_blocker":
        raise ValueError("legacy migration final publication transaction commit guard is invalid")
    if plan.get("rollback_preview_required") is not True:
        raise ValueError("legacy migration final publication transaction requires rollback preview")
    operations = _legacy_final_transaction_sequence(plan.get("operations"), "operations")
    if _required_legacy_final_transaction_int(plan, "planned_operation_count") != len(operations):
        raise ValueError("legacy migration final publication transaction operation count mismatch")
    if len(operations) != 2:
        raise ValueError("legacy migration final publication transaction requires L1 and L2 dry-run operations")
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise ValueError("legacy migration final publication transaction operation must be an object")
        if operation.get("action") != "would_create":
            raise ValueError("legacy migration final publication transaction operation must be would_create")
        if operation.get("execute") is not False:
            raise ValueError("legacy migration final publication transaction operation must not execute")
        target_ref = _required_legacy_final_transaction_str(operation, "target_ref")
        source_ref = _required_legacy_final_transaction_str(operation, "source_ref")
        if not target_ref.startswith("crp://") or _looks_like_os_path(target_ref):
            raise ValueError("legacy migration final publication transaction target refs must be portable")
        if source_ref != output_uri:
            raise ValueError("legacy migration final publication transaction operation source mismatch")
        if _looks_like_os_path(_required_legacy_final_transaction_str(operation, "target_collection")):
            raise ValueError("legacy migration final publication transaction target collection must be portable")
    planned_output_refs = _legacy_final_transaction_ref_list(plan.get("planned_output_refs"), "planned output refs")
    if len(planned_output_refs) != len(operations):
        raise ValueError("legacy migration final publication transaction output ref count mismatch")
    operation_targets = {str(operation.get("target_ref")) for operation in operations if isinstance(operation, Mapping)}
    if set(planned_output_refs) != operation_targets:
        raise ValueError("legacy migration final publication transaction output refs must match operations")
    preview = _legacy_final_transaction_mapping(payload, "publication_preview")
    if preview.get("source_verified_output_uri") != output_uri:
        raise ValueError("legacy migration final publication transaction preview source mismatch")
    if preview.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final publication transaction preview namespace mismatch")
    if preview.get("formal_memory_write_count") != 0:
        raise ValueError("legacy migration final publication transaction must not write formal Memory")
    if preview.get("requires_final_commit_guard") is not True:
        raise ValueError("legacy migration final publication transaction preview must require final commit guard")
    safety = _legacy_final_transaction_mapping(payload, "safety")
    if safety.get("transaction_dry_run_created") is not True:
        raise ValueError("legacy migration final publication transaction safety must mark dry-run created")
    if safety.get("final_approval_recorded") is not True:
        raise ValueError("legacy migration final publication transaction safety must record final approval")
    if safety.get("publish_transaction_dry_run_allowed") is not True:
        raise ValueError("legacy migration final publication transaction must allow dry-run planning only")
    if safety.get("publish_transaction_executed") is not False:
        raise ValueError("legacy migration final publication transaction must not execute transaction")
    if safety.get("final_commit_guard_required") is not True:
        raise ValueError("legacy migration final publication transaction must require final commit guard")
    for key in (
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final publication transaction must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final publication transaction must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final publication transaction must be dry-run-only")
    evidence_refs = _legacy_final_transaction_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R080:legacy-migration-final-publication-approval-guard",
        "R081:legacy-migration-final-publication-transaction-dry-run-guard",
        f"{payload['source_final_approval_id']}:final-publication-approval",
        f"{payload['source_final_approval_id']}:transaction-dry-run-planned",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final publication transaction requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_output_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final publication transaction refs must be portable")


def _legacy_final_transaction_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_transaction_sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final publication transaction {label} are required")
    return tuple(value)


def _legacy_final_transaction_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final publication transaction {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final publication transaction {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_transaction_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_transaction_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_publish_commit_block_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_final_commit_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final publish commit block schema_version must be 1.0.0")
    if _required_legacy_final_commit_block_str(payload, "kind") != "legacy_migration_final_publish_commit_block":
        raise ValueError("legacy migration final publish commit block kind is invalid")
    if _required_legacy_final_commit_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final publish commit block status is invalid")
    if payload.get("commit_allowed") is not False:
        raise ValueError("legacy migration final publish commit block must not allow commit")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration final publish commit block must not allow Memory publication")
    for key in (
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "blocked_by",
        "blocked_at",
        "required_next_gate",
    ):
        _required_legacy_final_commit_block_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_final_commit_block_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_final_commit_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final publish commit block output uri must be portable")
    blocked_at = _required_legacy_final_commit_block_str(payload, "blocked_at")
    if "T" not in blocked_at or not blocked_at.endswith("Z"):
        raise ValueError("legacy migration final publish commit block blocked_at must be UTC")
    if _required_legacy_final_commit_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final publish commit block requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_publish_audit_evidence_guard":
        raise ValueError("legacy migration final publish commit block next gate is invalid")
    blockers = _legacy_final_commit_block_ref_list(payload.get("blocking_check_names"), "blocking checks")
    for required in (
        "final_publish_audit_evidence_missing",
        "formal_memory_writer_not_implemented",
        "l1_l2_memory_write_not_authorized",
        "transaction_commit_not_authorized",
    ):
        if required not in blockers:
            raise ValueError("legacy migration final publish commit block requires blocker checks")
    preview = _legacy_final_commit_block_mapping(payload, "commit_preview")
    if preview.get("source_transaction_dry_run_id") != payload.get("source_transaction_dry_run_id"):
        raise ValueError("legacy migration final publish commit block preview source mismatch")
    planned_refs = _legacy_final_commit_block_ref_list(preview.get("planned_operation_refs"), "planned operation refs")
    if _required_legacy_final_commit_block_int(preview, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final publish commit block planned operation count mismatch")
    if preview.get("commit_operation_count") != 0:
        raise ValueError("legacy migration final publish commit block must not include commit operations")
    if preview.get("requires_audit_evidence") is not True:
        raise ValueError("legacy migration final publish commit block must require audit evidence")
    if preview.get("requires_formal_writer_guard") is not True:
        raise ValueError("legacy migration final publish commit block must require formal writer guard")
    safety = _legacy_final_commit_block_mapping(payload, "safety")
    if safety.get("commit_block_created") is not True:
        raise ValueError("legacy migration final publish commit block safety must mark block created")
    if safety.get("transaction_dry_run_reviewed") is not True:
        raise ValueError("legacy migration final publish commit block requires reviewed transaction")
    if safety.get("commit_allowed") is not False:
        raise ValueError("legacy migration final publish commit block must not allow commit")
    if safety.get("publish_transaction_executed") is not False:
        raise ValueError("legacy migration final publish commit block must not execute transaction")
    for key in (
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final publish commit block must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final publish commit block must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final publish commit block must be dry-run-only")
    evidence_refs = _legacy_final_commit_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R081:legacy-migration-final-publication-transaction-dry-run-guard",
        "R082:legacy-migration-final-publish-commit-blocker",
        f"{payload['source_transaction_dry_run_id']}:transaction-dry-run",
        f"{payload['source_transaction_dry_run_id']}:final-publish-commit-blocked",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final publish commit block requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final publish commit block refs must be portable")


def _legacy_final_commit_block_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_commit_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final publish commit block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final publish commit block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_commit_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_commit_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_publish_audit_evidence_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_final_audit_evidence_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final publish audit evidence schema_version must be 1.0.0")
    if _required_legacy_final_audit_evidence_str(payload, "kind") != "legacy_migration_final_publish_audit_evidence":
        raise ValueError("legacy migration final publish audit evidence kind is invalid")
    if _required_legacy_final_audit_evidence_str(payload, "status") != "audit_ready":
        raise ValueError("legacy migration final publish audit evidence status is invalid")
    if payload.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final publish audit evidence must not allow implementation")
    if payload.get("commit_allowed") is not False:
        raise ValueError("legacy migration final publish audit evidence must not allow commit")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration final publish audit evidence must not allow Memory publication")
    for key in (
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "audited_by",
        "audited_at",
        "audit_note",
        "required_next_gate",
    ):
        _required_legacy_final_audit_evidence_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_final_audit_evidence_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_final_audit_evidence_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final publish audit evidence output uri must be portable")
    audited_at = _required_legacy_final_audit_evidence_str(payload, "audited_at")
    if "T" not in audited_at or not audited_at.endswith("Z"):
        raise ValueError("legacy migration final publish audit evidence audited_at must be UTC")
    if _required_legacy_final_audit_evidence_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final publish audit evidence requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_publish_implementation_blocker":
        raise ValueError("legacy migration final publish audit evidence next gate is invalid")
    audit_result = _legacy_final_audit_evidence_mapping(payload, "audit_result")
    if audit_result.get("decision") != "ready_for_implementation_blocker":
        raise ValueError("legacy migration final publish audit evidence decision is invalid")
    planned_refs = _legacy_final_audit_evidence_ref_list(audit_result.get("planned_operation_refs"), "planned operation refs")
    if _required_legacy_final_audit_evidence_int(audit_result, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final publish audit evidence planned operation count mismatch")
    if audit_result.get("commit_operation_count") != 0:
        raise ValueError("legacy migration final publish audit evidence must not include commit operations")
    if audit_result.get("memory_write_count") != 0:
        raise ValueError("legacy migration final publish audit evidence must not write Memory")
    for key in (
        "commit_block_verified",
        "provenance_refs_verified",
        "portable_refs_verified",
        "no_side_effects_verified",
        "requires_formal_writer_contract",
    ):
        if audit_result.get(key) is not True:
            raise ValueError(f"legacy migration final publish audit evidence audit result requires {key}")
    safety = _legacy_final_audit_evidence_mapping(payload, "safety")
    if safety.get("audit_evidence_created") is not True:
        raise ValueError("legacy migration final publish audit evidence safety must mark evidence created")
    if safety.get("commit_block_verified") is not True:
        raise ValueError("legacy migration final publish audit evidence safety must verify commit block")
    if safety.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final publish audit evidence must not allow implementation")
    if safety.get("commit_allowed") is not False:
        raise ValueError("legacy migration final publish audit evidence must not allow commit")
    if safety.get("publish_transaction_executed") is not False:
        raise ValueError("legacy migration final publish audit evidence must not execute transaction")
    for key in (
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final publish audit evidence must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final publish audit evidence must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final publish audit evidence must be dry-run-only")
    evidence_refs = _legacy_final_audit_evidence_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R082:legacy-migration-final-publish-commit-blocker",
        "R083:legacy-migration-final-publish-audit-evidence-guard",
        f"{payload['source_transaction_dry_run_id']}:transaction-dry-run",
        f"{payload['source_transaction_dry_run_id']}:final-publish-commit-blocked",
        f"{payload['source_commit_block_id']}:final-publish-commit-block",
        f"{payload['source_commit_block_id']}:audit-evidence-created",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final publish audit evidence requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final publish audit evidence refs must be portable")


def _legacy_final_audit_evidence_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_audit_evidence_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final publish audit evidence {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final publish audit evidence {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_audit_evidence_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_audit_evidence_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_publish_implementation_block_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_final_implementation_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final publish implementation block schema_version must be 1.0.0")
    if _required_legacy_final_implementation_block_str(payload, "kind") != "legacy_migration_final_publish_implementation_block":
        raise ValueError("legacy migration final publish implementation block kind is invalid")
    if _required_legacy_final_implementation_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final publish implementation block status is invalid")
    if payload.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final publish implementation block must not allow implementation")
    if payload.get("writer_contract_allowed") is not False:
        raise ValueError("legacy migration final publish implementation block must not allow writer contract")
    if payload.get("commit_allowed") is not False:
        raise ValueError("legacy migration final publish implementation block must not allow commit")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration final publish implementation block must not allow Memory publication")
    for key in (
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "blocked_by",
        "blocked_at",
        "block_note",
        "required_next_gate",
    ):
        _required_legacy_final_implementation_block_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_final_implementation_block_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_final_implementation_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final publish implementation block output uri must be portable")
    blocked_at = _required_legacy_final_implementation_block_str(payload, "blocked_at")
    if "T" not in blocked_at or not blocked_at.endswith("Z"):
        raise ValueError("legacy migration final publish implementation block blocked_at must be UTC")
    if _required_legacy_final_implementation_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final publish implementation block requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_publish_writer_contract_gate":
        raise ValueError("legacy migration final publish implementation block next gate is invalid")
    blockers = _legacy_final_implementation_block_ref_list(payload.get("blocking_check_names"), "blocking checks")
    for required in (
        "formal_writer_contract_missing",
        "l1_l2_memory_writer_not_implemented",
        "implementation_not_authorized",
        "transaction_commit_not_authorized",
    ):
        if required not in blockers:
            raise ValueError("legacy migration final publish implementation block requires blocker checks")
    preview = _legacy_final_implementation_block_mapping(payload, "implementation_preview")
    if preview.get("source_audit_evidence_id") != payload.get("source_audit_evidence_id"):
        raise ValueError("legacy migration final publish implementation block preview source mismatch")
    if preview.get("audit_evidence_verified") is not True:
        raise ValueError("legacy migration final publish implementation block preview must verify audit evidence")
    planned_refs = _legacy_final_implementation_block_ref_list(preview.get("planned_operation_refs"), "planned operation refs")
    if _required_legacy_final_implementation_block_int(preview, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final publish implementation block planned operation count mismatch")
    if preview.get("implementation_operation_count") != 0:
        raise ValueError("legacy migration final publish implementation block must not include implementation operations")
    if preview.get("commit_operation_count") != 0:
        raise ValueError("legacy migration final publish implementation block must not include commit operations")
    if preview.get("memory_write_count") != 0:
        raise ValueError("legacy migration final publish implementation block must not write Memory")
    if preview.get("formal_writer_contract_present") is not False:
        raise ValueError("legacy migration final publish implementation block must not include writer contract")
    if preview.get("requires_writer_contract_gate") is not True:
        raise ValueError("legacy migration final publish implementation block must require writer contract gate")
    safety = _legacy_final_implementation_block_mapping(payload, "safety")
    if safety.get("implementation_block_created") is not True:
        raise ValueError("legacy migration final publish implementation block safety must mark block created")
    if safety.get("audit_evidence_reviewed") is not True:
        raise ValueError("legacy migration final publish implementation block safety must review audit evidence")
    if safety.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final publish implementation block must not allow implementation")
    if safety.get("writer_contract_allowed") is not False:
        raise ValueError("legacy migration final publish implementation block must not allow writer contract")
    if safety.get("commit_allowed") is not False:
        raise ValueError("legacy migration final publish implementation block must not allow commit")
    if safety.get("publish_transaction_executed") is not False:
        raise ValueError("legacy migration final publish implementation block must not execute transaction")
    for key in (
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final publish implementation block must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final publish implementation block must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final publish implementation block must be dry-run-only")
    evidence_refs = _legacy_final_implementation_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R083:legacy-migration-final-publish-audit-evidence-guard",
        "R084:legacy-migration-final-publish-implementation-blocker",
        f"{payload['source_commit_block_id']}:final-publish-commit-block",
        f"{payload['source_commit_block_id']}:audit-evidence-created",
        f"{payload['source_audit_evidence_id']}:final-publish-audit-evidence",
        f"{payload['source_audit_evidence_id']}:implementation-blocked",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final publish implementation block requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final publish implementation block refs must be portable")


def _legacy_final_implementation_block_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_implementation_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final publish implementation block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final publish implementation block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_implementation_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_implementation_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_publish_writer_contract_gate_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_final_writer_contract_gate_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final publish writer contract gate schema_version must be 1.0.0")
    if _required_legacy_final_writer_contract_gate_str(payload, "kind") != "legacy_migration_final_publish_writer_contract_gate":
        raise ValueError("legacy migration final publish writer contract gate kind is invalid")
    if _required_legacy_final_writer_contract_gate_str(payload, "status") != "contract_gate_ready":
        raise ValueError("legacy migration final publish writer contract gate status is invalid")
    if payload.get("writer_contract_defined") is not True:
        raise ValueError("legacy migration final publish writer contract gate must define contract")
    if payload.get("writer_contract_allowed") is not False:
        raise ValueError("legacy migration final publish writer contract gate must not allow writer contract execution")
    if payload.get("writer_implementation_allowed") is not False:
        raise ValueError("legacy migration final publish writer contract gate must not allow writer implementation")
    if payload.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final publish writer contract gate must not allow implementation")
    if payload.get("commit_allowed") is not False:
        raise ValueError("legacy migration final publish writer contract gate must not allow commit")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration final publish writer contract gate must not allow Memory publication")
    for key in (
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "defined_by",
        "defined_at",
        "contract_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_contract_gate_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_final_writer_contract_gate_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_final_writer_contract_gate_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final publish writer contract gate output uri must be portable")
    defined_at = _required_legacy_final_writer_contract_gate_str(payload, "defined_at")
    if "T" not in defined_at or not defined_at.endswith("Z"):
        raise ValueError("legacy migration final publish writer contract gate defined_at must be UTC")
    if _required_legacy_final_writer_contract_gate_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final publish writer contract gate requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_writer_contract_dry_run_validation":
        raise ValueError("legacy migration final publish writer contract gate next gate is invalid")
    contract = _legacy_final_writer_contract_gate_mapping(payload, "writer_contract")
    if contract.get("mode") != "contract_gate_only":
        raise ValueError("legacy migration final publish writer contract gate mode is invalid")
    if contract.get("source_implementation_block_id") != payload.get("source_implementation_block_id"):
        raise ValueError("legacy migration final publish writer contract gate source mismatch")
    if contract.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final publish writer contract gate namespace mismatch")
    planned_refs = _legacy_final_writer_contract_gate_ref_list(contract.get("planned_operation_refs"), "planned operation refs")
    if _required_legacy_final_writer_contract_gate_int(contract, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final publish writer contract gate planned operation count mismatch")
    if not _legacy_final_writer_contract_gate_ref_list(contract.get("required_inputs"), "required inputs"):
        raise ValueError("legacy migration final publish writer contract gate requires inputs")
    if not _legacy_final_writer_contract_gate_ref_list(contract.get("required_outputs"), "required outputs"):
        raise ValueError("legacy migration final publish writer contract gate requires outputs")
    guards = _legacy_final_writer_contract_gate_ref_list(contract.get("required_guards"), "required guards")
    for required in (
        "portable_refs_only",
        "source_refs_preserved",
        "idempotency_key_required",
        "rollback_manifest_required",
        "no_legacy_write",
        "no_memory_write_before_validation",
    ):
        if required not in guards:
            raise ValueError("legacy migration final publish writer contract gate requires contract guards")
    if contract.get("implementation_operation_count") != 0:
        raise ValueError("legacy migration final publish writer contract gate must not include implementation operations")
    if contract.get("commit_operation_count") != 0:
        raise ValueError("legacy migration final publish writer contract gate must not include commit operations")
    if contract.get("memory_write_count") != 0:
        raise ValueError("legacy migration final publish writer contract gate must not write Memory")
    if contract.get("execution_allowed") is not False:
        raise ValueError("legacy migration final publish writer contract gate must not allow execution")
    if contract.get("requires_dry_run_validation") is not True:
        raise ValueError("legacy migration final publish writer contract gate must require dry-run validation")
    safety = _legacy_final_writer_contract_gate_mapping(payload, "safety")
    if safety.get("writer_contract_gate_created") is not True:
        raise ValueError("legacy migration final publish writer contract gate safety must mark gate created")
    if safety.get("implementation_block_verified") is not True:
        raise ValueError("legacy migration final publish writer contract gate safety must verify implementation block")
    if safety.get("writer_contract_defined") is not True:
        raise ValueError("legacy migration final publish writer contract gate safety must define contract")
    for key in (
        "writer_contract_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final publish writer contract gate must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final publish writer contract gate must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final publish writer contract gate must be dry-run-only")
    evidence_refs = _legacy_final_writer_contract_gate_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R084:legacy-migration-final-publish-implementation-blocker",
        "R085:legacy-migration-final-publish-writer-contract-gate",
        f"{payload['source_audit_evidence_id']}:final-publish-audit-evidence",
        f"{payload['source_audit_evidence_id']}:implementation-blocked",
        f"{payload['source_implementation_block_id']}:final-publish-implementation-block",
        f"{payload['source_implementation_block_id']}:writer-contract-gate-created",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final publish writer contract gate requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final publish writer contract gate refs must be portable")


def _legacy_final_writer_contract_gate_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_contract_gate_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final publish writer contract gate {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final publish writer contract gate {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_contract_gate_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_contract_gate_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_contract_validation_payload(payload: Mapping[str, object]) -> None:
    if _required_legacy_final_writer_contract_validation_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final writer contract dry-run validation schema_version must be 1.0.0")
    if _required_legacy_final_writer_contract_validation_str(payload, "kind") != (
        "legacy_migration_final_writer_contract_dry_run_validation"
    ):
        raise ValueError("legacy migration final writer contract dry-run validation kind is invalid")
    if _required_legacy_final_writer_contract_validation_str(payload, "status") != "validated":
        raise ValueError("legacy migration final writer contract dry-run validation status is invalid")
    if payload.get("validation_passed") is not True:
        raise ValueError("legacy migration final writer contract dry-run validation must pass")
    if payload.get("writer_contract_validated") is not True:
        raise ValueError("legacy migration final writer contract dry-run validation must validate contract")
    if payload.get("writer_execution_allowed") is not False:
        raise ValueError("legacy migration final writer contract dry-run validation must not allow writer execution")
    if payload.get("writer_implementation_allowed") is not False:
        raise ValueError(
            "legacy migration final writer contract dry-run validation must not allow writer implementation"
        )
    if payload.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer contract dry-run validation must not allow implementation")
    if payload.get("commit_allowed") is not False:
        raise ValueError("legacy migration final writer contract dry-run validation must not allow commit")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError("legacy migration final writer contract dry-run validation must not allow Memory publication")
    for key in (
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "validated_by",
        "validated_at",
        "validation_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_contract_validation_str(payload, key)
    _validate_segment(
        "target_namespace_id",
        _required_legacy_final_writer_contract_validation_str(payload, "target_namespace_id"),
    )
    output_uri = _required_legacy_final_writer_contract_validation_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final writer contract dry-run validation output uri must be portable")
    validated_at = _required_legacy_final_writer_contract_validation_str(payload, "validated_at")
    if "T" not in validated_at or not validated_at.endswith("Z"):
        raise ValueError("legacy migration final writer contract dry-run validation validated_at must be UTC")
    if _required_legacy_final_writer_contract_validation_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final writer contract dry-run validation requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_writer_implementation_readiness_blocker":
        raise ValueError("legacy migration final writer contract dry-run validation next gate is invalid")
    contract = _legacy_final_writer_contract_validation_mapping(payload, "validated_contract")
    if contract.get("mode") != "dry_run_validation_only":
        raise ValueError("legacy migration final writer contract dry-run validation mode is invalid")
    if contract.get("source_writer_contract_gate_id") != payload.get("source_writer_contract_gate_id"):
        raise ValueError("legacy migration final writer contract dry-run validation source mismatch")
    if contract.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer contract dry-run validation namespace mismatch")
    planned_refs = _legacy_final_writer_contract_validation_ref_list(
        contract.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_contract_validation_int(contract, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final writer contract dry-run validation planned operation count mismatch")
    _require_legacy_final_writer_contract_validation_members(
        _legacy_final_writer_contract_validation_ref_list(contract.get("required_inputs"), "required inputs"),
        (
            "verified_output_uri",
            "source_transaction_dry_run_id",
            "source_final_approval_id",
            "source_rollback_manifest_id",
            "target_namespace_id",
        ),
        "inputs",
    )
    _require_legacy_final_writer_contract_validation_members(
        _legacy_final_writer_contract_validation_ref_list(contract.get("required_outputs"), "required outputs"),
        (
            "draft_l1_atom_payload",
            "draft_l2_scenario_payload",
            "provenance_evidence_refs",
            "rollback_precondition_refs",
        ),
        "outputs",
    )
    guards = _legacy_final_writer_contract_validation_ref_list(contract.get("required_guards"), "required guards")
    _require_legacy_final_writer_contract_validation_members(
        guards,
        (
            "portable_refs_only",
            "source_refs_preserved",
            "idempotency_key_required",
            "rollback_manifest_required",
            "no_legacy_write",
            "no_memory_write_before_validation",
        ),
        "guards",
    )
    if contract.get("implementation_operation_count") != 0:
        raise ValueError("legacy migration final writer contract dry-run validation must not include implementation")
    if contract.get("commit_operation_count") != 0:
        raise ValueError("legacy migration final writer contract dry-run validation must not include commit operations")
    if contract.get("memory_write_count") != 0:
        raise ValueError("legacy migration final writer contract dry-run validation must not write Memory")
    if contract.get("execution_allowed") is not False:
        raise ValueError("legacy migration final writer contract dry-run validation must not allow execution")
    if contract.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer contract dry-run validation must not allow implementation")
    if contract.get("requires_readiness_blocker") is not True:
        raise ValueError("legacy migration final writer contract dry-run validation must require readiness blocker")
    checks = _legacy_final_writer_contract_validation_mapping(payload, "validation_checks")
    for key in (
        "required_inputs_present",
        "required_outputs_present",
        "required_guards_present",
        "planned_operations_verified",
        "execution_disabled",
        "implementation_disabled",
        "memory_writes_disabled",
        "portable_refs_verified",
        "source_refs_preserved",
        "dry_run_only",
    ):
        if checks.get(key) is not True:
            raise ValueError("legacy migration final writer contract dry-run validation checks must all pass")
    safety = _legacy_final_writer_contract_validation_mapping(payload, "safety")
    if safety.get("writer_contract_validation_created") is not True:
        raise ValueError("legacy migration final writer contract dry-run validation safety must mark created")
    if safety.get("writer_contract_gate_verified") is not True:
        raise ValueError("legacy migration final writer contract dry-run validation safety must verify gate")
    if safety.get("writer_contract_shape_validated") is not True:
        raise ValueError("legacy migration final writer contract dry-run validation safety must validate shape")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer contract dry-run validation must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer contract dry-run validation must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer contract dry-run validation must be dry-run-only")
    evidence_refs = _legacy_final_writer_contract_validation_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R085:legacy-migration-final-publish-writer-contract-gate",
        "R086:legacy-migration-final-writer-contract-dry-run-validation",
        f"{payload['source_implementation_block_id']}:final-publish-implementation-block",
        f"{payload['source_implementation_block_id']}:writer-contract-gate-created",
        f"{payload['source_writer_contract_gate_id']}:writer-contract-gate",
        f"{payload['source_writer_contract_gate_id']}:dry-run-validated",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final writer contract dry-run validation requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_refs, *guards]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer contract dry-run validation refs must be portable")


def _require_legacy_final_writer_contract_validation_members(
    actual: Sequence[str], expected: Sequence[str], label: str
) -> None:
    for required in expected:
        if required not in actual:
            raise ValueError(f"legacy migration final writer contract dry-run validation requires contract {label}")


def _legacy_final_writer_contract_validation_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_contract_validation_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final writer contract dry-run validation {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final writer contract dry-run validation {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_contract_validation_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_contract_validation_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_implementation_readiness_block_payload(
    payload: Mapping[str, object],
) -> None:
    if _required_legacy_final_writer_readiness_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final writer implementation readiness block schema_version must be 1.0.0")
    if _required_legacy_final_writer_readiness_block_str(payload, "kind") != (
        "legacy_migration_final_writer_implementation_readiness_block"
    ):
        raise ValueError("legacy migration final writer implementation readiness block kind is invalid")
    if _required_legacy_final_writer_readiness_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final writer implementation readiness block status is invalid")
    if payload.get("readiness_block_created") is not True:
        raise ValueError("legacy migration final writer implementation readiness block must mark created")
    if payload.get("writer_contract_validated") is not True:
        raise ValueError("legacy migration final writer implementation readiness block must validate writer contract")
    if payload.get("writer_implementation_ready") is not False:
        raise ValueError("legacy migration final writer implementation readiness block must not be ready")
    if payload.get("writer_execution_allowed") is not False:
        raise ValueError("legacy migration final writer implementation readiness block must not allow writer execution")
    if payload.get("writer_implementation_allowed") is not False:
        raise ValueError(
            "legacy migration final writer implementation readiness block must not allow writer implementation"
        )
    if payload.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer implementation readiness block must not allow implementation")
    if payload.get("commit_allowed") is not False:
        raise ValueError("legacy migration final writer implementation readiness block must not allow commit")
    if payload.get("memory_publication_allowed") is not False:
        raise ValueError(
            "legacy migration final writer implementation readiness block must not allow Memory publication"
        )
    for key in (
        "source_validation_id",
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "blocked_by",
        "blocked_at",
        "block_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_readiness_block_str(payload, key)
    _validate_segment(
        "target_namespace_id",
        _required_legacy_final_writer_readiness_block_str(payload, "target_namespace_id"),
    )
    output_uri = _required_legacy_final_writer_readiness_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final writer implementation readiness block output uri must be portable")
    blocked_at = _required_legacy_final_writer_readiness_block_str(payload, "blocked_at")
    if "T" not in blocked_at or not blocked_at.endswith("Z"):
        raise ValueError("legacy migration final writer implementation readiness block blocked_at must be UTC")
    if _required_legacy_final_writer_readiness_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final writer implementation readiness block requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_writer_implementation_design_gate":
        raise ValueError("legacy migration final writer implementation readiness block next gate is invalid")
    blockers = _legacy_final_writer_readiness_block_ref_list(payload.get("blocking_check_names"), "blocking checks")
    for required in (
        "writer_implementation_not_authorized",
        "implementation_design_missing",
        "final_commit_not_authorized",
        "memory_publication_not_authorized",
    ):
        if required not in blockers:
            raise ValueError("legacy migration final writer implementation readiness block requires blocker checks")
    preview = _legacy_final_writer_readiness_block_mapping(payload, "readiness_preview")
    if preview.get("mode") != "readiness_block_only":
        raise ValueError("legacy migration final writer implementation readiness block mode is invalid")
    if preview.get("source_validation_id") != payload.get("source_validation_id"):
        raise ValueError("legacy migration final writer implementation readiness block source mismatch")
    if preview.get("source_writer_contract_gate_id") != payload.get("source_writer_contract_gate_id"):
        raise ValueError("legacy migration final writer implementation readiness block gate mismatch")
    if preview.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer implementation readiness block namespace mismatch")
    planned_refs = _legacy_final_writer_readiness_block_ref_list(
        preview.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_readiness_block_int(preview, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final writer implementation readiness block planned count mismatch")
    if preview.get("validation_checks_verified") is not True or preview.get("writer_contract_validated") is not True:
        raise ValueError("legacy migration final writer implementation readiness block must verify validation")
    if preview.get("writer_implementation_ready") is not False:
        raise ValueError("legacy migration final writer implementation readiness block must not mark ready")
    if preview.get("implementation_design_present") is not False:
        raise ValueError("legacy migration final writer implementation readiness block must not include design")
    if preview.get("implementation_operation_count") != 0:
        raise ValueError("legacy migration final writer implementation readiness block must not include implementation")
    if preview.get("commit_operation_count") != 0:
        raise ValueError("legacy migration final writer implementation readiness block must not include commit")
    if preview.get("memory_write_count") != 0:
        raise ValueError("legacy migration final writer implementation readiness block must not write Memory")
    if preview.get("execution_allowed") is not False:
        raise ValueError("legacy migration final writer implementation readiness block must not allow execution")
    if preview.get("requires_implementation_design_gate") is not True:
        raise ValueError("legacy migration final writer implementation readiness block must require design gate")
    safety = _legacy_final_writer_readiness_block_mapping(payload, "safety")
    if safety.get("readiness_block_created") is not True:
        raise ValueError("legacy migration final writer implementation readiness block safety must mark created")
    if safety.get("validation_evidence_verified") is not True:
        raise ValueError("legacy migration final writer implementation readiness block safety must verify validation")
    if safety.get("writer_contract_validated") is not True:
        raise ValueError("legacy migration final writer implementation readiness block safety must validate contract")
    if safety.get("writer_implementation_ready") is not False:
        raise ValueError("legacy migration final writer implementation readiness block safety must not be ready")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation readiness block must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer implementation readiness block must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer implementation readiness block must be dry-run-only")
    evidence_refs = _legacy_final_writer_readiness_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R086:legacy-migration-final-writer-contract-dry-run-validation",
        "R087:legacy-migration-final-writer-implementation-readiness-blocker",
        f"{payload['source_writer_contract_gate_id']}:writer-contract-gate",
        f"{payload['source_writer_contract_gate_id']}:dry-run-validated",
        f"{payload['source_validation_id']}:writer-contract-dry-run-validation",
        f"{payload['source_validation_id']}:implementation-readiness-blocked",
    ):
        if required_ref not in evidence_refs:
            raise ValueError(
                "legacy migration final writer implementation readiness block requires provenance evidence refs"
            )
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer implementation readiness block refs must be portable")


def _legacy_final_writer_readiness_block_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_readiness_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final writer implementation readiness block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final writer implementation readiness block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_readiness_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_readiness_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_implementation_design_gate_payload(
    payload: Mapping[str, object],
) -> None:
    if _required_legacy_final_writer_design_gate_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final writer implementation design gate schema_version must be 1.0.0")
    if _required_legacy_final_writer_design_gate_str(payload, "kind") != (
        "legacy_migration_final_writer_implementation_design_gate"
    ):
        raise ValueError("legacy migration final writer implementation design gate kind is invalid")
    if _required_legacy_final_writer_design_gate_str(payload, "status") != "design_gate_ready":
        raise ValueError("legacy migration final writer implementation design gate status is invalid")
    if payload.get("implementation_design_defined") is not True:
        raise ValueError("legacy migration final writer implementation design gate must define design")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "commit_allowed",
        "memory_publication_allowed",
    ):
        if payload.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation design gate must not allow {label}")
    for key in (
        "source_readiness_block_id",
        "source_validation_id",
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "designed_by",
        "designed_at",
        "design_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_design_gate_str(payload, key)
    _validate_segment("target_namespace_id", _required_legacy_final_writer_design_gate_str(payload, "target_namespace_id"))
    output_uri = _required_legacy_final_writer_design_gate_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final writer implementation design gate output uri must be portable")
    designed_at = _required_legacy_final_writer_design_gate_str(payload, "designed_at")
    if "T" not in designed_at or not designed_at.endswith("Z"):
        raise ValueError("legacy migration final writer implementation design gate designed_at must be UTC")
    if _required_legacy_final_writer_design_gate_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final writer implementation design gate requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_writer_implementation_design_review_blocker":
        raise ValueError("legacy migration final writer implementation design gate next gate is invalid")
    design = _legacy_final_writer_design_gate_mapping(payload, "implementation_design")
    if design.get("mode") != "design_gate_only":
        raise ValueError("legacy migration final writer implementation design gate mode is invalid")
    if design.get("source_readiness_block_id") != payload.get("source_readiness_block_id"):
        raise ValueError("legacy migration final writer implementation design gate source mismatch")
    if design.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer implementation design gate namespace mismatch")
    planned_refs = _legacy_final_writer_design_gate_ref_list(
        design.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_design_gate_int(design, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final writer implementation design gate planned count mismatch")
    components = _legacy_final_writer_design_gate_ref_list(design.get("required_components"), "required components")
    for required in (
        "l1_atom_payload_builder",
        "l2_scenario_payload_builder",
        "provenance_ref_validator",
        "rollback_precondition_validator",
        "idempotency_key_builder",
    ):
        if required not in components:
            raise ValueError("legacy migration final writer implementation design gate requires components")
    checks = _legacy_final_writer_design_gate_ref_list(design.get("required_review_checks"), "required review checks")
    for required in (
        "portable_refs_only",
        "source_refs_preserved",
        "no_legacy_write",
        "no_memory_write_before_review",
        "rollback_preconditions_preserved",
    ):
        if required not in checks:
            raise ValueError("legacy migration final writer implementation design gate requires review checks")
    if design.get("implementation_operation_count") != 0:
        raise ValueError("legacy migration final writer implementation design gate must not include implementation")
    if design.get("commit_operation_count") != 0:
        raise ValueError("legacy migration final writer implementation design gate must not include commit")
    if design.get("memory_write_count") != 0:
        raise ValueError("legacy migration final writer implementation design gate must not write Memory")
    if design.get("execution_allowed") is not False or design.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer implementation design gate must not allow execution")
    if design.get("requires_design_review_blocker") is not True:
        raise ValueError("legacy migration final writer implementation design gate must require review blocker")
    safety = _legacy_final_writer_design_gate_mapping(payload, "safety")
    if safety.get("implementation_design_gate_created") is not True:
        raise ValueError("legacy migration final writer implementation design gate safety must mark created")
    if safety.get("readiness_block_verified") is not True:
        raise ValueError("legacy migration final writer implementation design gate safety must verify readiness")
    if safety.get("implementation_design_defined") is not True:
        raise ValueError("legacy migration final writer implementation design gate safety must define design")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation design gate must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer implementation design gate must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer implementation design gate must be dry-run-only")
    evidence_refs = _legacy_final_writer_design_gate_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R087:legacy-migration-final-writer-implementation-readiness-blocker",
        "R088:legacy-migration-final-writer-implementation-design-gate",
        f"{payload['source_validation_id']}:writer-contract-dry-run-validation",
        f"{payload['source_validation_id']}:implementation-readiness-blocked",
        f"{payload['source_readiness_block_id']}:implementation-readiness-block",
        f"{payload['source_readiness_block_id']}:implementation-design-gate-created",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final writer implementation design gate requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer implementation design gate refs must be portable")


def _legacy_final_writer_design_gate_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_design_gate_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final writer implementation design gate {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final writer implementation design gate {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_design_gate_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_design_gate_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_implementation_design_review_block_payload(
    payload: Mapping[str, object],
) -> None:
    if _required_legacy_final_writer_design_review_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final writer implementation design review block schema_version must be 1.0.0")
    if _required_legacy_final_writer_design_review_block_str(payload, "kind") != (
        "legacy_migration_final_writer_implementation_design_review_block"
    ):
        raise ValueError("legacy migration final writer implementation design review block kind is invalid")
    if _required_legacy_final_writer_design_review_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final writer implementation design review block status is invalid")
    if payload.get("design_review_block_created") is not True:
        raise ValueError("legacy migration final writer implementation design review block must be created")
    if payload.get("implementation_design_reviewed") is not True:
        raise ValueError("legacy migration final writer implementation design review block must review design")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "dry_run_plan_allowed",
        "commit_allowed",
        "memory_publication_allowed",
    ):
        if payload.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation design review block must not allow {label}")
    for key in (
        "source_design_gate_id",
        "source_readiness_block_id",
        "source_validation_id",
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "reviewed_by",
        "reviewed_at",
        "review_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_design_review_block_str(payload, key)
    _validate_segment(
        "target_namespace_id",
        _required_legacy_final_writer_design_review_block_str(payload, "target_namespace_id"),
    )
    output_uri = _required_legacy_final_writer_design_review_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final writer implementation design review block output uri must be portable")
    reviewed_at = _required_legacy_final_writer_design_review_block_str(payload, "reviewed_at")
    if "T" not in reviewed_at or not reviewed_at.endswith("Z"):
        raise ValueError("legacy migration final writer implementation design review block reviewed_at must be UTC")
    if _required_legacy_final_writer_design_review_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final writer implementation design review block requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_writer_implementation_dry_run_plan_blocker":
        raise ValueError("legacy migration final writer implementation design review block next gate is invalid")
    review = _legacy_final_writer_design_review_block_mapping(payload, "review_summary")
    if review.get("mode") != "design_review_block_only":
        raise ValueError("legacy migration final writer implementation design review block mode is invalid")
    if review.get("source_design_gate_id") != payload.get("source_design_gate_id"):
        raise ValueError("legacy migration final writer implementation design review block design gate mismatch")
    if review.get("source_readiness_block_id") != payload.get("source_readiness_block_id"):
        raise ValueError("legacy migration final writer implementation design review block readiness mismatch")
    if review.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer implementation design review block namespace mismatch")
    planned_refs = _legacy_final_writer_design_review_block_ref_list(
        review.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_design_review_block_int(review, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final writer implementation design review block planned count mismatch")
    components = _legacy_final_writer_design_review_block_ref_list(review.get("reviewed_components"), "components")
    for required in (
        "l1_atom_payload_builder",
        "l2_scenario_payload_builder",
        "provenance_ref_validator",
        "rollback_precondition_validator",
        "idempotency_key_builder",
    ):
        if required not in components:
            raise ValueError("legacy migration final writer implementation design review block requires components")
    checks = _legacy_final_writer_design_review_block_ref_list(review.get("reviewed_checks"), "review checks")
    for required in (
        "portable_refs_only",
        "source_refs_preserved",
        "no_legacy_write",
        "no_memory_write_before_review",
        "rollback_preconditions_preserved",
    ):
        if required not in checks:
            raise ValueError("legacy migration final writer implementation design review block requires review checks")
    if review.get("review_block_reason") != "implementation_design_requires_non_executing_dry_run_plan_blocker":
        raise ValueError("legacy migration final writer implementation design review block reason is invalid")
    for key in (
        "implementation_operation_count",
        "dry_run_plan_operation_count",
        "commit_operation_count",
        "memory_write_count",
    ):
        if review.get(key) != 0:
            raise ValueError("legacy migration final writer implementation design review block must not include operations")
    if review.get("execution_allowed") is not False or review.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer implementation design review block must not allow execution")
    if review.get("requires_dry_run_plan_blocker") is not True:
        raise ValueError("legacy migration final writer implementation design review block must require dry-run plan blocker")
    safety = _legacy_final_writer_design_review_block_mapping(payload, "safety")
    for key in (
        "design_review_block_created",
        "design_gate_verified",
        "implementation_design_reviewed",
    ):
        if safety.get(key) is not True:
            raise ValueError("legacy migration final writer implementation design review block safety must verify review")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "dry_run_plan_allowed",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation design review block must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer implementation design review block must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer implementation design review block must be dry-run-only")
    evidence_refs = _legacy_final_writer_design_review_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R088:legacy-migration-final-writer-implementation-design-gate",
        "R089:legacy-migration-final-writer-implementation-design-review-blocker",
        f"{payload['source_readiness_block_id']}:implementation-readiness-block",
        f"{payload['source_readiness_block_id']}:implementation-design-gate-created",
        f"{payload['source_design_gate_id']}:implementation-design-gate",
        f"{payload['source_design_gate_id']}:implementation-design-reviewed",
    ):
        if required_ref not in evidence_refs:
            raise ValueError(
                "legacy migration final writer implementation design review block requires provenance evidence refs"
            )
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer implementation design review block refs must be portable")


def _legacy_final_writer_design_review_block_mapping(
    mapping: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_design_review_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final writer implementation design review block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final writer implementation design review block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_design_review_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_design_review_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_implementation_dry_run_plan_block_payload(
    payload: Mapping[str, object],
) -> None:
    if _required_legacy_final_writer_dry_run_plan_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final writer implementation dry-run plan block schema_version must be 1.0.0")
    if _required_legacy_final_writer_dry_run_plan_block_str(payload, "kind") != (
        "legacy_migration_final_writer_implementation_dry_run_plan_block"
    ):
        raise ValueError("legacy migration final writer implementation dry-run plan block kind is invalid")
    if _required_legacy_final_writer_dry_run_plan_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final writer implementation dry-run plan block status is invalid")
    if payload.get("dry_run_plan_block_created") is not True:
        raise ValueError("legacy migration final writer implementation dry-run plan block must be created")
    if payload.get("design_review_verified") is not True:
        raise ValueError("legacy migration final writer implementation dry-run plan block must verify design review")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "memory_publication_allowed",
    ):
        if payload.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation dry-run plan block must not allow {label}")
    for key in (
        "source_design_review_block_id",
        "source_design_gate_id",
        "source_readiness_block_id",
        "source_validation_id",
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "blocked_by",
        "blocked_at",
        "block_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_dry_run_plan_block_str(payload, key)
    _validate_segment(
        "target_namespace_id",
        _required_legacy_final_writer_dry_run_plan_block_str(payload, "target_namespace_id"),
    )
    output_uri = _required_legacy_final_writer_dry_run_plan_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final writer implementation dry-run plan block output uri must be portable")
    blocked_at = _required_legacy_final_writer_dry_run_plan_block_str(payload, "blocked_at")
    if "T" not in blocked_at or not blocked_at.endswith("Z"):
        raise ValueError("legacy migration final writer implementation dry-run plan block blocked_at must be UTC")
    if _required_legacy_final_writer_dry_run_plan_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final writer implementation dry-run plan block requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_writer_implementation_dry_run_plan_review_blocker":
        raise ValueError("legacy migration final writer implementation dry-run plan block next gate is invalid")
    planning = _legacy_final_writer_dry_run_plan_block_mapping(payload, "planning_block")
    if planning.get("mode") != "dry_run_plan_block_only":
        raise ValueError("legacy migration final writer implementation dry-run plan block mode is invalid")
    if planning.get("source_design_review_block_id") != payload.get("source_design_review_block_id"):
        raise ValueError("legacy migration final writer implementation dry-run plan block review source mismatch")
    if planning.get("source_design_gate_id") != payload.get("source_design_gate_id"):
        raise ValueError("legacy migration final writer implementation dry-run plan block design gate mismatch")
    if planning.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer implementation dry-run plan block namespace mismatch")
    planned_refs = _legacy_final_writer_dry_run_plan_block_ref_list(
        planning.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_dry_run_plan_block_int(planning, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final writer implementation dry-run plan block planned count mismatch")
    sections = _legacy_final_writer_dry_run_plan_block_ref_list(
        planning.get("required_plan_sections"), "required plan sections"
    )
    for required in (
        "operation_sequence",
        "provenance_checks",
        "idempotency_checks",
        "rollback_checks",
        "no_write_guard_checks",
    ):
        if required not in sections:
            raise ValueError("legacy migration final writer implementation dry-run plan block requires plan sections")
    blockers = _legacy_final_writer_dry_run_plan_block_ref_list(
        planning.get("blocking_check_names"), "blocking checks"
    )
    for required in (
        "dry_run_plan_not_authorized",
        "writer_implementation_not_authorized",
        "final_commit_not_authorized",
        "memory_publication_not_authorized",
    ):
        if required not in blockers:
            raise ValueError("legacy migration final writer implementation dry-run plan block requires blockers")
    for key in (
        "dry_run_plan_operation_count",
        "implementation_operation_count",
        "commit_operation_count",
        "memory_write_count",
    ):
        if planning.get(key) != 0:
            raise ValueError("legacy migration final writer implementation dry-run plan block must not include operations")
    if planning.get("execution_allowed") is not False or planning.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer implementation dry-run plan block must not allow execution")
    if planning.get("requires_dry_run_plan_review_blocker") is not True:
        raise ValueError(
            "legacy migration final writer implementation dry-run plan block must require dry-run plan review blocker"
        )
    safety = _legacy_final_writer_dry_run_plan_block_mapping(payload, "safety")
    for key in ("dry_run_plan_block_created", "design_review_verified"):
        if safety.get(key) is not True:
            raise ValueError("legacy migration final writer implementation dry-run plan block safety must verify block")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation dry-run plan block must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer implementation dry-run plan block must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer implementation dry-run plan block must be dry-run-only")
    evidence_refs = _legacy_final_writer_dry_run_plan_block_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R089:legacy-migration-final-writer-implementation-design-review-blocker",
        "R090:legacy-migration-final-writer-implementation-dry-run-plan-blocker",
        f"{payload['source_design_gate_id']}:implementation-design-gate",
        f"{payload['source_design_gate_id']}:implementation-design-reviewed",
        f"{payload['source_design_review_block_id']}:implementation-design-review-block",
        f"{payload['source_design_review_block_id']}:dry-run-plan-blocked",
    ):
        if required_ref not in evidence_refs:
            raise ValueError(
                "legacy migration final writer implementation dry-run plan block requires provenance evidence refs"
            )
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer implementation dry-run plan block refs must be portable")


def _legacy_final_writer_dry_run_plan_block_mapping(
    mapping: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_dry_run_plan_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final writer implementation dry-run plan block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final writer implementation dry-run plan block {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_dry_run_plan_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_dry_run_plan_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_implementation_dry_run_plan_review_block_payload(
    payload: Mapping[str, object],
) -> None:
    if _required_legacy_final_writer_dry_run_plan_review_block_str(payload, "schema_version") != "1.0.0":
        raise ValueError(
            "legacy migration final writer implementation dry-run plan review block schema_version must be 1.0.0"
        )
    if _required_legacy_final_writer_dry_run_plan_review_block_str(payload, "kind") != (
        "legacy_migration_final_writer_implementation_dry_run_plan_review_block"
    ):
        raise ValueError("legacy migration final writer implementation dry-run plan review block kind is invalid")
    if _required_legacy_final_writer_dry_run_plan_review_block_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final writer implementation dry-run plan review block status is invalid")
    for key in (
        "dry_run_plan_review_block_created",
        "dry_run_plan_block_verified",
        "dry_run_plan_reviewed",
    ):
        if payload.get(key) is not True:
            raise ValueError("legacy migration final writer implementation dry-run plan review block must be created")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "memory_publication_allowed",
    ):
        if payload.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(
                f"legacy migration final writer implementation dry-run plan review block must not allow {label}"
            )
    for key in (
        "source_dry_run_plan_block_id",
        "source_design_review_block_id",
        "source_design_gate_id",
        "source_readiness_block_id",
        "source_validation_id",
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "reviewed_by",
        "reviewed_at",
        "review_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_dry_run_plan_review_block_str(payload, key)
    _validate_segment(
        "target_namespace_id",
        _required_legacy_final_writer_dry_run_plan_review_block_str(payload, "target_namespace_id"),
    )
    output_uri = _required_legacy_final_writer_dry_run_plan_review_block_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError(
            "legacy migration final writer implementation dry-run plan review block output uri must be portable"
        )
    reviewed_at = _required_legacy_final_writer_dry_run_plan_review_block_str(payload, "reviewed_at")
    if "T" not in reviewed_at or not reviewed_at.endswith("Z"):
        raise ValueError("legacy migration final writer implementation dry-run plan review block reviewed_at must be UTC")
    if _required_legacy_final_writer_dry_run_plan_review_block_int(payload, "selected_candidate_count") <= 0:
        raise ValueError(
            "legacy migration final writer implementation dry-run plan review block requires selected candidates"
        )
    if payload.get("required_next_gate") != "legacy_migration_final_writer_implementation_preflight_guard":
        raise ValueError("legacy migration final writer implementation dry-run plan review block next gate is invalid")
    review = _legacy_final_writer_dry_run_plan_review_block_mapping(payload, "review_summary")
    if review.get("mode") != "dry_run_plan_review_block_only":
        raise ValueError("legacy migration final writer implementation dry-run plan review block mode is invalid")
    if review.get("source_dry_run_plan_block_id") != payload.get("source_dry_run_plan_block_id"):
        raise ValueError("legacy migration final writer implementation dry-run plan review block source mismatch")
    if review.get("source_design_review_block_id") != payload.get("source_design_review_block_id"):
        raise ValueError("legacy migration final writer implementation dry-run plan review block review source mismatch")
    if review.get("source_design_gate_id") != payload.get("source_design_gate_id"):
        raise ValueError("legacy migration final writer implementation dry-run plan review block design gate mismatch")
    if review.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer implementation dry-run plan review block namespace mismatch")
    planned_refs = _legacy_final_writer_dry_run_plan_review_block_ref_list(
        review.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_dry_run_plan_review_block_int(review, "planned_operation_count") != len(
        planned_refs
    ):
        raise ValueError("legacy migration final writer implementation dry-run plan review block planned count mismatch")
    sections = _legacy_final_writer_dry_run_plan_review_block_ref_list(
        review.get("reviewed_plan_sections"), "reviewed plan sections"
    )
    for required in (
        "operation_sequence",
        "provenance_checks",
        "idempotency_checks",
        "rollback_checks",
        "no_write_guard_checks",
    ):
        if required not in sections:
            raise ValueError(
                "legacy migration final writer implementation dry-run plan review block requires reviewed sections"
            )
    checks = _legacy_final_writer_dry_run_plan_review_block_ref_list(
        review.get("reviewed_blocking_checks"), "reviewed blocking checks"
    )
    for required in (
        "dry_run_plan_not_authorized",
        "writer_implementation_not_authorized",
        "final_commit_not_authorized",
        "memory_publication_not_authorized",
    ):
        if required not in checks:
            raise ValueError(
                "legacy migration final writer implementation dry-run plan review block requires reviewed checks"
            )
    if review.get("review_block_reason") != "dry_run_plan_requires_non_executing_preflight_guard":
        raise ValueError("legacy migration final writer implementation dry-run plan review block reason is invalid")
    for key in (
        "dry_run_plan_operation_count",
        "implementation_operation_count",
        "commit_operation_count",
        "memory_write_count",
    ):
        if review.get(key) != 0:
            raise ValueError("legacy migration final writer implementation dry-run plan review block must not include operations")
    if review.get("execution_allowed") is not False or review.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer implementation dry-run plan review block must not allow execution")
    if review.get("requires_preflight_guard") is not True:
        raise ValueError("legacy migration final writer implementation dry-run plan review block must require preflight guard")
    safety = _legacy_final_writer_dry_run_plan_review_block_mapping(payload, "safety")
    for key in (
        "dry_run_plan_review_block_created",
        "dry_run_plan_block_verified",
        "dry_run_plan_reviewed",
    ):
        if safety.get(key) is not True:
            raise ValueError("legacy migration final writer implementation dry-run plan review block safety must verify review")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(
                f"legacy migration final writer implementation dry-run plan review block must not {label}"
            )
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer implementation dry-run plan review block must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer implementation dry-run plan review block must be dry-run-only")
    evidence_refs = _legacy_final_writer_dry_run_plan_review_block_ref_list(
        payload.get("evidence_refs"), "evidence refs"
    )
    for required_ref in (
        "R090:legacy-migration-final-writer-implementation-dry-run-plan-blocker",
        "R091:legacy-migration-final-writer-implementation-dry-run-plan-review-blocker",
        f"{payload['source_design_review_block_id']}:implementation-design-review-block",
        f"{payload['source_design_review_block_id']}:dry-run-plan-blocked",
        f"{payload['source_dry_run_plan_block_id']}:dry-run-plan-block",
        f"{payload['source_dry_run_plan_block_id']}:dry-run-plan-reviewed",
    ):
        if required_ref not in evidence_refs:
            raise ValueError(
                "legacy migration final writer implementation dry-run plan review block requires provenance evidence refs"
            )
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer implementation dry-run plan review block refs must be portable")


def _legacy_final_writer_dry_run_plan_review_block_mapping(
    mapping: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_dry_run_plan_review_block_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final writer implementation dry-run plan review block {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(
                f"legacy migration final writer implementation dry-run plan review block {label} must be strings"
            )
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_dry_run_plan_review_block_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_dry_run_plan_review_block_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_implementation_preflight_guard_payload(
    payload: Mapping[str, object],
) -> None:
    if _required_legacy_final_writer_preflight_guard_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final writer implementation preflight guard schema_version must be 1.0.0")
    if _required_legacy_final_writer_preflight_guard_str(payload, "kind") != (
        "legacy_migration_final_writer_implementation_preflight_guard"
    ):
        raise ValueError("legacy migration final writer implementation preflight guard kind is invalid")
    if _required_legacy_final_writer_preflight_guard_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final writer implementation preflight guard status is invalid")
    if payload.get("preflight_guard_created") is not True:
        raise ValueError("legacy migration final writer implementation preflight guard must be created")
    if payload.get("dry_run_plan_review_verified") is not True:
        raise ValueError("legacy migration final writer implementation preflight guard must verify review")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "preflight_passed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "memory_publication_allowed",
    ):
        if payload.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation preflight guard must not allow {label}")
    for key in (
        "source_dry_run_plan_review_block_id",
        "source_dry_run_plan_block_id",
        "source_design_review_block_id",
        "source_design_gate_id",
        "source_readiness_block_id",
        "source_validation_id",
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "checked_by",
        "checked_at",
        "check_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_preflight_guard_str(payload, key)
    _validate_segment(
        "target_namespace_id",
        _required_legacy_final_writer_preflight_guard_str(payload, "target_namespace_id"),
    )
    output_uri = _required_legacy_final_writer_preflight_guard_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final writer implementation preflight guard output uri must be portable")
    checked_at = _required_legacy_final_writer_preflight_guard_str(payload, "checked_at")
    if "T" not in checked_at or not checked_at.endswith("Z"):
        raise ValueError("legacy migration final writer implementation preflight guard checked_at must be UTC")
    if _required_legacy_final_writer_preflight_guard_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final writer implementation preflight guard requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_writer_implementation_no_write_smoke":
        raise ValueError("legacy migration final writer implementation preflight guard next gate is invalid")
    preflight = _legacy_final_writer_preflight_guard_mapping(payload, "preflight_summary")
    if preflight.get("mode") != "preflight_guard_only":
        raise ValueError("legacy migration final writer implementation preflight guard mode is invalid")
    if preflight.get("source_dry_run_plan_review_block_id") != payload.get("source_dry_run_plan_review_block_id"):
        raise ValueError("legacy migration final writer implementation preflight guard review source mismatch")
    if preflight.get("source_dry_run_plan_block_id") != payload.get("source_dry_run_plan_block_id"):
        raise ValueError("legacy migration final writer implementation preflight guard plan source mismatch")
    if preflight.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer implementation preflight guard namespace mismatch")
    planned_refs = _legacy_final_writer_preflight_guard_ref_list(
        preflight.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_preflight_guard_int(preflight, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final writer implementation preflight guard planned count mismatch")
    checks = _legacy_final_writer_preflight_guard_ref_list(
        preflight.get("required_preflight_checks"), "required preflight checks"
    )
    for required in (
        "dry_run_plan_review_verified",
        "portable_provenance_refs_only",
        "writer_execution_still_blocked",
        "memory_publication_still_blocked",
        "legacy_write_still_blocked",
        "no_write_smoke_required",
    ):
        if required not in checks:
            raise ValueError("legacy migration final writer implementation preflight guard requires preflight checks")
    blockers = _legacy_final_writer_preflight_guard_ref_list(
        preflight.get("blocking_check_names"), "blocking checks"
    )
    for required in (
        "writer_implementation_not_authorized",
        "dry_run_plan_creation_not_authorized",
        "final_commit_not_authorized",
        "memory_publication_not_authorized",
        "legacy_write_not_authorized",
    ):
        if required not in blockers:
            raise ValueError("legacy migration final writer implementation preflight guard requires blockers")
    for key in (
        "dry_run_plan_operation_count",
        "implementation_operation_count",
        "commit_operation_count",
        "memory_write_count",
    ):
        if preflight.get(key) != 0:
            raise ValueError("legacy migration final writer implementation preflight guard must not include operations")
    if preflight.get("execution_allowed") is not False or preflight.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer implementation preflight guard must not allow execution")
    if preflight.get("requires_no_write_smoke") is not True:
        raise ValueError("legacy migration final writer implementation preflight guard must require no-write smoke")
    safety = _legacy_final_writer_preflight_guard_mapping(payload, "safety")
    for key in ("preflight_guard_created", "dry_run_plan_review_verified"):
        if safety.get(key) is not True:
            raise ValueError("legacy migration final writer implementation preflight guard safety must verify guard")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "preflight_passed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation preflight guard must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer implementation preflight guard must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer implementation preflight guard must be dry-run-only")
    evidence_refs = _legacy_final_writer_preflight_guard_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R091:legacy-migration-final-writer-implementation-dry-run-plan-review-blocker",
        "R092:legacy-migration-final-writer-implementation-preflight-guard",
        f"{payload['source_dry_run_plan_block_id']}:dry-run-plan-block",
        f"{payload['source_dry_run_plan_block_id']}:dry-run-plan-reviewed",
        f"{payload['source_dry_run_plan_review_block_id']}:dry-run-plan-review-block",
        f"{payload['source_dry_run_plan_review_block_id']}:implementation-preflight-blocked",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final writer implementation preflight guard requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer implementation preflight guard refs must be portable")


def _legacy_final_writer_preflight_guard_mapping(
    mapping: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_preflight_guard_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final writer implementation preflight guard {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final writer implementation preflight guard {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_preflight_guard_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_preflight_guard_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_implementation_no_write_smoke_payload(
    payload: Mapping[str, object],
) -> None:
    if _required_legacy_final_writer_no_write_smoke_str(payload, "schema_version") != "1.0.0":
        raise ValueError("legacy migration final writer implementation no-write smoke schema_version must be 1.0.0")
    if _required_legacy_final_writer_no_write_smoke_str(payload, "kind") != (
        "legacy_migration_final_writer_implementation_no_write_smoke"
    ):
        raise ValueError("legacy migration final writer implementation no-write smoke kind is invalid")
    if _required_legacy_final_writer_no_write_smoke_str(payload, "status") != "blocked":
        raise ValueError("legacy migration final writer implementation no-write smoke status is invalid")
    for key in ("no_write_smoke_created", "preflight_guard_verified", "no_write_smoke_passed"):
        if payload.get(key) is not True:
            raise ValueError("legacy migration final writer implementation no-write smoke must verify smoke")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "preflight_passed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "memory_publication_allowed",
    ):
        if payload.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation no-write smoke must not allow {label}")
    for key in (
        "source_preflight_guard_id",
        "source_dry_run_plan_review_block_id",
        "source_dry_run_plan_block_id",
        "source_design_review_block_id",
        "source_design_gate_id",
        "source_readiness_block_id",
        "source_validation_id",
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "checked_by",
        "checked_at",
        "check_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_no_write_smoke_str(payload, key)
    _validate_segment(
        "target_namespace_id",
        _required_legacy_final_writer_no_write_smoke_str(payload, "target_namespace_id"),
    )
    output_uri = _required_legacy_final_writer_no_write_smoke_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError("legacy migration final writer implementation no-write smoke output uri must be portable")
    checked_at = _required_legacy_final_writer_no_write_smoke_str(payload, "checked_at")
    if "T" not in checked_at or not checked_at.endswith("Z"):
        raise ValueError("legacy migration final writer implementation no-write smoke checked_at must be UTC")
    if _required_legacy_final_writer_no_write_smoke_int(payload, "selected_candidate_count") <= 0:
        raise ValueError("legacy migration final writer implementation no-write smoke requires selected candidates")
    if payload.get("required_next_gate") != "legacy_migration_final_writer_implementation_regression_consolidation":
        raise ValueError("legacy migration final writer implementation no-write smoke next gate is invalid")
    smoke = _legacy_final_writer_no_write_smoke_mapping(payload, "smoke_summary")
    if smoke.get("mode") != "no_write_smoke_only":
        raise ValueError("legacy migration final writer implementation no-write smoke mode is invalid")
    if smoke.get("source_preflight_guard_id") != payload.get("source_preflight_guard_id"):
        raise ValueError("legacy migration final writer implementation no-write smoke preflight source mismatch")
    if smoke.get("source_dry_run_plan_review_block_id") != payload.get("source_dry_run_plan_review_block_id"):
        raise ValueError("legacy migration final writer implementation no-write smoke review source mismatch")
    if smoke.get("source_dry_run_plan_block_id") != payload.get("source_dry_run_plan_block_id"):
        raise ValueError("legacy migration final writer implementation no-write smoke plan source mismatch")
    if smoke.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer implementation no-write smoke namespace mismatch")
    planned_refs = _legacy_final_writer_no_write_smoke_ref_list(
        smoke.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_no_write_smoke_int(smoke, "planned_operation_count") != len(planned_refs):
        raise ValueError("legacy migration final writer implementation no-write smoke planned count mismatch")
    checks = _legacy_final_writer_no_write_smoke_ref_list(smoke.get("no_write_checks"), "no-write checks")
    for required in (
        "memory_atoms_not_written",
        "staging_atoms_not_written",
        "legacy_root_unchanged",
        "published_outputs_empty",
        "writer_execution_blocked",
        "preflight_not_passed",
    ):
        if required not in checks:
            raise ValueError("legacy migration final writer implementation no-write smoke requires no-write checks")
    observed = _legacy_final_writer_no_write_smoke_mapping(smoke, "observed_write_counts")
    for key in (
        "memory_atoms",
        "staging_atoms",
        "legacy_writes",
        "published_outputs",
        "implementation_operations",
        "commit_operations",
    ):
        if observed.get(key) != 0:
            raise ValueError("legacy migration final writer implementation no-write smoke must not include writes")
    if smoke.get("smoke_passed") is not True:
        raise ValueError("legacy migration final writer implementation no-write smoke must pass smoke")
    if smoke.get("execution_allowed") is not False or smoke.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer implementation no-write smoke must not allow execution")
    if smoke.get("requires_regression_consolidation") is not True:
        raise ValueError("legacy migration final writer implementation no-write smoke must require regression consolidation")
    safety = _legacy_final_writer_no_write_smoke_mapping(payload, "safety")
    for key in ("no_write_smoke_created", "preflight_guard_verified", "no_write_smoke_passed"):
        if safety.get(key) is not True:
            raise ValueError("legacy migration final writer implementation no-write smoke safety must verify smoke")
    for key in (
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "preflight_passed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation no-write smoke must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer implementation no-write smoke must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer implementation no-write smoke must be dry-run-only")
    evidence_refs = _legacy_final_writer_no_write_smoke_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R092:legacy-migration-final-writer-implementation-preflight-guard",
        "R093:legacy-migration-final-writer-implementation-no-write-smoke",
        f"{payload['source_dry_run_plan_review_block_id']}:dry-run-plan-review-block",
        f"{payload['source_dry_run_plan_review_block_id']}:implementation-preflight-blocked",
        f"{payload['source_preflight_guard_id']}:implementation-preflight-guard",
        f"{payload['source_preflight_guard_id']}:no-write-smoke",
    ):
        if required_ref not in evidence_refs:
            raise ValueError("legacy migration final writer implementation no-write smoke requires provenance evidence refs")
    for ref in [*evidence_refs, *planned_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer implementation no-write smoke refs must be portable")


def _legacy_final_writer_no_write_smoke_mapping(
    mapping: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_no_write_smoke_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"legacy migration final writer implementation no-write smoke {label} are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"legacy migration final writer implementation no-write smoke {label} must be strings")
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_no_write_smoke_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_no_write_smoke_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_legacy_migration_final_writer_implementation_regression_consolidation_payload(
    payload: Mapping[str, object],
) -> None:
    if _required_legacy_final_writer_regression_consolidation_str(payload, "schema_version") != "1.0.0":
        raise ValueError(
            "legacy migration final writer implementation regression consolidation schema_version must be 1.0.0"
        )
    if _required_legacy_final_writer_regression_consolidation_str(payload, "kind") != (
        "legacy_migration_final_writer_implementation_regression_consolidation"
    ):
        raise ValueError("legacy migration final writer implementation regression consolidation kind is invalid")
    if _required_legacy_final_writer_regression_consolidation_str(payload, "status") != "consolidated":
        raise ValueError("legacy migration final writer implementation regression consolidation status is invalid")
    for key in (
        "regression_consolidation_created",
        "no_write_smoke_verified",
        "final_writer_chain_consolidated",
        "regression_passed",
    ):
        if payload.get(key) is not True:
            raise ValueError("legacy migration final writer implementation regression consolidation must verify regression")
    for key in (
        "implementation_opening_allowed",
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "preflight_passed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "memory_publication_allowed",
    ):
        if payload.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(
                f"legacy migration final writer implementation regression consolidation must not allow {label}"
            )
    for key in (
        "source_no_write_smoke_id",
        "source_preflight_guard_id",
        "source_dry_run_plan_review_block_id",
        "source_dry_run_plan_block_id",
        "source_design_review_block_id",
        "source_design_gate_id",
        "source_readiness_block_id",
        "source_validation_id",
        "source_writer_contract_gate_id",
        "source_implementation_block_id",
        "source_audit_evidence_id",
        "source_commit_block_id",
        "source_transaction_dry_run_id",
        "source_final_approval_id",
        "source_final_publication_block_id",
        "source_staged_candidate_id",
        "source_staged_review_block_id",
        "source_proposal_id",
        "source_import_candidate_id",
        "source_memory_publication_block_id",
        "source_promotion_block_id",
        "source_job_id",
        "source_approval_id",
        "source_plan_id",
        "source_selection_manifest_id",
        "source_rollback_manifest_id",
        "verified_worker_evidence_id",
        "checked_by",
        "checked_at",
        "check_note",
        "required_next_gate",
    ):
        _required_legacy_final_writer_regression_consolidation_str(payload, key)
    _validate_segment(
        "target_namespace_id",
        _required_legacy_final_writer_regression_consolidation_str(payload, "target_namespace_id"),
    )
    output_uri = _required_legacy_final_writer_regression_consolidation_str(payload, "verified_output_uri")
    if not output_uri.startswith("crp://") or _looks_like_os_path(output_uri):
        raise ValueError(
            "legacy migration final writer implementation regression consolidation output uri must be portable"
        )
    checked_at = _required_legacy_final_writer_regression_consolidation_str(payload, "checked_at")
    if "T" not in checked_at or not checked_at.endswith("Z"):
        raise ValueError("legacy migration final writer implementation regression consolidation checked_at must be UTC")
    if _required_legacy_final_writer_regression_consolidation_int(payload, "selected_candidate_count") <= 0:
        raise ValueError(
            "legacy migration final writer implementation regression consolidation requires selected candidates"
        )
    if payload.get("required_next_gate") != "phase9_thin_ui_validation_path_refresh":
        raise ValueError("legacy migration final writer implementation regression consolidation next gate is invalid")
    regression = _legacy_final_writer_regression_consolidation_mapping(payload, "regression_summary")
    if regression.get("mode") != "final_writer_blocker_chain_regression_consolidation":
        raise ValueError("legacy migration final writer implementation regression consolidation mode is invalid")
    if regression.get("source_no_write_smoke_id") != payload.get("source_no_write_smoke_id"):
        raise ValueError("legacy migration final writer implementation regression consolidation smoke source mismatch")
    if regression.get("source_preflight_guard_id") != payload.get("source_preflight_guard_id"):
        raise ValueError("legacy migration final writer implementation regression consolidation preflight source mismatch")
    if regression.get("target_namespace_id") != payload.get("target_namespace_id"):
        raise ValueError("legacy migration final writer implementation regression consolidation namespace mismatch")
    planned_refs = _legacy_final_writer_regression_consolidation_ref_list(
        regression.get("planned_operation_refs"), "planned operation refs"
    )
    if _required_legacy_final_writer_regression_consolidation_int(regression, "planned_operation_count") != len(
        planned_refs
    ):
        raise ValueError("legacy migration final writer implementation regression consolidation planned count mismatch")
    chain_refs = _legacy_final_writer_regression_consolidation_ref_list(
        regression.get("chain_stage_refs"), "chain stage refs"
    )
    if _required_legacy_final_writer_regression_consolidation_int(regression, "chain_stage_count") != len(
        chain_refs
    ):
        raise ValueError("legacy migration final writer implementation regression consolidation chain count mismatch")
    for required_ref in (
        f"{payload['source_writer_contract_gate_id']}:writer-contract-gate",
        f"{payload['source_validation_id']}:writer-contract-validation",
        f"{payload['source_readiness_block_id']}:implementation-readiness-block",
        f"{payload['source_design_gate_id']}:implementation-design-gate",
        f"{payload['source_design_review_block_id']}:implementation-design-review-block",
        f"{payload['source_dry_run_plan_block_id']}:implementation-dry-run-plan-block",
        f"{payload['source_dry_run_plan_review_block_id']}:implementation-dry-run-plan-review-block",
        f"{payload['source_preflight_guard_id']}:implementation-preflight-guard",
        f"{payload['source_no_write_smoke_id']}:implementation-no-write-smoke",
    ):
        if required_ref not in chain_refs:
            raise ValueError("legacy migration final writer implementation regression consolidation requires chain refs")
    checks = _legacy_final_writer_regression_consolidation_ref_list(
        regression.get("required_regression_checks"), "required regression checks"
    )
    for required in (
        "writer_contract_gate_present",
        "writer_contract_validation_present",
        "implementation_readiness_blocked",
        "implementation_design_reviewed",
        "dry_run_plan_reviewed",
        "preflight_not_passed",
        "no_write_smoke_passed",
        "memory_publication_blocked",
        "legacy_write_blocked",
        "portable_provenance_refs_only",
    ):
        if required not in checks:
            raise ValueError(
                "legacy migration final writer implementation regression consolidation requires regression checks"
            )
    observed = _legacy_final_writer_regression_consolidation_mapping(regression, "observed_write_counts")
    for key in (
        "memory_atoms",
        "staging_atoms",
        "legacy_writes",
        "published_outputs",
        "implementation_operations",
        "commit_operations",
    ):
        if observed.get(key) != 0:
            raise ValueError("legacy migration final writer implementation regression consolidation must not include writes")
    if regression.get("regression_passed") is not True:
        raise ValueError("legacy migration final writer implementation regression consolidation must pass regression")
    if regression.get("execution_allowed") is not False or regression.get("implementation_allowed") is not False:
        raise ValueError("legacy migration final writer implementation regression consolidation must not allow execution")
    if regression.get("requires_phase9_thin_ui_validation_refresh") is not True:
        raise ValueError("legacy migration final writer implementation regression consolidation must require phase9 refresh")
    safety = _legacy_final_writer_regression_consolidation_mapping(payload, "safety")
    for key in (
        "regression_consolidation_created",
        "no_write_smoke_verified",
        "final_writer_chain_consolidated",
        "regression_passed",
    ):
        if safety.get(key) is not True:
            raise ValueError(
                "legacy migration final writer implementation regression consolidation safety must verify regression"
            )
    for key in (
        "implementation_opening_allowed",
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "preflight_passed",
        "dry_run_plan_allowed",
        "dry_run_plan_created",
        "commit_allowed",
        "publish_transaction_executed",
        "legacy_write_allowed",
        "migration_executed",
        "rollback_executed",
        "memory_published",
        "memory_publication_allowed",
        "l1_l2_publication_allowed",
        "memory_atoms_written",
        "staging_atoms_written",
    ):
        if safety.get(key) is not False:
            label = key.replace("_", " ")
            raise ValueError(f"legacy migration final writer implementation regression consolidation must not {label}")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, Sequence) or isinstance(published_outputs, (str, bytes)) or published_outputs:
        raise ValueError("legacy migration final writer implementation regression consolidation must not publish outputs")
    if safety.get("dry_run_only") is not True:
        raise ValueError("legacy migration final writer implementation regression consolidation must be dry-run-only")
    evidence_refs = _legacy_final_writer_regression_consolidation_ref_list(payload.get("evidence_refs"), "evidence refs")
    for required_ref in (
        "R093:legacy-migration-final-writer-implementation-no-write-smoke",
        "R094:legacy-migration-final-writer-implementation-regression-consolidation",
        f"{payload['source_preflight_guard_id']}:implementation-preflight-guard",
        f"{payload['source_preflight_guard_id']}:no-write-smoke",
        f"{payload['source_no_write_smoke_id']}:implementation-no-write-smoke",
        f"{payload['source_no_write_smoke_id']}:final-writer-regression-consolidated",
    ):
        if required_ref not in evidence_refs:
            raise ValueError(
                "legacy migration final writer implementation regression consolidation requires provenance evidence refs"
            )
    for ref in [*evidence_refs, *planned_refs, *chain_refs]:
        if _looks_like_os_path(ref):
            raise ValueError("legacy migration final writer implementation regression consolidation refs must be portable")


def _legacy_final_writer_regression_consolidation_mapping(
    mapping: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} is required")
    return value


def _legacy_final_writer_regression_consolidation_ref_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(
            f"legacy migration final writer implementation regression consolidation {label} are required"
        )
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ValueError(
                f"legacy migration final writer implementation regression consolidation {label} must be strings"
            )
        refs.append(ref)
    return tuple(refs)


def _required_legacy_final_writer_regression_consolidation_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_legacy_final_writer_regression_consolidation_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is required")
    return value


def _validate_phase7_performance_budget_payload(payload: Mapping[str, object]) -> None:
    if _required_phase7_budget_str(payload, "schema_version") != "1.0.0":
        raise ValueError("phase 7 performance budget schema_version must be 1.0.0")
    if _required_phase7_budget_str(payload, "kind") != "phase7_performance_budget":
        raise ValueError("phase 7 performance budget kind is invalid")
    source_round = _required_phase7_budget_str(payload, "source_round")
    if not source_round.startswith("R"):
        raise ValueError("phase 7 performance budget source_round must be an R-round id")
    measured_at = _required_phase7_budget_str(payload, "measured_at")
    if "T" not in measured_at or not measured_at.endswith("Z"):
        raise ValueError("phase 7 performance budget measured_at must be UTC")
    status = _required_phase7_budget_str(payload, "status")
    if status not in {"within_budget", "over_budget", "blocked"}:
        raise ValueError("phase 7 performance budget status is invalid")
    samples = payload.get("samples")
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)) or not samples:
        raise ValueError("phase 7 performance budget requires samples")
    sample_names: list[str] = []
    over_budget_names: list[str] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ValueError("phase 7 performance budget sample must be an object")
        name = _required_phase7_budget_str(sample, "name")
        sample_names.append(name)
        evidence_ref = _required_phase7_budget_str(sample, "evidence_ref")
        if _looks_like_os_path(evidence_ref):
            raise ValueError("phase 7 performance budget evidence must not be an OS path")
        duration = _required_phase7_budget_number(sample, "duration_seconds")
        budget = _required_phase7_budget_number(sample, "budget_seconds")
        if duration < 0:
            raise ValueError("phase 7 performance budget duration cannot be negative")
        if budget <= 0:
            raise ValueError("phase 7 performance budget sample budget must be positive")
        within_budget = sample.get("within_budget")
        if within_budget is not (duration <= budget):
            raise ValueError("phase 7 performance budget sample within_budget mismatch")
        if duration > budget:
            over_budget_names.append(name)
    if len(set(sample_names)) != len(sample_names):
        raise ValueError("phase 7 performance budget sample names must be unique")
    sample_count = payload.get("sample_count")
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count != len(samples):
        raise ValueError("phase 7 performance budget sample_count mismatch")
    over_budget_samples = payload.get("over_budget_samples")
    if not isinstance(over_budget_samples, Sequence) or isinstance(over_budget_samples, (str, bytes)):
        raise ValueError("phase 7 performance budget requires over_budget_samples")
    if tuple(over_budget_samples) != tuple(over_budget_names):
        raise ValueError("phase 7 performance budget over_budget_samples mismatch")
    if status == "within_budget" and over_budget_names:
        raise ValueError("phase 7 performance budget within_budget status cannot include over-budget samples")
    if status == "over_budget" and not over_budget_names:
        raise ValueError("phase 7 performance budget over_budget status requires over-budget samples")
    if status == "blocked":
        blocking = payload.get("blocking_check_names")
        if not isinstance(blocking, Sequence) or isinstance(blocking, (str, bytes)) or not blocking:
            raise ValueError("blocked phase 7 performance budget requires blocking checks")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)) or not evidence_refs:
        raise ValueError("phase 7 performance budget requires evidence refs")
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref:
            raise ValueError("phase 7 performance budget evidence ref must be a string")
        if _looks_like_os_path(ref):
            raise ValueError("phase 7 performance budget evidence ref must not be an OS path")


def _required_phase7_budget_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_phase7_budget_number(mapping: Mapping[str, object], key: str) -> float:
    value = mapping.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} is required")
    return float(value)


def _is_absolute_or_parent_relative(value: str) -> bool:
    path = Path(value)
    if path.is_absolute():
        return True
    return any(part == ".." for part in path.parts)


def _looks_like_os_path(value: str) -> bool:
    return bool(re.search(r"(^[A-Za-z]:[\\/])|(^[\\/])|(file://)", value))
