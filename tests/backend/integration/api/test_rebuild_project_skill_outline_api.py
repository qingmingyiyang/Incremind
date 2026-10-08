from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from backend.api import ai_runtime
from backend.api.routes.product import providers as product_providers
from backend.api.routes.settings import _provider_egress_manifest
from backend.security import ProviderEgressPolicyStore
from fastapi.testclient import TestClient

from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, TARGET_IDENTITY
from core.model_gateway import ModelResult
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[4]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"
FIXTURE_PATH = CONTRACT_ROOT / "fixtures" / "project_skill" / "valid-active-skill.json"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_project_skill_provider_guard_uses_same_manifest_as_settings_consent(tmp_path: Path) -> None:
    provider = {
        "provider_id": "openai",
        "base_url": "https://api.deepseek.com",
        "api_path": "/chat/completions",
    }
    policy = ProviderEgressPolicyStore(tmp_path)
    settings_manifest = _provider_egress_manifest(provider, policy)
    policy.grant(settings_manifest, manifest_id=settings_manifest.manifest_id, confirm=True)
    guard = product_providers._provider_egress_guard(SimpleNamespace(root_dir=tmp_path), provider)

    finish = guard("memory_candidate", settings_manifest.payload_categories[:1], 256)
    finish("succeeded")

    assert policy.is_consented(settings_manifest) is True


def _activate_project_skill_sqlite_authority(tmp_path: Path) -> SQLiteStructuredRecordStore:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    evidence = AggregateAuthorityEvidence("skill-create-v1", "a" * 64, "b" * 64, TARGET_IDENTITY)
    with records.begin() as transaction:
        transaction.put("aggregate_authority_targets", "default~project_skills", {
            "namespace_id": "default", "aggregate": "project_skills",
            "migration_id": evidence.migration_id, "source_fingerprint": evidence.source_fingerprint,
            "target_fingerprint": evidence.target_fingerprint, "target_identity": evidence.target_identity,
        }, expected_revision=0)
        transaction.commit()
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    initial = authority.create_json_active(namespace_id="default", aggregate="project_skills", reason="test initial")
    staged = authority.transition(namespace_id="default", aggregate="project_skills", expected_revision=initial.revision, to_state="sqlite_staged", evidence=evidence, reason="test staged")
    authority.transition(namespace_id="default", aggregate="project_skills", expected_revision=staged.revision, to_state="sqlite_active", evidence=evidence, reason="test active")
    return records


def _save_skill(store: JsonObjectStore, *, structured: dict[str, object] | None = None) -> dict[str, object]:
    skills = ObjectStoreProjectSkillRepository(store)
    payload = structured if structured is not None else _load_fixture()
    saved = skills.save(
        ProjectSkillUpdate(
            project_id=str(payload["project_id"]),
            markdown="# Alpha 项目 Skill\n\n沿用旧结构。",
            structured=payload,
            expected_revision=0,
            reason="test project skill outline api",
        )
    )
    return dict(saved)


