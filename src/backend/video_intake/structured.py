from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from backend.shared.filesystem import atomic_write_text
from backend.video_intake.models import LibraryRecord


STRUCTURED_FILE = "structured.json"
INDEX_FILE = "index.json"
CHUNK_MAX_CHARACTERS = 900
CHUNK_MAX_SECONDS = 90.0
TOP_LEVEL_KEYS = (
    "metadata",
    "sources",
    "chunks",
    "timeline",
    "entities",
    "claims",
    "examples",
    "actions",
    "summary",
    "visual_analysis",
    "index",
    "retrieval_text",
)


def write_structured_document(record: LibraryRecord, data_dir: Path) -> dict[str, object]:
    document, search_index = build_structured_document(record, data_dir)
    atomic_write_text(data_dir / STRUCTURED_FILE, json.dumps(document, ensure_ascii=False, indent=2))
    atomic_write_text(data_dir / INDEX_FILE, json.dumps(search_index, ensure_ascii=False, indent=2))
    return document


def build_structured_document(
    record: LibraryRecord,
    data_dir: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    summary = _read_json(data_dir / "summary.json")
    official = _read_json(data_dir / "transcript.official.json")
    asr = _read_first_json(
        data_dir / "transcript.asr.json",
        data_dir / "transcript.raw.json",
        data_dir / ".cache" / "whisper" / "transcript.raw.json",
    )
    cleaned = _read_json(data_dir / "transcript.cleaned.json")
    preferred = cleaned or official or asr or {"segments": []}
    preferred_source = _source_name(preferred, official=official, asr=asr, cleaned=cleaned)
    chunks = build_chunks(preferred, source_type=preferred_source)
    evidence_lookup = {
        str(item.get("id", "")): item
        for item in _dict_list(summary.get("evidence"))
        if str(item.get("id", "")).strip()
    }
    timeline = _build_timeline(summary, chunks, evidence_lookup)
    claims = _build_claims(summary, chunks, evidence_lookup)
    examples = _build_examples(summary, chunks, evidence_lookup)
    actions = _build_actions(summary, chunks, evidence_lookup)
    visual = _default_visual_analysis(record, data_dir)
    search_index = build_search_index(chunks)
    retrieval_text = "\n".join(
        f"[{chunk['chunk_id']}] [{_timestamp(float(chunk['start']))}–{_timestamp(float(chunk['end']))}] "
        f"{chunk['text']}"
        for chunk in chunks
    )
    document: dict[str, object] = {
        "metadata": {
            "title": record.title,
            "uploader": record.uploader,
            "description": record.description,
            "publish_time": record.published_at,
            "tags": record.tags,
            "cover_url": record.cover_url,
            "source_url": record.source_url,
            "bvid": record.bvid,
            "pages": [{"page": record.page, "title": record.title, "record_id": record.id}],
        },
        "sources": {
            "official_subtitle": _source_descriptor(official, "transcript.official.json"),
            "asr_transcript": _source_descriptor(asr, "transcript.asr.json"),
            "cleaned_transcript": _source_descriptor(cleaned, "transcript.cleaned.json"),
            "source_preference": ["cleaned_transcript", "official_subtitle", "asr_transcript"],
        },
        "chunks": chunks,
        "timeline": timeline,
        "entities": {
            "people": _entity_items(summary.get("people"), evidence_lookup, chunks),
            "organizations": [],
            "terms": _entity_items(summary.get("terms"), evidence_lookup, chunks),
            "products": [],
            "locations": [],
        },
        "claims": claims,
        "examples": examples,
        "actions": actions,
        "summary": {
            "thirty_second": str(summary.get("thirty_second_summary") or summary.get("one_sentence_summary") or ""),
            "core_question": str(summary.get("core_problem") or ""),
            "main_conclusions": [str(item) for item in _list(summary.get("key_takeaways"))],
            "detailed_notes": [str(item) for item in _list(summary.get("detailed_notes"))],
        },
        "visual_analysis": visual,
        "index": {
            "available": bool(chunks),
            "index_type": "json_keyword_time_v1",
            "chunk_count": len(chunks),
            "chunk_strategy": f"consecutive_segments_max_{CHUNK_MAX_CHARACTERS}_chars_or_{int(CHUNK_MAX_SECONDS)}s",
            "embedding_model": "",
            "notes": "本地 JSON 关键词与时间索引，不依赖额外向量模型。",
        },
        "retrieval_text": retrieval_text,
    }
    if tuple(document) != TOP_LEVEL_KEYS:
        raise RuntimeError("结构化文档顶层字段顺序或集合不符合固定合约。")
    return document, search_index


def build_chunks(transcript: dict[str, object], *, source_type: str) -> list[dict[str, object]]:
    segments = _dict_list(transcript.get("segments"))
    chunks: list[dict[str, object]] = []
    current: list[dict[str, object]] = []
    current_chars = 0
    for segment in segments:
        text = " ".join(str(segment.get("text", "")).split())
        if not text:
            continue
        start = _number(segment.get("start_seconds"))
        end = max(start, _number(segment.get("end_seconds")))
        if current:
            group_start = _number(current[0].get("start_seconds"))
            if current_chars + len(text) > CHUNK_MAX_CHARACTERS or end - group_start > CHUNK_MAX_SECONDS:
                chunks.append(_make_chunk(len(chunks) + 1, current, source_type))
                current = []
                current_chars = 0
        current.append({"start_seconds": start, "end_seconds": end, "text": text})
        current_chars += len(text)
    if current:
        chunks.append(_make_chunk(len(chunks) + 1, current, source_type))
    return chunks


def build_search_index(chunks: list[dict[str, object]]) -> dict[str, object]:
    keyword_map: dict[str, list[str]] = {}
    time_ranges: list[dict[str, object]] = []
    for chunk in chunks:
        chunk_id = str(chunk["chunk_id"])
        for keyword in chunk.get("keywords", []):
            keyword_map.setdefault(str(keyword), []).append(chunk_id)
        time_ranges.append(
            {"chunk_id": chunk_id, "start": chunk["start"], "end": chunk["end"]}
        )
    return {
        "version": 1,
        "index_type": "json_keyword_time_v1",
        "chunk_order": [str(chunk["chunk_id"]) for chunk in chunks],
        "keywords": keyword_map,
        "time_ranges": time_ranges,
    }


def retrieve_chunks(
    structured: dict[str, object],
    question: str,
    *,
    limit: int = 12,
) -> list[dict[str, object]]:
    tokens = set(_keywords(question, limit=20))
    candidates: list[tuple[int, int, dict[str, object]]] = []
    for position, chunk in enumerate(_dict_list(structured.get("chunks"))):
        searchable = f"{chunk.get('text', '')} {' '.join(map(str, chunk.get('keywords', [])))}".lower()
        score = sum(2 if token in chunk.get("keywords", []) else 1 for token in tokens if token.lower() in searchable)
        candidates.append((score, -position, chunk))
    ranked = sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)
    selected = [item[2] for item in ranked[: max(1, limit)]]
    return sorted(selected, key=lambda item: _number(item.get("start")))


