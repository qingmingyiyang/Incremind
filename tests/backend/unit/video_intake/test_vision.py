from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from backend.video_intake.vision import (
    MockVisionProvider,
    VisionSettings,
    analyze_and_merge_visuals,
    render_visual_markdown,
    _nearby_context,
)


def test_mock_provider_is_explicit_and_schema_valid(tmp_path: Path) -> None:
    result = asyncio.run(
        MockVisionProvider().analyze(
            record_dir=tmp_path,
            frames=[{"frame_id": "frame-0001", "timestamp_text": "00:10"}],
            nearby_context={"frame-0001": "附近语音"},
        )
    )

    assert result["mode"] == "mock"
    assert result["provider"] == "mock"
    assert result["is_mock"] is True
    assert result["visual_claims"][0]["is_mock"] is True
    assert "不代表" in result["visual_claims"][0]["claim"]


def test_mock_result_merges_into_json_and_markdown(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    structured = {
        "chunks": [{"chunk_id": "chunk-1", "start": 0, "end": 20, "text": "附近语音", "keywords": []}],
        "timeline": [{"start": 0, "end": 20, "frame_ids": []}],
        "claims": [],
        "visual_analysis": {
            "keyframes": [{"frame_id": "frame-0001", "timestamp": 10, "timestamp_text": "00:10", "file": "visual/keyframes/a.jpg", "score": 0.8}],
            "sampled_frame_count": 4,
            "important_frame_count": 1,
        },
    }

    merged = asyncio.run(
        analyze_and_merge_visuals(
            record_dir=tmp_path,
            structured=structured,
            settings=VisionSettings(mode="mock"),
            requested=True,
        )
    )

    visual = merged["visual_analysis"]
    assert visual["cloud_vision_mode"] == "mock"
    assert visual["cloud_vision_used"] is False
    assert visual["is_mock"] is True
    assert merged["timeline"][0]["frame_ids"] == ["frame-0001"]
    assert merged["claims"][-1]["type"] == "visual_mock"
    assert "Mock 联调占位" in render_visual_markdown(merged)
    saved = json.loads((tmp_path / "data" / "structured.json").read_text(encoding="utf-8"))
    assert saved["visual_analysis"]["cloud_vision_provider"] == "mock"


def test_nearby_context_uses_time_range_instead_of_keyword_ranking() -> None:
    structured = {
        "chunks": [
            {"chunk_id": "early", "start": 0, "end": 30, "text": "开场"},
            {"chunk_id": "late", "start": 600, "end": 650, "text": "结尾部署步骤"},
        ]
    }

    contexts = _nearby_context(structured, [{"frame_id": "frame-late", "timestamp": 620}])

    assert contexts["frame-late"] == "结尾部署步骤"


def test_mock_provider_failure_is_recorded_without_raising(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    structured = {"chunks": [], "timeline": [], "claims": [], "visual_analysis": {"keyframes": []}}

    async def fail(*args, **kwargs):
        raise RuntimeError("mock provider unavailable")

    with patch.object(MockVisionProvider, "analyze", fail):
        merged = asyncio.run(
            analyze_and_merge_visuals(
                record_dir=tmp_path,
                structured=structured,
                settings=VisionSettings(mode="mock"),
                requested=True,
            )
        )

    assert merged["visual_analysis"]["cloud_vision_mode"] == "mock"
    assert any("未阻塞" in item for item in merged["visual_analysis"]["uncertainties"])
