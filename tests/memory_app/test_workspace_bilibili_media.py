from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.memory_app import workspace_bilibili_media as media
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.product_core.cloud_asr_provider_settings import SaveCloudAsrProviderSettings


BVID = "BV1xx411c7mD"
VIEW = f"https://api.bilibili.com/x/web-interface/view?bvid={BVID}"
CATALOG = f"https://api.bilibili.com/x/player/v2?bvid={BVID}&cid=101"
SUBTITLE = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/test.json"


def _view() -> str:
    return json.dumps({"code": 0, "data": {
        "bvid": BVID, "title": "整期视频", "desc": "简介不是正文", "duration": 12,
        "owner": {"name": "示例UP"},
        "pages": [{"page": 1, "cid": 101, "duration": 12, "part": "第一部分"}],
    }})


class FakeNetwork:
    responses: dict[str, str] = {}
    calls: list[str] = []

    def __init__(self, **kwargs):
        self.options = kwargs

    def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        return self.responses[url]


def test_reads_official_subtitle_with_timestamps_and_canonical_page(monkeypatch, tmp_path: Path):
    FakeNetwork.calls = []
    FakeNetwork.responses = {
        VIEW: _view(),
        CATALOG: json.dumps({"code": 0, "data": {"subtitle": {"subtitles": [
            {"lan": "zh-Hans", "subtitle_url": SUBTITLE},
        ]}}}),
        SUBTITLE: json.dumps({"body": [
            {"from": 1.2, "to": 2.5, "content": "真正的语音内容"},
        ]}),
    }
    monkeypatch.setattr(media, "SafeTextNetworkAdapter", FakeNetwork)
    monkeypatch.setattr(media, "_transcribe_audio", lambda *args: pytest.fail("ASR should not run"))

    result = media.read_bilibili_media(
        f"https://m.bilibili.com/video/{BVID}/?spm_id_from=abc", tmp_path
    )

    assert result["canonical_url"] == f"https://www.bilibili.com/video/{BVID}/"
    assert result["title"] == "第一部分"
    assert result["acquisition_method"] == "official_subtitle"
    assert result["content_kind"] == "video"
    assert "[00:00:01] 真正的语音内容" in result["source_text"]
    assert "简介（页面元数据，未验证为视频内容）" in result["source_text"]
    assert FakeNetwork.calls == [VIEW, CATALOG, SUBTITLE]


def test_falls_back_to_local_asr_when_subtitle_missing(monkeypatch, tmp_path: Path):
    FakeNetwork.calls = []
    FakeNetwork.responses = {
        VIEW: _view(), CATALOG: json.dumps({"code": 0, "data": {"subtitle": {"subtitles": []}}}),
    }
    monkeypatch.setattr(media, "SafeTextNetworkAdapter", FakeNetwork)
    monkeypatch.setattr(media, "_transcribe_audio", lambda *_: [
        SimpleNamespace(start_seconds=63.2, text="本地识别")
    ])

    result = media.read_bilibili_media(f"https://www.bilibili.com/video/{BVID}/", tmp_path)

    assert result["acquisition_method"] == "local_asr"
    assert "[00:01:03] 本地识别" in result["source_text"]


def test_missing_subtitle_selects_hy_asr_without_changing_timestamp_source(monkeypatch, tmp_path: Path):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    FakeNetwork.responses = {
        VIEW: _view(), CATALOG: json.dumps({"code": 0, "data": {"subtitle": {"subtitles": []}}}),
    }
    monkeypatch.setattr(media, "SafeTextNetworkAdapter", FakeNetwork)
    selected = []

    def cloud_segments(*args):
        selected.append(args[-2])
        return [{"start_seconds": 2.5, "text": "云端识别原文"}]

    monkeypatch.setattr(media, "_transcribe_audio", cloud_segments)
    result = media.read_bilibili_media(f"https://www.bilibili.com/video/{BVID}/", tmp_path)

    assert selected == ["tokenhub-asr"]
    assert result["acquisition_method"] == "hy_asr"
    assert "[00:00:02] 云端识别原文" in result["source_text"]
    assert "Hy-ASR 语音转写" in result["source_text"]


