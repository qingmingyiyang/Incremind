from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .ports import ObjectStorePort


PROMPT_ACTIVATION_UNITS: dict[str, tuple[str, ...]] = {
    "companion.chat": ("pt-companion-character",),
    "intake.classification": ("pt-input-understanding",),
    "source.template-document": (
        "pt-title",
        "pt-detail-summary",
        "pt-longterm-organize",
        "pt-output-validate",
    ),
}


class PromptActivationError(ValueError):
    pass


class PromptActivationConflict(PromptActivationError):
    pass


@dataclass(frozen=True, slots=True)
class PromptActivationResult:
    status: str
    config_revision: int
    activation_revision: int
    unit_id: str
    unit_revision: int
    replayed: bool
    projection: Mapping[str, object]


def initial_prompt_activation_projection(current: Mapping[str, object] | None) -> dict[str, object]:
    if isinstance(current, Mapping) and isinstance(current.get("prompt_activation"), Mapping):
        return _clean_projection(current["prompt_activation"])
    prompts = _prompt_sequence(current.get("prompts")) if isinstance(current, Mapping) else ()
    config_revision = _int_value(current.get("revision"), 0) if isinstance(current, Mapping) else 0
    updated_at = _text(current.get("updated_at")) if isinstance(current, Mapping) else ""
    units: dict[str, object] = {}
    for unit_id, prompt_ids in PROMPT_ACTIVATION_UNITS.items():
        snapshot = _snapshot_for(prompts, prompt_ids, require_complete=False)
        units[unit_id] = {
            "unit_id": unit_id,
            "unit_revision": 0,
            "prompt_ids": list(prompt_ids),
            "active_prompts": list(snapshot),
            "source_config_revision": config_revision,
            "activated_at": updated_at,
            "reason": "legacy_baseline" if snapshot else "product_core_fallback",
        }
    return {
        "schema_version": "1.0.0",
        "revision": 0,
        "units": units,
        "history": [],
    }


def resolve_active_prompt(config: object, prompt_id: str) -> Mapping[str, object] | None:
    projection = getattr(config, "prompt_activation", None)
    if not isinstance(projection, Mapping):
        return None
    units = projection.get("units")
    if not isinstance(units, Mapping):
        return None
    for unit in units.values():
        if not isinstance(unit, Mapping):
            continue
        for prompt in _prompt_sequence(unit.get("active_prompts")):
            if prompt.get("id") == prompt_id:
                return dict(prompt)
    return None


def serialize_prompt_activation(config: object) -> dict[str, object]:
    projection = getattr(config, "prompt_activation", None)
    prompts = getattr(config, "prompts", ())
    config_revision = getattr(config, "revision", 0)
    clean = _clean_projection(projection if isinstance(projection, Mapping) else {})
    units_payload: list[dict[str, object]] = []
    for unit_id, prompt_ids in PROMPT_ACTIVATION_UNITS.items():
        unit = _unit(clean, unit_id)
        active = _prompt_sequence(unit.get("active_prompts"))
        drafts = _snapshot_for(_prompt_sequence(prompts), prompt_ids, require_complete=False)
        units_payload.append(
            {
                "unit_id": unit_id,
                "prompt_ids": list(prompt_ids),
                "unit_revision": _int_value(unit.get("unit_revision"), 0),
                "active_prompt_ids": [str(item["id"]) for item in active],
                "active_prompt_versions": {
                    str(item["id"]): _int_value(item.get("version"), 0) for item in active
                },
                "draft_prompt_ids": [str(item["id"]) for item in drafts],
                "draft_prompt_versions": {
                    str(item["id"]): _int_value(item.get("version"), 0) for item in drafts
                },
                "active_fingerprint": _fingerprint(active),
                "draft_fingerprint": _fingerprint(drafts),
                "has_complete_draft": len(drafts) == len(prompt_ids),
                "dirty": _fingerprint(active) != _fingerprint(drafts),
                "can_rollback": _has_reversible_event(clean, unit_id, active),
                "source_config_revision": _int_value(unit.get("source_config_revision"), 0),
                "activated_at": _text(unit.get("activated_at")),
                "reason": _text(unit.get("reason")),
            }
        )
    return {
        "status": "ready",
        "config_revision": config_revision if isinstance(config_revision, int) else 0,
        "activation_revision": _int_value(clean.get("revision"), 0),
        "units": units_payload,
        "history": [dict(item) for item in _mapping_sequence(clean.get("history"))],
        "draft_save_effect": "none_until_explicit_activation",
        "test_lab_prompt_source": "draft_compatibility_until_stage_d4",
    }


