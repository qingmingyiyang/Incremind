from __future__ import annotations

import pytest

from backend.memory_app import workspace_media_url as media


def test_known_video_hosts_and_short_links(monkeypatch):
    assert media.media_platform("https://www.bilibili.com/video/BV1jj8yzLEWo/?p=2") == "bilibili"
    assert media.media_platform("https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=abc") == "xiaohongshu"
    assert media.media_platform("https://b23.tv/test") == "bilibili"
    assert media.media_platform("http://127.0.0.1/video/BV1jj8yzLEWo/") is None
    monkeypatch.setattr(media, "_redirect_location", lambda url: "https://www.bilibili.com/video/BV1jj8yzLEWo/?spm=abc")
    assert media.resolve_media_url("https://b23.tv/test")[1].startswith("https://www.bilibili.com/video/")


def test_short_link_rejects_cross_platform_and_private_target(monkeypatch):
    monkeypatch.setattr(media, "_redirect_location", lambda url: "http://127.0.0.1/private")
    with pytest.raises(ValueError, match="media_short_link_target_invalid"):
        media.resolve_media_url("https://b23.tv/test")
    monkeypatch.setattr(media, "_redirect_location", lambda url: "https://www.xiaohongshu.com/explore/0123456789abcdef01234567")
    with pytest.raises(ValueError, match="media_short_link_target_invalid"):
        media.resolve_media_url("https://b23.tv/test")
