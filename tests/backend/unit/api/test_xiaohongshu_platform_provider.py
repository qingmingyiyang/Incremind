from __future__ import annotations

import json
from dataclasses import replace

import pytest

from backend.api.analyze_source_ai_runtime import (
    AnalyzeSourceCapability,
    AnalyzeSourceReadiness,
    ArtifactSourceManifestResolver,
    PlatformArtifactSourceManifestResolver,
)
from backend.api.source_resolution_evidence import SourceResolutionEvidenceRepository
from backend.api.xiaohongshu_platform_provider import (
    XiaohongshuAnonymousMetadataPlatformProvider,
    XiaohongshuControlledMetadataPlatformProvider,
    XiaohongshuMetadataProviderError,
    verify_xiaohongshu_frozen_manifest,
)
from backend.api.xiaohongshu_controlled_credential_runtime import (
    SafeControlledCookieTextNetworkAdapter,
    XiaohongshuControlledCredentialRuntime,
    XiaohongshuControlledCredentialRuntimeError,
)
from backend.security.network_adapter import (
    BoundedHttpResponse,
    PinnedHttpRequest,
    SafeTextNetworkAdapter,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.secrets import InMemorySecretStore
from backend.security.xiaohongshu_controlled_credentials import (
    XiaohongshuControlledCredentialAuthority,
)
from core.source_processing import ManifestPermission, MediaRouter, SourceManifestCodec
from core.ai_kernel import InMemoryTurnPayloadStore
from core.source_processing import (
    PlatformResolver,
    SourceManifestArtifactRepository,
    SourcePermissionAuthority,
)
from core.storage_provider import JsonObjectStore


PUBLIC_IP = "93.184.216.34"
NOTE_ID = "65f1234567890abc12345678"


def _note(kind: str) -> dict[str, object]:
    base: dict[str, object] = {
        "noteId": NOTE_ID,
        "type": kind,
        "title": "公开标题",
        "desc": "公开正文",
        "time": 1_725_638_400_000,
        "user": {"userId": "private-user-canary", "nickname": "private-name"},
        "cookie": "private-cookie-canary",
        "xsec_token": "private-token-canary",
    }
    if kind == "image":
        base["imageList"] = [
            {"urlDefault": "https://private-cdn.example/image-1?token=secret"},
            {"urlDefault": "https://private-cdn.example/image-2?token=secret"},
        ]
    elif kind == "video":
        base["video"] = {"masterUrl": "https://private-cdn.example/video?token=secret"}
        base["imageList"] = [{"urlDefault": "https://private-cdn.example/cover"}]
    elif kind == "mixed":
        base["mediaList"] = [
            {"type": "image", "url": "https://private-cdn.example/1"},
            {"type": "video", "url": "https://private-cdn.example/2"},
            {"type": "image", "url": "https://private-cdn.example/3"},
        ]
    return base


def _html(kind: str, *, note: dict[str, object] | None = None) -> str:
    payload = {"note": {"noteDetailMap": {NOTE_ID: {"note": note or _note(kind)}}}}
    return (
        "<!doctype html><html><head></head><body>"
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


def _initial_state_html(kind: str) -> str:
    payload = json.dumps(
        {"note": {"noteDetailMap": {NOTE_ID: {"note": _note(kind)}}}},
        ensure_ascii=False,
    )
    payload = payload[:-1] + ',"optional":undefined}'
    return f"<script>window.__INITIAL_STATE__={payload}</script>"


def _provider(tmp_path, *, html: str, requests=None):
    recorded = requests if requests is not None else []

    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        recorded.append(request)
        return BoundedHttpResponse(
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            html.encode("utf-8"),
        )

    network = SafeTextNetworkAdapter(
        resolver=lambda host, port: (PUBLIC_IP,),
        transport=transport,
        allowed_hosts=("xiaohongshu.com", "www.xiaohongshu.com"),
        max_redirects=0,
        max_response_bytes=128 * 1024,
    )
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    return (
        XiaohongshuAnonymousMetadataPlatformProvider(
            network=network,
            evidence=SourceResolutionEvidenceRepository(store, namespace_id="default"),
            namespace_id="default",
        ),
        store,
        recorded,
    )


def _controlled_provider(tmp_path, *, html: str, requests=None):
    recorded = requests if requests is not None else []
    secrets = InMemorySecretStore()
    authority = XiaohongshuControlledCredentialAuthority(tmp_path, secret_store=secrets)
    authority.grant(
        project_id="project-1",
        credential_subject_id="account-1",
        boundary_profile_id="project-boundary-project-1",
        boundary_revision=1,
        expires_at="2030-01-02T03:04:05Z",
        cookie_value="xhs-private-cookie-canary",
        expected_authorization_revision=0,
        command_id="grant-1",
    )

    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        recorded.append(request)
        return BoundedHttpResponse(200, {"Content-Type": "text/html; charset=utf-8"}, html.encode("utf-8"))

    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    provider = XiaohongshuControlledMetadataPlatformProvider(
        runtime=XiaohongshuControlledCredentialRuntime(
            authority=authority, boundary_profiles=ProjectBoundaryProfileStore(tmp_path),
        ),
        network=SafeControlledCookieTextNetworkAdapter(
            resolver=lambda _host, _port: (PUBLIC_IP,), transport=transport,
        ),
        evidence=SourceResolutionEvidenceRepository(store, namespace_id="default"),
        namespace_id="default",
    )
    return provider, authority, secrets, store, recorded


def test_provider_accepts_current_initial_state_ssr_payload(tmp_path) -> None:
    provider, _store, _requests = _provider(
        tmp_path, html=_initial_state_html("image")
    )

    manifest = provider.provide(
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}", project_id="project-1"
    )

    assert manifest.content_kind == "image_set"
    assert [asset.kind for asset in manifest.assets] == ["image", "image"]


def test_controlled_provider_injects_cookie_only_at_wire_and_freezes_nonsecret_binding(tmp_path) -> None:
    provider, _authority, _secrets, store, requests = _controlled_provider(tmp_path, html=_html("image"))

    manifest = provider.provide_controlled(
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
        project_id="project-1", credential_subject_id="account-1",
    )

    assert manifest.schema_version == "1.1.0"
    assert manifest.credential_binding is not None
    assert manifest.credential_binding.credential_subject_id == "account-1"
    assert manifest.credential_binding.secret_generation == 1
    assert requests[0].headers["Cookie"] == "xhs-private-cookie-canary"
    assert requests[0].headers["Accept"].startswith("text/html")
    persisted = json.dumps(store.list(SourceResolutionEvidenceRepository.collection), ensure_ascii=False)
    for value in ("xhs-private-cookie-canary", "Cookie"):
        assert value not in persisted
    assert "xhs-private-cookie-canary" not in repr(manifest)


def test_controlled_provider_stops_pre_wire_on_rotation_and_marks_post_wire_drift_unknown(tmp_path) -> None:
    provider, authority, _secrets, _store, requests = _controlled_provider(tmp_path, html=_html("image"))
    binding = provider.runtime.current_binding(project_id="project-1", credential_subject_id="account-1")
    authority.rotate(
        project_id="project-1", credential_subject_id="account-1",
        boundary_profile_id="project-boundary-project-1", boundary_revision=1,
        expires_at="2030-01-02T03:04:05Z", cookie_value="rotated-cookie-canary",
        expected_authorization_revision=1, command_id="rotate-pre",
    )
    with pytest.raises(XiaohongshuControlledCredentialRuntimeError, match="controlled_credential_pre_wire_drift"):
        provider.runtime.fetch_text(
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}", project_id="project-1",
            binding=binding, network=provider.network,
        )
    assert requests == []

    # A fresh provider isolates the second race.  Rotation after the request
    # begins cannot be called a safe retry: the original wire may have reached
    # the platform, so it is terminal unknown and only one request is sent.
    provider, authority, _secrets, _store, requests = _controlled_provider(tmp_path / "post", html=_html("image"))
    original_transport = provider.network.transport

    def rotate_after_wire(request: PinnedHttpRequest) -> BoundedHttpResponse:
        authority.rotate(
            project_id="project-1", credential_subject_id="account-1",
            boundary_profile_id="project-boundary-project-1", boundary_revision=1,
            expires_at="2030-01-02T03:04:05Z", cookie_value="rotated-cookie-canary",
            expected_authorization_revision=1, command_id="rotate-post",
        )
        assert original_transport is not None
        return original_transport(request)

    provider = XiaohongshuControlledMetadataPlatformProvider(
        runtime=provider.runtime,
        network=SafeControlledCookieTextNetworkAdapter(
            resolver=provider.network.resolver, transport=rotate_after_wire,
        ), evidence=provider.evidence, namespace_id=provider.namespace_id,
    )
    with pytest.raises(XiaohongshuMetadataProviderError, match="controlled_credential_post_wire_unknown"):
        provider.provide_controlled(
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
            project_id="project-1", credential_subject_id="account-1",
        )
    assert len(requests) == 1
    assert requests[0].headers["Cookie"] == "xhs-private-cookie-canary"


