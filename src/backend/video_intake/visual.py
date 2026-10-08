from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

from backend.shared.filesystem import atomic_write_text
from backend.video_intake.models import VisualAssessment


VISUAL_KEYWORDS = {
    "ppt": "标题或简介提到 PPT",
    "课件": "标题或简介提到课件",
    "演示": "内容包含演示",
    "操作": "内容包含操作过程",
    "教程": "内容属于教程",
    "图表": "内容包含图表",
    "界面": "内容涉及软件界面",
    "设计": "内容涉及视觉设计",
    "作品集": "内容涉及作品展示",
    "代码": "内容可能包含屏幕代码",
    "建模": "内容可能包含建模画面",
}


@dataclass(frozen=True)
class VisualSamplingResult:
    sampled_frame_count: int
    frames: list[dict[str, object]]
    strategy: str = "uniform_plus_scene_change_with_perceptual_dedup_v1"


def assess_visual_importance(
    *,
    title: str,
    description: str,
    tags: list[str],
    probe_path: Path | None,
    ffmpeg_path: Path | None,
) -> VisualAssessment:
    haystack = " ".join([title, description, *tags]).lower()
    signals = [label for keyword, label in VISUAL_KEYWORDS.items() if keyword in haystack]
    scene_count = _scene_change_count(probe_path, ffmpeg_path) if probe_path is not None else 0
    if scene_count:
        signals.append(f"低清画面样本检测到 {scene_count} 次明显场景变化")
    if len(signals) >= 2 or scene_count >= 8:
        importance = "high"
        reason = "语音之外可能包含较多演示、图表或屏幕信息，建议阅读关键画面。"
    elif signals or scene_count >= 3:
        importance = "medium"
        reason = "画面可能补充语音信息，阅读总结时建议对照关键帧和原视频。"
    else:
        importance = "low"
        reason = "当前未发现强烈的画面依赖信号，优先使用语音结构化较为合适。"
    return VisualAssessment(
        importance=importance,
        reason=reason,
        signals=signals,
        ocr_status="not_available" if importance in {"medium", "high"} else "not_requested",
    )