def test_get_project_skill_returns_full_skill_with_outline_field(tmp_path: Path) -> None:
    """GET /api/rebuild/projects/{project_id}/skill 返回完整 ProjectSkill JSON，outline 字段始终存在。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])

    with _client(tmp_path) as client:
        response = client.get(f"/api/rebuild/projects/{project_id}/skill")

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id
    assert body["id"] == saved["id"]
    assert body["revision"] == saved["revision"]
    assert "outline" in body
    assert isinstance(body["outline"], list)
    assert body["outline"] == []  # fixture 没有 outline 覆盖
    # 其他字段保留
    assert body["name"] == "Alpha 项目 Skill"
    assert body["purpose"]
    assert "markdown" in body  # 编辑器需要展示 markdown
    assert body["revision_history"][0]["transition_kind"] == "revision_saved"


def test_get_project_skill_returns_404_for_missing_project(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/projects/project-missing/skill")

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "project skill not found"
    assert body["project_id"] == "project-missing"


def test_post_project_skill_creates_user_confirmed_revision_one(tmp_path: Path) -> None:
    payload = {
        "confirm": True,
        "expected_revision": 0,
        "name": "Alpha 项目规则",
        "purpose": "固定项目回答结构。",
        "outline": [{"section_id": "summary", "title": "结论", "kind": "summary", "required": True}],
        "reason": "用户确认创建项目工作规则",
    }
    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/projects/project-alpha/skill", json=payload)
        detail = client.get("/api/rebuild/projects/project-alpha/skill")

    assert response.status_code == 201
    body = response.json()
    assert body["revision"] == 1
    assert body["status"] == "active"
    assert body["trust_status"] == "user_confirmed"
    assert body["outline_status"] == "skill_created"
    assert body["revision_history"][0]["transition_kind"] == "user_edit"
    assert body["revision_history"][0]["confirmation_kind"] == "direct_user_save"
    assert detail.json()["outline"][0]["section_id"] == "summary"


def test_post_project_skill_requires_confirmation_and_revision_zero(tmp_path: Path) -> None:
    base = {"name": "Alpha", "purpose": "Purpose", "reason": "create"}
    with _client(tmp_path) as client:
        unconfirmed = client.post("/api/rebuild/projects/project-alpha/skill", json={**base, "expected_revision": 0})
        wrong_revision = client.post("/api/rebuild/projects/project-alpha/skill", json={**base, "confirm": True, "expected_revision": 1})
    assert unconfirmed.status_code == 400
    assert wrong_revision.status_code == 400


def test_post_project_skill_replay_fails_closed_without_new_revision(tmp_path: Path) -> None:
    payload = {
        "confirm": True,
        "expected_revision": 0,
        "name": "Alpha",
        "purpose": "Purpose",
        "reason": "create",
        "outline": [],
    }
    with _client(tmp_path) as client:
        first = client.post("/api/rebuild/projects/project-alpha/skill", json=payload)
        replay = client.post("/api/rebuild/projects/project-alpha/skill", json=payload)
        detail = client.get("/api/rebuild/projects/project-alpha/skill")
    assert first.status_code == 201
    assert replay.status_code == 409
    assert detail.json()["revision"] == 1
    assert len(detail.json()["revision_history"]) == 1


def test_post_project_skill_uses_only_active_sqlite_authority(tmp_path: Path) -> None:
    records = _activate_project_skill_sqlite_authority(tmp_path)
    payload = {"confirm": True, "expected_revision": 0, "name": "Alpha", "purpose": "Purpose", "reason": "create", "outline": []}
    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/projects/project-alpha/skill", json=payload)
    assert response.status_code == 201
    assert records.read("project_skills", "skill-project-alpha") is not None
    assert _store(tmp_path).read("project_skills", "skill-project-alpha") is None


def test_project_skill_markdown_import_preview_has_no_write(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        preview = client.post("/api/rebuild/projects/project-alpha/skill/import-preview", json={
            "format": "markdown", "content": "# Alpha 工作规则\n\n保持回答直接且可追溯。\n",
        })
        detail = client.get("/api/rebuild/projects/project-alpha/skill")
    assert preview.status_code == 200
    assert preview.json()["writes_performed"] is False
    assert preview.json()["expected_revision"] == 0
    assert preview.json()["preview"]["name"] == "Alpha 工作规则"
    assert detail.status_code == 404


def test_project_skill_json_import_confirm_creates_and_ignores_authority_fields(tmp_path: Path) -> None:
    content = json.dumps({
        "id": "attacker-id", "project_id": "other", "revision": 99,
        "name": "导入规则", "purpose": "从外部文件建立。", "markdown": "# 导入规则\n\n正文。",
        "outline": [{"section_id": "summary", "title": "结论", "kind": "summary", "required": True}],
    }, ensure_ascii=False)
    with _client(tmp_path) as client:
        preview = client.post("/api/rebuild/projects/project-alpha/skill/import-preview", json={"format": "json", "content": content})
        imported = client.post("/api/rebuild/projects/project-alpha/skill/import", json={
            "format": "json", "content": content, "confirm": True, "expected_revision": 0,
        })
    assert preview.status_code == 200
    assert imported.status_code == 201
    body = imported.json()
    assert body["id"] == "skill-project-alpha"
    assert body["project_id"] == "project-alpha"
    assert body["revision"] == 1
    assert body["revision_history"][0]["transition_kind"] == "external_proposal_apply"


def test_project_skill_import_updates_existing_with_cas_and_preserves_stable_id(tmp_path: Path) -> None:
    saved = _save_skill(_store(tmp_path))
    content = "# 新名称\n\n更新后的项目用途。\n"
    with _client(tmp_path) as client:
        imported = client.post(f"/api/rebuild/projects/{saved['project_id']}/skill/import", json={
            "format": "markdown", "content": content, "confirm": True, "expected_revision": saved["revision"],
        })
        stale = client.post(f"/api/rebuild/projects/{saved['project_id']}/skill/import", json={
            "format": "markdown", "content": content, "confirm": True, "expected_revision": saved["revision"],
        })
    assert imported.status_code == 200
    assert imported.json()["id"] == saved["id"]
    assert imported.json()["revision"] == saved["revision"] + 1
    assert stale.status_code == 409


def test_project_skill_import_rejects_invalid_format_and_oversize(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        invalid = client.post("/api/rebuild/projects/project-alpha/skill/import-preview", json={"format": "txt", "content": "hello"})
        oversize = client.post("/api/rebuild/projects/project-alpha/skill/import-preview", json={"format": "markdown", "content": "x" * (256 * 1024 + 1)})
    assert invalid.status_code == 400
    assert oversize.status_code == 400


def test_project_skill_import_confirm_uses_only_active_sqlite_authority(tmp_path: Path) -> None:
    records = _activate_project_skill_sqlite_authority(tmp_path)
    with _client(tmp_path) as client:
        imported = client.post("/api/rebuild/projects/project-alpha/skill/import", json={
            "format": "markdown", "content": "# Alpha\n\nPurpose\n", "confirm": True, "expected_revision": 0,
        })
    assert imported.status_code == 201
    assert records.read("project_skills", "skill-project-alpha") is not None
    assert _store(tmp_path).read("project_skills", "skill-project-alpha") is None


def _ai_skill_payload() -> dict[str, object]:
    return {
        "name": "Alpha AI 项目规则",
        "purpose": "复盘先给结论和证据，再列风险与下一步。",
        "output_rules": [{
            "rule_id": "rule-ai-summary",
            "origin": "ai",
            "rule": "先给结论与证据。",
            "priority": "must",
            "source_refs": [{"source_id": "source-alpha", "locator": "source:summary"}],
            "locked_by_user": False,
        }],
        "style_preferences": {"voice": "直接"},
        "update_rules": {"patch_strategy": "patch_existing_first", "allowed_auto_updates": []},
        "outline": [{"section_id": "summary", "title": "结论", "kind": "summary", "required": True}],
    }


def _seed_project_skill_ai_evidence(tmp_path: Path) -> None:
    _store(tmp_path).write("sources", "source-alpha", {
        "id": "source-alpha",
        "title": "Alpha 项目复盘",
        "metadata": {
            "project_id": "project-alpha",
            "summary": "已确认的项目复盘要求先给结论和证据，再列风险与下一步。",
        },
    }, expected_revision=0)


def _patch_ai_provider(monkeypatch, payload=None, *, error: Exception | None = None, calls=None) -> None:
    class Gateway:
        def invoke(self, _request):
            if calls is not None:
                calls.append("called")
            if error is not None:
                raise error
            return ModelResult(payload or _ai_skill_payload(), "provider-deepseek", "deepseek-chat", {})

    monkeypatch.setattr(
        ai_runtime,
        "resolve_model_gateway_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(gateway=Gateway(), egress_consented=True),
    )


def test_project_skill_ai_generate_creates_review_candidate_without_active_write(tmp_path: Path, monkeypatch) -> None:
    _seed_project_skill_ai_evidence(tmp_path)
    _patch_ai_provider(monkeypatch)
    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json={
            "goal": "让复盘先给结论和证据。",
            "provider_call_confirmed": True,
        })
        detail = client.get("/api/rebuild/projects/project-alpha/skill")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["active_project_skill_changed"] is False
    assert body["preview"]["name"] == "Alpha AI 项目规则"
    store = _store(tmp_path)
    assert store.read("memory_candidates", body["candidate_id"])["project_skill_draft"]["outline"][0]["section_id"] == "summary"
    assert store.read("project_skill_ai_drafts", body["draft_id"])["generated"]["output_rules"][0]["rule"] == "先给结论与证据。"
    assert detail.status_code == 404


def test_project_skill_ai_generate_defaults_non_object_style_and_update_fields(tmp_path: Path, monkeypatch) -> None:
    _seed_project_skill_ai_evidence(tmp_path)
    payload = _ai_skill_payload()
    payload["style_preferences"] = "直接"
    payload["update_rules"] = []
    _patch_ai_provider(monkeypatch, payload)

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json={
            "goal": "让复盘先给结论和证据。", "provider_call_confirmed": True,
        })

    assert response.status_code == 200
    candidate = _store(tmp_path).read("memory_candidates", response.json()["candidate_id"])
    assert candidate["project_skill_draft"]["style_preferences"]["voice"] == "直接、具体、可执行"
    assert candidate["project_skill_draft"]["update_rules"]["patch_strategy"] == "patch_existing_first"


def test_project_skill_ai_generate_deterministically_fills_non_semantic_outline_fields(tmp_path: Path, monkeypatch) -> None:
    _seed_project_skill_ai_evidence(tmp_path)
    payload = _ai_skill_payload()
    payload["outline"] = [{"kind": "summary"}, {"title": "证据", "kind": "sources", "required": False}]
    _patch_ai_provider(monkeypatch, payload)

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json={
            "goal": "让复盘先给结论和证据。", "provider_call_confirmed": True,
        })

    assert response.status_code == 200
    outline = response.json()["preview"]["outline"]
    assert outline[0] == {"section_id": "section_001", "title": "章节 1", "kind": "summary", "required": True}
    assert outline[1] == {"section_id": "section_002", "title": "证据", "kind": "sources", "required": False}


def test_project_skill_ai_generate_replay_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _seed_project_skill_ai_evidence(tmp_path)
    _patch_ai_provider(monkeypatch)
    payload = {"goal": "让复盘先给结论和证据。", "provider_call_confirmed": True}
    with _client(tmp_path) as client:
        first = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json=payload)
        replay = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json=payload)
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["candidate_id"] == first.json()["candidate_id"]
    assert replay.json()["replayed"] is True
    store = _store(tmp_path)
    assert len(store.list("memory_candidates")) == 1
    assert len(store.list("project_skill_ai_drafts")) == 1


def test_project_skill_ai_generate_requires_confirmation_and_fails_without_candidate(tmp_path: Path, monkeypatch) -> None:
    _seed_project_skill_ai_evidence(tmp_path)
    _patch_ai_provider(monkeypatch, error=ValueError("invalid schema"))
    with _client(tmp_path) as client:
        unconfirmed = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json={"goal": "目标"})
        failed = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json={"goal": "目标", "provider_call_confirmed": True})
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["provider_call_performed"] is False
    assert failed.status_code == 400
    assert failed.json()["active_project_skill_changed"] is False
    assert _store(tmp_path).list("memory_candidates") == ()


def test_project_skill_ai_generate_without_evidence_fails_before_provider_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    provider_calls = []
    _patch_ai_provider(monkeypatch, calls=provider_calls)

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json={
            "goal": "总结项目经验。",
            "provider_call_confirmed": True,
        })

    assert response.status_code == 409
    assert response.json()["reason"] == "insufficient_evidence"
    assert response.json()["provider_call_performed"] is False
    assert response.json()["active_project_skill_changed"] is False
    assert provider_calls == []
    store = _store(tmp_path)
    assert store.list("memory_candidates") == ()
    assert store.list("project_skill_ai_drafts") == ()


def test_project_skill_ai_candidate_edit_reject_restore_uses_cas_without_active_write(tmp_path: Path, monkeypatch) -> None:
    _seed_project_skill_ai_evidence(tmp_path)
    _patch_ai_provider(monkeypatch)
    with _client(tmp_path) as client:
        generated = client.post("/api/rebuild/projects/project-alpha/skill/ai-drafts", json={
            "goal": "让复盘先给结论和证据。", "provider_call_confirmed": True,
        })
        candidate_id = generated.json()["candidate_id"]
        revision_one = generated.json()["candidate_revision"]
        edited_draft = dict(generated.json()["preview"])
        edited_draft["name"] = "用户编辑后的 Alpha 规则"
        edited_draft["purpose"] = "用户确认先给结论，再核对真实证据。"
        edited = client.patch(
            f"/api/rebuild/projects/project-alpha/skill/ai-drafts/{candidate_id}",
            json={"confirm": True, "expected_revision": revision_one, "draft": edited_draft},
        )
        stale_edit = client.patch(
            f"/api/rebuild/projects/project-alpha/skill/ai-drafts/{candidate_id}",
            json={"confirm": True, "expected_revision": revision_one, "draft": edited_draft},
        )
        rejected = client.post(f"/api/rebuild/memory-candidates/{candidate_id}/review", json={
            "action": "reject", "reason": "用户暂不采用这份草稿。",
        })
        blocked_review = client.post(f"/api/rebuild/memory-candidates/{candidate_id}/review", json={
            "action": "promote_to_project_skill", "reason": "陈旧操作不得继续。",
        })
        restored = client.post(
            f"/api/rebuild/projects/project-alpha/skill/ai-drafts/{candidate_id}/restore",
            json={"confirm": True, "expected_revision": rejected.json()["candidate_revision"]},
        )
        active = client.get("/api/rebuild/projects/project-alpha/skill")

    assert generated.status_code == 200
    assert edited.status_code == 200
    assert edited.json()["candidate_revision"] == revision_one + 1
    assert edited.json()["preview"]["name"] == "用户编辑后的 Alpha 规则"
    assert edited.json()["preview"]["output_rules"][0]["origin"] == "user"
    assert edited.json()["preview"]["output_rules"][0]["locked_by_user"] is True
    assert stale_edit.status_code == 409
    assert stale_edit.json()["current_revision"] == revision_one + 1
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert blocked_review.status_code == 400
    assert restored.status_code == 200 and restored.json()["status"] == "pending_review"
    assert restored.json()["candidate_revision"] == rejected.json()["candidate_revision"] + 1
    assert active.status_code == 404
    candidate = _store(tmp_path).read("memory_candidates", candidate_id)
    assert candidate["edit_history"][0]["base_revision"] == revision_one
    assert candidate["lifecycle_history"][0]["transition"] == "rejected_to_pending_review"


def test_get_project_skill_rejects_empty_project_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/projects/%20/skill")

    assert response.status_code == 400
    body = response.json()
    assert "project_id" in body["detail"]


def test_put_outline_creates_override_and_increments_revision(tmp_path: Path) -> None:
    """PUT outline 创建项目级覆盖，revision +1，其它字段不变。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])
    expected_revision = int(saved["revision"])
    new_outline = [
        {"section_id": "summary", "title": "结论", "kind": "summary", "required": True},
        {"section_id": "evidence", "title": "依据", "kind": "key_points", "required": True},
        {"section_id": "sources", "title": "来源", "kind": "sources", "required": True},
    ]

    with _client(tmp_path) as client:
        response = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={
                "outline": new_outline,
                "expected_revision": expected_revision,
                "reason": "添加项目级 outline 覆盖",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["outline_status"] == "outline_updated"
    assert body["revision"] == expected_revision + 1
    assert len(body["outline"]) == 3
    assert body["outline"][0]["section_id"] == "summary"
    assert body["outline"][1]["kind"] == "key_points"
    # 其它字段保持不变
    assert body["name"] == saved["name"]
    assert body["purpose"] == saved["purpose"]
    assert body["status"] == saved["status"]
    # 后续 GET 也能读到 outline
    with _client(tmp_path) as client:
        get_response = client.get(f"/api/rebuild/projects/{project_id}/skill")
    assert get_response.json()["outline"] == new_outline


def test_put_outline_clears_override_with_empty_list(tmp_path: Path) -> None:
    """PUT outline 传空数组表示清除覆盖，outline 字段在响应中为空。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])
    # 先添加 outline
    skills = ObjectStoreProjectSkillRepository(store)
    structured_with_outline = dict(_load_fixture())
    structured_with_outline["outline"] = [
        {"section_id": "summary", "title": "结论", "kind": "summary", "required": True},
    ]
    updated = skills.save(
        ProjectSkillUpdate(
            project_id=project_id,
            markdown="# Alpha 项目 Skill\n\n沿用旧结构。",
            structured=structured_with_outline,
            expected_revision=int(saved["revision"]),
            reason="seed outline",
        )
    )

    with _client(tmp_path) as client:
        response = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={"outline": [], "expected_revision": int(updated["revision"]), "reason": "清除项目级 outline 覆盖"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["outline_status"] == "outline_updated"
    assert body["outline"] == []


def test_put_outline_rejects_invalid_kind(tmp_path: Path) -> None:
    """outline 包含不支持的 kind 时返回 400。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])

    with _client(tmp_path) as client:
        response = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={
                "outline": [
                    {"section_id": "x", "title": "x", "kind": "unsupported_kind", "required": True},
                ],
                "expected_revision": int(saved["revision"]),
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert "outline validation failed" in body["detail"]


def test_put_outline_rejects_duplicate_section_id(tmp_path: Path) -> None:
    """outline 包含重复 section_id 时返回 400。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])

    with _client(tmp_path) as client:
        response = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={
                "outline": [
                    {"section_id": "dup", "title": "first", "kind": "summary", "required": True},
                    {"section_id": "dup", "title": "second", "kind": "body", "required": True},
                ],
                "expected_revision": int(saved["revision"]),
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert "duplicated" in body["reason"]


def test_put_outline_rejects_missing_expected_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])

    with _client(tmp_path) as client:
        response = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={"outline": []},
        )

    assert response.status_code == 400
    body = response.json()
    assert "expected_revision" in body["detail"]


def test_put_outline_requires_explicit_user_reason(tmp_path: Path) -> None:
    saved = _save_skill(_store(tmp_path))

    with _client(tmp_path) as client:
        response = client.put(
            f"/api/rebuild/projects/{saved['project_id']}/skill/outline",
            json={"outline": [], "expected_revision": saved["revision"]},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "reason is required"


def test_put_outline_returns_409_on_stale_revision(tmp_path: Path) -> None:
    """过期 expected_revision 返回 409。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])
    # 已经被另一个操作更新到 revision+1
    skills = ObjectStoreProjectSkillRepository(store)
    skills.save(
        ProjectSkillUpdate(
            project_id=project_id,
            markdown="# Alpha 项目 Skill\n\n更新一版。",
            structured=dict(_load_fixture()),
            expected_revision=int(saved["revision"]),
            reason="another update",
        )
    )

    with _client(tmp_path) as client:
        response = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={
                "outline": [],
                "expected_revision": int(saved["revision"]),  # 用旧 revision
                "reason": "使用旧revision的冲突编辑",
            },
        )

    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "project skill revision conflict"