def test_controlled_provider_reports_revocation_as_explicit_pre_wire_drift(tmp_path) -> None:
    provider, authority, _secrets, _store, requests = _controlled_provider(
        tmp_path, html=_html("image")
    )
    authority.revoke(
        project_id="project-1",
        credential_subject_id="account-1",
        expected_authorization_revision=1,
        command_id="revoke-before-resolve",
    )

    with pytest.raises(
        XiaohongshuMetadataProviderError,
        match="controlled_credential_pre_wire_drift",
    ):
        provider.provide_controlled(
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
            project_id="project-1",
            credential_subject_id="account-1",
        )

    assert requests == []


def test_frozen_manifest_comparison_retains_controlled_credential_binding(tmp_path) -> None:
    provider, _authority, _secrets, _store, _requests = _controlled_provider(tmp_path, html=_html("image"))
    manifest = provider.provide_controlled(
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
        project_id="project-1", credential_subject_id="account-1",
    )
    granted = replace(
        manifest,
        permission=ManifestPermission("granted", manifest.permission.evidence_refs),
    )

    note_id, locators = verify_xiaohongshu_frozen_manifest(granted, _note("image"))

    assert note_id == NOTE_ID
    assert len(locators) == 2


def test_frozen_manifest_accepts_canonical_source_permission_derivative(tmp_path) -> None:
    provider, _authority, _secrets, _store, _requests = _controlled_provider(
        tmp_path, html=_html("image")
    )
    manifest = provider.provide_controlled(
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
        project_id="project-1",
        credential_subject_id="account-1",
    )
    granted = replace(
        manifest,
        normalizer_revision=f"{manifest.normalizer_revision}-permission-r1",
        permission=ManifestPermission("granted", manifest.permission.evidence_refs),
    )

    note_id, locators = verify_xiaohongshu_frozen_manifest(granted, _note("image"))

    assert note_id == NOTE_ID
    assert len(locators) == 2


