from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from core.composition import build_model_result_repository
from core.model_gateway import (
    ModelRequestRepositoryError,
    ModelResultRepositoryError,
    ObjectStoreModelRequestRepository,
    ObjectStoreModelResultRepository,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _answer_model_request(request_id: str = "model-request-alpha") -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": request_id,
        "project_id": "project-alpha",
        "capability": "text_generation",
        "provider_preference": {
            "mode": "local_only",
            "provider": None,
            "model": None,
            "allow_remote": False,
            "config_version": 1,
        },
        "payload": {
            "kind": "answer",
            "content": "请只根据证据回答。",
            "input_refs": [
                {
                    "kind": "recall_result",
                    "object_id": "recall-result-alpha",
                    "uri": "crp://default/recall-results/recall-result-alpha.json",
                }
            ],
            "source_refs": [
                {
                    "source_id": "source-alpha",
                    "locator": "char:0-20",
                }
            ],
            "recall_result_id": "recall-result-alpha",
        },
        "privacy": {
            "scope": "local_only",
            "pii": "possible",
            "allow_remote": False,
            "redaction": {
                "applied": False,
                "strategy": "none",
            },
            "retention": "none",
        },
        "timeout": {
            "request_timeout_ms": 60000,
            "idle_timeout_ms": 10000,
            "deadline_at": None,
        },
        "cancel": {
            "cancellable": True,
            "cancel_token": "cancel-model-request-alpha",
            "requested": False,
        },
        "budget": {
            "max_input_tokens": 12000,
            "max_output_tokens": 2048,
            "max_total_tokens": 14048,
            "max_cost_usd": 0,
        },
        "response_schema": {
            "type": "text",
            "json_schema_uri": None,
            "strict": False,
        },
        "created_at": "2026-06-30T12:00:00+08:00",
    }