class PromptActivationService:
    _COLLECTION = "developer_studio_configs"
    _CONFIG_ID = "default"

    def __init__(self, object_store: ObjectStorePort, *, now: str | None = None) -> None:
        self._store = object_store
        self._now = now or datetime.now(timezone.utc).isoformat()

    def preview(
        self,
        *,
        unit_id: str,
        expected_config_revision: int,
        expected_activation_revision: int,
    ) -> dict[str, object]:
        record, _store_revision = self._record()
        projection = initial_prompt_activation_projection(record)
        self._check_revisions(record, projection, expected_config_revision, expected_activation_revision)
        prompt_ids = _required_unit(unit_id)
        drafts = _snapshot_for(_prompt_sequence(record.get("prompts")), prompt_ids, require_complete=True)
        _reject_sensitive(drafts)
        unit = _unit(projection, unit_id)
        active = _prompt_sequence(unit.get("active_prompts"))
        token = _preview_token(
            unit_id=unit_id,
            config_revision=expected_config_revision,
            activation_revision=expected_activation_revision,
            drafts=drafts,
        )
        return {
            "status": "validated",
            "unit_id": unit_id,
            "prompt_ids": list(prompt_ids),
            "config_revision": expected_config_revision,
            "activation_revision": expected_activation_revision,
            "unit_revision": _int_value(unit.get("unit_revision"), 0),
            "preview_token": token,
            "active_fingerprint": _fingerprint(active),
            "draft_fingerprint": _fingerprint(drafts),
            "changed": _fingerprint(active) != _fingerprint(drafts),
            "consumer_manifest": _consumer_manifest(unit_id),
            "validation": {
                "complete_unit": True,
                "secret_free": True,
                "non_empty_content": True,
                "production_consumers_verified": True,
            },
        }

    def activate(
        self,
        *,
        unit_id: str,
        expected_config_revision: int,
        expected_activation_revision: int,
        preview_token: str,
        confirm: bool,
        reason: str,
    ) -> PromptActivationResult:
        if confirm is not True:
            raise PromptActivationError("prompt activation requires explicit confirmation")
        clean_reason = reason.strip()
        if not clean_reason:
            raise PromptActivationError("activation reason is required")
        _reject_sensitive(({"content": clean_reason},))
        preview = self.preview(
            unit_id=unit_id,
            expected_config_revision=expected_config_revision,
            expected_activation_revision=expected_activation_revision,
        )
        if preview_token != preview["preview_token"]:
            raise PromptActivationConflict("prompt activation preview drifted")
        record, store_revision = self._record()
        projection = initial_prompt_activation_projection(record)
        self._check_revisions(record, projection, expected_config_revision, expected_activation_revision)
        prompt_ids = _required_unit(unit_id)
        drafts = _snapshot_for(_prompt_sequence(record.get("prompts")), prompt_ids, require_complete=True)
        current_token = _preview_token(
            unit_id=unit_id,
            config_revision=expected_config_revision,
            activation_revision=expected_activation_revision,
            drafts=drafts,
        )
        if preview_token != current_token:
            raise PromptActivationConflict("prompt activation preview drifted")
        unit = _unit(projection, unit_id)
        before = _prompt_sequence(unit.get("active_prompts"))
        if _fingerprint(before) == _fingerprint(drafts):
            return _result("already_active", record, projection, unit_id, replayed=True)
        next_activation_revision = _int_value(projection.get("revision"), 0) + 1
        next_config_revision = _int_value(record.get("revision"), 0) + 1
        next_unit_revision = _int_value(unit.get("unit_revision"), 0) + 1
        next_unit = {
            "unit_id": unit_id,
            "unit_revision": next_unit_revision,
            "prompt_ids": list(prompt_ids),
            "active_prompts": [dict(item) for item in drafts],
            "source_config_revision": expected_config_revision,
            "activated_at": self._now,
            "reason": clean_reason,
        }
        next_projection = _replace_unit(
            projection,
            next_unit,
            history_entry={
                "action": "activate",
                "activation_revision": next_activation_revision,
                "unit_id": unit_id,
                "unit_revision": next_unit_revision,
                "before": [dict(item) for item in before],
                "after": [dict(item) for item in drafts],
                "before_source_config_revision": _int_value(unit.get("source_config_revision"), 0),
                "reason": clean_reason,
                "at": self._now,
            },
        )
        next_record = dict(record)
        next_record["revision"] = next_config_revision
        next_record["prompt_activation"] = next_projection
        next_record["updated_at"] = self._now
        self._write(next_record, store_revision)
        return _result("activated", next_record, next_projection, unit_id, replayed=False)

    def rollback(
        self,
        *,
        unit_id: str,
        expected_config_revision: int,
        expected_activation_revision: int,
        confirm: bool,
        reason: str,
    ) -> PromptActivationResult:
        if confirm is not True:
            raise PromptActivationError("prompt rollback requires explicit confirmation")
        clean_reason = reason.strip()
        if not clean_reason:
            raise PromptActivationError("rollback reason is required")
        _reject_sensitive(({"content": clean_reason},))
        record, store_revision = self._record()
        projection = initial_prompt_activation_projection(record)
        self._check_revisions(record, projection, expected_config_revision, expected_activation_revision)
        prompt_ids = _required_unit(unit_id)
        unit = _unit(projection, unit_id)
        current = _prompt_sequence(unit.get("active_prompts"))
        source_event = _latest_reversible_event(projection, unit_id, current)
        before = _prompt_sequence(source_event.get("before"))
        next_activation_revision = expected_activation_revision + 1
        next_config_revision = expected_config_revision + 1
        next_unit_revision = _int_value(unit.get("unit_revision"), 0) + 1
        next_unit = {
            "unit_id": unit_id,
            "unit_revision": next_unit_revision,
            "prompt_ids": list(prompt_ids),
            "active_prompts": [dict(item) for item in before],
            "source_config_revision": _int_value(source_event.get("before_source_config_revision"), 0),
            "activated_at": self._now,
            "reason": clean_reason,
        }
        next_projection = _replace_unit(
            projection,
            next_unit,
            history_entry={
                "action": "rollback",
                "activation_revision": next_activation_revision,
                "unit_id": unit_id,
                "unit_revision": next_unit_revision,
                "before": [dict(item) for item in current],
                "after": [dict(item) for item in before],
                "reason": clean_reason,
                "rollback_of_activation_revision": source_event["activation_revision"],
                "at": self._now,
            },
        )
        next_record = dict(record)
        next_record["revision"] = next_config_revision
        next_record["prompt_activation"] = next_projection
        next_record["updated_at"] = self._now
        self._write(next_record, store_revision)
        return _result("rolled_back", next_record, next_projection, unit_id, replayed=False)

    def _record(self) -> tuple[dict[str, object], int]:
        store_revision = self._store.revision(self._COLLECTION, self._CONFIG_ID)
        record = self._store.read(self._COLLECTION, self._CONFIG_ID)
        if self._store.revision(self._COLLECTION, self._CONFIG_ID) != store_revision:
            raise PromptActivationConflict("developer studio config storage revision conflict")
        if record is None:
            return ({
                "schema_version": "1.0.0",
                "id": self._CONFIG_ID,
                "revision": 0,
                "model_profiles": [],
                "task_model_map": {},
                "prompts": [],
                "skills": [],
                "workflow_steps": [],
                "snapshots": [],
                "updated_at": "",
            }, store_revision)
        return dict(record), store_revision

    def _write(self, record: Mapping[str, object], store_revision: int) -> None:
        try:
            self._store.write(
                self._COLLECTION,
                self._CONFIG_ID,
                record,
                expected_revision=store_revision,
            )
        except ValueError as error:
            if "expected revision" in str(error):
                raise PromptActivationConflict("developer studio config storage revision conflict") from error
            raise

    @staticmethod
    def _check_revisions(
        record: Mapping[str, object],
        projection: Mapping[str, object],
        expected_config_revision: int,
        expected_activation_revision: int,
    ) -> None:
        if _int_value(record.get("revision"), 0) != expected_config_revision:
            raise PromptActivationConflict("developer studio config revision conflict")
        if _int_value(projection.get("revision"), 0) != expected_activation_revision:
            raise PromptActivationConflict("prompt activation revision conflict")


