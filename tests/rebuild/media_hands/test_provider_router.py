from __future__ import annotations

import pytest

from core.media_hands import (
    MediaOperationProviderRouter,
    MediaOperationProviderRouterError,
    MediaOperationReceipt,
    MediaOperationRequest,
    SourcePermissionSnapshot,
)


class _Provider:
    supports_durable_recipe_resume = True

    def __init__(self, platform: str, provider_id: str, revision: str) -> None:
        self.provider_id = provider_id
        self.provider_revision = revision
        self.supported_platforms = frozenset({platform})
        self.requests: list[object] = []

    def execute(self, request: MediaOperationRequest) -> MediaOperationReceipt:
        self.requests.append(request)
        return MediaOperationReceipt({}, {}, {}, "crp://default/media-receipts/test.json")


def _request() -> MediaOperationRequest:
    return MediaOperationRequest(
        job_id="job-1",
        source_id="source-1",
        operation="analyze_source",
        manifest_ref="crp://default/source-manifests/source-1.json",
        manifest_revision="r1",
        checkpoint=None,
        budget={},
        permission_snapshot=SourcePermissionSnapshot(
            project_id="project-1",
            manifest_ref="crp://default/source-manifests/source-1.json",
            manifest_revision="r1",
            grant_ref="crp://default/grants/1",
            grant_revision="r1",
            revocation_generation=1,
        ),
    )


def test_routes_only_from_injected_frozen_manifest_platform() -> None:
    bilibili = _Provider("bilibili", "bilibili-media", "r1")
    xiaohongshu = _Provider("xiaohongshu", "xiaohongshu-media", "r3")
    seen = []
    router = MediaOperationProviderRouter(
        {"xiaohongshu": xiaohongshu, "bilibili": bilibili},
        lambda request: seen.append(request.manifest_ref) or "xiaohongshu",
    )

    assert router.supported_platforms == frozenset({"bilibili", "xiaohongshu"})
    assert router.provider_id == "media-operation-provider-router"
    assert router.supports_durable_recipe_resume is True
    router.execute(_request())

    assert seen == ["crp://default/source-manifests/source-1.json"]
    assert bilibili.requests == []
    assert len(xiaohongshu.requests) == 1


def test_revision_is_deterministic_and_binds_delegate_identity() -> None:
    one = _Provider("bilibili", "bilibili-media", "r1")
    two = _Provider("xiaohongshu", "xiaohongshu-media", "r3")
    first = MediaOperationProviderRouter({"bilibili": one, "xiaohongshu": two}, lambda _: "bilibili")
    reordered = MediaOperationProviderRouter({"xiaohongshu": two, "bilibili": one}, lambda _: "bilibili")
    changed = MediaOperationProviderRouter({"bilibili": _Provider("bilibili", "bilibili-media", "r2"), "xiaohongshu": two}, lambda _: "bilibili")

    assert first.provider_revision == reordered.provider_revision
    assert first.provider_revision != changed.provider_revision


@pytest.mark.parametrize(
    "provider, configured_platform",
    [
        (_Provider("bilibili", "bad id", "r1"), "bilibili"),
        (_Provider("bilibili", "bilibili-media", "bad revision"), "bilibili"),
        (_Provider("xiaohongshu", "xhs-media", "r1"), "bilibili"),
    ],
)
def test_rejects_invalid_or_mismatched_delegate_contract(provider, configured_platform) -> None:
    with pytest.raises(MediaOperationProviderRouterError):
        MediaOperationProviderRouter({configured_platform: provider}, lambda _: configured_platform)


def test_rejects_delegate_without_durable_resume_or_execute() -> None:
    provider = _Provider("bilibili", "bilibili-media", "r1")
    provider.supports_durable_recipe_resume = False
    with pytest.raises(MediaOperationProviderRouterError, match="durable recipe resume"):
        MediaOperationProviderRouter({"bilibili": provider}, lambda _: "bilibili")

    class _NoExecute:
        provider_id = "bilibili-media"
        provider_revision = "r1"
        supported_platforms = frozenset({"bilibili"})
        supports_durable_recipe_resume = True

    with pytest.raises(MediaOperationProviderRouterError, match="execute"):
        MediaOperationProviderRouter({"bilibili": _NoExecute()}, lambda _: "bilibili")


def test_unknown_manifest_platform_and_delegate_drift_fail_closed() -> None:
    provider = _Provider("bilibili", "bilibili-media", "r1")
    router = MediaOperationProviderRouter({"bilibili": provider}, lambda _: "douyin")
    with pytest.raises(MediaOperationProviderRouterError, match="no admitted"):
        router.execute(_request())
    assert provider.requests == []

    router = MediaOperationProviderRouter({"bilibili": provider}, lambda _: "bilibili")
    provider.provider_revision = "r2"
    with pytest.raises(MediaOperationProviderRouterError, match="drifted"):
        router.execute(_request())
    assert provider.requests == []
