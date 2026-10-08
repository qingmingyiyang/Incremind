from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI

from backend.api.team_memory_source_forget_startup import (
    TeamMemoryForgetStartupRecoveryReport,
    backfill_team_memory_source_forget_effects,
    dispatch_team_memory_source_forget_effects,
)
from core.effect_log import EffectLog, EffectReaper, EffectRunner
from core.product_core.team_memory_candidate_import import (
    CreateTeamMemoryImportDraft,
    ObjectStoreTeamMemoryImportDraftRepository,
)
from core.product_core.team_memory_source_authority_saga import (
    CommitTeamMemoryStagingToSource,
    ObjectStoreTeamSourceAuthority,
)
from core.product_core.team_memory_source_recovery import ForgetTeamCreatedSource
from core.product_core.team_memory_source_staging import (
    PrepareTeamMemoryLocalSource,
    TeamMemorySourceStagingRepository,
)
from core.storage_provider import JsonObjectStore


CONTENT = "团队部署规则：先在隔离环境演练，再记录生产回滚点。"


def _recover(application: FastAPI, tmp_path: Path, *, max_operations: int = 100):
    (tmp_path / ".rebuild-data").mkdir(parents=True, exist_ok=True)
    effects = EffectLog(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    backfill_team_memory_source_forget_effects(
        tmp_path, effects, max_operations=max_operations,
    )
    EffectReaper(effects).recover_expired(now=2**31)
    return dispatch_team_memory_source_forget_effects(
        application, tmp_path, EffectRunner(effects, owner_id="test-team-memory-forget"),
        max_operations=max_operations,
    )


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _stranded_forgetting(tmp_path: Path):
    """Drive one staging record into a stranded forgetting state."""
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
    completed = CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-26T16:30:00+08:00",
    )
    crashed = ForgetTeamCreatedSource(
        object_store=store,
        staging=staging,
        after_source_deleted=lambda: (_ for _ in ()).throw(RuntimeError("crash after delete")),
    )
    with pytest.raises(RuntimeError, match="crash after delete"):
        crashed.execute(
            staged.staging_id,
            expected_staging_revision=3,
            confirmed=True,
            reason="用户撤回团队资料。",
            forgotten_at="2026-07-26T17:00:00+08:00",
        )
    assert staging.get(staged.staging_id)["status"] == "forgetting"
    return store, staging, staged.staging_id, completed.source_id


def test_stranded_forgetting_converges_at_startup(tmp_path: Path) -> None:
    store, staging, staging_id, source_id = _stranded_forgetting(tmp_path)
    application = FastAPI()

    report = _recover(application, tmp_path)

    assert isinstance(report, TeamMemoryForgetStartupRecoveryReport)
    assert report.scanned == 1
    assert report.recovered == 1
    assert report.failed == 0
    assert report.deferred == 0
    assert report.items[0].staging_id == staging_id
    assert report.items[0].operation_id is not None
    record = staging.get(staging_id)
    assert record is not None and record["status"] == "forgotten"
    assert store.read_including_deleted("sources", source_id) is None
    assert application.state.team_memory_source_forget_startup_recovery is report


def test_authority_drift_recorded_failed_without_raising(tmp_path: Path) -> None:
    store, staging, staging_id, source_id = _stranded_forgetting(tmp_path)
    proposal = dict(staging.get(staging_id)["proposed_source"])
    store.write(
        "sources",
        source_id,
        {**proposal, "content_hash": "unrelated-content-hash"},
        expected_revision=0,
    )
    application = FastAPI()

    report = _recover(application, tmp_path)

    assert report.scanned == 1
    assert report.recovered == 0
    assert report.failed == 1
    assert report.items[0].error_code == "authority_conflict"
    record = staging.get(staging_id)
    assert record is not None and record["status"] == "forgetting"


def test_no_pending_forgets_empty_report(tmp_path: Path) -> None:
    application = FastAPI()

    report = _recover(application, tmp_path)

    assert report.scanned == 0
    assert report.items == ()


def test_invalid_max_operations_rejected(tmp_path: Path) -> None:
    application = FastAPI()
    with pytest.raises(ValueError, match="max_operations must be a positive integer"):
        _recover(application, tmp_path, max_operations=0)
