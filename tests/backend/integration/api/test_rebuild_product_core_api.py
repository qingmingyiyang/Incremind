from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api import ai_runtime
from backend.api import workbench_ai_runtime
from backend.api.routes import rebuild as rebuild_routes
from backend.api.routes.product import (
    bilibili as product_bilibili,
    memory_candidates as product_memory_candidates,
    source_content as product_source_content,
)
from backend.providers import ProviderRegistry
from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.job_runner import SQLiteJobStore
from core.memory_core import (
    ObjectStoreMemoryCandidateRepository,
    build_manual_publication_context,
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)
from core.memory_core.publication_trust_audit_uow import SQLiteMemoryPublicationTrustAuditUnitOfWork
from core.model_gateway import ModelResult, ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.product_core import ReadSourceTextContent
from core.product_core.four_layer_memory_candidate_import import ImportFourLayerMemoryCandidatesFromProviderOutput
from core.product_core.project_memory_recall import ProjectMemoryRecallResult
from core.search_and_recall import ObjectStoreRecallRepository
from core.product_core.video_link_adapter import AuthorizedBilibiliDownloadResult
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _client_with_secret(tmp_path, secret_value: str, *, secret_key: str = "provider:deepseek") -> TestClient:
    return _client_with_secrets(tmp_path, {secret_key: secret_value})


def _client_with_secrets(tmp_path, secrets: dict[str, str]) -> TestClient:
    class SecretStore:
        def get(self, key: str) -> str:
            return secrets.get(key, "")

        def has_secret(self, key: str) -> bool:
            return bool(secrets.get(key, ""))

    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path, secret_store=SecretStore())))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_library_tag_endpoints_share_project_scope(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("sources", "source-alpha", {
        "id": "source-alpha", "title": "Alpha", "project_id": "project-alpha",
    }, expected_revision=0)
    store.write("sources", "source-beta", {
        "id": "source-beta", "title": "Beta", "project_id": "project-beta",
    }, expected_revision=0)
    tag_id = f"tag-{hashlib.sha256('shared'.encode('utf-8')).hexdigest()[:16]}"
    store.write("tag_index", tag_id, {
        "id": tag_id, "tag": "Shared", "refs": [
            {"source_id": "source-alpha", "paragraph_id": "p1"},
            {"source_id": "source-beta", "paragraph_id": "p2"},
        ], "ref_count": 2, "source_count": 2,
    }, expected_revision=0)

    with _client(tmp_path) as client:
        facets = client.get("/api/rebuild/library/tag-facets", params={"project_id": "project-alpha"})
        hits = client.get("/api/rebuild/library/sources/by-tag", params={
            "tag": "Shared", "project_id": "project-alpha",
        })

    assert facets.status_code == 200 and facets.json()["facets"] == [
        {"tag": "Shared", "ref_count": 1, "source_count": 1},
    ]
    assert hits.status_code == 200
    assert [item["source_id"] for item in hits.json()["hits"]] == ["source-alpha"]
    assert hits.json()["total_hits"] == 1


def test_library_source_activity_endpoint_returns_latest_operations(tmp_path) -> None:
    store = _store(tmp_path)
    ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(kind="text", title="活动来源", content="活动 API 集成正文"),
    )
    source_id = str(store.list("sources")[0]["id"])
    with _client(tmp_path) as client:
        edited = client.put(
            f"/api/rebuild/library/sources/{source_id}/metadata",
            json={"expected_revision": 1, "title": "编辑后标题", "series_name": "", "tags": ["活动"]},
        )
        assert edited.status_code == 200
        assert edited.json()["status"] == "updated"

        activity = client.get(f"/api/rebuild/library/sources/{source_id}/activity")
        assert activity.status_code == 200
        payload = activity.json()
        assert payload["status"] == "completed"
        assert payload["source_id"] == source_id
        events = {event["type"]: event for event in payload["events"]}
        assert set(events) == {"library_source_edited"}
        assert events["library_source_edited"]["label"] == "编辑资料信息"
        assert events["library_source_edited"]["revision"] == 2
        assert events["library_source_edited"]["summary"] == "更新资料信息（编辑后标题）"

        missing = client.get("/api/rebuild/library/sources/source-not-exist/activity")
        assert missing.status_code == 404
        assert missing.json()["status"] == "not_found"


def test_rebuild_library_source_delete_is_soft_and_can_be_undone_after_restart(tmp_path) -> None:
    store = _store(tmp_path)
    ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(kind="text", title="Undo source", content="Preserve original source bytes and traceability."),
    )
    source = store.list("sources")[0]
    source_id = str(source["id"])

    with _client(tmp_path) as client:
        deleted = client.delete(f"/api/rebuild/library/items/{source_id}?item_type=source")
        assert deleted.status_code == 200
        body = deleted.json()
        assert body["status"] == "deleted"
        assert body["revision"] == 2
        assert body["operation_id"].startswith("library-delete-")
        overview = client.get("/api/rebuild/library/overview").json()
        assert source_id not in {item["item_id"] for item in overview["items"]}

    # A new app process reads the durable lifecycle record and accepts only the exact CAS token/revision.
    with _client(tmp_path) as restarted:
        stale = restarted.post(
            f"/api/rebuild/library/items/{source_id}/undo-delete",
            json={"item_type": "source", "operation_id": body["operation_id"], "expected_revision": 1},
        )
        assert stale.status_code == 409
        restored = restarted.post(
            f"/api/rebuild/library/items/{source_id}/undo-delete",
            json={"item_type": "source", "operation_id": body["operation_id"], "expected_revision": 2},
        )
        assert restored.status_code == 200
        assert restored.json()["status"] == "restored"
        assert restored.json()["revision"] == 3
        overview = restarted.get("/api/rebuild/library/overview").json()
        assert source_id in {item["item_id"] for item in overview["items"]}

    assert store.read("sources", source_id) is not None


@pytest.mark.parametrize(
    ("item_type", "collection"),
    (("document", "documents"), ("memory_candidate", "memory_candidates"), ("atom", "memory_atoms")),
)
def test_rebuild_library_generic_delete_rejects_non_source_without_mutation(tmp_path, item_type, collection) -> None:
    store = _store(tmp_path)
    item_id = f"{item_type}-delete-guard"
    store.write(collection, item_id, {"id": item_id, "status": "draft"}, expected_revision=None)

    with _client(tmp_path) as client:
        response = client.delete(f"/api/rebuild/library/items/{item_id}?item_type={item_type}")

    assert response.status_code == 400
    assert response.json()["status"] == "unsupported"
    assert store.read(collection, item_id) is not None


class _FakeFourLayerProvider:
    provider_name = "deepseek-test"

    def complete_json(self, *, system_prompt, user_payload):
        source_refs = user_payload["source_refs"]
        return {
            "candidates": [
                {
                    "target_layer": "scenario",
                    "candidate_type": "answer_summary",
                    "status": "pending_review",
                    "proposed_content": "通过后端 Provider endpoint 生成的四层场景候选。",
                    "source_refs": source_refs,
                    "evidence_refs": source_refs,
                    "review_prompt": "请确认该场景候选。",
                    "review": {
                        "requires_user_confirmation": True,
                        "auto_promote_allowed": False,
                    },
                }
            ],
            "insufficient_evidence": [],
            "provider_boundary": {"provider_must_not": ["publish_memory"]},
        }


class _FakeL0ToL3Provider:
    provider_name = "deepseek-l0-l3-test"

    def complete_json(self, *, system_prompt, user_payload):
        source_refs = user_payload["source_refs"]
        candidate_specs = (
            ("atom", "answer_fact", "用户需要逐层审核来源可追溯的项目记忆。"),
            ("scenario", "answer_summary", "项目记忆应从事实组织为可复用场景。"),
            ("series_memory", "answer_summary", "项目总览串联已确认事实与场景。"),
        )
        return {
            "candidates": [
                {
                    "target_layer": target_layer,
                    "candidate_type": candidate_type,
                    "status": "pending_review",
                    "proposed_content": proposed_content,
                    "source_refs": source_refs,
                    "evidence_refs": source_refs,
                    "review_prompt": f"请确认 {target_layer} 候选。",
                    "review": {
                        "requires_user_confirmation": True,
                        "auto_promote_allowed": False,
                    },
                }
                for target_layer, candidate_type, proposed_content in candidate_specs
            ],
            "insufficient_evidence": [],
            "provider_boundary": {"provider_must_not": ["publish_memory"]},
        }


def _install_memory_candidate_turn_stub(monkeypatch, tmp_path, provider) -> None:
    class Runtime:
        composition_metadata = {"memory_candidate_remote_usable": True}

        def __init__(self, request):
            self.request = request
            self.turn = None
            self.presentation = None

        def submit_turn(self, turn):
            self.turn = turn
            return SimpleNamespace(turn_id=turn["turn_id"], status="waiting_approval", current_sequence=4)

        def events_after(self, _turn_id):
            return ({"type": "approval.required", "event_id": "event-memory-approval"},)

        def apply_action(self, action):
            grant_id = self.turn["input"]["refs"][0]["object_id"]
            grant = self.request.app.state.four_layer_memory_candidate_evidence_grant_store.inspect(grant_id)
            store = _store(tmp_path)
            importer = ImportFourLayerMemoryCandidatesFromProviderOutput(store)
            if grant.evidence_kind == "source_content_read":
                record = store.read("source_content_reads", grant.evidence_id)
                source_refs = [{"source_id": grant.source_id, "locator": "source:content", "quote": record["preview"]}]
                output = provider.complete_json(system_prompt="test", user_payload={"source_refs": source_refs})
                result = importer.execute_from_content_read(source_id=grant.source_id, project_id=grant.project_id, content_read_id=grant.evidence_id, provider_output=output)
            else:
                record = store.read("media_processing_outputs", grant.evidence_id)
                source_refs = [{"source_id": grant.source_id, "locator": f"media:{record['output_kind']}", "quote": record["preview"]}]
                output = provider.complete_json(system_prompt="test", user_payload={"source_refs": source_refs})
                result = importer.execute_from_media_output(output_id=grant.evidence_id, project_id=grant.project_id, provider_output=output)
            self.presentation = {"candidate_ids": list(result.candidate_ids), "candidate_count": result.candidate_count, "provider_id": provider.provider_name}
            return SimpleNamespace(turn_id=action["turn_id"], status="completed", current_sequence=8)

        def presentation_for(self, _turn_id):
            return self.presentation

    runtimes = {}
    def runtime(request, _container):
        return runtimes.setdefault(id(request.app), Runtime(request))
    monkeypatch.setattr(product_memory_candidates, "get_or_build_ai_runtime", runtime)


class _FakeTemplateProvider:
    provider_name = "deepseek-template-test"

    def complete_json(self, *, system_prompt, user_payload):
        assert "source_refs" in user_payload
        assert "api_key" not in str(user_payload).lower()
        assert "cookie" not in str(user_payload).lower()
        return {
            "title": "AI 增强复盘",
            "markdown": "## 背景\n资料已经结构化。\n\n## 行动\n把模板草稿接入候选审核。",
        }


class _FakeAuthorizedBilibiliDownloader:
    def __init__(self, output_file) -> None:
        self._output_file = output_file

    def execute(self, *, plan, settings):
        return AuthorizedBilibiliDownloadResult(
            status="completed",
            provider=settings.provider_name,
            mode="authorized_download",
            series_id=plan.series_id,
            video_id=plan.video_id,
            bvid=plan.bvid,
            page=plan.page,
            command=("python", "-m", "yt_dlp"),
            output_file=str(self._output_file),
            output_video_reference="authorized-video://bilibili/BV1abcDEF234/p1",
            reads_cookies=False,
            cookie_mode=settings.cookie_mode,
            downloads_video=True,
            writes_video_file=True,
            starts_audio_extraction=False,
            starts_asr=False,
            starts_summary=False,
            creates_memory_candidate=False,
            publishes_memory=False,
            blocked_operations=("audio_track_extraction", "memory_publication"),
            error=None,
        )


def _save_source_with_content_read(tmp_path) -> str:
    store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(
            kind="text",
            title="Provider endpoint source",
            content="产品设计文档要求外部 Provider 只能生成待审四层记忆候选。",
        )
    )
    ReadSourceTextContent(store).execute(source_id=str(source["id"]))
    return str(source["id"])


def _save_active_deepseek_provider(tmp_path, *, provider_id: str = "personal-deepseek") -> None:
    fallback = {
        "name": "默认供应商",
        "llm_provider": "openai",
        "base_url": "",
        "api_path": "/chat/completions",
        "model": "",
        "models": [],
        "enabled": True,
    }
    registry = ProviderRegistry(tmp_path)
    registry.create(
        {
            "provider_id": provider_id,
            "name": "个人 DeepSeek",
            "llm_provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "api_path": "/chat/completions",
            "model": "deepseek-chat",
            "models": ["deepseek-chat"],
            "enabled": True,
        },
        fallback=fallback,
    )
    registry.activate(provider_id, fallback=fallback)


def _save_layer_candidate(tmp_path, *, target_layer: str = "scenario") -> str:
    candidate_id = f"memory-candidate-{target_layer}-api"
    ObjectStoreMemoryCandidateRepository(_store(tmp_path)).save(
        {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": "project-alpha",
            "target_layer": target_layer,
            "candidate_type": "answer_summary",
            "status": "pending_review",
            "proposed_content": "四层记忆候选必须先进入 staging，再由用户发布。",
            "source_refs": [
                {
                    "source_id": "source-alpha",
                    "locator": "char:0-80",
                    "quote": "Alpha evidence for layered memory",
                }
            ],
            "provenance": {
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": None,
                "document_revision": None,
                "source_content_read_id": "source-content-read-alpha",
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": "source-alpha",
                        "uri": "crp://default/sources/source-alpha.json",
                    },
                    {
                        "kind": "source_content_read",
                        "object_id": "source-content-read-alpha",
                        "uri": "crp://default/source-content-reads/source-content-read-alpha.json",
                    },
                ],
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "候选需要用户确认。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": "2026-07-02T10:00:00+08:00",
            "updated_at": "2026-07-02T10:00:00+08:00",
        }
    )
    return candidate_id


