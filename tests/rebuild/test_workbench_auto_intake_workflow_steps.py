"""阶段 1：统一 intake workflow step contract 测试。

覆盖 6 个必需场景：
1. 文本自动入库返回完整 7 步 workflow_steps
2. 链接自动入库返回完整 7 步
3. 音频即使只完成转写，也能返回后续待处理或 skipped 状态
4. 视频完整链路能映射到 summary / candidate
5. 低置信度系列进入 confirm_required 而不是 failed
6. 某一步失败时可恢复状态正确
"""
from __future__ import annotations

from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    OrchestrateWorkbenchAutoIntake,
    WorkbenchAutoIntakeItem,
    serialize_workbench_auto_intake_item,
)
from core.storage_provider import JsonObjectStore


_STEP_IDS = (
    "save_source",
    "extract_content",
    "generate_summary",
    "extract_tags",
    "assign_series",
    "create_memory_candidate",
    "update_project_brain",
)


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _orchestrator(store: JsonObjectStore, **kwargs) -> OrchestrateWorkbenchAutoIntake:
    defaults = {
        "fetch_html": "<html><body><p>个人 AI 记忆工作台 资料库 记忆 四层</p></body></html>",
        "namespace_id": "default",
    }
    defaults.update(kwargs)
    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id=defaults["namespace_id"]),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda url: defaults["fetch_html"],
        namespace_id=defaults["namespace_id"],
    )


def _steps(serialized: dict) -> list[dict]:
    steps = serialized["workflow_steps"]
    assert [s["id"] for s in steps] == list(_STEP_IDS), "步骤顺序必须固定为 7 步"
    return steps


def _step_by_id(steps: list[dict], step_id: str) -> dict:
    return next(s for s in steps if s["id"] == step_id)


# ── 场景 1：文本自动入库返回完整 7 步 workflow_steps ──────────────────

def test_text_auto_intake_returns_full_workflow_steps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="个人 AI 记忆工作台 资料库 记忆 四层",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert len(result.items) == 1
    serialized = serialize_workbench_auto_intake_item(result.items[0])
    steps = _steps(serialized)

    # 保存原始资料：done
    assert _step_by_id(steps, "save_source")["status"] == "done"
    # 转写/抽取正文：done（ReadSourceTextContent）
    assert _step_by_id(steps, "extract_content")["status"] == "done"
    # 生成摘要：done（StructureSourceContent）
    assert _step_by_id(steps, "generate_summary")["status"] == "done"
    # 提取标签：done（IndexSourceTags，命中 AI/Memory 等关键词）
    assert _step_by_id(steps, "extract_tags")["status"] == "done"
    # 判断项目系列：done（≥0.82 自动确认）
    assert _step_by_id(steps, "assign_series")["status"] == "done"
    # 生成记忆候选：skipped（文本路径当前不创建 candidate，已知缺口）
    assert _step_by_id(steps, "create_memory_candidate")["status"] == "skipped"
    # 更新项目大脑：skipped
    assert _step_by_id(steps, "update_project_brain")["status"] == "skipped"

    # 旧字段保留不变
    assert serialized["status"] == result.items[0].status
    assert serialized["auto_organization"] is not None


# ── 场景 2：链接自动入库返回完整 7 步 ──────────────────────────────

def test_link_auto_intake_returns_full_workflow_steps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="https://example.com/article",
        urls=["https://example.com/article"],
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert len(result.items) == 1
    assert result.items[0].workflow == "link_auto_organization"
    serialized = serialize_workbench_auto_intake_item(result.items[0])
    steps = _steps(serialized)

    assert _step_by_id(steps, "save_source")["status"] == "done"
    assert _step_by_id(steps, "extract_content")["status"] == "done"
    assert _step_by_id(steps, "generate_summary")["status"] == "done"
    assert _step_by_id(steps, "extract_tags")["status"] == "done"


# ── 场景 3：音频即使只完成转写，也能返回后续 skipped 状态 ────────────

