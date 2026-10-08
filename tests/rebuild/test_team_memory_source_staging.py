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
from core.product_core.team_memory_source_staging import (
    PrepareTeamMemoryLocalSource,
    TeamMemorySourceStagingConflict,
    TeamMemorySourceStagingError,
    TeamMemorySourceStagingRepository,
    WithdrawTeamMemorySourceStaging,
    serialize_team_memory_source_preview,
)
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
CONTENT_V1 = "团队部署规则：先演练，再由负责人确认。"
CONTENT_V2 = "团队部署规则：先演练，再由负责人确认，并记录回滚点。"


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _draft(
    tmp_path: Path,
    *,
    version: int = 7,
    content: str = CONTENT_V1,
):
    store = _store(tmp_path)
    return CreateTeamMemoryImportDraft(
        drafts=ObjectStoreTeamMemoryImportDraftRepository(store),
    ).execute(
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
            "version": version,
        },
        access_evidence={
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "asset_id": "asset-1",
            "asset_version": version,
            "action": "read",
            "inventory_fingerprint": "a" * 64,
            "verified_at": "2026-07-26T16:00:00+08:00",
        },
        project_id="project-1",
        target_layer="series_memory",
        candidate_type="answer_summary",
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        consent_id=f"consent-request-{version:04d}",
        confirmed=True,
        created_at=f"2026-07-26T16:{version:02d}:00+08:00",
    )


def _preparer(tmp_path: Path) -> PrepareTeamMemoryLocalSource:
    store = _store(tmp_path)
    return PrepareTeamMemoryLocalSource(
        drafts=ObjectStoreTeamMemoryImportDraftRepository(store),
        staging=TeamMemorySourceStagingRepository(store),
        namespace_id="default",
    )


def _schema_validator() -> Draft202012Validator:
    contract_root = ROOT / "core-contracts" / "rebuild"
    source_schema = json.loads(
        (contract_root / "source.schema.json").read_text(encoding="utf-8")
    )
    staging_schema = json.loads(
        (
            contract_root / "team_memory_source_staging.schema.json"
        ).read_text(encoding="utf-8")
    )
    registry = Registry().with_resource(
        source_schema["$id"],
        Resource.from_contents(source_schema),
    )
    return Draft202012Validator(
        staging_schema,
        registry=registry,
        format_checker=FormatChecker(),
    )


