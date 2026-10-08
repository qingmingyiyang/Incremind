from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from backend.security.secrets import InMemorySecretStore
from backend.team_memory import (
    TEAM_MEMORY_SERVICE_SECRET,
    TEAM_MEMORY_USER_SECRET,
    TeamMemoryConflict,
    TeamMemoryError,
    TeamMemoryPreflightError,
    TeamMemoryProfile,
    TeamMemoryProfileStore,
    run_team_memory_asset_inventory,
    run_team_memory_preflight,
    save_team_memory_secrets,
    serialize_team_memory_profile,
    validate_team_memory_endpoint,
)


PUBLIC_RESOLVER = lambda _host, _port: ("93.184.216.34",)


def test_profile_defaults_off_and_persists_cas_without_secrets(tmp_path) -> None:
    store = TeamMemoryProfileStore(tmp_path)
    secrets = InMemorySecretStore()

    assert serialize_team_memory_profile(store.load(), secret_store=secrets) == {
        "enabled": False,
        "endpoint": "",
        "service_id": "",
        "team_id": "",
        "agent_id": "",
        "user_id": "",
        "revision": 0,
        "schema_version": "1.0.0",
        "updated_at": "",
        "has_service_api_key": False,
        "has_user_key": False,
        "sync_available": False,
        "import_available": False,
        "export_available": False,
    }
    saved = store.save(
        enabled=False,
        endpoint="https://memory.example.test/",
        service_id="service-1",
        team_id="team-1",
        agent_id="agent-1",
        user_id="user-1",
        expected_revision=0,
        confirm_enable=False,
        secret_store=secrets,
        now=lambda: datetime(2026, 7, 23, 2, 0, tzinfo=UTC),
    )
    assert saved.revision == 1
    assert saved.endpoint == "https://memory.example.test"
    assert store.load() == saved
    with pytest.raises(TeamMemoryConflict, match="stale"):
        store.save(
            enabled=False,
            endpoint=saved.endpoint,
            service_id=saved.service_id,
            team_id=saved.team_id,
            agent_id=saved.agent_id,
            user_id=saved.user_id,
            expected_revision=0,
            confirm_enable=False,
            secret_store=secrets,
        )


def test_enable_requires_confirmation_complete_identity_and_both_secrets(tmp_path) -> None:
    store = TeamMemoryProfileStore(tmp_path)
    secrets = InMemorySecretStore()
    base = dict(
        enabled=True,
        endpoint="https://memory.example.test",
        service_id="service-1",
        team_id="team-1",
        agent_id="agent-1",
        user_id="user-1",
        expected_revision=0,
        secret_store=secrets,
    )
    with pytest.raises(TeamMemoryError, match="confirmation"):
        store.save(**base, confirm_enable=False)
    with pytest.raises(TeamMemoryError, match="service API key"):
        store.save(**base, confirm_enable=True)
    save_team_memory_secrets(secrets, service_api_key="service-secret", user_key="user-secret")
    saved = store.save(**base, confirm_enable=True)
    assert saved.enabled is True
    serialized = serialize_team_memory_profile(saved, secret_store=secrets)
    assert serialized["has_service_api_key"] is True
    assert serialized["has_user_key"] is True
    assert "service-secret" not in str(serialized)
    assert "user-secret" not in str(serialized)


def test_disconnect_requires_confirmation_and_cas_then_clears_profile_and_secrets(tmp_path) -> None:
    store = TeamMemoryProfileStore(tmp_path)
    secrets = InMemorySecretStore()
    save_team_memory_secrets(secrets, service_api_key="service-secret", user_key="user-secret")
    saved = store.save(
        enabled=True,
        endpoint="https://memory.example.test",
        service_id="service-1",
        team_id="team-1",
        agent_id="agent-1",
        user_id="user-1",
        expected_revision=0,
        confirm_enable=True,
        secret_store=secrets,
    )
    with pytest.raises(TeamMemoryError, match="confirmation"):
        store.disconnect(expected_revision=saved.revision, confirmed=False, secret_store=secrets)
    with pytest.raises(TeamMemoryConflict, match="stale"):
        store.disconnect(expected_revision=0, confirmed=True, secret_store=secrets)

    disconnected = store.disconnect(
        expected_revision=saved.revision,
        confirmed=True,
        secret_store=secrets,
        now=lambda: datetime(2026, 7, 23, 9, 0, tzinfo=UTC),
    )
    assert disconnected == TeamMemoryProfile(
        revision=2,
        updated_at="2026-07-23T09:00:00+00:00",
    )
    assert store.load() == disconnected
    assert secrets.get_snapshot(TEAM_MEMORY_SERVICE_SECRET).value == ""
    assert secrets.get_snapshot(TEAM_MEMORY_USER_SECRET).value == ""