def test_audio_workflow_returns_skipped_for_missing_steps(tmp_path: Path) -> None:
    """音频只有转写，没有 summary / candidate，后续步骤应显示 skipped 而不是 failed。"""
    item = WorkbenchAutoIntakeItem(
        source_id="src-audio-1",
        source_uri="crp://default/sources/src-audio-1.json",
        input_type="audio",
        workflow="audio_auto_workflow",
        status="completed",
        needs_user_confirmation=False,
        title="audio.mp3",
        content_read_status="not_started",
        structure_status="not_started",
        series_status="not_started",
        series_name="",
        series_confidence=0.0,
        inspiration_status="not_started",
        auto_organization={
            "media_auto_workflow": {
                "workflow_kind": "audio_auto_workflow",
                "status": "completed",
                "steps": [{"name": "transcribe_audio", "status": "completed"}],
            },
            "memory_publication_state": "not_started",
        },
        next_step="library_overview_refresh",
    )

    serialized = serialize_workbench_auto_intake_item(item)
    steps = _steps(serialized)

    assert serialized["progression_mode"] == "auto"
    assert serialized["progression_reason"] == "deterministic"

    # 转写完成
    assert _step_by_id(steps, "extract_content")["status"] == "done"
    # 后续步骤全部 skipped（不是 failed）
    assert _step_by_id(steps, "generate_summary")["status"] == "skipped"
    assert _step_by_id(steps, "extract_tags")["status"] == "skipped"
    assert _step_by_id(steps, "assign_series")["status"] == "skipped"
    assert _step_by_id(steps, "create_memory_candidate")["status"] == "skipped"
    assert _step_by_id(steps, "update_project_brain")["status"] == "skipped"


# ── 场景 4：视频完整链路能映射到 summary / candidate ────────────────

def test_video_workflow_maps_to_summary_and_candidate(tmp_path: Path) -> None:
    item = WorkbenchAutoIntakeItem(
        source_id="src-video-1",
        source_uri="crp://default/sources/src-video-1.json",
        input_type="video",
        workflow="video_auto_workflow",
        status="completed",
        needs_user_confirmation=False,
        title="video.mp4",
        content_read_status="not_started",
        structure_status="not_started",
        series_status="not_started",
        series_name="",
        series_confidence=0.0,
        inspiration_status="not_started",
        auto_organization={
            "media_auto_workflow": {
                "workflow_kind": "video_auto_workflow",
                "status": "completed",
                "steps": [
                    {"name": "extract_audio", "status": "completed"},
                    {"name": "transcribe_audio", "status": "completed"},
                    {"name": "summarize_transcript", "status": "completed"},
                    {"name": "create_memoryCandidate", "status": "completed"},
                ],
            },
            "memory_publication_state": "candidate_created_not_published",
        },
        next_step="library_overview_refresh",
    )

    serialized = serialize_workbench_auto_intake_item(item)
    steps = _steps(serialized)

    assert serialized["progression_mode"] == "ask"
    assert serialized["progression_reason"] == "formal_memory_publication"

    assert _step_by_id(steps, "save_source")["status"] == "done"
    assert _step_by_id(steps, "extract_content")["status"] == "done"
    assert _step_by_id(steps, "generate_summary")["status"] == "done"
    assert _step_by_id(steps, "create_memory_candidate")["status"] == "done"
    assert _step_by_id(steps, "update_project_brain")["status"] == "skipped"


# ── 场景 5：低置信度系列进入 confirm_required 而不是 failed ──────────

