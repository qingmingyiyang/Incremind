from __future__ import annotations

import json

import pytest

from backend.api.bilibili_favorite_collection import (
    BilibiliFavoriteCollectionError,
    BilibiliFavoriteCollectionService,
    BilibiliFavoriteSnapshotRepository,
)
from core.storage_provider import JsonObjectStore


class _Network:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def fetch_text(self, url: str) -> str:
        self.urls.append(url)
        return json.dumps(self.pages[len(self.urls) - 1], ensure_ascii=False)


def _page(*medias, has_more, count=0):
    return {
        "code": 0,
        "data": {
            "info": {
                "title": "研究收藏夹",
                "media_count": count,
                "upper": {"name": "测试用户"},
            },
            "medias": list(medias),
            "has_more": has_more,
        },
    }


def _service(tmp_path, pages):
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    return BilibiliFavoriteCollectionService(
        network=_Network(pages),
        snapshots=BilibiliFavoriteSnapshotRepository(store, namespace_id="default"),
    )


def test_resolve_reads_every_page_deduplicates_and_freezes_snapshot(tmp_path) -> None:
    service = _service(
        tmp_path,
        [
            _page(
                {"id": 1, "type": 2, "bvid": "BV1xx411c7mD", "title": "第一条", "duration": 10},
                {"id": 2, "type": 12, "title": "文章"},
                has_more=True,
                count=4,
            ),
            _page(
                {"id": 1, "type": 2, "bvid": "BV1xx411c7mD", "title": "重复"},
                {"id": 3, "type": 2, "bvid": "BV17x411w7KC", "title": "第二条", "upper": {"name": "UP主"}},
                has_more=False,
                count=4,
            ),
        ],
    )

    snapshot = service.resolve(
        source_url="https://space.bilibili.com/84912/favlist?fid=1103407912&ftype=create",
        project_id="default",
        snapshot_id="favorite-resolve-0001",
        resolved_at="2026-09-05T00:00:00Z",
    )

    assert snapshot.replayed is False
    assert snapshot.public_ref == (
        "crp://default/bilibili-favorite-snapshots/projects/default/"
        "favorite-resolve-0001/r1"
    )
    assert snapshot.payload["page_count"] == 2
    assert snapshot.payload["video_item_count"] == 2
    assert [item["bvid"] for item in snapshot.payload["items"]] == [
        "BV1xx411c7mD", "BV17x411w7KC"
    ]
    assert snapshot.payload["skipped_counts"] == {
        "non_video": 1, "unavailable": 0, "duplicate": 1
    }


def test_same_request_replays_frozen_snapshot_without_refetch(tmp_path) -> None:
    service = _service(tmp_path, [_page(has_more=False, count=0)])
    arguments = {
        "source_url": "https://www.bilibili.com/medialist/detail/ml1103407912",
        "project_id": "default",
        "snapshot_id": "favorite-resolve-0002",
        "resolved_at": "2026-09-05T00:00:00Z",
    }

    first = service.resolve(**arguments)
    replay = service.resolve(**dict(arguments, resolved_at="2026-09-06T00:00:00Z"))

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.payload["resolved_at"] == "2026-09-05T00:00:00Z"
    assert len(service.network.urls) == 1


def test_request_id_reuse_for_another_folder_fails_closed(tmp_path) -> None:
    service = _service(tmp_path, [_page(has_more=False, count=0)])
    service.resolve(
        source_url="https://www.bilibili.com/medialist/detail/ml1103407912",
        project_id="default",
        snapshot_id="favorite-resolve-0003",
        resolved_at="2026-09-05T00:00:00Z",
    )

    with pytest.raises(BilibiliFavoriteCollectionError, match="snapshot_request_conflict"):
        service.resolve(
            source_url="https://www.bilibili.com/medialist/detail/ml1103407913",
            project_id="default",
            snapshot_id="favorite-resolve-0003",
            resolved_at="2026-09-05T00:01:00Z",
        )


def test_private_folder_reports_controlled_credential_boundary(tmp_path) -> None:
    service = _service(tmp_path, [{"code": -403, "message": "forbidden"}])

    with pytest.raises(BilibiliFavoriteCollectionError, match="private_favorite_requires_login"):
        service.resolve(
            source_url="https://www.bilibili.com/medialist/detail/ml1103407912",
            project_id="default",
            snapshot_id="favorite-private-0001",
            resolved_at="2026-09-05T00:00:00Z",
        )


def test_repeated_page_is_rejected_instead_of_silently_truncating(tmp_path) -> None:
    repeated = _page(
        {"id": 1, "type": 2, "bvid": "BV1xx411c7mD", "title": "第一条"},
        has_more=True,
        count=30,
    )
    service = _service(tmp_path, [repeated, repeated])

    with pytest.raises(BilibiliFavoriteCollectionError, match="favorite_pagination_stalled"):
        service.resolve(
            source_url="https://www.bilibili.com/medialist/detail/ml1103407912",
            project_id="default",
            snapshot_id="favorite-stalled-0001",
            resolved_at="2026-09-05T00:00:00Z",
        )
