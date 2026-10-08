from __future__ import annotations

from types import SimpleNamespace
import json

from fastapi.testclient import TestClient

from backend.api.app import create_app
import backend.api.routes.series as series_routes
from backend.replay.contracts import UpdateIntakeRequest
from backend.video_intake.models import ResolvedVideoItem
from backend.video_intake.storage import LibraryStorage


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def test_series_trash_list_is_not_captured_as_a_series_identity(tmp_path) -> None:
    with _client(tmp_path) as client:
        trash = client.get("/api/series/trash")
        assert trash.status_code == 200
        assert trash.json() == []
        current = client.get("/api/series/default")
        assert current.status_code == 200
        assert current.json()["series_id"] == "default"
        assert client.get("/api/series/not-a-real-series").status_code == 404


def test_series_intake_promote_and_isolation_api(tmp_path) -> None:
    with _client(tmp_path) as client:
        default = client.get("/api/series")
        assert default.status_code == 200
        assert default.json()[0]["series_id"] == "default"

        created = client.post("/api/series", json={"name": "项目研究", "description": "隔离测试"})
        assert created.status_code == 201
        other_id = created.json()["series_id"]
        assert client.post(f"/api/series/{other_id}/activate").status_code == 200

        intake = client.post(
            "/api/series/default/intake",
            json={"type": "quick_note", "raw_text": "一条待整理记录", "tags": ["测试"]},
        )
        assert intake.status_code == 201
        intake_id = intake.json()["intake_id"]
        assert client.get(f"/api/series/{other_id}/intake/{intake_id}").status_code == 404
        assert client.patch(
            f"/api/series/default/intake/{intake_id}",
            json={"title": "修改后的标题"},
        ).json()["title"] == "修改后的标题"
        assert client.post(f"/api/series/default/intake/{intake_id}/approve").json()["status"] == "approved"
        promoted = client.post(f"/api/series/default/intake/{intake_id}/promote")
        assert promoted.status_code == 200
        assert promoted.json()["status"] == "merged"
        assert len(client.get("/api/series/default/knowledge-items").json()) == 1
        assert client.get(f"/api/series/{other_id}/knowledge-items").json() == []


