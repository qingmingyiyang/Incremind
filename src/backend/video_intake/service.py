from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from backend.shared.filesystem import atomic_write_text
from backend.shared.llm import RequestScopedWireAttemptRecorder
from backend.security import (
    ProviderEgressPolicyStore, SecretEgressBroker, build_active_provider_egress_guard,
    build_secret_store,
)
from backend.video_intake.bilibili import BilibiliClient
from backend.video_intake.models import (
    IntakeTask,
    LibraryRecord,
    RecordDetail,
    ResolvedSource,
    StartImportRequest,
    utc_now_iso,
)
from backend.video_intake.prompts import (
    VIDEO_INTAKE_QA_TIMEOUT_SECONDS,
    build_video_question_messages,
)
from backend.video_intake.storage import DATA_DIR, LibraryStorage
from backend.video_intake.structured import retrieve_chunks, write_structured_document
from backend.video_intake.transcript import (
    find_preferred_subtitle,
    parse_subtitle,
    render_transcript_markdown,
    transcript_payload,
    write_transcript_json,
)
from backend.video_intake.visual import (
    assess_visual_importance,
    extract_visual_candidates,
    remove_visual_probe,
)
from backend.video_intake.vision import analyze_and_merge_visuals, load_vision_settings, render_visual_markdown
from backend.video_summary.domain.models import Transcript, TranscriptSegment, VideoAsset
from backend.video_summary.infrastructure.filesystem_generation_artifact_store import (
    FileSystemGenerationArtifactStore,
)
from backend.video_summary.infrastructure.litellm_transcript_enhancer import LiteLLMTranscriptEnhancer
from backend.video_summary.infrastructure.settings import load_settings
from backend.video_summary.infrastructure.video_summary_runtime import (
    build_litellm_completion_gateway,
    build_video_summary_runtime,
)
from backend.video_summary.infrastructure.video_summary_workflow import ConfiguredVideoSummaryWorkflow


