from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.video_intake.models import LibraryRecord, RecordDetail


class FakeIntakeService:
    def __init__(self, detail: RecordDetail | None) -> None:
        self._detail = detail

    def detail(self, record_id: str) -> RecordDetail | None:
        if self._detail is None or self._detail.record.id != record_id:
            return None
        return self._detail


def _client(tmp_path, *, detail: RecordDetail | None = None) -> TestClient:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    app.state.intake_service = FakeIntakeService(detail)
    return TestClient(app, headers={"X-Series-Id": "default"})


def _video_detail(relative_dir: str = "2026/06/23/测试视频 [BV1TEST]") -> RecordDetail:
    record = LibraryRecord(
        id="BV1TEST",
        bvid="BV1TEST",
        title="测试视频",
        source_url="https://www.bilibili.com/video/BV1TEST/",
        uploader="测试 UP",
        imported_at="2026-06-23T10:00:00+08:00",
        tags=["Rust", "桌面应用"],
        status="completed",
        relative_dir=relative_dir,
    )
    return RecordDetail(
        record=record,
        summary={"thirty_second_summary": "这是视频摘要。"},
        structured={
            "summary": {
                "thirty_second": "这是视频摘要。",
                "core_question": "如何划分桌面业务边界？",
                "main_conclusions": ["业务保留在服务层。"],
                "detailed_notes": ["Electron 只负责系统能力。"],
            },
            "chunks": [
                {"chunk_id": "chunk-0001", "keywords": ["Rust", "Electron"], "text": "业务保留在服务层。"}
            ],
            "claims": [
                {"timestamp": "00:01:20", "claim": "业务边界明确", "evidence_text": "Electron 只负责系统能力。"}
            ],
            "visual_analysis": {
                "cloud_vision_mode": "mock",
                "keyframes": [{"frame_id": "frame-0001", "timestamp_text": "00:01:20"}],
            },
        },
    )


def test_quick_capture_and_list_api_persist_to_library(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/replay/items",
            json={
                "type": "thought",
                "title": "服务边界",
                "content": "报告生成放在 Python 服务。",
                "tags": ["架构"],
                "linked_video_ids": ["BV1TEST"],
                "linked_chunk_ids": ["chunk-0001"],
                "linked_frame_ids": ["frame-0001"],
                "timestamps": ["00:01:20"],
                "include_in_daily": True,
            },
        )

        assert response.status_code == 201
        item = response.json()
        assert item["type"] == "thought"
        assert item["status"]["in_daily"] is True
        assert client.get(f"/api/replay/items/{item['id']}").json() == item
        assert client.get("/api/replay/items", params={"type": "thought", "in_daily": True}).json() == [item]
        assert client.get("/api/replay/items", params={"query": "Python"}).json() == [item]
        status_payload = client.get("/api/replay/status").json()
        assert status_payload["item_count"] == 1
        assert status_payload["today"] == item["created_at"][:10]
        assert status_payload["today_item_count"] == 1

    assert (tmp_path / "library" / "series" / "default" / "knowledge" / "items" / f"{item['id']}.json").is_file()


def test_video_detail_converts_to_traceable_daily_knowledge_item(tmp_path) -> None:
    detail = _video_detail()
    with _client(tmp_path, detail=detail) as client:
        response = client.post(
            "/api/replay/videos/BV1TEST/knowledge-item",
            json={"include_in_daily": True, "high_value": True, "need_review": True},
        )

        assert response.status_code == 200
        item = response.json()
        assert item["id"] == "video_BV1TEST"
        assert item["source"]["bvid"] == "BV1TEST"
        assert item["source"]["url"].endswith("BV1TEST/")
        assert item["evidence"]["chunk_ids"] == ["chunk-0001"]
        assert item["evidence"]["frame_ids"] == ["frame-0001"]
        assert item["evidence"]["timestamps"] == ["00:01:20"]
        assert item["status"] | {"in_daily": True, "high_value": True, "need_review": True} == item["status"]
        assert item["index"]["available"] is True
        daily_response = client.post("/api/replay/reports/daily/2026-06-23")
        assert daily_response.status_code == 200
        daily = daily_response.json()
        assert daily["sources"]["videos"] == ["video_BV1TEST"]
        assert daily["video_knowledge"][0]["video_id"] == "BV1TEST"
        assert daily["video_knowledge"][0]["bvid"] == "BV1TEST"
        video_evidence = next(entry for entry in daily["evidence"] if entry["source_type"] == "video")
        assert video_evidence | {
            "video_id": "BV1TEST",
            "bvid": "BV1TEST",
            "chunk_id": "chunk-0001",
            "frame_id": "frame-0001",
            "timestamp": "00:01:20",
        } == video_evidence

    journal_path = tmp_path / "library" / "series" / "default" / "journals" / "2026" / "06" / "2026-06-23" / "items.json"
    assert journal_path.is_file()
    assert "video_BV1TEST" in journal_path.read_text(encoding="utf-8")
    assert (journal_path.parent / "daily.json").is_file()
    assert "BV1TEST" in (journal_path.parent / "daily.md").read_text(encoding="utf-8")


