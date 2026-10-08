from core.product_core.long_audio_decider import (
    LongAudioChunkFact,
    decide_long_audio_result,
    plan_long_audio_chunks,
)


def test_long_audio_planner_is_deterministic_and_side_effect_free() -> None:
    plans = plan_long_audio_chunks(2000.0, chunk_duration_seconds=900.0)

    assert [(plan.start_seconds, plan.end_seconds) for plan in plans] == [
        (0.0, 900.0), (900.0, 1800.0), (1800.0, 2000.0),
    ]


def test_long_audio_decider_projects_partial_effect_facts_without_retry_state() -> None:
    plans = plan_long_audio_chunks(1800.0, chunk_duration_seconds=900.0)
    result = decide_long_audio_result(
        source_id="source-1", project_id=None, audio_asset_id="asset-1",
        total_duration_seconds=1800.0,
        facts=(
            LongAudioChunkFact(plans[0], "output-1", None),
            LongAudioChunkFact(plans[1], None, "Effect is INFLIGHT"),
        ),
        merged_output_id="merged-1", merged_preview="preview",
    )

    assert result.status == "partial_completed"
    assert result.completed_chunk_count == 1
    assert result.failed_chunk_count == 1
    assert result.chunks[1].retry_count == 0
    assert result.next_step == "transcript_ready"