def _required_unit(unit_id: str) -> tuple[str, ...]:
    prompt_ids = PROMPT_ACTIVATION_UNITS.get(unit_id)
    if prompt_ids is None:
        raise PromptActivationError(f"unsupported prompt activation unit: {unit_id}")
    return prompt_ids


def _consumer_manifest(unit_id: str) -> list[str]:
    if unit_id == "intake.classification":
        return ["workbench.input-classifier", "workbench.auto-intake", "model-route:intake.classification"]
    if unit_id == "companion.chat":
        return ["companion.prompt-composer", "model-route:companion.chat"]
    return ["source.provider-template-document"]


def _snapshot_for(
    prompts: Sequence[Mapping[str, object]],
    prompt_ids: Sequence[str],
    *,
    require_complete: bool,
) -> tuple[dict[str, object], ...]:
    by_id = {str(item.get("id")): item for item in prompts if isinstance(item.get("id"), str)}
    result: list[dict[str, object]] = []
    for prompt_id in prompt_ids:
        prompt = by_id.get(prompt_id)
        if prompt is None:
            if require_complete:
                raise PromptActivationError(f"draft prompt missing for activation: {prompt_id}")
            continue
        content = prompt.get("content")
        if not isinstance(content, str) or not content.strip():
            if require_complete:
                raise PromptActivationError(f"draft prompt content is empty: {prompt_id}")
            continue
        result.append(_clean_prompt(prompt))
    if require_complete and len(result) != len(prompt_ids):
        raise PromptActivationError("prompt activation unit is incomplete")
    return tuple(result)


