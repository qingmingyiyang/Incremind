from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend.shared.filesystem import atomic_write_text
from backend.security.file_attribution import file_attribution
from backend.security.secret_egress import SecretEgressBroker, SecretEgressError


TEAM_MEMORY_SERVICE_SECRET = "team-memory:service-api-key"
TEAM_MEMORY_USER_SECRET = "team-memory:user-key"
TEAM_MEMORY_SCHEMA_VERSION = "1.0.0"
TEAM_MEMORY_MAX_RESPONSE_BYTES = 64 * 1024
TEAM_MEMORY_TIMEOUT_SECONDS = 8.0
TEAM_MEMORY_ASSET_TYPES = ("chat_memory", "skill", "llm_wiki", "code_graph")
TEAM_MEMORY_ASSET_PAGE_SIZE = 100
TEAM_MEMORY_ASSET_MAX_ITEMS = 1000
TEAM_MEMORY_ASSET_VISIBILITIES = frozenset({"private", "team", "restricted", "agent", "task"})
TEAM_MEMORY_ASSET_STATUSES = frozenset({"draft", "candidate", "approved", "deprecated", "archived", "failed"})


class TeamMemoryError(ValueError):
    pass


class TeamMemoryConflict(TeamMemoryError):
    pass


class TeamMemoryPreflightError(TeamMemoryError):
    pass


class SecretStorePort(Protocol):
    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def has_secret(self, key: str) -> bool: ...

    def get_generation(self, key: str) -> int: ...

    def get_snapshot(self, key: str): ...

    def replace_many(self, values: Mapping[str, str | None]) -> None: ...


@dataclass(frozen=True, slots=True)
class TeamMemoryProfile:
    enabled: bool = False
    endpoint: str = ""
    service_id: str = ""
    team_id: str = ""
    agent_id: str = ""
    user_id: str = ""
    revision: int = 0
    schema_version: str = TEAM_MEMORY_SCHEMA_VERSION
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class TeamMemoryPreflightResult:
    status: str
    endpoint_origin: str
    server_health: str
    server_version: str
    resolved_user_id: str
    capabilities: tuple[Mapping[str, str], ...]


@dataclass(frozen=True, slots=True)
class TeamMemoryAssetInventoryResult:
    endpoint_origin: str
    team_id: str
    agent_id: str
    user_id: str
    counts: Mapping[str, int]
    items: tuple[Mapping[str, object], ...]


