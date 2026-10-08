from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.security.provider_egress import (
    DEFAULT_PROVIDER_EGRESS_PURPOSES,
    ProviderEgressError,
    ProviderEgressPolicyStore,
    build_active_provider_egress_guard,
    build_provider_egress_guard,
)


def _manifest(policy: ProviderEgressPolicyStore, endpoint: str = "https://api.example.com/v1/chat"):
    return policy.manifest(
        provider_id="example",
        endpoint=endpoint,
        purposes=("memory_candidate", "model_discovery"),
        payload_categories=("instructions", "source_excerpt"),
        max_payload_bytes=128,
    )


def test_websocket_provider_endpoint_uses_the_same_consent_boundary(tmp_path: Path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy, "wss://workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference")

    assert manifest.endpoint == "wss://workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    assert manifest.external is True


def test_fresh_external_manifest_is_local_only_and_denied_before_network(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy)

    assert manifest.external is True
    assert policy.is_consented(manifest) is False
    with pytest.raises(ProviderEgressError, match="consent_required"):
        policy.authorize(
            manifest,
            purpose="memory_candidate",
            payload_categories=("source_excerpt",),
            payload_bytes=24,
        )


def test_explicit_current_manifest_grant_allows_and_audits_without_content(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy)
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)

    lease = policy.authorize(
        manifest,
        purpose="memory_candidate",
        payload_categories=("source_excerpt",),
        payload_bytes=24,
    )
    lease.finish("succeeded")
    lease.finish("failed", error_code="must_not_append_twice")

    audit = (tmp_path / "library/global/providers/provider-egress-audit.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit.splitlines()]
    assert [record["decision"] for record in records] == ["allowed"]
    assert records[0]["payload_bytes"] == 24
    assert "source_excerpt" in records[0]["payload_categories"]
    assert "private body" not in audit
    assert "authorization" not in audit.lower()


def test_endpoint_drift_invalidates_previous_consent(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    original = _manifest(policy)
    policy.grant(original, manifest_id=original.manifest_id, confirm=True)
    drifted = _manifest(policy, "https://other.example.com/v1/chat")

    assert policy.is_consented(original) is True
    assert policy.is_consented(drifted) is False


def test_manifest_revision_category_and_budget_fail_closed(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy)
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)

    with pytest.raises(ProviderEgressError, match="purpose_not_manifested"):
        policy.authorize(manifest, purpose="vision", payload_categories=("source_excerpt",), payload_bytes=1)
    with pytest.raises(ProviderEgressError, match="payload_category_not_manifested"):
        policy.authorize(manifest, purpose="memory_candidate", payload_categories=("raw_path",), payload_bytes=1)
    with pytest.raises(ProviderEgressError, match="payload_budget_exceeded"):
        policy.authorize(manifest, purpose="memory_candidate", payload_categories=("source_excerpt",), payload_bytes=129)


def test_revoke_is_idempotent_and_blocks_new_calls(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy)
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    policy.revoke("example")
    policy.revoke("example")
    assert policy.is_consented(manifest) is False


def test_loopback_provider_is_local_and_does_not_require_external_consent(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy, "http://127.0.0.1:11434/v1/chat")
    assert manifest.external is False
    lease = policy.authorize(
        manifest,
        purpose="model_discovery",
        payload_categories=("instructions",),
        payload_bytes=0,
    )
    lease.finish("succeeded")


def test_default_manifest_authorizes_configured_ambient_loopback_transport(tmp_path) -> None:
    """The user-facing consent manifest and runtime guard share this purpose."""
    assert "companion_ambient" in DEFAULT_PROVIDER_EGRESS_PURPOSES
    guard = build_provider_egress_guard(
        tmp_path,
        provider_id="ambient-loopback",
        endpoint="http://127.0.0.1:11434/v1",
    )
    lease = guard("companion_ambient", ("instructions", "source_excerpt"), 128)
    lease.finish("succeeded")
    audit = (tmp_path / "library/global/providers/provider-egress-audit.jsonl").read_text(encoding="utf-8")
    assert '"purpose":"companion_ambient"' in audit


def test_default_manifest_includes_bounded_project_routing_purpose() -> None:
    assert "project_routing" in DEFAULT_PROVIDER_EGRESS_PURPOSES


def test_default_manifest_includes_approval_gated_document_draft_purpose() -> None:
    assert "document_draft" in DEFAULT_PROVIDER_EGRESS_PURPOSES


def test_default_manifest_includes_image_generation_purpose() -> None:
    assert "image_generation" in DEFAULT_PROVIDER_EGRESS_PURPOSES


def test_default_manifest_includes_unified_turn_search_answer_purpose() -> None:
    assert "search_answer" in DEFAULT_PROVIDER_EGRESS_PURPOSES


def test_active_provider_guard_discovery_does_not_bootstrap_registry(tmp_path) -> None:
    guard = build_active_provider_egress_guard(
        tmp_path,
        endpoint="http://127.0.0.1:11434/v1",
    )

    assert callable(guard)
    assert not (
        tmp_path / "library" / "global" / "providers" / "providers.json"
    ).exists()


def test_default_purpose_expansion_invalidates_older_manifest_consent(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    older = policy.manifest(
        provider_id="example",
        endpoint="https://api.example.com/v1/chat",
        purposes=tuple(purpose for purpose in DEFAULT_PROVIDER_EGRESS_PURPOSES if purpose != "search_answer"),
        payload_categories=("instructions", "source_excerpt"),
        max_payload_bytes=128,
    )
    policy.grant(older, manifest_id=older.manifest_id, confirm=True)
    current = policy.manifest(
        provider_id="example",
        endpoint="https://api.example.com/v1/chat",
        purposes=DEFAULT_PROVIDER_EGRESS_PURPOSES,
        payload_categories=("instructions", "source_excerpt"),
        max_payload_bytes=128,
    )

    assert current.manifest_id != older.manifest_id
    assert policy.is_consented(current) is False


@pytest.mark.parametrize(
    "endpoint",
    ["file:///tmp/model", "https://user:secret@example.com/v1", "", "javascript:alert(1)"],
)
def test_manifest_rejects_unsafe_endpoint(endpoint: str, tmp_path) -> None:
    with pytest.raises(ProviderEgressError):
        _manifest(ProviderEgressPolicyStore(tmp_path), endpoint)


def test_corrupt_policy_fails_closed(tmp_path) -> None:
    path = tmp_path / "library/global/providers/provider-egress-policy.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    policy = ProviderEgressPolicyStore(tmp_path)
    with pytest.raises(ProviderEgressError, match="unreadable"):
        policy.is_consented(_manifest(policy))
