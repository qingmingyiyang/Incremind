from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError


class ModelRequestRepositoryError(ValueError):
    """Raised when Model Request persistence would violate local gateway rules."""


class ModelResultRepositoryError(ValueError):
    """Raised when Model Result persistence would violate local gateway rules."""


_SKILL_RESOLUTION_ID = re.compile(r"^skill-resolution-[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class ObjectStoreModelRequestRepository:
    """Persist Model Request contracts without invoking any provider."""

    object_store: ObjectStorePort
    request_collection: str = "model_requests"

    def save_request(self, request: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(request)
        _validate_model_request(payload)
        request_id = _required_string(payload, "id")
        try:
            self.object_store.write(self.request_collection, request_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            existing = self.object_store.read(self.request_collection, request_id)
            if isinstance(existing, Mapping) and dict(existing) == payload:
                return dict(existing)
            raise ModelRequestRepositoryError(
                f"model request identity conflict: {request_id}"
            ) from exc
        return payload

    def get_request(self, request_id: str) -> Mapping[str, object] | None:
        stored = self.object_store.read(self.request_collection, request_id)
        return dict(stored) if stored is not None else None

    def list_requests(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.object_store.list(self.request_collection)
            if item.get("project_id") == project_id
        )


@dataclass(frozen=True, slots=True)
class ObjectStoreModelResultRepository:
    """Persist Model Result contracts without invoking any provider."""

    object_store: ObjectStorePort
    request_collection: str = "model_requests"
    result_collection: str = "model_results"

    def create_completed_local_result(
        self,
        *,
        request_id: str,
        output_text: str,
        input_tokens: int,
        output_tokens: int,
        provider_id: str = "local-completion-smoke",
        model_name: str = "local-text-generation-smoke",
        model_version: str = "2026-06-30",
        provider_config_version: int = 1,
        started_at: str | None = None,
        completed_at: str | None = None,
        elapsed_ms: int = 0,
    ) -> Mapping[str, object]:
        request = self.object_store.read(self.request_collection, request_id)
        if request is None:
            raise ModelResultRepositoryError(f"model request not found: {request_id}")
        if not isinstance(output_text, str) or not output_text.strip():
            raise ModelResultRepositoryError("completed result requires output text")
        if input_tokens < 0 or output_tokens <= 0:
            raise ModelResultRepositoryError("completed result requires non-negative input and positive output tokens")
        timestamp = completed_at or _utc_now()
        start_timestamp = started_at or timestamp
        result = {
            "schema_version": "1.0.0",
            "id": _compact_id("model-result-completed-local", request_id, output_text),
            "request_id": request_id,
            "status": "completed",
            "provider": {
                "provider_id": provider_id,
                "mode": "local",
                "remote": False,
                "config_version": provider_config_version,
            },
            "model": {
                "name": model_name,
                "version": model_version,
                "capability": "text_generation",
            },
            "output": {
                "kind": "text",
                "content": output_text,
                "structured": None,
                "output_refs": [],
            },
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost_usd": 0,
                "currency": "none",
            },
            "latency": {
                "started_at": start_timestamp,
                "completed_at": timestamp,
                "elapsed_ms": elapsed_ms,
                "timed_out": False,
            },
            "error": None,
            "safety": {
                "blocked": False,
                "categories": [],
                "redaction_applied": False,
                "output_truncated": False,
            },
            "created_at": timestamp,
        }
        return self.save_result(result)

    def create_privacy_blocked_result(
        self,
        *,
        request_id: str,
        message: str = "隐私策略阻止本次模型调用。",
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        request = self.object_store.read(self.request_collection, request_id)
        if request is None:
            raise ModelResultRepositoryError(f"model request not found: {request_id}")
        timestamp = created_at or _utc_now()
        result = {
            "schema_version": "1.0.0",
            "id": _compact_id("model-result-privacy-blocked", request_id, message),
            "request_id": request_id,
            "status": "privacy_blocked",
            "provider": {
                "provider_id": None,
                "mode": "none",
                "remote": False,
                "config_version": None,
            },
            "model": {
                "name": None,
                "version": None,
                "capability": "none",
            },
            "output": {
                "kind": "none",
                "content": None,
                "structured": None,
                "output_refs": [],
            },
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0,
                "currency": "none",
            },
            "latency": {
                "started_at": timestamp,
                "completed_at": timestamp,
                "elapsed_ms": 0,
                "timed_out": False,
            },
            "error": {
                "code": "privacy_blocked",
                "message": message,
                "retryable": False,
            },
            "safety": {
                "blocked": True,
                "categories": ["privacy"],
                "redaction_applied": False,
                "output_truncated": False,
            },
            "created_at": timestamp,
        }
        return self.save_result(result)

    def create_safety_blocked_result(
        self,
        *,
        request_id: str,
        categories: Sequence[str] = ("policy",),
        message: str = "安全策略阻止本次模型输出。",
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        request = self.object_store.read(self.request_collection, request_id)
        if request is None:
            raise ModelResultRepositoryError(f"model request not found: {request_id}")
        safe_categories = _safety_categories(categories)
        timestamp = created_at or _utc_now()
        result = {
            "schema_version": "1.0.0",
            "id": _compact_id("model-result-safety-blocked", request_id, message, *safe_categories),
            "request_id": request_id,
            "status": "safety_blocked",
            "provider": {
                "provider_id": None,
                "mode": "none",
                "remote": False,
                "config_version": None,
            },
            "model": {
                "name": None,
                "version": None,
                "capability": "none",
            },
            "output": {
                "kind": "none",
                "content": None,
                "structured": None,
                "output_refs": [],
            },
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0,
                "currency": "none",
            },
            "latency": {
                "started_at": timestamp,
                "completed_at": timestamp,
                "elapsed_ms": 0,
                "timed_out": False,
            },
            "error": {
                "code": "safety_blocked",
                "message": message,
                "retryable": False,
            },
            "safety": {
                "blocked": True,
                "categories": safe_categories,
                "redaction_applied": False,
                "output_truncated": False,
            },
            "created_at": timestamp,
        }
        return self.save_result(result)

    def save_result(self, result: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(result)
        _validate_model_result(payload, request=self.object_store.read(self.request_collection, _required_string_result(payload, "request_id")))
        result_id = _required_string_result(payload, "id")
        try:
            self.object_store.write(self.result_collection, result_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise ModelResultRepositoryError(f"model result already exists: {result_id}") from exc
        return payload

    def get_result(self, result_id: str) -> Mapping[str, object] | None:
        stored = self.object_store.read(self.result_collection, result_id)
        return dict(stored) if stored is not None else None

    def list_results(self, request_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.object_store.list(self.result_collection)
            if item.get("request_id") == request_id
        )


def _validate_model_request(request: Mapping[str, object]) -> None:
    if request.get("capability") != "text_generation":
        raise ModelRequestRepositoryError("model request must use text_generation capability")
    provider = request.get("provider_preference")
    if not isinstance(provider, Mapping):
        raise ModelRequestRepositoryError("model request requires provider_preference")
    if provider.get("mode") != "local_only" or provider.get("allow_remote") is not False:
        raise ModelRequestRepositoryError("model request must be local_only and disallow remote")
    payload = request.get("payload")
    if not isinstance(payload, Mapping):
        raise ModelRequestRepositoryError("model request requires payload")
    kind = payload.get("kind")
    if kind not in {"answer", "document_draft"}:
        raise ModelRequestRepositoryError("model request payload kind is unsupported")
    input_refs = payload.get("input_refs")
    if not isinstance(input_refs, Sequence) or isinstance(input_refs, (str, bytes)) or not input_refs:
        raise ModelRequestRepositoryError("model request requires input_refs")
    if kind == "answer":
        _validate_answer_input_refs(payload, input_refs)
    else:
        _validate_document_input_refs(request, payload, input_refs)
    skill_refs = [
        ref
        for ref in input_refs
        if isinstance(ref, Mapping) and ref.get("kind") == "application_skill_resolution"
    ]
    if len(skill_refs) > 1:
        raise ModelRequestRepositoryError("model request accepts at most one Application Skill resolution")
    if skill_refs:
        skill_ref = skill_refs[0]
        resolution_id = skill_ref.get("object_id")
        uri = skill_ref.get("uri")
        if (
            not isinstance(resolution_id, str)
            or not _SKILL_RESOLUTION_ID.fullmatch(resolution_id)
            or not isinstance(uri, str)
            or not uri.endswith(f"/{resolution_id}.json")
        ):
            raise ModelRequestRepositoryError("model request Application Skill resolution ref drifted")
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)) or not source_refs:
        raise ModelRequestRepositoryError("model request requires source_refs")
    privacy = request.get("privacy")
    if not isinstance(privacy, Mapping):
        raise ModelRequestRepositoryError("model request requires privacy")
    if privacy.get("scope") != "local_only" or privacy.get("allow_remote") is not False:
        raise ModelRequestRepositoryError("local model request must keep privacy local_only")
    response_schema = request.get("response_schema")
    if not isinstance(response_schema, Mapping) or response_schema.get("type") != "text":
        raise ModelRequestRepositoryError("model request must request text response")


def _validate_answer_input_refs(
    payload: Mapping[str, object],
    input_refs: Sequence[object],
) -> None:
    if not _required_string(payload, "recall_result_id"):
        raise ModelRequestRepositoryError("answer model request requires recall_result_id")
    if not any(isinstance(ref, Mapping) and ref.get("kind") == "recall_result" for ref in input_refs):
        raise ModelRequestRepositoryError("answer model request must reference the Recall Result")


def _validate_document_input_refs(
    request: Mapping[str, object],
    payload: Mapping[str, object],
    input_refs: Sequence[object],
) -> None:
    if payload.get("recall_result_id") is not None:
        raise ModelRequestRepositoryError("document model request must not claim a Recall Result")
    project_id = _required_string(request, "project_id")
    project_refs = [
        ref for ref in input_refs if isinstance(ref, Mapping) and ref.get("kind") == "project_skill"
    ]
    if len(project_refs) != 1:
        raise ModelRequestRepositoryError("document model request requires exactly one Project Skill ref")
    project_ref = project_refs[0]
    uri = project_ref.get("uri")
    if not isinstance(uri, str) or f"/projects/{project_id}/project-skill.json" not in uri:
        raise ModelRequestRepositoryError("document model request Project Skill ref drifted")


def _validate_model_result(result: Mapping[str, object], *, request: Mapping[str, object] | None) -> None:
    if request is None:
        raise ModelResultRepositoryError("model result requires an existing request")
    status = result.get("status")
    if status == "completed":
        _validate_completed_local_model_result(result, request=request)
        return
    if status == "privacy_blocked":
        _validate_blocked_model_result(result, status="privacy_blocked", required_category="privacy")
        return
    if status == "safety_blocked":
        _validate_blocked_model_result(result, status="safety_blocked", required_category=None)
        return
    raise ModelResultRepositoryError(
        "local model result repository accepts completed, privacy_blocked or safety_blocked results only"
    )


def _validate_blocked_model_result(
    result: Mapping[str, object],
    *,
    status: str,
    required_category: str | None,
) -> None:
    error = result.get("error")
    if not isinstance(error, Mapping) or error.get("code") != status:
        raise ModelResultRepositoryError(f"{status} result requires matching error")
    provider = result.get("provider")
    if not isinstance(provider, Mapping):
        raise ModelResultRepositoryError("model result requires provider")
    if provider.get("mode") != "none" or provider.get("provider_id") is not None or provider.get("config_version") is not None:
        raise ModelResultRepositoryError(f"{status} result must not reach provider")
    if provider.get("remote") is not False:
        raise ModelResultRepositoryError(f"{status} result must not be remote")
    model = result.get("model")
    if not isinstance(model, Mapping) or model.get("capability") != "none":
        raise ModelResultRepositoryError(f"{status} result must not include model capability")
    if model.get("name") is not None or model.get("version") is not None:
        raise ModelResultRepositoryError(f"{status} result must not include model identity")
    output = result.get("output")
    if not isinstance(output, Mapping) or output.get("kind") != "none":
        raise ModelResultRepositoryError(f"{status} result must not include generated output")
    if output.get("content") is not None or output.get("structured") is not None or output.get("output_refs") != []:
        raise ModelResultRepositoryError(f"{status} result output must be empty")
    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        raise ModelResultRepositoryError("model result requires usage")
    if any(usage.get(key) != 0 for key in ("input_tokens", "output_tokens", "total_tokens", "cost_usd")):
        raise ModelResultRepositoryError(f"{status} result must not spend tokens or cost")
    if usage.get("currency") != "none":
        raise ModelResultRepositoryError(f"{status} result currency must be none")
    safety = result.get("safety")
    if not isinstance(safety, Mapping) or safety.get("blocked") is not True:
        raise ModelResultRepositoryError(f"{status} result must be safety blocked")
    categories = safety.get("categories")
    if not isinstance(categories, Sequence) or isinstance(categories, (str, bytes)) or not categories:
        raise ModelResultRepositoryError(f"{status} result requires safety categories")
    if not all(isinstance(category, str) and category for category in categories):
        raise ModelResultRepositoryError(f"{status} result safety categories must be strings")
    if required_category is not None and required_category not in categories:
        raise ModelResultRepositoryError(f"{status} result requires {required_category} safety category")
    if status == "safety_blocked" and set(categories) == {"privacy"}:
        raise ModelResultRepositoryError("safety_blocked result requires a non-privacy safety category")


def _validate_completed_local_model_result(
    result: Mapping[str, object],
    *,
    request: Mapping[str, object],
) -> None:
    if request.get("capability") != "text_generation":
        raise ModelResultRepositoryError("completed local result requires text_generation request")
    provider_preference = request.get("provider_preference")
    if not isinstance(provider_preference, Mapping):
        raise ModelResultRepositoryError("completed local result requires provider preference")
    if provider_preference.get("mode") != "local_only" or provider_preference.get("allow_remote") is not False:
        raise ModelResultRepositoryError("completed local result requires local_only request")
    if result.get("error") is not None:
        raise ModelResultRepositoryError("completed result must not include error")
    provider = result.get("provider")
    if not isinstance(provider, Mapping):
        raise ModelResultRepositoryError("completed result requires provider")
    if provider.get("mode") != "local" or provider.get("remote") is not False:
        raise ModelResultRepositoryError("completed result must stay on local provider")
    if not isinstance(provider.get("provider_id"), str) or not provider["provider_id"]:
        raise ModelResultRepositoryError("completed local result requires provider id")
    if not isinstance(provider.get("config_version"), int):
        raise ModelResultRepositoryError("completed local result requires provider config version")
    model = result.get("model")
    if not isinstance(model, Mapping):
        raise ModelResultRepositoryError("completed result requires model")
    if model.get("capability") != "text_generation":
        raise ModelResultRepositoryError("completed local result requires text_generation model")
    if not isinstance(model.get("name"), str) or not model["name"]:
        raise ModelResultRepositoryError("completed local result requires model name")
    if not isinstance(model.get("version"), str) or not model["version"]:
        raise ModelResultRepositoryError("completed local result requires model version")
    output = result.get("output")
    if not isinstance(output, Mapping) or output.get("kind") != "text":
        raise ModelResultRepositoryError("completed local result requires text output")
    if not isinstance(output.get("content"), str) or not output["content"]:
        raise ModelResultRepositoryError("completed local result requires output content")
    if output.get("structured") is not None:
        raise ModelResultRepositoryError("completed text result must not include structured output")
    output_refs = output.get("output_refs")
    if not isinstance(output_refs, Sequence) or isinstance(output_refs, (str, bytes)):
        raise ModelResultRepositoryError("completed result output refs must be a list")
    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        raise ModelResultRepositoryError("completed result requires usage")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if not all(isinstance(value, int) for value in (input_tokens, output_tokens, total_tokens)):
        raise ModelResultRepositoryError("completed result usage tokens must be integers")
    if input_tokens < 0 or output_tokens <= 0:
        raise ModelResultRepositoryError("completed result requires non-negative input and positive output tokens")
    if input_tokens + output_tokens != total_tokens:
        raise ModelResultRepositoryError("completed result total tokens must match input plus output")
    budget = request.get("budget")
    if isinstance(budget, Mapping):
        if isinstance(budget.get("max_input_tokens"), int) and input_tokens > budget["max_input_tokens"]:
            raise ModelResultRepositoryError("completed result exceeds input token budget")
        if isinstance(budget.get("max_output_tokens"), int) and output_tokens > budget["max_output_tokens"]:
            raise ModelResultRepositoryError("completed result exceeds output token budget")
        if isinstance(budget.get("max_total_tokens"), int) and total_tokens > budget["max_total_tokens"]:
            raise ModelResultRepositoryError("completed result exceeds total token budget")
        max_cost = budget.get("max_cost_usd")
        cost = usage.get("cost_usd")
        if isinstance(max_cost, (int, float)) and isinstance(cost, (int, float)) and cost > max_cost:
            raise ModelResultRepositoryError("completed result exceeds cost budget")
    if usage.get("currency") != "none" or usage.get("cost_usd") != 0:
        raise ModelResultRepositoryError("completed local result must not record provider cost")
    latency = result.get("latency")
    if not isinstance(latency, Mapping):
        raise ModelResultRepositoryError("completed result requires latency")
    if latency.get("completed_at") is None or latency.get("timed_out") is not False:
        raise ModelResultRepositoryError("completed result requires completed non-timeout latency")
    if not isinstance(latency.get("elapsed_ms"), int) or latency["elapsed_ms"] < 0:
        raise ModelResultRepositoryError("completed result requires non-negative elapsed_ms")
    safety = result.get("safety")
    if not isinstance(safety, Mapping) or safety.get("blocked") is not False:
        raise ModelResultRepositoryError("completed result must not be safety blocked")
    if safety.get("categories") != []:
        raise ModelResultRepositoryError("completed result must not include safety categories")


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ModelRequestRepositoryError(f"model request requires {key}")
    return value


def _required_string_result(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ModelResultRepositoryError(f"model result requires {key}")
    return value


def _compact_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safety_categories(categories: Sequence[str]) -> list[str]:
    allowed = {"policy", "malware", "self_harm", "violence", "unknown"}
    result: list[str] = []
    for category in categories:
        if not isinstance(category, str) or not category:
            raise ModelResultRepositoryError("safety_blocked result safety categories must be strings")
        if category not in allowed:
            raise ModelResultRepositoryError("safety_blocked result requires a non-privacy safety category")
        if category not in result:
            result.append(category)
    if not result:
        raise ModelResultRepositoryError("safety_blocked result requires safety categories")
    return result
