from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from core.product_core.team_memory_candidate_import import (
    CreateTeamMemoryImportDraft,
    DiscardTeamMemoryImportDraft,
    ObjectStoreTeamMemoryImportDraftRepository,
)
from core.product_core.team_memory_source_authority_saga import (
    CommitTeamMemoryStagingToSource,
    ObjectStoreTeamSourceAuthority,
    TeamMemorySourceAuthorityConflict,
    TeamMemorySourceAuthorityError,
)
from core.product_core.team_memory_source_recovery import (
    AbandonConflictingTeamSourceCommit,
    ForgetTeamCreatedSource,
    diagnose_team_source_commit,
)
from core.product_core.team_memory_source_staging import (
    PrepareTeamMemoryLocalSource,
    TeamMemorySourceStagingConflict,
    TeamMemorySourceStagingRepository,
    WithdrawTeamMemorySourceStaging,
)
from core.product_core.progressive_recall_authority_reader import (
    ObjectStoreProgressiveRecallAuthorityReader,
)
from core.product_core.progressive_recall_drilldown import EvidenceSourceRef
from core.search_and_recall import build_recall_entries_from_object_store
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
CONTENT = "团队部署规则：先在隔离环境演练，再记录生产回滚点。"


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _ready_staging(tmp_path: Path):
    store = _store(tmp_path)
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
            "asset_type": "chat_memory",
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
            "verified_at": "2026-07-26T16:00:00+08:00",
        },
        project_id="project-1",
        target_layer="series_memory",
        candidate_type="answer_summary",
        content=CONTENT,
        content_sha256=hashlib.sha256(CONTENT.encode("utf-8")).hexdigest(),
        consent_id="consent-request-0007",
        confirmed=True,
        created_at="2026-07-26T16:07:00+08:00",
    )
    staging = TeamMemorySourceStagingRepository(store)
    preparer = PrepareTeamMemoryLocalSource(
        drafts=drafts,
        staging=staging,
        namespace_id="default",
    )
    preview = preparer.preview(draft.draft_id)
    staged = preparer.stage(
        draft.draft_id,
        preview_id=preview.preview_id,
        expected_draft_revision=preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-26T16:20:00+08:00",
    )
    return store, drafts, staging, draft, staged


def _consumer(
    store: JsonObjectStore,
    drafts: ObjectStoreTeamMemoryImportDraftRepository,
    staging: TeamMemorySourceStagingRepository,
    **hooks,
) -> CommitTeamMemoryStagingToSource:
    return CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
        **hooks,
    )


def _validators():
    contract_root = ROOT / "core-contracts" / "rebuild"
    source = json.loads(
        (contract_root / "source.schema.json").read_text(encoding="utf-8")
    )
    staging = json.loads(
        (
            contract_root / "team_memory_source_staging.schema.json"
        ).read_text(encoding="utf-8")
    )
    registry = Registry().with_resource(
        source["$id"],
        Resource.from_contents(source),
    )
    return (
        Draft202012Validator(source, format_checker=FormatChecker()),
        Draft202012Validator(
            staging,
            registry=registry,
            format_checker=FormatChecker(),
        ),
    )


def test_confirmed_saga_creates_one_l0_source_and_completed_receipt(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)

    result = _consumer(store, drafts, staging).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:30:00+08:00",
    )
    source = store.read("sources", result.source_id)
    completed = staging.get(staged.staging_id)
    source_validator, staging_validator = _validators()

    assert result.status == "completed"
    assert result.replayed is False
    assert source is not None
    assert source["trust_status"] == "imported_unverified"
    assert source["metadata"]["content"] == CONTENT
    assert completed is not None
    assert completed["status"] == "completed"
    assert "content" not in completed["proposed_source"]["metadata"]
    assert completed["receipt"]["source_created"] is True
    assert completed["receipt"]["memory_created"] is False
    assert completed["receipt"]["project_skill_created"] is False
    assert completed["receipt"]["publication_created"] is False
    assert completed["safety"]["source_authority_written"] is True
    assert source_validator.is_valid(source)
    assert staging_validator.is_valid(completed)
    assert len(store.list("sources")) == 1