def test_disconnect_restores_both_secrets_and_profile_when_delete_fails(tmp_path) -> None:
    class FailingDeleteStore(InMemorySecretStore):
        def replace_many(self, values) -> None:
            raise RuntimeError("injected delete failure")

    store = TeamMemoryProfileStore(tmp_path)
    secrets = FailingDeleteStore({
        TEAM_MEMORY_SERVICE_SECRET: "service-secret",
        TEAM_MEMORY_USER_SECRET: "user-secret",
    })
    saved = store.save(
        enabled=False,
        endpoint="https://memory.example.test",
        service_id="service-1",
        team_id="team-1",
        agent_id="agent-1",
        user_id="user-1",
        expected_revision=0,
        confirm_enable=False,
        secret_store=secrets,
    )
    with pytest.raises(RuntimeError, match="injected"):
        store.disconnect(expected_revision=1, confirmed=True, secret_store=secrets)
    assert store.load() == saved
    assert secrets.get_snapshot(TEAM_MEMORY_SERVICE_SECRET).value == "service-secret"
    assert secrets.get_snapshot(TEAM_MEMORY_USER_SECRET).value == "user-secret"


@pytest.mark.asyncio
async def test_asset_inventory_reads_all_four_acl_scoped_types_and_returns_only_safe_metadata() -> None:
    requests: list[httpx.Request] = []
    asset_types = ("chat_memory", "skill", "llm_wiki", "code_graph")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = __import__("json").loads(request.content)
        asset_type = body["asset_type"]
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "items": [{
                    "asset_id": f"{asset_type}-1",
                    "team_id": "team-1",
                    "asset_type": asset_type,
                    "name": f"{asset_type} sample",
                    "description": "must-not-cross",
                    "owner_user_id": "owner-must-not-cross",
                    "visibility": "restricted",
                    "status": "approved",
                    "version": 3,
                    "content_ref": "secret-content-ref",
                    "source_ref": "secret-source-ref",
                    "metadata_json": "{\"secret\":true}",
                }],
                "total": 1,
                "limit": 100,
                "offset": 0,
            },
        })

    secrets = InMemorySecretStore({
        TEAM_MEMORY_SERVICE_SECRET: "service-canary",
        TEAM_MEMORY_USER_SECRET: "user-canary",
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_team_memory_asset_inventory(
            TeamMemoryProfile(
                enabled=True,
                endpoint="https://memory.example.test",
                service_id="service-1",
                team_id="team-1",
                agent_id="agent-1",
                user_id="user-1",
                revision=1,
            ),
            secret_store=secrets,
            client=client,
            resolver=PUBLIC_RESOLVER,
        )

    assert result.counts == {asset_type: 1 for asset_type in asset_types}
    assert tuple(item["asset_type"] for item in result.items) == asset_types
    serialized = str(result)
    assert "must-not-cross" not in serialized
    assert "secret-content-ref" not in serialized
    assert "secret-source-ref" not in serialized
    assert "metadata_json" not in serialized
    assert len(requests) == 4
    for request, expected_type in zip(requests, asset_types, strict=True):
        body = __import__("json").loads(request.content)
        assert request.url.path == "/v3/meta/asset/list-accessible"
        assert request.headers["authorization"] == "Bearer service-canary"
        assert request.headers["x-tdai-service-id"] == "service-1"
        assert request.headers["x-tdai-user-key"] == "user-canary"
        assert body == {
            "user_id": "user-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "asset_type": expected_type,
            "action": "read",
            "limit": 100,
            "offset": 0,
        }


@pytest.mark.asyncio
async def test_asset_inventory_paginates_with_stable_total_and_rejects_scope_drift() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        asset_type = body["asset_type"]
        offset = body["offset"]
        offsets.append(offset)
        if asset_type == "chat_memory":
            count = 100 if offset == 0 else 1
            items = [{
                "asset_id": f"chat-{offset + index}",
                "team_id": "team-1",
                "asset_type": asset_type,
                "name": f"chat {offset + index}",
                "visibility": "team",
                "status": "approved",
                "version": 1,
            } for index in range(count)]
            return httpx.Response(200, json={"code": 0, "data": {"items": items, "total": 101}})
        wrong_team = "other-team" if asset_type == "skill" else "team-1"
        items = [] if asset_type != "skill" else [{
            "asset_id": "skill-drift",
            "team_id": wrong_team,
            "asset_type": asset_type,
            "name": "drift",
            "visibility": "team",
            "status": "approved",
            "version": 1,
        }]
        return httpx.Response(200, json={"code": 0, "data": {"items": items, "total": len(items)}})

    secrets = InMemorySecretStore({
        TEAM_MEMORY_SERVICE_SECRET: "service-canary",
        TEAM_MEMORY_USER_SECRET: "user-canary",
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TeamMemoryPreflightError, match="scope"):
            await run_team_memory_asset_inventory(
                TeamMemoryProfile(enabled=True, endpoint="https://memory.example.test", service_id="service-1", team_id="team-1", agent_id="agent-1", user_id="user-1", revision=1),
                secret_store=secrets,
                client=client,
                resolver=PUBLIC_RESOLVER,
            )
    assert offsets[:2] == [0, 100]


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "message"), (("total_drift", "changed during pagination"), ("duplicate", "duplicate asset ids")))
async def test_asset_inventory_rejects_pagination_drift_and_duplicate_ids(mode, message) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        asset_type = body["asset_type"]
        offset = body["offset"]
        if asset_type != "chat_memory":
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "total": 0}})
        if offset == 0:
            items = [{
                "asset_id": f"chat-{index}",
                "team_id": "team-1",
                "asset_type": asset_type,
                "name": f"chat {index}",
                "visibility": "team",
                "status": "approved",
                "version": 1,
            } for index in range(100)]
            return httpx.Response(200, json={"code": 0, "data": {"items": items, "total": 101}})
        item = {
            "asset_id": "chat-0" if mode == "duplicate" else "chat-100",
            "team_id": "team-1",
            "asset_type": asset_type,
            "name": "last chat",
            "visibility": "team",
            "status": "approved",
            "version": 1,
        }
        return httpx.Response(200, json={"code": 0, "data": {"items": [item], "total": 102 if mode == "total_drift" else 101}})

    secrets = InMemorySecretStore({
        TEAM_MEMORY_SERVICE_SECRET: "service-canary",
        TEAM_MEMORY_USER_SECRET: "user-canary",
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TeamMemoryPreflightError, match=message):
            await run_team_memory_asset_inventory(
                TeamMemoryProfile(enabled=True, endpoint="https://memory.example.test", service_id="service-1", team_id="team-1", agent_id="agent-1", user_id="user-1", revision=1),
                secret_store=secrets,
                client=client,
                resolver=PUBLIC_RESOLVER,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ({"code": 0, "data": {"items": [], "total": 1001}}, "item limit"),
        ({"code": 0, "data": {"items": [], "total": 1}}, "ended early"),
        ({"code": 2, "data": {"items": [], "total": 0}}, "envelope"),
        ({"code": 0, "data": {"items": "bad", "total": 0}}, "page"),
    ),
)
async def test_asset_inventory_fails_closed_on_invalid_pagination(payload, message) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    secrets = InMemorySecretStore({
        TEAM_MEMORY_SERVICE_SECRET: "service-canary",
        TEAM_MEMORY_USER_SECRET: "user-canary",
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TeamMemoryPreflightError, match=message):
            await run_team_memory_asset_inventory(
                TeamMemoryProfile(enabled=True, endpoint="https://memory.example.test", service_id="service-1", team_id="team-1", agent_id="agent-1", user_id="user-1", revision=1),
                secret_store=secrets,
                client=client,
                resolver=PUBLIC_RESOLVER,
            )


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://memory.example.test",
        "https://user:pass@memory.example.test",
        "https://memory.example.test/path",
        "https://memory.example.test?token=secret",
        "https://memory.example.test/#fragment",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://169.254.169.254",
        "https://[::1]",
    ),
)
def test_endpoint_rejects_unsafe_or_non_origin_values(endpoint: str) -> None:
    with pytest.raises(TeamMemoryError):
        validate_team_memory_endpoint(endpoint, resolve_dns=False)