class TeamMemoryProfileStore:
    def __init__(self, root_dir: Path) -> None:
        self._path = root_dir / "library" / "global" / "team-memory" / "profile.json"
        self._mutation_attribution = file_attribution(root_dir, 'team-profile')

    def _write(self,result):
        value=asdict(result)
        if self._mutation_attribution is not None:
            previous=json.loads(self._path.read_text(encoding='utf8')) if self._path.exists() else {}
            value=self._mutation_attribution.json(value,previous)
        self._path.parent.mkdir(parents=True,exist_ok=True)
        atomic_write_text(self._path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')

    def load(self) -> TeamMemoryProfile:
        if not self._path.is_file():
            return TeamMemoryProfile()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TeamMemoryError("team memory profile is unreadable") from error
        if not isinstance(payload, Mapping):
            raise TeamMemoryError("team memory profile must be an object")
        return _profile_from_payload(payload)

    def save(
        self,
        *,
        enabled: bool,
        endpoint: str,
        service_id: str,
        team_id: str,
        agent_id: str,
        user_id: str,
        expected_revision: int,
        confirm_enable: bool,
        secret_store: SecretStorePort,
        now: Callable[[], datetime] | None = None,
    ) -> TeamMemoryProfile:
        current = self.load()
        if expected_revision != current.revision:
            raise TeamMemoryConflict("team memory profile revision is stale")
        if enabled and not confirm_enable:
            raise TeamMemoryError("enabling team memory requires explicit confirmation")
        normalized_endpoint = validate_team_memory_endpoint(endpoint, resolve_dns=False) if endpoint.strip() else ""
        fields = {
            "service_id": _identifier(service_id, "service_id"),
            "team_id": _identifier(team_id, "team_id"),
            "agent_id": _identifier(agent_id, "agent_id"),
            "user_id": _identifier(user_id, "user_id"),
        }
        if enabled:
            if not normalized_endpoint or not all(fields.values()):
                raise TeamMemoryError("enabled team memory requires endpoint and stable identities")
            if not secret_store.has_secret(TEAM_MEMORY_SERVICE_SECRET):
                raise TeamMemoryError("enabled team memory requires service API key")
            if not secret_store.has_secret(TEAM_MEMORY_USER_SECRET):
                raise TeamMemoryError("enabled team memory requires user key")
        timestamp = (now or (lambda: datetime.now(UTC)))().astimezone(UTC).isoformat(timespec="seconds")
        result = TeamMemoryProfile(
            enabled=enabled,
            endpoint=normalized_endpoint,
            revision=current.revision + 1,
            updated_at=timestamp,
            **fields,
        )
        self._write(result)
        return result

    def disconnect(
        self,
        *,
        expected_revision: int,
        confirmed: bool,
        secret_store: SecretStorePort,
        now: Callable[[], datetime] | None = None,
    ) -> TeamMemoryProfile:
        current = self.load()
        if expected_revision != current.revision:
            raise TeamMemoryConflict("team memory profile revision is stale")
        if confirmed is not True:
            raise TeamMemoryError("disconnecting team memory requires explicit confirmation")
        timestamp = (now or (lambda: datetime.now(UTC)))().astimezone(UTC).isoformat(timespec="seconds")
        result = TeamMemoryProfile(
            revision=current.revision + 1,
            updated_at=timestamp,
        )
        secret_store.replace_many({
            TEAM_MEMORY_SERVICE_SECRET: None,
            TEAM_MEMORY_USER_SECRET: None,
        })
        self._write(result)
        return result


def save_team_memory_secrets(
    secret_store: SecretStorePort,
    *,
    service_api_key: str,
    user_key: str,
) -> None:
    service_secret = service_api_key.strip()
    user_secret = user_key.strip()
    if len(service_secret) < 8 or len(user_secret) < 8:
        raise TeamMemoryError("team memory secrets must each contain at least 8 characters")
    secret_store.replace_many({
        TEAM_MEMORY_SERVICE_SECRET: service_secret,
        TEAM_MEMORY_USER_SECRET: user_secret,
    })


def serialize_team_memory_profile(
    profile: TeamMemoryProfile,
    *,
    secret_store: SecretStorePort,
) -> dict[str, object]:
    return {
        **asdict(profile),
        "has_service_api_key": secret_store.has_secret(TEAM_MEMORY_SERVICE_SECRET),
        "has_user_key": secret_store.has_secret(TEAM_MEMORY_USER_SECRET),
        "sync_available": False,
        "import_available": False,
        "export_available": False,
    }


def _team_memory_wire_credentials(
    profile: TeamMemoryProfile, *, secret_store: SecretStorePort,
    origin: str, purpose: str, boundary_revision_reader: Callable[[], int],
) -> tuple[dict[str, str], str]:
    project_id = f"team-memory:{profile.team_id}"
    revision = f"profile:{profile.revision}"
    host = urlsplit(origin).hostname
    if not host:
        raise TeamMemoryPreflightError("team memory endpoint host is unavailable")
    broker = SecretEgressBroker(
        secret_store,
        boundary_revision_reader=lambda project: (
            f"profile:{boundary_revision_reader()}" if project == project_id else "denied"
        ),
    )
    def materialize(secret_ref: str) -> str:
        lease = broker.grant(
            project_id=project_id, secret_ref=secret_ref, purpose=purpose,
            allowed_hosts=(host,), boundary_revision=revision, ttl_seconds=30,
        )
        try:
            return broker.materialize_for_sdk(
                lease, project_id=project_id, purpose=purpose,
                boundary_revision=revision, url=origin,
            )
        finally:
            broker.revoke(lease.lease_id)
    try:
        return (
            {"Authorization": f"Bearer {materialize(TEAM_MEMORY_SERVICE_SECRET)}"},
            materialize(TEAM_MEMORY_USER_SECRET),
        )
    except SecretEgressError as error:
        raise TeamMemoryPreflightError("team memory credential authorization failed") from error


async def run_team_memory_preflight(
    profile: TeamMemoryProfile,
    *,
    secret_store: SecretStorePort,
    client: httpx.AsyncClient | None = None,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
    boundary_revision_reader: Callable[[], int] | None = None,
) -> TeamMemoryPreflightResult:
    if not profile.enabled:
        raise TeamMemoryPreflightError("team memory profile is disabled")
    origin = validate_team_memory_endpoint(profile.endpoint, resolve_dns=True, resolver=resolver)
    if not secret_store.has_secret(TEAM_MEMORY_SERVICE_SECRET) or not secret_store.has_secret(TEAM_MEMORY_USER_SECRET):
        raise TeamMemoryPreflightError("team memory credentials are unavailable")
    owned_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=TEAM_MEMORY_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    )
    try:
        health = await _request_json(active_client, "GET", f"{origin}/health")
        server_health, server_version = _validate_health(health)
        headers, user_key = _team_memory_wire_credentials(
            profile, secret_store=secret_store, origin=origin, purpose="team_memory_preflight",
            boundary_revision_reader=boundary_revision_reader or (lambda: profile.revision),
        )
        auth = await _request_json(
            active_client,
            "POST",
            f"{origin}/v3/meta/auth/verify",
            headers={**headers, "x-tdai-service-id": profile.service_id, "Content-Type": "application/json"},
            json_body={"user_key": user_key},
        )
        resolved_user_id = _validate_auth(auth, expected_user_id=profile.user_id)
    except TeamMemoryPreflightError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise TeamMemoryPreflightError("team memory preflight failed") from error
    finally:
        if owned_client:
            await active_client.aclose()
    capabilities = (
        {"capability": "health", "state": "verified"},
        {"capability": "auth_verify", "state": "verified"},
        {"capability": "asset_acl", "state": "advertised_unverified"},
        {"capability": "skill_expected_version", "state": "advertised_unverified"},
        {"capability": "knowledge_tools", "state": "advertised_unverified"},
        {"capability": "asset_sync", "state": "disabled"},
    )
    return TeamMemoryPreflightResult(
        status=(
            "compatible_read_only_preflight"
            if server_health == "ok"
            else "degraded_read_only_preflight"
        ),
        endpoint_origin=origin,
        server_health=server_health,
        server_version=server_version,
        resolved_user_id=resolved_user_id,
        capabilities=capabilities,
    )