def _activate_project_skill_sqlite_authority(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    evidence = AggregateAuthorityEvidence("project-skill-route-v1", "a" * 64, "b" * 64, TARGET_IDENTITY)
    with records.begin() as transaction:
        transaction.put("aggregate_authority_targets", "default~project_skills", {"namespace_id": "default", "aggregate": "project_skills", "migration_id": evidence.migration_id, "source_fingerprint": evidence.source_fingerprint, "target_fingerprint": evidence.target_fingerprint, "target_identity": evidence.target_identity}, expected_revision=0)
        transaction.commit()
    initial = authority.create_json_active(namespace_id="default", aggregate="project_skills", reason="test initial")
    staged = authority.transition(namespace_id="default", aggregate="project_skills", expected_revision=initial.revision, to_state="sqlite_staged", evidence=evidence, reason="test staged")
    authority.transition(namespace_id="default", aggregate="project_skills", expected_revision=staged.revision, to_state="sqlite_active", evidence=evidence, reason="test active")


def _activate_shared_trust_audit_compound(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    members = (
        "memory_atoms",
        "memory_publications",
        "memory_scenarios",
        "memory_series_memory",
        "memory_transitions",
        "project_skills",
    )
    payload = shared_trust_audit_activation_payload(
        namespace_id="default",
        target_identity=TARGET_IDENTITY,
        activation_id="project-skill-route-compound-v1",
        member_migrations={member: f"{member.replace('_', '-')}-v1" for member in members},
        source_fingerprint="c" * 64,
        target_fingerprint="d" * 64,
        activated_at="2026-07-12T12:00:00+08:00",
    )
    with records.begin() as transaction:
        transaction.put("aggregate_authority_compound_activations", shared_trust_audit_activation_id("default"), payload, expected_revision=0)
        transaction.commit()


def _activate_generic_memory_publication_sqlite_authority(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    members = ("memory_atoms", "memory_publications", "memory_scenarios", "memory_series_memory", "memory_transitions", "project_skills")
    evidence = AggregateAuthorityEvidence("memory-publication-route-v1", "a" * 64, "b" * 64, TARGET_IDENTITY)
    with records.begin() as transaction:
        for member in members:
            transaction.put("aggregate_authority_targets", f"default~{member}", {"namespace_id": "default", "aggregate": member, "migration_id": evidence.migration_id, "source_fingerprint": evidence.source_fingerprint, "target_fingerprint": evidence.target_fingerprint, "target_identity": evidence.target_identity}, expected_revision=0)
        transaction.put("aggregate_authority_compound_activations", shared_trust_audit_activation_id("default"), shared_trust_audit_activation_payload(namespace_id="default", target_identity=TARGET_IDENTITY, activation_id="memory-publication-route-v1", member_migrations={member: evidence.migration_id for member in members}, source_fingerprint=evidence.source_fingerprint, target_fingerprint=evidence.target_fingerprint, activated_at="2026-07-12T21:00:00+08:00"), expected_revision=0)
        transaction.commit()
    for member in members:
        initial = authority.create_json_active(namespace_id="default", aggregate=member, reason="test initial")
        staged = authority.transition(namespace_id="default", aggregate=member, expected_revision=initial.revision, to_state="sqlite_staged", evidence=evidence, reason="test staged")
        authority.transition(namespace_id="default", aggregate=member, expected_revision=staged.revision, to_state="sqlite_active", evidence=evidence, reason="test active")


def _write_minimal_docx(path, text: str) -> None:
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
        )
        archive.writestr("word/document.xml", document_xml)


def test_rebuild_library_overview_is_mounted_in_fastapi(tmp_path) -> None:
    candidate_id = _save_layer_candidate(tmp_path, target_layer="scenario")

    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    candidate = next(item for item in body["items"] if item["item_id"] == candidate_id)
    assert candidate["item_type"] == "memory_candidate"
    assert candidate["target_layer"] == "scenario"


def test_rebuild_library_activity_overview_groups_dates_and_item_refs(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "sources",
        "source-activity-alpha",
        {
            "id": "source-activity-alpha",
            "title": "活动资料 Alpha",
            "type": "text",
            "media_type": "text/plain",
            "project_id": "project-alpha",
            "processing_state": "captured",
            "created_at": "2026-07-02T10:00:00+08:00",
            "updated_at": "2026-07-02T10:00:00+08:00",
        },
        expected_revision=None,
    )
    store.write(
        "documents",
        "document-activity-alpha",
        {
            "id": "document-activity-alpha",
            "title": "活动文档 Alpha",
            "project_id": "project-alpha",
            "status": "draft",
            "created_at": "2026-07-03T09:00:00+08:00",
            "updated_at": "2026-07-03T09:00:00+08:00",
            "source_refs": ["source-activity-alpha#source:metadata"],
        },
        expected_revision=None,
    )
    store.write(
        "sources",
        "source-activity-beta",
        {
            "id": "source-activity-beta",
            "title": "活动资料 Beta",
            "type": "text",
            "media_type": "text/plain",
            "project_id": "project-beta",
            "processing_state": "captured",
            "created_at": "2026-07-03T11:00:00+08:00",
            "updated_at": "2026-07-03T11:00:00+08:00",
        },
        expected_revision=None,
    )

    with _client(tmp_path) as client:
        all_scope = client.get("/api/rebuild/library/activity-overview")
        project_scope = client.get("/api/rebuild/library/activity-overview?project_id=project-alpha")

    assert all_scope.status_code == 200
    body = all_scope.json()
    assert body["status"] == "ready"
    assert body["counts"]["dated_items"] == 3
    assert body["date_counts"]["2026-07-02"] == 1
    assert body["date_counts"]["2026-07-03"] == 2
    assert 2026 in body["years"]
    assert body["item_refs_by_date"]["2026-07-02"][0]["item_id"] == "source-activity-alpha"
    assert body["item_refs_by_date"]["2026-07-02"][0]["type_label"] == "资料"

    assert project_scope.status_code == 200
    project_body = project_scope.json()
    assert project_body["scope"] == "project"
    assert project_body["project_id"] == "project-alpha"
    assert project_body["counts"]["dated_items"] == 2
    project_ids = {
        ref["item_id"]
        for refs in project_body["item_refs_by_date"].values()
        for ref in refs
    }
    assert project_ids == {"source-activity-alpha", "document-activity-alpha"}
    assert "source-activity-beta" not in str(project_body)


@pytest.mark.parametrize(
    ("endpoint", "payload", "source_type", "media_type"),
    [
        (
            "/api/rebuild/workbench/text-source-intake",
            {"title": "API text", "content": "下一步推进产品设计资料库统一入库模块，补齐验收标准。"},
            "text",
            "text/plain",
        ),
        (
            "/api/rebuild/workbench/link-source-intake",
            {"title": "API link", "url": "https://example.com/product-design"},
            "link",
            "text/uri-list",
        ),
        (
            "/api/rebuild/workbench/bookmark-collection-intake",
            {
                "title": "API collection",
                "urls": ["https://example.com/product-a", "https://example.com/product-b"],
            },
            "collection",
            "application/vnd.chriptmas.bookmark-collection+json",
        ),
        (
            "/api/rebuild/workbench/file-source-intake",
            {
                "title": "API file",
                "display_name": "design.pdf",
                "media_type": "application/pdf",
                "size_bytes": 4096,
                "file_reference": "platform-ref-design-pdf",
            },
            "file",
            "application/pdf",
        ),
        (
            "/api/rebuild/workbench/image-source-intake",
            {
                "title": "API image",
                "display_name": "screen.png",
                "media_type": "image/png",
                "size_bytes": 8192,
                "image_reference": "platform-image-ref",
                "width_px": 1280,
                "height_px": 720,
            },
            "image",
            "image/png",
        ),
        (
            "/api/rebuild/workbench/audio-source-intake",
            {
                "title": "API audio",
                "display_name": "meeting.mp3",
                "media_type": "audio/mpeg",
                "size_bytes": 65536,
                "audio_reference": "platform-audio-ref",
                "duration_ms": 120000,
            },
            "audio",
            "audio/mpeg",
        ),
        (
            "/api/rebuild/workbench/video-source-intake",
            {
                "title": "API video",
                "display_name": "meeting.mp4",
                "media_type": "video/mp4",
                "size_bytes": 262144,
                "video_reference": "platform-video-ref",
                "duration_ms": 180000,
                "width_px": 1920,
                "height_px": 1080,
            },
            "video",
            "video/mp4",
        ),
    ],
)
def test_rebuild_workbench_source_intake_routes_are_mounted(
    tmp_path,
    endpoint: str,
    payload: dict[str, object],
    source_type: str,
    media_type: str,
) -> None:
    with _client(tmp_path) as client:
        response = client.post(endpoint, json=payload)
        overview = client.get("/api/rebuild/library/overview")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "captured"
    assert body["source_type"] == source_type
    assert body["media_type"] == media_type
    assert body["job_status"] == "completed"
    assert body["library_bridge_item"]["item_kind"] == "source_preview"
    assert body["library_bridge_item"]["source_id"] == body["source_id"]
    assert body["library_bridge_item"]["capture_job_id"] == body["job_id"]
    assert body["library_bridge_item"]["selection_state"] == "available"
    assert body["library_bridge_item"]["memory_publication_state"] == "not_started"
    if source_type == "text":
        assert body["intake_intent"] == "project_progress"
        assert body["intake_intent_label"] == "项目推进"
        assert body["intake_route"] == "project_progress_material"
        assert "任务摘要" in body["structured_output_plan"]
        assert "project_skill" in body["memory_layer_update_plan"]
        assert "创建项目技能候选" in body["suggested_next_actions"]
    if source_type == "audio":
        assert body["auto_workflow"]["status"] == "blocked"
        assert body["auto_workflow"]["source_id"] == body["source_id"]
        assert body["auto_workflow"]["steps"][0]["name"] == "transcribe_audio"
        assert body["auto_workflow"]["blocked_operations"] == ["audio_asset_transcription"]

    assert overview.status_code == 200
    overview_body = overview.json()
    overview_item = next(item for item in overview_body["items"] if item["item_id"] == body["source_id"])
    assert overview_item["item_type"] == "source"
    assert overview_item["source_media_type"] == media_type
    assert overview_item["content_read_status"] == "not_started"


def test_rebuild_workbench_original_asset_endpoint_saves_uploaded_original_and_feeds_file_intake(tmp_path) -> None:
    content = b"fixture"
    expected_hash = hashlib.sha256(content).hexdigest()
    with _client(tmp_path) as client:
        upload = client.post(
            "/api/rebuild/workbench/original-asset",
            json={
                "display_name": "round7-note.pdf",
                "media_type": "application/pdf",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "source_kind": "file",
            },
        )
        assert upload.status_code == 201
        uploaded = upload.json()
        assert uploaded["status"] == "stored"
        assert uploaded["sha256"] == expected_hash
        assert uploaded["asset_ref"].startswith("crp-ref-default-assets-originals-original-file-")
        stored_file = (
            tmp_path
            / "library"
            / "assets"
            / "originals"
            / expected_hash[:2]
            / f"{uploaded['asset_id']}.pdf"
        )
        assert stored_file.read_bytes() == content

        intake = client.post(
            "/api/rebuild/workbench/file-source-intake",
            json={
                "title": "API file",
                "display_name": "round7-note.pdf",
                "media_type": "application/pdf",
                "size_bytes": len(content),
                "file_reference": uploaded["asset_ref"],
            },
        )
        assert intake.status_code == 201
        payload = intake.json()
        assert payload["file_reference"] == uploaded["asset_ref"]
        assert payload["asset_record"]["metadata"]["file_reference"] == uploaded["asset_ref"]

    stored = _store(tmp_path).read("workbench_original_assets", uploaded["asset_id"])
    assert stored is not None
    assert stored["sha256"] == expected_hash


def test_workbench_long_chinese_text_is_idempotent_and_survives_app_restart(tmp_path) -> None:
    unit = "真实长中文包含来源、事实、场景与下一步，保留标点，。！？；Emoji：🎄🐻\n第二行继续记录。\n"
    content = (unit * 600) + "正文终点"
    payload = {
        "content": content, "media_type": "", "file_name": "", "urls": [],
        "add_to_knowledge_base": True, "title": "长中文幂等复验",
    }
    with _client(tmp_path) as client:
        first = client.post("/api/rebuild/workbench/auto-intake", json=payload)
        second = client.post("/api/rebuild/workbench/auto-intake", json=payload)
        assert first.status_code == second.status_code == 201
        first_body, second_body = first.json(), second.json()
        source_id = first_body["items"][0]["source_id"]
        assert source_id == second_body["items"][0]["source_id"]
        assert first_body["job_id"] == second_body["job_id"]

    with _client(tmp_path) as restarted:
        overview = restarted.get("/api/rebuild/library/overview").json()
        assert sum(item["item_id"] == source_id for item in overview["items"]) == 1
    store = _store(tmp_path)
    source = store.read("sources", source_id)
    assert source is not None
    assert source["metadata"]["content"] == content
    assert source["size_bytes"] == len(content.encode("utf-8"))
    assert source["content_hash"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    job_store = SQLiteJobStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    rebuilt = job_store.rebuild_all_projections(rebuilt_at="2026-08-29T00:00:00Z")
    jobs = [record.payload for record in rebuilt if record.payload.get("source_id") == source_id]
    job_ids = {str(job["id"]) for job in jobs}
    assert {f"job-capture-{source_id}", f"job-intake-{source_id}"} <= job_ids
    assert len(job_ids) == 3
    assert sum(job_id.startswith("job-extract-memory-candidate-") for job_id in job_ids) == 1
    assert store.list("jobs") == ()
    assert store.list("memories") == ()
    assert store.list("memory_atoms") == ()


@pytest.mark.parametrize(
    ("question", "operation"),
    [
        ("请根据项目历史资料创建项目技能。", "create"),
        ("修改当前项目规则，补上来源核验。", "update"),
        ("给项目工作规则补充失败恢复步骤。", "supplement"),
        ("重构 Project Skill，让它先给结论。", "refactor"),
    ],
)
def test_project_skill_authoring_intent_endpoint_is_local_preview_only(
    tmp_path,
    question: str,
    operation: str,
) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/project-skill-authoring-intent",
            json={"project_id": "project-alpha", "question": question},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "project_skill_authoring"
    assert body["project_id"] == "project-alpha"
    assert body["operation"] == operation
    assert body["goal"] == question
    assert body["requires_user_confirmation"] is True
    assert body["provider_call_allowed"] is False
    assert body["authority_write_allowed"] is False
    assert _store(tmp_path).list("project_skills") == ()
    assert _store(tmp_path).list("project_skill_ai_drafts") == ()


def test_project_skill_authoring_intent_endpoint_keeps_normal_question_read_only(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/project-skill-authoring-intent",
            json={"project_id": "default", "question": "项目技能是什么？"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "kind": "normal_question",
        "project_id": "default",
        "operation": None,
        "goal": "",
        "evidence_scope": [],
        "requires_user_confirmation": False,
        "provider_call_allowed": False,
        "authority_write_allowed": False,
        "reason": "project_skill_target_has_no_authoring_action",
    }
    store = _store(tmp_path)
    assert store.list("project_skills") == ()
    assert store.list("project_skill_ai_drafts") == ()
    assert store.list("memory_candidates") == ()


def test_project_skill_authoring_intent_endpoint_rejects_invalid_question_without_writes(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/project-skill-authoring-intent",
            json={"project_id": "default", "question": "请创建项目规则。\u0000"},
        )

    assert response.status_code == 400
    assert response.json()["reason"] == "question contains control characters"
    store = _store(tmp_path)
    assert store.list("project_skills") == ()
    assert store.list("project_skill_ai_drafts") == ()
    assert store.list("memory_candidates") == ()


def test_rebuild_auto_intake_links_uploaded_original_asset_without_absolute_path(tmp_path) -> None:
    content = b"linked auto intake fixture"
    with _client(tmp_path) as client:
        upload = client.post(
            "/api/rebuild/workbench/original-asset",
            json={
                "display_name": "linked.pdf",
                "media_type": "application/pdf",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "source_kind": "file",
            },
        )
        assert upload.status_code == 201
        uploaded = upload.json()
        assert "path" not in uploaded
        assert uploaded["vault_ref"].startswith("assets/originals/")

        intake = client.post(
            "/api/rebuild/workbench/auto-intake",
            json={
                "content": "",
                "media_type": "application/pdf",
                "file_name": "linked.pdf",
                "urls": [],
                "add_to_knowledge_base": True,
                "title": "linked.pdf",
                "original_asset_ref": uploaded["asset_ref"],
            },
        )
        assert intake.status_code == 201
        payload = intake.json()
        item = payload["items"][0]
        assert item["auto_organization"]["original_asset_id"] == uploaded["asset_id"]
        assert item["auto_organization"]["source_asset_link_ref"].startswith("crp://default/source-assets/")
        assert str(tmp_path) not in str(payload)

    store = _store(tmp_path)
    source = store.read("sources", item["source_id"])
    assert source is not None
    assert source["metadata"]["file_reference"] == uploaded["asset_ref"]
    links = store.list("source_asset_links")
    assert len(links) == 1
    asset = store.read("workbench_original_assets", uploaded["asset_id"])
    assert asset is not None
    assert asset["link_status"] == "linked"


def test_rebuild_auto_intake_authorizes_and_reads_managed_text_original(tmp_path) -> None:
    text = "真实 TXT 正文，保留中文标点与 emoji：🎄\n\n第二段完整保留。"
    content = text.encode("utf-8")
    with _client(tmp_path) as client:
        upload = client.post(
            "/api/rebuild/workbench/original-asset",
            json={
                "display_name": "managed-note.txt",
                "media_type": "text/plain",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "source_kind": "file",
            },
        )
        assert upload.status_code == 201
        uploaded = upload.json()
        intake = client.post(
            "/api/rebuild/workbench/auto-intake",
            json={
                "content": "",
                "media_type": "text/plain",
                "file_name": "managed-note.txt",
                "urls": [],
                "add_to_knowledge_base": True,
                "title": "managed-note.txt",
                "original_asset_ref": uploaded["asset_ref"],
            },
        )

    assert intake.status_code == 201
    payload = intake.json()
    item = payload["items"][0]
    assert item["status"] == "completed"
    assert item["content_read_status"] == "completed"
    trace = item["auto_organization"]["media_auto_workflow"]
    assert trace["status"] == "completed"
    assert trace["char_count"] == len(text)
    assert str(tmp_path) not in str(payload)

    store = _store(tmp_path)
    source = store.read("sources", item["source_id"])
    assert source is not None
    authorization_id = source["metadata"]["file_authorization"]["authorization_id"]
    authorization = store.read("authorized_file_refs", authorization_id)
    assert authorization is not None
    assert authorization["status"] == "authorized"
    assert Path(str(authorization["path"])).read_bytes() == content
    content_read = store.read("source_content_reads", f"content-read-{item['source_id']}")
    assert content_read is not None
    assert content_read["text"] == text
    assert content_read["byte_count"] == len(content)
    assert content_read["text_sha256"] == hashlib.sha256(content).hexdigest()


def test_source_output_memory_candidate_route_is_reachable_and_idempotent(tmp_path) -> None:
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/text-source-intake",
            json={"title": "候选路由", "content": "真实正文先形成待审候选，不直接发布长期记忆。"},
        )
        source_id = intake.json()["source_id"]
        read = client.post(f"/api/rebuild/sources/{source_id}/content-read", json={})
        body = {
            "evidence_kind": "source_content_read",
            "project_id": "default",
            "target_layer": "atom",
            "candidate_type": "other",
        }
        first = client.post(f"/api/rebuild/sources/{source_id}/memory-candidate", json=body)
        second = client.post(f"/api/rebuild/sources/{source_id}/memory-candidate", json=body)

    assert read.status_code == 200
    assert first.status_code == second.status_code == 200
    assert first.json()["candidate_id"] == second.json()["candidate_id"]
    assert first.json()["candidate_status"] == "pending_review"
    assert first.json()["memory_publication_state"] == "candidate_created_not_published"
    store = _store(tmp_path)
    candidates = [
        item
        for item in store.list("memory_candidates")
        if any(ref.get("source_id") == source_id for ref in item.get("source_refs", ()))
    ]
    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending_review"
    assert store.list("memories") == ()
    assert store.list("memory_atoms") == ()


def test_library_source_original_asset_reports_current_bytes_without_path_leak(tmp_path) -> None:
    content = b"library-original-current"
    with _client(tmp_path) as client:
        uploaded = client.post("/api/rebuild/workbench/original-asset", json={
            "display_name": "current.txt", "media_type": "text/plain", "size_bytes": len(content),
            "content_base64": base64.b64encode(content).decode("ascii"), "source_kind": "file",
        }).json()
        intake = client.post("/api/rebuild/workbench/auto-intake", json={
            "content": "", "media_type": "text/plain", "file_name": "current.txt", "urls": [],
            "add_to_knowledge_base": True, "title": "current.txt", "original_asset_ref": uploaded["asset_ref"],
        }).json()
        source_id = intake["items"][0]["source_id"]
        available = client.get(f"/api/rebuild/library/sources/{source_id}/original-asset")
        assert available.status_code == 200
        assert available.json()["status"] == "available"
        assert "path" not in available.json()
        assert str(tmp_path) not in available.text

        stored = _store(tmp_path).read("workbench_original_assets", uploaded["asset_id"])
        assert stored is not None
        (tmp_path / "library" / str(stored["vault_ref"])).unlink()
        missing = client.get(f"/api/rebuild/library/sources/{source_id}/original-asset")
        assert missing.status_code == 200
        assert missing.json()["reason"] == "original_file_missing"


def test_rebuild_auto_intake_authorizes_vault_video_before_local_workflow(tmp_path) -> None:
    content = b"generated-video-fixture"
    with _client(tmp_path) as client:
        upload = client.post(
            "/api/rebuild/workbench/original-asset",
            json={
                "display_name": "two-hour-generated.mp4",
                "media_type": "video/mp4",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "source_kind": "video",
            },
        )
        assert upload.status_code == 201
        uploaded = upload.json()
        intake = client.post(
            "/api/rebuild/workbench/auto-intake",
            json={
                "content": "",
                "media_type": "video/mp4",
                "file_name": "two-hour-generated.mp4",
                "urls": [],
                "add_to_knowledge_base": True,
                "title": "two-hour-generated.mp4",
                "original_asset_ref": uploaded["asset_ref"],
            },
        )

    assert intake.status_code == 201
    payload = intake.json()
    item = payload["items"][0]
    trace = item["auto_organization"]["media_auto_workflow"]
    assert trace["status"] == "blocked"
    assert trace["error"] == "video audio extractor is disabled"
    assert "authorized video reference not found" not in str(trace)
    assert str(tmp_path) not in str(payload)

    store = _store(tmp_path)
    source = store.read("sources", item["source_id"])
    assert source is not None
    authorization_id = source["metadata"]["video_authorization"]["authorization_id"]
    authorization = store.read("authorized_file_refs", authorization_id)
    assert authorization is not None
    assert authorization["status"] == "authorized"
    assert authorization["source_id"] == item["source_id"]
    assert Path(str(authorization["path"])).read_bytes() == content
    assert str(tmp_path) not in str(source)


def test_rebuild_auto_intake_rejects_video_asset_vault_reference_escape(tmp_path) -> None:
    content = b"generated-video-fixture"
    with _client(tmp_path) as client:
        upload = client.post(
            "/api/rebuild/workbench/original-asset",
            json={
                "display_name": "generated.mp4",
                "media_type": "video/mp4",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "source_kind": "video",
            },
        )
        uploaded = upload.json()
        store = _store(tmp_path)
        asset = store.read("workbench_original_assets", uploaded["asset_id"])
        assert asset is not None
        store.write(
            "workbench_original_assets",
            uploaded["asset_id"],
            dict(asset) | {"vault_ref": "../outside.mp4"},
            expected_revision=None,
        )
        intake = client.post(
            "/api/rebuild/workbench/auto-intake",
            json={
                "content": "",
                "media_type": "video/mp4",
                "file_name": "generated.mp4",
                "urls": [],
                "add_to_knowledge_base": True,
                "title": "generated.mp4",
                "original_asset_ref": uploaded["asset_ref"],
            },
        )

    assert intake.status_code == 201
    item = intake.json()["items"][0]
    trace = item["auto_organization"]["media_auto_workflow"]
    assert trace["status"] == "failed"
    assert trace["blocked_reason"] == "video_source_preparation_failed"
    assert trace["error"] == "stored original video vault reference is invalid"
    assert _store(tmp_path).list("authorized_file_refs") == ()


def test_rebuild_home_text_intake_can_read_structure_and_assign_series(tmp_path) -> None:
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/text-source-intake",
            json={
                "title": "首页自动整理",
                "content": (
                    "个人 AI 记忆工作台需要把首页输入保存为资料。\n"
                    "资料进入知识库后要自动读取正文、结构化整理和归入系列。"
                ),
            },
        )
        source_id = intake.json()["source_id"]
        read = client.post(f"/api/rebuild/sources/{source_id}/content-read", json={})
        structure = client.post(
            f"/api/rebuild/sources/{source_id}/structure-content",
            json={"content_read_id": read.json()["content_read_id"]},
        )
        assignment = client.post(
            f"/api/rebuild/sources/{source_id}/series-assignment",
            json={
                "confirm": True,
                "series_name": structure.json()["series_candidate"],
                "reason": "首页自动整理测试。",
            },
        )
        overview = client.get("/api/rebuild/library/overview")

    assert intake.status_code == 201
    assert read.status_code == 200
    read_body = read.json()
    assert read_body["status"] == "completed"
    assert read_body["content_read_id"] == f"content-read-{source_id}"
    assert "个人 AI 记忆工作台" in read_body["preview"]

    assert structure.status_code == 200
    structure_body = structure.json()
    assert structure_body["status"] == "completed"
    assert structure_body["content_read_id"] == read_body["content_read_id"]
    assert structure_body["series_candidate"] == "个人 AI 记忆工作台"
    assert "long_term_memory_publication" in structure_body["blocked_operations"]

    assert assignment.status_code == 200
    assignment_body = assignment.json()
    assert assignment_body["status"] == "confirmed"
    assert assignment_body["series_name"] == "个人 AI 记忆工作台"

    overview_item = next(item for item in overview.json()["items"] if item["item_id"] == source_id)
    assert overview_item["content_read_status"] == "completed"
    assert overview_item["content_structure_status"] == "completed"
    assert overview_item["series_assignment_status"] == "confirmed"
    assert overview_item["series_name"] == "个人 AI 记忆工作台"
    assert "sk-" not in str(read_body).lower()


def test_rebuild_workbench_direct_question_does_not_create_library_item(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "这次只问答，不加入知识库。"},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["qa_mode"] == "direct_local_answer"
    assert body["knowledge_base_write"] is False
    assert body["source_created"] is False
    assert body["job_created"] is False
    assert body["library_item_created"] is False
    assert body["memory_publication_state"] == "not_published"
    assert "source_creation" in body["blocked_operations"]
    assert "long_term_memory_publication" in body["blocked_operations"]

    assert overview.status_code == 200
    overview_body = overview.json()
    assert all(item["item_id"] != body["question_id"] for item in overview_body["items"])
    store = _store(tmp_path)
    assert store.list("media_processing_jobs") == ()
    assert store.list("workflow_effect_receipts") == ()
    assert store.list("audio_auto_workflows") == ()
    assert store.list("video_auto_workflows") == ()
    assert store.list("long_audio_workflows") == ()


def test_rebuild_workbench_direct_question_uses_evidence_provider_without_business_writes(tmp_path, monkeypatch) -> None:
    class Recall:
        def execute(self, project_id: str, *, query: str, created_at: str | None = None):
            return ProjectMemoryRecallResult(
                project_id=project_id, skill_id="skill-default", request_id="recall-request-provider",
                result_id="recall-result-provider", hit_count=1, evidence_hits=({
                    "hit_id": "hit-provider", "layer": "l3_project_skill", "object_id": "skill-default",
                    "source_refs": [{"source_id": "source-provider", "locator": "char:0-30"}],
                    "snippet": "项目回答先给结论，再列证据。", "explanation": "已发布项目规则。",
                    "score": 1.0, "token_estimate": 12, "trust_status": "user_confirmed",
                },),
            )

    class Gateway:
        def invoke(self, request):
            assert request.capability == "structured"
            payload = json.loads(request.input)
            assert payload["question"] == "项目回答结构是什么？"
            source_refs = payload["evidence"][0]["source_refs"]
            assert source_refs == [{"source_id": "source-provider", "locator": "char:0-30"}]
            return ModelResult(
                {"answer": "应先给结论，再列项目证据。", "citations": ["crp://default/sources/source-provider"]},
                "provider-test",
                "model-test",
                {},
            )

    monkeypatch.setattr(workbench_ai_runtime, "CreateProjectMemoryRecall", lambda **_kwargs: Recall())
    monkeypatch.setattr(
        ai_runtime,
        "resolve_model_gateway_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(gateway=Gateway(), egress_consented=True),
    )
    with _client(tmp_path) as client:
        first = client.post("/api/rebuild/workbench/direct-question", json={"question": "项目回答结构是什么？"})
        second = client.post("/api/rebuild/workbench/direct-question", json={"question": "项目回答结构是什么？"})
        overview = client.get("/api/rebuild/library/overview")

    assert first.status_code == 200
    body = first.json()
    assert body["qa_mode"] == "provider_evidence_answer", body
    assert body["provider_call_performed"] is True
    assert body["provider_status"] == "succeeded"
    assert body["privacy"] == {
        "mode": "provider_evidence_only",
        "source_path_exposed": False,
        "provider_call_performed": True,
        "deep_evidence_ephemeral": True,
        "deep_evidence_persisted": False,
        "provider_egress_authorized": True,
    }
    assert body["source_links"] == [{
        "source_id": "source-provider", "locator": "char:0-30", "ref": "crp://default/sources/source-provider",
    }]
    assert second.json()["replayed"] is True
    assert second.json()["answer_id"] == body["answer_id"]
    assert overview.json()["items"] == []
    store = _store(tmp_path)
    assert store.list("sources") == ()
    jobs = store.list("jobs")
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "rebuild_memory_projection"
    assert jobs[0]["status"] == "completed"
    assert store.list("memory_candidates") == ()



def test_rebuild_link_web_content_read_is_mounted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><body><h1>产品设计网页</h1>"
            "<p>链接网页正文已经保存为 source_content_read。</p></body></html>"
        ),
    )
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "产品设计网页", "url": "https://example.com/product-design"},
        )
        source_id = intake.json()["source_id"]
        read = client.post(f"/api/rebuild/sources/{source_id}/web-content", json={})
        overview = client.get("/api/rebuild/library/overview")

    assert intake.status_code == 201
    assert read.status_code == 200
    body = read.json()
    assert body["status"] == "completed"
    assert body["media_type"] == "text/html"
    assert body["content_read"] is True
    assert "链接网页正文" in body["preview"]
    overview_item = next(item for item in overview.json()["items"] if item["item_id"] == source_id)
    assert overview_item["content_read_status"] == "completed"
    assert overview_item["content_read"] is True
    assert "source_content_read" not in overview_item["blocked_operations"]
    assert "sk-" not in str(body).lower()


def test_rebuild_source_content_structure_is_mounted_for_completed_link_read(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><body><h1>个人 AI 记忆工作台</h1>"
            "<p>产品设计需要把链接资料整理为可搜索的长期记忆。</p>"
            "<p>项目推进和视频转写材料需要自动进入系列候选。</p></body></html>"
        ),
    )
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "个人 AI 记忆工作台资料", "url": "https://example.com/memory-workbench"},
        )
        source_id = intake.json()["source_id"]
        read = client.post(f"/api/rebuild/sources/{source_id}/web-content", json={})
        structure = client.post(f"/api/rebuild/sources/{source_id}/structure-content", json={})
        overview = client.get("/api/rebuild/library/overview")

    assert intake.status_code == 201
    assert read.status_code == 200
    assert structure.status_code == 200
    body = structure.json()
    assert body["status"] == "completed"
    assert body["content_read_id"] == f"content-read-{source_id}"
    assert {"Memory", "Product"}.issubset(set(body["tags"]))
    assert body["series_candidate"] == "个人 AI 记忆工作台"
    assert body["memory_publication_state"] == "not_published"
    assert "long_term_memory_publication" in body["blocked_operations"]
    assert len(body["paragraph_tags"]) >= 2

    overview_item = next(item for item in overview.json()["items"] if item["item_id"] == source_id)
    assert overview_item["content_structure_status"] == "completed"
    assert {"Memory", "Product"}.issubset(set(overview_item["content_tags"]))
    assert overview_item["series_candidate"] == "个人 AI 记忆工作台"
    assert overview_item["paragraph_tags"][0]["paragraph_id"] == "p001"
    assert body["structure_ref"] in overview_item["trace_refs"]
    assert "sk-" not in str(body).lower()


