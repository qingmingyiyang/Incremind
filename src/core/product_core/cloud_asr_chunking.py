from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


CLOUD_ASR_CHUNK_DURATION_SECONDS = 60.0
CLOUD_ASR_CHUNK_OVERLAP_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class CloudAsrChunkPlan:
    index: int
    start_seconds: float
    end_seconds: float
    overlap_before_seconds: float


def plan_cloud_asr_chunks(
    total_duration_seconds: float,
    *,
    chunk_duration_seconds: float = CLOUD_ASR_CHUNK_DURATION_SECONDS,
    overlap_seconds: float = CLOUD_ASR_CHUNK_OVERLAP_SECONDS,
) -> tuple[CloudAsrChunkPlan, ...]:
    """Plan bounded windows whose adjacent ranges overlap deterministically."""

    total = float(total_duration_seconds)
    duration = float(chunk_duration_seconds)
    overlap = float(overlap_seconds)
    if total <= 0:
        return ()
    if duration <= 0 or overlap < 0 or overlap >= duration:
        raise ValueError("cloud ASR chunk duration and overlap are invalid")
    plans: list[CloudAsrChunkPlan] = []
    start = 0.0
    while start < total:
        end = min(start + duration, total)
        plans.append(CloudAsrChunkPlan(
            index=len(plans),
            start_seconds=round(start, 6),
            end_seconds=round(end, 6),
            overlap_before_seconds=0.0 if not plans else overlap,
        ))
        if end >= total:
            break
        start = end - overlap
    return tuple(plans)


def merge_cloud_asr_chunk_outputs(
    chunks: Sequence[tuple[CloudAsrChunkPlan, Mapping[str, object]]],
) -> dict[str, object]:
    """Merge chunk transcripts using midpoint ownership inside overlap windows."""

    if not chunks:
        raise ValueError("cloud ASR chunk outputs are unavailable")
    ordered = sorted(chunks, key=lambda item: item[0].index)
    merged_segments: list[dict[str, object]] = []
    fallback_parts: list[str] = []
    languages: list[str] = []
    total_tokens = 0
    duration_ms = 0
    for position, (plan, output) in enumerate(ordered):
        text = str(output.get("text") or "").strip()
        if text:
            fallback_parts.append(text)
        language = str(output.get("language") or "").strip()
        if language:
            languages.append(language)
        metadata = output.get("metadata")
        if isinstance(metadata, Mapping):
            token_count = metadata.get("usage_total_token")
            if isinstance(token_count, int):
                total_tokens += token_count
        duration_ms = max(duration_ms, round(plan.end_seconds * 1000))
        left_boundary = plan.start_seconds
        if position:
            previous = ordered[position - 1][0]
            left_boundary = (previous.end_seconds + plan.start_seconds) / 2
        right_boundary = plan.end_seconds
        if position + 1 < len(ordered):
            following = ordered[position + 1][0]
            right_boundary = (plan.end_seconds + following.start_seconds) / 2
        raw_segments = output.get("segments")
        if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)):
            continue
        for raw in raw_segments:
            if not isinstance(raw, Mapping):
                continue
            try:
                start = plan.start_seconds + float(raw["start_seconds"])
                end = plan.start_seconds + float(raw["end_seconds"])
            except (KeyError, TypeError, ValueError):
                continue
            center = (start + end) / 2
            is_last_window = position + 1 == len(ordered)
            if center < left_boundary or (center >= right_boundary and not is_last_window):
                continue
            segment_text = str(raw.get("text") or "").strip()
            if not segment_text:
                continue
            merged_segments.append({
                "start_seconds": round(max(plan.start_seconds, start), 3),
                "end_seconds": round(min(plan.end_seconds, end), 3),
                "text": segment_text,
            })
    if merged_segments:
        merged_segments.sort(key=lambda item: (float(item["start_seconds"]), float(item["end_seconds"])))
        merged_text = _merge_text_parts([str(item["text"]) for item in merged_segments])
    else:
        merged_text = _merge_text_parts(fallback_parts)
    if not merged_text:
        raise ValueError("cloud ASR chunk transcripts are empty")
    return {
        "text": merged_text,
        "segments": merged_segments,
        "language": languages[0] if languages else None,
        "duration_ms": duration_ms,
        "usage_total_token": total_tokens or None,
    }


def _merge_text_parts(parts: Sequence[str]) -> str:
    merged = ""
    for part in parts:
        clean = part.strip()
        if not clean:
            continue
        if not merged:
            merged = clean
            continue
        overlap_end = _normalized_prefix_overlap(merged, clean)
        remainder = clean[overlap_end:].lstrip() if overlap_end else clean
        if not remainder:
            continue
        needs_word_separator = (
            merged[-1:].isascii()
            and merged[-1:].isalnum()
            and remainder[:1].isascii()
            and remainder[:1].isalnum()
        )
        separator = " " if needs_word_separator else ""
        merged = merged.rstrip() + separator + remainder
    return merged


def _normalized_prefix_overlap(left: str, right: str, *, minimum: int = 4) -> int:
    left_normalized = "".join(character.casefold() for character in left if character.isalnum())
    right_positions = [index for index, character in enumerate(right) if character.isalnum()]
    right_normalized = "".join(right[index].casefold() for index in right_positions)
    maximum = min(len(left_normalized), len(right_normalized), 240)
    for length in range(maximum, minimum - 1, -1):
        if left_normalized[-length:] == right_normalized[:length]:
            return right_positions[length - 1] + 1
    return 0
