"""Phase 3 音频链路补齐：转写 → 摘要 → 标签 → 系列 → 记忆候选。

验证 orchestrator 在音频转写成功后自动串联：
1. summarize_transcript 生成摘要
2. create_memory_candidate 生成候选（默认不自动发布）
3. structure + tag + series 复用文本路径

覆盖场景：
- 音频转写成功后生成摘要
- 音频摘要后生成标签
- 音频生成记忆候选
- 低置信度系列进入待确认
- ASR Provider 不可用时任务可恢复
- 不自动发布长期记忆
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.product_core import OrchestrateWorkbenchAutoIntake
from core.product_core.audio_auto_workflow import AudioAutoWorkflowResult, AudioAutoWorkflowStep
from core.product_core.source_output_memory_candidate import SourceOutputMemoryCandidateResult
from core.product_core.transcript_summary_adapter import TranscriptSummaryResult
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _completed_audio_result(source_id: str, *, transcript_text: str = "个人 AI 记忆工作台的资料库需要长期记忆和四层结构。") -> AudioAutoWorkflowResult:
    step = AudioAutoWorkflowStep(
        name="transcribe_audio",
        status="completed",
        reason=None,
        job_id=f"job-transcribe-{source_id}",
        output_id=f"media-output-transcript-{source_id}",
        audio_asset_id=f"audio-asset-{source_id}",
    )
    return AudioAutoWorkflowResult(
        status="completed",
        workflow_id=f"audio-auto-workflow-{source_id}",
        source_id=source_id,
        project_id="default",
        steps=(step,),
        audio_asset_id=f"audio-asset-{source_id}",
        transcript_output_id=f"media-output-transcript-{source_id}",
        transcriber_status="enabled",
        transcriber_model_profile="local-whisper",
        transcriber_model_name="whisper-base",
        audio_asset_status="available",
        readiness_reason=None,
        next_step="transcript_ready",
        memory_publication="not_started",
        blocked_operations=(),
        error=None,
    )


def _blocked_audio_result(source_id: str) -> AudioAutoWorkflowResult:
    """ASR Provider 不可用。"""
    step = AudioAutoWorkflowStep(
        name="transcribe_audio",
        status="blocked",
        reason="local ASR provider disabled",
        audio_asset_id=f"audio-asset-{source_id}",
    )
    return AudioAutoWorkflowResult(
        status="blocked",
        workflow_id=f"audio-auto-workflow-{source_id}",
        source_id=source_id,
        project_id="default",
        steps=(step,),
        audio_asset_id=f"audio-asset-{source_id}",
        transcript_output_id=None,
        transcriber_status="disabled",
        transcriber_model_profile="",
        transcriber_model_name="",
        audio_asset_status="available",
        readiness_reason="local ASR provider disabled",
        next_step="enable_local_asr_provider",
        memory_publication="not_started",
        blocked_operations=("audio_asset_transcription",),
        error="local ASR provider disabled",
    )


def _completed_summary_result(source_id: str) -> TranscriptSummaryResult:
    return TranscriptSummaryResult(
        status="completed",
        job_id=f"job-summary-{source_id}",
        output_id=f"media-output-summary-{source_id}",
        source_id=source_id,
        transcript_output_id=f"media-output-transcript-{source_id}",
        provider="local-summary",
        title="音频摘要",
        chapter_count=2,
        evidence_count=3,
        output_preview="这是一段关于记忆工作台的音频摘要。",
        creates_memory_candidate=True,
        publishes_memory=False,
        error=None,
    )


def _completed_candidate_result(source_id: str) -> SourceOutputMemoryCandidateResult:
    return SourceOutputMemoryCandidateResult(
        status="candidate_created",
        project_id="default",
        source_id=source_id,
        candidate_id=f"memory-candidate-{source_id}",
        candidate_status="pending_review",
        target_layer="atom",
        evidence_kind="audio_summary",
        source_refs_display=(f"crp://default/sources/{source_id}.json",),
        memory_publication_state="candidate_created_not_published",
        review_state="pending_user_review",
        blocked_operations=(),
    )


def _make_orchestrator(
    store: JsonObjectStore,
    *,
    run_audio_auto_workflow=None,
    audio_summarize_transcript=None,
    audio_create_memory_candidate=None,
) -> OrchestrateWorkbenchAutoIntake:
    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id="default"),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda url: "<html><body><p>个人 AI 记忆工作台</p></body></html>",
        namespace_id="default",
        run_audio_auto_workflow=run_audio_auto_workflow,
        audio_summarize_transcript=audio_summarize_transcript,
        audio_create_memory_candidate=audio_create_memory_candidate,
    )


def _write_transcript_output(store: JsonObjectStore, source_id: str, text: str = "个人 AI 记忆工作台的资料库需要长期记忆和四层结构。") -> None:
    """把 transcript output 写入 media_processing_outputs，供 _organize_audio_transcript 读取。"""
    store.write(
        "media_processing_outputs",
        f"media-output-transcript-{source_id}",
        {
            "id": f"media-output-transcript-{source_id}",
            "source_id": source_id,
            "output_kind": "transcript",
            "status": "completed",
            "text": text,
            "segments": [],
            "language": "zh",
            "segment_count": 0,
            "char_count": len(text),
            "preview": text[:80],
            "provider": "local-whisper",
        },
        expected_revision=None,
    )


# ─── 测试 1：音频转写成功后生成摘要 ───


def test_audio_transcribe_success_generates_summary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    summary_calls: list[str] = []

    def run_audio(source_id: str, audio_asset_id) -> AudioAutoWorkflowResult:
        _write_transcript_output(store, source_id)
        return _completed_audio_result(source_id)

    def summarize_transcript(*, transcript_output_id: str) -> TranscriptSummaryResult:
        summary_calls.append(transcript_output_id)
        source_id = transcript_output_id.replace("media-output-transcript-", "")
        return _completed_summary_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        run_audio_auto_workflow=run_audio,
        audio_summarize_transcript=summarize_transcript,
    )

    result = orchestrator.execute(
        media_type="audio/mpeg",
        file_name="meeting.mp3",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    item = result.items[0]
    assert item.status == "completed"
    assert len(summary_calls) == 1, "summarize_transcript must be called once"
    assert summary_calls[0] == f"media-output-transcript-{item.source_id}"

    # workflow_steps 中 generate_summary 应该是 done
    steps = item.auto_organization.get("media_auto_workflow", {}).get("steps", [])
    step_names = [s["name"] for s in steps]
    assert "summarize_transcript" in step_names, "summarize_transcript step must be present"
    summary_step = next(s for s in steps if s["name"] == "summarize_transcript")
    assert summary_step["status"] == "completed"


# ─── 测试 2：音频摘要后生成标签 ───


def test_audio_summary_then_generates_tags(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def run_audio(source_id: str, audio_asset_id) -> AudioAutoWorkflowResult:
        _write_transcript_output(store, source_id)
        return _completed_audio_result(source_id)

    def summarize_transcript(*, transcript_output_id: str) -> TranscriptSummaryResult:
        source_id = transcript_output_id.replace("media-output-transcript-", "")
        return _completed_summary_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        run_audio_auto_workflow=run_audio,
        audio_summarize_transcript=summarize_transcript,
    )

    result = orchestrator.execute(
        media_type="audio/mpeg",
        file_name="meeting.mp3",
        add_to_knowledge_base=True,
    )

    item = result.items[0]
    # tag_index_status 应该是 indexed
    assert item.auto_organization.get("tag_index_status") == "indexed", \
        "tag index must be indexed after structure"
    # tags 应该非空
    tags = item.auto_organization.get("tags", [])
    assert len(tags) > 0, "tags must be extracted from transcript"

    # source_structures 应该有记录
    structure = store.read("source_structures", f"structure-{item.source_id}")
    assert structure is not None, "source_structures must be created"
    assert len(structure.get("paragraph_tags", [])) > 0

    # tag_index 集合应该有记录
    tag_index_records = store.list("tag_index")
    assert len(tag_index_records) > 0, "tag_index must have records"


# ─── 测试 3：音频生成记忆候选 ───


def test_audio_generates_memory_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate_calls: list[dict] = []

    def run_audio(source_id: str, audio_asset_id) -> AudioAutoWorkflowResult:
        _write_transcript_output(store, source_id)
        return _completed_audio_result(source_id)

    def summarize_transcript(*, transcript_output_id: str) -> TranscriptSummaryResult:
        source_id = transcript_output_id.replace("media-output-transcript-", "")
        return _completed_summary_result(source_id)

    def create_candidate(*, output_id, project_id, target_layer, candidate_type, created_at) -> SourceOutputMemoryCandidateResult:
        source_id = output_id.replace("media-output-summary-", "")
        candidate_calls.append({
            "output_id": output_id,
            "project_id": project_id,
            "target_layer": target_layer,
            "candidate_type": candidate_type,
        })
        return _completed_candidate_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        run_audio_auto_workflow=run_audio,
        audio_summarize_transcript=summarize_transcript,
        audio_create_memory_candidate=create_candidate,
    )

    result = orchestrator.execute(
        media_type="audio/mpeg",
        file_name="meeting.mp3",
        add_to_knowledge_base=True,
    )

    item = result.items[0]
    assert len(candidate_calls) == 1, "create_memory_candidate must be called once"
    assert candidate_calls[0]["candidate_type"] == "audio_summary"
    assert candidate_calls[0]["target_layer"] == "atom"

    # memory_publication_state 应该是 candidate_created_not_published
    assert item.auto_organization.get("memory_publication_state") == "candidate_created_not_published"

    # workflow_steps 中 create_memory_candidate 应该是 done
    steps = item.auto_organization.get("media_auto_workflow", {}).get("steps", [])
    cand_step = next((s for s in steps if s["name"] == "create_memory_candidate"), None)
    assert cand_step is not None, "create_memory_candidate step must be present"
    assert cand_step["status"] == "candidate_created"


# ─── 测试 4：低置信度系列进入待确认 ───


def test_audio_low_confidence_series_enters_confirm_required(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # 使用不含系列关键词的文本，触发低置信度
    low_confidence_text = "今天天气不错，去公园散步了。"

    def run_audio(source_id: str, audio_asset_id) -> AudioAutoWorkflowResult:
        _write_transcript_output(store, source_id, text=low_confidence_text)
        return _completed_audio_result(source_id)

    def summarize_transcript(*, transcript_output_id: str) -> TranscriptSummaryResult:
        source_id = transcript_output_id.replace("media-output-transcript-", "")
        return _completed_summary_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        run_audio_auto_workflow=run_audio,
        audio_summarize_transcript=summarize_transcript,
    )

    result = orchestrator.execute(
        media_type="audio/mpeg",
        file_name="meeting.mp3",
        add_to_knowledge_base=True,
    )

    item = result.items[0]
    # 系列状态应该是 pending_confirmation
    assert item.series_status == "pending_confirmation", \
        f"low confidence series must be pending_confirmation, got {item.series_status}"
    # item status 应该是 completed_pending_series，不是 failed
    assert item.status == "completed_pending_series", \
        f"item must be completed_pending_series, got {item.status}"
    # 摘要、标签、候选仍然可用
    assert item.auto_organization.get("tag_index_status") == "indexed"
    assert item.auto_organization.get("summary"), "summary must still be available"

    # workflow_steps 中 assign_series 应该是 confirm_required
    serialized = __import__("core.product_core.workbench_auto_intake", fromlist=["serialize_workbench_auto_intake_item"]).serialize_workbench_auto_intake_item(item)
    series_step = next(s for s in serialized["workflow_steps"] if s["id"] == "assign_series")
    assert series_step["status"] == "confirm_required"
    assert series_step["recoverable"] is True
    assert series_step["retry_action"] == "confirm_series"


# ─── 测试 5：ASR Provider 不可用时任务可恢复 ───


def test_audio_asr_unavailable_keeps_recoverable_status(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def run_audio(source_id: str, audio_asset_id) -> AudioAutoWorkflowResult:
        return _blocked_audio_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        run_audio_auto_workflow=run_audio,
        audio_summarize_transcript=lambda **kw: None,
    )

    result = orchestrator.execute(
        media_type="audio/mpeg",
        file_name="meeting.mp3",
        add_to_knowledge_base=True,
    )

    item = result.items[0]
    # 保持 needs_asr 状态，不升级为 completed
    assert item.status == "needs_asr", \
        f"blocked ASR must keep needs_asr status, got {item.status}"
    assert item.next_step == "await_asr_provider"

    # workflow_steps 中 extract_content 应该是 failed + recoverable
    serialized = __import__("core.product_core.workbench_auto_intake", fromlist=["serialize_workbench_auto_intake_item"]).serialize_workbench_auto_intake_item(item)
    extract_step = next(s for s in serialized["workflow_steps"] if s["id"] == "extract_content")
    assert extract_step["status"] == "failed", \
        f"extract_content must be failed when ASR blocked, got {extract_step['status']}"
    assert extract_step["recoverable"] is True
    assert extract_step["retry_action"] == "enable_provider"

    # 原始资料仍然保存
    assert item.source_id, "source must still be saved even if ASR fails"
    source = store.read("sources", item.source_id)
    assert source is not None, "source record must persist"


# ─── 测试 6：不自动发布长期记忆 ───


def test_audio_does_not_auto_publish_long_term_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def run_audio(source_id: str, audio_asset_id) -> AudioAutoWorkflowResult:
        _write_transcript_output(store, source_id)
        return _completed_audio_result(source_id)

    def summarize_transcript(*, transcript_output_id: str) -> TranscriptSummaryResult:
        source_id = transcript_output_id.replace("media-output-transcript-", "")
        return _completed_summary_result(source_id)

    def create_candidate(*, output_id, project_id, target_layer, candidate_type, created_at) -> SourceOutputMemoryCandidateResult:
        source_id = output_id.replace("media-output-summary-", "")
        # 写入 memory_candidates 记录，模拟真实 use case 的持久化行为
        candidate_id = f"memory-candidate-{source_id}"
        store.write(
            "memory_candidates",
            candidate_id,
            {
                "id": candidate_id,
                "source_id": source_id,
                "project_id": project_id,
                "target_layer": target_layer,
                "candidate_type": candidate_type,
                "status": "pending_review",
                "memory_publication_state": "candidate_created_not_published",
                "created_at": created_at,
            },
            expected_revision=None,
        )
        return _completed_candidate_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        run_audio_auto_workflow=run_audio,
        audio_summarize_transcript=summarize_transcript,
        audio_create_memory_candidate=create_candidate,
    )

    result = orchestrator.execute(
        media_type="audio/mpeg",
        file_name="meeting.mp3",
        add_to_knowledge_base=True,
    )

    item = result.items[0]
    # memory_publication_state 应该是 candidate_created_not_published，不是 published
    assert item.auto_organization.get("memory_publication_state") == "candidate_created_not_published"

    # memory_atoms 集合不应该有记录（未发布）
    memory_atoms = store.list("memory_atoms")
    assert len(memory_atoms) == 0, "memory_atoms must be empty (no auto-publish)"

    # memory_candidates 集合应该有记录（候选已创建）
    memory_candidates = store.list("memory_candidates")
    assert len(memory_candidates) > 0, "memory_candidates must have records"

    # workflow_steps 中 update_project_brain 应该是 skipped
    serialized = __import__("core.product_core.workbench_auto_intake", fromlist=["serialize_workbench_auto_intake_item"]).serialize_workbench_auto_intake_item(item)
    brain_step = next(s for s in serialized["workflow_steps"] if s["id"] == "update_project_brain")
    assert brain_step["status"] == "skipped", \
        f"update_project_brain must be skipped (no auto-publish), got {brain_step['status']}"
