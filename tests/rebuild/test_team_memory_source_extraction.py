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
)
from core.product_core.team_memory_source_extraction import (
    PrepareTeamMemorySourceExtraction,
    TeamMemorySourceExtractionConflict,
    TeamMemorySourceExtractionError,
)
from core.product_core.team_memory_source_staging import (
    PrepareTeamMemoryLocalSource,
    TeamMemorySourceStagingRepository,
)
from core.storage_provider import JsonObjectStore


CONTENT = (
    "# 启动方案\r\n\r\n"
    "决定采用本地优先启动，首次窗口需要尽快显示。🚀\r\n\r\n"
    "- 待办：记录 sidecar 就绪时间\r\n"
    "- 负责人：桌面组\r\n\r\n"
    "## 验收问答\r\n\r\n"
    "问：失败时怎么办？\r\n答：保留来源并显示恢复入口。\r\n\r\n"
    "| 指标 | 目标 |\r\n| --- | --- |\r\n| 首窗 | 3 秒 |"
)
ROOT = Path(__file__).resolve().parents[2]


def _service(tmp_path: Path, *, content: str = CONTENT):
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
            "asset_type": "chat_memory",
            "name": "启动方案",
            "visibility": "restricted",
            "status": "approved",
            "version": 1,
        },
        access_evidence={
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "asset_id": "asset-1",
            "asset_version": 1,
            "action": "read",
            "inventory_fingerprint": "a" * 64,
            "verified_at": "2026-07-27T09:00:00+08:00",
        },
        project_id="project-1",
        target_layer="series_memory",
        candidate_type="answer_summary",
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        consent_id="consent-request-0001",
        confirmed=True,
        created_at="2026-07-27T09:01:00+08:00",
    )
    staging = TeamMemorySourceStagingRepository(store)
    local = PrepareTeamMemoryLocalSource(
        drafts=drafts,
        staging=staging,
        namespace_id="default",
    )
    local_preview = local.preview(draft.draft_id)
    staged = local.stage(
        draft.draft_id,
        preview_id=local_preview.preview_id,
        expected_draft_revision=local_preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-27T09:02:00+08:00",
    )
    source_result = CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T09:03:00+08:00",
    )
    service = PrepareTeamMemorySourceExtraction(
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
        candidates=ObjectStoreMemoryCandidateRepository(store),
    )
    return store, staging, source_result, service


def test_preview_is_local_read_only_and_preserves_exact_unicode_locators(
    tmp_path: Path,
) -> None:
    store, _staging, source_result, service = _service(tmp_path)

    first = service.preview(source_result.staging_id)
    second = service.preview(source_result.staging_id)

    assert first == second
    assert first.safety == {
        "requires_user_confirmation": True,
        "one_item_per_request": True,
        "deterministic_local_extraction": True,
        "provider_called": False,
        "candidate_only": True,
        "publication_created": False,
        "automatic_recall_enabled": False,
    }
    atoms = [item for item in first.items if item.target_layer == "atom"]
    scenarios = [item for item in first.items if item.target_layer == "scenario"]
    assert atoms
    assert len(scenarios) == 2
    assert any(item.candidate_type == "answer_decision" for item in atoms)
    assert any(item.candidate_type == "answer_action" for item in atoms)
    assert "### 问答" in scenarios[1].proposed_content
    assert "### 表格" in scenarios[1].proposed_content
    for item in first.items:
        assert CONTENT[item.start_char : item.end_char] == item.source_quote
        assert item.source_locator.endswith(
            f"#char={item.start_char}-{item.end_char}"
        )
    assert store.list("memory_candidates") == ()


@pytest.mark.parametrize("layer", ["atom", "scenario"])
def test_stage_one_selected_item_is_pending_and_idempotent(
    tmp_path: Path, layer: str
) -> None:
    store, _staging, source_result, service = _service(tmp_path)
    preview = service.preview(source_result.staging_id)
    item = next(value for value in preview.items if value.target_layer == layer)
    request = {
        "preview_id": preview.preview_id,
        "item_id": item.item_id,
        "target_layer": layer,
        "expected_staging_revision": preview.staging_revision,
        "expected_source_revision": preview.source_revision,
        "confirmed": True,
        "created_at": "2026-07-27T09:04:00+08:00",
    }

    first = service.stage(source_result.staging_id, **request)
    replay = service.stage(source_result.staging_id, **request)

    assert first.status == "pending_review"
    assert replay.candidate_id == first.candidate_id
    assert replay.replayed is True
    saved = store.read("memory_candidates", first.candidate_id)
    assert saved is not None
    assert saved["target_layer"] == layer
    assert saved["source_refs"] == [
        {
            "source_id": preview.source_id,
            "locator": item.source_locator,
            "quote": item.source_quote,
        }
    ]
    assert saved["provenance"]["source_revision"] == preview.source_revision
    assert saved["provenance"]["source_content_sha256"] == preview.source_content_sha256
    assert saved["extraction"]["item_id"] == item.item_id
    schema = json.loads(
        (ROOT / "core-contracts" / "rebuild" / "memory_candidate.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(
        schema, format_checker=FormatChecker()
    ).validate(saved)
    assert store.list("memory_publications") == ()


def test_stage_requires_confirmation_and_exact_item_target(tmp_path: Path) -> None:
    _store, _staging, source_result, service = _service(tmp_path)
    preview = service.preview(source_result.staging_id)
    item = preview.items[0]
    request = {
        "preview_id": preview.preview_id,
        "item_id": item.item_id,
        "target_layer": item.target_layer,
        "expected_staging_revision": preview.staging_revision,
        "expected_source_revision": preview.source_revision,
        "confirmed": False,
        "created_at": "2026-07-27T09:04:00+08:00",
    }
    with pytest.raises(TeamMemorySourceExtractionError, match="confirmation"):
        service.stage(source_result.staging_id, **request)

    request["confirmed"] = True
    request["target_layer"] = "scenario" if item.target_layer == "atom" else "atom"
    with pytest.raises(TeamMemorySourceExtractionConflict, match="target layer"):
        service.stage(source_result.staging_id, **request)


def test_source_or_preview_drift_fails_closed(tmp_path: Path) -> None:
    store, _staging, source_result, service = _service(tmp_path)
    preview = service.preview(source_result.staging_id)
    item = preview.items[0]
    source = store.read("sources", preview.source_id)
    assert source is not None
    store.write(
        "sources",
        preview.source_id,
        {**source, "metadata": {**source["metadata"], "content": "漂移正文"}},
        expected_revision=preview.source_revision,
    )

    with pytest.raises(TeamMemorySourceExtractionConflict, match="authority drifted"):
        service.stage(
            source_result.staging_id,
            preview_id=preview.preview_id,
            item_id=item.item_id,
            target_layer=item.target_layer,
            expected_staging_revision=preview.staging_revision,
            expected_source_revision=preview.source_revision,
            confirmed=True,
            created_at="2026-07-27T09:04:00+08:00",
        )
