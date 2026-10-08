from __future__ import annotations

import pytest

from core.effect_log import (
    EffectClass,
    EffectLog,
    EffectIntent,
    EffectPurpose,
    EffectReaper,
    EffectRunner,
    EffectState,
    EffectWorkflowHandler,
)
from core.storage_provider import JsonObjectStore


def _execute(handler: EffectWorkflowHandler, invoke):
    return handler.execute(
        operation_id="workflow:test:step-1",
        session_id="workflow:test",
        root_id="workflow:test",
        step_key="step-1",
        kind="test_workflow_step",
        intent_ref="crp://test/workflow-intents/step-1",
        gate_decision_id="test-workflow-gate:v1",
        rev_set={"workflow_revision": "1", "handler_revision": "1"},
        payload={"input_ref": "crp://test/input/1"},
        effect_class=EffectClass.IDEMPOTENT,
        invoke=invoke,
        encode=lambda value: value,
        decode=dict,
    )


def test_workflow_handler_replays_durable_receipt_without_reinvocation(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    receipts = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "legacy")
    handler = EffectWorkflowHandler(
        EffectRunner(log, owner_id="workflow-runner"), receipts, namespace_id="test",
    )
    calls: list[int] = []

    first = _execute(handler, lambda: calls.append(1) or {"status": "completed"})
    replay = _execute(handler, lambda: calls.append(2) or {"status": "drift"})

    assert first == replay == {"status": "completed"}
    assert calls == [1]
    effect = log.get("workflow:test:step-1")
    assert effect.state is EffectState.SETTLED_OK
    assert effect.result_ref == "crp://test/workflow-effect-receipts/workflow:test:step-1"


def test_workflow_handler_failure_waits_for_core_reaper_before_retry(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    receipts = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "legacy")
    handler = EffectWorkflowHandler(
        EffectRunner(log, owner_id="workflow-runner", lease_seconds=1),
        receipts,
        namespace_id="test",
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        _execute(handler, lambda: (_ for _ in ()).throw(RuntimeError("provider failed")))

    assert log.get("workflow:test:step-1").state is EffectState.INFLIGHT
    EffectReaper(log).recover_expired(now=2_000_000_000)
    result = _execute(handler, lambda: {"status": "completed"})

    assert result == {"status": "completed"}
    assert log.get("workflow:test:step-1").attempt == 2


def test_at_most_once_workflow_without_receipt_verifies_unknown(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    receipts = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "legacy")
    runner = EffectRunner(log, owner_id="workflow-runner")
    handler = EffectWorkflowHandler(runner, receipts, namespace_id="test")
    effect, _ = log.plan(EffectIntent(
        session_id="download:1", root_id="download:1", step_key="download",
        kind="bilibili_authorized_download", effect_class=EffectClass.AT_MOST_ONCE,
        purpose=EffectPurpose.PRIMARY, intent_ref="crp://test/intent/1",
        gate_decision_id="test:v1", rev_set={"provider_revision": "1"},
        payload={}, idem_key="download:1", operation_id_override="download:1",
    ), now=1)
    effect, claimed = runner.claim_planned(effect.operation_id, now=1)

    assert claimed is True
    assert handler.verify_effect(effect) == (
        EffectState.UNKNOWN,
        "workflow.at_most_once_receipt_missing",
    )


def test_receipt_saved_before_effect_settle_is_recovered_without_reinvocation(
    tmp_path, monkeypatch,
) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    receipts = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "legacy")
    runner = EffectRunner(log, owner_id="workflow-runner", lease_seconds=1)
    handler = EffectWorkflowHandler(runner, receipts, namespace_id="test")
    original_settle = runner.settle_ok
    calls: list[int] = []

    monkeypatch.setattr(
        runner,
        "settle_ok",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash before settle")),
    )
    with pytest.raises(RuntimeError, match="crash before settle"):
        _execute(handler, lambda: calls.append(1) or {"status": "completed"})

    effect = log.get("workflow:test:step-1")
    assert effect.state is EffectState.INFLIGHT
    monkeypatch.setattr(runner, "settle_ok", original_settle)
    recovered = EffectReaper(log).recover_expired(
        now=2_000_000_000,
        verifiers={"test_workflow_step": handler.verify_effect},
    )

    assert recovered[0].state is EffectState.SETTLED_OK
    assert _execute(handler, lambda: calls.append(2) or {"status": "drift"}) == {
        "status": "completed"
    }
    assert calls == [1]
