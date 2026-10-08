from __future__ import annotations

from backend.api.ai_execution_control import begin_nested_model_call


class _Handle:
    def model_call_routed(self, **_kwargs) -> None:
        pass

    def model_call_started(self, **_kwargs) -> None:
        pass

    def model_call_completed(self, **_kwargs) -> None:
        pass

    def model_call_failed(self) -> None:
        pass

    def model_call_cache_observed(self, **_kwargs) -> None:
        pass

    def finalize(self, *, error_code: str | None) -> tuple[str, ...]:
        return ()


def test_begin_nested_model_call_forwards_key_and_purpose_to_execution_context() -> None:
    calls: list[dict[str, object]] = []

    class Control:
        def take_nested_model_handle(self, **kwargs):
            calls.append(kwargs)
            return _Handle()

    handle = begin_nested_model_call(
        {"execution_context": Control()},
        invocation_key="video-chunk-2",
        purpose="chunk_summary",
    )

    assert isinstance(handle, _Handle)
    assert calls == [{"invocation_key": "video-chunk-2", "purpose": "chunk_summary"}]


def test_begin_nested_model_call_keeps_zero_argument_control_compatibility() -> None:
    calls = 0

    class Control:
        def take_nested_model_handle(self):
            nonlocal calls
            calls += 1
            return _Handle()

    assert isinstance(begin_nested_model_call({"execution_context": Control()}), _Handle)
    assert calls == 1
