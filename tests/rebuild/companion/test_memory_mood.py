from __future__ import annotations

from core.companion_core.memory_mood import infer_companion_memory_mood


def test_memory_mood_is_driven_by_memory_activity_not_generic_jobs() -> None:
    def mood(today: int, recent: int, pending: int) -> str:
        return infer_companion_memory_mood(
            today_activity_count=today,
            recent_7d_activity_count=recent,
            pending_memory_candidate_count=pending,
        )

    assert mood(5, 5, 1) == "curious"
    assert mood(3, 3, 0) == "focused"
    assert mood(1, 1, 0) == "curious"
    assert mood(0, 3, 0) == "calm"
    assert mood(0, 0, 0) == "idle"