def _clean_prompt(prompt: Mapping[str, object]) -> dict[str, object]:
    allowed = (
        "id", "stageId", "name", "description", "content", "variables", "outputSchema",
        "modelProfileId", "version", "isProtected", "updatedAt",
    )
    return {key: _clean_value(prompt[key]) for key in allowed if key in prompt}


def _clean_projection(value: Mapping[str, object]) -> dict[str, object]:
    _reject_sensitive((value,))
    if value.get("schema_version") != "1.0.0":
        raise PromptActivationError("invalid prompt activation schema_version")
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise PromptActivationError("invalid prompt activation revision")
    units_value = value.get("units")
    if not isinstance(units_value, Mapping):
        raise PromptActivationError("prompt activation units must be an object")
    if set(str(key) for key in units_value) != set(PROMPT_ACTIVATION_UNITS):
        raise PromptActivationError("prompt activation units do not match the consumer manifest")
    units: dict[str, object] = {}
    for unit_id, prompt_ids in PROMPT_ACTIVATION_UNITS.items():
        raw_unit = units_value.get(unit_id)
        if not isinstance(raw_unit, Mapping) or raw_unit.get("unit_id") != unit_id:
            raise PromptActivationError(f"invalid prompt activation unit: {unit_id}")
        if tuple(raw_unit.get("prompt_ids", ())) != prompt_ids:
            raise PromptActivationError(f"prompt activation unit membership drifted: {unit_id}")
        unit_revision = raw_unit.get("unit_revision")
        if not isinstance(unit_revision, int) or isinstance(unit_revision, bool) or unit_revision < 0:
            raise PromptActivationError(f"invalid prompt activation unit revision: {unit_id}")
        active = _prompt_sequence(raw_unit.get("active_prompts"))
        active_ids = tuple(str(item.get("id")) for item in active)
        expected_subset = tuple(prompt_id for prompt_id in prompt_ids if prompt_id in set(active_ids))
        if active_ids != expected_subset or len(set(active_ids)) != len(active_ids):
            raise PromptActivationError(f"active prompt membership drifted: {unit_id}")
        for prompt in active:
            content = prompt.get("content")
            if not isinstance(content, str) or not content.strip():
                raise PromptActivationError(f"active prompt content is empty: {prompt.get('id')}")
        _reject_sensitive(active)
        units[unit_id] = _clean_value(raw_unit)
    history_value = value.get("history")
    if not isinstance(history_value, Sequence) or isinstance(history_value, (str, bytes)):
        raise PromptActivationError("prompt activation history must be an array")
    history = _mapping_sequence(history_value)
    if len(history) != len(history_value):
        raise PromptActivationError("prompt activation history items must be objects")
    clean_history = [_clean_history_event(item) for item in history]
    history_revisions = [_int_value(item.get("activation_revision"), -1) for item in clean_history]
    if history_revisions != sorted(set(history_revisions)):
        raise PromptActivationError("prompt activation history revisions must be unique and ordered")
    if clean_history and history_revisions[-1] != revision:
        raise PromptActivationError("prompt activation history does not reach the current revision")
    if not clean_history and revision != 0:
        raise PromptActivationError("prompt activation history is missing")
    return {
        "schema_version": "1.0.0",
        "revision": revision,
        "units": units,
        "history": clean_history,
    }


