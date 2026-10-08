from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.memory_app import workspace_xhs_media as xhs
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.product_core.cloud_asr_provider_settings import SaveCloudAsrProviderSettings


NOTE_ID = "67e65857000000001901abcd"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"


def test_paired_note_capture_reuses_real_public_parser_and_keeps_default_video_requirement(monkeypatch, tmp_path):
    from tests.memory_app.v2.test_xhs_comment_intake import public_note, BODY
    calls = public_note(monkeypatch)
    capture = xhs._capture_public_note(NOTE_URL)
    result = xhs._read_captured_note(capture, tmp_path, allow_nonvideo=True)
    assert result['source_text'] == f'标题：合成笔记\n\n来源：{NOTE_URL}\n\n笔记描述：{BODY}'
    assert result['content_kind'] == 'text'
    assert result['canonical_url'] == NOTE_URL
    assert len(calls) == 1
    with pytest.raises(ValueError, match='^xiaohongshu_video_required$'):
        xhs.read_xiaohongshu_media(NOTE_URL, tmp_path)
    assert len(calls) == 2


def test_source_url_canonicalizes_share_query_without_publishing_token() -> None:
    note_id, canonical, request = xhs._source_url(
        f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}"
        "?xsec_token=abc_DEF-123%3D&xsec_source=pc_user&source=tracking"
    )
    assert note_id == NOTE_ID
    assert canonical == NOTE_URL
    assert request == NOTE_URL + "?xsec_token=abc_DEF-123%3D&xsec_source=pc_user"


@pytest.mark.parametrize("url", [
    f"http://www.xiaohongshu.com/explore/{NOTE_ID}",
    f"https://www.xiaohongshu.com.evil.test/explore/{NOTE_ID}",
    f"https://evil.test@www.xiaohongshu.com/explore/{NOTE_ID}",
    f"https://xhslink.com/a/xyz",
    "https://www.xiaohongshu.com/explore/not-a-note",
])
def test_source_url_rejects_untrusted_or_unsupported_urls(url: str) -> None:
    with pytest.raises(ValueError, match="^invalid_source$"):
        xhs._source_url(url)


def test_reader_uses_real_video_speech_and_keeps_canonical_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: dict[str, str] = {}

    class FakeNetwork:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["max_redirects"] == 0

        def fetch_text(self, url: str) -> str:
            calls["request_url"] = url
            return "html"

    monkeypatch.setattr(xhs, "SafeTextNetworkAdapter", FakeNetwork)
    monkeypatch.setattr(xhs, "_extract_note", lambda html, *, note_id: {"video": {"url": "https://a.xhscdn.com/video.mp4"}})
    monkeypatch.setattr(xhs, "_normalize_note", lambda note, *, note_id: (
        {"title": "实测标题"}, [{"kind": "video"}], "video", {"text": "原笔记描述"},
    ))

    def transcribe(locator: str, canonical_url: str, runtime_root: Path, title: str, *context) -> str:
        assert locator == "https://a.xhscdn.com/video.mp4"
        assert canonical_url == NOTE_URL
        assert runtime_root == tmp_path
        assert title == "实测标题"
        return "视频实际说的话"

    monkeypatch.setattr(xhs, "_download_and_transcribe", transcribe)
    result = xhs.read_xiaohongshu_media(
        NOTE_URL + "?xsec_token=abc_123&xsec_source=pc_user", tmp_path
    )
    assert calls["request_url"].endswith("?xsec_token=abc_123&xsec_source=pc_user")
    assert result["canonical_url"] == NOTE_URL
    assert result["content_kind"] == "video"
    assert result["acquisition_method"] == "public_html_video_local_asr"
    assert "原笔记描述" in result["source_text"]
    assert "视频实际说的话" in result["source_text"]
    assert "xsec_token" not in str(result)


def test_video_speech_uses_hy_asr_when_cloud_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    monkeypatch.setattr(xhs, "SafeTextNetworkAdapter", lambda **_kwargs: type(
        "Network", (), {"fetch_text": lambda self, _url: "html"}
    )())
    monkeypatch.setattr(xhs, "_extract_note", lambda _html, *, note_id: {
        "video": {"url": "https://a.xhscdn.com/video.mp4"},
    })
    monkeypatch.setattr(xhs, "_normalize_note", lambda _note, *, note_id: (
        {"title": "视频标题"}, [{"kind": "video"}], "video", {"text": "描述"},
    ))
    selected = []

    def transcribe(*args):
        selected.append(args[-2])
        return "云端语音原文"

    monkeypatch.setattr(xhs, "_download_and_transcribe", transcribe)
    result = xhs.read_xiaohongshu_media(NOTE_URL, tmp_path)

    assert selected == ["tokenhub-asr"]
    assert result["acquisition_method"] == "public_html_video_hy_asr"
    assert "云端语音原文" in result["source_text"]