def test_endpoint_rejects_dns_rebinding_to_private_address() -> None:
    with pytest.raises(TeamMemoryError, match="non-public"):
        validate_team_memory_endpoint(
            "https://memory.example.test",
            resolve_dns=True,
            resolver=lambda _host, _port: ("93.184.216.34", "127.0.0.1"),
        )


@pytest.mark.asyncio
async def test_preflight_verifies_health_auth_headers_identity_and_marks_non_mutating_capabilities() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "2.0.0-beta.1", "uptime": 1, "stores": {}})
        assert request.url.path == "/v3/meta/auth/verify"
        return httpx.Response(200, json={"code": 0, "data": {"valid": True, "user": {"user_id": "user-1"}}})

    secrets = InMemorySecretStore()
    save_team_memory_secrets(secrets, service_api_key="service-secret", user_key="user-secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        result = await run_team_memory_preflight(
            TeamMemoryProfile(
                enabled=True,
                endpoint="https://memory.example.test",
                service_id="service-1",
                team_id="team-1",
                agent_id="agent-1",
                user_id="user-1",
                revision=1,
            ),
            secret_store=secrets,
            client=client,
            resolver=PUBLIC_RESOLVER,
        )
    assert result.status == "compatible_read_only_preflight"
    assert result.server_health == "ok"
    assert result.server_version == "2.0.0-beta.1"
    assert dict((item["capability"], item["state"]) for item in result.capabilities) == {
        "health": "verified",
        "auth_verify": "verified",
        "asset_acl": "advertised_unverified",
        "skill_expected_version": "advertised_unverified",
        "knowledge_tools": "advertised_unverified",
        "asset_sync": "disabled",
    }
    auth_request = seen[1]
    assert auth_request.headers["authorization"] == "Bearer service-secret"
    assert auth_request.headers["x-tdai-service-id"] == "service-1"
    assert auth_request.content == b'{"user_key":"user-secret"}'


