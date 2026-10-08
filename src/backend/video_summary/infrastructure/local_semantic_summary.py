from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from backend.video_summary.infrastructure.agent_memory.fastembed_adapter import (
    build_fastembed_embedding,
)
from backend.video_summary.infrastructure.rag_models import RagModelManager
from backend.video_summary.infrastructure.in_memory_progress_tracker import InMemoryProgressTracker


MODEL_NAME = "BAAI/bge-small-zh-v1.5"
METHOD = "semantic_extractive_bge_v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chriptmas OS packaged local semantic extractive summary")
    parser.add_argument("--stdin-json", action="store_true")
    args = parser.parse_args(argv)
    if not args.stdin_json:
        sys.stderr.write("--stdin-json is required")
        return 2
    try:
        root = _app_root()
        manager = RagModelManager(root_dir=root, progress_tracker=InMemoryProgressTracker())
        if not manager.is_downloaded("embedding"):
            raise ValueError("local semantic summary embedding model is missing")
        embedding = build_fastembed_embedding(
            model_name=MODEL_NAME,
            device="cpu",
            embed_batch_size=16,
            cache_dir=str(root / "data" / "models" / "fastembed"),
        )
        payload = json.load(sys.stdin)
        result = build_semantic_extractive_summary(payload, embed=embedding.get_text_embedding_batch)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as error:  # noqa: BLE001 - CLI boundary returns a bounded diagnostic.
        sys.stderr.write(_bounded_error(error))
        return 2


def build_semantic_extractive_summary(
    payload: Mapping[str, object],
    *,
    embed: Callable[[list[str]], list[list[float]]],
) -> dict[str, object]:
    title = _required_text(payload.get("title"), "title")
    segments = _segments(payload.get("segments"))
    if not segments:
        raise ValueError("timestamped transcript segments are required")
    chunks = _chunks(segments)
    vectors = embed([str(item["text"]) for item in chunks])
    if len(vectors) != len(chunks) or not vectors:
        raise ValueError("embedding provider returned an invalid vector count")
    normalized = [_normalize(vector) for vector in vectors]
    centroid = _normalize([sum(values) / len(normalized) for values in zip(*normalized, strict=True)])
    title_vector = _normalize(embed([title])[0])
    ranked = _mmr_indices(normalized, centroid, title_vector, min(8, len(chunks)))
    chronological = sorted(ranked, key=lambda index: float(chunks[index]["start_seconds"]))
    evidence = [_evidence(chunks[index], position + 1) for position, index in enumerate(chronological)]
    chapters = _chapters(chunks, normalized)
    keywords = _semantic_keywords(title, chunks, ranked, centroid, embed)
    representative_texts = [str(chunks[index]["text"]) for index in ranked]
    short_summary = " ".join(_extract_prefix(text, 96) for text in representative_texts[:3])
    one_sentence = _extract_prefix(representative_texts[0], 140)
    candidate_payload = _candidate_payload(evidence, chapters)
    return {
        "title": title,
        "content_type": "本地语义抽取式整理",
        "summary_method": METHOD,
        "generative_model_used": False,
        "thirty_second_summary": short_summary,
        "one_sentence_summary": one_sentence,
        "core_problem": one_sentence,
        "chapters": chapters,
        "key_takeaways": representative_texts[:5],
        "detailed_notes": [str(item["text"]) for item in sorted(chunks, key=lambda item: float(item["start_seconds"]))],
        "evidence": evidence,
        "keywords": keywords,
        "people": [],
        "terms": [{"name": term, "description": "原文语义代表短语", "evidence_ids": []} for term in keywords],
        "examples": [],
        "data_points": [],
        "viewpoints": [],
        "action_items": [],
        "relations": [],
        "open_questions": [],
        "visual_attention": {"importance": "unknown", "reason": "未读取画面", "signals": []},
        "memory_candidate_payload": candidate_payload,
    }


def _segments(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, object]] = []
    previous_end = 0.0
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("transcript segment must be an object")
        text = _required_text(item.get("text"), "segment text")
        start = _non_negative(item.get("start_seconds"), "segment start")
        end = _non_negative(item.get("end_seconds"), "segment end")
        if end < start or start + 0.001 < previous_end:
            raise ValueError("transcript segment timestamps are invalid")
        result.append({"start_seconds": start, "end_seconds": end, "text": text})
        previous_end = end
    return result