def test_low_confidence_series_enters_confirm_required(tmp_path: Path) -> None:
    """series_confidence < 0.82 时，assign_series 步骤应为 confirm_required，整个任务不算失败。"""
    item = WorkbenchAutoIntakeItem(
        source_id="src-low-conf-1",
        source_uri="crp://default/sources/src-low-conf-1.json",
        input_type="direct_idea",
        workflow="text_auto_organization",
        status="completed_pending_series",
        needs_user_confirmation=False,
        title="一些普通内容",
        content_read_status="completed",
        structure_status="completed",
        series_status="pending_confirmation",
        series_name="项目推进",
        series_confidence=0.59,
        inspiration_status="recorded",
        auto_organization={
            "content_read_id": "cr-1",
            "structure_ref": "crp://default/source-structures/structure-src-low-conf-1.json",
            "summary": "摘要内容",
            "tags": ["Project"],
            "tag_index_status": "indexed",
            "memory_publication_state": "not_published",
        },
        next_step="library_overview_refresh",
    )

    serialized = serialize_workbench_auto_intake_item(item)
    steps = _steps(serialized)

    assert serialized["progression_mode"] == "ask"
    assert serialized["progression_reason"] == "material_ambiguity"
    series_step = _step_by_id(steps, "assign_series")
    assert series_step["status"] == "confirm_required"
    assert series_step["recoverable"] is True
    assert series_step["retry_action"] == "confirm_series"
    # 其他步骤不应因系列待确认而失败
    assert _step_by_id(steps, "save_source")["status"] == "done"
    assert _step_by_id(steps, "extract_content")["status"] == "done"
    assert _step_by_id(steps, "generate_summary")["status"] == "done"
    assert _step_by_id(steps, "extract_tags")["status"] == "done"


# ── 场景 6：某一步失败时可恢复状态正确 ──────────────────────────────

def test_failed_step_marks_recoverable(tmp_path: Path) -> None:
    """文档抽取 Provider 未启用时，extract_content 应 failed + recoverable + retry_action。"""
    item = WorkbenchAutoIntakeItem(
        source_id="src-doc-1",
        source_uri="crp://default/sources/src-doc-1.json",
        input_type="pdf",
        workflow="document_text_extraction",
        status="needs_extractor",
        needs_user_confirmation=False,
        title="doc.pdf",
        content_read_status="not_started",
        structure_status="not_started",
        series_status="not_started",
        series_name="",
        series_confidence=0.0,
        inspiration_status="not_started",
        auto_organization={
            "media_auto_workflow": {
                "workflow_kind": "document_text_extraction",
                "status": "blocked",
                "steps": [],
            },
            "memory_publication_state": "not_started",
        },
        next_step="await_document_extractor",
    )

    serialized = serialize_workbench_auto_intake_item(item)
    steps = _steps(serialized)

    assert serialized["progression_mode"] == "ask"
    assert serialized["progression_reason"] == "permission_expansion"
    extract_step = _step_by_id(steps, "extract_content")
    assert extract_step["status"] == "failed"
    assert extract_step["recoverable"] is True
    assert extract_step["retry_action"] == "enable_provider"
    assert extract_step["error_code"] == "extract_failed"
    # 失败后后续步骤应 skipped
    assert _step_by_id(steps, "generate_summary")["status"] == "skipped"


# ── 兼容性：旧字段不回退 ──────────────────────────────────────────

def test_workflow_steps_does_not_break_legacy_fields(tmp_path: Path) -> None:
    """新增 workflow_steps 后，旧字段必须保留。"""
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="个人 AI 记忆工作台 资料库 记忆",
        add_to_knowledge_base=True,
    )

    serialized = serialize_workbench_auto_intake_item(result.items[0])
    # 旧字段全部保留
    for key in (
        "source_id", "source_uri", "input_type", "workflow", "status",
        "needs_user_confirmation", "title", "content_read_status",
        "structure_status", "series_status", "series_name",
        "series_confidence", "inspiration_status", "auto_organization",
        "next_step",
    ):
        assert key in serialized, f"旧字段 {key} 必须保留"
    # 新字段存在
    assert "workflow_steps" in serialized
    assert len(serialized["workflow_steps"]) == 7


# ── 端点响应包含 workflow_steps ───────────────────────────────────

def test_endpoint_response_contains_workflow_steps(tmp_path: Path) -> None:
    """端点返回的 items[].workflow_steps 可用。"""
    from core.product_core import ServeWorkbenchAutoIntakeEndpoint

    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    endpoint = ServeWorkbenchAutoIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={
            "content": "个人 AI 记忆工作台 资料库 记忆",
            "add_to_knowledge_base": True,
        },
        orchestrate=orchestrator.execute,
    )

    assert response.status_code == 201
    items = response.body["items"]
    assert len(items) == 1
    assert "workflow_steps" in items[0]
    assert len(items[0]["workflow_steps"]) == 7