def test_rebuild_source_series_assignment_requires_confirmation_and_updates_overview(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><body><h1>个人 AI 记忆工作台</h1>"
            "<p>资料库和记忆系统需要先结构化整理，再确认系列归类。</p></body></html>"
        ),
    )
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "个人 AI 记忆工作台资料", "url": "https://example.com/memory-workbench"},
        )
        source_id = intake.json()["source_id"]
        assert client.post(f"/api/rebuild/sources/{source_id}/web-content", json={}).status_code == 200
        assert client.post(f"/api/rebuild/sources/{source_id}/structure-content", json={}).status_code == 200
        rejected = client.post(f"/api/rebuild/sources/{source_id}/series-assignment", json={})
        assigned = client.post(
            f"/api/rebuild/sources/{source_id}/series-assignment",
            json={"confirm": True},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert rejected.status_code == 400
    assert "explicit user confirmation" in rejected.json()["reason"]
    assert assigned.status_code == 200
    body = assigned.json()
    assert body["status"] == "confirmed"
    assert body["series_name"] == "个人 AI 记忆工作台"
    assert body["memory_publication_state"] == "not_published"
    assert body["layered_memory_drafts"]["status"] == "candidates_created"
    assert {item["target_layer"] for item in body["layered_memory_drafts"]["candidates"]} == {
        "series_memory",
        "project_skill",
    }
    assert "long_term_memory_publication" in body["blocked_operations"]
    overview_item = next(item for item in overview.json()["items"] if item["item_id"] == source_id)
    assert overview_item["series_assignment_status"] == "confirmed"
    assert overview_item["series_name"] == "个人 AI 记忆工作台"


def test_rebuild_inspiration_record_and_collision_are_mounted(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "sources",
        "source-idea",
        {"id": "source-idea", "title": "灵感", "type": "text", "metadata": {}},
        expected_revision=None,
    )
    with _client(tmp_path) as client:
        recorded = client.post(
            "/api/rebuild/sources/source-idea/inspiration",
            json={
                "text": "灵感：资料库可以让想法默认进入灵感系列，再和项目 skill 碰撞。",
                "project_id": "project-alpha",
            },
        )
        collision = client.post(
            "/api/rebuild/inspirations/collision",
            json={"query": "资料库和项目 skill", "themes": ["项目", "记忆"], "project_id": "project-alpha"},
        )
        overview = client.get("/api/rebuild/inspirations/overview?project_id=project-alpha")

    assert recorded.status_code == 200
    body = recorded.json()
    assert body["status"] == "recorded"
    assert body["series_name"] == "灵感 · project-alpha"
    assert body["memory_publication_state"] == "not_published"
    assert "automatic_long_term_memory_publication" in body["blocked_operations"]

    assert collision.status_code == 200
    collision_body = collision.json()
    assert collision_body["status"] == "created"
    assert collision_body["selected_fragments"]
    assert collision_body["prompts"]
    assert "model_provider_execution" in collision_body["blocked_operations"]
    assert "sk-" not in str(collision_body).lower()

    assert overview.status_code == 200
    overview_body = overview.json()
    assert overview_body["status"] == "ready"
    assert overview_body["counts"]["records"] == 1
    assert overview_body["counts"]["series"] == 1
    assert overview_body["counts"]["collisions"] == 1
    assert overview_body["heatmap_days"]
    assert overview_body["records"][0]["summary"]
    assert overview_body["recommended_prompts"]














def test_rebuild_daily_reminders_today_returns_attention_items(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_candidates",
        "candidate-api",
        {
            "id": "candidate-api",
            "status": "pending_review",
            "project_id": "project-alpha",
            "source_refs": ["source:api"],
        },
        expected_revision=None,
    )
    store.write(
        "video_auto_workflows",
        "video-auto-workflow-api",
        {
            "workflow_id": "video-auto-workflow-api",
            "status": "blocked",
            "project_id": "project-alpha",
            "source_id": "source-video-api",
            "blocked_operations": ["transcript_summary"],
        },
        expected_revision=None,
    )
    store.write(
        "sources",
        "source-api-failed-document",
        {
            "id": "source-api-failed-document",
            "type": "file",
            "title": "Failed API document",
            "storage_uri": "crp://default/sources/source-api-failed-document",
            "metadata": {
                "project_id": "project-alpha",
                "content_read": {"status": "failed", "content_read": False},
            },
        },
        expected_revision=None,
    )
    store.write(
        "source_content_reads",
        "content-read-source-api-failed-document",
        {
            "id": "content-read-source-api-failed-document",
            "source_id": "source-api-failed-document",
            "status": "failed",
            "media_type": "application/pdf",
            "error": "document extractor failed",
        },
        expected_revision=None,
    )

    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/reminders/today?project_id=project-alpha")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["project_id"] == "project-alpha"
    assert body["counts"]["total"] == 4
    assert body["counts"]["failed_extraction"] == 1
    assert body["counts"]["provider_attention"] == 1
    assert {item["reminder_type"] for item in body["reminders"]} == {
        "extraction_failed_review_needed",
        "memory_candidate_pending_review",
        "video_auto_workflow_blocked",
        "provider_local_readiness_needed",
    }
    assert body["memory_publication_state"] == "not_published"
    assert "model_provider_execution" in body["blocked_operations"]


def test_rebuild_daily_reminders_today_response_excludes_secrets(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_candidates",
        "candidate-secret-check",
        {
            "id": "candidate-secret-check",
            "status": "pending_review",
            "project_id": "project-alpha",
            "source_refs": ["source:secret-check"],
        },
        expected_revision=None,
    )

    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/reminders/today?project_id=project-alpha")

    assert response.status_code == 200
    body = response.json()
    # blocked_operations 包含 "cookie_read" 等内部状态枚举（开发者用，不展示给用户），
    # 因此隐私断言只检查用户可见字段：reminders 数组里每条 reminder 的文本与 refs。
    forbidden_fragments = ("sk-", "bearer", "authorization", "password", "token")
    for reminder in body["reminders"]:
        visible_text = " ".join(
            str(reminder.get(field, ""))
            for field in ("title", "summary", "action", "due_label", "reminder_type")
        ).lower()
        visible_refs = " ".join(
            str(ref).lower() for ref in reminder.get("source_refs", [])
        ) + " ".join(
            str(rid).lower() for rid in reminder.get("related_ids", [])
        )
        for forbidden in forbidden_fragments:
            assert forbidden not in visible_text, f"reminder visible text leaked: {forbidden}"
            assert forbidden not in visible_refs, f"reminder refs leaked: {forbidden}"
        assert "cookie" not in visible_text
        assert "cookie" not in visible_refs






def test_rebuild_series_memory_skill_drafts_are_reviewable_from_confirmed_series(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><body><h1>个人 AI 记忆工作台</h1>"
            "<p>资料库需要把结构化摘要、段落级标签和系列归类沉淀为四层记忆。</p>"
            "<p>项目 Skill 需要记录默认阅读要求、统一输出模板和来源引用。</p></body></html>"
        ),
    )
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "个人 AI 记忆工作台资料", "url": "https://example.com/memory-workbench"},
        )
        source_id = intake.json()["source_id"]
        assert client.post(f"/api/rebuild/sources/{source_id}/web-content", json={}).status_code == 200
        assert client.post(f"/api/rebuild/sources/{source_id}/structure-content", json={}).status_code == 200
        assert client.post(
            f"/api/rebuild/sources/{source_id}/series-assignment",
            json={"confirm": True},
        ).status_code == 200
        drafts = client.post(
            f"/api/rebuild/sources/{source_id}/series-memory-skill-drafts",
            json={"project_id": "project-alpha"},
        )
        body = drafts.json()
        series_candidate_id = next(
            item["candidate_id"] for item in body["candidates"] if item["target_layer"] == "series_memory"
        )
        project_skill_candidate_id = next(
            item["candidate_id"] for item in body["candidates"] if item["target_layer"] == "project_skill"
        )
        review = client.post(
            f"/api/rebuild/memory-candidates/{series_candidate_id}/review",
            json={
                "action": "promote_to_series_memory",
                "reason": "确认系列总览候选可进入 staging。",
                "series_id": body["series_id"],
            },
        )
        overview = client.get("/api/rebuild/library/overview")

    assert drafts.status_code == 200
    assert body["status"] == "candidates_created"
    assert body["project_id"] == "project-alpha"
    assert body["series_name"] == "个人 AI 记忆工作台"
    assert body["memory_publication_state"] == "candidate_created_not_published"
    assert {item["target_layer"] for item in body["candidates"]} == {"series_memory", "project_skill"}
    assert "automatic_memory_publication" in body["blocked_operations"]
    candidates = ObjectStoreMemoryCandidateRepository(_store(tmp_path))
    series_candidate = candidates.get(series_candidate_id)
    project_skill_candidate = candidates.get(project_skill_candidate_id)
    assert series_candidate is not None
    assert series_candidate["status"] == "promoted"
    assert project_skill_candidate is not None
    assert project_skill_candidate["status"] == "pending_review"
    assert review.status_code == 200
    assert review.json()["memory_publication_state"] == "staging_series_memory_created_not_published"
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    staged_record = records.read(
        "staging_series_memory",
        review.json()["promoted_object_id"],
    )
    staged = staged_record.payload if staged_record is not None else None
    assert staged is not None
    assert staged["series_id"] == body["series_id"]
    assert records.read("memory_series_memory", staged["id"]) is None
    overview_ids = {item["item_id"] for item in overview.json()["items"]}
    assert source_id in overview_ids
    assert series_candidate_id in overview_ids
    assert project_skill_candidate_id in overview_ids
    assert "sk-" not in str(body).lower()
    assert "sk-" not in str(review.json()).lower()


def test_rebuild_source_template_document_creates_editable_draft_from_structure(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><body><h1>个人 AI 记忆工作台</h1>"
            "<p>结构化整理需要形成摘要、关键点和统一输出模板。</p>"
            "<p>项目总结需要保留待确认事项和来源引用。</p></body></html>"
        ),
    )
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "个人 AI 记忆工作台资料", "url": "https://example.com/memory-workbench"},
        )
        source_id = intake.json()["source_id"]
        assert client.post(f"/api/rebuild/sources/{source_id}/web-content", json={}).status_code == 200
        structure = client.post(f"/api/rebuild/sources/{source_id}/structure-content", json={})
        assert structure.status_code == 200
        assert client.post(
            f"/api/rebuild/sources/{source_id}/series-assignment",
            json={"confirm": True},
        ).status_code == 200
        template = client.post(
            f"/api/rebuild/sources/{source_id}/template-document",
            json={"template_type": "review"},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert template.status_code == 200
    body = template.json()
    assert body["status"] == "document_created"
    assert body["template_type"] == "review"
    assert body["template_label"] == "复盘"
    assert body["document_type"] == "review"
    assert "不得编造不存在的事实" in body["template_prompt"]
    assert body["memory_publication_state"] == "not_published"
    assert "long_term_memory_publication" in body["blocked_operations"]
    documents = ObjectStoreDocumentRepository(_store(tmp_path))
    stored = documents.read(body["document_id"])
    markdown = documents.markdown(body["document_id"])
    assert stored is not None
    assert stored["status"] == "draft"
    assert markdown is not None
    assert "## 生成提示词" in markdown
    assert "## 待确认" in markdown
    overview_item_ids = {item["item_id"] for item in overview.json()["items"]}
    assert body["document_id"] in overview_item_ids
    assert "sk-" not in str(body).lower()


def test_rebuild_provider_source_template_document_creates_editable_draft(
    tmp_path,
    monkeypatch,
) -> None:
    class Gateway:
        def invoke(self, request):
            assert request.capability == "structured"
            payload = json.loads(request.input)
            assert payload["source_title"] == "AI 增强模板资料"
            return ModelResult(
                {
                    "title": "AI 增强复盘",
                    "markdown": "## 背景\n\n模板草稿来自 Source。\n\n## 判断\n\n把模板草稿接入候选审核。",
                },
                "deepseek-template-test",
                "deepseek-chat",
                {},
            )

    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><body><h1>AI 增强模板资料</h1>"
            "<p>复盘模板需要沉淀背景、事实、行动和待确认内容。</p></body></html>"
        ),
    )
    monkeypatch.setattr(
        ai_runtime,
        "resolve_model_gateway_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(gateway=Gateway(), egress_consented=True),
    )
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "AI 增强模板资料", "url": "https://example.com/provider-template"},
        )
        source_id = intake.json()["source_id"]
        assert client.post(f"/api/rebuild/sources/{source_id}/web-content", json={}).status_code == 200
        assert client.post(f"/api/rebuild/sources/{source_id}/structure-content", json={}).status_code == 200
        assert client.post(
            f"/api/rebuild/sources/{source_id}/series-assignment",
            json={"confirm": True},
        ).status_code == 200
        response = client.post(
            f"/api/rebuild/sources/{source_id}/provider-template-document",
            json={"template_type": "review"},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "document_created"
    assert body["provider_enhanced"] is True
    assert body["provider_name"] == "deepseek-template-test"
    assert body["template_type"] == "review"
    assert body["document_type"] == "review"
    assert body["memory_publication_state"] == "not_published"
    documents = ObjectStoreDocumentRepository(_store(tmp_path))
    markdown = documents.markdown(body["document_id"])
    assert markdown is not None
    assert "## 判断" in markdown  # review outline 的 body section 标题
    assert "把模板草稿接入候选审核" in markdown
    overview_item_ids = {item["item_id"] for item in overview.json()["items"]}
    assert body["document_id"] in overview_item_ids
    assert "sk-" not in str(body).lower()
    assert "api_key" not in str(body).lower()


def test_rebuild_media_output_template_document_creates_editable_draft(tmp_path) -> None:
    store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(
            kind="video",
            title="资料库视频总结",
            display_name="library-video.mp4",
            media_type="video/mp4",
            size_bytes=4096,
            video_reference="bilibili/BV1library/p1",
            duration_ms=8000,
        )
    )
    output_id = f"media-output-summary-{source['id']}"
    job_id = f"media-job-summary-{source['id']}"
    store.write(
        "media_processing_jobs",
        job_id,
        {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source["id"],
            "source_type": "video",
            "required_capability": "transcript_summary",
            "status": "completed",
            "disabled_reason": None,
            "input_refs": [],
            "expected_output_refs": [],
            "adapter_contract": {},
            "error": None,
            "activity_refs": [],
            "output_refs": [f"crp://default/media-processing-outputs/{output_id}.json"],
            "created_at": "2026-07-02T19:20:00+08:00",
            "updated_at": "2026-07-02T19:20:00+08:00",
        },
        expected_revision=None,
    )
    store.write(
        "media_processing_outputs",
        output_id,
        {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source["id"],
            "source_type": "video",
            "output_kind": "summary",
            "status": "completed",
            "provider": "local-command-summary",
            "title": "资料库视频总结",
            "preview": "总结输出：视频讨论了资料库视频标签和四层记忆。",
            "text": "## 摘要\n视频讨论了资料库视频标签和四层记忆。",
            "markdown": "## 摘要\n视频讨论了资料库视频标签和四层记忆。",
            "summary_data": {
                "title": "资料库视频总结",
                "thirty_second_summary": "视频说明资料库视频标签应能生成统一模板草稿。",
                "chapters": [{"title": "视频标签", "summary": "从总结进入模板和候选审核。"}],
            },
            "metadata": {"memory_publication": "not_started"},
            "memory_publication": "not_started",
            "created_at": "2026-07-02T19:20:00+08:00",
            "ref": f"crp://default/media-processing-outputs/{output_id}.json",
        },
        expected_revision=None,
    )
    with _client(tmp_path) as client:
        response = client.post(
            f"/api/rebuild/media-processing-outputs/{output_id}/template-document",
            json={"template_type": "project_summary"},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "document_created"
    assert body["template_type"] == "project_summary"
    assert body["template_label"] == "项目总结"
    assert body["document_type"] == "project_summary"
    assert body["media_output_id"] == output_id
    assert body["media_output_kind"] == "summary"
    assert body["memory_publication_state"] == "not_published"
    assert "long_term_memory_publication" in body["blocked_operations"]
    documents = ObjectStoreDocumentRepository(_store(tmp_path))
    markdown = documents.markdown(body["document_id"])
    assert markdown is not None
    assert "## 当前进展" in markdown  # project_summary outline 的 body section 标题
    assert "视频标签：从总结进入模板" in markdown
    overview_item_ids = {item["item_id"] for item in overview.json()["items"]}
    assert body["document_id"] in overview_item_ids
    assert "sk-" not in str(body).lower()


def test_rebuild_media_output_template_document_defaults_to_media_summary(tmp_path) -> None:
    """media output 路由默认应使用 media_summary 模板，渲染媒体专用章节标题。"""
    store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(
            kind="video",
            title="媒体总结默认模板视频",
            display_name="media-summary-default.mp4",
            media_type="video/mp4",
            size_bytes=4096,
            video_reference="bilibili/BV1media-summary/p1",
            duration_ms=8000,
        )
    )
    output_id = f"media-output-summary-{source['id']}"
    job_id = f"media-job-summary-{source['id']}"
    store.write(
        "media_processing_jobs",
        job_id,
        {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source["id"],
            "source_type": "video",
            "required_capability": "transcript_summary",
            "status": "completed",
            "disabled_reason": None,
            "input_refs": [],
            "expected_output_refs": [],
            "adapter_contract": {},
            "error": None,
            "activity_refs": [],
            "output_refs": [f"crp://default/media-processing-outputs/{output_id}.json"],
            "created_at": "2026-07-02T19:20:00+08:00",
            "updated_at": "2026-07-02T19:20:00+08:00",
        },
        expected_revision=None,
    )
    store.write(
        "media_processing_outputs",
        output_id,
        {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source["id"],
            "source_type": "video",
            "output_kind": "summary",
            "status": "completed",
            "provider": "local-command-summary",
            "title": "媒体总结默认模板视频",
            "preview": "总结输出：视频讨论了媒体总结模板和关键结论。",
            "text": "## 摘要\n视频讨论了媒体总结模板的默认章节结构。",
            "markdown": "## 摘要\n视频讨论了媒体总结模板的默认章节结构。",
            "summary_data": {
                "title": "媒体总结默认模板视频",
                "thirty_second_summary": "视频说明媒体总结模板应渲染核心摘要与关键结论。",
                "core_problem": "如何让媒体输出走专用模板。",
                "key_takeaways": ["默认走 media_summary 模板", "渲染媒体专用章节标题"],
                "chapters": [{"title": "媒体模板", "summary": "从总结进入媒体专用模板。"}],
            },
            "metadata": {"memory_publication": "not_started"},
            "memory_publication": "not_started",
            "created_at": "2026-07-02T19:20:00+08:00",
            "ref": f"crp://default/media-processing-outputs/{output_id}.json",
        },
        expected_revision=None,
    )
    with _client(tmp_path) as client:
        response = client.post(
            f"/api/rebuild/media-processing-outputs/{output_id}/template-document",
            json={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "document_created"
    assert body["template_type"] == "media_summary"
    assert body["template_label"] == "媒体总结"
    assert body["document_type"] == "media_summary"
    assert body["media_output_id"] == output_id
    assert body["media_output_kind"] == "summary"
    assert body["memory_publication_state"] == "not_published"
    assert "long_term_memory_publication" in body["blocked_operations"]
    documents = ObjectStoreDocumentRepository(_store(tmp_path))
    markdown = documents.markdown(body["document_id"])
    assert markdown is not None
    # media_summary outline 的章节标题
    assert "## 核心摘要" in markdown
    assert "## 关键结论" in markdown
    assert "## 媒体正文" in markdown
    assert "## 追溯来源" in markdown
    # 关键结论应包含 key_takeaways 内容
    assert "默认走 media_summary 模板" in markdown
    # 核心摘要应优先用 thirty_second_summary
    assert "视频说明媒体总结模板应渲染核心摘要与关键结论" in markdown
    # 不应出现其它模板的章节
    assert "## 结论" not in markdown
    assert "## 背景" not in markdown
    assert "## 目标" not in markdown
    assert "sk-" not in str(body).lower()


def test_rebuild_source_template_document_creates_reviewable_memory_candidate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><body><h1>项目 Skill 更新</h1>"
            "<p>统一输出模板需要沉淀默认阅读要求、输出结构和下一步行动。</p>"
            "<p>候选进入四层记忆后必须先 review 再发布。</p></body></html>"
        ),
    )
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "项目 Skill 更新", "url": "https://example.com/project-skill"},
        )
        source_id = intake.json()["source_id"]
        assert client.post(f"/api/rebuild/sources/{source_id}/web-content", json={}).status_code == 200
        assert client.post(f"/api/rebuild/sources/{source_id}/structure-content", json={}).status_code == 200
        assert client.post(
            f"/api/rebuild/sources/{source_id}/series-assignment",
            json={"confirm": True},
        ).status_code == 200
        template = client.post(
            f"/api/rebuild/sources/{source_id}/template-document",
            json={"template_type": "project_summary"},
        )
        template_body = template.json()
        candidate = client.post(
            f"/api/rebuild/documents/{template_body['document_id']}/template-memory-candidate",
            json={"document_revision": template_body["document_revision"]},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert candidate.status_code == 200
    body = candidate.json()
    assert body["status"] == "candidate_created"
    assert body["document_type"] == "project_summary"
    assert body["target_layer"] == "project_skill"
    assert body["memory_publication_state"] == "candidate_created_not_published"
    assert "候选必须等待用户审核" in body["template_prompt"]
    stored_candidate = _store(tmp_path).read("memory_candidates", body["candidate_id"])
    assert stored_candidate is not None
    assert stored_candidate["status"] == "pending_review"
    assert stored_candidate["review"]["auto_promote_allowed"] is False
    overview_item = next(item for item in overview.json()["items"] if item["item_id"] == body["candidate_id"])
    assert overview_item["item_type"] == "memory_candidate"
    assert overview_item["target_layer"] == "project_skill"
    assert "sk-" not in str(body).lower()


def test_rebuild_link_web_content_can_answer_and_create_reviewable_memory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><head><title>产品设计网页</title></head><body>"
            "<h1>资料库网页入库</h1>"
            "<p>链接网页正文需要保存为可读写资料，并进入资料库问答。</p>"
            "<p>网页回答结果可以生成可编辑文档，也可以形成待审记忆候选。</p>"
            "</body></html>"
        ),
    )
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "资料库网页入库", "url": "https://example.com/product-design-library"},
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]
        read = client.post(f"/api/rebuild/sources/{source_id}/web-content", json={})
        assert read.status_code == 200
        recall = client.post(
            f"/api/rebuild/sources/{source_id}/qa-recall",
            json={
                "question": "链接网页正文如何进入资料库问答？",
                "project_id": "project-alpha",
                "project_skill_id": "skill-product-design",
                "content_read_id": f"content-read-{source_id}",
            },
        )
        assert recall.status_code == 200
        answer = client.post(
            f"/api/rebuild/model-requests/{recall.json()['model_request_id']}/local-answer",
            json={},
        )
        assert answer.status_code == 200
        model_result_id = answer.json()["model_result_id"]
        document = client.post(
            f"/api/rebuild/model-results/{model_result_id}/document",
            json={"title": "网页资料问答回答草稿"},
        )
        candidate = client.post(
            f"/api/rebuild/model-results/{model_result_id}/memory-candidate",
            json={"target_layer": "atom", "candidate_type": "answer_fact"},
        )
        four_layer = client.post(
            f"/api/rebuild/sources/{source_id}/four-layer-candidates",
            json={
                "project_id": "project-alpha",
                "evidence_kind": "source_content_read",
                "evidence_id": f"content-read-{source_id}",
                "allowed_layers": ["scenario"],
            },
        )
        overview = client.get("/api/rebuild/library/overview")

    read_body = read.json()
    assert read_body["status"] == "completed"
    assert read_body["media_type"] == "text/html"
    assert "链接网页正文" in read_body["preview"]
    recall_body = recall.json()
    assert recall_body["status"] == "model_request_created"
    assert recall_body["content_read_id"] == f"content-read-{source_id}"
    assert recall_body["qa_answer_state"] == "model_request_ready_no_answer_generated"
    assert answer.json()["qa_answer_state"] == "local_answer_completed"
    assert document.status_code == 200
    assert document.json()["status"] == "document_created"
    assert candidate.status_code == 200
    assert candidate.json()["candidate_status"] == "pending_review"
    assert four_layer.status_code == 409
    assert four_layer.json()["detail"] == "memory candidate provider is unavailable"
    store = _store(tmp_path)
    recall_result = ObjectStoreRecallRepository(store).get_result(recall_body["recall_result_id"])
    assert recall_result is not None
    assert recall_result["hits"][0]["layer"] == "l0_source"
    assert "资料库问答" in recall_result["hits"][0]["snippet"]
    stored_document = ObjectStoreDocumentRepository(store).read(document.json()["document_id"])
    stored_candidate = ObjectStoreMemoryCandidateRepository(store).get(candidate.json()["candidate_id"])
    assert stored_document is not None
    assert stored_document["status"] == "draft"
    assert stored_candidate is not None
    assert stored_candidate["status"] == "pending_review"
    overview_item_ids = {item["item_id"] for item in overview.json()["items"]}
    assert source_id in overview_item_ids
    assert document.json()["document_id"] in overview_item_ids
    assert candidate.json()["candidate_id"] in overview_item_ids
    assert "sk-" not in str(read_body).lower()
    assert "sk-" not in str(recall_body).lower()
    assert "sk-" not in str(answer.json()).lower()
    assert "sk-" not in str(document.json()).lower()
    assert "sk-" not in str(candidate.json()).lower()
    assert "sk-" not in str(four_layer.json()).lower()


