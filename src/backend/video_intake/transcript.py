from __future__ import annotations

import json
import re
from pathlib import Path

from backend.shared.filesystem import atomic_write_text
from backend.video_summary.domain.models import Transcript, TranscriptSegment


def find_preferred_subtitle(media_dir: Path) -> Path | None:
    candidates = list(media_dir.glob("source*.srt")) + list(media_dir.glob("source*.vtt"))
    if not candidates:
        return None
    priorities = ("zh-hans", "zh-cn", ".zh.", "zh-hant", "en")
    return sorted(
        candidates,
        key=lambda path: next(
            (index for index, token in enumerate(priorities) if token in path.name.lower()),
            len(priorities),
        ),
    )[0]


def parse_subtitle(path: Path) -> Transcript:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    segments: list[TranscriptSegment] = []
    for block in blocks:
        lines = [line.strip("\ufeff ") for line in block.splitlines() if line.strip()]
        timestamp_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timestamp_index < 0:
            continue
        start_raw, end_raw = [part.strip().split(" ")[0] for part in lines[timestamp_index].split("-->", 1)]
        body = " ".join(lines[timestamp_index + 1 :])
        body = re.sub(r"<[^>]+>", "", body).strip()
        if not body:
            continue
        segments.append(
            TranscriptSegment(
                start_seconds=_timestamp_seconds(start_raw),
                end_seconds=_timestamp_seconds(end_raw),
                text=body,
            )
        )
    if not segments:
        raise ValueError(f"字幕文件没有可解析的时间轴：{path.name}")
    return Transcript(language=_language_from_name(path.name), segments=_deduplicate(segments))


def transcript_payload(title: str, duration: float, transcript: Transcript, *, source: str) -> dict[str, object]:
    return {
        "title": title,
        "language": transcript.language,
        "duration_seconds": duration,
        "source": source,
        "segments": [
            {
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "text": segment.text,
            }
            for segment in transcript.segments
        ],
    }


def write_transcript_json(
    path: Path,
    *,
    title: str,
    duration: float,
    transcript: Transcript,
    source: str,
) -> None:
    atomic_write_text(
        path,
        json.dumps(transcript_payload(title, duration, transcript, source=source), ensure_ascii=False, indent=2),
    )


def render_transcript_markdown(payload: dict[str, object]) -> str:
    lines = [f"# {payload.get('title') or '完整转写'}", ""]
    lines.append(f"> 转写来源：{payload.get('source') or 'unknown'}")
    lines.append("")
    for segment in payload.get("segments", []):
        if not isinstance(segment, dict):
            continue
        start = _format_timestamp(float(segment.get("start_seconds", 0.0)))
        end = _format_timestamp(float(segment.get("end_seconds", 0.0)))
        lines.append(f"**[{start}–{end}]** {str(segment.get('text', '')).strip()}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _timestamp_seconds(value: str) -> float:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
    except ValueError:
        return 0.0
    return 0.0


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _language_from_name(name: str) -> str:
    lowered = name.lower()
    if any(token in lowered for token in ("zh", "hans", "hant")):
        return "zh"
    if ".en" in lowered:
        return "en"
    return "unknown"


def _deduplicate(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    result: list[TranscriptSegment] = []
    previous = ""
    for segment in segments:
        text = segment.text.strip()
        if not text or text == previous:
            continue
        result.append(segment)
        previous = text
    return result
