from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from backend.bilibili.authorized_public_download import (
    AuthorizedPublicDownloadError,
    download_authorized_bilibili_video,
)


BVID = "BV1abcDEF234"
URL = f"https://www.bilibili.com/video/{BVID}/"


def _yt_failure(message: str = "HTTP Error 412: Precondition Failed"):
    return lambda command: subprocess.CompletedProcess(command, 1, stdout="", stderr=message)


def _client(*, view_bvid: str = BVID, media_url: str = "https://cdn.bilivideo.com/video.mp4", size: int = 5):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"bvid": view_bvid, "pages": [{"page": 1, "cid": 99}]}},
            )
        if request.url.path == "/x/player/playurl":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"durl": [{"url": media_url, "size": size}]}},
            )
        if request.url.host == "cdn.bilivideo.com":
            return httpx.Response(200, headers={"content-length": str(size)}, content=b"video"[:size])
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _download(tmp_path: Path, client: httpx.Client, **kwargs) -> Path:
    return download_authorized_bilibili_video(
        url=URL,
        output_template=str(tmp_path / "video.%(ext)s"),
        format_selector="best",
        yt_dlp_runner=_yt_failure(),
        http_client=client,
        media_validator=lambda path: True,
        **kwargs,
    )


def test_primary_yt_dlp_success_does_not_call_public_fallback(tmp_path: Path) -> None:
    def runner(command):
        output = Path(command[command.index("--output") + 1].replace("%(ext)s", "mp4"))
        output.write_bytes(b"primary")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    output = download_authorized_bilibili_video(
        url=URL,
        output_template=str(tmp_path / "video.%(ext)s"),
        format_selector="best",
        yt_dlp_runner=runner,
        media_validator=lambda path: True,
    )

    assert output.read_bytes() == b"primary"


def test_primary_output_without_audio_is_removed_before_public_fallback(tmp_path: Path) -> None:
    def runner(command):
        output = Path(command[command.index("--output") + 1].replace("%(ext)s", "f100.mp4"))
        output.write_bytes(b"video-only")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with _client() as client:
        output = download_authorized_bilibili_video(
            url=URL,
            output_template=str(tmp_path / "video.%(ext)s"),
            format_selector="best",
            yt_dlp_runner=runner,
            http_client=client,
            media_validator=lambda path: path.name == "video.mp4",
        )

    assert output.name == "video.mp4"
    assert output.read_bytes() == b"video"
    assert not (tmp_path / "video.f100.mp4").exists()


def test_anonymous_http_412_uses_identity_bound_atomic_public_fallback(tmp_path: Path) -> None:
    with _client() as client:
        output = _download(tmp_path, client)

    assert output.name == "video.mp4"
    assert output.read_bytes() == b"video"
    assert not list(tmp_path.glob("*.part"))


def test_public_fallback_rejects_view_identity_drift(tmp_path: Path) -> None:
    with _client(view_bvid="BV1different999") as client:
        with pytest.raises(AuthorizedPublicDownloadError, match="identity drifted"):
            _download(tmp_path, client)

    assert not list(tmp_path.iterdir())


def test_public_fallback_rejects_non_bilibili_media_host(tmp_path: Path) -> None:
    with _client(media_url="https://example.com/video.mp4") as client:
        with pytest.raises(AuthorizedPublicDownloadError, match="escaped the allowlisted CDN"):
            _download(tmp_path, client)

    assert not list(tmp_path.iterdir())


def test_public_fallback_revalidates_redirect_host(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": {"bvid": BVID, "pages": [{"page": 1, "cid": 99}]}})
        if request.url.path == "/x/player/playurl":
            return httpx.Response(200, json={"code": 0, "data": {"durl": [{"url": "https://cdn.bilivideo.com/video.mp4", "size": 5}]}})
        return httpx.Response(302, headers={"location": "https://example.com/escaped.mp4"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(AuthorizedPublicDownloadError, match="escaped the allowlisted CDN"):
            _download(tmp_path, client)

    assert not list(tmp_path.iterdir())


def test_public_fallback_rejects_declared_oversize_before_stream(tmp_path: Path) -> None:
    with _client(size=6) as client:
        with pytest.raises(AuthorizedPublicDownloadError, match="exceeds the download byte limit"):
            _download(tmp_path, client, max_download_bytes=5)

    assert not list(tmp_path.iterdir())


def test_public_fallback_removes_partial_file_when_stream_is_short(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": {"bvid": BVID, "pages": [{"page": 1, "cid": 99}]}})
        if request.url.path == "/x/player/playurl":
            return httpx.Response(200, json={"code": 0, "data": {"durl": [{"url": "https://cdn.bilivideo.com/video.mp4", "size": 5}]}})
        return httpx.Response(200, headers={"content-length": "3"}, content=b"bad")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(AuthorizedPublicDownloadError, match="Content-Length drifted"):
            _download(tmp_path, client)

    assert not list(tmp_path.iterdir())


def test_cookie_mode_never_falls_back_to_anonymous_public_api(tmp_path: Path) -> None:
    with pytest.raises(AuthorizedPublicDownloadError, match="subprocess mode is prohibited"):
        download_authorized_bilibili_video(
            url=URL,
            output_template=str(tmp_path / "video.%(ext)s"),
            format_selector="best",
            cookie_mode="browser",
            cookies_from_browser="edge",
            yt_dlp_runner=_yt_failure(),
        )

    assert not list(tmp_path.iterdir())
