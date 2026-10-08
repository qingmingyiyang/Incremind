from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
from threading import Lock

from backend.shared.filesystem import atomic_write_text
from backend.shared.interprocess_lock import interprocess_file_lock
from backend.security.network_adapter import LoopbackHttpConnectProxy


_SCHEMA_VERSION = "1.0.0"
_PROFILE_FIELDS = frozenset({
    "schema_version",
    "profile_id",
    "revision",
    "mode",
    "literal_address",
    "port",
    "allowed_capabilities",
})
_MODES = frozenset({"direct", "loopback_http_connect"})
_CAPABILITIES = frozenset({"anonymous_public_media"})
_SENSITIVE_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "proxy_authorization",
    "secret",
    "token",
    "username",
})
_PATH_LOCKS: dict[Path, Lock] = {}
_PATH_LOCKS_GUARD = Lock()


class NetworkEgressProfileError(ValueError):
    """The non-secret local egress configuration is invalid or unreadable."""


class NetworkEgressProfileConflict(NetworkEgressProfileError):
    """The caller attempted to replace a stale egress configuration."""


@dataclass(frozen=True, slots=True)
class NetworkEgressProfile:
    profile_id: str
    revision: int
    mode: str
    literal_address: str | None
    port: int | None
    allowed_capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NetworkEgressProfileSnapshot:
    profile: NetworkEgressProfile
    store_revision: int
    persisted: bool


class NetworkEgressProfileStore:
    """One versioned, non-secret egress profile for a local authority root.

    This store deliberately only records an opt-in loopback HTTP CONNECT
    endpoint.  It never records proxy credentials, hostnames, URLs, or a
    destination allow-list.  Network execution remains outside this module.
    """

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir).resolve()

    def get(self) -> NetworkEgressProfileSnapshot:
        path = self._path()
        with _path_lock(path):
            if not path.exists():
                return NetworkEgressProfileSnapshot(_default_profile(), 0, False)
            profile = _decode_profile(_read_json(path))
            return NetworkEgressProfileSnapshot(profile, profile.revision, True)

    @contextmanager
    def locked_snapshot(self) -> Iterator[NetworkEgressProfileSnapshot]:
        """Hold the cross-process profile lock across one policy decision."""

        path = self._path()
        with _path_lock(path), interprocess_file_lock(path):
            if not path.exists():
                yield NetworkEgressProfileSnapshot(_default_profile(), 0, False)
                return
            profile = _decode_profile(_read_json(path))
            yield NetworkEgressProfileSnapshot(profile, profile.revision, True)

    def update(
        self,
        *,
        mode: str,
        literal_address: str | None = None,
        port: int | None = None,
        allowed_capabilities: Sequence[str] = ("anonymous_public_media",),
        confirm_enable: bool = False,
        expected_revision: int,
    ) -> NetworkEgressProfileSnapshot:
        """CAS-replace the profile.

        A loopback CONNECT proxy changes the network route and therefore must
        be explicitly confirmed by the immediate caller on every enable or
        endpoint change.  `direct` is the safe default and stores no endpoint.
        """

        profile = _validated_profile(
            mode=mode,
            literal_address=literal_address,
            port=port,
            allowed_capabilities=allowed_capabilities,
            confirm_enable=confirm_enable,
        )
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise NetworkEgressProfileError("expected profile revision must be zero or positive")
        path = self._path()
        with _path_lock(path), interprocess_file_lock(path):
            current_revision = 0
            if path.exists():
                current_revision = _decode_profile(_read_json(path)).revision
            if expected_revision != current_revision:
                raise NetworkEgressProfileConflict(
                    f"network egress profile revision conflict: expected {expected_revision}, current {current_revision}"
                )
            persisted = NetworkEgressProfile(
                profile_id=profile.profile_id,
                revision=current_revision + 1,
                mode=profile.mode,
                literal_address=profile.literal_address,
                port=profile.port,
                allowed_capabilities=profile.allowed_capabilities,
            )
            encoded = _encode_profile(persisted)
            _reject_sensitive_material(encoded)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(encoded, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            return NetworkEgressProfileSnapshot(persisted, persisted.revision, True)

    def _path(self) -> Path:
        path = self._root_dir / "security" / "network-egress-profile.json"
        resolved = path.resolve()
        if not resolved.is_relative_to(self._root_dir):
            raise NetworkEgressProfileError("network egress profile path escaped authority root")
        return resolved


def loopback_proxy_for_capability(
    profile: NetworkEgressProfile, capability: str
) -> LoopbackHttpConnectProxy | None:
    if not isinstance(profile, NetworkEgressProfile):
        raise NetworkEgressProfileError("network egress profile is invalid")
    if capability not in _CAPABILITIES:
        raise NetworkEgressProfileError("network egress capability is invalid")
    if profile.mode == "direct" or capability not in profile.allowed_capabilities:
        return None
    if profile.mode != "loopback_http_connect" or profile.literal_address is None or profile.port is None:
        raise NetworkEgressProfileError("network egress profile is inconsistent")
    return LoopbackHttpConnectProxy(profile.literal_address, profile.port)


def _default_profile() -> NetworkEgressProfile:
    return NetworkEgressProfile(
        profile_id="network-egress-local",
        revision=1,
        mode="direct",
        literal_address=None,
        port=None,
        allowed_capabilities=("anonymous_public_media",),
    )


def _validated_profile(
    *,
    mode: object,
    literal_address: object,
    port: object,
    allowed_capabilities: object,
    confirm_enable: object,
) -> NetworkEgressProfile:
    if not isinstance(mode, str) or mode not in _MODES:
        raise NetworkEgressProfileError("network egress mode is invalid")
    capabilities = _capabilities(allowed_capabilities)
    if mode == "direct":
        if literal_address is not None or port is not None:
            raise NetworkEgressProfileError("direct egress must not configure an endpoint")
        return NetworkEgressProfile(
            profile_id="network-egress-local", revision=1, mode="direct",
            literal_address=None, port=None, allowed_capabilities=capabilities,
        )
    if confirm_enable is not True:
        raise NetworkEgressProfileError("loopback HTTP CONNECT egress requires explicit confirmation")
    address = _loopback_address(literal_address)
    checked_port = _port(port)
    return NetworkEgressProfile(
        profile_id="network-egress-local", revision=1, mode="loopback_http_connect",
        literal_address=address, port=checked_port, allowed_capabilities=capabilities,
    )


def _encode_profile(profile: NetworkEgressProfile) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "profile_id": profile.profile_id,
        "revision": profile.revision,
        "mode": profile.mode,
        "literal_address": profile.literal_address,
        "port": profile.port,
        "allowed_capabilities": list(profile.allowed_capabilities),
    }