def test_put_outline_returns_404_for_missing_project(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put(
            "/api/rebuild/projects/project-missing/skill/outline",
            json={"outline": [], "expected_revision": 1, "reason": "不存在的项目"},
        )

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "project skill not found"


def test_put_outline_preserves_other_skill_fields(tmp_path: Path) -> None:
    """更新 outline 不应影响 source_refs / output_rules / required_context 等字段。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])

    with _client(tmp_path) as client:
        response = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={
                "outline": [
                    {"section_id": "summary", "title": "结论", "kind": "summary", "required": True},
                ],
                "expected_revision": int(saved["revision"]),
                "reason": "更新项目输出章节",
            },
        )

    body = response.json()
    assert body["source_refs"] == saved["source_refs"]
    assert body["output_rules"] == saved["output_rules"]
    assert body["required_context"] == saved["required_context"]
    assert body["update_rules"] == saved["update_rules"]


def test_direct_outline_edit_and_rollback_append_auditable_active_revisions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])
    outline = [{"section_id": "summary", "title": "结论", "kind": "summary", "required": True}]

    with _client(tmp_path) as client:
        edited = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={
                "outline": outline,
                "expected_revision": saved["revision"],
                "reason": "用户直接确认项目章节结构",
            },
        )
        rolled_back = client.post(
            f"/api/rebuild/projects/{project_id}/skill/rollback",
            json={
                "confirm": True,
                "target_revision": 1,
                "expected_revision": 2,
                "reason": "用户恢复上一版项目章节结构",
            },
        )
        replay = client.post(
            f"/api/rebuild/projects/{project_id}/skill/rollback",
            json={
                "confirm": True,
                "target_revision": 1,
                "expected_revision": 2,
                "reason": "用户恢复上一版项目章节结构",
            },
        )
        detail = client.get(f"/api/rebuild/projects/{project_id}/skill")

    assert edited.status_code == 200
    assert edited.json()["status"] == "active"
    edit_revision = edited.json()["revision_history"][-1]
    assert edit_revision["revision"] == 2
    assert edit_revision["transition_kind"] == "user_edit"
    assert edit_revision["actor"] == "user"
    assert edit_revision["confirmation_kind"] == "direct_user_save"
    assert edit_revision["source_revision"] is None
    assert edit_revision["reason"] == "用户直接确认项目章节结构"
    assert edited.json()["decision_log"][-1]["reason"] == "用户直接确认项目章节结构"
    assert rolled_back.status_code == 200
    assert rolled_back.json()["revision"] == 3
    assert rolled_back.json()["status"] == "active"
    assert rolled_back.json()["outline"] == []
    assert rolled_back.json()["outline_status"] == "user_rollback"
    assert rolled_back.json()["revision_history"][-1]["transition_kind"] == "user_rollback"
    assert rolled_back.json()["revision_history"][-1]["source_revision"] == 1
    assert replay.status_code == 409
    assert detail.json()["revision"] == 3
    assert detail.json()["outline"] == []
    assert len(detail.json()["revision_history"]) == 3


def test_direct_rollback_requires_confirmation_reason_and_prior_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = _save_skill(store)
    skills = ObjectStoreProjectSkillRepository(store)
    second = skills.save(ProjectSkillUpdate(
        project_id=str(saved["project_id"]),
        markdown=skills.markdown(str(saved["project_id"])) or "",
        structured=dict(saved),
        expected_revision=1,
        reason="准备回滚验证",
    ))
    endpoint = f"/api/rebuild/projects/{saved['project_id']}/skill/rollback"

    with _client(tmp_path) as client:
        unconfirmed = client.post(endpoint, json={"target_revision": 1, "expected_revision": 2, "reason": "回滚"})
        no_reason = client.post(endpoint, json={"confirm": True, "target_revision": 1, "expected_revision": 2})
        current_target = client.post(endpoint, json={"confirm": True, "target_revision": 2, "expected_revision": 2, "reason": "错误目标"})

    assert second["revision"] == 2
    assert unconfirmed.status_code == 400
    assert no_reason.status_code == 400
    assert current_target.status_code == 400
    assert skills.load(str(saved["project_id"]))["revision"] == 2


def test_get_project_skill_overview_returns_skill_data(tmp_path: Path) -> None:
    """GET /api/rebuild/project-skill/overview 返回 Phase 13 只读 overview。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])

    with _client(tmp_path) as client:
        response = client.get(
            f"/api/rebuild/project-skill/overview?project_id={project_id}"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ready", "degraded"}
    assert body["project_id"] == project_id
    assert body["skill"] is not None
    assert body["skill"]["skill_id"] == saved["id"]
    assert body["endpoint_boundary"]["read_only"] is True
    assert body["endpoint_boundary"]["allows_mutation"] is False


def test_get_project_skill_overview_returns_400_for_missing_project_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/project-skill/overview")

    assert response.status_code == 400
    body = response.json()
    assert "project_id" in body["detail"]


def test_get_project_skill_overview_returns_missing_state_for_unknown_project(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/project-skill/overview?project_id=project-unknown"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "missing"
    assert body["skill"] is None


def test_outline_routes_response_excludes_secrets(tmp_path: Path) -> None:
    """outline CRUD 响应不含 secret-like 值。"""
    store = _store(tmp_path)
    saved = _save_skill(store)
    project_id = str(saved["project_id"])

    with _client(tmp_path) as client:
        get_response = client.get(f"/api/rebuild/projects/{project_id}/skill")
        put_response = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={
                "outline": [
                    {"section_id": "summary", "title": "结论", "kind": "summary", "required": True},
                ],
                "expected_revision": int(saved["revision"]),
                "reason": "验证响应不泄露敏感字段",
            },
        )
        overview_response = client.get(
            f"/api/rebuild/project-skill/overview?project_id={project_id}"
        )

    assert get_response.status_code == 200
    assert put_response.status_code == 200
    assert overview_response.status_code == 200
    full_text = (
        str(get_response.json()).lower()
        + str(put_response.json()).lower()
        + str(overview_response.json()).lower()
    )
    assert "sk-" not in full_text
    assert "bearer " not in full_text
    assert "authorization" not in full_text
    assert "cookie" not in full_text
    assert "password" not in full_text
    assert "token" not in full_text