def _make_chunk(index: int, segments: list[dict[str, object]], source_type: str) -> dict[str, object]:
    start = _number(segments[0].get("start_seconds"))
    end = _number(segments[-1].get("end_seconds"))
    text = " ".join(str(segment.get("text", "")).strip() for segment in segments).strip()
    return {
        "chunk_id": f"chunk-{index:04d}-{int(start * 1000):010d}",
        "source_type": source_type,
        "start": start,
        "end": end,
        "text": text,
        "summary": text if len(text) <= 180 else text[:177].rstrip() + "…",
        "keywords": _keywords(text),
    }


def _build_timeline(
    summary: dict[str, object],
    chunks: list[dict[str, object]],
    evidence_lookup: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    result = []
    for item in _dict_list(summary.get("chapters")):
        start = _number(item.get("start_seconds"))
        end = max(start, _number(item.get("end_seconds")))
        evidence_items = [evidence_lookup[key] for key in _list(item.get("evidence_ids")) if key in evidence_lookup]
        result.append(
            {
                "start": start,
                "end": end,
                "title": str(item.get("title", "")),
                "summary": str(item.get("summary", "")),
                "evidence": [str(evidence.get("quote") or evidence.get("statement") or "") for evidence in evidence_items],
                "chunk_ids": _overlapping_chunk_ids(chunks, start, end),
                "frame_ids": [],
            }
        )
    return result


def _build_claims(
    summary: dict[str, object],
    chunks: list[dict[str, object]],
    evidence_lookup: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    result = []
    for evidence_id, item in evidence_lookup.items():
        start = _number(item.get("start_seconds"))
        end = max(start, _number(item.get("end_seconds")))
        result.append(
            {
                "type": "evidence",
                "source": "transcript",
                "claim": str(item.get("statement", "")),
                "evidence_text": str(item.get("quote", "")),
                "timestamp": _timestamp_range(start, end),
                "chunk_id": _nearest_chunk_id(chunks, start),
                "frame_id": "",
                "confidence": str(item.get("confidence") or "medium"),
                "evidence_id": evidence_id,
            }
        )
    if not result:
        for index, claim in enumerate(_list(summary.get("key_takeaways")), start=1):
            result.append(
                {
                    "type": "conclusion",
                    "source": "summary",
                    "claim": str(claim),
                    "evidence_text": "",
                    "timestamp": "",
                    "chunk_id": "",
                    "frame_id": "",
                    "confidence": "low",
                    "evidence_id": f"generated-{index}",
                }
            )
    return result


def _build_examples(
    summary: dict[str, object],
    chunks: list[dict[str, object]],
    evidence_lookup: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    result = []
    for item in _dict_list(summary.get("examples")):
        evidence = _first_evidence(item, evidence_lookup)
        start = _number(evidence.get("start_seconds"))
        end = _number(evidence.get("end_seconds"))
        result.append(
            {
                "title": str(item.get("name", "")),
                "description": str(item.get("description", "")),
                "timestamp": _timestamp_range(start, end) if evidence else "",
                "evidence_text": str(evidence.get("quote") or evidence.get("statement") or ""),
                "chunk_id": _nearest_chunk_id(chunks, start) if evidence else "",
                "frame_id": "",
            }
        )
    return result


def _build_actions(
    summary: dict[str, object],
    chunks: list[dict[str, object]],
    evidence_lookup: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    result = []
    for item in _dict_list(summary.get("action_items")):
        evidence = _first_evidence(item, evidence_lookup)
        start = _number(evidence.get("start_seconds"))
        end = _number(evidence.get("end_seconds"))
        result.append(
            {
                "action": str(item.get("action", "")),
                "reason": str(item.get("rationale", "")),
                "timestamp": _timestamp_range(start, end) if evidence else "",
                "evidence_text": str(evidence.get("quote") or evidence.get("statement") or ""),
                "chunk_id": _nearest_chunk_id(chunks, start) if evidence else "",
                "frame_id": "",
            }
        )
    return result


def _entity_items(
    value: object,
    evidence_lookup: dict[str, dict[str, object]],
    chunks: list[dict[str, object]],
) -> list[dict[str, object]]:
    result = []
    for item in _dict_list(value):
        evidence = _first_evidence(item, evidence_lookup)
        start = _number(evidence.get("start_seconds"))
        result.append(
            {
                "name": str(item.get("name", "")),
                "description": str(item.get("description", "")),
                "evidence_text": str(evidence.get("quote") or evidence.get("statement") or ""),
                "timestamp": _timestamp_range(start, _number(evidence.get("end_seconds"))) if evidence else "",
                "chunk_id": _nearest_chunk_id(chunks, start) if evidence else "",
            }
        )
    return result


def _source_descriptor(payload: dict[str, object], file_name: str) -> dict[str, object]:
    return {
        "available": bool(payload),
        "file": file_name if payload else "",
        "source": str(payload.get("source") or "") if payload else "",
        "language": str(payload.get("language") or "") if payload else "",
        "segment_count": len(_dict_list(payload.get("segments"))) if payload else 0,
    }


def _source_name(
    preferred: dict[str, object],
    *,
    official: dict[str, object],
    asr: dict[str, object],
    cleaned: dict[str, object],
) -> str:
    if cleaned and preferred is cleaned:
        return "cleaned_transcript"
    if official and preferred is official:
        return "official_subtitle"
    if asr and preferred is asr:
        return "asr_transcript"
    return "unknown"


def _default_visual_analysis(record: LibraryRecord, data_dir: Path) -> dict[str, object]:
    local = _read_json(data_dir / "visual-local.json")
    return {
        "visual_importance": record.visual.importance,
        "reason": record.visual.reason,
        "sampled_frame_count": int(_number(local.get("sampled_frame_count"))),
        "important_frame_count": int(_number(local.get("important_frame_count"))) or record.visual.keyframe_count,
        "local_detection_used": record.visual.importance != "unknown",
        "cloud_vision_used": False,
        "cloud_vision_mode": "disabled",
        "cloud_vision_provider": "",
        "cloud_vision_model": "",
        "suggest_enable_visual_mode": record.visual.importance in {"medium", "high"},
        "keyframes": _dict_list(local.get("keyframes")),
        "tables": [],
        "charts": [],
        "visual_claims": [],
        "uncertainties": [],
    }


def _overlapping_chunk_ids(chunks: list[dict[str, object]], start: float, end: float) -> list[str]:
    return [
        str(chunk["chunk_id"])
        for chunk in chunks
        if _number(chunk.get("end")) >= start and _number(chunk.get("start")) <= end
    ]


def _nearest_chunk_id(chunks: list[dict[str, object]], timestamp: float) -> str:
    if not chunks:
        return ""
    containing = [
        chunk
        for chunk in chunks
        if _number(chunk.get("start")) <= timestamp <= _number(chunk.get("end"))
    ]
    candidate = containing[0] if containing else min(chunks, key=lambda chunk: abs(_number(chunk.get("start")) - timestamp))
    return str(candidate["chunk_id"])


def _first_evidence(
    item: dict[str, object], evidence_lookup: dict[str, dict[str, object]]
) -> dict[str, object]:
    for evidence_id in _list(item.get("evidence_ids")):
        if str(evidence_id) in evidence_lookup:
            return evidence_lookup[str(evidence_id)]
    return {}


def _keywords(text: str, *, limit: int = 10) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{1,30}|[\u4e00-\u9fff]{2,8}", text.lower())
    stopwords = {"这个", "那个", "我们", "你们", "他们", "然后", "就是", "一个", "可以", "进行", "因为", "所以", "以及", "如果"}
    counts = Counter(word for word in words if word not in stopwords)
    return [word for word, _ in counts.most_common(limit)]


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _timestamp_range(start: float, end: float) -> str:
    return f"{_timestamp(start)}–{_timestamp(max(start, end))}"


def _read_first_json(*paths: Path) -> dict[str, object]:
    for path in paths:
        payload = _read_json(path)
        if payload:
            return payload
    return {}


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dict_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0
