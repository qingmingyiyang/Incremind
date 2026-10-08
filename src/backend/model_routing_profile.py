"""Durable, non-secret authority for canonical model tier routing."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from threading import Lock

from backend.shared.filesystem import atomic_write_text
from backend.shared.interprocess_lock import interprocess_file_lock
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence
from core.ai_tooling import ModelRoutingAuthorityBinding, ModelRoutingProfile


_SCHEMA_VERSION = "1.0.0"
_TIERS = ("fast", "standard", "deep", "vision", "image_generation")
_TEXT_TIERS = frozenset({"fast", "standard", "deep"})
_ROUTE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_FIELDS = frozenset({
    "schema_version", "revision", "rules_version", "text_default_tier", "tier_routes",
    "authority_binding",
})
_UNBOUND_FIELDS = _FIELDS - {"authority_binding"}
_SENSITIVE = frozenset({
    "api_key", "apikey", "authorization", "base_url", "cookie", "endpoint",
    "password", "prompt", "secret", "token",
})
_LOCKS: dict[Path, Lock] = {}
_LOCKS_GUARD = Lock()


class ModelRoutingProfileStoreError(ValueError):
    pass


class ModelRoutingProfileConflict(ModelRoutingProfileStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRoutingProfileSnapshot:
    profile: ModelRoutingProfile
    persisted: bool


class ModelRoutingProfileStore:
    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir).resolve()
        self._path = (
            Path(root_dir).resolve()
            / "library" / "global" / "model-routes" / "tier-routing-profile.json"
        )

    def get(self) -> ModelRoutingProfileSnapshot:
        with model_dispatch_authority_fence(self._root_dir), _path_lock(self._path):
            if not self._path.exists():
                return ModelRoutingProfileSnapshot(_default_profile(), False)
            return ModelRoutingProfileSnapshot(_decode(_read_json(self._path)), True)

    def update(
        self, *, expected_revision: int, rules_version: int,
        text_default_tier: str, tier_routes: Mapping[str, str | None],
        authority_binding: Mapping[str, object] | None = None,
    ) -> ModelRoutingProfileSnapshot:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ModelRoutingProfileStoreError("expected routing profile revision is invalid")
        with model_dispatch_authority_fence(self._root_dir), _path_lock(self._path), interprocess_file_lock(self._path):
            current = _default_profile() if not self._path.exists() else _decode(
                _read_json(self._path)
            )
            if current.revision != expected_revision:
                raise ModelRoutingProfileConflict(
                    "model routing profile revision conflict: "
                    f"expected {expected_revision}, current {current.revision}"
                )
            profile = ModelRoutingProfile(
                revision=current.revision + 1,
                rules_version=rules_version,
                text_default_tier=text_default_tier,  # type: ignore[arg-type]
                tier_routes=_tier_routes(tier_routes),
                authority_binding=_authority_binding(authority_binding),
            )
            payload = _encode(profile)
            _reject_sensitive(payload)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            return ModelRoutingProfileSnapshot(profile, True)

    def preview_update(
        self, *, expected_revision: int, rules_version: int,
        text_default_tier: str, tier_routes: Mapping[str, str | None],
        authority_binding: Mapping[str, object] | None = None,
    ) -> ModelRoutingProfile:
        current = self.get().profile
        if current.revision != expected_revision:
            raise ModelRoutingProfileConflict(
                "model routing profile revision conflict: "
                f"expected {expected_revision}, current {current.revision}"
            )
        return ModelRoutingProfile(
            revision=current.revision + 1,
            rules_version=rules_version,
            text_default_tier=text_default_tier,  # type: ignore[arg-type]
            tier_routes=_tier_routes(tier_routes),
            authority_binding=_authority_binding(authority_binding),
        )


def _default_profile() -> ModelRoutingProfile:
    return ModelRoutingProfile(
        revision=1,
        rules_version=1,
        text_default_tier="standard",
        tier_routes=(
            ("fast", None),
            ("standard", "search.answer"),
            ("deep", None),
            ("vision", "companion.vision"),
            ("image_generation", None),
        ),
    )


def _encode(profile: ModelRoutingProfile) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "revision": profile.revision,
        "rules_version": profile.rules_version,
        "text_default_tier": profile.text_default_tier,
        "tier_routes": dict(profile.tier_routes),
        "authority_binding": (
            None if profile.authority_binding is None else {
                "registry_revision": profile.authority_binding.registry_revision,
                "runtime_revision": profile.authority_binding.runtime_revision,
                "activation_fingerprint": profile.authority_binding.activation_fingerprint,
            }
        ),
    }


def _decode(payload: Mapping[str, object]) -> ModelRoutingProfile:
    if frozenset(payload) not in {_FIELDS, _UNBOUND_FIELDS} or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ModelRoutingProfileStoreError("model routing profile schema is invalid")
    _reject_sensitive(payload)
    try:
        return ModelRoutingProfile(
            revision=_positive(payload.get("revision"), "routing profile revision"),
            rules_version=_positive(payload.get("rules_version"), "routing rules version"),
            text_default_tier=_text_tier(payload.get("text_default_tier")),
            tier_routes=_tier_routes(payload.get("tier_routes")),
            authority_binding=_authority_binding(payload.get("authority_binding")),
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, ModelRoutingProfileStoreError):
            raise
        raise ModelRoutingProfileStoreError("model routing profile is invalid") from error


def _tier_routes(value: object) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, Mapping) or set(value) != set(_TIERS):
        raise ModelRoutingProfileStoreError(
            "model routing profile must define every canonical tier"
        )
    result: list[tuple[str, str | None]] = []
    for tier in _TIERS:
        route = value[tier]
        if route is not None and (
            not isinstance(route, str) or not _ROUTE_KEY.fullmatch(route)
        ):
            raise ModelRoutingProfileStoreError("model tier route identity is invalid")
        result.append((tier, route))
    return tuple(result)  # type: ignore[return-value]


def _text_tier(value: object) -> str:
    if not isinstance(value, str) or value not in _TEXT_TIERS:
        raise ModelRoutingProfileStoreError("text default model tier is invalid")
    return value


def _authority_binding(value: object) -> ModelRoutingAuthorityBinding | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "registry_revision", "runtime_revision", "activation_fingerprint",
    }:
        raise ModelRoutingProfileStoreError("model routing authority binding is invalid")
    return ModelRoutingAuthorityBinding(
        registry_revision=_positive(value.get("registry_revision"), "registry revision"),
        runtime_revision=_positive(value.get("runtime_revision"), "runtime revision"),
        activation_fingerprint=str(value.get("activation_fingerprint") or ""),
    )


def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ModelRoutingProfileStoreError(f"{label} must be positive")
    return value


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelRoutingProfileStoreError("model routing profile is unreadable") from error
    if not isinstance(value, dict):
        raise ModelRoutingProfileStoreError("model routing profile must be an object")
    return value


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE or normalized.endswith("_secret"):
                raise ModelRoutingProfileStoreError(
                    "sensitive material is forbidden in model routing profile"
                )
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)


def _path_lock(path: Path) -> Lock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(resolved, Lock())
