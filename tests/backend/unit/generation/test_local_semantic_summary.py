from __future__ import annotations

from backend.video_summary.infrastructure.local_semantic_summary import (
    build_semantic_extractive_summary,
)


def _embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        vectors.append([
            float(len(text) + 1),
            float(text.count("架构") + 1),
            float(text.count("测试") + 1),
        ])
    return vectors


def test_semantic_extractive_summary_keeps_every_claim_in_timestamped_transcript() -> None:
    segments = [
        {"start_seconds": 0.0, "end_seconds": 20.0, "text": "先建立项目架构边界。"},
        {"start_seconds": 20.0, "end_seconds": 50.0, "text": "测试需要覆盖真实用户流程。"},
        {"start_seconds": 50.0, "end_seconds": 80.0, "text": "上下文隔离能够降低修改冲突。"},
        {"start_seconds": 80.0, "end_seconds": 120.0, "text": "发布之前应重新验证架构和测试。"},
    ]
    transcript = "\n".join(str(item["text"]) for item in segments)

    result = build_semantic_extractive_summary(
        {"title": "真实项目讲解", "segments": segments},
        embed=_embed,
    )

    assert result["summary_method"] == "semantic_extractive_bge_v1"
    assert result["generative_model_used"] is False
    assert result["evidence"]
    assert result["chapters"]
    for evidence in result["evidence"]:
        for sentence in str(evidence["quote"]).split(" "):
            assert sentence in transcript
        assert evidence["start_seconds"] < evidence["end_seconds"]
    candidates = result["memory_candidate_payload"]["candidates"]
    assert {item["target_layer"] for item in candidates} == {
        "atom", "scenario", "series_memory", "project_skill"
    }
    assert all(item["status"] == "pending_review" for item in candidates)
    assert all(item["review"]["auto_promote_allowed"] is False for item in candidates)


def test_semantic_extractive_summary_rejects_missing_timestamp_segments() -> None:
    try:
        build_semantic_extractive_summary({"title": "无证据", "segments": []}, embed=_embed)
    except ValueError as error:
        assert str(error) == "timestamped transcript segments are required"
    else:
        raise AssertionError("expected timestamp evidence guard")
