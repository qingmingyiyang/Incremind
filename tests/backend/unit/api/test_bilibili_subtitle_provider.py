from __future__ import annotations

import json

import pytest

from backend.api.bilibili_subtitle_provider import (
    BilibiliOfficialSubtitleProvider,
    BilibiliSubtitleProviderError,
)
from core.source_processing import SourceManifestCodec


BVID = "BV1xx411c7mD"


class _TextNetwork:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        return self.responses[url]


def _manifest(**metadata: object):
    values = {"bvid": BVID, "cid": 101, "title": "A title", "duration_seconds": 9}
    values.update(metadata)
    return SourceManifestCodec.decode(
        {
            "schema_version": "1.0.0",
            "source_id": "bili-source",
            "source_ref": "crp://default/sources/bili-source",
            "platform": "bilibili",
            "input_identity": f"https://www.bilibili.com/video/{BVID}/",
            "resolver_revision": "bilibili-view-api-v1",
            "normalizer_revision": "bilibili-manifest-v1",
            "content_kind": "video",
            "body": None,
            "metadata": values,
            "permission": {"decision": "granted", "evidence_refs": ["crp://default/evidence/p1"]},
            "provenance_refs": ["crp://default/evidence/p1"],
            "assets": [
                {
                    "asset_id": "video-1",
                    "ordinal": 0,
                    "kind": "video",
                    "media_type": None,
                    "role": "primary",
                    "locator": f"https://www.bilibili.com/video/{BVID}/",
                    "source_ref": "crp://default/sources/bili-source/assets/video-1",
                    "relations": [],
                    "evidence_refs": ["crp://default/evidence/p1"],
                }
            ],
        }
    )


def _catalog(*subtitles: dict[str, object]) -> str:
    return json.dumps({"code": 0, "data": {"subtitle": {"subtitles": list(subtitles)}}})


def _subtitle(*body: dict[str, object]) -> str:
    return json.dumps({"body": list(body)})


def test_resolves_preferred_official_subtitle_into_normalized_transcript_and_chunks() -> None:
    catalog_url = f"https://api.bilibili.com/x/player/v2?bvid={BVID}&cid=101"
    zh_url = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/zh.json"
    en_url = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/en.json"
    network = _TextNetwork(
        {
            catalog_url: _catalog(
                {"lan": "en", "subtitle_url": en_url},
                {"lan": "zh-Hans", "subtitle_url": zh_url},
            ),
            zh_url: _subtitle(
                {"from": 0, "to": 1.5, "content": " 第一 <b>句话</b> "},
                {"from": 1.5, "to": 4, "content": "第二\u0000句话"},
            ),
            en_url: _subtitle({"from": 0, "to": 1, "content": "must not fetch"}),
        }
    )

    outcome = BilibiliOfficialSubtitleProvider(network).resolve(_manifest())

    assert outcome.available is True and outcome.unavailable_reason is None
    assert outcome.transcript == {
        "title": "A title",
        "language": "zh-hans",
        "duration_seconds": 9.0,
        "source": "official_subtitle",
        "segments": [
            {"start_seconds": 0.0, "end_seconds": 1.5, "text": "第一 句话"},
            {"start_seconds": 1.5, "end_seconds": 4.0, "text": "第二 句话"},
        ],
    }
    assert outcome.chunks[0]["source_type"] == "official_subtitle"
    assert outcome.chunks[0]["chunk_id"] == "chunk-0001-0000000000"


def test_normalizes_bilibili_protocol_relative_subtitle_url_to_https() -> None:
    catalog_url = f"https://api.bilibili.com/x/player/v2?bvid={BVID}&cid=101"
    absolute_url = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/zh.json"
    network = _TextNetwork({
        catalog_url: _catalog({"lan": "zh-Hans", "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/zh.json"}),
        absolute_url: _subtitle({"from": 0, "to": 1, "content": "字幕"}),
    })

    outcome = BilibiliOfficialSubtitleProvider(network).resolve(_manifest())

    assert outcome.available is True
    assert network.calls == [catalog_url, absolute_url]


@pytest.mark.parametrize(
    "subtitle_url",
    [
        "http://aisubtitle.hdslb.com/a.json",
        "https://user@aisubtitle.hdslb.com/a.json",
        "https://aisubtitle.hdslb.com:444/a.json",
        "https://evil.aisubtitle.hdslb.com/a.json",
        "https://aisubtitle.hdslb.com.evil/a.json",
    ],
)
def test_rejects_noncanonical_subtitle_urls_without_following_them(subtitle_url: str) -> None:
    catalog_url = f"https://api.bilibili.com/x/player/v2?bvid={BVID}&cid=101"
    network = _TextNetwork({catalog_url: _catalog({"lan": "zh-Hans", "subtitle_url": subtitle_url})})

    outcome = BilibiliOfficialSubtitleProvider(network).resolve(_manifest())

    assert outcome.available is False
    assert outcome.unavailable_reason == "official_subtitle_url_rejected"
    assert network.calls == [catalog_url]


@pytest.mark.parametrize(
    "catalog",
    [
        _catalog(),
        "not-json",
        json.dumps({"code": -404, "message": "raw upstream detail"}),
    ],
)
def test_catalog_absence_or_failure_is_an_explicit_asr_fallback_outcome(catalog: str) -> None:
    catalog_url = f"https://api.bilibili.com/x/player/v2?bvid={BVID}&cid=101"
    network = _TextNetwork({catalog_url: catalog})

    outcome = BilibiliOfficialSubtitleProvider(network).resolve(_manifest())

    assert outcome.available is False
    assert outcome.unavailable_reason in {
        "official_subtitle_unavailable",
        "official_subtitle_catalog_unavailable",
    }
    assert "raw upstream detail" not in str(outcome.unavailable_reason)


@pytest.mark.parametrize(
    "body",
    [
        ({"from": 2, "to": 1, "content": "reverse"},),
        ({"from": 1, "to": 1.5, "content": "first"}, {"from": 0.5, "to": 2, "content": "out of order"}),
        ({"from": 0, "to": 1, "content": ""},),
        ({"from": True, "to": 1, "content": "boolean"},),
    ],
)
def test_invalid_cues_return_explicit_fallback_without_partially_using_them(body) -> None:
    catalog_url = f"https://api.bilibili.com/x/player/v2?bvid={BVID}&cid=101"
    subtitle_url = "https://aisubtitle.hdslb.com/valid.json"
    network = _TextNetwork(
        {catalog_url: _catalog({"lan": "zh-Hans", "subtitle_url": subtitle_url}), subtitle_url: _subtitle(*body)}
    )

    outcome = BilibiliOfficialSubtitleProvider(network).resolve(_manifest())

    assert outcome.available is False
    assert outcome.transcript is None and outcome.chunks == ()
    assert outcome.unavailable_reason == "official_subtitle_invalid"


def test_manifest_requires_frozen_bilibili_metadata_before_network() -> None:
    provider = BilibiliOfficialSubtitleProvider(_TextNetwork({}))
    with pytest.raises(BilibiliSubtitleProviderError, match="manifest_identity_invalid"):
        provider.resolve(_manifest(cid=0))
