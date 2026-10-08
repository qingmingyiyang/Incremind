"""Intake consumes global authorization while preserving the wire-time guard."""
import pytest

from backend.memory_app.v2.privacy import set_private_project
from tests.memory_app.test_workspace import Model, client
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.product_core.cloud_asr_provider_settings import SaveCloudAsrProviderSettings


class RemoteModel(Model):
    remote = True
    allowed = True
    generation_revision = 3
    mode_revision = 7
    before_wire = None
    calls = 0
    wire_calls = 0

    def public(self):
        return {"generation": {"base_url": "https://api.example.test/v1" if self.remote else "http://127.0.0.1:8000/v1",
                               "model": "test-model", "revision": self.generation_revision,
                               "allow_remote": self.allowed, "enabled": True},
                "generation_mode": {"revision": self.mode_revision}}

    def complete(self, messages, *, max_tokens, validate_current=None):
        self.calls += 1
        if self.before_wire:
            self.before_wire()
        if validate_current:
            validate_current()
        self.wire_calls += 1
        return super().complete(messages, max_tokens=max_tokens)


def text_item(http):
    return http.post("/api/workspace/v1/items/text", json={"project_id": "alpha", "text": "原文证据"}).json()


@pytest.mark.parametrize("legacy_field", [None, False, True])
def test_remote_intake_uses_global_authorization_without_per_run_consent(tmp_path, legacy_field):
    model = RemoteModel()
    http, records = client(tmp_path, model)
    item = text_item(http)
    body = {"project_id": "alpha"}
    if legacy_field is not None:
        body["remote_processing_consent"] = legacy_field
    response = http.post(f"/api/workspace/v1/items/{item['id']}/process", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert model.wire_calls == 1
    receipt, = records.read("workspace_items", item["id"]).payload["remote_processing_receipts"]
    assert receipt["consent_scope"] == "global_setting"
    assert receipt["settings_revision"] == {"generation": 3, "mode": 7}
    assert receipt["send_categories"] == ["model_instructions", "source_text"]
    assert receipt["project_id"] == "alpha" and receipt["item_id"] == item["id"]


@pytest.mark.parametrize("private,allowed,error", [(True, True, "private_project_remote_blocked"),
        (True, False, "private_project_remote_blocked"), (False, False, "remote_disabled")])
def test_global_or_private_denial_leaves_staged_item_without_lease(tmp_path, private, allowed, error):
    model = RemoteModel()
    model.allowed = allowed
    http, records = client(tmp_path, model)
    item = text_item(http)
    if private:
        set_private_project(records, "alpha", True, 0)
    before = records.read("workspace_items", item["id"])
    response = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "alpha"})
    assert response.status_code == 409 and response.json()["detail"] == error
    assert records.read("workspace_items", item["id"]) == before
    assert before.payload["status"] == "staged"
    assert before.payload.get("processing_run_id") is None
    assert model.calls == 0


@pytest.mark.parametrize("change", ["private", "generation", "mode", "allow_remote"])
def test_authorization_changes_before_wire_keep_receipt_and_prevent_egress(tmp_path, change):
    model = RemoteModel()
    http, records = client(tmp_path, model)
    item = text_item(http)
    def mutate():
        if change == "private":
            set_private_project(records, "alpha", True, 0)
        elif change == "generation":
            model.generation_revision += 1
        elif change == "mode":
            model.mode_revision += 1
        else:
            model.allowed = False
    model.before_wire = mutate
    response = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "alpha"})
    assert response.status_code == 409
    assert response.json()["detail"] == "remote_processing_target_changed"
    assert model.wire_calls == 0
    saved = records.read("workspace_items", item["id"]).payload
    assert saved["status"] == "failed"
    receipt, = saved["remote_processing_receipts"]
    assert receipt["settings_revision"] == {"generation": 3, "mode": 7}
    assert saved.get("processing_run_id") is None


def test_private_project_can_use_local_generation_without_remote_receipt(tmp_path):
    model = RemoteModel()
    model.remote = False
    model.allowed = False
    http, records = client(tmp_path, model)
    item = text_item(http)
    set_private_project(records, "alpha", True, 0)
    response = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "alpha"})
    assert response.status_code == 200 and response.json()["status"] == "ready"
    assert records.read("workspace_items", item["id"]).payload["remote_processing_receipts"] == []


@pytest.mark.parametrize("private,remote,allowed,expected", [
    (False, False, False, None), (True, False, False, "private_project_remote_blocked"),
    (False, True, False, "remote_disabled")])
def test_cloud_asr_uses_its_enabled_setting_and_checks_generation_independently(
        tmp_path, monkeypatch, private, remote, allowed, expected):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-10-01T00:00:00Z").execute(enabled=True, confirm_enable=True)
    model = RemoteModel()
    model.remote, model.allowed = remote, allowed
    http, records = client(tmp_path, model)
    calls = []
    def read(*_args, **context):
        context["validate_remote"]()
        calls.append("platform_network")
        return {"source_text": "真实视频原文", "title": "视频", "canonical_url": "https://www.bilibili.com/video/BV1jj8yzLEWo/",
                "acquisition_method": "hy_asr", "content_kind": "video"}
    monkeypatch.setattr("backend.memory_app.workspace_bilibili_media.read_bilibili_media", read)
    item = http.post("/api/workspace/v1/items/link", json={
        "project_id": "alpha", "url": "https://www.bilibili.com/video/BV1jj8yzLEWo/"}).json()
    if private:
        set_private_project(records, "alpha", True, 0)
    before = records.read("workspace_items", item["id"])
    response = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "alpha"})
    if expected:
        assert response.status_code == 409 and response.json()["detail"] == expected
        assert records.read("workspace_items", item["id"]) == before
        assert calls == []
    else:
        assert response.status_code == 200 and response.json()["status"] == "ready"
        assert calls == ["platform_network"]
        receipt, = records.read("workspace_items", item["id"]).payload["remote_processing_receipts"]
        assert receipt["generation"] is None
        assert receipt["consent_scope"] == "global_setting"
        assert receipt["settings_revision"] == {"generation": 3, "mode": 7}
        assert receipt["asr"]["revision"] == 1
        assert receipt["send_categories"] == ["audio_data", "audio_chunks", "provider_metadata"]


def test_private_project_change_before_asr_wire_prevents_platform_network(tmp_path, monkeypatch):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-10-01T00:00:00Z").execute(enabled=True, confirm_enable=True)
    http, records = client(tmp_path)
    calls = []
    def read(*_args, **context):
        set_private_project(records, "alpha", True, 0)
        context["validate_remote"]()
        calls.append("platform_network")
        raise AssertionError("private material must not be sent")
    monkeypatch.setattr("backend.memory_app.workspace_bilibili_media.read_bilibili_media", read)
    item = http.post("/api/workspace/v1/items/link", json={
        "project_id": "alpha", "url": "https://www.bilibili.com/video/BV1jj8yzLEWo/"}).json()
    response = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "alpha"})
    assert response.status_code == 409
    assert response.json()["detail"] == "remote_processing_target_changed"
    assert calls == []
    saved = records.read("workspace_items", item["id"]).payload
    assert saved["status"] == "failed" and len(saved["remote_processing_receipts"]) == 1