@pytest.mark.parametrize("suffix", ["permission-r0", "permission-r01", "permission-r1-extra"])
def test_frozen_manifest_rejects_noncanonical_source_permission_derivative(
    tmp_path, suffix: str
) -> None:
    provider, _authority, _secrets, _store, _requests = _controlled_provider(
        tmp_path, html=_html("image")
    )
    manifest = provider.provide_controlled(
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
        project_id="project-1",
        credential_subject_id="account-1",
    )
    malformed = replace(
        manifest,
        normalizer_revision=f"{manifest.normalizer_revision}-{suffix}",
        permission=ManifestPermission("granted", manifest.permission.evidence_refs),
    )

    with pytest.raises(XiaohongshuMetadataProviderError, match="manifest_identity_invalid"):
        verify_xiaohongshu_frozen_manifest(malformed, _note("image"))


@pytest.mark.parametrize(
    ("kind", "content_kind", "asset_kinds"),
    [
        ("image", "image_set", ["image", "image"]),
        ("video", "video", ["video"]),
        ("mixed", "mixed", ["image", "video", "image", "text"]),
    ],
)
def test_anonymous_html_provider_builds_ordered_strict_manifests(
    tmp_path, kind: str, content_kind: str, asset_kinds: list[str]
) -> None:
    provider, store, requests = _provider(tmp_path, html=_html(kind))
    manifest = provider.provide(
        f"分享 https://www.xiaohongshu.com/explore/{NOTE_ID}#ignored",
        project_id="project-1",
    )

    assert SourceManifestCodec.decode(SourceManifestCodec.encode(manifest)) == manifest
    assert manifest.platform == "xiaohongshu"
    assert manifest.input_identity == f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
    assert manifest.content_kind == content_kind
    assert manifest.permission.decision == "unknown"
    assert [asset.kind for asset in manifest.assets] == asset_kinds
    assert [asset.ordinal for asset in manifest.assets] == list(range(len(asset_kinds)))
    for ordinal, asset in enumerate(manifest.assets):
        targets = {relation.relation: relation.target_asset_id for relation in asset.relations}
        if ordinal > 0:
            assert targets["previous"] == manifest.assets[ordinal - 1].asset_id
        if ordinal + 1 < len(manifest.assets):
            assert targets["next"] == manifest.assets[ordinal + 1].asset_id
    assert all(asset.locator is None for asset in manifest.assets)
    assert all(asset.evidence_refs == manifest.provenance_refs for asset in manifest.assets)
    assert requests[0].host == "www.xiaohongshu.com"
    assert requests[0].target == f"/explore/{NOTE_ID}"
    assert MediaRouter().route(manifest).reason == "source_permission_unresolved"
    encoded = json.dumps(
        store.list(SourceResolutionEvidenceRepository.collection), ensure_ascii=False
    )
    for canary in (
        "private-user-canary",
        "private-name",
        "private-cookie-canary",
        "private-token-canary",
        "private-cdn.example",
    ):
        assert canary not in encoded


