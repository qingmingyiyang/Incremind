from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.api import series_intake_ai_runtime as runtime
from backend.replay.contracts import CreateIntakeRequest, UpdateIntakeRequest
from backend.replay.series_workspace import SeriesWorkspace
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.model_gateway import ModelResult


class _Payloads:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def put(self, turn_id: str, kind: str, payload: object) -> str:
        ref = f"crp://default/receipts/{turn_id}/{kind}"
        self.values[ref] = payload
        return ref


class _Nested:
    def __init__(self) -> None:
        self.finalized: list[str | None] = []

    def model_call_routed(self, **_kwargs) -> None: pass
    def model_call_started(self, **_kwargs) -> None: pass
    def model_call_completed(self, **_kwargs) -> None: pass
    def model_call_failed(self) -> None: pass
    def model_call_cache_observed(self, **_kwargs) -> None: pass

    def finalize(self, *, error_code: str | None) -> tuple[str, ...]:
        self.finalized.append(error_code)
        return ("crp://default/model-evidence/model-1",)


class _Control:
    remaining_timeout_ms = 30_000
    cancel_requested = False

    def __init__(self) -> None:
        self.nested = _Nested()

    def checkpoint(self) -> None: pass
    def take_nested_model_handle(self, **_kwargs) -> _Nested: return self.nested


class _Gateway:
    def __init__(self, output: object, before_return=None) -> None:
        self.output = output
        self.before_return = before_return
        self.calls = 0
        self.requests = []

    def invoke(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.before_return is not None:
            self.before_return()
        return ModelResult(self.output, "provider-a", "model-a", {"total_tokens": 8})


def _output() -> dict[str, object]:
    return {
        "title": "整理标题", "structured_text": "## 完成事项\n\n- 已完成\n\n## 问题记录\n\n无\n\n## 后续计划\n\n- 复核",
        "summary": "整理摘要", "tags": ["测试"], "suggested_actions": ["复核"],
        "suggested_report_type": "daily",
    }


def _request(item, *, project_id: str = "project-a") -> dict[str, object]:
    authority = {
        "kind": "project_series_scope_v1", "object_id": "series-authority-a",
        "payload_revision": 1, "storage_revision": 1,
        "authority_identity": "sqlite:structured-records-v1",
        "authority_ref": "crp://default/memory/series/series-authority-a",
    }
    snapshot = {
        "kind": runtime.SERIES_INTAKE_ORGANIZE_SNAPSHOT_KIND,
        "series_id": item.series_id, "project_id": project_id, "authority": authority,
        "intake_id": item.intake_id, "intake": item.model_dump(mode="json"),
        "expected_revision": item.revision,
        "prompt_version": "replay-intake-organizer-v3",
    }
    return {
        "turn_id": "turn-a", "operation_id": "op-series-intake-a",
        "desired_outcome": runtime.SERIES_INTAKE_ORGANIZE_OUTCOME,
        "input": {"text": json.dumps(snapshot, ensure_ascii=False)},
        "arguments": {"snapshot": snapshot},
        "scope": {"kind": "series", "project_id": project_id, "series_id": item.series_id, "authority": authority},
        "privacy": {"allow_remote": True}, "execution_context": _Control(),
    }


def _capability(monkeypatch, tmp_path, gateway):
    payloads = _Payloads()
    workspace = SeriesWorkspace(tmp_path)
    monkeypatch.setattr(
        runtime, "load_turn_model_routing_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            snapshot_ref="crp://default/model-routing/snapshot-a", snapshot_revision="revision-a",
            parameters=lambda: {"_routing": "frozen"},
        ),
    )
    return workspace, payloads, runtime.SeriesIntakeOrganizeCommitCapability(
        workspace=workspace, gateway=gateway, payloads=payloads, namespace_id="default",
    )


