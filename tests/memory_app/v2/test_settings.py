"""Settings compose safe existing reads and independent privacy CAS."""
from pathlib import Path
from shutil import copyfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.recognition import WorkScope
from backend.memory_app.source_egress import SourceEgressService


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    copyfile(Path(__file__).resolve().parents[3] / "config/settings.toml.example",
             config / "settings.toml")
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=tmp_path / "runtime", legacy_app=FastAPI())
    with TestClient(app) as client:
        yield app, client


def test_safe_model_projection_has_independent_revisions(env):
    app, client = env
    configured = client.put("/api/recognition/settings", json={
        "purpose": "generation", "base_url": "https://example.test/v1", "model": "test",
        "api_key": "sk-test-DO-NOT-LEAK", "allow_remote": True, "expected_revision": 0})
    assert configured.status_code == 200
    response = client.get("/api/v2/settings")
    assert response.status_code == 200
    assert "sk-test-DO-NOT-LEAK" not in response.text
    body = response.json()
    assert "revision" not in body
    assert body["model"]["generation"]["revision"] == 1
    assert body["model"]["embedding"]["revision"] == 0
    assert body["model"]["generation_mode"]["revision"] == 0
    assert body["model"]["asr"]["settings_revision"] == 0
    assert body["privacy"] == {"revision": 0, "private_projects": []}
    assert client.get("/api/v2/settings/egress-receipts").json() == []


def test_replace_privacy_list_is_atomic_and_stale_cas_preserves_all(env):
    app, client = env
    for name in ("One", "Two"):
        assert client.post("/api/v2/projects", json={"name": name}).status_code == 200
    ids = [r["id"] for r in client.get("/api/v2/projects").json()["items"] if not r["builtin"]]
    response = client.patch("/api/v2/settings/privacy", json={"private_projects": ids, "expected_revision": 0})
    assert response.status_code == 200
    current = response.json()
    assert sorted(current["private_projects"]) == sorted(ids)
    assert current["revision"] > 0
    assert client.patch("/api/v2/settings/privacy", json={"private_projects": [], "expected_revision": 0}).status_code == 409
    assert client.get("/api/v2/settings").json()["privacy"] == current
    assert client.patch("/api/v2/settings/privacy", json={"private_projects": [ids[0]], "expected_revision": current["revision"]}).status_code == 200
    assert client.get("/api/v2/settings").json()["privacy"]["private_projects"] == [ids[0]]


@pytest.mark.parametrize("body", [
    {"private_projects": [], "expected_revision": 0, "allow_remote": True},
    {"private_projects": ["missing"], "expected_revision": 0},
    {"private_projects": [], "expected_revision": True},
    {"private_projects": "project", "expected_revision": 0},
])
def test_invalid_privacy_does_not_write(env, body):
    app, client = env
    assert client.patch("/api/v2/settings/privacy", json=body).status_code == 400
    assert app.state.recognition_records.list("v2_private_scopes") == ()


def test_private_sources_are_read_only_and_cancel_reuses_original_policy(env):
    app, client = env
    scope = WorkScope("local-user", "default")
    service = app.state.recognition_service
    source_id = service.stage_experience(scope=scope, content="Sensitive body")
    authority = SourceEgressService(service.records)
    authority.set_policy(scope, "experience", source_id, 1, 0, [])
    before = service.records.read("recognition_experiences", source_id)
    response = client.get("/api/v2/settings/private-sources")
    assert response.status_code == 200
    row = next(r for r in response.json() if r["source_id"] == source_id)
    assert row["type"] == "experience" and row["inherited"] is False
    assert row["policy_revision"] == 1
    assert row["source_revision"] == before.revision
    assert "Sensitive body" not in response.text
    assert client.put(f"/api/recognition/source-policies/experience/{source_id}", json={
        "project_id": "default", "expected_source_revision": 1, "expected_policy_revision": 1,
        "allowed_purposes": ["generation", "embedding", "rerank"]}).status_code == 200
    assert client.get("/api/v2/settings/private-sources").json() == []
    assert service.records.read("recognition_experiences", source_id) == before


def test_project_private_source_is_inherited(env):
    app, client = env
    from backend.memory_app.v2.privacy import set_private_project
    scope = WorkScope("local-user", "default")
    source_id = app.state.recognition_service.stage_experience(scope=scope, content="Private")
    set_private_project(app.state.recognition_records, "default", True, 0)
    row = next(r for r in client.get("/api/v2/settings/private-sources").json() if r["source_id"] == source_id)
    assert row["inherited"] is True and row["policy_revision"] == 0


def test_parent_private_source_is_inherited(env):
    app, client = env
    records = app.state.recognition_records
    scope = WorkScope("local-user", "default")
    parent = app.state.recognition_service.stage_experience(scope=scope, content="Parent")
    child = app.state.recognition_service.stage_experience(scope=scope, content="Child")
    with records.begin() as tx:
        row = tx.read("recognition_experiences", child)
        tx.put("recognition_experiences", child, {**row.payload, "provenance": {
            "source_refs": [{"type": "experience", "id": parent, "revision": 1}]}}, expected_revision=1)
        tx.commit()
    SourceEgressService(records).set_policy(scope, "experience", parent, 1, 0, [])
    rows = {r["source_id"]: r for r in client.get("/api/v2/settings/private-sources").json()}
    assert rows[parent]["inherited"] is False
    assert rows[child]["inherited"] is True


