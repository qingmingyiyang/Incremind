from __future__ import annotations

import pytest

from core.product_core import (
    ExternalAgentProposalImportError,
    ImportExternalAgentProposal,
    serialize_external_agent_proposal_import,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path):
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_external_agent_memory_proposal_import_creates_pending_review_candidate_only(tmp_path) -> None:
    store = _store(tmp_path)
    proposal = {
        "proposal_id": "proposal-alpha",
        "proposal_type": "memory_candidate_proposal",
        "summary": "外部 Agent 建议把灵感系列纳入项目复盘默认上下文。",
        "source_refs": [{"locator": "source:idea-1", "quote": "灵感系列参与输出"}],
        "evidence_refs": [{"locator": "series_summaries.json#inspiration-series"}],
        "suggested_changes": {
            "target_layer": "atom",
            "candidate_type": "external_agent_memory",
            "proposed_content": "灵感系列应作为项目复盘和回答手册的默认阅读上下文。",
        },
        "requires_user_review": True,
    }

    result = ImportExternalAgentProposal(store).execute(proposal=proposal, project_id="chriptmas-os")
    body = serialize_external_agent_proposal_import(result)

    assert body["status"] == "pending_review"
    assert body["proposal_id"] == "proposal-alpha"
    assert body["memory_candidate_id"]
    assert body["memory_publication_state"] == "not_published"
    assert "direct_long_term_memory_write" in body["blocked_operations"]

    stored = store.read("external_agent_proposals", "proposal-alpha")
    assert stored is not None
    assert stored["status"] == "pending_review"
    assert stored["memory_publication"] == "not_started"

    candidate = store.read("memory_candidates", body["memory_candidate_id"])
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["candidate_type"] == "other"
    assert candidate["provenance"]["external_agent_candidate_type"] == "external_agent_memory"
    assert candidate["review"]["requires_user_confirmation"] is True
    assert candidate["review"]["auto_promote_allowed"] is False
    assert candidate["provenance"]["external_agent_proposal_id"] == "proposal-alpha"
    assert candidate["memory_publication_state"] == "not_published"
    assert store.list("memory_atoms") == ()
    assert store.list("memory_publications") == ()
    assert store.list("external_agent_review_drafts") == ()


def test_external_agent_update_proposals_create_pending_review_drafts_without_staging(tmp_path) -> None:
    store = _store(tmp_path)
    importer = ImportExternalAgentProposal(store)

    proposals = (
        {
            "proposal_id": "series-proposal",
            "proposal_type": "series_update_proposal",
            "summary": "建议更新灵感系列总览。",
            "source_refs": ["source:idea-1"],
            "evidence_refs": ["series_summaries.json#inspiration_series"],
            "suggested_changes": {
                "series_id": "inspiration_series",
                "proposed_content": "灵感系列应参与项目构思、复盘和回答生成。",
            },
            "requires_user_review": True,
        },
        {
            "proposal_id": "skill-proposal",
            "proposal_type": "project_skill_update_proposal",
            "summary": "建议更新项目 skill 默认阅读要求。",
            "source_refs": ["source:idea-2"],
            "evidence_refs": ["project_skills.json#chriptmas-os"],
            "suggested_changes": {
                "project_skill_id": "skill-chriptmas-os",
                "proposed_content": "回答前先读取白盒导出、系列摘要和待审 proposal。",
            },
            "requires_user_review": True,
        },
        {
            "proposal_id": "document-proposal",
            "proposal_type": "document_revision_proposal",
            "summary": "建议生成文档修订草稿。",
            "source_refs": ["source:idea-3"],
            "evidence_refs": ["document_versions.json#doc-alpha"],
            "suggested_changes": {
                "document_id": "doc-alpha",
                "proposed_content": "## 修订建议\n补充灵感碰撞的应用场景。",
            },
            "requires_user_review": True,
        },
    )

    results = [importer.execute(proposal=proposal, project_id="chriptmas-os") for proposal in proposals]

    assert all(result.status == "pending_review" for result in results)
    assert all(result.memory_candidate_id is None for result in results)
    assert all(len(result.draft_ids) == 1 for result in results)
    drafts = {draft["proposal_id"]: draft for draft in store.list("external_agent_review_drafts")}
    assert set(drafts) == {"series-proposal", "skill-proposal", "document-proposal"}
    assert drafts["series-proposal"]["draft_type"] == "series_update"
    assert drafts["series-proposal"]["target_id"] == "inspiration_series"
    assert drafts["skill-proposal"]["draft_type"] == "project_skill_update"
    assert drafts["skill-proposal"]["target_id"] == "skill-chriptmas-os"
    assert drafts["document-proposal"]["draft_type"] == "document_revision"
    assert drafts["document-proposal"]["target_id"] == "doc-alpha"
    assert all(draft["review"]["requires_user_confirmation"] is True for draft in drafts.values())
    assert all(draft["review"]["auto_apply_allowed"] is False for draft in drafts.values())
    assert all(draft["application"]["state"] == "not_applied" for draft in drafts.values())
    assert store.list("staging_series_memory") == ()
    assert store.list("staging_project_skills") == ()
    assert store.list("memory_series_memory") == ()
    assert store.list("project_skills") == ()
    assert store.list("memory_publications") == ()


