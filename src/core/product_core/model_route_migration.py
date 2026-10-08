from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping, Sequence

from .model_route_registry import (
    ModelRouteRegistry,
    ModelRouteRegistryConflict,
    ModelRouteRegistryError,
    ModelRouteRegistryNotFound,
)


ROUTE_SPECS: tuple[tuple[str, str], ...] = (
    ("lightweight", "task.lightweight"),
    ("intakeMain", "intake.classification"),
    ("default", "conversation.default"),
    ("memory", "memory.candidate"),
    ("search", "memory.project_routing"),
    ("search", "search.answer"),
)
RETAINED_SPECIAL_KEYS: tuple[str, ...] = ("embed", "vision", "asr")
CHOICES = {"renderer", "developer", "compatibility", "skip"}
ALL_LEGACY_USE_KEYS = {use_key for use_key, _route_key in ROUTE_SPECS} | set(RETAINED_SPECIAL_KEYS)


class ModelRouteMigrationError(ValueError):
    pass


class ModelRouteMigrationConflict(ModelRouteMigrationError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderContext:
    record: Mapping[str, object]
    egress_consented: bool


class ModelRouteMigrationService:
    def __init__(self, registry: ModelRouteRegistry) -> None:
        self._registry = registry

    def preview(
        self,
        *,
        renderer_task_map: Mapping[str, object],
        developer_revision: int,
        developer_task_map: Mapping[str, object],
        developer_model_profiles: Sequence[Mapping[str, object]],
        providers: Sequence[ProviderContext],
    ) -> dict[str, object]:
        unknown_renderer_keys = _validate_renderer_task_map(renderer_task_map)
        provider_by_id = {
            str(context.record.get("provider_id") or ""): context
            for context in providers
            if str(context.record.get("provider_id") or "")
        }
        active = next((context for context in providers if context.record.get("is_active") is True), None)
        profiles = {
            str(profile.get("id") or ""): profile
            for profile in developer_model_profiles
            if str(profile.get("id") or "")
        }
        registry_state = self._registry.list()
        routes: list[dict[str, object]] = []
        for use_key, route_key in ROUTE_SPECS:
            renderer = self._renderer_candidate(use_key, renderer_task_map.get(use_key), provider_by_id)
            developer = self._developer_candidate(
                use_key,
                developer_task_map.get(use_key),
                profiles,
                provider_by_id,
            )
            compatibility = self._compatibility_candidate(use_key, active)
            legacy_valid = [item for item in (renderer, developer) if item["valid"]]
            distinct = {(item["provider_id"], item["model_name"]) for item in legacy_valid}
            conflict = len(distinct) > 1
            if conflict:
                recommended = None
            elif renderer["valid"]:
                recommended = "renderer"
            elif developer["valid"]:
                recommended = "developer"
            elif compatibility["valid"]:
                recommended = "compatibility"
            else:
                recommended = "skip"
            routes.append({
                "use_key": use_key,
                "route_key": route_key,
                "conflict": conflict,
                "recommended_source": recommended,
                "options": [renderer, developer, compatibility],
            })
        canonical = {
            "registry_revision": registry_state["registry_revision"],
            "developer_revision": int(developer_revision),
            "renderer_task_map": _json_safe_mapping(renderer_task_map),
            "developer_task_map": _json_safe_mapping(developer_task_map),
            "developer_model_profiles": [_json_safe_mapping(item) for item in developer_model_profiles],
            "providers": [
                {
                    "provider_id": str(item.record.get("provider_id") or ""),
                    "provider_revision": str(item.record.get("updated_at") or ""),
                    "enabled": item.record.get("enabled") is True,
                    "is_active": item.record.get("is_active") is True,
                    "model": str(item.record.get("model") or ""),
                    "models": sorted(str(model) for model in item.record.get("models", []) if str(model)),
                    "egress_consented": item.egress_consented,
                }
                for item in sorted(providers, key=lambda value: str(value.record.get("provider_id") or ""))
            ],
            "routes": routes,
        }
        preview_token = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "status": "preview",
            "preview_token": preview_token,
            "preview_id": f"preview-{preview_token[:24]}",
            "registry_revision": registry_state["registry_revision"],
            "developer_revision": int(developer_revision),
            "runtime_activation": self._registry.runtime_activation,
            "legacy_behavior": "legacy maps did not control production providers",
            "routes": routes,
            "retained_unmigrated": sorted({
                *RETAINED_SPECIAL_KEYS,
                *unknown_renderer_keys,
                *(str(key) for key in developer_task_map if str(key) not in ALL_LEGACY_USE_KEYS),
            }),
        }

    def confirm(
        self,
        *,
        preview_token: str,
        confirm: bool,
        choices: Mapping[str, object],
        renderer_task_map: Mapping[str, object],
        developer_revision: int,
        developer_task_map: Mapping[str, object],
        developer_model_profiles: Sequence[Mapping[str, object]],
        providers: Sequence[ProviderContext],
    ) -> dict[str, object]:
        if not confirm:
            raise ModelRouteMigrationError("explicit migration confirmation is required")
        if self._registry.runtime_activation:
            raise ModelRouteMigrationConflict("deactivate model route runtime before migration")
        if not isinstance(preview_token, str) or len(preview_token) != 64:
            raise ModelRouteMigrationError("invalid preview token")
        canonical_choices = json.dumps(
            {str(key): str(value) for key, value in sorted(choices.items())},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        confirmation_fingerprint = hashlib.sha256(
            f"{preview_token}:{canonical_choices}".encode("utf-8")
        ).hexdigest()
        migration_id = f"migration-{confirmation_fingerprint[:24]}"
        try:
            existing = self._registry.get_migration(migration_id)
        except ModelRouteRegistryNotFound:
            existing = None
        if existing is not None:
            if existing["source_fingerprint"] != confirmation_fingerprint:
                raise ModelRouteMigrationConflict("migration replay fingerprint mismatch")
            return {
                "status": existing["status"],
                "replayed": True,
                "runtime_activation": self._registry.runtime_activation,
                "migration": existing,
            }
        preview = self.preview(
            renderer_task_map=renderer_task_map,
            developer_revision=developer_revision,
            developer_task_map=developer_task_map,
            developer_model_profiles=developer_model_profiles,
            providers=providers,
        )
        if preview["preview_token"] != preview_token:
            raise ModelRouteMigrationConflict("migration preview drifted; create a new preview")
        route_by_key = {str(item["route_key"]): item for item in preview["routes"]}
        if set(choices) != set(route_by_key):
            raise ModelRouteMigrationError("every migration route requires an explicit choice")
        provider_by_id = {
            str(context.record.get("provider_id") or ""): context
            for context in providers
            if str(context.record.get("provider_id") or "")
        }
        assignments: list[tuple[str, Mapping[str, object], Mapping[str, object], bool]] = []
        selected: dict[str, str] = {}
        for route_key, route in route_by_key.items():
            choice = str(choices.get(route_key) or "")
            if choice not in CHOICES:
                raise ModelRouteMigrationError(f"invalid migration choice for {route_key}")
            selected[route_key] = choice
            if choice == "skip":
                continue
            option = next(item for item in route["options"] if item["source"] == choice)
            if option["valid"] is not True:
                raise ModelRouteMigrationError(f"selected migration option is invalid for {route_key}")
            provider = provider_by_id[str(option["provider_id"])]
            assignments.append((
                route_key,
                {
                    "provider_id": option["provider_id"],
                    "model_name": option["model_name"],
                    "adapter_kind": "openai-compatible",
                    "enabled": True,
                    "reason": f"explicit legacy migration from {choice}",
                },
                provider.record,
                provider.egress_consented,
            ))
        if not assignments:
            raise ModelRouteMigrationError("migration cannot skip every route")
        try:
            result = self._registry.apply_migration(
                migration_id,
                confirmation_fingerprint,
                assignments,
                expected_registry_revision=int(preview["registry_revision"]),
            )
        except ModelRouteRegistryConflict as error:
            raise ModelRouteMigrationConflict(str(error)) from error
        except ModelRouteRegistryError as error:
            raise ModelRouteMigrationError(str(error)) from error
        return {**result, "choices": selected}

    def rollback(
        self,
        migration_id: str,
        *,
        expected_registry_revision: int,
        confirm: bool,
    ) -> dict[str, object]:
        if not confirm:
            raise ModelRouteMigrationError("explicit rollback confirmation is required")
        try:
            return self._registry.rollback_migration(
                migration_id,
                expected_registry_revision=expected_registry_revision,
            )
        except ModelRouteRegistryNotFound as error:
            raise ModelRouteMigrationError("migration not found") from error
        except ModelRouteRegistryConflict as error:
            raise ModelRouteMigrationConflict(str(error)) from error

    @staticmethod
    def _renderer_candidate(use_key: str, value: object, providers: Mapping[str, ProviderContext]) -> dict[str, object]:
        if not isinstance(value, str) or not value.strip():
            return _candidate("renderer", use_key, valid=False, issue="not_configured")
        return _provider_candidate("renderer", use_key, value.strip(), None, providers, legacy_ref=value.strip())

    @staticmethod
    def _developer_candidate(
        use_key: str,
        value: object,
        profiles: Mapping[str, Mapping[str, object]],
        providers: Mapping[str, ProviderContext],
    ) -> dict[str, object]:
        profile: Mapping[str, object] | None = None
        if isinstance(value, str) and value.strip():
            profile = profiles.get(value.strip())
            if profile is None:
                return _candidate("developer", use_key, valid=False, issue="unknown_profile", legacy_ref=value.strip())
        elif isinstance(value, Mapping):
            profile_id = str(value.get("profile_id") or "").strip()
            profile = profiles.get(profile_id) if profile_id else value
            if profile_id and profile is None:
                return _candidate("developer", use_key, valid=False, issue="unknown_profile", legacy_ref=profile_id)
            if profile is not value:
                merged = dict(profile)
                merged.update(value)
                profile = merged
        else:
            return _candidate("developer", use_key, valid=False, issue="not_configured")
        provider_id = str(
            profile.get("provider_id")
            or profile.get("providerId")
            or profile.get("provider")
            or ""
        ).strip().lower()
        model_name = str(profile.get("model_name") or profile.get("modelId") or profile.get("model") or "").strip()
        return _provider_candidate(
            "developer",
            use_key,
            provider_id,
            model_name or None,
            providers,
            legacy_ref=str(profile.get("id") or value),
        )

    @staticmethod
    def _compatibility_candidate(use_key: str, active: ProviderContext | None) -> dict[str, object]:
        if active is None:
            return _candidate("compatibility", use_key, valid=False, issue="no_active_provider")
        return _provider_candidate(
            "compatibility",
            use_key,
            str(active.record.get("provider_id") or ""),
            str(active.record.get("model") or "") or None,
            {str(active.record.get("provider_id") or ""): active},
            legacy_ref="current_active_provider",
        )


def _provider_candidate(
    source: str,
    use_key: str,
    provider_id: str,
    model_name: str | None,
    providers: Mapping[str, ProviderContext],
    *,
    legacy_ref: str,
) -> dict[str, object]:
    context = providers.get(provider_id)
    if context is None:
        return _candidate(source, use_key, valid=False, issue="unknown_provider", provider_id=provider_id, legacy_ref=legacy_ref)
    provider = context.record
    if provider.get("enabled") is not True:
        return _candidate(source, use_key, valid=False, issue="provider_disabled", provider_id=provider_id, legacy_ref=legacy_ref)
    resolved_model = model_name or str(provider.get("model") or "").strip()
    models = {str(item) for item in provider.get("models", []) if str(item)}
    if provider.get("model"):
        models.add(str(provider["model"]))
    if not resolved_model or resolved_model not in models:
        return _candidate(
            source,
            use_key,
            valid=False,
            issue="unknown_model",
            provider_id=provider_id,
            model_name=resolved_model,
            legacy_ref=legacy_ref,
        )
    if not context.egress_consented:
        return _candidate(
            source,
            use_key,
            valid=False,
            issue="egress_not_consented",
            provider_id=provider_id,
            model_name=resolved_model,
            legacy_ref=legacy_ref,
        )
    return _candidate(
        source,
        use_key,
        valid=True,
        issue="",
        provider_id=provider_id,
        model_name=resolved_model,
        provider_revision=str(provider.get("updated_at") or ""),
        legacy_ref=legacy_ref,
    )


def _candidate(
    source: str,
    use_key: str,
    *,
    valid: bool,
    issue: str,
    provider_id: str = "",
    model_name: str = "",
    provider_revision: str = "",
    legacy_ref: str = "",
) -> dict[str, object]:
    return {
        "source": source,
        "use_key": use_key,
        "valid": valid,
        "issue": issue,
        "provider_id": provider_id,
        "provider_revision": provider_revision,
        "model_name": model_name,
        "legacy_ref": legacy_ref,
    }


def _json_safe_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str))


def _validate_renderer_task_map(value: Mapping[str, object]) -> list[str]:
    unknown: list[str] = []
    for key, provider_id in value.items():
        normalized_key = str(key)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", normalized_key):
            raise ModelRouteMigrationError(f"invalid renderer task key: {key}")
        if normalized_key.lower() in {"api_key", "secret", "token", "authorization", "cookie"}:
            raise ModelRouteMigrationError(f"sensitive renderer task key is forbidden: {key}")
        if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id.strip()) > 64:
            raise ModelRouteMigrationError(f"renderer task value must be a provider id string: {key}")
        normalized_provider = provider_id.strip()
        if normalized_provider.lower().startswith(("sk-", "bearer-", "token-")) or normalized_provider.startswith("eyJ"):
            raise ModelRouteMigrationError(f"secret-like renderer task value is forbidden: {key}")
        if normalized_key not in ALL_LEGACY_USE_KEYS:
            unknown.append(normalized_key)
    return unknown