class IntakeService:
    TASKS_MAX_SIZE = 200  # L1(up to 200 intake tasks per series, up to about 400 KB each)

    def __init__(self, root_dir: Path, series_id: str = "default") -> None:
        self.root_dir = root_dir
        self.series_id = series_id
        self.storage = LibraryStorage(root_dir, series_id)
        self.client = BilibiliClient(root_dir)
        self.workflow = ConfiguredVideoSummaryWorkflow(root_dir)
        self.tasks: dict[str, IntakeTask] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self.storage.ensure_root()

    async def resolve(self, url: str) -> ResolvedSource:
        return await self.client.resolve(url.strip())

    async def start(self, request: StartImportRequest) -> IntakeTask:
        if request.series_id != self.series_id:
            raise ValueError("视频任务的 series_id 与当前系列不一致。")
        resolved = await self.resolve(request.url)
        selected = (
            [item for item in resolved.items if item.key in set(request.selected_keys)]
            if request.selected_keys
            else resolved.items[:1]
        )
        if not selected:
            raise ValueError("至少选择一个视频。")
        task = IntakeTask(
            series_id=self.series_id,
            url=request.url,
            selected_count=len(selected),
            media_mode=request.media_mode,
            visual_analysis=request.visual_analysis,
        )
        # R103: LRU eviction cap to avoid unbounded cache growth (R96 🟢#7)
        if len(self.tasks) >= self.TASKS_MAX_SIZE:
            self.tasks.popitem(last=False)
        self.tasks[task.id] = task
        background = asyncio.create_task(self._run_task(task, selected, request))
        self._background_tasks.add(background)
        background.add_done_callback(self._background_tasks.discard)
        return task

    def list_tasks(self) -> list[IntakeTask]:
        return sorted(self.tasks.values(), key=lambda item: item.created_at, reverse=True)

    def get_task(self, task_id: str) -> IntakeTask | None:
        return self.tasks.get(task_id)

    def list_records(self) -> list[LibraryRecord]:
        return self.storage.list_records()

    def detail(self, record_id: str) -> RecordDetail | None:
        record = self.storage.find_record(record_id)
        if record is None:
            return None
        record_dir = self.storage.record_dir(record)
        data_dir = record_dir / DATA_DIR
        summary = _read_json(data_dir / "summary.json")
        transcript = _read_json(data_dir / "transcript.cleaned.json")
        transcripts = {
            "official_subtitle": payload
            for payload in [_read_json(data_dir / "transcript.official.json")]
            if payload is not None
        }
        asr_payload = _read_json(data_dir / "transcript.asr.json") or _read_json(
            data_dir / ".cache" / "whisper" / "transcript.raw.json"
        )
        if asr_payload is not None:
            transcripts["asr_transcript"] = asr_payload
        if transcript is not None:
            transcripts["cleaned_transcript"] = transcript
        structured = _read_json(data_dir / "structured.json")
        return RecordDetail(
            record=record,
            summary=summary,
            transcript=transcript,
            transcripts=transcripts,
            structured=structured,
            notes=self.storage.read_notes(record),
        )

    async def ask(self, record_id: str, question: str) -> tuple[str, list[dict[str, object]]]:
        detail = self.detail(record_id)
        if detail is None:
            raise LookupError(record_id)
        if not detail.transcript:
            raise ValueError("该视频还没有可用于问答的转写。")
        context = _build_question_context(detail, question)
        references = _build_question_references(detail, question)
        settings = load_settings(self.root_dir / "config" / "settings.toml", self.root_dir)
        gateway = build_litellm_completion_gateway(
            settings,
            egress_guard=build_active_provider_egress_guard(
                self.root_dir,
                endpoint=settings.openai.base_url,
            ),
        )
        recorder = _new_wire_attempt_recorder(
            stage="question",
            model_identity=_model_identity(settings, gateway),
        )
        try:
            answer = await gateway.acomplete_text(
                build_video_question_messages(question=question.strip(), context=context),
                temperature=0,
                max_tokens=3000,
                timeout=VIDEO_INTAKE_QA_TIMEOUT_SECONDS,
                wire_attempt_sink=recorder,
            )
            _validate_answer_citations(answer, context)
            return answer, references
        finally:
            try:
                data_dir = self.storage.record_dir(detail.record) / DATA_DIR
            except Exception:
                # The answer path retains its original behavior if legacy
                # record metadata cannot be resolved for diagnostics.
                pass
            else:
                await _persist_wire_attempts_best_effort(data_dir=data_dir, recorders=(recorder,))

    async def _run_task(self, task: IntakeTask, items: list, request: StartImportRequest) -> None:
        task.status = "running"
        task.updated_at = utc_now_iso()
        try:
            for index, item in enumerate(items):
                record, record_dir = self.storage.create_record(item, media_mode=request.media_mode)
                if record.id not in task.record_ids:
                    task.record_ids.append(record.id)
                try:
                    await self._process_record(
                        task,
                        record,
                        record_dir,
                        item,
                        request,
                        index=index,
                        total=len(items),
                    )
                except Exception as error:
                    self.storage.fail(record, str(error))
                    raise
                task.completed_count += 1
            task.status = "completed"
            task.stage = "completed"
            task.progress = 100.0
            task.detail = f"已完成 {task.completed_count} 个视频"
        except Exception as error:
            task.status = "failed"
            task.stage = "failed"
            task.error = str(error)
            task.detail = str(error)
        finally:
            task.updated_at = utc_now_iso()

    async def _process_record(
        self,
        task: IntakeTask,
        record: LibraryRecord,
        record_dir: Path,
        item,
        request: StartImportRequest,
        *,
        index: int,
        total: int,
    ) -> None:
        def report(local_progress: float, detail: str, stage: str = "download") -> None:
            overall = ((index + max(0.0, min(100.0, local_progress)) / 100.0) / total) * 100.0
            task.progress = round(overall, 1)
            task.stage = stage
            task.detail = detail
            task.updated_at = utc_now_iso()
            self.storage.mark_progress(record, stage=stage, progress=local_progress)

        report(2.0, "正在准备存储目录", "preparing")
        media_path = await self.client.download(
            item,
            record_dir,
            media_mode=request.media_mode,
            on_progress=lambda progress, detail: report(5.0 + progress * 0.30, detail, "download"),
        )
        record.media_file = media_path.relative_to(record_dir).as_posix()
        _enrich_record_from_info(record, record_dir)
        cover = _find_cover(record_dir / "media")
        record.cover_file = cover.relative_to(record_dir).as_posix() if cover else ""
        self.storage.save_record(record)

        probe_path = None
        ffmpeg_path = _ffmpeg_path(self.root_dir)
        try:
            if request.visual_analysis and request.media_mode == "audio":
                report(38.0, "正在抽取低清画面样本", "visual_probe")
                probe_path = await self.client.download_visual_probe(item, record_dir)
            record.visual = await asyncio.to_thread(
                assess_visual_importance,
                title=record.title,
                description=record.description,
                tags=record.tags,
                probe_path=probe_path,
                ffmpeg_path=ffmpeg_path,
            )
            analysis_media = media_path if request.media_mode == "video" else probe_path
            if request.visual_analysis and analysis_media is not None:
                report(42.0, "正在筛选低频样本和场景变化关键帧", "keyframes")
                sampling = await asyncio.to_thread(
                    extract_visual_candidates,
                    analysis_media,
                    record_dir,
                    duration_seconds=record.duration_seconds,
                    ffmpeg_path=ffmpeg_path,
                )
                record.visual.keyframe_count = len(sampling.frames)
        except Exception as error:
            record.visual.importance = "unknown"
            record.visual.reason = "画面筛选失败，音频、转写和总结已继续处理。"
            record.visual.signals = [f"visual_local_error: {type(error).__name__}"]
        finally:
            remove_visual_probe(probe_path)
        self.storage.save_record(record)

        data_dir = record_dir / DATA_DIR
        subtitle_path = find_preferred_subtitle(record_dir / "media")
        metadata = _summary_metadata(record)
        if subtitle_path is not None:
            report(48.0, "发现官方字幕，正在整理时间轴", "subtitle")
            try:
                await self._generate_from_subtitle(
                    record,
                    media_path,
                    data_dir,
                    subtitle_path,
                    metadata,
                    enhance=request.transcript_enhancement_enabled,
                    report=report,
                )
            except ValueError:
                await self._generate_from_asr(
                    record,
                    media_path,
                    data_dir,
                    metadata,
                    enhance=request.transcript_enhancement_enabled,
                    report=report,
                )
        else:
            await self._generate_from_asr(
                record,
                media_path,
                data_dir,
                metadata,
                enhance=request.transcript_enhancement_enabled,
                report=report,
            )

        structured = write_structured_document(record, data_dir)
        report(97.0, "正在合并画面证据", "vision")
        vision_settings = load_vision_settings(self.root_dir)
        structured = await analyze_and_merge_visuals(
            record_dir=record_dir,
            structured=structured,
            settings=vision_settings,
            requested=request.visual_analysis,
            egress_guard=self._vision_egress_guard(vision_settings),
            authorization_header_provider=self._vision_authorization_headers(vision_settings),
        )
        _write_human_files(record, record_dir)
        report(100.0, f"{record.title} 已整理完成", "completed")
        self.storage.complete(record)

    def _vision_egress_guard(self, settings):
        policy = ProviderEgressPolicyStore(self.root_dir)
        manifest = policy.manifest(
            provider_id="vision",
            endpoint=settings.base_url or "http://127.0.0.1/disabled",
            purposes=("vision_analysis",),
            payload_categories=("image_frame", "instructions", "source_excerpt"),
            max_payload_bytes=16 * 1024 * 1024,
        )

        def guard(payload_bytes: int):
            return policy.authorize(
                manifest,
                purpose="vision_analysis",
                payload_categories=("image_frame", "instructions", "source_excerpt"),
                payload_bytes=payload_bytes,
            )

        return guard

    def _vision_authorization_headers(self, settings):
        policy = ProviderEgressPolicyStore(self.root_dir)
        manifest = policy.manifest(
            provider_id="vision",
            endpoint=settings.base_url or "http://127.0.0.1/disabled",
            purposes=("vision_analysis",),
            payload_categories=("image_frame", "instructions", "source_excerpt"),
            max_payload_bytes=16 * 1024 * 1024,
        )
        broker = SecretEgressBroker(
            build_secret_store(self.root_dir),
            boundary_revision_reader=lambda _project_id: manifest.manifest_id,
        )

        def provide(endpoint: str):
            lease = broker.grant(
                project_id="legacy-video-intake", secret_ref="provider:vision",
                purpose="vision_analysis", allowed_hosts=(urlsplit(endpoint).hostname or "",),
                boundary_revision=manifest.manifest_id,
            )
            return broker.inject_header(
                lease, project_id="legacy-video-intake", purpose="vision_analysis",
                boundary_revision=manifest.manifest_id, url=endpoint,
                header_name="Authorization", prefix="Bearer ",
            )

        return provide

    async def _generate_from_subtitle(
        self,
        record: LibraryRecord,
        media_path: Path,
        data_dir: Path,
        subtitle_path: Path,
        metadata: dict[str, object],
        *,
        enhance: bool,
        report,
    ) -> None:
        transcript = parse_subtitle(subtitle_path)
        record.transcript_source = "official_subtitle"
        record.subtitle_language = transcript.language
        write_transcript_json(
            data_dir / "transcript.official.json",
            title=record.title,
            duration=record.duration_seconds,
            transcript=transcript,
            source="official_subtitle",
        )
        video = VideoAsset(
            source_path=media_path,
            title=record.title,
            duration_seconds=record.duration_seconds,
            metadata=metadata,
        )
        settings = load_settings(self.root_dir / "config" / "settings.toml", self.root_dir)
        runtime = build_video_summary_runtime(
            settings,
            egress_guard=build_active_provider_egress_guard(
                self.root_dir,
                endpoint=settings.openai.base_url,
            ),
        )
        request_id = _new_wire_attempt_request_id()
        summary_recorder = RequestScopedWireAttemptRecorder(
            request_id=request_id,
            stage="summary",
            model_identity=_model_identity(settings, runtime.gateway),
        )
        enhance_recorder = (
            RequestScopedWireAttemptRecorder(
                request_id=request_id,
                stage="transcript_enhance",
                model_identity=_model_identity(settings, runtime.gateway),
            )
            if enhance
            else None
        )
        recorders = tuple(item for item in (enhance_recorder, summary_recorder) if item is not None)
        try:
            cleaned = transcript
            if enhance:
                report(62.0, "正在修正字幕中的断句与专有名词", "enhance_transcript")
                cleaned = await LiteLLMTranscriptEnhancer(runtime.gateway).enhance(
                    video,
                    transcript,
                    wire_attempt_sink=enhance_recorder,
                )
            write_transcript_json(
                data_dir / "transcript.cleaned.json",
                title=record.title,
                duration=record.duration_seconds,
                transcript=cleaned,
                source="official_subtitle" if not enhance else "official_subtitle+ai_cleanup",
            )
            report(80.0, "正在生成证据化内容整理", "summarize")
            document = await runtime.summarizer.summarize(
                video,
                cleaned,
                wire_attempt_sink=summary_recorder,
            )
            await FileSystemGenerationArtifactStore().save_summary_document(document=document, output_dir=data_dir)
            self.storage.save_record(record)
        finally:
            await _persist_wire_attempts_best_effort(data_dir=data_dir, recorders=recorders)

    async def _generate_from_asr(
        self,
        record: LibraryRecord,
        media_path: Path,
        data_dir: Path,
        metadata: dict[str, object],
        *,
        enhance: bool,
        report,
    ) -> None:
        record.transcript_source = "whisper"
        self.storage.save_record(record)
        reporter = _WorkflowProgressReporter(report)
        await self.workflow.run(
            media_path,
            data_dir,
            progress_reporter=reporter,
            transcript_enhancement_enabled=enhance,
            video_metadata=metadata,
            video_title=record.title,
        )
        cleaned_path = data_dir / "transcript.cleaned.json"
        payload = _read_json(cleaned_path)
        if payload is not None:
            payload["source"] = "whisper+ai_cleanup" if enhance else "whisper"
            atomic_write_text(cleaned_path, json.dumps(payload, ensure_ascii=False, indent=2))
        raw_path = data_dir / ".cache" / "whisper" / "transcript.raw.json"
        if raw_path.exists():
            raw_payload = _read_json(raw_path)
            if raw_payload is not None:
                raw_payload["title"] = record.title
                raw_payload["duration_seconds"] = record.duration_seconds
                raw_payload["source"] = "whisper_raw"
                atomic_write_text(
                    data_dir / "transcript.asr.json",
                    json.dumps(raw_payload, ensure_ascii=False, indent=2),
                )


