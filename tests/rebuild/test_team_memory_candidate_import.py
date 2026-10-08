from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.product_core.team_memory_candidate_import import (
    CreateTeamMemoryImportDraft,
    DiscardTeamMemoryImportDraft,
    ObjectStoreTeamMemoryImportDraftRepository,
    TeamMemoryCandidateImportConflict,
    TeamMemoryCandidateImportError,
    serialize_team_memory_import_result,
)
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-07-26T16:00:00+08:00"
CONTENT = "团队已确认的部署约束：先在隔离环境演练，再由负责人批准生产变更。"


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _profile(**changes: object) -> dict[str, object]:
    return {
        "enabled": True,
        "service_id": "service-1",
        "team_id": "team-1",
        "agent_id": "agent-1",
        "user_id": "user-1",
        **changes,
    }


def _asset(**changes: object) -> dict[str, object]:
    return {
        "asset_id": "asset-1",
        "team_id": "team-1",
        "asset_type": "chat_memory",
        "name": "生产部署约束",
        "visibility": "restricted",
        "status": "approved",
        "version": 7,
        **changes,
    }


def _access(**changes: object) -> dict[str, object]:
    return {
        "service_id": "service-1",
        "team_id": "team-1",
        "agent_id": "agent-1",
        "user_id": "user-1",
        "asset_id": "asset-1",
        "asset_version": 7,
        "action": "read",
        "inventory_fingerprint": "a" * 64,
        "verified_at": "2026-07-26T15:59:00+08:00",
        **changes,
    }


def _execute(
    tmp_path: Path,
    *,
    profile: dict[str, object] | None = None,
    asset: dict[str, object] | None = None,
    access: dict[str, object] | None = None,
    content: str = CONTENT,
    content_sha256: str | None = None,
    consent_id: str = "consent-request-0001",
    confirmed: bool = True,
    created_at: str = NOW,
):
    return CreateTeamMemoryImportDraft(
        drafts=ObjectStoreTeamMemoryImportDraftRepository(_store(tmp_path)),
    ).execute(
        profile=profile or _profile(),
        asset=asset or _asset(),
        access_evidence=access or _access(),
        project_id="project-1",
        target_layer="series_memory",
        candidate_type="answer_summary",
        content=content,
        content_sha256=content_sha256
        or hashlib.sha256(content.encode("utf-8")).hexdigest(),
        consent_id=consent_id,
        confirmed=confirmed,
        created_at=created_at,
    )