def test_series_asset_and_report_markdown_api(tmp_path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/series", json={"name": "附件系列"}).json()
        other_id = created["series_id"]
        imported = client.post(
            "/api/series/default/assets/import",
            files={"file": ("笔记.txt", "正文".encode(), "text/plain")},
            data={"create_intake": "true"},
        )
        assert imported.status_code == 201
        asset_id = imported.json()["asset"]["asset_id"]
        assert imported.json()["intake"]["asset_ids"] == [asset_id]
        assert client.get(f"/api/series/default/assets/{asset_id}/metadata").status_code == 200
        assert client.get(f"/api/series/{other_id}/assets/{asset_id}/metadata").status_code == 404
        assert client.get(f"/api/series/default/assets/{asset_id}").content == "正文".encode()

        report = client.post("/api/series/default/reports/daily/2026-06-23")
        assert report.status_code == 200
        report_id = report.json()["report_id"]
        document = client.get(f"/api/series/default/reports/{report_id}/markdown").json()
        saved = client.put(
            f"/api/series/default/reports/{report_id}/markdown",
            json={"markdown": document["markdown"] + "\n用户编辑\n", "base_revision": document["revision"]},
        )
        assert saved.status_code == 200
        assert "用户编辑" in saved.json()["markdown"]
        conflict = client.put(
            f"/api/series/default/reports/{report_id}/markdown",
            json={"markdown": "覆盖", "base_revision": document["revision"]},
        )
        assert conflict.status_code == 409


def test_quick_capture_aggregates_multiple_asset_context_and_promotes_it(tmp_path) -> None:
    with _client(tmp_path) as client:
        other_id = client.post("/api/series", json={"name": "隔离系列"}).json()["series_id"]
        text = client.post(
            "/api/series/default/assets/import",
            files={"file": ("会议记录.txt", "附件中的会议决定".encode(), "text/plain")},
            data={"create_intake": "false"},
        ).json()["asset"]
        image = client.post(
            "/api/series/default/assets/import",
            files={"file": ("现场图.png", b"image fixture", "image/png")},
            data={"create_intake": "false"},
        ).json()["asset"]

        created = client.post(
            "/api/series/default/intake",
            json={
                "type": "quick_note",
                "raw_text": "用户补充说明",
                "asset_ids": [text["asset_id"], image["asset_id"]],
            },
        )

        assert created.status_code == 201
        intake = created.json()
        assert intake["raw_text"] == "用户补充说明"
        assert "## 附件正文：会议记录.txt" in intake["asset_text"]
        assert "附件中的会议决定" in intake["asset_text"]
        assert intake["warnings"] == ["图片 OCR 暂未启用，已安全保存原图。"]
        assert client.get(
            "/api/series/default/intake",
            params={"status": "pending", "query": "会议决定"},
        ).json()[0]["intake_id"] == intake["intake_id"]

        cross_series = client.post(
            f"/api/series/{other_id}/intake",
            json={"type": "asset", "asset_ids": [text["asset_id"]]},
        )
        assert cross_series.status_code == 404

        assert client.post(f"/api/series/default/intake/{intake['intake_id']}/approve").status_code == 200
        promoted = client.post(f"/api/series/default/intake/{intake['intake_id']}/promote").json()
        knowledge = next(
            item
            for item in client.get("/api/series/default/knowledge-items").json()
            if item["id"] == promoted["knowledge_item_id"]
        )
        assert "用户补充说明" in knowledge["content"]
        assert "附件中的会议决定" in knowledge["content"]
        assert knowledge["links"]["asset_ids"] == [text["asset_id"], image["asset_id"]]


def test_series_memory_returns_linked_asset_evidence_without_cross_series_leak(tmp_path) -> None:
    with _client(tmp_path) as client:
        other_id = client.post("/api/series", json={"name": "其他系列"}).json()["series_id"]
        imported = client.post(
            "/api/series/default/assets/import",
            files={"file": ("记忆机制.txt", "海马缓存用于保持长期上下文。".encode(), "text/plain")},
            data={"create_intake": "true"},
        ).json()
        intake_id = imported["intake"]["intake_id"]
        asset_id = imported["asset"]["asset_id"]
        assert client.post(f"/api/series/default/intake/{intake_id}/approve").status_code == 200
        assert client.post(f"/api/series/default/intake/{intake_id}/promote").status_code == 200

        answer = client.post(
            "/api/series/default/memory/ask",
            json={"question": "海马缓存有什么作用？", "persist": False},
        )
        assert answer.status_code == 200
        assert answer.json()["evidence_sufficient"] is True
        reference = next(item for item in answer.json()["references"] if item["asset_id"] == asset_id)
        assert reference["source_type"] == "asset"
        assert reference["title"] == "记忆机制.txt"
        assert "海马缓存" in reference["quote"]

        isolated = client.post(
            f"/api/series/{other_id}/memory/ask",
            json={"question": "海马缓存有什么作用？", "persist": False},
        )
        assert isolated.status_code == 200
        assert isolated.json()["evidence_sufficient"] is False
        assert isolated.json()["references"] == []


def test_retired_report_merge_preserves_assets_report_and_intake(tmp_path) -> None:
    with _client(tmp_path) as client:
        image = client.post(
            "/api/series/default/assets/import",
            files={"file": ("示意图.png", b"\x89PNG\r\nfixture", "image/png")},
            data={"create_intake": "false"},
        ).json()["asset"]
        document = client.post(
            "/api/series/default/assets/import",
            files={"file": ("参考资料.txt", "附件正文中的关键结论".encode(), "text/plain")},
            data={"create_intake": "false"},
        ).json()["asset"]
        intake = client.post(
            "/api/series/default/intake",
            json={
                "type": "asset",
                "title": "报告附件",
                "asset_ids": [image["asset_id"], document["asset_id"]],
                "suggested_report_type": "daily",
            },
        ).json()
        report = client.post("/api/series/default/reports/daily/2026-06-23").json()
        report_id = report["report_id"]
        base = client.get(f"/api/series/default/reports/{report_id}/markdown").json()
        payload = {
            "base_markdown": base["markdown"],
            "incoming_items": [intake["intake_id"]],
            "dry_run": True,
            "base_revision": base["revision"],
        }

        preview = client.post(f"/api/series/default/reports/{report_id}/merge", json=payload)

        assert preview.status_code == 503
        assert "统一 AI 工作台" in preview.json()["detail"]
        assert client.get(f"/api/series/default/reports/{report_id}/markdown").json()["markdown"] == base["markdown"]
        merged_intake = client.get(f"/api/series/default/intake/{intake['intake_id']}").json()
        assert merged_intake["status"] == "pending"
        assert merged_intake["report_id"] == ""
        assert client.get(f"/api/series/default/assets/{image['asset_id']}/metadata").status_code == 200
        assert client.get(f"/api/series/default/assets/{document['asset_id']}/metadata").status_code == 200


def test_retired_report_merge_never_writes_manual_text_or_intake_state(tmp_path) -> None:
    with _client(tmp_path) as client:
        report = client.post("/api/series/default/reports/daily/2026-06-24").json()
        report_id = report["report_id"]
        document = client.get(f"/api/series/default/reports/{report_id}/markdown").json()
        base = document["markdown"] + "\n用户手动保留行\n"
        intake = client.post(
            "/api/series/default/intake",
            json={"type": "quick_note", "title": "新增", "raw_text": "新增事实"},
        ).json()
        client.post(f"/api/series/default/intake/{intake['intake_id']}/approve")
        payload = {
            "base_markdown": base,
            "incoming_items": [intake["intake_id"]],
            "merge_mode": "preserve_manual_edits",
            "dry_run": True,
            "base_revision": document["revision"],
        }
        preview = client.post(f"/api/series/default/reports/{report_id}/merge", json=payload)
        assert preview.status_code == 503, preview.text
        assert "用户手动保留行" not in client.get(
            f"/api/series/default/reports/{report_id}/markdown"
        ).json()["markdown"]
        assert client.get(
            f"/api/series/default/intake/{intake['intake_id']}"
        ).json()["report_id"] == ""
        merged_intake = client.get(
            f"/api/series/default/intake/{intake['intake_id']}"
        ).json()
        assert merged_intake["status"] == "approved"
        assert merged_intake["report_id"] == ""


def test_intake_organize_configuration_failure_is_persisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        series_routes,
        "get_or_build_ai_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            composition_metadata={"series_intake_organize_remote_usable": False},
        ),
    )
    with _client(tmp_path) as client:
        created = client.post(
            "/api/series/default/intake",
            json={"type": "quick_note", "raw_text": "需要保留的原始内容"},
        ).json()

        response = client.post(f"/api/series/default/intake/{created['intake_id']}/organize")

        assert response.status_code == 503
        failed = client.get(f"/api/series/default/intake/{created['intake_id']}").json()
        assert failed["status"] == "failed"
        assert failed["raw_text"] == "需要保留的原始内容"
        assert failed["warnings"] == ["AI 整理失败，原始内容已保留。"]