def _new_wire_attempt_request_id() -> str:
    """Return a filesystem-safe, per-invocation diagnostic request identity."""
    return f"video-intake-{uuid4().hex}"


def _new_wire_attempt_recorder(
    *,
    stage: str,
    model_identity: str,
) -> RequestScopedWireAttemptRecorder:
    return RequestScopedWireAttemptRecorder(
        request_id=_new_wire_attempt_request_id(),
        stage=stage,
        model_identity=model_identity,
    )


def _model_identity(settings, gateway: object) -> str:
    """Keep diagnostic metadata free of endpoint paths and credential material."""
    openai = getattr(settings, "openai", None)
    provider = str(getattr(openai, "provider", "")).strip()
    model = str(getattr(openai, "model", "")).strip()
    if provider and model:
        return f"{provider}:{model}"
    return f"{type(gateway).__module__}.{type(gateway).__qualname__}"


async def _persist_wire_attempts_best_effort(
    *,
    data_dir: Path,
    recorders: tuple[RequestScopedWireAttemptRecorder, ...],
) -> None:
    """Persist non-authoritative diagnostics without changing an intake result.

    Each invocation and stage receives its own atomically-written file.  The
    records are operational observations only: recovery and task state remain
    owned by the existing intake record and workflow artifacts.
    """
    for recorder in recorders:
        records = recorder.records
        if not records:
            continue
        payload = {
            "schema_version": "1.0.0",
            "kind": "non_authoritative_wire_attempt_observation",
            "request_id": recorder.request_id,
            "stage": recorder.stage,
            "records": [record.to_dict() for record in records],
        }
        destination = data_dir / "wire-attempts" / recorder.request_id / f"{recorder.stage}.json"
        try:
            await asyncio.to_thread(
                atomic_write_text,
                destination,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception:
            # Diagnostics must never turn a successful intake into a failure.
            continue


class _WorkflowProgressReporter:
    def __init__(self, report) -> None:
        self._report = report

    def update(self, stage: str, progress: float | None = None, detail: str | None = None) -> None:
        source_progress = 45.0 if progress is None else float(progress)
        mapped = 45.0 + source_progress * 0.5
        self._report(mapped, detail or stage, stage)

    def completed(self, detail: str | None = None) -> None:
        self._report(98.0, detail or "内容整理完成", "finalize")

    def failed(self, message: str) -> None:
        raise RuntimeError(message)

    def cancelled(self, detail: str | None = None) -> None:
        raise RuntimeError(detail or "任务已取消")

    def is_cancel_requested(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


def _enrich_record_from_info(record: LibraryRecord, record_dir: Path) -> None:
    info = _read_json(record_dir / "media" / "source.info.json")
    if not info:
        return
    record.title = str(info.get("title") or record.title).strip()
    record.uploader = str(info.get("uploader") or record.uploader).strip()
    record.description = str(info.get("description") or record.description).strip()
    record.duration_seconds = float(info.get("duration") or record.duration_seconds or 0.0)
    upload_date = str(info.get("upload_date") or "")
    if len(upload_date) == 8 and upload_date.isdigit():
        record.published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    tags = info.get("tags")
    if isinstance(tags, list):
        record.tags = [str(tag).strip() for tag in tags if str(tag).strip()]


def _summary_metadata(record: LibraryRecord) -> dict[str, object]:
    return {
        "source_url": record.source_url,
        "bvid": record.bvid,
        "page": record.page,
        "uploader": record.uploader,
        "published_at": record.published_at,
        "description": record.description,
        "tags": record.tags,
        "transcript_source": record.transcript_source,
        "visual_assessment": record.visual.model_dump(mode="json"),
    }


def _write_human_files(record: LibraryRecord, record_dir: Path) -> None:
    data_dir = record_dir / DATA_DIR
    summary_path = data_dir / "summary.md"
    if summary_path.exists():
        summary_text = summary_path.read_text(encoding="utf-8").rstrip() + "\n\n"
        structured = _read_json(data_dir / "structured.json")
        visual_markdown = render_visual_markdown(structured or {})
        atomic_write_text(record_dir / record.summary_file, summary_text + visual_markdown)
    transcript = _read_json(data_dir / "transcript.cleaned.json")
    if transcript:
        atomic_write_text(record_dir / record.transcript_file, render_transcript_markdown(transcript))


def _find_cover(media_dir: Path) -> Path | None:
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".avif"):
        candidate = media_dir / f"source{suffix}"
        if candidate.exists():
            return candidate
    return next((path for path in media_dir.glob("source.*") if path.suffix.lower() in {".jpg", ".png", ".webp"}), None)


def _ffmpeg_path(root_dir: Path) -> Path | None:
    for candidate in (
        root_dir / "runtime" / "Library" / "bin" / "ffmpeg.exe",
        root_dir / "runtime" / "ffmpeg.exe",
    ):
        if candidate.exists():
            return candidate
    return None


def _read_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _build_question_context(detail: RecordDetail, question: str) -> str:
    if detail.structured:
        selected_chunks = retrieve_chunks(detail.structured, question, limit=16)
        lines = [
            f"[{item.get('chunk_id', '')}] [{_format_context_time(item.get('start'))}–{_format_context_time(item.get('end'))}] "
            f"{item.get('text', '')}"
            for item in selected_chunks
        ]
        return (
            "结构化总结：\n"
            + json.dumps(detail.structured.get("summary", {}), ensure_ascii=False)
            + "\n\n相关证据分块：\n"
            + "\n".join(lines)
        )
    summary_text = json.dumps(detail.summary or {}, ensure_ascii=False)
    segments = (detail.transcript or {}).get("segments", [])
    scored: list[tuple[int, dict[str, object]]] = []
    tokens = {token for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", question.lower())}
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text", ""))
        score = sum(1 for token in tokens if token in text.lower())
        scored.append((score, segment))
    selected = [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:80]]
    selected.sort(key=lambda item: float(item.get("start_seconds", 0.0)))
    lines = []
    for item in selected:
        seconds = int(float(item.get("start_seconds", 0.0)))
        lines.append(f"[{seconds // 60:02d}:{seconds % 60:02d}] {item.get('text', '')}")
    return f"结构化总结：\n{summary_text[:30000]}\n\n相关转写：\n" + "\n".join(lines)


def _build_question_references(detail: RecordDetail, question: str) -> list[dict[str, object]]:
    if not detail.structured:
        return []
    chunks = retrieve_chunks(detail.structured, question, limit=8)
    visual = detail.structured.get("visual_analysis")
    frames = visual.get("keyframes", []) if isinstance(visual, dict) else []
    timeline = detail.structured.get("timeline")
    references: list[dict[str, object]] = []
    for chunk in chunks:
        start = float(chunk.get("start") or 0.0)
        end = float(chunk.get("end") or start)
        frame_ids = {
            str(frame.get("frame_id"))
            for frame in frames
            if isinstance(frame, dict) and start - 3 <= float(frame.get("timestamp") or 0.0) <= end + 3
        }
        for chapter in timeline if isinstance(timeline, list) else []:
            if not isinstance(chapter, dict):
                continue
            if float(chapter.get("end") or 0.0) >= start and float(chapter.get("start") or 0.0) <= end:
                frame_ids.update(str(value) for value in chapter.get("frame_ids", []) if value)
        references.append(
            {
                "chunk_id": str(chunk.get("chunk_id") or ""),
                "start": start,
                "end": end,
                "timestamp": f"{_format_context_time(start)}–{_format_context_time(end)}",
                "evidence_text": str(chunk.get("text") or ""),
                "frame_ids": sorted(frame_ids),
            }
        )
    return references


def _format_context_time(value: object) -> str:
    seconds = max(0, int(float(value))) if isinstance(value, (int, float)) else 0
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes:02d}:{remainder:02d}"


_ANSWER_TIME_CITATION = re.compile(r"\[(\d{2,}:\d{2}(?:[-–—]\d{2,}:\d{2})?)\]")


def _validate_answer_citations(answer: str, context: str) -> None:
    """Reject model-generated timestamps that are absent from the local evidence."""

    cited = {_normalize_time_citation(value) for value in _ANSWER_TIME_CITATION.findall(answer)}
    allowed = {_normalize_time_citation(value) for value in _ANSWER_TIME_CITATION.findall(context)}
    if cited - allowed:
        raise RuntimeError("模型答案包含无法在当前视频证据中核验的时间引用。")


def _normalize_time_citation(value: str) -> str:
    return value.replace("–", "-").replace("—", "-")
