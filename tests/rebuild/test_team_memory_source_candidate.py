from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core.team_memory_candidate_import import (
    CreateTeamMemoryImportDraft,
    ObjectStoreTeamMemoryImportDraftRepository,
)
from core.product_core.team_memory_source_authority_saga import (
    CommitTeamMemoryStagingToSource,
    ObjectStoreTeamSourceAuthority,
    TeamMemorySourceAuthorityConflict,
)
from core.product_core.team_memory_source_candidate import (
    PrepareTeamMemorySourceCandidate,
    TeamMemorySourceCandidateConflict,
    TeamMemorySourceCandidateError,
)
from core.product_core.team_memory_source_recovery import ForgetTeamCreatedSource
from core.product_core.team_memory_source_staging import (
    PrepareTeamMemoryLocalSource,
    TeamMemorySourceStagingRepository,
)
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
CONTENT = "团队部署规则：先在隔离环境演练，再记录生产回滚点。"


def _completed_source(tmp_path: Path, *, asset_type: str = "chat_memory"):
    store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )
    drafts = ObjectStoreTeamMemoryImportDraftRepository(store)
    draft = CreateTeamMemoryImportDraft(drafts=drafts).execute(
        profile={
            "enabled": True,
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
        },
        asset={
            "asset_id": "asset-1",
            "team_id": "team-1",
            "asset_type": asset_type,
            "name": "部署规则",
            "visibility": "restricted",
            "status": "approved",
            "version": 7,
        },
        access_evidence={
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "asset_id": "asset-1",
            "asset_version": 7,
            "action": "read",
            "inventory_fingerprint": "a" * 64,
            "verified_at": "2026-07-27T09:00:00+08:00",
        },
        project_id="project-1",
        target_layer="project_skill" if asset_type == "skill" else "series_memory",
        candidate_type="other" if asset_type == "skill" else "answer_summary",
        content=CONTENT,
        content_sha256=hashlib.sha256(CONTENT.encode("utf-8")).hexdigest(),
        consent_id="consent-request-0007",
        confirmed=True,
        created_at="2026-07-27T09:01:00+08:00",
    )
    staging = TeamMemorySourceStagingRepository(store)
    preparer = PrepareTeamMemoryLocalSource(
        drafts=drafts,
        staging=staging,
        namespace_id="default",
    )
    source_preview = preparer.preview(draft.draft_id)
    staged = preparer.stage(
        draft.draft_id,
        preview_id=source_preview.preview_id,
        expected_draft_revision=source_preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-27T09:02:00+08:00",
    )
    result = CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T09:03:00+08:00",
    )
    service = PrepareTeamMemorySourceCandidate(
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
        candidates=ObjectStoreMemoryCandidateRepository(store),
    )
    return store, staging, result, service


def test_completed_team_source_previews_and_stages_review_only_candidate(
    tmp_path: Path,
) -> None:
    store, staging, source_result, service = _completed_source(tmp_path)
    preview = service.preview(source_result.staging_id)

    assert preview.target_layer == "series_memory"
    assert preview.candidate_type == "answer_summary"
    assert preview.proposed_content == CONTENT
    assert preview.source_revision == source_result.source_revision
    assert preview.difference == {
        "state": "new_candidate",
        "current_revision": None,
        "proposed_source_revision": source_result.source_revision,
        "content_sha256": hashlib.sha256(CONTENT.encode("utf-8")).hexdigest(),
        "content_bytes": len(CONTENT.encode("utf-8")),
    }
    assert preview.safety["candidate_only"] is True
    assert preview.safety["publication_created"] is False
    assert store.list("memory_candidates") == ()

    result = service.stage(
        source_result.staging_id,
        preview_id=preview.preview_id,
        expected_staging_revision=preview.staging_revision,
        expected_source_revision=preview.source_revision,
        confirmed=True,
        created_at="2026-07-27T09:04:00+08:00",
    )
    candidate = result.candidate

    assert result.status == "pending_review"
    assert result.replayed is False
    assert candidate["proposed_content"] == CONTENT
    assert candidate["review"]["requires_user_confirmation"] is True
    assert candidate["review"]["auto_promote_allowed"] is False
    assert candidate["provenance"]["source_id"] == source_result.source_id
    assert candidate["provenance"]["source_revision"] == source_result.source_revision
    assert candidate["source_refs"] == [
        {
            "source_id": source_result.source_id,
            "locator": source_result.source_uri,
        }
    ]
    assert len(store.list("memory_candidates")) == 1
    for collection in (
        "memory_atoms",
        "memory_scenarios",
        "memory_series_memory",
        "project_skills",
        "memory_publications",
        "recall_requests",
        "recall_results",
    ):
        assert store.list(collection) == ()
    assert staging.get(source_result.staging_id)["receipt"]["memory_created"] is False


