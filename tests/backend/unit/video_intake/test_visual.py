from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend.video_intake.visual import _deduplicate_candidates, _frame_metrics


def test_visual_metrics_score_information_dense_frame(tmp_path: Path) -> None:
    path = tmp_path / "frame.jpg"
    image = Image.new("L", (320, 180), color=255)
    for x in range(0, 320, 8):
        for y in range(0, 180, 8):
            if (x + y) % 16 == 0:
                image.paste(0, (x, y, min(x + 6, 320), min(y + 6, 180)))
    image.save(path)

    metrics = _frame_metrics(path, 12.0, "scene_change")

    assert metrics["information_density"] > 0
    assert metrics["score"] > 0
    assert metrics["source"] == "scene_change"


def test_visual_candidates_deduplicate_similar_images(tmp_path: Path) -> None:
    path_a = tmp_path / "a.jpg"
    path_b = tmp_path / "b.jpg"
    Image.new("L", (320, 180), color=128).save(path_a)
    Image.new("L", (320, 180), color=128).save(path_b)
    candidates = [
        _frame_metrics(path_a, 10.0, "uniform"),
        _frame_metrics(path_b, 40.0, "scene_change"),
    ]

    result = _deduplicate_candidates(candidates)

    assert len(result) == 1