def extract_visual_candidates(
    media_path: Path,
    record_dir: Path,
    *,
    duration_seconds: float,
    ffmpeg_path: Path | None,
    max_frames: int = 12,
) -> VisualSamplingResult:
    executable = _ffmpeg_executable(ffmpeg_path)
    if not executable or not media_path.exists():
        return VisualSamplingResult(sampled_frame_count=0, frames=[])
    max_frames = max(8, min(20, int(max_frames)))
    work_dir = record_dir / "data" / "visual-candidates"
    output_dir = record_dir / "visual" / "keyframes"
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(output_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = max(1.0, float(duration_seconds or 0.0))
    interval = max(8.0, duration / (max_frames * 1.7))
    uniform_limit = max_frames * 2
    uniform_pattern = work_dir / "uniform-%03d.jpg"
    uniform_command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-vf",
        f"fps=1/{interval:.3f},scale=960:-2",
        "-frames:v",
        str(uniform_limit),
        str(uniform_pattern),
    ]
    _run_ffmpeg(uniform_command, timeout=300)

    scene_pattern = work_dir / "scene-%03d.jpg"
    scene_command = [
        executable,
        "-hide_banner",
        "-y",
        "-i",
        str(media_path),
        "-vf",
        "select='gt(scene,0.28)',showinfo,scale=960:-2",
        "-fps_mode",
        "vfr",
        "-frames:v",
        str(max_frames * 3),
        str(scene_pattern),
    ]
    scene_result = _run_ffmpeg(scene_command, timeout=300)
    scene_times = [float(value) for value in re.findall(r"showinfo.*?pts_time:([0-9.]+)", scene_result.stderr)]

    candidates: list[dict[str, object]] = []
    for index, path in enumerate(sorted(work_dir.glob("uniform-*.jpg"))):
        candidates.append(_frame_metrics(path, min(duration, index * interval), "uniform"))
    for index, path in enumerate(sorted(work_dir.glob("scene-*.jpg"))):
        timestamp = scene_times[index] if index < len(scene_times) else min(duration, index * interval)
        candidates.append(_frame_metrics(path, timestamp, "scene_change"))

    deduplicated = _deduplicate_candidates(candidates)
    desired_count = min(max_frames, max(8, int(math.ceil(duration / 60.0))))
    selected = sorted(
        sorted(deduplicated, key=lambda item: float(item["score"]), reverse=True)[:desired_count],
        key=lambda item: float(item["timestamp"]),
    )
    frames: list[dict[str, object]] = []
    for index, item in enumerate(selected, start=1):
        timestamp = float(item["timestamp"])
        destination = output_dir / f"frame-{index:03d}-{int(timestamp):06d}.jpg"
        shutil.copy2(Path(str(item["path"])), destination)
        frames.append(
            {
                "frame_id": f"frame-{index:04d}",
                "timestamp": timestamp,
                "timestamp_text": _timestamp(timestamp),
                "file": destination.relative_to(record_dir).as_posix(),
                "source": item["source"],
                "score": round(float(item["score"]), 4),
                "sharpness": round(float(item["sharpness"]), 4),
                "contrast": round(float(item["contrast"]), 4),
                "information_density": round(float(item["information_density"]), 4),
                "mock": False,
            }
        )
    shutil.rmtree(work_dir, ignore_errors=True)
    result = VisualSamplingResult(sampled_frame_count=len(candidates), frames=frames)
    atomic_write_text(
        record_dir / "data" / "visual-local.json",
        json.dumps(
            {
                "sampled_frame_count": result.sampled_frame_count,
                "important_frame_count": len(result.frames),
                "strategy": result.strategy,
                "keyframes": result.frames,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return result


def extract_keyframes(
    media_path: Path,
    record_dir: Path,
    *,
    duration_seconds: float,
    ffmpeg_path: Path | None,
) -> int:
    return len(
        extract_visual_candidates(
            media_path,
            record_dir,
            duration_seconds=duration_seconds,
            ffmpeg_path=ffmpeg_path,
        ).frames
    )


def remove_visual_probe(probe_path: Path | None) -> None:
    if probe_path is None:
        return
    probe_dir = probe_path.parent
    if probe_dir.exists() and probe_dir.name == "visual-probe":
        shutil.rmtree(probe_dir, ignore_errors=True)


def _frame_metrics(path: Path, timestamp: float, source: str) -> dict[str, object]:
    try:
        with Image.open(path) as image:
            grayscale = image.convert("L").resize((320, 180))
            contrast = min(1.0, float(ImageStat.Stat(grayscale).stddev[0]) / 72.0)
            edges = grayscale.filter(ImageFilter.FIND_EDGES)
            edge_mean = float(ImageStat.Stat(edges).mean[0]) / 255.0
            sharpness = min(1.0, edge_mean * 7.5)
            information_density = min(1.0, edge_mean * 5.0 + contrast * 0.35)
            signature = _average_hash(grayscale)
    except (OSError, ValueError):
        contrast = sharpness = information_density = 0.0
        signature = "0" * 64
    scene_bonus = 0.14 if source == "scene_change" else 0.0
    score = min(1.0, contrast * 0.25 + sharpness * 0.30 + information_density * 0.45 + scene_bonus)
    return {
        "path": str(path),
        "timestamp": max(0.0, timestamp),
        "source": source,
        "score": score,
        "sharpness": sharpness,
        "contrast": contrast,
        "information_density": information_density,
        "signature": signature,
    }


def _deduplicate_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    accepted: list[dict[str, object]] = []
    for candidate in sorted(candidates, key=lambda item: float(item["score"]), reverse=True):
        signature = str(candidate["signature"])
        timestamp = float(candidate["timestamp"])
        duplicate = any(
            _hamming(signature, str(existing["signature"])) <= 7
            or abs(timestamp - float(existing["timestamp"])) < 3.0
            for existing in accepted
        )
        if not duplicate:
            accepted.append(candidate)
    return accepted


def _average_hash(image: Image.Image) -> str:
    pixels = list(image.resize((8, 8)).tobytes())
    average = sum(pixels) / max(1, len(pixels))
    return "".join("1" if pixel >= average else "0" for pixel in pixels)


def _hamming(left: str, right: str) -> int:
    return sum(a != b for a, b in zip(left, right, strict=False)) + abs(len(left) - len(right))


def _scene_change_count(probe_path: Path, ffmpeg_path: Path | None) -> int:
    executable = _ffmpeg_executable(ffmpeg_path)
    if not executable or not probe_path.exists():
        return 0
    command = [
        executable,
        "-hide_banner",
        "-i",
        str(probe_path),
        "-vf",
        "select='gt(scene,0.28)',showinfo",
        "-f",
        "null",
        "-",
    ]
    result = _run_ffmpeg(command, timeout=300)
    return len(re.findall(r"showinfo.*pts_time:", result.stderr))


def _run_ffmpeg(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr=str(error))


def _ffmpeg_executable(ffmpeg_path: Path | None) -> str | None:
    return str(ffmpeg_path) if ffmpeg_path and ffmpeg_path.exists() else shutil.which("ffmpeg")


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remainder = divmod(total, 60)
    return f"{minutes:02d}:{remainder:02d}"
