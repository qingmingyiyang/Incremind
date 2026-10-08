from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from threading import Lock
import tempfile
from typing import Mapping, Sequence

from .model_dispatch_authority import model_dispatch_authority_fence

from .model_route_migration import ProviderContext
from .model_route_registry import ModelRouteRegistry, ModelRouteRegistryError, ModelRouteRegistryNotFound


SUPPORTED_ROUTES = {
    "tier.fast",
    "tier.standard",
    "tier.deep",
    "tier.vision",
    "tier.image_generation",
    "intake.classification",
    "memory.project_routing",
    "search.answer",
    "companion.chat",
    "companion.event",
    "companion.ambient",
    "companion.diary",
    "companion.vision",
    "companion.voice",
}
RUNTIME_FIELDS = {
    "schema_version", "runtime_revision", "mode", "activated_registry_revision", "route_keys",
    "assignments", "activation_fingerprint", "activated_at", "updated_at", "history", "resolutions",
}
ASSIGNMENT_FIELDS = {"route_key", "provider_id", "provider_revision", "model_name", "route_revision"}
RESOLUTION_FIELDS = {
    "route_key", "source", "provider_id", "provider_revision", "model_name", "registry_revision",
    "route_revision", "runtime_revision", "resolved_at",
}
HISTORY_FIELDS = {"runtime_revision", "action", "recorded_at"}
_LOCKS: dict[Path, Lock] = {}
_LOCK_GUARD = Lock()


class ModelRouteRuntimeError(RuntimeError):
    pass