@pytest.mark.parametrize(
    "source",
    [
        f"http://www.xiaohongshu.com/explore/{NOTE_ID}",
        f"https://user@www.xiaohongshu.com/explore/{NOTE_ID}",
        f"https://www.xiaohongshu.com:444/explore/{NOTE_ID}",
        f"https://xiaohongshu.com.evil/explore/{NOTE_ID}",
        f"https://www.xiaohongshu.com/user/profile/abc/{NOTE_ID}",
        "https://www.xiaohongshu.com/explore/1234567890abcdef",
        "https://www.xiaohongshu.com/explore/1234567890abcdef1234567890abcdef",
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=secret",
        f"https://www.xiaohongshu.com/explore/{NOTE_ID} https://www.xiaohongshu.com/explore/{NOTE_ID}",
    ],
)
def test_invalid_source_is_rejected_before_network(tmp_path, source: str) -> None:
    provider, _, requests = _provider(tmp_path, html=_html("image"))
    with pytest.raises(XiaohongshuMetadataProviderError, match="invalid_source"):
        provider.provide(source, project_id="project-1")
    assert requests == []


@pytest.mark.parametrize(
    ("html", "code"),
    [
        ("<html></html>", "unsupported_source"),
        (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">not-json private-secret</script>',
            "metadata_unavailable",
        ),
        (_html("image", note={"noteId": "65f000000000000000000000", "type": "image", "imageList": [{}]}), "metadata_identity_mismatch"),
    ],
)
def test_unsupported_or_invalid_page_uses_stable_private_errors(
    tmp_path, html: str, code: str
) -> None:
    provider, store, _ = _provider(tmp_path, html=html)
    with pytest.raises(XiaohongshuMetadataProviderError) as captured:
        provider.provide(
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}", project_id="project-1"
        )
    assert str(captured.value) == code
    assert "private-secret" not in str(captured.value)
    assert store.list(SourceResolutionEvidenceRepository.collection) == ()


def test_single_wrong_map_entry_without_embedded_identity_is_rejected(tmp_path) -> None:
    payload = {
        "note": {
            "noteDetailMap": {
                "65f000000000000000000000": {
                    "note": {"type": "image", "imageList": [{"url": "https://cdn.example/a"}]}
                }
            }
        }
    }
    html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        + json.dumps(payload)
        + "</script>"
    )
    provider, store, _ = _provider(tmp_path, html=html)
    with pytest.raises(XiaohongshuMetadataProviderError, match="unsupported_source"):
        provider.provide(
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}", project_id="project-1"
        )
    assert store.list(SourceResolutionEvidenceRepository.collection) == ()


