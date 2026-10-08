from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from backend.api.bilibili_audio_fetcher import (
    BilibiliAnonymousAudioFetcher,
    BilibiliAudioFetchError,
)
from backend.security import DownloadedBinary
from core.job_runner.media_execution_receipt import media_job_uri_segment
from core.source_processing import SourceManifestCodec


BVID = "BV1xx411c7mD"


class TextNetwork:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = []

    def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        return self.response


class BinaryNetwork:
    def __init__(self, path: Path, *, media_type: str = "audio/mp4") -> None:
        self.path = path
        self.media_type = media_type
        self.calls = []

    def download(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"audio")
        return DownloadedBinary(self.path, 5, self.media_type)

    def staged_path(self, relative_path):
        self.calls.append(("staged_path", relative_path))
        return self.path


def manifest(*, granted: bool = True):
    return SourceManifestCodec.decode({
        "schema_version": "1.0.0",
        "source_id": "bili-source",
        "source_ref": "crp://default/sources/bili-source",
        "platform": "bilibili",
        "input_identity": f"https://www.bilibili.com/video/{BVID}/",
        "resolver_revision": "bilibili-view-api-v1",
        "normalizer_revision": "bilibili-manifest-v1",
        "content_kind": "video",
        "body": None,
        "metadata": {"bvid": BVID, "cid": 101, "title": "视频", "duration_seconds": 5},
        "permission": {
            "decision": "granted" if granted else "unknown",
            "evidence_refs": ["crp://default/evidence/bili"],
        },
        "provenance_refs": ["crp://default/evidence/bili"],
        "assets": [{
            "asset_id": "video-1", "ordinal": 0, "kind": "video", "media_type": None,
            "role": "primary", "locator": f"https://www.bilibili.com/video/{BVID}/",
            "source_ref": "crp://default/sources/bili-source/assets/video-1",
            "relations": [], "evidence_refs": ["crp://default/evidence/bili"],
        }],
    })


def playurl(url="https://cdn.bilivideo.com/audio.m4s"):
    return json.dumps({"code": 0, "data": {"dash": {"audio": [
        {"baseUrl": url, "bandwidth": 64000, "codecs": "mp4a.40.2"},
        {"baseUrl": "https://low.bilivideo.com/audio.m4s", "bandwidth": 32000},
    ]}}})


def test_fetch_derives_playurl_cdn_and_staging_identity_from_manifest(tmp_path: Path) -> None:
    text = TextNetwork(playurl())
    binary = BinaryNetwork(tmp_path / "staging" / "audio.m4s")
    checks = []
    result = BilibiliAnonymousAudioFetcher(text, binary).fetch(
        manifest(), job_id="media_hands:bili-source:analyze_source",
        max_download_bytes=100, timeout_seconds=5,
        control_check=lambda: checks.append("check"),
    )

    assert text.calls == [
        f"https://api.bilibili.com/x/player/playurl?bvid={BVID}&cid=101&fnval=16&fnver=0&fourk=0"
    ]
    assert binary.calls[0][0] == "https://cdn.bilivideo.com/audio.m4s"
    arguments = binary.calls[0][1]
    assert arguments["relative_path"].endswith("/source-audio.m4s")
    assert ":" not in arguments["relative_path"]
    assert arguments["max_response_bytes"] == 100
    assert arguments["timeout_seconds"] == 5
    assert "Cookie" not in arguments["headers"]
    assert arguments["control_check"] is not None
    assert result.byte_count == 5 and result.codec == "mp4a.40.2"
    assert checks == ["check"]


def test_fetch_canonicalizes_bilibili_octet_stream_as_mp4_audio(tmp_path: Path) -> None:
    binary = BinaryNetwork(
        tmp_path / "staging" / "audio.m4s",
        media_type="application/octet-stream",
    )

    result = BilibiliAnonymousAudioFetcher(TextNetwork(playurl()), binary).fetch(
        manifest(), job_id="media_hands:bili-source:analyze_source",
        max_download_bytes=100, timeout_seconds=5,
    )

    assert result.media_type == "audio/mp4"


@pytest.mark.parametrize(
    "payload,reason",
    [
        (json.dumps({"code": 0, "data": {"dash": {"audio": [
            {"baseUrl": "https://evil.example/audio.m4s", "bandwidth": 64000}
        ]}}}), "url_rejected"),
        (json.dumps({"code": 0, "data": {"dash": {"audio": []}}}), "audio_unavailable"),
        ("not-json", "payload_invalid"),
    ],
)
def test_fetch_rejects_untrusted_or_missing_platform_audio_before_binary_download(
    tmp_path: Path, payload: str, reason: str
) -> None:
    binary = BinaryNetwork(tmp_path / "audio.m4s")
    with pytest.raises(BilibiliAudioFetchError, match=reason):
        BilibiliAnonymousAudioFetcher(TextNetwork(payload), binary).fetch(
            manifest(), job_id="media_hands:bili-source:analyze_source",
            max_download_bytes=100, timeout_seconds=5
        )
    assert binary.calls == []


def test_fetch_requires_granted_frozen_manifest_and_positive_budget(tmp_path: Path) -> None:
    binary = BinaryNetwork(tmp_path / "audio.m4s")
    fetcher = BilibiliAnonymousAudioFetcher(TextNetwork(playurl()), binary)
    with pytest.raises(BilibiliAudioFetchError, match="not_authorized"):
        fetcher.fetch(manifest(granted=False), job_id="job-1234567890123456", max_download_bytes=100, timeout_seconds=5)
    with pytest.raises(BilibiliAudioFetchError, match="budget_exhausted"):
        fetcher.fetch(manifest(), job_id="job-1234567890123456", max_download_bytes=0, timeout_seconds=5)
    assert binary.calls == []


def test_restore_uses_verified_receipt_and_staging_without_platform_network(tmp_path: Path) -> None:
    job_id = "media_hands:bili-source:analyze_source"
    path = tmp_path / "staging" / "source-audio.m4s"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio")
    text = TextNetwork(playurl())
    binary = BinaryNetwork(path)
    segment = media_job_uri_segment(job_id)
    namespace_id = "tenant-media"
    receipt = {
        "step_name": "fetch_audio",
        "output_ref": f"crp://{namespace_id}/jobs/{segment}/staging/source-audio",
        "consumed": {"download_octets": 5},
    }
    receipt["output_state_hash"] = "sha256:" + hashlib.sha256(json.dumps(
        {"output_ref": receipt["output_ref"], "byte_count": 5, "media_type": "audio/mp4"},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    restored = BilibiliAnonymousAudioFetcher(text, binary).restore(
        job_id=job_id, namespace_id=namespace_id, receipt=receipt
    )

    assert restored.path == str(path) and restored.byte_count == 5
    assert text.calls == []
    assert binary.calls == [("staged_path", f"{segment}/source-audio.m4s")]

    path.write_bytes(b"changed")
    with pytest.raises(BilibiliAudioFetchError, match="staging_receipt_mismatch"):
        BilibiliAnonymousAudioFetcher(text, binary).restore(
            job_id=job_id, namespace_id=namespace_id, receipt=receipt
        )