def test_preview_is_content_free_and_has_no_write_side_effect(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    store = _store(tmp_path)

    preview = _preparer(tmp_path).preview(draft.draft_id)
    payload = serialize_team_memory_source_preview(preview)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert preview.difference == {
        "state": "new_source",
        "previous_asset_version": None,
        "previous_content_sha256": None,
        "content_changed": False,
    }
    assert payload["content_included"] is False
    assert CONTENT_V1 not in serialized
    assert all(value is False for value in payload["safety"].values())
    assert store.list("team_memory_source_staging") == ()
    assert store.list("sources") == ()


def test_confirmed_preview_stages_source_proposal_without_creating_source(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    preview = _preparer(tmp_path).preview(draft.draft_id)

    result = _preparer(tmp_path).stage(
        draft.draft_id,
        preview_id=preview.preview_id,
        expected_draft_revision=preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-26T16:20:00+08:00",
    )
    reloaded = TeamMemorySourceStagingRepository(_store(tmp_path)).get(
        result.staging_id
    )

    assert result.status == "staged"
    assert result.replayed is False
    assert reloaded == result.record
    assert result.record["proposed_source"]["metadata"]["content"] == CONTENT_V1
    assert result.record["receipt"]["source_created"] is False
    assert result.record["occurred_at"] is None
    assert result.record["recorded_at"] == "2026-07-26T16:20:00+08:00"
    assert result.record["proposed_source"]["occurred_at"] is None
    assert result.record["proposed_source"]["recorded_at"] == "2026-07-26T16:20:00+08:00"
    assert _store(tmp_path).list("sources") == ()
    assert _schema_validator().is_valid(result.record)


def test_same_preview_replays_across_restart(tmp_path: Path) -> None:
    draft = _draft(tmp_path)
    first_preview = _preparer(tmp_path).preview(draft.draft_id)
    first = _preparer(tmp_path).stage(
        draft.draft_id,
        preview_id=first_preview.preview_id,
        expected_draft_revision=first_preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-26T16:20:00+08:00",
    )
    replay_preview = _preparer(tmp_path).preview(draft.draft_id)
    replay = _preparer(tmp_path).stage(
        draft.draft_id,
        preview_id=replay_preview.preview_id,
        expected_draft_revision=replay_preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-26T16:20:00+08:00",
    )

    assert replay_preview == first_preview
    assert replay.staging_id == first.staging_id
    assert replay.replayed is True
    assert len(_store(tmp_path).list("team_memory_source_staging")) == 1


def test_new_remote_version_previews_difference_and_gets_new_source(
    tmp_path: Path,
) -> None:
    first_draft = _draft(tmp_path)
    first_preview = _preparer(tmp_path).preview(first_draft.draft_id)
    _preparer(tmp_path).stage(
        first_draft.draft_id,
        preview_id=first_preview.preview_id,
        expected_draft_revision=first_preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-26T16:20:00+08:00",
    )
    next_draft = _draft(tmp_path, version=8, content=CONTENT_V2)

    preview = _preparer(tmp_path).preview(next_draft.draft_id)

    assert preview.source_id != first_preview.source_id
    assert preview.difference["state"] == "new_remote_version"
    assert preview.difference["previous_asset_version"] == 7
    assert preview.difference["content_changed"] is True


def test_remote_version_regression_and_same_version_content_drift_fail_closed(
    tmp_path: Path,
) -> None:
    latest = _draft(tmp_path, version=8, content=CONTENT_V2)
    latest_preview = _preparer(tmp_path).preview(latest.draft_id)
    _preparer(tmp_path).stage(
        latest.draft_id,
        preview_id=latest_preview.preview_id,
        expected_draft_revision=latest_preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-26T16:20:00+08:00",
    )
    older = _draft(tmp_path, version=7)
    same_version_drift = _draft(
        tmp_path,
        version=8,
        content=f"{CONTENT_V2} 未递增版本却改变正文。",
    )

    with pytest.raises(
        TeamMemorySourceStagingConflict,
        match="version regressed",
    ):
        _preparer(tmp_path).preview(older.draft_id)
    with pytest.raises(
        TeamMemorySourceStagingConflict,
        match="without version increment",
    ):
        _preparer(tmp_path).preview(same_version_drift.draft_id)


def test_cancel_preview_drift_revision_drift_and_terminal_draft_fail_closed(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    preview = _preparer(tmp_path).preview(draft.draft_id)
    with pytest.raises(
        TeamMemorySourceStagingError,
        match="explicit confirmation",
    ):
        _preparer(tmp_path).stage(
            draft.draft_id,
            preview_id=preview.preview_id,
            expected_draft_revision=preview.draft_revision,
            confirmed=False,
            staged_at="2026-07-26T16:20:00+08:00",
        )
    with pytest.raises(
        TeamMemorySourceStagingConflict,
        match="preview identity drifted",
    ):
        _preparer(tmp_path).stage(
            draft.draft_id,
            preview_id="team-source-preview-" + "0" * 32,
            expected_draft_revision=preview.draft_revision,
            confirmed=True,
            staged_at="2026-07-26T16:20:00+08:00",
        )

    store = _store(tmp_path)
    stored = store.read("team_memory_import_drafts", draft.draft_id)
    assert stored is not None
    store.write(
        "team_memory_import_drafts",
        draft.draft_id,
        stored,
        expected_revision=1,
    )
    with pytest.raises(
        TeamMemorySourceStagingConflict,
        match="preview identity drifted",
    ):
        _preparer(tmp_path).stage(
            draft.draft_id,
            preview_id=preview.preview_id,
            expected_draft_revision=preview.draft_revision,
            confirmed=True,
            staged_at="2026-07-26T16:20:00+08:00",
        )

    DiscardTeamMemoryImportDraft(
        drafts=ObjectStoreTeamMemoryImportDraftRepository(store)
    ).execute(
        draft.draft_id,
        disposition="rejected",
        reason="拒绝导入。",
        reviewed_at="2026-07-26T16:21:00+08:00",
    )
    with pytest.raises(
        TeamMemorySourceStagingConflict,
        match="not pending review",
    ):
        _preparer(tmp_path).preview(draft.draft_id)


def test_withdraw_staging_erases_body_and_keeps_receipt(
    tmp_path: Path,
) -> None:
    draft = _draft(tmp_path)
    preview = _preparer(tmp_path).preview(draft.draft_id)
    staged = _preparer(tmp_path).stage(
        draft.draft_id,
        preview_id=preview.preview_id,
        expected_draft_revision=preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-26T16:20:00+08:00",
    )
    withdrawal = WithdrawTeamMemorySourceStaging(
        staging=TeamMemorySourceStagingRepository(_store(tmp_path))
    )

    first = withdrawal.execute(
        staged.staging_id,
        reason="用户取消创建本地 Source。",
        withdrawn_at="2026-07-26T16:22:00+08:00",
    )
    replay = withdrawal.execute(
        staged.staging_id,
        reason="用户取消创建本地 Source。",
        withdrawn_at="2026-07-26T16:23:00+08:00",
    )
    serialized = json.dumps(first.record, ensure_ascii=False)

    assert first.status == "withdrawn"
    assert first.replayed is False
    assert replay.replayed is True
    assert CONTENT_V1 not in serialized
    assert first.record["receipt"]["source_created"] is False
    assert first.record["receipt"]["withdrawn"] is True
    assert _schema_validator().is_valid(first.record)
