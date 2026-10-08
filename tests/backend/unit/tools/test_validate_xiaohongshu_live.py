from __future__ import annotations

from tools.validate_xiaohongshu_live import gate_exit_code, metadata_failure_status, public_error_code


def test_live_gate_error_projection_allows_only_stable_codes() -> None:
    assert public_error_code(ValueError("network_denied")) == "network_denied"
    assert public_error_code(ValueError("https://private.example/?token=secret")) == "redacted_provider_error"
    assert public_error_code(ValueError("C:\\private\\image.jpg")) == "redacted_provider_error"
    assert public_error_code(ValueError("invalid_source")) == "invalid_source"


def test_live_gate_exits_nonzero_for_every_unverified_or_blocked_state() -> None:
    for status in ("environment_unverified", "platform_blocked", "product_blocked", "sample_not_image_set"):
        assert gate_exit_code({"status": status}) == 2
    for status in ("metadata_passed", "materialization_passed", "passed"):
        assert gate_exit_code({"status": status}) == 0


def test_metadata_failure_status_separates_input_platform_and_environment() -> None:
    assert metadata_failure_status(ValueError("invalid_source")) == "input_invalid"
    assert metadata_failure_status(ValueError("network_denied")) == "environment_unverified"
    assert metadata_failure_status(ValueError("metadata_unavailable")) == "platform_blocked"