def test_rebuild_document_detail_endpoint_reads_saves_and_rejects_stale_revision(tmp_path) -> None:
    store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(store)
    document = documents.create(
        DocumentDraft(
            title="资料库可编辑预览",
            document_type="answer_manual",
            markdown="# 资料库可编辑预览\n\n初始正文。",
            project_id="project-alpha",
            source_refs=(
                {
                    "source_id": "source-text-001",
                    "locator": "source-text-001#source:content",
                    "kind": "source_content_read",
                },
            ),
        )
    )
    document_id = str(document["id"])

    with _client(tmp_path) as client:
        detail = client.get(f"/api/rebuild/documents/{document_id}")
        saved = client.put(
            f"/api/rebuild/documents/{document_id}",
            json={
                "title": "用户编辑标题",
                "markdown": "# 用户编辑标题\n\n保存后的正文。",
                "expected_revision": 1,
            },
        )
        stale = client.put(
            f"/api/rebuild/documents/{document_id}",
            json={
                "title": "旧 revision 编辑",
                "markdown": "# stale",
                "expected_revision": 1,
            },
        )

    assert detail.status_code == 200
    assert detail.json()["status"] == "document_ready"
    assert detail.json()["document_id"] == document_id
    assert detail.json()["revision"] == 1
    assert "初始正文" in detail.json()["markdown"]
    assert detail.json()["memory_publication_state"] == "not_published"
    assert "document_overwrite_without_revision" in detail.json()["blocked_operations"]

    assert saved.status_code == 200
    saved_body = saved.json()
    assert saved_body["status"] == "document_saved"
    assert saved_body["title"] == "用户编辑标题"
    assert saved_body["revision"] == 2
    assert "保存后的正文" in saved_body["markdown"]
    assert saved_body["memory_publication_state"] == "not_published"

    reloaded = documents.read(document_id)
    assert reloaded is not None
    assert reloaded["title"] == "用户编辑标题"
    assert reloaded["revision"] == 2
    assert documents.markdown(document_id) == "# 用户编辑标题\n\n保存后的正文。"

    assert stale.status_code == 409
    stale_body = stale.json()
    assert stale_body["detail"] == "document revision conflict"
    assert stale_body["current_revision"] == 2
    assert stale_body["current_document"]["status"] == "document_current_after_conflict"
    assert stale_body["current_document"]["revision"] == 2
    assert stale_body["current_document"]["title"] == "用户编辑标题"
    assert stale_body["current_document"]["markdown"] == "# 用户编辑标题\n\n保存后的正文。"
    assert stale_body["current_document"]["source_refs"] == [
        {"source_id": "source-text-001", "locator": "source-text-001#source:content"}
    ]
    assert len(documents.revisions(document_id)) == 2


def test_rebuild_bookmark_collection_intake_creates_collection_and_link_sources(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/workbench/bookmark-collection-intake",
            json={
                "title": "产品资料收藏夹",
                "urls": ["https://example.com/a", "https://example.com/b"],
            },
        )
        overview = client.get("/api/rebuild/library/overview")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "captured"
    assert body["source_type"] == "collection"
    assert body["collection_item_count"] == 2
    assert len(body["child_source_ids"]) == 2
    assert body["remote_fetch_state"] == "not_performed"
    store = _store(tmp_path)
    collection_source = store.read("sources", body["source_id"])
    assert collection_source is not None
    assert collection_source["metadata"]["collection_type"] == "bookmark_collection"
    assert collection_source["metadata"]["item_count"] == 2
    for child_source_id in body["child_source_ids"]:
        child_source = store.read("sources", child_source_id)
        assert child_source is not None
        assert child_source["type"] == "link"
        assert child_source["media_type"] == "text/uri-list"
    assert overview.status_code == 200
    overview_items = {item["item_id"]: item for item in overview.json()["items"]}
    assert body["source_id"] in overview_items
    collection_item = overview_items[body["source_id"]]
    assert collection_item["source_media_type"] == "application/vnd.chriptmas.bookmark-collection+json"
    assert collection_item["collection_item_count"] == 2
    assert collection_item["collection_urls"] == ["https://example.com/a", "https://example.com/b"]
    assert collection_item["collection_child_source_ids"] == body["child_source_ids"]
    for child_source_id in body["child_source_ids"]:
        assert f"crp://default/sources/{child_source_id}" in collection_item["trace_refs"]
    assert "source_content_read" not in collection_item["blocked_operations"]
    for child_source_id in body["child_source_ids"]:
        assert child_source_id in overview_items
        assert overview_items[child_source_id]["source_media_type"] == "text/uri-list"
    assert "sk-" not in str(body).lower()


def test_rebuild_bookmark_collection_web_content_reads_child_links(tmp_path, monkeypatch) -> None:
    def fake_fetch(url: str) -> str:
        return f"<html><body><h1>产品资料</h1><p>{url} 已转成可读写网页正文。</p></body></html>"

    monkeypatch.setattr(product_source_content, "_fetch_url_text", fake_fetch)
    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/bookmark-collection-intake",
            json={
                "title": "产品资料收藏夹",
                "urls": ["https://example.com/a", "https://example.com/b"],
            },
        )
        body = intake.json()
        read = client.post(f"/api/rebuild/sources/{body['source_id']}/collection-web-content", json={})
        overview = client.get("/api/rebuild/library/overview")

    assert intake.status_code == 201
    assert read.status_code == 200
    read_body = read.json()
    assert read_body["status"] == "completed"
    assert read_body["collection_item_count"] == 2
    assert read_body["completed_count"] == 2
    assert read_body["failed_count"] == 0
    assert read_body["child_source_ids"] == body["child_source_ids"]
    assert all(result["status"] == "completed" for result in read_body["child_results"])
    assert read_body["memory_publication_state"] == "not_published"
    assert "cookie_read" in read_body["blocked_operations"]
    assert "long_term_memory_publication" in read_body["blocked_operations"]
    store = _store(tmp_path)
    for child_source_id in body["child_source_ids"]:
        read_record = store.read("source_content_reads", f"content-read-{child_source_id}")
        assert read_record is not None
        assert read_record["status"] == "completed"
        assert "已转成可读写网页正文" in read_record["text"]
    overview_items = {item["item_id"]: item for item in overview.json()["items"]}
    assert "source_content_read" not in overview_items[body["source_id"]]["blocked_operations"]
    for child_source_id in body["child_source_ids"]:
        child_item = overview_items[child_source_id]
        assert child_item["content_read"] is True
        assert child_item["content_read_status"] == "completed"
    assert "sk-" not in str(read_body).lower()


def test_rebuild_docx_file_authorization_and_document_text_run_are_mounted(tmp_path) -> None:
    docx_path = tmp_path / "product-design.docx"
    _write_minimal_docx(docx_path, "DOCX 正文读取已进入资料库。")
    extractor_code = (
        "import sys, zipfile, xml.etree.ElementTree as ET; "
        "data=zipfile.ZipFile(sys.argv[1]).read('word/document.xml'); "
        "root=ET.fromstring(data); "
        "ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'; "
        "print('\\n'.join(t.text or '' for t in root.iter(ns+'t')))"
    )

    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/file-source-intake",
            json={
                "title": "DOCX source",
                "display_name": "product-design.docx",
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": docx_path.stat().st_size,
                "file_reference": "platform-ref-product-design-docx",
            },
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]

        authorization = client.post(
            f"/api/rebuild/sources/{source_id}/file-authorization",
            json={"file_path": str(docx_path)},
        )
        assert authorization.status_code == 200
        auth_body = authorization.json()
        assert auth_body["authorization_id"] == f"authorized-document-{source_id}"
        assert auth_body["media_type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

        settings = client.put(
            "/api/rebuild/settings/local-document-text-extractor",
            json={
                "enabled": True,
                "provider_name": "stdlib-docx-smoke",
                "command": [sys.executable, "-c", extractor_code, "{document_path}"],
                "confirm_enable": True,
            },
        )
        assert settings.status_code == 200
        assert settings.json()["status"] == "ready"

        content_read = client.post(f"/api/rebuild/sources/{source_id}/document-text", json={})
        overview = client.get("/api/rebuild/library/overview")

    assert content_read.status_code == 200
    body = content_read.json()
    assert body["status"] == "completed"
    assert body["content_read"] is True
    assert body["preview"] == "DOCX 正文读取已进入资料库。"

    overview_item = next(item for item in overview.json()["items"] if item["item_id"] == source_id)
    assert overview_item["content_read_status"] == "completed"
    assert overview_item["content_preview"] == "DOCX 正文读取已进入资料库。"
    assert overview_item["content_read_ref"] == f"crp://default/source-content-reads/content-read-{source_id}.json"


def test_rebuild_memory_candidate_review_and_layered_publication_are_mounted(tmp_path) -> None:
    candidate_id = _save_layer_candidate(tmp_path, target_layer="scenario")

    with _client(tmp_path) as client:
        review = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={
                "action": "promote_to_scenario",
                "reason": "用户确认场景候选。",
                    "series_id": "project-alpha",
            },
        )
        assert review.status_code == 200
        promoted_id = review.json()["promoted_object_id"]

        publication = client.post(
            f"/api/rebuild/staging-scenarios/{promoted_id}/publication",
            json={"confirm": True, "reason": "用户确认发布场景记忆。"},
        )

    assert publication.status_code == 200
    body = publication.json()
    assert body["status"] == "published"
    assert body["layer"] == "scenario"


def test_rebuild_generic_review_uses_sqlite_saga_only_with_complete_compound_authority(tmp_path) -> None:
    _activate_generic_memory_publication_sqlite_authority(tmp_path)
    candidate_id = _save_layer_candidate(tmp_path, target_layer="atom")

    with _client(tmp_path) as client:
        reviewed = client.post(f"/api/rebuild/memory-candidates/{candidate_id}/review", json={"action": "promote_to_atom", "reason": "用户确认Atom草稿。"})
        replay = client.post(f"/api/rebuild/memory-candidates/{candidate_id}/review", json={"action": "promote_to_atom", "reason": "用户确认Atom草稿。"})

    assert reviewed.status_code == replay.status_code == 200
    draft_id = reviewed.json()["promoted_object_id"]
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    assert records.read("staging_atoms", draft_id) is not None
    assert records.read("staging_memory_publication_contexts", f"atom~{draft_id}") is not None
    assert _store(tmp_path).read("staging_atoms", draft_id) is None
    assert records.list("memory_atoms") == records.list("memory_publications") == records.list("memory_transitions") == ()
    assert _store(tmp_path).revision("memory_candidates", candidate_id) == 2


def test_rebuild_generic_sqlite_publication_and_rollback_use_compound_authority(tmp_path) -> None:
    _activate_generic_memory_publication_sqlite_authority(tmp_path)
    candidate_id = _save_layer_candidate(tmp_path, target_layer="atom")

    before = datetime.now(timezone.utc)
    with _client(tmp_path) as client:
        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_atom", "reason": "用户确认Atom草稿。"},
        )
        draft_id = reviewed.json()["promoted_object_id"]
        published = client.post(
            f"/api/rebuild/staging-atoms/{draft_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布。"},
        )
        replay = client.post(
            f"/api/rebuild/staging-atoms/{draft_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布。"},
        )
        rolled_back = client.post(
            f"/api/rebuild/memory-publications/{published.json()['publication_id']}/rollback",
            json={"confirm": True, "reason": "用户确认撤回。"},
        )
    after = datetime.now(timezone.utc)

    assert reviewed.status_code == published.status_code == replay.status_code == rolled_back.status_code == 200
    assert replay.json()["publication_id"] == published.json()["publication_id"]
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    assert records.read("staging_atoms", draft_id) is None
    assert records.read("memory_atoms", draft_id) is None
    assert len(records.list("memory_atom_revisions")) == 2
    publication = records.read("memory_publications", published.json()["publication_id"]).payload
    assert publication["status"] == "rolled_back"
    assert before <= datetime.fromisoformat(publication["published_at"]) <= after
    assert before <= datetime.fromisoformat(publication["rolled_back_at"]) <= after
    assert _store(tmp_path).read("memory_atoms", draft_id) is None
    assert _store(tmp_path).read("memory_publications", published.json()["publication_id"]) is None