def test_completed_saga_replays_without_rewriting_source(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)
    first = _consumer(store, drafts, staging).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:30:00+08:00",
    )
    replay = _consumer(
        _store(tmp_path),
        ObjectStoreTeamMemoryImportDraftRepository(_store(tmp_path)),
        TeamMemorySourceStagingRepository(_store(tmp_path)),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:31:00+08:00",
    )

    assert replay.operation_id == first.operation_id
    assert replay.source_revision == 1
    assert replay.replayed is True
    assert len(store.list("sources")) == 1


def test_crash_after_claim_recovers_without_source_duplication(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)

    with pytest.raises(RuntimeError, match="crash after claim"):
        _consumer(
            store,
            drafts,
            staging,
            after_claimed=lambda: (_ for _ in ()).throw(
                RuntimeError("crash after claim")
            ),
        ).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-26T16:30:00+08:00",
        )
    assert staging.get(staged.staging_id)["status"] == "committing"
    assert store.list("sources") == ()

    recovered = _consumer(store, drafts, staging).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:31:00+08:00",
    )
    assert recovered.status == "completed"
    assert len(store.list("sources")) == 1


def test_crash_after_source_write_recovers_receipt_without_duplicate(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)

    with pytest.raises(RuntimeError, match="crash after source"):
        _consumer(
            store,
            drafts,
            staging,
            after_source_created=lambda: (_ for _ in ()).throw(
                RuntimeError("crash after source")
            ),
        ).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-26T16:30:00+08:00",
        )
    assert staging.get(staged.staging_id)["status"] == "committing"
    assert len(store.list("sources")) == 1

    recovered = _consumer(store, drafts, staging).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:31:00+08:00",
    )
    assert recovered.status == "completed"
    assert len(store.list("sources")) == 1
    assert store.revision("sources", recovered.source_id) == 1


def test_cancel_revision_drift_withdrawal_and_draft_terminal_fail_closed(
    tmp_path: Path,
) -> None:
    store, drafts, staging, draft, staged = _ready_staging(tmp_path)
    consumer = _consumer(store, drafts, staging)
    with pytest.raises(
        TeamMemorySourceAuthorityError,
        match="explicit confirmation",
    ):
        consumer.execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=False,
            completed_at="2026-07-26T16:30:00+08:00",
        )
    with pytest.raises(
        TeamMemorySourceAuthorityConflict,
        match="revision drifted",
    ):
        consumer.execute(
            staged.staging_id,
            expected_staging_revision=2,
            confirmed=True,
            completed_at="2026-07-26T16:30:00+08:00",
        )

    WithdrawTeamMemorySourceStaging(staging=staging).execute(
        staged.staging_id,
        reason="取消本地 Source 创建。",
        withdrawn_at="2026-07-26T16:25:00+08:00",
    )
    with pytest.raises(
        TeamMemorySourceAuthorityConflict,
        match="withdrawn",
    ):
        consumer.execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-26T16:30:00+08:00",
        )

    _store2, drafts2, staging2, draft2, staged2 = _ready_staging(
        tmp_path / "terminal"
    )
    DiscardTeamMemoryImportDraft(drafts=drafts2).execute(
        draft2.draft_id,
        disposition="rejected",
        reason="拒绝团队草稿。",
        reviewed_at="2026-07-26T16:25:00+08:00",
    )
    with pytest.raises(
        TeamMemorySourceAuthorityConflict,
        match="no longer pending",
    ):
        _consumer(_store2, drafts2, staging2).execute(
            staged2.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-26T16:30:00+08:00",
        )