def test_skill_asset_routes_to_project_skill_draft_review_and_replays(
    tmp_path: Path,
) -> None:
    store, _staging, source_result, service = _completed_source(
        tmp_path,
        asset_type="skill",
    )
    preview = service.preview(source_result.staging_id)
    first = service.stage(
        source_result.staging_id,
        preview_id=preview.preview_id,
        expected_staging_revision=preview.staging_revision,
        expected_source_revision=preview.source_revision,
        confirmed=True,
        created_at="2026-07-27T09:04:00+08:00",
    )
    replay = service.stage(
        source_result.staging_id,
        preview_id=preview.preview_id,
        expected_staging_revision=preview.staging_revision,
        expected_source_revision=preview.source_revision,
        confirmed=True,
        created_at="2026-07-27T09:04:00+08:00",
    )

    assert first.target_layer == "project_skill"
    assert first.candidate["candidate_type"] == "other"
    assert replay.candidate_id == first.candidate_id
    assert replay.replayed is True
    assert len(store.list("memory_candidates")) == 1
    assert store.list("project_skills") == ()


def test_candidate_requires_confirmation_and_exact_preview_and_revisions(
    tmp_path: Path,
) -> None:
    store, staging, source_result, service = _completed_source(tmp_path)
    preview = service.preview(source_result.staging_id)

    with pytest.raises(TeamMemorySourceCandidateError, match="confirmation"):
        service.stage(
            source_result.staging_id,
            preview_id=preview.preview_id,
            expected_staging_revision=preview.staging_revision,
            expected_source_revision=preview.source_revision,
            confirmed=False,
            created_at="2026-07-27T09:04:00+08:00",
        )
    with pytest.raises(TeamMemorySourceCandidateConflict, match="preview"):
        service.stage(
            source_result.staging_id,
            preview_id="team-source-candidate-preview-wrong",
            expected_staging_revision=preview.staging_revision,
            expected_source_revision=preview.source_revision,
            confirmed=True,
            created_at="2026-07-27T09:04:00+08:00",
        )
    with pytest.raises(TeamMemorySourceCandidateConflict, match="staging revision"):
        service.stage(
            source_result.staging_id,
            preview_id=preview.preview_id,
            expected_staging_revision=preview.staging_revision + 1,
            expected_source_revision=preview.source_revision,
            confirmed=True,
            created_at="2026-07-27T09:04:00+08:00",
        )
    assert store.list("memory_candidates") == ()
    assert staging.get(source_result.staging_id)["status"] == "completed"


def test_source_drift_blocks_preview_and_candidate_blocks_hard_forget(
    tmp_path: Path,
) -> None:
    store, staging, source_result, service = _completed_source(tmp_path)
    preview = service.preview(source_result.staging_id)
    service.stage(
        source_result.staging_id,
        preview_id=preview.preview_id,
        expected_staging_revision=preview.staging_revision,
        expected_source_revision=preview.source_revision,
        confirmed=True,
        created_at="2026-07-27T09:04:00+08:00",
    )

    with pytest.raises(TeamMemorySourceAuthorityConflict, match="memory_candidates"):
        ForgetTeamCreatedSource(
            object_store=store,
            staging=staging,
        ).execute(
            source_result.staging_id,
            expected_staging_revision=preview.staging_revision,
            confirmed=True,
            reason="用户要求删除。",
            forgotten_at="2026-07-27T09:05:00+08:00",
        )

    source = dict(store.read("sources", source_result.source_id))
    metadata = dict(source["metadata"])
    metadata["content"] = CONTENT + "篡改"
    source["metadata"] = metadata
    store.write(
        "sources",
        source_result.source_id,
        source,
        expected_revision=source_result.source_revision,
    )
    with pytest.raises(TeamMemorySourceCandidateConflict, match="authority drifted"):
        service.preview(source_result.staging_id)