def _clean_history_event(event: Mapping[str, object]) -> dict[str, object]:
    action = event.get("action")
    if action not in {"activate", "rollback"}:
        raise PromptActivationError("invalid prompt activation history action")
    unit_id = event.get("unit_id")
    if not isinstance(unit_id, str) or unit_id not in PROMPT_ACTIVATION_UNITS:
        raise PromptActivationError("invalid prompt activation history unit")
    activation_revision = event.get("activation_revision")
    unit_revision = event.get("unit_revision")
    if not isinstance(activation_revision, int) or isinstance(activation_revision, bool) or activation_revision < 1:
        raise PromptActivationError("invalid prompt activation history revision")
    if not isinstance(unit_revision, int) or isinstance(unit_revision, bool) or unit_revision < 1:
        raise PromptActivationError("invalid prompt activation history unit revision")
    before = _validated_history_snapshot(event.get("before"), unit_id, "before")
    after = _validated_history_snapshot(event.get("after"), unit_id, "after")
    if action == "activate" and len(after) != len(PROMPT_ACTIVATION_UNITS[unit_id]):
        raise PromptActivationError("activated prompt history snapshot is incomplete")
    reason = event.get("reason")
    at = event.get("at")
    if not isinstance(reason, str) or not reason.strip() or not isinstance(at, str) or not at.strip():
        raise PromptActivationError("prompt activation history reason and timestamp are required")
    cleaned = dict(event)
    cleaned["before"] = [dict(item) for item in before]
    cleaned["after"] = [dict(item) for item in after]
    if action == "activate":
        source_revision = event.get("before_source_config_revision")
        if not isinstance(source_revision, int) or isinstance(source_revision, bool) or source_revision < 0:
            raise PromptActivationError("prompt activation history source revision is invalid")
    else:
        rollback_of = event.get("rollback_of_activation_revision")
        if not isinstance(rollback_of, int) or isinstance(rollback_of, bool) or rollback_of < 1:
            raise PromptActivationError("prompt rollback history source revision is invalid")
    return _clean_value(cleaned)  # type: ignore[return-value]


