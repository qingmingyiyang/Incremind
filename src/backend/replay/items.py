from __future__ import annotations

import re
from pathlib import Path

from backend.replay.contracts import (
    KnowledgeEvidence,
    KnowledgeIndex,
    KnowledgeItem,
    KnowledgeLinks,
    KnowledgeSource,
    KnowledgeStatus,
    QuickCaptureRequest,
)
from backend.replay.library import ReplayLibrary, _keywords
from backend.video_intake.models import RecordDetail, utc_now_iso


class KnowledgeItemService:
    def __init__(self, library: ReplayLibrary) -> None:
        self.library = library

    def capture(self, request: QuickCaptureRequest) -> KnowledgeItem:
        content = request.content.strip()
        if not content:
            raise ValueError("记录内容不能为空")
        title = request.title.strip() or _derive_title(content)
        keywords = _keywords(" ".join([title, content, *request.tags]))
        item = KnowledgeItem(
            series_id=self.library.series_id or request.series_id,
            type=request.type,
            title=title,
            content=content,
            summary=_summary(content),
            tags=_unique(request.tags),
            source=KnowledgeSource(kind=request.source_kind, url=request.source_url.strip()),
            evidence=KnowledgeEvidence(
                chunk_ids=_unique(request.linked_chunk_ids),
                frame_ids=_unique(request.linked_frame_ids),
                timestamps=_unique(request.timestamps),
                quotes=_unique(request.quotes),
            ),
            links=KnowledgeLinks(linked_video_ids=_unique(request.linked_video_ids)),
            status=KnowledgeStatus(
                in_inbox=request.status == "inbox",
                in_daily=request.include_in_daily,
                archived=request.status == "archived",
            ),
            index=KnowledgeIndex(available=True, keywords=keywords, chunk_ids=_unique(request.linked_chunk_ids)),
        )
        return self.library.save_item(item)

    def from_video(
        self,
        detail: RecordDetail,
        *,
        include_in_daily: bool,
        high_value: bool = False,
        need_review: bool = False,
    ) -> KnowledgeItem:
        record = detail.record
        structured = detail.structured if isinstance(detail.structured, dict) else {}
        chunks = _dict_list(structured.get("chunks"))
        claims = _dict_list(structured.get("claims"))
        visual = structured.get("visual_analysis")
        visual = visual if isinstance(visual, dict) else {}
        frames = _dict_list(visual.get("keyframes"))
        structured_summary = structured.get("summary")
        structured_summary = structured_summary if isinstance(structured_summary, dict) else {}
        legacy_summary = detail.summary if isinstance(detail.summary, dict) else {}

        summary = str(
            structured_summary.get("thirty_second")
            or legacy_summary.get("thirty_second_summary")
            or legacy_summary.get("one_sentence_summary")
            or ""
        ).strip()
        content_parts = [
            str(structured_summary.get("core_question") or legacy_summary.get("core_problem") or "").strip(),
            *_strings(structured_summary.get("main_conclusions") or legacy_summary.get("key_takeaways")),
            *_strings(structured_summary.get("detailed_notes") or legacy_summary.get("detailed_notes")),
        ]
        content = "\n\n".join(part for part in content_parts if part) or summary or record.description
        chunk_ids = _unique([str(chunk.get("chunk_id") or "") for chunk in chunks])
        frame_ids = _unique([str(frame.get("frame_id") or "") for frame in frames])
        timestamps = _unique(
            [str(claim.get("timestamp") or "") for claim in claims]
            + [str(frame.get("timestamp_text") or frame.get("timestamp") or "") for frame in frames]
        )
        quotes = _unique(
            [str(claim.get("evidence_text") or claim.get("claim") or "") for claim in claims]
        )
        keywords = _unique(
            [*record.tags]
            + [str(keyword) for chunk in chunks for keyword in _strings(chunk.get("keywords"))]
        )
        local_path = _safe_video_path(self.library.root, record.relative_dir)
        item_id = _video_item_id(record.id)
        existing = self.library.get_item(item_id)
        existing_status = existing.status if existing is not None else KnowledgeStatus()
        item = KnowledgeItem(
            id=item_id,
            series_id=self.library.series_id or "default",
            type="video",
            title=record.title,
            content=content,
            summary=summary,
            tags=_unique(record.tags),
            created_at=record.imported_at,
            updated_at=utc_now_iso(),
            source=KnowledgeSource(
                kind="bilibili",
                url=record.source_url,
                bvid=record.bvid,
                local_path=str(local_path),
            ),
            evidence=KnowledgeEvidence(
                chunk_ids=chunk_ids,
                frame_ids=frame_ids,
                timestamps=timestamps,
                quotes=quotes,
            ),
            links=KnowledgeLinks(linked_video_ids=[record.id]),
            status=KnowledgeStatus(
                in_daily=include_in_daily or existing_status.in_daily,
                favorite=existing_status.favorite,
                high_value=high_value or existing_status.high_value,
                need_review=need_review or existing_status.need_review,
                archived=existing_status.archived,
            ),
            index=KnowledgeIndex(
                available=bool(chunk_ids or keywords),
                keywords=keywords,
                chunk_ids=chunk_ids,
            ),
        )
        return self.library.save_item(item)


def _derive_title(content: str) -> str:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "快速记录")
    return re.sub(r"^#+\s*", "", first_line)[:80]


def _summary(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip()[:240]


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if value and value not in result:
            result.append(value)
    return result


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _safe_video_path(library_root: Path, relative_dir: str) -> Path:
    root = library_root.resolve()
    path = (root / relative_dir).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("视频资料路径超出资料库范围") from error
    return path


def _video_item_id(record_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", record_id).strip("_")
    if not value:
        raise ValueError("视频记录 ID 无效")
    return f"video_{value}"