def test_authorized_asset_becomes_durable_non_production_review_draft(
    tmp_path: Path,
) -> None:
    result = _execute(tmp_path)
    reloaded = ObjectStoreTeamMemoryImportDraftRepository(
        _store(tmp_path)
    ).get(result.draft_id)
    schema = json.loads(
        (
            ROOT
            / "core-contracts"
            / "rebuild"
            / "team_memory_import_draft.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert result.status == "pending_review"
    assert result.replayed is False
    assert reloaded == result.draft
    assert Draft202012Validator(schema).is_valid(result.draft)
    assert result.draft["proposed_content"] == CONTENT
    assert result.draft["review"] == {
        "requires_human_review": True,
        "publication_allowed": False,
        "automatic_recall_allowed": False,
        "auto_promote_allowed": False,
        "reviewed_by": None,
        "reviewed_at": None,
        "reason": (
            "团队资产先进入本地待审草稿；创建本地 Source、"
            "差异预览和用户确认后才可进入现有 Memory publication。"
        ),
    }
    assert all(value is False for value in result.draft["safety"].values())
    assert result.draft["schema_version"] == "1.1.0"
    assert result.draft["occurred_at"] is None
    assert result.draft["recorded_at"] == NOW
    assert result.draft["occurred_at"] != result.draft["authorization"]["verified_at"]


def test_legacy_draft_without_temporal_pair_remains_readable(tmp_path: Path) -> None:
    created = _execute(tmp_path)
    legacy = dict(created.draft)
    legacy["schema_version"] = "1.0.0"
    legacy.pop("occurred_at")
    legacy.pop("recorded_at")
    store = _store(tmp_path)
    store.write(
        "team_memory_import_drafts",
        created.draft_id,
        legacy,
        expected_revision=1,
    )

    assert ObjectStoreTeamMemoryImportDraftRepository(store).get(created.draft_id) == legacy


def test_same_remote_revision_replays_across_restart_without_rewriting(
    tmp_path: Path,
) -> None:
    first = _execute(tmp_path)
    replay = _execute(
        tmp_path,
        consent_id="consent-request-0002",
        created_at="2026-07-26T16:05:00+08:00",
    )

    assert replay.draft_id == first.draft_id
    assert replay.replayed is True
    assert replay.draft == first.draft
    assert len(_store(tmp_path).list("team_memory_import_drafts")) == 1


def test_changed_remote_version_or_body_gets_a_new_draft_identity(
    tmp_path: Path,
) -> None:
    first = _execute(tmp_path)
    changed = f"{CONTENT}\n新增回滚检查。"
    second = _execute(
        tmp_path,
        asset=_asset(version=8),
        access=_access(asset_version=8),
        content=changed,
    )

    assert second.draft_id != first.draft_id
    assert len(_store(tmp_path).list("team_memory_import_drafts")) == 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"profile": _profile(enabled=False)}, "profile must be enabled"),
        ({"profile": _profile(team_id="team-2")}, "team scope mismatch"),
        ({"asset": _asset(status="draft")}, "requires an approved asset"),
        ({"access": _access(action="write")}, "requires read authorization"),
        (
            {"access": _access(asset_version=6)},
            "access version drift",
        ),
        ({"confirmed": False}, "explicit per-asset confirmation"),
        ({"content_sha256": "b" * 64}, "body hash mismatch"),
    ],
)
def test_scope_acl_version_consent_and_body_drift_fail_closed(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(TeamMemoryCandidateImportError, match=message):
        _execute(tmp_path, **overrides)


def test_same_identity_with_tampered_persisted_payload_conflicts(
    tmp_path: Path,
) -> None:
    first = _execute(tmp_path)
    store = _store(tmp_path)
    tampered = dict(first.draft)
    tampered["created_at"] = "2026-07-26T16:01:00+08:00"
    source = dict(tampered["source"])
    source["asset_name"] = "被静默改名"
    tampered["source"] = source
    store.write(
        "team_memory_import_drafts",
        first.draft_id,
        tampered,
        expected_revision=1,
    )

    with pytest.raises(
        TeamMemoryCandidateImportConflict,
        match="identity conflict",
    ):
        _execute(tmp_path)


def test_safe_result_excludes_body_endpoint_identity_and_consent(
    tmp_path: Path,
) -> None:
    result = _execute(tmp_path)
    payload = serialize_team_memory_import_result(result)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert payload["content_included"] is False
    assert CONTENT not in serialized
    assert "user-1" not in serialized
    assert "agent-1" not in serialized
    assert "consent-request" not in serialized
    assert "https://" not in serialized
    assert "api-key" not in serialized


@pytest.mark.parametrize("disposition", ["rejected", "withdrawn"])
def test_user_disposition_erases_body_and_persists_idempotent_tombstone(
    tmp_path: Path,
    disposition: str,
) -> None:
    created = _execute(tmp_path)
    repository = ObjectStoreTeamMemoryImportDraftRepository(_store(tmp_path))
    discard = DiscardTeamMemoryImportDraft(drafts=repository)

    first = discard.execute(
        created.draft_id,
        disposition=disposition,
        reason="用户不希望将该团队资料带入本地记忆。",
        reviewed_at="2026-07-26T16:10:00+08:00",
    )
    replay = DiscardTeamMemoryImportDraft(
        drafts=ObjectStoreTeamMemoryImportDraftRepository(_store(tmp_path))
    ).execute(
        created.draft_id,
        disposition=disposition,
        reason="用户不希望将该团队资料带入本地记忆。",
        reviewed_at="2026-07-26T16:11:00+08:00",
    )
    stored = repository.get(created.draft_id)
    schema = json.loads(
        (
            ROOT
            / "core-contracts"
            / "rebuild"
            / "team_memory_import_draft.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert first.replayed is False
    assert first.content_erased is True
    assert replay.replayed is True
    assert stored is not None
    assert stored["status"] == disposition
    assert stored["proposed_content"] is None
    assert CONTENT not in json.dumps(stored, ensure_ascii=False)
    assert Draft202012Validator(schema).is_valid(stored)


def test_terminal_draft_cannot_be_silently_resurrected_or_changed(
    tmp_path: Path,
) -> None:
    created = _execute(tmp_path)
    repository = ObjectStoreTeamMemoryImportDraftRepository(_store(tmp_path))
    discard = DiscardTeamMemoryImportDraft(drafts=repository)
    discard.execute(
        created.draft_id,
        disposition="rejected",
        reason="拒绝导入。",
        reviewed_at="2026-07-26T16:10:00+08:00",
    )

    with pytest.raises(
        TeamMemoryCandidateImportConflict,
        match="identity conflict",
    ):
        _execute(tmp_path)
    with pytest.raises(
        TeamMemoryCandidateImportConflict,
        match="terminal disposition",
    ):
        discard.execute(
            created.draft_id,
            disposition="withdrawn",
            reason="改成撤回。",
            reviewed_at="2026-07-26T16:11:00+08:00",
        )