def test_video_knowledge_actions_merge_without_clearing_existing_flags(tmp_path) -> None:
    with _client(tmp_path, detail=_video_detail()) as client:
        daily = client.post(
            "/api/replay/videos/BV1TEST/knowledge-item",
            json={"include_in_daily": True},
        )
        high_value = client.post(
            "/api/replay/videos/BV1TEST/knowledge-item",
            json={"include_in_daily": False, "high_value": True},
        )
        need_review = client.post(
            "/api/replay/videos/BV1TEST/knowledge-item",
            json={"include_in_daily": False, "need_review": True},
        )

        assert daily.status_code == high_value.status_code == need_review.status_code == 200
        assert need_review.json()["status"] | {
            "in_daily": True,
            "high_value": True,
            "need_review": True,
        } == need_review.json()["status"]
        items = client.get("/api/replay/items", params={"type": "video"}).json()
        assert len(items) == 1
        assert items[0]["id"] == "video_BV1TEST"

    assert len((tmp_path / "library" / "series" / "default" / "journals" / "2026" / "06" / "2026-06-23" / "items.json").read_text(encoding="utf-8").split("video_BV1TEST")) == 2


def test_video_conversion_rejects_path_outside_library(tmp_path) -> None:
    with _client(tmp_path, detail=_video_detail("../../outside")) as client:
        response = client.post(
            "/api/replay/videos/BV1TEST/knowledge-item",
            json={"include_in_daily": True},
        )

    assert response.status_code == 422
    assert "超出资料库范围" in response.json()["detail"]
    assert not (tmp_path / "outside").exists()


