"""Immutable derived transcript that excludes only explicit advertisement blocks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re


TRANSCRIPT_AD_FILTER_VERSION = "transcript-ad-filter-v1"
TRANSCRIPT_AD_FILTER_PROMPT_CLAUSE = (
    "广告过滤规则：广告口播、赞助推广、优惠码、购买引流、无关片头片尾导流不得进入摘要、章节、关键词、"
    "行动项、知识卡或记忆候选。若品牌、产品或赞助信息本身是主题事实，或与正文事实混杂，只保留可由原始"
    "时间戳核验的主题事实；无法确定是否为广告的内容必须保留并标为不确定，禁止猜测性删除。原始转写是"
    "不可修改的证据，任何过滤只作用于派生整理结果。"
)


_STRONG_AD_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"本期(?:视频|节目|内容).{0,24}(?:由|感谢).{0,50}(?:赞助|支持|冠名)",
        r"(?:广告|推广)时间",
        r"(?:优惠码|优惠口令|专属口令)\s*[:：]?[A-Za-z0-9\u4e00-\u9fff_-]{1,32}",
        r"扫描.{0,16}二维码.{0,30}(?:领取|购买|下载|关注)",
        r"点击.{0,20}(?:下方|评论区|简介).{0,24}链接.{0,30}(?:购买|领取|下载)",
        r"商务合作.{0,20}(?:联系|邮箱|微信)",
        r"下载.{0,30}(?:APP|应用).{0,30}(?:领取|获得|优惠|红包)",
    )
)
_WEAK_AD_MARKERS = (
    "赞助", "优惠", "折扣", "限时", "购买", "下单", "领取", "链接",
    "关注公众号", "一键三连", "点赞投币收藏", "商务合作", "推广",
)


@dataclass(frozen=True, slots=True)
class AdFilteredTranscriptResult:
    job_id: str
    output_id: str
    output_ref: str
    excluded_count: int
    uncertain_count: int


class CreateAdFilteredTranscript:
    """Create a conservative content-only derivative and preserve the raw source."""

    def __init__(self, object_store: object, *, namespace_id: str) -> None:
        self._store = object_store
        self._namespace_id = namespace_id

    def execute(self, *, transcript_output_id: str) -> AdFilteredTranscriptResult:
        transcript = self._store.read("media_processing_outputs", transcript_output_id)
        if (
            not isinstance(transcript, Mapping)
            or transcript.get("status") != "completed"
            or transcript.get("output_kind") != "transcript"
        ):
            raise ValueError("completed raw transcript output is required")
        source_id = _required(transcript, "source_id")
        raw_text = _required(transcript, "text")
        classified = _classify_transcript(transcript, raw_text)
        kept = [item for item in classified if item["classification"] != "advertisement"]
        content_text = _content_text(kept)
        excluded = [item for item in classified if item["classification"] == "advertisement"]
        uncertain = [item for item in classified if item["classification"] == "uncertain"]
        suffix = TRANSCRIPT_AD_FILTER_VERSION.replace("transcript-", "").replace("-", "_")
        job_id = f"media-job-content-transcript-{suffix}-{source_id}"
        output_id = f"media-output-content-transcript-{suffix}-{source_id}"
        output_ref = f"crp://{self._namespace_id}/media-processing-outputs/{output_id}.json"
        created_at = str(transcript.get("created_at") or _utc_now())
        transcript_ref = _required(transcript, "ref")
        job = {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "source_type": str(transcript.get("source_type") or "video"),
            "status": "completed",
            "pipeline": "conservative_transcript_ad_filter",
            "input_refs": [transcript_ref],
            "output_refs": [output_ref],
            "error": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        output = {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source_id,
            "source_type": str(transcript.get("source_type") or "video"),
            "output_kind": "content_transcript",
            "status": "completed",
            "provider": "builtin-conservative-ad-filter",
            "title": str(transcript.get("title") or "内容转写"),
            "preview": _plain_text(content_text)[:600],
            "text": content_text,
            "segments": [
                {
                    "start_seconds": item["start_seconds"],
                    "end_seconds": item["end_seconds"],
                    "text": item["text"],
                    "ad_classification": item["classification"],
                }
                for item in kept
            ],
            "metadata": {
                "local_processing": True,
                "remote_processing": False,
                "original_transcript_output_id": transcript_output_id,
                "original_transcript_ref": transcript_ref,
                "ad_filter_version": TRANSCRIPT_AD_FILTER_VERSION,
                "ad_filter_policy": "explicit_ads_only_uncertain_retained",
                "content_block_count": len(kept),
                "excluded_ad_count": len(excluded),
                "uncertain_ad_count": len(uncertain),
                "excluded_segments": [
                    {
                        "start_seconds": item["start_seconds"],
                        "end_seconds": item["end_seconds"],
                        "reason": item["reason"],
                        "evidence_quote": _plain_text(str(item["text"]))[:160],
                    }
                    for item in excluded
                ],
            },
            "memory_publication": "not_started",
            "created_at": created_at,
            "ref": output_ref,
        }
        _write_or_verify(self._store, "media_processing_jobs", job_id, job)
        _write_or_verify(self._store, "media_processing_outputs", output_id, output)
        return AdFilteredTranscriptResult(
            job_id, output_id, output_ref, len(excluded), len(uncertain)
        )


def _classify_transcript(
    transcript: Mapping[str, object], raw_text: str,
) -> list[dict[str, object]]:
    raw_segments = transcript.get("segments")
    blocks: list[dict[str, object]] = []
    if isinstance(raw_segments, list) and raw_segments:
        for segment in raw_segments:
            if not isinstance(segment, Mapping) or not isinstance(segment.get("text"), str):
                continue
            text = segment["text"].strip()
            if text:
                blocks.append({
                    "start_seconds": _optional_seconds(segment.get("start_seconds")),
                    "end_seconds": _optional_seconds(segment.get("end_seconds")),
                    "text": text,
                })
    if not blocks:
        for text in re.split(r"\n\s*\n+|(?<=[。！？!?])(?=\S)", raw_text):
            if text.strip():
                blocks.append({"start_seconds": None, "end_seconds": None, "text": text.strip()})
    for block in blocks:
        classification, reason = _classify_block(str(block["text"]))
        block["classification"] = classification
        block["reason"] = reason
    return blocks


def _classify_block(text: str) -> tuple[str, str]:
    if any(pattern.search(text) for pattern in _STRONG_AD_PATTERNS):
        return "advertisement", "explicit_promotional_call_to_action"
    marker_count = sum(marker in text for marker in _WEAK_AD_MARKERS)
    if marker_count >= 2:
        return "uncertain", "possible_promotion_retained_for_review"
    return "content", "topic_content"


def _content_text(blocks: list[Mapping[str, object]]) -> str:
    return "\n\n".join(str(block["text"]).strip() for block in blocks if str(block["text"]).strip())


def _optional_seconds(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _write_or_verify(
    store: object, collection: str, object_id: str, expected: Mapping[str, object]
) -> None:
    existing = store.read(collection, object_id)
    if existing is None:
        store.write(collection, object_id, dict(expected), expected_revision=None)
        return
    if dict(existing) != dict(expected):
        raise ValueError(f"existing {collection} output identity drifted")


def _plain_text(value: str) -> str:
    text = re.sub(r"[`#>*_\[\]()]", " ", value)
    return re.sub(r"\s+", " ", text).strip()


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be non-empty")
    return item


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