@pytest.mark.asyncio
async def test_preflight_preserves_degraded_health_in_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "degraded", "version": "2.0.0-beta.1"})
        return httpx.Response(200, json={"code": 0, "data": {"valid": True, "user": {"user_id": "user-1"}}})

    secrets = InMemorySecretStore({TEAM_MEMORY_SERVICE_SECRET: "service-canary", TEAM_MEMORY_USER_SECRET: "user-canary"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        result = await run_team_memory_preflight(
            TeamMemoryProfile(enabled=True, endpoint="https://memory.example.test", service_id="service-1", team_id="team-1", agent_id="agent-1", user_id="user-1", revision=1),
            secret_store=secrets,
            client=client,
            resolver=PUBLIC_RESOLVER,
        )
    assert result.status == "degraded_read_only_preflight"
    assert result.server_health == "degraded"


@pytest.mark.asyncio
async def test_preflight_rejects_profile_revision_drift_before_secret_injection() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "version": "2.0.0-beta.1"})

    revisions = iter((1, 2))
    secrets = InMemorySecretStore(
        {TEAM_MEMORY_SERVICE_SECRET: "service-canary", TEAM_MEMORY_USER_SECRET: "user-canary"}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(TeamMemoryPreflightError, match="credential authorization failed"):
            await run_team_memory_preflight(
                TeamMemoryProfile(
                    enabled=True,
                    endpoint="https://memory.example.test",
                    service_id="service-1",
                    team_id="team-1",
                    agent_id="agent-1",
                    user_id="user-1",
                    revision=1,
                ),
                secret_store=secrets,
                client=client,
                resolver=PUBLIC_RESOLVER,
                boundary_revision_reader=lambda: next(revisions),
            )
    assert seen_paths == ["/health"]


def test_secret_pair_rolls_back_when_second_write_fails() -> None:
    class FailingStore(InMemorySecretStore):
        def replace_many(self, values) -> None:
            raise RuntimeError("injected write failure")

    secrets = FailingStore({TEAM_MEMORY_SERVICE_SECRET: "old-service", TEAM_MEMORY_USER_SECRET: "old-user"})
    with pytest.raises(RuntimeError, match="injected"):
        save_team_memory_secrets(secrets, service_api_key="new-service-secret", user_key="new-user-secret")
    assert secrets.get_snapshot(TEAM_MEMORY_SERVICE_SECRET).value == "old-service"
    assert secrets.get_snapshot(TEAM_MEMORY_USER_SECRET).value == "old-user"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("redirect", "oversize", "identity", "invalid"))
async def test_preflight_fails_closed_without_exposing_secrets(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://attacker.invalid"})
        if request.url.path == "/health":
            if failure == "oversize":
                return httpx.Response(200, content=b"x" * (64 * 1024 + 1))
            return httpx.Response(200, json={"status": "ok", "version": "2.0.0-beta.1"})
        if failure == "identity":
            return httpx.Response(200, json={"code": 0, "data": {"valid": True, "user": {"user_id": "other-user"}}})
        return httpx.Response(200, json={"code": 1, "data": {"valid": False}})

    secrets = InMemorySecretStore({TEAM_MEMORY_SERVICE_SECRET: "service-canary", TEAM_MEMORY_USER_SECRET: "user-canary"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(TeamMemoryPreflightError) as captured:
            await run_team_memory_preflight(
                TeamMemoryProfile(
                    enabled=True,
                    endpoint="https://memory.example.test",
                    service_id="service-1",
                    team_id="team-1",
                    agent_id="agent-1",
                    user_id="user-1",
                    revision=1,
                ),
                secret_store=secrets,
                client=client,
                resolver=PUBLIC_RESOLVER,
            )
    assert "service-canary" not in str(captured.value)
    assert "user-canary" not in str(captured.value)