def test_replay_api_returns_clear_errors(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/api/replay/items", json={"type": "note", "content": "   "}).status_code == 422
        assert client.get("/api/replay/items/missing").status_code == 404
        assert client.post(
            "/api/replay/videos/missing/knowledge-item",
            json={"include_in_daily": True},
        ).status_code == 404


def test_daily_report_api_writes_json_markdown_and_task(tmp_path, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("backend.api.routes.replay._open_folder", lambda path: opened.append(str(path)))
    with _client(tmp_path) as client:
        created = client.post(
            "/api/replay/items",
            json={"type": "action", "content": "完成日报 API 验证。", "include_in_daily": True},
        ).json()
        day = created["created_at"][:10]

        response = client.post(f"/api/replay/reports/daily/{day}")

        assert response.status_code == 200
        report = response.json()
        assert report["type"] == "daily"
        assert report["sources"]["actions"] == [created["id"]]
        assert client.get("/api/replay/reports", params={"type": "daily"}).json() == [report]
        assert client.get(f"/api/replay/reports/{report['report_id']}").json() == report
        tasks = client.get("/api/replay/tasks").json()
        assert tasks[0]["status"] == "success"
        assert tasks[0]["source_count"] == 1
        status_payload = client.get("/api/replay/status").json()
        assert status_payload["daily_report_ready"] is True
        markdown = client.get(f"/api/replay/reports/{report['report_id']}/export.md")
        exported_json = client.get(f"/api/replay/reports/{report['report_id']}/export.json")
        assert markdown.status_code == 200
        assert markdown.headers["content-disposition"].endswith(f'filename="{day}.md"')
        assert "完成日报 API 验证" in markdown.text
        assert exported_json.status_code == 200
        assert exported_json.json()["report_id"] == report["report_id"]
        assert client.post(f"/api/replay/reports/{report['report_id']}/open-folder").status_code == 200
        assert client.post(f"/api/replay/journals/{day}/open-folder").status_code == 200
        assert opened == [
            str(tmp_path / "library" / "series" / "default" / "reports" / "daily"),
            str(tmp_path / "library" / "series" / "default" / "journals" / day[:4] / day[5:7] / day),
        ]

    journal = tmp_path / "library" / "series" / "default" / "journals" / day[:4] / day[5:7] / day
    assert (journal / "daily.json").is_file()
    assert (journal / "daily.md").is_file()


def test_daily_report_api_rejects_invalid_date(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/api/replay/reports/daily/2026-6-23")

    assert response.status_code == 422
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_report_file_actions_reject_missing_or_unsafe_ids(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/replay/reports/missing/export.md").status_code == 404
        assert client.get("/api/replay/reports/../outside/export.json").status_code == 404
        assert client.post("/api/replay/reports/missing/open-folder").status_code == 404
        invalid_day = client.post("/api/replay/journals/2026-6-23/open-folder")

    assert invalid_day.status_code == 422
    assert "YYYY-MM-DD" in invalid_day.json()["detail"]


def test_weekly_report_api_aggregates_existing_daily_reports(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/api/replay/reports/daily/2026-06-22").status_code == 200
        assert client.post("/api/replay/reports/daily/2026-06-23").status_code == 200

        response = client.post("/api/replay/reports/weekly/2026-W26")

        assert response.status_code == 200
        weekly = response.json()
        assert weekly["type"] == "weekly"
        assert weekly["sources"]["previous_reports"] == ["daily_2026-06-23", "daily_2026-06-22"]
        assert client.get("/api/replay/reports", params={"type": "weekly"}).json() == [weekly]
        assert client.post("/api/replay/reports/weekly/2026-W99").status_code == 422


def test_monthly_report_api_aggregates_existing_weekly_reports(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/api/replay/reports/weekly/2026-W26").status_code == 200
        assert client.post("/api/replay/reports/weekly/2026-W27").status_code == 200

        response = client.post("/api/replay/reports/monthly/2026-06")

        assert response.status_code == 200
        monthly = response.json()
        assert monthly["type"] == "monthly"
        assert monthly["sources"]["previous_reports"] == ["weekly_2026-W27", "weekly_2026-W26"]
        assert client.get("/api/replay/reports", params={"type": "monthly"}).json() == [monthly]
        assert client.post("/api/replay/reports/monthly/2026-13").status_code == 422


def test_yearly_report_api_aggregates_existing_monthly_reports(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/api/replay/reports/monthly/2026-06").status_code == 200
        assert client.post("/api/replay/reports/monthly/2026-07").status_code == 200

        response = client.post("/api/replay/reports/yearly/2026")

        assert response.status_code == 200
        yearly = response.json()
        assert yearly["type"] == "yearly"
        assert yearly["sources"]["previous_reports"] == ["monthly_2026-07", "monthly_2026-06"]
        assert client.get("/api/replay/reports", params={"type": "yearly"}).json() == [yearly]
        assert client.post("/api/replay/reports/yearly/26").status_code == 422


def test_memory_qa_api_returns_sources_and_fixed_no_evidence_message(tmp_path) -> None:
    with _client(tmp_path, detail=_video_detail()) as client:
        assert client.post(
            "/api/replay/videos/BV1TEST/knowledge-item",
            json={"include_in_daily": True},
        ).status_code == 200
        assert client.post("/api/replay/reports/daily/2026-06-23").status_code == 200

        answer_response = client.post(
            "/api/replay/memory/ask",
            json={"question": "BV1TEST 的桌面业务边界是什么？", "persist": False},
        )
        missing_response = client.post(
            "/api/replay/memory/ask",
            json={"question": "火星农业有什么结论？", "persist": False},
        )

        assert answer_response.status_code == 200
        answer = answer_response.json()
        assert answer["evidence_sufficient"] is True
        reference = next(item for item in answer["references"] if item["bvid"] == "BV1TEST")
        assert reference["video_id"] == "BV1TEST"
        assert reference["chunk_id"] == "chunk-0001"
        assert reference["frame_id"] == "frame-0001"
        assert missing_response.json()["answer"] == "当前系列资料库中没有找到足够证据。"
        assert missing_response.json()["references"] == []


def test_dashboard_api_initializes_journal_without_automatic_report_generation(tmp_path) -> None:
    with _client(tmp_path) as client:
        before_tasks = client.get("/api/replay/tasks").json()
        before_reports = client.get("/api/replay/reports").json()

        response = client.get("/api/replay/dashboard")

        assert response.status_code == 200
        dashboard = response.json()
        assert dashboard["journal_ready"] is True
        assert len(dashboard["report_states"]) == 5
        assert client.get("/api/replay/tasks").json() == before_tasks
        assert client.get("/api/replay/reports").json() == before_reports