def test_xhs_download_keeps_cdn_guard_and_duration_limit_for_asr(monkeypatch, tmp_path: Path) -> None:
    class Binary:
        def __init__(self, root, **options):
            assert options["allowed_host_suffixes"] == ("xhscdn.com",)
            self.root = root

        def download(self, _url, **_options):
            path = self.root / "video.mp4"
            path.write_bytes(b"video")
            return SimpleNamespace(path=path, media_type="video/mp4")

    monkeypatch.setattr(xhs, "SafeBinaryDownloadAdapter", Binary)
    from backend.memory_app import workspace_audio as workspace

    calls = []

    def transcribe(path, root, project_id, item_id, run_id, **options):
        calls.append((project_id, item_id, run_id, options))
        return {"segments": [{"text": "真实视频语音"}]}

    monkeypatch.setattr(workspace, "_transcribe_output", transcribe)
    speech = xhs._download_and_transcribe(
        "https://a.xhscdn.com/video.mp4", NOTE_URL, tmp_path, "标题",
        "project-a", "workspace-a", "run-a", "tokenhub-asr",
    )
    assert speech == "真实视频语音"
    assert calls == [("project-a", "workspace-a", "run-a", {
        "source_type": "video", "max_duration_seconds": 900.0,
        "expected_provider": "tokenhub-asr", "validate_remote": None,
    })]


def test_reader_does_not_pretend_description_is_video_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeNetwork:
        def __init__(self, **kwargs: object) -> None:
            pass

        def fetch_text(self, url: str) -> str:
            return "html"

    monkeypatch.setattr(xhs, "SafeTextNetworkAdapter", FakeNetwork)
    monkeypatch.setattr(xhs, "_extract_note", lambda html, *, note_id: {"video": {"url": "https://a.xhscdn.com/video.mp4"}})
    monkeypatch.setattr(xhs, "_normalize_note", lambda note, *, note_id: (
        {"title": "标题"}, [{"kind": "video"}], "video", {"text": "只有描述"},
    ))
    monkeypatch.setattr(xhs, "_download_and_transcribe", lambda *args: "")
    with pytest.raises(ValueError, match="^xiaohongshu_video_speech_unavailable$"):
        xhs.read_xiaohongshu_media(NOTE_URL, tmp_path)


def test_nonvideo_note_is_explicitly_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeNetwork:
        def __init__(self, **kwargs: object) -> None:
            pass

        def fetch_text(self, url: str) -> str:
            return "html"

    monkeypatch.setattr(xhs, "SafeTextNetworkAdapter", FakeNetwork)
    monkeypatch.setattr(xhs, "_extract_note", lambda html, *, note_id: {"imageList": []})
    monkeypatch.setattr(xhs, "_normalize_note", lambda note, *, note_id: (
        {"title": "图文"}, [], "image_set", {"text": "图文描述"},
    ))
    with pytest.raises(ValueError, match="^xiaohongshu_video_required$"):
        xhs.read_xiaohongshu_media(NOTE_URL, tmp_path)


def test_short_link_uses_only_verified_xiaohongshu_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def resolved(url: str) -> tuple[str, str]:
        calls.append(url)
        return "xiaohongshu", NOTE_URL

    monkeypatch.setattr(xhs, "resolve_media_url", resolved)
    monkeypatch.setattr(xhs, "SafeTextNetworkAdapter", lambda **kwargs: type(
        "Network", (), {"fetch_text": lambda self, url: "html"}
    )())
    monkeypatch.setattr(xhs, "_extract_note", lambda html, *, note_id: {"video": {"url": "https://a.xhscdn.com/v.mp4"}})
    monkeypatch.setattr(xhs, "_normalize_note", lambda note, *, note_id: (
        {"title": "视频"}, [{"kind": "video"}], "video", None,
    ))
    monkeypatch.setattr(xhs, "_download_and_transcribe", lambda *args: "语音")
    result = xhs.read_xiaohongshu_media("https://xhslink.com/a/abc", tmp_path)
    assert calls == ["https://xhslink.com/a/abc"]
    assert result["canonical_url"] == NOTE_URL


@pytest.mark.parametrize("locator", [
    "http://a.xhscdn.com/v.mp4",
    "https://xhscdn.com/v.mp4",
    "https://a.xhscdn.com.evil.test/v.mp4",
    "https://evil.test@a.xhscdn.com/v.mp4",
])
def test_video_locator_must_be_https_reviewed_cdn(locator: str) -> None:
    with pytest.raises(ValueError, match="^xiaohongshu_video_unavailable$"):
        xhs._validate_video_locator(locator)