def test_existing_different_source_fails_closed_after_claim(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)
    proposal = dict(staged.record["proposed_source"])
    conflicting = {**proposal, "title": "冲突标题"}
    store.write(
        "sources",
        str(proposal["id"]),
        conflicting,
        expected_revision=0,
    )

    with pytest.raises(
        TeamMemorySourceAuthorityConflict,
        match="different content",
    ):
        _consumer(store, drafts, staging).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-26T16:30:00+08:00",
        )

    assert staging.get(staged.staging_id)["status"] == "committing"
    assert store.read("sources", str(proposal["id"])) == conflicting
    with pytest.raises(
        TeamMemorySourceStagingConflict,
        match="not staged",
    ):
        WithdrawTeamMemorySourceStaging(staging=staging).execute(
            staged.staging_id,
            reason="冲突期间不能绕过 saga 撤回。",
            withdrawn_at="2026-07-26T16:31:00+08:00",
        )


def test_completed_receipt_detects_source_revision_drift(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)
    result = _consumer(store, drafts, staging).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:30:00+08:00",
    )
    source = store.read("sources", result.source_id)
    assert source is not None
    store.write(
        "sources",
        result.source_id,
        source,
        expected_revision=1,
    )

    with pytest.raises(
        TeamMemorySourceAuthorityConflict,
        match="authority drifted",
    ):
        _consumer(store, drafts, staging).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-26T16:31:00+08:00",
        )


def test_committing_recovery_diagnoses_missing_pending_and_conflict(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)
    sources = ObjectStoreTeamSourceAuthority(store)
    with pytest.raises(RuntimeError, match="crash after claim"):
        _consumer(
            store,
            drafts,
            staging,
            after_claimed=lambda: (_ for _ in ()).throw(
                RuntimeError("crash after claim")
            ),
        ).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-26T16:30:00+08:00",
        )
    missing = diagnose_team_source_commit(
        staging=staging,
        sources=sources,
        staging_id=staged.staging_id,
    )
    assert missing.status == "resume_source_missing"
    assert missing.action == "resume_commit"
    assert missing.observed_content_sha256 is None

    proposal = dict(staging.get(staged.staging_id)["proposed_source"])
    store.write("sources", str(proposal["id"]), proposal, expected_revision=0)
    pending = diagnose_team_source_commit(
        staging=staging,
        sources=sources,
        staging_id=staged.staging_id,
    )
    assert pending.status == "resume_receipt_pending"
    assert pending.source_revision == 1

    store.delete("sources", str(proposal["id"]))
    store.write(
        "sources",
        str(proposal["id"]),
        {**proposal, "title": "unrelated owner"},
        expected_revision=0,
    )
    conflict = diagnose_team_source_commit(
        staging=staging,
        sources=sources,
        staging_id=staged.staging_id,
    )
    assert conflict.status == "source_identity_conflict"
    assert conflict.action == "abandon_or_resolve_source"

    abandoned = AbandonConflictingTeamSourceCommit(
        staging=staging,
        sources=sources,
    ).execute(
        staged.staging_id,
        expected_staging_revision=2,
        reason="保留已有 Source，放弃团队导入。",
        abandoned_at="2026-07-26T16:35:00+08:00",
    )
    assert abandoned["status"] == "abandoned"
    assert "content" not in abandoned["proposed_source"]["metadata"]
    assert store.read("sources", str(proposal["id"]))["title"] == "unrelated owner"
    assert _validators()[1].is_valid(abandoned)


