from __future__ import annotations

def infer_companion_memory_mood(
    *,
    today_activity_count: int,
    recent_7d_activity_count: int,
    pending_memory_candidate_count: int = 0,
) -> str:
    """Project anonymous Library activity into the Companion mood vocabulary."""

    if pending_memory_candidate_count > 0:
        return "curious"
    if today_activity_count >= 3:
        return "focused"
    if today_activity_count >= 1:
        return "curious"
    if recent_7d_activity_count == 0:
        return "idle"
    return "calm"
