from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from backend.replay.contracts import KnowledgeItem, KnowledgeSource, QuickCaptureRequest
from backend.replay.items import KnowledgeItemService
from backend.replay.library import ReplayLibrary


def test_quick_capture_persists_item_inbox_journal_and_indexes(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    service = KnowledgeItemService(library)

    item = service.capture(
        QuickCaptureRequest(
            type="thought",
            title="桌面边界",
            content="报告生成保留在 Python 服务层。",
            tags=["架构", "复盘", "架构"],
            linked_video_ids=["video-1"],
            linked_chunk_ids=["chunk-001"],
            linked_frame_ids=["frame-0001"],
            timestamps=["00:01:20"],
            quotes=["业务不能写死在 Electron"],
            include_in_daily=True,
        )
    )

    assert library.get_item(item.id) == item
    assert library.journal_items(item.created_at[:10]) == [item]
    assert item.links.linked_video_ids == ["video-1"]
    assert item.evidence.chunk_ids == ["chunk-001"]
    assert item.evidence.frame_ids == ["frame-0001"]
    assert item.status.in_daily is True

    root = tmp_path / "library"
    assert (root / "inbox" / f"{item.created_at[:10]}.md").is_file()
    assert (root / "inbox" / "raw" / f"{item.id}.json").is_file()
    search = json.loads((root / "index" / "search.json").read_text(encoding="utf-8"))
    timeline = json.loads((root / "index" / "timeline.json").read_text(encoding="utf-8"))
    assert search[item.id]["type"] == "thought"
    assert timeline[0]["id"] == item.id
    assert not list(root.rglob("*.tmp"))


def test_capture_without_title_derives_title_and_can_skip_daily(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    item = KnowledgeItemService(library).capture(
        QuickCaptureRequest(type="question", content="# 还需要验证什么？\n检查真实桌面启动。", include_in_daily=False)
    )

    assert item.title == "还需要验证什么？"
    assert item.status.in_daily is False
    assert library.journal_items(item.created_at[:10]) == []
    assert library.search_items("桌面启动") == [item]


def test_library_rejects_unsafe_item_id(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    malicious = KnowledgeItem(id="../outside", type="note", title="x", source=KnowledgeSource(kind="manual"))

    with pytest.raises(ValueError, match="unsafe"):
        library.save_item(malicious)

    assert not (tmp_path / "outside.json").exists()


def test_capture_rejects_blank_content(tmp_path) -> None:
    service = KnowledgeItemService(ReplayLibrary(tmp_path / "library"))

    with pytest.raises(ValueError, match="不能为空"):
        service.capture(QuickCaptureRequest(type="note", content="  "))


def test_concurrent_index_refresh_uses_unique_atomic_temp_files(tmp_path) -> None:
    root = tmp_path / "library"
    libraries = [ReplayLibrary(root), ReplayLibrary(root)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(libraries[index % 2].refresh_indexes) for index in range(40)]
        for future in futures:
            future.result()

    assert json.loads((root / "index" / "reports.json").read_text(encoding="utf-8")) == []
    assert not list(root.rglob("*.tmp"))