def test_intake_organize_configuration_failure_does_not_mark_concurrent_edit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        series_routes,
        "get_or_build_ai_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            composition_metadata={"series_intake_organize_remote_usable": False},
        ),
    )
    original_snapshot = series_routes.SeriesWorkspace.organization_snapshot

    def snapshot_then_edit(workspace, series_id, intake_id):
        snapshot = original_snapshot(workspace, series_id, intake_id)
        workspace.update_intake(
            series_id, intake_id, UpdateIntakeRequest(raw_text="用户并发编辑后的内容"),
        )
        return snapshot

    monkeypatch.setattr(
        series_routes.SeriesWorkspace, "organization_snapshot", snapshot_then_edit,
    )
    with _client(tmp_path) as client:
        created = client.post(
            "/api/series/default/intake",
            json={"type": "quick_note", "raw_text": "旧内容"},
        ).json()

        response = client.post(f"/api/series/default/intake/{created['intake_id']}/organize")
        current = client.get(f"/api/series/default/intake/{created['intake_id']}").json()

    assert response.status_code == 503
    assert current["status"] == "pending"
    assert current["raw_text"] == "用户并发编辑后的内容"
    assert current["warnings"] == []


def test_memory_session_and_stats_api(tmp_path) -> None:
    with _client(tmp_path) as client:
        answer = client.post(
            "/api/series/default/memory/ask",
            json={"question": "没有资料时怎么回答"},
        )
        assert answer.status_code == 200
        assert answer.json()["evidence_sufficient"] is False
        session = client.get("/api/series/default/memory/session").json()
        assert [item["role"] for item in session["messages"]] == ["user", "assistant"]
        assert client.post("/api/series/default/memory/new").json()["messages"] == []
        stats = client.get("/api/series/default/stats/overview")
        assert stats.status_code == 200
        assert stats.json()["counts"]["series_count"] == 1
        custom = client.get(
            "/api/series/default/stats/overview",
            params={"range": "custom", "start": "2099-01-01", "end": "2099-01-31"},
        )
        assert custom.status_code == 200
        assert custom.json()["range"] == "custom"
        assert custom.json()["counts"]["knowledge_item_count"] == 0
        assert client.get(
            "/api/series/default/stats/overview",
            params={"range": "custom", "start": "2099-02-01", "end": "2099-01-01"},
        ).status_code == 422


def test_series_delete_requires_confirmation(tmp_path) -> None:
    with _client(tmp_path) as client:
        series_id = client.post("/api/series", json={"name": "可删除"}).json()["series_id"]
        assert client.delete(f"/api/series/{series_id}").status_code == 403
        assert client.delete(f"/api/series/{series_id}", params={"token": "INVALID"}).status_code == 403
        token = client.post("/api/confirm/generate", json={"action": "delete_series"}).json()["token"]
        assert client.delete(f"/api/series/{series_id}", params={"token": token}).status_code == 204
        assert client.delete(f"/api/series/{series_id}", params={"token": token}).status_code == 403
        assert client.get(f"/api/series/{series_id}").status_code == 404
        default_token = client.post("/api/confirm/generate", json={"action": "delete_series"}).json()["token"]
        assert client.delete("/api/series/default", params={"token": default_token}).status_code == 422


