from __future__ import annotations

import json

import pytest

from backend.api.bilibili_platform_provider import (
    BilibiliMetadataEvidenceRepository,
    BilibiliMetadataProviderError,
    BilibiliViewApiPlatformProvider,
)
from backend.security.network_adapter import BoundedHttpResponse, PinnedHttpRequest, SafeTextNetworkAdapter
from core.source_processing import MediaRouter, SourceManifestCodec
from core.storage_provider import JsonObjectStore


PUBLIC_IP = "93.184.216.34"
BVID = "BV1xx411c7mD"


def _payload() -> str:
    return json.dumps(
        {
            "code": 0,
            "data": {
                "bvid": BVID,
                "title": "Series title",
                "desc": "Public description",
                "duration": 180,
                "pubdate": 1_725_638_400,
                "owner": {"name": "Uploader", "mid": 123},
                "pages": [
                    {"page": 1, "cid": 101, "part": "Part one", "duration": 60},
                    {"page": 2, "cid": 102, "part": "Part two", "duration": 120},
                ],
                "cookie": "must-not-survive",
            },
        },
        ensure_ascii=False,
    )


def _provider(tmp_path, *, response: str | None = None, requests=None):
    recorded = requests if requests is not None else []

    def transport(request: PinnedHttpRequest) -> BoundedHttpResponse:
        recorded.append(request)
        return BoundedHttpResponse(
            200,
            {"Content-Type": "application/json; charset=utf-8"},
            (response if response is not None else _payload()).encode("utf-8"),
        )

    network = SafeTextNetworkAdapter(
        resolver=lambda host, port: (PUBLIC_IP,),
        transport=transport,
        allowed_hosts=("api.bilibili.com",),
        max_response_bytes=64 * 1024,
    )
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    return (
        BilibiliViewApiPlatformProvider(
            network=network,
            evidence=BilibiliMetadataEvidenceRepository(store, namespace_id="default"),
            namespace_id="default",
        ),
        store,
        recorded,
    )


def test_anonymous_view_api_provider_builds_strict_manifest_and_evidence(tmp_path) -> None:
    provider, store, requests = _provider(tmp_path)
    manifest = provider.provide(
        f"分享 https://www.bilibili.com/video/{BVID}/?p=2&utm_source=ignored#fragment",
        project_id="project-1",
    )

    assert SourceManifestCodec.decode(SourceManifestCodec.encode(manifest)) == manifest
    assert manifest.source_id == f"bili-{BVID}-p2"
    assert manifest.input_identity == f"https://www.bilibili.com/video/{BVID}/?p=2"
    assert manifest.content_kind == "video" and manifest.permission.decision == "unknown"
    metadata = dict(manifest.metadata.entries)
    assert metadata["title"] == "Part two" and metadata["cid"] == 102
    assert "cookie" not in metadata and "mid" not in metadata
    assert requests[0].host == "api.bilibili.com"
    assert requests[0].target == f"/x/web-interface/view?bvid={BVID}"
    evidence_ref = manifest.provenance_refs[0]
    assert evidence_ref.startswith(
        "crp://default/source-resolution-evidence/projects/project-1/"
    )
    records = store.list(BilibiliMetadataEvidenceRepository.collection)
    assert len(records) == 1
    encoded_record = json.dumps(records[0], ensure_ascii=False)
    assert "must-not-survive" not in encoded_record
    assert MediaRouter().route(manifest).reason == "source_permission_unresolved"
    assert not (tmp_path / "library").exists()
    assert not (tmp_path / "data" / "bilibili").exists()


def test_same_metadata_replays_evidence_but_drift_conflicts(tmp_path) -> None:
    provider, _, _ = _provider(tmp_path)
    url = f"https://www.bilibili.com/video/{BVID}/"
    first = provider.provide(url, project_id="project-1")
    assert provider.provide(url, project_id="project-1") == first

    changed = json.loads(_payload())
    changed["data"]["title"] = "Changed"
    changed_provider, _, _ = _provider(tmp_path, response=json.dumps(changed))
    with pytest.raises(BilibiliMetadataProviderError, match="metadata_identity_conflict"):
        changed_provider.provide(url, project_id="project-1")


@pytest.mark.parametrize(
    "source",
    [
        f"http://www.bilibili.com/video/{BVID}/",
        f"https://user@www.bilibili.com/video/{BVID}/",
        f"https://bilibili.com.evil/video/{BVID}/",
        f"https://space.bilibili.com/video/{BVID}/",
        "https://www.bilibili.com/video/not-a-bvid/",
        f"https://www.bilibili.com/video/{BVID}/?p=0",
        f"https://www.bilibili.com/video/{BVID}/ https://www.bilibili.com/video/{BVID}/",
    ],
)
def test_invalid_source_is_rejected_before_network(tmp_path, source: str) -> None:
    provider, _, requests = _provider(tmp_path)
    with pytest.raises(BilibiliMetadataProviderError, match="invalid_source"):
        provider.provide(source, project_id="project-1")
    assert requests == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ("not json", "metadata_unavailable"),
        (json.dumps({"code": -404, "message": "secret raw detail"}), "metadata_unavailable"),
        (json.dumps({"code": 0, "data": {"bvid": "BV1wrong00000", "pages": []}}), "metadata_identity_mismatch"),
    ],
)
def test_raw_api_failures_are_reduced_to_stable_codes(tmp_path, response: str, code: str) -> None:
    provider, _, _ = _provider(tmp_path, response=response)
    with pytest.raises(BilibiliMetadataProviderError) as captured:
        provider.provide(f"https://www.bilibili.com/video/{BVID}/", project_id="project-1")
    assert str(captured.value) == code
    assert "secret raw detail" not in str(captured.value)


def test_external_text_is_bounded_and_control_characters_are_removed(tmp_path) -> None:
    payload = json.loads(_payload())
    payload["data"]["title"] = "A\x00\x1f" + "x" * 600
    payload["data"]["desc"] = "D\x7f\x85" + "y" * 5000
    payload["data"]["owner"]["name"] = "U\x00" + "z" * 200
    provider, store, _ = _provider(tmp_path, response=json.dumps(payload))
    manifest = provider.provide(
        f"https://www.bilibili.com/video/{BVID}/", project_id="project-1"
    )
    metadata = dict(manifest.metadata.entries)
    assert len(metadata["series_title"]) == 300
    assert len(metadata["description"]) == 4000
    assert len(metadata["uploader"]) == 100
    encoded = json.dumps(store.list(BilibiliMetadataEvidenceRepository.collection), ensure_ascii=False)
    assert "\\u0000" not in encoded and "\\u007f" not in encoded and "\\u0085" not in encoded


def test_invalid_cid_fails_before_evidence_write(tmp_path) -> None:
    payload = json.loads(_payload())
    payload["data"]["pages"][0]["cid"] = 0
    provider, store, _ = _provider(tmp_path, response=json.dumps(payload))
    with pytest.raises(BilibiliMetadataProviderError, match="metadata_unavailable"):
        provider.provide(f"https://www.bilibili.com/video/{BVID}/", project_id="project-1")
    assert store.list(BilibiliMetadataEvidenceRepository.collection) == ()
