from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket
from PIL import Image, ImageDraw

from backend.shared.filesystem import atomic_write_text
from backend.video_intake.models import ResolvedVideoItem
from backend.video_intake.storage import LibraryStorage


FIXTURE_ID = "BV1UiFixture1"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:4182")
    parser.add_argument("--edge", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-title", default="测试视频资料：App 开发完整流程")
    parser.add_argument("--skip-fixture", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    storage = LibraryStorage(root)
    if not args.skip_fixture:
        _create_fixture(storage)
    profile = root / "temp" / "edge-ui-validation-profile"
    shutil.rmtree(profile, ignore_errors=True)
    edge = subprocess.Popen(
        [
            str(args.edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--remote-allow-origins=*",
            "--remote-debugging-port=9224",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_json("http://127.0.0.1:9224/json/version")
        target = _put_json(
            "http://127.0.0.1:9224/json/new?"
            + urllib.parse.quote(args.base_url + "/", safe="")
        )
        cdp = CdpSession(str(target["webSocketDebuggerUrl"]))
        try:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            captures = []
            captures.append(_capture(cdp, args.base_url + "/", output / "home-desktop.png", 1440, 1000, "份资料"))
            hover_point = _evaluate(
                cdp,
                "(() => { const cell = document.querySelector('.intake-day-cell:not(.level-none):not(:disabled)'); "
                "if (!cell) return null; const box = cell.getBoundingClientRect(); "
                "return { x: box.left + box.width / 2, y: box.top + box.height / 2 }; })()",
            )
            if not isinstance(hover_point, dict):
                raise RuntimeError("没有找到可悬停的日历日期格。")
            cdp.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": hover_point["x"], "y": hover_point["y"]},
            )
            _wait_expression(
                cdp,
                "getComputedStyle(document.querySelector('.intake-day-cell:not(.level-none):not(:disabled)'), '::after').opacity === '1'",
            )
            captures.append(_screenshot(cdp, output / "home-calendar-tooltip.png"))
            _evaluate(cdp, "document.querySelector('.intake-day-cell:not(.level-none):not(:disabled)')?.click()")
            _wait_text(cdp, args.record_title)
            captures.append(_screenshot(cdp, output / "home-calendar-modal.png"))
            captures.append(_capture(cdp, args.base_url + "/?view=library", output / "library-desktop.png", 1440, 1000, args.record_title))
            title_json = json.dumps(args.record_title, ensure_ascii=False)
            _evaluate(cdp, f"[...document.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === '打开 ' + {title_json})?.click()")
            _wait_text(cdp, "画面 ")
            _evaluate(cdp, "[...document.querySelectorAll('button')].find(b => b.textContent.trim().startsWith('画面 '))?.click()")
            _wait_text(cdp, "Mock 联调占位")
            _wait_expression(cdp, "[...document.querySelectorAll('.intake-frame-grid img')].length > 0 && [...document.querySelectorAll('.intake-frame-grid img')].every(img => img.complete && img.naturalWidth > 0)")
            captures.append(_screenshot(cdp, output / "detail-visual-desktop.png"))
            captures.append(_capture(cdp, args.base_url + "/?view=settings", output / "settings-desktop.png", 1440, 1100, "画面理解"))
            captures.append(_capture(cdp, args.base_url + "/", output / "home-narrow.png", 500, 1000, "把一个链接"))
        finally:
            cdp.close()
        print(json.dumps({"captures": captures}, ensure_ascii=False, indent=2))
    finally:
        edge.terminate()
        try:
            edge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            edge.kill()
        if not args.skip_fixture:
            storage.delete_records([FIXTURE_ID])
        shutil.rmtree(profile, ignore_errors=True)


class CdpSession:
    def __init__(self, url: str) -> None:
        self._socket = websocket.create_connection(url, timeout=15, origin="http://localhost")
        self._next_id = 0

    def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self._next_id += 1
        message_id = self._next_id
        self._socket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            response = json.loads(self._socket.recv())
            if response.get("id") != message_id:
                continue
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            result = response.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
        self._socket.close()


def _capture(
    cdp: CdpSession,
    url: str,
    path: Path,
    width: int,
    height: int,
    expected_text: str,
) -> dict[str, object]:
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
    )
    cdp.call("Page.navigate", {"url": url})
    _wait_text(cdp, expected_text)
    time.sleep(0.8)
    return _screenshot(cdp, path)


def _screenshot(cdp: CdpSession, path: Path) -> dict[str, object]:
    result = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
    path.write_bytes(base64.b64decode(str(result["data"])))
    return {"file": str(path), "bytes": path.stat().st_size}


def _wait_text(cdp: CdpSession, text: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _evaluate(cdp, f"document.body?.innerText.includes({json.dumps(text, ensure_ascii=False)})")
        if result is True:
            return
        time.sleep(0.25)
    raise TimeoutError(f"页面没有出现预期文本：{text}")


def _wait_expression(cdp: CdpSession, expression: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _evaluate(cdp, expression) is True:
            return
        time.sleep(0.25)
    raise TimeoutError(f"页面条件未满足：{expression}")


def _evaluate(cdp: CdpSession, expression: str) -> object:
    result = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    return result.get("result", {}).get("value") if isinstance(result.get("result"), dict) else None


def _wait_json(url: str, timeout: float = 15.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.load(response)
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(url)


def _put_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, method="PUT")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def _create_fixture(storage: LibraryStorage) -> None:
    storage.delete_records([FIXTURE_ID])
    item = ResolvedVideoItem(
        key=f"{FIXTURE_ID}:p1",
        bvid=FIXTURE_ID,
        title="测试视频资料：App 开发完整流程",
        source_url=f"https://www.bilibili.com/video/{FIXTURE_ID}/",
        uploader="验收 UP 主",
        published_at="2026-06-01",
        description="用于检查摘要、证据、原文与画面页面的临时资料。",
        tags=["App 开发", "教程", "流程"],
        duration_seconds=120,
    )
    record, record_dir = storage.create_record(item, media_mode="audio")
    record.transcript_source = "whisper"
    record.visual.importance = "high"
    record.visual.reason = "包含流程图、软件界面和操作步骤。"
    record.visual.keyframe_count = 1
    data_dir = record_dir / "data"
    segments = [
        {"start_seconds": 0, "end_seconds": 12, "text": "这段视频说明 App 开发从需求到发布的完整流程。"},
        {"start_seconds": 12, "end_seconds": 30, "text": "第一步先确认需求和原型，再进入界面设计与开发。"},
    ]
    transcript = {"title": record.title, "source": "whisper", "language": "zh", "segments": segments}
    atomic_write_text(data_dir / "transcript.asr.json", json.dumps(transcript, ensure_ascii=False, indent=2))
    atomic_write_text(data_dir / "transcript.cleaned.json", json.dumps(transcript, ensure_ascii=False, indent=2))
    summary = {
        "title": record.title,
        "thirty_second_summary": "App 开发从需求、原型、设计、编码、测试走向发布，每一步都需要可验证的交付物。",
        "one_sentence_summary": "用阶段化交付降低 App 开发返工。",
        "core_problem": "如何把一个 App 想法稳定推进到发布？",
        "key_takeaways": ["先验证需求再编码", "每个阶段保留可验收产物"],
        "detailed_notes": ["需求阶段明确用户和场景", "原型阶段验证流程"],
        "chapters": [{"id": "chapter-1", "title": "开发全流程", "start_seconds": 0, "end_seconds": 30, "summary": "介绍阶段与衔接。", "key_points": ["需求", "设计", "编码"], "evidence_ids": ["ev-1"]}],
        "evidence": [{"id": "ev-1", "statement": "开发从需求开始", "quote": "第一步先确认需求和原型", "start_seconds": 12, "end_seconds": 20, "confidence": "high"}],
        "people": [], "terms": [], "examples": [], "data_points": [], "viewpoints": [], "action_items": [],
    }
    atomic_write_text(data_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    atomic_write_text(data_dir / "summary.md", "# 测试视频资料\n\n## 30 秒摘要\n\n" + summary["thirty_second_summary"] + "\n")
    frame_dir = record_dir / "visual" / "keyframes"
    frame_dir.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (960, 540), "#f5f2ec")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 880, 460), outline="#a90000", width=6)
    draw.text((130, 150), "APP FLOW", fill="#a90000")
    draw.text((130, 240), "IDEA  >  DESIGN  >  CODE  >  TEST  >  RELEASE", fill="#222222")
    frame_path = frame_dir / "frame-001-000015.jpg"
    image.save(frame_path, quality=90)
    visual = {
        "visual_importance": "high", "reason": record.visual.reason,
        "sampled_frame_count": 8, "important_frame_count": 1,
        "local_detection_used": True, "cloud_vision_used": False,
        "cloud_vision_mode": "mock", "cloud_vision_provider": "mock",
        "cloud_vision_model": "deterministic-schema-v1", "suggest_enable_visual_mode": True,
        "keyframes": [{"frame_id": "frame-0001", "timestamp": 15, "timestamp_text": "00:15", "file": "visual/keyframes/frame-001-000015.jpg", "score": 0.88, "information_density": 0.82}],
        "tables": [], "charts": [],
        "visual_claims": [{"claim": "Mock Vision 联调占位，不代表真实识别。", "frame_id": "frame-0001", "timestamp": "00:15", "confidence": "mock", "is_mock": True}],
        "uncertainties": ["Mock Provider 不执行真实 OCR 和画面语义识别。"],
    }
    structured = {
        "metadata": {"title": record.title, "uploader": record.uploader, "description": record.description, "publish_time": record.published_at, "tags": record.tags, "cover_url": "", "source_url": record.source_url, "bvid": record.bvid, "pages": [{"page": 1, "title": record.title}]},
        "sources": {"official_subtitle": {"available": False}, "asr_transcript": {"available": True}, "cleaned_transcript": {"available": True}, "source_preference": ["cleaned_transcript", "asr_transcript"]},
        "chunks": [{"chunk_id": "chunk-0001", "source_type": "cleaned_transcript", "start": 0, "end": 30, "text": " ".join(item["text"] for item in segments), "summary": "App 开发流程", "keywords": ["App", "开发", "需求"]}],
        "timeline": [{"start": 0, "end": 30, "title": "开发全流程", "summary": "介绍阶段与衔接。", "evidence": ["第一步先确认需求和原型"], "chunk_ids": ["chunk-0001"], "frame_ids": ["frame-0001"]}],
        "entities": {"people": [], "organizations": [], "terms": [], "products": [], "locations": []},
        "claims": [{"type": "evidence", "source": "transcript", "claim": "开发从需求开始", "evidence_text": "第一步先确认需求和原型", "timestamp": "00:12–00:20", "chunk_id": "chunk-0001", "frame_id": "", "confidence": "high"}],
        "examples": [], "actions": [],
        "summary": {"thirty_second": summary["thirty_second_summary"], "core_question": summary["core_problem"], "main_conclusions": summary["key_takeaways"], "detailed_notes": summary["detailed_notes"]},
        "visual_analysis": visual,
        "index": {"available": True, "index_type": "json_keyword_time_v1", "chunk_count": 1, "chunk_strategy": "90s", "embedding_model": "", "notes": "测试索引"},
        "retrieval_text": "[chunk-0001] " + " ".join(item["text"] for item in segments),
    }
    atomic_write_text(data_dir / "structured.json", json.dumps(structured, ensure_ascii=False, indent=2))
    atomic_write_text(data_dir / "index.json", json.dumps({"chunk_order": ["chunk-0001"]}, ensure_ascii=False))
    atomic_write_text(data_dir / "visual-analysis.json", json.dumps(visual, ensure_ascii=False, indent=2))
    atomic_write_text(record_dir / record.summary_file, "# 测试视频资料\n\n" + summary["thirty_second_summary"] + "\n")
    atomic_write_text(record_dir / record.transcript_file, "# 完整转写\n\n[00:00] App 开发流程。\n")
    storage.complete(record)


if __name__ == "__main__":
    main()
