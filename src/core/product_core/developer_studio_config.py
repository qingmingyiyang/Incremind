from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .ports import ObjectStorePort
from .prompt_activation import initial_prompt_activation_projection, serialize_prompt_activation


class DeveloperStudioConfigError(ValueError):
    """Raised when Developer Studio config is invalid."""


@dataclass(frozen=True, slots=True)
class DeveloperStudioConfig:
    status: str
    config_id: str
    revision: int
    model_profiles: tuple[Mapping[str, object], ...]
    task_model_map: Mapping[str, object]
    prompts: tuple[Mapping[str, object], ...]
    skills: tuple[Mapping[str, object], ...]
    workflow_steps: tuple[Mapping[str, object], ...]
    snapshots: tuple[Mapping[str, object], ...]
    prompt_activation: Mapping[str, object]
    updated_at: str


class GetDeveloperStudioConfig:
    _COLLECTION = "developer_studio_configs"
    _CONFIG_ID = "default"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> DeveloperStudioConfig:
        record = self._object_store.read(self._COLLECTION, self._CONFIG_ID)
        if record is None:
            return _config_from_record(_default_config_record())
        return _config_from_record(record)


class SaveDeveloperStudioConfig:
    _COLLECTION = "developer_studio_configs"
    _CONFIG_ID = "default"

    def __init__(self, object_store: ObjectStorePort, *, now: str = "2026-07-03T17:10:00+08:00") -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        model_profiles: Sequence[Mapping[str, object]],
        task_model_map: Mapping[str, object] | None = None,
        prompts: Sequence[Mapping[str, object]],
        skills: Sequence[Mapping[str, object]],
        workflow_steps: Sequence[Mapping[str, object]],
        snapshots: Sequence[Mapping[str, object]] = (),
        expected_revision: int | None = None,
    ) -> DeveloperStudioConfig:
        store_revision = self._object_store.revision(self._COLLECTION, self._CONFIG_ID)
        current = self._object_store.read(self._COLLECTION, self._CONFIG_ID)
        if self._object_store.revision(self._COLLECTION, self._CONFIG_ID) != store_revision:
            raise DeveloperStudioConfigError("developer studio config revision conflict")
        if expected_revision is not None:
            current_revision = int(current.get("revision", 0)) if current is not None else 0
            if current_revision != expected_revision:
                raise DeveloperStudioConfigError("developer studio config revision conflict")
        current_task_model_map = _clean_mapping(
            current.get("task_model_map", {}) if current is not None else {},
            "task_model_map",
        )
        if task_model_map is not None:
            requested_task_model_map = _clean_mapping(task_model_map, "task_model_map")
            if requested_task_model_map != current_task_model_map:
                raise DeveloperStudioConfigError("legacy task_model_map is read-only")
        revision = (int(current.get("revision", 0)) if current is not None else 0) + 1
        record = {
            "schema_version": "1.0.0",
            "id": self._CONFIG_ID,
            "revision": revision,
            "model_profiles": _clean_sequence(model_profiles, "model_profiles"),
            "task_model_map": current_task_model_map,
            "prompts": _clean_sequence(prompts, "prompts"),
            "skills": _clean_sequence(skills, "skills"),
            "workflow_steps": _clean_sequence(workflow_steps, "workflow_steps"),
            "snapshots": _clean_sequence(snapshots, "snapshots"),
            "prompt_activation": initial_prompt_activation_projection(current),
            "updated_at": self._now,
        }
        _reject_sensitive_material(record)
        try:
            self._object_store.write(
                self._COLLECTION,
                self._CONFIG_ID,
                record,
                expected_revision=store_revision,
            )
        except ValueError as error:
            if "expected revision" in str(error):
                raise DeveloperStudioConfigError("developer studio config revision conflict") from error
            raise
        return _config_from_record(record)


def serialize_developer_studio_config(config: DeveloperStudioConfig) -> dict[str, object]:
    return {
        "status": config.status,
        "config_id": config.config_id,
        "revision": config.revision,
        "model_profiles": [dict(item) for item in config.model_profiles],
        "task_model_map": dict(config.task_model_map),
        "prompts": [dict(item) for item in config.prompts],
        "skills": [dict(item) for item in config.skills],
        "workflow_steps": [dict(item) for item in config.workflow_steps],
        "snapshots": [dict(item) for item in config.snapshots],
        "prompt_activation": dict(config.prompt_activation),
        "prompt_activation_status": serialize_prompt_activation(config),
        "updated_at": config.updated_at,
        "sensitive_material_policy": "api keys, cookies, authorization headers and local credential files are not accepted",
    }


def _default_config_record() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "default",
        "revision": 0,
        "model_profiles": [],
        "task_model_map": {},
        "prompts": [],
        "skills": [],
        "workflow_steps": [],
        "snapshots": [],
        "prompt_activation": initial_prompt_activation_projection(None),
        "updated_at": "",
    }


def _config_from_record(record: Mapping[str, object]) -> DeveloperStudioConfig:
    revision_value = record.get("revision", 0)
    revision = revision_value if isinstance(revision_value, int) else 0
    return DeveloperStudioConfig(
        status="ready" if revision > 0 else "default_empty",
        config_id=_required_text(record.get("id"), "id"),
        revision=revision,
        model_profiles=tuple(_mapping_items(record.get("model_profiles"))),
        task_model_map=_clean_mapping(record.get("task_model_map", {}), "task_model_map"),
        prompts=tuple(_mapping_items(record.get("prompts"))),
        skills=tuple(_mapping_items(record.get("skills"))),
        workflow_steps=tuple(_mapping_items(record.get("workflow_steps"))),
        snapshots=tuple(_mapping_items(record.get("snapshots"))),
        prompt_activation=initial_prompt_activation_projection(record),
        updated_at=record.get("updated_at") if isinstance(record.get("updated_at"), str) else "",
    )


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _clean_sequence(value: Sequence[Mapping[str, object]], name: str) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DeveloperStudioConfigError(f"{name} must be a list")
    cleaned: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise DeveloperStudioConfigError(f"{name} items must be objects")
        cleaned.append(_clean_mapping(item, f"{name} item"))
    return cleaned


def _clean_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DeveloperStudioConfigError(f"{name} must be an object")
    return {str(key): _clean_value(item) for key, item in value.items()}


def _clean_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _clean_mapping(value, "nested")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_clean_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _reject_sensitive_material(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in {"api_key", "apikey", "authorization", "cookie", "cookies", "cookies_file"}:
                raise DeveloperStudioConfigError(f"sensitive field is not allowed: {key}")
            _reject_sensitive_material(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_sensitive_material(item)
        return
    if isinstance(value, str):
        lowered = value.lower()
        if "authorization:" in lowered or "cookie:" in lowered:
            raise DeveloperStudioConfigError("sensitive header text is not allowed")
        if "sk-" in lowered and len(value) >= 16:
            raise DeveloperStudioConfigError("secret-like token is not allowed")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeveloperStudioConfigError(f"{name} is required")
    return value.strip()