def test_asr_read_preserves_core_revision_without_secret(env):
    app, client = env
    from core.product_core.cloud_asr_provider_settings import SaveCloudAsrProviderSettings
    from core.storage_provider import JsonObjectStore
    store = JsonObjectStore(app.state.recognition_runtime_root / ".rebuild-data")
    saved = SaveCloudAsrProviderSettings(store, now="2026-10-01T00:00:00Z").execute(
        enabled=True, confirm_enable=True)
    asr = client.get("/api/v2/settings").json()["model"]["asr"]
    assert asr["settings_revision"] == saved.settings_revision
    assert asr["enabled"] is True and asr["model"] == saved.model
    assert asr["max_audio_bytes"] == saved.max_audio_bytes
    assert asr["chunk_duration_seconds"] == 60 and asr["chunk_overlap_seconds"] == 5


def test_legacy_partial_parents_are_nonprivate_and_privacy_is_inherited(env):
    app, client = env
    records = app.state.recognition_records
    scope = WorkScope("local-user", "default")
    parents = [app.state.recognition_service.stage_experience(scope=scope, content=text)
               for text in ("First", "Second")]
    child = app.state.recognition_service.stage_experience(scope=scope, content="Child")
    with records.begin() as tx:
        row = tx.read("recognition_experiences", child)
        tx.put("recognition_experiences", child, {**row.payload, "provenance": {"source_refs": [
            {"type": "experience", "id": source, "revision": 1} for source in parents]}}, expected_revision=1)
        tx.commit()
    authority = SourceEgressService(records)
    with records.begin() as tx:
        for source, purpose in zip(parents, ("generation", "embedding")):
            tx.put("source_egress_experience_policies", source, {
                "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
                "source_revision": 1, "allowed_purposes": [purpose]}, expected_revision=0)
        tx.commit()
    assert client.get("/api/v2/settings/private-sources").json() == []
    authority.set_policy(scope, "experience", parents[0], 1, 1, [])
    rows = {r["source_id"]: r for r in client.get("/api/v2/settings/private-sources").json()}
    assert rows[child]["inherited"] is True
    assert rows[parents[0]]["inherited"] is False and parents[1] not in rows


def test_generation_provenance_without_remote_receipt_is_not_fabricated(env):
    app, client = env
    records = app.state.recognition_records
    with records.begin() as tx:
        tx.put("recognition_candidates", "candidate", {"generation": {
            "model": "local", "completed_at": "2026-10-01T00:00:00Z"}}, expected_revision=0)
        tx.put("recognition_tasks", "task", {"state": "completed", "model": "local",
            "created_at": "2026-10-01T00:00:00Z"}, expected_revision=0)
        tx.commit()
    assert client.get("/api/v2/settings/egress-receipts").json() == []


def test_receipts_merge_remote_only_limit_null_and_no_body_or_key(env):
    app, client = env
    records = app.state.recognition_records
    with records.begin() as tx:
        tx.put("workspace_ask_receipts", "ask", {"created_at": "2026-10-01T01:00:00Z",
            "target": {"execution_location": "remote", "model": "ask-model", "api_key": "sk-test-DO-NOT-LEAK"},
            "sources": [{"body": "private content"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}, expected_revision=0)
        tx.put("workspace_ask_receipts", "local", {"created_at": "2026-10-01T04:00:00Z",
            "target": {"execution_location": "local", "model": "local"}}, expected_revision=0)
        tx.put("workspace_items", "intake", {"remote_processing_receipts": [{
            "consented_at": "2026-10-01T02:00:00Z", "generation": {"model": "draft"}, "asr": {"model": "asr"}}]}, expected_revision=0)
        tx.commit()
    before = records.list("workspace_items")
    response = client.get("/api/v2/settings/egress-receipts")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 3
    assert {r["purpose"] for r in rows} == {"问", "整理", "转写"}
    ask = next(r for r in rows if r["purpose"] == "问")
    assert ask["usage"] == {"input": 2, "output": 3} and ask["items"] == 1
    assert all(r["duration"] is None for r in rows)
    assert "sk-test-DO-NOT-LEAK" not in response.text and "private content" not in response.text
    assert len(client.get("/api/v2/settings/egress-receipts?limit=1").json()) == 1
    assert client.get("/api/v2/settings/egress-receipts?limit=0").status_code == 422
    assert records.list("workspace_items") == before


def test_snapshot_size_counts_all_nested_files_and_missing_is_unknown(tmp_path):
    from backend.api.routes.product.vault_snapshots import _snapshot_size
    root = tmp_path / 'snapshot'
    (root / 'payload').mkdir(parents=True)
    (root / 'manifest.json').write_bytes(b'abc')
    (root / 'payload' / 'document').write_bytes(b'abcdef')
    assert _snapshot_size(root) == 9
    assert _snapshot_size(tmp_path / 'missing') is None
