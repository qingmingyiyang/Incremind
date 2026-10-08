from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.video_summary.domain.models import SummaryDocument, Transcript, TranscriptSegment
from backend.video_summary.generation.usecases.generate_summary import GenerateVideoSummary
from backend.video_summary.infrastructure.filesystem_generation_artifact_store import FileSystemGenerationArtifactStore


class _FakeMediaProcessor:
    def probe_duration(self, video_path: Path) -> float:
        if not video_path.exists():
            video_path.parent.mkdir(parents=True, exist_ok=True)
            video_path.write_text("video", encoding="utf-8")
        return 10.0

    def extract_audio(self, video_path: Path, audio_path: Path, cancellation=None) -> Path:
        audio_path.write_text("audio", encoding="utf-8")
        return audio_path


class _FakeTranscriber:
    def transcribe(self, audio_path, output_stem, on_progress=None) -> Transcript:
        return Transcript(
            language="zh",
            segments=[TranscriptSegment(start_seconds=1.0, end_seconds=2.0, text="cached transcript")],
        )


class _RecordingSummarizer:
    """Fake summarizer that emits two governed wire attempts through the sink."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.received_sink = None

    async def summarize(self, video, transcript, cancellation=None, wire_attempt_sink=None) -> SummaryDocument:
        self.calls += 1
        self.received_sink = wire_attempt_sink
        if wire_attempt_sink is not None:
            first = wire_attempt_sink.begin_model_wire_attempt()
            first.succeeded(usage={"prompt_tokens": 7, "completion_tokens": 2})
            second = wire_attempt_sink.begin_model_wire_attempt()
            if self.fail:
                second.failed_transport(error_code="ai.connection_error")
                raise RuntimeError("模型服务调用失败")
            second.succeeded(usage={"prompt_tokens": 5, "completion_tokens": 3})
        return SummaryDocument(markdown="# Test", summary_data={"title": "Test"})


def _build_use_case(summarizer) -> GenerateVideoSummary:
    return GenerateVideoSummary(
        media_processor=_FakeMediaProcessor(),
        transcriber=_FakeTranscriber(),
        transcript_enhancer=None,
        summarizer=summarizer,
        artifact_store=FileSystemGenerationArtifactStore(),
    )


async def _fake_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


class GenerateVideoSummaryWireAttemptsTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_generation_persists_wire_attempts_next_to_artifacts(self) -> None:
        summarizer = _RecordingSummarizer()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "workspace" / "series-1" / "video-1"
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                document = await _build_use_case(summarizer).run(root / "v.mp4", output_dir)

            self.assertEqual(document.summary_data["title"], "Test")
            attempts_path = output_dir / "wire-attempts.json"
            self.assertTrue(attempts_path.exists())
            payload = json.loads(attempts_path.read_text(encoding="utf-8"))
            records = payload["records"]
            self.assertEqual([record["status"] for record in records], ["succeeded", "succeeded"])
            self.assertEqual(records[0]["usage"], {"prompt_tokens": 7, "completion_tokens": 2})
            self.assertTrue(all(record["stage"] == "summary" for record in records))
            self.assertEqual(records[0]["attempt_number"], 1)
            self.assertEqual(records[1]["attempt_number"], 2)
            self.assertTrue((output_dir / "summary.json").exists())

    async def test_failed_generation_still_persists_wire_attempts(self) -> None:
        summarizer = _RecordingSummarizer(fail=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "workspace" / "series-1" / "video-1"
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with self.assertRaises(RuntimeError):
                    await _build_use_case(summarizer).run(root / "v.mp4", output_dir)

            attempts_path = output_dir / "wire-attempts.json"
            self.assertTrue(attempts_path.exists())
            payload = json.loads(attempts_path.read_text(encoding="utf-8"))
            records = payload["records"]
            self.assertEqual([record["status"] for record in records], ["succeeded", "failed_transport"])
            self.assertEqual(records[1]["error_code"], "ai.connection_error")
            self.assertFalse((output_dir / "summary.json").exists())

    async def test_run_without_model_calls_writes_empty_attempts_file(self) -> None:
        class _NoWireSummarizer:
            async def summarize(self, video, transcript, cancellation=None, wire_attempt_sink=None) -> SummaryDocument:
                return SummaryDocument(markdown="# Test", summary_data={"title": "Test"})

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "workspace" / "series-1" / "video-1"
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                await _build_use_case(_NoWireSummarizer()).run(root / "v.mp4", output_dir)

            payload = json.loads((output_dir / "wire-attempts.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["records"], [])


if __name__ == "__main__":
    unittest.main()