def test_rebuild_external_series_candidate_replaces_sqlite_series_after_second_confirmation(tmp_path) -> None:
    _activate_generic_memory_publication_sqlite_authority(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    object_id = "series-memory-sqlite-external"
    refs = [{"source_id": "source-series", "locator": "char:0-20"}]
    staged = {"id": object_id, "series_id": "series-sqlite-external", "overview": "before", "revision": 1, "source_refs": refs, "trust_status": "system_generated"}
    context = build_manual_publication_context(namespace_id="default", layer="series_memory", draft_id=object_id, candidate_id="candidate-initial-series", reviewed_at="2026-07-12T12:00:00+08:00", review_reason="初始Series发布。", source_refs=refs, evidence_refs=refs)
    with records.begin() as transaction:
        transaction.put("staging_series_memory", object_id, staged, expected_revision=0)
        transaction.put("staging_memory_publication_contexts", context["id"], context, expected_revision=0)
        transaction.commit()
    with SQLiteMemoryPublicationTrustAuditUnitOfWork(tmp_path / ".rebuild-data" / "structured-records.sqlite3").begin() as transaction:
        transaction.publish_user_confirmed(layer="series_memory", staged_id=object_id, published_at="2026-07-12T12:01:00+08:00")
        transaction.commit()
    proposal = {"proposal_id": "sqlite-series-proposal", "proposal_type": "series_update_proposal", "summary": "更新Series。", "source_refs": [{"locator": "source-series"}], "evidence_refs": [{"locator": "source-series"}], "suggested_changes": {"series_id": "series-sqlite-external", "structured": {**staged, "overview": "after", "revision": 2}}, "requires_user_review": True}
    with _client(tmp_path) as client:
        imported = client.post("/api/rebuild/external-agent/proposals", json={"project_id": "project-alpha", "proposal": proposal})
        applied = client.post(f"/api/rebuild/external-agent/review-drafts/{imported.json()['draft_ids'][0]}/apply", json={"confirm": True, "expected_revision": 1})
        candidate_id = applied.json()["memory_candidate_id"]
        reviewed = client.post(f"/api/rebuild/memory-candidates/{candidate_id}/review", json={"action": "promote_to_series_memory", "reason": "用户确认Series草稿。"})
        published = client.post(f"/api/rebuild/staging-series-memory/{object_id}/publication", json={"confirm": True, "reason": "用户二次确认替换。"})
    assert imported.status_code == applied.status_code == reviewed.status_code == published.status_code == 200
    assert _store(tmp_path).read("memory_candidates", candidate_id)["external_series_update"]["authority_identity"] == "sqlite:structured-records-v1"
    assert records.read("memory_series_memory", object_id).payload["overview"] == "after"
    assert len(records.list("memory_series_memory_revisions")) == 2
    assert len(records.list("memory_publications")) == 2


def test_rebuild_related_memory_reads_complete_sqlite_authority_without_json_fallback(tmp_path) -> None:
    _activate_generic_memory_publication_sqlite_authority(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    atom_a = {"id": "related-sqlite-a", "source_id": "source-related", "source_refs": [{"source_id": "source-related", "locator": "char:0-1"}]}
    atom_b = {"id": "related-sqlite-b", "source_id": "source-related", "source_refs": [{"source_id": "source-related", "locator": "char:2-3"}]}
    with records.begin() as transaction:
        transaction.put("memory_atoms", atom_a["id"], atom_a, expected_revision=0)
        transaction.put("memory_atoms", atom_b["id"], atom_b, expected_revision=0)
        transaction.commit()
    _store(tmp_path).write("memory_atoms", atom_a["id"], {**atom_a, "source_id": "stale-json"}, expected_revision=0)

    with _client(tmp_path) as client:
        response = client.get(f"/api/rebuild/library/related?object_id={atom_a['id']}&layer=atom")

    assert response.status_code == 200
    assert [item["object_id"] for item in response.json()["related"]] == [atom_b["id"]]
    assert records.read("memory_atoms", atom_a["id"]).payload == atom_a


def test_rebuild_project_brain_reads_complete_sqlite_current_without_json_fallback(tmp_path) -> None:
    _activate_generic_memory_publication_sqlite_authority(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    atom = {"id": "brain-sqlite-atom", "content": "SQLite current brain evidence", "source_refs": [], "updated_at": "2026-07-12T23:30:00+08:00"}
    scenario = {
        "id": "brain-sqlite-scenario",
        "project_id": "project-alpha",
        "atom_ids": [atom["id"]],
        "title": "Project Alpha",
        "summary": "Project Alpha memory",
        "source_refs": [],
    }
    with records.begin() as transaction:
        transaction.put("memory_atoms", atom["id"], atom, expected_revision=0)
        transaction.put("memory_scenarios", scenario["id"], scenario, expected_revision=0)
        transaction.commit()
    _store(tmp_path).write("memory_atoms", atom["id"], {**atom, "content": "Stale JSON brain evidence"}, expected_revision=0)

    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/project-brain?project_id=project-alpha")

    assert response.status_code == 200
    body = response.json()
    item = next(item for item in body["memories"] if item["memory_id"] == atom["id"])
    assert item["title"] == "SQLite current brain evidence"
    assert body["retrieval_status"]["project_id"] == "project-alpha"
    assert body["retrieval_status"]["status"] == "preparing"
    assert body["retrieval_status"]["source"] == {
        "kind": "published_project_memory",
        "label": "已发布项目记忆",
        "series_count": 0,
        "source_count": 0,
    }
    assert body["retrieval_status"]["safety"]["read_only"] is True
    assert _store(tmp_path).list("jobs") == ()


def test_memory_projection_settings_keep_reads_safe_and_legacy_refresh_is_retired(
    tmp_path,
) -> None:
    with _client(tmp_path) as client:
        settings = client.get("/api/rebuild/settings/memory-retrieval")
        diagnostics_before = client.get("/api/rebuild/developer-studio/memory-projection")
        retired = client.post(
            "/api/rebuild/developer-studio/memory-projection/refresh",
            json={"project_id": "default", "confirm": True},
        )
        diagnostics_after = client.get("/api/rebuild/developer-studio/memory-projection")

    assert settings.status_code == diagnostics_before.status_code == 200
    assert settings.json()["status"]["status"] == "preparing"
    assert settings.json()["reading_order"][0]["label"] == "项目总览"
    assert settings.json()["privacy"]["team_memory_included"] is False
    assert diagnostics_before.json()["actions"]["refresh_available"] is True
    assert retired.status_code == 410
    assert retired.json() == {
        "detail": "memory_projection_refresh_migrated_to_automation_grant",
        "replacement": "/api/rebuild/automations/memory-projection-rebuild/preview",
    }
    assert diagnostics_after.status_code == 200
    assert diagnostics_after.json()["public_status"]["status"] == "preparing"
    assert _store(tmp_path).list("jobs") == ()
    serialized = json.dumps(diagnostics_after.json()).lower()
    for forbidden in ('"source_id":', '"locator":', "private://", '"api_key":'):
        assert forbidden not in serialized


def test_memory_projection_legacy_refresh_never_admits_an_effect(tmp_path) -> None:
    with _client(tmp_path) as client:
        retired = client.post(
            "/api/rebuild/developer-studio/memory-projection/refresh",
            json={"project_id": "default", "confirm": True},
        )

    assert retired.status_code == 410
    assert _store(tmp_path).list("jobs") == ()


def test_memory_retrieval_plan_preview_is_stateless_and_does_not_echo_query(
    tmp_path,
) -> None:
    canary = "CANARY-PLAN-PREVIEW-DO-NOT-PERSIST-7291"
    store = _store(tmp_path)
    before_jobs = store.list("jobs")

    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/developer-studio/memory-retrieval/plan-preview",
            json={"query": f"请核对 {canary} 的原文和出处"},
        )
        missing = client.post(
            "/api/rebuild/developer-studio/memory-retrieval/plan-preview",
            json={},
        )
        wrong_type = client.post(
            "/api/rebuild/developer-studio/memory-retrieval/plan-preview",
            json={"query": 42},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "source_verification"
    assert payload["requires_source_body"] is True
    assert all(stage["initial"] is True for stage in payload["stages"])
    assert canary not in json.dumps(payload, ensure_ascii=False)
    assert store.list("jobs") == before_jobs
    persisted = b"".join(
        path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert canary.encode("utf-8") not in persisted
    assert missing.status_code == 400
    assert wrong_type.status_code == 400


def test_memory_retrieval_performance_is_read_only_bounded_and_content_free(
    tmp_path,
) -> None:
    canary = "CANARY-PERFORMANCE-PRIVATE-QUESTION-1842"
    store = _store(tmp_path)
    record = {
        "id": "private-question-id",
        "question": canary,
        "created_at": "2026-07-26T12:00:00+00:00",
        "recall_trace": {
            "trace_version": "progressive-direct-question-v2",
            "project_id": "default",
            "query_fingerprint": "a" * 64,
            "projection": {"reason_code": "projection_fresh"},
            "route": {"reason_code": "route_confident"},
            "fallback": {"used": False, "reason_code": None},
            "performance": {
                "total_ms": 12.0,
                "legacy_recall_ms": 5.0,
                "stages": [
                    {"stage": "r0_series_router", "status": "used", "elapsed_ms": 3.0, "hit_count": 2},
                    {"stage": "r1_series_digest", "status": "used", "elapsed_ms": 4.0, "hit_count": 1},
                    {"stage": "r2_structured_content", "status": "not_requested", "elapsed_ms": 0.0, "hit_count": 0},
                    {"stage": "r3_source_evidence", "status": "not_requested", "elapsed_ms": 0.0, "hit_count": 0},
                ],
            },
        },
    }
    store.write("workbench_direct_questions", record["id"], record, expected_revision=0)
    before = store.read("workbench_direct_questions", record["id"])
    before_jobs = store.list("jobs")

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/developer-studio/memory-retrieval/performance?project_id=default&limit=100"
        )
        invalid_limit = client.get(
            "/api/rebuild/developer-studio/memory-retrieval/performance?limit=501"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["window"]["timed_sample_count"] == 1
    assert payload["overall"]["average_ms"] == 12.0
    assert payload["stages"][0]["stage"] == "r0_series_router"
    assert payload["stages"][0]["attempt_count"] == 1
    assert canary not in json.dumps(payload, ensure_ascii=False)
    assert "private-question-id" not in json.dumps(payload, ensure_ascii=False)
    assert "query_fingerprint" not in json.dumps(payload, ensure_ascii=False)
    assert invalid_limit.status_code == 400
    assert store.read("workbench_direct_questions", record["id"]) == before
    assert store.list("jobs") == before_jobs


def test_rebuild_project_brain_drill_reads_complete_sqlite_current_without_json_fallback(tmp_path) -> None:
    _activate_generic_memory_publication_sqlite_authority(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    atom = {"id": "drill-sqlite-atom", "content": "SQLite drill evidence", "source_refs": []}
    with records.begin() as transaction:
        transaction.put("memory_atoms", atom["id"], atom, expected_revision=0)
        transaction.commit()
    _store(tmp_path).write("memory_atoms", atom["id"], {**atom, "content": "Stale JSON drill evidence"}, expected_revision=0)

    with _client(tmp_path) as client:
        response = client.get(f"/api/rebuild/project-brain/layer/L1/{atom['id']}")

    assert response.status_code == 200
    assert response.json()["current"]["title"] == "SQLite drill evidence"


def test_rebuild_external_series_preview_reads_complete_sqlite_authority_without_json_fallback(tmp_path) -> None:
    _activate_generic_memory_publication_sqlite_authority(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    object_id = "series-memory-preview-sqlite"
    sqlite_current = {
        "id": object_id,
        "series_id": "series-preview-sqlite",
        "overview": "SQLite authority current overview.",
        "revision": 1,
        "source_refs": [{"source_id": "source-preview", "locator": "char:0-20"}],
        "trust_status": "user_confirmed",
    }
    with records.begin() as transaction:
        transaction.put("memory_series_memory", object_id, sqlite_current, expected_revision=0)
        transaction.commit()
    _store(tmp_path).write(
        "memory_series_memory",
        object_id,
        {**sqlite_current, "overview": "Stale JSON overview.", "revision": 99},
        expected_revision=0,
    )
    proposal = {
        "proposal_id": "sqlite-preview-proposal",
        "proposal_type": "series_update_proposal",
        "summary": "预览 SQLite authority Series。",
        "source_refs": [{"locator": "source-preview"}],
        "evidence_refs": [{"locator": "source-preview"}],
        "suggested_changes": {
            "series_id": "series-preview-sqlite",
            "structured": {**sqlite_current, "overview": "Proposed overview.", "revision": 2},
        },
        "requires_user_review": True,
    }

    with _client(tmp_path) as client:
        imported = client.post("/api/rebuild/external-agent/proposals", json={"project_id": "project-alpha", "proposal": proposal})
        preview = client.get(f"/api/rebuild/external-agent/review-drafts/{imported.json()['draft_ids'][0]}/preview")

    assert imported.status_code == preview.status_code == 200
    body = preview.json()
    assert body["read_only"] is True
    assert body["preview"]["current_revision"] == 1
    assert body["preview"]["current_summary"] == "SQLite authority current overview."
    assert records.read("memory_series_memory", object_id).payload == sqlite_current
    assert _store(tmp_path).read("memory_series_memory", object_id)["revision"] == 99


def test_rebuild_external_series_preview_rejects_partial_memory_authority(tmp_path) -> None:
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    evidence = AggregateAuthorityEvidence("preview-partial-v1", "a" * 64, "b" * 64, TARGET_IDENTITY)
    initial = authority.create_json_active(namespace_id="default", aggregate="memory_atoms", reason="test initial")
    staged = authority.transition(
        namespace_id="default",
        aggregate="memory_atoms",
        expected_revision=initial.revision,
        to_state="sqlite_staged",
        evidence=evidence,
        reason="test staged",
    )
    authority.transition(
        namespace_id="default",
        aggregate="memory_atoms",
        expected_revision=staged.revision,
        to_state="sqlite_active",
        reason="test active",
    )
    draft_id = "draft-preview-partial-authority"
    _store(tmp_path).write(
        "external_agent_review_drafts",
        draft_id,
        {
            "id": draft_id,
            "draft_type": "series_update",
            "status": "pending_review",
            "target_id": "series-preview-partial",
            "suggested_changes": {
                "structured": {
                    "id": "series-preview-partial",
                    "series_id": "series-preview-partial",
                    "overview": "Proposed overview.",
                    "revision": 1,
                }
            },
        },
        expected_revision=0,
    )

    with _client(tmp_path) as client:
        preview = client.get(f"/api/rebuild/external-agent/review-drafts/{draft_id}/preview")

    assert preview.status_code == 409
    assert preview.json()["detail"] == "external agent review draft preview rejected"
    assert "partially SQLite active" in preview.json()["reason"]
    assert _store(tmp_path).read("external_agent_review_drafts", draft_id)["status"] == "pending_review"


def test_rebuild_project_skill_review_uses_sqlite_staging_saga_not_json_writer(tmp_path) -> None:
    candidate_id = _save_layer_candidate(tmp_path, target_layer="project_skill")

    with _client(tmp_path) as client:
        response = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_project_skill", "reason": "用户确认项目技能草案。"},
        )
        replay = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_project_skill", "reason": "用户确认项目技能草案。"},
        )

    store = _store(tmp_path)
    candidate = store.read("memory_candidates", candidate_id)
    assert response.status_code == replay.status_code == 200
    assert response.json()["promoted_layer"] == "project_skill"
    assert candidate is not None
    assert candidate["status"] == "promoted"
    assert store.revision("memory_candidates", candidate_id) == 2
    assert store.read("staging_project_skills", response.json()["promoted_object_id"]) is None
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    assert records.read("staging_project_skills", response.json()["promoted_object_id"]) is not None
    assert records.list("project_skills") == ()
    assert records.list("memory_publications") == ()
    assert records.list("memory_transitions") == ()


def test_rebuild_project_skill_sqlite_publish_replay_and_rollback_are_confirmed(tmp_path) -> None:
    _activate_project_skill_sqlite_authority(tmp_path)
    _activate_shared_trust_audit_compound(tmp_path)
    candidate_id = _save_layer_candidate(tmp_path, target_layer="project_skill")
    with _client(tmp_path) as client:
        reviewed = client.post(f"/api/rebuild/memory-candidates/{candidate_id}/review", json={"action": "promote_to_project_skill", "reason": "用户确认项目技能草案。"})
        draft_id = reviewed.json()["promoted_object_id"]
        published = client.post(f"/api/rebuild/staging-project-skills/{draft_id}/publication", json={"confirm": True, "reason": "用户二次确认发布。"})
        replay = client.post(f"/api/rebuild/staging-project-skills/{draft_id}/publication", json={"confirm": True, "reason": "用户二次确认发布。"})
        rolled_back = client.post(f"/api/rebuild/memory-publications/{published.json()['publication_id']}/rollback", json={"confirm": True, "reason": "用户确认撤回。", "expected_publication_revision": 1, "expected_project_skill_revision": 1})

    assert reviewed.status_code == published.status_code == rolled_back.status_code == 200
    assert replay.status_code == 409
    assert published.json()["project_skill_revision"] == 1
    assert rolled_back.json()["project_skill_revision"] == 2
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    assert records.read("project_skills", "skill-project-alpha").payload["status"] == "rolled_back"
    assert records.read("staging_project_skills", draft_id) is None


def test_project_skill_ai_draft_requires_two_confirmations_before_active_sqlite_write(tmp_path, monkeypatch) -> None:
    _activate_project_skill_sqlite_authority(tmp_path)
    _activate_shared_trust_audit_compound(tmp_path)
    _store(tmp_path).write("sources", "source-alpha", {
        "id": "source-alpha",
        "title": "Alpha 项目复盘",
        "metadata": {
            "project_id": "project-alpha",
            "summary": "已确认的项目复盘要求先给结论和证据。",
        },
    }, expected_revision=0)
    generated_payload = {
        "name": "Alpha AI 项目规则",
        "purpose": "复盘先给结论和证据，再列风险与下一步。",
        "output_rules": [{
            "rule": "先给结论与证据。",
            "priority": "must",
            "source_refs": [{"source_id": "source-alpha", "locator": "source:summary"}],
        }],
        "style_preferences": {"voice": "直接"},
        "update_rules": {"patch_strategy": "patch_existing_first", "allowed_auto_updates": []},
        "outline": [{"section_id": "summary", "title": "结论", "kind": "summary", "required": True}],
    }

    class Gateway:
        def invoke(self, _request):
            return ModelResult(generated_payload, "provider-deepseek", "deepseek-chat", {})

    monkeypatch.setattr(
        ai_runtime,
        "resolve_model_gateway_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(gateway=Gateway(), egress_consented=True),
    )
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    with _client(tmp_path) as client:
        generated = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json={
            "goal": "让复盘先给结论和证据。", "provider_call_confirmed": True,
        })
        assert generated.status_code == 200, generated.text
        assert records.list("project_skills") == ()
        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{generated.json()['candidate_id']}/review",
            json={"action": "promote_to_project_skill", "reason": "用户确认 AI 草稿进入待发布区。"},
        )
        assert records.list("project_skills") == ()
        published = client.post(
            f"/api/rebuild/staging-project-skills/{reviewed.json()['promoted_object_id']}/publication",
            json={"confirm": True, "reason": "用户二次确认发布 AI 项目规则。"},
        )

    assert generated.status_code == reviewed.status_code == published.status_code == 200
    active = records.read("project_skills", "skill-project-alpha").payload
    assert active["revision"] == 1
    assert active["name"] == "Alpha AI 项目规则"
    assert active["outline"][0]["section_id"] == "summary"
    assert active["output_rules"][0]["rule"] == "先给结论与证据。"
    assert active["output_rules"][0]["source_refs"]
    assert _store(tmp_path).read("project_skills", "skill-project-alpha") is None


def test_rebuild_project_skill_publication_requires_shared_trust_audit_compound_activation(tmp_path) -> None:
    _activate_project_skill_sqlite_authority(tmp_path)
    candidate_id = _save_layer_candidate(tmp_path, target_layer="project_skill")
    with _client(tmp_path) as client:
        reviewed = client.post(f"/api/rebuild/memory-candidates/{candidate_id}/review", json={"action": "promote_to_project_skill", "reason": "用户确认项目技能草案。"})
        publication = client.post(f"/api/rebuild/staging-project-skills/{reviewed.json()['promoted_object_id']}/publication", json={"confirm": True, "reason": "用户二次确认发布。"})

    assert reviewed.status_code == 200
    assert publication.status_code == 409
    assert publication.json()["reason"] == "shared Trust Audit compound activation is not ready"
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    assert records.list("project_skills") == ()
    assert records.list("memory_publications") == ()
    assert records.list("memory_transitions") == ()


def test_rebuild_external_agent_proposal_imports_pending_candidate_only(tmp_path) -> None:
    proposal = {
        "proposal_id": "proposal-alpha",
        "proposal_type": "memory_candidate_proposal",
        "summary": "外部 Agent 建议把灵感碎片沉淀为原子记忆。",
        "source_refs": [{"locator": "crp://default/sources/source-alpha.json"}],
        "evidence_refs": [{"locator": "crp://default/sources/source-alpha/content.md"}],
        "suggested_changes": {
            "target_layer": "atom",
            "candidate_type": "inspiration_atom",
            "proposed_content": "灵感系统应自动识别想法，并进入灵感系列参与后续输出。",
        },
        "requires_user_review": True,
    }

    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/external-agent/proposals",
            json={"project_id": "chriptmas-os", "proposal": proposal},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["proposal_id"] == "proposal-alpha"
    assert body["memory_candidate_id"]
    assert body["memory_publication_state"] == "not_published"
    assert "direct_long_term_memory_write" in body["blocked_operations"]

    store = _store(tmp_path)
    stored_proposal = store.read("external_agent_proposals", "proposal-alpha")
    assert stored_proposal is not None
    assert stored_proposal["status"] == "pending_review"
    assert stored_proposal["memory_publication"] == "not_started"
    candidate = store.read("memory_candidates", body["memory_candidate_id"])
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["target_layer"] == "atom"
    assert candidate["candidate_type"] == "other"
    assert candidate["provenance"]["external_agent_candidate_type"] == "inspiration_atom"
    assert candidate["review"]["auto_promote_allowed"] is False
    assert candidate["provenance"]["external_agent_proposal_id"] == "proposal-alpha"
    assert store.list("memory_atoms") == ()
    assert store.list("memory_publications") == ()

    overview_item = next(item for item in overview.json()["items"] if item["item_id"] == body["memory_candidate_id"])
    assert overview_item["item_type"] == "memory_candidate"
    assert overview_item["target_layer"] == "atom"


def test_rebuild_external_agent_proposal_rejects_direct_memory_publication(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/external-agent/proposals",
            json={
                "proposal": {
                    "proposal_id": "proposal-direct-write",
                    "proposal_type": "memory_candidate_proposal",
                    "summary": "非法直接写入长期记忆。",
                    "source_refs": [{"locator": "crp://default/sources/source-alpha.json"}],
                    "evidence_refs": [{"locator": "crp://default/sources/source-alpha/content.md"}],
                    "suggested_changes": {"proposed_content": "should not import"},
                    "requires_user_review": True,
                    "memory_publications": [{"id": "publication-alpha"}],
                }
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert body["reason"] == "forbidden proposal field: memory_publications"
    assert body["memory_publication_state"] == "not_published"
    assert "direct_long_term_memory_write" in body["blocked_operations"]
    store = _store(tmp_path)
    assert store.list("external_agent_proposals") == ()
    assert store.list("memory_candidates") == ()
    assert store.list("memory_publications") == ()


def test_rebuild_external_agent_project_skill_proposal_creates_review_draft_only(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/rebuild/external-agent/proposals",
            json={
                "project_id": "chriptmas-os",
                "proposal": {
                    "proposal_id": "skill-proposal",
                    "proposal_type": "project_skill_update_proposal",
                    "summary": "外部 Agent 建议更新项目 skill 默认阅读要求。",
                    "source_refs": [{"locator": "crp://default/exports/project_skills.json"}],
                    "evidence_refs": [{"locator": "crp://default/exports/memory_layers.json"}],
                    "suggested_changes": {
                        "project_skill_id": "skill-chriptmas-os",
                        "proposed_content": "回答前读取白盒导出、系列摘要、标签和待审 proposal。",
                    },
                    "requires_user_review": True,
                },
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["memory_candidate_id"] is None
    assert len(body["draft_ids"]) == 1
    assert body["memory_publication_state"] == "not_published"

    store = _store(tmp_path)
    proposal = store.read("external_agent_proposals", "skill-proposal")
    assert proposal is not None
    assert proposal["draft_ids"] == body["draft_ids"]
    draft = store.read("external_agent_review_drafts", body["draft_ids"][0])
    assert draft is not None
    assert draft["draft_type"] == "project_skill_update"
    assert draft["status"] == "pending_review"
    assert draft["target_id"] == "skill-chriptmas-os"
    assert draft["review"]["auto_apply_allowed"] is False
    assert draft["application"]["writes_long_term_memory"] is False
    assert store.list("staging_project_skills") == ()
    assert store.list("project_skills") == ()
    assert store.list("memory_publications") == ()


def test_rebuild_external_agent_review_drafts_are_readable_without_apply(tmp_path) -> None:
    proposal = {
        "proposal_id": "series-proposal",
        "proposal_type": "series_update_proposal",
        "summary": "外部 Agent 建议更新灵感系列。",
        "source_refs": [{"locator": "crp://default/exports/series_summaries.json"}],
        "evidence_refs": [{"locator": "crp://default/exports/tags.json"}],
        "suggested_changes": {
            "series_id": "inspiration_series",
            "proposed_content": "灵感系列应参与后续项目构思和复盘。",
        },
        "requires_user_review": True,
    }

    with _client(tmp_path) as client:
        imported = client.post(
            "/api/rebuild/external-agent/proposals",
            json={"project_id": "chriptmas-os", "proposal": proposal},
        )
        draft_id = imported.json()["draft_ids"][0]
        listing = client.get("/api/rebuild/external-agent/review-drafts?project_id=chriptmas-os")
        detail = client.get(f"/api/rebuild/external-agent/review-drafts/{draft_id}")
        missing = client.get("/api/rebuild/external-agent/review-drafts/missing-draft")

    assert imported.status_code == 200
    assert listing.status_code == 200
    list_body = listing.json()
    assert list_body["read_only"] is True
    assert list_body["memory_publication_state"] == "not_published"
    assert list_body["count"] == 1
    item = list_body["items"][0]
    assert item["id"] == draft_id
    assert item["draft_type"] == "series_update"
    assert item["target_id"] == "inspiration_series"
    assert item["read_only"] is True
    assert item["allowed_operations"] == ["review_later"]
    assert "direct_long_term_memory_write" in item["forbidden_operations"]
    assert item["review"]["auto_apply_allowed"] is False
    assert item["application"]["state"] == "not_applied"
    assert item["application"]["writes_long_term_memory"] is False

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["id"] == draft_id
    assert detail_body["proposal_id"] == "series-proposal"
    assert detail_body["source_refs"]
    assert "sk-" not in str(detail_body).lower()
    assert missing.status_code == 404
    assert missing.json()["read_only"] is True

    store = _store(tmp_path)
    assert store.list("staging_series_memory") == ()
    assert store.list("memory_series_memory") == ()
    assert store.list("memory_publications") == ()


def test_rebuild_external_agent_review_draft_preview_is_read_only_for_all_draft_types(tmp_path) -> None:
    store = _store(tmp_path)
    document = ObjectStoreDocumentRepository(store).create(
        DocumentDraft(
            title="灵感系统说明",
            document_type="qa_answer",
            markdown="# 灵感系统\n\n原始内容。",
            project_id="chriptmas-os",
            source_refs=({"source_id": "source-doc", "locator": "char:0-20"},),
        )
    )
    skill = {
        "schema_version": "1.0.0",
        "id": "skill-chriptmas-os",
        "project_id": "chriptmas-os",
        "name": "Chriptmas OS 项目 Skill",
        "purpose": "固定 Chriptmas OS 的默认阅读要求。",
        "required_context": [
            {
                "context_id": "ctx-inspiration-series",
                "kind": "series_memory",
                "object_id": "series-memory-inspiration",
                "uri": "crp://default/memory/series/series-memory-inspiration.json",
                "reason": "作为灵感系统的系列总览。",
                "stale": False,
            }
        ],
        "output_rules": [
            {
                "rule_id": "rule-whitebox-first",
                "origin": "user",
                "rule": "回答前先读取白盒导出和系列摘要。",
                "priority": "must",
                "source_refs": [{"source_id": "source-skill", "locator": "char:0-40"}],
                "locked_by_user": True,
            }
        ],
        "style_preferences": {"voice": "直接、具体", "format_defaults": ["Markdown"]},
        "update_rules": {
            "patch_strategy": "patch_existing_first",
            "user_edit_policy": "user_wins",
            "allowed_auto_updates": ["append_low_risk_context"],
        },
        "source_refs": [{"source_id": "source-skill", "locator": "char:0-40"}],
        "evidence_refs": [{"source_id": "source-skill", "locator": "char:0-40"}],
        "decision_log": [
            {
                "decision_id": "decision-skill-chriptmas-r1",
                "reason": "用户确认项目 Skill 初始结构。",
                "actor": "user",
                "created_at": "2026-07-03T05:30:00+08:00",
            }
        ],
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "status": "active",
        "trust_status": "user_confirmed",
    }
    saved_skill = ObjectStoreProjectSkillRepository(store).save(
        ProjectSkillUpdate(
            project_id="chriptmas-os",
            markdown="# Chriptmas OS 项目 Skill\n\n回答前读取白盒导出。",
            structured=skill,
            expected_revision=0,
            reason="initial project skill",
        )
    )
    updated_skill = copy.deepcopy(saved_skill)
    updated_skill["style_preferences"]["voice"] = "直接、具体、贴合用户表达"
    series = {
        "schema_version": "1.0.0",
        "id": "series-memory-inspiration",
        "series_id": "inspiration_series",
        "scope": "project",
        "overview": "灵感系列保存产品构思片段。",
        "scenario_ids": [],
        "source_refs": [{"source_id": "source-inspiration", "locator": "char:0-40"}],
        "project_ids": ["chriptmas-os"],
        "stale": False,
        "stale_reason": None,
        "revision": 1,
        "created_at": "2026-07-03T05:30:00+08:00",
        "updated_at": "2026-07-03T05:30:00+08:00",
        "trust_status": "user_confirmed",
    }
    store.write("memory_series_memory", "series-memory-inspiration", series, expected_revision=0)
    updated_series = copy.deepcopy(series)
    updated_series["overview"] = "灵感系列保存产品构思片段，并参与后续项目构思。"
    updated_series["revision"] = 2
    proposals = {
        "document": {
            "proposal_id": "preview-document-proposal",
            "proposal_type": "document_revision_proposal",
            "summary": "外部 Agent 建议补充文档。",
            "source_refs": [{"source_id": "source-doc", "locator": "char:0-20"}],
            "evidence_refs": [{"locator": "document_versions.json#doc"}],
            "suggested_changes": {
                "document_id": document["id"],
                "proposed_content": "# 灵感系统\n\n原始内容。\n\n## 灵感碰撞\n用于项目构思。",
            },
            "requires_user_review": True,
        },
        "project_skill": {
            "proposal_id": "preview-skill-proposal",
            "proposal_type": "project_skill_update_proposal",
            "summary": "外部 Agent 建议更新项目 skill。",
            "source_refs": [{"locator": "project_skills.json#chriptmas-os"}],
            "evidence_refs": [{"locator": "external_agent_tasks.json#rules"}],
            "suggested_changes": {
                "project_skill_id": "skill-chriptmas-os",
                "markdown": "# Chriptmas OS 项目 Skill\n\n回答前检查待审草稿。",
                "structured": updated_skill,
            },
            "requires_user_review": True,
        },
        "series": {
            "proposal_id": "preview-series-proposal",
            "proposal_type": "series_update_proposal",
            "summary": "外部 Agent 建议更新系列。",
            "source_refs": [{"locator": "series_summaries.json#inspiration"}],
            "evidence_refs": [{"locator": "tags.json#idea"}],
            "suggested_changes": {
                "series_id": "inspiration_series",
                "structured": updated_series,
            },
            "requires_user_review": True,
        },
    }

    previews = {}
    with _client(tmp_path) as client:
        for key, proposal in proposals.items():
            imported = client.post(
                "/api/rebuild/external-agent/proposals",
                json={"project_id": "chriptmas-os", "proposal": proposal},
            )
            draft_id = imported.json()["draft_ids"][0]
            preview = client.get(f"/api/rebuild/external-agent/review-drafts/{draft_id}/preview")
            previews[key] = preview

    assert previews["document"].status_code == 200
    assert previews["project_skill"].status_code == 200
    assert previews["series"].status_code == 200
    assert previews["document"].json()["preview"]["target_kind"] == "document"
    assert previews["document"].json()["preview"]["changed_fields"] == ["markdown"]
    assert previews["project_skill"].json()["preview"]["target_kind"] == "project_skill"
    assert "style_preferences" in previews["project_skill"].json()["preview"]["changed_fields"]
    assert previews["series"].json()["preview"]["target_kind"] == "series_memory"
    assert "overview" in previews["series"].json()["preview"]["changed_fields"]
    assert all(item.json()["read_only"] is True for item in previews.values())
    assert all("apply_without_user_confirmation" in item.json()["forbidden_operations"] for item in previews.values())
    assert ObjectStoreDocumentRepository(store).read(str(document["id"]))["revision"] == 1
    assert ObjectStoreProjectSkillRepository(store).load("chriptmas-os")["revision"] == 1
    assert store.read("memory_series_memory", "series-memory-inspiration")["revision"] == 1
    assert {item["status"] for item in store.list("external_agent_review_drafts")} == {"pending_review"}
    assert store.list("memory_publications") == ()


def test_rebuild_library_overview_includes_external_agent_review_drafts(tmp_path) -> None:
    with _client(tmp_path) as client:
        imported = client.post(
            "/api/rebuild/external-agent/proposals",
            json={
                "project_id": "chriptmas-os",
                "proposal": {
                    "proposal_id": "overview-series-proposal",
                    "proposal_type": "series_update_proposal",
                    "summary": "外部 Agent 建议把灵感系列接入项目构思。",
                    "source_refs": [{"locator": "crp://default/exports/series_summaries.json"}],
                    "evidence_refs": [{"locator": "crp://default/exports/tags.json"}],
                    "suggested_changes": {
                        "series_id": "inspiration_series",
                        "proposed_content": "灵感系列应参与后续项目构思和回答生成。",
                    },
                    "requires_user_review": True,
                },
            },
        )
        overview = client.get("/api/rebuild/library/overview?project_id=chriptmas-os")

    assert imported.status_code == 200
    assert overview.status_code == 200
    body = overview.json()
    draft_id = imported.json()["draft_ids"][0]
    item = next(item for item in body["items"] if item["item_id"] == draft_id)
    assert item["item_type"] == "external_agent_review_draft"
    assert item["status"] == "pending_review"
    assert item["external_agent_proposal_id"] == "overview-series-proposal"
    assert item["external_agent_draft_type"] == "series_update"
    assert item["external_agent_target_id"] == "inspiration_series"
    assert item["external_agent_review_state"] == "pending_review"
    assert item["external_agent_application_state"] == "not_applied"
    assert item["external_agent_memory_publication_state"] == "not_published"
    assert item["external_agent_writes_long_term_memory"] is False
    assert item["external_agent_writes_staging_memory"] is False
    assert "automatic_memory_publication" in item["blocked_operations"]
    assert body["counts"]["external_agent_review_draft"] == 1

    store = _store(tmp_path)
    assert store.list("staging_series_memory") == ()
    assert store.list("memory_series_memory") == ()
    assert store.list("memory_publications") == ()


def test_rebuild_external_agent_document_revision_draft_applies_with_user_confirmation(tmp_path) -> None:
    store = _store(tmp_path)
    document = ObjectStoreDocumentRepository(store).create(
        DocumentDraft(
            title="灵感系统说明",
            document_type="qa_answer",
            markdown="# 灵感系统\n\n原始内容。",
            project_id="chriptmas-os",
            source_refs=({"source_id": "source-doc", "locator": "char:0-20"},),
        )
    )
    document_id = document["id"]
    proposal = {
        "proposal_id": "document-proposal",
        "proposal_type": "document_revision_proposal",
        "summary": "外部 Agent 建议补充灵感碰撞说明。",
        "source_refs": [{"source_id": "source-doc", "locator": "char:0-20"}],
        "evidence_refs": [{"locator": "document_versions.json#doc"}],
        "suggested_changes": {
            "document_id": document_id,
            "proposed_content": "# 灵感系统\n\n原始内容。\n\n## 灵感碰撞\n用于项目构思和回答生成。",
        },
        "requires_user_review": True,
    }

    with _client(tmp_path) as client:
        imported = client.post(
            "/api/rebuild/external-agent/proposals",
            json={"project_id": "chriptmas-os", "proposal": proposal},
        )
        draft_id = imported.json()["draft_ids"][0]
        rejected_without_confirm = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"expected_revision": 1},
        )
        applied = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        stale = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert rejected_without_confirm.status_code == 400
    assert applied.status_code == 200
    body = applied.json()
    assert body["status"] == "applied"
    assert body["document_id"] == document_id
    assert body["document_revision"] == 2
    assert body["memory_publication_state"] == "not_published"
    assert body["staging_memory_written"] is False
    assert body["long_term_memory_written"] is False
    assert stale.status_code == 409

    documents = ObjectStoreDocumentRepository(store)
    updated = documents.read(document_id)
    assert updated is not None
    assert updated["revision"] == 2
    assert "## 灵感碰撞" in (documents.markdown(document_id) or "")
    draft = store.read("external_agent_review_drafts", draft_id)
    assert draft is not None
    assert draft["status"] == "applied"
    assert draft["review"]["reviewed_by"] == "user"
    assert draft["application"]["applied_document_id"] == document_id
    assert draft["application"]["applied_document_revision"] == 2
    assert draft["application"]["writes_long_term_memory"] is False
    assert store.list("memory_publications") == ()
    assert store.list("staging_atoms") == ()


def test_rebuild_external_agent_series_draft_applies_with_user_confirmation(tmp_path) -> None:
    _activate_generic_memory_publication_sqlite_authority(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    initial_series = {
        "schema_version": "1.0.0",
        "id": "series-memory-inspiration",
        "series_id": "inspiration_series",
        "scope": "project",
        "overview": "灵感系列保存产品构思片段。",
        "scenario_ids": [],
        "source_refs": [{"source_id": "source-inspiration", "locator": "char:0-40"}],
        "project_ids": ["chriptmas-os"],
        "stale": False,
        "stale_reason": None,
        "revision": 1,
        "created_at": "2026-07-03T04:00:00+08:00",
        "updated_at": "2026-07-03T04:00:00+08:00",
        "trust_status": "user_confirmed",
    }
    initial_refs = [{"source_id": "source-inspiration", "locator": "char:0-40"}]
    initial_context = build_manual_publication_context(
        namespace_id="default",
        layer="series_memory",
        draft_id="series-memory-inspiration",
        candidate_id="candidate-initial-series",
        reviewed_at="2026-07-03T04:00:00+08:00",
        review_reason="初始灵感系列发布。",
        source_refs=initial_refs,
        evidence_refs=initial_refs,
    )
    with records.begin() as transaction:
        transaction.put("staging_series_memory", "series-memory-inspiration", initial_series, expected_revision=0)
        transaction.put("staging_memory_publication_contexts", initial_context["id"], initial_context, expected_revision=0)
        transaction.commit()
    with SQLiteMemoryPublicationTrustAuditUnitOfWork(tmp_path / ".rebuild-data" / "structured-records.sqlite3").begin() as transaction:
        transaction.publish_user_confirmed(layer="series_memory", staged_id="series-memory-inspiration", published_at="2026-07-03T04:00:00+08:00")
        transaction.commit()
    updated_series = copy.deepcopy(initial_series)
    updated_series["overview"] = "灵感系列保存产品构思片段，并参与后续项目构思和回答生成。"
    updated_series["revision"] = 2
    updated_series["updated_at"] = "2026-07-03T04:20:00+08:00"
    proposal = {
        "proposal_id": "series-apply-proposal",
        "proposal_type": "series_update_proposal",
        "summary": "外部 Agent 建议补充灵感系列用途。",
        "source_refs": [{"locator": "crp://default/exports/series_summaries.json"}],
        "evidence_refs": [{"locator": "crp://default/exports/tags.json"}],
        "suggested_changes": {
            "series_id": "inspiration_series",
            "structured": updated_series,
        },
        "requires_user_review": True,
    }

    with _client(tmp_path) as client:
        imported = client.post(
            "/api/rebuild/external-agent/proposals",
            json={"project_id": "chriptmas-os", "proposal": proposal},
        )
        draft_id = imported.json()["draft_ids"][0]
        applied = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        candidate_id = applied.json()["memory_candidate_id"]
        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_series_memory", "reason": "用户确认外部 Series 候选进入 staging。"},
        )
        published = client.post(
            "/api/rebuild/staging-series-memory/series-memory-inspiration/publication",
            json={"confirm": True, "reason": "用户二次确认发布 Series 更新。"},
        )
        stale = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert imported.status_code == 200
    assert applied.status_code == 200
    body = applied.json()
    assert body["status"] == "candidate_created"
    assert body["draft_type"] == "series_update"
    assert body["series_id"] == "inspiration_series"
    assert body["series_memory_id"] == "series-memory-inspiration"
    assert body["series_memory_revision"] == 2
    assert body["memory_candidate_id"] == candidate_id
    assert body["memory_publication_state"] == "not_published"
    assert body["staging_memory_written"] is False
    assert body["long_term_memory_written"] is False
    assert body["long_term_memory_write_reason"] == "external_series_update_requires_candidate_review"
    assert reviewed.status_code == 200
    assert reviewed.json()["memory_publication_state"] == "staging_series_memory_created_not_published"
    assert published.status_code == 200
    assert published.json()["memory_publication_state"] == "published_with_rollback_ref"
    assert stale.status_code == 409

    series = records.read("memory_series_memory", "series-memory-inspiration").payload
    assert series["revision"] == 2
    assert "参与后续项目构思" in series["overview"]
    store = _store(tmp_path)
    draft = store.read("external_agent_review_drafts", draft_id)
    assert draft is not None
    assert draft["status"] == "candidate_created"
    assert draft["review"]["reviewed_by"] == "user"
    assert draft["application"]["memory_candidate_id"] == candidate_id
    assert draft["application"]["writes_long_term_memory"] is False
    assert draft["application"]["writes_staging_memory"] is False
    assert records.list("staging_series_memory") == ()
    assert len(records.list("memory_publications")) == 2


def test_rebuild_external_agent_project_skill_draft_applies_with_user_confirmation(tmp_path) -> None:
    store = _store(tmp_path)
    initial_skill = {
        "schema_version": "1.0.0",
        "id": "skill-chriptmas-os",
        "project_id": "chriptmas-os",
        "name": "Chriptmas OS 项目 Skill",
        "purpose": "固定 Chriptmas OS 的默认阅读要求和输出结构。",
        "required_context": [
            {
                "context_id": "ctx-inspiration-series",
                "kind": "series_memory",
                "object_id": "series-inspiration",
                "uri": "crp://default/memory/series/series-inspiration.json",
                "reason": "作为灵感系统的系列总览。",
                "stale": False,
            }
        ],
        "output_rules": [
            {
                "rule_id": "rule-whitebox-first",
                "origin": "user",
                "rule": "回答前先读取白盒导出和系列摘要。",
                "priority": "must",
                "source_refs": [{"source_id": "source-skill", "locator": "char:0-40"}],
                "locked_by_user": True,
            }
        ],
        "style_preferences": {"voice": "直接、具体", "format_defaults": ["Markdown"]},
        "update_rules": {
            "patch_strategy": "patch_existing_first",
            "user_edit_policy": "user_wins",
            "allowed_auto_updates": ["append_low_risk_context"],
        },
        "source_refs": [{"source_id": "source-skill", "locator": "char:0-40"}],
        "evidence_refs": [{"source_id": "source-skill", "locator": "char:0-40"}],
        "decision_log": [
            {
                "decision_id": "decision-skill-chriptmas-r1",
                "reason": "用户确认项目 Skill 初始结构。",
                "actor": "user",
                "created_at": "2026-07-03T04:00:00+08:00",
            }
        ],
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "status": "active",
        "trust_status": "user_confirmed",
    }
    skills = ObjectStoreProjectSkillRepository(store)
    saved = skills.save(
        ProjectSkillUpdate(
            project_id="chriptmas-os",
            markdown="# Chriptmas OS 项目 Skill\n\n回答前读取白盒导出。",
            structured=initial_skill,
            expected_revision=0,
            reason="initial project skill",
        )
    )
    updated_skill = copy.deepcopy(saved)
    updated_skill["style_preferences"]["voice"] = "直接、具体、贴合用户表达"
    updated_skill["output_rules"].append(
        {
            "rule_id": "rule-review-drafts",
            "origin": "ai",
            "rule": "跨 Agent 回灌草稿必须进入本地 review 后再应用。",
            "priority": "must",
            "source_refs": [{"source_id": "source-skill", "locator": "char:40-90"}],
            "locked_by_user": False,
        }
    )
    proposal = {
        "proposal_id": "skill-apply-proposal",
        "proposal_type": "project_skill_update_proposal",
        "summary": "外部 Agent 建议补充跨 Agent 回灌 review 规则。",
        "source_refs": [{"locator": "crp://default/exports/project_skills.json"}],
        "evidence_refs": [{"locator": "crp://default/exports/external_agent_tasks.json"}],
        "suggested_changes": {
            "project_skill_id": "skill-chriptmas-os",
            "markdown": "# Chriptmas OS 项目 Skill\n\n回答前读取白盒导出，并检查待审草稿。",
            "structured": updated_skill,
        },
        "requires_user_review": True,
    }

    with _client(tmp_path) as client:
        imported = client.post(
            "/api/rebuild/external-agent/proposals",
            json={"project_id": "chriptmas-os", "proposal": proposal},
        )
        draft_id = imported.json()["draft_ids"][0]
        applied = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        stale = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert imported.status_code == 200
    assert applied.status_code == 200
    body = applied.json()
    assert body["status"] == "applied"
    assert body["draft_type"] == "project_skill_update"
    assert body["project_id"] == "chriptmas-os"
    assert body["project_skill_id"] == "skill-chriptmas-os"
    assert body["project_skill_revision"] == 2
    assert body["memory_publication_state"] == "not_published"
    assert body["staging_memory_written"] is False
    assert body["long_term_memory_written"] is False
    assert stale.status_code == 409

    reloaded = ObjectStoreProjectSkillRepository(store)
    skill = reloaded.load("chriptmas-os")
    assert skill is not None
    assert skill["revision"] == 2
    assert skill["style_preferences"]["voice"] == "直接、具体、贴合用户表达"
    assert "检查待审草稿" in (reloaded.markdown("chriptmas-os") or "")
    draft = store.read("external_agent_review_drafts", draft_id)
    assert draft is not None
    assert draft["status"] == "applied"
    assert draft["review"]["reviewed_by"] == "user"
    assert draft["application"]["applied_project_skill_id"] == "skill-chriptmas-os"
    assert draft["application"]["applied_project_skill_revision"] == 2
    assert draft["application"]["writes_long_term_memory"] is False
    assert draft["application"]["writes_staging_memory"] is False
    assert store.list("staging_project_skills") == ()
    assert store.list("memory_publications") == ()


def test_rebuild_external_agent_apply_rejects_unstructured_series_draft(tmp_path) -> None:
    with _client(tmp_path) as client:
        imported = client.post(
            "/api/rebuild/external-agent/proposals",
            json={
                "project_id": "chriptmas-os",
                "proposal": {
                    "proposal_id": "series-proposal",
                    "proposal_type": "series_update_proposal",
                    "summary": "外部 Agent 建议更新系列。",
                    "source_refs": [{"locator": "series_summaries.json#inspiration"}],
                    "evidence_refs": [{"locator": "tags.json#idea"}],
                    "suggested_changes": {
                        "series_id": "inspiration_series",
                        "proposed_content": "灵感系列参与项目构思。",
                    },
                    "requires_user_review": True,
                },
            },
        )
        draft_id = imported.json()["draft_ids"][0]
        applied = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert applied.status_code == 400
    body = applied.json()
    assert body["reason"] == "series_update draft requires structured series memory JSON"
    assert body["memory_publication_state"] == "not_published"
    store = _store(tmp_path)
    assert store.list("staging_series_memory") == ()
    assert store.list("memory_series_memory") == ()
    assert store.list("memory_publications") == ()


def test_rebuild_four_layer_provider_endpoint_imports_pending_candidates(
    tmp_path,
    monkeypatch,
) -> None:
    source_id = _save_source_with_content_read(tmp_path)
    monkeypatch.setattr(
        rebuild_routes,
        "_build_deepseek_provider_for_rebuild",
        lambda container: _FakeFourLayerProvider(),
    )
    _install_memory_candidate_turn_stub(monkeypatch, tmp_path, _FakeFourLayerProvider())

    with _client(tmp_path) as client:
        response = client.post(
            f"/api/rebuild/sources/{source_id}/four-layer-candidates",
            json={"project_id": "project-alpha", "evidence_kind": "source_content_read"},
        )

    assert response.status_code == 200
    body = response.json()
    candidate_id = body["import_result"]["candidate_ids"][0]
    candidate = ObjectStoreMemoryCandidateRepository(_store(tmp_path)).get(candidate_id)
    assert body["status"] == "provider_candidates_imported"
    assert body["provider_name"] == "deepseek-test"
    assert candidate is not None
    assert candidate["target_layer"] == "scenario"
    assert candidate["status"] == "pending_review"
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_scenarios").exists()
    assert "sk-" not in str(body).lower()


def test_rebuild_four_layer_provider_endpoint_imports_from_docx_content_read(
    tmp_path,
    monkeypatch,
) -> None:
    docx_path = tmp_path / "product-design-memory-workbench.docx"
    _write_minimal_docx(docx_path, "产品设计文档正文已读取，可生成四层记忆候选。")
    extractor_code = (
        "import sys, zipfile, xml.etree.ElementTree as ET; "
        "data=zipfile.ZipFile(sys.argv[1]).read('word/document.xml'); "
        "root=ET.fromstring(data); "
        "ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'; "
        "print('\\n'.join(t.text or '' for t in root.iter(ns+'t')))"
    )
    monkeypatch.setattr(
        rebuild_routes,
        "_build_deepseek_provider_for_rebuild",
        lambda container: _FakeFourLayerProvider(),
    )
    _install_memory_candidate_turn_stub(monkeypatch, tmp_path, _FakeFourLayerProvider())

    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/file-source-intake",
            json={
                "title": "产品设计信息提取文档",
                "display_name": "product-design-memory-workbench.docx",
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": docx_path.stat().st_size,
                "file_reference": "platform-ref-product-design-memory-workbench",
            },
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]

        authorization = client.post(
            f"/api/rebuild/sources/{source_id}/file-authorization",
            json={"file_path": str(docx_path)},
        )
        assert authorization.status_code == 200

        settings = client.put(
            "/api/rebuild/settings/local-document-text-extractor",
            json={
                "enabled": True,
                "provider_name": "stdlib-docx-smoke",
                "command": [sys.executable, "-c", extractor_code, "{document_path}"],
                "confirm_enable": True,
            },
        )
        assert settings.status_code == 200

        content_read = client.post(f"/api/rebuild/sources/{source_id}/document-text", json={})
        response = client.post(
            f"/api/rebuild/sources/{source_id}/four-layer-candidates",
            json={"project_id": "project-alpha", "evidence_kind": "source_content_read"},
        )

    assert content_read.status_code == 200
    assert content_read.json()["status"] == "completed"
    assert response.status_code == 200
    body = response.json()
    candidate_id = body["import_result"]["candidate_ids"][0]
    candidate = ObjectStoreMemoryCandidateRepository(_store(tmp_path)).get(candidate_id)
    assert body["status"] == "provider_candidates_imported"
    assert body["provider_name"] == "deepseek-test"
    assert body["evidence_kind"] == "source_content_read"
    assert candidate is not None
    assert candidate["target_layer"] == "scenario"
    assert candidate["status"] == "pending_review"
    assert candidate["provenance"]["source_content_read_id"] == f"content-read-{source_id}"
    assert "产品设计文档" not in str(body)
    assert "sk-" not in str(body).lower()


def test_rebuild_docx_four_layer_candidate_review_publication_and_rollback(
    tmp_path,
    monkeypatch,
) -> None:
    docx_path = tmp_path / "product-design-memory-lifecycle.docx"
    _write_minimal_docx(docx_path, "产品设计文档正文已读取，进入四层记忆审核发布流程。")
    extractor_code = (
        "import sys, zipfile, xml.etree.ElementTree as ET; "
        "data=zipfile.ZipFile(sys.argv[1]).read('word/document.xml'); "
        "root=ET.fromstring(data); "
        "ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'; "
        "print('\\n'.join(t.text or '' for t in root.iter(ns+'t')))"
    )
    monkeypatch.setattr(
        rebuild_routes,
        "_build_deepseek_provider_for_rebuild",
        lambda container: _FakeFourLayerProvider(),
    )
    _install_memory_candidate_turn_stub(monkeypatch, tmp_path, _FakeFourLayerProvider())

    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/file-source-intake",
            json={
                "title": "产品设计信息提取文档",
                "display_name": "product-design-memory-lifecycle.docx",
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": docx_path.stat().st_size,
                "file_reference": "platform-ref-product-design-memory-lifecycle",
            },
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]
        assert client.post(
            f"/api/rebuild/sources/{source_id}/file-authorization",
            json={"file_path": str(docx_path)},
        ).status_code == 200
        assert client.put(
            "/api/rebuild/settings/local-document-text-extractor",
            json={
                "enabled": True,
                "provider_name": "stdlib-docx-smoke",
                "command": [sys.executable, "-c", extractor_code, "{document_path}"],
                "confirm_enable": True,
            },
        ).status_code == 200
        assert client.post(f"/api/rebuild/sources/{source_id}/document-text", json={}).status_code == 200
        candidates = client.post(
            f"/api/rebuild/sources/{source_id}/four-layer-candidates",
            json={"project_id": "project-alpha", "evidence_kind": "source_content_read"},
        )
        assert candidates.status_code == 200
        candidate_id = candidates.json()["import_result"]["candidate_ids"][0]

        review = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={
                "action": "promote_to_scenario",
                "reason": "确认产品设计文档场景候选。",
                "series_id": "project-alpha",
            },
        )
        assert review.status_code == 200
        promoted_id = review.json()["promoted_object_id"]
        publication = client.post(
            f"/api/rebuild/staging-scenarios/{promoted_id}/publication",
            json={"confirm": True, "reason": "确认发布产品设计文档场景记忆。"},
        )
        assert publication.status_code == 200
        publication_id = publication.json()["publication_id"]
        rollback = client.post(
            f"/api/rebuild/memory-publications/{publication_id}/rollback",
            json={"confirm": True, "reason": "验收后撤回测试场景记忆。"},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert review.json()["memory_publication_state"] == "staging_scenario_created_not_published"
    assert publication.json()["status"] == "published"
    assert publication.json()["layer"] == "scenario"
    assert publication.json()["memory_publication_state"] == "published_with_rollback_ref"
    assert rollback.status_code == 200
    assert rollback.json()["status"] == "rolled_back"
    assert rollback.json()["memory_publication_state"] == "rolled_back_not_published"
    assert overview.status_code == 200
    item_ids = {item["item_id"] for item in overview.json()["items"]}
    assert source_id in item_ids
    assert candidate_id in item_ids
    assert "sk-" not in str(publication.json()).lower()
    assert "sk-" not in str(rollback.json()).lower()


def test_rebuild_docx_l0_to_l3_manual_publication_and_l3_only_rollback(
    tmp_path,
    monkeypatch,
) -> None:
    docx_path = tmp_path / "l0-l3-manual-memory-lifecycle.docx"
    _write_minimal_docx(docx_path, "同一来源形成事实、场景和项目总览，并保留逐层引用与回滚证据。")
    extractor_code = (
        "import sys, zipfile, xml.etree.ElementTree as ET; "
        "data=zipfile.ZipFile(sys.argv[1]).read('word/document.xml'); "
        "root=ET.fromstring(data); "
        "ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'; "
        "print('\\n'.join(t.text or '' for t in root.iter(ns+'t')))"
    )
    monkeypatch.setattr(
        rebuild_routes,
        "_build_deepseek_provider_for_rebuild",
        lambda container: _FakeL0ToL3Provider(),
    )
    _install_memory_candidate_turn_stub(monkeypatch, tmp_path, _FakeL0ToL3Provider())

    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/file-source-intake",
            json={
                "title": "L0 到 L3 人工记忆生命周期",
                "display_name": docx_path.name,
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": docx_path.stat().st_size,
                "file_reference": "platform-ref-l0-l3-manual-memory-lifecycle",
            },
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]
        assert client.post(
            f"/api/rebuild/sources/{source_id}/file-authorization",
            json={"file_path": str(docx_path)},
        ).status_code == 200
        assert client.put(
            "/api/rebuild/settings/local-document-text-extractor",
            json={
                "enabled": True,
                "provider_name": "stdlib-docx-l0-l3",
                "command": [sys.executable, "-c", extractor_code, "{document_path}"],
                "confirm_enable": True,
            },
        ).status_code == 200
        content_read = client.post(f"/api/rebuild/sources/{source_id}/document-text", json={})
        assert content_read.status_code == 200

        imported = client.post(
            f"/api/rebuild/sources/{source_id}/four-layer-candidates",
            json={"project_id": "project-l0-l3", "evidence_kind": "source_content_read"},
        )
        assert imported.status_code == 200
        candidate_ids = imported.json()["import_result"]["candidate_ids"]
        store = _store(tmp_path)
        candidates = [ObjectStoreMemoryCandidateRepository(store).get(candidate_id) for candidate_id in candidate_ids]
        candidates_by_layer = {candidate["target_layer"]: candidate for candidate in candidates if candidate is not None}

        assert set(candidates_by_layer) == {"atom", "scenario", "series_memory"}
        assert all(candidate["status"] == "pending_review" for candidate in candidates_by_layer.values())
        assert store.list("memory_atoms") == ()
        assert store.list("memory_scenarios") == ()
        assert store.list("memory_series_memory") == ()

        atom_review = client.post(
            f"/api/rebuild/memory-candidates/{candidates_by_layer['atom']['id']}/review",
            json={"action": "promote_to_atom", "reason": "确认来源事实。"},
        )
        assert atom_review.status_code == 200
        atom_id = atom_review.json()["promoted_object_id"]
        atom_publication = client.post(
            f"/api/rebuild/staging-atoms/{atom_id}/publication",
            json={"confirm": True, "reason": "发布已确认事实。"},
        )
        assert atom_publication.status_code == 200

        scenario_review = client.post(
            f"/api/rebuild/memory-candidates/{candidates_by_layer['scenario']['id']}/review",
            json={
                "action": "promote_to_scenario",
                "reason": "确认由事实组成的场景。",
                    "series_id": "project-l0-l3",
                "atom_ids": [atom_id],
            },
        )
        assert scenario_review.status_code == 200
        scenario_id = scenario_review.json()["promoted_object_id"]
        scenario_publication = client.post(
            f"/api/rebuild/staging-scenarios/{scenario_id}/publication",
            json={"confirm": True, "reason": "发布已确认场景。"},
        )
        assert scenario_publication.status_code == 200

        series_review = client.post(
            f"/api/rebuild/memory-candidates/{candidates_by_layer['series_memory']['id']}/review",
            json={
                "action": "promote_to_series_memory",
                "reason": "确认由场景组成的项目总览。",
                "series_id": "project-l0-l3",
                "scenario_ids": [scenario_id],
            },
        )
        assert series_review.status_code == 200
        series_id = series_review.json()["promoted_object_id"]
        series_publication = client.post(
            f"/api/rebuild/staging-series-memory/{series_id}/publication",
            json={"confirm": True, "reason": "发布已确认项目总览。"},
        )
        assert series_publication.status_code == 200
        alternative_candidate = json.loads(
            json.dumps(candidates_by_layer["series_memory"], ensure_ascii=False)
        )
        alternative_candidate["id"] = "memory-candidate-series-alternative"
        alternative_candidate["status"] = "pending_review"
        alternative_candidate["review"] = {
            **alternative_candidate["review"],
            "reviewed_by": None,
            "reviewed_at": None,
        }
        ObjectStoreMemoryCandidateRepository(store).save(alternative_candidate)
        alternative_review = client.post(
            "/api/rebuild/memory-candidates/memory-candidate-series-alternative/review",
            json={
                "action": "promote_to_series_memory",
                "reason": "建立可解释归类建议的同项目备选系列。",
                "series_id": "series-alternative",
                "scenario_ids": [],
            },
        )
        assert alternative_review.status_code == 200, alternative_review.text
        alternative_series_id = alternative_review.json()["promoted_object_id"]
        alternative_publication = client.post(
            f"/api/rebuild/staging-series-memory/{alternative_series_id}/publication",
            json={"confirm": True, "reason": "发布同项目备选系列。"},
        )
        assert alternative_publication.status_code == 200

        records = SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
        )
        atom_record = records.read("memory_atoms", atom_id)
        scenario_record = records.read("memory_scenarios", scenario_id)
        series_record = records.read("memory_series_memory", series_id)
        atom = atom_record.payload if atom_record is not None else None
        scenario = scenario_record.payload if scenario_record is not None else None
        series = series_record.payload if series_record is not None else None
        assert atom is not None and scenario is not None and series is not None
        assert scenario["atom_ids"] == [atom_id]
        assert series["scenario_ids"] == [scenario_id]
        assert all(item["source_refs"][0]["source_id"] == source_id for item in (atom, scenario, series))

        hierarchy_options = client.get(
            "/api/rebuild/projects/project-l0-l3/memory-hierarchy-options"
        )
        assert hierarchy_options.status_code == 200
        structured_before_suggestion = records.generation_token(
            (
                "memory_atoms",
                "memory_scenarios",
                "memory_series_memory",
                "memory_publications",
                "memory_transitions",
            )
        )
        candidates_before_suggestion = len(
            store.list("memory_candidates")
        )
        suggestion_preview = client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-series-suggestions?scenario_id={scenario_id}"
            )
        )
        assert suggestion_preview.status_code == 200, suggestion_preview.text
        assert suggestion_preview.json()["network_called"] is False
        assert suggestion_preview.json()["writes_performed"] is False
        assert suggestion_preview.json()["content_included"] is False
        assert suggestion_preview.json()["suggestions"][0]["scenario_id"] == scenario_id
        assert suggestion_preview.json()["suggestions"][0]["current_atom_ids"] == [atom_id]
        assert suggestion_preview.json()["suggestions"][0]["candidates"][0][
            "series_id"
        ] == "series-alternative"
        assert suggestion_preview.json()["unresolved"] == []
        assert records.generation_token(
            (
                "memory_atoms",
                "memory_scenarios",
                "memory_series_memory",
                "memory_publications",
                "memory_transitions",
            )
        ) == structured_before_suggestion
        assert len(store.list("memory_candidates")) == candidates_before_suggestion
        initial_freshness = client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-series-freshness?series_object_id={series_id}"
            )
        )
        assert initial_freshness.status_code == 200
        assert initial_freshness.json()["needs_refresh_count"] == 0
        assert initial_freshness.json()["writes_performed"] is False
        assert initial_freshness.json()["network_called"] is False
        scenario_option = next(
            value
            for value in hierarchy_options.json()["scenarios"]
            if value["object_id"] == scenario_id
        )
        series_option = next(
            value
            for value in hierarchy_options.json()["series"]
            if value["object_id"] == series_id
        )
        unchanged_batch = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates/batch",
            json={
                "project_id": "project-l0-l3",
                "items": [{
                    "scenario_id": scenario_id,
                    "expected_object_revision": scenario_option["object_revision"],
                    "expected_domain_revision": scenario_option["revision"],
                    "target_series_id": "project-l0-l3",
                    "current_atom_ids": [atom_id],
                }],
                "confirmed": True,
            },
        )
        assert unchanged_batch.status_code == 200
        assert unchanged_batch.json()["status"] == "failed"
        assert unchanged_batch.json()["results"][0]["status_code"] == 409
        assert len(store.list("memory_candidates")) == candidates_before_suggestion

        partial_batch_payload = {
            "project_id": "project-l0-l3",
            "items": [
                {
                    "scenario_id": scenario_id,
                    "expected_object_revision": scenario_option["object_revision"],
                    "expected_domain_revision": scenario_option["revision"],
                    "target_series_id": "series-alternative",
                    "current_atom_ids": [atom_id],
                },
                {
                    "scenario_id": "scenario-missing",
                    "expected_object_revision": 1,
                    "expected_domain_revision": 1,
                    "target_series_id": "project-l0-l3",
                    "current_atom_ids": [],
                },
            ],
            "confirmed": True,
        }
        duplicate_batch = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates/batch",
            json={
                **partial_batch_payload,
                "items": [
                    partial_batch_payload["items"][0],
                    {
                        **partial_batch_payload["items"][0],
                        "scenario_id": f" {scenario_id} ",
                    },
                ],
            },
        )
        assert duplicate_batch.status_code == 400
        assert "unique" in duplicate_batch.json()["detail"]
        assert len(store.list("memory_candidates")) == candidates_before_suggestion
        atom_mutation_batch = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates/batch",
            json={
                **partial_batch_payload,
                "items": [{
                    **partial_batch_payload["items"][0],
                    "current_atom_ids": [],
                }],
            },
        )
        assert atom_mutation_batch.status_code == 200
        assert atom_mutation_batch.json()["results"][0]["status_code"] == 409
        assert "Atom bindings conflicted" in atom_mutation_batch.json()["results"][0]["detail"]
        assert len(store.list("memory_candidates")) == candidates_before_suggestion
        partial_batch = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates/batch",
            json=partial_batch_payload,
        )
        assert partial_batch.status_code == 200, partial_batch.text
        assert partial_batch.json()["status"] == "partially_completed"
        assert partial_batch.json()["succeeded_count"] == 1
        assert partial_batch.json()["failed_count"] == 1
        assert [value["scenario_id"] for value in partial_batch.json()["results"]] == [
            scenario_id,
            "scenario-missing",
        ]
        assert partial_batch.json()["results"][0]["status_code"] == 200
        assert partial_batch.json()["results"][1]["status_code"] == 404
        batch_candidate_id = partial_batch.json()["results"][0]["candidate_id"]
        assert records.read("memory_scenarios", scenario_id).payload["atom_ids"] == [atom_id]
        assert store.read("memory_candidates", batch_candidate_id)["status"] == "pending_review"
        assert records.read("staging_scenarios", scenario_id) is None
        assert partial_batch.json()["long_term_memory_written"] is False
        assert partial_batch.json()["staging_written"] is False
        assert partial_batch.json()["review_performed"] is False

        partial_batch_replay = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates/batch",
            json=partial_batch_payload,
        )
        assert partial_batch_replay.status_code == 200
        assert partial_batch_replay.json()["results"][0]["candidate_id"] == batch_candidate_id
        foreign_project_batch = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates/batch",
            json={
                **partial_batch_payload,
                "project_id": "project-other",
                "items": [partial_batch_payload["items"][0]],
            },
        )
        assert foreign_project_batch.status_code == 200
        assert foreign_project_batch.json()["results"][0]["status_code"] == 404

        unchanged_scenario_update = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates",
            json={
                "project_id": "project-l0-l3",
                "layer": "scenario",
                "object_id": scenario_id,
                "expected_object_revision": scenario_option["object_revision"],
                "expected_domain_revision": scenario_option["revision"],
                "series_id": "project-l0-l3",
                "atom_ids": [atom_id],
                "scenario_ids": [],
                "confirmed": True,
            },
        )
        assert unchanged_scenario_update.status_code == 409
        scenario_update = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates",
            json={
                "project_id": "project-l0-l3",
                "layer": "scenario",
                "object_id": scenario_id,
                "expected_object_revision": scenario_option["object_revision"],
                "expected_domain_revision": scenario_option["revision"],
                "series_id": "project-l0-l3",
                "atom_ids": [],
                "scenario_ids": [],
                "confirmed": True,
            },
        )
        assert scenario_update.status_code == 200, scenario_update.text
        assert records.read("memory_scenarios", scenario_id).payload["atom_ids"] == [atom_id]
        scenario_update_candidate_id = scenario_update.json()["candidate_id"]
        scenario_update_replay = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates",
            json={
                "project_id": "project-l0-l3",
                "layer": "scenario",
                "object_id": scenario_id,
                "expected_object_revision": scenario_option["object_revision"],
                "expected_domain_revision": scenario_option["revision"],
                "series_id": "project-l0-l3",
                "atom_ids": [],
                "scenario_ids": [],
                "confirmed": True,
            },
        )
        assert scenario_update_replay.json()["candidate_id"] == scenario_update_candidate_id
        mismatched_scenario_update_review = client.post(
            f"/api/rebuild/memory-candidates/{scenario_update_candidate_id}/review",
            json={
                "action": "promote_to_scenario",
                "reason": "尝试审核与候选不一致的关系。",
                "series_id": "project-l0-l3",
                "atom_ids": [atom_id],
            },
        )
        assert mismatched_scenario_update_review.status_code == 400
        assert records.read("memory_scenarios", scenario_id).payload["atom_ids"] == [atom_id]
        scenario_update_review = client.post(
            f"/api/rebuild/memory-candidates/{scenario_update_candidate_id}/review",
            json={
                "action": "promote_to_scenario",
                "reason": "确认 Scenario 层级关系更新。",
                "series_id": "project-l0-l3",
                "atom_ids": [],
            },
        )
        assert scenario_update_review.status_code == 200, scenario_update_review.text
        assert records.read("memory_scenarios", scenario_id).payload["atom_ids"] == [atom_id]
        reviewed_freshness = client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-series-freshness?series_object_id={series_id}"
            )
        )
        assert reviewed_freshness.json()["needs_refresh_count"] == 0
        scenario_update_publication = client.post(
            f"/api/rebuild/staging-scenarios/{scenario_id}/publication",
            json={"confirm": True, "reason": "发布 Scenario 层级关系更新。"},
        )
        assert scenario_update_publication.status_code == 200, scenario_update_publication.text
        assert records.read("memory_scenarios", scenario_id).payload["atom_ids"] == []
        assert records.read("memory_scenarios", scenario_id).payload["revision"] == 2
        assert records.read("memory_scenario_revisions", f"{scenario_id}~r1") is not None
        assert records.read("memory_scenario_revisions", f"{scenario_id}~r2") is not None
        assert records.read(
            "memory_publications",
            scenario_publication.json()["publication_id"],
        ).payload["status"] == "superseded"
        freshness_before = records.generation_token(
            (
                "memory_series_memory",
                "memory_publications",
                "memory_transitions",
            )
        )
        stale_series = client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-series-freshness?series_object_id={series_id}"
            )
        )
        assert stale_series.status_code == 200, stale_series.text
        freshness_item = stale_series.json()["items"][0]
        assert freshness_item["needs_refresh"] is True
        assert [value["code"] for value in freshness_item["reasons"]] == [
            "scenario_revision_newer"
        ]
        assert freshness_item["suggested_scenario_ids"] == [scenario_id]
        assert freshness_item["generated_locally"] is True
        assert records.generation_token(
            (
                "memory_series_memory",
                "memory_publications",
                "memory_transitions",
            )
        ) == freshness_before
        empty_overview_refresh = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates",
            json={
                "project_id": "project-l0-l3",
                "layer": "series_memory",
                "object_id": series_id,
                "expected_object_revision": freshness_item["series_object_revision"],
                "expected_domain_revision": freshness_item["series_revision"],
                "series_id": None,
                "atom_ids": [],
                "scenario_ids": freshness_item["suggested_scenario_ids"],
                "proposed_overview": " ",
                "confirmed": True,
            },
        )
        assert empty_overview_refresh.status_code == 400
        refresh_candidate = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates",
            json={
                "project_id": "project-l0-l3",
                "layer": "series_memory",
                "object_id": series_id,
                "expected_object_revision": freshness_item["series_object_revision"],
                "expected_domain_revision": freshness_item["series_revision"],
                "series_id": None,
                "atom_ids": [],
                "scenario_ids": freshness_item["suggested_scenario_ids"],
                "proposed_overview": (
                    freshness_item["suggested_overview"]
                    + " 用户确认：Scenario 已进入 revision 2。"
                ),
                "confirmed": True,
            },
        )
        assert refresh_candidate.status_code == 200, refresh_candidate.text
        refresh_review = client.post(
            f"/api/rebuild/memory-candidates/{refresh_candidate.json()['candidate_id']}/review",
            json={
                "action": "promote_to_series_memory",
                "reason": "确认刷新 Series 总览。",
                "scenario_ids": [scenario_id],
            },
        )
        assert refresh_review.status_code == 200, refresh_review.text
        assert records.read("memory_series_memory", series_id).payload["revision"] == 1
        refresh_publication = client.post(
            f"/api/rebuild/staging-series-memory/{series_id}/publication",
            json={"confirm": True, "reason": "发布刷新后的 Series 总览。"},
        )
        assert refresh_publication.status_code == 200, refresh_publication.text
        refreshed_payload = records.read("memory_series_memory", series_id).payload
        assert refreshed_payload["revision"] == 2
        assert "用户确认" in refreshed_payload["overview"]
        assert client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-series-freshness?series_object_id={series_id}"
            )
        ).json()["needs_refresh_count"] == 0
        stale_update = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates",
            json={
                "project_id": "project-l0-l3",
                "layer": "scenario",
                "object_id": scenario_id,
                "expected_object_revision": scenario_option["object_revision"],
                "expected_domain_revision": scenario_option["revision"],
                "series_id": "project-l0-l3",
                "atom_ids": [atom_id],
                "scenario_ids": [],
                "confirmed": True,
            },
        )
        assert stale_update.status_code == 409

        refreshed_options = client.get(
            "/api/rebuild/projects/project-l0-l3/memory-hierarchy-options"
        ).json()
        refreshed_series = next(
            value for value in refreshed_options["series"] if value["object_id"] == series_id
        )
        series_update = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates",
            json={
                "project_id": "project-l0-l3",
                "layer": "series_memory",
                "object_id": series_id,
                "expected_object_revision": refreshed_series["object_revision"],
                "expected_domain_revision": refreshed_series["revision"],
                "series_id": None,
                "atom_ids": [],
                "scenario_ids": [],
                "confirmed": True,
            },
        )
        assert series_update.status_code == 200, series_update.text
        series_update_review = client.post(
            f"/api/rebuild/memory-candidates/{series_update.json()['candidate_id']}/review",
            json={
                "action": "promote_to_series_memory",
                "reason": "确认 Series 层级关系更新。",
                "scenario_ids": [],
            },
        )
        assert series_update_review.status_code == 200, series_update_review.text
        series_update_publication = client.post(
            f"/api/rebuild/staging-series-memory/{series_id}/publication",
            json={"confirm": True, "reason": "发布 Series 层级关系更新。"},
        )
        assert series_update_publication.status_code == 200, series_update_publication.text
        assert records.read("memory_series_memory", series_id).payload["scenario_ids"] == []
        assert records.read("memory_series_memory", series_id).payload["revision"] == 3
        inconsistent_series = client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-series-freshness?series_object_id={series_id}"
            )
        )
        assert inconsistent_series.status_code == 200
        assert [value["code"] for value in inconsistent_series.json()["items"][0]["reasons"]] == [
            "scenario_not_listed"
        ]
        assert inconsistent_series.json()["items"][0]["suggested_scenario_ids"] == [
            scenario_id
        ]

        transfer_options = client.get(
            "/api/rebuild/projects/project-l0-l3/memory-hierarchy-options"
        ).json()
        transfer_scenario = next(
            value
            for value in transfer_options["scenarios"]
            if value["object_id"] == scenario_id
        )
        transfer_source = next(
            value
            for value in transfer_options["series"]
            if value["object_id"] == series_id
        )
        transfer_target = next(
            value
            for value in transfer_options["series"]
            if value["object_id"] == alternative_series_id
        )
        transfer_preview_token = records.generation_token(
            (
                "memory_scenarios",
                "memory_series_memory",
                "memory_publications",
                "memory_transitions",
            )
        )
        transfer_preview = client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-scenario-transfer-preview?scenario_object_id={scenario_id}"
            )
        )
        assert transfer_preview.status_code == 200, transfer_preview.text
        assert transfer_preview.json()["writes_performed"] is False
        assert transfer_preview.json()["network_called"] is False
        assert transfer_preview.json()["source_series"]["object_id"] == series_id
        assert [
            value["object_id"] for value in transfer_preview.json()["target_options"]
        ] == [alternative_series_id]
        assert records.generation_token(
            (
                "memory_scenarios",
                "memory_series_memory",
                "memory_publications",
                "memory_transitions",
            )
        ) == transfer_preview_token
        unchanged_transfer = client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-scenario-transfer-preview?scenario_object_id={scenario_id}"
                f"&target_series_object_id={series_id}"
            )
        )
        assert unchanged_transfer.status_code == 409
        transfer_payload = {
            "project_id": "project-l0-l3",
            "scenario_object_id": scenario_id,
            "target_series_object_id": alternative_series_id,
            "expected_scenario_object_revision": transfer_scenario["object_revision"],
            "expected_scenario_revision": transfer_scenario["revision"],
            "expected_source_series_object_revision": transfer_source["object_revision"],
            "expected_source_series_revision": transfer_source["revision"],
            "expected_target_series_object_revision": transfer_target["object_revision"],
            "expected_target_series_revision": transfer_target["revision"],
            "confirmed": True,
        }
        stale_transfer = client.post(
            "/api/rebuild/memory-scenario-transfer-plans",
            json={**transfer_payload, "expected_target_series_revision": 999},
        )
        assert stale_transfer.status_code == 409
        transfer = client.post(
            "/api/rebuild/memory-scenario-transfer-plans",
            json=transfer_payload,
        )
        assert transfer.status_code == 200, transfer.text
        transfer_body = transfer.json()
        transfer_plan_id = transfer_body["plan_id"]
        transfer_candidate_id = transfer_body["scenario_candidate_id"]
        assert transfer_body["writes_performed"] is True
        assert records.read("memory_scenarios", scenario_id).payload["series_id"] == (
            "project-l0-l3"
        )
        assert records.read("staging_scenarios", scenario_id) is None
        transfer_replay = client.post(
            "/api/rebuild/memory-scenario-transfer-plans",
            json=transfer_payload,
        )
        assert transfer_replay.status_code == 200
        assert transfer_replay.json()["plan_id"] == transfer_plan_id
        assert transfer_replay.json()["scenario_candidate_id"] == transfer_candidate_id
        assert transfer_replay.json()["replayed"] is True
        restored_preview = client.get(
            (
                "/api/rebuild/projects/project-l0-l3/"
                f"memory-scenario-transfer-preview?scenario_object_id={scenario_id}"
            )
        )
        assert restored_preview.status_code == 200
        assert restored_preview.json()["existing_plans"][0]["plan_id"] == transfer_plan_id
        transfer_review = client.post(
            f"/api/rebuild/memory-candidates/{transfer_candidate_id}/review",
            json={
                "action": "promote_to_scenario",
                "reason": "确认跨 Series 搬移 Scenario。",
                "series_id": "series-alternative",
                "atom_ids": [],
            },
        )
        assert transfer_review.status_code == 200, transfer_review.text
        assert records.read("memory_scenarios", scenario_id).payload["series_id"] == (
            "project-l0-l3"
        )
        pending_transfer = client.get(
            f"/api/rebuild/memory-scenario-transfer-plans/{transfer_plan_id}"
        )
        assert pending_transfer.status_code == 200
        assert pending_transfer.json()["status"] == "scenario_publication_pending"
        transfer_publication = client.post(
            f"/api/rebuild/staging-scenarios/{scenario_id}/publication",
            json={"confirm": True, "reason": "发布跨 Series 搬移后的 Scenario。"},
        )
        assert transfer_publication.status_code == 200, transfer_publication.text
        assert records.read("memory_scenarios", scenario_id).payload["series_id"] == (
            "series-alternative"
        )
        transfer_status = client.get(
            f"/api/rebuild/memory-scenario-transfer-plans/{transfer_plan_id}"
        )
        assert transfer_status.status_code == 200, transfer_status.text
        assert transfer_status.json()["status"] == "series_refresh_required"
        refresh_items = transfer_status.json()["series_refresh_items"]
        assert {value["series_object_id"] for value in refresh_items} == {
            series_id,
            alternative_series_id,
        }
        target_refresh = next(
            value
            for value in refresh_items
            if value["series_object_id"] == alternative_series_id
        )
        assert {
            value["code"] for value in target_refresh["reasons"]
        } == {"scenario_not_listed"}
        relation_refresh = client.post(
            "/api/rebuild/memory-hierarchy/update-candidates",
            json={
                "project_id": "project-l0-l3",
                "layer": "series_memory",
                "object_id": alternative_series_id,
                "expected_object_revision": target_refresh["series_object_revision"],
                "expected_domain_revision": target_refresh["series_revision"],
                "series_id": None,
                "atom_ids": [],
                "scenario_ids": target_refresh["suggested_scenario_ids"],
                "proposed_overview": (
                    target_refresh["suggested_overview"]
                    + " 用户确认：完成跨 Series 搬移。"
                ),
                "confirmed": True,
            },
        )
        assert relation_refresh.status_code == 200, relation_refresh.text
        relation_review = client.post(
            f"/api/rebuild/memory-candidates/{relation_refresh.json()['candidate_id']}/review",
            json={
                "action": "promote_to_series_memory",
                "reason": "确认搬移后的目标 Series 关系。",
                "scenario_ids": [scenario_id],
            },
        )
        assert relation_review.status_code == 200, relation_review.text
        relation_publication = client.post(
            f"/api/rebuild/staging-series-memory/{alternative_series_id}/publication",
            json={"confirm": True, "reason": "发布搬移后的目标 Series 总览。"},
        )
        assert relation_publication.status_code == 200, relation_publication.text
        completed_transfer = client.get(
            f"/api/rebuild/memory-scenario-transfer-plans/{transfer_plan_id}"
        )
        assert completed_transfer.status_code == 200, completed_transfer.text
        assert completed_transfer.json()["status"] == "completed"
        assert all(
            value["needs_refresh"] is False
            for value in completed_transfer.json()["series_refresh_items"]
        )
        cross_project_transfer = client.get(
            (
                "/api/rebuild/projects/project-other/"
                f"memory-scenario-transfer-preview?scenario_object_id={scenario_id}"
            )
        )
        assert cross_project_transfer.status_code == 404

        publication_id = series_update_publication.json()["publication_id"]
        transitions_before_rollback = len(records.list("memory_transitions"))
        rollback = client.post(
            f"/api/rebuild/memory-publications/{publication_id}/rollback",
            json={"confirm": True, "reason": "仅撤回项目总览。"},
        )
        assert rollback.status_code == 200, rollback.text
        transitions_after_rollback = len(records.list("memory_transitions"))
        replay = client.post(
            f"/api/rebuild/memory-publications/{publication_id}/rollback",
            json={"confirm": True, "reason": "重复撤回应拒绝。"},
        )

    assert store.read("sources", source_id) is not None
    assert store.read("source_content_reads", f"content-read-{source_id}") is not None
    assert records.read("memory_atoms", atom_id) is not None
    assert records.read("memory_scenarios", scenario_id) is not None
    assert records.read("memory_series_memory", series_id) is None
    assert all(ObjectStoreMemoryCandidateRepository(store).get(candidate_id) is not None for candidate_id in candidate_ids)
    publication_record = records.read("memory_publications", publication_id)
    assert publication_record is not None and publication_record.payload["status"] == "rolled_back"
    assert transitions_after_rollback == transitions_before_rollback + 1
    assert replay.status_code == 200
    assert len(records.list("memory_transitions")) == transitions_after_rollback