def test_team_source_hard_forget_is_recoverable_and_terminal(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)
    completed = _consumer(store, drafts, staging).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:30:00+08:00",
    )
    service = ForgetTeamCreatedSource(
        object_store=store,
        staging=staging,
        after_source_deleted=lambda: (_ for _ in ()).throw(
            RuntimeError("crash after delete")
        ),
    )
    with pytest.raises(RuntimeError, match="crash after delete"):
        service.execute(
            staged.staging_id,
            expected_staging_revision=3,
            confirmed=True,
            reason="用户撤回团队资料。",
            forgotten_at="2026-07-26T17:00:00+08:00",
        )
    assert staging.get(staged.staging_id)["status"] == "forgetting"
    assert store.read_including_deleted("sources", completed.source_id) is None

    recovered = ForgetTeamCreatedSource(
        object_store=_store(tmp_path),
        staging=TeamMemorySourceStagingRepository(_store(tmp_path)),
    ).execute(
        staged.staging_id,
        expected_staging_revision=3,
        confirmed=True,
        reason="用户撤回团队资料。",
        forgotten_at="2026-07-26T17:01:00+08:00",
    )
    record = staging.get(staged.staging_id)
    assert recovered.status == "forgotten"
    assert recovered.replayed is False
    assert record["status"] == "forgotten"
    assert record["receipt"]["source_created"] is False
    assert record["safety"]["source_authority_written"] is False
    assert _validators()[1].is_valid(record)
    origin_draft = drafts.get(record["draft_id"])
    assert origin_draft["status"] == "withdrawn"
    assert origin_draft["proposed_content"] is None
    assert all(
        entry.object_id != completed.source_id
        for entry in build_recall_entries_from_object_store(store)
    )
    reader = ObjectStoreProgressiveRecallAuthorityReader(store)
    allowed = ((completed.source_id, "source:content"),)
    assert reader.read_structured(
        project_id="project-1",
        series_ids=(),
        allowed_source_refs=allowed,
        query="团队部署规则",
    ) == ()
    assert reader.read_source_evidence(
        project_id="project-1",
        source_refs=(
            EvidenceSourceRef(
                completed.source_id,
                "source:content",
                hashlib.sha256(CONTENT.encode("utf-8")).hexdigest(),
            ),
        ),
        allowed_source_refs=allowed,
        query="团队部署规则",
    ) == ()

    replay = ForgetTeamCreatedSource(
        object_store=store,
        staging=staging,
    ).execute(
        staged.staging_id,
        expected_staging_revision=3,
        confirmed=True,
        reason="用户撤回团队资料。",
        forgotten_at="2026-07-26T17:02:00+08:00",
    )
    assert replay.replayed is True


def test_team_source_hard_forget_fails_closed_with_dependency_or_drift(
    tmp_path: Path,
) -> None:
    store, drafts, staging, _draft, staged = _ready_staging(tmp_path)
    completed = _consumer(store, drafts, staging).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:30:00+08:00",
    )
    store.write(
        "memory_atoms",
        "atom-dependent",
        {
            "id": "atom-dependent",
            "source_refs": [
                {"source_id": completed.source_id, "locator": "source:content"}
            ],
        },
        expected_revision=0,
    )
    with pytest.raises(
        TeamMemorySourceAuthorityConflict,
        match="inbound references",
    ):
        ForgetTeamCreatedSource(
            object_store=store,
            staging=staging,
        ).execute(
            staged.staging_id,
            expected_staging_revision=3,
            confirmed=True,
            reason="不能越过下游记忆。",
            forgotten_at="2026-07-26T17:00:00+08:00",
        )
    assert staging.get(staged.staging_id)["status"] == "completed"
    assert store.read("sources", completed.source_id) is not None

    store.delete("memory_atoms", "atom-dependent")
    source = store.read("sources", completed.source_id)
    store.write(
        "sources",
        completed.source_id,
        source,
        expected_revision=1,
    )
    with pytest.raises(
        TeamMemorySourceAuthorityConflict,
        match="authority drifted",
    ):
        ForgetTeamCreatedSource(
            object_store=store,
            staging=staging,
        ).execute(
            staged.staging_id,
            expected_staging_revision=3,
            confirmed=True,
            reason="拒绝删除漂移权威。",
            forgotten_at="2026-07-26T17:00:00+08:00",
        )