async def run_team_memory_asset_inventory(
    profile: TeamMemoryProfile,
    *,
    secret_store: SecretStorePort,
    client: httpx.AsyncClient | None = None,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
    boundary_revision_reader: Callable[[], int] | None = None,
) -> TeamMemoryAssetInventoryResult:
    if not profile.enabled:
        raise TeamMemoryPreflightError("team memory profile is disabled")
    origin = validate_team_memory_endpoint(profile.endpoint, resolve_dns=True, resolver=resolver)
    if not secret_store.has_secret(TEAM_MEMORY_SERVICE_SECRET) or not secret_store.has_secret(TEAM_MEMORY_USER_SECRET):
        raise TeamMemoryPreflightError("team memory credentials are unavailable")
    if not all((profile.service_id, profile.team_id, profile.agent_id, profile.user_id)):
        raise TeamMemoryPreflightError("team memory stable identities are unavailable")

    owned_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=TEAM_MEMORY_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    )
    injected, user_key = _team_memory_wire_credentials(
        profile, secret_store=secret_store, origin=origin, purpose="team_memory_inventory",
        boundary_revision_reader=boundary_revision_reader or (lambda: profile.revision),
    )
    headers = {
        **injected,
        "x-tdai-service-id": profile.service_id,
        "x-tdai-user-key": user_key,
        "Content-Type": "application/json",
    }
    counts: dict[str, int] = {}
    safe_items: list[Mapping[str, object]] = []
    seen_asset_ids: set[str] = set()
    try:
        for asset_type in TEAM_MEMORY_ASSET_TYPES:
            items = await _list_accessible_assets(
                active_client,
                origin=origin,
                headers=headers,
                profile=profile,
                asset_type=asset_type,
                remaining_budget=TEAM_MEMORY_ASSET_MAX_ITEMS - len(safe_items),
            )
            counts[asset_type] = len(items)
            for item in items:
                asset_id = str(item["asset_id"])
                if asset_id in seen_asset_ids:
                    raise TeamMemoryPreflightError("team memory asset inventory contains duplicate asset ids")
                seen_asset_ids.add(asset_id)
                safe_items.append(item)
    except (httpx.HTTPError, ValueError, TypeError) as error:
        if isinstance(error, TeamMemoryPreflightError):
            raise
        raise TeamMemoryPreflightError("team memory asset inventory failed") from error
    finally:
        if owned_client:
            await active_client.aclose()
    return TeamMemoryAssetInventoryResult(
        endpoint_origin=origin,
        team_id=profile.team_id,
        agent_id=profile.agent_id,
        user_id=profile.user_id,
        counts=counts,
        items=tuple(safe_items),
    )