def test_video_library_requires_and_scopes_series_context(tmp_path) -> None:
    with _client(tmp_path) as client:
        other_id = client.post("/api/series", json={"name": "视频研究"}).json()["series_id"]
        assert client.get("/api/intake/library").status_code == 400
        default = client.get("/api/intake/library", headers={"X-Series-Id": "default"})
        other = client.get("/api/intake/library", headers={"X-Series-Id": other_id})
        assert default.status_code == 200 and other.status_code == 200
        assert "/series/default/videos" in default.json()["root_path"].replace("\\", "/")
        assert f"/series/{other_id}/videos" in other.json()["root_path"].replace("\\", "/")
        mismatch = client.post(
            "/api/intake/tasks",
            headers={"X-Series-Id": "default"},
            json={
                "series_id": other_id,
                "url": "https://www.bilibili.com/video/BV1mismatch/",
                "selected_keys": [],
                "media_mode": "audio",
            },
        )
        assert mismatch.status_code == 503
        assert "analyze_source" in mismatch.json()["detail"]
        retired = client.post(
            "/api/intake/tasks",
            headers={"X-Series-Id": "default"},
            json={
                "series_id": "default",
                "url": "https://www.bilibili.com/video/BV1retired/",
                "selected_keys": [],
                "media_mode": "audio",
            },
        )
        assert retired.status_code == 503
        assert "analyze_source" in retired.json()["detail"]
        assert client.get("/api/intake/tasks", headers={"X-Series-Id": "default"}).json() == []
        assert client.get("/api/intake/library", headers={"X-Series-Id": "default"}).json()["records"] == []


def test_video_library_cross_series_search_is_explicit_gated_and_labeled(tmp_path) -> None:
    with _client(tmp_path) as client:
        other_id = client.post("/api/series", json={"name": "跨系列来源"}).json()["series_id"]
        source = ResolvedVideoItem(
            key="BV1crossseries:p1",
            bvid="BV1crossseries",
            title="同一视频不同系列",
            source_url="https://www.bilibili.com/video/BV1crossseries/",
        )
        LibraryStorage(tmp_path, "default").create_record(source, media_mode="audio")
        LibraryStorage(tmp_path, other_id).create_record(source, media_mode="video")
        headers = {"X-Series-Id": "default"}

        scoped = client.get("/api/intake/library", headers=headers)
        assert scoped.status_code == 200
        assert [item["series_id"] for item in scoped.json()["records"]] == ["default"]
        assert client.get(
            "/api/intake/library", params={"cross_series": "true"}, headers=headers
        ).status_code == 403

        preferences = client.get("/api/series/default").json()["preferences"]
        preferences["allow_cross_series_search"] = True
        assert client.patch(
            "/api/series/default", json={"preferences": preferences}
        ).status_code == 200
        cross = client.get(
            "/api/intake/library", params={"cross_series": "true"}, headers=headers
        )

        assert cross.status_code == 200
        assert {item["series_id"] for item in cross.json()["records"]} == {"default", other_id}
        assert cross.json()["root_path"].replace("\\", "/").endswith("/library/series")


def test_video_summary_enters_series_intake_before_daily(tmp_path) -> None:
    storage = LibraryStorage(tmp_path, "default")
    record, record_dir = storage.create_record(
        ResolvedVideoItem(
            key="BV1series001:p1",
            bvid="BV1series001",
            title="系列视频",
            source_url="https://www.bilibili.com/video/BV1series001/",
            tags=["架构"],
        ),
        media_mode="audio",
    )
    (record_dir / "data" / "summary.json").write_text(
        json.dumps({"thirty_second_summary": "系列隔离摘要", "key_takeaways": ["先进入待整理区"]}),
        encoding="utf-8",
    )

    with _client(tmp_path) as client:
        response = client.post(
            f"/api/series/default/videos/{record.id}/intake",
            json={"include_in_daily": True, "high_value": False, "need_review": False},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert response.json()["suggested_report_type"] == "daily"
        assert response.json()["series_id"] == "default"
        assert client.get("/api/series/default/knowledge-items").json() == []


def test_loopback_origin_can_send_series_header(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.options(
            "/api/intake/library",
            headers={
                "Origin": "http://127.0.0.1:4173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Series-Id",
            },
        )
        assert response.status_code == 200
        assert "x-series-id" in response.headers["access-control-allow-headers"].lower()
