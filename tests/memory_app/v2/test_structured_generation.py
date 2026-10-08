import json

import pytest
from pydantic import BaseModel, ValidationError

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.security.secrets import InMemorySecretStore
from backend.recognition import RecognitionConflict
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_workbench_ask import env as workbench_env, assemble, publish


class Answer(BaseModel):
    answer: str


@pytest.fixture
def model(tmp_path):
    calls = []
    def completion(**request):
        calls.append(request)
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"ok"}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    config = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / "models.sqlite3"), tmp_path,
        InMemorySecretStore(), completion_fn=completion)
    config.update("generation", {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
        "api_key": "test-private-value", "allow_remote": True, "expected_revision": 0})
    return config, calls


def test_native_structured_generation_uses_compatible_mode_and_retains_usage(model):
    from backend.shared.llm.litellm_gateway import _estimate_input_tokens
    config, calls = model
    checks = []
    result, metadata = config.complete_structured([{"role": "user", "content": "JSON please"}],
        response_model=Answer, max_tokens=128, validate_current=lambda: checks.append(True))
    assert result.answer == "ok"
    assert metadata == {"model": "deepseek-flash", "configuration_revision": 1,
                        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                        "context_budget": {"window": 16000, "reserve": 128,
                            "estimated_input_tokens": _estimate_input_tokens(calls[0]["messages"])}}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 128
    assert len(checks) >= 3
    assert "test-private-value" not in str(metadata)


def test_structured_generation_revocation_blocks_actual_wire(model):
    config, calls = model
    def revoked():
        raise RecognitionConflict("revoked")
    with pytest.raises(RecognitionConflict):
        config.complete_structured([{"role": "user", "content": "JSON"}],
            response_model=Answer, validate_current=revoked)
    assert calls == []


def test_structured_generation_sanitizes_provider_failure(model):
    config, _ = model
    def broken(**request):
        raise RuntimeError("private prompt test-private-value")
    config._completion_fn = broken
    with pytest.raises(ModelConfigurationError) as failure:
        config.complete_structured([{"role": "user", "content": "JSON"}], response_model=Answer)
    assert "test-private-value" not in str(failure.value)
    assert "private prompt" not in str(failure.value)


def test_native_method_is_used_by_shared_generation_adapter(model):
    from backend.memory_app.structured_generation import generate_structured
    config, calls = model
    result, metadata = generate_structured(config, [{"role": "user", "content": "JSON"}],
        response_model=Answer, max_tokens=90, validate_current=lambda: None)
    assert result.answer == "ok" and metadata["usage"]["total_tokens"] == 5
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_workbench_ask_reaches_native_structured_transport(workbench_env):
    from fastapi.testclient import TestClient
    env = workbench_env
    calls = []
    def completion(**request):
        calls.append(request)
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"answer": "Native answer", "citations": [1]})}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    native = ModelConfiguration(env.records, env.root, InMemorySecretStore(), completion_fn=completion)
    native.update("generation", {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
        "api_key": "test-private-value", "allow_remote": True, "expected_revision": 0})
    publish(env)
    app, _ = assemble(env.root, env.records, env.documents, env.service, native)
    with TestClient(app) as http:
        response = http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "alpha?"})
    assert response.status_code == 200
    assert response.json()["turn"]["receipt"]["ask"]["answer"] == "Native answer"
    assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("payload", [{"answer": "ok", "citations": [True]}, {"answer": "", "citations": []},
    {"answer": "ok", "citations": [0]}])
def test_answer_schema_keeps_strict_citation_and_nonempty_answer_rules(payload):
    from backend.memory_app.structured_generation import AskOutput
    with pytest.raises(ValidationError):
        AskOutput.model_validate(payload)


@pytest.mark.parametrize("payload", [{"insights": [{"text": "ok", "conditions": [None]}]},
    {"insights": [{"text": " ", "conditions": []}]}, {"insights": [], "extra": True}])
def test_insight_schema_keeps_original_shape_rules(payload):
    from backend.memory_app.structured_generation import InsightOutput
    with pytest.raises(ValidationError):
        InsightOutput.model_validate(payload)