async def _list_accessible_assets(
    client: httpx.AsyncClient,
    *,
    origin: str,
    headers: Mapping[str, str],
    profile: TeamMemoryProfile,
    asset_type: str,
    remaining_budget: int,
) -> list[Mapping[str, object]]:
    if remaining_budget < 0:
        raise TeamMemoryPreflightError("team memory asset inventory exceeds the item limit")
    offset = 0
    expected_total: int | None = None
    results: list[Mapping[str, object]] = []
    while True:
        payload = await _request_json(
            client,
            "POST",
            f"{origin}/v3/meta/asset/list-accessible",
            headers=headers,
            json_body={
                "user_id": profile.user_id,
                "team_id": profile.team_id,
                "agent_id": profile.agent_id,
                "asset_type": asset_type,
                "action": "read",
                "limit": TEAM_MEMORY_ASSET_PAGE_SIZE,
                "offset": offset,
            },
        )
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, Mapping):
            raise TeamMemoryPreflightError("team memory asset inventory envelope is invalid")
        total = data.get("total")
        page = data.get("items")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise TeamMemoryPreflightError("team memory asset inventory total is invalid")
        if total > remaining_budget:
            raise TeamMemoryPreflightError("team memory asset inventory exceeds the item limit")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise TeamMemoryPreflightError("team memory asset inventory changed during pagination")
        if not isinstance(page, list) or len(page) > TEAM_MEMORY_ASSET_PAGE_SIZE:
            raise TeamMemoryPreflightError("team memory asset inventory page is invalid")
        if not page and len(results) < total:
            raise TeamMemoryPreflightError("team memory asset inventory pagination ended early")
        for raw in page:
            results.append(_safe_asset_summary(raw, profile=profile, expected_type=asset_type))
        if len(results) > total:
            raise TeamMemoryPreflightError("team memory asset inventory exceeds the declared total")
        if len(results) == total:
            return results
        offset += len(page)


def _safe_asset_summary(
    raw: object,
    *,
    profile: TeamMemoryProfile,
    expected_type: str,
) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise TeamMemoryPreflightError("team memory asset inventory item is invalid")
    asset_id = _identifier(str(raw.get("asset_id") or ""), "asset_id")
    team_id = _identifier(str(raw.get("team_id") or ""), "team_id")
    asset_type = str(raw.get("asset_type") or "")
    name = raw.get("name")
    visibility = str(raw.get("visibility") or "")
    status = str(raw.get("status") or "")
    version = raw.get("version")
    if not asset_id or team_id != profile.team_id or asset_type != expected_type:
        raise TeamMemoryPreflightError("team memory asset inventory scope is invalid")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
        raise TeamMemoryPreflightError("team memory asset inventory name is invalid")
    if visibility not in TEAM_MEMORY_ASSET_VISIBILITIES or status not in TEAM_MEMORY_ASSET_STATUSES:
        raise TeamMemoryPreflightError("team memory asset inventory state is invalid")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise TeamMemoryPreflightError("team memory asset inventory version is invalid")
    return {
        "asset_id": asset_id,
        "asset_type": asset_type,
        "name": name.strip(),
        "visibility": visibility,
        "status": status,
        "version": version,
    }