@pytest.mark.parametrize("media_type", ["audio/mp4", "video/mp4"])
def test_bilibili_download_keeps_host_guard_and_dispatches_selected_asr(monkeypatch, tmp_path: Path, media_type: str):
    monkeypatch.setattr(media, "_select_audio", lambda _raw: (
        "https://a.bilivideo.com/audio.m4s", "aac",
    ))

    class Binary:
        def __init__(self, root, **options):
            assert options["allowed_host_suffixes"] == ("bilivideo.com",)
            self.root = root

        def download(self, _url, **options):
            assert options["headers"]["User-Agent"] == "Mozilla/5.0"
            assert "Cookie" not in options["headers"]
            path = self.root / "audio.m4s"
            path.write_bytes(b"audio")
            return SimpleNamespace(path=path, media_type=media_type)

    monkeypatch.setattr(media, "SafeBinaryDownloadAdapter", Binary)
    from backend.memory_app import workspace_audio as workspace

    calls = []

    def transcribe(path, root, project_id, item_id, run_id, **options):
        calls.append((project_id, item_id, run_id, options))
        return {"segments": [{"start_seconds": 1.0, "text": "真实视频语音"}]}

    monkeypatch.setattr(workspace, "_transcribe_output", transcribe)
    segments = media._transcribe_audio(
        SimpleNamespace(fetch_text=lambda _url: "playurl"),
        {"bvid": BVID, "cid": 101}, tmp_path,
        "project-a", "workspace-a", "run-a", "tokenhub-asr",
    )
    assert segments[0]["text"] == "真实视频语音"
    assert calls == [("project-a", "workspace-a", "run-a", {
        "source_type": "video", "expected_provider": "tokenhub-asr",
        "validate_remote": None,
    })]


def test_metadata_failure_is_not_reported_as_video_content(monkeypatch, tmp_path: Path):
    FakeNetwork.calls = []
    FakeNetwork.responses = {VIEW: json.dumps({"code": -412, "message": "blocked"})}
    monkeypatch.setattr(media, "SafeTextNetworkAdapter", FakeNetwork)

    with pytest.raises(ValueError, match="^bilibili_metadata_unavailable$"):
        media.read_bilibili_media(f"https://www.bilibili.com/video/{BVID}/", tmp_path)


def test_empty_transcript_cannot_succeed(monkeypatch, tmp_path: Path):
    FakeNetwork.responses = {
        VIEW: _view(), CATALOG: json.dumps({"code": 0, "data": {"subtitle": {"subtitles": []}}}),
    }
    monkeypatch.setattr(media, "SafeTextNetworkAdapter", FakeNetwork)
    monkeypatch.setattr(media, "_transcribe_audio", lambda *_: [])

    with pytest.raises(ValueError, match="^bilibili_video_transcription_failed$"):
        media.read_bilibili_media(f"https://www.bilibili.com/video/{BVID}/", tmp_path)


def _official_comment_boundary(monkeypatch, pages, *, speech='真正的语音内容 😀'):
    """Keep the real media/yt-dlp/boundary owners; replace only DNS and HTTP."""
    from urllib.parse import parse_qs, urlsplit
    from backend.security import network_adapter
    payload = json.loads(_view())
    calls = []
    def transport(request):
        calls.append(request)
        url = f'https://{request.host}{request.target}'
        if url == VIEW:
            response = payload
        elif url == CATALOG:
            response = {'code': 0, 'data': {'subtitle': {'subtitles': [
                {'lan': 'zh-Hans', 'subtitle_url': SUBTITLE}]}}}
        elif url == SUBTITLE:
            segments = [speech] if isinstance(speech, str) else speech
            response = {'body': [{'from': n + 1.2, 'to': n + 2.5, 'content': text}
                for n, text in enumerate(segments)]}
        else:
            assert request.host == 'api.bilibili.com'
            assert urlsplit(request.target).path == '/x/v2/reply'
            response = pages[int(parse_qs(urlsplit(request.target).query)['pn'][0])]
        if isinstance(response, Exception):
            raise response
        body = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        return network_adapter.BoundedHttpResponse(200,
            {'Content-Type': 'application/json; charset=utf-8'}, body.encode())
    monkeypatch.setattr(network_adapter, '_resolve_addresses', lambda *_: ('8.8.8.8',))
    monkeypatch.setattr(network_adapter, '_perform_pinned_request', transport)
    return payload, calls


def _comment(identity, likes, text, *, children=None):
    return {'rpid': identity, 'like': likes, 'content': {'message': text},
        'member': {}, 'parent': 0, 'replies': children or []}


def _comments_page(rows):
    return {'code': 0, 'data': {'replies': rows}}