def _chunks(segments: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    chunks: list[dict[str, object]] = []
    current: list[Mapping[str, object]] = []
    for segment in segments:
        if current and (
            float(segment["end_seconds"]) - float(current[0]["start_seconds"]) > 45.0
            or sum(len(str(item["text"])) for item in current) + len(str(segment["text"])) > 420
        ):
            chunks.append(_merge_chunk(current))
            current = []
        current.append(segment)
    if current:
        chunks.append(_merge_chunk(current))
    return chunks


def _merge_chunk(items: Sequence[Mapping[str, object]]) -> dict[str, object]:
    return {
        "start_seconds": float(items[0]["start_seconds"]),
        "end_seconds": float(items[-1]["end_seconds"]),
        "text": " ".join(str(item["text"]).strip() for item in items),
    }


def _mmr_indices(
    vectors: Sequence[Sequence[float]],
    centroid: Sequence[float],
    title_vector: Sequence[float],
    count: int,
) -> list[int]:
    selected: list[int] = []
    remaining = set(range(len(vectors)))
    while remaining and len(selected) < count:
        best = max(
            remaining,
            key=lambda index: (
                0.42 * _dot(vectors[index], centroid)
                + 0.38 * _dot(vectors[index], title_vector)
                - 0.20 * max((_dot(vectors[index], vectors[chosen]) for chosen in selected), default=0.0),
                -index,
            ),
        )
        selected.append(best)
        remaining.remove(best)
    return selected


def _chapters(chunks: Sequence[Mapping[str, object]], vectors: Sequence[Sequence[float]]) -> list[dict[str, object]]:
    chapter_count = min(8, max(1, math.ceil(len(chunks) / 12)))
    duration = float(chunks[-1]["end_seconds"])
    result: list[dict[str, object]] = []
    for chapter_index in range(chapter_count):
        start = duration * chapter_index / chapter_count
        end = duration * (chapter_index + 1) / chapter_count
        indices = [
            index for index, chunk in enumerate(chunks)
            if float(chunk["start_seconds"]) < end and float(chunk["end_seconds"]) >= start
        ]
        if not indices:
            continue
        local_centroid = _normalize([
            sum(vectors[index][dimension] for index in indices) / len(indices)
            for dimension in range(len(vectors[0]))
        ])
        representative = max(indices, key=lambda index: _dot(vectors[index], local_centroid))
        chunk = chunks[representative]
        chapter_id = f"chapter-{len(result) + 1}"
        result.append({
            "id": chapter_id,
            "title": f"{_format_time(float(chunk['start_seconds']))} 原文主题片段",
            "start_seconds": float(chunks[indices[0]]["start_seconds"]),
            "end_seconds": float(chunks[indices[-1]]["end_seconds"]),
            "summary": str(chunk["text"]),
            "key_points": [str(chunk["text"])],
            "evidence_ids": [],
        })
    return result


def _semantic_keywords(
    title: str,
    chunks: Sequence[Mapping[str, object]],
    ranked: Sequence[int],
    centroid: Sequence[float],
    embed: Callable[[list[str]], list[list[float]]],
) -> list[str]:
    selected_texts = [str(chunks[index]["text"]) for index in ranked[:8]]
    transcript_text = " ".join(str(chunk["text"]) for chunk in chunks).lower()
    candidates = [term for term in _title_keyword_candidates(title) if term in transcript_text]
    for text in selected_texts:
        for phrase in re.findall(
            r"[A-Za-z][A-Za-z0-9+#.-]*(?:\s+[A-Za-z][A-Za-z0-9+#.-]*){0,2}",
            text,
        ):
            clean = phrase.strip(" .").lower()
            if 2 <= len(clean) <= 30 and clean not in candidates:
                candidates.append(clean)
    cjk_counts: dict[str, int] = {}
    for text in selected_texts:
        observed: set[str] = set()
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            for width in range(2, min(6, len(run)) + 1):
                for start in range(len(run) - width + 1):
                    phrase = run[start : start + width]
                    if _useful_cjk_phrase(phrase):
                        observed.add(phrase)
        for phrase in observed:
            cjk_counts[phrase] = cjk_counts.get(phrase, 0) + 1
    repeated = sorted(cjk_counts, key=lambda phrase: (cjk_counts[phrase], len(phrase)), reverse=True)
    candidates.extend(phrase for phrase in repeated if cjk_counts[phrase] >= 2 and phrase not in candidates)
    candidates = candidates[:80]
    if not candidates:
        return []
    vectors = [_normalize(vector) for vector in embed(candidates)]
    ranked_terms = sorted(range(len(candidates)), key=lambda index: _dot(vectors[index], centroid), reverse=True)
    title_candidates = [term for term in _title_keyword_candidates(title) if term in transcript_text][:3]
    semantic = [candidates[index] for index in ranked_terms if candidates[index] not in title_candidates]
    return (title_candidates + semantic)[:10]


def _title_keyword_candidates(title: str) -> list[str]:
    candidates = [
        phrase.strip().lower()
        for phrase in re.findall(
            r"[A-Za-z][A-Za-z0-9+#.-]*(?:\s+[A-Za-z][A-Za-z0-9+#.-]*){0,2}|[\u4e00-\u9fff]{2,12}",
            title,
        )
        if phrase.strip()
    ]
    return list(dict.fromkeys(candidates))


def _useful_cjk_phrase(phrase: str) -> bool:
    edge_stop = set("你我他她它的是了在和就也都而又把被让会要能这那有没很吗呢啊呀吧个一")
    exact_stop = {
        "然后", "就是", "这个", "那个", "其实", "因为", "所以", "如果", "但是", "还是",
        "可以", "觉得", "一个", "一些", "什么", "事情", "时候", "里面", "现在", "可能",
    }
    return phrase not in exact_stop and phrase[0] not in edge_stop and phrase[-1] not in edge_stop


def _candidate_payload(evidence: Sequence[Mapping[str, object]], chapters: Sequence[Mapping[str, object]]) -> dict[str, object]:
    source = list(evidence) or list(chapters)
    layers = ("atom", "scenario", "series_memory", "project_skill")
    candidates = []
    for index, layer in enumerate(layers):
        item = source[index % len(source)]
        text = _required_text(item.get("statement") or item.get("summary"), "candidate evidence")
        start = float(item.get("start_seconds", 0.0))
        candidates.append({
            "target_layer": layer,
            "candidate_type": "answer_summary" if layer != "atom" else "answer_fact",
            "status": "pending_review",
            "proposed_content": f"[{_format_time(start)}] {text}",
            "review_prompt": "请核对原文时间证据后决定是否进入长期记忆。",
            "review": {"requires_user_confirmation": True, "auto_promote_allowed": False},
        })
    return {
        "candidates": candidates,
        "insufficient_evidence": [],
        "provider_boundary": {
            "local_processing": True,
            "semantic_extractive": True,
            "generative_model_used": False,
            "memory_publication": "not_started",
        },
    }


def _evidence(chunk: Mapping[str, object], position: int) -> dict[str, object]:
    text = str(chunk["text"])
    return {
        "id": f"ev-{position}",
        "statement": text,
        "quote": text,
        "start_seconds": float(chunk["start_seconds"]),
        "end_seconds": float(chunk["end_seconds"]),
        "confidence": "extractive",
    }


def _normalize(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    magnitude = math.sqrt(sum(value * value for value in values))
    if not values or magnitude <= 0:
        raise ValueError("embedding provider returned an empty vector")
    return [value / magnitude for value in values]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    return sum(a * b for a, b in zip(left, right, strict=True))


def _app_root() -> Path:
    configured = os.environ.get("CHRIPTMAS_APP_ROOT", "").strip()
    if not configured:
        raise ValueError("CHRIPTMAS_APP_ROOT is required for packaged local summary")
    root = Path(configured).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("CHRIPTMAS_APP_ROOT is not a directory")
    return root


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _non_negative(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be non-negative") from error
    if number < 0:
        raise ValueError(f"{label} must be non-negative")
    return number


def _format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _extract_prefix(text: str, limit: int) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    prefix = clean[:limit]
    boundary = max(prefix.rfind(mark) for mark in "。！？.!?")
    return prefix[: boundary + 1].strip() if boundary >= limit // 2 else prefix.strip()


def _bounded_error(error: Exception) -> str:
    return " ".join((str(error) or error.__class__.__name__).split())[:240]


if __name__ == "__main__":
    raise SystemExit(main())
