from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.source_processing import PlatformResolver, SourceManifestCodec


FIXTURES = Path(__file__).resolve().parents[3] / "core-contracts" / "rebuild" / "source-processing" / "fixtures"


class _FixtureProvider:
    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    def provide(self, text: str, *, project_id: str):
        assert project_id == "project-1"
        self.calls += 1
        raw = json.loads((FIXTURES / self._name).read_text(encoding="utf-8"))
        raw["platform"] = "xiaohongshu" if self._name.startswith("xiaohongshu") else "bilibili"
        return SourceManifestCodec.decode(raw)


@pytest.mark.parametrize(("fixture", "kind"), [("xiaohongshu-image-set.json", "image_set"), ("xiaohongshu-video.json", "video"), ("xiaohongshu-mixed.json", "mixed")])
def test_xiaohongshu_provider_can_return_each_supported_content_kind(fixture: str, kind: str) -> None:
    result = PlatformResolver({"xiaohongshu": _FixtureProvider(fixture)}).resolve("小红书 分享文本", project_id="project-1")
    assert result.status == "resolved"
    assert result.manifest is not None and result.manifest.content_kind == kind


def test_bilibili_url_is_deterministically_resolved_by_domain() -> None:
    result = PlatformResolver({"bilibili": _FixtureProvider("bilibili-video.json")}).resolve("https://www.bilibili.com/video/BV1xx411c7mD", project_id="project-1")
    assert result.status == "resolved"
    assert result.platform == "bilibili"


def test_unknown_and_missing_provider_are_structured_non_video_outcomes() -> None:
    resolver = PlatformResolver({})
    assert resolver.resolve("ordinary local note", project_id="project-1").reason == "unknown_platform"
    missing = resolver.resolve("https://www.bilibili.com/video/BV1xx411c7mD", project_id="project-1")
    assert (missing.status, missing.reason, missing.manifest) == ("unsupported", "provider_missing", None)


def test_unknown_manifest_is_not_silently_promoted_to_video() -> None:
    result = PlatformResolver({"xiaohongshu": _FixtureProvider("xiaohongshu-unknown.json")}).resolve("小红书 分享文本", project_id="project-1")
    assert result.status == "unsupported"
    assert result.reason == "unknown_content_kind"
    assert result.manifest is not None and result.manifest.content_kind == "unknown"


def test_ambiguous_domains_never_choose_a_provider() -> None:
    resolver = PlatformResolver({"bilibili": _FixtureProvider("bilibili-video.json")})
    ambiguous = resolver.resolve("https://bilibili.com/video/a https://xiaohongshu.com/explore/b", project_id="project-1")
    assert ambiguous.reason == "ambiguous_platform" and ambiguous.manifest is None


def test_invalid_provider_registry_fails_before_readiness() -> None:
    with pytest.raises(ValueError):
        PlatformResolver({"bilibili": object()})


@pytest.mark.parametrize(
    "text",
    [
        "https://bilibili.com.evil/video/x",
        "https://xiaohongshu.com.evil/x",
        "https://evil-bilibili.com/x",
        "https://user@bilibili.com/x",
    ],
)
def test_url_lookalikes_and_credentials_never_reach_a_provider(text: str) -> None:
    provider = _FixtureProvider("bilibili-video.json")
    result = PlatformResolver({"bilibili": provider}).resolve(text, project_id="project-1")
    assert result.reason == "unknown_platform"
    assert result.manifest is None
    assert provider.calls == 0