def _validated_history_snapshot(value: object, unit_id: str, label: str) -> tuple[Mapping[str, object], ...]:
    snapshot = _prompt_sequence(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(snapshot) != len(value):
        raise PromptActivationError(f"prompt activation history {label} snapshot must be an array of objects")
    prompt_ids = PROMPT_ACTIVATION_UNITS[unit_id]
    active_ids = tuple(str(item.get("id")) for item in snapshot)
    expected_subset = tuple(prompt_id for prompt_id in prompt_ids if prompt_id in set(active_ids))
    if active_ids != expected_subset or len(set(active_ids)) != len(active_ids):
        raise PromptActivationError(f"prompt activation history {label} membership drifted")
    for prompt in snapshot:
        content = prompt.get("content")
        if not isinstance(content, str) or not content.strip():
            raise PromptActivationError(f"prompt activation history {label} content is empty")
    return snapshot


def _unit(projection: Mapping[str, object], unit_id: str) -> dict[str, object]:
    units = projection.get("units")
    if not isinstance(units, Mapping) or not isinstance(units.get(unit_id), Mapping):
        raise PromptActivationError(f"prompt activation unit missing: {unit_id}")
    return dict(units[unit_id])


def _replace_unit(
    projection: Mapping[str, object],
    unit: Mapping[str, object],
    *,
    history_entry: Mapping[str, object],
) -> dict[str, object]:
    units = dict(projection.get("units") if isinstance(projection.get("units"), Mapping) else {})
    units[str(unit["unit_id"])] = dict(unit)
    history = [dict(item) for item in _mapping_sequence(projection.get("history"))]
    history.append(dict(history_entry))
    return {
        "schema_version": "1.0.0",
        "revision": _int_value(projection.get("revision"), 0) + 1,
        "units": units,
        "history": history[-100:],
    }


def _latest_reversible_event(
    projection: Mapping[str, object],
    unit_id: str,
    current: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    for event in reversed(_mapping_sequence(projection.get("history"))):
        if event.get("unit_id") != unit_id or event.get("action") != "activate":
            continue
        after = _prompt_sequence(event.get("after"))
        if _fingerprint(after) != _fingerprint(current):
            continue
        return event
    raise PromptActivationConflict("no current activation is available for rollback")


def _has_reversible_event(
    projection: Mapping[str, object],
    unit_id: str,
    current: Sequence[Mapping[str, object]],
) -> bool:
    try:
        _latest_reversible_event(projection, unit_id, current)
    except PromptActivationConflict:
        return False
    return True


def _result(
    status: str,
    record: Mapping[str, object],
    projection: Mapping[str, object],
    unit_id: str,
    *,
    replayed: bool,
) -> PromptActivationResult:
    unit = _unit(projection, unit_id)
    return PromptActivationResult(
        status=status,
        config_revision=_int_value(record.get("revision"), 0),
        activation_revision=_int_value(projection.get("revision"), 0),
        unit_id=unit_id,
        unit_revision=_int_value(unit.get("unit_revision"), 0),
        replayed=replayed,
        projection=projection,
    )


def serialize_prompt_activation_result(result: PromptActivationResult) -> dict[str, object]:
    unit = _unit(result.projection, result.unit_id)
    return {
        "status": result.status,
        "config_revision": result.config_revision,
        "activation_revision": result.activation_revision,
        "unit_id": result.unit_id,
        "unit_revision": result.unit_revision,
        "active_prompt_ids": [str(item["id"]) for item in _prompt_sequence(unit.get("active_prompts"))],
        "replayed": result.replayed,
    }


def _preview_token(
    *,
    unit_id: str,
    config_revision: int,
    activation_revision: int,
    drafts: Sequence[Mapping[str, object]],
) -> str:
    payload = {
        "unit_id": unit_id,
        "config_revision": config_revision,
        "activation_revision": activation_revision,
        "draft_fingerprint": _fingerprint(drafts),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _fingerprint(prompts: Sequence[Mapping[str, object]]) -> str:
    raw = json.dumps(list(prompts), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"api_key", "apikey", "authorization", "cookie", "cookies", "cookies_file"}:
                raise PromptActivationError(f"sensitive field is not allowed: {key}")
            _reject_sensitive(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_sensitive(item)
        return
    if isinstance(value, str):
        lowered = value.lower()
        if "authorization:" in lowered or "cookie:" in lowered or ("sk-" in lowered and len(value) >= 16):
            raise PromptActivationError("secret-like prompt content is not allowed")


def _prompt_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    return _mapping_sequence(value)


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _clean_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_clean_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _int_value(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