def test_real_reader_appends_ranked_complete_comments_without_changing_speech_bytes(monkeypatch, tmp_path):
    pages = {1: _comments_page([_comment(1, 2, '首条', children=[_comment(2, 8, '孩子纠错')])]),
        2: _comments_page([_comment(3, 8, '后页补充')]), 3: _comments_page([])}
    view, calls = _official_comment_boundary(monkeypatch, pages)
    url = f'https://www.bilibili.com/video/{BVID}/'
    original = media.read_bilibili_media(url, tmp_path)
    assert len(calls) == 3
    view['data']['aid'] = 123
    calls.clear()
    captured = media.read_bilibili_media(url, tmp_path)
    suffix = '\n\n## 评论区\n\n- 8 赞 · 孩子纠错\n\n- 8 赞 · 后页补充\n\n- 2 赞 · 首条'
    assert captured == {**original, 'source_text': original['source_text'] + suffix}
    raw = original['source_text'].encode('utf-8')
    assert captured['source_text'].encode('utf-8')[:len(raw)] == raw
    assert [call.host for call in calls] == ['api.bilibili.com', 'api.bilibili.com',
        'aisubtitle.hdslb.com', 'api.bilibili.com', 'api.bilibili.com', 'api.bilibili.com']


def test_internal_section_opt_in_uses_one_capture_and_keeps_default_reader_shape(monkeypatch, tmp_path):
    view, calls = _official_comment_boundary(monkeypatch,
        {1: _comments_page([_comment(1, 8, '纠错 😀')]), 2: _comments_page([])})
    view['data']['aid'] = 123
    url = f'https://www.bilibili.com/video/{BVID}/'
    ordinary = media.read_bilibili_media(url, tmp_path)
    calls.clear()
    result = media.read_bilibili_media(url, tmp_path, with_sections=True)
    assert {key: value for key, value in result.items() if key != '_source_sections'} == ordinary
    assert len(calls) == 5
    sections = result['_source_sections']
    assert result['source_text'][sections['comments'][0]['start']:sections['comments'][0]['end']] == '纠错 😀'
    assert '_source_sections' not in ordinary


@pytest.mark.parametrize('good_before_bad', [False, True])
def test_json_escaped_unpaired_unicode_comment_cannot_break_successful_body(monkeypatch, tmp_path, good_before_bad):
    comments = ([_comment(2, 9, '前面的完整条目')] if good_before_bad else []) + [_comment(1, 8, '坏字符\ud800')]
    page = json.dumps(_comments_page(comments), ensure_ascii=True)
    view, calls = _official_comment_boundary(monkeypatch, {1: page, 2: _comments_page([])})
    url = f'https://www.bilibili.com/video/{BVID}/'
    original = media.read_bilibili_media(url, tmp_path)
    view['data']['aid'] = 123
    calls.clear()
    result = media.read_bilibili_media(url, tmp_path, with_sections=True)
    assert result['source_text'].encode('utf-8') == original['source_text'].encode('utf-8')
    assert result['_source_sections'] is None
    assert len(calls) == 5


@pytest.mark.parametrize('failure', [OSError('synthetic failure'), 'bad json',
    {'code': -412, 'data': {'replies': []}}, {'code': 0, 'data': {}}])
def test_comment_failure_after_partial_capture_preserves_successful_body(monkeypatch, tmp_path, failure):
    view, calls = _official_comment_boundary(monkeypatch,
        {1: _comments_page([_comment(1, 9, '半份评论不可用')]), 2: failure})
    url = f'https://www.bilibili.com/video/{BVID}/'
    original = media.read_bilibili_media(url, tmp_path)
    view['data']['aid'] = 123
    calls.clear()
    assert media.read_bilibili_media(url, tmp_path) == original
    assert len(calls) == 5


@pytest.mark.parametrize('limit', ['pages', 'unique_candidates'])
def test_comment_resource_limit_preserves_body_and_stops_before_extra_requests(monkeypatch, tmp_path, limit):
    pages = ({n: _comments_page([_comment(n, n, '未证完整')]) for n in range(1, 11)}
        if limit == 'pages' else {1: _comments_page([_comment(n, n, '评论') for n in range(1, 2002)])})
    view, calls = _official_comment_boundary(monkeypatch, pages)
    url = f'https://www.bilibili.com/video/{BVID}/'
    original = media.read_bilibili_media(url, tmp_path)
    view['data']['aid'] = 123
    calls.clear()
    assert media.read_bilibili_media(url, tmp_path) == original
    assert len(calls) == (13 if limit == 'pages' else 4)