def test_commit_uses_frozen_snapshot_nested_model_and_creates_safe_receipt(monkeypatch, tmp_path) -> None:
    gateway = _Gateway(_output())
    workspace, payloads, capability = _capability(monkeypatch, tmp_path, gateway)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="正文含 sk-secret-value 但不能进入 Receipt"))
    request = _request(item)

    result = capability.invoke(request)

    saved = workspace.get_intake("default", item.intake_id)
    assert saved.status == "reviewing" and saved.title == "整理标题"
    assert gateway.calls == 1
    assert gateway.requests[0].metadata_sink is request["execution_context"].nested
    assert gateway.requests[0].parameters["_routing"] == "frozen"
    receipt = payloads.values[result["receipt_ref"]]
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert "sk-secret-value" not in serialized
    assert "raw_text" not in serialized and "path" not in serialized.lower()
    assert result["evidence_refs"][-1] == "crp://default/model-evidence/model-1"


def test_commit_rejects_scope_authority_drift_before_model(monkeypatch, tmp_path) -> None:
    gateway = _Gateway(_output())
    workspace, _payloads, capability = _capability(monkeypatch, tmp_path, gateway)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="正文"))
    request = _request(item)
    request["scope"] = {**request["scope"], "project_id": "project-b"}

    with pytest.raises(ValueError, match="scope drifted"):
        capability.invoke(request)
    assert gateway.calls == 0


def test_commit_conflict_never_overwrites_current_intake(monkeypatch, tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="旧正文"))
    gateway = _Gateway(_output(), before_return=lambda: workspace.update_intake(
        "default", item.intake_id, UpdateIntakeRequest(raw_text="用户并发编辑"),
    ))
    _workspace, _payloads, capability = _capability(monkeypatch, tmp_path, gateway)
    # The capability's workspace must be the same authority observed by the provider hook.
    capability._workspace = workspace  # noqa: SLF001

    with pytest.raises(ToolProviderFailure) as failure:
        capability.invoke(_request(item))
    assert failure.value.effect_certainty == "confirmed_none"
    assert workspace.get_intake("default", item.intake_id).raw_text == "用户并发编辑"


def test_prepared_operation_replays_without_second_model_call(monkeypatch, tmp_path) -> None:
    gateway = _Gateway(_output())
    workspace, _payloads, capability = _capability(monkeypatch, tmp_path, gateway)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="正文"))
    request = _request(item)
    first = capability.invoke(request)
    operation_path = next(workspace.series_path("default").glob("intake/operations/*.json"))
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    operation["state"] = "prepared"
    operation.pop("finalized_at", None)
    operation_path.write_text(json.dumps(operation, ensure_ascii=False), encoding="utf-8")

    second = capability.invoke(request)

    assert gateway.calls == 1
    assert second["result"]["content"]["replayed"] is True
    assert first["result"]["content"]["intake_revision"] == second["result"]["content"]["intake_revision"]


def test_planner_selects_one_write_capability_then_completes(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="正文"))
    request = _request(item)
    planner = runtime.SeriesIntakeOrganizeTurnPlanner()

    decision = planner.plan(request, [], [], _Payloads())

    assert decision["type"] == "tool"
    assert decision["capability_id"] == runtime.SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY
    completed = planner.plan(request, [{"type": "tool.completed", "data": {"capability_id": runtime.SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY, "evidence_refs": []}}], [], _Payloads())
    assert completed["type"] == "complete"


def test_snapshot_allows_model_visible_asset_projection_with_storage_baseline(tmp_path) -> None:
    workspace = SeriesWorkspace(tmp_path)
    item = workspace.create_intake("default", CreateIntakeRequest(raw_text="正文"))
    request = _request(item)
    projected = item.model_copy(update={"asset_text": "附件提取文本"})
    request["arguments"]["snapshot"]["intake"] = projected.model_dump(mode="json")
    request["input"]["text"] = json.dumps(request["arguments"]["snapshot"], ensure_ascii=False)

    loaded = runtime.load_series_intake_organize_snapshot(request)

    assert loaded["expected_revision"] == item.revision
    assert loaded["intake"]["asset_text"] == "附件提取文本"
