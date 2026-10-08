"""Source-grounded model drafts using the inherited video chunker."""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import logging
import math
import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit
from .model_config import ModelConfigurationError
from backend.video_summary.domain.models import TranscriptSegment
from backend.video_summary.generation.prompts.summary import VIDEO_SUMMARY_CHUNK_TIMEOUT_SECONDS, VIDEO_SUMMARY_DOCUMENT_TIMEOUT_SECONDS, chunk_segments
from .model_config import ModelConfiguration


_FIELDS = ("title", "summary", "topics", "facts", "todos", "uncertainties", "people", "dates", "suggestions")


_PROMPT = ("只返回紧凑 JSON 对象，字段 title,summary,topics,facts,todos,uncertainties,people,dates,suggestions。"
           "title、summary 是字符串，其他字段是数组。facts 与 todos 最多各 6 项，"
           "每项为 {\"text\":\"...\",\"evidence\":{\"quote\":\"...\"}}。"
           "quote 必须是原文中连续、完全相同且能唯一定位的短句，不要计算字符下标。"
           "只写明确有依据的事实与待办；推断写入 suggestions 或 uncertainties。"
           "摘要最多 150 字，其他数组最多各 5 项。不要解释或 Markdown。")


_VIDEO_CHUNK_PROMPT = (
    "你正在整理长视频的一段原始转写。只返回紧凑 JSON 对象，字段 "
    "summary,topics,facts,todos,uncertainties。summary 是不超过 100 字的字符串，"
    "其余字段是数组；topics、uncertainties 各最多 3 项，facts、todos 各最多 3 项。"
    "facts 与 todos 每项为 {\"text\":\"...\",\"evidence\":{\"quote\":\"...\"}}。"
    "quote 必须是本段原文中连续、完全相同且在本段唯一的短句；不要计算字符下标。"
    "找不到唯一原文证据时不要写入 facts 或 todos。"
    "只处理本段，不补造其他段内容，不要解释或 Markdown。"
)


_LOG = logging.getLogger(__name__)


