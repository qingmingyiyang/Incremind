from __future__ import annotations

from datetime import date
import json
from threading import Barrier, Thread

import pytest

from backend.replay.contracts import (
    CreateIntakeRequest,
    CreateSeriesRequest,
    MemoryQuestionRequest,
    ReportMergeRequest,
    UpdateAssetMetadataRequest,
    UpdateIntakeRequest,
    UpdateSeriesRequest,
)
from backend.replay.reports import ReportService
from backend.replay.series_workspace import IntakeRevisionConflictError, SeriesWorkspace


class _MergeGateway:
    def __init__(self, result: str) -> None:
        self.result = result

    def complete_text(self, messages, *, temperature=0, max_tokens=None, timeout=None) -> str:
        assert messages[0]["role"] == "system"
        assert "base_markdown" in messages[1]["content"]
        return self.result


class _OrganizeGateway:
    def complete_text(self, messages, *, temperature=0, max_tokens=None, timeout=None) -> str:
        assert messages[0]["role"] == "system"
        assert "source_text" in messages[1]["content"]
        return json.dumps(
            {
                "title": "系列隔离完成",
                "structured_text": "## 完成事项\n\n- 完成系列隔离\n\n## 问题记录\n\n无\n\n## 后续计划\n\n- 验证",
                "summary": "完成隔离并准备验证",
                "tags": ["架构", "验证"],
                "suggested_actions": ["运行验证"],
                "suggested_report_type": "daily",
            },
            ensure_ascii=False,
        )


class _FailingGateway:
    def complete_text(self, messages, *, temperature=0, max_tokens=None, timeout=None) -> str:
        raise RuntimeError("provider unavailable")


class _CountingGateway(_OrganizeGateway):
    def __init__(self) -> None:
        self.calls = 0

    def complete_text(self, messages, *, temperature=0, max_tokens=None, timeout=None) -> str:
        self.calls += 1
        return super().complete_text(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )


def test_series_crud_and_default_protection(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)

    assert workspace.active_series().series_id == "default"
    created = workspace.create_series(CreateSeriesRequest(name="项目研究", description="独立资料"))
    assert created.series_id != "default"
    assert workspace.activate_series(created.series_id).series_id == created.series_id

    updated = workspace.update_series(
        created.series_id,
        UpdateSeriesRequest(name="项目研究二", color="#920f0f"),
    )
    assert updated.name == "项目研究二"
    assert updated.color == "#920f0f"
    assert workspace.archive_series(created.series_id).status == "archived"
    assert workspace.active_series().series_id == "default"

    with pytest.raises(ValueError, match="默认系列"):
        workspace.delete_series("default", confirm=True)
    workspace.delete_series(created.series_id, confirm=True)
    with pytest.raises(LookupError):
        workspace.get_series(created.series_id)


def test_intake_promote_is_series_scoped(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    other = workspace.create_series(CreateSeriesRequest(name="阅读笔记"))
    item = workspace.create_intake(
        "default",
        CreateIntakeRequest(
            type="quick_note",
            title="今天",
            raw_text="完成系列隔离测试",
            tags=["测试"],
            links=["https://example.com/source", "https://example.com/source"],
        ),
    )

    with pytest.raises(LookupError):
        workspace.get_intake(other.series_id, item.intake_id)
    approved = workspace.approve_intake("default", item.intake_id)
    assert approved.status == "approved"
    promoted = workspace.promote_intake("default", item.intake_id)
    assert promoted.status == "merged"
    assert promoted.knowledge_item_id
    assert promoted.links == ["https://example.com/source"]
    assert workspace.replay_library("default").get_item(promoted.knowledge_item_id).series_id == "default"
    assert workspace.replay_library(other.series_id).list_items() == []


@pytest.mark.parametrize(
    ("intake_type", "knowledge_type"),
    [
        ("clip", "clip"),
        ("thought", "thought"),
        ("action", "action"),
        ("question", "question"),
    ],
)
def test_goal_intake_types_can_be_promoted(tmp_path, intake_type, knowledge_type) -> None:
    workspace = SeriesWorkspace(tmp_path)
    intake = workspace.create_intake(
        "default",
        CreateIntakeRequest(type=intake_type, raw_text=f"{intake_type} 内容"),
    )

    workspace.approve_intake("default", intake.intake_id)
    promoted = workspace.promote_intake("default", intake.intake_id)
    knowledge = workspace.replay_library("default").get_item(promoted.knowledge_item_id)

    assert knowledge is not None
    assert knowledge.type == knowledge_type






def test_legacy_intake_json_derives_stable_revision_on_read(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="旧格式内容"))
    path = workspace.series_path("default") / "intake" / "pending" / f"{item.intake_id}.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy.pop("revision")
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    loaded = workspace.get_intake("default", item.intake_id)

    assert loaded.revision == item.revision
    assert loaded.revision