def test_rebuild_docx_content_read_creates_source_qa_recall_request(tmp_path) -> None:
    docx_path = tmp_path / "product-design-source-qa.docx"
    _write_minimal_docx(
        docx_path,
        "产品设计文档要求资料库能够基于本地资料回答真实项目问题，并显示引用来源。",
    )
    extractor_code = (
        "import sys, zipfile, xml.etree.ElementTree as ET; "
        "data=zipfile.ZipFile(sys.argv[1]).read('word/document.xml'); "
        "root=ET.fromstring(data); "
        "ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'; "
        "print('\\n'.join(t.text or '' for t in root.iter(ns+'t')))"
    )

    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/file-source-intake",
            json={
                "title": "产品设计信息提取文档",
                "display_name": "product-design-source-qa.docx",
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": docx_path.stat().st_size,
                "file_reference": "platform-ref-product-design-source-qa",
            },
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]
        assert client.post(
            f"/api/rebuild/sources/{source_id}/file-authorization",
            json={"file_path": str(docx_path)},
        ).status_code == 200
        assert client.put(
            "/api/rebuild/settings/local-document-text-extractor",
            json={
                "enabled": True,
                "provider_name": "stdlib-docx-smoke",
                "command": [sys.executable, "-c", extractor_code, "{document_path}"],
                "confirm_enable": True,
            },
        ).status_code == 200
        content_read = client.post(f"/api/rebuild/sources/{source_id}/document-text", json={})
        assert content_read.status_code == 200
        response = client.post(
            f"/api/rebuild/sources/{source_id}/qa-recall",
            json={
                "question": "资料库如何回答真实项目问题？",
                "project_id": "project-alpha",
                "project_skill_id": "skill-product-design",
                "content_read_id": f"content-read-{source_id}",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "model_request_created"
    assert body["qa_answer_state"] == "model_request_ready_no_answer_generated"
    assert body["memory_publication_state"] == "not_published"
    assert body["source_refs_display"] == [f"{source_id}#source_content_read:content-read-{source_id}"]
    assert body["source_links"] == [
        {
            "source_id": source_id,
            "title": "产品设计信息提取文档",
            "locator": f"source_content_read:content-read-{source_id}",
            "label": f"{source_id}#source_content_read:content-read-{source_id}",
            "href": f"/?view=rebuild-library-overview&source_id={source_id}",
        }
    ]
    assert "model_provider_execution" in body["blocked_operations"]
    assert "long_term_memory_publication" in body["blocked_operations"]
    recalls = ObjectStoreRecallRepository(_store(tmp_path))
    model_requests = ObjectStoreModelRequestRepository(_store(tmp_path))
    recall_request = recalls.get_request(body["recall_request_id"])
    recall_result = recalls.get_result(body["recall_result_id"])
    model_request = model_requests.get_request(body["model_request_id"])
    assert recall_request is not None
    assert recall_request["required_context_refs"][0]["kind"] == "project_skill"
    assert recall_request["required_context_refs"][1]["kind"] == "source"
    assert recall_result is not None
    assert recall_result["hits"][0]["layer"] == "l0_source"
    assert "真实项目问题" in recall_result["hits"][0]["snippet"]
    assert model_request is not None
    assert model_request["provider_preference"]["allow_remote"] is False
    assert model_request["payload"]["kind"] == "answer"
    assert model_request["payload"]["input_refs"][0]["kind"] == "recall_result"
    assert "output" not in model_request
    assert "sk-" not in str(body).lower()


def test_rebuild_docx_source_qa_recall_creates_local_answer_result(tmp_path) -> None:
    docx_path = tmp_path / "product-design-local-answer.docx"
    _write_minimal_docx(
        docx_path,
        "产品设计文档要求问答必须优先读取个人资料，并显示引用来源，不能只调用空模型回答。",
    )
    extractor_code = (
        "import sys, zipfile, xml.etree.ElementTree as ET; "
        "data=zipfile.ZipFile(sys.argv[1]).read('word/document.xml'); "
        "root=ET.fromstring(data); "
        "ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'; "
        "print('\\n'.join(t.text or '' for t in root.iter(ns+'t')))"
    )

    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/file-source-intake",
            json={
                "title": "产品设计信息提取文档",
                "display_name": "product-design-local-answer.docx",
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": docx_path.stat().st_size,
                "file_reference": "platform-ref-product-design-local-answer",
            },
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]
        assert client.post(
            f"/api/rebuild/sources/{source_id}/file-authorization",
            json={"file_path": str(docx_path)},
        ).status_code == 200
        assert client.put(
            "/api/rebuild/settings/local-document-text-extractor",
            json={
                "enabled": True,
                "provider_name": "stdlib-docx-smoke",
                "command": [sys.executable, "-c", extractor_code, "{document_path}"],
                "confirm_enable": True,
            },
        ).status_code == 200
        assert client.post(f"/api/rebuild/sources/{source_id}/document-text", json={}).status_code == 200
        recall = client.post(
            f"/api/rebuild/sources/{source_id}/qa-recall",
            json={
                "question": "问答应该如何使用个人资料？",
                "project_id": "project-alpha",
                "project_skill_id": "skill-product-design",
            },
        )
        assert recall.status_code == 200
        model_request_id = recall.json()["model_request_id"]
        answer = client.post(
            f"/api/rebuild/model-requests/{model_request_id}/local-answer",
            json={},
        )

    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "completed"
    assert body["qa_answer_state"] == "local_answer_completed"
    assert body["memory_publication_state"] == "not_published"
    assert body["model_request_id"] == model_request_id
    assert "基于已召回证据的本地回答" in body["output_preview"]
    assert "remote_model_provider_execution" in body["blocked_operations"]
    assert "api_key_use" in body["blocked_operations"]
    model_result = ObjectStoreModelResultRepository(_store(tmp_path)).get_result(body["model_result_id"])
    assert model_result is not None
    assert model_result["status"] == "completed"
    assert model_result["provider"]["provider_id"] == "local-extractive-answer"
    assert model_result["provider"]["remote"] is False
    assert "引用来源" in model_result["output"]["content"]
    assert "空模型回答" in model_result["output"]["content"]
    assert "sk-" not in str(body).lower()
    assert "sk-" not in str(model_result).lower()


def test_rebuild_docx_local_answer_creates_document_and_memory_candidate(tmp_path) -> None:
    docx_path = tmp_path / "product-design-answer-handoff.docx"
    _write_minimal_docx(
        docx_path,
        "产品设计文档要求问答结果可以进入可编辑文档，也可以生成待审记忆候选。",
    )
    extractor_code = (
        "import sys, zipfile, xml.etree.ElementTree as ET; "
        "data=zipfile.ZipFile(sys.argv[1]).read('word/document.xml'); "
        "root=ET.fromstring(data); "
        "ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'; "
        "print('\\n'.join(t.text or '' for t in root.iter(ns+'t')))"
    )

    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/file-source-intake",
            json={
                "title": "产品设计信息提取文档",
                "display_name": "product-design-answer-handoff.docx",
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": docx_path.stat().st_size,
                "file_reference": "platform-ref-product-design-answer-handoff",
            },
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]
        assert client.post(
            f"/api/rebuild/sources/{source_id}/file-authorization",
            json={"file_path": str(docx_path)},
        ).status_code == 200
        assert client.put(
            "/api/rebuild/settings/local-document-text-extractor",
            json={
                "enabled": True,
                "provider_name": "stdlib-docx-smoke",
                "command": [sys.executable, "-c", extractor_code, "{document_path}"],
                "confirm_enable": True,
            },
        ).status_code == 200
        assert client.post(f"/api/rebuild/sources/{source_id}/document-text", json={}).status_code == 200
        recall = client.post(
            f"/api/rebuild/sources/{source_id}/qa-recall",
            json={"question": "问答结果后续可以进入哪里？", "project_id": "project-alpha"},
        )
        assert recall.status_code == 200
        answer = client.post(
            f"/api/rebuild/model-requests/{recall.json()['model_request_id']}/local-answer",
            json={},
        )
        assert answer.status_code == 200
        model_result_id = answer.json()["model_result_id"]
        document = client.post(
            f"/api/rebuild/model-results/{model_result_id}/document",
            json={"title": "产品设计问答回答草稿"},
        )
        candidate = client.post(
            f"/api/rebuild/model-results/{model_result_id}/memory-candidate",
            json={"target_layer": "atom", "candidate_type": "answer_fact"},
        )
        overview = client.get("/api/rebuild/library/overview")

    assert document.status_code == 200
    document_body = document.json()
    assert document_body["status"] == "document_created"
    assert document_body["memory_publication_state"] == "not_published"
    assert "long_term_memory_publication" in document_body["blocked_operations"]
    documents = ObjectStoreDocumentRepository(_store(tmp_path))
    stored_document = documents.read(document_body["document_id"])
    markdown = documents.markdown(document_body["document_id"])
    assert stored_document is not None
    assert stored_document["type"] == "qa_answer"
    assert stored_document["status"] == "draft"
    assert "产品设计问答回答草稿" == stored_document["title"]
    assert "基于已召回证据的本地回答" in (markdown or "")
    assert candidate.status_code == 200
    candidate_body = candidate.json()
    assert candidate_body["status"] == "candidate_created"
    assert candidate_body["candidate_status"] == "pending_review"
    assert candidate_body["memory_publication_state"] == "not_published"
    assert "auto_promote_memory" in candidate_body["blocked_operations"]
    stored_candidate = ObjectStoreMemoryCandidateRepository(_store(tmp_path)).get(candidate_body["candidate_id"])
    assert stored_candidate is not None
    assert stored_candidate["status"] == "pending_review"
    assert stored_candidate["provenance"]["model_result_id"] == model_result_id
    assert overview.status_code == 200
    item_ids = {item["item_id"] for item in overview.json()["items"]}
    assert document_body["document_id"] in item_ids
    assert candidate_body["candidate_id"] in item_ids
    assert "sk-" not in str(document_body).lower()
    assert "sk-" not in str(candidate_body).lower()