def validate_team_memory_endpoint(
    endpoint: str,
    *,
    resolve_dns: bool,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
) -> str:
    value = endpoint.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
    except ValueError as error:
        raise TeamMemoryError("team memory endpoint is invalid") from error
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise TeamMemoryError("team memory endpoint must use https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TeamMemoryError("team memory endpoint must not contain credentials, query or fragment")
    if parsed.path not in {"", "/"}:
        raise TeamMemoryError("team memory endpoint must be an origin without a path")
    host = parsed.hostname.lower().rstrip(".")
    _reject_unsafe_ip(host)
    if resolve_dns:
        addresses = tuple((resolver or _resolve_host)(host, port))
        if not addresses:
            raise TeamMemoryError("team memory endpoint DNS resolution returned no addresses")
        for address in addresses:
            _reject_unsafe_ip(address)
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    if port == 443:
        netloc = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", netloc, "", "", ""))


def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


def _reject_unsafe_ip(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if value in {"localhost", "localhost.localdomain"}:
            raise TeamMemoryError("team memory endpoint must not target localhost")
        return
    if not address.is_global:
        raise TeamMemoryError("team memory endpoint resolved to a non-public address")


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    response = await client.request(method, url, headers=headers, json=json_body)
    if 300 <= response.status_code < 400:
        raise TeamMemoryPreflightError("team memory preflight rejected redirect")
    if response.status_code < 200 or response.status_code >= 300:
        raise TeamMemoryPreflightError("team memory preflight remote status rejected")
    if len(response.content) > TEAM_MEMORY_MAX_RESPONSE_BYTES:
        raise TeamMemoryPreflightError("team memory preflight response is too large")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise TeamMemoryPreflightError("team memory preflight response must be an object")
    return payload


def _validate_health(payload: Mapping[str, object]) -> tuple[str, str]:
    status = payload.get("status")
    if status not in {"ok", "degraded"}:
        raise TeamMemoryPreflightError("team memory health status is invalid")
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise TeamMemoryPreflightError("team memory health version is missing")
    return str(status), version.strip()[:80]


def _validate_auth(payload: Mapping[str, object], *, expected_user_id: str) -> str:
    data = payload.get("data")
    user = data.get("user") if isinstance(data, Mapping) else None
    resolved = user.get("user_id") if isinstance(user, Mapping) else None
    if payload.get("code") != 0 or not isinstance(data, Mapping) or data.get("valid") is not True:
        raise TeamMemoryPreflightError("team memory auth verification failed")
    if not isinstance(resolved, str) or resolved != expected_user_id:
        raise TeamMemoryPreflightError("team memory auth identity mismatch")
    return resolved


def _profile_from_payload(payload: Mapping[str, object]) -> TeamMemoryProfile:
    if payload.get("schema_version") != TEAM_MEMORY_SCHEMA_VERSION:
        raise TeamMemoryError("team memory profile schema version is unsupported")
    revision = payload.get("revision")
    enabled = payload.get("enabled")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise TeamMemoryError("team memory profile revision is invalid")
    if not isinstance(enabled, bool):
        raise TeamMemoryError("team memory profile enabled flag is invalid")
    endpoint = str(payload.get("endpoint") or "")
    if endpoint:
        endpoint = validate_team_memory_endpoint(endpoint, resolve_dns=False)
    return TeamMemoryProfile(
        enabled=enabled,
        endpoint=endpoint,
        service_id=_identifier(str(payload.get("service_id") or ""), "service_id"),
        team_id=_identifier(str(payload.get("team_id") or ""), "team_id"),
        agent_id=_identifier(str(payload.get("agent_id") or ""), "agent_id"),
        user_id=_identifier(str(payload.get("user_id") or ""), "user_id"),
        revision=revision,
        schema_version=TEAM_MEMORY_SCHEMA_VERSION,
        updated_at=str(payload.get("updated_at") or ""),
    )


def _identifier(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if len(normalized) > 128 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in normalized):
        raise TeamMemoryError(f"{field_name} is invalid")
    return normalized