def test_intake_update_returns_new_business_revision(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="原始内容"))

    updated = workspace.update_intake(
        "default",
        item.intake_id,
        UpdateIntakeRequest(raw_text="用户修改后的内容"),
    )

    assert updated.revision != item.revision
    assert workspace.get_intake("default", item.intake_id).revision == updated.revision








def test_mark_organization_failed_does_not_overwrite_concurrent_edit(tmp_path) -> None:
    class RacingWorkspace(SeriesWorkspace):
        race_on_read = False

        def get_intake(self, series_id: str, intake_id: str):
            current = super().get_intake(series_id, intake_id)
            if self.race_on_read:
                self.race_on_read = False
                super().update_intake(
                    series_id,
                    intake_id,
                    UpdateIntakeRequest(raw_text="配置失败期间的用户修改"),
                )
            return current

    workspace = RacingWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="旧内容"))
    workspace.race_on_read = True

    with pytest.raises(IntakeRevisionConflictError, match="已被更新"):
        workspace.mark_intake_organization_failed("default", item.intake_id)

    current = workspace.get_intake("default", item.intake_id)
    assert current.raw_text == "配置失败期间的用户修改"
    assert current.status == "pending"
    assert "AI 整理失败，原始内容已保留。" not in current.warnings


def test_intake_compare_and_apply_allows_only_one_concurrent_writer(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="起始内容"))
    baseline = item.revision
    barrier = Barrier(2)
    result: list[str] = []
    failures: list[Exception] = []

    def save(raw_text: str) -> None:
        candidate = item.model_copy(update={"raw_text": raw_text})
        barrier.wait()
        try:
            saved = workspace._save_intake(candidate, expected_revision=baseline)
            result.append(saved.raw_text)
        except Exception as error:  # the assertion below proves the expected conflict class
            failures.append(error)

    first = Thread(target=save, args=("第一个写入",))
    second = Thread(target=save, args=("第二个写入",))
    first.start()
    second.start()
    first.join()
    second.join()

    assert len(result) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], IntakeRevisionConflictError)
    assert workspace.get_intake("default", item.intake_id).raw_text == result[0]


def test_asset_import_deduplicates_and_creates_intake(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    metadata, intake = workspace.import_asset(
        "default",
        filename="../危险 笔记.txt",
        media_type="text/plain",
        data="资料正文".encode(),
    )

    assert metadata.filename == "危险_笔记.txt"
    assert metadata.extracted_text_path
    assert intake is not None and intake.asset_ids == [metadata.asset_id]
    assert intake.raw_text == ""
    assert "资料正文" in intake.asset_text
    same, duplicate_intake = workspace.import_asset(
        "default",
        filename="duplicate.txt",
        media_type="text/plain",
        data="资料正文".encode(),
    )
    assert same.asset_id == metadata.asset_id
    assert duplicate_intake is None










def test_memory_session_only_clears_current_series(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    other = workspace.create_series(CreateSeriesRequest(name="另一系列"))
    workspace.ask_memory("default", MemoryQuestionRequest(question="没有证据的问题"))
    assert len(workspace.memory_session("default").messages) == 2
    assert workspace.memory_session(other.series_id).messages == []

    workspace.new_memory_session("default")
    assert workspace.memory_session("default").messages == []
    assert workspace.get_series(other.series_id).name == "另一系列"


def test_legacy_migration_copies_metadata_and_maps_video_records(tmp_path) -> None:
    library = tmp_path / "library"
    (library / "index" / "items").mkdir(parents=True)
    (library / "index" / "items" / "legacy.json").write_text("{}", encoding="utf-8")
    video_dir = library / "2026" / "01" / "01" / "video"
    video_dir.mkdir(parents=True)
    (video_dir / "record.json").write_text("{}", encoding="utf-8")
    workspace = SeriesWorkspace(tmp_path)

    result = workspace.migrate_legacy()

    assert result["status"] == "completed"
    assert result["legacy_video_records"] == 1
    assert (workspace.series_path("default") / "knowledge" / "items" / "legacy.json").is_file()
    mapping = json.loads((workspace.series_path("default") / "videos" / "legacy-map.json").read_text(encoding="utf-8"))
    assert len(mapping["records"]) == 1
