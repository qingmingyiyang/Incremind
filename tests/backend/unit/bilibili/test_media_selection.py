from __future__ import annotations

import pytest

from backend.bilibili.authorized_public_download import (
    AuthorizedPublicDownloadError,
    _require_media_url,
)
from backend.bilibili.media_selection import (
    BilibiliMediaSelectionError,
    require_bilibili_cdn_url,
    select_dash_audio,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://bilivideo.com/audio.m4s",
        "https://cdn.bilivideo.com/audio.m4s?token=opaque",
    ],
)
def test_legacy_and_governed_paths_share_bilibili_cdn_acceptance(url: str) -> None:
    assert require_bilibili_cdn_url(url) == url
    assert _require_media_url(url) is None
    selected, _codec = select_dash_audio(
        {"dash": {"audio": [{"baseUrl": url, "bandwidth": 1}]}}
    )
    assert selected == url


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.bilivideo.com/audio.m4s",
        "https://bilivideo.com.evil.example/audio.m4s",
        "https://user:password@cdn.bilivideo.com/audio.m4s",
        "https://cdn.bilivideo.com:444/audio.m4s",
        "https://cdn.bilivideo.com/audio.m4s#fragment",
    ],
)
def test_legacy_and_governed_paths_share_bilibili_cdn_rejection(url: str) -> None:
    with pytest.raises(BilibiliMediaSelectionError, match="media_url_invalid"):
        require_bilibili_cdn_url(url)
    with pytest.raises(AuthorizedPublicDownloadError, match="allowlisted CDN"):
        _require_media_url(url)
    with pytest.raises(BilibiliMediaSelectionError, match="dash_audio_url_rejected"):
        select_dash_audio({"dash": {"audio": [{"baseUrl": url, "bandwidth": 1}]}})


def test_governed_dash_selector_uses_highest_valid_bandwidth_deterministically() -> None:
    assert select_dash_audio({
        "dash": {"audio": [
            {"baseUrl": "https://low.bilivideo.com/a.m4s", "bandwidth": 32000},
            {"baseUrl": "https://high.bilivideo.com/a.m4s", "bandwidth": 64000, "codecs": "mp4a.40.2"},
            {"baseUrl": "https://evil.example/a.m4s", "bandwidth": 999999},
        ]}
    }) == ("https://high.bilivideo.com/a.m4s", "mp4a.40.2")
