from __future__ import annotations

import pytest

from core.media_hands import MediaHandsPolicySourceError, default_personal_workbench_policy_snapshot, load_media_hands_policy


def test_default_snapshot_is_disabled_and_returns_fresh_data() -> None:
    first = default_personal_workbench_policy_snapshot()
    second = default_personal_workbench_policy_snapshot()
    assert first["enabled"] is False
    first["revision"] = "changed"
    assert second["revision"] == "personal-workbench-v1"
    with pytest.raises(MediaHandsPolicySourceError, match="disabled"):
        load_media_hands_policy(second)


def test_enabled_exact_snapshot_loads_immutable_policy() -> None:
    snapshot = default_personal_workbench_policy_snapshot(); snapshot["enabled"] = True
    policy = load_media_hands_policy(snapshot)
    assert set(policy.operation_profiles) == {"analyze_source", "extract_audio_track", "transcribe_video", "extract_images"}
    with pytest.raises(TypeError):
        policy.lane_max_queue["download"] = 3  # type: ignore[index]


@pytest.mark.parametrize("mutate", [
    lambda value: value.pop("revision"),
    lambda value: value.__setitem__("revision", ""),
    lambda value: value.__setitem__("unknown", True),
    lambda value: value["operations"].pop("extract_images"),
])
def test_missing_or_invalid_snapshot_fails_closed(mutate) -> None:
    snapshot = default_personal_workbench_policy_snapshot(); snapshot["enabled"] = True
    mutate(snapshot)
    with pytest.raises(MediaHandsPolicySourceError):
        load_media_hands_policy(snapshot)