def test_model_request_repository_accepts_one_exact_skill_resolution_ref(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    resolution_id = "skill-resolution-" + ("a" * 32)
    valid = _answer_model_request("model-request-with-skill")
    valid["payload"]["input_refs"].append(
        {
            "kind": "application_skill_resolution",
            "object_id": resolution_id,
            "uri": f"crp://default/application-skill-resolutions/{resolution_id}.json",
        }
    )

    saved = requests.save_request(valid)

    assert validate_contract_instance(
        "model_request.schema.json", _schema("model_request.schema.json"), saved
    ) == []
    duplicate = deepcopy(valid)
    duplicate["id"] = "model-request-duplicate-skill-ref"
    duplicate["payload"]["input_refs"].append(dict(duplicate["payload"]["input_refs"][-1]))
    drifted = deepcopy(valid)
    drifted["id"] = "model-request-drifted-skill-ref"
    drifted["payload"]["input_refs"][-1]["uri"] = (
        "crp://default/application-skill-resolutions/other.json"
    )
    with pytest.raises(ModelRequestRepositoryError, match="at most one"):
        requests.save_request(duplicate)
    with pytest.raises(ModelRequestRepositoryError, match="resolution ref drifted"):
        requests.save_request(drifted)


def test_model_request_repository_replays_only_an_exact_existing_payload(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    original = _answer_model_request("model-request-exact-replay")

    saved = requests.save_request(original)
    replayed = requests.save_request(deepcopy(original))

    assert replayed == saved
    assert object_store.revision("model_requests", "model-request-exact-replay") == 1

    conflicting = deepcopy(original)
    conflicting["payload"]["content"] = "试图以相同 ID 改写请求。"
    with pytest.raises(ModelRequestRepositoryError, match="identity conflict"):
        requests.save_request(conflicting)

    assert requests.get_request("model-request-exact-replay") == saved
    assert object_store.revision("model_requests", "model-request-exact-replay") == 1


def test_model_result_repository_creates_privacy_blocked_without_provider_call(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    request = requests.save_request(_answer_model_request())

    result = dict(
        results.create_privacy_blocked_result(
            request_id=str(request["id"]),
            message="隐私策略阻止本次模型调用。",
            created_at="2026-06-30T12:01:00+08:00",
        )
    )
    reloaded = ObjectStoreModelResultRepository(_store(tmp_path))

    assert validate_contract_instance("model_result.schema.json", _schema("model_result.schema.json"), result) == []
    assert result["status"] == "privacy_blocked"
    assert result["provider"] == {
        "provider_id": None,
        "mode": "none",
        "remote": False,
        "config_version": None,
    }
    assert result["model"] == {"name": None, "version": None, "capability": "none"}
    assert result["output"] == {"kind": "none", "content": None, "structured": None, "output_refs": []}
    assert result["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0,
        "currency": "none",
    }
    assert result["error"]["code"] == "privacy_blocked"
    assert result["safety"] == {
        "blocked": True,
        "categories": ["privacy"],
        "redaction_applied": False,
        "output_truncated": False,
    }
    assert reloaded.get_result(str(result["id"])) == result
    assert reloaded.list_results(str(request["id"])) == (result,)
    assert not (tmp_path / "library").exists()


def test_model_result_repository_creates_safety_blocked_without_output_or_provider(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    request = requests.save_request(_answer_model_request())

    result = dict(
        results.create_safety_blocked_result(
            request_id=str(request["id"]),
            categories=("policy", "violence"),
            message="安全策略阻止本次模型输出。",
            created_at="2026-06-30T12:02:00+08:00",
        )
    )
    reloaded = ObjectStoreModelResultRepository(_store(tmp_path))

    assert validate_contract_instance("model_result.schema.json", _schema("model_result.schema.json"), result) == []
    assert result["status"] == "safety_blocked"
    assert result["provider"] == {
        "provider_id": None,
        "mode": "none",
        "remote": False,
        "config_version": None,
    }
    assert result["model"] == {"name": None, "version": None, "capability": "none"}
    assert result["output"] == {"kind": "none", "content": None, "structured": None, "output_refs": []}
    assert result["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0,
        "currency": "none",
    }
    assert result["error"] == {
        "code": "safety_blocked",
        "message": "安全策略阻止本次模型输出。",
        "retryable": False,
    }
    assert result["safety"] == {
        "blocked": True,
        "categories": ["policy", "violence"],
        "redaction_applied": False,
        "output_truncated": False,
    }
    assert reloaded.get_result(str(result["id"])) == result
    assert reloaded.list_results(str(request["id"])) == (result,)
    assert not (tmp_path / "library").exists()


def test_model_result_repository_creates_completed_local_without_provider_adapter(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    request = requests.save_request(_answer_model_request())

    result = dict(
        results.create_completed_local_result(
            request_id=str(request["id"]),
            output_text="根据当前证据，应先保持本地模型结果持久化边界。",
            input_tokens=128,
            output_tokens=32,
            provider_id="local-completion-smoke",
            model_name="local-text-generation-smoke",
            model_version="2026-06-30",
            started_at="2026-06-30T12:03:00+08:00",
            completed_at="2026-06-30T12:03:01+08:00",
            elapsed_ms=1000,
        )
    )
    reloaded = ObjectStoreModelResultRepository(_store(tmp_path))

    assert validate_contract_instance("model_result.schema.json", _schema("model_result.schema.json"), result) == []
    assert result["status"] == "completed"
    assert result["provider"] == {
        "provider_id": "local-completion-smoke",
        "mode": "local",
        "remote": False,
        "config_version": 1,
    }
    assert result["model"] == {
        "name": "local-text-generation-smoke",
        "version": "2026-06-30",
        "capability": "text_generation",
    }
    assert result["output"] == {
        "kind": "text",
        "content": "根据当前证据，应先保持本地模型结果持久化边界。",
        "structured": None,
        "output_refs": [],
    }
    assert result["usage"] == {
        "input_tokens": 128,
        "output_tokens": 32,
        "total_tokens": 160,
        "cost_usd": 0,
        "currency": "none",
    }
    assert result["error"] is None
    assert result["safety"] == {
        "blocked": False,
        "categories": [],
        "redaction_applied": False,
        "output_truncated": False,
    }
    assert reloaded.get_result(str(result["id"])) == result
    assert reloaded.list_results(str(request["id"])) == (result,)
    assert not (tmp_path / "library").exists()


def test_model_result_repository_rejects_privacy_blocked_provider_or_missing_request(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    request = requests.save_request(_answer_model_request())
    result = dict(
        results.create_privacy_blocked_result(
            request_id=str(request["id"]),
            created_at="2026-06-30T12:01:00+08:00",
        )
    )
    invalid = dict(result)
    invalid["id"] = "model-result-invalid-provider"
    invalid["provider"] = {"provider_id": "local-llm", "mode": "local", "remote": False, "config_version": 1}

    with pytest.raises(ModelResultRepositoryError, match="must not reach provider"):
        results.save_result(invalid)
    with pytest.raises(ModelResultRepositoryError, match="not found"):
        results.create_privacy_blocked_result(request_id="model-request-missing")


def test_model_result_repository_rejects_safety_blocked_provider_or_privacy_only_category(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    request = requests.save_request(_answer_model_request())
    result = dict(
        results.create_safety_blocked_result(
            request_id=str(request["id"]),
            created_at="2026-06-30T12:02:00+08:00",
        )
    )
    invalid = dict(result)
    invalid["id"] = "model-result-invalid-safety-provider"
    invalid["provider"] = {"provider_id": "local-llm", "mode": "local", "remote": False, "config_version": 1}
    privacy_only = dict(result)
    privacy_only["id"] = "model-result-invalid-privacy-only-safety"
    privacy_only["safety"] = {
        "blocked": True,
        "categories": ["privacy"],
        "redaction_applied": False,
        "output_truncated": False,
    }

    with pytest.raises(ModelResultRepositoryError, match="must not reach provider"):
        results.save_result(invalid)
    with pytest.raises(ModelResultRepositoryError, match="non-privacy"):
        results.save_result(privacy_only)
    with pytest.raises(ModelResultRepositoryError, match="non-privacy"):
        results.create_safety_blocked_result(request_id=str(request["id"]), categories=("privacy",))
    with pytest.raises(ModelResultRepositoryError, match="not found"):
        results.create_safety_blocked_result(request_id="model-request-missing")


def test_model_result_repository_rejects_completed_remote_or_budget_overrun(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    request = requests.save_request(_answer_model_request())
    result = dict(
        results.create_completed_local_result(
            request_id=str(request["id"]),
            output_text="本地完成结果。",
            input_tokens=10,
            output_tokens=5,
            started_at="2026-06-30T12:04:00+08:00",
            completed_at="2026-06-30T12:04:01+08:00",
            elapsed_ms=1000,
        )
    )
    remote = dict(result)
    remote["id"] = "model-result-invalid-remote"
    remote["provider"] = {
        "provider_id": "remote-llm",
        "mode": "remote",
        "remote": True,
        "config_version": 1,
    }
    over_budget = dict(result)
    over_budget["id"] = "model-result-invalid-budget"
    over_budget["usage"] = {
        "input_tokens": 13000,
        "output_tokens": 5,
        "total_tokens": 13005,
        "cost_usd": 0,
        "currency": "none",
    }

    with pytest.raises(ModelResultRepositoryError, match="local provider"):
        results.save_result(remote)
    with pytest.raises(ModelResultRepositoryError, match="input token budget"):
        results.save_result(over_budget)
    with pytest.raises(ModelResultRepositoryError, match="not found"):
        results.create_completed_local_result(
            request_id="model-request-missing",
            output_text="missing",
            input_tokens=1,
            output_tokens=1,
        )


def test_model_result_repository_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    request = ObjectStoreModelRequestRepository(object_store).save_request(
        _answer_model_request("model-request-composition")
    )
    results = build_model_result_repository(ROOT, runtime_root=tmp_path)

    result = results.create_privacy_blocked_result(
        request_id=str(request["id"]),
        created_at="2026-06-30T12:02:00+08:00",
    )
    safety = results.create_safety_blocked_result(
        request_id=str(request["id"]),
        created_at="2026-06-30T12:02:30+08:00",
    )
    completed = results.create_completed_local_result(
        request_id=str(request["id"]),
        output_text="本地完成结果已保存。",
        input_tokens=20,
        output_tokens=6,
        started_at="2026-06-30T12:03:00+08:00",
        completed_at="2026-06-30T12:03:01+08:00",
        elapsed_ms=1000,
    )

    assert result["status"] == "privacy_blocked"
    assert result["provider"]["mode"] == "none"
    assert result["usage"]["total_tokens"] == 0
    assert safety["status"] == "safety_blocked"
    assert safety["provider"]["mode"] == "none"
    assert safety["usage"]["total_tokens"] == 0
    assert completed["status"] == "completed"
    assert completed["provider"]["mode"] == "local"
    assert completed["usage"]["total_tokens"] == 26
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "model_results").exists()
    assert not (tmp_path / "library").exists()
