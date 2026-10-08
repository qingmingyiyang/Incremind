from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from threading import Lock
import tempfile
from typing import Mapping

from .model_dispatch_authority import model_dispatch_authority_fence


_ROUTE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ADAPTER_KINDS = {
    "openai-compatible",
    "openai-compatible-vision",
    "openai-compatible-image-generation",
}
_ROUTE_FIELDS = {
    "route_key", "provider_id", "provider_revision", "model_name", "adapter_kind",
    "enabled", "revision", "reason", "created_at", "updated_at",
}
_HISTORY_FIELDS = {
    "registry_revision", "route_key", "route_revision", "action", "recorded_at", "route",
}
_MIGRATION_FIELDS = {
    "migration_id", "source_fingerprint", "from_registry_revision", "to_registry_revision",
    "status", "before_routes", "after_routes", "created_at", "rolled_back_at",
    "rollback_registry_revision",
}
_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "cookies", "password",
    "secret", "secret_key", "token", "access_token", "refresh_token",
}
_PATH_LOCKS: dict[Path, Lock] = {}
_PATH_LOCKS_GUARD = Lock()


class ModelRouteRegistryError(ValueError):
    pass


class ModelRouteRegistryConflict(ModelRouteRegistryError):
    pass


class ModelRouteRegistryNotFound(ModelRouteRegistryError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _path_lock(path: Path) -> Lock:
    resolved = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(resolved, Lock())


def _reject_sensitive_material(value: object, *, path: str = "route") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_secret"):
                raise ModelRouteRegistryError(f"sensitive material is forbidden at {path}.{key}")
            _reject_sensitive_material(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_material(nested, path=f"{path}[{index}]")


class ModelRouteRegistry:
    """Versioned, non-secret route intent with separately controlled runtime activation."""

    schema_version = "1.0.0"

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        self._path = root_dir / "library" / "global" / "model-routes" / "model-routes.json"
        self._lock = _path_lock(self._path)

    @property
    def runtime_activation(self) -> bool:
        from .model_route_runtime import ModelRouteRuntimeError, ModelRouteRuntimeService

        try:
            return ModelRouteRuntimeService(self._root_dir).status()["mode"] == "active"
        except ModelRouteRuntimeError as error:
            raise ModelRouteRegistryError(str(error)) from error

    def list(self) -> dict[str, object]:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            return {
                "schema_version": self.schema_version,
                "registry_revision": payload["registry_revision"],
                "runtime_activation": self.runtime_activation,
                "routes": deepcopy(payload["routes"]),
                "migrations": deepcopy(payload["migrations"]),
            }

    def get(self, route_key: str) -> dict[str, object]:
        key = self._normalize_route_key(route_key)
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            route = self._find(payload, key)
            if route is None:
                raise ModelRouteRegistryNotFound(key)
            history = [item for item in payload["history"] if item["route_key"] == key]
            return {
                "schema_version": self.schema_version,
                "registry_revision": payload["registry_revision"],
                "runtime_activation": self.runtime_activation,
                "route": deepcopy(route),
                "history": deepcopy(history),
            }

    def get_migration(self, migration_id: str) -> dict[str, object]:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            migration = next(
                (item for item in payload["migrations"] if item["migration_id"] == migration_id),
                None,
            )
            if migration is None:
                raise ModelRouteRegistryNotFound(migration_id)
            return deepcopy(migration)

    def preview(
        self,
        route_key: str,
        values: Mapping[str, object],
        *,
        provider: Mapping[str, object],
        egress_consented: bool,
    ) -> dict[str, object]:
        _reject_sensitive_material(values)
        key = self._normalize_route_key(route_key)
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            current = self._find(payload, key)
            route = self._normalize_route(
                key,
                values,
                provider=provider,
                egress_consented=egress_consented,
                revision=int(current["revision"]) + 1 if current else 1,
                created_at=str(current["created_at"]) if current else _now(),
            )
            return {
                "valid": True,
                "registry_revision": payload["registry_revision"],
                "runtime_activation": self.runtime_activation,
                "runtime_effect": "none_until_stage_c3",
                "route": route,
            }

    def update(
        self,
        route_key: str,
        values: Mapping[str, object],
        *,
        expected_registry_revision: int,
        provider: Mapping[str, object],
        egress_consented: bool,
    ) -> dict[str, object]:
        _reject_sensitive_material(values)
        key = self._normalize_route_key(route_key)
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            if int(expected_registry_revision) != int(payload["registry_revision"]):
                raise ModelRouteRegistryConflict(
                    f"model route registry revision conflict: expected {expected_registry_revision}, current {payload['registry_revision']}"
                )
            current = self._find(payload, key)
            route = self._normalize_route(
                key,
                values,
                provider=provider,
                egress_consented=egress_consented,
                revision=int(current["revision"]) + 1 if current else 1,
                created_at=str(current["created_at"]) if current else _now(),
            )
            routes = [item for item in payload["routes"] if item["route_key"] != key]
            routes.append(route)
            routes.sort(key=lambda item: str(item["route_key"]))
            next_revision = int(payload["registry_revision"]) + 1
            history = list(payload["history"])
            history.append({
                "registry_revision": next_revision,
                "route_key": key,
                "route_revision": route["revision"],
                "action": "created" if current is None else "updated",
                "recorded_at": route["updated_at"],
                "route": deepcopy(route),
            })
            next_payload = {
                "schema_version": self.schema_version,
                "registry_revision": next_revision,
                "routes": routes,
                "history": history,
                "migrations": payload["migrations"],
            }
            self._write(next_payload)
            return {
                "schema_version": self.schema_version,
                "registry_revision": next_revision,
                "runtime_activation": self.runtime_activation,
                "route": deepcopy(route),
                "history": [item for item in history if item["route_key"] == key],
            }

    def update_batch(
        self,
        assignments: list[tuple[str, Mapping[str, object], Mapping[str, object], bool]],
        *,
        expected_registry_revision: int,
    ) -> dict[str, object]:
        """Validate and persist a complete route plan with one registry CAS/write."""
        _reject_sensitive_material(assignments, path="assignments")
        if not assignments:
            raise ModelRouteRegistryError("model route batch must contain at least one assignment")
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            if int(expected_registry_revision) != int(payload["registry_revision"]):
                raise ModelRouteRegistryConflict(
                    f"model route registry revision conflict: expected {expected_registry_revision}, current {payload['registry_revision']}"
                )
            routes_by_key = {str(item["route_key"]): deepcopy(item) for item in payload["routes"]}
            candidates: list[dict[str, object]] = []
            seen: set[str] = set()
            for route_key, values, provider, consented in assignments:
                key = self._normalize_route_key(route_key)
                if key in seen:
                    raise ModelRouteRegistryError("duplicate model route key in batch")
                seen.add(key)
                current = routes_by_key.get(key)
                candidate = self._normalize_route(
                    key,
                    values,
                    provider=provider,
                    egress_consented=consented,
                    revision=int(current["revision"]) + 1 if current else 1,
                    created_at=str(current["created_at"]) if current else _now(),
                )
                if current is not None and self._same_assignment(current, candidate):
                    continue
                candidates.append(candidate)

            if not candidates:
                return {
                    "schema_version": self.schema_version,
                    "registry_revision": payload["registry_revision"],
                    "runtime_activation": self.runtime_activation,
                    "routes": deepcopy(payload["routes"]),
                    "changed_route_keys": [],
                    "replayed": True,
                }
            if self.runtime_activation:
                raise ModelRouteRegistryConflict("deactivate model route runtime before applying a batch plan")

            next_revision = int(payload["registry_revision"]) + 1
            history = list(payload["history"])
            for route in candidates:
                current = routes_by_key.get(str(route["route_key"]))
                routes_by_key[str(route["route_key"])] = route
                history.append({
                    "registry_revision": next_revision,
                    "route_key": route["route_key"],
                    "route_revision": route["revision"],
                    "action": "created" if current is None else "updated",
                    "recorded_at": route["updated_at"],
                    "route": deepcopy(route),
                })
            routes = sorted(routes_by_key.values(), key=lambda item: str(item["route_key"]))
            self._write({
                "schema_version": self.schema_version,
                "registry_revision": next_revision,
                "routes": routes,
                "history": history,
                "migrations": payload["migrations"],
            })
            return {
                "schema_version": self.schema_version,
                "registry_revision": next_revision,
                "runtime_activation": False,
                "routes": deepcopy(routes),
                "changed_route_keys": [str(item["route_key"]) for item in candidates],
                "replayed": False,
            }

    def apply_migration(
        self,
        migration_id: str,
        source_fingerprint: str,
        assignments: list[tuple[str, Mapping[str, object], Mapping[str, object], bool]],
        *,
        expected_registry_revision: int,
    ) -> dict[str, object]:
        if not re.fullmatch(r"migration-[a-f0-9]{24}", migration_id):
            raise ModelRouteRegistryError("invalid migration_id")
        if not re.fullmatch(r"[a-f0-9]{64}", source_fingerprint):
            raise ModelRouteRegistryError("invalid source_fingerprint")
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            existing = next(
                (item for item in payload["migrations"] if item["migration_id"] == migration_id),
                None,
            )
            if existing is not None:
                if existing["source_fingerprint"] != source_fingerprint:
                    raise ModelRouteRegistryConflict("migration replay fingerprint mismatch")
                return {
                    "status": existing["status"],
                    "replayed": True,
                    "runtime_activation": self.runtime_activation,
                    "migration": deepcopy(existing),
                }
            if int(expected_registry_revision) != int(payload["registry_revision"]):
                raise ModelRouteRegistryConflict(
                    f"model route registry revision conflict: expected {expected_registry_revision}, current {payload['registry_revision']}"
                )
            if not assignments:
                raise ModelRouteRegistryError("migration must select at least one route")
            before_routes = deepcopy(payload["routes"])
            routes_by_key = {str(item["route_key"]): deepcopy(item) for item in payload["routes"]}
            normalized: list[dict[str, object]] = []
            seen: set[str] = set()
            for route_key, values, provider, consented in assignments:
                key = self._normalize_route_key(route_key)
                if key in seen:
                    raise ModelRouteRegistryError("duplicate migration route key")
                seen.add(key)
                current = routes_by_key.get(key)
                route = self._normalize_route(
                    key,
                    values,
                    provider=provider,
                    egress_consented=consented,
                    revision=int(current["revision"]) + 1 if current else 1,
                    created_at=str(current["created_at"]) if current else _now(),
                )
                routes_by_key[key] = route
                normalized.append(route)
            next_revision = int(payload["registry_revision"]) + 1
            history = list(payload["history"])
            for route in normalized:
                history.append({
                    "registry_revision": next_revision,
                    "route_key": route["route_key"],
                    "route_revision": route["revision"],
                    "action": "migration_applied",
                    "recorded_at": route["updated_at"],
                    "route": deepcopy(route),
                })
            after_routes = sorted(routes_by_key.values(), key=lambda item: str(item["route_key"]))
            created_at = _now()
            migration = {
                "migration_id": migration_id,
                "source_fingerprint": source_fingerprint,
                "from_registry_revision": payload["registry_revision"],
                "to_registry_revision": next_revision,
                "status": "applied",
                "before_routes": before_routes,
                "after_routes": deepcopy(after_routes),
                "created_at": created_at,
                "rolled_back_at": "",
                "rollback_registry_revision": None,
            }
            self._write({
                "schema_version": self.schema_version,
                "registry_revision": next_revision,
                "routes": after_routes,
                "history": history,
                "migrations": [*payload["migrations"], migration],
            })
            return {
                "status": "applied",
                "replayed": False,
                "runtime_activation": self.runtime_activation,
                "migration": deepcopy(migration),
            }

    def rollback_migration(
        self,
        migration_id: str,
        *,
        expected_registry_revision: int,
    ) -> dict[str, object]:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            migration = next(
                (item for item in payload["migrations"] if item["migration_id"] == migration_id),
                None,
            )
            if migration is None:
                raise ModelRouteRegistryNotFound(migration_id)
            if migration["status"] == "rolled_back":
                return {
                    "status": "rolled_back",
                    "replayed": True,
                    "runtime_activation": self.runtime_activation,
                    "migration": deepcopy(migration),
                }
            if int(expected_registry_revision) != int(payload["registry_revision"]):
                raise ModelRouteRegistryConflict(
                    f"model route registry revision conflict: expected {expected_registry_revision}, current {payload['registry_revision']}"
                )
            if int(payload["registry_revision"]) != int(migration["to_registry_revision"]):
                raise ModelRouteRegistryConflict("migration rollback requires the migration to be the latest registry change")
            next_revision = int(payload["registry_revision"]) + 1
            recorded_at = _now()
            before_routes = deepcopy(migration["before_routes"])
            history = list(payload["history"])
            changed_keys = {
                str(item["route_key"])
                for item in [*migration["before_routes"], *migration["after_routes"]]
            }
            before_by_key = {str(item["route_key"]): item for item in before_routes}
            for key in sorted(changed_keys):
                prior = before_by_key.get(key)
                history.append({
                    "registry_revision": next_revision,
                    "route_key": key,
                    "route_revision": int(prior["revision"]) if prior else 0,
                    "action": "migration_rolled_back",
                    "recorded_at": recorded_at,
                    "route": deepcopy(prior) if prior else {
                        "route_key": key,
                        "provider_id": "removed",
                        "provider_revision": "removed",
                        "model_name": "removed",
                        "adapter_kind": "openai-compatible",
                        "enabled": False,
                        "revision": 1,
                        "reason": "route removed by migration rollback",
                        "created_at": recorded_at,
                        "updated_at": recorded_at,
                    },
                })
            migration["status"] = "rolled_back"
            migration["rolled_back_at"] = recorded_at
            migration["rollback_registry_revision"] = next_revision
            self._write({
                "schema_version": self.schema_version,
                "registry_revision": next_revision,
                "routes": before_routes,
                "history": history,
                "migrations": payload["migrations"],
            })
            return {
                "status": "rolled_back",
                "replayed": False,
                "runtime_activation": self.runtime_activation,
                "migration": deepcopy(migration),
            }

    def validate_provider_reference(
        self,
        route: Mapping[str, object],
        *,
        provider: Mapping[str, object],
        egress_consented: bool,
    ) -> None:
        self._normalize_route(
            str(route.get("route_key") or ""),
            route,
            provider=provider,
            egress_consented=egress_consented,
            revision=int(route.get("revision") or 0),
            created_at=str(route.get("created_at") or _now()),
            require_provider_revision=str(route.get("provider_revision") or ""),
        )

    def _normalize_route(
        self,
        route_key: str,
        values: Mapping[str, object],
        *,
        provider: Mapping[str, object],
        egress_consented: bool,
        revision: int,
        created_at: str,
        require_provider_revision: str | None = None,
    ) -> dict[str, object]:
        key = self._normalize_route_key(route_key)
        provider_id = str(values.get("provider_id") or "").strip().lower()
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ModelRouteRegistryError("invalid provider_id")
        if provider_id != str(provider.get("provider_id") or ""):
            raise ModelRouteRegistryError("provider reference mismatch")
        if provider.get("enabled") is not True:
            raise ModelRouteRegistryError("provider is disabled")
        provider_revision = str(provider.get("updated_at") or "").strip()
        if not provider_revision:
            raise ModelRouteRegistryError("provider revision is missing")
        if require_provider_revision is not None and provider_revision != require_provider_revision:
            raise ModelRouteRegistryError("provider revision drift")
        model_name = str(values.get("model_name") or "").strip()
        available_models = {str(item).strip() for item in provider.get("models", []) if str(item).strip()}
        current_model = str(provider.get("model") or "").strip()
        if current_model:
            available_models.add(current_model)
        if not model_name or model_name not in available_models:
            raise ModelRouteRegistryError("model is not available from the referenced provider")
        adapter_kind = str(values.get("adapter_kind") or "").strip().lower()
        if adapter_kind not in _ADAPTER_KINDS:
            raise ModelRouteRegistryError("unsupported adapter_kind")
        if not egress_consented:
            raise ModelRouteRegistryError("provider egress consent is required")
        reason = str(values.get("reason") or "").strip()
        if len(reason) < 3 or len(reason) > 500:
            raise ModelRouteRegistryError("reason must contain 3 to 500 characters")
        if revision < 1:
            raise ModelRouteRegistryError("route revision must be positive")
        updated_at = _now()
        return {
            "route_key": key,
            "provider_id": provider_id,
            "provider_revision": provider_revision,
            "model_name": model_name,
            "adapter_kind": adapter_kind,
            "enabled": bool(values.get("enabled", True)),
            "revision": revision,
            "reason": reason,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    @staticmethod
    def _normalize_route_key(route_key: str) -> str:
        key = str(route_key or "").strip().lower()
        if not _ROUTE_KEY.fullmatch(key):
            raise ModelRouteRegistryError("invalid route_key")
        return key

    @staticmethod
    def _find(payload: Mapping[str, object], route_key: str) -> dict[str, object] | None:
        return next((item for item in payload["routes"] if item["route_key"] == route_key), None)

    @staticmethod
    def _same_assignment(current: Mapping[str, object], candidate: Mapping[str, object]) -> bool:
        fields = ("provider_id", "provider_revision", "model_name", "adapter_kind", "enabled", "reason")
        return all(current.get(field) == candidate.get(field) for field in fields)

    def _read(self) -> dict[str, object]:
        if not self._path.exists():
            return {"schema_version": self.schema_version, "registry_revision": 0, "routes": [], "history": [], "migrations": []}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelRouteRegistryError("model route registry is corrupt") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != self.schema_version:
            raise ModelRouteRegistryError("unsupported model route registry schema")
        if not isinstance(payload.get("registry_revision"), int) or payload["registry_revision"] < 0:
            raise ModelRouteRegistryError("invalid model route registry revision")
        if not isinstance(payload.get("routes"), list) or not isinstance(payload.get("history"), list):
            raise ModelRouteRegistryError("invalid model route registry payload")
        migrations = payload.setdefault("migrations", [])
        if not isinstance(migrations, list):
            raise ModelRouteRegistryError("invalid model route migrations")
        _reject_sensitive_material(payload, path="registry")
        seen: set[str] = set()
        for route in payload["routes"]:
            if not isinstance(route, dict) or set(route) != _ROUTE_FIELDS:
                raise ModelRouteRegistryError("invalid model route record fields")
            key = self._normalize_route_key(str(route.get("route_key") or ""))
            if key in seen:
                raise ModelRouteRegistryError("duplicate model route key")
            seen.add(key)
        for history in payload["history"]:
            if not isinstance(history, dict) or set(history) != _HISTORY_FIELDS:
                raise ModelRouteRegistryError("invalid model route history fields")
            if not isinstance(history.get("route"), dict) or set(history["route"]) != _ROUTE_FIELDS:
                raise ModelRouteRegistryError("invalid model route history snapshot")
        for migration in migrations:
            if not isinstance(migration, dict) or set(migration) != _MIGRATION_FIELDS:
                raise ModelRouteRegistryError("invalid model route migration fields")
            if migration.get("status") not in {"applied", "rolled_back"}:
                raise ModelRouteRegistryError("invalid model route migration status")
            for snapshot_name in ("before_routes", "after_routes"):
                snapshots = migration.get(snapshot_name)
                if not isinstance(snapshots, list):
                    raise ModelRouteRegistryError("invalid model route migration snapshot")
                for route in snapshots:
                    if not isinstance(route, dict) or set(route) != _ROUTE_FIELDS:
                        raise ModelRouteRegistryError("invalid model route migration route")
        return payload

    def _write(self, payload: Mapping[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        staged_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as staged:
                staged.write(serialized)
                staged.flush()
                os.fsync(staged.fileno())
                staged_path = Path(staged.name)
            os.replace(staged_path, self._path)
        finally:
            if staged_path is not None and staged_path.exists():
                staged_path.unlink()