def test_external_agent_proposal_rejects_direct_memory_and_secret_material(tmp_path) -> None:
    store = _store(tmp_path)
    importer = ImportExternalAgentProposal(store)

    with pytest.raises(ExternalAgentProposalImportError, match="forbidden proposal field"):
        importer.execute(
            proposal={
                "proposal_id": "bad-direct-write",
                "proposal_type": "memory_candidate_proposal",
                "summary": "bad",
                "source_refs": ["source:idea-1"],
                "evidence_refs": ["source:idea-1"],
                "suggested_changes": {"proposed_content": "bad"},
                "requires_user_review": True,
                "memory_atoms": [{"id": "atom-direct"}],
            }
        )

    with pytest.raises(ExternalAgentProposalImportError, match="sensitive"):
        importer.execute(
            proposal={
                "proposal_id": "bad-secret",
                "proposal_type": "memory_candidate_proposal",
                "summary": "bad",
                "source_refs": ["source:idea-1"],
                "evidence_refs": ["source:idea-1"],
                "suggested_changes": {"proposed_content": "api_key = abcdefghijklmnop"},
                "requires_user_review": True,
            }
        )

    assert store.list("external_agent_proposals") == ()
    assert store.list("memory_candidates") == ()


def test_external_agent_proposal_import_replays_same_submission_and_rejects_drift(tmp_path) -> None:
    store = _store(tmp_path)
    importer = ImportExternalAgentProposal(store)
    proposal = {
        "proposal_id": "proposal-replay",
        "proposal_type": "memory_candidate_proposal", "summary": "same",
        "source_refs": ["crp://default/context/one"],
        "evidence_refs": ["crp://default/context/one"],
        "suggested_changes": {"proposed_content": "same", "target_layer": "atom"},
        "requires_user_review": True,
    }
    first = importer.execute(proposal=proposal, project_id="project-a")
    replay = importer.execute(proposal=proposal, project_id="project-a")
    assert first == replay
    assert len(store.list("external_agent_proposals")) == 1
    assert len(store.list("memory_candidates")) == 1

    with pytest.raises(ExternalAgentProposalImportError, match="identity conflicts"):
        importer.execute(
            proposal={**proposal, "summary": "changed"}, project_id="project-a",
        )

    stored = dict(store.read("external_agent_proposals", "proposal-replay") or {})
    stored.pop("submission", None)
    store.write(
        "external_agent_proposals", "proposal-replay", stored,
        expected_revision=store.revision("external_agent_proposals", "proposal-replay"),
    )
    assert importer.execute(proposal=proposal, project_id="project-a").proposal_id == "proposal-replay"