def test_rebuild_four_layer_provider_endpoint_requires_local_key(tmp_path, monkeypatch) -> None:
    source_id = _save_source_with_content_read(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with _client(tmp_path) as client:
        response = client.post(
            f"/api/rebuild/sources/{source_id}/four-layer-candidates",
            json={"project_id": "project-alpha", "evidence_kind": "source_content_read"},
        )

    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "memory candidate provider is unavailable"
    assert body["actionable"] is True
    assert "sk-" not in str(body).lower()


def test_rebuild_deepseek_provider_status_reports_missing_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/providers/deepseek/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "missing_key"
    assert body["credential_source"] == "missing"
    assert body["has_api_key"] is False
    assert body["key_material_returned"] is False
    assert "sk-" not in str(body).lower()


def test_rebuild_local_provider_settings_endpoints_are_mounted(tmp_path) -> None:
    endpoints = [
        ("/api/rebuild/settings/local-ocr-provider", "local-command-ocr", "{image_path}"),
        ("/api/rebuild/settings/local-asr-provider", "local-command-asr", "{audio_path}"),
        ("/api/rebuild/settings/local-video-provider", "local-command-video", "{video_path}"),
        (
            "/api/rebuild/settings/local-document-text-extractor",
            "local-command-document-text",
            "{document_path}",
        ),
    ]

    with _client(tmp_path) as client:
        for endpoint, provider_name, placeholder in endpoints:
            initial = client.get(endpoint)
            assert initial.status_code == 200
            assert initial.json()["status"] == "disabled"
            assert initial.json()["enabled"] is False

            rejected = client.put(
                endpoint,
                json={
                    "enabled": True,
                    "provider_name": provider_name,
                    "command": ["missing-provider-executable", placeholder],
                    "confirm_enable": False,
                },
            )
            assert rejected.status_code == 400

            saved = client.put(
                endpoint,
                json={
                    "enabled": True,
                    "provider_name": provider_name,
                    "command": ["missing-provider-executable", placeholder],
                    "confirm_enable": True,
                },
            )
            assert saved.status_code == 200
            body = saved.json()
            assert body["enabled"] is True
            assert body["provider_name"] == provider_name
            assert body["command"] == ["missing-provider-executable", placeholder]
            assert body["remote_processing"] is False
            assert body["memory_publication"] == "not_started"


def test_rebuild_local_asr_builtin_enable_and_cancel_contract(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    model_dir = tmp_path / "data" / "models" / "faster-whisper" / "large-v3-turbo"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"model")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
    from backend.video_summary.infrastructure.huggingface_model_downloader import write_downloaded_model_manifest
    manager = FasterWhisperModelManager(model_dir.parent)
    write_downloaded_model_manifest(model_dir, manager.download_spec("large-v3-turbo"))
    store = _store(tmp_path)
    asset_id = "audio-track-cancel-contract"
    store.write("audio_asset_refs", asset_id, {
        "id": asset_id,
        "source_id": "source-cancel-contract",
        "audio_asset_ref": f"crp-ref://default/assets/{asset_id}",
        "path": str(tmp_path / "audio.wav"),
        "status": "available",
    }, expected_revision=None)
    job_id = f"media-job-transcript-{asset_id}"
    store.write("media_processing_jobs", job_id, {
        "id": job_id,
        "source_id": "source-cancel-contract",
        "status": "running",
    }, expected_revision=None)

    with _client(tmp_path) as client:
        enabled = client.put("/api/rebuild/settings/local-asr-provider", json={
            "enabled": True,
            "confirm_enable": True,
        })
        from core.effect_log import EffectClass, EffectIntent, EffectPurpose
        operation_id = f"audio-auto:source-cancel-contract:{asset_id}:transcribe"
        client.app.state.effect_runtime.log.plan(EffectIntent(
            session_id="audio-auto:source-cancel-contract",
            root_id="audio-auto:source-cancel-contract",
            step_key=f"transcribe_audio:{asset_id}",
            kind="audio_auto_transcribe",
            effect_class=EffectClass.IDEMPOTENT,
            purpose=EffectPurpose.PRIMARY,
            intent_ref="crp://default/workflow-intents/audio-auto/source-cancel-contract/transcribe",
            gate_decision_id="audio-auto:v2",
            rev_set={"workflow_revision": "2"},
            payload={"audio_asset_id": asset_id, "source_id": "source-cancel-contract"},
            operation_id_override=operation_id,
        ), now=1)
        cancelled = client.post(
            f"/api/rebuild/audio-assets/{asset_id}/transcription/cancel",
            json={"confirm_cancel": True},
        )
        repeated = client.post(
            f"/api/rebuild/audio-assets/{asset_id}/transcription/cancel",
            json={"confirm_cancel": True},
        )

    assert enabled.status_code == 200
    assert enabled.json()["command"] == ["builtin:faster-whisper"]
    assert enabled.json()["status"] == "ready"
    assert enabled.json()["model_status"] == "ready"
    assert cancelled.status_code == 202
    assert cancelled.json() == {"status": "cancel_requested", "operation_id": operation_id}
    assert repeated.status_code == 202
    assert _store(tmp_path).read("media_processing_jobs", job_id)["status"] == "running"


def test_rebuild_transcript_summary_provider_settings_endpoint_is_mounted(tmp_path) -> None:
    provider_file = tmp_path / "local-summary-provider.cmd"
    provider_file.write_text("@echo off\n", encoding="utf-8")

    with _client(tmp_path) as client:
        initial = client.get("/api/rebuild/settings/transcript-summary-provider")
        assert initial.status_code == 200
        assert initial.json()["status"] == "disabled"
        assert initial.json()["enabled"] is False
        assert initial.json()["remote_processing"] is False
        assert initial.json()["memory_publication"] == "not_started"

        rejected = client.put(
            "/api/rebuild/settings/transcript-summary-provider",
            json={
                "enabled": True,
                "provider_name": "local-command-transcript-summary",
                "command": [str(provider_file), "--json"],
                "confirm_enable": False,
            },
        )
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "transcript summary provider settings rejected"

        saved = client.put(
            "/api/rebuild/settings/transcript-summary-provider",
            json={
                "enabled": True,
                "provider_name": "local-command-transcript-summary",
                "command": [str(provider_file), "--json"],
                "timeout_seconds": 120,
                "confirm_enable": True,
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["status"] == "ready"
        assert body["enabled"] is True
        assert body["provider_name"] == "local-command-transcript-summary"
        assert body["command"] == [str(provider_file), "--json"]
        assert body["timeout_seconds"] == 120
        assert body["remote_processing"] is False
        assert body["memory_publication"] == "not_started"


def test_rebuild_video_workflow_endpoints_are_mounted(tmp_path, monkeypatch) -> None:
    output_file = tmp_path / "downloads" / "BV1abcDEF234_p1.mp4"
    output_file.parent.mkdir()
    output_file.write_bytes(b"fake video")
    monkeypatch.setattr(
        product_bilibili,
        "AuthorizedBilibiliDownloader",
        lambda: _FakeAuthorizedBilibiliDownloader(output_file),
    )

    with _client(tmp_path) as client:
        plan_response = client.post(
            "/api/rebuild/video-links/bilibili/download-plan",
            json={"url": "https://www.bilibili.com/video/BV1abcDEF234"},
        )
        assert plan_response.status_code == 200
        plan = plan_response.json()

        download_response = client.post(
            "/api/rebuild/video-links/bilibili/authorized-download",
            json={
                "confirm_download": True,
                "project_id": "project-alpha",
                "plan": plan,
                "settings": {
                    "enabled": True,
                    "provider_name": "yt-dlp-bilibili",
                    "output_root": "local-video-output",
                    "cookie_mode": "none",
                },
            },
        )

    assert plan["downloads_video"] is False
    assert download_response.status_code == 200
    body = download_response.json()
    assert body["status"] == "completed"
    assert body["downloads_video"] is True
    assert body["source_registration"]["status"] == "authorized_source_created"
    assert body["source_id"].startswith("source-video-")
    assert body["authorization_ref"].startswith("crp://default/authorized-video/")
    assert body["starts_audio_extraction"] is False
    assert body["auto_workflow"]["status"] == "blocked"
    assert body["auto_workflow"]["project_id"] == "project-alpha"
    assert body["auto_workflow"]["steps"][0]["name"] == "extract_audio"
    assert body["auto_workflow"]["steps"][0]["status"] == "blocked"
    assert body["auto_workflow"]["blocked_operations"] == ["extract_audio"]
    assert body["publishes_memory"] is False
    assert "sk-" not in str(body).lower()
    source_record = _store(tmp_path).read("sources", body["source_id"])
    assert str(output_file) not in str(source_record)
    assert source_record["metadata"]["video_auto_workflow"]["status"] == "blocked"


def test_rebuild_auto_memory_publication_settings_are_explicitly_enabled(tmp_path) -> None:
    with _client(tmp_path) as client:
        initial = client.get("/api/rebuild/settings/auto-memory-publication")
        rejected = client.put(
            "/api/rebuild/settings/auto-memory-publication",
            json={"enabled": True, "confirm_enable": False, "allowed_layers": ["atom"]},
        )
        saved = client.put(
            "/api/rebuild/settings/auto-memory-publication",
            json={"enabled": True, "confirm_enable": True, "allowed_layers": ["atom"]},
        )

    assert initial.status_code == 200
    assert initial.json()["status"] == "disabled"
    assert initial.json()["enabled"] is False
    assert rejected.status_code == 400
    assert rejected.json()["reason"] == "enabling auto memory publication requires confirm_enable=true"
    assert saved.status_code == 200
    body = saved.json()
    assert body["status"] == "quarantined"
    assert body["enabled"] is True
    assert body["allowed_layers"] == ["atom"]
    assert body["reviewer"] == "system"
    assert body["publisher"] == "system"
    assert body["quarantined"] is True
    assert "sk-" not in str(body).lower()
    assert "cookie" not in str(body).lower()


def test_rebuild_memory_candidate_auto_publication_endpoint_uses_local_policy(tmp_path) -> None:
    store = _store(tmp_path)
    candidate_id = "memory-candidate-auto-publication-api-001"
    ObjectStoreMemoryCandidateRepository(store).save(
        {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": "project-alpha",
            "target_layer": "atom",
            "candidate_type": "answer_summary",
            "status": "pending_review",
            "proposed_content": "自动发布 endpoint 只能使用本地授权策略。",
            "source_refs": [{"source_id": "source-alpha", "locator": "media:summary"}],
            "provenance": {
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": None,
                "document_revision": None,
                "source_content_read_id": "source-content-read-alpha",
                "media_processing_output_id": None,
                "media_processing_job_id": None,
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": "source-alpha",
                        "uri": "crp://default/sources/source-alpha.json",
                    },
                    {
                        "kind": "source_content_read",
                        "object_id": "source-content-read-alpha",
                        "uri": "crp://default/source-content-reads/source-content-read-alpha.json",
                    },
                ],
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "等待本地自动发布策略。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": "2026-07-02T10:00:00+08:00",
            "updated_at": "2026-07-02T10:00:00+08:00",
        }
    )

    with _client(tmp_path) as client:
        skipped = client.post(f"/api/rebuild/memory-candidates/{candidate_id}/auto-publication", json={})
        command_rejected = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/auto-publication",
            json={"command": "provider-runner"},
        )
        client.put(
            "/api/rebuild/settings/auto-memory-publication",
            json={"enabled": True, "confirm_enable": True, "allowed_layers": ["atom"]},
        )
        published = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/auto-publication",
            json={"reason": "用户授权 endpoint 自动发布该候选。"},
        )

    assert skipped.status_code == 200
    assert skipped.json()["status"] == "skipped"
    assert skipped.json()["skipped_reason"] == "auto memory publication is disabled"
    assert command_rejected.status_code == 400
    assert command_rejected.json()["reason"] == "memory candidate auto publication endpoint does not accept provider command"
    assert published.status_code == 200
    body = published.json()
    assert body["status"] == "skipped"
    assert body["memory_publication_state"] == "not_published"
    assert body["skipped_reason"] == "automatic memory publication is quarantined pending a low-risk Atom policy"
    assert _store(tmp_path).read("memory_candidates", candidate_id)["status"] == "pending_review"
    assert _store(tmp_path).list("staging_atoms") == ()
    assert _store(tmp_path).list("memory_atoms") == ()
    assert _store(tmp_path).list("memory_publications") == ()
    assert _store(tmp_path).list("memory_transitions") == ()
    assert _store(tmp_path).list("auto_memory_publications") == ()
    assert "sk-" not in str(body).lower()
    assert "cookie" not in str(body).lower()