def test_bad_like_does_not_create_a_comment_section_or_change_body(monkeypatch, tmp_path):
    view, calls = _official_comment_boundary(monkeypatch,
        {1: _comments_page([_comment(1, True, '不是零赞')]), 2: _comments_page([])})
    url = f'https://www.bilibili.com/video/{BVID}/'
    original = media.read_bilibili_media(url, tmp_path)
    view['data']['aid'] = 123
    calls.clear()
    assert media.read_bilibili_media(url, tmp_path) == original
    assert len(calls) == 5


def test_unavailable_external_comment_extractor_keeps_the_successful_source(monkeypatch, tmp_path, capsys):
    import builtins
    import sys
    view, calls = _official_comment_boundary(monkeypatch, {})
    url = f'https://www.bilibili.com/video/{BVID}/'
    original = media.read_bilibili_media(url, tmp_path)
    view['data']['aid'] = 123
    calls.clear()
    original_import = builtins.__import__
    def without_external_extractor(name, *args, **kwargs):
        if name == 'yt_dlp.extractor.bilibili':
            raise ImportError('synthetic external extractor unavailable')
        return original_import(name, *args, **kwargs)
    # Evict the real cached adapter so its actual import reaches the unavailable
    # external provider. No replacement module or SUT implementation is used.
    monkeypatch.delitem(sys.modules, 'backend.memory_app.v2.bilibili_comments', raising=False)
    monkeypatch.setattr(builtins, '__import__', without_external_extractor)
    assert media.read_bilibili_media(url, tmp_path) == original
    assert len(calls) == 3 and capsys.readouterr() == ('', '')


def _source_with_remaining_budget(monkeypatch, tmp_path, remaining):
    pages = {1: _comments_page([_comment(1, 9, '第一条完整评论'), _comment(2, 8, '第二条完整评论')]),
        2: _comments_page([])}
    view, calls = _official_comment_boundary(monkeypatch, pages, speech=['正'] * 16)
    url = f'https://www.bilibili.com/video/{BVID}/'
    prefix_size = len(media.read_bilibili_media(url, tmp_path)['source_text']) - 16
    length, extra = divmod(60_000 - remaining - prefix_size, 16)
    speech = ['正' * (length + (n < extra)) for n in range(16)]
    view, calls = _official_comment_boundary(monkeypatch, pages, speech=speech)
    original = media.read_bilibili_media(url, tmp_path)
    assert len(original['source_text']) == 60_000 - remaining
    view['data']['aid'] = 123
    calls.clear()
    return original, media.read_bilibili_media(url, tmp_path), calls


def test_source_budget_appends_only_whole_ranked_entries(monkeypatch, tmp_path):
    suffix = '\n\n## 评论区\n\n- 9 赞 · 第一条完整评论'
    original, captured, calls = _source_with_remaining_budget(monkeypatch, tmp_path, len(suffix))
    assert captured == {**original, 'source_text': original['source_text'] + suffix}
    assert len(captured['source_text']) == 60_000 and len(calls) == 5
    assert '第二条' not in captured['source_text']
    raw = original['source_text'].encode('utf-8')
    assert captured['source_text'].encode('utf-8')[:len(raw)] == raw


def test_highest_ranked_entry_that_cannot_fit_is_not_cut_to_fill_the_budget(monkeypatch, tmp_path):
    suffix = '\n\n## 评论区\n\n- 9 赞 · 第一条完整评论'
    original, captured, calls = _source_with_remaining_budget(monkeypatch, tmp_path, len(suffix) - 1)
    assert captured == original and len(calls) == 5
    assert '评论区' not in captured['source_text']


def test_full_source_budget_performs_no_comment_request(monkeypatch, tmp_path):
    original, captured, calls = _source_with_remaining_budget(monkeypatch, tmp_path, 0)
    assert captured == original and len(calls) == 3
    assert len(captured['source_text']) == 60_000


def test_complete_multiline_unicode_comment_stays_inside_its_section(monkeypatch, tmp_path):
    view, calls = _official_comment_boundary(monkeypatch,
        {1: _comments_page([_comment(1, 3, '中文😀\r\n## 假标题\r最后一行')]), 2: _comments_page([])})
    view['data']['aid'] = 123
    source = media.read_bilibili_media(f'https://www.bilibili.com/video/{BVID}/', tmp_path)['source_text']
    assert source.endswith('\n\n## 评论区\n\n- 3 赞 · 中文😀\n  ## 假标题\n  最后一行')
    assert len(calls) == 5