def _strip_fence(value: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", value.strip(), flags=re.IGNORECASE)


def _is_local_model(models: ModelConfiguration) -> bool:
    public = getattr(models, "public", None)
    if not callable(public):
        return False
    base_url = str(public()["generation"].get("base_url", ""))
    return urlsplit(base_url).hostname in {"localhost", "127.0.0.1", "::1"}


def _remote_generation_target(models: ModelConfiguration) -> dict[str, object] | None:
    public = getattr(models, "public", None)
    if not callable(public):
        return None
    generation = public().get("generation", {})
    if not isinstance(generation, dict):
        return None
    base_url = str(generation.get("base_url") or "")
    host = urlsplit(base_url).hostname
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return None
    return {
        "base_url": base_url,
        "model": generation.get("model"),
        "revision": generation.get("revision"),
        "allow_remote": generation.get("allow_remote"),
        "enabled": generation.get("enabled"),
    }


def _ground_local_draft(value: object, source: str) -> object:
    """Keep small local-model facts as literal source excerpts, never invented citations."""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    spans = _source_sentence_spans(source)
    for field, fallback in (("facts", "uncertainties"), ("todos", "suggestions")):
        entries = result.get(field)
        if not isinstance(entries, list):
            continue
        grounded = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                grounded.append(entry)
                continue
            evidence = entry.get("evidence")
            quote = evidence.get("quote") if isinstance(evidence, dict) else None
            if isinstance(quote, str) and quote and source.count(quote) == 1:
                start = source.index(quote)
                end = start + len(quote)
                # A model can quote only the first part of a sentence. Keep the
                # complete source assertion, especially around version numbers.
                start, end = next(((a, b) for a, b in spans if a <= start and end <= b), (start, end))
            else:
                target = quote if isinstance(quote, str) and quote else entry["text"]
                ranked = sorted(((SequenceMatcher(None, target.casefold(), source[a:b].casefold()).ratio(), a, b)
                                 for a, b in spans), reverse=True)
                if not ranked or ranked[0][0] < 0.35:
                    if isinstance(result.get(fallback), list):
                        result[fallback] = [*result[fallback], entry["text"]]
                    continue
                _score, start, end = ranked[0]
            literal = source[start:end]
            grounded.append({"text": literal, "evidence": {"start": start, "end": end, "quote": literal}})
        result[field] = grounded
    # Explicit task and unknown markers are more reliable than a small model's
    # category guess. Preserve the whole source line as clickable evidence.
    marked = []
    for match in re.finditer(r"(?m)^\s*(待办|TODO|To Do|尚未确定|待确认|不确定|TBD)\s*[:：]\s*(.+)$", source, re.IGNORECASE):
        kind = "todos" if match.group(1).casefold() in {"待办", "todo", "to do"} else "uncertainties"
        start, end = match.start(), match.end()
        while start < end and source[start].isspace():
            start += 1
        marked.append((kind, start, end, match.group(2).strip()))
    if marked:
        result["facts"] = [entry for entry in result.get("facts", []) if not any(
            entry.get("evidence", {}).get("start", -1) < end
            and entry.get("evidence", {}).get("end", -1) > start
            for _kind, start, end, _text in marked) if isinstance(entry, dict)]
        for kind, start, end, text in marked:
            if kind == "todos":
                if not any(entry.get("evidence", {}).get("start") == start for entry in result.get("todos", []) if isinstance(entry, dict)):
                    result.setdefault("todos", []).append({"text": source[start:end],
                                                           "evidence": {"start": start, "end": end, "quote": source[start:end]}})
            elif text and text not in result.get("uncertainties", []):
                result.setdefault("uncertainties", []).append(text)
    return result


def _source_sentence_spans(source: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for index, char in enumerate(source):
        decimal_point = char == "." and 0 < index < len(source) - 1 and source[index - 1].isdigit() and source[index + 1].isdigit()
        if char not in ".!?。！？\n" or decimal_point:
            continue
        end = index if char == "\n" else index + 1
        while start < end and source[start].isspace():
            start += 1
        while end > start and source[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append((start, end))
        start = index + 1
    if start < len(source):
        end = len(source)
        while start < end and source[start].isspace():
            start += 1
        while end > start and source[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append((start, end))
    return spans


def _video_source_chunks(source: str) -> list[tuple[str, int]]:
    """Adapt source offsets to the inherited OS transcript chunker."""
    positions: dict[int, tuple[int, int]] = {}
    segments = []
    for start, end in _source_sentence_spans(source):
        for part_start in range(start, end, 3500):
            part_end = min(part_start + 3500, end)
            segment = TranscriptSegment(0.0, 0.0, source[part_start:part_end])
            segments.append(segment)
            positions[id(segment)] = (part_start, part_end)
    result = []
    for group in chunk_segments(segments, max_chars=3500):
        start = positions[id(group[0])][0]
        end = start
        for segment in group:
            segment_start, segment_end = positions[id(segment)]
            if end > start and segment_end - start > 3500:
                result.append((source[start:end], start))
                start = segment_start
            end = segment_end
        result.append((source[start:end], start))
    return result


def _complete_chunked_video_draft(
    source: str, validate_current: Callable[[], None], local_model: bool,
    *, organize,
) -> tuple[dict, dict]:
    """Use the OS chunk plan, then merge only source-checked candidates."""
    chunks = _video_source_chunks(source)
    if len(chunks) < 2:
        raise ValueError("source_text_too_large")
    candidates: dict[str, dict] = {}
    overviews = []
    usage: dict[str, int] = {}
    final_meta: dict = {}

    def complete_validated(
        messages: list[dict[str, str]], *, timeout_seconds: int, stage: str,
        parse: Callable[[str], dict],
    ) -> dict:
        nonlocal final_meta
        deadline = time.monotonic() + timeout_seconds
        correction = None
        for attempt in (1, 2):
            remaining = math.ceil(deadline - time.monotonic())
            if remaining < 1:
                _LOG.warning("workspace video draft stage=%s error=invalid_response_budget_exhausted", stage)
                raise ModelConfigurationError("model_response_invalid")
            validate_current()
            try:
                response, final_meta = organize.complete(
                    messages if correction is None else [*messages, correction],
                    max_tokens=768 if local_model else 7000,
                    validate_current=validate_current, timeout_seconds=remaining,
                    stage=f'{stage}-{attempt}', validate_output=parse,
                )
            except ModelConfigurationError as error:
                safe_code = str(error) if str(error) in {
                    "model_output_incomplete", "model_output_missing_content", "model_response_invalid",
                } else "model_request_failed" if str(error).startswith("model_request_failed") else "model_error"
                _LOG.warning("workspace video draft stage=%s error=%s", stage, safe_code)
                raise
            validate_current()
            for key, value in (final_meta.get("usage") or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    usage[key] = usage.get(key, 0) + value
            try:
                return parse(response)
            except (ValueError, json.JSONDecodeError) as error:
                error_code = str(error) if str(error) in {"invalid_draft", "invalid_evidence"} else "invalid_json"
                _LOG.warning("workspace video draft stage=%s error=%s attempt=%d", stage, error_code, attempt)
                if attempt == 2 or deadline - time.monotonic() < 1:
                    raise ModelConfigurationError("model_response_invalid") from None
                correction = {
                    "role": "user",
                    "content": (
                        "上次输出未通过本地校验（" + error_code + "）。请仅根据同一段输入重新生成完整 JSON，"
                        "严格使用系统消息指定的字段和数组类型。事实与待办的 quote 必须在当前原文中连续、完全相同且唯一；"
                        "不要输出字符下标、额外字段、说明或代码围栏。合并阶段的 fact_ids/todo_ids "
                        "只能选用输入中对应类型的候选 ID。"
                    ),
                }
        raise ModelConfigurationError("model_response_invalid")

    for index, (chunk_text, offset) in enumerate(chunks, 1):
        def parse_chunk(response: str) -> dict:
            value = json.loads(_strip_fence(response))
            if not isinstance(value, dict) or set(value) != {"summary", "topics", "facts", "todos", "uncertainties"}:
                raise ValueError("invalid_draft")
            value = {"title": f"片段 {index}", "people": [], "dates": [], "suggestions": [], **value}
            if local_model:
                value = _ground_local_draft(value, chunk_text)
            partial = _draft(value, chunk_text)
            translated = {"facts": [], "todos": []}
            for field in ("facts", "todos"):
                for entry in partial[field]:
                    evidence = entry["evidence"]
                    translated[field].append({
                        "text": entry["text"],
                        "evidence": {
                            "start": offset + evidence["start"],
                            "end": offset + evidence["end"],
                            "quote": evidence["quote"],
                        },
                    })
            return _draft({**partial, **translated}, source)

        grounded = complete_validated([
            {"role": "system", "content": _VIDEO_CHUNK_PROMPT + f" 当前仅处理全文第 {index}/{len(chunks)} 段。"},
            {"role": "user", "content": chunk_text},
        ], timeout_seconds=VIDEO_SUMMARY_CHUNK_TIMEOUT_SECONDS, stage=f"chunk_{index}", parse=parse_chunk)
        for field in ("facts", "todos"):
            for number, entry in enumerate(grounded[field], 1):
                candidates[f"{field}-{index}-{number}"] = entry
        overviews.append({
            "part": index, "summary": grounded["summary"], "topics": grounded["topics"],
            "uncertainties": grounded["uncertainties"], "people": grounded["people"],
            "dates": grounded["dates"], "suggestions": grounded["suggestions"],
        })

    def parse_merge(response: str) -> dict:
        merged = json.loads(_strip_fence(response))
        if not isinstance(merged, dict) or set(merged) != {
            "title", "summary", "topics", "uncertainties", "people", "dates", "suggestions",
            "fact_ids", "todo_ids",
        }:
            raise ValueError("invalid_draft")
        selected = {}
        for field, id_field in (("facts", "fact_ids"), ("todos", "todo_ids")):
            ids = merged.pop(id_field)
            if not isinstance(ids, list) or len(ids) > 6 or any(
                not isinstance(candidate_id, str) or not candidate_id.startswith(field + "-")
                or candidate_id not in candidates for candidate_id in ids
            ):
                raise ValueError("invalid_draft")
            selected[field] = [candidates[candidate_id] for candidate_id in dict.fromkeys(ids)]
        return _draft({**merged, **selected}, source)

    draft = complete_validated([
        {"role": "system", "content": (
            "将分段整理结果合并为一个紧凑 JSON 草稿，只返回字段 "
            "title,summary,topics,uncertainties,people,dates,suggestions,fact_ids,todo_ids。"
            "title、summary 为字符串，其余字段为数组；summary 最多 150 字，"
            "fact_ids 与 todo_ids 各最多 6 个，只能从输入的候选 ID 中选择。"
            "分段摘要是二手材料，不得新增事实、引用或候选 ID。"
        )},
        {"role": "user", "content": json.dumps({
            "parts": overviews,
            "candidates": [
                {"id": candidate_id, "text": entry["text"], "quote": entry["evidence"]["quote"]}
                for candidate_id, entry in candidates.items()
            ],
        }, ensure_ascii=False)},
    ], timeout_seconds=VIDEO_SUMMARY_DOCUMENT_TIMEOUT_SECONDS, stage="merge", parse=parse_merge)
    return draft, {"model": final_meta.get("model", ""), "usage": usage}


def _locate_quote(source: str, quote: str) -> tuple[int, int] | None:
    """Find a model quote in the stored source; PDF text breaks lines mid-sentence.

    An exact match wins (the first one when repeated). Otherwise whitespace
    differences are ignored. A quote that is not in the source returns None.
    """
    start = source.find(quote)
    if start >= 0:
        return start, start + len(quote)
    compact = re.sub(r"\s+", "", quote)
    if not compact:
        return None
    match = re.search(r"\s*".join(map(re.escape, compact)), source)
    return (match.start(), match.end()) if match else None


def _draft(value: object, source: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(_FIELDS):
        raise ValueError("invalid_draft")
    result = {}
    for field in ("title", "summary"):
        text = value[field]
        if not isinstance(text, str) or not text.strip() or len(text) > (200 if field == "title" else 5000):
            raise ValueError("invalid_draft")
        result[field] = text.strip()
    for field in ("topics", "uncertainties", "people", "dates", "suggestions"):
        entries = value[field]
        if not isinstance(entries, list) or len(entries) > 40 or any(not isinstance(x, str) or len(x) > 1000 for x in entries):
            raise ValueError("invalid_draft")
        result[field] = entries
    for field in ("facts", "todos"):
        entries = value[field]
        if not isinstance(entries, list) or len(entries) > 40:
            raise ValueError("invalid_draft")
        checked = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str) or not entry["text"].strip():
                raise ValueError("invalid_draft")
            evidence = entry.get("evidence")
            quote = evidence.get("quote") if isinstance(evidence, dict) else None
            if not isinstance(quote, str) or not quote:
                # An entry without a source quote is dropped; the rest of the draft stays.
                continue
            start, end = evidence.get("start"), evidence.get("end")
            if (type(start) is not int or type(end) is not int or not (0 <= start < end <= len(source))
                    or source[start:end] != quote):
                # Resolve model quotes against the stored source. This avoids
                # asking a model to count Unicode offsets and never invents a location.
                located = _locate_quote(source, quote)
                if located is None:
                    continue
                start, end = located
            checked.append({"text": entry["text"].strip(), "evidence": {"start": start, "end": end, "quote": source[start:end]}})
        result[field] = checked
    return result


def _markdown(draft: dict) -> str:
    lines = ["# " + draft["title"], "", "## 摘要", "", draft["summary"]]
    for key, heading in (("topics", "主题"), ("facts", "关键事实"), ("todos", "待办"),
                         ("uncertainties", "不确定项"), ("people", "人物"), ("dates", "时间"),
                         ("suggestions", "建议")):
        if draft[key]:
            lines.extend(["", "## " + heading])
            lines.extend("- " + (x if isinstance(x, str) else x["text"]) for x in draft[key])
    return "\n".join(lines) + "\n"