def _decode_profile(payload: Mapping[str, object]) -> NetworkEgressProfile:
    _require_shape(payload)
    _reject_sensitive_material(payload)
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise NetworkEgressProfileError("network egress profile schema is unsupported")
    if payload.get("profile_id") != "network-egress-local":
        raise NetworkEgressProfileError("network egress profile identity drifted")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise NetworkEgressProfileError("network egress profile revision is invalid")
    profile = _validated_profile(
        mode=payload.get("mode"),
        literal_address=payload.get("literal_address"),
        port=payload.get("port"),
        allowed_capabilities=payload.get("allowed_capabilities"),
        # A persisted loopback profile is valid only because its original
        # update was confirmed.  A read must never require a new user action.
        confirm_enable=True,
    )
    return NetworkEgressProfile(
        profile_id=profile.profile_id,
        revision=revision,
        mode=profile.mode,
        literal_address=profile.literal_address,
        port=profile.port,
        allowed_capabilities=profile.allowed_capabilities,
    )


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NetworkEgressProfileError("network egress profile is unreadable") from error
    if not isinstance(payload, dict):
        raise NetworkEgressProfileError("network egress profile must be an object")
    return payload


def _require_shape(payload: Mapping[str, object]) -> None:
    if {str(key) for key in payload} != _PROFILE_FIELDS:
        raise NetworkEgressProfileError("network egress profile fields are invalid")


def _capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise NetworkEgressProfileError("network egress capabilities must be a string array")
    values = tuple(value)
    if any(not isinstance(item, str) or item not in _CAPABILITIES for item in values):
        raise NetworkEgressProfileError("network egress capability is invalid")
    if len(values) != len(set(values)):
        raise NetworkEgressProfileError("network egress capabilities must not repeat")
    return values


def _loopback_address(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "://" in value or "/" in value:
        raise NetworkEgressProfileError("network egress endpoint must be a literal loopback address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise NetworkEgressProfileError("network egress endpoint must be a literal loopback address") from error
    if not address.is_loopback:
        raise NetworkEgressProfileError("network egress endpoint must be loopback")
    return str(address)


def _port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise NetworkEgressProfileError("network egress endpoint port is invalid")
    return value


def _path_lock(path: Path) -> Lock:
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(path.resolve(), Lock())


def _reject_sensitive_material(value: object, *, path: str = "profile") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if (
                normalized in _SENSITIVE_KEYS
                or normalized.endswith("_secret")
                or normalized.endswith("_token")
                or normalized.startswith("secret_")
            ):
                raise NetworkEgressProfileError(f"sensitive material is forbidden at {path}.{key}")
            _reject_sensitive_material(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_material(nested, path=f"{path}[{index}]")