def test_same_metadata_replays_and_drift_conflicts(tmp_path) -> None:
    provider, _, _ = _provider(tmp_path, html=_html("image"))
    url = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
    first = provider.provide(url, project_id="project-1")
    assert provider.provide(url, project_id="project-1") == first

    changed = _note("image")
    changed["title"] = "漂移标题"
    changed_provider, _, _ = _provider(tmp_path, html=_html("image", note=changed))
    with pytest.raises(XiaohongshuMetadataProviderError, match="metadata_identity_conflict"):
        changed_provider.provide(url, project_id="project-1")


def test_external_text_is_bounded_and_controls_are_removed(tmp_path) -> None:
    note = _note("image")
    note["title"] = "T\x00" + "x" * 600
    note["desc"] = "D\x7f" + "y" * 5000
    provider, store, _ = _provider(tmp_path, html=_html("image", note=note))
    manifest = provider.provide(
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}", project_id="project-1"
    )
    metadata = dict(manifest.metadata.entries)
    assert len(metadata["title"]) == 300
    assert manifest.body is not None and len(manifest.body.text or "") == 4000
    encoded = json.dumps(
        store.list(SourceResolutionEvidenceRepository.collection), ensure_ascii=False
    )
    assert "\\u0000" not in encoded and "\\u007f" not in encoded


def test_unified_analyze_source_persists_manifest_but_creates_no_job_without_grant(
    tmp_path,
) -> None:
    provider, store, _ = _provider(tmp_path, html=_html("mixed"))
    artifacts = SourceManifestArtifactRepository(store, namespace_id="default")
    resolver = PlatformArtifactSourceManifestResolver(
        artifacts=artifacts,
        platforms=PlatformResolver({"xiaohongshu": provider}),
        permissions=SourcePermissionAuthority(store, namespace_id="default"),
        executable_platforms=frozenset({"bilibili"}),
    )

    class NeverProvisioner:
        calls = 0

        def provision(self, **kwargs):
            self.calls += 1
            raise AssertionError(f"ungranted source reached Media Job: {kwargs}")

    provisioner = NeverProvisioner()
    payloads = InMemoryTurnPayloadStore()
    capability = AnalyzeSourceCapability(
        resolver=resolver,
        provisioner=provisioner,
        payloads=payloads,
        readiness=lambda: AnalyzeSourceReadiness(True, True, True),
        created_at=lambda: "2026-08-25T00:00:00Z",
        namespace_id="default",
    )
    result = capability.invoke(
        {
            "turn_id": "turn-xhs-1",
            "tool_call_id": "tool-xhs-1",
            "operation_id": "operation-xhs-1",
            "idempotency_key": "xhs-analyze-idempotency-0001",
            "scope": {"project_id": "project-1"},
            "arguments": {
                "input": {
                    "kind": "text",
                    "text": f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
                    "source_ref": None,
                },
                "intent": "organize",
                "output_profile": {"profile_id": "default", "revision": "1"},
                "resource_budget": {
                    "max_assets": 8,
                    "max_bytes": 1000,
                    "max_seconds": 20,
                },
            },
        }
    )
    assert result["result"]["status"] == "terminal"
    assert result["result"]["reason"] == "source_permission_unresolved"
    assert provisioner.calls == 0
    manifest_ref = result["result"]["manifest_ref"]
    assert isinstance(manifest_ref, str)
    persisted = json.dumps(
        store.list(SourceManifestArtifactRepository.collection), ensure_ascii=False
    )
    result_text = json.dumps(result, ensure_ascii=False)
    receipt_record = payloads.get_immutable_payload(
        "turn-xhs-1", "analyze-source-receipt:tool-xhs-1"
    )
    assert receipt_record is not None
    receipt_text = json.dumps(receipt_record[1], ensure_ascii=False)
    for canary in (
        "private-user-canary",
        "private-name",
        "private-cookie-canary",
        "private-token-canary",
        "private-cdn.example",
    ):
        assert canary not in persisted
        assert canary not in result_text
        assert canary not in receipt_text
    with pytest.raises(ValueError):
        ArtifactSourceManifestResolver(artifacts).resolve(
            {"input": {"kind": "source_ref", "text": None, "source_ref": manifest_ref}},
            {"project_id": "project-2"},
        )
