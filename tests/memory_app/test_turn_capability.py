import json

import pytest

from backend.memory_app.turn_capability import (
    RECOGNITION_TASK_CAPABILITY,
    RECOGNITION_TASK_LOCAL_CAPABILITY,
    RECOGNITION_TASK_OUTCOME,
    RecognitionTaskCapability,
    recognition_task_capability_definition,
)
from core.ai_kernel import InMemoryTurnPayloadStore
from core.ai_kernel.dispatcher import ToolProviderFailure


class Handle:
    def __init__(self):
        self.finalized = []

    def model_call_routed(self, **_kwargs): pass
    def model_call_started(self, **_kwargs): pass
    def model_call_completed(self, **_kwargs): pass
    def model_call_failed(self): pass
    def model_call_cache_observed(self, **_kwargs): pass

    def finalize(self, *, error_code):
        self.finalized.append(error_code)
        return ("crp://session/turn-a/model-attempt/one",)


class Control:
    remaining_timeout_ms = 20_000
    cancel_requested = False

    def __init__(self):
        self.handle = Handle()

    def checkpoint(self): pass

    def take_nested_model_handle(self, **kwargs):
        assert kwargs == {"invocation_key": "recognition-task", "purpose": "primary"}
        return self.handle


class Models:
    def __init__(self, *, failure=None):
        self.calls = []
        self.failure = failure

    def complete_governed(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.failure:
            raise self.failure
        return "private generated answer", {"model": "test-model", "configuration_revision": 4, "usage": {"total_tokens": 3}}


def _request():
    return {
        "turn_id": "turn-a", "scope": {"project_id": "project-a"},
        "arguments": {"task_id": "task-a"}, "execution_context": Control(),
    }


def _loaded(*, existing_result=None):
    return {
        "task_id": "task-a", "project_id": "project-a", "context_packet_id": "packet-a",
        "messages": [{"role": "user", "content": "private source prompt"}],
        "routing_snapshot": {"newturn_routing": "generation-binding"},
        **({"existing_result": existing_result} if existing_result is not None else {}),
    }


def test_definitions_keep_separate_approval_gated_local_and_remote_boundaries():
    definition = recognition_task_capability_definition()
    assert (definition.capability_id, definition.mode, definition.requires_approval, definition.operation_semantics) == (
        RECOGNITION_TASK_CAPABILITY, "external", True, "receipt_required",
    )
    assert definition.tool_definition is not None
    assert definition.tool_definition.nested_model_handle_budget == 1
    assert definition.tool_definition.effect == "external"
    assert definition.tool_definition.destination == "provider"
    local = recognition_task_capability_definition(local=True)
    assert (local.capability_id, local.mode, local.requires_approval, local.operation_semantics) == (
        RECOGNITION_TASK_LOCAL_CAPABILITY, "write", True, "receipt_required",
    )
    assert local.tool_definition.nested_model_handle_budget == 1
    assert local.tool_definition.effect == "write"
    assert local.tool_definition.destination == "local"


def test_success_replay_only_persists_identity_receipt_and_never_reissues_model():
    models, payloads, commits = Models(), InMemoryTurnPayloadStore(), []

    def commit(_request, _loaded, answer, metadata):
        commits.append((answer, metadata))
        return {"task_id": "task-a", "document_id": "document-a", "document_revision": 2}

    capability = RecognitionTaskCapability(models=models, payloads=payloads, load_task=lambda _request: _loaded(), commit_result=commit)
    first = capability.invoke(_request())
    second = capability.invoke(_request())

    assert first["result"]["kind"] == RECOGNITION_TASK_OUTCOME
    assert first["result"]["content"] == {"task_id": "task-a", "document_id": "document-a", "document_revision": 2}
    assert second["result"] == first["result"]
    assert len(models.calls) == len(commits) == 1
    assert commits[0][0] == "private generated answer"
    assert "private generated answer" not in json.dumps(payloads.get(first["receipt_ref"]))
    assert "private source prompt" not in json.dumps(payloads.get(first["receipt_ref"]))
    assert "private generated answer" not in json.dumps(first)


def test_existing_authority_result_repairs_receipt_without_second_model_call():
    models, payloads, commits = Models(), InMemoryTurnPayloadStore(), []
    capability = RecognitionTaskCapability(
        models=models, payloads=payloads,
        load_task=lambda _request: _loaded(existing_result={"task_id": "task-a", "document_id": "document-a", "document_revision": 7}),
        commit_result=lambda *_args: commits.append(True),
    )

    result = capability.invoke(_request())

    assert result["result"]["content"]["document_revision"] == 7
    assert not models.calls and not commits
    assert result["receipt_ref"].startswith("crp://session/turn-a/")


def test_invalid_or_stale_authority_never_calls_model():
    models = Models()
    capability = RecognitionTaskCapability(
        models=models, payloads=InMemoryTurnPayloadStore(),
        load_task=lambda _request: {**_loaded(), "project_id": "another-project"},
        commit_result=lambda *_args: pytest.fail("commit must not run"),
    )

    with pytest.raises(ToolProviderFailure) as error:
        capability.invoke(_request())

    assert error.value.error_code == "recognition.task.source_invalid"
    assert error.value.effect_certainty == "confirmed_none"
    assert not models.calls


def test_authority_validator_reaches_governed_model_without_entering_receipt():
    checked = []
    validator = lambda: checked.append(True)
    class GuardedModels(Models):
        def complete_governed(self, messages, **kwargs):
            assert kwargs["validate_current"] is validator
            kwargs["validate_current"]()
            return super().complete_governed(messages, **kwargs)
    models, payloads = GuardedModels(), InMemoryTurnPayloadStore()
    capability = RecognitionTaskCapability(
        models=models, payloads=payloads,
        load_task=lambda _request: {**_loaded(), "validate_current": validator},
        commit_result=lambda *_args: {"task_id": "task-a", "document_id": "document-a", "document_revision": 1},
    )
    result = capability.invoke(_request())
    assert checked == [True]
    assert "validate_current" not in json.dumps(payloads.get(result["receipt_ref"]))


def test_invalid_authority_validator_rejected_before_model():
    models = Models()
    capability = RecognitionTaskCapability(
        models=models, payloads=InMemoryTurnPayloadStore(),
        load_task=lambda _request: {**_loaded(), "validate_current": "not-callable"},
        commit_result=lambda *_args: pytest.fail("must not commit"),
    )
    with pytest.raises(ToolProviderFailure) as error:
        capability.invoke(_request())
    assert error.value.error_code == "recognition.task.source_invalid"
    assert models.calls == []


def test_model_failure_finalizes_and_keeps_effect_certainty_unknown():
    models, payloads = Models(failure=RuntimeError("provider body is private")), InMemoryTurnPayloadStore()
    control = Control()
    request = _request()
    request["execution_context"] = control
    capability = RecognitionTaskCapability(
        models=models, payloads=payloads, load_task=lambda _request: _loaded(),
        commit_result=lambda *_args: pytest.fail("commit must not run"),
    )

    with pytest.raises(ToolProviderFailure) as error:
        capability.invoke(request)

    assert error.value.error_code == "recognition.task.model_failed"
    assert error.value.effect_certainty == "unknown"
    assert control.handle.finalized == ["ai.nested_model_failed"]
    assert payloads.get_immutable_payload("turn-a", "recognition-task-receipt-v1") is None
