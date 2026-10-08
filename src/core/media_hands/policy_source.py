"""Local, declarative Media Hands policy snapshots; no runtime wiring or I/O."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .provisioner import LANES, OPERATIONS, MediaHandsPolicy, MediaOperationProfile, MediaResourceBudget


class MediaHandsPolicySourceError(ValueError):
    pass


_BUDGET_KEYS = tuple(MediaResourceBudget.__dataclass_fields__)
_PERSONAL_BUDGET = {
    "max_download_bytes": 536_870_912,
    "max_media_cpu_ms": 1_800_000,
    "max_asr_audio_ms": 7_200_000,
    "max_vision_frames": 120,
    "max_model_input_tokens": 100_000,
    "max_model_output_tokens": 20_000,
    "max_wall_ms": 7_200_000,
}
DEFAULT_PERSONAL_WORKBENCH_POLICY: dict[str, object] = {
    "schema_version": "1.0.0", "enabled": False, "revision": "personal-workbench-v1",
    "lane_max_queue": {lane: 2 for lane in LANES},
    "lane_max_concurrency": {lane: 1 for lane in LANES},
    "operations": {
        "analyze_source": {"required_lanes": ["download", "asr", "vision", "model", "media_cpu"], "budget": dict(_PERSONAL_BUDGET)},
        "extract_audio_track": {"required_lanes": ["download", "media_cpu"], "budget": dict(_PERSONAL_BUDGET)},
        "transcribe_video": {"required_lanes": ["download", "asr", "media_cpu"], "budget": dict(_PERSONAL_BUDGET)},
        "extract_images": {"required_lanes": ["download", "vision", "media_cpu"], "budget": dict(_PERSONAL_BUDGET)},
    },
}


def default_personal_workbench_policy_snapshot() -> dict[str, object]:
    """Return a fresh disabled snapshot, never derived from tool arguments."""
    return deepcopy(DEFAULT_PERSONAL_WORKBENCH_POLICY)


def load_media_hands_policy(snapshot: Mapping[str, object]) -> MediaHandsPolicy:
    normalized = validate_media_hands_policy_snapshot(snapshot)
    if normalized["enabled"] is not True:
        raise MediaHandsPolicySourceError("media policy snapshot is missing or disabled")
    return _policy_from_validated_snapshot(normalized)


def validate_media_hands_policy_snapshot(
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Validate enabled or disabled policy data without granting readiness."""

    if not isinstance(snapshot, Mapping) or set(snapshot) != {"schema_version", "enabled", "revision", "lane_max_queue", "lane_max_concurrency", "operations"}:
        raise MediaHandsPolicySourceError("media policy snapshot fields are not exact")
    if snapshot["schema_version"] != "1.0.0" or not isinstance(snapshot["enabled"], bool):
        raise MediaHandsPolicySourceError("media policy schema or enabled flag is invalid")
    revision = snapshot["revision"]
    if not isinstance(revision, str) or not revision:
        raise MediaHandsPolicySourceError("media policy revision is required")
    queue = _lane_limits(snapshot["lane_max_queue"], "lane_max_queue")
    concurrency = _lane_limits(snapshot["lane_max_concurrency"], "lane_max_concurrency")
    operations = snapshot["operations"]
    if not isinstance(operations, Mapping) or set(operations) != set(OPERATIONS):
        raise MediaHandsPolicySourceError("media policy operations are not exact")
    profiles: dict[str, MediaOperationProfile] = {}
    for operation in OPERATIONS:
        profile = operations[operation]
        if not isinstance(profile, Mapping) or set(profile) != {"required_lanes", "budget"}:
            raise MediaHandsPolicySourceError("media operation profile fields are not exact")
        lanes = profile["required_lanes"]
        budget = profile["budget"]
        if not isinstance(lanes, list) or not all(isinstance(lane, str) for lane in lanes) or not isinstance(budget, Mapping) or set(budget) != set(_BUDGET_KEYS):
            raise MediaHandsPolicySourceError("media operation profile is invalid")
        try:
            profiles[operation] = MediaOperationProfile(tuple(lanes), MediaResourceBudget(**dict(budget)))
        except (TypeError, ValueError) as exc:
            raise MediaHandsPolicySourceError("media operation profile is invalid") from exc
    normalized = deepcopy(dict(snapshot))
    _policy_from_validated_snapshot(normalized)
    return normalized


def _policy_from_validated_snapshot(snapshot: Mapping[str, object]) -> MediaHandsPolicy:
    revision = snapshot["revision"]
    queue = _lane_limits(snapshot["lane_max_queue"], "lane_max_queue")
    concurrency = _lane_limits(snapshot["lane_max_concurrency"], "lane_max_concurrency")
    operations = snapshot["operations"]
    profiles: dict[str, MediaOperationProfile] = {}
    assert isinstance(operations, Mapping)
    for operation in OPERATIONS:
        profile = operations[operation]
        assert isinstance(profile, Mapping)
        lanes = profile["required_lanes"]
        budget = profile["budget"]
        assert isinstance(lanes, list) and isinstance(budget, Mapping)
        profiles[operation] = MediaOperationProfile(
            tuple(lanes), MediaResourceBudget(**dict(budget))
        )
    try:
        return MediaHandsPolicy(str(revision), queue, concurrency, profiles)
    except ValueError as exc:
        raise MediaHandsPolicySourceError("media policy snapshot is invalid") from exc


def _lane_limits(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(LANES) or any(not isinstance(limit, int) or isinstance(limit, bool) or limit < 0 for limit in value.values()):
        raise MediaHandsPolicySourceError(f"{name} is invalid")
    return dict(value)
