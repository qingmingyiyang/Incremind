from __future__ import annotations

from backend.shared.llm import RequestScopedWireAttemptRecorder


def _recorder() -> RequestScopedWireAttemptRecorder:
    return RequestScopedWireAttemptRecorder(
        request_id="video-summary:request-alpha",
        stage="summary",
        model_identity="fake.gateway",
    )


def test_success_records_usage_and_cache_observation() -> None:
    recorder = _recorder()

    handle = recorder.begin_model_wire_attempt()
    handle.succeeded(usage={"prompt_tokens": 12, "completion_tokens": 3}, cache_observation={"cache_read_input_tokens": 4})

    records = recorder.records
    assert len(records) == 1
    record = records[0]
    assert record.request_id == "video-summary:request-alpha"
    assert record.stage == "summary"
    assert record.attempt_number == 1
    assert record.model_identity == "fake.gateway"
    assert record.status == "succeeded"
    assert record.usage == {"prompt_tokens": 12, "completion_tokens": 3}
    assert record.cache_observation == {"cache_read_input_tokens": 4}
    assert record.error_code is None
    assert record.started_at <= record.completed_at
    assert record.attempt_id.startswith("video-summary:request-alpha:summary:1:")
    assert record.to_dict()["status"] == "succeeded"


def test_failed_transport_records_error_code() -> None:
    recorder = _recorder()

    handle = recorder.begin_model_wire_attempt()
    handle.failed_transport(error_code="ai.connection_error")

    record = recorder.records[0]
    assert record.status == "failed_transport"
    assert record.error_code == "ai.connection_error"
    assert record.usage is None
    assert record.cache_observation is None


def test_consumer_cancelled_records_cancelled_terminal() -> None:
    recorder = _recorder()

    handle = recorder.begin_model_wire_attempt()
    handle.consumer_cancelled()

    record = recorder.records[0]
    assert record.status == "consumer_cancelled"
    assert record.error_code is None


def test_terminal_is_exactly_once() -> None:
    recorder = _recorder()

    handle = recorder.begin_model_wire_attempt()
    handle.succeeded(usage={"prompt_tokens": 1})
    handle.failed_transport(error_code="ai.late_terminal")
    handle.consumer_cancelled()

    records = recorder.records
    assert len(records) == 1
    assert records[0].status == "succeeded"


def test_attempt_numbers_increment_across_attempts() -> None:
    recorder = _recorder()

    first = recorder.begin_model_wire_attempt()
    first.succeeded(usage={})
    second = recorder.begin_model_wire_attempt()
    second.consumer_cancelled()

    assert [record.attempt_number for record in recorder.records] == [1, 2]
    assert len({record.attempt_id for record in recorder.records}) == 2


def test_records_are_immutable_snapshots() -> None:
    recorder = _recorder()
    handle = recorder.begin_model_wire_attempt()
    usage = {"prompt_tokens": 5}
    handle.succeeded(usage=usage)
    usage["prompt_tokens"] = 99

    assert recorder.records[0].usage == {"prompt_tokens": 5}


def test_empty_recorder_has_no_records() -> None:
    assert _recorder().records == ()
