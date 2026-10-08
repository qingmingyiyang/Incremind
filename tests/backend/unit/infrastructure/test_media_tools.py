from __future__ import annotations

from backend.video_summary.infrastructure.media_tools import _media_executable


def test_media_tools_find_bundled_ffmpeg_without_path() -> None:
    assert _media_executable("ffmpeg").lower().endswith("ffmpeg.exe")
    assert _media_executable("ffprobe").lower().endswith("ffprobe.exe")
