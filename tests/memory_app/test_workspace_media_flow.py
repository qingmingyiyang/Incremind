from __future__ import annotations

from backend.memory_app import workspace_bilibili_media, workspace_xhs_media
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.product_core.cloud_asr_provider_settings import SaveCloudAsrProviderSettings
from tests.memory_app.test_workspace import Model, client
from backend.memory_app.v2.privacy import set_private_project


def test_bilibili_video_is_reviewed_before_document_admission(tmp_path, monkeypatch):
    calls = []

    def read(url, root, **context):
        calls.append(url)
        assert context["item_id"]
        assert context["project_id"] == "default"
        return {
            "source_text": "标题：公开演讲\n\n视频语音转写：\n[00:01] 这是演讲原文。",
            "title": "公开演讲",
            "canonical_url": "https://www.bilibili.com/video/BV1jj8yzLEWo/",
            "acquisition_method": "official_subtitle",
            "content_kind": "video",
        }

    monkeypatch.setattr(workspace_bilibili_media, "read_bilibili_media", read)
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/link", json={
        "url": "https://www.bilibili.com/video/BV1jj8yzLEWo/?spm=tracking"
    }).json()
    assert item["status"] == "staged"
    assert item["source_text"] == ""
    assert item["platform"] == "bilibili"
    assert "media_request_url" not in item
    assert http.post(f"/api/workspace/v1/items/{item['id']}/confirm", json={
        "expected_revision": item["revision"],
    }).status_code == 409
    ready = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={}).json()
    assert ready["status"] == "ready"
    assert ready["acquisition_method"] == "official_subtitle"
    assert len(calls) == 1
    assert ready["document_id"] is None
    assert http.get(f"/api/workspace/v1/items/{item['id']}/source").json()["original_url"] == ready["url"]


def test_xiaohongshu_token_is_private_and_model_retry_reuses_transcript(tmp_path, monkeypatch):
    calls = []

    def read(url, root, **context):
        calls.append(url)
        assert context["item_id"]
        return {
            "source_text": "标题：示例\n\n视频语音转写：\n这是视频真实语音。",
            "title": "示例",
            "canonical_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
            "acquisition_method": "public_html_video_local_asr",
            "content_kind": "video",
        }

    monkeypatch.setattr(workspace_xhs_media, "read_xiaohongshu_media", read)
    model = Model(failure=True)
    http, records = client(tmp_path, model)
    url = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=private123"
    item = http.post("/api/workspace/v1/items/link", json={"url": url}).json()
    assert "private123" not in str(item)
    failed = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={}).json()
    assert failed["status"] == "failed"
    assert "private123" not in str(failed)
    assert "视频真实语音" in records.read("workspace_items", item["id"]).payload["source_text"]
    assert len(calls) == 1
    model.failure = False
    assert http.post(f"/api/workspace/v1/items/{item['id']}/retry", json={}).json()["status"] == "staged"
    assert http.post(f"/api/workspace/v1/items/{item['id']}/process", json={}).json()["status"] == "ready"
    assert len(calls) == 1


def test_video_fetch_failure_does_not_create_a_draft(tmp_path, monkeypatch):
    def blocked(url, root, **context):
        raise ValueError("bilibili_metadata_unavailable")

    monkeypatch.setattr(workspace_bilibili_media, "read_bilibili_media", blocked)
    http, _ = client(tmp_path)
    item = http.post("/api/workspace/v1/items/link", json={
        "url": "https://www.bilibili.com/video/BV1jj8yzLEWo/"
    }).json()
    failed = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={}).json()
    assert failed["status"] == "failed"
    assert failed["error"] == "bilibili_metadata_unavailable"
    assert failed["draft"] is None
    assert failed["source_text"] == ""


def test_cloud_video_obeys_project_privacy_before_platform_network(tmp_path, monkeypatch):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    calls = []

    def read(*_args, **_kwargs):
        calls.append("platform_network")
        return {
            "source_text": "视频真实语音原文", "title": "演讲",
            "canonical_url": "https://www.bilibili.com/video/BV1jj8yzLEWo/",
            "acquisition_method": "hy_asr", "content_kind": "video",
        }

    monkeypatch.setattr(workspace_bilibili_media, "read_bilibili_media", read)
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/link", json={
        "url": "https://www.bilibili.com/video/BV1jj8yzLEWo/",
    }).json()
    path = f"/api/workspace/v1/items/{item['id']}/process"
    set_private_project(records, "default", True, 0)
    rejected = http.post(path, json={})
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "private_project_remote_blocked"
    assert calls == []
    assert records.read("workspace_items", item["id"]).payload["status"] == "staged"
    set_private_project(records, "default", False, 1)
    assert http.post(path, json={}).json()["status"] == "ready"
    assert calls == ["platform_network"]