def test_direct_source_provenance_passes_schema_and_rejects_partial_runtime(
    tmp_path: Path,
) -> None:
    store, _staging, source_result, service = _completed_source(tmp_path)
    preview = service.preview(source_result.staging_id)
    candidate = dict(
        service.stage(
            source_result.staging_id,
            preview_id=preview.preview_id,
            expected_staging_revision=preview.staging_revision,
            expected_source_revision=preview.source_revision,
            confirmed=True,
            created_at="2026-07-27T09:04:00+08:00",
        ).candidate
    )
    schema = json.loads(
        (ROOT / "core-contracts" / "rebuild" / "memory_candidate.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(candidate)

    partial = dict(candidate)
    partial["id"] = "memory-candidate-partial-source"
    partial["provenance"] = {
        **dict(candidate["provenance"]),
        "source_revision": None,
    }
    assert list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(partial)
    )
    with pytest.raises(Exception, match="direct Source provenance"):
        ObjectStoreMemoryCandidateRepository(store).save(partial)


@pytest.mark.parametrize(
    ("action", "expected_status"),
    (("reject", "rejected"), ("withdraw", "withdrawn")),
)
def test_candidate_disposition_atomically_erases_content_and_replays(
    tmp_path: Path,
    action: str,
    expected_status: str,
) -> None:
    store, staging, source_result, service = _completed_source(tmp_path)
    preview = service.preview(source_result.staging_id)
    staged = service.stage(
        source_result.staging_id,
        preview_id=preview.preview_id,
        expected_staging_revision=preview.staging_revision,
        expected_source_revision=preview.source_revision,
        confirmed=True,
        created_at="2026-07-27T09:04:00+08:00",
    )
    disposed = service.dispose(
        staged.candidate_id,
        expected_candidate_revision=1,
        action=action,
        reason="用户不希望保留这条候选。",
        confirmed=True,
        reviewed_at="2026-07-27T09:05:00+08:00",
    )
    persisted = store.read("memory_candidates", staged.candidate_id)

    assert disposed.status == expected_status
    assert disposed.replayed is False
    assert persisted["proposed_content"] == "[content erased]"
    assert persisted["source_erasure"]["state"] == "content_erased"
    assert persisted["source_erasure"]["action"] == action
    assert persisted["source_erasure"]["requested_candidate_revision"] == 1
    assert "用户不希望保留" not in json.dumps(persisted, ensure_ascii=False)

    replay = service.dispose(
        staged.candidate_id,
        expected_candidate_revision=1,
        action=action,
        reason="用户不希望保留这条候选。",
        confirmed=True,
        reviewed_at="2026-07-27T09:06:00+08:00",
    )
    assert replay.operation_id == disposed.operation_id
    assert replay.replayed is True

    forgotten = ForgetTeamCreatedSource(
        object_store=store,
        staging=staging,
    ).execute(
        source_result.staging_id,
        expected_staging_revision=preview.staging_revision,
        confirmed=True,
        reason="用户要求删除本地副本。",
        forgotten_at="2026-07-27T09:07:00+08:00",
    )
    assert forgotten.status == "forgotten"
    assert store.read_including_deleted("sources", source_result.source_id) is None
    persisted_bytes = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / ".rebuild-data").rglob("*.json")
    )
    assert CONTENT not in persisted_bytes


def test_disposition_rejects_wrong_confirmation_revision_action_and_generic_candidate(
    tmp_path: Path,
) -> None:
    store, _staging, source_result, service = _completed_source(tmp_path)
    preview = service.preview(source_result.staging_id)
    staged = service.stage(
        source_result.staging_id,
        preview_id=preview.preview_id,
        expected_staging_revision=preview.staging_revision,
        expected_source_revision=preview.source_revision,
        confirmed=True,
        created_at="2026-07-27T09:04:00+08:00",
    )
    with pytest.raises(TeamMemorySourceCandidateError, match="confirmation"):
        service.dispose(
            staged.candidate_id,
            expected_candidate_revision=1,
            action="reject",
            reason="拒绝。",
            confirmed=False,
            reviewed_at="2026-07-27T09:05:00+08:00",
        )
    with pytest.raises(TeamMemorySourceCandidateError, match="reject or withdraw"):
        service.dispose(
            staged.candidate_id,
            expected_candidate_revision=1,
            action="publish",
            reason="越权。",
            confirmed=True,
            reviewed_at="2026-07-27T09:05:00+08:00",
        )
    with pytest.raises(TeamMemorySourceCandidateConflict, match="revision drifted"):
        service.dispose(
            staged.candidate_id,
            expected_candidate_revision=2,
            action="reject",
            reason="拒绝。",
            confirmed=True,
            reviewed_at="2026-07-27T09:05:00+08:00",
        )
    generic = dict(staged.candidate)
    generic["id"] = "memory-candidate-generic"
    generic["provenance"] = {
        "model_result_id": "result-1",
        "model_request_id": "request-1",
        "recall_result_id": "recall-1",
        "document_id": None,
        "document_revision": None,
        "input_refs": [
            {
                "kind": "recall_result",
                "object_id": "recall-1",
                "uri": "crp://default/recall-results/recall-1",
            },
            {
                "kind": "model_request",
                "object_id": "request-1",
                "uri": "crp://default/model-requests/request-1",
            },
            {
                "kind": "model_result",
                "object_id": "result-1",
                "uri": "crp://default/model-results/result-1",
            },
        ],
    }
    ObjectStoreMemoryCandidateRepository(store).save(generic)
    with pytest.raises(TeamMemorySourceCandidateConflict, match="direct-Source"):
        service.dispose(
            "memory-candidate-generic",
            expected_candidate_revision=1,
            action="reject",
            reason="拒绝。",
            confirmed=True,
            reviewed_at="2026-07-27T09:05:00+08:00",
        )