def test_rebuild_deepseek_provider_status_uses_local_secret_without_returning_it(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with _client_with_secret(tmp_path, "local-test-secret") as client:
        response = client.get("/api/rebuild/providers/deepseek/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["credential_source"] == "secret"
    assert body["has_api_key"] is True
    assert body["secret_name"] == "provider:deepseek"
    assert "local-test-secret" not in str(body)


def test_rebuild_deepseek_provider_status_uses_active_provider_registry_secret(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _save_active_deepseek_provider(tmp_path, provider_id="personal-deepseek")

    with _client_with_secret(
        tmp_path,
        "local-test-secret",
        secret_key="provider:personal-deepseek",
    ) as client:
        response = client.get("/api/rebuild/providers/deepseek/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["provider_id"] == "personal-deepseek"
    assert body["provider_name"] == "个人 DeepSeek"
    assert body["credential_source"] == "secret"
    assert body["secret_name"] == "provider:personal-deepseek"
    assert body["selected_secret_name"] == "provider:personal-deepseek"
    assert body["endpoint_url"] == "https://api.deepseek.com/chat/completions"
    assert body["model"] == "deepseek-chat"
    assert body["key_material_returned"] is False
    assert "local-test-secret" not in str(body)


def test_rebuild_deepseek_provider_status_keeps_legacy_secret_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _save_active_deepseek_provider(tmp_path, provider_id="personal-deepseek")

    with _client_with_secret(tmp_path, "local-test-secret") as client:
        response = client.get("/api/rebuild/providers/deepseek/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["provider_id"] == "personal-deepseek"
    assert body["credential_source"] == "deepseek_secret"
    assert body["secret_name"] == "provider:personal-deepseek"
    assert body["selected_secret_name"] == "provider:deepseek"
    assert "local-test-secret" not in str(body)


def test_rebuild_deepseek_provider_build_uses_registry_secret_for_calls(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _save_active_deepseek_provider(tmp_path, provider_id="personal-deepseek")
    container = SimpleNamespace(
        root_dir=tmp_path,
        secret_store=type(
            "SecretStore",
            (),
            {"get": lambda self, key: "local-test-secret" if key == "provider:personal-deepseek" else ""},
        )(),
    )

    provider = rebuild_routes._build_deepseek_provider_for_rebuild(container)

    assert provider.provider_name == "personal-deepseek"
    assert provider._settings.api_key_secret_name == "provider:personal-deepseek"
    assert provider._settings.endpoint_url == "https://api.deepseek.com/chat/completions"
    assert "local-test-secret" not in str(provider._settings)
