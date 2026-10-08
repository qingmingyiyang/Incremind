from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone

from .ports import ObjectStorePort


RECIPE_SCHEMA_VERSION = "1.0.0"
RECIPE_EXECUTORS = {"empty_guard.deterministic"}
RECIPE_SIDE_EFFECT_CLASSES = {"none"}
PROCESSING_RECIPE_RUNTIME_ENABLED = True
_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_REFERENCE = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "cookies", "password",
    "secret", "secret_key", "token", "access_token", "refresh_token", "endpoint", "base_url",
}


class ProcessingRecipeError(ValueError):
    pass


class ProcessingRecipeConflict(ProcessingRecipeError):
    pass


class ProcessingRecipeRegistry:
    """Versioned recipe authority shared by Test Lab and the bounded D5 runtime."""

    _COLLECTION = "processing_recipe_registries"
    _REGISTRY_ID = "default"

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        now: str | None = None,
        authority_validator: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self._store = store
        self._now = now or datetime.now(timezone.utc).isoformat()
        self._authority_validator = authority_validator

    def status(self) -> dict[str, object]:
        record, _store_revision = self._read()
        return _public(record)

    def preview_draft(self, values: Mapping[str, object]) -> dict[str, object]:
        _reject_sensitive(values)
        record, _store_revision = self._read()
        recipe_id = _recipe_id(values.get("id"))
        current = _find(record["drafts"], recipe_id)
        revision = int(current["revision"]) + 1 if current is not None else 1
        recipe = _normalize_recipe(values, revision=revision, status="draft", now=self._now)
        return {
            "status": "validated",
            "registry_revision": record["registry_revision"],
            "recipe": recipe,
            "validation_token": _validation_token(record["registry_revision"], recipe),
            "runtime_effect": "active_empty_guard_preflight",
        }

    def save_draft(
        self,
        values: Mapping[str, object],
        *,
        expected_registry_revision: int,
        validation_token: str,
    ) -> dict[str, object]:
        preview = self.preview_draft(values)
        if preview["registry_revision"] != expected_registry_revision:
            raise ProcessingRecipeConflict("processing recipe registry revision conflict")
        if preview["validation_token"] != validation_token:
            raise ProcessingRecipeConflict("processing recipe draft validation drifted")
        record, store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        recipe = dict(preview["recipe"])
        drafts = [dict(item) for item in record["drafts"] if item["id"] != recipe["id"]]
        drafts.append(recipe)
        drafts.sort(key=lambda item: str(item["id"]))
        next_revision = expected_registry_revision + 1
        next_record = {
            **record,
            "registry_revision": next_revision,
            "drafts": drafts,
            "updated_at": self._now,
            "history": _append_history(record, {
                "registry_revision": next_revision,
                "action": "draft_saved",
                "recipe_id": recipe["id"],
                "recipe_revision": recipe["revision"],
                "recorded_at": self._now,
                "before_active": [],
                "after_active": [],
                "reason": "validated recipe draft saved",
            }),
        }
        self._write(next_record, store_revision)
        return {**_public(next_record), "saved_recipe": deepcopy(recipe)}

    def preview_activation(
        self,
        recipe_id: str,
        *,
        expected_registry_revision: int,
        expected_recipe_revision: int,
    ) -> dict[str, object]:
        record, _store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        draft = _required_find(record["drafts"], _recipe_id(recipe_id), "draft")
        if draft["revision"] != expected_recipe_revision:
            raise ProcessingRecipeConflict("processing recipe draft revision conflict")
        if self._authority_validator is None:
            raise ProcessingRecipeError("processing recipe authority validator is required for activation")
        self._authority_validator(draft)
        token = _activation_token(expected_registry_revision, draft, record["active"])
        return {
            "status": "validated",
            "registry_revision": expected_registry_revision,
            "recipe_id": draft["id"],
            "recipe_revision": draft["revision"],
            "activation_token": token,
            "current_active_revision": _active_revision(record["active"], str(draft["id"])),
            "runtime_effect": "active_empty_guard_preflight",
            "validation_record": deepcopy(draft["validation_record"]),
        }

    def activate(
        self,
        recipe_id: str,
        *,
        expected_registry_revision: int,
        expected_recipe_revision: int,
        activation_token: str,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        clean_reason = _confirmation(confirm, reason, "activation")
        preview = self.preview_activation(
            recipe_id,
            expected_registry_revision=expected_registry_revision,
            expected_recipe_revision=expected_recipe_revision,
        )
        if preview["activation_token"] != activation_token:
            raise ProcessingRecipeConflict("processing recipe activation preview drifted")
        record, store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        draft = _required_find(record["drafts"], _recipe_id(recipe_id), "draft")
        if draft["revision"] != expected_recipe_revision:
            raise ProcessingRecipeConflict("processing recipe draft revision conflict")
        if _activation_token(expected_registry_revision, draft, record["active"]) != activation_token:
            raise ProcessingRecipeConflict("processing recipe activation preview drifted")
        current = _find(record["active"], str(draft["id"]))
        active = {**deepcopy(draft), "status": "active", "activated_at": self._now, "activation_reason": clean_reason}
        if current is not None and _recipe_fingerprint(current, ignore_activation=True) == _recipe_fingerprint(active, ignore_activation=True):
            return {**_public(record), "status": "already_active", "replayed": True}
        active_recipes = [dict(item) for item in record["active"] if item["id"] != active["id"]]
        active_recipes.append(active)
        active_recipes.sort(key=_active_sort_key)
        next_revision = expected_registry_revision + 1
        next_record = {
            **record,
            "registry_revision": next_revision,
            "active": active_recipes,
            "updated_at": self._now,
            "history": _append_history(record, {
                "registry_revision": next_revision,
                "action": "activated",
                "recipe_id": active["id"],
                "recipe_revision": active["revision"],
                "recorded_at": self._now,
                "before_active": [deepcopy(current)] if current is not None else [],
                "after_active": [deepcopy(active)],
                "reason": clean_reason,
            }),
        }
        self._write(next_record, store_revision)
        return {**_public(next_record), "status": "activated", "replayed": False}

    def deactivate(
        self,
        recipe_id: str,
        *,
        expected_registry_revision: int,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        clean_reason = _confirmation(confirm, reason, "deactivation")
        record, store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        key = _recipe_id(recipe_id)
        current = _find(record["active"], key)
        if current is None:
            return {**_public(record), "status": "already_inactive", "replayed": True}
        active = [dict(item) for item in record["active"] if item["id"] != key]
        next_revision = expected_registry_revision + 1
        next_record = {
            **record,
            "registry_revision": next_revision,
            "active": active,
            "updated_at": self._now,
            "history": _append_history(record, {
                "registry_revision": next_revision,
                "action": "deactivated",
                "recipe_id": key,
                "recipe_revision": current["revision"],
                "recorded_at": self._now,
                "before_active": [deepcopy(current)],
                "after_active": [],
                "reason": clean_reason,
            }),
        }
        self._write(next_record, store_revision)
        return {**_public(next_record), "status": "deactivated", "replayed": False}

    def rollback(
        self,
        recipe_id: str,
        *,
        expected_registry_revision: int,
        confirm: bool,
        reason: str,
    ) -> dict[str, object]:
        clean_reason = _confirmation(confirm, reason, "rollback")
        record, store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        key = _recipe_id(recipe_id)
        event = next(
            (
                item for item in reversed(record["history"])
                if item["recipe_id"] == key and item["action"] in {"activated", "deactivated"}
            ),
            None,
        )
        if event is None:
            raise ProcessingRecipeConflict("no processing recipe activation is available for rollback")
        current = _find(record["active"], key)
        current_snapshot = [current] if current is not None else []
        if current_snapshot != event["after_active"]:
            raise ProcessingRecipeConflict("processing recipe active snapshot drifted")
        restored = [dict(item) for item in event["before_active"]]
        active = [dict(item) for item in record["active"] if item["id"] != key]
        active.extend(restored)
        active.sort(key=_active_sort_key)
        next_revision = expected_registry_revision + 1
        next_record = {
            **record,
            "registry_revision": next_revision,
            "active": active,
            "updated_at": self._now,
            "history": _append_history(record, {
                "registry_revision": next_revision,
                "action": "rolled_back",
                "recipe_id": key,
                "recipe_revision": _active_revision(restored, key),
                "recorded_at": self._now,
                "before_active": [deepcopy(current)] if current is not None else [],
                "after_active": deepcopy(restored),
                "reason": clean_reason,
            }),
        }
        self._write(next_record, store_revision)
        return {**_public(next_record), "status": "rolled_back", "replayed": False}

    def resolve(
        self,
        *,
        content_type: str,
        text: str,
        trigger: str = "",
        feature_enabled: bool = False,
    ) -> dict[str, object]:
        record, _store_revision = self._read()
        if not feature_enabled:
            return _resolution(record, "feature_off", None, [])
        if self._authority_validator is None:
            raise ProcessingRecipeError("processing recipe authority validator is required for resolution")
        for recipe in record["active"]:
            self._authority_validator(recipe)
        evaluations = [_match(recipe, content_type=content_type, text=text, trigger=trigger) for recipe in record["active"]]
        matched = [item for item in evaluations if item["matched"]]
        selected = matched[0]["recipe"] if matched else None
        return _resolution(record, "matched" if selected else "no_match", selected, evaluations)

    def legacy_preview(self, skills: Sequence[Mapping[str, object]]) -> dict[str, object]:
        record, _store_revision = self._read()
        candidates = [_legacy_candidate(item, now=self._now) for item in skills]
        return {
            "status": "preview",
            "registry_revision": record["registry_revision"],
            "write_effect": "none",
            "activation_effect": "none",
            "candidates": candidates,
        }

    def evaluate_for_test(
        self,
        recipe_id: str,
        *,
        source: str,
        expected_registry_revision: int,
        expected_recipe_revision: int,
        content_type: str,
        text: str,
        trigger: str = "",
    ) -> dict[str, object]:
        """Evaluate one exact draft/active snapshot without executing its executor or writing state."""
        if source not in {"draft", "active"}:
            raise ProcessingRecipeError("processing recipe test source must be draft or active")
        record, _store_revision = self._read()
        self._expect_revision(record, expected_registry_revision)
        recipes = record["drafts"] if source == "draft" else record["active"]
        recipe = _required_find(recipes, _recipe_id(recipe_id), source)
        if recipe["revision"] != expected_recipe_revision:
            raise ProcessingRecipeConflict("processing recipe test revision conflict")
        if self._authority_validator is None:
            raise ProcessingRecipeError("processing recipe authority validator is required for testing")
        self._authority_validator(recipe)
        evaluation = _match(recipe, content_type=content_type, text=text, trigger=trigger)
        return {
            "status": "matched" if evaluation["matched"] else "no_match",
            "registry_revision": record["registry_revision"],
            "recipe_revision": recipe["revision"],
            "source": source,
            "recipe": evaluation["recipe"],
            "evidence": evaluation["evidence"],
            "executor_executed": False,
            "side_effects": "none",
            "write_effect": "none",
        }

    @staticmethod
    def validate_test_output(recipe: Mapping[str, object], output: object) -> dict[str, object]:
        schema = recipe.get("output_schema")
        if not isinstance(schema, Mapping) or not isinstance(output, Mapping):
            return {"valid": False, "errors": ["output must be an object"]}
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping) or not isinstance(required, Sequence):
            return {"valid": False, "errors": ["recipe output schema is invalid"]}
        errors: list[str] = []
        for key in required:
            if key not in output:
                errors.append(f"missing required output field: {key}")
        expected_types = {"boolean": bool, "number": (int, float), "string": str}
        for key, value in output.items():
            spec = properties.get(key)
            if spec is None:
                errors.append(f"unexpected output field: {key}")
                continue
            expected = expected_types.get(spec.get("type")) if isinstance(spec, Mapping) else None
            if expected is None or not isinstance(value, expected) or (spec.get("type") == "number" and isinstance(value, bool)):
                errors.append(f"invalid output field type: {key}")
        return {"valid": not errors, "errors": errors}

    def _read(self) -> tuple[dict[str, object], int]:
        store_revision = self._store.revision(self._COLLECTION, self._REGISTRY_ID)
        raw = self._store.read(self._COLLECTION, self._REGISTRY_ID)
        if self._store.revision(self._COLLECTION, self._REGISTRY_ID) != store_revision:
            raise ProcessingRecipeConflict("processing recipe storage revision conflict")
        if raw is None:
            return _empty_registry(), store_revision
        return _validate_registry(raw), store_revision

    def _write(self, record: Mapping[str, object], store_revision: int) -> None:
        clean = _validate_registry(record)
        try:
            self._store.write(self._COLLECTION, self._REGISTRY_ID, clean, expected_revision=store_revision)
        except ValueError as error:
            if "expected revision" in str(error):
                raise ProcessingRecipeConflict("processing recipe storage revision conflict") from error
            raise

    @staticmethod
    def _expect_revision(record: Mapping[str, object], expected: int) -> None:
        if record["registry_revision"] != expected:
            raise ProcessingRecipeConflict(
                f"processing recipe registry revision conflict: expected {expected}, current {record['registry_revision']}"
            )


class ProcessingRecipeRuntime:
    """Execute the single side-effect-free Recipe preflight without product writes."""

    def __init__(
        self,
        registry: ProcessingRecipeRegistry,
        *,
        executors: Mapping[str, Callable[[str], Mapping[str, object]]] | None = None,
    ) -> None:
        self._registry = registry
        self._executors = dict(executors or {"empty_guard.deterministic": _execute_empty_guard})

    def preflight(self, *, content_type: str, text: str, trigger: str) -> dict[str, object]:
        resolution = self._registry.resolve(
            content_type=content_type,
            text=text,
            trigger=trigger,
            feature_enabled=PROCESSING_RECIPE_RUNTIME_ENABLED,
        )
        trace: dict[str, object] = {
            "status": resolution["status"],
            "registry_revision": resolution["registry_revision"],
            "production_feature_enabled": True,
            "consumer": trigger,
            "action": "continue_default",
            "selected": None,
            "evaluations": [
                {
                    "recipe_id": item["recipe"]["id"],
                    "recipe_revision": item["recipe"]["revision"],
                    "matched": item["matched"],
                    "evidence": deepcopy(item["evidence"]),
                }
                for item in resolution["evaluations"]
            ],
            "executor_executed": False,
            "side_effects": "none",
            "output_schema": None,
        }
        recipe = resolution["selected"]
        if recipe is None:
            return trace
        selected = {
            "recipe_id": recipe["id"],
            "recipe_revision": recipe["revision"],
            "executor_id": recipe["executor_id"],
            "side_effect_class": recipe["side_effect_class"],
            "prompt_id": recipe["prompt_ref"]["prompt_id"],
            "prompt_unit_revision": recipe["prompt_ref"]["unit_revision"],
            "model_route_key": recipe["model_route_key"],
            "model_route_revision": recipe["model_route_revision"],
        }
        trace["selected"] = selected
        if recipe["side_effect_class"] != "none":
            raise ProcessingRecipeError("processing recipe runtime forbids side effects")
        executor = self._executors.get(str(recipe["executor_id"]))
        if executor is None:
            raise ProcessingRecipeError("processing recipe runtime executor is unavailable")
        try:
            output = executor(text)
        except Exception:  # noqa: BLE001 - bounded deterministic fallback hides implementation details
            trace.update({"status": "executor_failed", "fallback": "continue_default"})
            return trace
        schema = self._registry.validate_test_output(recipe, output)
        trace["output_schema"] = schema
        if not schema["valid"]:
            raise ProcessingRecipeError("processing recipe runtime output schema validation failed")
        empty = output.get("empty")
        trace.update({
            "status": "executed",
            "action": "reject_empty" if empty is True else "continue_default",
            "executor_executed": True,
        })
        return trace


def _empty_registry() -> dict[str, object]:
    return {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "registry_revision": 0,
        "production_feature_enabled": False,
        "drafts": [],
        "active": [],
        "history": [],
        "updated_at": "",
    }


def _public(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": record["schema_version"],
        "registry_revision": record["registry_revision"],
        "production_feature_enabled": PROCESSING_RECIPE_RUNTIME_ENABLED,
        "drafts": deepcopy(record["drafts"]),
        "active": deepcopy(record["active"]),
        "history": deepcopy(record["history"]),
        "updated_at": record["updated_at"],
    }


def _normalize_recipe(values: Mapping[str, object], *, revision: int, status: str, now: str) -> dict[str, object]:
    allowed = {
        "id", "name", "description", "content_matcher", "trigger_matcher", "prompt_ref",
        "model_route_key", "model_route_revision", "output_schema", "executor_id", "side_effect_class", "priority", "fallback",
    }
    if set(values) - allowed:
        raise ProcessingRecipeError("processing recipe contains unknown fields")
    recipe_id = _recipe_id(values.get("id"))
    name = _required_text(values.get("name"), "name", maximum=120)
    description = _required_text(values.get("description"), "description", maximum=500)
    executor = _reference(values.get("executor_id"), "executor_id")
    if executor not in RECIPE_EXECUTORS:
        raise ProcessingRecipeError("unsupported processing recipe executor")
    side_effect = values.get("side_effect_class")
    if side_effect not in RECIPE_SIDE_EFFECT_CLASSES:
        raise ProcessingRecipeError("unsupported processing recipe side effect class")
    route_key = _reference(values.get("model_route_key"), "model_route_key")
    route_revision = values.get("model_route_revision")
    if not isinstance(route_revision, int) or isinstance(route_revision, bool) or route_revision < 1:
        raise ProcessingRecipeError("invalid processing recipe model route revision")
    priority = values.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1000:
        raise ProcessingRecipeError("processing recipe priority must be an integer from 0 to 1000")
    content_matcher = _content_matcher(values.get("content_matcher"))
    trigger_matcher = _trigger_matcher(values.get("trigger_matcher"))
    prompt_ref = _prompt_ref(values.get("prompt_ref"))
    output_schema = _output_schema(values.get("output_schema"))
    fallback = _fallback(values.get("fallback"))
    canonical = {
        "id": recipe_id,
        "revision": revision,
        "status": status,
        "name": name,
        "description": description,
        "content_matcher": content_matcher,
        "trigger_matcher": trigger_matcher,
        "prompt_ref": prompt_ref,
        "model_route_key": route_key,
        "model_route_revision": route_revision,
        "output_schema": output_schema,
        "executor_id": executor,
        "side_effect_class": side_effect,
        "priority": priority,
        "fallback": fallback,
        "validation_record": {
            "status": "validated",
            "contract_version": RECIPE_SCHEMA_VERSION,
            "validated_at": now,
            "checks": ["schema", "executor_allowlist", "side_effect_none", "secret_free", "deterministic_matcher"],
        },
        "updated_at": now,
    }
    _reject_sensitive(canonical)
    return canonical


def _content_matcher(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"content_types", "min_length", "max_length"}:
        raise ProcessingRecipeError("invalid processing recipe content matcher")
    content_types = value.get("content_types")
    if not isinstance(content_types, Sequence) or isinstance(content_types, (str, bytes)):
        raise ProcessingRecipeError("content matcher types must be an array")
    types = sorted(set(_required_text(item, "content type", maximum=40).lower() for item in content_types))
    if not types or any(not re.fullmatch(r"\*|[a-z][a-z0-9_-]{0,39}", item) for item in types):
        raise ProcessingRecipeError("invalid processing recipe content type")
    minimum, maximum = value.get("min_length"), value.get("max_length")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
        raise ProcessingRecipeError("invalid content matcher min_length")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < minimum or maximum > 1_000_000:
        raise ProcessingRecipeError("invalid content matcher max_length")
    return {"content_types": types, "min_length": minimum, "max_length": maximum}


def _trigger_matcher(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"mode", "values"} or value.get("mode") not in {"any", "contains", "prefix"}:
        raise ProcessingRecipeError("invalid processing recipe trigger matcher")
    raw = value.get("values")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ProcessingRecipeError("trigger matcher values must be an array")
    values = sorted(set(_required_text(item, "trigger", maximum=100).casefold() for item in raw))
    if value["mode"] != "any" and not values:
        raise ProcessingRecipeError("trigger matcher values are required")
    if value["mode"] == "any" and values:
        raise ProcessingRecipeError("any trigger matcher cannot contain values")
    return {"mode": value["mode"], "values": values}


def _prompt_ref(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"prompt_id", "source", "unit_id", "unit_revision"}:
        raise ProcessingRecipeError("invalid processing recipe prompt reference")
    if value.get("source") != "active":
        raise ProcessingRecipeError("processing recipe prompt reference must use active source")
    unit_revision = value.get("unit_revision")
    if not isinstance(unit_revision, int) or isinstance(unit_revision, bool) or unit_revision < 0:
        raise ProcessingRecipeError("invalid processing recipe prompt unit revision")
    return {
        "prompt_id": _reference(value.get("prompt_id"), "prompt_id"),
        "source": "active",
        "unit_id": _reference(value.get("unit_id"), "unit_id"),
        "unit_revision": unit_revision,
    }


def _output_schema(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("type") != "object" or set(value) != {"type", "required", "properties"}:
        raise ProcessingRecipeError("processing recipe output schema must be a bounded object schema")
    required, properties = value.get("required"), value.get("properties")
    if not isinstance(required, Sequence) or isinstance(required, (str, bytes)) or not isinstance(properties, Mapping):
        raise ProcessingRecipeError("invalid processing recipe output schema")
    if any(not isinstance(item, str) for item in required):
        raise ProcessingRecipeError("invalid processing recipe output schema required fields")
    if len(properties) > 32 or any(item not in properties for item in required):
        raise ProcessingRecipeError("invalid processing recipe output schema properties")
    clean_properties: dict[str, object] = {}
    for key, spec in properties.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise ProcessingRecipeError("invalid processing recipe output property")
        if not isinstance(spec, Mapping) or set(spec) != {"type"} or spec.get("type") not in {"boolean", "number", "string"}:
            raise ProcessingRecipeError("unsupported processing recipe output property type")
        clean_properties[key] = {"type": spec["type"]}
    return {"type": "object", "required": sorted(set(str(item) for item in required)), "properties": clean_properties}


def _fallback(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"mode", "reason"} or value.get("mode") != "continue_default":
        raise ProcessingRecipeError("processing recipe fallback must continue the default workflow")
    return {"mode": "continue_default", "reason": _required_text(value.get("reason"), "fallback reason", maximum=300)}


def _match(recipe: Mapping[str, object], *, content_type: str, text: str, trigger: str) -> dict[str, object]:
    matcher = recipe["content_matcher"]
    types = matcher["content_types"]
    type_match = "*" in types or content_type.casefold() in types
    length_match = matcher["min_length"] <= len(text) <= matcher["max_length"]
    trigger_matcher = recipe["trigger_matcher"]
    mode, values = trigger_matcher["mode"], trigger_matcher["values"]
    normalized_trigger, normalized_text = trigger.casefold(), text.casefold()
    if mode == "any":
        trigger_match = True
    elif mode == "prefix":
        trigger_match = any(normalized_trigger.startswith(item) or normalized_text.startswith(item) for item in values)
    else:
        trigger_match = any(item in normalized_trigger or item in normalized_text for item in values)
    return {
        "recipe": deepcopy(recipe),
        "matched": type_match and length_match and trigger_match,
        "evidence": {"content_type": type_match, "length": length_match, "trigger": trigger_match},
    }


def _resolution(record: Mapping[str, object], status: str, selected: object, evaluations: list[dict[str, object]]) -> dict[str, object]:
    return {
        "status": status,
        "registry_revision": record["registry_revision"],
        "production_feature_enabled": PROCESSING_RECIPE_RUNTIME_ENABLED,
        "selected": deepcopy(selected),
        "evaluations": evaluations,
        "fallback": "continue_default" if selected is None else None,
    }


def _execute_empty_guard(text: str) -> Mapping[str, object]:
    return {"empty": not bool(text.strip())}


def _legacy_candidate(skill: Mapping[str, object], *, now: str) -> dict[str, object]:
    skill_id = str(skill.get("id") or "")
    valid_executor = skill_id == "sk-empty-guard"
    candidate = {
        "legacy_id": skill_id,
        "legacy_enabled_is_production_active": False,
        "status": "mappable_draft" if valid_executor else "unbound_legacy_draft",
        "executor_id": "empty_guard.deterministic" if valid_executor else None,
        "side_effect_class": "none" if valid_executor else None,
        "prompt_id": str(skill.get("promptTemplateId") or ""),
        "model_profile_id": str(skill.get("modelProfileId") or ""),
        "validated_at": now,
        "validation_errors": [] if valid_executor else ["no allowlisted executor is mapped"],
    }
    _reject_sensitive(candidate)
    return candidate


def _validate_registry(value: Mapping[str, object]) -> dict[str, object]:
    _reject_sensitive(value)
    expected = {"schema_version", "registry_revision", "production_feature_enabled", "drafts", "active", "history", "updated_at"}
    if set(value) != expected or value.get("schema_version") != RECIPE_SCHEMA_VERSION or value.get("production_feature_enabled") is not False:
        raise ProcessingRecipeError("invalid processing recipe registry schema")
    revision = value.get("registry_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ProcessingRecipeError("invalid processing recipe registry revision")
    drafts = _recipe_list(value.get("drafts"), "draft")
    active = _recipe_list(value.get("active"), "active")
    history = value.get("history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)) or len(history) > 100:
        raise ProcessingRecipeError("invalid or unbounded processing recipe history")
    clean_history = [_history_event(item) for item in history]
    history_revisions = [item["registry_revision"] for item in clean_history]
    if history_revisions != sorted(set(history_revisions)) or any(item > revision for item in history_revisions):
        raise ProcessingRecipeError("processing recipe history revision drifted")
    if clean_history and history_revisions[-1] != revision:
        raise ProcessingRecipeError("processing recipe history does not reach current revision")
    if not clean_history and revision != 0:
        raise ProcessingRecipeError("processing recipe history is missing")
    active_by_id = {str(item["id"]): item for item in active}
    latest_change: dict[str, Mapping[str, object]] = {}
    for event in clean_history:
        if event["action"] in {"activated", "deactivated", "rolled_back"}:
            latest_change[str(event["recipe_id"])] = event
    for recipe_id, event in latest_change.items():
        current = active_by_id.get(recipe_id)
        current_snapshot = [current] if current is not None else []
        if current_snapshot != event["after_active"]:
            raise ProcessingRecipeError("processing recipe active authority drifted from history")
    return {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "registry_revision": revision,
        "production_feature_enabled": False,
        "drafts": drafts,
        "active": sorted(active, key=_active_sort_key),
        "history": clean_history,
        "updated_at": str(value.get("updated_at") or ""),
    }


def _recipe_list(value: object, status: str) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProcessingRecipeError("processing recipe snapshots must be arrays")
    recipes: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("status") != status:
            raise ProcessingRecipeError("invalid processing recipe snapshot")
        recipes.append(deepcopy(dict(item)))
    ids = [item.get("id") for item in recipes]
    if len(ids) != len(set(ids)):
        raise ProcessingRecipeError("duplicate processing recipe id")
    for item in recipes:
        _validate_persisted_recipe(item)
    return recipes


def _validate_persisted_recipe(recipe: Mapping[str, object]) -> None:
    expected = {
        "id", "revision", "status", "name", "description", "content_matcher", "trigger_matcher",
        "prompt_ref", "model_route_key", "model_route_revision", "output_schema", "executor_id", "side_effect_class", "priority",
        "fallback", "validation_record", "updated_at",
    }
    if recipe.get("status") == "active":
        expected |= {"activated_at", "activation_reason"}
    if set(recipe) != expected:
        raise ProcessingRecipeError("processing recipe persisted fields drifted")
    revision = recipe.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ProcessingRecipeError("invalid persisted processing recipe revision")
    if not isinstance(recipe.get("updated_at"), str) or not str(recipe["updated_at"]).strip():
        raise ProcessingRecipeError("invalid persisted processing recipe timestamp")
    if recipe.get("status") == "active":
        if not isinstance(recipe.get("activated_at"), str) or not str(recipe["activated_at"]).strip():
            raise ProcessingRecipeError("invalid processing recipe activation timestamp")
        if not isinstance(recipe.get("activation_reason"), str) or not str(recipe["activation_reason"]).strip():
            raise ProcessingRecipeError("invalid processing recipe activation reason")
    values = {key: recipe[key] for key in {
        "id", "name", "description", "content_matcher", "trigger_matcher", "prompt_ref", "model_route_key", "model_route_revision",
        "output_schema", "executor_id", "side_effect_class", "priority", "fallback",
    }}
    normalized = _normalize_recipe(values, revision=revision, status=str(recipe["status"]), now=str(recipe["updated_at"]))
    if normalized["validation_record"] != recipe["validation_record"]:
        raise ProcessingRecipeError("processing recipe validation record drifted")


def _history_event(value: object) -> dict[str, object]:
    fields = {"registry_revision", "action", "recipe_id", "recipe_revision", "recorded_at", "before_active", "after_active", "reason"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProcessingRecipeError("invalid processing recipe history event")
    if value.get("action") not in {"draft_saved", "activated", "deactivated", "rolled_back"}:
        raise ProcessingRecipeError("invalid processing recipe history action")
    if not isinstance(value.get("registry_revision"), int) or value["registry_revision"] < 1:
        raise ProcessingRecipeError("invalid processing recipe history revision")
    _recipe_id(value.get("recipe_id"))
    if not isinstance(value.get("recipe_revision"), int) or value["recipe_revision"] < 0:
        raise ProcessingRecipeError("invalid processing recipe history recipe revision")
    if not isinstance(value.get("recorded_at"), str) or not str(value["recorded_at"]).strip():
        raise ProcessingRecipeError("invalid processing recipe history timestamp")
    if not isinstance(value.get("reason"), str) or not str(value["reason"]).strip():
        raise ProcessingRecipeError("invalid processing recipe history reason")
    before = _recipe_list(value.get("before_active"), "active")
    after = _recipe_list(value.get("after_active"), "active")
    if len(before) > 1 or len(after) > 1:
        raise ProcessingRecipeError("processing recipe history snapshot is unbounded")
    if any(item["id"] != value["recipe_id"] for item in [*before, *after]):
        raise ProcessingRecipeError("processing recipe history snapshot identity drifted")
    if value["action"] == "draft_saved" and (before or after):
        raise ProcessingRecipeError("processing recipe draft history cannot change active snapshots")
    return {**deepcopy(dict(value)), "before_active": before, "after_active": after}


def _append_history(record: Mapping[str, object], event: Mapping[str, object]) -> list[dict[str, object]]:
    return [*[deepcopy(dict(item)) for item in record["history"]], deepcopy(dict(event))][-100:]


def _validation_token(registry_revision: int, recipe: Mapping[str, object]) -> str:
    canonical = deepcopy(dict(recipe))
    canonical.pop("updated_at", None)
    validation = canonical.get("validation_record")
    if isinstance(validation, Mapping):
        canonical["validation_record"] = {
            key: deepcopy(value) for key, value in validation.items() if key != "validated_at"
        }
    return _hash({"registry_revision": registry_revision, "recipe": canonical})


def _activation_token(registry_revision: int, draft: Mapping[str, object], active: object) -> str:
    return _hash({"registry_revision": registry_revision, "draft": draft, "active": active})


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _recipe_fingerprint(recipe: Mapping[str, object], *, ignore_activation: bool = False) -> str:
    value = dict(recipe)
    if ignore_activation:
        value.pop("activated_at", None)
        value.pop("activation_reason", None)
        value["status"] = "draft"
    return _hash(value)


def _active_sort_key(recipe: Mapping[str, object]) -> tuple[int, str]:
    return (-int(recipe["priority"]), str(recipe["id"]))


def _find(recipes: object, recipe_id: str) -> dict[str, object] | None:
    if not isinstance(recipes, Sequence) or isinstance(recipes, (str, bytes)):
        return None
    item = next((item for item in recipes if isinstance(item, Mapping) and item.get("id") == recipe_id), None)
    return deepcopy(dict(item)) if item is not None else None


def _required_find(recipes: object, recipe_id: str, label: str) -> dict[str, object]:
    found = _find(recipes, recipe_id)
    if found is None:
        raise ProcessingRecipeError(f"processing recipe {label} not found: {recipe_id}")
    return found


def _active_revision(recipes: object, recipe_id: str) -> int:
    found = _find(recipes, recipe_id)
    return int(found["revision"]) if found is not None else 0


def _recipe_id(value: object) -> str:
    text = str(value or "").strip().lower()
    if not _ID.fullmatch(text):
        raise ProcessingRecipeError("invalid processing recipe id")
    return text


def _reference(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _REFERENCE.fullmatch(text):
        raise ProcessingRecipeError(f"invalid processing recipe {field}")
    return text


def _required_text(value: object, field: str, *, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > maximum:
        raise ProcessingRecipeError(f"invalid processing recipe {field}")
    return text


def _confirmation(confirm: bool, reason: str, action: str) -> str:
    if confirm is not True:
        raise ProcessingRecipeError(f"processing recipe {action} requires explicit confirmation")
    clean = reason.strip()
    if not clean:
        raise ProcessingRecipeError(f"processing recipe {action} reason is required")
    _reject_sensitive({"reason": clean})
    return clean


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_secret"):
                raise ProcessingRecipeError(f"sensitive material is forbidden: {key}")
            _reject_sensitive(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_sensitive(item)
    elif isinstance(value, str):
        lowered = value.lower()
        if "authorization:" in lowered or "cookie:" in lowered or ("sk-" in lowered and len(value) >= 16):
            raise ProcessingRecipeError("secret-like processing recipe value is forbidden")