class ModelRouteRuntimeConflict(ModelRouteRuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lock(path: Path) -> Lock:
    with _LOCK_GUARD:
        return _LOCKS.setdefault(path.resolve(), Lock())


def _emergency_enabled() -> bool:
    return os.environ.get("CHRIPTMAS_MODEL_ROUTE_RUNTIME", "1").strip().lower() not in {
        "0", "false", "off", "disabled",
    }


class ModelRouteRuntimeService:
    schema_version = "1.0.0"

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir).resolve()
        self._registry = ModelRouteRegistry(root_dir)
        self._path = root_dir / "library" / "global" / "model-routes" / "runtime.json"
        self._lock = _lock(self._path)

    def status(self) -> dict[str, object]:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            state = self._read()
        return {**deepcopy(state), "runtime_activation": state["mode"] == "active" and _emergency_enabled(), "emergency_enabled": _emergency_enabled()}

    def preview(self, *, route_keys: Sequence[str], compatibility: Mapping[str, ProviderContext], providers: Sequence[ProviderContext]) -> dict[str, object]:
        keys = self._keys(route_keys)
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            state = self._read()
        registry_state = self._registry.list()
        provider_by_id = {str(item.record.get("provider_id") or ""): item for item in providers}
        assignments: list[dict[str, object]] = []
        comparisons: list[dict[str, object]] = []
        for key in keys:
            try:
                route = self._registry.get(key)["route"]
            except ModelRouteRegistryNotFound as error:
                raise ModelRouteRuntimeError(f"activated model route is missing: {key}") from error
            context = provider_by_id.get(str(route["provider_id"]))
            if context is None:
                raise ModelRouteRuntimeError(f"activated model route provider is missing: {key}")
            self._validate(route, context)
            assignment = self._assignment(route)
            fallback = compatibility.get(key)
            assignments.append(assignment)
            comparisons.append({
                "route_key": key,
                "selected": deepcopy(assignment),
                "compatibility": self._compatibility_snapshot(fallback),
                "same_provider_and_model": bool(fallback and assignment["provider_id"] == str(fallback.record.get("provider_id") or "") and assignment["model_name"] == str(fallback.record.get("model") or "")),
            })
        canonical = {
            "runtime_revision": state["runtime_revision"], "registry_revision": registry_state["registry_revision"],
            "route_keys": keys, "assignments": assignments, "comparisons": comparisons,
            "emergency_enabled": _emergency_enabled(),
        }
        token = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {
            "status": "shadow", "shadow_token": token, "runtime_revision": state["runtime_revision"],
            "registry_revision": registry_state["registry_revision"],
            "runtime_activation": state["mode"] == "active" and _emergency_enabled(),
            "emergency_enabled": _emergency_enabled(), "route_keys": keys,
            "assignments": assignments, "comparisons": comparisons,
        }

    def activate(self, *, shadow_token: str, route_keys: Sequence[str], expected_runtime_revision: int, confirm: bool, compatibility: Mapping[str, ProviderContext], providers: Sequence[ProviderContext]) -> dict[str, object]:
        if not confirm:
            raise ModelRouteRuntimeError("explicit runtime activation confirmation is required")
        if not _emergency_enabled():
            raise ModelRouteRuntimeError("model route runtime emergency flag is disabled")
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            current = self._read()
        requested_keys = self._keys(route_keys)
        if current["mode"] == "active":
            if current["activation_fingerprint"] != shadow_token or current["route_keys"] != requested_keys:
                raise ModelRouteRuntimeConflict("deactivate model route runtime before changing active routes")
            for key in requested_keys:
                self.resolve(key, compatibility=compatibility[key], providers=providers)
            return {**deepcopy(current), "runtime_activation": True, "replayed": True}
        preview = self.preview(route_keys=route_keys, compatibility=compatibility, providers=providers)
        if preview["shadow_token"] != shadow_token:
            raise ModelRouteRuntimeConflict("model route shadow preview drifted")
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            state = self._read()
            self._expect_revision(state, expected_runtime_revision)
            revision, now = int(state["runtime_revision"]) + 1, _now()
            next_state = {
                **state, "runtime_revision": revision, "mode": "active",
                "activated_registry_revision": preview["registry_revision"], "route_keys": list(preview["route_keys"]),
                "assignments": deepcopy(preview["assignments"]), "activation_fingerprint": shadow_token,
                "activated_at": now, "updated_at": now,
                "history": [*state["history"], {"runtime_revision": revision, "action": "activated", "recorded_at": now}],
            }
            self._write(next_state)
        return {**deepcopy(next_state), "runtime_activation": True, "replayed": False}

    def deactivate(self, *, expected_runtime_revision: int, confirm: bool) -> dict[str, object]:
        if not confirm:
            raise ModelRouteRuntimeError("explicit runtime deactivation confirmation is required")
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            state = self._read()
            self._expect_revision(state, expected_runtime_revision)
            if state["mode"] == "off":
                return {**deepcopy(state), "runtime_activation": False, "replayed": True}
            revision, now = int(state["runtime_revision"]) + 1, _now()
            next_state = {
                **state, "runtime_revision": revision, "mode": "off", "activated_registry_revision": None,
                "route_keys": [], "assignments": [], "activation_fingerprint": "", "updated_at": now,
                "history": [*state["history"], {"runtime_revision": revision, "action": "deactivated", "recorded_at": now}],
            }
            self._write(next_state)
        return {**deepcopy(next_state), "runtime_activation": False, "replayed": False}

    def resolve(self, route_key: str, *, compatibility: ProviderContext, providers: Sequence[ProviderContext]) -> dict[str, object]:
        key = self._keys([route_key])[0]
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            state = self._read()
        if not _emergency_enabled() or state["mode"] == "off" or key not in state["route_keys"]:
            selected = self._compatibility_resolution(key, compatibility, state)
        else:
            registry_state = self._registry.list()
            if registry_state["registry_revision"] != state["activated_registry_revision"]:
                raise ModelRouteRuntimeConflict("active model route registry revision drifted")
            assignment = next((item for item in state["assignments"] if item["route_key"] == key), None)
            if assignment is None:
                raise ModelRouteRuntimeError("active model route assignment is missing")
            route = self._registry.get(key)["route"]
            if self._assignment(route) != assignment:
                raise ModelRouteRuntimeConflict("active model route assignment drifted")
            context = {str(item.record.get("provider_id") or ""): item for item in providers}.get(str(assignment["provider_id"]))
            if context is None:
                raise ModelRouteRuntimeError("active model route provider is missing")
            self._validate(route, context)
            selected = {
                **assignment, "source": "registry", "registry_revision": registry_state["registry_revision"],
                "runtime_revision": state["runtime_revision"], "provider": deepcopy(dict(context.record)),
            }
        self._record(selected, expected_runtime_revision=int(state["runtime_revision"]))
        return selected

    def _record(self, selected: Mapping[str, object], *, expected_runtime_revision: int) -> None:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            state = self._read()
            self._expect_revision(state, expected_runtime_revision)
            audit = {key: selected[key] for key in RESOLUTION_FIELDS - {"resolved_at"}}
            audit["resolved_at"] = _now()
            state["resolutions"] = [*state["resolutions"], audit][-100:]
            state["updated_at"] = audit["resolved_at"]
            self._write(state)

    def _compatibility_resolution(self, key: str, context: ProviderContext, state: Mapping[str, object]) -> dict[str, object]:
        record = context.record
        if record.get("enabled") is not True or not record.get("model"):
            raise ModelRouteRuntimeError("compatibility provider is unavailable")
        if not context.egress_consented:
            raise ModelRouteRuntimeError("compatibility provider egress consent is missing")
        return {
            "route_key": key, "source": "emergency_compatibility" if not _emergency_enabled() else "compatibility",
            "provider_id": str(record.get("provider_id") or ""), "provider_revision": str(record.get("updated_at") or ""),
            "model_name": str(record.get("model") or ""), "registry_revision": self._registry.list()["registry_revision"],
            "route_revision": 0, "runtime_revision": state["runtime_revision"], "provider": deepcopy(dict(record)),
        }

    def _validate(self, route: Mapping[str, object], context: ProviderContext) -> None:
        try:
            self._registry.validate_provider_reference(route, provider=context.record, egress_consented=context.egress_consented)
        except ModelRouteRegistryError as error:
            raise ModelRouteRuntimeError(str(error)) from error

    @staticmethod
    def _assignment(route: Mapping[str, object]) -> dict[str, object]:
        return {"route_key": str(route["route_key"]), "provider_id": str(route["provider_id"]), "provider_revision": str(route["provider_revision"]), "model_name": str(route["model_name"]), "route_revision": int(route["revision"])}

    @staticmethod
    def _compatibility_snapshot(context: ProviderContext | None) -> dict[str, object]:
        if context is None:
            return {"available": False, "provider_id": "", "provider_revision": "", "model_name": "", "egress_consented": False}
        record = context.record
        return {"available": record.get("enabled") is True and bool(record.get("model")), "provider_id": str(record.get("provider_id") or ""), "provider_revision": str(record.get("updated_at") or ""), "model_name": str(record.get("model") or ""), "egress_consented": context.egress_consented}

    @staticmethod
    def _keys(route_keys: Sequence[str]) -> list[str]:
        keys = list(dict.fromkeys(str(item).strip() for item in route_keys if str(item).strip()))
        if not keys:
            raise ModelRouteRuntimeError("at least one model route is required")
        if any(key not in SUPPORTED_ROUTES for key in keys):
            raise ModelRouteRuntimeError("unsupported runtime model route")
        return keys

    @staticmethod
    def _expect_revision(state: Mapping[str, object], expected: int) -> None:
        if int(state["runtime_revision"]) != int(expected):
            raise ModelRouteRuntimeConflict(f"model route runtime revision conflict: expected {expected}, current {state['runtime_revision']}")

    def _read(self) -> dict[str, object]:
        if not self._path.exists():
            return {"schema_version": self.schema_version, "runtime_revision": 0, "mode": "off", "activated_registry_revision": None, "route_keys": [], "assignments": [], "activation_fingerprint": "", "activated_at": "", "updated_at": "", "history": [], "resolutions": []}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelRouteRuntimeError("model route runtime state is unreadable") from error
        if not isinstance(payload, dict) or set(payload) != RUNTIME_FIELDS or payload.get("schema_version") != self.schema_version or payload.get("mode") not in {"off", "active"}:
            raise ModelRouteRuntimeError("invalid model route runtime state")
        if not isinstance(payload.get("runtime_revision"), int) or not isinstance(payload.get("route_keys"), list) or not isinstance(payload.get("assignments"), list):
            raise ModelRouteRuntimeError("invalid model route runtime revision or assignments")
        if any(not isinstance(item, dict) or set(item) != ASSIGNMENT_FIELDS for item in payload["assignments"]):
            raise ModelRouteRuntimeError("invalid model route runtime assignment")
        if not isinstance(payload.get("history"), list) or not isinstance(payload.get("resolutions"), list) or len(payload["resolutions"]) > 100:
            raise ModelRouteRuntimeError("invalid or unbounded model route runtime audit")
        if any(not isinstance(item, dict) or set(item) != HISTORY_FIELDS or item.get("action") not in {"activated", "deactivated"} for item in payload["history"]):
            raise ModelRouteRuntimeError("invalid model route runtime history")
        if any(not isinstance(item, dict) or set(item) != RESOLUTION_FIELDS for item in payload["resolutions"]):
            raise ModelRouteRuntimeError("invalid model route resolution evidence")
        if payload["mode"] == "active":
            assignment_keys = [str(item.get("route_key") or "") for item in payload["assignments"]]
            if payload["route_keys"] != assignment_keys or not isinstance(payload["activated_registry_revision"], int) or not re.fullmatch(r"[a-f0-9]{64}", str(payload["activation_fingerprint"])):
                raise ModelRouteRuntimeError("invalid active model route runtime state")
        elif payload["route_keys"] or payload["assignments"] or payload["activated_registry_revision"] is not None or payload["activation_fingerprint"]:
            raise ModelRouteRuntimeError("invalid inactive model route runtime state")
        if re.search(r'"(?:api[_-]?key|secret|token|authorization|cookie|base_url|endpoint)"\s*:', json.dumps(payload).lower()):
            raise ModelRouteRuntimeError("sensitive material is forbidden in model route runtime")
        return payload

    def _write(self, payload: Mapping[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
